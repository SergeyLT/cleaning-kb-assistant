"""Ошибки настроек и безопасная первичная настройка."""

from pathlib import Path

import pytest
from conftest import API_KEY, PASSWORD
from pydantic import ValidationError

from cleaning_kb.__main__ import initialize_env
from cleaning_kb.config import ConfigurationError, Settings, load_settings
from cleaning_kb.credentials import verify_password


def test_initialize_env_never_overwrites_and_stores_only_hash(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    initialize_env(path, PASSWORD)
    original = path.read_text(encoding="utf-8")
    assert PASSWORD not in original
    settings = Settings(_env_file=path)
    assert verify_password(PASSWORD, settings.admin_password_hash.get_secret_value())
    with pytest.raises(FileExistsError):
        initialize_env(path, PASSWORD)
    assert path.read_text(encoding="utf-8") == original


def test_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    initialize_env(path, PASSWORD)
    monkeypatch.setenv("APP_VERSION", "override")
    assert Settings(_env_file=path).app_version == "override"


@pytest.mark.parametrize("key", ["", "short", "x" * 64, "replace-this-with-a-real-long-api-key"])
def test_invalid_keys_rejected(key: str, password_hash: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_password_hash=password_hash, api_key=key)


def test_unsafe_production_and_urls_rejected(password_hash: str) -> None:
    common = {"_env_file": None, "admin_password_hash": password_hash, "api_key": API_KEY}
    with pytest.raises(ValidationError):
        Settings(**common, app_environment="production")
    for url in ("http://name:password@host", "http://host/path", "http://host/?token=secret"):
        with pytest.raises(ValidationError):
            Settings(**common, qdrant_url=url)


def test_configuration_error_does_not_expose_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "sensitive-configuration-input")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "sensitive-password-input")
    with pytest.raises(ConfigurationError) as captured:
        load_settings()
    assert "sensitive" not in str(captured.value)
    assert "api_key" in str(captured.value)
