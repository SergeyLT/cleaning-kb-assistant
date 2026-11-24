"""Доступ к HTML, ресурсам панели и защищённым результатам/аудиту."""

from conftest import API_KEY, PASSWORD
from fastapi.testclient import TestClient


def test_login_and_authenticated_shell_and_static_resources(client: TestClient) -> None:
    anonymous = client.get("/")
    assert anonymous.status_code == 200 and 'id="login-form"' in anonymous.text
    assert "frame-ancestors 'none'" in anonymous.headers["content-security-policy"]
    assert 'name="viewport"' in anonymous.text and 'lang="ru"' in anonymous.text
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    client.post("/auth/login", json={"username": "manager", "password": PASSWORD})
    signed = client.get("/")
    assert 'id="view"' in signed.text and 'id="login-form"' not in signed.text
    assert client.get("/login", follow_redirects=False).status_code in (302, 307)


def test_audit_and_standalone_result_are_protected_and_do_not_expose_secrets(
    client: TestClient,
) -> None:
    for url in ("/audit", "/qa/missing"):
        assert client.get(url).status_code == 401
    headers = {"X-API-Key": API_KEY}
    result = client.post("/kb/ask", headers=headers, json={"question": "Вымышленный вопрос"}).json()
    opened = client.get("/qa/" + result["operation_id"], headers=headers)
    assert opened.status_code == 200 and opened.json()["result"]["needs_review"] is True
    records = client.get("/audit", headers=headers).json()
    assert records and all("input" not in row and "output" not in row for row in records)
    assert API_KEY not in str(records) and PASSWORD not in str(records)
    assert client.get("/qa/missing", headers=headers).status_code == 404
    assert client.get("/audit?limit=101", headers=headers).status_code == 422
