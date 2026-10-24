"""Contexto in front of a CLIProxyAPI upstream (D35). Synthetic fixtures only, zero spend.

``FakeCLIProxyAPI`` copies what CLIProxyAPI does at its HTTP edge, read from its v7.2.142 source
(internal/api/server_routes.go, internal/access/config_access/provider.go,
sdk/api/handlers/claude/code_handlers.go, sdk/api/handlers/stream_forwarder.go,
sdk/api/handlers/openai/openai_responses_handlers.go) and checked against a running 7.3.12 binary:

- Routes: ``POST /v1/messages``, ``POST /v1/messages/count_tokens``, ``POST /v1/responses``,
  ``GET /v1/models``, and the Codex alias ``POST /backend-api/codex/responses``; anything else is
  a 404 with an empty body (``NoRoute`` aborts with the status only; measured on 7.3.12).
- Client keys: ``Authorization: Bearer``, ``X-Api-Key``, ``X-Goog-Api-Key``, ``?key=``, or
  ``?auth_token=``. No key is
  a 401 ``{"error":"Missing API key"}``, a wrong one a 401 ``{"error":"Invalid API key"}``: not the
  Anthropic error shape.
- A model no provider serves is a 400 ``unknown provider for model`` (measured on the owner's
  deployment); a rate limit is a 429 with ``Retry-After``.
- Streams carry ``Cache-Control: no-cache``, ``Connection: keep-alive``, CORS headers, and
  ``X-Cpa-Trace-Id``, and may carry ``: keep-alive`` comment events when
  ``streaming.keepalive-seconds`` is set. A failure after the stream started is written as
  ``event: error`` (Messages) or ``\\nevent: response.failed`` (Responses, Codex clients).
"""

from __future__ import annotations

import http.client
import io
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from contexto import launcher
from contexto.codex import API_KEY_MODE, codex_upstream_with_source, plan_codex, resolve_codex_upstream
from contexto.proxy import SSEEventSplitter, StreamHold, sse_is_comment
from contexto.store.memory import MemoryStore
from contexto.upstream import resolve_upstream

CLIENT_KEY = "sk-cpa-test-not-real-0123456789"
KEEPALIVE = b": keep-alive\n\n"
SERVED_MODELS = {"claude-haiku-5-5", "gpt-5.6-luna"}

MESSAGES_REQUEST = {
    "model": "claude-haiku-5-5",
    "max_tokens": 64,
    "system": [{"type": "text", "text": "You are a synthetic test assistant."}],
    "messages": [{"role": "user", "content": [{"type": "text", "text": "synthetic prompt"}]}],
    "stream": True,
}
RESPONSES_REQUEST = {
    "model": "gpt-5.6-luna",
    "instructions": "synthetic instructions",
    "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "synthetic"}]}],
    "stream": True,
    "store": False,
}
USAGE = {"input_tokens": 40, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1}


def event(payload: dict) -> bytes:
    return b"event: " + payload["type"].encode() + b"\ndata: " + json.dumps(payload).encode() + b"\n\n"


def text_stream() -> list[bytes]:
    return [
        event({"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                                    "content": [], "usage": USAGE}}),
        KEEPALIVE,
        event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}}),
        event({"type": "content_block_stop", "index": 0}),
        event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
        event({"type": "message_stop"}),
    ]


