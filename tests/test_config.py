from pathlib import Path

import pytest
import yaml

from metrics_srv.config import SettingsError, load_settings


@pytest.fixture
def configuration_files(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "config.yaml"
    secrets_path = tmp_path / "secrets.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "default": {
                    "database": {"host": "db", "port": 5432, "name": "tests", "user": "service"},
                    "redis": {"host": "redis", "port": 6379},
                    "storage": {
                        "path": "/data/imports",
                        "max_archive_bytes": 1024,
                        "max_uncompressed_bytes": 4096,
                        "max_result_files": 20,
                    },
                    "scheduler": {"interval_seconds": 5},
                },
                "development": {"database": {"host": "local-db"}},
                "staging": {"scheduler": {"interval_seconds": 10}},
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(
        yaml.safe_dump(
            {
                "default": {
                    "database": {"password": "test-password"},
                    "api": {"token": "test-token"},
                },
                "development": {},
                "staging": {"api": {"token": "staging-token"}},
            }
        ),
        encoding="utf-8",
    )
    return config_path, secrets_path


def test_default_environment_merges_configuration_without_exposing_secrets(
    configuration_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("METRICS_SRV_ENV", raising=False)

    settings = load_settings(*configuration_files)

    assert settings.database_host == "local-db"
    assert settings.database_port == 5432
    assert settings.storage_path == Path("/data/imports")
    assert settings.api_token == "test-token"
    assert "test-password" not in repr(settings)
    assert "test-token" not in repr(settings)


def test_selected_environment_merges_settings_and_secrets(
    configuration_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METRICS_SRV_ENV", "staging")

    settings = load_settings(*configuration_files)

    assert settings.database_host == "db"
    assert settings.database_password == "test-password"
    assert settings.api_token == "staging-token"
    assert settings.scheduler_interval_seconds == 10


def test_unknown_environment_is_rejected(
    configuration_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METRICS_SRV_ENV", "unknown")

    with pytest.raises(SettingsError):
        load_settings(*configuration_files)


def test_missing_secret_is_rejected(
    configuration_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METRICS_SRV_ENV", "development")
    config_path, secrets_path = configuration_files
    secrets_path.write_text("default: {}\ndevelopment: {}\n", encoding="utf-8")

    with pytest.raises(SettingsError):
        load_settings(config_path, secrets_path)


def test_invalid_limit_is_rejected(
    configuration_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METRICS_SRV_ENV", "development")
    config_path, secrets_path = configuration_files
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["default"]["storage"]["max_result_files"] = 0
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(SettingsError):
        load_settings(config_path, secrets_path)


@pytest.mark.parametrize("field", ["project_allowlist", "suite_allowlist"])
@pytest.mark.parametrize("reserved", ["other", "UNKNOWN", "Other", "unknown"])
def test_metrics_allowlist_rejects_reserved_labels(
    configuration_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    field: str, reserved: str,
) -> None:
    monkeypatch.setenv("METRICS_SRV_ENV", "development")
    config_path, secrets_path = configuration_files
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["default"]["metrics"] = {field: [reserved]}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(SettingsError):
        load_settings(config_path, secrets_path)
