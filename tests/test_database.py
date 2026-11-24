"""Сохранность данных и атомарность миграций."""

import sqlite3
from pathlib import Path

import pytest

import cleaning_kb.database as database_module
from cleaning_kb.database import Database, MigrationError
from cleaning_kb.locking import InstanceLock


def test_migrations_preserve_data_and_foreign_keys(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO documents (id, created_at, title, text, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            ("sample", "now", "Пример", "Демонстрационный текст", "hash"),
        )
    database.initialize()
    with database.connect() as connection:
        assert (
            connection.execute("SELECT text FROM documents").fetchone()[0]
            == "Демонстрационный текст"
        )
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO snippets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("bad", "now", "missing", 0, "text", "hash", None, None, None),
            )


def test_reading_missing_database_does_not_create_it(tmp_path: Path) -> None:
    database = Database(tmp_path / "lost.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        database.check()
    assert not database.path.exists()


def test_failed_migration_rolls_back_whole_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resources = tmp_path / "migrations"
    resources.mkdir()
    (resources / "001.sql").write_text("CREATE TABLE sample (id INTEGER);\n", encoding="utf-8")
    (resources / "002.sql").write_text("INVALID SQL;\n", encoding="utf-8")
    monkeypatch.setattr(database_module, "files", lambda _: tmp_path)
    database = Database(tmp_path / "db.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connect() as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        )


def test_changed_or_newer_migration_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("UPDATE schema_migrations SET checksum = 'changed'")
    with pytest.raises(MigrationError, match="Изменена"):
        database.initialize()
    with database.connect() as connection:
        connection.execute(
            "UPDATE schema_migrations SET name = '999_future.sql' WHERE name='001_initial.sql'"
        )
    with pytest.raises(MigrationError, match="новой"):
        database.initialize()


def test_only_one_instance_can_hold_database(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    with InstanceLock(path):
        with pytest.raises(RuntimeError, match="другим экземпляром"):
            with InstanceLock(path):
                pytest.fail("Второй экземпляр не должен получить блокировку")
    with InstanceLock(path):
        pass


def test_stage1_database_upgrades_preserving_documents(tmp_path: Path, monkeypatch) -> None:
    """Поставленная миграция 001 не изменена, данные переживают обновление до 002."""
    original_files = database_module.files
    directory = tmp_path / "migrations"
    directory.mkdir()
    source = original_files("cleaning_kb").joinpath("migrations/001_initial.sql")
    (directory / "001_initial.sql").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(database_module, "files", lambda _: tmp_path)
    database = Database(tmp_path / "upgrade.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO documents (id, created_at, title, text, content_hash) "
            "VALUES ('old', 'now', 'Регламент', 'Текст', 'hash')"
        )
    monkeypatch.setattr(database_module, "files", original_files)
    database.initialize()
    with database.connect() as connection:
        assert (
            connection.execute("SELECT text FROM documents WHERE id='old'").fetchone()[0] == "Текст"
        )
        assert connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0] == 0
