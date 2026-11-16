"""Запросы к импортам и результатам Allure."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, TypedDict, cast
from uuid import UUID, uuid4

from sqlalchemy import func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from metrics_srv.domains.imports.models import Import, TestResult, TestStep
from metrics_srv.domains.imports.parser import ParsedStep, ParsedTest

type ImportStatus = Literal["pending", "processing", "completed", "failed"]
type StepRow = dict[str, UUID | int | str | None]


class ImportState(TypedDict):
    """Снимок полей импорта после чтения из БД."""

    id: UUID
    status: ImportStatus
    error_message: str | None
    archive_deleted: bool


def _snapshot(row: Import) -> ImportState:
    return {
        "id": row.id,
        "status": cast(ImportStatus, row.status),
        "error_message": row.error_message,
        "archive_deleted": row.archive_deleted,
    }


def _append_steps(
    rows: list[StepRow], test_id: UUID, parent_id: UUID | None, steps: list[ParsedStep]
) -> None:
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
        _append_steps(rows, test_id, step_id, step.steps)


class ImportRepository:
    """Работает с импортами в переданной сессии."""

    def create_import(self, session: Session, import_id: UUID) -> None:
        """Добавляет запись о принятом архиве."""
        session.add(Import(id=import_id))

    def register_keyed_import(
        self, session: Session, import_id: UUID, key: str, archive_sha256: str
    ) -> ImportState | None:
        """Возвращает новый или существующий импорт с тем же ключом и хешем."""
        row = session.scalar(
            pg_insert(Import)
            .values(id=import_id, idempotency_key=key, archive_sha256=archive_sha256)
            .on_conflict_do_nothing(
                index_elements=[Import.idempotency_key],
                index_where=Import.idempotency_key.is_not(None),
            )
            .returning(Import)
        )
        if row is None:
            # READ COMMITTED видит импорт, зафиксированный параллельной отправкой.
            row = session.scalar(select(Import).where(
                Import.idempotency_key == key, Import.archive_sha256 == archive_sha256
            ))
        return _snapshot(row) if row is not None else None

    def get_import(self, session: Session, import_id: UUID) -> ImportState | None:
        """Читает состояние импорта для ответа API или очереди."""
        row = session.get(Import, import_id)
        return _snapshot(row) if row is not None else None

    def import_exists(self, session: Session, import_id: UUID) -> bool:
        """Проверяет, зарегистрирован ли архив."""
        return session.scalar(select(Import.id).where(Import.id == import_id)) is not None

    def pending_ids(self, session: Session) -> list[UUID]:
        """Выбирает до ста незавершённых импортов для Scheduler."""
        return list(session.scalars(
            select(Import.id)
            .where(Import.status.in_(("pending", "processing")))
            .order_by(Import.created_at)
            .limit(100)
        ))

    def lock_import(self, session: Session, import_id: UUID) -> Import | None:
        """Читает и блокирует строку до конца транзакции."""
        return session.scalar(select(Import).where(Import.id == import_id).with_for_update())

    def try_processing_lock(self, session: Session, import_id: UUID) -> bool:
        """Пытается закрепить обработку импорта за текущим соединением."""
        return bool(session.scalar(
            select(func.pg_try_advisory_lock(func.hashtextextended(str(import_id), 0)))
        ))

    def add_results(self, session: Session, import_id: UUID, tests: list[ParsedTest]) -> None:
        """Пакетно записывает результаты и дерево шагов."""
        results = []
        steps: list[StepRow] = []
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
            _append_steps(steps, test_id, None, test.steps)
        if results:
            session.execute(insert(TestResult).execution_options(render_nulls=True), results)
        if steps:
            # При пакетной вставке родители должны идти раньше дочерних шагов.
            session.execute(insert(TestStep).execution_options(render_nulls=True), steps)

    def archives_due_for_cleanup(self, session: Session) -> list[UUID]:
        """Блокирует до ста импортов, архивы которых пора удалить."""
        return list(session.scalars(
            select(Import.id)
            .where(
                Import.status == "completed",
                Import.archive_deleted.is_(False),
                or_(
                    Import.archive_cleanup_after.is_(None),
                    Import.archive_cleanup_after <= func.current_timestamp(),
                ),
            )
            .order_by(func.coalesce(Import.archive_cleanup_after, Import.created_at), Import.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        ))

    def defer_archive_cleanup(self, session: Session, import_id: UUID) -> None:
        """Переносит следующую попытку удаления архива на пять минут."""
        session.execute(update(Import).where(Import.id == import_id).values(
            archive_cleanup_after=func.statement_timestamp() + literal(timedelta(minutes=5))
        ))

    def mark_archive_deleted(self, session: Session, import_id: UUID) -> None:
        """Отмечает архив удалённым и сбрасывает срок повтора."""
        session.execute(update(Import).where(Import.id == import_id).values(
            archive_deleted=True, archive_cleanup_after=None
        ))
