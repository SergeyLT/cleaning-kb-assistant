"""Границы доступа, CSRF, истечение и отзыв сессий."""

import json
import sqlite3

import pytest
from conftest import API_KEY, PASSWORD
from fastapi.testclient import TestClient

from cleaning_kb.app import create_app
from cleaning_kb.auth import COOKIE_NAME
from cleaning_kb.config import Settings
from cleaning_kb.database import Database


def login(client: TestClient) -> str:
    response = client.post("/auth/login", json={"username": "manager", "password": PASSWORD})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_api_and_schema_require_access(client: TestClient) -> None:
    for url in ("/api/status", "/docs", "/openapi.json", "/auth/session"):
        assert client.get(url).status_code == 401
    response = client.get("/api/status", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    assert response.json()["migrations"] == [
        "001_initial.sql",
        "002_provider_calls.sql",
        "003_conversations.sql",
    ]
    schema = client.get("/openapi.json", headers={"X-API-Key": API_KEY})
    assert schema.status_code == 200
    assert "APIKeyHeader" in schema.json()["components"]["securitySchemes"]


def test_session_csrf_and_logout_revoke_old_cookie(client: TestClient) -> None:
    csrf = login(client)
    old_cookie = client.cookies.get(COOKIE_NAME)
    assert client.get("/api/status").status_code == 200
    assert client.get("/auth/session").json()["csrf_token"] == csrf
    assert client.post("/auth/logout").status_code == 403
    assert client.post("/auth/logout", headers={"X-CSRF-Token": "incorrect"}).status_code == 403
    response = client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert client.get("/api/status").status_code == 401
    client.cookies.set(COOKIE_NAME, old_cookie)
    assert client.get("/api/status").status_code == 401


def test_cookie_properties_and_invalid_api_key_precedence(client: TestClient) -> None:
    response = client.post("/auth/login", json={"username": "manager", "password": PASSWORD})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Max-Age=28800" in cookie
    assert client.get("/api/status", headers={"X-API-Key": "incorrect"}).status_code == 401


def test_login_fails_without_revealing_inputs(client: TestClient, settings: Settings) -> None:
    secret = "user-input-must-not-leak"
    response = client.post("/auth/login", json={"username": secret, "password": secret})
    assert response.status_code == 401
    assert secret not in response.text
    invalid = client.post("/auth/login", json={"username": "manager", "password": secret * 100})
    assert invalid.status_code == 422
    assert secret not in invalid.text
    with Database(settings.database_path).connect() as connection:
        audits = [dict(row) for row in connection.execute("SELECT * FROM audit_runs")]
    assert audits[0]["error"] == "invalid_credentials"
    assert secret not in json.dumps(audits)


def test_cross_origin_cannot_login_or_logout(client: TestClient) -> None:
    assert (
        client.post(
            "/auth/login",
            json={"username": "manager", "password": PASSWORD},
            headers={"Origin": "https://another.invalid"},
        ).status_code
        == 403
    )
    csrf = login(client)
    assert (
        client.post("/auth/logout", headers={"Origin": "null", "X-CSRF-Token": csrf}).status_code
        == 403
    )
    assert client.get("/api/status").status_code == 200


def test_session_expiration_and_restart(settings: Settings) -> None:
    with TestClient(
        create_app(settings, qdrant_probe=lambda: True), base_url="http://127.0.0.1:8000"
    ) as first:
        login(first)
        token = first.cookies.get(COOKIE_NAME)
    with TestClient(
        create_app(settings, qdrant_probe=lambda: True), base_url="http://127.0.0.1:8000"
    ) as second:
        second.cookies.set(COOKIE_NAME, token)
        assert second.get("/api/status").status_code == 200
        with Database(settings.database_path).connect() as connection:
            connection.execute("UPDATE auth_sessions SET expires_at = 0")
        assert second.get("/api/status").status_code == 401


def test_login_limit_applies_before_password_work(settings: Settings) -> None:
    limited = settings.model_copy(update={"login_attempts_per_minute": 2})
    with TestClient(
        create_app(limited, qdrant_probe=lambda: True), base_url="http://127.0.0.1:8000"
    ) as client:
        for _ in range(2):
            assert (
                client.post("/auth/login", json={"username": "bad", "password": "bad"}).status_code
                == 401
            )
        response = client.post("/auth/login", json={"username": "manager", "password": PASSWORD})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"


def test_production_cookie_is_secure(settings: Settings) -> None:
    from pydantic import AnyHttpUrl

    secure = settings.model_copy(
        update={"app_environment": "production", "app_base_url": AnyHttpUrl("https://demo.invalid")}
    )
    with TestClient(
        create_app(secure, qdrant_probe=lambda: True), base_url="https://demo.invalid"
    ) as client:
        response = client.post("/auth/login", json={"username": "manager", "password": PASSWORD})
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/api/status").status_code == 200


def test_session_creation_rolls_back_if_audit_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings, qdrant_probe=lambda: True)

    def failed_audit(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("audit-failed")

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        monkeypatch.setattr(app.state.runtime.database, "audit", failed_audit)
        response = client.post("/auth/login", json={"username": "manager", "password": PASSWORD})
        assert response.status_code == 503
        assert COOKIE_NAME not in client.cookies
        with Database(settings.database_path).connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0
