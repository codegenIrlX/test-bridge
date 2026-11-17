"""Хранение ZIP и блокировка временных файлов при загрузке."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO
from uuid import UUID

try:
    import fcntl
except ImportError:
    fcntl = None


class ArchiveTooLargeError(ValueError):
    """Размер загружаемого ZIP превышает настроенный предел."""


class ArchiveStorage:
    """Архивы импорта в общем каталоге приложения."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def archive_path(self, import_id: UUID) -> Path:
        """Возвращает путь к ZIP импорта."""
        return self.root / f"{import_id}.zip"

    def save(
        self, import_id: UUID, source: BinaryIO, *, max_archive_bytes: int, calculate_digest: bool
    ) -> str | None:
        """Записывает ZIP и при необходимости вычисляет SHA-256."""
        self.root.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        digest = hashlib.sha256() if calculate_digest else None
        try:
            with NamedTemporaryFile(
                dir=self.root, prefix="upload-", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                lock_upload(stream)
                size = 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_archive_bytes:
                        raise ArchiveTooLargeError("Архив превышает допустимый размер")
                    stream.write(chunk)
                    if digest is not None:
                        digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
                os.utime(temporary, None)
            temporary.replace(self.archive_path(import_id))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return digest.hexdigest() if digest is not None else None

    def remove_stale_uploads(self, cutoff: float) -> None:
        """Удаляет старые временные файлы, свободные от блокировки."""
        for path in self.root.glob("upload-*.tmp"):
            remove_stale_upload(path, cutoff)

    def old_archives(self, cutoff: float) -> Iterator[tuple[UUID, Path]]:
        """Перечисляет ZIP с идентификатором импорта старше указанного времени."""
        for path in self.root.glob("*.zip"):
            try:
                import_id = UUID(path.stem)
                if path.stat().st_mtime < cutoff:
                    yield import_id, path
            except (ValueError, OSError):
                continue


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
