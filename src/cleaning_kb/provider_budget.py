"""Динамические лимиты и сброс отсчёта без удаления истории провайдера."""

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from cleaning_kb.config import Settings
from cleaning_kb.database import Database
from cleaning_kb.errors import AppError
from cleaning_kb.logging_config import log_event
from cleaning_kb.rag_models import StrictModel


class ProviderLimits(StrictModel):
    """Изменяются только три порога, не ключи и модели."""

    calls_per_minute: int = Field(ge=1, le=100)
    calls_per_day: int = Field(ge=1, le=10000)
    token_budget_per_day: int = Field(ge=1000, le=100_000_000)


class BudgetReset(StrictModel):
    """Явное подтверждение операции поверх обычной авторизации/CSRF."""

    confirm: Literal[True]


class ProviderBudget:
    """SQLite-транзакция объединяет чтение порогов, учёт и изменение отсчёта."""

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings, self.database = settings, database

    def _limits(self, connection: sqlite3.Connection) -> dict[str, int]:
        row = connection.execute("SELECT * FROM provider_settings WHERE id=1").fetchone()
        return {
            key: row[key] if row[key] is not None else getattr(self.settings, "provider_" + key)
            for key in ("calls_per_minute", "calls_per_day", "token_budget_per_day")
        }

    @staticmethod
    def _counts(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        row = connection.execute("SELECT * FROM provider_settings WHERE id=1").fetchone()
        start = max(day.isoformat(), row["reset_at"] or day.isoformat())
        calls, reserved = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(reserved_tokens),0) FROM provider_calls "
            "WHERE created_at>=?",
            (start,),
        ).fetchone()
        recent = connection.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE created_at>=?",
            ((now - timedelta(minutes=1)).isoformat(),),
        ).fetchone()[0]
        return {
            "calls_per_minute": recent,
            "calls_per_day": calls,
            "token_budget_per_day": reserved,
            "period_started_at": start,
            "next_day_at": (day + timedelta(days=1)).isoformat(),
        }

    def reserve(self, kind: str, model: str, tokens: int, operation_id: str, now: datetime) -> str:
        """Отказ не создаёт provider_calls; успешная попытка всегда занимает резерв."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            limits = self._limits(connection)
            counts = self._counts(connection, now)
            messages = {
                "calls_per_minute": "Достигнут лимит вызовов за последние 60 секунд",
                "calls_per_day": "Достигнут суточный лимит вызовов",
                "token_budget_per_day": "Недостаточно суточного резерва токенов для запроса",
            }
            exceeded = [
                key
                for key in limits
                if counts[key] + (tokens if key == "token_budget_per_day" else 1) > limits[key]
            ]
            if exceeded:
                key = exceeded[0]
                log_event(
                    "provider_budget_rejected",
                    level=logging.WARNING,
                    fields={
                        "operation_id": operation_id,
                        "stage": kind,
                        "model": model,
                        "error_code": "provider_budget_exceeded",
                        "error_category": "rate_limit",
                        "limit_kind": key,
                        "exceeded_limits": exceeded,
                        "used": counts[key],
                        "limit": limits[key],
                        "requested": tokens if key == "token_budget_per_day" else 1,
                        "remaining": max(0, limits[key] - counts[key]),
                        "period_started_at": counts["period_started_at"],
                    },
                )
                raise AppError(429, "provider_budget_exceeded", messages[key], "rate_limit")
            identifier = str(uuid4())
            connection.execute(
                "INSERT INTO provider_calls VALUES (?, ?, ?, ?, ?, ?, 'started', NULL, NULL)",
                # Время вычислено до ожидания SQLite-lock: не отнести новый вызов
                # к периоду до параллельного сброса.
                (
                    identifier,
                    max(now.isoformat(), counts["period_started_at"]),
                    operation_id,
                    kind,
                    model,
                    tokens,
                ),
            )
            return identifier

    def status(self) -> dict[str, Any]:
        """Расход лимита и фактический reported usage показываются раздельно."""
        now = datetime.now(UTC)
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute("SELECT * FROM provider_settings WHERE id=1").fetchone()
            counts = self._counts(connection, now)
            limits = self._limits(connection)
            day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            usages = connection.execute(
                "SELECT usage_json FROM provider_calls WHERE created_at>=?", (day,)
            ).fetchall()
        reported = [json.loads(r["usage_json"]) for r in usages if r["usage_json"]]
        reported = [r for r in reported if r and isinstance(r, dict)]
        return {
            "limits": limits,
            "usage": {key: counts[key] for key in limits},
            "remaining": {key: max(0, limits[key] - counts[key]) for key in limits},
            "period_started_at": counts["period_started_at"],
            "next_day_at": counts["next_day_at"],
            "saved_overrides": row["calls_per_day"] is not None,
            "last_reset": {
                "at": row["reset_at"],
                "actor_type": row["reset_actor_type"],
                "actor_id": row["reset_actor_id"],
            }
            if row["reset_at"]
            else None,
            "actual_usage_today": {
                "total_tokens": sum(
                    r.get("total_tokens", r.get("prompt_tokens", 0) + r.get("completion_tokens", 0))
                    for r in reported
                ),
                "reported_calls": len(reported),
                "all_calls": len(usages),
            },
        }

    def save(
        self, limits: ProviderLimits, actor_type: str, actor_id: str, request_id: str
    ) -> dict[str, Any]:
        """Настройки действуют со следующего резервирования, без перезапуска."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = self._limits(connection)
            connection.execute(
                "UPDATE provider_settings SET calls_per_minute=?, calls_per_day=?, "
                "token_budget_per_day=? WHERE id=1",
                (limits.calls_per_minute, limits.calls_per_day, limits.token_budget_per_day),
            )
            self.database.audit(
                "provider_limits_updated",
                request_id,
                actor_type=actor_type,
                actor_id=actor_id,
                details={"previous": previous, "limits": limits.model_dump()},
                connection=connection,
            )
        log_event(
            "provider_limits_updated", fields={"actor_type": actor_type, "actor_id": actor_id}
        )
        return self.status()

    def reset(self, actor_type: str, actor_id: str, request_id: str) -> dict[str, Any]:
        """Меняем только суточный отсчёт; минутное окно/история остаются."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            busy = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM provider_calls WHERE state='started') "
                "OR EXISTS(SELECT 1 FROM qa_runs WHERE state='running') "
                "OR EXISTS(SELECT 1 FROM documents WHERE indexing_status='indexing')"
            ).fetchone()[0]
            if busy:
                raise AppError(
                    409,
                    "operation_busy",
                    "Дождитесь завершения текущих операций перед сбросом",
                    "business_rule",
                )
            now = datetime.now(UTC)
            previous = self._counts(connection, now)
            connection.execute(
                "UPDATE provider_settings SET reset_at=?, reset_actor_type=?, reset_actor_id=? "
                "WHERE id=1",
                (now.isoformat(), actor_type, actor_id),
            )
            self.database.audit(
                "provider_budget_reset",
                request_id,
                actor_type=actor_type,
                actor_id=actor_id,
                details={"previous_usage": previous},
                connection=connection,
            )
        log_event("provider_budget_reset", fields={"actor_type": actor_type, "actor_id": actor_id})
        return self.status()
