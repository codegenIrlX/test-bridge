from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import OperationalError
from redis.exceptions import ConnectionError as RedisConnectionError

from metrics_srv.api import create_app
from metrics_srv.config import Settings


class FakeRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, dict] = {}

    def create_import(self, import_id: UUID, **metadata: str | None) -> dict | None:
        key = metadata.get("idempotency_key")
        if key:
            for row in self.rows.values():
                if row.get("idempotency_key") == key:
                    return row
        self.rows[import_id] = {
            "id": import_id,
            "status": "pending",
            "error_message": None,
            "archive_deleted": False,
            **metadata,
        }
        return self.rows[import_id] if metadata else None

    def get_import(self, import_id: UUID) -> dict | None:
        return self.rows.get(import_id)

    def retry_import(self, import_id: UUID) -> str:
        row = self.rows.get(import_id)
        if row is None:
            return "not_found"
        if row["status"] != "failed":
            return "conflict"
        row.update(status="pending", error_message=None, archive_deleted=False)
        return "pending"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_host="db",
        database_port=5432,
        database_name="tests",
        database_user="service",
        database_password="fake-password",
        redis_host="redis",
        redis_port=6379,
        storage_path=tmp_path,
        max_archive_bytes=128,
        max_uncompressed_bytes=4096,
        max_result_files=10,
        scheduler_interval_seconds=5,
        api_token="fake-token",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, FakeRepository()), raise_server_exceptions=False)


def test_upload_and_fetch_status(client: TestClient, settings: Settings) -> None:
    headers = {"Authorization": "Bearer fake-token"}
    payload = b"example-archive"

    response = client.post(
        "/api/v1/imports", headers=headers, files={"file": ("results.zip", payload, "application/zip")}
    )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "pending"
    import_id = UUID(data["importId"])
    assert (settings.storage_path / f"{import_id}.zip").read_bytes() == payload

    status = client.get(f"/api/v1/imports/{import_id}", headers=headers)
    assert status.status_code == 200
    assert status.json() == {
        "importId": str(import_id),
        "status": "pending",
        "errorMessage": None,
        "archiveDeleted": False,
    }


def test_upload_metadata_and_idempotent_retry(settings: Settings) -> None:
    repository = FakeRepository()
    client = TestClient(create_app(settings, repository), raise_server_exceptions=False)
    headers = {"Authorization": "Bearer fake-token", "Idempotency-Key": "build-42"}
    data = {"project": "project-a", "job": "job-a", "buildNumber": "42",
            "buildUrl": "https://ci.example/build/42", "revision": "abc123"}
    files = {"file": ("results.zip", b"archive")}

    first = client.post("/api/v1/imports", headers=headers, data=data, files=files)
    second = client.post("/api/v1/imports", headers=headers, data=data, files=files)

    assert first.status_code == second.status_code == 202
    assert first.json()["importId"] == second.json()["importId"]
    assert len(repository.rows) == 1
    row = next(iter(repository.rows.values()))
    assert (row["project"], row["job"], row["build_number"], row["build_url"], row["revision"]) == (
        "project-a", "job-a", "42", "https://ci.example/build/42", "abc123"
    )
    assert len(list(settings.storage_path.glob("*.zip"))) == 1


