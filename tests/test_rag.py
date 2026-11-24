"""Поведение RAG без оплаты: настоящий сервис и Haystack с подменой внешних границ."""

import json
from collections.abc import Iterator

import pytest
from conftest import API_KEY
from fastapi.testclient import TestClient

from cleaning_kb.app import create_app
from cleaning_kb.config import Settings
from cleaning_kb.database import Database
from cleaning_kb.errors import AppError
from cleaning_kb.kb_service import KnowledgeService, needs_operational_confirmation, split_text
from cleaning_kb.rag_models import DocumentInput, Evidence

HEADERS = {"X-API-Key": API_KEY}
TEXT = "Специализированное удаление плесени не выполняется. Духовка — отдельная опция."


class FakeProvider:
    """Счётчики выявляют лишние платные действия и скрытые повторы."""

    def __init__(self) -> None:
        self.embeddings = 0
        self.generations = 0
        self.replies: list[str] = []
        self.fail: AppError | None = None
        self.on_generate = None

    def embed(self, texts: list[str], operation_id: str) -> list[list[float]]:
        self.embeddings += 1
        if self.fail:
            raise self.fail
        return [[1.0, 0.0] for _ in texts]

    def generate(
        self, question: str, evidence: list[Evidence], operation_id: str, *, repair: bool = False
    ) -> str:
        self.generations += 1
        if self.on_generate:
            self.on_generate()
        if self.fail:
            raise self.fail
        return (
            self.replies.pop(0)
            if self.replies
            else json.dumps(
                {
                    "answer": "Нет, специализированное удаление плесени не выполняется.",
                    "evidence_ids": [evidence[0].evidence_id],
                    "confidence": 0.95,
                    "coverage": "full",
                }
            )
        )


class FakeStore:
    """В памяти хранятся только векторы/ID, как в реальном адаптере."""

    def __init__(self) -> None:
        self.points = {}
        self.fail = False
        self.last_filter = []

    def write(self, points: list[tuple[str, str, list[float]]]) -> None:
        for identifier, doc, vector in points:
            self.points[identifier] = (doc, vector)
        if self.fail:
            raise AppError(503, "vector_unavailable", "Тестовый сбой", "external_service")

    def search(self, vector: list[float], active_ids: list[str]) -> list[tuple[str, float]]:
        self.last_filter = active_ids
        return [
            (identifier, 0.9) for identifier, (doc, _) in self.points.items() if doc in active_ids
        ]


@pytest.fixture
def rag(settings: Settings) -> Iterator[tuple[TestClient, FakeProvider, FakeStore]]:
    settings.embedding_dimension = 2
    provider, store = FakeProvider(), FakeStore()
    with TestClient(
        create_app(settings, qdrant_probe=lambda: True, provider=provider, vector_store=store),
        base_url=settings.origin,
    ) as client:
        yield client, provider, store


