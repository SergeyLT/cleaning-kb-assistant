"""Сохраняемые диалоги, проверка сотрудником и ревизии итогов."""

import json
import sqlite3
import time
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from cleaning_kb.budget import DialogBudget
from cleaning_kb.database import Database, utc_now
from cleaning_kb.errors import AppError
from cleaning_kb.kb_service import KnowledgeService, pipeline_error
from cleaning_kb.logging_config import log_event
from cleaning_kb.provider import SUMMARY_PROMPT_VERSION
from cleaning_kb.rag_models import Answer, ModelSummary, Rewrite, legacy_answer_view, safe_answer
from cleaning_kb.source_views import source_names_view

VERIFICATION_NOTICE = (
    "Отметка сотрудника означает только факт проверки или уточнения. "
    "Результат внешней проверки не записан и не подтверждает цену, выезд или назначение."
)


def decode_run(row: sqlite3.Row, revision: int | None = None) -> dict[str, Any]:
    """Разобрать JSON; актуальность итога определяется текущей ревизией БД."""
    result = dict(row)
    for field in ("result_json", "sources_json", "usage_json"):
        result[field.removesuffix("_json")] = json.loads(result.pop(field) or "null")
    if row["kind"] == "answer" and isinstance(result["result"], dict):
        result["result"] = legacy_answer_view(result["result"])
    result["is_stale"] = row["kind"] == "summary" and row["source_revision"] != revision
    return result