@pytest.mark.parametrize("changed", ["archive", "metadata"])
def test_idempotency_key_conflicts_do_not_create_imports(
    settings: Settings, changed: str
) -> None:
    repository = FakeRepository()
    client = TestClient(create_app(settings, repository), raise_server_exceptions=False)
    headers = {"Authorization": "Bearer fake-token", "Idempotency-Key": "build-42"}
    first = client.post(
        "/api/v1/imports", headers=headers, data={"project": "a"},
        files={"file": ("results.zip", b"archive")},
    )
    second = client.post(
        "/api/v1/imports", headers=headers,
        data={"project": "b" if changed == "metadata" else "a"},
        files={"file": ("results.zip", b"different" if changed == "archive" else b"archive")},
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert set(second.json()["error"]) == {"code", "message", "details"}
    assert len(repository.rows) == 1
    assert len(list(settings.storage_path.glob("*.zip"))) == 1


def test_idempotent_retry_returns_current_import_state(settings: Settings) -> None:
    repository = FakeRepository()
    client = TestClient(create_app(settings, repository), raise_server_exceptions=False)
    headers = {"Authorization": "Bearer fake-token", "Idempotency-Key": "build-42"}
    files = {"file": ("results.zip", b"archive")}
    first = client.post("/api/v1/imports", headers=headers, files=files)
    assert first.status_code == 202
    row = next(iter(repository.rows.values()))
    row.update(status="failed", error_message="Не удалось прочитать результаты Allure",
               archive_deleted=False)

    second = client.post("/api/v1/imports", headers=headers, files=files)

    assert second.status_code == 202
    assert second.json() == {
        "importId": first.json()["importId"],
        "status": "failed",
        "errorMessage": "Не удалось прочитать результаты Allure",
        "archiveDeleted": False,
    }


def test_metrics_requires_bearer_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from metrics_srv import metrics

    monkeypatch.setattr(metrics, "render_metrics", lambda repository, settings: "test_metric 1\n")

    unauthorized = client.get("/metrics")
    authorized = client.get("/metrics", headers={"Authorization": "Bearer fake-token"})

    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "unauthorized"
    assert authorized.status_code == 200
    assert authorized.text == "test_metric 1\n"


@pytest.mark.parametrize("error", [OperationalError, RedisConnectionError])
def test_metrics_source_failure_is_reported_without_private_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    from metrics_srv import metrics

    def fail(repository: object, settings: object) -> str:
        raise error("private connection detail")

    monkeypatch.setattr(metrics, "render_metrics", fail)

    response = client.get("/metrics", headers={"Authorization": "Bearer fake-token"})

    assert response.status_code == 503
    assert set(response.json()["error"]) == {"code", "message", "details"}
    assert "private connection detail" not in response.text


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token", "Basic Zm9vOmJhcg=="])
def test_unauthorized_upload_is_rejected(
    client: TestClient, settings: Settings, authorization: str | None
) -> None:
    headers = {"Authorization": authorization} if authorization else {}

    response = client.post(
        "/api/v1/imports", headers=headers, files={"file": ("results.zip", b"archive")}
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert set(response.json()["error"]) == {"code", "message", "details"}
    assert list(settings.storage_path.iterdir()) == []


def test_oversized_upload_is_rejected_and_temporary_file_removed(
    client: TestClient, settings: Settings
) -> None:
    response = client.post(
        "/api/v1/imports",
        headers={"Authorization": "Bearer fake-token"},
        files={"file": ("results.zip", b"x" * 129)},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "archive_too_large"
    assert list(settings.storage_path.iterdir()) == []


def test_oversized_content_length_is_rejected_without_reading_body(
    client: TestClient, settings: Settings
) -> None:
    def body():
        raise AssertionError("The server read a body that exceeds the configured limit")
        yield b""

    response = client.post(
        "/api/v1/imports",
        headers={
            "Authorization": "Bearer fake-token",
            "Content-Length": "9999999",
            "Content-Type": "multipart/form-data; boundary=unused",
        },
        content=body(),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "archive_too_large"
    assert list(settings.storage_path.iterdir()) == []


def test_unauthorized_body_is_not_read(client: TestClient, settings: Settings) -> None:
    def body():
        raise AssertionError("The server read an unauthorized body")
        yield b""

    response = client.post(
        "/api/v1/imports",
        headers={"Content-Type": "multipart/form-data; boundary=unused"},
        content=body(),
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert list(settings.storage_path.iterdir()) == []


def test_stream_without_content_length_is_limited(client: TestClient, settings: Settings) -> None:
    response = client.post(
        "/api/v1/imports",
        headers={
            "Authorization": "Bearer fake-token",
            "Content-Type": "multipart/form-data; boundary=unused",
        },
        content=iter([b"x" * (1024 * 1024 + settings.max_archive_bytes + 1)]),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "archive_too_large"
    assert list(settings.storage_path.iterdir()) == []


def test_missing_import_returns_documented_error(client: TestClient) -> None:
    response = client.get(
        f"/api/v1/imports/{uuid4()}", headers={"Authorization": "Bearer fake-token"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_missing_file_returns_validation_error(client: TestClient) -> None:
    response = client.post("/api/v1/imports", headers={"Authorization": "Bearer fake-token"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_failed_import_can_be_retried(settings: Settings) -> None:
    repository = FakeRepository()
    import_id = uuid4()
    repository.create_import(import_id)
    repository.rows[import_id].update(status="failed", error_message="failed")
    client = TestClient(create_app(settings, repository), raise_server_exceptions=False)

    response = client.post(
        f"/api/v1/imports/{import_id}/retry", headers={"Authorization": "Bearer fake-token"}
    )

    assert response.status_code == 202
    assert response.json()["importId"] == str(import_id)
    assert response.json()["status"] == "pending"
    assert repository.rows[import_id]["error_message"] is None


def test_retry_rejects_missing_or_incomplete_imports(settings: Settings) -> None:
    repository = FakeRepository()
    pending_id = uuid4()
    repository.create_import(pending_id)
    client = TestClient(create_app(settings, repository), raise_server_exceptions=False)
    headers = {"Authorization": "Bearer fake-token"}

    missing = client.post(f"/api/v1/imports/{uuid4()}/retry", headers=headers)
    conflict = client.post(f"/api/v1/imports/{pending_id}/retry", headers=headers)

    assert missing.status_code == 404
    assert conflict.status_code == 409
    assert set(missing.json()["error"]) == {"code", "message", "details"}
    assert set(conflict.json()["error"]) == {"code", "message", "details"}


def test_unexpected_error_is_safe(settings: Settings) -> None:
    class FailingRepository(FakeRepository):
        def get_import(self, import_id: UUID) -> dict | None:
            raise RuntimeError("private database detail")

    client = TestClient(create_app(settings, FailingRepository()), raise_server_exceptions=False)

    response = client.get(
        f"/api/v1/imports/{uuid4()}", headers={"Authorization": "Bearer fake-token"}
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private database detail" not in response.text


def test_swagger_schema_describes_upload_status_and_authorization(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    upload = schema["paths"]["/api/v1/imports"]["post"]
    status = schema["paths"]["/api/v1/imports/{import_id}"]["get"]
    retry = schema["paths"]["/api/v1/imports/{import_id}/retry"]["post"]
    metrics = schema["paths"]["/metrics"]["get"]
    assert "multipart/form-data" in upload["requestBody"]["content"]
    assert {"202", "401", "409", "413", "422", "500"}.issubset(upload["responses"])
    assert any(
        parameter["name"] == "Idempotency-Key" and parameter["in"] == "header"
        for parameter in upload["parameters"]
    )
    ref = upload["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    properties = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]["properties"]
    assert {"file", "project", "job", "buildNumber", "buildUrl", "revision"}.issubset(properties)
    assert {"200", "401", "404", "422", "500"}.issubset(status["responses"])
    assert {"202", "401", "404", "409", "422", "500"}.issubset(retry["responses"])
    assert upload["security"] and status["security"] and retry["security"]
    assert metrics["security"]
    assert {"200", "401", "503"}.issubset(metrics["responses"])
    assert "text/plain" in metrics["responses"]["200"]["content"]
    assert client.get("/docs").status_code == 200
