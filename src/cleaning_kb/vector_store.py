"""REST-адаптер локального Qdrant; в payload нет копий документов."""

import hashlib
from typing import Any, Protocol

import httpx

from cleaning_kb.config import Settings
from cleaning_kb.errors import AppError


class VectorStore(Protocol):
    """Векторный индекс возвращает только идентификаторы и cosine."""

    def write(self, points: list[tuple[str, str, list[float]]]) -> None: ...

    def search(self, vector: list[float], active_ids: list[str]) -> list[tuple[str, float]]: ...


class QdrantStore:
    """Отдельная коллекция для каждой модели/размерности; SQLite владеет текстом."""

    def __init__(self, settings: Settings, client: httpx.Client) -> None:
        self.settings, self.client = settings, client
        fingerprint = hashlib.sha256(
            f"{settings.embedding_model}:{settings.embedding_dimension}".encode()
        ).hexdigest()[:16]
        self.collection = "cleaning_kb_" + fingerprint
        self.url = str(settings.qdrant_url).rstrip("/") + "/collections/" + self.collection

    def _request(self, method: str, path: str = "", **kwargs: Any) -> httpx.Response:
        try:
            return self.client.request(method, self.url + path, **kwargs)
        except httpx.HTTPError:
            raise AppError(
                503, "vector_unavailable", "Векторный индекс недоступен", "external_service"
            ) from None

    @staticmethod
    def _result(response: httpx.Response) -> Any:
        try:
            response.raise_for_status()
            return response.json()["result"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise AppError(
                503,
                "vector_invalid_response",
                "Не удалось обработать ответ индекса",
                "external_service",
            ) from None

    def write(self, points: list[tuple[str, str, list[float]]]) -> None:
        """Upsert детерминированных UUID с ожиданием фактической записи."""
        response = self._request("GET")
        if response.status_code == 404:
            response = self._request(
                "PUT",
                json={"vectors": {"size": self.settings.embedding_dimension, "distance": "Cosine"}},
            )
            # Параллельное создание тем же приложением допускает already exists.
            if response.status_code not in (200, 409):
                self._result(response)
            response = self._request("GET")
        config = self._result(response)
        try:
            vectors = config["config"]["params"]["vectors"]
            if (
                vectors["size"] != self.settings.embedding_dimension
                or vectors["distance"] != "Cosine"
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AppError(
                503,
                "vector_schema_mismatch",
                "Схема коллекции индекса несовместима",
                "configuration",
            ) from None
        self._result(
            self._request(
                "PUT",
                "/points?wait=true",
                json={
                    "points": [
                        {
                            "id": identifier,
                            "vector": vector,
                            "payload": {"document_id": document_id},
                        }
                        for identifier, document_id, vector in points
                    ]
                },
            )
        )

    def search(self, vector: list[float], active_ids: list[str]) -> list[tuple[str, float]]:
        """Неполный или отсутствующий индекс не маскируется под пустую базу."""
        if not active_ids:
            return []
        result = self._result(
            self._request(
                "POST",
                "/points/query",
                json={
                    "query": vector,
                    "filter": {"must": [{"key": "document_id", "match": {"any": active_ids}}]},
                    "limit": self.settings.retrieval_top_k,
                    "score_threshold": self.settings.retrieval_min_score,
                    "with_payload": False,
                    "with_vector": False,
                },
            )
        )
        try:
            return [(str(row["id"]), float(row["score"])) for row in result["points"]]
        except (KeyError, ValueError, TypeError):
            raise AppError(
                503, "vector_invalid_response", "Некорректный результат поиска", "serialization"
            ) from None
