"""Универсальный бэкенд для любого OpenAI-совместимого ``/chat/completions``.

Один код для OpenAI, DeepSeek, Gemini (OpenAI-совместимый режим), Groq, Together,
OpenRouter, vLLM, LM Studio и т.п. — меняются только base_url, ключ и модель.
Ключ берётся из env, в сообщения об ошибках не попадает.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

from clipfactory.backends.llm.base import LLMError
from clipfactory.log import redact_text

# Популярные эндпоинты (для справки в docs/runbook.md; код их не хардкодит)
PRESETS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
}


class OpenAICompatLLM:
    name = "openai_compat"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 600,
        temperature: float | None = 0.2,
        json_mode: bool = True,
        token_param: str = "max_tokens",
    ) -> None:
        if token_param not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("token_param must be max_tokens or max_completion_tokens")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.json_mode = json_mode
        self.token_param = token_param

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc

    def _body(self, system: str, prompt: str, max_tokens: int) -> dict:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            self.token_param: max_tokens,
        }
        if self.temperature is not None:  # некоторые модели (o-серия) не принимают temperature
            body["temperature"] = self.temperature
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _error_detail(self, e: urllib.error.HTTPError) -> str:
        raw = e.read().decode("utf-8", errors="replace")[:400]
        try:
            err = json.loads(raw).get("error", raw)
            raw = err.get("message", raw) if isinstance(err, dict) else str(err)
        except (ValueError, AttributeError):
            pass
        text = redact_text(str(raw))
        if self.api_key:
            text = text.replace(self.api_key, "***")
        return text

    def complete(self, *, system: str, prompt: str, max_tokens: int = 4096) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(  # noqa: S310 — URL из конфига
            f"{self.base_url}/chat/completions",
            data=json.dumps(self._body(system, prompt, max_tokens)).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = self._error_detail(e)
            if e.code in (401, 403):
                raise LLMError(f"{self.host}: auth failed ({e.code}); check CF_LLM_API_KEY") from e
            if e.code == 404:
                raise LLMError(f"{self.host}: model or endpoint not found: {detail}") from e
            if e.code == 429 or e.code >= 500:
                raise LLMError(f"{self.host}: HTTP {e.code} {detail}", retryable=True) from e
            raise LLMError(f"{self.host}: HTTP {e.code} {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"{self.host} is not reachable", retryable=True) from e
        except json.JSONDecodeError as e:
            raise LLMError(f"{self.host} returned invalid JSON", retryable=True) from e

        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError(f"{self.host}: unexpected response shape", retryable=True) from e
        if choice.get("finish_reason") == "content_filter":
            raise LLMError(f"{self.host}: response blocked by content filter")
        if not text.strip():
            raise LLMError(f"{self.host}: empty response", retryable=True)
        return text
