"""Фабрика HTTP-приложения и операции технической основы."""

import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, cast

import httpx
from fastapi import Depends, FastAPI, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.exceptions import HTTPException

from cleaning_kb.auth import COOKIE_NAME, AuthService
from cleaning_kb.config import Settings, load_settings
from cleaning_kb.conversations import ConversationService
from cleaning_kb.database import Database
from cleaning_kb.errors import AppError
from cleaning_kb.health import Readiness
from cleaning_kb.kb_service import KnowledgeService
from cleaning_kb.locking import InstanceLock
from cleaning_kb.logging_config import configure_logging, log_event, safe_frames
from cleaning_kb.middleware import RequestMiddleware
from cleaning_kb.provider import PROMPT_VERSION, Provider, ProxyProvider
from cleaning_kb.provider_budget import BudgetReset, ProviderBudget, ProviderLimits
from cleaning_kb.rag_models import (
    Answer,
    AskInput,
    ContextInput,
    ConversationInput,
    DocumentCreated,
    DocumentInput,
    RenameInput,
)
from cleaning_kb.vector_store import QdrantStore, VectorStore
from cleaning_kb.web import mount_ui

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass
class Runtime:
    """Ресурсы одного экземпляра; настройки тестов передаются явно."""

    database: Database
    auth: AuthService
    readiness: Readiness
    knowledge: KnowledgeService
    conversations: ConversationService
    provider_budget: ProviderBudget


