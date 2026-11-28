"""Документы и ответы: SQLite владеет состоянием, Qdrant — только индексом."""

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import ValidationError

from cleaning_kb.config import Settings
from cleaning_kb.database import Database, utc_now
from cleaning_kb.errors import AppError
from cleaning_kb.logging_config import log_event, safe_frames
from cleaning_kb.pipelines import ingestion_pipeline, retrieval_pipeline
from cleaning_kb.provider import PROMPT_VERSION, Provider
from cleaning_kb.rag_models import (
    Answer,
    DocumentInput,
    Evidence,
    ModelAnswer,
    Source,
    confidence_label,
    safe_answer,
)
from cleaning_kb.vector_store import VectorStore


def split_text(text: str, size: int = 1000) -> list[str]:
    """Резать по абзацам/строкам, сохраняя каждую цитату подстрокой исходника."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    return chunks


SPECIFIC_PRICE = re.compile(
    r"сколько.{0,80}(?:стоит|стоить|заплачу)|"
    r"(?:точн|окончательн|итогов).{0,40}(?:цен|стоим)|"
    r"(?:назов|рассчита).{0,40}(?:цен|стоим)",
    re.IGNORECASE | re.DOTALL,
)

OPERATIONAL_INTENT = re.compile(r"подтверд|обещ|свободн\w*\s+ли|есть\s+ли", re.IGNORECASE)
VISIT_SUBJECT = re.compile(r"бригад|выезд|приезд|сотрудник|клинер", re.IGNORECASE)
SPECIFIC_VISIT = re.compile(
    r"завтра|сегодня|послезавтра|\bуже\b|\bмо(?:й|я|ё|е|ему|ем|ём|его|ей)\b|"
    r"понедельник|вторник|сред[ау]|четверг|пятниц|суббот|воскресень|\d{1,2}:\d{2}",
    re.IGNORECASE,
)
ASSIGNED_PERSON = re.compile(
    r"(?:какой|кто).{0,45}(?:сотрудник|клинер).{0,60}(?:назнач|приед)", re.IGNORECASE | re.DOTALL
)


def needs_operational_confirmation(question: str) -> bool:
    """У базы условий нет расписания; явный запрос конкретного подтверждения — менеджеру."""
    return bool(
        ASSIGNED_PERSON.search(question)
        or (
            OPERATIONAL_INTENT.search(question)
            and VISIT_SUBJECT.search(question)
            and SPECIFIC_VISIT.search(question)
        )
    )


def pipeline_error(error: BaseException) -> AppError:
    """Haystack оборачивает исключения компонентов: раскрывать только безопасный код."""
    current: BaseException | None = error
    for _ in range(8):
        if isinstance(current, AppError):
            return current
        current = current.__cause__ if current else None
    return AppError(
        503, "rag_pipeline_failed", "Не удалось выполнить операцию базы знаний", "external_service"
    )


class KnowledgeService:
    """Ограниченные синхронные операции без транзакции во время сетевого ожидания."""

    def __init__(
        self, settings: Settings, database: Database, provider: Provider, store: VectorStore
    ) -> None:
        self.settings, self.database, self.provider, self.store = (
            settings,
            database,
            provider,
            store,
        )
        self.document_lock = threading.RLock()
        self.operation_slots = threading.BoundedSemaphore(2)

    def recover(self) -> None:
        """Прерванные операции не продолжаются с незаметным повторным списанием."""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE documents SET indexing_status='failed', error='operation_interrupted' "
                "WHERE indexing_status='indexing'"
            )
            connection.execute(
                "UPDATE qa_runs SET state='interrupted', needs_review=1, "
                "error='operation_interrupted' WHERE state='running'"
            )
            connection.execute(
                "UPDATE provider_calls SET state='interrupted', error='operation_interrupted' "
                "WHERE state='started'"
            )

    def documents(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Список с состоянием; тексты доступны отдельной операцией просмотра."""
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id, created_at, title, indexing_status, is_active, error "
                    "FROM documents "
                    "ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]

    def document(self, document_id: str) -> dict[str, Any]:
        """Показать исходный документ и его текущее состояние."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id=?", (document_id,)
            ).fetchone()
        if row is None:
            raise AppError(404, "document_not_found", "Документ не найден", "business_rule")
        return dict(row)

    def add(self, data: DocumentInput, request_id: str) -> dict[str, Any]:
        """Одинаковые название/текст не создают дубль; ошибку можно повторить."""
        if len(data.text) > self.settings.max_document_chars:
            raise AppError(
                422, "document_too_large", "Превышен допустимый объём документа", "validation"
            )
        digest = hashlib.sha256((data.title + "\x00" + data.text).encode()).hexdigest()
        with self.document_lock:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT id FROM documents WHERE content_hash=?", (digest,)
                ).fetchone()
                if row:
                    document_id = row["id"]
                else:
                    if connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] >= 1000:
                        raise AppError(
                            409,
                            "document_limit",
                            "Достигнут предел 1000 документов MVP",
                            "business_rule",
                        )
                    document_id = str(uuid4())
                    connection.execute(
                        "INSERT INTO documents (id, created_at, title, text, content_hash) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (document_id, utc_now(), data.title, data.text, digest),
                    )
                    for position, text in enumerate(split_text(data.text)):
                        snippet_id = str(uuid5(NAMESPACE_URL, f"{document_id}:{position}:{text}"))
                        connection.execute(
                            "INSERT INTO snippets "
                            "(id, created_at, document_id, position, snippet_text, content_hash) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                snippet_id,
                                utc_now(),
                                document_id,
                                position,
                                text,
                                hashlib.sha256(text.encode()).hexdigest(),
                            ),
                        )
                    self.database.audit("document_added", request_id, connection=connection)
            document = self.document(document_id)
            if not document["is_active"]:
                raise AppError(
                    409,
                    "document_inactive",
                    "Этот документ исключён; добавьте новую редакцию",
                    "business_rule",
                )
            if document["indexing_status"] != "ready":
                return self.reindex(document_id, request_id)
            return document

    def rename(self, document_id: str, title: str, request_id: str) -> dict[str, Any]:
        """Менять только метаданные; снимки ответов и векторы остаются прежними."""
        with self.document_lock:
            if not self.operation_slots.acquire(blocking=False):
                raise AppError(
                    409, "operation_busy", "Дождитесь завершения текущей операции", "business_rule"
                )
            try:
                with self.database.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    document = connection.execute(
                        "SELECT * FROM documents WHERE id=?", (document_id,)
                    ).fetchone()
                    if document is None:
                        raise AppError(
                            404, "document_not_found", "Документ не найден", "business_rule"
                        )
                    digest = hashlib.sha256(
                        (title + "\x00" + document["text"]).encode()
                    ).hexdigest()
                    if connection.execute(
                        "SELECT 1 FROM documents WHERE content_hash=? AND id!=?",
                        (digest, document_id),
                    ).fetchone():
                        raise AppError(
                            409,
                            "document_duplicate",
                            "Документ с таким названием и текстом уже существует",
                            "business_rule",
                        )
                    if title != document["title"]:
                        connection.execute(
                            "UPDATE documents SET title=?, content_hash=? WHERE id=?",
                            (title, digest, document_id),
                        )
                        self.database.audit(
                            "document_renamed",
                            request_id,
                            connection=connection,
                            details={"document_id": document_id},
                        )
                return self.document(document_id)
            finally:
                self.operation_slots.release()

    def reindex(self, document_id: str, request_id: str) -> dict[str, Any]:
        """Повтор записи сохраняет UUID; готовность ставится после обоих хранилищ."""
        with self.document_lock:
            document = self.document(document_id)
            if not document["is_active"]:
                raise AppError(
                    409, "document_inactive", "Документ исключён из поиска", "business_rule"
                )
            if not self.operation_slots.acquire(blocking=False):
                raise AppError(
                    429, "operation_busy", "Сервис занят; повторите операцию позже", "rate_limit"
                )
            operation_id, started = str(uuid4()), time.monotonic()
            try:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE documents SET indexing_status='indexing', error=NULL WHERE id=?",
                        (document_id,),
                    )
                ingestion_pipeline(self.settings, self.database, self.provider, self.store).run(
                    {"embed": {"document_id": document_id, "operation_id": operation_id}}
                )
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE documents SET indexing_status='ready', error=NULL WHERE id=?",
                        (document_id,),
                    )
                    self.database.audit(
                        "document_indexed",
                        request_id,
                        connection=connection,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                log_event(
                    "rag_operation_completed",
                    fields={"operation_id": operation_id, "operation": "index"},
                )
            except Exception as error:
                safe = pipeline_error(error)
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE documents SET indexing_status='failed', error=? WHERE id=?",
                        (safe.code, document_id),
                    )
                    self.database.audit(
                        "document_indexed",
                        request_id,
                        status="error",
                        error=safe.code,
                        connection=connection,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                self._log_failure(error, safe, operation_id, "index")
            finally:
                self.operation_slots.release()
            # Документ сохранён даже при сбое; его id и ошибка доступны для повтора.
            return self.document(document_id)

    def deactivate(self, document_id: str, request_id: str) -> dict[str, Any]:
        """Снимки старых ответов сохраняются; будущий поиск отфильтрует документ."""
        with self.document_lock:
            self.document(document_id)
            with self.database.connect() as connection:
                connection.execute("UPDATE documents SET is_active=0 WHERE id=?", (document_id,))
                self.database.audit("document_deactivated", request_id, connection=connection)
            return self.document(document_id)

    def _active_ids(self) -> list[str]:
        with self.database.connect() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT d.id FROM documents d "
                    "WHERE d.is_active=1 AND d.indexing_status='ready' "
                    "AND NOT EXISTS (SELECT 1 FROM snippets s WHERE s.document_id=d.id "
                    "AND (s.embedding_model IS NOT ? OR s.embedding_dimension IS NOT ?))",
                    (self.settings.embedding_model, self.settings.embedding_dimension),
                )
            ]

    def _retrieve(self, question: str, operation_id: str) -> list[Evidence]:
        active = self._active_ids()
        if not active:
            return []
        output = retrieval_pipeline(self.provider, self.store).run(
            {
                "embed": {"question": question, "operation_id": operation_id},
                "retrieve": {"active_ids": active},
            }
        )
        evidence: list[Evidence] = []
        length = 0
        with self.database.connect() as connection:
            for snippet_id, _score in output["retrieve"]["hits"]:
                row = connection.execute(
                    "SELECT s.*, d.title FROM snippets s JOIN documents d "
                    "ON d.id=s.document_id WHERE s.id=? "
                    "AND d.is_active=1 AND d.indexing_status='ready'",
                    (snippet_id,),
                ).fetchone()
                if row is None or row["document_id"] not in active:
                    continue
                if length + len(row["snippet_text"]) > self.settings.max_context_chars:
                    break
                length += len(row["snippet_text"])
                evidence.append(
                    Evidence(
                        evidence_id=f"e{len(evidence) + 1}",
                        retrieval_score=_score,
                        source=Source(
                            document_id=row["document_id"],
                            title=row["title"],
                            snippet_id=row["id"],
                            quote=row["snippet_text"],
                        ),
                    )
                )
        return evidence

    def _generate(
        self,
        question: str,
        evidence: list[Evidence],
        operation_id: str,
        history: list[dict[str, Any]] | None = None,
    ) -> Answer:
        if not evidence:
            return safe_answer(operation_id, "no_evidence")
        available = {e.evidence_id: e.source for e in evidence}
        for attempt in range(2):
            raw = (
                self.provider.generate(
                    question, evidence, operation_id, repair=bool(attempt), history=history
                )
                if history
                else self.provider.generate(question, evidence, operation_id, repair=bool(attempt))
            )
            try:
                model = ModelAnswer.model_validate_json(raw)
                if not model.answer.strip() or any(
                    key not in available for key in model.evidence_ids
                ):
                    raise ValueError
                sources = [available[key] for key in dict.fromkeys(model.evidence_ids)]
                reasons = []
                if model.coverage != "full":
                    reasons.append(
                        {
                            "partial": "partial_coverage",
                            "none": "no_answer",
                            "conflict": "conflicting_sources",
                        }[model.coverage]
                    )
                if model.confidence < self.settings.confidence_threshold:
                    reasons.append("low_confidence")
                if not sources:
                    return safe_answer(operation_id, "no_verified_sources")
                return Answer(
                    operation_id=operation_id,
                    answer=model.answer,
                    sources=sources,
                    confidence=confidence_label(
                        model.confidence, self.settings.confidence_threshold
                    ),
                    confidence_score=model.confidence,
                    needs_review=bool(reasons),
                    review_reasons=reasons,
                )
            except (ValidationError, ValueError):
                if attempt:
                    return safe_answer(operation_id, "invalid_model_output")
        raise AssertionError("Недостижимое состояние")

    def ask(self, question: str, request_id: str, *, context: str | None = None) -> Answer:
        """Сохранить только проверенный результат; ошибки тоже остаются в истории операций."""
        if context is not None and len(context) > self.settings.max_context_chars:
            raise AppError(
                422, "context_too_large", "Превышен допустимый объём контекста", "validation"
            )
        if not self.operation_slots.acquire(blocking=False):
            raise AppError(429, "operation_busy", "Сервис занят; повторите позже", "rate_limit")
        operation_id, started = str(uuid4()), time.monotonic()
        try:
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO qa_runs (id, created_at, question, search_question, model) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (operation_id, utc_now(), question, question, self.settings.generation_model),
                )
            return self.complete_answer(
                question, request_id, operation_id, started, context=context
            )
        finally:
            self.operation_slots.release()

    def complete_answer(
        self,
        question: str,
        request_id: str,
        operation_id: str,
        started: float,
        *,
        context: str | None = None,
        search_question: str | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> Answer:
        """Завершить заранее зарегистрированный ответ; слот принадлежит вызывающему."""
        try:
            evidence = (
                self._retrieve(search_question or question, operation_id)
                if context is None
                else [
                    Evidence(
                        evidence_id=f"e{i + 1}",
                        source=Source(
                            document_id=None,
                            title="Контекст запроса",
                            snippet_id=f"context-{i + 1}",
                            quote=text,
                        ),
                    )
                    for i, text in enumerate(split_text(context))
                ]
            )
            retrieval = [
                {
                    "evidence_id": e.evidence_id,
                    "document_id": e.source.document_id,
                    "snippet_id": e.source.snippet_id,
                    "score": e.retrieval_score,
                }
                for e in evidence
            ]
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE qa_runs SET usage_json=? WHERE id=? AND state='running'",
                    (json.dumps({"retrieval": retrieval}), operation_id),
                )
            result = self._generate(question, evidence, operation_id, history)
            # В утверждённой базе нет прайса: запрос конкретной цены требует
            # уточнения независимо от самооценки модели. Внешний context — другой контракт.
            if context is None and (
                SPECIFIC_PRICE.search(question)
                or SPECIFIC_PRICE.search(search_question or question)
            ):
                result.needs_review = True
                if "price_requires_confirmation" not in result.review_reasons:
                    result.review_reasons.append("price_requires_confirmation")
            if context is None and (
                needs_operational_confirmation(question)
                or needs_operational_confirmation(search_question or question)
            ):
                result.needs_review = True
                if "operational_confirmation_required" not in result.review_reasons:
                    result.review_reasons.append("operational_confirmation_required")
            # Деактивация во время генерации не должна выдавать старые условия.
            if context is None:
                with self.document_lock:
                    active = set(self._active_ids())
                    if any(source.document_id not in active for source in result.sources):
                        result = safe_answer(operation_id, "sources_changed")
                    self._save(result, request_id, started)
            else:
                self._save(result, request_id, started)
            return result
        except Exception as error:
            safe = pipeline_error(error)
            result = safe_answer(operation_id, safe.code)
            self._save(result, request_id, started, error=safe.code)
            self._log_failure(error, safe, operation_id, "answer")
            # Техническая ошибка — явный HTTP-код, а не успешный ответ модели.
            raise safe from None

    def _save(
        self, result: Answer, request_id: str, started: float, *, error: str | None = None
    ) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT conversation_id, usage_json FROM qa_runs WHERE id=? AND state='running'",
                (result.operation_id,),
            ).fetchone()
            if row is None:
                raise AppError(
                    409,
                    "operation_not_running",
                    "Операция уже завершена или удалена",
                    "business_rule",
                )
            conversation_id = row["conversation_id"]
            metadata = json.loads(row["usage_json"] or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
            if result.confidence_score is not None:
                metadata["confidence_score"] = result.confidence_score
                metadata["confidence_threshold"] = self.settings.confidence_threshold
            usage = [
                dict(row)
                for row in connection.execute(
                    "SELECT kind, model, state, usage_json, reserved_tokens FROM provider_calls "
                    "WHERE operation_id=? ORDER BY created_at",
                    (result.operation_id,),
                )
            ]
            connection.execute(
                "UPDATE qa_runs SET state=?, answer=?, result_json=?, sources_json=?, "
                "needs_review=?, error=?, usage_json=? WHERE id=?",
                (
                    "failed" if error else "completed",
                    result.answer,
                    result.model_dump_json(),
                    json.dumps([s.model_dump() for s in result.sources], ensure_ascii=False),
                    int(result.needs_review),
                    error,
                    json.dumps({**metadata, "prompt_version": PROMPT_VERSION, "calls": usage}),
                    result.operation_id,
                ),
            )
            self.database.audit(
                "answer_with_context"
                if any(s.document_id is None for s in result.sources)
                else "kb_answer",
                request_id,
                status="error" if error else "ok",
                error=error,
                duration_ms=int((time.monotonic() - started) * 1000),
                connection=connection,
                conversation_id=conversation_id,
            )
            if conversation_id:
                connection.execute(
                    "UPDATE conversations SET revision=revision+1, updated_at=? WHERE id=?",
                    (utc_now(), conversation_id),
                )
        if not error:
            log_event(
                "rag_operation_completed",
                fields={
                    "operation_id": result.operation_id,
                    "operation": "answer",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )

    @staticmethod
    def _log_failure(
        error: BaseException, safe: AppError, operation_id: str, operation: str
    ) -> None:
        log_event(
            "rag_operation_failed",
            level=logging.ERROR,
            fields={
                "operation_id": operation_id,
                "operation": operation,
                "component": "rag",
                "stage": operation,
                "error_code": safe.code,
                "error_category": safe.category,
                "error_type": type(error).__name__,
                "retryable": False,
                "stack_frames": safe_frames(error),
            },
        )
