"""Таблицы импортов, результатов и шагов Allure."""

from datetime import datetime
from typing import ClassVar
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from metrics_srv.db.base import Base
from metrics_srv.db.mixins import UUIDPrimaryKeyMixin


class ExecutionTimeMixin:
    """Время выполнения теста или шага из Allure."""

    started_at_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="Начало выполнения в миллисекундах Unix"
    )
    stopped_at_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="Окончание выполнения в миллисекундах Unix"
    )


class Import(UUIDPrimaryKeyMixin, Base):
    """Архив Allure и состояние его обработки."""

    __tablename__ = "imports"
    __id_comment__ = "Идентификатор импорта и имя архива"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="status",
        ),
        Index(
            None,
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        {"comment": "Принятые архивы Allure и состояние их обработки"},
    )

    status: Mapped[str] = mapped_column(
        Text, server_default="pending", comment="Состояние обработки архива"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, comment="Безопасное описание ошибки обработки"
    )
    attempt_count: Mapped[int] = mapped_column(
        server_default=text("0"), comment="Число начатых попыток обработки"
    )
    archive_deleted: Mapped[bool] = mapped_column(
        server_default=false(), comment="Признак удаления обработанного архива"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="Время регистрации импорта"
    )
    archive_cleanup_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Время следующей попытки удаления архива"
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        Text, comment="Ключ повторной отправки архива из CI"
    )
    archive_sha256: Mapped[str | None] = mapped_column(
        Text, comment="Контрольная сумма SHA-256 архива с ключом повторной отправки"
    )


class TestResult(UUIDPrimaryKeyMixin, ExecutionTimeMixin, Base):
    """Результат теста в принятом архиве."""

    __tablename__ = "test_results"
    __id_comment__ = "Идентификатор результата теста"
    __table_args__ = (
        UniqueConstraint("import_id", "position"),
        {"comment": "Результаты тестов из принятых архивов Allure"},
    )

    import_id: Mapped[UUID] = mapped_column(
        ForeignKey("imports.id"), comment="Импорт, содержащий результат"
    )
    position: Mapped[int] = mapped_column(comment="Порядок результата в архиве")
    name: Mapped[str] = mapped_column(Text, comment="Название теста из Allure")


class TestStep(UUIDPrimaryKeyMixin, ExecutionTimeMixin, Base):
    """Шаг теста со ссылкой на родителя при вложении."""

    __tablename__ = "test_steps"
    __id_comment__ = "Идентификатор шага"
    __table_args__: ClassVar[dict[str, str]] = {
        "comment": "Шаги тестов Allure и связи между вложенными шагами"
    }

    test_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_results.id"), comment="Тест, которому принадлежит шаг"
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("test_steps.id"), comment="Родительский шаг, если шаг вложен"
    )
    position: Mapped[int] = mapped_column(comment="Порядок шага среди соседних шагов")
    name: Mapped[str] = mapped_column(Text, comment="Название шага из Allure")
