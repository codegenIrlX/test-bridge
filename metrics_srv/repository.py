"""Хранение импортов и результатов в PostgreSQL."""

from __future__ import annotations

from pathlib import Path
from time import time
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from metrics_srv.config import Settings
from metrics_srv.filelocks import remove_stale_upload
from metrics_srv.parser import InvalidArchiveError, ParsedStep, parse_archive


class Repository:
    """Операции с импортами и принадлежащими им результатами."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def connect(self) -> psycopg.Connection:
        """Открыть соединение с базой данных."""
        s = self.settings
        return psycopg.connect(
            host=s.database_host, port=s.database_port, dbname=s.database_name,
            user=s.database_user, password=s.database_password, row_factory=dict_row,
        )

    def migrate(self) -> None:
        """Создать схему первой версии, не изменяя существующие данные."""
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as connection:
            connection.execute(schema)

    def create_import(
        self, import_id: UUID, *, project: str | None = None, job: str | None = None,
        build_number: str | None = None, build_url: str | None = None,
        revision: str | None = None, idempotency_key: str | None = None,
        archive_sha256: str | None = None,
    ) -> dict:
        """Зарегистрировать принятый архив."""
        with self.connect() as connection:
            inserted = connection.execute(
                "INSERT INTO imports (id, project, job, build_number, build_url, revision, "
                "idempotency_key, archive_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id, status, error_message, "
                "archive_deleted, archive_sha256, "
                "project, job, build_number, build_url, revision",
                (import_id, project, job, build_number, build_url, revision, idempotency_key,
                 archive_sha256),
            ).fetchone()
            if inserted is not None:
                return inserted
            return connection.execute(
                "SELECT id, status, error_message, archive_deleted, archive_sha256, "
                "project, job, build_number, build_url, revision "
                "FROM imports WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()

    def get_import(self, import_id: UUID) -> dict | None:
        """Получить состояние импорта по идентификатору."""
        with self.connect() as connection:
            return connection.execute(
                "SELECT id, status, error_message, archive_deleted FROM imports WHERE id = %s",
                (import_id,),
            ).fetchone()

    def pending_ids(self) -> list[UUID]:
        """Найти задания, которые ещё не обработаны."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM imports WHERE status IN ('pending', 'processing') "
                "ORDER BY created_at LIMIT 100"
            ).fetchall()
            return [row["id"] for row in rows]

    def retry_import(self, import_id: UUID) -> str:
        """Вернуть неуспешный импорт в очередь при наличии архива."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM imports WHERE id = %s FOR UPDATE", (import_id,)
            ).fetchone()
            if row is None:
                return "not_found"
            if row["status"] != "failed" or not (self.settings.storage_path / f"{import_id}.zip").is_file():
                return "conflict"
            connection.execute(
                "UPDATE imports SET status = 'pending', attempt_count = 0, error_message = NULL "
                "WHERE id = %s", (import_id,)
            )
            return "pending"

    def process_import(self, import_id: UUID) -> None:
        """Разобрать архив и атомарно сохранить его результаты."""
        with self.connect() as connection:
            lock = connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                (str(import_id),),
            ).fetchone()
            if not lock["acquired"]:
                raise RuntimeError("Импорт уже обрабатывается")
            row = connection.execute(
                "SELECT status, attempt_count FROM imports WHERE id = %s", (import_id,)
            ).fetchone()
            if row is None or row["status"] not in ("pending", "processing"):
                return
            if row["attempt_count"] >= 3:
                connection.execute(
                    "UPDATE imports SET status = 'failed', error_message = %s, "
                    "processing_finished_at = now() WHERE id = %s",
                    ("Исчерпаны попытки обработки импорта", import_id),
                )
                return
            connection.execute(
                "UPDATE imports SET status = 'processing', attempt_count = attempt_count + 1, "
                "processing_started_at = now(), processing_finished_at = NULL "
                "WHERE id = %s", (import_id,)
            )
            connection.commit()
            path = self.settings.storage_path / f"{import_id}.zip"
            try:
                tests = parse_archive(
                    path, self.settings.max_uncompressed_bytes, self.settings.max_result_files
                )
            except (InvalidArchiveError, FileNotFoundError):
                connection.execute(
                    "UPDATE imports SET status = 'failed', error_message = %s, "
                    "processing_finished_at = now() WHERE id = %s",
                    ("Не удалось прочитать результаты Allure", import_id),
                )
                return
            for position, test in enumerate(tests):
                test_id = uuid4()
                connection.execute(
                    "INSERT INTO test_results (id, import_id, position, name, started_at_ms, "
                    "stopped_at_ms, status, allure_uuid, history_id, test_case_id, full_name, "
                    "labels, parameters) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (test_id, import_id, position, test.name, test.start, test.stop, test.status,
                     test.uuid, test.history_id, test.test_case_id, test.full_name,
                     Jsonb(test.labels), Jsonb(test.parameters)),
                )
                self._insert_steps(connection, test_id, None, test.steps)
            connection.execute(
                "UPDATE imports SET status = 'completed', error_message = NULL, "
                "processing_finished_at = now() WHERE id = %s",
                (import_id,),
            )

    def _insert_steps(
        self, connection: psycopg.Connection, test_id: UUID, parent_id: UUID | None,
        steps: list[ParsedStep],
    ) -> None:
        for position, step in enumerate(steps):
            step_id = uuid4()
            connection.execute(
                "INSERT INTO test_steps (id, test_id, parent_id, position, name, started_at_ms, "
                "stopped_at_ms, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (step_id, test_id, parent_id, position, step.name, step.start, step.stop, step.status),
            )
            self._insert_steps(connection, test_id, step_id, step.steps)

    def cleanup_completed(self) -> None:
        """Удалить сохранённые архивы и повторить ранее неудачную очистку."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM imports WHERE status = 'completed' AND NOT archive_deleted "
                "ORDER BY created_at LIMIT 100 FOR UPDATE SKIP LOCKED"
            ).fetchall()
            for row in rows:
                path = self.settings.storage_path / f"{row['id']}.zip"
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                connection.execute("UPDATE imports SET archive_deleted = true WHERE id = %s", (row["id"],))

    def cleanup_orphans(self) -> None:
        """Удалить старые архивы без записи в доступной базе данных."""
        cutoff = time() - 86400
        if not self.settings.storage_path.exists():
            return
        for path in self.settings.storage_path.glob("upload-*.tmp"):
            remove_stale_upload(path, cutoff)
        with self.connect() as connection:
            for path in self.settings.storage_path.glob("*.zip"):
                try:
                    import_id = UUID(path.stem)
                    if path.stat().st_mtime >= cutoff:
                        continue
                except (ValueError, OSError):
                    continue
                row = connection.execute("SELECT 1 FROM imports WHERE id = %s", (import_id,)).fetchone()
                if row is None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue
