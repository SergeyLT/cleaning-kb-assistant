"""Счётчики, настройки, аудит и атомарность без внешних запросов."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from conftest import API_KEY, PASSWORD

from cleaning_kb.database import Database
from cleaning_kb.errors import AppError
from cleaning_kb.provider_budget import ProviderBudget, ProviderLimits

HEADERS = {"X-API-Key": API_KEY}


def test_reservation_waiting_on_reset_is_counted_in_new_period(client, settings):
    budget = ProviderBudget(settings, Database(settings.database_path))
    earlier = datetime.now(UTC)
    budget.reset("api_key", "service-api", "reset")
    budget.reserve("generation", "model", 100, "waiting", earlier)
    assert budget.status()["usage"]["calls_per_day"] == 1
    assert budget.status()["usage"]["token_budget_per_day"] == 100


LIMITS = {"calls_per_minute": 20, "calls_per_day": 200, "token_budget_per_day": 500000}


def test_settings_access_validation_and_persistence(client, settings):
    assert client.get("/settings/provider").status_code == 401
    assert client.post("/settings/provider/reset", json={"confirm": True}).status_code == 401
    assert client.get("/settings/provider", headers=HEADERS).json()["limits"] == LIMITS
    for bad in [
        {"calls_per_day": 0},
        {"calls_per_minute": True},
        {"calls_per_day": "12"},
        {"token_budget_per_day": 999},
        {"actor_id": "forged"},
    ]:
        assert (
            client.put("/settings/provider", headers=HEADERS, json=LIMITS | bad).status_code == 422
        )
    assert (
        client.post(
            "/settings/provider/reset", headers=HEADERS, json={"confirm": False}
        ).status_code
        == 422
    )
    saved = LIMITS | {"calls_per_day": 300, "token_budget_per_day": 1500000}
    assert client.put("/settings/provider", headers=HEADERS, json=saved).json()["limits"] == saved
    database = Database(settings.database_path)
    database.initialize()
    settings.provider_calls_per_day = 1
    assert ProviderBudget(settings, database).status()["limits"] == saved
    assert settings.provider_calls_per_day == 1  # ENV-объект не изменялся.


def test_reset_preserves_history_actual_usage_minute_and_actor(client, settings):
    database = Database(settings.database_path)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO provider_calls VALUES ('previous', ?, 'op', 'generation', 'model', "
            "900, 'completed', ?, NULL)",
            (datetime.now(UTC).isoformat(), json.dumps({"total_tokens": 12})),
        )
    client.post("/auth/login", json={"username": "manager", "password": PASSWORD})
    session = client.get("/auth/session").json()
    assert client.post("/settings/provider/reset", json={"confirm": True}).status_code == 403
    response = client.post(
        "/settings/provider/reset",
        json={"confirm": True},
        headers={"X-CSRF-Token": session["csrf_token"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["usage"] == {"calls_per_minute": 1, "calls_per_day": 0, "token_budget_per_day": 0}
    assert data["actual_usage_today"]["total_tokens"] == 12
    assert (
        data["last_reset"]["actor_type"] == "user" and data["last_reset"]["actor_id"] == "manager"
    )
    records = client.get("/audit").json()
    reset = next(row for row in records if row["action"] == "provider_budget_reset")
    assert reset["actor_id"] == "manager"
    assert reset["details"]["previous_usage"]["token_budget_per_day"] == 900
    assert API_KEY not in json.dumps(records) and PASSWORD not in json.dumps(records)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0] == 1
    # Ручной сброс не является обходом минутного ограничения.
    budget = ProviderBudget(settings, database)
    budget.save(
        ProviderLimits(**(LIMITS | {"calls_per_minute": 1})), "api_key", "service-api", "req"
    )
    with pytest.raises(AppError) as error:
        budget.reserve("generation", "model", 10, "blocked", datetime.now(UTC))
    assert error.value.code == "provider_budget_exceeded"
    with database.connect() as connection:
        tomorrow = budget._counts(connection, datetime.now(UTC) + timedelta(days=1))
    assert tomorrow["calls_per_day"] == tomorrow["token_budget_per_day"] == 0


def test_active_call_prevents_reset(client, settings):
    with Database(settings.database_path).connect() as connection:
        connection.execute(
            "INSERT INTO provider_calls VALUES ('active', ?, 'op', 'generation', 'model', "
            "100, 'started', NULL, NULL)",
            (datetime.now(UTC).isoformat(),),
        )
    assert (
        client.post("/settings/provider/reset", headers=HEADERS, json={"confirm": True}).status_code
        == 409
    )
    assert client.get("/settings/provider", headers=HEADERS).json()["last_reset"] is None


@pytest.mark.parametrize(
    "key,used,requested",
    [
        ("calls_per_minute", 1, 10),
        ("calls_per_day", 1, 10),
        ("token_budget_per_day", 900, 200),
    ],
)
def test_rejection_logs_exact_threshold_without_reserving(
    client, settings, caplog, key, used, requested
):
    database = Database(settings.database_path)
    budget = ProviderBudget(settings, database)
    value = 1000 if key == "token_budget_per_day" else 1
    budget.save(ProviderLimits(**(LIMITS | {key: value})), "api_key", "service-api", "req")
    now = datetime.now(UTC)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO provider_calls VALUES ('previous', ?, 'op', 'generation', 'model', "
            "900, 'completed', '{}', NULL)",
            (now.isoformat(),),
        )
    with caplog.at_level(logging.WARNING), pytest.raises(AppError):
        budget.reserve("generation", "model", requested, "denied", now)
    record = next(
        r for r in caplog.records if getattr(r, "event", "") == "provider_budget_rejected"
    )
    assert record.safe_fields["limit_kind"] == key
    assert record.safe_fields["used"] == used
    assert record.safe_fields["requested"] == (requested if key == "token_budget_per_day" else 1)
    assert record.safe_fields["limit"] == value
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0] == 1


def test_saved_limits_reservation_is_atomic(client, settings):
    budget = ProviderBudget(settings, Database(settings.database_path))
    budget.save(ProviderLimits(**(LIMITS | {"calls_per_day": 2})), "api_key", "service-api", "req")

    def reserve(index):
        try:
            budget.reserve("generation", "model", 100, str(index), datetime.now(UTC))
            return True
        except AppError as error:
            assert error.code == "provider_budget_exceeded"
            return False

    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(reserve, range(8))) == 2
