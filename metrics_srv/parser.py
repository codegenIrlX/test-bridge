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
    status: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedTest:
    """Результат теста с шагами и временем выполнения."""

    name: str
    start: int | None
    stop: int | None
    steps: list[ParsedStep]
    status: str | None = None
    uuid: str | None = None
    history_id: str | None = None
    test_case_id: str | None = None
    full_name: str | None = None
    labels: list[dict[str, str]] | None = None
    parameters: list[dict] | None = None


def _optional_text(value: dict, key: str) -> str | None:
    result = value.get(key)
    if result is not None and not isinstance(result, str):
        raise InvalidArchiveError("Некорректное поле результата Allure")
    return result


def _parse_test(value: object) -> ParsedTest:
    item = _parse_item(value)
    # _parse_item validates the root as an object.
    labels = value.get("labels", [])
    parameters = value.get("parameters", [])
    if not isinstance(labels, list) or not isinstance(parameters, list):
        raise InvalidArchiveError("Некорректные метки или параметры Allure")
    selected = {"suite", "parentSuite", "subSuite", "tag", "framework", "language"}
    parsed_labels = []
    for label in labels:
        if not isinstance(label, dict):
            raise InvalidArchiveError("Некорректная метка Allure")
        name, label_value = label.get("name"), label.get("value")
        if not isinstance(name, str) or not isinstance(label_value, str):
            raise InvalidArchiveError("Некорректная метка Allure")
        if name in selected:
            parsed_labels.append({"name": name, "value": label_value})
    if any(not isinstance(parameter, dict) for parameter in parameters):
        raise InvalidArchiveError("Некорректный параметр Allure")
    parsed_parameters = []
    for parameter in parameters:
        mode = parameter.get("mode")
        if mode is not None and not isinstance(mode, str):
            raise InvalidArchiveError("Некорректный режим параметра Allure")
        if mode and mode.lower() in {"masked", "hidden"}:
            parsed_parameters.append({key: parameter[key] for key in ("name", "mode") if key in parameter})
        else:
            parsed_parameters.append(parameter)
    return ParsedTest(
        item.name, item.start, item.stop, item.steps, item.status,
        _optional_text(value, "uuid"), _optional_text(value, "historyId"),
        _optional_text(value, "testCaseId"), _optional_text(value, "fullName"),
        parsed_labels, parsed_parameters,
    )


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
    return ParsedStep(
        name, times[0], times[1], [_parse_item(step, depth + 1) for step in steps],
        _optional_text(value, "status"),
    )


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
                result.append(_parse_test(json.loads(archive.read(entry))))
            return result
    except (BadZipFile, EOFError, json.JSONDecodeError, UnicodeDecodeError, RuntimeError) as exc:
        raise InvalidArchiveError("Не удалось прочитать результаты Allure") from exc
