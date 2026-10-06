"""OllamaLLM против локального HTTP-сервера в тесте (без интернета)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from clipfactory.backends.llm.base import LLMError
from clipfactory.backends.llm.ollama import OllamaLLM


@pytest.fixture
def server():
    state = {"status": 200, "reply": {"message": {"content": '{"highlights": []}'}}, "seen": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["seen"] = (self.path, json.loads(body))
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
    yield f"http://127.0.0.1:{httpd.server_port}", state
    httpd.shutdown()


def test_chat_request_and_response(server):
    url, state = server
    llm = OllamaLLM("qwen2.5:7b-instruct", base_url=url + "/")
    assert llm.complete(system="SYS", prompt="P", max_tokens=321) == '{"highlights": []}'
    path, body = state["seen"]
    assert path == "/api/chat" and body["model"] == "qwen2.5:7b-instruct"
    assert body["stream"] is False and body["format"] == "json"
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["options"]["num_predict"] == 321


def test_errors(server):
    url, state = server
    state["status"], state["reply"] = 404, {"error": "model not found"}
    with pytest.raises(LLMError, match="ollama pull") as ei:
        OllamaLLM("nope", base_url=url).complete(system="s", prompt="p")
    assert ei.value.retryable is False
    state["status"] = 500
    with pytest.raises(LLMError) as ei:
        OllamaLLM("m", base_url=url).complete(system="s", prompt="p")
    assert ei.value.retryable is True
    state["status"], state["reply"] = 200, {"message": {"content": "  "}}
    with pytest.raises(LLMError, match="empty"):
        OllamaLLM("m", base_url=url).complete(system="s", prompt="p")


def test_unreachable_is_retryable():
    with pytest.raises(LLMError, match="ollama serve") as ei:
        OllamaLLM("m", base_url="http://127.0.0.1:9", timeout=2).complete(system="s", prompt="p")
    assert ei.value.retryable is True


def test_select_pipeline_through_ollama(server):
    """select целиком на Ollama-бэкенде: промпт -> HTTP -> JSON -> кандидаты."""
    from clipfactory.pipeline.select import select_highlights
    from tests.test_select import CAMPAIGN, make_transcript

    url, state = server
    state["reply"] = {"message": {"content": json.dumps({"highlights": [
        {"start_time": 6.5, "end_time": 19.0, "score": 77, "hook_sentence": "хук"}]})}}  # fmt: skip
    out = select_highlights(make_transcript(10), CAMPAIGN, OllamaLLM("m", base_url=url),
                            chunk_sec=1200, overlap_sec=60)  # fmt: skip
    assert len(out) == 1 and out[0].score == 77 and out[0].id == "c01"
