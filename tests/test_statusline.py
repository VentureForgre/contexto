"""``contexto statusline`` and the status line ``contexto run claude`` installs (D64).

Every log is synthetic, the ``claude`` is a fake, and every settings file sits under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import pty
import shlex
import subprocess
import sys
import threading
import time
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import launcher, statusline
from contexto.stats import session_stats
from contexto.statusline import (
    RESTART_NOTICE,
    installed_command,
    launch_status_line,
    render,
    settings_files,
    short_tokens,
    status_line,
)
from contexto.store import SessionStore
from contexto.store.sessions import resolve_session
from test_launcher import FAKE_CLAUDE, Upstream
from test_stats import ENTER, LEAVE, launch, long_session_log, mcp, request, rotation, store_error, ts, write_log

CLAUDE_ID = "0acd81f6-1f21-40f8-ab64-5c09545905cf"
OTHER_ID = "7d0e5a0b-3f0c-4c57-9d5e-0c4f6a0d1b22"
OWN = {"statusLine": {"type": "command", "command": "echo mine"}}
NOTICE_LINE = "ctx: your status line needs a restart\n"  # what the installed command prints once the user has one


def pages(home: Path, session: str, count: int) -> None:
    """``count`` page files in the session's store, as the status line counts them (it opens none)."""
    directory = home / "sessions" / session / "pages"
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (directory / f"pg_{i:06d}.json").write_text("{}")


# The line --------------------------------------------------------------------------------------------

def test_the_line_shows_the_prompt_against_the_bound_turns_paged_and_recall_activity(tmp_path):
    write_log(tmp_path, "s1", [
        *launch(0), request(1, tokens=148_000), request(2, tokens=112_400, events=rotation(6)),
        mcp("recall", 3), mcp("recall", 4), mcp("recall", 5), mcp("search", 6), mcp("pin", 7), mcp("state", 8)])
    pages(tmp_path, "s1", 41)
    # The latest main request's prompt, not the peak (148,000), and rounded down.
    assert status_line(tmp_path, "s1") == "ctx 112k/150k · 41 paged · 3 recalls · 1 search"


def test_a_part_without_data_is_left_out(tmp_path):
    # Before the first request: the bound alone says Contexto is on.
    write_log(tmp_path, "new", launch(0))
    assert status_line(tmp_path, "new") == "ctx bound 150k"
    # Nothing paged and no tool call yet.
    write_log(tmp_path, "early", [*launch(0), request(1, tokens=18_400)])
    assert status_line(tmp_path, "early") == "ctx 18k/150k"
    # One page and one call: singular.
    write_log(tmp_path, "one", [*launch(0), request(1, tokens=9_450), mcp("recall", 2)])
    pages(tmp_path, "one", 1)
    assert status_line(tmp_path, "one") == "ctx 9.4k/150k · 1 paged · 1 recall"
    # A log without its launch line has no bound.
    write_log(tmp_path, "nobound", [request(1, tokens=61_000), mcp("search", 2), mcp("search", 3)])
    assert status_line(tmp_path, "nobound") == "ctx 61k · 2 searches"
    # A failed tool call is not recall activity.
    write_log(tmp_path, "failed", [*launch(0), request(1, tokens=61_000), mcp("recall", 2, ok=False)])
    assert status_line(tmp_path, "failed") == "ctx 61k/150k"


def test_the_prompt_is_the_main_conversation_s_latest_and_an_estimate_is_marked(tmp_path):
    write_log(tmp_path, "side", [*launch(0), request(1, tokens=90_000), request(2, tokens=4_000, main=False)])
    assert status_line(tmp_path, "side") == "ctx 90k/150k"
    # No usage from the API: Contexto's own estimate, marked with "~".
    write_log(tmp_path, "est", [*launch(0), request(1, tokens=None, estimate=77_300)])
    assert status_line(tmp_path, "est") == "ctx ~77k/150k"
    # A resumed session shows the latest run's bound.
    write_log(tmp_path, "two", [*launch(0), request(1, tokens=140_000), *launch(50, bound=64_000),
                                request(51, tokens=30_000)])
    assert status_line(tmp_path, "two") == "ctx 30k/64k"


def test_paging_stopped_is_said_first_with_its_cause(tmp_path):
    stopped = [*launch(0), request(1, tokens=90_000),
               request(2, tokens=212_000, degraded=True, events=(store_error(), ENTER)), mcp("recall", 3)]
    write_log(tmp_path, "full", stopped)
    pages(tmp_path, "full", 41)
    assert status_line(tmp_path, "full") == (
        "ctx PAGING STOPPED (disk full) · 212k/150k · 41 paged · see contexto stats")
    assert session_stats(tmp_path, "full")["faults"]["degraded"]["now"] is True
    for name, cause in (("EACCES", "store not writable"), ("EIO", "store error")):
        write_log(tmp_path, "c", [*launch(0), request(1, degraded=True, events=(store_error(name=name), ENTER))])
        assert status_line(tmp_path, "c").startswith(f"ctx PAGING STOPPED ({cause}) · ")
    conflict = {"event": "page_conflict", "epoch": 0, "page_id": "pg_000001"}
    write_log(tmp_path, "c", [*launch(0), request(1, degraded=True, events=(conflict, ENTER))])
    assert status_line(tmp_path, "c").startswith("ctx PAGING STOPPED (page conflict) · ")
    # The enter line was lost with the full disk: the engine's own flag on the request still says it.
    write_log(tmp_path, "c", [*launch(0), request(1, tokens=70_000, degraded=True)])
    assert status_line(tmp_path, "c") == "ctx PAGING STOPPED · 70k/150k · see contexto stats"
    # A cause is named only when it belongs to this stop: an earlier run's full disk is not it.
    write_log(tmp_path, "c", [*launch(0), request(1, degraded=True, events=(store_error(), ENTER)),
                              *launch(50), request(51, tokens=70_000, degraded=True)])
    assert status_line(tmp_path, "c") == "ctx PAGING STOPPED · 70k/150k · see contexto stats"
    write_log(tmp_path, "c", [*launch(0), request(1, degraded=True, events=(store_error(), ENTER)), *launch(50),
                              request(51, tokens=70_000, degraded=True, events=(store_error(name="EACCES"), ENTER))])
    assert status_line(tmp_path, "c").startswith("ctx PAGING STOPPED (store not writable) · ")
    # Paging resumed, or a new run started (a new engine pages again): the marker goes.
    write_log(tmp_path, "full", [*stopped, request(4, tokens=80_000, events=(LEAVE,))])
    assert status_line(tmp_path, "full") == "ctx 80k/150k · 41 paged · 1 recall"
    write_log(tmp_path, "full", [*stopped, *launch(50), request(51, tokens=60_000)])
    assert status_line(tmp_path, "full") == "ctx 60k/150k · 41 paged · 1 recall"
    # A side conversation's failure does not mark the main conversation.
    write_log(tmp_path, "sidefail", [*launch(0), request(1, tokens=50_000),
                                     request(2, main=False, degraded=True, events=(store_error(), ENTER))])
    assert status_line(tmp_path, "sidefail") == "ctx 50k/150k"


def test_colour_is_only_on_request_and_only_marks_stopped_paging(tmp_path):
    write_log(tmp_path, "ok", [*launch(0), request(1, tokens=90_000)])
    assert status_line(tmp_path, "ok", color=True) == "ctx 90k/150k"
    write_log(tmp_path, "bad", [*launch(0), request(1, tokens=90_000, degraded=True, events=(store_error(), ENTER))])
    assert "\x1b" not in status_line(tmp_path, "bad")
    assert status_line(tmp_path, "bad", color=True) == (
        "ctx \x1b[1;31mPAGING STOPPED (disk full)\x1b[0m · 90k/150k · see contexto stats")


def test_a_terminal_that_cannot_show_the_dot_gets_a_bar(tmp_path):
    write_log(tmp_path, "s", [*launch(0), request(1, tokens=90_000), mcp("recall", 2)])
    assert status_line(tmp_path, "s", separator=statusline.separator_for("ascii")) == "ctx 90k/150k | 1 recall"
    assert statusline.separator_for("utf-8") == " · " and statusline.separator_for(None) == " | "
    assert statusline.separator_for("no-such-codec") == " | "


