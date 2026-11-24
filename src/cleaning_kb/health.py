"""Проверки готовности без обращения к платному провайдеру."""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from cleaning_kb.database import Database
from cleaning_kb.logging_config import log_event


@dataclass
class Readiness:
    """Проверить обязательные зависимости; результаты не кешируются навсегда."""

    database: Database
    qdrant_probe: Callable[[], bool]

    def check(self) -> dict[str, bool]:
        """Вернуть безопасные результаты без адресов, ключей и внешних тел ответа."""
        try:
            sqlite_ok = self.database.check()
        except sqlite3.Error:
            sqlite_ok = False
        try:
            qdrant_ok = self.qdrant_probe()
        except (httpx.HTTPError, OSError):
            qdrant_ok = False
        checks = {"sqlite": sqlite_ok, "qdrant": qdrant_ok}
        for name, ready in checks.items():
            if not ready:
                log_event(
                    "dependency_unavailable",
                    level=logging.WARNING,
                    fields={
                        "component": name,
                        "operation": "readiness",
                        "stage": "dependency_check",
                        "error_category": "external_service" if name == "qdrant" else "database",
                        "error_code": f"{name}_unavailable",
                        "retryable": True,
                    },
                )
        return checks
