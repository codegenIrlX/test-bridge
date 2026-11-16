"""Очистка архивов после импорта и бесхозных файлов."""

from __future__ import annotations

import logging
from time import time

from sqlalchemy.orm import Session, sessionmaker

from metrics_srv.config import Settings
from metrics_srv.domains.imports.archive_storage import ArchiveStorage
from metrics_srv.domains.imports.repository import ImportRepository

logger = logging.getLogger(__name__)


class ArchiveCleanup:
    """Удаляет ZIP, которые больше не нужны для обработки."""

    def __init__(
        self, settings: Settings, sessions: sessionmaker[Session], repository: ImportRepository
    ) -> None:
        self._sessions = sessions
        self._repository = repository
        self._storage = ArchiveStorage(settings.storage_path)

    def cleanup_completed(self) -> None:
        """Удаляет архивы завершённых импортов и откладывает повторы после ошибки."""
        deferred_count = 0
        with self._sessions.begin() as session:
            for import_id in self._repository.archives_due_for_cleanup(session):
                try:
                    self._storage.archive_path(import_id).unlink(missing_ok=True)
                except OSError:
                    self._repository.defer_archive_cleanup(session, import_id)
                    deferred_count += 1
                    continue
                self._repository.mark_archive_deleted(session, import_id)
        if deferred_count:
            logger.warning("Не удалось удалить архивы: %d. Повтор через пять минут.", deferred_count)

    def cleanup_orphans(self) -> None:
        """Удаляет старые файлы загрузки и ZIP без записи импорта."""
        cutoff = time() - 86400
        if not self._storage.root.exists():
            return
        self._storage.remove_stale_uploads(cutoff)
        with self._sessions() as session:
            for import_id, path in self._storage.old_archives(cutoff):
                if self._repository.import_exists(session, import_id):
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
