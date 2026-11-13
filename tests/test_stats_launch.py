"""D64 in ``contexto run``: the exit summary, ``--quiet``, and store faults a user can see.

No live model: a fake ``claude`` or a stand-in for ``codex``, and stub upstreams on 127.0.0.1.
"""

from __future__ import annotations

import errno
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import cli, launcher, stats
from contexto.codex import run_codex
from contexto.launcher import LaunchOptions, NoticeLog
from contexto.rewrite import EpochState, StoreCalls, store_failure
from contexto.stats import fault_notices, print_exit_summary, session_stats
from contexto.store import SessionStore
from contexto.store.ledger import LedgerCorruptError
from test_codex import parse
from test_launcher import FAKE_CLAUDE, Upstream

CLAUDE_SESSION = "0acd81f6-1f21-40f8-ab64-5c09545905cf"


@pytest.fixture
def upstream():
    Upstream.seen = []
    Upstream.body_delay = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A fake ``claude`` first on PATH, and a temporary HOME and CONTEXTO_HOME."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    fake.chmod(0o755)
    (tmp_path / "userhome").mkdir()
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("ANTHROPIC_", "CLAUDE", "OPENRIG_", "RIGGED_", "CONTEXTO_"))}
    clean.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", HOME=str(tmp_path / "userhome"),
                 CONTEXTO_HOME=str(tmp_path / "contexto"), FAKE_RECORD=str(tmp_path / "record.json"))
    return clean


def run(env, *args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "contexto", "run", *args], env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=60)


def summary(err: str) -> list[str]:
    """The exit summary's lines: its header and the indented lines under it."""
    lines = err.splitlines()
    start = next((i for i, line in enumerate(lines) if " ended after " in line), None)
    if start is None:
        return []
    end = start + 1
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    return lines[start:end]


# The exit summary --------------------------------------------------------------------------------

def test_a_run_ends_with_a_summary_and_the_resume_line_stays_last(env, upstream, tmp_path):
    env.update(FAKE_REQUEST="1", FAKE_CLAUDE_SESSION=CLAUDE_SESSION, FAKE_SAY="claude-still-running", FAKE_EXIT="3")
    done = run(env, "--upstream", upstream, "--session", "s1", "claude", "-p", "hello", cwd=tmp_path)
    assert done.returncode == 3, done.stderr  # the CLI's own code, summary or not
    lines = summary(done.stderr)
    assert lines[0].startswith("contexto: session s1 ended after ") and lines[0].split()[-1].endswith("s")
    # The stub upstream reports 5 input tokens: a measured number, shown against the bound.
    assert lines[1] == "  prompt      peak 5 of 150,000 tokens (measured); over the bound: 0 requests"
    assert "  cache       0% of input tokens read from cache (measured)" in lines
    assert lines[-1] == "  compaction  none seen: Claude Code did not compact the main conversation"
    # Nothing was paged or recalled in one request, so those lines are left out.
    assert not [line for line in lines if line.startswith(("  paged out", "  recall", "  faults"))]
    err = done.stderr
    assert err.index("claude-still-running") < err.index(lines[0])  # after the CLI released the terminal
    assert err.splitlines()[-1].startswith("contexto: resume this conversation through Contexto with: ")
    assert done.stdout == ""


