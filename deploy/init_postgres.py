"""Подготовить файлы настроек для штатного entrypoint PostgreSQL."""

from pathlib import Path
from tempfile import NamedTemporaryFile

from metrics_srv.config import load_settings


def main() -> None:
    """Записать реквизиты БД во временный общий том."""
    settings = load_settings()
    for target, name, value in (
        (Path("/run/postgres-bootstrap"), "user", settings.database_user),
        (Path("/run/postgres-bootstrap"), "database", settings.database_name),
        (Path("/run/postgres-bootstrap"), "password", settings.database_password),
    ):
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(value)
                stream.flush()
                temporary.chmod(0o444)
                temporary.replace(target / name)
            finally:
                temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