def test_tokens_are_shortened_and_never_rounded_up():
    assert [short_tokens(n) for n in (0, 950, 1_000, 9_450, 9_999, 10_000, 149_999, 150_000, 999_999,
                                      1_000_000, 1_950_000, 12_000_000)] == [
        "0", "950", "1k", "9.4k", "9.9k", "10k", "149k", "150k", "999k", "1M", "1.9M", "12M"]


def shown_value(text: str) -> Decimal:
    """The number a reader takes from one side of ``112k/150k``."""
    text = text.lstrip("~")
    scale = {"k": 1_000, "M": 1_000_000}.get(text[-1:], 1)
    return Decimal(text[:-1] if scale > 1 else text) * scale


def test_a_prompt_over_or_under_the_bound_never_reads_as_equal_to_it():
    """Reviewer B, PR #276: rounding both numbers down showed 1,099,999 against a bound of 1M as ``1M/1M``,
    150,900 against 150k as ``150k/150k``, and 150,200 against 150,500 as ``150k/150k``. When the two
    differ but shorten alike, they get the decimals it takes to tell them apart."""
    def line(prompt: int, bound: int, source: str = "usage") -> str:
        return render({"prompt": prompt, "prompt_source": source, "bound": bound})

    assert line(1_099_999, 1_000_000) == "ctx 1.09M/1M"
    assert line(150_900, 150_000) == "ctx 150.9k/150k"
    assert line(150_200, 150_500) == "ctx 150.2k/150.5k"
    assert line(10_400, 10_000) == "ctx 10.4k/10k"
    assert line(150_001, 150_000) == "ctx 150.001k/150k"
    assert line(1_000_001, 1_000_000) == "ctx 1.000001M/1M"
    assert line(9_450, 9_400) == "ctx 9.45k/9.4k"
    assert line(12_400_000, 12_000_000) == "ctx 12.4M/12M"
    assert line(150_900, 150_000, "estimate") == "ctx ~150.9k/150k"
    # Unchanged when they shorten differently, or are equal.
    assert line(149_999, 150_000) == "ctx 149k/150k"
    assert line(150_000, 150_000) == "ctx 150k/150k"
    assert line(212_000, 150_000) == "ctx 212k/150k"
    # Every pair near a bound: the order a reader sees is the true order.
    for bound in (1_000, 9_400, 9_999, 10_000, 64_000, 150_000, 150_500, 999_999, 1_000_000, 1_000_500,
                  12_000_000):
        for delta in (*range(-1_200, 1_201, 7), -1, 1, -99, 99, -100, 100, -999, 999, -100_000, 100_000):
            prompt = bound + delta
            if prompt < 0:
                continue
            shown, bound_text = line(prompt, bound).removeprefix("ctx ").split("/")
            seen, limit = shown_value(shown), shown_value(bound_text)
            assert (seen > limit) == (prompt > bound) and (seen < limit) == (prompt < bound), (prompt, bound)
            assert seen <= prompt and limit <= bound  # still never rounded up


def test_a_damaged_log_is_read_as_far_as_it_goes(tmp_path):
    lines = [*launch(0), request(1, tokens=50_000), b'{"event":"request","method":"POST","path":"/v1/mess',
             b"\xff\xfe not json", "[1, 2]", "null", request(2, tokens=71_000), mcp("recall", 3),
             {"event": "mcp_call", "tool": 7, "ok": True}, {"event": "request", "usage": "many", "ts": 5}]
    write_log(tmp_path, "dmg", lines)
    assert status_line(tmp_path, "dmg") == "ctx 71k/150k · 1 recall"
    # Fields of the wrong type cost only their own part.
    write_log(tmp_path, "odd", [{"event": "launch", "cli": "claude", "config": {"bound": "big"}},
                                request(1, tokens=71_000)])
    assert status_line(tmp_path, "odd") == "ctx 71k"


@pytest.mark.parametrize("damage", ["missing", "empty", "directory", "fifo", "symlink"])
def test_a_missing_or_unusable_log_shows_what_the_store_has_and_never_waits(tmp_path, damage):
    directory = tmp_path / "sessions" / "s"
    directory.mkdir(parents=True)
    log = directory / "log.jsonl"
    if damage == "empty":
        log.write_bytes(b"")
    elif damage == "directory":
        log.mkdir()
    elif damage == "fifo":
        os.mkfifo(log)
    elif damage == "symlink":
        (tmp_path / "elsewhere.jsonl").write_text(json.dumps(request(1, tokens=99_000)) + "\n")
        log.symlink_to(tmp_path / "elsewhere.jsonl")
    started = time.monotonic()
    assert status_line(tmp_path, "s") == ""  # nothing known: nothing shown
    pages(tmp_path, "s", 3)
    assert status_line(tmp_path, "s") == "ctx 3 paged"
    assert time.monotonic() - started < 2.0


def test_no_session_and_a_damaged_store_give_an_empty_line(tmp_path):
    assert status_line(tmp_path, "nothing") == ""
    assert status_line(tmp_path / "no-home", "s") == ""
    (tmp_path / "file-home").write_text("not a directory")
    assert status_line(tmp_path / "file-home", "s") == ""
    write_log(tmp_path, "real", [*launch(0), request(1, tokens=50_000)])
    (tmp_path / "sessions" / "link").symlink_to(tmp_path / "sessions" / "real")
    assert status_line(tmp_path, "link") == ""  # as every reader: a symlink is not a session (D58)
    assert status_line(tmp_path, "../sessions/real") == ""
    # The pages directory cannot be listed: the part is left out.
    (tmp_path / "sessions" / "real" / "pages").write_text("not a directory")
    assert status_line(tmp_path, "real") == "ctx 50k/150k"


LOG_SHAPES = {
    "healthy": lambda: [*launch(0), request(1, tokens=90_000), mcp("recall", 2), request(3, tokens=95_000)],
    "no_requests": lambda: launch(0),
    "no_launch": lambda: [request(1, tokens=61_000), mcp("search", 2)],
    "bound_only_in_an_event": lambda: [request(1, tokens=61_000, events=(
        {"event": "over_bound", "source": "estimate", "in_tool_loop": True, "bound": 64_000, "estimate": 70_000},))],
    "side_last": lambda: [*launch(0), request(1, tokens=90_000), request(2, tokens=4_000, main=False)],
    "side_only": lambda: [*launch(0), request(1, tokens=4_000, main=False, degraded=True, events=(ENTER,))],
    "estimate_then_usage_missing": lambda: [*launch(0), request(1, tokens=50_000),
                                           request(2, tokens=None, estimate=None)],
    "stopped": lambda: [*launch(0), request(1), request(2, degraded=True, events=(store_error(), ENTER))],
    "stopped_then_plain_request": lambda: [*launch(0), request(1, degraded=True, events=(store_error(), ENTER)),
                                           request(2, tokens=70_000)],
    "stopped_then_left": lambda: [*launch(0), request(1, degraded=True, events=(store_error(), ENTER)),
                                  request(2, events=(LEAVE,))],
    "stopped_by_usage_event": lambda: [*launch(0), request(1, tokens=70_000, usage_events=(store_error(), ENTER))],
    "stopped_then_new_run": lambda: [*launch(0), request(1, degraded=True, events=(store_error(), ENTER)),
                                     *launch(50, bound=64_000)],
    # Tool calls in one run, then a second launch: the counts are the session's, not the latest run's.
    "tools_then_new_run": lambda: [*launch(0), request(1, tokens=40_000), mcp("recall", 2), mcp("search", 3),
                                   mcp("recall", 4, ok=False), *launch(50), request(51, tokens=20_000),
                                   mcp("search", 52)],
    "stopped_main_then_side": lambda:[*launch(0), request(1, degraded=True, events=(store_error(), ENTER)),
                                       request(2, tokens=3_000, main=False)],
    "enter_without_engine_line": lambda: [*launch(0), {
        "event": "request", "method": "POST", "path": "/v1/messages", "status": 200, "ts": ts(1),
        "transform_events": [store_error(), ENTER]}],
    "failed_request_last": lambda: [*launch(0), request(1, tokens=90_000), request(2, tokens=None, status=529)],
    "codex": lambda: [*launch(0, cli="codex"), request(1, tokens=41_000, cache_read=30_000, api="responses")],
    "other_requests_last": lambda: [*launch(0), request(1, tokens=41_000), {
        "event": "request", "method": "HEAD", "path": "/api/hello", "status": 401, "ts": ts(2)}],
    "damaged": lambda: [*launch(0), request(1, tokens=50_000), b'{"event":"request","meth', request(2, tokens=71_000),
                        b"\xff", mcp("recall", 3)],
    "words_in_other_lines": lambda: [*launch(0), request(1, tokens=50_000, model="launch-mcp_call-proxy_start"),
                                     {"event": "note", "text": "mcp_call launch proxy_start", "ts": ts(2)}],
    "six_hours": long_session_log,
}


