"""Подсчёт полного контекста o200k_base без скрытого усечения."""

import json
import os
from pathlib import Path
from typing import Any

import tiktoken

from cleaning_kb.config import Settings
from cleaning_kb.database import Database
from cleaning_kb.errors import AppError


def prepare_tokenizer() -> None:
    """В Docker словарь прогрет при сборке; локальный кеш остаётся в проекте."""
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path(".tmp/tiktoken").resolve()))
    tiktoken.get_encoding("o200k_base")


class DialogBudget:
    """Лимит истории с источниками; отдельные резервы генерации и протокола."""

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings, self.database = settings, database

    def count(self, value: Any) -> int:
        """Специальные строки пользователя считаются обычным текстом."""
        prepare_tokenizer()
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=()))

    def source_reserve(self) -> int:
        """До платного поиска оценить максимальные top_k снимков текущих фрагментов."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT s.id, s.document_id, s.snippet_text, d.title FROM snippets s "
                "JOIN documents d ON d.id=s.document_id "
                "WHERE d.is_active=1 AND d.indexing_status='ready'"
            ).fetchall()
        costs = [
            self.count(
                {
                    "document_id": row["document_id"],
                    "title": row["title"],
                    "snippet_id": row["id"],
                    "quote": row["snippet_text"],
                }
            )
            + 64
            for row in rows
        ]
        return sum(sorted(costs, reverse=True)[: self.settings.retrieval_top_k])

    def status(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        used = self.count(history)
        reserve = self.source_reserve() + self.settings.max_answer_tokens + 768
        return {
            "used_tokens": used,
            "max_tokens": self.settings.dialog_max_tokens,
            "next_answer_reserve": reserve,
            "warning": used + reserve >= self.settings.dialog_max_tokens * 0.8,
            "can_ask": used + reserve < self.settings.dialog_max_tokens,
            "tokenizer": "o200k_base",
        }

    def ensure_question(self, history: list[dict[str, Any]], question: str) -> None:
        state = self.status(history)
        if (
            not history
            and state["next_answer_reserve"] + self.count(question)
            > self.settings.dialog_max_tokens
        ):
            raise AppError(
                409,
                "dialog_budget_too_small",
                "Лимит не оставляет резерва для ответа; увеличьте DIALOG_MAX_TOKENS",
                "configuration",
            )
        if (
            state["used_tokens"] + state["next_answer_reserve"] + self.count(question)
            > self.settings.dialog_max_tokens
        ):
            raise AppError(
                409,
                "dialog_limit_reached",
                "Достигнут предел диалога; начните новый. Вопрос не отправлен модели",
                "business_rule",
            )

    def ensure_summary(self, payload: dict[str, Any]) -> None:
        if self.count(payload) + self.settings.summary_max_tokens + 2048 > 64_000:
            raise AppError(
                409,
                "summary_context_limit",
                "Контекст превышает технический предел итога",
                "business_rule",
            )
