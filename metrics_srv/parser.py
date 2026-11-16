"""Чтение результатов тестов Allure из ZIP-архива."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile


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
    times = []
    for key in ("start", "stop"):
        time = value.get(key)
        if time is not None and (not isinstance(time, int) or isinstance(time, bool) or time < 0):
            raise InvalidArchiveError("Некорректное время теста или шага")
        times.append(time)
    steps = value.get("steps", [])
    if not isinstance(steps, list):
        raise InvalidArchiveError("Некорректный список шагов")
    return ParsedStep(name, times[0], times[1], [_parse_item(step, depth + 1) for step in steps])


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
                item = _parse_item(json.loads(archive.read(entry)))
                result.append(ParsedTest(item.name, item.start, item.stop, item.steps))
            return result
    except (BadZipFile, EOFError, json.JSONDecodeError, UnicodeDecodeError, RuntimeError) as exc:
        raise InvalidArchiveError("Не удалось прочитать результаты Allure") from exc