@pytest.mark.parametrize("how", ["flag", "env"])
def test_quiet_drops_the_summary_and_never_a_warning(env, upstream, tmp_path, how):
    # Claude Code's own tools and none of Contexto's: the missing-tools warning is due at exit.
    env.update(FAKE_REQUEST="1", FAKE_CLAUDE_SESSION=CLAUDE_SESSION, FAKE_TOOLS="Bash,Read,Edit")
    args = ["--quiet"] if how == "flag" else []
    if how == "env":
        env["CONTEXTO_QUIET"] = "1"
    done = run(env, *args, "--upstream", upstream, "--session", "q1", "claude", "-p", "hello", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert summary(done.stderr) == [] and "ended after" not in done.stderr
    assert "warning: Claude Code is running without Contexto's tools" in done.stderr
    assert done.stderr.splitlines()[-1].startswith("contexto: resume this conversation")
    # The same run without it has both, and the warning that counts the fault outlives --quiet.
    assert "contexto: warning: this run logged 1 event (mcp_tools_missing 1). Details: contexto stats q1" in done.stderr
    env.pop("CONTEXTO_QUIET", None)
    loud = run(env, "--upstream", upstream, "--session", "q2", "claude", "-p", "hello", cwd=tmp_path)
    assert summary(loud.stderr) and "this run logged 1 event (mcp_tools_missing 1)" in loud.stderr
    assert env["CONTEXTO_HOME"] not in loud.stderr and str(tmp_path) not in "".join(summary(loud.stderr))


def test_a_run_whose_cli_sent_no_model_request_says_so_in_one_line(env, upstream, tmp_path):
    env["FAKE_EXIT"] = "4"  # the fake claude starts and makes no request: what a bypassed proxy looks like
    done = run(env, "--upstream", upstream, "--session", "n1", "claude", "-p", "hello", cwd=tmp_path)
    assert done.returncode == 4, done.stderr
    [line] = summary(done.stderr)
    assert line.startswith("contexto: session n1 ended after ")
    assert "with no model request through Contexto (nothing reached the proxy)" in line
    assert "If you sent claude a prompt, it bypassed Contexto" in line and "docs/stats.md" in line
    assert "warning" not in done.stderr


def test_a_resumed_session_s_summary_covers_this_run_only(env, upstream, tmp_path):
    first = run(dict(env, FAKE_REQUEST="1"), "--upstream", upstream, "--session", "r1", "claude", "-p", "a",
                cwd=tmp_path)
    assert summary(first.stderr)[0].split(" ended after ")[1].isalnum()  # one run: nothing about others
    second = run(env, "--upstream", upstream, "--session", "r1", "claude", "-p", "b", cwd=tmp_path)
    assert "with no model request through Contexto" in summary(second.stderr)[0]  # the session has one; this run none
    third = run(dict(env, FAKE_REQUEST="1"), "--upstream", upstream, "--session", "r1", "claude", "-p", "c",
                cwd=tmp_path)
    lines = summary(third.stderr)
    assert lines[0].endswith("(this run; `contexto stats r1` has all 3)") and "peak 5 of" in lines[1]
    whole = session_stats(Path(env["CONTEXTO_HOME"]), "r1")
    assert whole["runs"] == 3 and whole["requests"]["model"] == 2


def test_the_quiet_variable_reads_like_the_version_check_s(monkeypatch):
    monkeypatch.delenv("CONTEXTO_QUIET", raising=False)
    for value in ("", "0", "false", "No", " 0 "):
        assert not stats.quiet_from_env({"CONTEXTO_QUIET": value}), value
    assert not stats.quiet_from_env({}) and stats.quiet_from_env({"CONTEXTO_QUIET": "1"})
    args = parse("--session", "s", "claude")
    assert launcher.options_from_args(args).quiet is False  # a namespace without the flag, as codex's tests build
    args.quiet = True
    assert launcher.options_from_args(args).quiet is True


@pytest.mark.parametrize("args", [["--version"], ["-v"], ["--help"], ["-h"]])
def test_an_info_only_launch_prints_no_summary(env, tmp_path, args):
    done = run(env, "claude", *args, cwd=tmp_path)  # the fake claude answers only --version; the others fail in it
    assert (done.returncode == 0 or args != ["--version"]) and "contexto:" not in done.stderr


def test_a_launch_that_never_starts_the_cli_prints_no_summary(env, upstream, tmp_path):
    marker = tmp_path / "version-running"
    proc = subprocess.Popen([sys.executable, "-m", "contexto", "run", "--upstream", upstream, "--session", "i1",
                             "claude", "-p", "hi"], env=dict(env, FAKE_VERSION_HANG=str(marker)), cwd=tmp_path,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 10
    while not marker.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    proc.send_signal(2)  # Ctrl+C during the version check: claude itself never ran
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 130 and "ended after" not in err, err


def in_process(monkeypatch, env, tmp_path, child):
    """``run_claude`` in this process with ``child`` in place of starting claude; returns (code, stderr)."""
    for name in ("PATH", "HOME", "CONTEXTO_HOME"):
        monkeypatch.setenv(name, env[name])
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    monkeypatch.delenv("CONTEXTO_QUIET", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launcher, "run_child", child)
    err = io.StringIO()
    options = LaunchOptions(claude_args=["-p", "hi"], session_id="x1", upstream="http://127.0.0.1:9",
                            version_check=False)
    return launcher.run_claude(options, stderr=err), err.getvalue()


def test_a_summary_that_cannot_be_built_never_changes_the_exit_code(monkeypatch, env, tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic failure in stats")

    monkeypatch.setattr(stats, "session_stats", broken)
    code, err = in_process(monkeypatch, env, tmp_path, lambda argv, env=None, **kw: 7)
    assert code == 7 and "ended after" not in err and "Traceback" not in err and "synthetic" not in err
    # Called on its own with nothing it can use: silent, not an exception.
    out = io.StringIO()
    print_exit_summary(object(), "claude", time.monotonic(), stderr=out)
    print_exit_summary(None, "claude", time.monotonic(), stderr=out)
    assert out.getvalue() == ""


def test_a_log_that_cannot_be_written_at_exit_keeps_the_cli_s_exit_code_and_is_said(monkeypatch, env, tmp_path):
    log = Path(env["CONTEXTO_HOME"]) / "sessions" / "x1" / "log.jsonl"

    def child(argv, env=None, **kwargs):
        log.unlink()
        log.mkdir()  # every later write to the session log fails, as on a full disk
        return 6

    code, err = in_process(monkeypatch, env, tmp_path, child)
    assert code == 6, err  # before D64: an OSError out of the cleanup, and exit code 1
    [warning] = [line for line in err.splitlines() if "session log could not be written" in line]
    assert warning.startswith("contexto: warning: ") and "contexto stats" in warning
    assert str(tmp_path) not in warning  # the errno's name, never the exception's text
    assert "has no numbers" in err and "bypassed Contexto" not in err  # no claim from a log that is not there


# Store faults: NoticeLog -----------------------------------------------------------------------------

class Store:
    """A store stand-in that keeps what is logged, or fails every log write with ``error``."""

    session_id = "abc"

    def __init__(self, error: Exception | None = None) -> None:
        self.events: list[dict] = []
        self.error = error

    def log(self, event: dict) -> None:
        if self.error is not None:
            raise self.error
        self.events.append(event)


def failure(code: str | None = "ENOSPC", **extra) -> dict:
    event = {"event": "store_error", "epoch": 0, "operation": "page_out", "error": "OSError", **extra}
    return {**event, "errno": code} if code else event


def test_store_failure_names_the_errno_and_never_the_message():
    calls = StoreCalls(object())
    calls.last = "page_out"
    full = store_failure(OSError(errno.ENOSPC, "No space left on device: /synthetic/private/path"), calls, EpochState())
    assert full == {"event": "store_error", "epoch": 0, "operation": "page_out", "error": "OSError", "errno": "ENOSPC"}
    denied = store_failure(PermissionError(errno.EACCES, "denied"), calls, EpochState())
    assert denied["error"] == "PermissionError" and denied["errno"] == "EACCES"
    assert "errno" not in store_failure(OSError("no errno"), calls, EpochState())
    assert "errno" not in store_failure(LedgerCorruptError("synthetic"), calls, EpochState())
    assert "synthetic" not in json.dumps([full, denied])


def test_notice_log_says_a_full_disk_once_and_holds_it_until_the_cli_exits():
    store, err = Store(), io.StringIO()
    notices = NoticeLog(store, err)
    notices.hold()
    entered = {"event": "request", "transform_events": [failure(), {"event": "degraded_enter", "epoch": 0},
                                                        {"event": "request", "degraded": True}]}
    notices.log(entered)
    notices.log({"event": "request", "transform_events": [failure(degraded=True)]})  # the retry at the next turn
    notices.log({"event": "request", "usage_events": [failure("EACCES", operation="set_turn_room")]})
    assert err.getvalue() == "" and len(store.events) == 3  # logged at once, shown when the terminal is free
    notices.release()
    lines = err.getvalue().splitlines()
    assert len(lines) == 2 and all(line.startswith("contexto: warning: ") for line in lines)
    assert "disk" in lines[0] and "full" in lines[0] and "ENOSPC during page_out" in lines[0]
    assert "Free space" in lines[0] and "retries at each turn start" in lines[0] and "exceed the bound" in lines[0]
    assert "permissions" in lines[1] and "EACCES during set_turn_room" in lines[1] and "Free space" not in lines[1]
    notices.log(entered)
    assert len(err.getvalue().splitlines()) == 2  # said once per cause


def test_notice_log_shows_the_fault_even_when_the_log_itself_cannot_be_written():
    store, err = Store(OSError(errno.ENOSPC, "No space left on device: /synthetic/path/log.jsonl")), io.StringIO()
    notices = NoticeLog(store, err)
    with pytest.raises(OSError):  # as before: the proxy's own log() swallows it
        notices.log({"event": "request", "transform_events": [failure()]})
    with pytest.raises(OSError):
        notices.log({"event": "proxy_stop"})
    lines = err.getvalue().splitlines()
    assert len(lines) == 2 and "disk" in lines[0] and "Free space" in lines[0]
    assert "session log could not be written (ENOSPC)" in lines[1] and "/synthetic" not in err.getvalue()
    assert "log_unwritable" in notices.shown


def test_fault_notices_reads_any_event_without_raising():
    assert fault_notices(None) == [] and fault_notices({"event": "request", "transform_events": "x"}) == []
    assert fault_notices({"event": ["store_error"]}) == [] and fault_notices({"event": "rotation"}) == []
    [(key, message)] = fault_notices({"event": "store_error", "operation": 7, "errno": ["x"], "error": None})
    assert key == "store_fault_store" and "paging has stopped" in message
    [(key, message)] = fault_notices({"event": "page_conflict", "page_id": "pg_ab"}, "sid")
    assert key == "store_fault_page_conflict" and "pg_ab" in message and "quarantine" in message
    # A copy of a request's events on its upstream_4xx line is not a second failure.
    assert fault_notices({"event": "upstream_4xx", "transform_events": [failure()]}) == []


# Store faults: a store whose disk is full, through the real proxy and engine -------------------------

class TokenUpstream(BaseHTTPRequestHandler):
    """Answers every POST with usage sized from the request, 4 bytes a token, as JSON."""

    protocol_version = "HTTP/1.1"
    received: list[int] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(size)
        TokenUpstream.received.append(size)
        if self.path.endswith("/responses"):
            payload = {"id": "r", "object": "response", "status": "completed", "output": [],
                       "usage": {"input_tokens": size // 4, "output_tokens": 1,
                                 "input_tokens_details": {"cached_tokens": size // 8}}}
        else:
            payload = {"type": "message", "content": [], "stop_reason": "end_turn",
                       "usage": {"input_tokens": size // 4, "output_tokens": 1}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def token_upstream():
    TokenUpstream.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), TokenUpstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


FILL = "synthetic filler text " * 180  # about 4 KB a message


def conversation(turns: int) -> list[dict]:
    """``turns`` finished turns and a new prompt, in Claude Code's shape."""
    messages = []
    for t in range(turns):
        messages += [{"role": "user", "content": [{"type": "text", "text": f"prompt {t} {FILL}"}]},
                     {"role": "assistant", "content": [{"type": "text", "text": f"answer {t} {FILL}"}]}]
    return [*messages, {"role": "user", "content": [{"type": "text", "text": f"prompt {turns}"}]}]


def post(proxy, messages: list[dict]) -> None:
    tools = [{"name": name, "input_schema": {"type": "object"}}
             for name in ("Bash", "mcp__contexto__contexto_search", "mcp__contexto__contexto_recall")]
    body = json.dumps({"model": "claude-haiku-5-5", "max_tokens": 16, "tools": tools, "messages": messages}).encode()
    request = urllib.request.Request(proxy.url + "/v1/messages", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "x-api-key": "fake-key"})
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def test_a_full_disk_is_seen_at_exit_in_stats_and_in_state(tmp_path, token_upstream, monkeypatch, capsys):
    """The 2026-11-12 audit's finding: on a full disk the engine stopped paging, requests grew several
    times over, and nothing was said. A store whose page writes fail with ENOSPC, driven through the
    launcher's own proxy and engine."""
    home = tmp_path / "contexto"
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    store = SessionStore(home, "disk1")
    page_out = store.page_out
    err = io.StringIO()
    config = {"bound": 30_000, "headroom_floor": 2_000, "min_turn_room": 1_000, "pin_budget": 200, "index_budget": 200}
    proxy, _ = launcher.start_proxy(store, token_upstream, "flag", config, False, stderr=err, cli="claude")
    notices = proxy.session_log
    try:
        notices.hold()  # the CLI owns the terminal
        post(proxy, conversation(4))  # well under the bound: nothing to page

        def full_disk(turns, first_turn):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(home / "sessions" / "disk1" / "pages" / "pg_x"))

        store.page_out = full_disk
        post(proxy, conversation(30))  # over the bound at a turn start: the rotation cannot write its pages
        post(proxy, conversation(31))  # the retry at the next turn start fails too
        small, unpaged, larger = TokenUpstream.received
        assert unpaged > 5 * small and larger > unpaged  # what went upstream grew, unpaged
        assert "full" not in err.getvalue()  # held while the CLI runs
        faults = session_stats(home, "disk1")["faults"]
        assert faults["degraded"]["now"] is True and faults["degraded"]["unpaged_requests"] == 2
        assert faults["degraded"]["largest_unpaged_request_bytes"] == larger
        [group] = faults["store_errors"]
        assert (group["errno"], group["cause"], group["operation"], group["count"]) == ("ENOSPC", "disk_full",
                                                                                       "page_out", 2)
        assert session_stats(home, "disk1")["over_bound"]["degraded"] == 2

        assert cli.main(["state", "--session", "disk1"]) == 0
        shown = capsys.readouterr().out
        assert "Warning: The disk that holds Contexto's store is full" in shown and "Free space" in shown
        assert cli.main(["stats", "disk1"]) == 0
        shown = capsys.readouterr().out
        assert "faults      3 events (store_error 2, degraded_enter 1)" in shown and "has not resumed" in shown

        store.page_out = page_out  # space was freed
        post(proxy, conversation(32))  # the next turn start retries, and pages
        assert TokenUpstream.received[-1] < unpaged / 2
        after = session_stats(home, "disk1")
        assert after["faults"]["degraded"]["now"] is False and after["faults"]["degraded"]["left"] == 1
        assert after["paging"]["rotations"] == 1 and after["pages"]["count"] > 0
        assert "has resumed" in after["faults"]["messages"][-1]
    finally:
        proxy.stop()
        notices.release()
    warnings = [line for line in err.getvalue().splitlines() if "full" in line]
    assert len(warnings) == 1, err.getvalue()  # said once, after the CLI gave the terminal back
    assert warnings[0].startswith("contexto: warning: the disk that holds Contexto's store is full (ENOSPC during "
                                  "page_out), so paging has stopped: requests go out unpaged and can exceed the bound.")
    assert "Free space" in warnings[0] and "retries at each turn start" in warnings[0]
    assert str(home) not in err.getvalue()  # the exception's text, with its path, is never shown or logged
    assert str(home) not in (home / "sessions" / "disk1" / "log.jsonl").read_text()
    lines = stats.exit_summary(home, "disk1", "claude", 12.0, quiet=True)
    assert lines == ["contexto: warning: this run logged 4 events (store_error 2, degraded_enter 1, bound_too_small 1)"
                     ": 2 requests went out unpaged, and paging resumed before exit. Details: contexto stats disk1"] \
        or (len(lines) == 1 and "2 requests went out unpaged, and paging resumed before exit" in lines[0])


# Codex -----------------------------------------------------------------------------------------------

@pytest.fixture
def codex_bin(tmp_path, monkeypatch):
    """A stub ``codex`` first on PATH that prints a version (CI has no Codex)."""
    bindir = tmp_path / "codex-bin"
    bindir.mkdir()
    stub = bindir / "codex"
    stub.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "codex-cli 0.166.0" ;;\nesac\nexit 0\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    monkeypatch.delenv("CONTEXTO_QUIET", raising=False)
    (tmp_path / "userhome").mkdir()
    return str(stub)


def codex_run(monkeypatch, token_upstream, session: str, child, *codex_args: str, quiet: bool = False):
    """``run_codex`` with the real launcher helpers and ``child(proxy, stderr)`` standing in for Codex."""
    seen: dict = {}
    start_proxy = launcher.start_proxy

    def spy(*args, **kwargs):
        seen["proxy"], name = start_proxy(*args, **kwargs)
        return seen["proxy"], name

    monkeypatch.setattr(launcher, "start_proxy", spy)
    monkeypatch.setattr(launcher, "run_child", lambda argv, env=None, *, stderr=None: child(seen["proxy"], stderr))
    args = parse("--session", session, "--codex-upstream", token_upstream + "/v1", "--codex-key-helper", "echo k",
                 "codex", *codex_args)
    args.no_version_check, args.quiet = True, quiet
    err = io.StringIO()
    return run_codex(args, stderr=err), err.getvalue()


def responses_request(proxy, stderr) -> int:
    body = json.dumps({"model": "gpt-6-astra", "stream": False, "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "synthetic " * 400}]}]}).encode()
    request = urllib.request.Request(proxy.url + "/responses", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer fake"})
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()
    return 5


def test_codex_ends_with_a_summary_too(tmp_path, monkeypatch, codex_bin, token_upstream):
    code, err = codex_run(monkeypatch, token_upstream, "cx1", responses_request, "exec", "hi")
    assert code == 5
    lines = summary(err)
    assert lines[0].startswith("contexto: session cx1 ended after ")
    tokens = TokenUpstream.received[0] // 4
    assert lines[1] == f"  prompt      peak {tokens:,} of 150,000 tokens (measured); over the bound: 0 requests"
    assert "  cache       50% of input tokens read from cache (measured)" in lines
    assert not [line for line in lines if "compaction" in line]  # nothing in a Codex log shows one
    # Codex has no resume line. The stand-in never asked the MCP server for its tools, which is a fault,
    # so the line that counts it follows the summary.
    assert err.splitlines()[-2] == lines[-1]
    assert err.splitlines()[-1] == ("contexto: warning: this run logged 1 event (mcp_tools_missing 1). Details: "
                                    "contexto stats cx1")
    quiet_code, quiet_err = codex_run(monkeypatch, token_upstream, "cx2", responses_request, "exec", "hi", quiet=True)
    assert quiet_code == 5 and "ended after" not in quiet_err
    monkeypatch.setenv("CONTEXTO_QUIET", "yes")
    assert "ended after" not in codex_run(monkeypatch, token_upstream, "cx3", responses_request, "exec", "hi")[1]


def test_codex_without_a_request_and_codex_version_are_told_apart(tmp_path, monkeypatch, codex_bin, token_upstream):
    code, err = codex_run(monkeypatch, token_upstream, "cy1", lambda proxy, stderr: 0, "exec", "hi")
    [line] = summary(err)
    assert code == 0 and "with no model request through Contexto" in line and "If you sent codex a prompt" in line
    for args in (["--version"], ["-V"], ["--help"], ["-h"]):
        code, err = codex_run(monkeypatch, token_upstream, "cy2", lambda proxy, stderr: 0, *args)
        assert code == 0 and "ended after" not in err, args


def test_codex_holds_a_store_fault_until_it_exits(tmp_path, monkeypatch, codex_bin, token_upstream):
    during: dict = {}

    def child(proxy, stderr) -> int:
        proxy.log({"event": "request", "transform_events": [failure()]})
        during["err"] = stderr.getvalue()
        return 0

    code, err = codex_run(monkeypatch, token_upstream, "cz1", child, "exec", "hi")
    assert code == 0 and "full" not in during["err"]  # Codex's TUI owns the terminal, as Claude Code's does
    [warning] = [line for line in err.splitlines() if "full" in line]
    assert warning.startswith("contexto: warning: the disk that holds Contexto's store is full")
    assert err.index(warning) < err.index("ended after")