def add(client: TestClient) -> dict:
    response = client.post(
        "/kb/documents", headers=HEADERS, json={"title": "Регламент", "text": TEXT}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_document_retry_uses_cache_and_deterministic_ids(rag) -> None:
    client, provider, store = rag
    store.fail = True
    first = add(client)
    assert first["indexing_status"] == "failed"
    assert first["status"] == "ok" and first["document_id"] == first["id"]
    assert first["error"]
    identifiers = set(store.points)
    assert provider.embeddings == 1
    # Недоиндексированный документ отсутствует в поиске, платного вызова нет.
    empty = client.post("/kb/ask", headers=HEADERS, json={"question": "Плесень?"}).json()
    assert empty["needs_review"] and empty["review_reasons"] == ["no_evidence"]
    assert empty["confidence"] == "low"
    assert provider.embeddings == 1
    store.fail = False
    second = add(client)
    assert second["id"] == first["id"] and second["indexing_status"] == "ready"
    assert second["status"] == "ok" and second["document_id"] == first["document_id"]
    assert second["error"] is None
    assert set(store.points) == identifiers and provider.embeddings == 1
    assert len(client.get("/kb/documents", headers=HEADERS).json()) == 1
    assert add(client)["id"] == first["id"] and provider.embeddings == 1


def test_grounded_negative_and_deactivation_preserve_snapshot(rag, settings) -> None:
    client, provider, store = rag
    doc = add(client)
    answer = client.post("/kb/ask", headers=HEADERS, json={"question": "Удаляете плесень?"}).json()
    assert answer["needs_review"] is False
    assert answer["confidence"] == "high" and "confidence_score" not in answer
    assert answer["sources"][0]["document_id"] == doc["id"]
    assert answer["sources"][0]["quote"] in TEXT
    assert store.last_filter == [doc["id"]]
    client.post(f"/kb/documents/{doc['id']}/deactivate", headers=HEADERS)
    before = provider.embeddings
    assert client.post("/kb/ask", headers=HEADERS, json={"question": "Плесень?"}).json()[
        "needs_review"
    ]
    assert provider.embeddings == before
    with Database(settings.database_path).connect() as connection:
        row = connection.execute(
            "SELECT * FROM qa_runs WHERE id=?", (answer["operation_id"],)
        ).fetchone()
        assert json.loads(row["sources_json"])[0]["quote"] == TEXT
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_runs WHERE action='kb_answer'"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize(
    "coverage,confidence,reason",
    [
        ("partial", 0.95, "partial_coverage"),
        ("none", 0.95, "no_answer"),
        ("conflict", 0.95, "conflicting_sources"),
        ("full", 0.2, "low_confidence"),
    ],
)
def test_review_reasons(rag, coverage, confidence, reason) -> None:
    client, provider, _ = rag
    add(client)
    provider.replies = [
        json.dumps(
            {
                "answer": "Нужно уточнить детали",
                "evidence_ids": ["e1"],
                "coverage": coverage,
                "confidence": confidence,
            }
        )
    ]
    result = client.post("/kb/ask", headers=HEADERS, json={"question": "Как быть?"}).json()
    assert result["needs_review"] and reason in result["review_reasons"]


def test_invalid_citations_one_repair_then_safe_result(rag) -> None:
    client, provider, _ = rag
    add(client)
    provider.replies = [
        json.dumps(
            {
                "answer": "Выдумка",
                "evidence_ids": ["foreign"],
                "coverage": "full",
                "confidence": 1.0,
            }
        ),
        "not json",
    ]
    result = client.post("/kb/ask", headers=HEADERS, json={"question": "Вопрос"}).json()
    assert provider.generations == 2
    assert result["review_reasons"] == ["invalid_model_output"] and result["sources"] == []
    assert "Выдумка" not in result["answer"]


def test_context_endpoint_cannot_invent_database_document(rag) -> None:
    client, provider, _ = rag
    result = client.post(
        "/ai/answer_with_sources", headers=HEADERS, json={"question": "Плесень?", "context": TEXT}
    ).json()
    assert result["sources"][0]["document_id"] is None
    assert result["sources"][0]["quote"] == TEXT
    assert provider.embeddings == 0


def test_provider_error_saved_and_not_retried(rag, settings) -> None:
    client, provider, _ = rag
    add(client)
    provider.fail = AppError(503, "provider_timeout", "Таймаут", "timeout")
    response = client.post(
        "/ai/answer_with_sources", headers=HEADERS, json={"question": "Вопрос", "context": TEXT}
    )
    assert response.status_code == 503 and provider.generations == 1
    with Database(settings.database_path).connect() as connection:
        row = connection.execute("SELECT * FROM qa_runs").fetchone()
        assert row["state"] == "failed" and row["needs_review"] == 1
        assert row["error"] == "provider_timeout"


def test_deactivation_during_generation_discards_old_answer(rag) -> None:
    client, provider, _ = rag
    doc = add(client)
    provider.on_generate = lambda: client.post(
        f"/kb/documents/{doc['id']}/deactivate", headers=HEADERS
    )
    result = client.post("/kb/ask", headers=HEADERS, json={"question": "Плесень?"}).json()
    assert result["review_reasons"] == ["sources_changed"] and result["sources"] == []


def test_recover_interrupted_and_model_mismatch(settings) -> None:
    database = Database(settings.database_path)
    database.initialize()
    provider, store = FakeProvider(), FakeStore()
    settings.embedding_dimension = 2
    service = KnowledgeService(settings, database, provider, store)
    doc = service.add(DocumentInput(title="Регламент", text=TEXT), "req")
    settings.embedding_model = "other/model"
    assert service.ask("Вопрос", "req").review_reasons == ["no_evidence"]
    with database.connect() as connection:
        connection.execute(
            "UPDATE documents SET indexing_status='indexing' WHERE id=?", (doc["id"],)
        )
        connection.execute(
            "INSERT INTO qa_runs (id, created_at, question) VALUES ('interrupted', 'now', 'Вопрос')"
        )
    service.recover()
    assert service.document(doc["id"])["error"] == "operation_interrupted"
    with database.connect() as connection:
        assert (
            connection.execute("SELECT state FROM qa_runs WHERE id='interrupted'").fetchone()[0]
            == "interrupted"
        )


