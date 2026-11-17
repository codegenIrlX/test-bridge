"""Запуск миграций схемы TestBridge."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection

from metrics_srv.config import Settings
from metrics_srv.db.session import create_database_engine


def build_alembic_config(connection: Connection | None = None) -> Config:
    """Находит ревизии независимо от рабочего каталога."""
    config = Config()
    location = str(Path(__file__).resolve().parent)
    config.set_main_option("script_location", location.replace("%", "%%"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def upgrade_database(settings: Settings) -> None:
    """Применяет миграции до последней ревизии."""
    engine = create_database_engine(settings)
    try:
        with engine.begin() as connection:
            command.upgrade(build_alembic_config(connection), "head")
    finally:
        engine.dispose()
