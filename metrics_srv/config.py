"""Загрузка и проверка конфигурации сервиса из YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class SettingsError(ValueError):
    """Настройки отсутствуют или имеют неверный формат."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Проверенные настройки процессов платформы."""

    database_host: str
    database_port: int
    database_name: str
    database_user: str
    database_password: str = field(repr=False)
    redis_host: str
    redis_port: int
    storage_path: Path
    max_archive_bytes: int
    max_uncompressed_bytes: int
    max_result_files: int
    scheduler_interval_seconds: int
    api_token: str = field(repr=False)
    metrics_projects: tuple[str, ...] = ()


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise SettingsError(f"Не удалось прочитать файл настроек: {path.name}") from None
    if not isinstance(value, dict):
        raise SettingsError(f"Корнем файла {path.name} должен быть объект YAML")
    return value


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = base.copy()
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise SettingsError(f"Раздел {name} должен быть объектом YAML")
    return value


def _environment(data: dict[str, Any], name: str) -> dict[str, Any]:
    if name != "default" and name not in data:
        raise SettingsError(f"Окружение {name} отсутствует в файле настроек")
    return _merge(_section(data, "default"), _section(data, name))


def _text(section: dict[str, Any], name: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise SettingsError(f"Параметр {name} должен быть непустой строкой без перевода строки")
    return value


def _positive_int(section: dict[str, Any], name: str, maximum: int | None = None) -> int:
    value = section.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SettingsError(f"Параметр {name} должен быть положительным целым числом")
    if maximum is not None and value > maximum:
        raise SettingsError(f"Параметр {name} превышает допустимый предел")
    return value


def _metrics_projects(config: dict[str, Any]) -> tuple[str, ...]:
    projects = _section(config, "metrics").get("allowed_projects", [])
    if not isinstance(projects, list) or len(projects) > 50 or any(
        not isinstance(project, str) or not project.strip() or len(project) > 255
        or project in ("unknown", "other")
        or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in project)
        for project in projects
    ):
        raise SettingsError("metrics.allowed_projects должен содержать до 50 имён проектов")
    return tuple(dict.fromkeys(projects))


def load_settings(
    config_path: Path = Path("config.yaml"), secrets_path: Path | None = None
) -> Settings:
    """Прочитать настройки выбранного окружения и проверить обязательные поля."""
    environment = os.getenv("METRICS_SRV_ENV", "development")
    if not environment or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in environment):
        raise SettingsError("METRICS_SRV_ENV должен содержать имя одного окружения")

    if secrets_path is None:
        docker_secret = Path("/run/secrets/secrets.yaml")
        secrets_path = docker_secret if docker_secret.is_file() else Path("secrets.yaml")
    config = _environment(_read_yaml(config_path), environment)
    secrets = _environment(_read_yaml(secrets_path), environment)
    database = _section(config, "database")
    redis = _section(config, "redis")
    storage = _section(config, "storage")
    scheduler = _section(config, "scheduler")
    storage_path = Path(_text(storage, "path"))
    required_storage_path = os.getenv("METRICS_SRV_REQUIRED_STORAGE_PATH")
    if required_storage_path is not None and storage_path != Path(required_storage_path):
        raise SettingsError("Параметр storage.path должен совпадать с путём общего тома архивов")
    return Settings(
        database_host=_text(database, "host"),
        database_port=_positive_int(database, "port", 65535),
        database_name=_text(database, "name"),
        database_user=_text(database, "user"),
        database_password=_text(_section(secrets, "database"), "password"),
        redis_host=_text(redis, "host"),
        redis_port=_positive_int(redis, "port", 65535),
        storage_path=storage_path,
        max_archive_bytes=_positive_int(storage, "max_archive_bytes"),
        max_uncompressed_bytes=_positive_int(storage, "max_uncompressed_bytes"),
        max_result_files=_positive_int(storage, "max_result_files"),
        scheduler_interval_seconds=_positive_int(scheduler, "interval_seconds"),
        api_token=_text(_section(secrets, "api"), "token"),
        metrics_projects=_metrics_projects(config),
    )
