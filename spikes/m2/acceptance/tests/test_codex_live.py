"""M2 A22-prep: codex_live.py, the live entry point for Codex step 1, exercised end to end against
127.0.0.1 stubs (zero spend). Integration tests need the codex CLI and the contexto executable."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import codex_budget, codex_driver, codex_live, script  # noqa: E402
from acceptance.codex_stub import CodexStub, reply_items, stream_events  # noqa: E402

KEY = "sk-synthetic-NOT-A-KEY-a22prep"  # what the test key helper prints; must never be recorded
CONTEXTO = shutil.which("contexto") or str(Path(sys.executable).parent / "contexto")
needs_codex = pytest.mark.skipif(shutil.which("codex") is None or not Path(CONTEXTO).exists(),
                                 reason="needs the codex CLI and the contexto executable (127.0.0.1 stubs only)")
ESTIMATE = codex_budget.staged(codex_budget.history_turns(64_000, 20_000), 64_000)["step1_h1"]


def clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q",
                                 "--allow-empty", "-m", "synthetic"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
    return repo


def helper(tmp_path: Path) -> str:
    path = tmp_path / "key-helper.sh"
    path.write_text(f"#!/bin/sh\necho {KEY}\n")
    path.chmod(0o700)
    return str(path)


def test_preflight_refuses_a_dirty_tree_or_no_commit(tmp_path):
    repo = clean_repo(tmp_path)
    head = codex_live.preflight(repo)
    assert len(head["commit"]) == 40 and len(head["tree"]) == 40
    (repo / "local.txt").write_text("synthetic")
    with pytest.raises(codex_live.LiveStop, match="local changes"):
        codex_live.preflight(repo)
    with pytest.raises(codex_live.LiveStop, match="not a checkout"):
        codex_live.preflight(tmp_path / "nowhere")


def test_codex_live_refuses_an_approval_below_the_estimate_and_a_dirty_tree(tmp_path):
    repo = clean_repo(tmp_path)
    args = [CONTEXTO, str(tmp_path / "out"), "--upstream", "http://127.0.0.1:9", "--key-helper", helper(tmp_path)]
    assert codex_live.main([*args, "--approved", str(ESTIMATE - 1)], repo=repo) == 2
    refused = json.loads((tmp_path / "out" / "codex-step1-record.json").read_text())["refused"]
    assert refused == f"--approved {ESTIMATE - 1:,} is below the step-1 worst case {ESTIMATE:,}"
    (repo / "local.txt").write_text("synthetic")
    assert codex_live.main([*args, "--approved", str(ESTIMATE)], repo=repo) == 2
    assert "local changes" in json.loads((tmp_path / "out" / "codex-step1-record.json").read_text())["refused"]


@pytest.mark.parametrize("entry,reason", [
    ({"status": 401}, "upstream answered 401 (auth)"), ({"status": 403}, "upstream answered 403 (auth)"),
    ({"status": 500}, "upstream answered 500"),
    ({"status": 200, "usage": {"input_tokens": 10, "output_tokens": 32_001}}, "output passed 32,000"),
])
def test_live_watch_stop_rules(tmp_path, entry, reason):
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps({"event": "request", "method": "POST", "path": "/responses", **entry}) + "\n")
    watch = codex_live.LiveWatch(log, approved=10**9, inflight=1)
    with pytest.raises(codex_live.LiveStop, match=reason.replace("(", r"\(").replace(")", r"\)")):
        watch()


class Upstream:
    """A 127.0.0.1 Responses upstream for one stop rule: every POST answers ``status``, or a normal reply
    whose usage is ``usage`` (to pass the output cap or the approval line)."""

    def __init__(self, status: int = 200, usage: dict | None = None):
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(200, b'{"models": []}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if outer.status != 200:
                    self._send(outer.status, b'{"error": {"message": "synthetic"}}')
                    return
                _, output = reply_items(body.get("input") or [], {}, "q")
                use = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 5}
                use.update(outer.usage or {})
                sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in stream_events("r", output, use))
                self._send(200, sse.encode(), "text/event-stream")

        self.status, self.usage = status, usage
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def run_live(tmp_path, upstream_url, approved=ESTIMATE):
    out = tmp_path / "out"
    code = codex_live.main([CONTEXTO, str(out), "--upstream", upstream_url, "--key-helper", helper(tmp_path),
                            "--approved", str(approved)], repo=clean_repo(tmp_path))
    return code, json.loads((out / "codex-step1-record.json").read_text())


@needs_codex
def test_full_step1_run_through_codex_live_against_the_stub(tmp_path):
    s = codex_budget.history_turns(64_000, 20_000)
    with CodexStub({p.id: p.answer for p in s.probes}, query="retention_days") as stub:
        code, rec = run_live(tmp_path, stub.url)
    assert code == 0 and rec["ran"]["turns_completed"] == rec["ran"]["turns"] == 21
    assert rec["acceptance_1"]["acceptance_1"] and rec["history_tokens"] >= rec["history_needed"]
    assert rec["acceptance_3"]["credit"]["acceptance_3"] and rec["acceptance_3"]["expected_turns"] == 21
    assert rec["spent"]["within_line"] and rec["max_output"] <= codex_live.OUTPUT_CAP
    assert rec["preflight"]["model"] == "gpt-6-astra" and rec["preflight"]["reasoning_effort"] == "low"
    assert "model_reasoning_effort=low" in rec["preflight"]["codex_command"]
    # The key never reaches a record, a log, Codex's files, or a command line.
    root = Path(rec["preflight"]["scratch"])
    assert KEY not in json.dumps(rec) and KEY not in json.dumps(rec["preflight"]["codex_command"])
    for path in root.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path.name


@needs_codex
@pytest.mark.parametrize("status,usage,reason", [
    (401, None, "upstream answered 401 (auth)"), (403, None, "upstream answered 403 (auth)"),
    (200, {"output_tokens": 40_000}, "output passed 32,000"),
    (200, {"input_tokens": ESTIMATE - 100_000}, "stopped by the spend watcher"),
])
def test_codex_live_stops_and_records_why(tmp_path, status, usage, reason):
    upstream = Upstream(status, usage)
    try:
        code, rec = run_live(tmp_path, upstream.url)
    finally:
        upstream.close()
    assert code == 1 and reason in rec["ran"]["stopped"] and rec["ran"]["turns_completed"] == 0
    assert KEY not in json.dumps(rec)