@pytest.mark.parametrize("shape", sorted(LOG_SHAPES))
def test_the_quick_read_gives_the_numbers_contexto_stats_gives(tmp_path, shape):
    """The status line reads only what it shows (the log's tail, and the few lines that hold a launch or a
    tool call); for every log shape that must be what ``session_stats`` reports."""
    write_log(tmp_path, "s", LOG_SHAPES[shape]())
    full = session_stats(tmp_path, "s", store=False)
    quick = statusline.session_numbers(tmp_path, "s")
    assert quick["prompt"] == full["prompt"]["last_tokens"] and quick["prompt_source"] == full["prompt"]["last_source"]
    assert quick["bound"] == full["bound"]
    assert quick["recalls"] == full["tools"]["recall"]["calls"]
    assert quick["searches"] == full["tools"]["search"]["calls"]
    assert quick["stopped"] is full["faults"]["degraded"]["now"]


def test_the_quick_read_parses_a_small_part_of_a_long_log(tmp_path, monkeypatch):
    write_log(tmp_path, "long", long_session_log())
    parsed = []
    real = json.loads
    monkeypatch.setattr(statusline.json, "loads", lambda data, *a, **k: (parsed.append(1), real(data, *a, **k))[1])
    assert status_line(tmp_path, "long") == "ctx 136k/150k · 223 recalls · 222 searches"
    assert len(parsed) < 600, len(parsed)  # 4,449 lines; the tool calls, the launch, and the last request


def test_the_quick_read_falls_back_to_the_full_one(tmp_path, monkeypatch):
    """If the quick read cannot be trusted (it raised, say after stats.py changed), the whole log is read."""
    write_log(tmp_path, "s", [*launch(0), request(1, tokens=90_000), mcp("recall", 2)])
    monkeypatch.setattr(statusline, "quick_numbers", lambda directory: 1 / 0)
    assert status_line(tmp_path, "s") == "ctx 90k/150k · 1 recall"


# The command -----------------------------------------------------------------------------------------

