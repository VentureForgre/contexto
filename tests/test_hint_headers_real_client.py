"""Zero-spend proof with the real Claude Code binary (D70): through ``contexto run claude`` the upstream
receives no gated hint header, and the session log's request events carry the request class.

The real ``claude -p`` runs under the real launcher against a fake upstream on 127.0.0.1 that answers
one streamed turn and records every request's headers. It never reaches a model and never uses a real
credential or the user's Claude configuration: a temporary HOME, ``CLAUDE_CONFIG_DIR`` and
``CONTEXTO_HOME``, a fake key, and the fake upstream given as ``--upstream``.

Skipped when the ``claude`` CLI is not installed (as in CI).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto.hints import GATED_HINT_HEADERS, HINT_HEADERS_KEY, HINT_MARKER_KEY, REQUEST_CLASS_HEADER, REQUEST_CLASSES
from contexto.launcher import settings_payload
from contexto.upstream import managed_settings_path

CLAUDE = shutil.which("claude")
# Read at import, before conftest points the table at a temporary directory: the launcher these tests
# start as a subprocess reads this machine's real managed settings file (D70).
_MACHINE_MANAGED = managed_settings_path()
pytestmark = [pytest.mark.skipif(CLAUDE is None, reason="claude CLI not installed"),
              pytest.mark.skipif(_MACHINE_MANAGED is not None and _MACHINE_MANAGED.exists(),
                                 reason="this machine has a Claude Code managed settings file")]


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _texts(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def reply(body: dict) -> tuple[list[dict], str]:
    """The fake upstream's answer, as the stub of spikes/m2/serve/http_probe.py scripts it: a prompt
    holding MAIN-AGENT gets an Agent call whose prompt holds SUB-TASK, the summarization request gets a
    ``<summary>``, and everything else a short text."""
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    tools = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
    first = next((_texts(m) for m in messages if m.get("role") == "user"), "")
    has_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else []))
    if not tools or (messages and "Respond with TEXT ONLY" in _texts(messages[-1])):
        return [{"type": "text", "text": "<summary>fake summary of the conversation so far</summary>"}], "end_turn"
    if "MAIN-AGENT" in first and "SUB-TASK" not in first and not has_result and tools & {"Agent", "Task"}:
        name = "Agent" if "Agent" in tools else "Task"
        return [{"type": "tool_use", "id": "toolu_main_agent_1", "name": name,
                 "input": {"description": "the agent", "prompt": "SUB-TASK do the work",
                           "subagent_type": "general-purpose"}}], "tool_use"
    return [{"type": "text", "text": "hi from the fake upstream"}], "end_turn"


class _Upstream(BaseHTTPRequestHandler):
    """Answers a Messages request with one short turn (streamed when asked; ``reply``) and keeps each
    request's method, path and header names and values."""

    protocol_version = "HTTP/1.1"
    seen: list[tuple[str, str, dict[str, str]]] = []

    def log_message(self, *args) -> None:
        pass

    def _record(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        _Upstream.seen.append((self.command, self.path.split("?")[0],
                               {name.lower(): value for name, value in self.headers.items()}))
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _json(self, status: int, payload: dict, head: bool = False) -> None:
        out = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0" if head else str(len(out)))
        self.end_headers()
        if not head:
            self.wfile.write(out)

    def do_HEAD(self) -> None:  # noqa: N802
        self._record()
        self._json(200, {}, head=True)

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "fake upstream"}})

    def do_POST(self) -> None:  # noqa: N802
        body = self._record()
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            self._json(200, {"input_tokens": 100})
            return
        if not path.endswith("/v1/messages"):
            self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "fake upstream"}})
            return
        usage = {"input_tokens": 100, "output_tokens": 5, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
        message = {"id": "msg_fake_1", "type": "message", "role": "assistant", "model": body.get("model", "m"),
                   "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
        blocks, stop = reply(body)
        if not body.get("stream"):
            self._json(200, {**message, "content": blocks, "stop_reason": stop})
            return
        chunks = [sse("message_start", {"type": "message_start", "message": message})]
        for i, block in enumerate(blocks):
            if block["type"] == "text":
                start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
            else:
                start = {**block, "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            chunks += [sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}),
                       sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}),
                       sse("content_block_stop", {"type": "content_block_stop", "index": i})]
        chunks += [sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                         "usage": {"output_tokens": 5}}),
                   sse("message_stop", {"type": "message_stop"})]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        self.wfile.write(b"0\r\n\r\n")


