import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import Engine
from sqlalchemy.pool import NullPool

from metrics_srv.domains.imports.cleanup import ArchiveCleanup
from metrics_srv.domains.imports.models import Import
from metrics_srv.domains.imports.processing import ImportProcessor
from metrics_srv.domains.imports.repository import ImportRepository


@pytest.fixture
def repository() -> MagicMock:
    repository = MagicMock(spec=ImportRepository)
    repository.try_processing_lock.return_value = True
    return repository


@pytest.fixture
def processor(tmp_path: Path, repository: MagicMock) -> ImportProcessor:
    settings = SimpleNamespace(
        storage_path=tmp_path, max_uncompressed_bytes=4096, max_result_files=10,
    )
    engine = MagicMock(spec=Engine, pool=NullPool(lambda: None))
    return ImportProcessor(settings, engine, repository)


def write_result_archive(path: Path) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "allure-results/example-result.json",
            json.dumps({"name": "Example", "steps": [{"name": "First step"}]}),
        )


def test_storage_failure_does_not_delete_archive(
    tmp_path: Path, repository: MagicMock, processor: ImportProcessor,
) -> None:
    import_id = uuid4()
    archive = tmp_path / f"{import_id}.zip"
    write_result_archive(archive)
    row = Import(id=import_id, status="pending", attempt_count=0)
    repository.lock_import.return_value = row
    repository.add_results.side_effect = OSError("database write failed")

    with pytest.raises(OSError, match="database write failed"):
        processor.process_import(import_id)

    assert archive.exists()
    assert row.status == "processing" and row.attempt_count == 1


def test_failed_parse_keeps_archive_and_records_failure(
    tmp_path: Path, repository: MagicMock, processor: ImportProcessor,
) -> None:
    import_id = uuid4()
    archive = tmp_path / f"{import_id}.zip"
    archive.write_bytes(b"not a zip")
    row = Import(id=import_id, status="pending", attempt_count=0)
    repository.lock_import.return_value = row

    processor.process_import(import_id)

    assert archive.exists()
    repository.mark_failed.assert_called_once()
    assert repository.mark_failed.call_args.args[1:] == (
        import_id, "Не удалось прочитать результаты Allure",
    )
    repository.add_results.assert_not_called()


def test_exhausted_processing_attempts_preserve_archive(
    tmp_path: Path, repository: MagicMock, processor: ImportProcessor,
) -> None:
    import_id = uuid4()
    archive = tmp_path / f"{import_id}.zip"
    write_result_archive(archive)
    row = Import(id=import_id, status="processing", attempt_count=3)
    repository.lock_import.return_value = row

    processor.process_import(import_id)

    assert archive.exists()
    assert row.attempt_count == 3
    repository.mark_failed.assert_called_once()
    assert repository.mark_failed.call_args.args[1:] == (
        import_id, "Исчерпаны попытки обработки импорта",
    )
    repository.add_results.assert_not_called()


def test_archive_cleanup_can_retry_after_filesystem_error(
    tmp_path: Path, repository: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_id = uuid4()
    archive = tmp_path / f"{import_id}.zip"
    write_result_archive(archive)
    cleanup = ArchiveCleanup(SimpleNamespace(storage_path=tmp_path), MagicMock(), repository)
    repository.archives_due_for_cleanup.return_value = [import_id]

    def reject_unlink(path: Path, *, missing_ok: bool = False) -> None:
        assert path == archive
        raise PermissionError("file is temporarily locked")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", reject_unlink)
        cleanup.cleanup_due_archives()
    assert archive.exists()
    repository.mark_archive_deleted.assert_not_called()
    repository.defer_archive_cleanup.assert_called_once()
    assert repository.defer_archive_cleanup.call_args.args[1] == import_id

    cleanup.cleanup_due_archives()
    assert not archive.exists()
    repository.mark_archive_deleted.assert_called_once()
    assert repository.mark_archive_deleted.call_args.args[1] == import_id
