"""Подготовка пароля Bull Board в отдельном временном томе."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from metrics_srv.config import _environment, _read_yaml, _section, _text


def main() -> None:
    """Записать пароль панели, не открывая ей доступ к секретам БД."""
    data = _read_yaml(Path("/run/secrets/secrets.yaml"))
    settings = _environment(data, os.getenv("METRICS_SRV_ENV", "development"))
    password = _text(_section(settings, "bull_board"), "password")
    target = Path("/run/board-auth")
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
