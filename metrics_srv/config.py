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
    metrics_project_allowlist: tuple[str, ...] = ()
    metrics_suite_allowlist: tuple[str, ...] = ()


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


def _text_list(section: dict[str, Any], name: str) -> tuple[str, ...]:
    value = section.get(name, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() or "\n" in item or "\r" in item
        for item in value
    ):
        raise SettingsError(f"Параметр {name} должен быть списком непустых строк")
    if len(value) != len(set(value)):
        raise SettingsError(f"Параметр {name} содержит повторяющиеся значения")
    if any(item.casefold() in {"other", "unknown"} for item in value):
        raise SettingsError(f"Параметр {name} содержит зарезервированное значение")
    return tuple(value)


def _selected_environment() -> str:
    environment = os.getenv("METRICS_SRV_ENV", "development")
    if not environment or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in environment):
        raise SettingsError("METRICS_SRV_ENV должен содержать имя одного окружения")
    return environment


def _secrets_path(secrets_path: Path | None) -> Path:
    if secrets_path is None:
        docker_secret = Path("/run/secrets/secrets.yaml")
        secrets_path = docker_secret if docker_secret.is_file() else Path("secrets.yaml")
    return secrets_path


def load_grafana_database_password(secrets_path: Path | None = None) -> str:
    """Прочитать пароль учётной записи Grafana для выбранного окружения."""
    secrets = _environment(_read_yaml(_secrets_path(secrets_path)), _selected_environment())
    return _text(_section(secrets, "grafana"), "database_password")


def load_settings(
    config_path: Path = Path("config.yaml"), secrets_path: Path | None = None
) -> Settings:
    """Прочитать настройки выбранного окружения и проверить обязательные поля."""
    environment = _selected_environment()
    secrets_path = _secrets_path(secrets_path)
    config = _environment(_read_yaml(config_path), environment)
    secrets = _environment(_read_yaml(secrets_path), environment)
    database = _section(config, "database")
    redis = _section(config, "redis")
    storage = _section(config, "storage")
    scheduler = _section(config, "scheduler")
    metrics = _section(config, "metrics")
    return Settings(
        database_host=_text(database, "host"),
        database_port=_positive_int(database, "port", 65535),
        database_name=_text(database, "name"),
        database_user=_text(database, "user"),
        database_password=_text(_section(secrets, "database"), "password"),
        redis_host=_text(redis, "host"),
        redis_port=_positive_int(redis, "port", 65535),
        storage_path=Path(_text(storage, "path")),
        max_archive_bytes=_positive_int(storage, "max_archive_bytes"),
        max_uncompressed_bytes=_positive_int(storage, "max_uncompressed_bytes"),
        max_result_files=_positive_int(storage, "max_result_files"),
        scheduler_interval_seconds=_positive_int(scheduler, "interval_seconds"),
        api_token=_text(_section(secrets, "api"), "token"),
        metrics_project_allowlist=_text_list(metrics, "project_allowlist"),
        metrics_suite_allowlist=_text_list(metrics, "suite_allowlist"),
    )
