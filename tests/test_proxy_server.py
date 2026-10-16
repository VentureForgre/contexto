"""Proxy server, SSE relay, and session log (M2 lane A packet A1). Synthetic fixtures only."""

from __future__ import annotations

import gzip
import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from contexto.store.memory import MemoryStore
from contexto.proxy import (
    THINKING_BINDING_BETA,
    ContextoProxy,
    add_beta,
    identity_transform,
    serialize_request,
)

SECRET = "test-key-not-real-0123456789"
CONTENT = "PRIVATE-PROMPT-TEXT"

# Request shape modeled on Claude Code's (block types, roles, cache_control, thinking with a fake
# signature), serialized the way JavaScript JSON.stringify does: compact, UTF-8, insertion order.
REQUEST = {
    "model": "claude-haiku-5-5",
    "max_tokens": 32000,
    "system": [
        {"type": "text", "text": "You are Claude Code. Café ✓ 日本語   \"quoted\" \\ back\tslash\n"},
        {"type": "text", "text": CONTENT, "cache_control": {"type": "ephemeral"}},
    ],
    "tools": [
        {"name": "Read", "description": "Reads a file. Emoji 🚀", "input_schema": {
            "type": "object", "properties": {"file_path": {"type": "string"}, "limit": {"type": "number"}},
            "required": ["file_path"], "additionalProperties": False}},
        {"name": "Bash", "description": "Runs a command", "input_schema": {"type": "object", "properties": {}},
         "cache_control": {"type": "ephemeral"}},
    ],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": CONTENT}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": CONTENT, "signature": "ZmFrZS1zaWduYXR1cmU="},
            {"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {"file_path": "a.py", "limit": 2.5}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": CONTENT,
                                      "cache_control": {"type": "ephemeral"}}]},
    ],
    "thinking": {"type": "enabled", "budget_tokens": 1024},
    "stream": True,
}
REQUEST_BYTES = json.dumps(REQUEST, ensure_ascii=False, separators=(",", ":")).encode()


def sse(*events: dict) -> bytes:
    return b"".join(b"event: " + e["type"].encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)


STREAM = [
    {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "content": [],
                                          "usage": {"input_tokens": 12, "cache_creation_input_tokens": 300,
                                                    "cache_read_input_tokens": 4000, "output_tokens": 1},
                                          "input_transformations": [{"type": "thinking_dropped",
                                                                     "reason": "end_user_binding_mismatch",
                                                                     "location": "messages.1"}]}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": CONTENT}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
]


# OpenAI Responses API shapes as Codex sends them (instructions and tools as items, no content).
RESPONSES_REQUEST = {
    "model": "gpt-6-astra",
    "instructions": "synthetic instructions",
    "input": [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": CONTENT}]},
        {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "call_1"},
        {"type": "function_call_output", "call_id": "call_1", "output": CONTENT},
    ],
    "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object", "properties": {}}}],
    "stream": True,
    "store": False,
}
RESPONSES_BYTES = json.dumps(RESPONSES_REQUEST, ensure_ascii=False, separators=(",", ":")).encode()
RESPONSES_USAGE = {"input_tokens": 5000, "input_tokens_details": {"cached_tokens": 4096}, "cache_write_tokens": 100,
                   "output_tokens": 300, "output_tokens_details": {"reasoning_tokens": 120}, "total_tokens": 5300}
