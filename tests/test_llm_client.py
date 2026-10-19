"""LLM client: OpenAI SDK request shape, retries, cache-key stability, typed failures.

Runs against a local stub HTTP server so the real SDK path (env config, auth header,
extra_body flattening) is exercised without touching the live proxy.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import config
from pipeline import common

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


def _completion(content: str, finish_reason: str = "stop") -> dict:
    return {"id": "c1", "object": "chat.completion", "created": 0, "model": "local-general",
            "choices": [{"index": 0, "finish_reason": finish_reason,
                         "message": {"role": "assistant", "content": content}}]}


class Stub:
    """Serves queued (status, body) replies and records every request."""

    def __init__(self) -> None:
        self.replies: list[tuple[int, dict]] = []
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append({"path": self.path, "auth": self.headers["Authorization"],
                                      "body": body})
                status, reply = stub.replies.pop(0)
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"


@pytest.fixture
def stub(monkeypatch, tmp_path):
    s = Stub()
    monkeypatch.setenv("OPENAI_BASE_URL", s.base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(common, "LLM_CACHE", tmp_path)
    monkeypatch.setattr(config, "LLM_MODEL", "local-general")
    monkeypatch.setattr(common.asyncio, "sleep", _no_sleep)
    yield s
    s.server.shutdown()


async def _no_sleep(_seconds: float) -> None:
    return None


def _run(prompt: str = "hi", **kw) -> list:
    return common.run(common.llm_json_many([("k", prompt)], SCHEMA, schema_name="t",
                                           max_tokens=64, **kw))


def test_request_uses_sdk_shape_and_env_config(stub):
    stub.replies.append((200, _completion('{"ok": true}')))
    assert _run() == [("k", {"ok": True})]
    req = stub.requests[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["auth"] == "Bearer test-key"
    body = req["body"]
    assert body["model"] == "local-general"
    assert body["max_completion_tokens"] == 64
    assert "max_tokens" not in body
    assert "chat_template_kwargs" not in body  # OpenAI rejects unknown fields
    assert body["response_format"]["json_schema"]["strict"] is True


def test_disable_thinking_sends_extra_body(stub, monkeypatch):
    monkeypatch.setattr(common, "NO_THINKING", {"chat_template_kwargs": {"enable_thinking": False},
                                                "include_reasoning": False})
    stub.replies.append((200, _completion('{"ok": true}')))
    _run()
    body = stub.requests[0]["body"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["include_reasoning"] is False


def test_retries_then_succeeds(stub):
    stub.replies += [(500, {"error": {"message": "boom"}}), (200, _completion('{"ok": false}'))]
    assert _run() == [("k", {"ok": False})]
    assert len(stub.requests) == 2


def test_retry_budget_is_ours_alone(stub):
    stub.replies += [(500, {"error": {"message": "boom"}})] * 20
    [(_, result)] = _run()
    assert result["_llm_error"].startswith("InternalServerError:")
    assert len(stub.requests) == config.LLM_RETRIES + 1  # SDK retries would multiply this


def test_truncation_is_reported_not_cached(stub, tmp_path):
    stub.replies += [(200, _completion('{"ok":', "length"))] * (config.LLM_RETRIES + 1)
    [(_, result)] = _run()
    assert "finish_reason=length" in result["_llm_error"]
    assert not list(tmp_path.iterdir())


def test_cache_hit_skips_network(stub):
    stub.replies.append((200, _completion('{"ok": true}')))
    _run("cached")
    assert _run("cached") == [("k", {"ok": True})]
    assert len(stub.requests) == 1


def test_healthcheck_ok(stub):
    stub.replies.append((200, _completion("OK")))
    assert common.llm_healthcheck() == (True, "OK")
    assert stub.requests[0]["body"]["max_completion_tokens"] == 8


def test_healthcheck_bad_key_is_typed_and_secret_free(stub, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-key-value")
    stub.replies.append((401, {"error": {"message": "Authentication Error", "type": "auth_error"}}))
    ok, detail = common.llm_healthcheck()
    assert not ok
    assert detail.startswith("AuthenticationError:")
    assert "wrong-key-value" not in detail


def test_healthcheck_without_injected_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ok, detail = common.llm_healthcheck()
    assert not ok
    assert detail.startswith("OpenAIError:")


def test_payload_shape_and_committed_cache_keys_unchanged():
    payload = common._llm_payload("p", SCHEMA, schema_name="t", temperature=0.0,
                                  max_tokens=10, system="s")
    assert set(payload) == {"model", "messages", "temperature", "max_tokens",
                            "chat_template_kwargs", "response_format"}
    for f in sorted(common.LLM_CACHE.glob("*.json"))[:50]:
        rec = json.loads(f.read_text())
        assert common._llm_key(rec["payload"]) == f.stem
