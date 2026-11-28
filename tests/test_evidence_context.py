"""Целостность цитат и границы контекста без привязки к формулировке вопроса."""

import pytest

from cleaning_kb.evidence_context import section_quote


def test_split_section_retains_heading_and_all_requirements() -> None:
    text = (
        "# Правила\n\n## Подготовка\nОсвободить проходы.\nУбрать хрупкие вещи.\n"
        "\n## Время\nУточнить время."
    )
    first = section_quote(text, "Освободить проходы.")
    second = section_quote(text, "Убрать хрупкие вещи.")
    assert first == second
    assert first.startswith("## Подготовка")
    assert "Освободить проходы." in first and "Убрать хрупкие вещи." in first
    assert "Уточнить время." not in first and first in text


@pytest.mark.parametrize(
    "text,snippet",
    [
        ("Без заголовков. Часть текста.", "Часть текста."),
        ("# Один\nПовтор.\n# Два\nПовтор.", "Повтор."),
        ("# Длинный\n" + "Условие. " * 400, "Условие. " * 100),
        ("# Правила\nИзвестно.", "Отсутствует."),
    ],
)
def test_ambiguous_or_unbounded_context_keeps_original(text: str, snippet: str) -> None:
    assert section_quote(text, snippet) == snippet
