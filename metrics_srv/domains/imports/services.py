"""Приём архива, просмотр статуса и ручной повтор импорта."""

from __future__ import annotations

import logging
from typing import BinaryIO, Literal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from metrics_srv.config import Settings
from metrics_srv.domains.imports.archive_storage import ArchiveStorage
from metrics_srv.domains.imports.repository import ImportRepository, ImportState

logger = logging.getLogger(__name__)


class IdempotencyConflictError(ValueError):
    """Ключ повторной отправки уже связан с другим ZIP."""


class ImportService:
    """Координирует приём и повтор импорта."""

    def __init__(
        self, settings: Settings, sessions: sessionmaker[Session], repository: ImportRepository
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._repository = repository
        self._storage = ArchiveStorage(settings.storage_path)

    def upload(self, file: BinaryIO, idempotency_key: str | None) -> ImportState:
        """Сохраняет ZIP до регистрации импорта в БД."""
        import_id = uuid4()
        archive_sha256 = self._storage.save(
            import_id,
            file,
            max_archive_bytes=self._settings.max_archive_bytes,
            calculate_digest=idempotency_key is not None,
        )
        with self._sessions.begin() as session:
            if idempotency_key is None:
                self._repository.create_import(session, import_id)
                return {
                    "id": import_id,
                    "status": "pending",
                    "error_message": None,
                    "archive_deleted": False,
                }
            assert archive_sha256 is not None
            row = self._repository.register_keyed_import(
                session, import_id, idempotency_key, archive_sha256
            )

        if row is None or row["id"] != import_id:
            try:
                self._storage.archive_path(import_id).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Не удалось удалить лишний ZIP: %s", type(exc).__name__)
        if row is None:
            raise IdempotencyConflictError("Этот ключ уже принят для другого ZIP")
        return row

    def get_import(self, import_id: UUID) -> ImportState | None:
        """Возвращает текущее состояние импорта."""
        with self._sessions() as session:
            return self._repository.get_import(session, import_id)

    def pending_ids(self) -> list[UUID]:
        """Возвращает незавершённые импорты для постановки в очередь."""
        with self._sessions() as session:
            return self._repository.pending_ids(session)

    def retry_import(self, import_id: UUID) -> Literal["not_found", "conflict", "pending"]:
        """Сбрасывает счётчик попыток неуспешного импорта, если ZIP сохранился."""
        with self._sessions.begin() as session:
            row = self._repository.lock_import(session, import_id)
            if row is None:
                return "not_found"
            if row.status != "failed" or not self._storage.archive_path(import_id).is_file():
                return "conflict"
            row.status = "pending"
            row.attempt_count = 0
            row.error_message = None
            return "pending"