def clean_env(home: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CONTEXTO_", "CLAUDE"))}
    return {**env, "CONTEXTO_HOME": str(home), **extra}


def command(home: Path, *args: str, stdin: str | None = "", env: dict | None = None, **run):
    """``contexto statusline`` as a status line runs it: its own process, the JSON (or nothing) on stdin."""
    kwargs = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run([sys.executable, "-P", "-m", "contexto", "statusline", *args], capture_output=True,
                          text=True, timeout=60, env=clean_env(home, **(env or {})), **kwargs, **run)


def claude_stdin(session_id: str = CLAUDE_ID) -> str:
    return json.dumps({"session_id": session_id, "transcript_path": "/synthetic/t.jsonl", "cwd": "/synthetic",
                       "model": {"id": "claude-haiku-5-5", "display_name": "Haiku"},
                       "workspace": {"current_dir": "/synthetic", "project_dir": "/synthetic"}})


# A time inside the runs the synthetic logs hold: test_stats.launch(0) starts its run at 1,800,000,000.
IN_RUN = 1_800_000_010.0


def serving(home: Path, session: str, claude_session: str = CLAUDE_ID, seen_at: float = IN_RUN) -> SessionStore:
    """A session with a log that served ``claude_session`` in its latest run; while the returned store is
    alive the session is in use, as it is while ``contexto run`` has it open."""
    store = SessionStore(home, session)
    store.record_cli_session("claude", claude_session, seen_at)
    write_log(home, session, [*launch(0), request(1, tokens=int(session[-2:]) * 1_000)])
    return store


def test_the_session_is_the_option_then_the_environment_then_the_one_serving_claude_code(tmp_path):
    held = [serving(tmp_path, "aaaa11"), serving(tmp_path, "bbbb22", OTHER_ID), serving(tmp_path, "cccc33", "x-1")]
    by_stdin = command(tmp_path, stdin=claude_stdin())
    assert (by_stdin.returncode, by_stdin.stdout, by_stdin.stderr) == (0, "ctx 11k/150k\n", "")
    by_env = command(tmp_path, stdin=claude_stdin(OTHER_ID), env={"CONTEXTO_SESSION": "bbbb22"})
    assert by_env.stdout == "ctx 22k/150k\n"
    by_option = command(tmp_path, "--session", "cccc33", stdin=claude_stdin(), env={"CONTEXTO_SESSION": "bbbb22"})
    assert by_option.stdout == "ctx 33k/150k\n"
    # A unique prefix names a session, as it does for every reader; one that is not unique names none.
    assert command(tmp_path, "--session", "cc").stdout == "ctx 33k/150k\n"
    assert command(tmp_path, "--session=bbbb").stdout == "ctx 22k/150k\n"
    write_log(tmp_path, "cccc99", [*launch(0), request(1, tokens=99_000)])
    ambiguous = command(tmp_path, "--session", "cc")
    assert (ambiguous.returncode, ambiguous.stdout) == (0, "")
    assert command(tmp_path, env={"CONTEXTO_SESSION": "cccc3"}).stdout == "ctx 33k/150k\n"
    del held


def test_the_prefix_rule_is_the_readers_rule(tmp_path):
    for name in ("abc123", "abd999", "zz"):
        write_log(tmp_path, name, launch(0))
    for given in ("abc123", "abc", "ab", "zz", "z", "nope", "", "..", "a/b"):
        try:
            expected = resolve_session(tmp_path, given)
        except Exception:
            expected = None
        assert statusline.find_session(tmp_path, given) == expected, given


def test_no_session_means_no_output_and_exit_0(tmp_path):
    write_log(tmp_path, "recent", [*launch(0), request(1, tokens=50_000)])  # never "the most recent session"
    pages(tmp_path, "recent", 4)
    for stdin in ("", claude_stdin(), "not json", "[]", '{"session_id": 7}', '{"session_id": "../recent"}', None):
        done = command(tmp_path, stdin=stdin)
        assert (done.returncode, done.stdout, done.stderr) == (0, "", ""), stdin
    for args, env in ((("--session", "nope"), {}), ((), {"CONTEXTO_SESSION": "nope"}),
                      (("--session", ""), {"CONTEXTO_SESSION": "recent"})):
        done = command(tmp_path, *args, stdin=claude_stdin(), env=env)
        assert (done.returncode, done.stdout, done.stderr) == (0, "", ""), (args, env)
    # A session named by the option or the environment that is gone does not fall through to stdin's.
    held = serving(tmp_path, "aaaa11")
    assert command(tmp_path, "--session", "nope", stdin=claude_stdin()).stdout == ""
    assert command(tmp_path, stdin=claude_stdin(), env={"CONTEXTO_SESSION": "nope"}).stdout == ""
    del held


def test_stdin_names_only_a_session_that_is_serving_now(tmp_path):
    """Claude Code's session id is looked up in the sessions' records, and counts only while a launch has
    that session open: after ``contexto run`` ended, a plain ``claude --resume`` of the same conversation
    is not running through Contexto, and the old session's numbers would be wrong."""
    store = serving(tmp_path, "aaaa11")
    assert command(tmp_path, stdin=claude_stdin()).stdout == "ctx 11k/150k\n"
    assert statusline.session_live(tmp_path, "aaaa11") is launcher.session_in_use(tmp_path, "aaaa11") is True
    store.release()
    assert statusline.session_live(tmp_path, "aaaa11") is launcher.session_in_use(tmp_path, "aaaa11") is False
    assert command(tmp_path, stdin=claude_stdin()).stdout == ""
    # Two sessions served the conversation (a resume that got a new session): the one in use shows.
    old = serving(tmp_path, "bbbb22", seen_at=2_000_000_000.0)
    new = serving(tmp_path, "cccc33", seen_at=IN_RUN)
    # Both open (two launches on one conversation): either could be this window's, so neither is shown
    # (Reviewer B, PR #276: picking the last to see it showed one launch's numbers in the other's window).
    assert command(tmp_path, stdin=claude_stdin()).stdout == ""
    assert statusline.serving_session(tmp_path, CLAUDE_ID) is None
    old.release()
    assert command(tmp_path, stdin=claude_stdin()).stdout == "ctx 33k/150k\n"
    new.release()
    assert command(tmp_path, stdin=claude_stdin()).stdout == ""


def test_the_environment_s_session_counts_only_while_open_and_serving_the_claude_code_session(tmp_path):
    """Reviewer B, PR #276: ``CONTEXTO_SESSION`` left set in a shell (the readers default to it) made a plain
    ``claude`` show that session's old numbers under a conversation it never served. When stdin names a
    Claude Code session, the variable's session is shown only while a launch has it open and its records
    say it served that conversation."""
    store = serving(tmp_path, "aaaa11")  # serves CLAUDE_ID
    write_log(tmp_path, "zzzz99", [*launch(0), request(1, tokens=99_000)])  # served nothing, never opened

    def shown(stdin, session="aaaa11", *args):
        done = command(tmp_path, *args, stdin=stdin, env={"CONTEXTO_SESSION": session})
        assert (done.returncode, done.stderr) == (0, "")
        return done.stdout

    assert shown(claude_stdin()) == "ctx 11k/150k\n"  # open, and serving this conversation
    assert shown(claude_stdin(OTHER_ID)) == ""  # open, but this conversation is not one it serves
    assert shown(claude_stdin(), "zzzz99") == ""
    store.release()
    assert shown(claude_stdin()) == ""  # served it, but no launch has it open: a plain `claude --resume`
    assert shown(claude_stdin(OTHER_ID)) == ""  # the review's case
    # Without a Claude Code session on stdin (run by hand), the variable names the session, as for every reader.
    for stdin in ("", None, "not json", '{"session_id": 7}', '{"model": "x"}'):
        assert shown(stdin) == "ctx 11k/150k\n", stdin
    # --session is the user's own choice: shown whatever stdin says.
    assert shown(claude_stdin(OTHER_ID), "aaaa11", "--session", "zzzz99") == "ctx 99k/150k\n"
    assert shown(claude_stdin(OTHER_ID), "", "--session=aaaa11") == "ctx 11k/150k\n"


def test_a_session_reopened_for_another_conversation_does_not_serve_the_old_one(tmp_path):
    """Reviewer A, PR #276: "has served" meant any run. Session aaaa11 served CLAUDE_ID in an earlier run, and
    a new launch has it open for OTHER_ID; a plain `claude --resume CLAUDE_ID` elsewhere, whose own status line
    calls `contexto statusline`, showed aaaa11's numbers, with and without $CONTEXTO_SESSION. A conversation
    counts only when it was recorded in the open launch, after its proxy started."""
    store = SessionStore(tmp_path, "aaaa11")
    store.record_cli_session("claude", CLAUDE_ID, 1_799_999_000.0)  # the earlier run
    store.record_cli_session("claude", OTHER_ID, IN_RUN)  # this run
    write_log(tmp_path, "aaaa11", [*launch(-2_000), request(-1_999, tokens=5_000),
                                   *launch(0), request(1, tokens=11_000)])
    assert command(tmp_path, stdin=claude_stdin()).stdout == ""
    assert command(tmp_path, stdin=claude_stdin(), env={"CONTEXTO_SESSION": "aaaa11"}).stdout == ""
    assert command(tmp_path, stdin=claude_stdin(OTHER_ID)).stdout == "ctx 11k/150k\n"
    assert statusline.serving_session(tmp_path, CLAUDE_ID) is None
    assert statusline.serves_now(tmp_path, "aaaa11", OTHER_ID) is True
    # The same conversation resumed in the new launch is recorded again at its first request there.
    store.record_cli_session("claude", CLAUDE_ID, IN_RUN + 5)
    assert command(tmp_path, stdin=claude_stdin()).stdout == "ctx 11k/150k\n"
    # The start time is compared in full: a last request of the earlier run in the same second as the
    # new run's start is still the earlier run's (Reviewer A, PR #276 round 3).
    started = {"event": "proxy_start", "port": 1, "ts": "2027-01-15T08:00:00.600000Z"}
    write_log(tmp_path, "aaaa11", [started, launch(0)[1] | {"ts": started["ts"]}, request(1, tokens=11_000)])
    at = statusline.run_started(tmp_path, "aaaa11")
    assert at == 1_800_000_000.6 or abs(at - 1_800_000_000.6) < 1e-6
    store.record_cli_session("claude", OTHER_ID, 1_800_000_000.2)
    assert statusline.serves_in_this_run(tmp_path, "aaaa11", 1_800_000_000.2) is False
    assert statusline.serves_in_this_run(tmp_path, "aaaa11", 1_800_000_000.7) is True
    # A line that only mentions the word is not a proxy start; the newest real one counts.
    write_log(tmp_path, "aaaa11", [*launch(0), request(1, tokens=11_000),
                                   {"event": "note", "text": "proxy_start", "ts": "2027-01-15T09:00:00Z"}])
    assert statusline.run_started(tmp_path, "aaaa11") == 1_800_000_000.0
    # A session `contexto serve` opened after the launch is the service's now: it shows nothing, rather than
    # the last launch's bound under the service's requests (Reviewer B, PR #276 round 4).
    write_log(tmp_path, "aaaa11", [*launch(0), request(1, tokens=11_000),
                                   {"event": "serve_open", "cli": "claude", "session": "aaaa11", "ts": ts(5)}])
    assert statusline.run_started(tmp_path, "aaaa11") is None
    assert statusline.serving_session(tmp_path, CLAUDE_ID) is None
    write_log(tmp_path, "aaaa11", [{"event": "serve_open", "cli": "claude", "session": "aaaa11", "ts": ts(-9)},
                                   *launch(0), request(1, tokens=11_000)])  # a launch after the service: shown
    assert statusline.run_started(tmp_path, "aaaa11") == 1_800_000_000.0
    # A log with no proxy start, or none that can be read, serves nothing.
    write_log(tmp_path, "aaaa11", [request(1, tokens=11_000)])
    assert statusline.serving_session(tmp_path, CLAUDE_ID) is None
    del store


def test_the_served_lookup_reads_what_the_launcher_records(tmp_path):
    store = serving(tmp_path, "aaaa11")
    store.record_cli_session("claude", OTHER_ID, 5.0)
    SessionStore(tmp_path, "empty1")
    (tmp_path / "sessions" / "junk").mkdir()
    (tmp_path / "sessions" / "junk" / "meta.json").write_text("[not an object")
    (tmp_path / "sessions" / "afile").write_text("x")
    expected = sorted((seen, name) for seen, name, cli, _ in launcher.served_sessions(tmp_path) if cli == OTHER_ID)
    assert sorted(statusline.served_by(tmp_path, OTHER_ID)) == expected == [(5.0, "aaaa11")]
    assert statusline.served_by(tmp_path, "never-seen") == [] == statusline.served_by(tmp_path / "none", OTHER_ID)


def test_the_command_does_not_wait_for_a_terminal_or_a_silent_pipe(tmp_path):
    write_log(tmp_path, "s", [*launch(0), request(1, tokens=50_000)])
    # A terminal on stdin (run by hand): not read at all.
    leader, follower = pty.openpty()
    try:
        started = time.monotonic()
        done = subprocess.run([sys.executable, "-P", "-m", "contexto", "statusline"], stdin=follower,
                              capture_output=True, text=True, timeout=30, env=clean_env(tmp_path))
        assert (done.returncode, done.stdout) == (0, "")
        # A pipe nobody writes to or closes: given up on after a moment.
        proc = subprocess.Popen([sys.executable, "-P", "-m", "contexto", "statusline"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=clean_env(tmp_path))
        out, err = proc.stdout.read(), proc.stderr.read()  # ends when the command gives up and exits
        assert (proc.wait(timeout=30), out, err) == (0, "", "")
        proc.stdin.close()
        assert time.monotonic() - started < 25
    finally:
        os.close(leader)
        os.close(follower)
    # With the session named, stdin is never read: a silent pipe costs nothing.
    proc = subprocess.Popen([sys.executable, "-P", "-m", "contexto", "statusline", "--session", "s"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=clean_env(tmp_path))
    assert proc.stdout.read() == "ctx 50k/150k\n" and proc.wait(timeout=30) == 0
    proc.stdin.close()


def test_a_terminal_on_stdin_is_never_waited_on(monkeypatch):
    """Reviewer A, PR #276: without the terminal check every test still passed, because the half-second
    deadline also ends the wait. The check is held here by the code path, not by a time: a terminal is
    not even polled."""
    import select

    polled = []
    real = select.select
    monkeypatch.setattr(select, "select", lambda *a: (polled.append(a), real(*a))[1])
    leader, follower = pty.openpty()
    try:
        with os.fdopen(os.dup(follower), "rb", buffering=0) as terminal:
            assert statusline.stdin_object(terminal) is None
        assert polled == []
        read, write = os.pipe()  # the same call on a pipe does poll it: the patch above is seen
        os.write(write, claude_stdin().encode())
        os.close(write)
        with os.fdopen(read, "rb", buffering=0) as pipe:
            assert statusline.stdin_object(pipe)["session_id"] == CLAUDE_ID
        assert polled
    finally:
        os.close(leader)
        os.close(follower)


def test_stdin_json_is_read_without_waiting_for_the_pipe_to_close(tmp_path):
    held = serving(tmp_path, "aaaa11")
    proc = subprocess.Popen([sys.executable, "-P", "-m", "contexto", "statusline"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=clean_env(tmp_path))
    proc.stdin.write(claude_stdin())
    proc.stdin.flush()  # written, not closed
    assert proc.stdout.read() == "ctx 11k/150k\n" and proc.wait(timeout=30) == 0
    proc.stdin.close()
    huge = json.dumps({"session_id": CLAUDE_ID, "padding": "x" * (2 << 20)})
    assert command(tmp_path, stdin=huge).stdout == ""  # more than a status line's input: not parsed
    del held


def test_the_command_never_fails_or_prints_a_traceback(tmp_path, monkeypatch, capsys):
    write_log(tmp_path, "s", [*launch(0), request(1, tokens=50_000)])
    for args in (("--no-such-option",), ("--session",), ("extra", "words"), ("--home",)):
        done = command(tmp_path, *args)
        assert done.returncode == 0 and done.stdout == "" and "Traceback" not in done.stderr, args
        assert "usage: contexto statusline" in done.stderr
    (tmp_path / "file").write_text("x")
    for home in (tmp_path / "file", tmp_path / "missing"):
        done = command(home, "--session", "s")
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    # Anything unexpected inside: nothing printed, exit 0.
    monkeypatch.setattr(statusline, "status_line", lambda *a, **k: 1 / 0)
    assert statusline.main(["--session", "s", "--home", str(tmp_path)]) == 0
    assert capsys.readouterr() == ("", "")
    # A closed stdout (the status line was cancelled) is not an error either.
    monkeypatch.undo()
    closed = open(os.devnull, "w")
    closed.close()
    assert statusline.main(["--session", "s", "--home", str(tmp_path)], stdout=closed) == 0


def test_help_lists_the_command_and_its_options(tmp_path):
    listed = subprocess.run([sys.executable, "-m", "contexto", "--help"], capture_output=True, text=True, timeout=60)
    assert "statusline" in listed.stdout
    done = command(tmp_path, "--help")
    assert done.returncode == 0 and "--session" in done.stdout and "--color" in done.stdout


def test_the_command_starts_without_the_proxy_the_store_or_the_version_lookup(tmp_path):
    """A status line runs after every assistant message, so it loads only what it needs: not the launcher,
    the proxy, the engine or the store, and not ``importlib.metadata`` (the package's version lookup)."""
    write_log(tmp_path, "s", [*launch(0), request(1, tokens=50_000)])
    code = ("import sys\nfrom contexto import cli\ncode = cli.main(['statusline', '--session', 's'])\n"
            "heavy = [m for m in ('contexto.launcher', 'contexto.proxy', 'contexto.rewrite', 'contexto.store', "
            "'contexto.mcp', 'importlib.metadata', 'contexto.commands.run') if m in sys.modules]\n"
            "print(code, heavy)")
    done = subprocess.run([sys.executable, "-P", "-c", code], capture_output=True, text=True, timeout=60,
                          env=clean_env(tmp_path))
    assert done.stdout == "ctx 50k/150k\n0 []\n", done.stderr
    # The version is still there for whoever asks.
    import contexto

    assert isinstance(contexto.__version__, str) and contexto.__version__


def test_the_command_is_fast_on_a_six_hour_log(tmp_path):
    directory = write_log(tmp_path, "long", long_session_log())
    assert (directory / "log.jsonl").stat().st_size > 2_500_000
    pages(tmp_path, "long", 1_200)
    status_line(tmp_path, "long")  # the imports, once
    started = time.perf_counter()
    line = status_line(tmp_path, "long")
    elapsed = time.perf_counter() - started
    assert line == "ctx 136k/150k · 1,200 paged · 223 recalls · 222 searches"
    # Measured at a few milliseconds (docs/statusline.md); the limit only catches a change that makes it
    # read the whole log again, on any CI machine.
    assert elapsed < 1.0, f"{elapsed * 1000:.0f} ms"


# What `contexto run claude` installs ------------------------------------------------------------------

def layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """(the user's home, a repository, the directory claude runs in, the managed settings directory)."""
    home, repo, managed = tmp_path / "userhome", tmp_path / "repo", tmp_path / "managed"
    for directory in (home / ".claude", repo / ".git", repo / "sub" / ".claude", repo / ".claude", managed):
        directory.mkdir(parents=True)
    return home, repo, repo / "sub", managed


def decide(tmp_path: Path, cwd: Path, home: Path, managed: Path, **kwargs):
    kwargs.setdefault("env", {})
    return launch_status_line("s1", tmp_path / "store", cwd, user_home=home, managed_dir=managed, **kwargs)


def put(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def test_the_status_line_is_added_when_no_settings_file_has_one(tmp_path):
    home, repo, cwd, managed = layout(tmp_path)
    # Settings files that say nothing about a status line or hooks do not stop it.
    put(home / ".claude" / "settings.json", {"model": "claude-haiku-5-5", "disableAllHooks": False})
    put(cwd / ".claude" / "settings.json", {"permissions": {"allow": []}})
    put(managed / "managed-settings.json", {"env": {}})
    # An empty file holds no status line: there is nothing in it to replace.
    put(cwd / ".claude" / "settings.local.json", "")
    put(repo / ".claude" / "settings.local.json", " \n")
    added, note = decide(tmp_path, cwd, home, managed)
    assert note == "added" and set(added) == {"statusLine"}
    assert set(added["statusLine"]) == {"type", "command"} and added["statusLine"]["type"] == "command"
    argv = shlex.split(added["statusLine"]["command"])
    assert argv[:5] == [sys.executable, "-P", "-m", "contexto", "statusline"]
    assert "--session=s1" in argv and f"--home={tmp_path / 'store'}" in argv and "--color" in argv
    # The files a later `/statusline` could write are named, so the command can tell (below).
    watched = [a.removeprefix("--own-settings=") for a in argv if a.startswith("--own-settings=")]
    assert watched == [str(repo / ".claude" / "settings.local.json"), str(cwd / ".claude" / "settings.local.json"),
                       str(cwd / ".claude" / "settings.json"), str(home / ".claude" / "settings.json")]
    # NO_COLOR in the launcher's environment: no colour asked for.
    plain, _ = decide(tmp_path, cwd, home, managed, env={"NO_COLOR": "1"})
    assert "--color" not in shlex.split(plain["statusLine"]["command"])


SCOPES = {
    "user": lambda home, repo, cwd, managed: home / ".claude" / "settings.json",
    "project": lambda home, repo, cwd, managed: cwd / ".claude" / "settings.json",
    "local": lambda home, repo, cwd, managed: cwd / ".claude" / "settings.local.json",
    "main-checkout-local": lambda home, repo, cwd, managed: repo / ".claude" / "settings.local.json",
    "managed": lambda home, repo, cwd, managed: managed / "managed-settings.json",
    "managed-drop-in": lambda home, repo, cwd, managed: managed / "managed-settings.d" / "10-team.json",
}


@pytest.mark.parametrize("scope", sorted(SCOPES))
def test_a_status_line_in_any_settings_file_is_never_replaced(tmp_path, scope):
    home, repo, cwd, managed = layout(tmp_path)
    path = put(SCOPES[scope](home, repo, cwd, managed), OWN)
    kind = scope.removesuffix("-drop-in")
    assert decide(tmp_path, cwd, home, managed) == ({}, f"own:{kind}")
    # Any value under the key counts, even one Claude Code would reject: the key is the user's.
    for value in (None, "", {}, {"type": "command"}):
        put(path, {"statusLine": value})
        assert decide(tmp_path, cwd, home, managed) == ({}, f"own:{kind}"), value


@pytest.mark.parametrize("scope", sorted(SCOPES))
def test_hooks_turned_off_in_any_settings_file_means_no_status_line(tmp_path, scope):
    home, repo, cwd, managed = layout(tmp_path)
    path = put(SCOPES[scope](home, repo, cwd, managed), {"disableAllHooks": True})
    assert decide(tmp_path, cwd, home, managed) == ({}, f"hooks_off:{scope.removesuffix('-drop-in')}")
    put(path, {"disableAllHooks": False})
    assert decide(tmp_path, cwd, home, managed)[1] == "added"


@pytest.mark.parametrize("scope", sorted(SCOPES))
@pytest.mark.parametrize("damage", ["malformed", "not_an_object", "not_utf8", "unreadable", "directory", "fifo",
                                    "dangling_link", "too_deep", "too_large"])
def test_a_settings_file_that_cannot_be_read_means_no_status_line(tmp_path, scope, damage):
    """If in doubt, do not add it: a file Contexto cannot read may hold the user's status line."""
    home, repo, cwd, managed = layout(tmp_path)
    path = SCOPES[scope](home, repo, cwd, managed)
    path.parent.mkdir(parents=True, exist_ok=True)
    if damage == "malformed":
        path.write_text('{"statusLine": {"type": "command", "command": "echo mine"},}')
    elif damage == "not_an_object":
        path.write_text('["statusLine"]')
    elif damage == "too_large":
        path.write_text(json.dumps({"padding": "x" * (statusline.SETTINGS_LIMIT + 1)}))
    elif damage == "not_utf8":
        path.write_bytes(b'{"model": "\xff\xfe"}')
    elif damage == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root reads any file")
        put(path, OWN).chmod(0)
    elif damage == "directory":
        path.mkdir()
    elif damage == "fifo":
        os.mkfifo(path)
    elif damage == "dangling_link":
        path.symlink_to(tmp_path / "gone.json")
    elif damage == "too_deep":
        path.write_text("[" * 100_000 + "]" * 100_000)
    started = time.monotonic()
    try:
        assert decide(tmp_path, cwd, home, managed) == ({}, f"unreadable:{scope.removesuffix('-drop-in')}")
    finally:
        if damage == "unreadable":
            path.chmod(0o600)
    assert time.monotonic() - started < 5  # a FIFO is never waited on


def test_a_symlinked_settings_file_is_read_through_the_link(tmp_path):
    home, repo, cwd, managed = layout(tmp_path)
    put(tmp_path / "dotfiles" / "claude.json", OWN)
    (home / ".claude" / "settings.json").symlink_to(tmp_path / "dotfiles" / "claude.json")
    assert decide(tmp_path, cwd, home, managed) == ({}, "own:user")


def test_the_user_file_follows_claude_config_dir_and_the_managed_directory_its_rules(tmp_path):
    home, repo, cwd, managed = layout(tmp_path)
    put(tmp_path / "cfg" / "settings.json", OWN)
    assert decide(tmp_path, cwd, home, managed)[1] == "added"  # not the user file without CLAUDE_CONFIG_DIR
    assert decide(tmp_path, cwd, home, managed, env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}) == ({}, "own:user")
    # With CLAUDE_CONFIG_DIR, ~/.claude/settings.json is not a file Claude Code reads.
    put(home / ".claude" / "settings.json", OWN)
    (tmp_path / "cfg" / "settings.json").unlink()
    added, note = decide(tmp_path, cwd, home, managed, env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")})
    assert note == "added" and f"--own-settings={tmp_path / 'cfg' / 'settings.json'}" in shlex.split(
        added["statusLine"]["command"])
    (home / ".claude" / "settings.json").unlink()
    # Drop-ins: only *.json that is not hidden, as Claude Code reads them.
    for name in (".hidden.json", "notes.txt"):
        put(managed / "managed-settings.d" / name, OWN)
    assert decide(tmp_path, cwd, home, managed)[1] == "added"
    # Managed settings that allow only managed hooks would blank a status line from --settings.
    put(managed / "managed-settings.json", {"allowManagedHooksOnly": True})
    assert decide(tmp_path, cwd, home, managed) == ({}, "managed_hooks_only")
    # No managed directory on this platform, or none there: nothing to read.
    assert launch_status_line("s1", tmp_path / "store", cwd, user_home=home, managed_dir=tmp_path / "none",
                              env={})[1] == "added"
    assert statusline.managed_settings_dir("darwin") == Path("/Library/Application Support/ClaudeCode")
    assert statusline.managed_settings_dir("linux") == Path("/etc/claude-code")
    assert statusline.managed_settings_dir("win32") is None


def test_the_files_checked_are_the_ones_claude_code_reads_in_its_order(tmp_path):
    from contexto.upstream import claude_settings_order

    home, repo, cwd, managed = layout(tmp_path)
    put(managed / "managed-settings.d" / "20-b.json", {})
    put(managed / "managed-settings.d" / "10-a.json", {})
    files = settings_files(cwd, home, {}, managed)
    assert files[:3] == [("managed", managed / "managed-settings.json"),
                         ("managed", managed / "managed-settings.d" / "10-a.json"),
                         ("managed", managed / "managed-settings.d" / "20-b.json")]
    assert files[3:] == claude_settings_order(cwd, home, {})  # D52's order, CLAUDE_CONFIG_DIR included
    # The reason names the highest file that has one.
    for scope in ("user", "project", "local", "main-checkout-local", "managed"):
        put(SCOPES[scope](home, repo, cwd, managed), OWN)
        assert decide(tmp_path, cwd, home, managed) == ({}, f"own:{scope}")


def test_the_opt_outs(tmp_path):
    home, repo, cwd, managed = layout(tmp_path)
    assert decide(tmp_path, cwd, home, managed, enabled=False) == ({}, "off:flag")
    for value in ("1", "true", "yes", "anything"):
        assert decide(tmp_path, cwd, home, managed, env={"CONTEXTO_NO_STATUSLINE": value}) == ({}, "off:env"), value
    for value in ("", "0", "false", "no", " No "):
        assert decide(tmp_path, cwd, home, managed, env={"CONTEXTO_NO_STATUSLINE": value})[1] == "added", value
    # An opt-out is reported even when a settings file would have decided the same.
    put(home / ".claude" / "settings.json", OWN)
    assert decide(tmp_path, cwd, home, managed, enabled=False) == ({}, "off:flag")
    from contexto import cli

    args = cli.build_parser().parse_args(["run", "--no-statusline", "claude"])
    assert launcher.options_from_args(args).statusline is False
    assert launcher.options_from_args(cli.build_parser().parse_args(["run", "claude"])).statusline is True
    assert launcher.RUN_OPTIONS["--no-statusline"] is False


def test_a_failure_while_deciding_adds_nothing(tmp_path, monkeypatch):
    home, repo, cwd, managed = layout(tmp_path)
    monkeypatch.setattr(statusline, "settings_files", lambda *a, **k: 1 / 0)
    assert decide(tmp_path, cwd, home, managed) == ({}, "error")


@pytest.mark.parametrize("name", ["plain", "with space", "it's", 'say "hi"', "dollar $HOME `id`", "semi;colon&amp",
                                  "back\\slash", "new\nline", "tab\there", "glob*?[x]", "~tilde", "-dash", "ünï"])
def test_the_command_is_quoted_for_a_shell(tmp_path, name):
    """The store home can hold anything a directory name can; the command must reach ``contexto
    statusline`` with it intact, through the shell Claude Code runs it in."""
    home = tmp_path / name / "store"
    write_log(home, "s1", [*launch(0), request(1, tokens=50_000)])
    own = tmp_path / name / "settings.json"
    text = installed_command("s1", home, [own], color=False)
    assert shlex.split(text) == [sys.executable, "-P", "-m", "contexto", "statusline", "--session=s1",
                                 f"--home={home}", f"--own-settings={own}"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CONTEXTO_")}
    for shell in ("/bin/sh", "/bin/bash"):
        if not os.path.exists(shell):
            continue
        done = subprocess.run([shell, "-c", text], capture_output=True, text=True, timeout=60, env=env,
                              cwd=tmp_path, stdin=subprocess.DEVNULL)
        assert (done.returncode, done.stdout, done.stderr) == (0, "ctx 50k/150k\n", ""), shell
    # And the user's file gaining a status line is seen through the same quoting.
    put(own, OWN)
    done = subprocess.run(["/bin/sh", "-c", text], capture_output=True, text=True, timeout=60, env=env, cwd=tmp_path)
    assert done.stdout == NOTICE_LINE


def test_a_session_id_that_starts_with_a_dash_stays_one_argument(tmp_path):
    write_log(tmp_path, "-x", [*launch(0), request(1, tokens=50_000)])
    text = installed_command("-x", tmp_path, [], color=True)
    assert shlex.split(text)[-3:] == ["--session=-x", f"--home={tmp_path}", "--color"]
    done = subprocess.run(["/bin/sh", "-c", text], capture_output=True, text=True, timeout=60,
                          env={k: v for k, v in os.environ.items() if not k.startswith("CONTEXTO_")})
    assert done.stdout == "ctx 50k/150k\n"


def test_a_value_claude_code_might_expand_is_not_put_in_the_command(tmp_path):
    """Claude Code expands ``${VAR}`` in an MCP config (D55). It did not in a status line command on
    2.1.318 and 2.1.322, but a path cannot be escaped if a later version does, so a command that would
    hold ``${`` is not installed."""
    home, repo, cwd, managed = layout(tmp_path)
    odd = tmp_path / "${HOME}"
    odd.mkdir()
    assert launch_status_line("s1", tmp_path / "store", odd, user_home=home, managed_dir=managed,
                              env={}) == ({}, "unsafe_path")


# The mid-session notice ------------------------------------------------------------------------------

def test_a_status_line_the_user_sets_up_mid_session_gets_a_notice_in_place_of_the_numbers(tmp_path):
    """``/statusline`` in a session started by ``contexto run`` writes the user's settings, which the
    launcher's ``--settings`` outranks until the next launch. The installed command sees the new key at
    its next run and says so; it never runs the user's command."""
    write_log(tmp_path, "s1", [*launch(0), request(1, tokens=50_000)])
    user, project = tmp_path / "user.json", tmp_path / "project.json"
    marker = tmp_path / "ran"
    args = ("--session", "s1", f"--own-settings={user}", f"--own-settings={project}")
    assert command(tmp_path, *args).stdout == "ctx 50k/150k\n"  # neither file exists yet
    put(user, {"model": "x"})
    assert command(tmp_path, *args).stdout == "ctx 50k/150k\n"
    put(project, {"statusLine": {"type": "command", "command": f"touch {shlex.quote(str(marker))}"}})
    done = command(tmp_path, *args)
    assert (done.returncode, done.stdout, done.stderr) == (0, NOTICE_LINE, "")
    assert NOTICE_LINE == f"ctx: {RESTART_NOTICE}\n" and "\n" not in RESTART_NOTICE
    # Reason first, and whole on a 40-column row (the first notice was 68 characters, cut before its reason).
    assert NOTICE_LINE.startswith("ctx: your status line") and len(NOTICE_LINE.rstrip("\n")) <= 40
    assert not marker.exists()
    # The notice does not need the session: it is about the settings.
    assert command(tmp_path, "--session", "gone", f"--own-settings={project}").stdout == NOTICE_LINE
    # Stopped paging matters more than the notice (Reviewer B, PR #276: the notice had hidden it): the
    # marker stays first, and the notice follows it.
    write_log(tmp_path, "s1", [*launch(0), request(1, tokens=212_000, degraded=True, events=(store_error(), ENTER))])
    assert command(tmp_path, *args).stdout == f"ctx PAGING STOPPED (disk full) · {RESTART_NOTICE}\n"
    assert command(tmp_path, *args, "--color").stdout == (
        f"ctx \x1b[1;31mPAGING STOPPED (disk full)\x1b[0m · {RESTART_NOTICE}\n")
    write_log(tmp_path, "s1", [*launch(0), request(1, tokens=50_000)])
    # A file that cannot be read mid-session is not a reason to hide the numbers.
    for content in ("{broken", "[]", "", "{}"):
        put(project, content)
        assert command(tmp_path, *args).stdout == "ctx 50k/150k\n", content
    project.unlink()
    os.mkfifo(project)
    assert command(tmp_path, *args).stdout == "ctx 50k/150k\n"
    # Without --own-settings (a user's own script calling the command) no settings file is read.
    put(user, OWN)
    assert command(tmp_path, "--session", "s1").stdout == "ctx 50k/150k\n"


# Through `contexto run claude` ------------------------------------------------------------------------

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
    (tmp_path / "work").mkdir()
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("ANTHROPIC_", "CLAUDE", "OPENRIG_", "RIGGED_", "CONTEXTO_")) and k != "NO_COLOR"}
    clean.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", HOME=str(tmp_path / "userhome"),
                 CONTEXTO_HOME=str(tmp_path / "it's a store"), FAKE_RECORD=str(tmp_path / "record.json"),
                 FAKE_REQUEST="1", FAKE_CLAUDE_SESSION=CLAUDE_ID)
    return clean


def run(env, *args, cwd) -> tuple[subprocess.CompletedProcess, dict, dict]:
    """Run ``contexto run ...``; return it, the fake claude's record, and the session's ``launch`` event."""
    done = subprocess.run([sys.executable, "-m", "contexto", "run", *args], env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=60)
    record = json.loads(Path(env["FAKE_RECORD"]).read_text())
    log = Path(env["CONTEXTO_HOME"]) / "sessions" / "s1" / "log.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    return done, record, [e for e in events if e["event"] == "launch"][-1]


def expected(env, cwd: Path) -> tuple[dict, str]:
    """What the launcher should decide here, with this machine's own managed settings (if it has any)."""
    return launch_status_line("s1", Path(env["CONTEXTO_HOME"]), cwd.resolve(), user_home=Path(env["HOME"]), env=env)


def test_run_claude_installs_a_status_line_that_shows_the_session(env, upstream, tmp_path):
    cwd = tmp_path / "work"
    added, note = expected(env, cwd)
    done, record, launched = run(env, "--upstream", upstream, "--session", "s1", "claude", "-p", "hi", cwd=cwd)
    assert done.returncode == 0, done.stderr
    assert launched["statusline"] == note
    if note != "added":  # a machine whose managed settings decide otherwise
        assert "statusLine" not in record["settings"]
        return
    assert record["settings"]["statusLine"] == added["statusLine"]
    assert set(record["settings"]) == {"env", "permissions", "statusLine"}
    # The command works as Claude Code runs it: a shell, the session's JSON on stdin, and nothing of the
    # launcher's environment (neither CONTEXTO_HOME nor CONTEXTO_SESSION).
    bare = {"PATH": os.environ["PATH"], "HOME": env["HOME"]}
    shown = subprocess.run(["/bin/sh", "-c", record["settings"]["statusLine"]["command"]], input=claude_stdin(),
                           capture_output=True, text=True, timeout=60, env=bare, cwd=cwd)
    assert (shown.returncode, shown.stderr) == (0, "") and shown.stdout.startswith("ctx ")
    assert shown.stdout.rstrip("\n").endswith("/150k"), shown.stdout
    # A status line set up after the launch (as /statusline does) is noticed by the same command.
    put(Path(env["HOME"]) / ".claude" / "settings.json", OWN)
    later = subprocess.run(["/bin/sh", "-c", record["settings"]["statusLine"]["command"]], input=claude_stdin(),
                           capture_output=True, text=True, timeout=60, env=bare, cwd=cwd)
    assert later.stdout == NOTICE_LINE
    assert "statusline" not in done.stderr.lower()  # the launcher says nothing about it on stderr


@pytest.mark.parametrize("where", ["user", "config_dir", "project", "local"])
def test_run_claude_never_writes_a_status_line_beside_the_user_s(env, upstream, tmp_path, where):
    cwd = tmp_path / "work"
    if where == "config_dir":
        env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "cfg")
    path = {"user": Path(env["HOME"]) / ".claude" / "settings.json", "config_dir": tmp_path / "cfg" / "settings.json",
            "project": cwd / ".claude" / "settings.json", "local": cwd / ".claude" / "settings.local.json"}[where]
    put(path, OWN)
    before = path.read_bytes()
    done, record, launched = run(env, "--upstream", upstream, "--session", "s1", "claude", "-p", "hi", cwd=cwd)
    assert done.returncode == 0, done.stderr
    assert "statusLine" not in record["settings"] and set(record["settings"]) == {"env", "permissions"}
    kind = "user" if where == "config_dir" else where
    assert launched["statusline"] in (f"own:{kind}", expected(env, cwd)[1])  # a managed file may be named first
    assert launched["statusline"].startswith("own:")
    assert path.read_bytes() == before  # the user's file is never written


@pytest.mark.parametrize("how", ["flag", "env", "broken", "hooks_off"])
def test_run_claude_adds_no_status_line_when_told_not_to_or_in_doubt(env, upstream, tmp_path, how):
    cwd = tmp_path / "work"
    args = ["--upstream", upstream, "--session", "s1"]
    if how == "flag":
        args.append("--no-statusline")
    elif how == "env":
        env["CONTEXTO_NO_STATUSLINE"] = "1"
    elif how == "broken":
        put(Path(env["HOME"]) / ".claude" / "settings.json", '{"statusLine": ')
    else:
        put(cwd / ".claude" / "settings.json", {"disableAllHooks": True})
    done, record, launched = run(env, *args, "claude", "-p", "hi", cwd=cwd)
    assert done.returncode == 0, done.stderr
    assert "statusLine" not in record["settings"]
    wanted = {"flag": "off:flag", "env": "off:env", "broken": "unreadable:user", "hooks_off": "hooks_off:project"}[how]
    assert launched["statusline"] in (wanted, expected(env, cwd)[1]) and launched["statusline"] != "added"


def test_run_codex_takes_the_option_and_ignores_it():
    """Codex has no status line setting: the option is accepted before ``codex`` (it belongs to ``contexto
    run``) and nothing is written for it."""
    from contexto import cli

    args = cli.build_parser().parse_args(["run", "--no-statusline", "codex", "exec", "hi"])
    assert args.cli == "codex" and args.no_statusline is True and args.cli_args == ["exec", "hi"]


def test_the_documented_script_appends_contexto_s_line_to_its_own(tmp_path):
    """docs/statusline.md gives a status line script that calls ``contexto statusline``; it is run here as
    written, with ``contexto`` on PATH, inside and outside a session Contexto serves."""
    docs = (Path(__file__).resolve().parent.parent / "docs" / "statusline.md").read_text()
    script = docs.split("```sh\n#!/bin/sh\n", 1)[1].split("```", 1)[0]
    assert "contexto statusline" in script
    bin_dir, work = tmp_path / "bin", tmp_path / "my project"
    bin_dir.mkdir()
    work.mkdir()
    shim = bin_dir / "contexto"
    shim.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -P -m contexto \"$@\"\n")
    shim.chmod(0o755)
    path = tmp_path / "statusline.sh"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    held = serving(tmp_path, "aaaa11")
    env = clean_env(tmp_path, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def shown(stdin: str, **extra: str) -> tuple[int, str, str]:
        done = subprocess.run([str(path)], input=stdin, capture_output=True, text=True, timeout=60, cwd=work,
                              env={**env, **extra})
        return done.returncode, done.stdout, done.stderr

    # Inside `contexto run claude`: the launcher's CONTEXTO_SESSION reaches the script (measured).
    assert shown(claude_stdin(), CONTEXTO_SESSION="aaaa11") == (0, "my project | ctx 11k/150k\n", "")
    # Without it, the Claude Code session on stdin names the Contexto session serving it.
    assert shown(claude_stdin()) == (0, "my project | ctx 11k/150k\n", "")
    # A plain `claude` session Contexto is not serving, and a machine without contexto: the script's own line.
    assert shown(claude_stdin(OTHER_ID)) == (0, "my project\n", "")
    assert shown(claude_stdin(), PATH="/usr/bin:/bin") == (0, "my project\n", "")
    # The same plain session with CONTEXTO_SESSION left set in the shell (Reviewer B, PR #276): not its line.
    assert shown(claude_stdin(OTHER_ID), CONTEXTO_SESSION="aaaa11") == (0, "my project\n", "")
    held.release()
    assert shown(claude_stdin(), CONTEXTO_SESSION="aaaa11") == (0, "my project\n", "")


def test_the_documentation_names_what_the_code_does():
    docs = (Path(__file__).resolve().parent.parent / "docs" / "statusline.md").read_text()
    for text in ("contexto statusline", "--no-statusline", "CONTEXTO_NO_STATUSLINE", "PAGING STOPPED",
                 RESTART_NOTICE, "--own-settings", "--color", "$CONTEXTO_SESSION", "disableAllHooks",
                 "ctx 112k/150k · 41 paged · 3 recalls · 5 searches"):
        assert text in docs, text
    for note in ("added", "off:flag", "off:env", "own:", "hooks_off:", "unreadable:", "managed_hooks_only",
                 "unsafe_path", "error"):
        assert f"`{note}" in docs, note
    assert NOTICE_LINE.rstrip("\n") in docs and "NO_COLOR" in docs and "PYTHONIOENCODING=ascii" in docs


def test_paged_is_described_as_page_files_from_every_conversation_not_turns():
    """Reviewer B, PR #276: ``N paged`` counts the page files in the store. A page from the bounded fallback
    holds a carried message, not a turn, and a subagent's pages count too (docs/stats.md: "pages")."""
    root = Path(__file__).resolve().parent.parent
    docs = (root / "docs" / "statusline.md").read_text()
    row = next(line for line in docs.splitlines() if line.startswith("| `41 paged`"))
    changelog = (root / "CHANGELOG.md").read_text()
    entry = changelog[changelog.index("`contexto run claude` shows Contexto's numbers"):].split("\n- ", 1)[0]
    for text in (row, " ".join(statusline.DESCRIPTION.split()), " ".join(entry.split())):
        assert "turns paged" not in text.lower(), text
        assert "page files" in text and "every conversation" in text, text


def test_the_version_comes_from_the_contexto_proxy_distribution_and_is_still_looked_up_lazily(monkeypatch):
    """Reviewer A, PR #276: merging #256 (packaging as contexto-proxy) into the lazy lookup had to keep the
    distribution's name, or every installed user would see 0.0.0+unknown. The name a lookup asks for is
    checked here without depending on how this checkout is installed."""
    import importlib.metadata

    import contexto

    asked: list[str] = []
    # setitem first records an undo even when __version__ was never loaded, so the fake version below is
    # not left in the module for the rest of the run (Reviewer A, PR #276: delitem alone records none).
    monkeypatch.setitem(contexto.__dict__, "__version__", None)
    monkeypatch.delitem(contexto.__dict__, "__version__")
    monkeypatch.setattr(importlib.metadata, "version", lambda name: asked.append(name) or "9.9.9")
    assert contexto.__version__ == "9.9.9" and asked == ["contexto-proxy"]
    assert contexto.version_line().startswith("contexto 9.9.9")
