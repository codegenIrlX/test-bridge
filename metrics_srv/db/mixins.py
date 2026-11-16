"""Общие поля ORM-моделей."""

from typing import ClassVar
from uuid import UUID

from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class UUIDPrimaryKeyMixin:
    """Первичный ключ UUID, который задаёт вызывающий код."""

    __id_comment__: ClassVar[str]

    @declared_attr
    def id(cls) -> Mapped[UUID]:
        """Возвращает колонку ключа с описанием для конкретной таблицы."""
        return mapped_column(primary_key=True, comment=cls.__id_comment__, sort_order=-100)
