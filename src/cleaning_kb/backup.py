"""Согласованная резервная копия SQLite через backup API, включая записи из WAL."""

import os
import sqlite3
from pathlib import Path

from cleaning_kb.database import Database


def backup_database(database: Database, destination: Path) -> None:
    """Не перезаписывать файл; копия содержит все прикладные данные и требует защиты."""
    if destination.resolve() == database.path.resolve():
        raise ValueError("Нельзя заменить рабочую базу резервной копией")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with database.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Нарушена целостность резервной копии")
            finally:
                target.close()
    except Exception:
        destination.unlink(missing_ok=True)
        raise
