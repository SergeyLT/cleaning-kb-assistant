"""Один webhook Coolify и ограниченное ожидание конкретного SHA без раскрытия секретов."""

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit


class DeploymentError(RuntimeError):
    """Безопасное сообщение вместо URL или тела внешней ошибки."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Не пересылать авторизацию и не принять чужой health через редирект."""

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def request_json(url: str, method: str, token: str | None) -> dict[str, Any]:
    """Ограничить ожидание и размер; не печатать запрос, ответ или исключение."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            content = response.read(1_000_001)
            if len(content) > 1_000_000:
                raise ValueError
            payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (OSError, ValueError, urllib.error.URLError):
        raise DeploymentError("Сервис не вернул ожидаемый JSON; адрес и тело скрыты") from None


def deploy(
    webhook: str,
    token: str,
    public_url: str,
    expected_sha: str,
    *,
    request: Callable[[str, str, str | None], dict[str, Any]] = request_json,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = 600,
) -> None:
    """Принятие webhook не является успехом; старый healthy SHA тоже не подходит."""
    target = urlsplit(webhook)
    origin = urlsplit(public_url)
    query = parse_qs(target.query)
    resource = query.get("uuid", [])
    if (
        target.scheme != "https"
        or not target.hostname
        or target.username
        or target.password
        or target.fragment
        or target.path != "/api/v1/deploy"
        or len(resource) != 1
        or not re.fullmatch(r"[A-Za-z0-9_-]+", resource[0])
        or set(query) - {"uuid", "force"}
        or query.get("force", ["false"]) != ["false"]
        or origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
        or not re.fullmatch(r"[0-9a-f]{40}", expected_sha)
        or not token
        or any(character.isspace() for character in token)
    ):
        raise DeploymentError("Проверьте HTTPS, webhook одного ресурса, токен и полный SHA")
    # Запрос выполняется ровно один раз: при неопределённом результате нужен разбор в Coolify.
    accepted = request(webhook, "POST", token)
    deployments = accepted.get("deployments")
    if (
        not isinstance(deployments, list)
        or len(deployments) != 1
        or not isinstance(deployments[0], dict)
        or deployments[0].get("resource_uuid") != resource[0]
        or not deployments[0].get("deployment_uuid")
    ):
        raise DeploymentError(
            "Coolify не подтвердил единственный целевой deploy; повтор не запущен"
        )
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            health = request(public_url.rstrip("/") + "/health", "GET", None)
            if health.get("status") == "ok" and health.get("version") == expected_sha:
                print("Deploy подтверждён: health успешен и SHA совпадает")
                return
        except DeploymentError:
            pass
        sleep(min(10, max(0, deadline - clock())))
    raise DeploymentError(
        "Новая версия не стала healthy вовремя; проверьте Coolify, повтор выключен"
    )


def main() -> int:
    """Все параметры — через окружение, секреты не попадают в командную строку."""
    try:
        deploy(
            os.environ.get("COOLIFY_DEPLOY_WEBHOOK", ""),
            os.environ.get("COOLIFY_TOKEN", ""),
            os.environ.get("APP_PUBLIC_URL", ""),
            os.environ.get("EXPECTED_SHA", ""),
        )
    except (DeploymentError, ValueError):
        print("Deploy не подтверждён; проверьте настройки и состояние ресурса в Coolify")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
