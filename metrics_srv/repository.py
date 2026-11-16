"""Хранение импортов и результатов в PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import time
from typing import Literal, TypedDict
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from metrics_srv.config import Settings
from metrics_srv.database import create_database_engine
from metrics_srv.filelocks import remove_stale_upload
from metrics_srv.models import Import, TestResult, TestStep
from metrics_srv.parser import ParsedStep, ParsedTest

_PENDING_STATUSES = ("pending", "processing")


class ImportState(TypedDict):
    """Поля импорта, которые читают API и Processor."""

    id: UUID
    status: str
    error_message: str | None
    archive_deleted: bool


class Repository:
    """Операции с импортами и принадлежащими им результатами."""

    def __init__(self, settings: Settings, *, engine: Engine | None = None) -> None:
        self.settings = settings
        self._engine = create_database_engine(settings) if engine is None else engine
        if not isinstance(self._engine.pool, NullPool):
            raise TypeError("Репозиторий требует физического закрытия соединений")
        self._sessions = sessionmaker(self._engine)

    def create_import(self, import_id: UUID) -> None:
        """Зарегистрировать принятый архив."""
        with self._sessions.begin() as session:
            session.add(Import(id=import_id))

    def get_import(self, import_id: UUID) -> ImportState | None:
        """Получить состояние импорта по идентификатору."""
        with self._sessions() as session:
            row = session.get(Import, import_id)
            if row is None:
                return None
            return ImportState(
                id=row.id,
                status=row.status,
                error_message=row.error_message,
                archive_deleted=row.archive_deleted,
            )

    def pending_ids(self) -> list[UUID]:
        """Найти задания, которые ещё не обработаны."""
        with self._sessions() as session:
            return list(session.scalars(
                select(Import.id)
                .where(Import.status.in_(_PENDING_STATUSES))
                .order_by(Import.created_at)
                .limit(100)
            ))

    def retry_import(self, import_id: UUID) -> Literal["not_found", "conflict", "pending"]:
        """Вернуть неуспешный импорт в очередь при наличии архива."""
        with self._sessions.begin() as session:
            row = session.scalar(select(Import).where(Import.id == import_id).with_for_update())
            if row is None:
                return "not_found"
            path = self.settings.storage_path / f"{import_id}.zip"
            if row.status != "failed" or not path.is_file():
                return "conflict"
            row.status = "pending"
            row.attempt_count = 0
            row.error_message = None
            return "pending"

    @contextmanager
    def processing_connection(self, import_id: UUID) -> Iterator[Connection | None]:
        """Зафиксировать попытку и удерживать блокировку до завершения обработки."""
        with self._engine.connect() as connection:
            with Session(connection) as session, session.begin():
                # Блокировка переживает фиксацию попытки и снимается при закрытии соединения.
                if not session.scalar(
                    select(func.pg_try_advisory_lock(func.hashtextextended(str(import_id), 0)))
                ):
                    raise RuntimeError("Импорт уже обрабатывается")
                row = session.get(Import, import_id)
                started = False
                if row is not None and row.status in _PENDING_STATUSES:
                    if row.attempt_count >= 3:
                        row.status = "failed"
                        row.error_message = "Исчерпаны попытки обработки импорта"
                    else:
                        row.status = "processing"
                        row.attempt_count += 1
                        started = True

            yield connection if started else None

    def fail_import(self, connection: Connection, import_id: UUID, message: str) -> None:
        """Зафиксировать ошибку текущей попытки импорта."""
        with Session(connection) as session, session.begin():
            session.execute(
                update(Import).where(Import.id == import_id)
                .values(status="failed", error_message=message)
            )

    def complete_import(
        self, connection: Connection, import_id: UUID, tests: list[ParsedTest]
    ) -> None:
        """Сохранить результаты и завершить импорт одной транзакцией."""
        results = []
        step_groups = []
        for position, test in enumerate(tests):
            test_id = uuid4()
            results.append({
                "id": test_id,
                "import_id": import_id,
                "position": position,
                "name": test.name,
                "started_at_ms": test.start,
                "stopped_at_ms": test.stop,
            })
            if test.steps:
                step_groups.append((test_id, None, test.steps))

        with Session(connection) as session, session.begin():
            if results:
                session.execute(insert(TestResult), results, execution_options={"render_nulls": True})
            self._insert_steps(session, step_groups)
            session.execute(
                update(Import).where(Import.id == import_id)
                .values(status="completed", error_message=None)
            )

    def _insert_steps(
        self, session: Session, step_groups: list[tuple[UUID, UUID | None, list[ParsedStep]]]
    ) -> None:
        while step_groups:
            rows = []
            children = []
            for test_id, parent_id, steps in step_groups:
                for position, step in enumerate(steps):
                    step_id = uuid4()
                    rows.append({
                        "id": step_id,
                        "test_id": test_id,
                        "parent_id": parent_id,
                        "position": position,
                        "name": step.name,
                        "started_at_ms": step.start,
                        "stopped_at_ms": step.stop,
                    })
                    if step.steps:
                        children.append((test_id, step_id, step.steps))
            # Родительский уровень должен существовать до вставки внешних ключей детей.
            session.execute(insert(TestStep), rows, execution_options={"render_nulls": True})
            step_groups = children

    def cleanup_completed(self) -> None:
        """Удалить сохранённые архивы и повторить ранее неудачную очистку."""
        with self._sessions.begin() as session:
            import_ids = session.scalars(
                select(Import.id)
                .where(Import.status == "completed", Import.archive_deleted.is_(False))
                .order_by(Import.created_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            ).all()
            for import_id in import_ids:
                path = self.settings.storage_path / f"{import_id}.zip"
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                session.execute(
                    update(Import).where(Import.id == import_id).values(archive_deleted=True)
                )

    def cleanup_orphans(self) -> None:
        """Удалить старые архивы без записи в доступной базе данных."""
        cutoff = time() - 86400
        if not self.settings.storage_path.exists():
            return
        for path in self.settings.storage_path.glob("upload-*.tmp"):
            remove_stale_upload(path, cutoff)
        with self._sessions() as session:
            for path in self.settings.storage_path.glob("*.zip"):
                try:
                    import_id = UUID(path.stem)
                    if path.stat().st_mtime >= cutoff:
                        continue
                except (ValueError, OSError):
                    continue
                if session.scalar(select(Import.id).where(Import.id == import_id)) is None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue
