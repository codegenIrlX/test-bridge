"""Подготовить секрет приложения в отдельном временном томе."""

import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile


def main() -> None:
    """Скопировать секрет с правом чтения только для пользователя приложения."""
    source = Path("/run/secrets/source.yaml")
    target_dir = Path("/run/secret-volume")
    with NamedTemporaryFile(dir=target_dir, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, stream)
            stream.flush()
            os.chown(temporary, 10001, -1)
            temporary.chmod(0o400)
            temporary.replace(target_dir / "secrets.yaml")
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
