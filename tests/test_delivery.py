"""Deploy не повторяет webhook и не принимает healthy старой версии за новую."""

import runpy
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cleaning_kb.backup import backup_database
from cleaning_kb.database import Database

delivery = runpy.run_path("scripts/deploy.py")
deploy = delivery["deploy"]
DeploymentError = delivery["DeploymentError"]
WEBHOOK = "https://deploy.example.com/api/v1/deploy?uuid=fictional-resource&force=false"
ORIGIN = "https://app.example.com"
SHA = "a" * 40
ACCEPTED = {"deployments": [{"resource_uuid": "fictional-resource", "deployment_uuid": "run"}]}


def test_deploy_waits_for_expected_version_and_never_sends_token_to_health() -> None:
    calls = []
    answers = [ACCEPTED, {"status": "ok", "version": "old"}, {"status": "ok", "version": SHA}]

    def request(url, method, token):
        calls.append((url, method, token))
        return answers.pop(0)

    deploy(WEBHOOK, "fictional-token", ORIGIN, SHA, request=request, sleep=lambda _: None)
    assert calls == [
        (WEBHOOK, "POST", "fictional-token"),
        (ORIGIN + "/health", "GET", None),
        (ORIGIN + "/health", "GET", None),
    ]


def test_deploy_timeout_is_bounded_without_webhook_retry() -> None:
    now = [0.0]
    calls = []

    def request(url, method, token):
        calls.append(method)
        if method == "POST":
            return ACCEPTED
        raise DeploymentError("Временная недоступность")

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(DeploymentError):
        deploy(
            WEBHOOK,
            "fictional-token",
            ORIGIN,
            SHA,
            request=request,
            sleep=sleep,
            clock=lambda: now[0],
            timeout=25,
        )
    assert now[0] == 25 and calls == ["POST", "GET", "GET", "GET"]


def test_uncertain_webhook_failure_is_never_retried() -> None:
    calls = []

    def request(url, method, token):
        calls.append(method)
        raise DeploymentError("Ответ webhook потерян")

    with pytest.raises(DeploymentError):
        deploy(WEBHOOK, "fictional-token", ORIGIN, SHA, request=request)
    assert calls == ["POST"]


@pytest.mark.parametrize("status", [302, 503])
def test_transport_rejects_redirect_and_hides_external_body(status) -> None:
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.send_response(status)
            self.send_header("Location", "/capture")
            self.end_headers()
            self.wfile.write(b"fictional-private-error")

        def do_GET(self):
            paths.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/webhook"
        with pytest.raises(DeploymentError) as error:
            delivery["request_json"](url, "POST", "fictional-token")
        assert paths == ["/webhook"]
        assert "fictional-private-error" not in str(error.value)
        assert url not in str(error.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("response", [{}, {"deployments": []}, {"deployments": [{}]}])
def test_deploy_rejects_unconfirmed_acceptance(response) -> None:
    calls = []

    def request(url, method, token):
        calls.append(method)
        return response

    with pytest.raises(DeploymentError):
        deploy(WEBHOOK, "fictional-token", ORIGIN, SHA, request=request)
    assert calls == ["POST"]


@pytest.mark.parametrize(
    "webhook,origin,sha",
    [
        (WEBHOOK.replace("https:", "http:"), ORIGIN, SHA),
        (WEBHOOK.replace("uuid=fictional-resource", "tag=all"), ORIGIN, SHA),
        (WEBHOOK, "http://app.example.com", SHA),
        (WEBHOOK, ORIGIN + "/nested", SHA),
        (WEBHOOK, ORIGIN, "latest"),
    ],
)
def test_invalid_deploy_configuration_does_not_send_requests(webhook, origin, sha) -> None:
    def unexpected(*args):
        raise AssertionError("Запрос при неверной конфигурации")

    with pytest.raises(DeploymentError):
        deploy(webhook, "fictional-token", origin, sha, request=unexpected)


def test_sqlite_backup_contains_committed_wal_and_is_restorable(tmp_path: Path) -> None:
    database = Database(tmp_path / "source.sqlite3")
    database.initialize()
    # Держим соединение открытым, чтобы запись осталась в WAL при копировании.
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO conversations(id,title,created_at,updated_at) "
            "VALUES ('demo','Обращение','now','now')"
        )
        connection.commit()
        target = tmp_path / "backups/snapshot.sqlite3"
        backup_database(database, target)
    restored = Database(target)
    restored.initialize()
    with restored.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT title FROM conversations").fetchone()[0] == "Обращение"
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3


def test_backup_never_overwrites_existing_file_and_removes_failed_copy(tmp_path: Path) -> None:
    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"original")
    missing = Database(tmp_path / "missing.sqlite3")
    with pytest.raises(FileExistsError):
        backup_database(missing, existing)
    assert existing.read_bytes() == b"original"
    failed = tmp_path / "failed.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        backup_database(missing, failed)
    assert not failed.exists() and not missing.path.exists()
