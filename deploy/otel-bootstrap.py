"""Подготовка файла токена Collector во временном томе."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from metrics_srv.config import _environment, _read_yaml, _section, _text


def main() -> None:
    """Передать Collector только сервисный токен API."""
    data = _read_yaml(Path("/run/secrets/secrets.yaml"))
    settings = _environment(data, os.getenv("METRICS_SRV_ENV", "development"))
    token = _text(_section(settings, "api"), "token")
    target = Path("/run/otel-auth")
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(token)
            stream.flush()
            temporary.chmod(0o444)
            temporary.replace(target / "token")
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
