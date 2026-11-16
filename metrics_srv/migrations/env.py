"""Окружение Alembic для миграций TestBridge."""

from alembic import context
from sqlalchemy.engine import Connection

from metrics_srv.models import Base


def run_migrations() -> None:
    """Применить ревизии через соединение, переданное вызывающей командой."""
    connection = context.config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise TypeError("Для запуска миграций требуется соединение с PostgreSQL")
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
