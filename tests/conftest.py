"""Изолированные настройки и клиенты без внешних сервисов."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cleaning_kb.app import create_app
from cleaning_kb.config import Settings
from cleaning_kb.credentials import hash_password

PASSWORD = "test-password-это-не-секрет"
API_KEY = "test-api-key-only-0123456789-abcdef"


@pytest.fixture(scope="session")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def settings(tmp_path: Path, password_hash: str) -> Settings:
    return Settings(
        _env_file=None,
        app_environment="test",
        database_path=tmp_path / "application.sqlite3",
        admin_password_hash=password_hash,
        api_key=API_KEY,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(
        create_app(settings, qdrant_probe=lambda: True), base_url="http://127.0.0.1:8000"
    ) as test_client:
        yield test_client
