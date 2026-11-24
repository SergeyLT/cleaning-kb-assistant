"""Воспроизводимые проверки ответов; не заменяют содержательный разбор человеком."""

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from cleaning_kb.config import Settings
from cleaning_kb.database import utc_now


def load_cases(directory: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Базовые вопросы остаются авторитетным JSONL; негативные материалы отдельно."""
    rubric = json.loads((directory / "quality_cases.json").read_text(encoding="utf-8"))
    baseline = [
        json.loads(line)
        for line in (directory / "kb_questions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = []
    for index, case in enumerate(baseline, 1):
        case_id = f"base{index:02}"
        cases.append({**case, "id": case_id, **rubric["baseline_checks"].get(case_id, {})})
    return cases + rubric["extra_cases"], rubric


def assess(
    case: dict[str, Any],
    answer: dict[str, Any],
    documents: dict[str, dict[str, Any]],
    source_titles: set[str] | None = None,
) -> dict[str, Any]:
    """Проверить метки, происхождение цитат и явные контрольные смысловые группы."""
    text = answer.get("answer", "")
    sources = answer.get("sources", [])
    quotes_ok = all(
        source.get("document_id") in documents
        and bool(source.get("quote"))
        and source["quote"] in documents[source["document_id"]]["text"]
        for source in sources
    )
    checks = {
        "review_matches": answer.get("needs_review") is case["expected_needs_review"],
        "quotes_authentic": quotes_ok,
        "grounded_answer_has_sources": bool(sources) or bool(answer.get("needs_review")),
        "suitable_source_present": not source_titles
        or any(source.get("title") in source_titles for source in sources),
        "required_groups_present": all(
            re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            for pattern in case.get("required_groups", [])
        ),
        "forbidden_patterns_absent": not any(
            re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            for pattern in case.get("forbidden_patterns", [])
        ),
    }
    return {
        "id": case["id"],
        "question": case["question"],
        "expected_needs_review": case["expected_needs_review"],
        "checks": checks,
        "automatic_pass": all(checks.values()),
        "result": answer,
        "manual_review": "pending",
    }


def run_cases(
    settings: Settings,
    directory: Path,
    output: Path,
    *,
    case_ids: list[str] | None = None,
    max_questions: int = 20,
) -> dict[str, Any]:
    """Явно запущенный платный прогон API, без скрытых повторов запроса."""
    cases, rubric = load_cases(directory)
    if case_ids:
        unknown = set(case_ids) - {case["id"] for case in cases}
        if unknown:
            raise ValueError("Неизвестный идентификатор контрольного вопроса")
        cases = [case for case in cases if case["id"] in case_ids]
    if not 1 <= len(cases) <= max_questions <= 30:
        raise ValueError("Нарушено ограничение числа контрольных вопросов")
    canonical = [
        json.loads(line)
        for line in (directory / "kb_documents.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    names = [
        "01-apartment-cleaning.md",
        "02-post-renovation.md",
        "03-office-cleaning.md",
        "04-extras-and-limits.md",
        "05-service-process.md",
    ]
    title_by_file = {name: doc["title"] for name, doc in zip(names, canonical, strict=True)}
    report: dict[str, Any] = {
        "started_at": utc_now(),
        "status": "running",
        "cases": [],
        "manual_review_required": rubric["manual_review"],
    }
    headers = {"X-API-Key": settings.api_key.get_secret_value()}
    with httpx.Client(
        base_url=settings.origin,
        headers=headers,
        timeout=120,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        status = client.get("/api/status")
        status.raise_for_status()
        report["server"] = status.json()
        for case in cases:
            fixture_id = None
            try:
                if case.get("fixture"):
                    fixture = {**rubric["fixtures"][case["fixture"]]}
                    fixture["title"] += " [контроль " + uuid4().hex[:8] + "]"
                    response = client.post("/kb/documents", json=fixture)
                    response.raise_for_status()
                    doc = response.json()
                    fixture_id = doc["id"]
                    if not doc["is_active"] or doc["indexing_status"] != "ready":
                        raise ValueError("Негативная фикстура не готова; нужен изолированный стенд")
                response = client.post("/kb/ask", json={"question": case["question"]})
                if response.status_code != 200:
                    report["cases"].append(
                        {
                            "id": case["id"],
                            "automatic_pass": False,
                            "http_status": response.status_code,
                            "error": response.json().get("error", {}).get("code"),
                        }
                    )
                    continue
                answer = response.json()
                documents = {}
                for source in answer["sources"]:
                    doc_response = client.get("/kb/documents/" + source["document_id"])
                    doc_response.raise_for_status()
                    documents[source["document_id"]] = doc_response.json()
                expected = {title_by_file[name] for name in case.get("expected_source_files", [])}
                assessed = assess(case, answer, documents, expected)
                operation = client.get("/qa/" + answer["operation_id"])
                operation.raise_for_status()
                assessed["usage"] = operation.json().get("usage")
                report["cases"].append(assessed)
            finally:
                if fixture_id:
                    deactivated = client.post(f"/kb/documents/{fixture_id}/deactivate")
                    deactivated.raise_for_status()
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
    report["finished_at"] = utc_now()
    report["automatic_pass"] = all(case["automatic_pass"] for case in report["cases"])
    report["status"] = (
        "automatic_checks_passed" if report["automatic_pass"] else "automatic_checks_failed"
    )
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
