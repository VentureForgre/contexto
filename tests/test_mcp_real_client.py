"""Zero-spend integration test against the real Claude Code client.

Claude Code spawns the MCP server from ``--mcp-config``, lists its tools, and sends its first model
request to a local stub upstream, which records the request and answers 401 without forwarding.
The test passes when all five contexto tools appear in that request's ``tools``. It never reaches a
model, never uses a real credential, and never touches the user's Claude configuration (a temporary
config directory, ``--bare``, ``--strict-mcp-config``, and an explicit ``--settings`` file).

Skipped when the ``claude`` CLI is not installed (as in CI).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

CLAUDE = shutil.which("claude")
pytestmark = pytest.mark.skipif(CLAUDE is None, reason="claude CLI not installed")
EXPECTED = {"mcp__contexto__contexto_recall", "mcp__contexto__contexto_pin", "mcp__contexto__contexto_close",
            "mcp__contexto__contexto_state", "mcp__contexto__contexto_search"}


class _Stub(BaseHTTPRequestHandler):
    bodies: list[dict] = []

    def _answer(self) -> None:
        body = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "stub upstream"}})
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            _Stub.bodies.append({"path": self.path, "json": json.loads(raw or b"{}")})
        except json.JSONDecodeError:
            _Stub.bodies.append({"path": self.path, "json": {}})
        self._answer()

    def do_GET(self) -> None:  # noqa: N802
        self._answer()

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


def _captured() -> list[dict]:
    return [b["json"] for b in list(_Stub.bodies) if b["path"].startswith("/v1/messages") and "tools" in b["json"]]


SERVER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {tests!r})
    from mcp_fakes import FakeStore, load_mcp
    sys.exit(load_mcp().serve_stdio(FakeStore()))
""")


def test_real_claude_code_lists_all_five_contexto_tools(tmp_path):
    _Stub.bodies = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        server_script = tmp_path / "serve.py"
        server_script.write_text(SERVER.format(tests=str(Path(__file__).resolve().parent)))
        mcp_config = tmp_path / "mcp.json"
        mcp_config.write_text(json.dumps({"mcpServers": {"contexto": {"command": sys.executable,
                                                                      "args": [str(server_script)]}}}))
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{httpd.server_port}"}}))
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OPENRIG_", "CLAUDE_CODE_", "CLAUDECODE", "ANTHROPIC_"))}
        env.update({"CLAUDE_CONFIG_DIR": str(tmp_path / "config"), "ANTHROPIC_API_KEY": "sk-ant-fake-for-stub",
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{httpd.server_port}"})
        proc = subprocess.Popen(
            [CLAUDE, "-p", "--bare", "--settings", str(settings), "--mcp-config", str(mcp_config),
             "--strict-mcp-config", "--model", "claude-haiku-5-5", "--output-format", "json", "say hi"],
            cwd=scratch, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # The client retries a 401; stop it once a model request carrying tools has been captured.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and proc.poll() is None and not _captured():
            time.sleep(0.2)
        proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
    finally:
        httpd.shutdown()
    messages = _captured()
    assert messages, f"no model request reached the stub (exit {proc.returncode}, stderr {stderr[-400:]!r})"
    names = {t.get("name") for t in messages[0]["tools"]}
    assert EXPECTED <= names, f"missing: {sorted(EXPECTED - names)}"