RESPONSES_STREAM = [
    {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
    {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "role": "assistant"}},
    {"type": "response.output_text.delta", "output_index": 0, "delta": CONTENT},
    {"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "usage": RESPONSES_USAGE}},
]


class MemoryLog(MemoryStore):
    """Lane B's test double, checking that every event is JSON-serializable."""

    def log(self, event: dict[str, Any]) -> None:
        json.dumps(event)
        super().log(event)

    def requests(self, count: int = 1, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Request entries, waiting briefly: the proxy logs after it finishes relaying."""
        deadline = time.monotonic() + timeout
        while True:
            found = [e for e in self.events if e["event"] == "request"]
            if len(found) >= count or time.monotonic() > deadline:
                return found
            time.sleep(0.01)


class StubEngine:
    """Stand-in for contexto.rewrite.RewriteEngine (A2) with the agreed shape."""

    def __init__(self, mode: str = "identity") -> None:
        self.mode = mode
        self.calls: list[tuple[str, int]] = []
        self.usage: list[tuple[str, dict, int]] = []

    def __call__(self, request, headers, endpoint):
        self.calls.append((endpoint, len(request.get("messages") or [])))
        if self.mode == "raise":
            request["messages"].pop()  # mutate the argument, then fail
            raise RuntimeError("engine bug")
        if self.mode == "bad-return":
            return None, headers, []
        if self.mode == "unserializable":
            return dict(request, extra={1, 2}), headers, []
        if self.mode == "drop-first":
            new = dict(request)
            new["messages"] = request["messages"][2:]
            return new, headers, [{"epoch": {"k": 2, "id": "e1"}}, {"event": "rotated", "pages": 1, "epoch": {"k": 2}}]
        if self.mode == "over-bound":
            return request, headers, [{"over_bound": True, "epoch": {"k": 0}}]
        return request, headers, [{"epoch": {"k": 0}}]

    def observe_usage(self, endpoint, usage, request):
        self.usage.append((endpoint, usage, len(request.get("messages") or request.get("input") or [])))


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list[tuple[str, str, dict[str, str], bytes]] = []
    mode = "sse"

    def log_message(self, *args):
        pass

    def _record(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        Upstream.received.append((self.command, self.path, dict(self.headers), body))
        return body

    def do_HEAD(self):
        self._record()
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._record()
        self._json(200, {"data": [], "path": self.path})

    def do_POST(self):
        body = self._record()
        if self.path.split("?")[0].endswith("/responses"):
            self._responses(json.loads(body))
            return
        if "/v1/messages/count_tokens" in self.path:
            self._json(200, {"input_tokens": 1234})
        elif Upstream.mode == "error":
            self._json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})
        elif Upstream.mode == "overloaded":
            self._json(529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
        elif Upstream.mode == "json" or not json.loads(body).get("stream"):
            self._json(200, {"id": "msg_2", "type": "message", "content": [{"type": "text", "text": CONTENT}],
                             "usage": {"input_tokens": 7, "cache_read_input_tokens": 70, "output_tokens": 3}})
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            gz = Upstream.mode == "sse-gzip"
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            compressor = __import__("zlib").compressobj(9, 8, 16 + 15) if gz else None
            for event in STREAM:
                chunk = sse(event)
                if compressor:
                    chunk = compressor.compress(chunk) + compressor.flush(__import__("zlib").Z_SYNC_FLUSH)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                time.sleep(0.15)
            if compressor:
                tail = compressor.flush()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(tail), tail))
            self.wfile.write(b"0\r\n\r\n")

    def _responses(self, request: dict) -> None:
        if not request.get("stream"):
            self._json(200, {"id": "resp_1", "object": "response", "status": "completed", "output": [],
                             "usage": RESPONSES_USAGE})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for event in RESPONSES_STREAM:
            chunk = b"event: " + event["type"].encode() + b"\ndata: " + json.dumps(event).encode() + b"\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
            time.sleep(0.1)
        self.wfile.write(b"0\r\n\r\n")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def upstream():
    Upstream.received = []
    Upstream.mode = "sse"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/base"
    server.shutdown()
    server.server_close()


@pytest.fixture
def make_proxy(upstream):
    started = []

    def make(**kwargs) -> tuple[ContextoProxy, MemoryLog]:
        log = MemoryLog()
        proxy = ContextoProxy(kwargs.pop("upstream", upstream), log, **kwargs)
        proxy.start()
        started.append(proxy)
        return proxy, log

    yield make
    for proxy in started:
        proxy.stop()


def call(proxy: ContextoProxy, method: str, path: str, body: bytes = b"", headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    conn.request(method, path, body=body or None, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


AUTH = {"Content-Type": "application/json", "x-api-key": SECRET, "Authorization": f"Bearer {SECRET}",
        "Cookie": f"s={SECRET}", "anthropic-version": "2023-06-01", "anthropic-beta": "claude-code-20250219"}


# Byte stability


def test_serializer_is_deterministic_and_round_trips_cli_json():
    parsed = json.loads(REQUEST_BYTES)
    assert serialize_request(parsed) == REQUEST_BYTES
    assert serialize_request(json.loads(REQUEST_BYTES)) == serialize_request(json.loads(REQUEST_BYTES))
    for field in ("system", "tools"):
        original = json.dumps(REQUEST[field], ensure_ascii=False, separators=(",", ":")).encode()
        assert original in serialize_request(parsed)


def test_identity_forwards_the_cli_bytes_exactly(make_proxy):
    proxy, log = make_proxy()
    odd = json.dumps(REQUEST, indent=1).encode()  # formatting the serializer would not reproduce
    response, _ = call(proxy, "POST", "/v1/messages?beta=true", odd, AUTH)
    assert response.status == 200
    method, path, _, body = Upstream.received[0]
    assert (method, path, body) == ("POST", "/base/v1/messages?beta=true", odd)
    entry = log.requests()[0]
    assert entry["request_bytes"] == entry["original_request_bytes"] == len(odd)


def test_rewritten_request_is_sent_with_stable_bytes(make_proxy):
    engine = StubEngine("drop-first")
    proxy, log = make_proxy(transform=engine, observe_usage=engine.observe_usage)
    for _ in range(2):
        call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    first, second = Upstream.received[0][3], Upstream.received[1][3]
    assert first == second
    sent = json.loads(first)
    assert len(sent["messages"]) == 1 and sent["messages"][0]["role"] == "user"
    assert json.dumps(sent["system"], ensure_ascii=False, separators=(",", ":")).encode() in first
    entry = log.requests()[0]
    assert entry["original_message_count"] == 3 and entry["message_count"] == 1
    assert entry["epoch"] == {"k": 2} and entry["transform_events"][1]["event"] == "rotated"


# Endpoints


def test_count_tokens_goes_through_the_transform_with_its_endpoint(make_proxy):
    engine = StubEngine("drop-first")
    proxy, log = make_proxy(transform=engine, observe_usage=engine.observe_usage)
    response, data = call(proxy, "POST", "/v1/messages/count_tokens?beta=true", REQUEST_BYTES, AUTH)
    assert response.status == 200 and json.loads(data) == {"input_tokens": 1234}
    assert engine.calls == [("/v1/messages/count_tokens", 3)]
    assert len(json.loads(Upstream.received[0][3])["messages"]) == 1
    assert engine.usage == []  # no usage feedback for count_tokens


def test_other_paths_pass_through_untouched(make_proxy):
    engine = StubEngine("drop-first")
    proxy, log = make_proxy(transform=engine)
    response, _ = call(proxy, "HEAD", "/api/hello")
    assert response.status == 200
    response, data = call(proxy, "GET", "/v1/models?limit=5", headers={"x-api-key": SECRET})
    assert response.status == 200 and json.loads(data)["path"] == "/base/v1/models?limit=5"
    response, _ = call(proxy, "POST", "/v1/other", REQUEST_BYTES, AUTH)
    assert Upstream.received[2][3] == REQUEST_BYTES
    assert engine.calls == []
    assert [e["path"] for e in log.requests(3)] == ["/api/hello", "/v1/models", "/v1/other"]


# SSE relay and usage


def test_sse_is_relayed_in_order_as_it_arrives_and_usage_is_logged(make_proxy):
    engine = StubEngine()
    proxy, log = make_proxy(transform=engine, observe_usage=engine.observe_usage)
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    conn.request("POST", "/v1/messages", body=REQUEST_BYTES, headers=AUTH)
    response = conn.getresponse()
    started = time.monotonic()
    first = response.read1(65536)
    first_at = time.monotonic() - started
    rest = response.read()
    conn.close()
    assert b"message_start" in first and first_at < 0.4  # upstream takes about 0.75 s in total
    assert first + rest == sse(*STREAM)  # byte-identical, so usage reaches the CLI unchanged
    entry = log.requests()[0]
    assert entry["usage"] == {"input_tokens": 12, "cache_creation_input_tokens": 300,
                              "cache_read_input_tokens": 4000, "output_tokens": 42}
    assert entry["status"] == 200 and entry["model"] == "claude-haiku-5-5" and entry["stream"] is True
    assert entry["input_transformations"] == [{"type": "thinking_dropped", "reason": "end_user_binding_mismatch",
                                               "location": "messages.1"}]
    assert engine.usage == [("/v1/messages", entry["usage"], 3)]


def test_gzip_sse_is_relayed_unchanged_and_usage_still_read(make_proxy):
    Upstream.mode = "sse-gzip"
    proxy, log = make_proxy()
    response, data = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, dict(AUTH, **{"Accept-Encoding": "gzip"}))
    assert response.getheader("Content-Encoding") == "gzip"
    assert gzip.decompress(data) == sse(*STREAM)
    assert log.requests()[0]["usage"]["output_tokens"] == 42


def test_non_streaming_response_passes_through(make_proxy):
    Upstream.mode = "json"
    proxy, log = make_proxy()
    response, data = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert response.status == 200 and json.loads(data)["usage"]["input_tokens"] == 7
    assert log.requests()[0]["usage"] == {"input_tokens": 7, "cache_creation_input_tokens": None,
                                          "cache_read_input_tokens": 70, "output_tokens": 3}


# Errors


@pytest.mark.parametrize("mode, status", [("error", 400), ("overloaded", 529)])
def test_upstream_errors_are_forwarded_unchanged_and_never_retried(make_proxy, mode, status):
    Upstream.mode = mode
    engine = StubEngine("drop-first")
    proxy, log = make_proxy(transform=engine, observe_usage=engine.observe_usage)
    response, data = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert response.status == status and json.loads(data)["type"] == "error"
    assert len(Upstream.received) == 1
    assert log.requests()[0]["status"] == status
    assert engine.usage == []
    four_xx = [e for e in log.events if e["event"] == "upstream_4xx"]
    if status == 400:
        assert four_xx == [dict(four_xx[0], status=400, path="/v1/messages", epoch={"k": 2})]
        assert four_xx[0]["transform_events"]
    else:
        assert four_xx == []


def test_unreachable_upstream_returns_local_502_once(make_proxy):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    proxy, log = make_proxy(upstream=f"http://127.0.0.1:{dead}")
    response, data = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert response.status == 502 and json.loads(data)["type"] == "error"
    assert log.requests()[0]["status"] == 502 and "unreachable" in log.requests()[0]["error"]


@pytest.mark.parametrize("mode, error", [("raise", "RuntimeError"), ("bad-return", "TypeError")])
def test_failing_transform_forwards_original_bytes(make_proxy, mode, error):
    proxy, log = make_proxy(transform=StubEngine(mode))
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert Upstream.received[0][3] == REQUEST_BYTES
    entry = log.requests()[0]
    assert entry["transform_error"] == error and entry["message_count"] == 3


def test_over_bound_flag_is_logged(make_proxy):
    proxy, log = make_proxy(transform=StubEngine("over-bound"))
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert log.requests()[0]["over_bound"] is True


# Headers and privacy


def test_auth_headers_forwarded_unchanged_and_never_logged(make_proxy):
    proxy, log = make_proxy(transform=StubEngine("drop-first"))
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    headers = Upstream.received[0][2]
    assert headers["x-api-key"] == SECRET and headers["Authorization"] == f"Bearer {SECRET}"
    assert headers["Cookie"] == f"s={SECRET}" and headers["anthropic-beta"] == "claude-code-20250219"
    text = json.dumps(log.events)
    assert SECRET not in text and CONTENT not in text and "ZmFrZS1zaWduYXR1cmU" not in text


def test_observe_thinking_adds_the_binding_beta_only_when_enabled(make_proxy):
    proxy, _ = make_proxy()
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert Upstream.received[0][2]["anthropic-beta"] == "claude-code-20250219"
    proxy_on, log = make_proxy(observe_thinking=True)
    call(proxy_on, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert Upstream.received[1][2]["anthropic-beta"] == f"claude-code-20250219,{THINKING_BINDING_BETA}"
    assert Upstream.received[1][3] == REQUEST_BYTES  # the body is untouched; only the header changes
    assert log.events[0] == dict(log.events[0], event="proxy_start", observe_thinking=True)


def test_add_beta_is_case_insensitive_and_idempotent():
    assert add_beta({}, "b") == {"anthropic-beta": "b"}
    assert add_beta({"Anthropic-Beta": "a, b"}, "b") == {"Anthropic-Beta": "a,b"}


# Binding


def test_binds_loopback_only_with_a_free_port(make_proxy):
    proxy, log = make_proxy()
    assert proxy.port > 0 and proxy.url == f"http://127.0.0.1:{proxy.port}"
    assert proxy._server.server_address[0] == "127.0.0.1"
    assert log.events[0]["event"] == "proxy_start" and log.events[0]["port"] == proxy.port


def test_rejects_non_http_upstream():
    with pytest.raises(ValueError):
        ContextoProxy("ftp://example.invalid", MemoryLog(), identity_transform)


# Lone surrogates and malformed input (reviewer-1, PR #26 B1)

# JSON.stringify escapes a lone surrogate as a backslash-u escape; tool output cut mid-emoji does that.
SURROGATE_REQUEST = {
    "model": "claude-haiku-5-5",
    "system": [{"type": "text", "text": "head \ude00 tail"}],
    "tools": REQUEST["tools"],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_02", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_02", "content": "cut \ud83d"}]},
    ],
    "stream": False,
}
SURROGATE_BYTES = json.dumps(SURROGATE_REQUEST, ensure_ascii=False, separators=(",", ":")).encode(
    "utf-8", "backslashreplace")


def test_serializer_writes_lone_surrogates_like_json_stringify():
    assert b"\\ud83d" in SURROGATE_BYTES and b"\\ude00" in SURROGATE_BYTES
    assert serialize_request(json.loads(SURROGATE_BYTES)) == SURROGATE_BYTES


def test_lone_surrogates_are_forwarded_byte_identical_with_identity(make_proxy):
    Upstream.mode = "json"
    proxy, log = make_proxy(transform=StubEngine())
    response, _ = call(proxy, "POST", "/v1/messages", SURROGATE_BYTES, AUTH)
    assert response.status == 200 and Upstream.received[0][3] == SURROGATE_BYTES
    assert "transform_error" not in log.requests()[0]


def test_lone_surrogates_survive_a_rewrite(make_proxy):
    Upstream.mode = "json"
    proxy, _ = make_proxy(transform=StubEngine("drop-first"))
    response, _ = call(proxy, "POST", "/v1/messages", SURROGATE_BYTES, AUTH)
    sent = Upstream.received[0][3]
    assert response.status == 200
    assert json.loads(sent)["messages"] == SURROGATE_REQUEST["messages"][2:]
    system = json.dumps(SURROGATE_REQUEST["system"], ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", "backslashreplace")
    assert system in sent


def test_unserializable_transform_result_falls_back_to_original_bytes(make_proxy):
    proxy, log = make_proxy(transform=StubEngine("unserializable"))
    response, _ = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert response.status == 200 and Upstream.received[0][3] == REQUEST_BYTES
    assert log.requests()[0]["transform_error"] == "serialize: TypeError"


def test_deeply_nested_json_is_forwarded_untouched(make_proxy):
    Upstream.mode = "json"
    proxy, log = make_proxy(transform=StubEngine("drop-first"))
    body = b'{"model":"m","stream":false,"messages":[],"x":' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    response, _ = call(proxy, "POST", "/v1/messages", body, AUTH)
    assert response.status == 200 and Upstream.received[0][3] == body
    # Whether parsing hits the recursion limit depends on the Python version; either way the
    # body is forwarded untouched and nothing crashes.
    assert log.requests()[0].get("transform_skipped") in (None, "request body is not JSON")
    assert not [e for e in log.events if e["event"] == "proxy_error"]


def test_malformed_framing_gets_a_400_not_a_dropped_connection(make_proxy):
    proxy, log = make_proxy()
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as sock:
        sock.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n")
        reply = b""
        while chunk := sock.recv(65536):  # the proxy closes the connection after the error
            reply += chunk
    assert reply.startswith(b"HTTP/1.1 400") and b"malformed request framing" in reply
    assert Upstream.received == []
    assert log.requests()[0]["status"] == 400


def test_internal_error_before_upstream_forwards_the_original_request(make_proxy, monkeypatch):
    def broken(headers, beta):
        raise RuntimeError("boom")

    Upstream.mode = "json"
    monkeypatch.setattr("contexto.proxy.add_beta", broken)
    proxy, log = make_proxy(transform=StubEngine("drop-first"), observe_thinking=True)
    response, data = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert response.status == 200 and json.loads(data)["usage"]["input_tokens"] == 7
    assert len(Upstream.received) == 1 and Upstream.received[0][3] == REQUEST_BYTES  # the CLI's own bytes
    assert Upstream.received[0][2]["x-api-key"] == SECRET
    errors = [e for e in log.events if e["event"] == "proxy_error"]
    assert [(e["error"], e["fallback"]) for e in errors] == [("RuntimeError", "forward_original")]
    assert log.requests()[0]["fallback"] is True and log.requests()[0]["status"] == 200


def test_internal_error_after_upstream_answers_502_without_resending(make_proxy, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    Upstream.mode = "json"
    monkeypatch.setattr("contexto.proxy.UsageReader", broken)  # fails after the request went upstream
    proxy, log = make_proxy()
    response, data = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert response.status == 502 and b"internal error" in data
    assert len(Upstream.received) == 1  # never sent twice
    errors = [e for e in log.events if e["event"] == "proxy_error"]
    assert [(e["error"], e["fallback"]) for e in errors] == [("RuntimeError", "local_502")]


# OpenAI Responses API (lane A packet A1b, for the A5 Codex adapter)


def test_responses_go_through_the_transform_with_their_endpoint(make_proxy):
    engine = StubEngine()
    proxy, log = make_proxy(transform=engine, observe_usage=engine.observe_usage)
    for path in ("/responses", "/v1/responses"):
        response, data = call(proxy, "POST", path, RESPONSES_BYTES, AUTH)
        assert response.status == 200
    assert [c[0] for c in engine.calls] == ["/responses", "/v1/responses"]
    # Path mapping: the upstream base carries the version path; the request path is appended.
    assert [r[1] for r in Upstream.received] == ["/base/responses", "/base/v1/responses"]
    assert Upstream.received[0][3] == RESPONSES_BYTES


def test_responses_sse_is_relayed_in_order_and_usage_parsed(make_proxy):
    engine = StubEngine()
    proxy, log = make_proxy(transform=engine, observe_usage=engine.observe_usage)
    response, data = call(proxy, "POST", "/responses", RESPONSES_BYTES, AUTH)
    types = [json.loads(line[6:])["type"] for line in data.decode().splitlines() if line.startswith("data: ")]
    assert types == [e["type"] for e in RESPONSES_STREAM]
    entry = log.requests()[0]
    assert entry["api"] == "responses" and entry["usage"] == RESPONSES_USAGE  # kept as given
    assert engine.usage == [("/responses", RESPONSES_USAGE, 4)]
    assert CONTENT not in json.dumps(log.events)


def test_non_streaming_responses_usage_is_parsed(make_proxy):
    engine = StubEngine()
    proxy, log = make_proxy(transform=engine, observe_usage=engine.observe_usage)
    body = json.dumps(dict(RESPONSES_REQUEST, stream=False)).encode()
    response, data = call(proxy, "POST", "/responses", body, AUTH)
    assert response.status == 200 and log.requests()[0]["usage"] == RESPONSES_USAGE
    assert engine.usage[0][1] == RESPONSES_USAGE


def test_messages_entries_are_tagged_with_their_api(make_proxy):
    Upstream.mode = "json"
    proxy, log = make_proxy()
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert log.requests()[0]["api"] == "messages"


def test_models_and_other_paths_pass_through_for_codex(make_proxy):
    engine = StubEngine("drop-first")
    proxy, log = make_proxy(transform=engine)
    response, data = call(proxy, "GET", "/models?client_version=0.154.0", headers={"chatgpt-account-id": "acct"})
    assert response.status == 200 and json.loads(data)["path"] == "/base/models?client_version=0.154.0"
    assert engine.calls == [] and "api" not in log.requests()[0]


def test_websocket_upgrade_is_refused_and_logged(make_proxy):
    proxy, log = make_proxy()
    response, data = call(proxy, "GET", "/responses", headers={"Upgrade": "websocket", "Connection": "Upgrade",
                                                                "Sec-WebSocket-Version": "13"})
    assert response.status == 501 and b"WebSocket is not supported" in data
    assert Upstream.received == []
    assert [e["event"] for e in log.events if e["event"] == "websocket_rejected"] == ["websocket_rejected"]


def _deflate(data: bytes, raw: bool = False) -> bytes:
    compressor = __import__("zlib").compressobj(9, 8, -15 if raw else 15)
    return compressor.compress(data) + compressor.flush()


@pytest.mark.parametrize("encoding, encode", [
    ("gzip", gzip.compress),
    ("deflate", _deflate),
    ("deflate", lambda data: _deflate(data, raw=True)),
])
@pytest.mark.parametrize("path, body", [("/v1/messages", REQUEST_BYTES), ("/responses", RESPONSES_BYTES)])
def test_encoded_body_is_decoded_transformed_and_forwarded_uncompressed(make_proxy, encoding, encode, path, body):
    engine = StubEngine("drop-first" if path == "/v1/messages" else "identity")
    proxy, log = make_proxy(transform=engine)
    wire = encode(body)
    response, _ = call(proxy, "POST", path, wire, dict(AUTH, **{"Content-Encoding": encoding}))
    assert response.status == 200 and [c[0] for c in engine.calls] == [path]
    _, _, headers, sent = Upstream.received[0]
    assert "Content-Encoding" not in headers and int(headers["Content-Length"]) == len(sent)
    if path == "/v1/messages":
        assert len(json.loads(sent)["messages"]) == 1  # the engine's rewrite went upstream
    else:
        assert sent == body  # unchanged request: the decoded CLI bytes, uncompressed
    entry = log.requests()[0]
    assert entry["request_content_encoding"] == encoding and entry["wire_request_bytes"] == len(wire)
    assert entry["original_request_bytes"] == len(body)


def test_zstd_is_decoded_on_314_and_rejected_before(make_proxy):
    proxy, log = make_proxy(transform=StubEngine())
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    wire = zstd.compress(REQUEST_BYTES) if zstd else b"\x28\xb5\x2f\xfdzstd-frame"
    response, data = call(proxy, "POST", "/v1/messages", wire, dict(AUTH, **{"Content-Encoding": "zstd"}))
    if zstd:
        assert response.status == 200 and Upstream.received[0][3] == REQUEST_BYTES
    else:
        assert response.status == 415 and b"Python 3.14" in data and Upstream.received == []


@pytest.mark.parametrize("encoding, wire", [("br", b"\x0b\x02\x80opaque"), ("gzip", b"\x1f\x8b corrupt"),
                                            ("deflate", b"not deflate at all")])
@pytest.mark.parametrize("path, api", [("/v1/messages", "messages"), ("/v1/messages/count_tokens", "messages"),
                                       ("/responses", "responses")])
def test_undecodable_body_is_rejected_415_in_the_api_error_format(make_proxy, encoding, wire, path, api):
    engine = StubEngine("drop-first")
    proxy, log = make_proxy(transform=engine)
    response, data = call(proxy, "POST", path, wire, dict(AUTH, **{"Content-Encoding": encoding}))
    assert response.status == 415
    error = json.loads(data)
    if api == "messages":
        assert error["type"] == "error" and error["error"]["type"] == "invalid_request_error"
        message = error["error"]["message"]
    else:
        assert error["error"]["code"] == "unsupported_content_encoding" and "param" in error["error"]
        message = error["error"]["message"]
    assert message.startswith("contexto:") and encoding in message and "contexto run" in message
    assert Upstream.received == [] and engine.calls == []  # never forwarded, never transformed
    rejected = [e for e in log.events if e["event"] == "encoded_body_rejected"]
    assert rejected == [dict(rejected[0], encoding=encoding, endpoint=path)] and rejected[0]["reason"]


def test_decompression_bomb_is_rejected(make_proxy, monkeypatch):
    monkeypatch.setattr("contexto.proxy.MAX_DECODED_BODY_BYTES", 1000)
    proxy, log = make_proxy(transform=StubEngine())
    response, data = call(proxy, "POST", "/v1/messages", gzip.compress(b"0" * 1_000_000),
                          dict(AUTH, **{"Content-Encoding": "gzip"}))
    assert response.status == 415 and b"more than 1,000 bytes" in data and Upstream.received == []


def test_decode_request_body_unit():
    from contexto.proxy import EncodedBodyError, decode_request_body

    assert decode_request_body(gzip.compress(b"abc"), "gzip") == b"abc"
    assert decode_request_body(_deflate(b"abc"), "deflate") == b"abc"
    with pytest.raises(EncodedBodyError):
        decode_request_body(gzip.compress(b"abc")[:-6], "gzip")  # truncated
    with pytest.raises(EncodedBodyError, match="more than 10 bytes"):
        decode_request_body(gzip.compress(b"x" * 11), "gzip", limit=10)
    assert decode_request_body(gzip.compress(b"x" * 10), "gzip", limit=10) == b"x" * 10


def test_identity_content_encoding_is_not_rejected(make_proxy):
    Upstream.mode = "json"
    proxy, log = make_proxy()
    response, _ = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, dict(AUTH, **{"Content-Encoding": "identity"}))
    assert response.status == 200 and Upstream.received[0][3] == REQUEST_BYTES


def test_encoded_body_on_other_paths_passes_through(make_proxy):
    proxy, log = make_proxy()
    response, _ = call(proxy, "POST", "/v1/other", b"opaque", dict(AUTH, **{"Content-Encoding": "gzip"}))
    assert Upstream.received[0][3] == b"opaque"


def test_usage_reaches_the_engine_before_the_client_sees_the_end(make_proxy):
    """Claude Code sends its next request as soon as a response completes; the engine must already
    have that response's usage (rule 4 sizes the next request from it)."""
    engine = StubEngine()
    seen_at_completion = []

    def observe(endpoint, usage, request):
        time.sleep(0.3)  # a slow engine widens any window between completion and feedback
        engine.observe_usage(endpoint, usage, request)

    Upstream.mode = "json"
    proxy, log = make_proxy(transform=engine, observe_usage=observe)
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    seen_at_completion.append(len(engine.usage))
    assert seen_at_completion == [1]


def test_engine_exception_logs_transform_error_at_error_level(make_proxy):
    """Contract rule 14, genuine bugs: forward the original request and log transform_error."""
    Upstream.mode = "json"
    engine = StubEngine("drop-first")
    proxy, log = make_proxy(transform=engine)
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)  # a good transform sets the known epoch
    engine.mode = "raise"
    response, _ = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert response.status == 200 and Upstream.received[1][3] == REQUEST_BYTES
    errors = [e for e in log.events if e["event"] == "transform_error"]
    assert errors == [dict(errors[0], level="error", error="RuntimeError", stage="transform",
                           endpoint="/v1/messages", epoch={"k": 2})]
    engine.mode = "unserializable"
    call(proxy, "POST", "/responses", RESPONSES_BYTES, AUTH)
    errors = [e for e in log.events if e["event"] == "transform_error"]
    assert errors[-1]["stage"] == "serialize" and errors[-1]["endpoint"] == "/responses"
    assert CONTENT not in json.dumps(log.events)


# Multi-member gzip and trailing data (A1c, reviewer-1 on #57): never forward a partial body


def test_every_gzip_member_is_decoded():
    from contexto.proxy import decode_request_body

    assert decode_request_body(gzip.compress(b"abc") + gzip.compress(b"def"), "gzip") == b"abcdef"



@pytest.mark.parametrize("wire, encoding, reason", [
    (gzip.compress(b"abc") + b"garbage", "gzip", "after the first compressed member"),
    (gzip.compress(b"abc") + b"\0" * 16, "gzip", "after the first compressed member"),  # padding is data too
    (_deflate(b"abc") + b"garbage", "deflate", "after the end of the compressed stream"),
    (_deflate(b"abc", raw=True) + b"garbage", "deflate", "after the end of the compressed stream"),
    (gzip.compress(b"abc") + gzip.compress(b"def")[:-4], "gzip", "truncated"),
    (gzip.compress(b"abc")[:-4], "gzip", "truncated"),
    (b"", "gzip", "empty"),
])
def test_trailing_or_truncated_data_is_refused(wire, encoding, reason):
    from contexto.proxy import EncodedBodyError, decode_request_body

    with pytest.raises(EncodedBodyError, match=reason):
        decode_request_body(wire, encoding)


def test_the_cap_applies_across_members():
    from contexto.proxy import EncodedBodyError, decode_request_body

    two = gzip.compress(b"x" * 600) + gzip.compress(b"y" * 600)
    assert decode_request_body(two, "gzip", limit=1200) == b"x" * 600 + b"y" * 600
    with pytest.raises(EncodedBodyError, match="more than 1,000 bytes"):
        decode_request_body(two, "gzip", limit=1000)


def test_zstd_frames_are_all_decoded_on_314():
    from contexto.proxy import EncodedBodyError, decode_request_body

    zstd = pytest.importorskip("compression.zstd")
    assert decode_request_body(zstd.compress(b"abc") + zstd.compress(b"def"), "zstd") == b"abcdef"
    with pytest.raises(EncodedBodyError):
        decode_request_body(zstd.compress(b"abc") + b"garbage", "zstd")


def test_proxy_forwards_the_whole_multi_member_body(make_proxy):
    half = len(REQUEST_BYTES) // 2
    wire = gzip.compress(REQUEST_BYTES[:half]) + gzip.compress(REQUEST_BYTES[half:])
    proxy, log = make_proxy(transform=StubEngine())
    response, _ = call(proxy, "POST", "/v1/messages", wire, dict(AUTH, **{"Content-Encoding": "gzip"}))
    assert response.status == 200 and Upstream.received[0][3] == REQUEST_BYTES


def test_proxy_refuses_trailing_garbage_with_415(make_proxy):
    proxy, log = make_proxy(transform=StubEngine())
    wire = gzip.compress(REQUEST_BYTES) + b"garbage"
    response, data = call(proxy, "POST", "/v1/messages", wire, dict(AUTH, **{"Content-Encoding": "gzip"}))
    assert response.status == 415 and Upstream.received == []
    assert "after the first compressed member" in json.loads(data)["error"]["message"]
