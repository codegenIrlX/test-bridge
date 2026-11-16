"""Настройка подключения к PostgreSQL."""

from sqlalchemy import URL, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from metrics_srv.config import Settings


def create_database_engine(settings: Settings) -> Engine:
    """Создать движок с отдельным физическим соединением на каждое открытие."""
    url = URL.create(
        "postgresql+psycopg",
        username=settings.database_user,
        password=settings.database_password,
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
    )
    return create_engine(url, poolclass=NullPool, hide_parameters=True)
