"""Контракты RAG и строгого ответа модели."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """Не принимать лишние поля и не включать вход в ошибки."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class DocumentInput(StrictModel):
    """Текстовый документ без загрузки файлов."""

    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100_000)

    @field_validator("title", "text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        """Сохранить содержимое, исключив пустые документы."""
        if not value.strip() or "\x00" in value:
            raise ValueError("Поле не должно быть пустым или содержать нулевой символ")
        return value.strip()


class DocumentCreated(StrictModel):
    """Успешное сохранение не означает завершённую индексацию."""

    status: Literal["ok"]
    document_id: str
    id: str
    created_at: str
    title: str
    text: str
    content_hash: str
    indexing_status: Literal["pending", "indexing", "ready", "failed"]
    is_active: bool
    error: str | None


class QuestionInput(StrictModel):
    """Общие поля запроса ответа."""

    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Нужен непустой вопрос")
        return value.strip()


class ContextInput(QuestionInput):
    """Контекст вызывающего без присвоения ему идентификатора документа БД."""

    context: str = Field(max_length=24_000)


class AskInput(QuestionInput):
    """Старый запрос с одним question остаётся совместимым."""

    conversation_id: str | None = Field(default=None, min_length=1, max_length=80)


class ConversationInput(StrictModel):
    """Название необязательно: заменится первым вопросом."""

    title: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("Нужно непустое название")
        return value.strip() if value is not None else None


class RenameInput(ConversationInput):
    """Явное новое название."""

    title: str = Field(min_length=1, max_length=200)


class Rewrite(StrictModel):
    """Неоднозначное продолжение не должно порождать выдуманный поиск."""

    search_question: str = Field(min_length=1, max_length=2000)
    ambiguous: bool


class ClientFact(StrictModel):
    """Факт со слов пользователя, с привязкой к сообщениям."""

    text: str = Field(min_length=1, max_length=1000)
    turn_ids: list[str] = Field(min_length=1, max_length=20)


class Finding(StrictModel):
    """Вывод итога со ссылками на сохранённые источники."""

    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class ModelSummary(StrictModel):
    """Состояния проверки сотрудником не доверяются модели."""

    client_facts: list[ClientFact] = Field(max_length=20)
    findings: list[Finding] = Field(max_length=20)
    limitations: list[str] = Field(max_length=20)


class ModelAnswer(StrictModel):
    """Модель выбирает только подготовленные номера выдержек."""

    answer: str = Field(min_length=1, max_length=6000)
    evidence_ids: list[str] = Field(max_length=12)
    confidence: float = Field(ge=0, le=1)
    coverage: Literal["full", "partial", "none", "conflict"]


class Source(StrictModel):
    """Проверенная цитата и снимок её происхождения."""

    document_id: str | None
    title: str
    snippet_id: str
    quote: str


class Answer(StrictModel):
    """Результат после серверных проверок."""

    operation_id: str
    answer: str
    sources: list[Source]
    confidence: Literal["high", "medium", "low"]
    confidence_score: float | None = Field(default=None, ge=0, le=1, exclude=True, repr=False)
    needs_review: bool
    review_reasons: list[str]


def confidence_label(score: float, threshold: float) -> Literal["high", "medium", "low"]:
    """Низкая категория совпадает с действующим порогом ручной проверки."""
    if score < threshold:
        return "low"
    return "high" if score >= max(0.9, threshold) else "medium"


def legacy_answer_view(result: dict[str, Any]) -> dict[str, Any]:
    """Показать старый числовой снимок новым контрактом, не переписывая БД."""
    score = result.get("confidence")
    if not isinstance(score, (float, int)) or isinstance(score, bool):
        return result
    # Старый настраиваемый порог не сохранялся. Опираться на записанное решение,
    # а не пересчитывать его текущей конфигурацией: причины/отметки неизменны.
    if "low_confidence" in result.get("review_reasons", []) or not result.get("sources"):
        label = "low"
    else:
        label = "high" if score >= 0.9 else "medium"
    return {**result, "confidence": label}


class Evidence(StrictModel):
    """Одна доступная модели выдержка."""

    evidence_id: str
    source: Source
    retrieval_score: float | None = None


def safe_answer(operation_id: str, reason: str) -> Answer:
    """Не показывать непроверенный ответ или выдуманные источники."""
    return Answer(
        operation_id=operation_id,
        answer=(
            "В доступных материалах недостаточно подтверждённых данных. Нужно уточнение менеджера."
        ),
        sources=[],
        confidence="low",
        needs_review=True,
        review_reasons=[reason],
    )