class ConversationService:
    """Общие данные одной учётной записи, изолированные контексты обращений."""

    def __init__(self, database: Database, knowledge: KnowledgeService) -> None:
        self.database, self.knowledge = database, knowledge
        self.budget = DialogBudget(knowledge.settings, database)

    @staticmethod
    def _conversation(connection: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise AppError(404, "conversation_not_found", "Диалог не найден", "business_rule")
        return row

    @staticmethod
    def _idle(connection: sqlite3.Connection, conversation_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM qa_runs WHERE conversation_id=? AND state='running'", (conversation_id,)
        ).fetchone():
            raise AppError(
                409, "conversation_busy", "В диалоге уже выполняется операция", "business_rule"
            )

    @staticmethod
    def _history(connection: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
        return [
            {
                "turn_id": row["id"],
                "question": row["question"],
                "answer": row["answer"] or "Операция не завершена; ответа нет",
                "state": row["state"],
                "needs_review": bool(row["needs_review"]),
                "sources": json.loads(row["sources_json"]),
            }
            for row in connection.execute(
                "SELECT * FROM qa_runs WHERE conversation_id=? AND kind='answer' ORDER BY position",
                (conversation_id,),
            )
        ]

    def create(self, title: str | None, request_id: str) -> dict[str, Any]:
        conversation_id, now = str(uuid4()), utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO conversations (id, created_at, updated_at, title, auto_title) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, now, now, title or "Новый диалог", int(title is None)),
            )
            self.database.audit(
                "conversation_created",
                request_id,
                connection=connection,
                conversation_id=conversation_id,
            )
        return self.get(conversation_id)

    def list_conversations(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT c.*, (SELECT COUNT(*) FROM qa_runs q WHERE q.conversation_id=c.id "
                    "AND q.kind='answer' AND q.needs_review=1 AND q.reviewed_at IS NULL "
                    "AND q.state!='running') AS pending_reviews, EXISTS(SELECT 1 FROM qa_runs q "
                    "WHERE q.conversation_id=c.id AND q.state='running') AS busy "
                    "FROM conversations c ORDER BY updated_at DESC, id "
                    "LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]

    def get(self, conversation_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            conversation = dict(self._conversation(connection, conversation_id))
            history = self._history(connection, conversation_id)
            runs = [
                decode_run(row, conversation["revision"])
                for row in connection.execute(
                    "SELECT * FROM qa_runs WHERE conversation_id=? ORDER BY position",
                    (conversation_id,),
                )
            ]
        return source_names_view(
            {
                **conversation,
                "runs": runs,
                "budget": self.budget.status(history),
                "busy": any(row["state"] == "running" for row in runs),
            },
            self.database,
        )

    def rename(self, conversation_id: str, title: str, request_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._conversation(connection, conversation_id)
            self._idle(connection, conversation_id)
            connection.execute(
                "UPDATE conversations SET title=?, auto_title=0, updated_at=? WHERE id=?",
                (title, utc_now(), conversation_id),
            )
            self.database.audit(
                "conversation_renamed",
                request_id,
                connection=connection,
                conversation_id=conversation_id,
            )
        return self.get(conversation_id)

    def delete(self, conversation_id: str, confirm: bool, request_id: str) -> None:
        if not confirm:
            raise AppError(
                409,
                "deletion_confirmation_required",
                "Подтвердите удаление диалога",
                "business_rule",
            )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._conversation(connection, conversation_id)
            self._idle(connection, conversation_id)
            connection.execute("DELETE FROM audit_runs WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            self.database.audit("conversation_deleted", request_id, connection=connection)

    def _start(
        self, conversation_id: str, question: str, kind: str, request_id: str
    ) -> tuple[str, int, list[dict[str, Any]]]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conversation = self._conversation(connection, conversation_id)
            self._idle(connection, conversation_id)
            history = self._history(connection, conversation_id)
            if kind == "answer":
                self.budget.ensure_question(history, question)
            elif not history:
                raise AppError(
                    409,
                    "empty_conversation",
                    "Пока нечего подводить: задайте вопрос",
                    "business_rule",
                )
            operation_id = str(uuid4())
            position = connection.execute(
                "SELECT COALESCE(MAX(position),-1)+1 FROM qa_runs WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            revision = conversation["revision"]
            connection.execute(
                "INSERT INTO qa_runs (id, created_at, conversation_id, position, kind, "
                "question, source_revision, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    utc_now(),
                    conversation_id,
                    position,
                    kind,
                    question,
                    revision,
                    self.knowledge.settings.generation_model,
                ),
            )
            if kind == "answer":
                connection.execute(
                    "UPDATE conversations SET revision=revision+1, updated_at=?, "
                    "title=CASE WHEN auto_title=1 THEN ? ELSE title END, auto_title=0 WHERE id=?",
                    (utc_now(), question[:80], conversation_id),
                )
            self.database.audit(
                "conversation_operation_started",
                request_id,
                connection=connection,
                conversation_id=conversation_id,
            )
        return operation_id, revision, history

    def ask(self, conversation_id: str, question: str, request_id: str) -> Answer:
        if not self.knowledge.operation_slots.acquire(blocking=False):
            raise AppError(429, "operation_busy", "Сервис занят; повторите позже", "rate_limit")
        started = time.monotonic()
        try:
            operation_id, _, history = self._start(conversation_id, question, "answer", request_id)
            try:
                search_question = question
                if history:
                    try:
                        rewritten = Rewrite.model_validate_json(
                            self.knowledge.provider.rewrite(question, history, operation_id)
                        )
                    except ValidationError:
                        result = safe_answer(operation_id, "invalid_rewrite")
                        self.knowledge._save(result, request_id, started)
                        return result
                    if rewritten.ambiguous or not rewritten.search_question.strip():
                        result = safe_answer(operation_id, "ambiguous_context")
                        result.answer = (
                            "Уточните, к какой услуге или части обращения относится вопрос."
                        )
                        self.knowledge._save(result, request_id, started)
                        return result
                    search_question = rewritten.search_question
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE qa_runs SET search_question=? WHERE id=?",
                        (search_question, operation_id),
                    )
                return self.knowledge.complete_answer(
                    question,
                    request_id,
                    operation_id,
                    started,
                    search_question=search_question,
                    history=history,
                )
            except Exception as error:
                # complete_answer уже сохраняет свои ошибки; переписывание — ещё нет.
                with self.database.connect() as connection:
                    row = connection.execute(
                        "SELECT state FROM qa_runs WHERE id=?", (operation_id,)
                    ).fetchone()
                if row and row[0] == "running":
                    safe = pipeline_error(error)
                    self.knowledge._save(
                        safe_answer(operation_id, safe.code), request_id, started, error=safe.code
                    )
                    self.knowledge._log_failure(error, safe, operation_id, "rewrite")
                    raise safe from None
                raise
        finally:
            self.knowledge.operation_slots.release()

    def review(self, operation_id: str, request_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM qa_runs WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise AppError(404, "operation_not_found", "Операция не найдена", "business_rule")
            if row["conversation_id"]:
                self._idle(connection, row["conversation_id"])
            if row["kind"] != "answer" or not row["needs_review"] or row["state"] == "running":
                raise AppError(
                    409,
                    "review_not_applicable",
                    "Для этого ответа отметка проверки недоступна",
                    "business_rule",
                )
            if row["reviewed_at"] is None:
                connection.execute(
                    "UPDATE qa_runs SET reviewed_at=? WHERE id=?", (utc_now(), operation_id)
                )
                if row["conversation_id"]:
                    connection.execute(
                        "UPDATE conversations SET revision=revision+1, updated_at=? WHERE id=?",
                        (utc_now(), row["conversation_id"]),
                    )
                self.database.audit(
                    "answer_reviewed",
                    request_id,
                    connection=connection,
                    conversation_id=row["conversation_id"],
                )
            return dict(
                connection.execute(
                    "SELECT id, needs_review, reviewed_at FROM qa_runs WHERE id=?", (operation_id,)
                ).fetchone()
            )

    def history(
        self, *, pending_only: bool = False, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            runs = [
                decode_run(row, row["current_revision"])
                for row in connection.execute(
                    "SELECT q.*, c.revision AS current_revision FROM qa_runs q "
                    "LEFT JOIN conversations c "
                    "ON c.id=q.conversation_id "
                    + (
                        "WHERE q.kind='answer' AND q.needs_review=1 "
                        "AND q.reviewed_at IS NULL AND q.state!='running' "
                        if pending_only
                        else ""
                    )
                    + "ORDER BY q.created_at DESC, q.id LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]
        return source_names_view(runs, self.database)

    def summary(self, conversation_id: str, request_id: str) -> dict[str, Any]:
        if not self.knowledge.operation_slots.acquire(blocking=False):
            raise AppError(429, "operation_busy", "Сервис занят; повторите позже", "rate_limit")
        operation_id: str | None = None
        started = time.monotonic()
        try:
            operation_id, revision, history = self._start(
                conversation_id, "Подвести итог", "summary", request_id
            )
            evidence: dict[str, dict[str, Any]] = {}
            known_sources: dict[str, str] = {}
            turns = []
            for turn in history:
                ids = []
                for source in turn["sources"]:
                    signature = json.dumps(source, sort_keys=True, ensure_ascii=False)
                    key = known_sources.get(signature, "s" + str(len(evidence) + 1))
                    known_sources[signature] = key
                    evidence[key] = source
                    ids.append(key)
                turns.append(
                    {k: v for k, v in turn.items() if k != "sources"} | {"evidence_ids": ids}
                )
            payload = {
                "turns": turns,
                "evidence": [{"evidence_id": k, **s} for k, s in evidence.items()],
            }
            self.budget.ensure_summary(payload)
            model: ModelSummary | None = None
            for attempt in range(2):
                try:
                    candidate = ModelSummary.model_validate_json(
                        self.knowledge.provider.summarize(
                            payload, operation_id, repair=bool(attempt)
                        )
                    )
                    turn_ids = {turn["turn_id"] for turn in turns}
                    if any(
                        k not in evidence
                        for finding in candidate.findings
                        for k in finding.evidence_ids
                    ):
                        raise ValueError
                    if any(
                        k not in turn_ids for fact in candidate.client_facts for k in fact.turn_ids
                    ):
                        raise ValueError
                    model = candidate
                    break
                except (ValidationError, ValueError):
                    if attempt:
                        raise AppError(
                            503,
                            "invalid_summary",
                            "Итог не прошёл проверку; попробуйте позже",
                            "serialization",
                        ) from None
            assert model is not None
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                conversation = self._conversation(connection, conversation_id)
                if conversation["revision"] != revision:
                    raise AppError(
                        409,
                        "summary_revision_changed",
                        "Диалог изменился; обновите итог",
                        "business_rule",
                    )
                reviews = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT id AS operation_id, question, needs_review, reviewed_at, state "
                        "FROM qa_runs WHERE conversation_id=? AND kind='answer' "
                        "AND needs_review=1 ORDER BY position",
                        (conversation_id,),
                    )
                ]
                result = {
                    "operation_id": operation_id,
                    "source_revision": revision,
                    "client_facts": [
                        fact.model_dump() | {"origin": "user_reported"}
                        for fact in model.client_facts
                    ],
                    "findings": [
                        {
                            "text": finding.text,
                            "sources": [evidence[k] for k in dict.fromkeys(finding.evidence_ids)],
                        }
                        for finding in model.findings
                    ],
                    "limitations": model.limitations,
                    "reviews": reviews,
                    "verification_notice": VERIFICATION_NOTICE,
                }
                review_lines = [
                    f"{row['question']} — "
                    + (
                        f"проверка отмечена выполненной ({row['reviewed_at']}); "
                        "результат не записан"
                        if row["reviewed_at"]
                        else "ожидает проверки"
                    )
                    for row in reviews
                ]
                text = "\n".join(
                    ["Со слов пользователя:"]
                    + [f.text for f in model.client_facts]
                    + ["По сохранённым источникам:"]
                    + [
                        finding.text
                        + " ["
                        + "; ".join(
                            dict.fromkeys(evidence[k]["title"] for k in finding.evidence_ids)
                        )
                        + "]"
                        for finding in model.findings
                    ]
                    + ["Ограничения:"]
                    + model.limitations
                    + ["Проверка сотрудником:"]
                    + review_lines
                    + [VERIFICATION_NOTICE]
                )
                result["text"] = text
                usage = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT kind, model, usage_json, state FROM provider_calls "
                        "WHERE operation_id=? ORDER BY created_at",
                        (operation_id,),
                    )
                ]
                connection.execute(
                    "UPDATE qa_runs SET state='completed', answer=?, result_json=?, "
                    "sources_json=?, usage_json=? WHERE id=?",
                    (
                        text,
                        json.dumps(result, ensure_ascii=False),
                        json.dumps(list(evidence.values()), ensure_ascii=False),
                        json.dumps({"prompt_version": SUMMARY_PROMPT_VERSION, "calls": usage}),
                        operation_id,
                    ),
                )
                connection.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?", (utc_now(), conversation_id)
                )
                self.database.audit(
                    "conversation_summarized",
                    request_id,
                    connection=connection,
                    conversation_id=conversation_id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            log_event(
                "rag_operation_completed",
                fields={
                    "operation_id": operation_id,
                    "operation": "summary",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            return result | {"is_stale": False}
        except Exception as error:
            if operation_id:
                safe = error if isinstance(error, AppError) else pipeline_error(error)
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE qa_runs SET state='failed', needs_review=1, error=? "
                        "WHERE id=? AND state='running'",
                        (safe.code, operation_id),
                    )
                    self.database.audit(
                        "conversation_summarized",
                        request_id,
                        status="error",
                        error=safe.code,
                        connection=connection,
                        conversation_id=conversation_id,
                    )
                self.knowledge._log_failure(error, safe, operation_id, "summary")
                raise safe from None
            raise
        finally:
            self.knowledge.operation_slots.release()

    def export(self, conversation_id: str) -> dict[str, Any]:
        """Один JSON-снимок без ключей и cookies."""
        return {
            "schema_version": 2,
            "exported_at": utc_now(),
            "conversation": self.get(conversation_id),
        }

    def get_run(self, operation_id: str) -> dict[str, Any]:
        """Открыть в истории также одиночный ответ без диалога."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT q.*, c.revision AS current_revision FROM qa_runs q "
                "LEFT JOIN conversations c ON c.id=q.conversation_id WHERE q.id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise AppError(404, "operation_not_found", "Операция не найдена", "business_rule")
        return source_names_view(decode_run(row, row["current_revision"]), self.database)
