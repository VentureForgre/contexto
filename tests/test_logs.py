"""Tests for ``contexto logs [SESSION] [--follow]``."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path

import pytest

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


class _FollowBuffer(io.StringIO):
    """Buffer that signals on each flushed line and stops ``follow_log`` once ``stop_at`` lines arrive."""

    def __init__(self, *, on_line=None, stop_at: int | None = None) -> None:
        super().__init__()
        self._on_line = on_line
        self._stop_at = stop_at
        self._seen = 0

    def flush(self) -> None:
        super().flush()
        count = len(self.getvalue().splitlines())
        while self._seen < count:
            self._seen += 1
            if self._on_line is not None:
                self._on_line(self._seen)
        if self._stop_at is not None and count >= self._stop_at:
            raise KeyboardInterrupt


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

    saw_exit = threading.Event()
    buf = _FollowBuffer(on_line=lambda n: saw_exit.set() if n == 1 else None, stop_at=2)

    def writer() -> None:
        time.sleep(0.02)
        with log.open("a", encoding="utf-8") as fh:
            fh.write('"exit", "code": 0}\n')
        # Wait until follow_log has emitted the completed line before rotating the file.
        if not saw_exit.wait(timeout=5.0):
            return
        # Simulate log rotation (replace file with a brand-new inode); retry briefly on Windows
        # if follow_log transiently holds the target open during the replace.
        tmp_new = tmp_path / "log.jsonl.new"
        tmp_new.write_text(
            json.dumps({"ts": "2026-10-01T12:00:02Z", "event": "proxy_error", "error": "rotated"}) + "\n",
            encoding="utf-8",
        )
        for attempt in range(20):
            try:
                os.replace(tmp_new, log)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.01)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    code = logs_cmd.follow_log(log, offset, set(), False, True, buf, ident=ident, poll_s=0.005, max_polls=1000)
    t.join(timeout=5)
    assert not t.is_alive()
    assert saw_exit.is_set()
    assert code == 0
    rows = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert [r["event"] for r in rows] == ["exit", "proxy_error"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only")
def test_logs_refuses_fifo_without_blocking(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    s = SessionStore(home, "s1")
    s.release()
    os.mkfifo(home / "sessions" / "s1" / "log.jsonl")
    code, _, err = _run_cli(monkeypatch, home, ["logs", "s1"], capsys)
    assert code == 1 and "cannot read" in err


def test_logs_refuses_symlink_escapes_status_ignores_bool_bound_and_waits_for_new_file(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    s = SessionStore(home, "s1")
    s.release()
    target = tmp_path / "outside.jsonl"
    target.write_text(json.dumps({"event": "exit", "code": 0}) + "\n", encoding="utf-8")
    try:
        (home / "sessions" / "s1" / "log.jsonl").symlink_to(target)
    except OSError:
        pass
    else:
        code, _, err = _run_cli(monkeypatch, home, ["logs", "s1"], capsys)
        assert code == 1 and "cannot read" in err

    # Status control-char escaping and bool bound ignored
    line = logs_cmd.format_event({"ts": "2026-10-01T12:00:00Z", "event": "request", "method": "POST",
                                  "path": "/v1/messages", "status": "200\x1b[31m"})
    assert "\x1b" not in line and "200" in line
    line_bool = logs_cmd.format_event({"ts": "2026-10-01T12:00:00Z", "event": "launch", "config": {"bound": True}})
    assert "bound=" not in line_bool

    # follow_log waits for a log file that does not exist yet
    missing_log = tmp_path / "delayed.jsonl"
    buf = _FollowBuffer(stop_at=1)

    def create_later() -> None:
        time.sleep(0.02)
        missing_log.write_text(json.dumps({"ts": "2026-10-01T12:00:00Z", "event": "exit", "code": 0}) + "\n",
                               encoding="utf-8")

    t = threading.Thread(target=create_later, daemon=True)
    t.start()
    assert logs_cmd.follow_log(missing_log, 0, set(), False, True, buf, poll_s=0.005, max_polls=1000) == 0
    t.join(timeout=5)
    assert not t.is_alive()
    assert [json.loads(x)["event"] for x in buf.getvalue().splitlines()] == ["exit"]
