import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4
from zipfile import ZipFile

import pytest

from metrics_srv.repository import Repository


def write_result_archive(path: Path) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "allure-results/example-result.json",
            json.dumps({"name": "Example", "steps": [{"name": "First step"}]}),
        )


def test_storage_failure_does_not_delete_archive(tmp_path: Path) -> None:
    import_id = uuid4()
    archive = tmp_path / f"{import_id}.zip"
    write_result_archive(archive)
    repository = Repository(
        SimpleNamespace(storage_path=tmp_path, max_uncompressed_bytes=4096, max_result_files=10)
    )
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {
        "acquired": True,
        "status": "pending",
        "attempt_count": 0,
    }

    def execute(query: str, parameters: tuple) -> MagicMock:
        if "INSERT INTO test_results" in query:
            raise OSError("database write failed")
        return connection.execute.return_value

    connection.execute.side_effect = execute
    context = MagicMock()
    context.__enter__.return_value = connection
    context.__exit__.return_value = False
    repository.connect = MagicMock(return_value=context)

    with pytest.raises(OSError, match="database write failed"):
        repository.process_import(import_id)

    assert archive.exists()
    assert context.__exit__.call_args.args[0] is OSError


def test_failed_parse_keeps_archive_and_records_failure(tmp_path: Path) -> None:
    import_id = uuid4()
    archive = tmp_path / f"{import_id}.zip"
    archive.write_bytes(b"not a zip")
    repository = Repository(
        SimpleNamespace(storage_path=tmp_path, max_uncompressed_bytes=4096, max_result_files=10)
    )
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {
        "acquired": True,
        "status": "pending",
        "attempt_count": 0,
    }
    context = MagicMock()
    context.__enter__.return_value = connection
    context.__exit__.return_value = False
    repository.connect = MagicMock(return_value=context)

    repository.process_import(import_id)

    assert archive.exists()
    statements = [call.args[0] for call in connection.execute.call_args_list]
    assert any("status = 'failed'" in statement for statement in statements)
    assert not any("INSERT INTO test_results" in statement for statement in statements)


def test_exhausted_processing_attempts_preserve_archive(tmp_path: Path) -> None:
    import_id = uuid4()
    archive = tmp_path / f"{import_id}.zip"
    write_result_archive(archive)
    repository = Repository(
        SimpleNamespace(storage_path=tmp_path, max_uncompressed_bytes=4096, max_result_files=10)
    )
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {
        "acquired": True,
        "status": "processing",
        "attempt_count": 3,
    }
    context = MagicMock()
    context.__enter__.return_value = connection
    context.__exit__.return_value = False
    repository.connect = MagicMock(return_value=context)

    repository.process_import(import_id)

    assert archive.exists()
    statements = [call.args[0] for call in connection.execute.call_args_list]
    assert any("status = 'failed'" in statement for statement in statements)
    assert not any("INSERT INTO test_results" in statement for statement in statements)


def test_archive_cleanup_can_retry_after_filesystem_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_id = uuid4()
    archive = tmp_path / f"{import_id}.zip"
    write_result_archive(archive)
    repository = Repository(SimpleNamespace(storage_path=tmp_path))
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [{"id": import_id}]
    context = MagicMock()
    context.__enter__.return_value = connection
    context.__exit__.return_value = False
    repository.connect = MagicMock(return_value=context)
    original_unlink = Path.unlink
    attempts = 0

    def unlink_after_failure(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("file is temporarily locked")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink_after_failure)

    repository.cleanup_completed()
    assert archive.exists()
    assert not any(
        "archive_deleted = true" in call.args[0] for call in connection.execute.call_args_list
    )

    repository.cleanup_completed()
    assert not archive.exists()
    assert any(
        "archive_deleted = true" in call.args[0] for call in connection.execute.call_args_list
    )
