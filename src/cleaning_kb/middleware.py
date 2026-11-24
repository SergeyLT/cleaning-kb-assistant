"""Ограничение HTTP-ввода, корреляция и безопасная граница неожиданных ошибок."""

import logging
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cleaning_kb.config import Settings
from cleaning_kb.logging_config import REQUEST_ID, log_event, safe_frames


class RequestMiddleware:
    """Не выводить сырые исключения в журнал ASGI-сервера."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Пропустить lifespan, обработать один HTTP-запрос в своём контексте."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        context_token = REQUEST_ID.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started, status, response_started = time.monotonic(), 500, False

        async def safe_send(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status, response_started = message["status"], True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        async def reject(code: int, error: str, message: str) -> None:
            log_event(
                "request_rejected",
                level=logging.WARNING,
                fields={"error_code": error, "status": code},
            )
            await JSONResponse(
                {"error": {"code": error, "message": message}, "request_id": request_id},
                status_code=code,
            )(scope, receive, safe_send)

        try:
            headers = Headers(scope=scope)
            try:
                host_url = urlsplit("http://" + headers.get("host", ""))
                allowed_hosts = {self.settings.app_base_url.host, "127.0.0.1", "localhost", "::1"}
                valid_host = (
                    host_url.hostname in allowed_hosts
                    and host_url.username is None
                    and host_url.password is None
                    and not host_url.path
                    and not host_url.query
                    and not host_url.fragment
                )
                _validated_port = host_url.port
            except ValueError:
                valid_host = False
            if not valid_host:
                await reject(400, "host_rejected", "Адрес запроса не разрешён")
                return
            if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                origin = headers.get("origin")
                if origin is not None and origin != self.settings.origin:
                    await reject(403, "origin_rejected", "Источник запроса не разрешён")
                    return
            raw_length = headers.get("content-length")
            if raw_length is not None:
                try:
                    length = int(raw_length)
                except ValueError:
                    length = -1
                if length < 0:
                    await reject(400, "invalid_content_length", "Некорректная длина запроса")
                    return
                if length > self.settings.max_request_bytes:
                    await reject(413, "request_too_large", "Запрос превышает допустимый размер")
                    return
            body = bytearray()
            while True:
                chunk = await receive()
                if chunk["type"] == "http.disconnect":
                    return
                body.extend(chunk.get("body", b""))
                if len(body) > self.settings.max_request_bytes:
                    await reject(413, "request_too_large", "Запрос превышает допустимый размер")
                    return
                if not chunk.get("more_body", False):
                    break
            delivered = False

            async def buffered_receive() -> Message:
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.app(scope, buffered_receive, safe_send)
        except Exception as error:
            is_database = isinstance(error, sqlite3.Error)
            code = "database_unavailable" if is_database else "unexpected_internal_error"
            log_event(
                "request_failed",
                level=logging.ERROR,
                fields={
                    "component": "http",
                    "operation": "handle_request",
                    "stage": "processing",
                    "error_category": "database" if is_database else "internal",
                    "error_code": code,
                    "error_type": type(error).__name__,
                    "retryable": is_database,
                    "stack_frames": safe_frames(error),
                },
            )
            if not response_started:
                await JSONResponse(
                    {
                        "error": {"code": code, "message": "Не удалось выполнить запрос"},
                        "request_id": request_id,
                    },
                    status_code=503 if is_database else 500,
                )(scope, receive, safe_send)
        finally:
            route = scope.get("route")
            log_event(
                "request_completed",
                fields={
                    "route": getattr(route, "path", "unmatched"),
                    "method": scope["method"],
                    "status": status,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            REQUEST_ID.reset(context_token)
