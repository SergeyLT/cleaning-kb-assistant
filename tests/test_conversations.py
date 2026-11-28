"""Память, бюджеты, отметки, ревизии и удаление без внешних платных вызовов."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import API_KEY
from fastapi.testclient import TestClient
from test_rag import FakeProvider, FakeStore

from cleaning_kb.app import create_app
from cleaning_kb.database import Database
from cleaning_kb.errors import AppError

HEADERS = {"X-API-Key": API_KEY}


def test_document_rename_updates_source_views_without_reindexing_or_rewriting(memory, settings):
    client, provider = memory
    document = client.get("/kb/documents", headers=HEADERS).json()[0]
    cid = create(client)
    answer = ask(client, cid, "Входит ли духовка?")
    summary = client.post(f"/conversations/{cid}/summary", headers=HEADERS).json()
    database = Database(settings.database_path)
    with database.connect() as connection:
        snapshots = list(
            connection.execute("SELECT id, result_json FROM qa_runs ORDER BY position")
        )
        vectors = list(connection.execute("SELECT id, embedding_json FROM snippets"))
    embeddings = provider.embeddings
    renamed = client.patch(
        "/kb/documents/" + document["id"], headers=HEADERS, json={"title": "Исправленное название"}
    )
    assert renamed.status_code == 200
    assert provider.embeddings == embeddings
    old_answer = client.get("/qa/" + answer["operation_id"], headers=HEADERS).json()
    assert old_answer["sources"][0]["title"] == "Правила"
    assert old_answer["sources"][0]["current_title"] == "Исправленное название"
    view = get(client, cid)
    old_summary = next(r for r in view["runs"] if r["id"] == summary["operation_id"])
    assert "[Исправленное название]" in old_summary["display_text"]
    assert "[Правила]" in old_summary["answer"]
    assert old_summary["is_stale"] is False
    with database.connect() as connection:
        assert [
            tuple(r)
            for r in connection.execute("SELECT id, result_json FROM qa_runs ORDER BY position")
        ] == [tuple(r) for r in snapshots]
        assert [
            tuple(r) for r in connection.execute("SELECT id, embedding_json FROM snippets")
        ] == [tuple(r) for r in vectors]
    duplicate = client.post(
        "/kb/documents",
        headers=HEADERS,
        json={
            "title": "Другое имя",
            "text": client.get("/kb/documents/" + document["id"], headers=HEADERS).json()["text"],
        },
    ).json()
    conflict = client.patch(
        "/kb/documents/" + duplicate["id"], headers=HEADERS, json={"title": "Исправленное название"}
    )
    assert conflict.status_code == 409
    assert (
        client.patch(
            "/kb/documents/" + document["id"], headers=HEADERS, json={"title": "   "}
        ).status_code
        == 422
    )
    client.post("/kb/documents/" + document["id"] + "/deactivate", headers=HEADERS)
    assert (
        client.get("/qa/" + answer["operation_id"], headers=HEADERS).json()["sources"][0][
            "current_title"
        ]
        == "Исправленное название"
    )


class MemoryProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.rewrite_histories = []
        self.answer_histories = []
        self.summary_payloads = []
        self.ambiguous = False
        self.summary_callback = None
        self.summary_reply = None
        self.rewrite_error = None

    def rewrite(self, question, history, operation_id):
        self.rewrite_histories.append(history)
        if self.rewrite_error:
            raise self.rewrite_error
        return json.dumps(
            {
                "search_question": "Входит ли духовка в генеральную уборку?",
                "ambiguous": self.ambiguous,
            }
        )

    def generate(self, question, evidence, operation_id, *, repair=False, history=None):
        self.answer_histories.append(history or [])
        return super().generate(question, evidence, operation_id, repair=repair)

    def summarize(self, payload, operation_id, *, repair=False):
        self.summary_payloads.append(payload)
        if self.summary_callback:
            self.summary_callback()
        return self.summary_reply or json.dumps(
            {
                "client_facts": [
                    {"text": "Обращение пользователя", "turn_ids": [payload["turns"][0]["turn_id"]]}
                ],
                "findings": [
                    {
                        "text": "Условия из документов",
                        "evidence_ids": [payload["evidence"][0]["evidence_id"]],
                    }
                ]
                if payload["evidence"]
                else [],
                "limitations": ["Результаты внешних уточнений неизвестны"],
            }
        )


@pytest.fixture
def memory(settings):
    settings.embedding_dimension = 2
    provider, store = MemoryProvider(), FakeStore()
    with TestClient(
        create_app(settings, qdrant_probe=lambda: True, provider=provider, vector_store=store),
        base_url=settings.origin,
    ) as client:
        client.post(
            "/kb/documents",
            headers=HEADERS,
            json={
                "title": "Правила",
                "text": "Духовка — отдельная опция генеральной уборки. Прайса нет.",
            },
        )
        yield client, provider


def create(client, title=None):
    response = client.post(
        "/conversations", headers=HEADERS, json={} if title is None else {"title": title}
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def ask(client, cid, question="Что входит в генеральную уборку?"):
    response = client.post(
        "/kb/ask", headers=HEADERS, json={"question": question, "conversation_id": cid}
    )
    assert response.status_code == 200, response.text
    return response.json()


def get(client, cid):
    return client.get("/conversations/" + cid, headers=HEADERS).json()


def test_followup_uses_full_isolated_history_and_persists_search(memory, settings):
    client, provider = memory
    a, b = create(client), create(client)
    first = ask(client, a)
    ask(client, a, "А духовка туда входит?")
    assert len(provider.rewrite_histories) == 1
    assert provider.rewrite_histories[0][0]["turn_id"] == first["operation_id"]
    assert provider.answer_histories[-1] == provider.rewrite_histories[0]
    ask(client, b, "Поддерживающая уборка")
    assert provider.answer_histories[-1] == []
    assert len(provider.rewrite_histories) == 1
    a_view = get(client, a)
    assert a_view["title"] == "Что входит в генеральную уборку?"
    assert a_view["runs"][1]["question"] == "А духовка туда входит?"
    assert a_view["runs"][1]["search_question"] == "Входит ли духовка в генеральную уборку?"
    # Отдельный процесс приложения читает тот же файл только после остановки первого.
    database = Database(settings.database_path)
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM qa_runs WHERE conversation_id=?", (a,)
            ).fetchone()[0]
            == 2
        )


def test_rename_and_confirmed_delete_clear_texts_but_keep_budget(memory, settings):
    client, _ = memory
    cid = create(client)
    private = "Уникальное обращение СИНТЕТИЧЕСКИЙ_КЛИЕНТ_123"
    answer = ask(client, cid, private)
    assert (
        client.patch("/conversations/" + cid, headers=HEADERS, json={"title": "Обращение"}).json()[
            "title"
        ]
        == "Обращение"
    )
    assert client.delete("/conversations/" + cid, headers=HEADERS).status_code == 409
    database = Database(settings.database_path)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO provider_calls VALUES "
            "(?, 'now', ?, 'generation', 'model', 100, 'completed', '{}', NULL)",
            ("budget", answer["operation_id"]),
        )
    assert (
        client.delete("/conversations/" + cid + "?confirm=true", headers=HEADERS).status_code == 200
    )
    assert client.get("/conversations/" + cid, headers=HEADERS).status_code == 404
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM qa_runs WHERE conversation_id=?", (cid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_runs WHERE conversation_id=?", (cid,)
            ).fetchone()[0]
            == 0
        )
        assert private not in "\n".join(connection.iterdump())
        assert (
            connection.execute(
                "SELECT reserved_tokens FROM provider_calls WHERE id='budget'"
            ).fetchone()[0]
            == 100
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_runs WHERE action='conversation_deleted'"
            ).fetchone()[0]
            == 1
        )


def test_review_is_separate_and_invalidates_summary(memory):
    client, provider = memory
    cid = create(client)
    answer = ask(client, cid, "Сколько точно будет стоить уборка?")
    assert answer["needs_review"]
    summary = client.post(f"/conversations/{cid}/summary", headers=HEADERS).json()
    assert summary["reviews"][0]["reviewed_at"] is None
    queue = client.get("/reviews", headers=HEADERS).json()
    assert queue[0]["id"] == answer["operation_id"]
    reviewed = client.post(f"/qa/{answer['operation_id']}/review", headers=HEADERS).json()
    assert reviewed["needs_review"] == 1 and reviewed["reviewed_at"]
    assert client.get("/reviews", headers=HEADERS).json() == []
    old = get(client, cid)
    assert old["runs"][0]["result"]["needs_review"] is True
    assert old["runs"][1]["is_stale"] is True
    revision = old["revision"]
    client.post(f"/qa/{answer['operation_id']}/review", headers=HEADERS)
    assert get(client, cid)["revision"] == revision
    fresh = client.post(f"/conversations/{cid}/summary", headers=HEADERS).json()
    assert fresh["reviews"][0]["reviewed_at"] == reviewed["reviewed_at"]
    assert "Результат внешней проверки не записан" in fresh["verification_notice"]
    summary_run = next(
        run for run in get(client, cid)["runs"] if run["id"] == fresh["operation_id"]
    )
    assert summary_run["usage"]["prompt_version"] == "summary-v2"
    assert "reviewed_at" not in json.dumps(provider.summary_payloads[-1])
    assert len(provider.summary_payloads[-1]["turns"]) == 1
    ask(client, cid, "А духовка?")
    assert len(provider.rewrite_histories[-1]) == 1  # Два итога не попали в память.
    assert all(r["is_stale"] for r in get(client, cid)["runs"] if r["kind"] == "summary")


def test_limit_prevents_all_paid_actions_but_allows_summary(memory, settings):
    client, provider = memory
    cid = create(client)
    ask(client, cid)
    used = get(client, cid)["budget"]["used_tokens"]
    settings.dialog_max_tokens = used + 10
    counts = (provider.embeddings, provider.generations, len(provider.rewrite_histories))
    response = client.post(
        "/kb/ask",
        headers=HEADERS,
        json={"conversation_id": cid, "question": "Не терять набранный вопрос"},
    )
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "dialog_limit_reached"
    )
    assert counts == (provider.embeddings, provider.generations, len(provider.rewrite_histories))
    assert len(get(client, cid)["runs"]) == 1
    assert client.post(f"/conversations/{cid}/summary", headers=HEADERS).status_code == 200
    assert get(client, cid)["budget"]["can_ask"] is False


def test_ambiguous_followup_stops_before_retrieval(memory):
    client, provider = memory
    cid = create(client)
    ask(client, cid)
    provider.ambiguous = True
    before = provider.embeddings
    result = ask(client, cid, "А это?")
    assert result["review_reasons"] == ["ambiguous_context"] and result["sources"] == []
    assert provider.embeddings == before


def test_concurrent_operation_and_mutations_rejected_without_more_paid_calls(memory):
    client, provider = memory
    cid = create(client)
    entered, release = threading.Event(), threading.Event()
    provider.on_generate = lambda: (entered.set(), release.wait(5))
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(ask, client, cid)
        assert entered.wait(5)
        before = provider.embeddings
        try:
            assert (
                client.post(
                    "/kb/ask", headers=HEADERS, json={"conversation_id": cid, "question": "Повтор"}
                ).status_code
                == 409
            )
            assert client.post(f"/conversations/{cid}/summary", headers=HEADERS).status_code == 409
            assert (
                client.delete(f"/conversations/{cid}?confirm=true", headers=HEADERS).status_code
                == 409
            )
            assert (
                client.patch(
                    f"/conversations/{cid}", headers=HEADERS, json={"title": "Новое"}
                ).status_code
                == 409
            )
            assert provider.embeddings == before
        finally:
            release.set()
        assert future.result()["operation_id"]


def test_summary_rechecks_revision_and_fails_safely(memory, settings):
    client, provider = memory
    cid = create(client)
    ask(client, cid)

    def change_revision():
        with Database(settings.database_path).connect() as connection:
            connection.execute("UPDATE conversations SET revision=revision+1 WHERE id=?", (cid,))

    provider.summary_callback = change_revision
    response = client.post(f"/conversations/{cid}/summary", headers=HEADERS)
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "summary_revision_changed"
    )
    assert get(client, cid)["runs"][-1]["state"] == "failed"


def test_bad_summary_has_one_repair_and_never_stores_fabricated_sources(memory):
    client, provider = memory
    cid = create(client)
    ask(client, cid)
    provider.summary_reply = json.dumps(
        {
            "client_facts": [],
            "findings": [{"text": "Выдумка", "evidence_ids": ["fake"]}],
            "limitations": [],
        }
    )
    response = client.post(f"/conversations/{cid}/summary", headers=HEADERS)
    assert response.status_code == 503 and len(provider.summary_payloads) == 2
    assert get(client, cid)["runs"][-1]["result"] is None


def test_rewrite_failure_is_saved_without_retry(memory):
    client, provider = memory
    cid = create(client)
    ask(client, cid)
    provider.rewrite_error = AppError(503, "provider_timeout", "Таймаут", "timeout")
    response = client.post(
        "/kb/ask", headers=HEADERS, json={"conversation_id": cid, "question": "Уточнение"}
    )
    assert response.status_code == 503 and len(provider.rewrite_histories) == 1
    row = get(client, cid)["runs"][-1]
    assert (
        row["state"] == "failed" and row["needs_review"] == 1 and row["error"] == "provider_timeout"
    )


def test_history_export_access_and_validation(memory):
    client, _ = memory
    cid = create(client, "Моё обращение")
    assert client.post(f"/conversations/{cid}/summary", headers=HEADERS).status_code == 409
    answer = ask(client, cid)
    exported = client.get(f"/conversations/{cid}/export", headers=HEADERS)
    assert "attachment" in exported.headers["content-disposition"]
    assert exported.json()["conversation"]["runs"][0]["id"] == answer["operation_id"]
    assert client.get("/history?limit=1", headers=HEADERS).json()[0]["id"] == answer["operation_id"]
    assert client.post(f"/qa/{answer['operation_id']}/review", headers=HEADERS).status_code == 409
    for url in ("/conversations", "/history", "/reviews", f"/conversations/{cid}/export"):
        assert client.get(url).status_code == 401
    assert client.post("/conversations", headers=HEADERS, json={"title": "   "}).status_code == 422
    assert (
        client.post(
            "/conversations", headers=HEADERS, json={"title": "a", "comment": "extra"}
        ).status_code
        == 422
    )
