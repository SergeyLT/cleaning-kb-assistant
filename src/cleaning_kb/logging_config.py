"""JSON-логи с разрешёнными полями и безопасной диагностикой исключений."""

import json
import logging
import re
import sys
import traceback
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
EVENTS = {
    "application_started": "Приложение запущено",
    "application_stopped": "Приложение остановлено",
    "startup_failed": "Приложение не смогло запуститься",
    "request_completed": "HTTP-запрос завершён",
    "request_failed": "HTTP-запрос не выполнен",
    "request_rejected": "HTTP-запрос отклонён",
    "authentication_succeeded": "Вход выполнен",
    "authentication_failed": "Вход отклонён",
    "session_revoked": "Сессия завершена",
    "dependency_unavailable": "Зависимость недоступна",
    "provider_call_started": "Начат вызов модели",
    "provider_call_completed": "Вызов модели завершён",
    "provider_call_failed": "Вызов модели не выполнен",
    "rag_operation_completed": "Операция базы знаний завершена",
    "rag_operation_failed": "Операция базы знаний не выполнена",
}
FIELDS = {
    "model",
    "usage_available",
    "input_tokens",
    "output_tokens",
    "operation_id",
    "route",
    "method",
    "status",
    "duration_ms",
    "component",
    "operation",
    "stage",
    "error_category",
    "error_code",
    "error_type",
    "retryable",
    "stack_frames",
}
SENSITIVE = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-[A-Za-z0-9_-]+|"
    r"(?:password|api[_-]?key|token|secret)\s*[:=]\s*\S+)"
)


def redact(value: object) -> object:
    """Дополнительно маскировать известные форматы секретов в разрешённых полях."""
    if isinstance(value, str):
        return SENSITIVE.sub("[скрыто]", value)[:1000]
    if isinstance(value, list):
        return [redact(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key): redact(item) for key, item in value.items()}
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return "[неподдерживаемое значение]"


def safe_frames(error: BaseException) -> list[dict[str, object]]:
    """Сохранить место сбоя без сообщения исключения и локальных переменных."""
    return [
        {"module": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for frame in traceback.extract_tb(error.__traceback__)[-12:]
    ]


class JsonFormatter(logging.Formatter):
    """Произвольное сообщение и extra-поля не попадают в выходной JSON."""

    def __init__(self, environment: str, version: str) -> None:
        super().__init__()
        self.environment, self.version = environment, version

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", "library_event")
        if event not in EVENTS:
            event = "library_event"
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": "cleaning-kb",
            "environment": self.environment,
            "version": self.version,
            "logger": record.name,
            "event": event,
            "message": EVENTS.get(event, "Событие стороннего компонента"),
            "request_id": REQUEST_ID.get(),
        }
        fields = getattr(record, "safe_fields", {})
        if isinstance(fields, dict):
            entry.update({key: redact(value) for key, value in fields.items() if key in FIELDS})
        return json.dumps(entry, ensure_ascii=False, allow_nan=False)


class JsonHandler(logging.Handler):
    """Писать в текущий stdout; ошибка доставки не должна ломать операцию."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError, TypeError):
            pass


def configure_logging(environment: str, version: str) -> None:
    """Настроить один обработчик приложения без повторного добавления."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, JsonHandler):
            root.removeHandler(handler)
            handler.close()
    handler = JsonHandler()
    handler.setFormatter(JsonFormatter(environment, version))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    fields: Mapping[str, object] | None = None,
) -> None:
    """Записать событие из каталога со строго ограниченными метаданными."""
    if event not in EVENTS:
        raise ValueError("Событие отсутствует в каталоге логирования")
    logging.getLogger("cleaning_kb").log(
        level, event, extra={"event": event, "safe_fields": dict(fields or {})}
    )
