"""AnthropicLLM без сети: подменённый клиент, настоящие исключения SDK."""

from __future__ import annotations

import types

import anthropic
import httpx2
import pytest

from clipfactory.backends.llm.anthropic import AnthropicLLM
from clipfactory.backends.llm.base import LLMError

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def response(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=REQ)


class FakeStream:
    def __init__(self, outcome):
        self.outcome = outcome

    def __enter__(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.outcome


def message(text="{}", stop_reason="end_turn", category=None):
    blocks = [types.SimpleNamespace(type="thinking", thinking=""),
              types.SimpleNamespace(type="text", text=text)]  # fmt: skip
    details = types.SimpleNamespace(category=category) if category else None
    return types.SimpleNamespace(content=blocks, stop_reason=stop_reason, stop_details=details)


def llm_with(outcome) -> tuple[AnthropicLLM, dict]:
    llm = AnthropicLLM("claude-opus-5-5", api_key="sk-ant-test", effort="medium")
    seen: dict = {}

    def stream(**kwargs):
        seen.update(kwargs)
        return FakeStream(outcome)

    llm._client = types.SimpleNamespace(
        beta=types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream))
    )
    return llm, seen


def test_success_joins_text_blocks_and_sends_expected_params():
    llm, seen = llm_with(message('{"highlights": []}'))
    assert llm.complete(system="sys", prompt="p", max_tokens=1234) == '{"highlights": []}'
    assert seen["model"] == "claude-opus-5-5" and seen["max_tokens"] == 1234
    assert seen["system"] == "sys" and seen["output_config"] == {"effort": "medium"}
    assert seen["fallbacks"] == "default" and seen["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in seen  # на Opus 5.5 thinking не отключается, параметр не шлём


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (anthropic.RateLimitError("rl", response=response(429), body=None), True),
        (anthropic.InternalServerError("boom", response=response(500), body=None), True),
        (anthropic.APIConnectionError(request=REQ), True),
        (anthropic.AuthenticationError("bad key", response=response(401), body=None), False),
        (anthropic.BadRequestError("bad", response=response(400), body=None), False),
        (anthropic.NotFoundError("nf", response=response(404), body=None), False),
    ],
)
def test_sdk_errors_map_to_llm_error(exc, retryable):
    llm, _ = llm_with(exc)
    with pytest.raises(LLMError) as ei:
        llm.complete(system="s", prompt="p")
    assert ei.value.retryable is retryable
    assert "sk-ant-test" not in str(ei.value)


def test_refusal_and_empty_response():
    llm, _ = llm_with(message("", stop_reason="refusal", category="cyber"))
    with pytest.raises(LLMError, match="refused.*cyber"):
        llm.complete(system="s", prompt="p")
    llm, _ = llm_with(message("   "))
    with pytest.raises(LLMError) as ei:
        llm.complete(system="s", prompt="p")
    assert ei.value.retryable is True
