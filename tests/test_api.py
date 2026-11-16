from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from metrics_srv.api import create_app
from metrics_srv.config import Settings
from metrics_srv.domains.imports.models import Import
from metrics_srv.domains.imports.repository import ImportRepository, ImportState
from metrics_srv.domains.imports.services import ImportService


class FakeRepository(ImportRepository):
    """Хранит импорты в памяти для тестов HTTP и файлового сценария."""

    def __init__(self) -> None:
        self.rows: dict[UUID, Import] = {}

    def create_import(self, session: Session, import_id: UUID) -> None:
        self.rows[import_id] = Import(
            id=import_id, status="pending", error_message=None, archive_deleted=False, attempt_count=0,
        )

    def get_import(self, session: Session, import_id: UUID) -> ImportState | None:
        row = self.rows.get(import_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "status": row.status,
            "error_message": row.error_message,
            "archive_deleted": row.archive_deleted,
        }

    def lock_import(self, session: Session, import_id: UUID) -> Import | None:
        return self.rows.get(import_id)


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
def repository() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def service(settings: Settings, repository: FakeRepository) -> ImportService:
    return ImportService(settings, MagicMock(), repository)


@pytest.fixture
def client(settings: Settings, service: ImportService) -> Iterator[TestClient]:
    with TestClient(create_app(settings, service), raise_server_exceptions=False) as client:
        yield client


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


def test_failed_import_can_be_retried(
    client: TestClient, service: ImportService, repository: FakeRepository,
) -> None:
    record = service.upload(BytesIO(b"archive"), None)
    import_id = record["id"]
    row = repository.rows[import_id]
    row.status = "failed"
    row.error_message = "failed"
    row.attempt_count = 3

    response = client.post(
        f"/api/v1/imports/{import_id}/retry", headers={"Authorization": "Bearer fake-token"}
    )

    assert response.status_code == 202
    assert response.json()["importId"] == str(import_id)
    assert response.json()["status"] == "pending"
    assert row.error_message is None
    assert row.attempt_count == 0


def test_retry_rejects_missing_or_incomplete_imports(client: TestClient) -> None:
    headers = {"Authorization": "Bearer fake-token"}
    uploaded = client.post(
        "/api/v1/imports", headers=headers, files={"file": ("results.zip", b"archive")},
    )
    assert uploaded.status_code == 202
    pending_id = uploaded.json()["importId"]

    missing = client.post(f"/api/v1/imports/{uuid4()}/retry", headers=headers)
    conflict = client.post(f"/api/v1/imports/{pending_id}/retry", headers=headers)

    assert missing.status_code == 404
    assert conflict.status_code == 409
    assert set(missing.json()["error"]) == {"code", "message", "details"}
    assert set(conflict.json()["error"]) == {"code", "message", "details"}


def test_unexpected_error_is_safe(settings: Settings) -> None:
    class FailingRepository(FakeRepository):
        def get_import(self, session: Session, import_id: UUID) -> ImportState | None:
            raise RuntimeError("private database detail")

    service = ImportService(settings, MagicMock(), FailingRepository())
    with TestClient(create_app(settings, service), raise_server_exceptions=False) as client:
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
    assert {"202", "401", "409", "413", "422", "500"}.issubset(upload["responses"])
    key, = upload["parameters"]
    assert key["name"] == "Idempotency-Key" and key["in"] == "header"
    assert key["required"] is False
    assert {"200", "401", "404", "422", "500"}.issubset(status["responses"])
    assert {"202", "401", "404", "409", "422", "500"}.issubset(retry["responses"])
    assert upload["security"] and status["security"] and retry["security"]
    assert client.get("/docs").status_code == 200


@pytest.mark.parametrize("key", ["", "with space", "a" * 129])
def test_upload_rejects_invalid_idempotency_key(
    client: TestClient, settings: Settings, key: str,
) -> None:
    response = client.post(
        "/api/v1/imports", headers={"Authorization": "Bearer fake-token", "Idempotency-Key": key},
        files={"file": ("results.zip", b"archive")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert not list(settings.storage_path.iterdir())
