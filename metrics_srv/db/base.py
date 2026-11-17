"""Метаданные SQLAlchemy для моделей и миграций."""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Единое соглашение об именах объектов схемы."""

    metadata = MetaData(
        naming_convention={
            "pk": "pk__%(table_name)s",
            "fk": "fk__%(table_name)s__%(column_0_N_name)s__%(referred_table_name)s",
            "uq": "uq__%(table_name)s__%(column_0_N_name)s",
            "ck": "ck__%(table_name)s__%(constraint_name)s",
            "ix": "ix__%(table_name)s__%(column_0_N_name)s",
        }
    )
