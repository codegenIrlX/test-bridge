"""Блокировка временных файлов, пока API принимает загрузку."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

try:
    import fcntl
except ImportError:
    fcntl = None


def lock_upload(stream: BinaryIO) -> None:
    """Удерживать блокировку открытого файла до завершения загрузки."""
    if fcntl is not None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def remove_stale_upload(path: Path, cutoff: float) -> None:
    """Удалить старый временный файл, только если он не занят загрузкой."""
    if fcntl is None:
        return
    try:
        with path.open("rb") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            file_info = os.fstat(stream.fileno())
            path_info = path.stat()
            if file_info.st_mtime < cutoff and (file_info.st_dev, file_info.st_ino) == (
                path_info.st_dev, path_info.st_ino
            ):
                path.unlink(missing_ok=True)
    except OSError:
        return
