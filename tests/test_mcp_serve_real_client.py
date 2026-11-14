"""Zero-spend tests of ``contexto mcp --serve`` against the real Claude Code client (D70).

Claude Code spawns the server from ``--mcp-config`` and talks to a stub upstream on 127.0.0.1 that
never forwards. The first test has no Contexto service at all and passes when the five tools reach
the model request: the tools must list whatever the service does. The second has the stub answer
with a ``contexto_pin`` tool call and a fake service (the resolve contract of
``contexto.serveclient``, from ``test_mcp_serve``) that knows the call's ``tool_use`` id: it passes
when Claude Code hands the server that id, the pin lands in the session the service named, and no
other session is touched. The service itself is ``contexto serve``'s; these tests cover the MCP side.

No model is reached and no real credential or configuration is used: a temporary ``HOME`` and
``CLAUDE_CONFIG_DIR``, a stub key, ``--bare``, ``--strict-mcp-config`` and an explicit ``--settings``.

Skipped when the ``claude`` CLI is not installed (as in CI).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from contexto.store import SessionStore
from test_mcp_real_client import CLAUDE, EXPECTED
from test_mcp_serve import FakeService

pytestmark = pytest.mark.skipif(CLAUDE is None, reason="claude CLI not installed")
PIN_TOOL = "mcp__contexto__contexto_pin"
TOOL_USE_ID = "toolu_01D70serveQ7mZp4LkTr9VbYs"
PIN_TEXT = "pinned through contexto mcp --serve by the real client"


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class _Upstream(BaseHTTPRequestHandler):
    """A scripted Messages API. A request that offers ``contexto_pin`` and holds no tool result gets
    that tool call when ``call_pin`` is set, else a 401 (the client retries; the test stops it). A
    request that holds a tool result, or offers no tools (the session title), gets a line of text."""

    protocol_version = "HTTP/1.1"
    call_pin = False
    requests: list[dict] = []

    def log_message(self, *args) -> None:
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub upstream"}})

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            return self._json(200, {"input_tokens": 100})
        if not path.endswith("/v1/messages"):
            return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub upstream"}})
        _Upstream.requests.append(body)
        offered = any(isinstance(t, dict) and t.get("name") == PIN_TOOL for t in body.get("tools") or [])
        if offered and not _tool_results(body):
            if not _Upstream.call_pin:
                return self._json(401, {"type": "error", "error": {"type": "authentication_error",
                                                                  "message": "stub upstream"}})
            block = {"type": "tool_use", "id": TOOL_USE_ID, "name": PIN_TOOL,
                     "input": {"kind": "decision", "text": PIN_TEXT}}
            stop = "tool_use"
        else:
            block, stop = {"type": "text", "text": "stub reply"}, "end_turn"
        message = {"id": "msg_01D70serve", "type": "message", "role": "assistant", "model": body.get("model", "m"),
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0}}
        if not body.get("stream"):
            return self._json(200, {**message, "content": [block], "stop_reason": stop})
        if block["type"] == "text":
            start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
        else:
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        chunks = [_sse("message_start", {"type": "message_start", "message": message}),
                  _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": start}),
                  _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                  _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
                  _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                         "usage": {"output_tokens": 10}}),
                  _sse("message_stop", {"type": "message_stop"})]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def _tool_results(body: dict) -> list[dict]:
    return [b for m in body.get("messages") or [] if isinstance(m, dict) and isinstance(m.get("content"), list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


def _offering() -> list[dict]:
    return [b for b in list(_Upstream.requests) if b.get("tools")]


@pytest.fixture
def upstream():
    _Upstream.requests, _Upstream.call_pin = [], False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def run_claude(tmp_path, port: int, home, done, timeout: float = 90) -> tuple[int | None, str]:
    """Run ``claude -p`` with ``contexto mcp --serve --home HOME`` as its only MCP server until
    ``done()`` or its exit; return its exit status (None when it was stopped) and the end of stderr."""
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": {"contexto": {
        "command": sys.executable, "args": ["-m", "contexto", "mcp", "--serve", "--home", str(home)]}}}))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"},
                                    "permissions": {"allow": [PIN_TOOL]}}))
    for name in ("scratch", "claude-home", "config"):
        (tmp_path / name).mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENRIG_", "CLAUDE_CODE_", "CLAUDECODE", "ANTHROPIC_", "CONTEXTO_"))}
    env.update({"HOME": str(tmp_path / "claude-home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
                "ANTHROPIC_API_KEY": "sk-ant-fake-for-stub", "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"})
    proc = subprocess.Popen(
        [CLAUDE, "-p", "--bare", "--settings", str(settings), "--mcp-config", str(mcp_config),
         "--strict-mcp-config", "--model", "claude-haiku-5-5", "--output-format", "json", "pin the decision"],
        cwd=tmp_path / "scratch", env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None and not done():
        time.sleep(0.2)
    status = proc.poll()
    proc.terminate()
    try:
        _, stderr = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
    return status, stderr[-400:]


def test_real_claude_code_lists_the_tools_with_no_service_running(tmp_path, upstream):
    home = tmp_path / "home"  # no serve.json, no serve.token, no sessions: not even the directory
    status, stderr = run_claude(tmp_path, upstream.server_port, home, _offering)
    offering = _offering()
    assert offering, f"no model request reached the stub (exit {status}, stderr {stderr!r})"
    names = {t.get("name") for t in offering[0]["tools"]}
    assert EXPECTED <= names, f"missing: {sorted(EXPECTED - names)}"
    assert not home.exists()


def test_real_claude_code_sends_the_tool_use_id_and_the_pin_lands_in_the_named_session(tmp_path, upstream):
    home = tmp_path / "home"
    named, other = SessionStore(home, "named-by-the-service"), SessionStore(home, "another-conversation")
    service = FakeService(home)
    service.known[TOOL_USE_ID] = "named-by-the-service"
    _Upstream.call_pin = True
    try:
        status, stderr = run_claude(tmp_path, upstream.server_port, home,
                                    lambda: any(_tool_results(b) for b in list(_Upstream.requests)))
    finally:
        service.stop()
    asked = [json.loads(r["body"]) for r in service.received]
    assert asked == [{"tool_use_id": TOOL_USE_ID}], f"exit {status}, stderr {stderr!r}, service saw {asked}"
    (result,) = [b for body in _Upstream.requests for b in _tool_results(body)][:1]
    assert result["tool_use_id"] == TOOL_USE_ID and not result.get("is_error"), result
    state = named.state_text()
    # 2.1.322 gives the model the call's structuredContent, {"record_id": ...}, as the result
    (record_id,) = set(re.findall(r"rec_[0-9a-f]+", json.dumps(result["content"])))
    assert record_id in state
    assert PIN_TEXT in state and PIN_TEXT not in other.state_text()
    assert not (other.dir / "ledger.jsonl").exists()