def test_rag_auth_validation_limits(rag, settings) -> None:
    client, provider, _ = rag
    for path in ("/kb/ask", "/kb/documents", "/ai/answer_with_sources"):
        assert client.post(path, json={}).status_code == 401
    assert client.post("/kb/ask", headers=HEADERS, json={"question": "   "}).status_code == 422
    assert (
        client.post("/kb/ask", headers=HEADERS, json={"question": "x", "extra": True}).status_code
        == 422
    )
    settings.max_document_chars = 1000
    assert (
        client.post(
            "/kb/documents", headers=HEADERS, json={"title": "a", "text": "a" * 1001}
        ).status_code
        == 422
    )
    assert provider.embeddings == 0


def test_chunks_are_exact_nonempty_and_bounded() -> None:
    text = ("Абзац " * 77 + "\n\n") * 8
    chunks = split_text(text)
    assert all(chunk in text and 0 < len(chunk) <= 1000 for chunk in chunks)
    assert "".join(chunks).replace(" ", "").replace("\n", "") == text.replace(" ", "").replace(
        "\n", ""
    )


@pytest.mark.parametrize(
    "question,requires_review",
    [
        ("Сколько точно будет стоить генеральная уборка моего помещения?", True),
        ("Назовите окончательную цену", True),
        ("Сколько я заплачу за уборку?", True),
        ("Какие сведения собрать для расчёта стоимости?", False),
        ("Что нужно уточнить перед расчётом уборки после ремонта?", False),
    ],
)
def test_price_policy_overrides_model_confidence(rag, question, requires_review) -> None:
    client, _, _ = rag
    add(client)
    result = client.post("/kb/ask", headers=HEADERS, json={"question": question}).json()
    assert result["needs_review"] is requires_review
    assert ("price_requires_confirmation" in result["review_reasons"]) is requires_review


def test_successful_format_repair_does_not_repeat_retrieval(rag) -> None:
    client, provider, _ = rag
    add(client)
    before = provider.embeddings
    provider.replies = [
        "not json",
        json.dumps(
            {
                "answer": "Специализированное удаление плесени не выполняется.",
                "evidence_ids": ["e1"],
                "coverage": "full",
                "confidence": 0.95,
            }
        ),
    ]
    response = client.post("/kb/ask", headers=HEADERS, json={"question": "Удаляете плесень?"})
    assert response.status_code == 200 and response.json()["needs_review"] is False
    assert provider.generations == 2 and provider.embeddings == before + 1


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Подтвердите, что завтра утром свободна бригада", True),
        ("Можно ли уже обещать клиенту приезд свободной бригады завтра утром?", True),
        ("Какой сотрудник назначен на мой заказ?", True),
        ("Что входит в уборку и какой конкретно сотрудник приедет завтра?", True),
        ("Кто подтверждает свободную бригаду и время выезда?", False),
        ("Можно ли обещать приезд бригады без согласования?", False),
        ("Какие сведения уточнить для переноса уборки?", False),
    ],
)
def test_operational_policy_distinguishes_confirmation_and_procedure(question, expected) -> None:
    assert needs_operational_confirmation(question) is expected


def test_operational_policy_and_retrieval_provenance_are_saved(rag, settings) -> None:
    client, _, _ = rag
    doc = add(client)
    result = client.post(
        "/kb/ask", headers=HEADERS, json={"question": "Можно ли обещать свободную бригаду завтра?"}
    ).json()
    assert (
        result["needs_review"] and "operational_confirmation_required" in result["review_reasons"]
    )
    with Database(settings.database_path).connect() as connection:
        usage = json.loads(
            connection.execute(
                "SELECT usage_json FROM qa_runs WHERE id=?", (result["operation_id"],)
            ).fetchone()[0]
        )
    assert usage["retrieval"][0]["document_id"] == doc["id"]
    assert usage["retrieval"][0]["score"] == 0.9