def settled(quiet: float = 4.0, limit: float = 90.0) -> None:
    """Wait until the fake upstream has had no new request for ``quiet`` seconds."""
    deadline, count, since = time.monotonic() + limit, len(_Upstream.seen), time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if len(_Upstream.seen) != count:
            count, since = len(_Upstream.seen), time.monotonic()
        elif count and time.monotonic() - since >= quiet:
            return


def run_claude_through_contexto(tmp_path: Path, extra_env: dict[str, str],
                                prompts: list[str] | None = None) -> tuple[list, list[dict], str]:
    """``contexto run claude -p`` with the real binary against the fake upstream: one prompt, or with
    ``prompts`` a headless session fed one user message at a time (``--input-format stream-json``).
    Returns what the upstream saw, the session log, and claude's output."""
    _Upstream.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        for name in ("home", "config", "scratch"):
            (tmp_path / name).mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OPENRIG_", "RIGGED_", "CLAUDE", "ANTHROPIC_", "CONTEXTO_", "AWS_"))}
        env.update({"HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
                    "CONTEXTO_HOME": str(tmp_path / "contexto"), "ANTHROPIC_API_KEY": "sk-ant-fake-for-stub",
                    "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", **extra_env})
        argv = [sys.executable, "-m", "contexto", "run", "--upstream", f"http://127.0.0.1:{httpd.server_port}",
                "--session", "real", "claude", "-p", "--model", "claude-haiku-5-5"]
        if prompts is None:
            proc = subprocess.run([*argv, "--output-format", "json", "say hi"], cwd=tmp_path / "scratch", env=env,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        else:
            proc = subprocess.Popen([*argv, "--input-format", "stream-json", "--output-format", "stream-json",
                                     "--verbose", "--dangerously-skip-permissions"], cwd=tmp_path / "scratch",
                                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True)
            try:
                for prompt in prompts:
                    line = {"type": "user", "message": {"role": "user", "content": prompt}}
                    proc.stdin.write(json.dumps(line) + "\n")
                    proc.stdin.flush()
                    settled()
                out, err = proc.communicate(timeout=60)  # closes stdin: the session ends
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
            code = proc.returncode
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert code == 0, f"exit {code}, stderr {err[-600:]!r}"
    if prompts is None:
        assert json.loads(out)["result"] == "hi from the fake upstream"
    log = tmp_path / "contexto" / "sessions" / "real" / "log.jsonl"
    return list(_Upstream.seen), [json.loads(line) for line in log.read_text().splitlines()], out + err


def model_requests(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event"] == "request" and e["path"] == "/v1/messages"]


def test_real_claude_code_through_contexto_sends_the_upstream_no_gated_hint_header(tmp_path):
    seen, events, _ = run_claude_through_contexto(tmp_path, {})
    posts = [headers for method, path, headers in seen if method == "POST" and path == "/v1/messages"]
    assert posts, f"no model request reached the fake upstream: {[(m, p) for m, p, _ in seen]}"
    for method, path, headers in seen:
        assert not set(headers) & GATED_HINT_HEADERS, (method, path, sorted(set(headers) & GATED_HINT_HEADERS))
    assert all("x-claude-code-session-id" in headers for headers in posts)  # the ungated header is forwarded
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "stripped"
    # Claude Code did send the hints to the proxy: every model request's engine event has its class.
    logged = model_requests(events)
    assert len(logged) == len(posts)
    classes = [e["transform_events"][-1].get("request_class") for e in logged]
    assert set(classes) <= REQUEST_CLASSES and "main" in classes, classes
    main = next(e for e in logged if e["transform_events"][-1].get("request_class") == "main")
    assert main["transform_events"][-1]["main"] is True
    assert not [e for e in events if e["event"] in ("transform_error", "proxy_error")]


def test_real_claude_code_hints_the_user_turned_on_reach_the_upstream(tmp_path):
    seen, events, _ = run_claude_through_contexto(tmp_path, {HINT_HEADERS_KEY: "1"})
    posts = [headers for method, path, headers in seen if method == "POST" and path == "/v1/messages"]
    assert posts and all(headers.get(REQUEST_CLASS_HEADER) in REQUEST_CLASSES for headers in posts)
    assert "main" in [headers[REQUEST_CLASS_HEADER] for headers in posts]
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "forwarded"
    classes = [e["transform_events"][-1].get("request_class") for e in model_requests(events)]
    assert classes == [headers[REQUEST_CLASS_HEADER] for headers in posts]


def nested_env(outer_decision: str) -> dict[str, str]:
    """What a process started inside an outer ``contexto run claude`` session inherits from it: Claude
    Code applies the launcher's ``--settings`` ``env`` block to its own environment (measured on
    2.1.322: a Bash command of the session printed ``CLAUDE_CODE_GATEWAY_HINT_HEADERS=1``). The base
    URL is left out; ``--upstream`` stands in for one the user configured."""
    block = settings_payload("http://127.0.0.1:1", outer_decision)["env"]
    return {name: value for name, value in block.items() if name != "ANTHROPIC_BASE_URL"}


def test_real_claude_code_nested_under_a_launch_that_strips_sends_the_upstream_no_gated_hint_header(tmp_path):
    """Review round 2: the nested launch inherits Contexto's own ``1`` with the marker ``stripped``, and
    must not take it for the user's."""
    inherited = nested_env("stripped")
    assert inherited == {HINT_HEADERS_KEY: "1", HINT_MARKER_KEY: "stripped"}
    seen, events, _ = run_claude_through_contexto(tmp_path, inherited)
    posts = [headers for method, path, headers in seen if method == "POST" and path == "/v1/messages"]
    assert posts
    for method, path, headers in seen:
        assert not set(headers) & GATED_HINT_HEADERS, (method, path, sorted(set(headers) & GATED_HINT_HEADERS))
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "stripped"
    classes = [e["transform_events"][-1].get("request_class") for e in model_requests(events)]
    assert "main" in classes  # the engine still got the hints


def test_real_claude_code_nested_under_a_launch_that_forwards_sends_them_on(tmp_path):
    seen, events, _ = run_claude_through_contexto(tmp_path, nested_env("forwarded"))
    posts = [headers for method, path, headers in seen if method == "POST" and path == "/v1/messages"]
    assert posts and all(headers.get(REQUEST_CLASS_HEADER) in REQUEST_CLASSES for headers in posts)
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "forwarded"


def test_real_claude_code_with_the_hints_turned_off_sends_none_and_the_engine_reads_its_signatures(tmp_path):
    """An explicit off: the launcher leaves the variable out, the real Claude Code sends no hint
    header, and the main request is still found by its tool list."""
    seen, events, _ = run_claude_through_contexto(tmp_path, {HINT_HEADERS_KEY: "0"})
    posts = [headers for method, path, headers in seen if method == "POST" and path == "/v1/messages"]
    assert posts and all(not set(headers) & GATED_HINT_HEADERS for _, _, headers in seen)
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "off"
    last = [e["transform_events"][-1] for e in model_requests(events)]
    assert all("request_class" not in e for e in last) and any(e["main"] is True for e in last)
    assert not [e for e in events if e["event"] in ("transform_error", "proxy_error")]


def test_real_claude_code_subagent_and_compaction_are_classed_and_agree_with_the_signatures(tmp_path):
    """A headless session: a prompt that launches a subagent, ``/compact``, one more prompt. Every
    request's class is in the log, the first main request after the compaction carries
    ``context-compacted`` and takes main over, and no header disagrees with a signature. A
    ``hint_mismatch`` here means a signature drifted in the installed Claude Code (docs/cli-updates.md)."""
    prompts = ["MAIN-AGENT go", "/compact", "hello after compact"]
    seen, events, output = run_claude_through_contexto(tmp_path, {}, prompts)
    assert all(not set(headers) & GATED_HINT_HEADERS for _, _, headers in seen)
    engine_events = [e["transform_events"] for e in model_requests(events)]
    last = [found[-1] for found in engine_events]
    classes = [e.get("request_class") for e in last]
    assert {"main", "subagent", "compaction"} <= set(classes), (classes, output[-600:])
    assert all(e["main"] is (e["request_class"] != "subagent") for e in last), last
    assert {e["compaction"] for e in last if e["request_class"] == "compaction"} == {"manual"}
    compacted = [i for i, e in enumerate(last) if e.get("context_compacted")]
    assert len(compacted) == 1 and compacted[0] > classes.index("compaction")  # once, after the summary request
    after = engine_events[compacted[0]]
    assert last[compacted[0]]["context_compacted"] == "manual" and last[compacted[0]]["request_class"] == "main"
    assert [e["reason"] for e in after if e["event"] == "main_changed"] == ["compacted"]
    assert not [e for found in engine_events for e in found if e["event"] == "hint_mismatch"]
    # The subagent's request reached the upstream with its agent id, which is not a gated header.
    assert sum("x-claude-code-agent-id" in headers for _, _, headers in seen) == classes.count("subagent")
    assert not [e for e in events if e["event"] in ("transform_error", "proxy_error")]
