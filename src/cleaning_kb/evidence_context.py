"""Целостный раздел вокруг найденного фрагмента без изменения векторного индекса."""

import re

HEADINGS = re.compile(r"^#{1,6}[ \t]+\S.*$", re.MULTILINE)
MAX_QUOTE_CHARS = 2000


def section_quote(document: str, snippet: str) -> str:
    """Расширить уникальную выдержку до границ разделов в пределах 2000 символов.

    Идентификатор фрагмента остаётся якорем поиска, а цитата — точной подстрокой
    того же документа. Для неоднозначного совпадения или длинного раздела
    сохраняем исходную выдержку, не обрезая требования произвольной границей.
    """
    start = document.find(snippet)
    if start < 0 or document.find(snippet, start + 1) >= 0:
        return snippet
    end = start + len(snippet)
    headings = [match.start() for match in HEADINGS.finditer(document)]
    before = [offset for offset in headings if offset <= start]
    if not before:
        return snippet
    section_start = before[-1]
    section_end = next((offset for offset in headings if offset >= end), len(document))
    quote = document[section_start:section_end].strip()
    return quote if len(quote) <= MAX_QUOTE_CHARS else snippet
