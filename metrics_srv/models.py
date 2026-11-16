"""Модели импортов, результатов и шагов Allure."""

from datetime import datetime
from typing import ClassVar
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    MetaData,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    """Общие метаданные моделей и миграций."""

    metadata = MetaData(
        naming_convention={
            "pk": "pk__%(table_name)s",
            "fk": "fk__%(table_name)s__%(column_0_N_name)s__%(referred_table_name)s",
            "uq": "uq__%(table_name)s__%(column_0_N_name)s",
            "ck": "ck__%(table_name)s__%(constraint_name)s",
            "ix": "ix__%(table_name)s__%(column_0_N_name)s",
        }
    )


class UUIDPrimaryKeyMixin:
    """Первичный ключ UUID, который задаёт вызывающий код."""

    __id_comment__: ClassVar[str]

    @declared_attr
    def id(cls) -> Mapped[UUID]:
        """Создать колонку ключа с описанием для конкретной таблицы."""
        return mapped_column(primary_key=True, comment=cls.__id_comment__, sort_order=-100)


class Import(UUIDPrimaryKeyMixin, Base):
    """Архив Allure и состояние его обработки."""

    __tablename__ = "imports"
    __id_comment__ = "Идентификатор импорта и имя архива"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="status",
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


class TestResult(UUIDPrimaryKeyMixin, Base):
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
    started_at_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="Начало теста в миллисекундах Unix"
    )
    stopped_at_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="Окончание теста в миллисекундах Unix"
    )


class TestStep(UUIDPrimaryKeyMixin, Base):
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
    started_at_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="Начало шага в миллисекундах Unix"
    )
    stopped_at_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="Окончание шага в миллисекундах Unix"
    )
