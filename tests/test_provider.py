"""Резервирование платных вызовов и валидация внешнего протокола."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

import cleaning_kb.provider as provider_module
from cleaning_kb.database import Database
from cleaning_kb.errors import AppError
from cleaning_kb.provider import ProxyProvider
from cleaning_kb.vector_store import QdrantStore


def configured(settings):
    settings.proxyapi_key = SecretStr("synthetic-provider-key")
    settings.embedding_dimension = 2
    database = Database(settings.database_path)
    database.initialize()
    return database


def test_budget_reservation_is_atomic_and_survives_restart(settings) -> None:
    database = configured(settings)
    settings.provider_calls_per_day = 1
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                "usage": {"total_tokens": 5, "secret": "hidden"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        provider = ProxyProvider(settings, database, client)

        def embed(_):
            try:
                provider.embed(["Вопрос"], "operation")
                return "ok"
            except AppError as error:
                return error.code

        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(embed, range(2)))
        assert sorted(results) == ["ok", "provider_budget_exceeded"]
        assert len(calls) == 1
        restarted = ProxyProvider(settings, Database(settings.database_path), client)
        with pytest.raises(AppError, match="Достигнут лимит"):
            restarted.embed(["Вопрос"], "operation2")
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM provider_calls").fetchone()
        assert row["state"] == "completed"
        assert json.loads(row["usage_json"]) == {"total_tokens": 5}


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("timeout", "provider_timeout"),
        ("429", "provider_rate_limited"),
        ("401", "provider_auth_failed"),
        ("402", "provider_balance_exhausted"),
        ("broken_json", "provider_invalid_response"),
    ],
)
def test_failed_provider_call_is_not_retried_or_refunded(settings, failure, expected) -> None:
    database = configured(settings)
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("secret must not leak", request=request)
        if failure == "broken_json":
            return httpx.Response(200, content=b"secret malformed body")
        return httpx.Response(int(failure), json={"error": "secret must not leak"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AppError) as caught:
            ProxyProvider(settings, database, client).embed(["Вопрос"], "op")
        assert caught.value.code == expected and "secret" not in str(caught.value)
    assert count == 1
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM provider_calls").fetchone()
        assert row["state"] == "failed" and row["error"] == expected
        assert row["reserved_tokens"] > 0


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 1, "embedding": [1.0, 0.0]}],
        [{"index": 0, "embedding": [1.0]}],
        [{"index": 0, "embedding": [0.0, 0.0]}],
        [{"index": 0, "embedding": ["nan", 1.0]}],
    ],
)
def test_invalid_embeddings_rejected(settings, data) -> None:
    database = configured(settings)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data}))
    ) as client:
        with pytest.raises(AppError) as caught:
            ProxyProvider(settings, database, client).embed(["Вопрос"], "op")
        assert caught.value.code == "invalid_embedding"


def test_missing_provider_key_sends_nothing(settings) -> None:
    database = Database(settings.database_path)
    database.initialize()
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Вызов запрещён"))
    ) as client:
        with pytest.raises(AppError) as caught:
            ProxyProvider(settings, database, client).embed(["Вопрос"], "op")
        assert caught.value.code == "provider_not_configured"


@pytest.mark.parametrize(
    "choice",
    [
        {"finish_reason": "length", "message": {"content": "{unfinished"}},
        {"finish_reason": "stop", "message": {"content": None, "refusal": "Отказ"}},
        {"finish_reason": "content_filter", "message": {"content": "Неполный ответ"}},
        {"finish_reason": "stop", "message": {"content": []}},
    ],
)
def test_incomplete_or_refused_generation_never_becomes_an_answer(settings, choice) -> None:
    database = configured(settings)
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"choices": [choice], "usage": {"total_tokens": 10}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AppError) as caught:
            ProxyProvider(settings, database, client).generate("Вопрос", [], "operation")
        assert caught.value.code == "provider_incomplete_answer"
    assert count == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0] == 1


def test_minute_limit_spans_utc_midnight(settings, monkeypatch) -> None:
    database = configured(settings)
    settings.provider_calls_per_minute = 1
    now = datetime(2026, 11, 23, 0, 0, 1, tzinfo=UTC)
    monkeypatch.setattr(provider_module, "datetime", SimpleNamespace(now=lambda _: now))
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO provider_calls VALUES "
            "('previous', '2026-11-22T23:59:59+00:00', 'op', 'embedding', 'model', 10, "
            "'completed', '{}', NULL)"
        )
    with httpx.Client() as client:
        with pytest.raises(AppError) as caught:
            ProxyProvider(settings, database, client)._reserve("embedding", "model", 10, "new")
        assert caught.value.code == "provider_budget_exceeded"


def test_qdrant_filters_and_wait_and_no_text_payload(settings) -> None:
    configured(settings)
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": {"config": {"params": {"vectors": {"size": 2, "distance": "Cosine"}}}}
                },
            )
        if request.method == "PUT":
            return httpx.Response(200, json={"result": {"status": "completed"}})
        body = json.loads(request.content)
        assert body["filter"]["must"] == [{"key": "document_id", "match": {"any": ["active"]}}]
        return httpx.Response(200, json={"result": {"points": [{"id": "snippet", "score": 0.9}]}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        store = QdrantStore(settings, client)
        store.write([("snippet", "active", [1.0, 0.0])])
        assert requests[1].url.params["wait"] == "true"
        assert json.loads(requests[1].content)["points"][0]["payload"] == {"document_id": "active"}
        assert store.search([1.0, 0.0], ["active"]) == [("snippet", 0.9)]
        before = len(requests)
        assert store.search([1.0, 0.0], []) == []
        assert len(requests) == before
