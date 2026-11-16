"""Чтение результатов Allure из ZIP-архива."""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from lzma import LZMAError
from pathlib import Path, PurePosixPath
from zipfile import ZIP_BZIP2, BadZipFile, ZipFile, ZipInfo

_ZSTD_ERRORS: tuple[type[Exception], ...]
try:
    from compression.zstd import ZstdError
except ImportError:
    _ZSTD_ERRORS = ()
else:
    _ZSTD_ERRORS = (ZstdError,)

_MAX_BIGINT = (1 << 63) - 1
_ZIP_READ_ERRORS = (
    BadZipFile, EOFError, UnicodeDecodeError, RuntimeError, zlib.error, LZMAError, *_ZSTD_ERRORS
)


class InvalidArchiveError(ValueError):
    """Архив не содержит корректные результаты Allure."""


@dataclass(frozen=True, slots=True)
class ParsedStep:
    """Шаг теста и его вложенные шаги."""

    name: str
    start: int | None
    stop: int | None
    steps: list[ParsedStep]


@dataclass(frozen=True, slots=True)
class ParsedTest:
    """Результат теста с шагами и временем выполнения."""

    name: str
    start: int | None
    stop: int | None
    steps: list[ParsedStep]


def _parse_item(value: object, depth: int = 0) -> ParsedStep:
    if depth > 100 or not isinstance(value, dict):
        raise InvalidArchiveError("Некорректная структура теста или шага")
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InvalidArchiveError("Отсутствует название теста или шага")
    if "\x00" in name:
        raise InvalidArchiveError("Некорректное название теста или шага")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidArchiveError("Некорректное название теста или шага") from exc
    times = []
    for key in ("start", "stop"):
        time = value.get(key)
        if time is not None and (
            not isinstance(time, int) or isinstance(time, bool) or not 0 <= time <= _MAX_BIGINT
        ):
            raise InvalidArchiveError("Некорректное время теста или шага")
        times.append(time)
    steps = value.get("steps", [])
    if not isinstance(steps, list):
        raise InvalidArchiveError("Некорректный список шагов")
    return ParsedStep(name, times[0], times[1], [_parse_item(step, depth + 1) for step in steps])


def _read_result(archive: ZipFile, entry: ZipInfo) -> object:
    """Прочитать JSON одного результата и распознать ошибки его содержимого."""
    try:
        payload = archive.read(entry)
    except _ZIP_READ_ERRORS as exc:
        raise InvalidArchiveError("Не удалось прочитать результаты Allure") from exc
    except OSError as exc:
        # BZIP2 сообщает о повреждении потока через OSError, как и файловый ввод-вывод.
        if entry.compress_type == ZIP_BZIP2 and exc.args == ("Invalid data stream",):
            raise InvalidArchiveError("Не удалось прочитать результаты Allure") from exc
        raise
    try:
        return json.loads(payload)
    except (ValueError, RecursionError) as exc:
        raise InvalidArchiveError("Не удалось прочитать результаты Allure") from exc


def parse_archive(
    path: Path, max_uncompressed_bytes: int, max_result_files: int
) -> list[ParsedTest]:
    """Прочитать исходные файлы результатов без извлечения вложений."""
    try:
        with ZipFile(path) as archive:
            entries = [
                entry for entry in archive.infolist()
                if not entry.is_dir() and PurePosixPath(entry.filename).name.endswith("-result.json")
            ]
            if not entries:
                raise InvalidArchiveError("В архиве нет файлов результатов Allure")
            if len(entries) > max_result_files:
                raise InvalidArchiveError("Превышено допустимое число результатов")
            if sum(entry.file_size for entry in entries) > max_uncompressed_bytes:
                raise InvalidArchiveError("Превышен размер результатов после распаковки")
            result = []
            for entry in entries:
                item = _parse_item(_read_result(archive, entry))
                result.append(ParsedTest(item.name, item.start, item.stop, item.steps))
            return result
    except (BadZipFile, EOFError, UnicodeDecodeError) as exc:
        raise InvalidArchiveError("Не удалось прочитать результаты Allure") from exc
