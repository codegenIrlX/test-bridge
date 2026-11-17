import os
from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from metrics_srv.domains.imports import archive_storage
from metrics_srv.domains.imports.cleanup import ArchiveCleanup
from metrics_srv.domains.imports.repository import ImportRepository


@pytest.fixture
def cleanup(tmp_path: Path) -> ArchiveCleanup:
    repository = MagicMock(spec=ImportRepository)
    repository.import_exists.return_value = False
    return ArchiveCleanup(SimpleNamespace(storage_path=tmp_path), MagicMock(), repository)


@pytest.fixture
def old_upload(tmp_path: Path) -> Path:
    path = tmp_path / "upload-old.tmp"
    path.write_bytes(b"incomplete upload")
    old = time() - 90_000
    os.utime(path, (old, old))
    return path


def test_cleanup_requests_removal_of_old_unlocked_upload(
    cleanup: ArchiveCleanup, old_upload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())
    monkeypatch.setattr(archive_storage, "fcntl", fake_fcntl)
    unlink = MagicMock()
    monkeypatch.setattr(Path, "unlink", unlink)

    cleanup.cleanup_orphans()

    fake_fcntl.flock.assert_called_once()
    assert fake_fcntl.flock.call_args.args[1] == fake_fcntl.LOCK_EX | fake_fcntl.LOCK_NB
    unlink.assert_called_once_with(missing_ok=True)


def test_cleanup_preserves_locked_upload(
    cleanup: ArchiveCleanup, old_upload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock(side_effect=BlockingIOError))
    monkeypatch.setattr(archive_storage, "fcntl", fake_fcntl)

    cleanup.cleanup_orphans()

    fake_fcntl.flock.assert_called_once()
    assert old_upload.exists()


def test_cleanup_preserves_upload_without_fcntl(
    cleanup: ArchiveCleanup, old_upload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_storage, "fcntl", None)

    cleanup.cleanup_orphans()

    assert old_upload.exists()


def test_cleanup_preserves_recent_upload(
    cleanup: ArchiveCleanup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = tmp_path / "upload-recent.tmp"
    upload.write_bytes(b"incomplete upload")
    fake_fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())
    monkeypatch.setattr(archive_storage, "fcntl", fake_fcntl)

    cleanup.cleanup_orphans()

    assert upload.exists()


def test_upload_uses_exclusive_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upload = tmp_path / "upload-active.tmp"
    fake_fcntl = SimpleNamespace(LOCK_EX=2, flock=MagicMock())
    monkeypatch.setattr(archive_storage, "fcntl", fake_fcntl)

    with upload.open("wb") as stream:
        archive_storage.lock_upload(stream)
        fake_fcntl.flock.assert_called_once_with(stream.fileno(), fake_fcntl.LOCK_EX)


@pytest.mark.skipif(os.name == "nt", reason="flock и удаление открытого файла требуют POSIX")
def test_cleanup_really_removes_old_unlocked_upload(
    cleanup: ArchiveCleanup, old_upload: Path
) -> None:
    cleanup.cleanup_orphans()

    assert not old_upload.exists()


@pytest.mark.skipif(os.name == "nt", reason="flock и удаление открытого файла требуют POSIX")
def test_cleanup_waits_for_actual_upload_lock(
    cleanup: ArchiveCleanup, old_upload: Path
) -> None:
    with old_upload.open("rb") as stream:
        archive_storage.lock_upload(stream)
        cleanup.cleanup_orphans()
        assert old_upload.exists()

    cleanup.cleanup_orphans()
    assert not old_upload.exists()
