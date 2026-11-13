"""``contexto.stats`` and ``contexto stats`` (D64): a session's numbers, read from its log and store.

Every log here is synthetic and shaped like the lines the proxy, the engine and the MCP server write.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from contexto import proxy, stats
from contexto.stats import FAULT_EVENTS, render, session_faults, session_stats
from contexto.store import SessionStore

DOCS = Path(__file__).resolve().parent.parent / "docs" / "stats.md"


def ts(second: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_800_000_000 + second)) + ".000000Z"


def launch(second: int = 0, cli: str = "claude", bound: int = 150_000) -> list[dict]:
    return [{"event": "proxy_start", "port": 1, "ts": ts(second)},
            {"event": "launch", "cli": cli, "config": {"bound": bound}, "engine": "rewrite", "ts": ts(second)}]


def request(second: int, *, tokens: int | None = 1_000, cache_read: int = 0, sent: int = 2_000, original: int = 2_000,
            events: tuple = (), usage_events: tuple = (), main: bool = True, degraded: bool = False,
            status: int = 200, api: str = "messages", estimate: int = 900, **extra) -> dict:
    """One model request line as the proxy logs it; ``tokens`` is the prompt the API counted."""
    engine = {"event": "request", "epoch": 0, "k": 0, "messages": 3, "upstream_messages": 3, "estimate": estimate,
              "sent_bytes": sent, "main": main, "conversations": 1}
    if degraded:
        engine["degraded"] = True
    if api == "responses":
        usage = None if tokens is None else {"input_tokens": tokens, "output_tokens": 7,
                                             "input_tokens_details": {"cached_tokens": cache_read}}
    else:
        usage = None if tokens is None else {"input_tokens": tokens - cache_read, "cache_read_input_tokens": cache_read,
                                             "cache_creation_input_tokens": 0, "output_tokens": 7}
    nested = [*events, engine]
    line = {"event": "request", "method": "POST", "path": "/responses" if api == "responses" else "/v1/messages",
            "api": api, "status": status, "original_request_bytes": original, "request_bytes": sent,
            "transform_events": nested, "usage": usage, "ts": ts(second),
            "over_bound": any(e.get("over_bound") for e in [*nested, *usage_events]), **extra}
    if usage_events:
        line["usage_events"] = list(usage_events)
    return line


def over(source: str = "estimate", in_tool_loop: bool = True, bound: int = 150_000, **extra) -> dict:
    if source == "real":
        return {"event": "over_bound", "source": "real", "epoch": 0, "over_bound": True, "real_tokens": bound + 1,
                "estimate": bound - 5, "bound": bound, **extra}
    return {"event": "over_bound", "source": "estimate", "in_tool_loop": in_tool_loop, "epoch": 0,
            "estimate": bound + 1, "bound": bound, **extra}


def rotation(turns: int, epoch: int = 1) -> tuple[dict, dict]:
    return ({"event": "page_out", "epoch": epoch, "turns": turns, "pages": turns, "first_turn": 0},
            {"event": "rotation", "epoch": epoch, "reason": "bound", "k": 9, "retained_messages": 4,
             "turns_paged": turns, "fixed_overhead": 20_000, "window_budget": 90_000})


def mcp(tool: str, second: int = 0, ok: bool = True, charged: int | None = None, **extra) -> dict:
    line = {"event": "mcp_call", "tool": f"contexto_{tool}", "ok": ok, "ts": ts(second), **extra}
    if charged is not None:
        line.update(turn_budget="charged", charged_tokens=charged, room_left=50_000)
    return line


def write_log(home: Path, session: str, lines: list) -> Path:
    """A session directory holding ``lines``: dicts as JSON lines, bytes and strings as they are."""
    directory = home / "sessions" / session
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for line in lines:
        out.append(line if isinstance(line, bytes) else
                   (line if isinstance(line, str) else json.dumps(line, separators=(",", ":"))).encode())
    (directory / "log.jsonl").write_bytes(b"\n".join(out) + (b"\n" if out else b""))
    return directory


def turn(i: int) -> list[dict]:
    return [{"role": "user", "content": [{"type": "text", "text": f"synthetic prompt {i} " + "filler " * 200}]},
            {"role": "assistant", "content": [{"type": "text", "text": f"synthetic answer {i}"}]}]


# What the numbers are ------------------------------------------------------------------------------

def test_stats_reads_a_session_s_numbers_from_its_log_and_store(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.page_out([turn(i) for i in range(3)], 0)
    pinned = store.pin("obligation", "synthetic standing rule")
    store.pin("decision", "synthetic decision")
    store.close(store.pin("obligation", "synthetic done"), "synthetic closure")
    write_log(tmp_path, "s1", [
        *launch(0),
        {"event": "request", "method": "HEAD", "path": "/api/hello", "status": 401, "ts": ts(1)},
        {"event": "upstream_4xx", "status": 401, "path": "/api/hello", "ts": ts(1)},
        request(10, tokens=40_000, cache_read=0, original=160_000, sent=160_000),
        request(20, tokens=120_000, cache_read=100_000, original=480_000, sent=480_000),
        request(30, tokens=148_210, cache_read=140_000, original=600_000, sent=590_000, main=False),
        request(40, tokens=60_000, cache_read=30_000, original=700_000, sent=240_000, events=rotation(3)),
        {"event": "request", "method": "POST", "path": "/v1/messages/count_tokens", "status": 200, "ts": ts(41),
         "original_request_bytes": 700_000, "request_bytes": 240_000,
         "transform_events": [{"event": "count_tokens", "epoch": 1}]},
        mcp("search", 42, charged=900), mcp("search", 43, charged=100), mcp("recall", 44, charged=4_000),
        mcp("recall", 45, ok=False, error="ToolError"), mcp("pin", 46), mcp("state", 47, charged=50),
        {"event": "proxy_stop", "ts": ts(3_725)},
        {"event": "exit", "code": 0, "ts": ts(3_725)},
    ])
    out = session_stats(tmp_path, "s1")
    assert out["session"] == "s1" and out["scope"] == "session" and out["runs"] == 1
    assert out["cli"] == "claude" and out["bound"] == 150_000 and out["span_seconds"] == 3_725
    assert out["requests"] == {"model": 4, "main": 3, "side": 1, "with_usage": 4, "failed": 0, "status": {},
                               "aborted": 0, "count_tokens": 1, "other": 1}
    # The peak is the largest prompt the API counted, in any conversation; the last is the main one's.
    assert out["prompt"] == {"peak_tokens": 148_210, "peak_source": "usage", "last_tokens": 60_000,
                             "last_source": "usage", "bound": 150_000}
    assert out["over_bound"] == {"requests": 0, "measured": 0, "estimate_only": 0, "in_tool_loop": 0,
                                 "at_turn_start": 0, "degraded": 0}
    assert out["paging"] == {"rotations": 1, "turns_paged": 3, "pages_written": 3, "carried_items_paged": 0}
    assert out["pages"]["count"] == 3 and out["pages"]["unreadable"] == 0
    stored = [json.loads(p.read_text()) for p in store.pages_dir.glob("pg_*.json")]
    assert out["pages"]["tokens_estimate"] == sum(p["tokens"] for p in stored) > 0
    assert out["pages"]["file_bytes"] == sum(p.stat().st_size for p in store.pages_dir.glob("pg_*.json"))
    assert out["tools"]["search"] == {"calls": 2, "failed": 0, "charged_tokens": 1_000}
    assert out["tools"]["recall"] == {"calls": 1, "failed": 1, "charged_tokens": 4_000}
    assert out["tools"]["pin"]["calls"] == 1 and out["tools"]["state"]["calls"] == 1
    assert out["tools"]["close"] == {"calls": 0, "failed": 0, "charged_tokens": 0} and out["tools"]["out_of_room"] == 0
    assert out["pins"] == {"readable": True, "active_records": 4, "open_obligations": 1}
    assert pinned  # the open obligation
    assert out["cache"] == {"requests": 4, "input_tokens": 368_210, "cache_read_tokens": 270_000,
                            "cache_write_tokens": 0, "read_share": round(270_000 / 368_210, 4)}
    assert out["bytes"] == {"cli_sent": 1_940_000, "forwarded": 1_470_000,
                            "forwarded_share": round(1_470_000 / 1_940_000, 4),
                            "largest_cli_request": 700_000, "largest_forwarded_request": 590_000}
    assert out["compaction"] == {"seen": 0}
    assert out["faults"]["total"] == 0 and out["faults"]["events"] == {} and out["faults"]["messages"] == []
    text = "\n".join(render(out))
    assert "peak 148,210 of 150,000 tokens (measured)" in text and "over the bound: 0 requests" in text
    assert "3 turns in 1 rotation" in text and "3 pages on disk" in text and "estimate" in text
    assert "1 recall, 2 search, 1 state, 1 pin" in text and "1 obligation open" in text
    assert "73% of input tokens read from cache" in text
    assert "none seen" in text and "faults      none" in text


def test_a_codex_session_reads_responses_usage_and_claims_nothing_about_compaction(tmp_path):
    write_log(tmp_path, "c1", [
        *launch(0, cli="codex", bound=64_000),
        request(5, api="responses", tokens=30_000, cache_read=24_000),
        request(9, api="responses", tokens=50_000, cache_read=45_000),
    ])
    out = session_stats(tmp_path, "c1")
    assert out["cli"] == "codex" and out["prompt"]["peak_tokens"] == 50_000 and out["prompt"]["bound"] == 64_000
    # Responses usage: cached_tokens is part of input_tokens, so it is not added to it.
    assert out["cache"]["input_tokens"] == 80_000 and out["cache"]["cache_read_tokens"] == 69_000
    assert out["compaction"] is None  # no event shows a Codex compaction, so nothing is claimed
    assert "compaction" not in "\n".join(render(out))


def test_a_number_without_data_is_left_out_and_an_estimate_is_called_one(tmp_path):
    write_log(tmp_path, "e1", [*launch(0), request(1, tokens=None, estimate=31_000),
                               request(2, tokens=None, estimate=30_000, events=(dict(rotation(2)[1], k=4),))])
    out = session_stats(tmp_path, "e1")
    assert out["prompt"] == {"peak_tokens": 31_000, "peak_source": "estimate", "last_tokens": 30_000,
                             "last_source": "estimate", "bound": 150_000}
    assert out["cache"] == {"requests": 0, "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                            "read_share": None}
    assert out["pages"]["count"] == 0 and out["pins"]["active_records"] == 0
    lines = render(out)
    text = "\n".join(lines)
    assert "peak 31,000 of 150,000 tokens (Contexto's estimate; the API reported no usage)" in text
    assert not [line for line in lines if line.lstrip().startswith(("cache", "recall"))]
    # A rotation that pages nothing (a head change) is a rotation, and no page is claimed.
    assert "0 turns in 1 rotation" in text and "pages on disk" not in text
    # No launch event: the bound comes from an event that carries it, else the line has no "of".
    write_log(tmp_path, "e2", [request(1, tokens=5_000)])
    assert "peak 5,000 tokens (measured)" in "\n".join(render(session_stats(tmp_path, "e2")))
    write_log(tmp_path, "e3", [request(1, tokens=5_000, events=(over(bound=4_000),))])
    assert session_stats(tmp_path, "e3")["bound"] == 4_000


def test_over_bound_requests_are_split_by_where_and_how_they_were_seen(tmp_path):
    write_log(tmp_path, "o1", [
        *launch(0),
        request(1, tokens=151_000, events=(over(in_tool_loop=True),), usage_events=(over("real"),)),
        request(2, tokens=149_000, events=(over(in_tool_loop=True),)),  # the estimate ran high
        request(3, tokens=152_000, events=(over(in_tool_loop=False),), usage_events=(over("real"),)),
        request(4, tokens=150_500, usage_events=(over("real"),)),  # the estimate ran low
        request(5, tokens=160_000, degraded=True, events=(over(in_tool_loop=True, degraded=True),),
                usage_events=(over("real"),)),
        request(6, tokens=90_000),
    ])
    out = session_stats(tmp_path, "o1")
    assert out["over_bound"] == {"requests": 5, "measured": 4, "estimate_only": 1, "in_tool_loop": 3,
                                 "at_turn_start": 1, "degraded": 1}
    text = "\n".join(render(out))
    assert "over the bound: 5 requests" in text and "4 measured by the API" in text and "1 by estimate only" in text
    assert "3 inside a tool loop" in text and "1 at a turn start" in text


def test_a_compaction_of_the_main_conversation_is_counted(tmp_path):
    changed = {"event": "main_changed", "reason": "compacted", "turn_starts": 2}
    other = {"event": "main_changed", "reason": "new_cli_session", "turn_starts": 1}  # /clear is not a compaction
    write_log(tmp_path, "k1", [*launch(0), request(1), request(2, events=(other,)), request(3, events=(changed,))])
    out = session_stats(tmp_path, "k1")
    assert out["compaction"] == {"seen": 1}
    assert "Claude Code compacted the main conversation 1 time" in "\n".join(render(out))


def test_the_last_run_of_a_resumed_session_is_counted_apart(tmp_path):
    write_log(tmp_path, "r1", [
        *launch(0, bound=150_000), request(10, tokens=140_000, events=rotation(5)), mcp("recall", 11, charged=10),
        {"event": "exit", "code": 0, "ts": ts(100)},
        *launch(1_000, bound=120_000), request(1_010, tokens=70_000), mcp("search", 1_011, charged=10),
        {"event": "exit", "code": 0, "ts": ts(1_060)},
    ])
    whole, last = session_stats(tmp_path, "r1"), session_stats(tmp_path, "r1", last_run=True)
    assert whole["scope"] == "session" and last["scope"] == "last_run" and whole["runs"] == last["runs"] == 2
    assert whole["prompt"]["peak_tokens"] == 140_000 and last["prompt"]["peak_tokens"] == 70_000
    assert whole["bound"] == last["bound"] == 120_000  # the latest launch's
    assert whole["paging"]["turns_paged"] == 5 and last["paging"]["turns_paged"] == 0
    assert whole["tools"]["recall"]["calls"] == 1 and last["tools"]["recall"]["calls"] == 0
    assert last["tools"]["search"]["calls"] == 1 and last["requests"]["model"] == 1
    assert whole["span_seconds"] == 1_060 and last["span_seconds"] == 60
    assert whole["started"] == ts(0) and last["started"] == ts(1_000)


# A damaged log ---------------------------------------------------------------------------------------

def test_lines_that_only_parse_when_joined_are_still_damaged_lines(tmp_path):
    """A healthy log is parsed in one call; these lines parse that way and not on their own."""
    write_log(tmp_path, "d2", [*launch(0), request(1, tokens=9_000), '{"event": "request", "x": [1', "2]}",
                               '{"event": "exit"}, {"event": "transform_error"}', "1, 2"])
    out = session_stats(tmp_path, "d2")
    assert out["log"] == {"missing": False, "lines": 7, "unreadable_lines": 4}
    assert out["requests"]["model"] == 1 and out["faults"]["total"] == 0


def test_a_damaged_log_is_read_as_far_as_it_goes(tmp_path):
    good = request(2, tokens=9_000)
    cut = json.dumps(request(3, tokens=99_000))[:120]  # a line a full disk cut short
    wrong = request(4, tokens=7_000)
    wrong.update(original_request_bytes="many", request_bytes=None, status="ok", transform_events="none",
                 usage={"input_tokens": "lots", "cache_read_input_tokens": [1], "output_tokens": True})
    odd_nested = request(5, tokens=8_000)
    odd_nested["transform_events"] = [7, "x", None, {"event": ["rotation"]}, {"event": "rotation", "turns_paged": "9"},
                                      {"event": "page_out", "turns": "many", "pages": None},
                                      {"event": "over_bound", "source": 3}, *odd_nested["transform_events"]]
    write_log(tmp_path, "d1", [
        *launch(0), good, cut, "[1, 2, 3]", '"a string"', "null", "17", b"\xff\xfe not utf-8 \x00", "", "{}",
        {"event": 5}, {"event": "launch", "config": "none", "cli": 9}, {"event": "mcp_call", "tool": 3, "ok": "yes"},
        {"event": "mcp_call", "tool": "contexto_search", "ok": True, "charged_tokens": "9"},
        wrong, odd_nested, {"ts": 12, "event": "exit"},
    ])
    out = session_stats(tmp_path, "d1")
    assert out["log"] == {"missing": False, "lines": 17, "unreadable_lines": 6}
    assert out["requests"]["model"] == 3 and out["prompt"]["peak_tokens"] == 9_000
    assert out["bytes"]["cli_sent"] == 4_000  # the two lines whose byte counts are numbers
    assert out["paging"] == {"rotations": 1, "turns_paged": 0, "pages_written": 0, "carried_items_paged": 0}
    assert out["tools"]["search"] == {"calls": 1, "failed": 0, "charged_tokens": 0}
    assert out["bound"] == 150_000 and out["cli"] == "claude"
    assert "6 lines of the log could not be read" in "\n".join(render(out))
    json.dumps(out)  # a plain dict of JSON values


@pytest.mark.parametrize("damage", ["missing", "empty", "directory", "fifo", "symlink"])
def test_a_missing_or_unusable_log_gives_empty_numbers_and_never_waits(tmp_path, damage):
    directory = tmp_path / "sessions" / "m1"
    directory.mkdir(parents=True)
    log = directory / "log.jsonl"
    if damage == "empty":
        log.write_bytes(b"")
    elif damage == "directory":
        log.mkdir()
    elif damage == "fifo":
        os.mkfifo(log)  # a plain open would wait for a writer
    elif damage == "symlink":
        (tmp_path / "elsewhere.jsonl").write_text(json.dumps(request(1)) + "\n")
        log.symlink_to(tmp_path / "elsewhere.jsonl")  # never followed out of the session
    out = session_stats(tmp_path, "m1")
    assert out["log"] == {"missing": damage != "empty", "lines": 0, "unreadable_lines": 0}
    assert out["requests"]["model"] == 0 and out["prompt"]["peak_tokens"] is None and out["runs"] == 0
    assert render(out)  # says there is nothing, rather than nothing at all
    before = sorted(p.name for p in directory.iterdir())
    session_stats(tmp_path, "m1")
    assert sorted(p.name for p in directory.iterdir()) == before  # reading creates nothing


def test_stats_of_a_session_that_does_not_exist_is_an_error(tmp_path):
    from contexto.store import SessionNotFoundError

    with pytest.raises(SessionNotFoundError):
        session_stats(tmp_path, "nope")


def test_a_damaged_page_or_ledger_costs_only_its_own_number(tmp_path):
    store = SessionStore(tmp_path, "p1")
    ids = store.page_out([turn(i) for i in range(4)], 0)
    store.pin("decision", "synthetic")
    whole = sum(json.loads((store.pages_dir / f"{i}.json").read_text())["tokens"] for i in ids)
    lost = json.loads((store.pages_dir / f"{ids[0]}.json").read_text())["tokens"]
    (store.pages_dir / f"{ids[0]}.json").write_bytes(b'{"bytes": 12, "id"')  # cut short
    # Another JSON layout of a whole page: read in full when the fast path does not match.
    spaced = json.loads((store.pages_dir / f"{ids[1]}.json").read_text())
    (store.pages_dir / f"{ids[1]}.json").write_text(json.dumps(spaced, indent=2))
    (store.pages_dir / "pg_0000000000000000.json").symlink_to(store.pages_dir / f"{ids[2]}.json")
    (store.pages_dir / "notes.txt").write_text("not a page")
    write_log(tmp_path, "p1", [*launch(0), request(1)])
    out = session_stats(tmp_path, "p1")
    assert out["pages"]["count"] == 4 and out["pages"]["unreadable"] == 1  # the symlink is not a page
    assert out["pages"]["tokens_estimate"] == whole - lost
    assert "1 page could not be read" in "\n".join(render(out))
    (store.dir / "ledger.jsonl").write_bytes(b'{"not": "a record"}\n')
    out = session_stats(tmp_path, "p1")
    assert out["pins"] == {"readable": False, "active_records": None, "open_obligations": None}
    assert not [line for line in render(out) if "obligation" in line]
    # The status line's cheaper call: pages are counted, not opened.
    quick = session_stats(tmp_path, "p1", page_tokens=False)
    assert quick["pages"]["count"] == 4 and quick["pages"]["tokens_estimate"] is None


# Faults ----------------------------------------------------------------------------------------------

def store_error(op: str = "page_out", name: str | None = "ENOSPC", **extra) -> dict:
    event = {"event": "store_error", "epoch": 0, "operation": op, "error": "OSError", **extra}
    if name:
        event["errno"] = name
    return event


ENTER = {"event": "degraded_enter", "epoch": 0, "reason": "store_error"}
LEAVE = {"event": "degraded_leave", "epoch": 0}


def test_a_full_disk_is_named_with_what_to_do(tmp_path):
    write_log(tmp_path, "f1", [
        *launch(0),
        request(1, tokens=60_000, sent=67_000),
        request(2, tokens=70_000, sent=90_000, degraded=True, events=(store_error(), ENTER)),
        request(3, tokens=120_000, sent=200_000, degraded=True),
        request(4, tokens=170_000, sent=354_000, degraded=True,
                events=(store_error(degraded=True), over(in_tool_loop=False, degraded=True)),
                usage_events=(over("real"),)),
    ])
    faults = session_stats(tmp_path, "f1")["faults"]
    assert faults["events"] == {"store_error": 2, "degraded_enter": 1} and faults["total"] == 3
    assert faults["degraded"] == {"entered": 1, "left": 0, "now": True, "since": ts(2), "unpaged_requests": 3,
                                  "largest_unpaged_request_bytes": 354_000}
    assert faults["store_errors"] == [{"event": "store_error", "operation": "page_out", "error": "OSError",
                                       "errno": "ENOSPC", "cause": "disk_full", "count": 2, "first": ts(2),
                                       "last": ts(4)}]
    [message] = faults["messages"]
    assert "disk" in message and "full" in message and "ENOSPC" in message and "page_out" in message
    assert "Free space" in message and "retries at each turn start" in message
    assert "unpaged" in message and "exceed the bound" in message and "has not resumed" in message
    assert session_faults(tmp_path, "f1") == faults  # what `contexto state` shows, as the same dict
    text = "\n".join(render(session_stats(tmp_path, "f1")))
    assert "3 requests went out unpaged" in text and "largest 345.7 KiB" in text and "Free space" in text


def test_a_store_that_recovers_says_so_and_other_causes_get_their_own_advice(tmp_path):
    write_log(tmp_path, "f2", [
        *launch(0),
        request(1, degraded=True, events=(store_error("pinned_text", "EACCES"), ENTER)),
        request(2, events=(LEAVE, *rotation(2))),
        request(3, degraded=True, events=({"event": "page_conflict", "epoch": 1, "operation": "page_out",
                                           "error": "PageConflictError", "page_id": "pg_00aa"},
                                          dict(ENTER, reason="page_conflict"))),
        request(4, events=(dict(LEAVE, quarantined="pg_00aa"),)),
        request(5, degraded=True, events=(store_error("index_text", None, error="LedgerCorruptError"), ENTER)),
        request(6, events=(LEAVE,)),
    ])
    faults = session_stats(tmp_path, "f2")["faults"]
    assert faults["degraded"]["entered"] == 3 and faults["degraded"]["left"] == 3 and faults["degraded"]["now"] is False
    assert faults["degraded"]["since"] is None and faults["degraded"]["unpaged_requests"] == 3
    assert [e["cause"] for e in faults["store_errors"]] == ["no_access", "page_conflict", "store"]
    text = " ".join(faults["messages"])
    assert "permissions" in text and "EACCES" in text  # not a full disk: no advice to free space
    assert "Free space" not in text
    assert "quarantine" in text and "pg_00aa" in text
    assert "LedgerCorruptError" in text and "contexto sessions show" in text
    assert "has resumed" in text and "has not resumed" not in text


def test_a_new_run_starts_out_of_degraded_mode_and_state_shows_only_the_latest_run(tmp_path):
    write_log(tmp_path, "f3", [
        *launch(0), request(1, degraded=True, events=(store_error(), ENTER)), request(2, degraded=True),
        *launch(100), request(101), request(102),
    ])
    whole = session_stats(tmp_path, "f3")["faults"]
    assert whole["degraded"]["entered"] == 1 and whole["degraded"]["now"] is False  # the engine is new
    assert whole["messages"] and "an earlier run" in whole["messages"][-1]
    latest = session_faults(tmp_path, "f3")
    assert latest["total"] == 0 and latest["messages"] == [] and latest["degraded"]["entered"] == 0


def test_a_side_conversation_s_store_failure_does_not_mark_main_as_unpaged(tmp_path):
    write_log(tmp_path, "f4", [
        *launch(0), request(1), request(2, main=False, degraded=True, events=(store_error(), ENTER)),
        request(3), request(4, main=False, degraded=True),
    ])
    degraded = session_stats(tmp_path, "f4")["faults"]["degraded"]
    assert degraded["entered"] == 1 and degraded["now"] is False and degraded["unpaged_requests"] == 2
    write_log(tmp_path, "f5", [*launch(0), request(1, usage_events=(store_error("set_turn_room", "ENOSPC"), ENTER))])
    assert session_stats(tmp_path, "f5")["faults"]["degraded"]["now"] is True  # entered after the response


def test_only_events_that_mean_something_went_wrong_are_counted(tmp_path):
    expected_in_normal_use = [
        {"event": "upstream_4xx", "status": 401, "path": "/api/hello"},  # Claude Code's keyless probe, every launch
        {"event": "upstream_4xx", "status": 429, "path": "/v1/messages",
         "transform_events": [{"event": "transform_error"}, ENTER]},  # a copy of its request's events: never recounted
        {"event": "cli_version_check", "cli": "claude", "tested": True}, {"event": "window_budget_warning"},
        {"event": "upstream_retry", "path": "/v1/messages"}, {"event": "side_room_expired", "age_s": 900},
        {"event": "hold_skipped", "path": "/v1/messages"}, {"event": "stale_temp_removed", "files": 1},
        {"event": "mcp_tools_missing", "at": "exit", "repeated": True},  # said once already during the run
    ]
    nested_normal = ({"event": "epoch_not_resumed", "reason": "no_record"}, {"event": "usage_missing"},
                     {"event": "usage_unattributed"}, {"event": "resync", "kind": "window_changed"},
                     {"event": "rotation_noop"}, {"event": "turn_room_low", "room": 9}, {"event": "head_changed"},
                     {"event": "window_minimal"}, {"event": "pdf_allowance"}, {"event": "estimate_error"},
                     {"event": "mcp_tools_found"}, {"event": "cli_version", "version": "2.1.318"},
                     {"event": "cli_version_untested", "version": "9.9.9"}, {"event": "pdf_pages_uncertain"},
                     {"event": "sizing_restored"}, {"event": "main_changed", "reason": "more_turn_starts"})
    write_log(tmp_path, "n1", [*launch(0), *expected_in_normal_use, request(1, events=nested_normal),
                               request(2, status=429), request(3, client_aborted=True, tokens=None)])
    write_log(tmp_path, "n2x", [*launch(0), request(1)])
    out = session_stats(tmp_path, "n1")
    assert out["faults"]["total"] == 0 and out["faults"]["events"] == {}
    assert out["requests"]["failed"] == 1 and out["requests"]["status"] == {"429": 1}
    assert out["requests"]["aborted"] == 1
    assert "faults      none" in "\n".join(render(out)) and "1 failed (429 x1)" in "\n".join(render(out))
    # The exit summary leaves the request line to `contexto stats`, unless a request failed.
    assert "1 failed (429 x1)" in "\n".join(render(out, brief=True))
    assert not [line for line in render(session_stats(tmp_path, "n2x"), brief=True) if "requests" in line.split()[:1]]
    wrong_top = [{"event": name} for name in ("transform_error", "proxy_error", "request_too_deep", "page_quarantined",
                                              "page_unreadable", "ledger_damaged", "turn_room_missing",
                                              "engine_unavailable", "websocket_rejected", "observe_usage_error")]
    wrong_nested = tuple({"event": name} for name in (
        "fallback_failed", "retained_stale", "epoch_record_stale", "mcp_tools_missing", "mcp_tools_denied",
        "toolcheck_error", "cli_version_error", "side_room_close_failed", "unclassified_system_message",
        "carried_over_budget", "bound_too_small", "session_start_unrecognised"))
    write_log(tmp_path, "n2", [*launch(0), *wrong_top, request(1, events=wrong_nested),
                               {"event": "mcp_tools_missing", "at": "exit", "repeated": False}])
    out = session_stats(tmp_path, "n2")
    assert out["faults"]["events"] == {**{e["event"]: 1 for e in [*wrong_top, *wrong_nested]}, "mcp_tools_missing": 2}
    assert out["faults"]["total"] == 23
    assert set(out["faults"]["events"]) | {"degraded_enter", "store_error", "page_conflict"} == set(FAULT_EVENTS)
    assert "23 events" in "\n".join(render(out)) and "transform_error 1" in "\n".join(render(out))


def test_a_fault_nothing_else_warned_about_outlives_quiet(tmp_path):
    from contexto.stats import NOTICED_EVENTS, exit_summary

    write_log(tmp_path, "w1", [*launch(0), {"event": "transform_error", "level": "error", "error": "KeyError"},
                               request(1, events=({"event": "mcp_tools_missing", "at": "request"},)),
                               request(2, events=({"event": "retained_stale", "error": "OSError"},))])
    warning = ("contexto: warning: this run logged 2 events (retained_stale 1, transform_error 1) that Contexto left "
               "its designed path. Details: contexto stats w1")
    assert exit_summary(tmp_path, "w1", "claude", 5.0, quiet=True) == [warning]
    loud = exit_summary(tmp_path, "w1", "claude", 5.0)
    assert loud[0] == "contexto: run of session w1 ended after 5s" and loud[-1] == warning
    assert ("  faults      3 events (mcp_tools_missing 1, retained_stale 1, transform_error 1); details: contexto "
            "stats w1") == loud[-2]
    # A run whose only faults the launcher's own warnings described adds no second warning.
    write_log(tmp_path, "w2", [*launch(0), request(1, degraded=True, events=(store_error(), ENTER)),
                               request(2, events=({"event": "mcp_tools_denied"}, {"event": "bound_too_small"}))])
    assert exit_summary(tmp_path, "w2", "claude", 5.0, quiet=True) == []
    assert NOTICED_EVENTS < set(FAULT_EVENTS)


def test_the_engine_s_own_fault_list_is_covered():
    """The contract's acceptance check (M2, check 1) fails a run on these events; all but one are faults
    here. ``turn_room_low`` is left out on purpose: at a small bound every epoch logs it (docs/stats.md)."""
    contract = (Path(__file__).resolve().parent.parent / "docs" / "milestones" / "M2-contexto-proxy.md").read_text()
    section = contract[contract.index("means the product left its designed path"):]
    listed = set(re.findall(r"`([a-z_]+)`", section[:section.index("2. **Fidelity")]))
    assert {"degraded_enter", "store_error", "transform_error", "turn_room_low"} <= listed
    assert listed - set(FAULT_EVENTS) == {"turn_room_low"}


def test_the_paths_stats_counts_are_the_proxy_s():
    assert stats.MODEL_PATHS == proxy.USAGE_FEEDBACK_PATHS
    assert stats.COUNT_TOKENS_PATH == proxy.COUNT_TOKENS_PATH


# The documented JSON shape ---------------------------------------------------------------------------

DOCUMENTED = {
    "": ["version", "session", "scope", "runs", "cli", "bound", "started", "ended", "span_seconds", "log",
         "requests", "prompt", "over_bound", "paging", "pages", "tools", "pins", "cache", "bytes", "compaction",
         "faults"],
    "log": ["missing", "lines", "unreadable_lines"],
    "requests": ["model", "main", "side", "with_usage", "failed", "status", "aborted", "count_tokens", "other"],
    "prompt": ["peak_tokens", "peak_source", "last_tokens", "last_source", "bound"],
    "over_bound": ["requests", "measured", "estimate_only", "in_tool_loop", "at_turn_start", "degraded"],
    "paging": ["rotations", "turns_paged", "pages_written", "carried_items_paged"],
    "pages": ["count", "tokens_estimate", "file_bytes", "unreadable"],
    "tools": ["recall", "search", "state", "pin", "close", "out_of_room"],
    "tools.recall": ["calls", "failed", "charged_tokens"],
    "pins": ["readable", "active_records", "open_obligations"],
    "cache": ["requests", "input_tokens", "cache_read_tokens", "cache_write_tokens", "read_share"],
    "bytes": ["cli_sent", "forwarded", "forwarded_share", "largest_cli_request", "largest_forwarded_request"],
    "compaction": ["seen"],
    "faults": ["total", "events", "degraded", "store_errors", "messages"],
    "faults.degraded": ["entered", "left", "now", "since", "unpaged_requests", "largest_unpaged_request_bytes"],
}


def test_the_json_shape_is_the_documented_one(tmp_path):
    write_log(tmp_path, "j1", [*launch(0), request(1), mcp("recall", 2, charged=5)])
    empty = tmp_path / "sessions" / "j2"
    empty.mkdir()
    docs = DOCS.read_text()
    for session in ("j1", "j2"):  # a session with data and one with none: the keys do not depend on the data
        out = session_stats(tmp_path, session)
        for where, keys in DOCUMENTED.items():
            value = out
            for part in filter(None, where.split(".")):
                value = value[part]
            if where == "compaction" and value is None:
                continue
            assert sorted(value) == sorted(keys), where
    assert out["version"] == stats.STATS_VERSION == 1
    for where, keys in DOCUMENTED.items():
        for key in keys:
            name = f"{where}.{key}" if where else key
            assert f"`{name}`" in docs, f"docs/stats.md does not define {name}"
    for name in FAULT_EVENTS:
        assert f"`{name}`" in docs, f"docs/stats.md does not list the fault event {name}"


# Speed -----------------------------------------------------------------------------------------------

def long_session_log(requests: int = 4_000) -> list[dict]:
    """A log shaped like a 6-hour session: a request every 5.4 s, a rotation every 20th, tool calls."""
    lines = launch(0)
    for i in range(requests):
        at = int(i * 5.4)
        events = [{"event": "cli_version", "cli": "claude", "version": "2.1.318"}] if i == 0 else []
        if i % 20 == 19:
            events += rotation(6, epoch=i // 20 + 1)
        line = request(at, tokens=60_000 + (i % 20) * 4_000, cache_read=55_000 + (i % 20) * 3_900,
                       original=400_000 + i * 900, sent=230_000 + (i % 20) * 15_000, events=tuple(events),
                       model="claude-opus-5-5", stream=True, duration_ms=4_812, response_bytes=18_220, epoch=i // 20,
                       message_count=140, original_message_count=140 + i, stop_reason="tool_use",
                       stream_hold={"held_events": 3, "triggered": True})
        lines.append(line)
        if i % 9 == 0:
            lines.append(mcp("search" if i % 2 else "recall", at, charged=1_200, limited_by="main", side_rooms=0))
    return [*lines, {"event": "proxy_stop", "ts": ts(21_600)}, {"event": "exit", "code": 0, "ts": ts(21_600)}]


def test_stats_of_a_six_hour_log_is_fast(tmp_path):
    lines = long_session_log()
    directory = write_log(tmp_path, "long", lines)
    assert (directory / "log.jsonl").stat().st_size > 2_500_000
    session_stats(tmp_path, "long")  # the imports, once
    started = time.perf_counter()
    out = session_stats(tmp_path, "long")
    elapsed = time.perf_counter() - started
    assert out["requests"]["model"] == 4_000 and out["paging"]["rotations"] == 200 and out["span_seconds"] == 21_600
    assert out["paging"]["turns_paged"] == 1_200
    assert out["tools"]["recall"]["calls"] + out["tools"]["search"]["calls"] == 445
    # Measured at 54 ms on a loaded laptop, 1.1 times a plain json.loads of the same lines (docs/stats.md);
    # the limit only catches a change that makes it many times slower, on any CI machine.
    assert elapsed < 2.0, f"{elapsed * 1000:.0f} ms"


# The command -----------------------------------------------------------------------------------------

def contexto(home: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if k not in ("CONTEXTO_SESSION", "CONTEXTO_HOME")}
    return subprocess.run([sys.executable, "-m", "contexto", *args], capture_output=True, text=True, timeout=60,
                          env={**clean, "CONTEXTO_HOME": str(home), **(env or {})})


def test_the_stats_command_prints_a_session_by_id_prefix_or_default(tmp_path):
    store = SessionStore(tmp_path, "abc123")
    store.page_out([turn(0)], 0)
    write_log(tmp_path, "abc123", [*launch(0), request(1, tokens=41_000), {"event": "exit", "code": 0, "ts": ts(65)}])
    write_log(tmp_path, "abd999", [*launch(0), request(1, tokens=9_000)])
    done = contexto(tmp_path, "stats", "abc")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "Contexto stats for session abc123: the whole session (1 run, 1m05s of log)"
    assert "peak 41,000 of 150,000 tokens (measured)" in done.stdout and "1 page on disk" in done.stdout
    assert contexto(tmp_path, "stats", "--session", "abc").stdout == done.stdout  # as the other readers name it
    # No id: the same default as `contexto state` (the most recent session with pages or pins).
    default = contexto(tmp_path, "stats")
    assert default.stdout == done.stdout and "session abc123 (" in default.stderr
    assert done.stderr == "session abc123 (SESSION)\n"  # how it was named
    assert contexto(tmp_path, "stats", env={"CONTEXTO_SESSION": "abd999"}).stdout.startswith(
        "Contexto stats for session abd999")
    ambiguous = contexto(tmp_path, "stats", "ab")
    assert ambiguous.returncode == 1 and "matches 2 sessions" in ambiguous.stderr and ambiguous.stdout == ""
    missing = contexto(tmp_path, "stats", "zzz")
    assert missing.returncode == 1 and "no session 'zzz'" in missing.stderr
    both = contexto(tmp_path, "stats", "abc", "--session", "abd")
    assert both.returncode == 2 and "not both" in both.stderr
    assert contexto(tmp_path / "nothing-here", "stats").returncode == 1


def test_the_stats_command_s_json_is_the_module_s_dict(tmp_path):
    write_log(tmp_path, "j9", [*launch(0), request(1, tokens=41_000), *launch(50), request(51, tokens=12_000)])
    done = contexto(tmp_path, "stats", "j9", "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == session_stats(tmp_path, "j9")
    last = contexto(tmp_path, "stats", "j9", "--json", "--last-run")
    assert json.loads(last.stdout)["prompt"]["peak_tokens"] == 12_000
    text = contexto(tmp_path, "stats", "j9", "--last-run").stdout
    assert text.splitlines()[0].startswith("Contexto stats for session j9: the latest of 2 runs")


def test_state_shows_a_store_that_stopped_paging_and_how_to_fix_it(tmp_path):
    store = SessionStore(tmp_path, "st1")
    store.pin("decision", "synthetic decision")
    write_log(tmp_path, "st1", [*launch(0), request(1), request(2, degraded=True, events=(store_error(), ENTER))])
    done = contexto(tmp_path, "state", "--session", "st1")
    assert done.returncode == 0, done.stderr
    assert "synthetic decision" in done.stdout
    warning = done.stdout[done.stdout.index("Warning:"):]
    assert "full" in warning and "Free space" in warning and "has not resumed" in warning
    # --raw is what the model gets from the MCP tool: unchanged.
    assert "Warning:" not in contexto(tmp_path, "state", "--session", "st1", "--raw").stdout
    write_log(tmp_path, "st1", [*launch(0), request(1)])
    assert "Warning:" not in contexto(tmp_path, "state", "--session", "st1").stdout
