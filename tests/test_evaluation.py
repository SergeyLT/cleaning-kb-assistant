"""Контроль качества проверяет больше, чем существование цитаты и флаг review."""

import subprocess
import sys
from pathlib import Path

from cleaning_kb.evaluation import assess, load_cases


def test_corpus_has_ten_baseline_and_separate_negative_fixtures() -> None:
    cases, rubric = load_cases(Path("tests_data"))
    baseline = [case for case in cases if case["id"].startswith("base")]
    assert len(baseline) == 10 and sum(case["expected_needs_review"] for case in baseline) == 3
    assert len(cases) == 18
    assert {case["fixture"] for case in cases if case.get("fixture")} == {"injection", "conflict"}
    assert set(rubric["fixtures"]) == {"injection", "conflict"}


def test_genuine_quote_does_not_hide_wrong_content_or_review() -> None:
    case = {
        "id": "test",
        "question": "Духовка?",
        "expected_needs_review": False,
        "required_groups": ["отдельн"],
        "forbidden_patterns": ["КОД_ПОДМЕНЫ"],
    }
    documents = {"doc": {"text": "Духовка — отдельная опция."}}
    result = {
        "answer": "Духовка входит в базовую уборку.",
        "needs_review": False,
        "sources": [
            {"document_id": "doc", "title": "Правила", "quote": "Духовка — отдельная опция."}
        ],
    }
    assessed = assess(case, result, documents, {"Правила"})
    assert assessed["checks"]["quotes_authentic"] and not assessed["automatic_pass"]
    result["answer"] = "Духовка — отдельная опция."
    assert assess(case, result, documents)["automatic_pass"]
    result["sources"][0]["quote"] = "Выдуманная цитата"
    assert not assess(case, result, documents)["automatic_pass"]


def test_live_cli_requires_explicit_opt_in_without_loading_secrets() -> None:
    completed = subprocess.run([sys.executable, "scripts/evaluate.py"], capture_output=True)
    assert completed.returncode == 2


def test_preparation_rubric_requires_complete_general_rules() -> None:
    cases, _ = load_cases(Path("tests_data"))
    case = next(case for case in cases if case["id"] == "base05")
    text = (
        "Обеспечить безопасный вход и доступ, воду и электропитание. "
        "Освободить проходы и поверхности. Убрать ценные и хрупкие предметы; "
        "сообщить ограничения материалов. Заранее согласовать ключ или пропуск. "
        "Организовать людей и животных так, чтобы не мешать безопасной работе."
    )
    documents = {"service": {"text": text}}
    answer = {
        "answer": text,
        "needs_review": False,
        "sources": [{"document_id": "service", "title": "Порядок обслуживания", "quote": text}],
    }
    assert assess(case, answer, documents)["automatic_pass"]
    answer["answer"] = "Обеспечить доступ, воду и электричество."
    assert not assess(case, answer, documents)["checks"]["required_groups_present"]
    answer["answer"] = text + " При уборке после ремонта убрать строительный мусор."
    assert not assess(case, answer, documents)["checks"]["forbidden_patterns_absent"]
