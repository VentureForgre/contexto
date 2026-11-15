"""The recipes of ``docs/serve.md`` run as written, at zero spend (D70).

Each test takes its recipe out of ``docs/serve.md`` (the fenced block after a ``<!-- recipe: NAME -->``
comment), so the page and the test cannot drift, and runs it against a fake upstream on 127.0.0.1.
The fake answers as a model asked to pin would: the first request that offers tools gets a
``contexto_pin`` call naming the request's own Claude Code session id (after a ``ToolSearch`` call
that loads it when Claude Code offers it deferred), and every other request a line of text. A test
passes when every model request went through the service into the store of its own session id, and
each pin landed in that store and no other. The pin is written by ``contexto mcp --serve``, which
finds the session by asking the service about the call's tool-use id, so a pin in the right store
shows the tool call was bound to the right session.

- ``claude -p`` in CI: the ``run`` script of the GitHub Actions step, with the real ``claude``. The
  one change made to it is the upstream URL, pointed at the fake. Its install steps are not run.
- The Agent SDK: the Python program, with the real ``claude-agent-sdk`` and the Claude Code it bundles,
  twice (two conversations, two stores). Skipped unless the SDK is importable; to run it:
  ``uv run --with pytest --with claude-agent-sdk pytest tests/test_serve_recipes.py``.
- The VS Code extension: the setup commands (user settings merged, MCP server added at user scope),
  then the real ``claude -p`` with no ``--settings`` and no ``--mcp-config``, as the extension's own
  Claude Code starts. The extension itself is not run; ``docs/serve.md`` gives the steps to check it.

Nothing of the developer's setup is used: each run has its own ``HOME`` under ``tmp_path``, no
``CLAUDE_CONFIG_DIR``, a stub API key, ``DISABLE_TELEMETRY`` and
``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC``, and on macOS a sandbox profile that denies every
outbound connection except to localhost. Skipped when the ``claude`` CLI (or, for the SDK, the SDK)
is not installed, as in CI.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import serve
from contexto.store import SessionStore
from test_mcp_real_client import EXPECTED
from test_serve import Running, served, sessions
from test_serve_end_to_end import _blocks, _offered, _sse, _tool_results
from test_serve_real_client import CLAUDE, LOCAL_ONLY, SANDBOX

DOC = Path(__file__).resolve().parents[1] / "docs" / "serve.md"
PIN_TOOL = "mcp__contexto__contexto_pin"
SEARCH_TOOL = "ToolSearch"
REPLY = "stub reply through serve"
TIMEOUT_S = 240
HAS_SDK = importlib.util.find_spec("claude_agent_sdk") is not None
needs_claude = pytest.mark.skipif(CLAUDE is None, reason="claude CLI not installed")
needs_sdk = pytest.mark.skipif(not HAS_SDK, reason="claude-agent-sdk not installed (uv run --with claude-agent-sdk)")


def snippet(name: str) -> str:
    """The fenced block that follows ``<!-- recipe: NAME -->`` in docs/serve.md, exactly once."""
    found = re.findall(rf"^<!-- recipe: {re.escape(name)} -->\n+```[a-z]*\n(.*?)^```$", DOC.read_text(),
                       re.MULTILINE | re.DOTALL)
    assert len(found) == 1, f"docs/serve.md has {len(found)} recipe blocks named {name!r}"
    return found[0]


def workflow_step(workflow: str, name: str) -> tuple[dict[str, str], str]:
    """The ``env`` mapping and the ``run`` script of the step named ``name`` in a GitHub Actions
    workflow, read by indentation (only the plain ``key: value`` and ``run: |`` forms the recipe uses)."""
    lines = workflow.splitlines()
    (start,) = [i for i, line in enumerate(lines) if line.strip() == f"- name: {name}"]
    step_indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    env, script, i = {}, None, start + 1
    while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) >= step_indent):
        key = lines[i].strip()
        if key == "env:":
            i += 1
            while i < len(lines) and len(lines[i]) - len(lines[i].lstrip()) > step_indent:
                variable, _, value = lines[i].strip().partition(": ")
                env[variable] = value
                i += 1
            continue
        if key == "run: |":
            body = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > step_indent):
                body.append(lines[i])
                i += 1
            script = textwrap.dedent("\n".join(body)).strip("\n") + "\n"
            continue
        i += 1
    assert script is not None, f"step {name!r} has no run: | block"
    return env, script


class RecipeUpstream(BaseHTTPRequestHandler):
    """The Messages API as far as one pinning turn needs it, streamed as the Anthropic API streams it.
    ``seen`` holds one record per ``/v1/messages``; ``other`` every other request."""

    protocol_version = "HTTP/1.1"
    seen: list[dict] = []
    other: list[tuple[str, str]] = []
    headers_seen: list[dict[str, str]] = []
    issued = 0
    lock = threading.Lock()

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

    def _record_other(self) -> None:
        RecipeUpstream.other.append((self.command, self.path))
        RecipeUpstream.headers_seen.append(dict(self.headers))

    def do_HEAD(self) -> None:  # noqa: N802
        self._record_other()
        self._json(200, {})

    def do_GET(self) -> None:  # noqa: N802
        self._record_other()
        self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub"}})

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            request = json.loads(raw or b"{}")
        except ValueError:
            request = {}
        path = self.path.split("?")[0]
        if path != "/v1/messages":
            self._record_other()
            if path.endswith("/count_tokens"):
                return self._json(200, {"input_tokens": 100})
            return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub"}})
        RecipeUpstream.headers_seen.append(dict(self.headers))
        session = self.headers.get("X-Claude-Code-Session-Id")
        messages = [m for m in request.get("messages") or [] if isinstance(m, dict)]
        tools = sorted(t.get("name") for t in request.get("tools") or [] if isinstance(t, dict))
        called = [b.get("name") for m in messages if m.get("role") == "assistant" for b in _blocks(m, "tool_use")]
        block: dict = {"type": "text", "text": REPLY}
        if tools and PIN_TOOL not in called:
            if PIN_TOOL in tools:
                block = {"type": "tool_use", "name": PIN_TOOL,
                         "input": {"kind": "decision", "text": f"recipe pin for {session}"}}
            elif SEARCH_TOOL in tools and SEARCH_TOOL not in called:
                block = {"type": "tool_use", "name": SEARCH_TOOL,
                         "input": {"query": f"select:{PIN_TOOL}", "max_results": 1}}
        record = {"session": session, "agent": self.headers.get("User-Agent", ""), "tools": tools,
                  "offered": _offered(request, tools), "call": None, "tool_use_id": None,
                  "results": [b for m in messages[-1:] for b in _tool_results(m)]}
        if block["type"] == "tool_use":
            with RecipeUpstream.lock:
                RecipeUpstream.issued += 1
                block = {"type": "tool_use", "id": f"toolu_01RCP{RecipeUpstream.issued:04d}serveQ7mZp4LkTr", **block}
            record.update(call=block["name"], tool_use_id=block["id"])
        RecipeUpstream.seen.append(record)
        stop = "tool_use" if block["type"] == "tool_use" else "end_turn"
        message = {"id": f"msg_01RCP{len(RecipeUpstream.seen):04d}", "type": "message", "role": "assistant",
                   "content": [], "model": request.get("model", "m"), "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0}}
        if not request.get("stream"):
            return self._json(200, {**message, "content": [block], "stop_reason": stop})
        if block["type"] == "text":
            start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
        else:
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": start},
                  {"type": "content_block_delta", "index": 0, "delta": delta},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                   "usage": {"output_tokens": 10}},
                  {"type": "message_stop"}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for event in events:
            chunk = _sse(event)
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


@pytest.fixture
def fake_upstream():
    RecipeUpstream.seen, RecipeUpstream.other, RecipeUpstream.headers_seen, RecipeUpstream.issued = [], [], [], 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RecipeUpstream)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def user(tmp_path) -> dict:
    """A user's machine in miniature: a home, a working directory, and a ``bin`` on ``PATH`` with
    ``contexto`` and ``python3`` (this checkout's, by this interpreter) ahead of the real ``claude``."""
    home, work, bin_dir = tmp_path / "user-home", tmp_path / "work", tmp_path / "bin"
    for directory in (home, work, bin_dir):
        directory.mkdir()
    for name, argv in (("contexto", f'"{sys.executable}" -m contexto'), ("python3", f'"{sys.executable}"')):
        shim = bin_dir / name
        shim.write_text(f'#!/bin/sh\nexec {argv} "$@"\n')
        shim.chmod(0o755)
    path = [str(bin_dir)] + ([os.path.dirname(CLAUDE)] if CLAUDE else []) + ["/usr/bin", "/bin"]
    env = {"HOME": str(home), "PATH": os.pathsep.join(path), "TERM": "xterm-256color",
           "ANTHROPIC_API_KEY": "stub-key", "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    return {"home": home, "work": work, "env": env, "sandbox": tmp_path / "localonly.sb"}


def run(user: dict, argv: list[str], env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """``argv`` in the user's environment (plus ``env``), inside the local-only sandbox on macOS."""
    if sys.platform == "darwin" and os.path.exists(SANDBOX):
        user["sandbox"].write_text(LOCAL_ONLY)
        argv = [SANDBOX, "-f", str(user["sandbox"]), *argv]
    return subprocess.run(argv, cwd=cwd or user["work"], env={**user["env"], **(env or {})}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=TIMEOUT_S)


def pinned_in(home: Path, name: str) -> set[str]:
    return set(re.findall(r"recipe pin for ([0-9a-f-]+)", SessionStore.open_existing(home, name).state_text()))


def check_routed(home: Path, token: str, conversations: int, entrypoint: str) -> dict[str, str]:
    """What every recipe must show, from what the fake upstream saw and the stores on disk. Returns the
    Claude Code session id -> Contexto session mapping."""
    seen = RecipeUpstream.seen
    assert seen, "no model request reached the fake upstream"
    # The token was removed before anything was forwarded.
    assert not any(token in path for _, path in RecipeUpstream.other)
    assert not any(token in value for head in RecipeUpstream.headers_seen for value in head.values())
    # The client is the one the recipe is for: its User-Agent names the entrypoint.
    assert all(f", {entrypoint}" in r["agent"] for r in seen), sorted({r["agent"] for r in seen})
    # Every request that offers tools offers the five Contexto tools (in tools, or deferred).
    offering = [r for r in seen if r["tools"]]
    assert offering and all(r["offered"] == EXPECTED for r in offering), [sorted(EXPECTED - r["offered"])
                                                                          for r in offering]
    # One pin per conversation, each answered by a successful tool result.
    pins = [r for r in seen if r["call"] == PIN_TOOL]
    answered = {b["tool_use_id"]: b for r in seen for b in r["results"]}
    assert len({r["session"] for r in pins}) == len(pins) == conversations, pins
    assert all(r["tool_use_id"] in answered and not answered[r["tool_use_id"]].get("is_error") for r in pins), answered
    # Each conversation has its own store, which holds its requests and its pin, and no other store does.
    mapping = served(home)
    assert set(mapping) == {r["session"] for r in seen} and len(sessions(home)) == conversations, (mapping, seen)
    everywhere = {name: pinned_in(home, name) for name in sessions(home)}
    for r in pins:
        assert [name for name, found in everywhere.items() if r["session"] in found] == [mapping[r["session"]]], \
            everywhere
    return mapping


@needs_claude
def test_ci_recipe_runs_claude_p_through_serve_and_stops_it(tmp_path, fake_upstream, user):
    workflow = snippet("ci-workflow")
    step_env, script = workflow_step(workflow, "Run claude -p through contexto serve")
    assert step_env == {"ANTHROPIC_API_KEY": "${{ secrets.ANTHROPIC_API_KEY }}"}, step_env
    assert script.count("https://api.anthropic.com") == 1
    script_path = tmp_path / "step.sh"
    script_path.write_text(script.replace("https://api.anthropic.com", fake_upstream))
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    # GitHub runs a run: step with this shell; ANTHROPIC_API_KEY stands for the secret.
    done = run(user, ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script_path)],
               env={"RUNNER_TEMP": str(runner_temp)})
    log = (runner_temp / "contexto-serve.log").read_text()
    assert done.returncode == 0, (done.stdout[-600:], done.stderr[-600:], log[-600:])
    assert REPLY in done.stdout
    home = runner_temp / "contexto"
    token = (home / serve.TOKEN_FILE).read_text().strip()
    check_routed(home, token, conversations=1, entrypoint="sdk-cli")
    # The service was stopped by the step: it said it was listening, removed serve.json, and its lock is free.
    assert "contexto serve: listening on 127.0.0.1:" in log and token not in log + done.stdout + done.stderr
    assert not (home / serve.STATE_FILE).exists() and not serve.service_running(home)
    for name in ("serve-settings.json", "serve-mcp.json"):
        assert stat.S_IMODE((runner_temp / name).stat().st_mode) == 0o600
    print(f"\nCI recipe with {CLAUDE}: {len(RecipeUpstream.seen)} Messages requests, "
          f"{len(RecipeUpstream.other)} others; {len(sessions(home))} store")


@needs_sdk
def test_agent_sdk_recipe_reaches_serve_and_each_run_gets_its_own_store(tmp_path, fake_upstream, user):
    contexto_home = user["home"] / ".contexto"
    service = Running(contexto_home, "--upstream", fake_upstream, env={"HOME": str(user["home"])})
    try:
        files = run(user, ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", snippet("sdk-files")])
        assert files.returncode == 0, files.stderr
        program = user["work"] / "agent.py"
        program.write_text(snippet("sdk-program"))
        outcomes = [run(user, [sys.executable, str(program)]) for _ in range(2)]
    finally:
        stopped = service.stop()
    for done in outcomes:
        assert done.returncode == 0, (done.stdout[-600:], done.stderr[-600:])
        assert REPLY in done.stdout
    assert stopped == 0, service.stderr_lines
    check_routed(contexto_home, service.token, conversations=2, entrypoint="sdk-py")
    assert service.token not in "\n".join(service.stderr_lines)
    versions = sorted({m.group(1) for r in RecipeUpstream.seen for m in [re.match(r"claude-cli/(\S+)", r["agent"])]
                       if m})
    import claude_agent_sdk

    print(f"\nAgent SDK recipe with claude-agent-sdk {claude_agent_sdk.__version__}, Claude Code {versions}: "
          f"{len(RecipeUpstream.seen)} Messages requests; {len(sessions(contexto_home))} stores")


@needs_claude
def test_vscode_recipe_settings_route_a_plain_claude_through_serve(tmp_path, fake_upstream, user):
    contexto_home = user["home"] / ".contexto"
    # The user already has settings of their own, at the mode Claude Code often leaves (0644).
    claude_dir = user["home"] / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps({"env": {"KEEP_ME": "1"}, "permissions": {"allow": ["Bash(ls:*)"]},
                                    "theme": "dark"}))
    settings.chmod(0o644)
    service = Running(contexto_home, "--upstream", fake_upstream, env={"HOME": str(user["home"])})
    try:
        setup = run(user, ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", snippet("vscode-setup")])
        assert setup.returncode == 0, (setup.stdout[-600:], setup.stderr[-600:])
        written = json.loads(settings.read_text())
        printed = serve.settings_payload(service.port, service.token)
        # Merged: the service's env and allow rules added, the user's own kept, the file made 0600.
        assert written["env"] == {"KEEP_ME": "1", **printed["env"]}
        assert written["permissions"]["allow"] == ["Bash(ls:*)", *printed["permissions"]["allow"]]
        assert written["theme"] == "dark" and stat.S_IMODE(settings.stat().st_mode) == 0o600
        servers = json.loads((user["home"] / ".claude.json").read_text())["mcpServers"]
        assert servers == serve.mcp_config_payload(contexto_home)["mcpServers"]
        # Running the setup again changes nothing.
        again = run(user, ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", snippet("vscode-setup")])
        assert again.returncode == 0 and json.loads(settings.read_text()) == written, again.stderr[-600:]
        # Claude Code with nothing but the user's own configuration, as the extension starts it.
        done = run(user, [CLAUDE, "-p", "--output-format", "json", "pin the decision"])
    finally:
        stopped = service.stop()
    assert done.returncode == 0, (done.stdout[-600:], done.stderr[-600:])
    assert REPLY in done.stdout
    assert stopped == 0, service.stderr_lines
    check_routed(contexto_home, service.token, conversations=1, entrypoint="sdk-cli")
    print(f"\nVS Code recipe's settings with {CLAUDE}: {len(RecipeUpstream.seen)} Messages requests; "
          f"{len(sessions(contexto_home))} store")
