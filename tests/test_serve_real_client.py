"""``contexto serve`` with the real Claude Code client, at zero spend.

``claude -p`` gets the settings ``contexto serve --print-settings`` prints and nothing else of
Contexto's. Its requests must reach a stub upstream through the service with the token prefix
removed, and each Claude Code session id must get its own session store. The run never reaches a
model and never touches the user's Claude configuration: a temporary home and ``CLAUDE_CONFIG_DIR``,
a stub API key, a stub upstream on 127.0.0.1 that answers one streamed turn, and on macOS a sandbox
profile that denies every outbound connection except to localhost.

Skipped when the ``claude`` CLI is not installed (as in CI).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import serve
from contexto.hints import GATED_HINT_HEADERS
from contexto.upstream import Upstream
from test_serve import child_env, served, session_log, sessions

CLAUDE = shutil.which("claude")
pytestmark = pytest.mark.skipif(CLAUDE is None, reason="claude CLI not installed")
SANDBOX = "/usr/bin/sandbox-exec"
LOCAL_ONLY = ('(version 1)\n(allow default)\n(deny network-outbound)\n'
              '(allow network-outbound (remote ip "localhost:*"))\n')
FIRST = "0d700000-0000-4000-8000-000000000001"
SECOND = "0d700000-0000-4000-8000-000000000002"


def sse(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class Stub(BaseHTTPRequestHandler):
    """The Messages API as far as one ``claude -p`` turn needs it: a streamed text reply."""

    protocol_version = "HTTP/1.1"
    received: list[tuple[str, str, dict[str, str], dict]] = []

    def log_message(self, *args) -> None:
        pass

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self) -> None:  # noqa: N802
        Stub.received.append((self.command, self.path, dict(self.headers), {}))
        self._json(200, {})

    def do_GET(self) -> None:  # noqa: N802
        Stub.received.append((self.command, self.path, dict(self.headers), {}))
        self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub"}})

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            request = json.loads(raw or b"{}")
        except ValueError:
            request = {}
        Stub.received.append((self.command, self.path, dict(self.headers), request))
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            return self._json(200, {"input_tokens": 100})
        if not path.endswith("/v1/messages"):
            return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub"}})
        usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
        message = {"id": "msg_01D70serve", "type": "message", "role": "assistant", "content": [],
                   "model": request.get("model", "m"), "stop_reason": None, "stop_sequence": None, "usage": usage}
        text = {"type": "text", "text": "stub reply through serve"}
        if not request.get("stream"):
            return self._json(200, {**message, "content": [text], "stop_reason": "end_turn"})
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text["text"]}},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                   "usage": {"output_tokens": 10}},
                  {"type": "message_stop"}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for event in events:
            chunk = sse(event)
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def claude_print(tmp_path: Path, settings: Path, session_id: str, prompt: str) -> subprocess.CompletedProcess:
    """One ``claude -p`` in a scrubbed environment (nothing of the developer's Claude Code setup)."""
    user_home, config, work = tmp_path / "claude-home", tmp_path / "claude-config", tmp_path / "work"
    for directory in (user_home, config, work):
        directory.mkdir(exist_ok=True)
    env = {"HOME": str(user_home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm-256color",
           "CLAUDE_CONFIG_DIR": str(config), "ANTHROPIC_API_KEY": "stub-key", "DISABLE_AUTOUPDATER": "1",
           "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    argv = [CLAUDE, "-p", f"--settings={settings}", "--session-id", session_id, "--model", "claude-haiku-5-5",
            "--output-format", "json", prompt]
    if sys.platform == "darwin" and os.path.exists(SANDBOX):
        profile = tmp_path / "localonly.sb"
        profile.write_text(LOCAL_ONLY)
        argv = [SANDBOX, "-f", str(profile), *argv]
    return subprocess.run(argv, cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=180)


def test_claude_reaches_upstream_through_serve_and_each_session_id_gets_a_store(tmp_path):
    Stub.received = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=upstream.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    home = tmp_path / "home"
    stderr = io.StringIO()
    service = serve.ServeService(home, Upstream(f"http://127.0.0.1:{upstream.server_address[1]}", "flag"),
                                 serve.ensure_token(home), stderr=stderr)
    service.start(0)
    try:
        # The settings file exactly as the command prints it, kept in a 0600 file.
        printed = subprocess.run([sys.executable, "-m", "contexto", "serve", "--print-settings", "--port",
                                  str(service.port)], capture_output=True, text=True, env=child_env(home),
                                 cwd=tmp_path, timeout=120)
        assert printed.returncode == 0, printed.stderr
        settings = tmp_path / "serve-settings.json"
        settings.touch(mode=0o600)
        settings.write_text(printed.stdout)
        assert json.loads(printed.stdout)["env"]["ANTHROPIC_BASE_URL"] == serve.base_url(service.port, service.token)

        first = claude_print(tmp_path, settings, FIRST, "say hi")
        assert first.returncode == 0, (first.stdout[-400:], first.stderr[-400:])
        assert "stub reply through serve" in first.stdout
        after_first = len(Stub.received)
        second = claude_print(tmp_path, settings, SECOND, "say hi again")
        assert second.returncode == 0, (second.stdout[-400:], second.stderr[-400:])
    finally:
        service.stop()
        upstream.shutdown()
        upstream.server_close()

    # Every request reached the stub with the prefix removed: no token in any path or header.
    paths = sorted({(method, path) for method, path, _, _ in Stub.received})
    # With CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, 2.1.322 sends no HEAD /api/hello: Messages requests only.
    assert ("POST", "/v1/messages?beta=true") in paths, paths
    for _, path, head, _ in Stub.received:
        assert service.token not in path and not any(service.token in value for value in head.values())
        # D70: the printed settings turn the hint headers on; the service reads them and sends none on.
        assert not {name.lower() for name in head} & GATED_HINT_HEADERS, head
    posts = [(head, request) for method, path, head, request in Stub.received
             if method == "POST" and path.split("?")[0] == "/v1/messages"]
    ids = {head.get("X-Claude-Code-Session-Id") for head, _ in posts}
    assert ids == {FIRST, SECOND}
    # One store per Claude Code session id, each holding only its own requests.
    mapping = served(home)
    assert set(mapping) == {FIRST, SECOND} and mapping[FIRST] != mapping[SECOND] and len(sessions(home)) == 2
    for session_id in (FIRST, SECOND):
        logged = [e for e in session_log(home, mapping[session_id]) if e["event"] == "request"]
        sent = [request for head, request in posts if head.get("X-Claude-Code-Session-Id") == session_id]
        assert len(logged) == len(sent) >= 1
        assert all(e["path"] == "/v1/messages" and e["status"] == 200 for e in logged)
        assert any(e["transform_events"][-1].get("request_class") == "main" for e in logged), logged
    # A request with no session id (none here but a HEAD or a GET) is in the service log, never in a store.
    service_requests = [e for e in (json.loads(line) for line in (home / serve.LOG_FILE).read_text().splitlines())
                        if e["event"] == "request"]
    assert all(e["method"] in ("HEAD", "GET") for e in service_requests), service_requests
    assert not [e for e in (json.loads(line) for line in (home / serve.LOG_FILE).read_text().splitlines())
                if e["event"] in ("unrouted", "request_refused", "serve_error")]
    assert service.token not in stderr.getvalue()
    print(f"\nserve with claude {CLAUDE}: {len(Stub.received)} requests at the stub ({after_first} from the first "
          f"run): {sorted(set(paths))}; {len(posts)} Messages requests; {len(sessions(home))} stores; "
          f"{len(service_requests)} requests without a session id")
