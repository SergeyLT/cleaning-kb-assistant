"""Явный контроль качества на отдельном стенде; обычные pytest его не запускают."""

import argparse
from pathlib import Path

import httpx

from cleaning_kb.config import Settings
from cleaning_kb.evaluation import run_cases


def main() -> int:
    """Секреты читаются из файла/окружения, не аргументов и не чата."""
    parser = argparse.ArgumentParser(description="Контроль качества RAG с платными вызовами")
    parser.add_argument("--live", action="store_true", help="Явно разрешить платные вызовы")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--cases", nargs="+", help="Выбранные id; без параметра весь компактный набор"
    )
    parser.add_argument("--data", type=Path, default=Path("tests_data"))
    parser.add_argument("--output", type=Path, default=Path("output/quality-evaluation.json"))
    args = parser.parse_args()
    if not args.live:
        print(
            "Проверка вызывает платный API и добавляет негативные документы. "
            "Используйте отдельный стенд и явно укажите --live"
        )
        return 2
    try:
        report = run_cases(
            Settings(_env_file=args.env_file), args.data, args.output, case_ids=args.cases
        )
    except (ValueError, OSError, httpx.HTTPError):
        print(
            "Прогон не завершён; проверьте настройки, доступ и сохранённый отчёт. "
            "Секреты не выводятся"
        )
        return 1
    passed = sum(case["automatic_pass"] for case in report["cases"])
    print(
        f"Автоматические проверки: {passed}/{len(report['cases'])}. "
        "Обязателен содержательный разбор ответов человеком"
    )
    return 0 if report["automatic_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
