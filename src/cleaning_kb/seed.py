"""Загрузка демонстрационного JSONL через защищённый API работающего приложения."""

import json
from pathlib import Path

import httpx

from cleaning_kb.config import Settings
from cleaning_kb.rag_models import DocumentInput


def load_documents(path: Path, settings: Settings) -> int:
    """Проверить весь файл до первой записи; повтор не дублирует документы."""
    if path.stat().st_size > 1_000_000:
        raise ValueError("Слишком большой файл")
    documents = [
        DocumentInput.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not 1 <= len(documents) <= 100:
        raise ValueError("Допустимо от 1 до 100 документов")
    if any(len(doc.text) > settings.max_document_chars for doc in documents):
        raise ValueError("Превышен предел документа")
    with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as client:
        for position, doc in enumerate(documents, start=1):
            response = client.post(
                settings.origin + "/kb/documents",
                json=doc.model_dump(),
                headers={"X-API-Key": settings.api_key.get_secret_value()},
            )
            if response.status_code != 200:
                raise ValueError("Сервер отклонил загрузку")
            result = response.json()
            if result.get("indexing_status") != "ready":
                raise ValueError(
                    "Документ сохранён, но не проиндексирован; проверьте список документов"
                )
            print(f"Проиндексирован документ {position} из {len(documents)}")
    return len(documents)
