"""Обработка архива и атомарная запись результатов."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from metrics_srv.config import Settings
from metrics_srv.domains.imports.archive_storage import ArchiveStorage
from metrics_srv.domains.imports.parser import InvalidArchiveError, parse_archive
from metrics_srv.domains.imports.repository import ImportRepository


class ImportProcessor:
    """Фиксирует попытку импорта, разбирает ZIP и сохраняет результаты."""

    def __init__(self, settings: Settings, engine: Engine, repository: ImportRepository) -> None:
        if not isinstance(engine.pool, NullPool):
            raise TypeError("Обработка импорта требует физического закрытия соединений")
        self._settings = settings
        self._engine = engine
        self._repository = repository
        self._storage = ArchiveStorage(settings.storage_path)

    def process_import(self, import_id: UUID) -> None:
        """Обрабатывает импорт под блокировкой на одном физическом соединении."""
        # Соединение с NullPool удерживает advisory lock до завершения обеих транзакций.
        with self._engine.connect() as connection, Session(
            bind=connection, expire_on_commit=False
        ) as session:
            with session.begin():
                if not self._repository.try_processing_lock(session, import_id):
                    raise RuntimeError("Импорт уже обрабатывается")
                row = self._repository.lock_import(session, import_id)
                if row is None or row.status not in ("pending", "processing"):
                    return
                if row.attempt_count >= 3:
                    row.status = "failed"
                    row.error_message = "Исчерпаны попытки обработки импорта"
                    return
                row.status = "processing"
                row.attempt_count += 1

            try:
                tests = parse_archive(
                    self._storage.archive_path(import_id),
                    self._settings.max_uncompressed_bytes,
                    self._settings.max_result_files,
                )
            except (InvalidArchiveError, FileNotFoundError):
                with session.begin():
                    row.status = "failed"
                    row.error_message = "Не удалось прочитать результаты Allure"
                return

            with session.begin():
                self._repository.add_results(session, import_id, tests)
                row.status = "completed"
                row.error_message = None
