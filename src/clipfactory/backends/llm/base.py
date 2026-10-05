from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class LLMProvider(Protocol):
    """Текстовая LLM. Возвращает сырой текст ответа; разбор JSON — у вызывающего."""

    name: str
    model: str

    def complete(self, *, system: str, prompt: str, max_tokens: int = 4096) -> str: ...


def parse_json_loose(raw: str) -> Any:
    """Снять markdown-ограждения и вытащить первый JSON-объект из ответа модели."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise
