"""Подготовка отдельного файла пароля Grafana во временном томе."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from metrics_srv.config import _environment, _read_yaml, _section, _text


def main() -> None:
    """Передать Grafana только её пароль из общего файла секретов."""
    data = _read_yaml(Path("/run/secrets/secrets.yaml"))
    settings = _environment(data, os.getenv("METRICS_SRV_ENV", "development"))
    password = _text(_section(settings, "grafana"), "password")
    target = Path("/run/grafana-auth")
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(password)
            stream.flush()
            temporary.chmod(0o444)
            temporary.replace(target / "password")
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
