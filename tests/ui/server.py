"""Изолированный сервер E2E. В production-пакет этот модуль не входит."""

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn

from cleaning_kb.app import create_app
from cleaning_kb.config import Settings
from cleaning_kb.credentials import hash_password
from cleaning_kb.rag_models import Evidence


class TestProvider:
    """Детерминированная граница модели; авторизация и сервисы настоящие."""

    def embed(self, texts: list[str], operation_id: str) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    def rewrite(self, question: str, history: list[dict[str, Any]], operation_id: str) -> str:
        return json.dumps(
            {"search_question": "Генеральная уборка: " + question, "ambiguous": False}
        )

    def generate(
        self,
        question: str,
        evidence: list[Evidence],
        operation_id: str,
        *,
        repair: bool = False,
        history: list[dict[str, Any]] | None = None,
    ) -> str:
        price = "стоить" in question or "цена" in question.lower()
        return json.dumps(
            {
                "answer": "Точную стоимость нужно уточнить у менеджера."
                if price
                else (
                    "Духовка — отдельная опция генеральной уборки. "
                    "Перед чисткой её выключают и остужают."
                ),
                "evidence_ids": [evidence[0].evidence_id],
                "confidence": 0.95,
                "coverage": "none" if price else "full",
            },
            ensure_ascii=False,
        )

    def summarize(self, payload: dict[str, Any], operation_id: str, *, repair: bool = False) -> str:
        return json.dumps(
            {
                "client_facts": [],
                "findings": [
                    {
                        "text": "Духовка является отдельной опцией.",
                        "evidence_ids": [payload["evidence"][0]["evidence_id"]],
                    }
                ]
                if payload["evidence"]
                else [],
                "limitations": ["Стоимость и результат внешнего уточнения не записаны."],
            },
            ensure_ascii=False,
        )


class TestStore:
    """Векторная граница подменена, тексты и доступность проверяет обычный сервис."""

    def __init__(self) -> None:
        self.points: dict[str, tuple[str, list[float]]] = {}

    def write(self, points: list[tuple[str, str, list[float]]]) -> None:
        for identifier, document_id, vector in points:
            self.points[identifier] = (document_id, vector)

    def search(self, vector: list[float], active_ids: list[str]) -> list[tuple[str, float]]:
        return [
            (identifier, 0.95)
            for identifier, (document_id, _) in reversed(list(self.points.items()))
            if document_id in active_ids
        ][:8]


if __name__ == "__main__":
    port = int(os.environ.get("UI_TEST_PORT", "8877"))
    directory = Path(".tmp") / ("ui-server-" + uuid4().hex)
    settings = Settings(
        _env_file=None,
        app_environment="test",
        app_version="ui-e2e",
        app_base_url=f"http://127.0.0.1:{port}",
        database_path=directory / "app.sqlite3",
        admin_password_hash=hash_password("ui-test-password-only"),
        api_key="ui-e2e-only-key-0123456789-abcdef",
        embedding_dimension=2,
        login_attempts_per_minute=100,
    )
    app = create_app(
        settings, qdrant_probe=lambda: True, provider=TestProvider(), vector_store=TestStore()
    )
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)
