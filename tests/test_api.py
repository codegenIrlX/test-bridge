from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from metrics_srv.api import create_app
from metrics_srv.config import Settings


class FakeRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, dict] = {}

    def create_import(self, import_id: UUID) -> None:
        self.rows[import_id] = {
            "id": import_id,
            "status": "pending",
            "error_message": None,
            "archive_deleted": False,
        }

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
    assert "multipart/form-data" in upload["requestBody"]["content"]
    assert {"202", "401", "413", "422", "500"}.issubset(upload["responses"])
    assert {"200", "401", "404", "422", "500"}.issubset(status["responses"])
    assert {"202", "401", "404", "409", "422", "500"}.issubset(retry["responses"])
    assert upload["security"] and status["security"] and retry["security"]
    assert client.get("/docs").status_code == 200