class LoginInput(BaseModel):
    """Реквизиты не включаются в сообщения ошибок валидации."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=1, max_length=256)


def runtime_from(request: Request) -> Runtime:
    """Получить ресурсы, созданные во время lifespan."""
    return cast(Runtime, request.app.state.runtime)


def require_access(
    request: Request,
    _api_key: Annotated[str | None, Security(API_KEY_HEADER)] = None,
) -> None:
    """Защитить API ключом или браузерной сессией с CSRF для изменений."""
    runtime_from(request).auth.authorize(request)


def create_app(
    settings: Settings | None = None,
    *,
    qdrant_probe: Callable[[], bool] | None = None,
    provider: Provider | None = None,
    vector_store: VectorStore | None = None,
) -> FastAPI:
    """Создать приложение без скрытых тестовых переключателей окружения."""
    settings = settings or load_settings()
    database = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.app_environment, settings.app_version)
        try:
            with InstanceLock(settings.database_path):
                database.initialize()
                headers = (
                    {"api-key": settings.qdrant_api_key.get_secret_value()}
                    if settings.qdrant_api_key
                    else {}
                )
                with (
                    httpx.Client(
                        timeout=settings.dependency_timeout_seconds,
                        follow_redirects=False,
                        trust_env=False,
                        headers=headers,
                    ) as client,
                    httpx.Client(follow_redirects=False, trust_env=False) as provider_client,
                ):

                    def probe() -> bool:
                        response = client.get(str(settings.qdrant_url).rstrip("/") + "/readyz")
                        return response.status_code == 200

                    knowledge = KnowledgeService(
                        settings,
                        database,
                        provider or ProxyProvider(settings, database, provider_client),
                        vector_store or QdrantStore(settings, client),
                    )
                    knowledge.recover()
                    app.state.runtime = Runtime(
                        database,
                        AuthService(settings, database),
                        Readiness(database, qdrant_probe or probe),
                        knowledge,
                        ConversationService(database, knowledge),
                        ProviderBudget(settings, database),
                    )
                    log_event("application_started")
                    try:
                        yield
                    finally:
                        log_event("application_stopped")
        except Exception as error:
            log_event(
                "startup_failed",
                level=logging.ERROR,
                fields={
                    "component": "application",
                    "operation": "lifespan",
                    "stage": "startup",
                    "error_category": "database"
                    if isinstance(error, sqlite3.Error)
                    else "configuration",
                    "error_code": "startup_failed",
                    "error_type": type(error).__name__,
                    "retryable": False,
                    "stack_frames": safe_frames(error),
                },
            )
            raise RuntimeError(
                "Не удалось запустить приложение; проверьте безопасный журнал"
            ) from None

    app = FastAPI(
        title="База знаний клининговой компании",
        version=settings.app_version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(RequestMiddleware, settings=settings)

    @app.exception_handler(AppError)
    async def app_error(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, AppError)
        log_event(
            "request_rejected",
            level=logging.WARNING,
            fields={
                "error_category": error.category,
                "error_code": error.code,
                "error_type": type(error).__name__,
                "component": "http",
                "operation": "handle_request",
                "stage": "validation",
                "retryable": False,
            },
        )
        return JSONResponse(
            {
                "error": {"code": error.code, "message": error.message},
                "request_id": request.state.request_id,
            },
            status_code=error.status,
            headers={"Retry-After": "60"} if error.status == 429 else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: Exception) -> JSONResponse:
        log_event(
            "request_rejected",
            level=logging.WARNING,
            fields={
                "component": "http",
                "operation": "handle_request",
                "stage": "validation",
                "error_category": "validation",
                "error_code": "request_validation_failed",
                "error_type": "RequestValidationError",
                "retryable": False,
            },
        )
        return JSONResponse(
            {
                "error": {
                    "code": "request_validation_failed",
                    "message": "Проверьте формат и допустимые размеры полей запроса",
                },
                "request_id": request.state.request_id,
            },
            status_code=422,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, HTTPException)
        code, message = {
            404: ("not_found", "Запись или страница не найдена"),
            405: ("method_not_allowed", "Метод запроса не поддерживается"),
        }.get(error.status_code, ("http_error", "HTTP-запрос отклонён"))
        return JSONResponse(
            {"error": {"code": code, "message": message}, "request_id": request.state.request_id},
            status_code=error.status_code,
            headers={"Allow": error.headers["Allow"]}
            if error.headers and "Allow" in error.headers
            else None,
        )

    mount_ui(app, lambda request: runtime_from(request).auth)

    @app.get("/live", summary="Проверка процесса")
    def live() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    @app.get("/health", summary="Готовность приложения и обязательных зависимостей")
    def health(request: Request) -> JSONResponse:
        checks = runtime_from(request).readiness.check()
        ready = all(checks.values())
        return JSONResponse(
            {
                "status": "ok" if ready else "unavailable",
                "version": settings.app_version,
                "checks": checks,
            },
            status_code=200 if ready else 503,
        )

    @app.post("/auth/login", summary="Войти в браузерную сессию")
    def login(data: LoginInput, request: Request) -> JSONResponse:
        if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
            raise AppError(415, "json_required", "Ожидается JSON-запрос", "validation")
        token, session = runtime_from(request).auth.login(
            data.username,
            data.password.get_secret_value(),
            request.state.request_id,
        )
        response = JSONResponse({"authenticated": True, "csrf_token": session.csrf_token})
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/auth/session", summary="Получить состояние сессии и CSRF-токен")
    def current_session(request: Request) -> dict[str, object]:
        session = runtime_from(request).auth.session(request.cookies.get(COOKIE_NAME))
        return {"authenticated": True, "csrf_token": session.csrf_token}

    @app.post("/auth/logout", dependencies=[Depends(require_access)], summary="Завершить сессию")
    def logout(request: Request) -> JSONResponse:
        runtime = runtime_from(request)
        session = runtime.auth.session(request.cookies.get(COOKIE_NAME))
        runtime.auth.logout(session, request.state.request_id)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            COOKIE_NAME,
            path="/",
            secure=settings.secure_cookies,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get(
        "/api/status", dependencies=[Depends(require_access)], summary="Состояние схемы данных"
    )
    def system_status(request: Request) -> dict[str, object]:
        with runtime_from(request).database.connect() as connection:
            migrations = [
                row[0]
                for row in connection.execute("SELECT name FROM schema_migrations ORDER BY name")
            ]
        return {
            "version": settings.app_version,
            "migrations": migrations,
            "ai": {
                "prompt_version": PROMPT_VERSION,
                "generation_model": settings.generation_model,
                "embedding_model": settings.embedding_model,
                "embedding_dimension": settings.embedding_dimension,
                "retrieval_top_k": settings.retrieval_top_k,
                "retrieval_min_score": settings.retrieval_min_score,
                "confidence_threshold": settings.confidence_threshold,
            },
        }

    @app.get("/openapi.json", dependencies=[Depends(require_access)], include_in_schema=False)
    def openapi_schema() -> dict[str, object]:
        return app.openapi()

    @app.get("/docs", dependencies=[Depends(require_access)], include_in_schema=False)
    def docs() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title="API базы знаний")

    @app.post(
        "/kb/documents",
        dependencies=[Depends(require_access)],
        summary="Добавить и индексировать документ",
    )
    def add_document(data: DocumentInput, request: Request) -> DocumentCreated:
        document = runtime_from(request).knowledge.add(data, request.state.request_id)
        return DocumentCreated(
            **{**document, "is_active": bool(document["is_active"])},
            status="ok",
            document_id=document["id"],
        )

    @app.get("/kb/documents", dependencies=[Depends(require_access)], summary="Список документов")
    def list_documents(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return runtime_from(request).knowledge.documents(limit, offset)

    @app.get(
        "/kb/documents/{document_id}",
        dependencies=[Depends(require_access)],
        summary="Просмотр документа",
    )
    def get_document(document_id: str, request: Request) -> dict[str, object]:
        return runtime_from(request).knowledge.document(document_id)

    @app.post(
        "/kb/documents/{document_id}/reindex",
        dependencies=[Depends(require_access)],
        summary="Повторить индексацию",
    )
    def reindex_document(document_id: str, request: Request) -> dict[str, object]:
        return runtime_from(request).knowledge.reindex(document_id, request.state.request_id)

    @app.post(
        "/kb/documents/{document_id}/deactivate",
        dependencies=[Depends(require_access)],
        summary="Исключить документ из поиска",
    )
    def deactivate_document(document_id: str, request: Request) -> dict[str, object]:
        return runtime_from(request).knowledge.deactivate(document_id, request.state.request_id)

    @app.patch(
        "/kb/documents/{document_id}",
        dependencies=[Depends(require_access)],
        summary="Переименовать документ без индексации",
    )
    def rename_document(document_id: str, data: RenameInput, request: Request) -> dict[str, object]:
        return runtime_from(request).knowledge.rename(
            document_id, data.title, request.state.request_id
        )

    @app.get(
        "/settings/provider", dependencies=[Depends(require_access)], summary="Расход и лимиты"
    )
    def provider_settings(request: Request) -> dict[str, object]:
        return runtime_from(request).provider_budget.status()

    @app.put(
        "/settings/provider", dependencies=[Depends(require_access)], summary="Сохранить лимиты"
    )
    def save_provider_settings(data: ProviderLimits, request: Request) -> dict[str, object]:
        return runtime_from(request).provider_budget.save(
            data, request.state.actor_type, request.state.actor_id, request.state.request_id
        )

    @app.post(
        "/settings/provider/reset",
        dependencies=[Depends(require_access)],
        summary="Сбросить суточные счётчики без удаления истории",
    )
    def reset_provider_budget(data: BudgetReset, request: Request) -> dict[str, object]:
        return runtime_from(request).provider_budget.reset(
            request.state.actor_type, request.state.actor_id, request.state.request_id
        )

    @app.post("/kb/ask", dependencies=[Depends(require_access)], summary="Ответить по базе знаний")
    def ask(data: AskInput, request: Request) -> Answer:
        if data.conversation_id is not None:
            return runtime_from(request).conversations.ask(
                data.conversation_id, data.question, request.state.request_id
            )
        return runtime_from(request).knowledge.ask(data.question, request.state.request_id)

    @app.post(
        "/ai/answer_with_sources",
        dependencies=[Depends(require_access)],
        summary="Ответить по переданному контексту",
    )
    def answer_with_sources(data: ContextInput, request: Request) -> Answer:
        return runtime_from(request).knowledge.ask(
            data.question, request.state.request_id, context=data.context
        )

    @app.post("/conversations", dependencies=[Depends(require_access)], summary="Создать диалог")
    def create_conversation(data: ConversationInput, request: Request) -> dict[str, object]:
        return runtime_from(request).conversations.create(data.title, request.state.request_id)

    @app.get("/conversations", dependencies=[Depends(require_access)], summary="Список диалогов")
    def list_conversations(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return runtime_from(request).conversations.list_conversations(limit, offset)

    @app.get(
        "/conversations/{conversation_id}",
        dependencies=[Depends(require_access)],
        summary="Открыть диалог",
    )
    def get_conversation(conversation_id: str, request: Request) -> dict[str, object]:
        return runtime_from(request).conversations.get(conversation_id)

    @app.patch(
        "/conversations/{conversation_id}",
        dependencies=[Depends(require_access)],
        summary="Переименовать диалог",
    )
    def rename_conversation(
        conversation_id: str, data: RenameInput, request: Request
    ) -> dict[str, object]:
        return runtime_from(request).conversations.rename(
            conversation_id, data.title, request.state.request_id
        )

    @app.delete(
        "/conversations/{conversation_id}",
        dependencies=[Depends(require_access)],
        summary="Удалить диалог",
    )
    def delete_conversation(
        conversation_id: str, request: Request, confirm: bool = False
    ) -> dict[str, bool]:
        runtime_from(request).conversations.delete(
            conversation_id, confirm, request.state.request_id
        )
        return {"deleted": True}

    @app.post(
        "/conversations/{conversation_id}/summary",
        dependencies=[Depends(require_access)],
        summary="Подвести итог",
    )
    def summarize_conversation(conversation_id: str, request: Request) -> dict[str, object]:
        return runtime_from(request).conversations.summary(
            conversation_id, request.state.request_id
        )

    @app.get(
        "/conversations/{conversation_id}/export",
        dependencies=[Depends(require_access)],
        summary="Экспортировать JSON",
    )
    def export_conversation(conversation_id: str, request: Request) -> JSONResponse:
        return JSONResponse(
            runtime_from(request).conversations.export(conversation_id),
            headers={"Content-Disposition": 'attachment; filename="conversation.json"'},
        )

    @app.post(
        "/qa/{operation_id}/review",
        dependencies=[Depends(require_access)],
        summary="Отметить проверку выполненной",
    )
    def review_answer(operation_id: str, request: Request) -> dict[str, object]:
        return runtime_from(request).conversations.review(operation_id, request.state.request_id)

    @app.get("/history", dependencies=[Depends(require_access)], summary="История обработки")
    def processing_history(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return runtime_from(request).conversations.history(limit=limit, offset=offset)

    @app.get(
        "/reviews", dependencies=[Depends(require_access)], summary="Очередь непроверенных ответов"
    )
    def review_queue(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return runtime_from(request).conversations.history(
            pending_only=True, limit=limit, offset=offset
        )

    @app.get(
        "/qa/{operation_id}",
        dependencies=[Depends(require_access)],
        summary="Открыть результат обработки",
    )
    def get_operation(operation_id: str, request: Request) -> dict[str, object]:
        return runtime_from(request).conversations.get_run(operation_id)

    @app.get("/audit", dependencies=[Depends(require_access)], summary="Журнал действий")
    def audit_records(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        conversation_id: Annotated[str | None, Query(max_length=80)] = None,
    ) -> list[dict[str, object]]:
        with runtime_from(request).database.connect() as connection:
            return [
                dict(row) | {"details": json.loads(row["details_json"])}
                for row in connection.execute(
                    "SELECT id, created_at, request_id, conversation_id, action, status, error, "
                    "duration_ms, actor_type, actor_id, details_json "
                    "FROM audit_runs "
                    + ("WHERE conversation_id=? " if conversation_id is not None else "")
                    + "ORDER BY id DESC LIMIT ? OFFSET ?",
                    (conversation_id, limit, offset)
                    if conversation_id is not None
                    else (limit, offset),
                )
            ]

    return app
