"""Tests for ``contexto logs [SESSION] [--follow]``."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path

from contexto import cli
from contexto.commands import logs as logs_cmd
from contexto.store import SessionStore


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _run_cli(monkeypatch, home: Path, argv: list[str], capsys) -> tuple[int, str, str]:
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    args = cli.build_parser().parse_args(argv)
    code = args.func(args)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_logs_reads_session_events_and_formats_fields(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    s = SessionStore(home, "s1")
    s.release()
    _write_events(home / "sessions" / "s1" / "log.jsonl", [
        {"ts": "2026-10-01T12:00:00Z", "event": "launch", "cli": "claude",
         "config": {"bound": 150000}, "upstream_source": "default", "statusline": "added"},
        {"ts": "2026-10-01T12:00:01Z", "event": "request", "method": "POST", "path": "/v1/messages",
         "status": 200, "model": "claude-sonnet-4-6",
         "usage": {"input_tokens": 1000, "cache_read_input_tokens": 4000, "output_tokens": 250},
         "original_request_bytes": 20000, "request_bytes": 12000,
         "transform_events": [{"event": "rotation"}]},
        {"ts": "2026-10-01T12:00:02Z", "event": "mcp_call", "tool": "contexto_search", "ok": True,
         "charged_tokens": 300},
        {"ts": "2026-10-01T12:00:03Z", "event": "proxy_error", "error": "upstream_down"},
        {"ts": "2026-10-01T12:00:04Z", "event": "exit", "code": 0},
    ])

    code, out, err = _run_cli(monkeypatch, home, ["logs", "s1"], capsys)
    assert code == 0
    assert "session s1" in err
    lines = out.splitlines()
    assert len(lines) == 5
    assert "launch" in lines[0] and "cli=claude" in lines[0] and "bound=150,000" in lines[0]
    assert "POST /v1/messages 200" in lines[1]
    assert "model=claude-sonnet-4-6" in lines[1]
    assert "in=5,000" in lines[1] and "out=250" in lines[1] and "cache_read=4,000" in lines[1]
    assert "bytes=20,000->12,000" in lines[1] and "[ROTATION]" in lines[1]
    assert "mcp_call" in lines[2] and "tool=contexto_search" in lines[2] and "charged=300" in lines[2]
    assert "[FAULT] proxy_error" in lines[3]
    assert "exit" in lines[4] and "code=0" in lines[4]


def test_logs_filters_lines_events_faults_and_json(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    s = SessionStore(home, "s1")
    s.release()
    _write_events(home / "sessions" / "s1" / "log.jsonl", [
        {"ts": "2026-10-01T12:00:00Z", "event": "launch", "cli": "codex", "config": {"bound": 120000}},
        {"ts": "2026-10-01T12:00:01Z", "event": "request", "api": "responses", "method": "POST",
         "path": "/v1/responses", "status": 200, "model": "gpt-5.3-codex",
         "usage": {"input_tokens": 5000, "output_tokens": 120, "input_tokens_details": {"cached_tokens": 3000}}},
        {"ts": "2026-10-01T12:00:02Z", "event": "request", "method": "POST", "path": "/v1/messages",
         "status": 200, "over_bound": True,
         "transform_events": [{"event": "fallback_failed"}]},
        {"ts": "2026-10-01T12:00:03Z", "event": "proxy_error", "error": "boom"},
        {"ts": "2026-10-01T12:00:04Z", "event": "exit", "code": 0},
    ])

    # Default session (no ID given) + -n 2
    code, out, _ = _run_cli(monkeypatch, home, ["logs", "-n", "2"], capsys)
    assert code == 0 and len(out.splitlines()) == 2
    assert "proxy_error" in out and "exit" in out

    # --event filter (comma-separated) + --json suppresses stderr banner
    code, out, err = _run_cli(
        monkeypatch, home, ["logs", "--session", "s1", "--event", "launch,exit", "--json"], capsys,
    )
    assert code == 0 and err == ""
    rows = [json.loads(line) for line in out.splitlines()]
    assert [r["event"] for r in rows] == ["launch", "exit"]

    # --faults includes both direct faults and requests with nested faults
    code, out, _ = _run_cli(monkeypatch, home, ["logs", "s1", "--faults"], capsys)
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 2
    assert "[FAULT:fallback_failed]" in lines[0] and "[OVER_BOUND]" in lines[0]
    assert "[FAULT] proxy_error" in lines[1]

    # Codex responses API uses total input_tokens without double-counting cached_tokens
    code, out, _ = _run_cli(monkeypatch, home, ["logs", "s1", "--event", "request"], capsys)
    assert code == 0
    assert "in=5,000 out=120 cache_read=3,000" in out


def test_logs_serve_and_argument_validation(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _write_events(home / "serve.log", [
        {"ts": "2026-10-01T12:00:00Z", "event": "serve_start", "port": 8080},
    ])
    code, out, _ = _run_cli(monkeypatch, home, ["logs", "--serve"], capsys)
    assert code == 0 and "serve_start" in out and "port=8080" in out

    # Conflicting flags exit with EXIT_USAGE (2)
    code, _, err = _run_cli(monkeypatch, home, ["logs", "s1", "--serve"], capsys)
    assert code == 2 and "--serve" in err
    code, _, err = _run_cli(monkeypatch, home, ["logs", "s1", "--session", "s2"], capsys)
    assert code == 2 and "not both" in err


def test_logs_escapes_terminal_control_sequences(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    s = SessionStore(home, "s1")
    s.release()
    _write_events(home / "sessions" / "s1" / "log.jsonl", [
        {"ts": "2026-10-01T12:00:00Z", "event": "proxy_error", "error": "bad\x1b[31mred\x1b[0m\u202ehi"},
    ])
    code, out, _ = _run_cli(monkeypatch, home, ["logs", "s1"], capsys)
    assert code == 0
    assert "\x1b" not in out and "\u202e" not in out
    assert "bad" in out


def test_logs_follow_streams_appended_partial_and_rotated_lines(tmp_path):
    log = tmp_path / "log.jsonl"
    # Start with one complete line and one partial (unterminated) line
    log.write_text(
        json.dumps({"ts": "2026-10-01T12:00:00Z", "event": "launch", "cli": "claude"}) + "\n"
        + '{"ts": "2026-10-01T12:00:01Z", "event": ',
        encoding="utf-8",
    )
    initial, offset, ident = logs_cmd._read_existing(log, set(), False, 50, follow=True)
    assert [e["event"] for e in initial] == ["launch"]

    buf = io.StringIO()

    def writer() -> None:
        time.sleep(0.03)
        with log.open("a", encoding="utf-8") as fh:
            fh.write('"exit", "code": 0}\n')
        time.sleep(0.05)
        # Simulate log rotation (replace file with a brand-new inode)
        tmp_new = tmp_path / "log.jsonl.new"
        tmp_new.write_text(
            json.dumps({"ts": "2026-10-01T12:00:02Z", "event": "proxy_error", "error": "rotated"}) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_new, log)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    code = logs_cmd.follow_log(log, offset, set(), False, True, buf, ident=ident, poll_s=0.02, max_polls=15)
    t.join(timeout=5)
    assert code == 0
    rows = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert [r["event"] for r in rows] == ["exit", "proxy_error"]


def test_logs_refuses_fifo_without_blocking(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    s = SessionStore(home, "s1")
    s.release()
    os.mkfifo(home / "sessions" / "s1" / "log.jsonl")
    code, _, err = _run_cli(monkeypatch, home, ["logs", "s1"], capsys)
    assert code == 1 and "cannot read" in err
