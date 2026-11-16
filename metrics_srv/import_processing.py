"""Координация разбора архива и записи результатов импорта."""

from uuid import UUID

from metrics_srv.config import Settings
from metrics_srv.parser import InvalidArchiveError, parse_archive
from metrics_srv.repository import Repository


class ImportProcessor:
    """Разбор Allure и завершение попытки импорта."""

    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository

    def process_import(self, import_id: UUID) -> None:
        """Прочитать архив и передать результаты на атомарное сохранение."""
        with self.repository.processing_connection(import_id) as connection:
            if connection is None:
                return
            path = self.settings.storage_path / f"{import_id}.zip"
            try:
                tests = parse_archive(
                    path, self.settings.max_uncompressed_bytes, self.settings.max_result_files
                )
            except (InvalidArchiveError, FileNotFoundError):
                self.repository.fail_import(
                    connection, import_id, "Не удалось прочитать результаты Allure"
                )
                return
            self.repository.complete_import(connection, import_id, tests)
