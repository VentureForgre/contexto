"""M2 A22-prep: codex_live.py, the live entry point for Codex step 1, exercised end to end against
127.0.0.1 stubs (zero spend). Integration tests need the codex CLI and the contexto executable."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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


@pytest.fixture(autouse=True)
def _scratch_in_tmp(tmp_path, monkeypatch):
    # codex_live makes its scratch with mkdtemp; keep the tests' scratch under pytest's tmp_path.
    (tmp_path / "t").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "t"))


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
    # A23's fields, as builder-1's proxy logs them on a Responses request entry.
    ({"status": 200, "end_event": "incomplete", "response_status": "incomplete",
      "incomplete_reason": "max_output_tokens"}, "a response ended with end_event incomplete (max_output_tokens)"),
    ({"status": 200, "end_event": "none"}, "a response ended with end_event none"),
    ({"status": 200, "end_event": "failed", "error_code": "server_error"},
     "a response ended with end_event failed (server_error)"),
    ({"status": 200, "response_status": "failed"}, "a response ended with response_status failed"),
])
def test_live_watch_stop_rules(tmp_path, entry, reason):
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps({"event": "request", "method": "POST", "path": "/responses", **entry}) + "\n")
    watch = codex_live.LiveWatch(log, approved=10**9, inflight=1)
    with pytest.raises(codex_live.LiveStop, match=reason.replace("(", r"\(").replace(")", r"\)")):
        watch()


REFUSAL_PART = {"type": "refusal", "refusal": "synthetic"}


def refusal_events(use: dict, parts: list[dict]) -> list[dict]:
    """A 200 stream whose only output is one message with ``parts``, a refusal part last."""
    message = {"type": "message", "role": "assistant", "content": parts}
    last = len(parts) - 1
    return [{"type": "response.created", "response": {"id": "r", "status": "in_progress"}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {**message, "content": [], "status": "in_progress"}},
            *[{"type": "response.output_text.delta", "output_index": 0, "content_index": i, "delta": p["text"]}
              for i, p in enumerate(parts) if p["type"] == "output_text"],
            {"type": "response.content_part.added", "output_index": 0, "content_index": last,
             "part": {"type": "refusal", "refusal": ""}},
            {"type": "response.refusal.delta", "output_index": 0, "content_index": last, "delta": "synthetic"},
            {"type": "response.refusal.done", "output_index": 0, "content_index": last, "refusal": "synthetic"},
            {"type": "response.content_part.done", "output_index": 0, "content_index": last, "part": REFUSAL_PART},
            {"type": "response.output_item.done", "output_index": 0, "item": {**message, "status": "completed"}},
            {"type": "response.completed",
             "response": {"id": "r", "status": "completed", "output": [message],
                          "usage": use}}]


def test_live_watch_passes_a_completed_end_and_entries_without_a23_fields(tmp_path):
    log = tmp_path / "log.jsonl"
    entries = [{"status": 200, "end_event": "completed", "response_status": "completed"}, {"status": 200}]
    log.write_text("".join(json.dumps({"event": "request", "method": "POST", "path": "/responses", **e}) + "\n"
                           for e in entries))
    assert codex_live.LiveWatch(log, approved=10**9, inflight=1)() is False


def test_outside_hosts_says_what_it_scanned_and_reads_codex_sqlite_logs(tmp_path):
    home = tmp_path / "codex-home"
    (home / "log").mkdir(parents=True)
    (home / "log" / "codex-tui.log").write_text("GET https://127.0.0.1:5/v1 and https://example.invalid/x\n")
    (home / "plugin-LICENSE.txt").write_text("https://not-scanned.invalid\n")
    db = sqlite3.connect(home / "logs_2.sqlite")
    db.execute("CREATE TABLE logs (ts INTEGER, message TEXT)")
    db.executemany("INSERT INTO logs VALUES (?, ?)", [(1, "connect https://other.invalid/v1"), (2, "idle")])
    db.commit()
    db.close()
    out = codex_live.outside_hosts(home, "127.0.0.1")
    assert out["hosts"] == ["example.invalid", "other.invalid"]
    assert (out["files_scanned"], out["sqlite_rows_scanned"]) == (1, 2)
    assert out["label"] == ("hosts named in Codex's local logs: ['example.invalid', 'other.invalid'], "
                            "1 files and 2 rows scanned; not a network observation")


def test_remote_addresses_reads_only_the_remote_end_of_each_connection():
    lsof = ("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            "codex 1 u 9u IPv4 0x1 0t0 TCP 127.0.0.1:50000->127.0.0.1:8080 (ESTABLISHED)\n"
            "codex 1 u 10u IPv6 0x2 0t0 TCP [::1]:50001->[::1]:8080 (ESTABLISHED)\n"
            "node 2 u 11u IPv4 0x3 0t0 TCP 10.0.0.2:50002->203.0.113.7:443 (SYN_SENT)\n"
            "node 2 u 12u IPv4 0x4 0t0 TCP 127.0.0.1:7000 (LISTEN)\n")
    assert codex_live.remote_addresses(lsof) == {"codex 127.0.0.1:8080", "codex [::1]:8080", "node 203.0.113.7:443"}


def test_the_network_sampler_fails_open_and_says_it_could_not_run(tmp_path, monkeypatch):
    def no_lsof(*a, **kw):
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(codex_live.subprocess, "run", no_lsof)
    turns = codex_live.LiveTurns(CONTEXTO, "s", 64_000, "http://127.0.0.1:9", "helper", tmp_path, {})
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    turns._sample(proc)  # returns at once on the error, never raises
    proc.wait()
    assert turns.network == {"remote": set(), "samples": 0, "errors": ["FileNotFoundError"]}


class Upstream:
    """A 127.0.0.1 Responses upstream for one stop rule: every POST answers ``status``, a normal reply
    whose usage is ``usage`` (to pass the output cap or the approval line), or a 200 refusal."""

    def __init__(self, status: int = 200, usage: dict | None = None, refuse: list[dict] | None = None,
                 incomplete: str | None = None):
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
                use["total_tokens"] = use["input_tokens"] + use["output_tokens"]
                events = refusal_events(use, outer.refuse) if outer.refuse else stream_events("r", output, use)
                if outer.incomplete:  # the stream ends response.incomplete with this reason
                    end = events[-1]["response"]
                    events[-1] = {"type": "response.incomplete", "response": {
                        **end, "status": "incomplete", "incomplete_details": {"reason": outer.incomplete}}}
                sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
                self._send(200, sse.encode(), "text/event-stream")

        self.status, self.usage, self.refuse, self.incomplete = status, usage, refuse, incomplete
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
    assert rec["preflight"]["overrun_bound"] == rec["overrun_bound"] == codex_live.OVERRUN
    hosts, net = rec["outside_hosts"], rec["network_sample"]
    assert hosts["hosts"] == [] and hosts["label"].endswith("not a network observation")
    assert net["label"] == "sampled every 1s; short connections can be missed" and net["could_not_run"] == []
    # Codex itself may reach github.com (plugin sync) and chatgpt.com (analytics); the proxy and the engine
    # (the contexto and Python processes) make no outside connection.
    assert net["samples"] > 0 and not [r for r in net["outside"] if r.startswith(("contexto", "python", "Python"))]
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
    assert "failed closed" not in rec["ran"]["stopped"]
    assert KEY not in json.dumps(rec)


@needs_codex
@pytest.mark.parametrize("parts", [[REFUSAL_PART], [{"type": "output_text", "text": "OK"}, REFUSAL_PART]])
def test_a_refusal_that_arrives_as_a_200_stops_the_run(tmp_path, parts):
    # Codex 0.154.0 drops a message with a refusal part, alone or after text: the turn completes with no
    # agent_message, and that is the stop.
    upstream = Upstream(refuse=parts)
    try:
        code, rec = run_live(tmp_path, upstream.url)
    finally:
        upstream.close()
    assert code == 1 and rec["ran"]["stopped"] == "turn 1: turn 1 ended with no assistant message"
    assert rec["ran"]["turns_completed"] == 0 and KEY not in json.dumps(rec)


def test_refusal_parts_in_the_rollout_are_counted_by_type(tmp_path):
    rollout = tmp_path / "sessions" / "rollout-x.jsonl"
    rollout.parent.mkdir()
    items = [{"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": parts}}
             for parts in ([{"type": "output_text"}], [{"type": "refusal"}, {"type": "output_text"}],
                           [{"type": "refusal"}])]
    rollout.write_text("".join(json.dumps(i) + "\n" for i in [{"type": "session_meta"}, *items]))
    assert codex_live.refusal_parts(tmp_path) == 2 and codex_live.refusal_parts(tmp_path / "none") == 0


def test_a_refusal_part_in_the_rollout_stops_the_turn(tmp_path):
    scratch, home = tmp_path / "scratch", tmp_path / "home"
    scratch.mkdir()
    (home / "sessions").mkdir(parents=True)

    class Done:
        pid, returncode, stdin = 0, 0, None

        def __init__(self, cmd, stdout, **kw):
            stdout.write(json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}) + "\n")
            (home / "sessions" / "rollout-x.jsonl").write_text(json.dumps(
                {"type": "response_item", "payload": {"type": "message", "content": [{"type": "refusal"}]}}) + "\n")
            self.stdin = type("S", (), {"write": lambda *a: None, "close": lambda *a: None})()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    turns = codex_live.LiveTurns(CONTEXTO, "s", 64_000, "http://127.0.0.1:9", "helper", scratch, {}, popen=Done)
    turns.codex_home = home
    with pytest.raises(codex_live.LiveStop, match="a refusal content part on turn 1"):
        turns.send(0, "synthetic")


@needs_codex
def test_a_response_that_ends_incomplete_through_the_real_proxy_stops_the_run(tmp_path):
    # A23: the proxy logs end_event, response_status and incomplete_reason; codex_live stops on them.
    upstream = Upstream(incomplete="content_filter")
    try:
        code, rec = run_live(tmp_path, upstream.url)
    finally:
        upstream.close()
    assert code == 1 and rec["ran"]["turns_completed"] == 0 and KEY not in json.dumps(rec)
    assert rec["ran"]["stopped"] == "turn 1: a response ended with end_event incomplete (content_filter)"
    log = Path(rec["preflight"]["contexto_home"]) / "sessions" / "codex-step1" / "log.jsonl"
    entries = [e for e in codex_driver.read_log(log) if e.get("path", "").endswith("responses") and "status" in e]
    assert {(e["end_event"], e["response_status"], e["incomplete_reason"]) for e in entries} == {
        ("incomplete", "incomplete", "content_filter")}
