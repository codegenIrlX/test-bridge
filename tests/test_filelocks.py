import os
from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from metrics_srv import filelocks
from metrics_srv.repository import Repository


@pytest.fixture
def orphan_repository(tmp_path: Path) -> Repository:
    repository = Repository(SimpleNamespace(storage_path=tmp_path))
    context = MagicMock()
    context.__enter__.return_value.execute.return_value.fetchone.return_value = None
    context.__exit__.return_value = False
    repository.connect = MagicMock(return_value=context)
    return repository


@pytest.fixture
def old_upload(tmp_path: Path) -> Path:
    path = tmp_path / "upload-old.tmp"
    path.write_bytes(b"incomplete upload")
    old = time() - 90_000
    os.utime(path, (old, old))
    return path


def test_cleanup_requests_removal_of_old_unlocked_upload(
    orphan_repository: Repository, old_upload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())
    monkeypatch.setattr(filelocks, "fcntl", fake_fcntl)
    unlink = MagicMock()
    monkeypatch.setattr(Path, "unlink", unlink)

    orphan_repository.cleanup_orphans()

    fake_fcntl.flock.assert_called_once()
    assert fake_fcntl.flock.call_args.args[1] == fake_fcntl.LOCK_EX | fake_fcntl.LOCK_NB
    unlink.assert_called_once_with(missing_ok=True)


def test_cleanup_preserves_locked_upload(
    orphan_repository: Repository, old_upload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock(side_effect=BlockingIOError))
    monkeypatch.setattr(filelocks, "fcntl", fake_fcntl)

    orphan_repository.cleanup_orphans()

    fake_fcntl.flock.assert_called_once()
    assert old_upload.exists()


def test_cleanup_preserves_upload_without_fcntl(
    orphan_repository: Repository, old_upload: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(filelocks, "fcntl", None)

    orphan_repository.cleanup_orphans()

    assert old_upload.exists()


def test_cleanup_preserves_recent_upload(
    orphan_repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = tmp_path / "upload-recent.tmp"
    upload.write_bytes(b"incomplete upload")
    fake_fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())
    monkeypatch.setattr(filelocks, "fcntl", fake_fcntl)

    orphan_repository.cleanup_orphans()

    assert upload.exists()


def test_upload_uses_exclusive_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upload = tmp_path / "upload-active.tmp"
    fake_fcntl = SimpleNamespace(LOCK_EX=2, flock=MagicMock())
    monkeypatch.setattr(filelocks, "fcntl", fake_fcntl)

    with upload.open("wb") as stream:
        filelocks.lock_upload(stream)
        fake_fcntl.flock.assert_called_once_with(stream.fileno(), fake_fcntl.LOCK_EX)


@pytest.mark.skipif(os.name == "nt", reason="flock и удаление открытого файла требуют POSIX")
def test_cleanup_really_removes_old_unlocked_upload(
    orphan_repository: Repository, old_upload: Path
) -> None:
    orphan_repository.cleanup_orphans()

    assert not old_upload.exists()


@pytest.mark.skipif(os.name == "nt", reason="flock и удаление открытого файла требуют POSIX")
def test_cleanup_waits_for_actual_upload_lock(
    orphan_repository: Repository, old_upload: Path
) -> None:
    with old_upload.open("rb") as stream:
        filelocks.lock_upload(stream)
        orphan_repository.cleanup_orphans()
        assert old_upload.exists()

    orphan_repository.cleanup_orphans()
    assert not old_upload.exists()
