"""Локальная LLM через Ollama HTTP API (без внешних сервисов и ключей)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from clipfactory.backends.llm.base import LLMError


class OllamaLLM:
    name = "ollama"

    def __init__(
        self,
        model: str = "qwen2.5:7b-instruct",
        *,
        base_url: str = "http://localhost:11434",
        timeout: float = 600,
        temperature: float = 0.2,
        num_ctx: int = 16384,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        # окно Ollama по умолчанию 2–4K токенов: длиннее промпт (транскрипт чанка)
        # обрезается молча, и модель видит лишь хвост
        self.num_ctx = num_ctx

    def complete(self, *, system: str, prompt: str, max_tokens: int = 4096) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",  # наши промпты всегда ждут JSON-объект
            "options": {
                "num_predict": max_tokens,
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
            },
        }
        req = urllib.request.Request(  # noqa: S310 — URL из конфига
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if e.code == 404:
                raise LLMError(
                    f"Ollama model {self.model!r} not found; run `ollama pull {self.model}`"
                ) from e
            raise LLMError(f"Ollama HTTP {e.code}: {detail}", retryable=e.code >= 500) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(
                f"Ollama is not reachable at {self.base_url} (is `ollama serve` running?)",
                retryable=True,
            ) from e
        except json.JSONDecodeError as e:
            raise LLMError("Ollama returned invalid JSON", retryable=True) from e
        text = (data.get("message") or {}).get("content", "")
        if not text.strip():
            raise LLMError("empty Ollama response", retryable=True)
        return text
