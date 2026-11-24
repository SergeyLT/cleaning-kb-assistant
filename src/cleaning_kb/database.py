"""SQLite: подключения, атомарные миграции и технический аудит."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path


class MigrationError(RuntimeError):
    """Версии схемы не соответствуют поставленному приложению."""


def utc_now() -> str:
    """Вернуть время UTC в едином формате."""
    return datetime.now(UTC).isoformat()


def sql_statements(script: str) -> Iterator[str]:
    """Разбить миграцию, сохраняя управление транзакцией у вызывающего кода."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if buffer.strip():
        raise MigrationError("Миграция содержит незавершённую SQL-команду")


class Database:
    """Короткие соединения с foreign keys и ограниченным ожиданием блокировки."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Открыть существующую базу; не создавать потерянный файл при чтении."""
        uri = self.path.resolve().as_uri() + "?mode=rw"
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Создать файл и применить миграции с проверкой контрольных сумм."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Создание файла разрешено только при инициализации.
        initial = sqlite3.connect(self.path, timeout=3)
        try:
            initial.execute("PRAGMA journal_mode = WAL")
        finally:
            initial.close()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            migrations = sorted(
                (
                    entry
                    for entry in files("cleaning_kb").joinpath("migrations").iterdir()
                    if entry.name.endswith(".sql")
                ),
                key=lambda entry: entry.name,
            )
            known = {entry.name for entry in migrations}
            applied = {
                row["name"]: row["checksum"]
                for row in connection.execute("SELECT name, checksum FROM schema_migrations")
            }
            if set(applied) - known:
                raise MigrationError(
                    "База создана более новой или несовместимой версией приложения"
                )
            for migration in migrations:
                script = migration.read_text(encoding="utf-8")
                checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
                if migration.name in applied:
                    if applied[migration.name] != checksum:
                        raise MigrationError("Изменена уже применённая миграция")
                    continue
                for statement in sql_statements(script):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                    (migration.name, checksum, utc_now()),
                )

    def check(self) -> bool:
        """Проверить доступ к существующим обязательным таблицам."""
        with self.connect() as connection:
            connection.execute("SELECT id FROM documents LIMIT 1")
            connection.execute("SELECT token_hash FROM auth_sessions LIMIT 1")
            return connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] > 0

    def audit(
        self,
        action: str,
        request_id: str,
        *,
        status: str = "ok",
        error: str | None = None,
        duration_ms: int = 0,
        conversation_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Записать техническую операцию доступа без реквизитов и cookie."""
        if connection is None:
            with self.connect() as transaction:
                self.audit(
                    action,
                    request_id,
                    status=status,
                    error=error,
                    duration_ms=duration_ms,
                    conversation_id=conversation_id,
                    connection=transaction,
                )
        else:
            connection.execute(
                "INSERT INTO audit_runs "
                "(created_at, request_id, action, input, output, status, error, "
                "duration_ms, conversation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    utc_now(),
                    request_id,
                    action,
                    "{}",
                    json.dumps({"status": status}),
                    status,
                    error,
                    max(0, duration_ms),
                    conversation_id,
                ),
            )
