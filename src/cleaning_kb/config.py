"""Настройки из окружения и локального .env без раскрытия секретов."""

import re
from pathlib import Path
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cleaning_kb.credentials import validate_password_hash


class ConfigurationError(RuntimeError):
    """Ошибка настроек с безопасным сообщением."""


class Settings(BaseSettings):
    """Конфигурация одного экземпляра приложения."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_environment: Literal["local", "test", "production"] = "local"
    app_version: str = "0.1.0"
    app_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    database_path: Path = Path("output/cleaning-kb.sqlite3")
    admin_username: str = "manager"
    admin_password_hash: SecretStr
    api_key: SecretStr
    session_ttl_seconds: int = Field(default=28_800, ge=60, le=86_400)
    login_attempts_per_minute: int = Field(default=10, ge=1, le=100)
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    qdrant_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:6333")
    qdrant_api_key: SecretStr | None = None
    dependency_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    proxyapi_key: SecretStr | None = None
    proxyapi_base_url: str = "https://api.proxyapi.ru/v1"
    generation_model: str = "openai/gpt-4.1-mini"
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dimension: int = Field(default=1536, ge=1, le=3072)
    provider_timeout_seconds: float = Field(default=45, ge=1, le=120)
    provider_calls_per_minute: int = Field(default=20, ge=1, le=100)
    provider_calls_per_day: int = Field(default=200, ge=1, le=10000)
    provider_token_budget_per_day: int = Field(default=500_000, ge=1000, le=100_000_000)
    max_answer_tokens: int = Field(default=1600, ge=256, le=4096)
    max_document_chars: int = Field(default=30_000, ge=1000, le=100_000)
    max_context_chars: int = Field(default=14_000, ge=1000, le=24_000)
    retrieval_top_k: int = Field(default=8, ge=1, le=20)
    retrieval_min_score: float = Field(default=0.2, ge=0, le=1)
    confidence_threshold: float = Field(default=0.7, ge=0, le=1)
    dialog_max_tokens: int = Field(default=16_000, ge=2000, le=40_000)
    summary_max_tokens: int = Field(default=2400, ge=512, le=4096)

    @field_validator("proxyapi_base_url")
    @classmethod
    def validate_provider_url(cls, value: str) -> str:
        """Ключ отправляется только фиксированному доверенному HTTPS-сервису."""
        if value.rstrip("/") != "https://api.proxyapi.ru/v1":
            raise ValueError("Поддерживается HTTPS-адрес единого API ProxyAPI")
        return value.rstrip("/")

    @field_validator("generation_model", "embedding_model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        """Идентификатор модели не должен содержать управляющие символы."""
        if not re.fullmatch(r"[A-Za-z0-9_./:-]{1,120}", value):
            raise ValueError("Некорректный идентификатор модели")
        return value

    @field_validator("qdrant_api_key", "proxyapi_key", mode="before")
    @classmethod
    def empty_optional_key(cls, value: object) -> object:
        """Пустое поле в .env означает отсутствие необязательного ключа."""
        return None if value == "" else value

    @field_validator("app_version", "admin_username")
    @classmethod
    def validate_label(cls, value: str) -> str:
        """Не допускать управляющие символы и неограниченные значения."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
            raise ValueError("Нужны латинские буквы, цифры, точка, дефис или подчёркивание")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        """Отклонить короткие ключи и очевидные заглушки."""
        raw = value.get_secret_value()
        if (
            not 32 <= len(raw) <= 256
            or len(set(raw)) < 8
            or not raw.isascii()
            or any(character.isspace() for character in raw)
            or any(marker in raw.lower() for marker in ("replace", "change-me", "example"))
        ):
            raise ValueError("Нужен отдельный случайный ключ длиной от 32 символов")
        return value

    @field_validator("admin_password_hash")
    @classmethod
    def validate_hash(cls, value: SecretStr) -> SecretStr:
        """Проверить формат без вывода значения хеша."""
        if not validate_password_hash(value.get_secret_value()):
            raise ValueError("Нужен хеш, созданный командой init-env")
        return value

    @field_validator("app_base_url", "qdrant_url")
    @classmethod
    def validate_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Адрес сервиса не должен содержать пароль, query или вложенный путь."""
        if value.username or value.password or value.query or value.fragment or value.path != "/":
            raise ValueError("Нужен базовый HTTP(S)-адрес без credentials, пути и query")
        return value

    @model_validator(mode="after")
    def validate_production(self) -> Self:
        """Публичная сессия разрешена только по HTTPS."""
        if self.app_environment == "production" and self.app_base_url.scheme != "https":
            raise ValueError("В production APP_BASE_URL должен использовать HTTPS")
        return self

    @property
    def secure_cookies(self) -> bool:
        """Использовать защищённый cookie на HTTPS."""
        return self.app_base_url.scheme == "https"

    @property
    def origin(self) -> str:
        """Вернуть согласованный origin без завершающего слеша."""
        return str(self.app_base_url).rstrip("/")


def load_settings() -> Settings:
    """Прочитать настройки, заменив подробности валидации безопасной ошибкой."""
    try:
        return Settings()
    except ValidationError as error:
        fields = sorted({str(item["loc"][0]) for item in error.errors() if item["loc"]})
        raise ConfigurationError(
            "Проверьте настройки .env и окружения: " + (", ".join(fields) or "общие ограничения")
        ) from None
