"""Подготовка отдельных файлов секретов Collector и Grafana."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from psycopg import connect, sql

from metrics_srv.config import Settings, _environment, _read_yaml, _section, _text, load_settings

_GRAFANA_READER = "testbridge_grafana_reader"


def write_secret(path: Path, value: str, uid: int) -> None:
    """Атомарно заменяет файл, доступный владельцу на чтение."""
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(value)
            stream.flush()
            os.chown(temporary, uid, -1)
            temporary.chmod(0o400)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _configure_grafana_reader(settings: Settings, password: str) -> None:
    """Даёт Grafana учётные данные для чтения представлений."""
    role = sql.Identifier(_GRAFANA_READER)
    with connect(
        host=settings.database_host,
        port=settings.database_port,
        dbname=settings.database_name,
        user=settings.database_user,
        password=settings.database_password,
        connect_timeout=5,
    ) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_GRAFANA_READER,))
        existing = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (_GRAFANA_READER,)
        ).fetchone()
        if existing is None:
            conn.execute(sql.SQL("CREATE ROLE {}").format(role))
        encoding = conn.info.encoding
        verifier = conn.pgconn.encrypt_password(
            password.encode(encoding), _GRAFANA_READER.encode(encoding), b"scram-sha-256"
        ).decode("ascii")
        conn.execute(sql.SQL(
            "ALTER ROLE {} WITH LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS PASSWORD {}"
        ).format(role, sql.Literal(verifier)))
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        conn.execute(sql.SQL(
            "GRANT SELECT ON public.dashboard_imports, public.dashboard_attempts, "
            "public.dashboard_final_results TO {}"
        ).format(role))


def main() -> None:
    """Готовит учётные данные источников аналитики."""
    secrets_path = Path("/run/app-secrets/secrets.yaml")
    settings = load_settings(secrets_path=secrets_path)
    secrets = _environment(_read_yaml(secrets_path), os.getenv("METRICS_SRV_ENV", "development"))
    grafana = _section(secrets, "grafana")
    admin_password = _text(grafana, "password")
    database_password = _text(grafana, "database_password")
    _configure_grafana_reader(settings, database_password)

    host = settings.database_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = Path("/run/grafana-auth")
    write_secret(Path("/run/otel-auth/token"), settings.api_token, uid=10001)
    write_secret(auth / "password", admin_password, uid=472)
    write_secret(auth / "database-password", database_password, uid=472)
    write_secret(auth / "database-url", f"{host}:{settings.database_port}", uid=472)
    write_secret(auth / "database-name", settings.database_name, uid=472)


if __name__ == "__main__":
    main()
