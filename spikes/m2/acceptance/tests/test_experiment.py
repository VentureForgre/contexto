"""The recall experiment sender, against a local stub (no model calls, fake key)."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import experiment, replay  # noqa: E402

PAGE = "This is a long working session. cfg checkout-api.retention_days=juniper-2573 s0 line 0001"


def _body(text: str = "q_p_01: What value did checkout-api.retention_days have?") -> dict[str, Any]:
    preamble = ("Earlier turns of this conversation are paged out.\n\n"
                "pg_aaaa1111 turn 1: Next part of the working log: s1 line 0000\n"
                "pg_bbbb2222 turn 0: This is a long working session. Each message pastes")
    return {"model": "claude-haiku-5-5", "max_tokens": 128000, "system": [{"type": "text", "text": "sys"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "ctx"}, {"type": "text", "text": preamble},
                                                      {"type": "text", "text": text}]}]}


class _Stub(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []
    count = 100_000
    tool: dict[str, Any] | None = None

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        _Stub.seen.append({"path": self.path, "max_tokens": body.get("max_tokens"), "key": self.headers.get("x-api-key"),
                           "beta": self.headers.get("anthropic-beta")})
        if self.path.endswith("/count_tokens"):
            data, ctype = json.dumps({"input_tokens": _Stub.count}).encode(), "application/json"
        else:
            events = [{"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": _Stub.count - 10}}}]
            if _Stub.tool:
                events += [{"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": _Stub.tool["name"]}},
                           {"type": "content_block_delta", "index": 0,
                            "delta": {"type": "input_json_delta", "partial_json": json.dumps(_Stub.tool["input"])}}]
                stop = "tool_use"
            else:
                events += [{"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                           {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "q_p_01: unknown"}}]
                stop = "end_turn"
            events.append({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 50}})
            data = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
            ctype = "text/event-stream"
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture()
def stub():
    _Stub.seen, _Stub.count, _Stub.tool = [], 100_000, None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_reads_the_plant_page_id_from_the_request_index():
    assert experiment.target_page_id(_body(), 0) == "pg_bbbb2222"
    assert experiment.target_page_id(_body(), 7) is None


def test_targets_recall_of_the_page_or_a_search_that_hits_it():
    t = experiment.targets
    assert t({"name": "mcp__contexto__contexto_recall", "input": {"page_id": "pg_bbbb2222"}}, "pg_bbbb2222", PAGE)
    assert not t({"name": "mcp__contexto__contexto_recall", "input": {"page_id": "pg_aaaa1111"}}, "pg_bbbb2222", PAGE)
    assert t({"name": "mcp__contexto__contexto_search", "input": {"query": "checkout-api.retention_days"}}, None, PAGE)
    assert not t({"name": "mcp__contexto__contexto_search", "input": {"query": "Checkout"}}, None, PAGE)  # case-sensitive
    assert not t({"name": "Read", "input": {}}, "pg_bbbb2222", PAGE)


def test_sends_each_variant_once_and_records_the_first_tool_call(stub):
    _Stub.tool = {"name": "mcp__contexto__contexto_search", "input": {"query": "checkout-api.retention_days"}}
    out = experiment.run([("A", _body()), ("B", _body())], stub, "fake-key", ["b1"], PAGE)
    assert [s["path"] for s in _Stub.seen] == ["/v1/messages/count_tokens", "/v1/messages"] * 2
    assert all(s["key"] == "fake-key" for s in _Stub.seen)
    assert [s["max_tokens"] for s in _Stub.seen if s["path"] == "/v1/messages"] == [8_000, 8_000]
    first = out["sends"][0]
    assert first["tool_uses"] == [{"name": "mcp__contexto__contexto_search", "input": {"query": "checkout-api.retention_days"},
                                   "targets_plant_page": True}]
    assert first["stop_reason"] == "tool_use" and first["spent"] == 100_050 and out["spent"] == 200_100
    assert out["stopped"] is None and "fake-key" not in json.dumps(out)


def test_counts_unknown_answers_when_there_is_no_tool_call(stub):
    out = experiment.run([("A", _body())], stub, "fake-key", [], PAGE)
    assert out["sends"][0]["tool_uses"] == [] and out["sends"][0]["unknown_answers"] == 1


def test_refuses_a_send_over_the_input_limit_or_the_approval(stub):
    _Stub.count = 110_001
    out = experiment.run([("A", _body())], stub, "k", [], PAGE)
    assert "per-send limit" in out["stopped"] and [s["path"] for s in _Stub.seen] == ["/v1/messages/count_tokens"]
    _Stub.seen, _Stub.count = [], 100_000
    out = experiment.run([("A", _body()), ("B", _body())], stub, "k", [], PAGE, approved=200_000)
    assert "would pass" in out["stopped"] and len(out["sends"]) == 2  # A sent (100,050), B refused


def test_strip_thinking_and_paged_answers():
    body = {"messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "s"},
                                                           {"type": "text", "text": "OK"}]}]}
    out, removed = experiment.strip_thinking(body)
    assert removed == 1 and out["messages"][0]["content"] == [{"type": "text", "text": "OK"}]
    assert body["messages"][0]["content"][0]["type"] == "thinking"  # the input is not changed
    with pytest.raises(ValueError):
        experiment.strip_thinking({"messages": [{"role": "assistant", "content": [{"type": "thinking"}]}]})
    text = "q_p_01: juniper-2573\nq_p_02: unknown\nq_c_01: x"
    assert experiment.paged_answers(text) == {"q_p_01": "juniper-2573", "q_p_02": "unknown"}


def test_estimates_replace_count_tokens_for_the_gate(stub):
    out = experiment.run([("A", _body())], stub, "k", [], PAGE, estimates={"A": 101_800})
    assert [x["path"] for x in _Stub.seen] == ["/v1/messages"] and out["sends"][0]["estimated_input"] == 101_800
    _Stub.seen = []
    out = experiment.run([("A", _body())], stub, "k", [], PAGE, estimates={"A": 110_001})
    assert _Stub.seen == [] and "per-send limit" in out["stopped"]


class _Forbidden(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        data = json.dumps({"type": "error", "error": {"type": "permission_error",
                                                       "message": "count_tokens not allowed for key k-secret"}}).encode()
        self.send_response(403)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


def test_diagnose_count_tokens_records_status_type_and_a_redacted_message():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Forbidden)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = experiment.diagnose_count_tokens(_body(), f"http://127.0.0.1:{httpd.server_port}", "k-secret", ["b1"])
    finally:
        httpd.shutdown()
    assert out == {"status": 403, "error_type": "permission_error", "message": "count_tokens not allowed for key [redacted]"}


class _Echo(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        _Echo.seen.append({"beta": self.headers.get("anthropic-beta"), "ua": self.headers.get("user-agent")})
        data = json.dumps({"input_tokens": 5}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


def test_no_claude_code_beta_on_any_experiment_request(stub):
    # D23: the sender's own calls drop claude-code-* betas through replay's shared filter.
    betas = ["b1", "claude-code-20250219"]
    experiment.run([("A", _body())], stub, "k", betas, PAGE)
    assert _Stub.seen and all(x["beta"] == "b1" for x in _Stub.seen)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        experiment.diagnose_count_tokens(_body(), f"http://127.0.0.1:{httpd.server_port}", "k", betas)
    finally:
        httpd.shutdown()
    assert _Echo.seen == [{"beta": "b1", "ua": replay.USER_AGENT}]
