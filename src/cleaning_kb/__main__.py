"""Локальная настройка окружения без передачи паролей в аргументах процесса."""

import argparse
import getpass
import os
import secrets
from pathlib import Path

from cleaning_kb.credentials import hash_password


def initialize_env(path: Path, password: str) -> None:
    """Создать новый .env; существующий файл никогда не перезаписывается."""
    password_hash = hash_password(password)
    lines = [
        "APP_ENVIRONMENT=local",
        "APP_VERSION=0.1.0",
        "HOST_PORT=8001",
        "APP_BASE_URL=http://127.0.0.1:8001",
        "DATABASE_PATH=output/cleaning-kb.sqlite3",
        "ADMIN_USERNAME=manager",
        f"ADMIN_PASSWORD_HASH='{password_hash}'",
        f"API_KEY={secrets.token_urlsafe(32)}",
        "SESSION_TTL_SECONDS=28800",
        "LOGIN_ATTEMPTS_PER_MINUTE=10",
        "MAX_REQUEST_BYTES=1048576",
        "QDRANT_URL=http://127.0.0.1:6333",
        "DEPENDENCY_TIMEOUT_SECONDS=2",
        "PROXYAPI_KEY=",
        "GENERATION_MODEL=openai/gpt-4.1-mini",
        "EMBEDDING_MODEL=openai/text-embedding-3-small",
    ]
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(lines) + "\n")


def main() -> int:
    """Запросить пароль интерактивно и сохранить только его хеш."""
    parser = argparse.ArgumentParser(description="Настройка базы знаний")
    parser.add_argument(
        "command",
        choices=["init-env", "seed", "backup"],
        help="Настройка, загрузка JSONL или backup",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Новый файл настроек")
    parser.add_argument(
        "--output", type=Path, help="Новый файл резервной копии SQLite (только для backup)"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("tests_data/kb_documents.jsonl"),
        help="JSONL с полями title и text",
    )
    args = parser.parse_args()
    if args.command == "backup":
        if args.output is None:
            parser.error("Для backup требуется --output с новым именем файла")
        from cleaning_kb.backup import backup_database
        from cleaning_kb.config import Settings
        from cleaning_kb.database import Database

        try:
            backup_database(Database(Settings(_env_file=args.env_file).database_path), args.output)
        except Exception:
            print("Копия не создана; проверьте настройки, исходную БД и новое имя файла")
            return 1
        print("Согласованная копия SQLite создана; храните её с ограниченным доступом")
        return 0
    if args.command == "seed":
        from cleaning_kb.config import ConfigurationError, Settings
        from cleaning_kb.seed import load_documents

        try:
            load_documents(args.input, Settings(_env_file=args.env_file))
        except (ValueError, OSError, ConfigurationError):
            print("Загрузка не завершена; проверьте файл, настройки, доступ и состояние документов")
            return 1
        except Exception:
            print("Загрузка не завершена; проверьте доступность приложения и безопасный журнал")
            return 1
        return 0
    if args.env_file.exists():
        print("Файл уже существует; сохранён без изменений")
        return 1
    password = getpass.getpass("Пароль менеджера (от 12 символов): ")
    repeated = getpass.getpass("Повторите пароль: ")
    if password != repeated:
        print("Пароли не совпадают; файл не создан")
        return 1
    try:
        initialize_env(args.env_file, password)
    except (ValueError, OSError):
        print("Не удалось создать настройки; проверьте пароль, путь и права записи")
        return 1
    print("Настройки созданы. Логин: manager. Ключ API сохранён только в файле настроек")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