@pytest.mark.parametrize("path", ["/kb/ask", "/ai/answer_with_sources"])
@pytest.mark.parametrize(
    "score,threshold,label,review",
    [
        (0.0, 0.7, "low", True),
        (0.699, 0.7, "low", True),
        (0.7, 0.7, "medium", False),
        (0.899, 0.7, "medium", False),
        (0.9, 0.7, "high", False),
        (1.0, 0.7, "high", False),
        (0.75, 0.8, "low", True),
        (0.92, 0.95, "low", True),
    ],
)
def test_public_confidence_categories_keep_internal_score(
    rag, settings, path, score, threshold, label, review
) -> None:
    client, provider, _ = rag
    add(client)
    settings.confidence_threshold = threshold
    provider.replies = [
        json.dumps(
            {
                "answer": "Специализированное удаление плесени не выполняется.",
                "evidence_ids": ["e1"],
                "confidence": score,
                "coverage": "full",
            }
        )
    ]
    body = {"question": "Удаляете плесень?"}
    if path == "/ai/answer_with_sources":
        body["context"] = TEXT
    response = client.post(path, headers=HEADERS, json=body)
    assert response.status_code == 200
    answer = response.json()
    assert answer["confidence"] == label and answer["needs_review"] is review
    assert ("low_confidence" in answer["review_reasons"]) is review
    assert "confidence_score" not in answer
    saved = client.get(f"/qa/{answer['operation_id']}", headers=HEADERS).json()
    assert saved["usage"]["confidence_score"] == score
    assert saved["usage"]["confidence_threshold"] == threshold
    assert saved["result"]["confidence"] == label
    assert "confidence_score" not in saved["result"]


def test_openapi_declares_assignment_contracts(rag) -> None:
    client, _, _ = rag
    spec = client.get("/openapi.json", headers=HEADERS).json()
    for path in ["/kb/ask", "/ai/answer_with_sources"]:
        reference = spec["paths"][path]["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        schema = spec["components"]["schemas"][reference.rsplit("/", 1)[1]]
        assert schema["properties"]["confidence"]["enum"] == ["high", "medium", "low"]
        assert "confidence" in schema["required"]
        assert "confidence_score" not in schema["properties"]
    schema = spec["components"]["schemas"]["DocumentCreated"]
    assert {"status", "document_id"} <= set(schema["required"])
    assert schema["properties"]["status"]["const"] == "ok"
    assert (
        client.post("/kb/documents", headers=HEADERS, json={"title": "", "text": ""}).status_code
        == 422
    )


@pytest.mark.parametrize(
    "score,reasons,label", [(0.65, [], "medium"), (0.95, ["low_confidence"], "low")]
)
def test_legacy_history_and_export_do_not_rewrite_original_snapshot(
    rag, settings, score, reasons, label
) -> None:
    client, _, _ = rag
    add(client)
    cid = client.post("/conversations", headers=HEADERS, json={}).json()["id"]
    answer = client.post(
        "/kb/ask", headers=HEADERS, json={"question": "Плесень?", "conversation_id": cid}
    ).json()
    operation = answer["operation_id"]
    legacy = {
        **answer,
        "confidence": score,
        "review_reasons": reasons,
        "needs_review": bool(reasons),
    }
    snapshot = json.dumps(legacy)
    with Database(settings.database_path).connect() as connection:
        connection.execute(
            "UPDATE qa_runs SET result_json=?, needs_review=?, usage_json='{}' WHERE id=?",
            (snapshot, int(bool(reasons)), operation),
        )
    exported = client.get(f"/conversations/{cid}/export", headers=HEADERS).json()
    assert exported["schema_version"] == 2
    views = [
        client.get(f"/qa/{operation}", headers=HEADERS).json(),
        client.get("/history", headers=HEADERS).json()[0],
        client.get(f"/conversations/{cid}", headers=HEADERS).json()["runs"][0],
        exported["conversation"]["runs"][0],
    ]
    assert all(view["result"]["confidence"] == label for view in views)
    assert all(view["result"]["review_reasons"] == reasons for view in views)
    with Database(settings.database_path).connect() as connection:
        assert (
            connection.execute(
                "SELECT result_json FROM qa_runs WHERE id=?", (operation,)
            ).fetchone()[0]
            == snapshot
        )
