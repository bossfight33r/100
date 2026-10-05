from __future__ import annotations

from clipfactory.backends.llm.base import LLMError
from clipfactory.log import get_logger

log = get_logger(__name__)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    """Claude через официальный SDK. Стриминг — транскрипт-чанки бывают длинными."""

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        *,
        api_key: str | None = None,
        effort: str = "medium",
        max_retries: int = 3,
        timeout: float = 600,
    ) -> None:
        self.model = model
        self.effort = effort
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise LLMError("anthropic SDK is not installed") from e
        self._anthropic = anthropic
        # api_key=None -> SDK сам берёт ANTHROPIC_API_KEY / профиль `ant auth login`.
        self._client = anthropic.Anthropic(
            api_key=api_key, max_retries=max_retries, timeout=timeout
        )

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = 16000
    ) -> str:  # pragma: no cover
        a = self._anthropic
        try:
            with self._client.beta.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                output_config={"effort": self.effort},
                betas=[_FALLBACK_BETA],
                fallbacks="default",
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
        except a.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed (check ANTHROPIC_API_KEY)") from e
        except a.PermissionDeniedError as e:
            raise LLMError("Anthropic permission denied") from e
        except a.NotFoundError as e:
            raise LLMError(f"Anthropic model not found: {self.model}") from e
        except a.BadRequestError as e:
            raise LLMError(f"Anthropic bad request: {e.message}") from e
        except a.RateLimitError as e:
            raise LLMError("Anthropic rate limit", retryable=True) from e
        except a.APIStatusError as e:
            raise LLMError(
                f"Anthropic API error {e.status_code}", retryable=e.status_code >= 500
            ) from e
        except a.APIConnectionError as e:
            raise LLMError("Anthropic connection error", retryable=True) from e

        if message.stop_reason == "refusal":
            category = (
                getattr(message.stop_details, "category", None) if message.stop_details else None
            )
            raise LLMError(f"model refused the request (category={category})")
        if message.stop_reason == "max_tokens":
            log.warning("llm.max_tokens", model=self.model, max_tokens=max_tokens)
        text = "".join(b.text for b in message.content if b.type == "text")
        if not text.strip():
            raise LLMError("empty LLM response", retryable=True)
        return text
