"""Текущие названия источников поверх неизменяемых снимков ответов."""

from typing import Any

from cleaning_kb.database import Database


def source_names_view(value: Any, database: Database) -> Any:
    """Дополнить свежеразобранный JSON метаданными без записи в БД."""
    with database.connect() as connection:
        titles = dict(connection.execute("SELECT id, title FROM documents"))

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            if {"document_id", "snippet_id", "quote", "title"} <= item.keys():
                item["current_title"] = titles.get(item["document_id"], item["title"])
            for child in item.values():
                visit(child)
            if item.get("kind") == "summary" and isinstance(item.get("result"), dict):
                result = item["result"]
                # Сохранённый text/answer не меняем: отдельный текст только для представления.
                lines = ["Со слов пользователя:"]
                lines += [f["text"] for f in result.get("client_facts", [])]
                lines += ["По сохранённым источникам:"]
                for finding in result.get("findings", []):
                    names = dict.fromkeys(
                        s.get("current_title") or s["title"] for s in finding["sources"]
                    )
                    lines.append(finding["text"] + " [" + "; ".join(names) + "]")
                lines += ["Ограничения:"] + result.get("limitations", [])
                lines += ["Проверка сотрудником:"]
                for review in result.get("reviews", []):
                    status = (
                        (
                            "проверка отмечена выполненной ("
                            + review["reviewed_at"]
                            + "); результат не записан"
                        )
                        if review.get("reviewed_at")
                        else "ожидает проверки"
                    )
                    lines.append(review["question"] + " — " + status)
                if result.get("verification_notice"):
                    lines.append(result["verification_notice"])
                item["display_text"] = "\n".join(lines)

    visit(value)
    return value
