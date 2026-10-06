"""OpenAICompatLLM против локального HTTP-сервера (без интернета, ключ фиктивный)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from clipfactory.backends.llm.base import LLMError
from clipfactory.backends.llm.openai_compat import OpenAICompatLLM

KEY = "sk-test-SECRET-123456"


@pytest.fixture
def server():
    state = {
        "status": 200,
        "reply": {
            "choices": [{"message": {"content": '{"highlights": []}'}, "finish_reason": "stop"}]
        },
        "seen": None,
        "auth": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["seen"] = (self.path, json.loads(body))
            state["auth"] = self.headers.get("Authorization")
            payload = json.dumps(state["reply"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1", state
    httpd.shutdown()


def test_request_shape_and_auth(server):
    url, state = server
    llm = OpenAICompatLLM("gpt-x", base_url=url + "/", api_key=KEY)
    assert llm.complete(system="SYS", prompt="P", max_tokens=321) == '{"highlights": []}'
    path, body = state["seen"]
    assert path == "/v1/chat/completions" and state["auth"] == f"Bearer {KEY}"
    assert body["model"] == "gpt-x" and body["max_tokens"] == 321 and body["temperature"] == 0.2
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "P"},
    ]


def test_provider_variants(server):
    url, state = server
    llm = OpenAICompatLLM("o-model", base_url=url, token_param="max_completion_tokens",
                          temperature=None, json_mode=False)  # fmt: skip
    llm.complete(system="s", prompt="p", max_tokens=99)
    _, body = state["seen"]
    assert body["max_completion_tokens"] == 99 and "max_tokens" not in body
    assert "temperature" not in body and "response_format" not in body
    assert state["auth"] is None  # локальные серверы (vLLM, LM Studio) без ключа
    with pytest.raises(ValueError):
        OpenAICompatLLM("m", base_url=url, token_param="max_new_tokens")


@pytest.mark.parametrize(
    ("status", "retryable", "text"),
    [(401, False, "auth failed"), (403, False, "auth failed"), (404, False, "not found"),
     (400, False, "HTTP 400"), (429, True, "429"), (503, True, "503")],
)  # fmt: skip
def test_http_errors_classified_and_key_never_leaks(server, status, retryable, text):
    url, state = server
    state["status"] = status
    state["reply"] = {"error": {"message": f"bad key {KEY} rejected"}}
    with pytest.raises(LLMError) as ei:
        OpenAICompatLLM("m", base_url=url, api_key=KEY).complete(system="s", prompt="p")
    assert ei.value.retryable is retryable and text in str(ei.value)
    assert KEY not in str(ei.value)


def test_bad_responses(server):
    url, state = server
    llm = OpenAICompatLLM("m", base_url=url)
    state["reply"] = {"choices": [{"message": {"content": "  "}, "finish_reason": "stop"}]}
    with pytest.raises(LLMError, match="empty") as ei:
        llm.complete(system="s", prompt="p")
    assert ei.value.retryable
    state["reply"] = {"choices": []}
    with pytest.raises(LLMError, match="unexpected"):
        llm.complete(system="s", prompt="p")
    state["reply"] = {
        "choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]
    }
    with pytest.raises(LLMError, match="content filter") as ei:
        llm.complete(system="s", prompt="p")
    assert not ei.value.retryable


def test_unreachable_is_retryable():
    with pytest.raises(LLMError, match="not reachable") as ei:
        OpenAICompatLLM("m", base_url="http://127.0.0.1:9/v1", timeout=2).complete(
            system="s", prompt="p"
        )
    assert ei.value.retryable


def test_settings_wiring_and_select_pipeline(server, tmp_path, monkeypatch):
    from clipfactory.pipeline.select import select_highlights
    from clipfactory.services import backend_ids, build_llm
    from tests.helpers import make_settings
    from tests.test_select import CAMPAIGN, make_transcript

    url, state = server
    state["reply"] = {"choices": [{"message": {"content": json.dumps({"highlights": [
        {"start_time": 6.5, "end_time": 19.0, "score": 88, "hook_sentence": "хук"}]})}}]}  # fmt: skip
    monkeypatch.setenv("CF_LLM_API_KEY", KEY)
    s = make_settings(
        tmp_path, llm_provider="openai_compat", llm_base_url=url, llm_model="deepseek-chat"
    )
    assert s.llm_api_key.get_secret_value() == KEY and KEY not in repr(s)
    llm = build_llm(s)
    assert isinstance(llm, OpenAICompatLLM) and llm.api_key == KEY
    assert backend_ids(s)["llm"] == f"openai_compat/{url}/deepseek-chat"
    out = select_highlights(make_transcript(10), CAMPAIGN, llm, chunk_sec=1200, overlap_sec=60)
    assert len(out) == 1 and out[0].score == 88

    with pytest.raises(ValueError, match="CF_LLM_BASE_URL"):
        build_llm(make_settings(tmp_path, llm_provider="openai_compat"))
    assert make_settings(tmp_path, llm_temperature=-1).llm_temperature is None
