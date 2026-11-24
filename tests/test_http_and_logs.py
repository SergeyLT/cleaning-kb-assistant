"""Проверка готовности, ограничений HTTP и полезных логов без содержимого."""

import asyncio
import json
import logging
import sqlite3

import httpx
import pytest
from conftest import API_KEY
from fastapi.testclient import TestClient

from cleaning_kb.app import create_app
from cleaning_kb.config import Settings
from cleaning_kb.database import Database
from cleaning_kb.logging_config import JsonHandler, configure_logging, log_event


def test_readiness_distinguishes_live_process_and_dependencies(settings: Settings) -> None:
    state = {"available": True}
    with TestClient(
        create_app(settings, qdrant_probe=lambda: state["available"]),
        base_url="http://127.0.0.1:8000",
    ) as client:
        assert client.get("/live").status_code == 200
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["checks"] == {"sqlite": True, "qdrant": True}
        state["available"] = False
        assert client.get("/health").status_code == 503
        assert client.get("/live").status_code == 200
        state["available"] = True
        assert client.get("/health").status_code == 200
        with Database(settings.database_path).connect() as connection:
            connection.execute("DROP TABLE documents")
        assert client.get("/health").json()["checks"]["sqlite"] is False


def test_dependency_connection_error_is_safe(
    settings: Settings, capsys: pytest.CaptureFixture
) -> None:
    def unavailable() -> bool:
        raise httpx.ConnectError("sensitive-response-and-url")

    with TestClient(
        create_app(settings, qdrant_probe=unavailable), base_url="http://127.0.0.1:8000"
    ) as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert "sensitive" not in response.text
    assert "sensitive" not in capsys.readouterr().out


def test_request_limit_and_invalid_host(client: TestClient, settings: Settings) -> None:
    response = client.post("/auth/login", content=b"x" * (settings.max_request_bytes + 1))
    assert response.status_code == 413
    assert client.get("/live", headers={"Host": "untrusted.invalid"}).status_code == 400
    assert client.get("/live", headers={"Host": "user@127.0.0.1"}).status_code == 400


def test_streamed_body_limit_without_content_length(settings: Settings) -> None:
    app = create_app(settings, qdrant_probe=lambda: True)

    async def run_request() -> list[dict]:
        messages = iter(
            [
                {
                    "type": "http.request",
                    "body": b"x" * settings.max_request_bytes,
                    "more_body": True,
                },
                {"type": "http.request", "body": b"x", "more_body": False},
            ]
        )
        output = []

        async def receive() -> dict:
            return next(messages)

        async def send(message: dict) -> None:
            output.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/auth/login",
            "raw_path": b"/auth/login",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1")],
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 1234),
        }
        await app(scope, receive, send)
        return output

    messages = asyncio.run(run_request())
    assert messages[0]["status"] == 413


def test_unknown_exception_and_correlation_are_safe(
    settings: Settings, capsys: pytest.CaptureFixture
) -> None:
    app = create_app(settings, qdrant_probe=lambda: True)

    @app.get("/test-error")
    def broken() -> None:
        raise RuntimeError("private-text-and-api-key-should-not-leak")

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get(
            "/test-error?token=private-query", headers={"X-Request-ID": "untrusted-id"}
        )
        assert response.status_code == 500
        assert "private" not in response.text
        request_id = response.headers["x-request-id"]
        assert request_id != "untrusted-id"
        assert response.json()["request_id"] == request_id
    output = capsys.readouterr().out
    assert "private" not in output and "untrusted-id" not in output
    records = [json.loads(line) for line in output.splitlines()]
    failed = next(row for row in records if row["event"] == "request_failed")
    completed = next(row for row in records if row["event"] == "request_completed")
    assert failed["request_id"] == completed["request_id"] == request_id
    assert failed["error_type"] == "RuntimeError"
    assert failed["error_category"] == "internal"
    assert failed["operation"] == "handle_request"
    assert failed["stack_frames"][-1]["function"] == "broken"
    assert completed["route"] == "/test-error"


def test_database_error_has_distinct_status_and_diagnostics(
    settings: Settings, capsys: pytest.CaptureFixture
) -> None:
    app = create_app(settings, qdrant_probe=lambda: True)

    @app.get("/test-database-error")
    def broken_database() -> None:
        raise sqlite3.OperationalError("private-database-path")

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get("/test-database-error")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "database_unavailable"
    output = capsys.readouterr().out
    assert "private-database-path" not in output
    failed = next(json.loads(line) for line in output.splitlines() if '"request_failed"' in line)
    assert failed["error_category"] == "database" and failed["retryable"] is True


def test_log_whitelist_redaction_and_repeat_configuration(capsys: pytest.CaptureFixture) -> None:
    configure_logging("test", "log-test")
    configure_logging("test", "log-test")
    assert len([h for h in logging.getLogger().handlers if isinstance(h, JsonHandler)]) == 1
    log_event(
        "request_completed",
        fields={
            "status": 200,
            "body": "sensitive-body",
            "password": "sensitive-password",
            "error_code": "Bearer sk-sensitive-token",
        },
    )
    logging.getLogger("external").warning("sensitive-message %s", "sensitive-argument")
    output = capsys.readouterr().out
    assert "sensitive" not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert len(records) == 2
    assert records[0]["error_code"] == "[скрыто]"
    assert "body" not in records[0]
    assert {
        "timestamp",
        "level",
        "service",
        "environment",
        "version",
        "logger",
        "event",
        "message",
        "request_id",
    } <= records[0].keys()


def test_secret_inputs_and_unknown_path_not_logged(
    client: TestClient, capsys: pytest.CaptureFixture
) -> None:
    response = client.post(
        "/auth/login", json={"username": "sensitive-user", "password": "sensitive-password"}
    )
    assert response.status_code == 401
    missing = client.get("/sensitive-path?api_key=sensitive-query", headers={"X-API-Key": API_KEY})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    output = capsys.readouterr().out
    assert "sensitive" not in output and API_KEY not in output