class FakeCLIProxyAPI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list[dict[str, Any]] = []
    stream: list[bytes] | None = None  # override for the next /v1/messages stream
    gate: threading.Event | None = None  # when set, the stream waits on it after a keep-alive

    def log_message(self, *args):
        pass

    def _read(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeCLIProxyAPI.received.append({"method": self.command, "path": self.path,
                                         "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        return body

    def _cpa_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Cpa-Trace-Id", "trace-synthetic-1")

    def _json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self._cpa_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self) -> bool:
        query = parse_qs(urlsplit(self.path).query)
        bearer = self.headers.get("Authorization", "")
        bearer = bearer.split(" ", 1)[1].strip() if bearer.lower().startswith("bearer ") else bearer
        candidates = [bearer, self.headers.get("X-Goog-Api-Key", ""), self.headers.get("X-Api-Key", ""),
                      *query.get("key", []), *query.get("auth_token", [])]
        if not any(candidates):
            self._json(401, {"error": "Missing API key"})
            return False
        if CLIENT_KEY not in candidates:
            self._json(401, {"error": "Invalid API key"})
            return False
        return True

    def do_GET(self):
        self._read()
        path = urlsplit(self.path).path
        if path == "/v1/models":
            if self._authenticated():
                self._json(200, {"object": "list",
                                 "data": [{"id": m, "object": "model"} for m in sorted(SERVED_MODELS)]})
            return
        self._not_found()

    def do_POST(self):
        body = self._read()
        path = urlsplit(self.path).path
        if path not in ("/v1/messages", "/v1/messages/count_tokens", "/v1/responses",
                        "/backend-api/codex/responses"):
            self._not_found()
            return
        if not self._authenticated():
            return
        request = json.loads(body)
        model = request.get("model")
        if model == "rate-limited":
            self._json(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "quota"}},
                       {"Retry-After": "7"})
            return
        if model not in SERVED_MODELS:
            self._json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                        "message": "unknown provider for model"}})
            return
        if path == "/v1/messages/count_tokens":
            self._json(200, {"input_tokens": 41})
        elif path.endswith("/responses"):
            self._responses()
        elif not request.get("stream"):
            self._json(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                             "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "usage": USAGE})
        else:
            self._sse(FakeCLIProxyAPI.stream or text_stream())

    def _sse(self, events: list[bytes]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cpa_headers()
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for raw in events:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
            self.wfile.flush()
            if raw == KEEPALIVE and FakeCLIProxyAPI.gate is not None:
                FakeCLIProxyAPI.gate.wait(5)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _responses(self) -> None:
        usage = {"input_tokens": 30, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 2,
                 "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 32}
        self._sse(FakeCLIProxyAPI.stream or [
            event({"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}}),
            KEEPALIVE,
            event({"type": "response.output_item.added", "output_index": 0,
                   "item": {"type": "message", "role": "assistant"}}),
            event({"type": "response.output_text.delta", "output_index": 0, "delta": "OK"}),
            event({"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "usage": usage}}),
        ])

    def _not_found(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def cpa():
    FakeCLIProxyAPI.received = []
    FakeCLIProxyAPI.stream = None
    FakeCLIProxyAPI.gate = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCLIProxyAPI)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    if FakeCLIProxyAPI.gate is not None:
        FakeCLIProxyAPI.gate.set()
    server.shutdown()
    server.server_close()


@pytest.fixture
def contexto():
    """Start a proxy with the real rewrite engine, as ``contexto run`` does."""
    started = []

    def start(upstream: str) -> tuple[Any, MemoryStore]:
        store = MemoryStore("s1")
        proxy, engine = launcher.start_proxy(store, upstream, "flag", {"bound": 150_000}, False, stderr=io.StringIO())
        assert engine == "rewrite"
        started.append(proxy)
        return proxy, store

    yield start
    for proxy in started:
        proxy.stop()


def call(proxy, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response, raw


def requests_logged(store: MemoryStore, count: int = 1) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    while True:
        found = [e for e in store.events if e.get("event") == "request"]
        if len(found) >= count or time.monotonic() > deadline:
            return found
        time.sleep(0.01)


BEARER = {"Authorization": f"Bearer {CLIENT_KEY}", "anthropic-version": "2023-06-01"}


# Claude Code through Contexto to CLIProxyAPI ------------------------------------------------------

def test_claude_upstream_is_the_cliproxyapi_root_from_settings(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"}}))
    found = resolve_upstream(None, env={}, cwd=tmp_path, home=tmp_path / "home")
    assert (found.url, found.source) == ("http://127.0.0.1:8317", "settings:project")


@pytest.mark.parametrize("auth", [
    {"Authorization": f"Bearer {CLIENT_KEY}"},  # ANTHROPIC_AUTH_TOKEN, or apiKeyHelper
    {"x-api-key": CLIENT_KEY},  # ANTHROPIC_API_KEY
])
def test_messages_stream_reaches_cliproxyapi_with_the_client_key(cpa, contexto, auth):
    proxy, store = contexto(cpa)
    response, raw = call(proxy, "POST", "/v1/messages?beta=true", MESSAGES_REQUEST,
                         {**auth, "anthropic-version": "2023-06-01"})
    assert response.status == 200
    assert raw == b"".join(text_stream())  # keep-alive comments included, byte for byte
    sent = FakeCLIProxyAPI.received[-1]
    assert sent["path"] == "/v1/messages?beta=true"
    assert {k: sent["headers"].get(k.lower()) for k in auth} == auth  # the client key, unchanged
    assert sent["headers"]["accept-encoding"] == "identity"
    # CLIProxyAPI's own headers reach the CLI; its hop-by-hop Connection header does not.
    assert response.getheader("X-Cpa-Trace-Id") == "trace-synthetic-1"
    assert response.getheader("Access-Control-Allow-Origin") == "*"
    assert response.getheader("Connection") != "keep-alive"
    entry = requests_logged(store)[0]
    assert entry["status"] == 200 and entry["stop_reason"] == "end_turn"
    assert entry["usage"]["input_tokens"] == 40 and entry["usage"]["output_tokens"] == 2
    assert CLIENT_KEY not in json.dumps(store.events)


def test_non_streaming_messages_and_count_tokens(cpa, contexto):
    proxy, store = contexto(cpa)
    response, raw = call(proxy, "POST", "/v1/messages", {**MESSAGES_REQUEST, "stream": False}, BEARER)
    assert response.status == 200 and json.loads(raw)["usage"] == USAGE
    response, raw = call(proxy, "POST", "/v1/messages/count_tokens?beta=true",
                         {k: MESSAGES_REQUEST[k] for k in ("model", "system", "messages")}, BEARER)
    assert response.status == 200 and json.loads(raw) == {"input_tokens": 41}
    assert [r["path"] for r in FakeCLIProxyAPI.received] == ["/v1/messages", "/v1/messages/count_tokens?beta=true"]
    assert [e["status"] for e in requests_logged(store, 2)] == [200, 200]


@pytest.mark.parametrize("query", ["key", "auth_token"])
def test_query_key_and_models_list_pass_through(cpa, contexto, query):
    proxy, store = contexto(cpa)
    response, raw = call(proxy, "GET", f"/v1/models?{query}={CLIENT_KEY}")
    assert response.status == 200 and len(json.loads(raw)["data"]) == len(SERVED_MODELS)
    assert FakeCLIProxyAPI.received[-1]["path"] == f"/v1/models?{query}={CLIENT_KEY}"
    assert CLIENT_KEY not in json.dumps(requests_logged(store))  # the logged path drops the query


@pytest.mark.parametrize("headers,status,body", [
    ({}, 401, {"error": "Missing API key"}),
    ({"Authorization": "Bearer wrong"}, 401, {"error": "Invalid API key"}),
])
def test_cliproxyapi_auth_errors_reach_the_cli_unchanged(cpa, contexto, headers, status, body):
    proxy, store = contexto(cpa)
    response, raw = call(proxy, "POST", "/v1/messages", MESSAGES_REQUEST, headers)
    assert (response.status, json.loads(raw)) == (status, body)
    assert response.getheader("Content-Type") == "application/json; charset=utf-8"
    assert len(FakeCLIProxyAPI.received) == 1  # never retried
    assert requests_logged(store)[0]["status"] == status
    assert any(e.get("event") == "upstream_4xx" and e["status"] == status for e in store.events)


@pytest.mark.parametrize("model,status,header", [
    ("claude-unknown-9", 400, None),
    ("rate-limited", 429, ("Retry-After", "7")),
])
def test_routing_and_quota_errors_reach_the_cli_unchanged(cpa, contexto, model, status, header):
    proxy, _ = contexto(cpa)
    response, raw = call(proxy, "POST", "/v1/messages", {**MESSAGES_REQUEST, "model": model}, BEARER)
    assert response.status == status and json.loads(raw)["type"] == "error"
    if header:
        assert response.getheader(header[0]) == header[1]
    assert len(FakeCLIProxyAPI.received) == 1


def test_an_error_after_the_stream_started_is_relayed_and_logged(cpa, contexto):
    FakeCLIProxyAPI.stream = text_stream()[:4] + [
        event({"type": "error", "error": {"type": "api_error", "message": "upstream stream closed"}})]
    proxy, store = contexto(cpa)
    response, raw = call(proxy, "POST", "/v1/messages", MESSAGES_REQUEST, BEARER)
    assert response.status == 200 and raw == b"".join(FakeCLIProxyAPI.stream)
    entry = requests_logged(store)[0]
    assert entry["error_type"] == "api_error" and entry["usage"]["input_tokens"] == 40


def test_a_path_prefix_on_the_cliproxyapi_url_is_kept(cpa, contexto):
    proxy, _ = contexto(cpa + "/cpa")  # a deployment behind a reverse proxy at /cpa
    call(proxy, "POST", "/v1/messages", MESSAGES_REQUEST, BEARER)
    assert FakeCLIProxyAPI.received[-1]["path"] == "/cpa/v1/messages"


# Keep-alive comments during the stream hold ----------------------------------------------------------

def tool_stream() -> list[bytes]:
    return [
        event({"type": "message_start", "message": {"id": "msg_2", "type": "message", "role": "assistant",
                                                    "content": [], "usage": USAGE}}),
        event({"type": "content_block_start", "index": 0,
               "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": {}}}),
        event({"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"a\"}"}}),
        KEEPALIVE,
        event({"type": "content_block_stop", "index": 0}),
        event({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}}),
        event({"type": "message_stop"}),
    ]


def test_sse_is_comment():
    assert sse_is_comment(b": keep-alive\n\n") and sse_is_comment(b":\r\n\r\n") and sse_is_comment(b":a\n:b\n\n")
    assert not sse_is_comment(b"\n\n") and not sse_is_comment(b"")
    assert not sse_is_comment(b": note\ndata: {}\n\n") and not sse_is_comment(b"event: ping\ndata: {}\n\n")


def test_hold_passes_keepalive_comments_and_keeps_content_order():
    published = []
    hold = StreamHold("messages", lambda: published.append(True))
    out = [hold.feed(raw) for raw in tool_stream()]
    assert out[1] == [] and out[2] == []  # the tool call and its input are held
    assert out[3] == [KEEPALIVE]  # the comment is not content: it goes out at once
    assert out[5] == tool_stream()[1:3] + [tool_stream()[4], tool_stream()[5]] and published == [True]
    released = [raw for chunk in out for raw in chunk]
    assert [r for r in released if r != KEEPALIVE] == [r for r in tool_stream() if r != KEEPALIVE]


@pytest.mark.parametrize("tail", [b": keep-alive", b": keep-alive\r\n:"])
def test_an_unterminated_comment_tail_stays_held_and_last(tail):
    """A stream that ends on a comment without its line ending (reviewer-A): passing it at once and
    releasing the held events after it would glue the next event onto the comment line."""
    stream = b"".join(tool_stream()[:3]) + tail
    splitter = SSEEventSplitter()
    hold = StreamHold("messages", lambda: None)
    out = [raw for event in splitter.feed(stream) for raw in hold.feed(event)]
    assert b"tool_use" not in b"".join(out)
    out += hold.feed(splitter.flush())
    assert b"tool_use" not in b"".join(out)  # the tail is held behind the tool call, not passed
    out += hold.eof()
    assert b"".join(out) == stream  # the bytes upstream sent, in order


def test_a_terminated_comment_after_crlf_passes_during_the_hold():
    hold = StreamHold("messages", lambda: None)
    assert hold.feed(tool_stream()[1]) == []
    assert hold.feed(b": keep-alive\r\n\r\n") == [b": keep-alive\r\n\r\n"]


def test_keepalive_reaches_the_cli_while_a_tool_call_is_held(cpa, contexto):
    """CLIProxyAPI's heartbeat reaches the CLI during a hold, before the held tool call is released.
    The fake waits after its keep-alive until the client has read it; a held comment would time out."""
    FakeCLIProxyAPI.stream = tool_stream()
    FakeCLIProxyAPI.gate = gate = threading.Event()
    proxy, store = contexto(cpa)
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    body = json.dumps(MESSAGES_REQUEST).encode()
    head = (f"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            f"Authorization: Bearer {CLIENT_KEY}\r\nContent-Length: {len(body)}\r\n\r\n").encode()
    sock.sendall(head + body)
    got = b""
    while b"keep-alive\n\n" not in got.split(b"\r\n\r\n", 1)[-1]:
        chunk = sock.recv(65536)
        assert chunk, "connection closed before the keep-alive arrived"
        got += chunk
    assert b"tool_use" not in got  # the tool call is still held
    gate.set()
    while not got.endswith(b"0\r\n\r\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        got += chunk
    sock.close()
    assert b"tool_use" in got and got.index(b"keep-alive") < got.index(b"tool_use")
    entry = requests_logged(store)[0]
    assert entry["stream_hold"] == {"held_events": 3, "triggered": True}


# Codex through Contexto to CLIProxyAPI --------------------------------------------------------------

def test_codex_default_upstream_is_the_cliproxyapi_v1_path(cpa):
    assert resolve_codex_upstream(API_KEY_MODE, None, {}, cpa) == cpa + "/v1"
    assert resolve_codex_upstream(API_KEY_MODE, None, {}, cpa + "/") == cpa + "/v1"


def test_codex_upstream_source_names_where_the_url_came_from(cpa):
    assert codex_upstream_with_source(API_KEY_MODE, None, {}, cpa, "settings:local") == (
        cpa + "/v1", "claude-settings:local+/v1")
    assert codex_upstream_with_source(API_KEY_MODE, cpa + "/v1", {}, None) == (cpa + "/v1", "codex-flag")
    assert codex_upstream_with_source(API_KEY_MODE, None, {"CONTEXTO_CODEX_UPSTREAM": cpa + "/v1"}, cpa) == (
        cpa + "/v1", "codex-env")


def test_codex_api_key_mode_uses_the_key_helper_for_cliproxyapi(tmp_path):
    launch = plan_codex([], "http://127.0.0.1:1", "s1", key_helper="/bin/cat /keys/cpa-client-key",
                        upstream="http://127.0.0.1:8317/v1", env={}, cwd=tmp_path, home=tmp_path,
                        contexto_home=tmp_path / "cx")
    try:
        config = (launch.codex_home / "config.toml").read_text()
        assert launch.upstream == "http://127.0.0.1:8317/v1"
        assert 'command = "/bin/cat"' in config and 'args = ["/keys/cpa-client-key"]' in config
        assert "supports_websockets = false" in config
    finally:
        launch.cleanup()


def test_codex_responses_reach_cliproxyapi_v1_responses(cpa, contexto):
    proxy, store = contexto(resolve_codex_upstream(API_KEY_MODE, None, {}, cpa))
    # Codex's base_url is the proxy root, so it sends POST /responses; the upstream carries /v1.
    response, raw = call(proxy, "POST", "/responses", RESPONSES_REQUEST, {"Authorization": f"Bearer {CLIENT_KEY}"})
    assert response.status == 200 and KEEPALIVE in raw
    assert FakeCLIProxyAPI.received[-1]["path"] == "/v1/responses"
    assert FakeCLIProxyAPI.received[-1]["headers"]["authorization"] == f"Bearer {CLIENT_KEY}"
    entry = requests_logged(store)[0]
    assert entry["api"] == "responses" and entry["end_event"] == "completed"
    assert entry["usage"]["input_tokens"] == 30


def test_codex_failure_event_after_a_held_tool_call_is_relayed(cpa, contexto):
    failed = {"type": "response.failed", "sequence_number": 0,
              "response": {"status": "failed", "error": {"type": "server_error", "code": "internal_server_error",
                                                         "message": "internal error"}}}
    FakeCLIProxyAPI.stream = [
        event({"type": "response.created", "response": {"id": "resp_2", "status": "in_progress"}}),
        event({"type": "response.output_item.added", "output_index": 0,
               "item": {"type": "function_call", "name": "shell", "call_id": "call_1", "arguments": ""}}),
        KEEPALIVE,
        b"\nevent: response.failed\ndata: " + json.dumps(failed).encode() + b"\n\n",  # CLIProxyAPI's framing
    ]
    proxy, store = contexto(resolve_codex_upstream(API_KEY_MODE, None, {}, cpa))
    response, raw = call(proxy, "POST", "/responses", RESPONSES_REQUEST, {"Authorization": f"Bearer {CLIENT_KEY}"})
    assert response.status == 200
    assert sorted(raw.split(b"\n")) == sorted(b"".join(FakeCLIProxyAPI.stream).split(b"\n"))
    assert raw.index(b": keep-alive") < raw.index(b"function_call")  # passed during the hold
    entry = requests_logged(store)[0]
    assert entry["end_event"] == "failed" and entry["response_status"] == "failed"
    assert entry["error_code"] == "other"  # internal_server_error is not on the Responses allowlist
    assert entry["stream_hold"]["triggered"] is True


def test_codex_upstream_without_v1_gets_cliproxyapi_404(cpa, contexto):
    """The documented mistake: --codex-upstream without /v1 sends Codex to a path CLIProxyAPI lacks."""
    proxy, _ = contexto(cpa)
    response, raw = call(proxy, "POST", "/responses", RESPONSES_REQUEST, {"Authorization": f"Bearer {CLIENT_KEY}"})
    assert (response.status, raw) == (404, b"")
