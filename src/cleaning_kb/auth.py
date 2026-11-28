"""Одна учётная запись, отзываемые сессии и отдельный API-ключ."""

import hashlib
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

from fastapi import Request

from cleaning_kb.config import Settings
from cleaning_kb.credentials import verify_password
from cleaning_kb.database import Database
from cleaning_kb.errors import AppError
from cleaning_kb.logging_config import log_event

COOKIE_NAME = "cleaning_kb_session"


def token_hash(token: str) -> str:
    """Сохранить в БД только отпечаток сессионного секрета."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authentication_error() -> AppError:
    """Вернуть одинаковую ошибку для неверного ключа и отсутствующей сессии."""
    return AppError(401, "authentication_required", "Требуется вход или ключ API", "authentication")


@dataclass(frozen=True)
class Session:
    """Проверенная серверная сессия без исходного cookie."""

    token_hash: str
    csrf_token: str
    actor_id: str


class AuthService:
    """Состояние доступа одного процесса; сессии переживают перезапуск."""

    def __init__(
        self, settings: Settings, database: Database, clock: Callable[[], float] = time.time
    ) -> None:
        self.settings, self.database, self.clock = settings, database, clock
        self._attempts: deque[float] = deque()
        self._lock = Lock()

    def _reserve_attempt(self) -> None:
        now = self.clock()
        with self._lock:
            while self._attempts and self._attempts[0] <= now - 60:
                self._attempts.popleft()
            if len(self._attempts) >= self.settings.login_attempts_per_minute:
                raise AppError(
                    429,
                    "login_rate_limited",
                    "Слишком много попыток входа. Повторите позже",
                    "rate_limit",
                )
            self._attempts.append(now)

    def login(self, username: str, password: str, request_id: str) -> tuple[str, Session]:
        """Проверить пароль и выдать новую сессию, не записывая реквизиты в аудит."""
        started = time.monotonic()
        self._reserve_attempt()
        password_ok = verify_password(
            password, self.settings.admin_password_hash.get_secret_value()
        )
        username_ok = secrets.compare_digest(
            username.encode("utf-8"), self.settings.admin_username.encode("utf-8")
        )
        if not password_ok or not username_ok:
            self.database.audit(
                "auth_login",
                request_id,
                status="error",
                error="invalid_credentials",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            log_event("authentication_failed", level=logging.WARNING)
            raise AppError(401, "invalid_credentials", "Неверные реквизиты входа", "authentication")
        token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(self.clock())
        with self.database.connect() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO auth_sessions "
                "(token_hash, csrf_token, created_at, expires_at, actor_id) VALUES (?, ?, ?, ?, ?)",
                (
                    token_hash(token),
                    csrf_token,
                    now,
                    now + self.settings.session_ttl_seconds,
                    self.settings.admin_username,
                ),
            )
            self.database.audit(
                "auth_login",
                request_id,
                duration_ms=int((time.monotonic() - started) * 1000),
                connection=connection,
            )
        log_event("authentication_succeeded")
        return token, Session(token_hash(token), csrf_token, self.settings.admin_username)

    def session(self, token: str | None) -> Session:
        """Проверить наличие и срок сессии в БД."""
        if not token or len(token) > 128:
            raise authentication_error()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT csrf_token, expires_at, actor_id FROM auth_sessions WHERE token_hash = ?",
                (token_hash(token),),
            ).fetchone()
        if row is None or row["expires_at"] <= int(self.clock()):
            raise authentication_error()
        return Session(
            token_hash(token),
            str(row["csrf_token"]),
            row["actor_id"] or self.settings.admin_username,
        )

    def authorize(self, request: Request) -> None:
        """Ключ API имеет явный приоритет; cookie требует CSRF для изменений."""
        key = request.headers.get("x-api-key")
        if key is not None:
            if len(key) > 256 or not secrets.compare_digest(
                key.encode("utf-8"), self.settings.api_key.get_secret_value().encode("utf-8")
            ):
                raise authentication_error()
            request.state.actor_type = "api_key"
            request.state.actor_id = "service-api"
            return
        session = self.session(request.cookies.get(COOKIE_NAME))
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            submitted = request.headers.get("x-csrf-token", "")
            if len(submitted) > 128 or not secrets.compare_digest(
                submitted.encode("utf-8"), session.csrf_token.encode("utf-8")
            ):
                raise AppError(
                    403, "csrf_failed", "Проверка защиты запроса не пройдена", "authorization"
                )
        request.state.actor_type = "user"
        request.state.actor_id = session.actor_id

    def logout(self, session: Session, request_id: str) -> None:
        """Удалить серверную сессию, включая возможность повторить старый cookie."""
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?", (session.token_hash,)
            )
            self.database.audit("auth_logout", request_id, connection=connection)
        log_event("session_revoked")
