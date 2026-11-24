"""Явные Haystack-пайплайны; оплачиваемые компоненты зависят от адаптера."""

import json
import os
from typing import Any

# Отключить телеметрию до импорта Haystack: документы и трассировки не отправляются.
os.environ["HAYSTACK_TELEMETRY_ENABLED"] = "False"
os.environ["HAYSTACK_CONTENT_TRACING_ENABLED"] = "False"

from haystack import Pipeline, component  # noqa: E402

from cleaning_kb.config import Settings  # noqa: E402
from cleaning_kb.database import Database  # noqa: E402
from cleaning_kb.provider import Provider  # noqa: E402
from cleaning_kb.vector_store import VectorStore  # noqa: E402


@component
class EmbedSnippets:
    """Использовать кеш SQLite при повторной записи в Qdrant."""

    def __init__(self, settings: Settings, database: Database, provider: Provider) -> None:
        self.settings, self.database, self.provider = settings, database, provider

    @component.output_types(points=list[tuple[str, str, list[float]]])
    def run(self, document_id: str, operation_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM snippets WHERE document_id=? ORDER BY position", (document_id,)
            ).fetchall()
        missing = [
            row
            for row in rows
            if row["embedding_json"] is None
            or row["embedding_model"] != self.settings.embedding_model
            or row["embedding_dimension"] != self.settings.embedding_dimension
        ]
        if missing:
            vectors = self.provider.embed([row["snippet_text"] for row in missing], operation_id)
            if len(vectors) != len(missing):
                raise ValueError("Количество векторов не соответствует фрагментам")
            with self.database.connect() as connection:
                for row, vector in zip(missing, vectors, strict=True):
                    connection.execute(
                        "UPDATE snippets SET embedding_json=?, embedding_model=?, "
                        "embedding_dimension=? WHERE id=?",
                        (
                            json.dumps(vector),
                            self.settings.embedding_model,
                            self.settings.embedding_dimension,
                            row["id"],
                        ),
                    )
        with self.database.connect() as connection:
            points = [
                (row["id"], document_id, json.loads(row["embedding_json"]))
                for row in connection.execute(
                    "SELECT id, embedding_json FROM snippets WHERE document_id=? ORDER BY position",
                    (document_id,),
                )
            ]
        return {"points": points}


@component
class WriteVectors:
    """Идемпотентная запись индекса."""

    def __init__(self, store: VectorStore) -> None:
        self.store = store

    @component.output_types(count=int)
    def run(self, points: list[tuple[str, str, list[float]]]) -> dict[str, int]:
        self.store.write(points)
        return {"count": len(points)}


@component
class EmbedQuestion:
    """Вектор вопроса считается той же моделью, что документы."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    @component.output_types(vector=list[float])
    def run(self, question: str, operation_id: str) -> dict[str, list[float]]:
        return {"vector": self.provider.embed([question], operation_id)[0]}


@component
class RetrieveVectors:
    """Список разрешённых документов приходит только от сервиса SQLite."""

    def __init__(self, store: VectorStore) -> None:
        self.store = store

    @component.output_types(hits=list[tuple[str, float]])
    def run(self, vector: list[float], active_ids: list[str]) -> dict[str, Any]:
        return {"hits": self.store.search(vector, active_ids)}


def ingestion_pipeline(
    settings: Settings, database: Database, provider: Provider, store: VectorStore
) -> Pipeline:
    """Собрать индексирование с кешированием эмбеддингов."""
    pipeline = Pipeline()
    pipeline.add_component("embed", EmbedSnippets(settings, database, provider))
    pipeline.add_component("write", WriteVectors(store))
    pipeline.connect("embed.points", "write.points")
    return pipeline


def retrieval_pipeline(provider: Provider, store: VectorStore) -> Pipeline:
    """Собрать поиск; генерация и контроль выполняются сервисом явно."""
    pipeline = Pipeline()
    pipeline.add_component("embed", EmbedQuestion(provider))
    pipeline.add_component("retrieve", RetrieveVectors(store))
    pipeline.connect("embed.vector", "retrieve.vector")
    return pipeline
