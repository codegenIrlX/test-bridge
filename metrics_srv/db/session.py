"""Подключения и сессии SQLAlchemy."""

from sqlalchemy import URL, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from metrics_srv.config import Settings


def create_database_engine(settings: Settings) -> Engine:
    """Создаёт движок с физическим закрытием соединений при возврате."""
    url = URL.create(
        "postgresql+psycopg",
        username=settings.database_user,
        password=settings.database_password,
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
    )
    return create_engine(url, poolclass=NullPool, hide_parameters=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Создаёт фабрику синхронных сессий для операций импорта."""
    return sessionmaker(engine)
