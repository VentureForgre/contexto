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


def launch(second: int = 0, cli: str = "claude", bound: int = 150_000, pages: int | None = None) -> list[dict]:
    """A run's first lines; ``pages`` is the store's page count the proxy logs when it starts."""
    start = {"event": "proxy_start", "port": 1, "ts": ts(second)}
    if pages is not None:
        start["pages"] = pages
    return [start, {"event": "launch", "cli": cli, "config": {"bound": bound}, "engine": "rewrite", "ts": ts(second)}]


def stop(second: int, pages: int | None = None) -> list[dict]:
    """A run's last lines, as the proxy and the launcher write them."""
    stopped = {"event": "proxy_stop", "ts": ts(second)}
    if pages is not None:
        stopped["pages"] = pages
    return [stopped, {"event": "exit", "code": 0, "ts": ts(second)}]


def version(number: str = "2.1.318", cli: str = "claude") -> dict:
    """The engine's ``cli_version`` event, logged with the first request of each client version."""
    return {"event": "cli_version", "cli": cli, "client": "claude-cli" if cli == "claude" else "codex_exec",
            "version": number, "tested": True}


def request(second: int, *, tokens: int | None = 1_000, cache_read: int = 0, sent: int = 2_000, original: int = 2_000,
            events: tuple = (), usage_events: tuple = (), main: bool = True, degraded: bool = False,
            status: int = 200, api: str = "messages", estimate: int = 900, cli_session: bool | None = True,
            **extra) -> dict:
    """One model request line as the proxy logs it; ``tokens`` is the prompt the API counted."""
    engine = {"event": "request", "epoch": 0, "k": 0, "messages": 3, "upstream_messages": 3, "estimate": estimate,
              "sent_bytes": sent, "main": main, "conversations": 1}
    if api == "messages" and cli_session is not None:
        engine["cli_session"] = cli_session
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
        *launch(0, pages=0),
        {"event": "request", "method": "HEAD", "path": "/api/hello", "status": 401, "ts": ts(1)},
        {"event": "upstream_4xx", "status": 401, "path": "/api/hello", "ts": ts(1)},
        request(10, tokens=40_000, cache_read=0, original=160_000, sent=160_000, events=(version(),)),
        request(20, tokens=120_000, cache_read=100_000, original=480_000, sent=480_000),
        request(30, tokens=148_210, cache_read=140_000, original=600_000, sent=590_000, main=False),
        request(40, tokens=60_000, cache_read=30_000, original=700_000, sent=240_000, events=rotation(3)),
        {"event": "request", "method": "POST", "path": "/v1/messages/count_tokens", "status": 200, "ts": ts(41),
         "original_request_bytes": 700_000, "request_bytes": 240_000,
         "transform_events": [{"event": "count_tokens", "epoch": 1}]},
        mcp("search", 42, charged=900), mcp("search", 43, charged=100), mcp("recall", 44, charged=4_000),
        mcp("recall", 45, ok=False, error="ToolError"), mcp("pin", 46), mcp("state", 47, charged=50),
        *stop(3_725, pages=3),
    ])
    out = session_stats(tmp_path, "s1")
    assert out["session"] == "s1" and out["scope"] == "session" and out["runs"] == 1
    assert out["cli"] == "claude" and out["bound"] == 150_000 and out["span_seconds"] == 3_725
    assert out["requests"] == {"model": 4, "main": 3, "side": 1, "with_usage": 4, "failed": 0, "status": {},
                               "aborted": 0, "cut_short": 0, "not_transformed": 0, "rejected_encoding": 0,
                               "in_flight_at_stop": 0, "count_tokens": 1, "other": 1}
    # The peak is the largest prompt the API counted, in any conversation; the last is the main one's.
    assert out["prompt"] == {"peak_tokens": 148_210, "peak_source": "usage", "peak_estimate_without_usage": None,
                             "last_tokens": 60_000, "last_source": "usage", "bound": 150_000}
    assert out["over_bound"] == {"requests": 0, "measured": 0, "estimate_only": 0, "in_tool_loop": 0,
                                 "at_turn_start": 0, "degraded": 0}
    assert out["paging"] == {"rotations": 1, "pages_added": 3, "carried_items_paged": 0}
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
    assert out["cache"] == {"requests": 4, "unreported_requests": 0, "input_tokens": 368_210,
                            "cache_read_tokens": 270_000, "cache_write_tokens": 0,
                            "read_share": round(270_000 / 368_210, 4)}
    assert out["bytes"] == {"cli_sent": 1_940_000, "forwarded": 1_470_000,
                            "forwarded_share": round(1_470_000 / 1_940_000, 4),
                            "largest_cli_request": 700_000, "largest_forwarded_request": 590_000}
    assert out["compaction"] == {"seen": 0, "detectable": True, "reasons": [], "cli_versions": ["2.1.318"]}
    assert out["run_seconds"] == 3_725 and out["log"]["ignored_values"] == 0
    assert out["faults"]["total"] == 0 and out["faults"]["events"] == {} and out["faults"]["messages"] == []
    text = "\n".join(render(out))
    assert "peak 148,210 of 150,000 tokens (measured)" in text and "over the bound: 0 requests" in text
    assert "paged out   3 pages on disk, about" in text and "; 1 rotation" in text and "turns" not in text
    assert "5 calls (1 recall, 2 search, 1 state, 1 pin); 1 failed" in text and "1 obligation open" in text
    assert "73% of input tokens read from cache (measured)" in text
    assert "none seen: Claude Code did not compact the main conversation" in text and "faults      none" in text


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
    assert out["prompt"] == {"peak_tokens": 31_000, "peak_source": "estimate", "peak_estimate_without_usage": 31_000,
                             "last_tokens": 30_000, "last_source": "estimate", "bound": 150_000}
    assert out["cache"] == {"requests": 0, "unreported_requests": 0, "input_tokens": 0, "cache_read_tokens": 0,
                            "cache_write_tokens": 0, "read_share": None}
    assert out["pages"]["count"] == 0 and out["pins"]["active_records"] == 0
    lines = render(out)
    text = "\n".join(lines)
    assert "peak 31,000 of 150,000 tokens (Contexto's estimate; the API reported no usage)" in text
    assert not [line for line in lines if line.lstrip().startswith(("cache", "recall"))]
    # A rotation that pages nothing (a head change) is a rotation, and no page is claimed.
    assert "paged out   no page on disk; 1 rotation" in text
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
    # L3: the headline is what the API measured; the estimate-only request and the positions come after it.
    assert ("over the bound: 4 requests (measured); 1 more by Contexto's estimate only; of the 4 flagged before "
            "sending, 3 inside a tool loop and 1 at a turn start; 1 while paging was stopped") in text


def test_a_compaction_of_the_main_conversation_is_counted(tmp_path):
    changed = {"event": "main_changed", "reason": "compacted", "turn_starts": 2}
    other = {"event": "main_changed", "reason": "new_cli_session", "turn_starts": 1}  # /clear is not a compaction
    write_log(tmp_path, "k1", [*launch(0), request(1, events=(version(),)), request(2, events=(other,)),
                               request(3, events=(changed,))])
    out = session_stats(tmp_path, "k1")
    assert out["compaction"]["seen"] == 1
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
    assert whole["paging"]["rotations"] == 1 and last["paging"]["rotations"] == 0
    assert whole["tools"]["recall"]["calls"] == 1 and last["tools"]["recall"]["calls"] == 0
    assert last["tools"]["search"]["calls"] == 1 and last["requests"]["model"] == 1
    assert whole["span_seconds"] == 1_060 and last["span_seconds"] == 60
    assert whole["started"] == ts(0) and last["started"] == ts(1_000)


# Reviewer B's findings on what the numbers mean (PR #263) --------------------------------------------

def paged(turns: int, first: int = 0, epoch: int = 1) -> tuple[dict, dict]:
    """A rotation as the engine logs it: ``first_turn`` and ``turns_paged`` are the conversation's."""
    return ({"event": "page_out", "epoch": epoch, "turns": turns, "pages": turns, "first_turn": first},
            {"event": "rotation", "epoch": epoch, "reason": "bound", "k": 9, "retained_messages": 4,
             "turns_paged": first + turns})


def three_launches(pages: bool = True) -> list[dict]:
    """M1: 25 turns paged in five rotations, then two launches that could not resume their epoch and
    paged the whole history again, onto pages that already existed, plus the 3 turns each added."""
    def count(n: int) -> int | None:
        return n if pages else None

    not_resumed = {"event": "epoch_not_resumed", "reason": "head_changed"}
    return [
        *launch(0, pages=count(0)), *[request(10 + i, events=paged(4, 4 * i, i + 1)) for i in range(5)],
        *stop(100, pages=count(20)),
        *launch(200, pages=count(20)), request(210, events=(not_resumed, *paged(23))), request(211), request(212),
        *stop(300, pages=count(23)),
        *launch(400, pages=count(23)), request(410, events=(dict(not_resumed, reason="model_changed"), *paged(26))),
        request(411), request(412), *stop(500, pages=count(26)),
    ]


def fill_pages(home: Path, session: str, count: int) -> None:
    SessionStore(home, session).page_out([turn(i) for i in range(count)], 0)


def test_m1_turns_paged_again_by_a_launch_that_cannot_resume_are_not_counted_again(tmp_path):
    from contexto.stats import exit_summary

    fill_pages(tmp_path, "m1", 26)
    lines = three_launches()
    write_log(tmp_path, "m1", lines)
    whole = session_stats(tmp_path, "m1")
    assert whole["pages"]["count"] == 26 and whole["paging"] == {"rotations": 7, "pages_added": 26,
                                                                  "carried_items_paged": 0}
    text = "\n".join(render(whole))
    assert "paged out   26 pages on disk, about" in text and "; 7 rotations" in text
    assert "69" not in text and "turns" not in text  # the sum of page_out.turns is never shown
    # The run in which 3 turns were typed gained 3 pages, whatever its page_out event counted.
    cut = lines.index(next(e for e in lines if e.get("ts") == ts(400)))
    write_log(tmp_path, "m1b", lines[:cut])
    fill_pages(tmp_path, "m1b", 23)
    second = session_stats(tmp_path, "m1b", last_run=True)
    assert second["paging"] == {"rotations": 1, "pages_added": 3, "carried_items_paged": 0}
    summary = "\n".join(exit_summary(tmp_path, "m1b", "claude", 5.0))
    assert "paged out   3 pages in 1 rotation this run; the session has 23 pages on disk" in summary
    assert "23 turns" not in summary
    assert session_stats(tmp_path, "m1", last_run=True)["paging"]["pages_added"] == 3


def test_m1_a_run_in_progress_counts_from_the_disk_and_an_old_log_gives_no_number(tmp_path):
    # Still running (no proxy_stop yet): the store as it stands is the end of the run.
    fill_pages(tmp_path, "g1", 9)
    write_log(tmp_path, "g1", [*launch(0, pages=4), request(1, events=paged(5, 4))])
    assert session_stats(tmp_path, "g1", last_run=True)["paging"]["pages_added"] == 5
    assert session_stats(tmp_path, "g1", last_run=True, store=False)["paging"]["pages_added"] is None
    # A run killed before its proxy_stop ends where the next run starts.
    write_log(tmp_path, "g1", [*launch(0, pages=0), request(1, events=paged(4)), *launch(50, pages=4),
                               request(51, events=paged(5, 4)), *stop(60, pages=9)])
    assert session_stats(tmp_path, "g1")["paging"]["pages_added"] == 9
    assert session_stats(tmp_path, "g1", last_run=True)["paging"]["pages_added"] == 5
    # A log written before the proxy recorded the count: no number, and no sum of page_out.turns either.
    fill_pages(tmp_path, "g2", 26)
    write_log(tmp_path, "g2", three_launches(pages=False))
    for last_run in (False, True):
        out = session_stats(tmp_path, "g2", last_run=last_run)
        assert out["paging"]["pages_added"] is None and out["paging"]["rotations"] == (1 if last_run else 7)
    last = "\n".join(render(session_stats(tmp_path, "g2", last_run=True), brief=True))
    assert "paged out   1 rotation this run; the session has 26 pages on disk" in last
    # A count that cannot be one (damage) is no count; pages removed during a run are not a negative gain.
    write_log(tmp_path, "g3", [*launch(0, pages=-4), *stop(5, pages="many")])
    assert session_stats(tmp_path, "g3")["paging"]["pages_added"] is None
    write_log(tmp_path, "g3", [*launch(0, pages=9), *stop(5, pages=7)])
    assert session_stats(tmp_path, "g3")["paging"]["pages_added"] == 0


def test_l5_a_run_that_paged_nothing_does_not_show_the_session_s_pages_as_its_own(tmp_path):
    fill_pages(tmp_path, "q1", 43)
    write_log(tmp_path, "q1", [*launch(0, pages=0), request(1, events=paged(43)), *stop(9, pages=43),
                               *launch(50, pages=43), request(51), *stop(60, pages=43)])
    [line] = [x for x in render(session_stats(tmp_path, "q1", last_run=True), brief=True) if "paged out" in x]
    assert line.startswith("  paged out   nothing this run; the session has 43 pages on disk, about ")


def test_count_pages_counts_page_files_without_opening_them(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, "cp")
    ids = store.page_out([turn(i) for i in range(3)], 0)
    (store.pages_dir / "pg_0000000000000000.json").symlink_to(store.pages_dir / f"{ids[0]}.json")
    (store.pages_dir / "notes.txt").write_text("not a page")
    (store.pages_dir / "pg_dir.json").mkdir()
    monkeypatch.setattr(stats, "open_regular", lambda *a, **k: pytest.fail("a page was opened for the count"))
    assert stats.count_pages(store.pages_dir) == 3
    assert stats.count_pages(tmp_path / "nothing") == 0  # a session that has paged nothing yet
    assert stats.count_pages(store.pages_dir / "notes.txt") is None  # not a directory: no count, not 0


def test_m2_a_larger_request_without_usage_is_said_beside_the_measured_peak(tmp_path):
    refused = request(2, tokens=None, estimate=412_000, status=400, events=(over(in_tool_loop=False),))
    write_log(tmp_path, "u1", [*launch(0), request(1, tokens=50_000, estimate=49_000), refused,
                               request(3, tokens=None, estimate=20_000, client_aborted=True)])
    out = session_stats(tmp_path, "u1")
    assert out["prompt"]["peak_tokens"] == 50_000 and out["prompt"]["peak_source"] == "usage"
    assert out["prompt"]["peak_estimate_without_usage"] == 412_000
    [line] = [x for x in render(out) if x.startswith("  prompt")]
    assert ("peak 50,000 of 150,000 tokens (measured); a request the API reported no usage for was estimated "
            "larger, at 412,000") in line
    # An estimate below the measured peak is in the JSON and adds nothing to the text.
    write_log(tmp_path, "u2", [*launch(0), request(1, tokens=50_000), request(2, tokens=None, estimate=20_000)])
    out = session_stats(tmp_path, "u2")
    assert out["prompt"]["peak_estimate_without_usage"] == 20_000
    assert "estimated larger" not in "\n".join(render(out))
    # A request that has usage never counts here, however high its estimate ran.
    write_log(tmp_path, "u3", [*launch(0), request(1, tokens=50_000, estimate=90_000)])
    assert session_stats(tmp_path, "u3")["prompt"]["peak_estimate_without_usage"] is None


def bare_usage(second: int, tokens: int, **cache) -> dict:
    """A Messages request whose usage reports the cache fields as given (``None`` is the proxy's
    record of a field the API left out)."""
    line = request(second, tokens=tokens)
    line["usage"] = {"input_tokens": tokens, "output_tokens": 5, "cache_read_input_tokens": None,
                     "cache_creation_input_tokens": None, **cache}
    return line


def test_m3_the_cache_share_covers_only_requests_that_reported_a_cache_field(tmp_path):
    write_log(tmp_path, "h1", [*launch(0), bare_usage(1, 4_278), bare_usage(2, 9_000)])
    responses = request(3, api="responses", tokens=6_000)
    del responses["usage"]["input_tokens_details"]
    write_log(tmp_path, "h2", [*launch(0, cli="codex"), responses])
    for session in ("h1", "h2"):
        out = session_stats(tmp_path, session)
        assert out["cache"]["read_share"] is None and out["cache"]["requests"] == 0, session
        assert out["cache"]["unreported_requests"] == out["requests"]["with_usage"] > 0
        assert not [x for x in render(out) if x.startswith("  cache")]  # never "0% ... (measured)"
        assert out["prompt"]["peak_source"] == "usage"  # the prompt was still measured
    # One request reported the field and two did not: the share is over the one that did.
    reporting = bare_usage(1, 3_000, cache_read_input_tokens=7_000, cache_creation_input_tokens=0)
    write_log(tmp_path, "h3", [*launch(0), reporting, bare_usage(2, 20_000), bare_usage(3, 21_000)])
    out = session_stats(tmp_path, "h3")
    assert out["cache"] == {"requests": 1, "unreported_requests": 2, "input_tokens": 10_000,
                            "cache_read_tokens": 7_000, "cache_write_tokens": 0, "read_share": 0.7}
    assert out["prompt"]["peak_tokens"] == 21_000 and out["requests"]["with_usage"] == 3
    [line] = [x for x in render(out) if x.startswith("  cache")]
    assert line == ("  cache       70% of input tokens read from cache (measured, over the 1 of 3 requests with usage "
                    "that reported a cache field)")
    # A reported zero is a measurement.
    write_log(tmp_path, "h4", [*launch(0), bare_usage(1, 500, cache_read_input_tokens=0)])
    assert "  cache       0% of input tokens read from cache (measured)" in render(session_stats(tmp_path, "h4"))


def test_m4_the_recall_room_charge_is_named_a_charge_not_tokens_read(tmp_path):
    write_log(tmp_path, "t1", [*launch(0), request(1), mcp("recall", 2, charged=5_322, chars=7_983),
                               mcp("search", 3, charged=7_000, chars=10_500)])
    out = session_stats(tmp_path, "t1")
    assert out["tools"]["recall"]["charged_tokens"] == 5_322
    [line] = [x for x in render(out) if x.startswith("  recall")]
    assert "read back" not in line and "an estimate" not in line
    assert ("about 12k tokens charged to the recall room (a budget charge that runs high, not a count of what was "
            "read)") in line
    docs = DOCS.read_text()
    assert "errs high for tool output" in docs  # the pages' estimate, which is not comparable with the charge


def test_l7_calls_that_found_no_room_and_failed_calls_are_said(tmp_path):
    spent = {"turn_budget": "spent", "charged_tokens": 0, "room_left": 0}
    write_log(tmp_path, "t2", [*launch(0), request(1), dict(mcp("search", 2), **spent), dict(mcp("state", 3), **spent),
                               mcp("recall", 4, ok=False, error="ToolError"), mcp("pin", 5, ok=False)])
    out = session_stats(tmp_path, "t2")
    assert out["tools"]["out_of_room"] == 2 and out["tools"]["search"]["calls"] == 1
    [line] = [x for x in render(out) if x.startswith("  recall")]
    assert line == ("  recall      2 calls (1 search, 1 state), 2 of which found the turn's recall room used up and "
                    "returned little or nothing; 2 failed (1 recall, 1 pin)")
    write_log(tmp_path, "t3", [*launch(0), request(1), mcp("recall", 4, ok=False)])
    assert "  recall      no call succeeded; 1 failed (1 recall)" in render(session_stats(tmp_path, "t3"))


COMPACTED = {"event": "main_changed", "reason": "compacted", "turn_starts": 2}


def test_m5_no_compaction_is_claimed_only_when_the_log_could_have_shown_one(tmp_path):
    def line(out: dict) -> str:
        return next(x for x in render(out) if x.startswith("  compaction"))

    # Today's Claude Code, with its session id on every request: the claim holds.
    write_log(tmp_path, "c1", [*launch(0), request(1, events=(version(),)), request(2)])
    out = session_stats(tmp_path, "c1")
    assert out["compaction"] == {"seen": 0, "detectable": True, "reasons": [], "cli_versions": ["2.1.318"]}
    assert line(out) == "  compaction  none seen: Claude Code did not compact the main conversation"
    # No Claude Code session id on the requests: the engine cannot recognise a compaction.
    write_log(tmp_path, "c2", [*launch(0), request(1, events=(version(),), cli_session=False),
                               request(2, cli_session=False)])
    out = session_stats(tmp_path, "c2")
    assert out["compaction"] == {"seen": 0, "detectable": False, "reasons": ["no_session_id"],
                                 "cli_versions": ["2.1.318"]}
    assert "did not compact" not in line(out) and "cannot be told" in line(out) and "session id" in line(out)
    # A User-Agent the engine does not read (no cli_version event), or a version the check was not measured on.
    write_log(tmp_path, "c3", [*launch(0), request(1), request(2)])
    out = session_stats(tmp_path, "c3")
    assert out["compaction"] == {"seen": 0, "detectable": False, "reasons": ["cli_version_unknown"],
                                 "cli_versions": []}
    assert "did not compact" not in line(out)
    write_log(tmp_path, "c4", [*launch(0), request(1, events=(version("3.0.0"),)), request(2)])
    out = session_stats(tmp_path, "c4")
    assert out["compaction"] == {"seen": 0, "detectable": False, "reasons": ["cli_version_not_measured"],
                                 "cli_versions": ["3.0.0"]}
    assert "did not compact" not in line(out) and "3.0.0" in line(out)
    # A log written before the engine recorded the session id: nothing is claimed for it either.
    write_log(tmp_path, "c5", [*launch(0), request(1, events=(version(),), cli_session=None)])
    assert session_stats(tmp_path, "c5")["compaction"]["reasons"] == ["no_session_id"]
    # One run that could not show a compaction leaves the whole session unable to; its latest run can.
    write_log(tmp_path, "c6", [*launch(0), request(1, cli_session=False), *stop(5),
                               *launch(50), request(51, events=(version(),))])
    assert session_stats(tmp_path, "c6")["compaction"]["detectable"] is False
    assert session_stats(tmp_path, "c6", last_run=True)["compaction"]["detectable"] is True
    # A compaction that was seen is said either way, as a lower bound when others could have been missed.
    write_log(tmp_path, "c7", [*launch(0), request(1, events=(version(), COMPACTED))])
    assert line(session_stats(tmp_path, "c7")) == "  compaction  Claude Code compacted the main conversation 1 time"
    write_log(tmp_path, "c8", [*launch(0), request(1, events=(version("3.0.0"), COMPACTED))])
    assert "at least 1 time" in line(session_stats(tmp_path, "c8"))


def test_m5_the_versions_the_compaction_check_was_measured_on_are_the_engine_s():
    from contexto import cliversions, rewrite

    source = Path(rewrite.__file__).read_text()
    note = source[source.index("# D50, D59: the start of the summary"):source.index("CLAUDE_CODE_COMPACTION_SUMMARY =")]
    assert stats.COMPACTION_MEASURED_ON and all(v in note for v in stats.COMPACTION_MEASURED_ON)
    assert set(re.findall(r"\d+\.\d+\.\d+", note)) == set(stats.COMPACTION_MEASURED_ON)
    assert set(stats.COMPACTION_MEASURED_ON) <= set(cliversions.TESTED_VERSIONS["claude"])


def test_l1_requests_that_are_in_no_other_number_are_counted(tmp_path):
    skipped = request(3, tokens=None)
    skipped.update(transform_skipped="request body is not JSON", transform_events=[])
    cut = request(4, tokens=None, error="relay: IncompleteRead")
    write_log(tmp_path, "x1", [
        *launch(0), request(1),
        {"event": "encoded_body_rejected", "encoding": "br", "endpoint": "/v1/messages", "reason": "unsupported",
         "ts": ts(2)},
        {"event": "encoded_body_rejected", "encoding": "br", "endpoint": "/v1/messages", "reason": "unsupported",
         "ts": ts(2)},
        skipped, cut, {"event": "proxy_stop_incomplete", "in_flight": 1, "ts": ts(5)}])
    out = session_stats(tmp_path, "x1")
    assert out["requests"]["model"] == 3 and out["requests"]["rejected_encoding"] == 2
    assert out["requests"]["not_transformed"] == 1 and out["requests"]["cut_short"] == 1
    assert out["requests"]["in_flight_at_stop"] == 1
    assert out["faults"]["events"] == {"encoded_body_rejected": 2}
    [line] = [x for x in render(out) if x.startswith("  requests")]
    assert line == ("  requests    3 model requests (3 main, 0 side); 1 response cut short; 1 forwarded as the CLI "
                    "sent it, unread; 2 more refused with 415 before they were read; 1 still in flight when the "
                    "proxy stopped, so not in these numbers")
    assert [x for x in render(out, brief=True) if x.startswith("  requests")] == [line]  # in the exit summary too


def test_l1_faults_logged_while_a_launch_opens_its_session_belong_to_that_run(tmp_path):
    """The store repairs its ledger, and the launcher reads the affinity salt, before the proxy starts."""
    write_log(tmp_path, "y1", [*launch(0), request(1), *stop(5),
                               {"event": "ledger_repaired", "quarantine": "ledger.jsonl.torn", "ts": ts(50)},
                               {"event": "affinity_salt_unavailable", "error": "PermissionError", "ts": ts(50)},
                               *launch(51), request(52)])
    last = session_stats(tmp_path, "y1", last_run=True)
    assert last["faults"]["events"] == {"ledger_repaired": 1, "affinity_salt_unavailable": 1}
    assert last["requests"]["model"] == 1 and last["started"] == ts(50)
    # Lines after the last run's exit that no launch follows are not a run.
    write_log(tmp_path, "y2", [*launch(0), request(1), *stop(5), {"event": "ledger_repaired", "ts": ts(50)}])
    last = session_stats(tmp_path, "y2", last_run=True)
    assert last["faults"]["total"] == 0 and last["requests"]["model"] == 1
    assert session_stats(tmp_path, "y2")["faults"]["events"] == {"ledger_repaired": 1}


@pytest.mark.parametrize("tokens,text", [(999, "999"), (9_999, "9,999"), (10_000, "10k"), (85_400, "85k"),
                                         (999_499, "999k"), (999_500, "1.0M"), (999_999, "1.0M"), (1_000_000, "1.0M"),
                                         (1_949_999, "1.9M")])
def test_l2_a_token_count_never_reads_1000k(tokens, text):
    assert stats._about(tokens) == text


@pytest.mark.parametrize("share,text", [(0.0, "0%"), (0.004, "under 1%"), (0.0149, "1%"), (0.5, "50%"), (0.9949, "99%"),
                                        (0.9951, "over 99%"), (0.9996, "over 99%"), (1.0, "100%"), (3.0, "300%")])
def test_l2_a_share_reads_0_or_100_percent_only_when_it_is(share, text):
    assert stats.percent_text(share) == text


@pytest.mark.parametrize("size,text", [(1023, "1023 B"), (1024, "1.0 KiB"), (1_048_575, "1.0 MiB"),
                                       (1024 ** 2 * 1024 - 1, "1.0 GiB"), (5 * 1024 ** 3, "5.0 GiB"),
                                       (5120 * 1024 ** 3, "5.0 TiB"), (345_700 * 1024 // 1000, "345.7 KiB")])
def test_l2_a_size_never_reads_1024_of_a_unit(size, text):
    assert stats.size_text(size) == text


def test_l4_two_short_runs_days_apart_are_not_three_days_of_work(tmp_path):
    day = 86_400
    write_log(tmp_path, "z1", [*launch(0), request(2), *stop(5), *launch(3 * day), request(3 * day + 2),
                               *stop(3 * day + 5)])
    out = session_stats(tmp_path, "z1")
    assert out["span_seconds"] == 3 * day + 5 and out["run_seconds"] == 10
    done = contexto(tmp_path, "stats", "z1")
    assert done.stdout.splitlines()[0] == ("Contexto stats for session z1: the whole session (2 runs, 10s in runs, "
                                           "72h00m from the first log line to the last)")
    last = session_stats(tmp_path, "z1", last_run=True)
    assert last["span_seconds"] == last["run_seconds"] == 5
    # A run that was killed has no exit line: it lasts to its last line.
    write_log(tmp_path, "z2", [*launch(0), request(7), *launch(100), request(103)])
    assert session_stats(tmp_path, "z2")["run_seconds"] == 10


def test_l6_values_that_cannot_be_a_count_are_ignored_and_counted(tmp_path):
    negative = request(2, tokens=9_000, original=-10, sent=-10)
    worse = request(3, tokens=8_000)
    worse.update(request_bytes="many", status=-200)
    worse["usage"]["input_tokens"] = -5
    write_log(tmp_path, "v1", [
        *launch(0), request(1, tokens=7_000, original=3_000, sent=2_000), negative, worse,
        {"event": "exit", "code": 0, "ts": "yesterday"}, {"event": "mcp_call", "tool": "contexto_search", "ok": True,
                                                         "charged_tokens": -40, "ts": 12}])
    out = session_stats(tmp_path, "v1")
    assert out["bytes"]["cli_sent"] == 3_000 and out["bytes"]["forwarded"] == 2_000  # never "the CLI sent -10 B"
    assert out["ended"] == ts(3) and out["span_seconds"] == 3  # "yesterday" and 12 are not timestamps
    assert out["tools"]["search"]["charged_tokens"] == 0 and out["prompt"]["peak_tokens"] == 9_000
    # negative: 2 byte counts; worse: a byte count, a status, a token count; 2 timestamps; a charge.
    assert out["log"] == {"missing": False, "lines": 7, "unreadable_lines": 0, "ignored_values": 8}
    text = "\n".join(render(out))
    assert "8 values in the log were not what their field holds" in text and "-10" not in text


def test_l6_a_page_with_a_damaged_head_or_a_negative_estimate_is_unreadable(tmp_path):
    store = SessionStore(tmp_path, "pd")
    ids = store.page_out([turn(i) for i in range(4)], 0)
    files = [store.pages_dir / f"{i}.json" for i in ids]
    tokens = [json.loads(f.read_text())["tokens"] for f in files]
    data = files[0].read_bytes()
    files[0].write_bytes(b"\x00" * 40 + data[40:])  # the head is gone and the last bytes are intact
    spaced = json.loads(files[1].read_text())
    files[1].write_text(json.dumps(dict(spaced, tokens=-500), indent=2))  # not canonical, and not a count
    other = files[2].read_bytes()
    files[2].write_bytes(other.replace(ids[2].encode(), b"pg_ffffffffffffffff", 1))  # another page's record
    out = session_stats(tmp_path, "pd")["pages"]
    assert out["count"] == 4 and out["unreadable"] == 3 and out["tokens_estimate"] == tokens[3]


def test_l8_a_large_log_is_read_in_blocks_and_gives_the_same_numbers(tmp_path, monkeypatch):
    lines = long_session_log(600)
    lines.insert(40, '{"event": "request", "cut short')  # one damaged line: only its block is read line by line
    directory = write_log(tmp_path, "big", lines)
    whole = session_stats(tmp_path, "big")
    assert whole["log"]["unreadable_lines"] == 1 and whole["requests"]["model"] == 600
    size = (directory / "log.jsonl").stat().st_size
    parsed: list[int] = []
    loads = json.loads

    def spy(data, *args, **kwargs):
        parsed.append(len(data))
        return loads(data, *args, **kwargs)

    monkeypatch.setattr(stats.json, "loads", spy)
    monkeypatch.setattr(stats, "LOG_BLOCK_BYTES", 16_384)
    assert session_stats(tmp_path, "big") == whole  # block edges fall inside lines, and nothing changes
    assert size > 20 * 16_384 and max(parsed) < 2 * 16_384 + 64  # no parse ever held more than a block
    # A line longer than a block (not a log line) is counted unreadable without being held or parsed.
    write_log(tmp_path, "wide", [*launch(0), request(1), "[" * 100_000, request(2), b"\xff" * 50_000])
    out = session_stats(tmp_path, "wide")
    assert out["log"] == {"missing": False, "lines": 6, "unreadable_lines": 2, "ignored_values": 0}
    assert out["requests"]["model"] == 2


# Reviewer A's findings (PR #263) -------------------------------------------------------------------

def test_a_store_still_failing_at_exit_is_not_reported_as_recovered(tmp_path):
    """A disk that stays full while the session is under the bound: the publish after the request was
    built fails, so the engine's ``request`` event has no ``degraded`` flag, and the next turn start
    leaves degraded mode and enters it again. The enter and leave events decide the state."""
    from contexto.stats import exit_summary

    failed = (store_error("set_turn_room"), ENTER)
    write_log(tmp_path, "sf", [*launch(0), request(1), request(2, events=failed),
                               request(3, events=(LEAVE, *failed)), *stop(9)])
    degraded = session_stats(tmp_path, "sf")["faults"]["degraded"]
    assert (degraded["entered"], degraded["left"], degraded["now"], degraded["since"]) == (2, 1, True, ts(3))
    summary = "\n".join(exit_summary(tmp_path, "sf", "claude", 5.0))
    assert "paging had not resumed at exit" in summary and "resumed before exit" not in summary
    assert "has not resumed" in session_faults(tmp_path, "sf")["messages"][-1]
    # The request event's own flag still decides when the line has neither event (a lost enter line).
    write_log(tmp_path, "sg", [*launch(0), request(1, degraded=True), request(2, degraded=True)])
    assert session_stats(tmp_path, "sg")["faults"]["degraded"]["now"] is True
    write_log(tmp_path, "sh", [*launch(0), request(1, degraded=True, events=(store_error(), ENTER)), request(2)])
    assert session_stats(tmp_path, "sh")["faults"]["degraded"]["now"] is False


def test_reading_pins_never_waits_for_the_ledger_lock_and_creates_nothing(tmp_path):
    import fcntl
    import threading

    store = SessionStore(tmp_path, "lk")
    store.pin("obligation", "synthetic standing rule")
    store.pin("decision", "synthetic decision")
    write_log(tmp_path, "lk", [*launch(0), request(1)])
    lock = store.dir / "ledger.lock"
    holder = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)  # a stuck writer: an MCP server on a hung filesystem
        found: dict = {}
        reader = threading.Thread(target=lambda: found.update(session_stats(tmp_path, "lk")["pins"]), daemon=True)
        reader.start()
        reader.join(15)
        waiting = reader.is_alive()
    finally:
        os.close(holder)
    assert not waiting, "session_stats waited for the ledger's lock"
    assert found == {"readable": True, "active_records": 2, "open_obligations": 1}
    # A session inspected by stats is not changed: no lock file is made, and its mode is left alone.
    lock.unlink()
    store.dir.chmod(0o755)
    before = sorted(path.name for path in store.dir.iterdir())
    assert session_stats(tmp_path, "lk")["pins"]["active_records"] == 2
    assert sorted(path.name for path in store.dir.iterdir()) == before
    assert store.dir.stat().st_mode & 0o777 == 0o755


def run_lines(start: int, requests: int, pages: int = 0) -> list[dict]:
    return [*launch(start, pages=pages), *[request(start + 1 + i, tokens=1_000 + i) for i in range(requests)],
            *stop(start + 50, pages=pages)]


def test_the_last_run_is_read_from_the_end_of_the_log(tmp_path, monkeypatch):
    """Reviewer A's L1: the exit summary and `contexto state` parsed every line of a resumed session's
    log. Only the latest run's lines are parsed now; the rest of the file is searched for run starts."""
    lines = [line for start in range(0, 40_000, 100) for line in run_lines(start, 40)]
    lines += [{"event": "ledger_repaired", "quarantine": "x", "ts": ts(50_000)}, *run_lines(50_001, 3, pages=2)]
    directory = write_log(tmp_path, "lr", lines)
    size = (directory / "log.jsonl").stat().st_size
    parsed: list[int] = []
    loads = json.loads

    def spy(data, *args, **kwargs):
        parsed.append(len(data))
        return loads(data, *args, **kwargs)

    monkeypatch.setattr(stats.json, "loads", spy)
    last = session_stats(tmp_path, "lr", last_run=True)
    monkeypatch.undo()
    assert size > 5_000_000 and sum(parsed) < size / 20  # the run's lines, and one line per run start
    assert last["runs"] == 401 and last["requests"]["model"] == 3 and last["started"] == ts(50_000)
    assert last["faults"]["events"] == {"ledger_repaired": 1} and last["cli"] == "claude"
    # Its launch's line, the proxy's two, the launcher's two and three requests: the lines in scope.
    assert last["log"] == {"missing": False, "lines": 8, "unreadable_lines": 0, "ignored_values": 0}
    assert session_stats(tmp_path, "lr")["requests"]["model"] == 16_003


def test_the_last_run_read_from_the_end_is_the_one_a_full_read_finds(tmp_path, monkeypatch):
    import random

    rng = random.Random(64)
    pieces = {
        "run": lambda at: run_lines(at, rng.randrange(0, 4)),
        "killed": lambda at: run_lines(at, 2)[:-2],  # no proxy_stop, no exit
        "bare proxy": lambda at: [x for x in run_lines(at, 1) if x["event"] not in ("launch", "exit")],
        "opening": lambda at: [{"event": name, "ts": ts(at)} for name in ("ledger_repaired", "stale_temp_removed")],
        "many": lambda at: [{"event": "stale_temp_removed", "ts": ts(at)}] * 70,  # more than a launch writes
        "cut": lambda at: ['{"event": "request", "cut short', "[1, 2]"],
        "exit": lambda at: [{"event": "exit", "code": 0, "ts": ts(at)}],
        "blank": lambda at: ["", "   "],
    }
    for case in range(120):
        lines = []
        for step in range(rng.randrange(0, 9)):
            lines += pieces[rng.choice(list(pieces))](case * 1_000 + step * 60)
        write_log(tmp_path, f"q{case}", lines)
        monkeypatch.setattr(stats, "LOG_BLOCK_BYTES", rng.choice([256, 4_096, 4 << 20]))
        fast = session_stats(tmp_path, f"q{case}", last_run=True)
        with monkeypatch.context() as whole:
            whole.setattr(stats, "_last_run_tail", lambda directory: (0, 0, None))  # read every line
            slow = session_stats(tmp_path, f"q{case}", last_run=True)
        fast_log, slow_log = fast.pop("log"), slow.pop("log")
        assert fast == slow, (case, lines)
        assert fast_log["lines"] <= slow_log["lines"] and fast_log["ignored_values"] == slow_log["ignored_values"]


def test_a_fault_that_is_not_a_store_error_is_explained_in_stats(tmp_path):
    write_log(tmp_path, "ex", [*launch(0), request(1, events=({"event": "retained_stale", "error": "OSError"},)),
                               request(2, events=({"event": "retained_stale", "error": "OSError"},)),
                               {"event": "transform_error", "error": "KeyError", "ts": ts(3)}])
    text = render(session_stats(tmp_path, "ex"))
    at = text.index("  faults      3 events (retained_stale 2, transform_error 1)")
    assert text[at + 1:at + 3] == [
        "    retained_stale x2: the snapshot of the conversation in view could not be written",
        "    transform_error x1: a Contexto bug: a request was forwarded as the CLI sent it"]


# A damaged log ---------------------------------------------------------------------------------------

def test_lines_that_only_parse_when_joined_are_still_damaged_lines(tmp_path):
    """A healthy log is parsed in one call; these lines parse that way and not on their own."""
    write_log(tmp_path, "d2", [*launch(0), request(1, tokens=9_000), '{"event": "request", "x": [1', "2]}",
                               '{"event": "exit"}, {"event": "transform_error"}', "1, 2"])
    out = session_stats(tmp_path, "d2")
    assert out["log"] == {"missing": False, "lines": 7, "unreadable_lines": 4, "ignored_values": 0}
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
    assert out["log"]["lines"] == 17 and out["log"]["unreadable_lines"] == 6
    assert out["requests"]["model"] == 3 and out["prompt"]["peak_tokens"] == 9_000
    assert out["bytes"]["cli_sent"] == 4_000  # the two lines whose byte counts are numbers
    assert out["paging"] == {"rotations": 1, "pages_added": None, "carried_items_paged": 0}
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
    assert out["log"] == {"missing": damage != "empty", "lines": 0, "unreadable_lines": 0, "ignored_values": 0}
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
    assert "is full" in message and "so paging has stopped" in message  # still failing: the present tense
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
    assert "Paging has resumed; meanwhile 3 requests went out unpaged, the largest " in text
    # It is over, so it is told as past: nothing says paging "has stopped" or that the store "cannot" be used.
    assert "has stopped" not in text and "cannot" not in text and "could not read or write its store" in text
    assert "so paging stopped: requests went out unpaged and could exceed the bound" in text


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
        {"event": "proxy_stop_incomplete", "in_flight": 2},  # its own number, under requests
    ]
    nested_normal = ({"event": "epoch_not_resumed", "reason": "no_record"}, {"event": "usage_missing"},
                     {"event": "usage_unattributed"}, {"event": "resync", "kind": "window_changed"},
                     {"event": "rotation_noop"}, {"event": "turn_room_low", "room": 9}, {"event": "head_changed"},
                     {"event": "window_minimal"}, {"event": "pdf_allowance"}, {"event": "estimate_error"},
                     {"event": "mcp_tools_found"}, {"event": "cli_version", "version": "2.1.318"},
                     {"event": "cli_version_untested", "version": "9.9.9"}, {"event": "pdf_pages_uncertain"},
                     {"event": "sizing_restored"}, {"event": "main_changed", "reason": "more_turn_starts"},
                     {"event": "carried_text_growing", "messages": 3},
                     {"event": "sizing_not_restored", "reason": "model_changed"})
    write_log(tmp_path, "n1", [*launch(0), *expected_in_normal_use, request(1, events=nested_normal),
                               request(2, status=429), request(3, client_aborted=True, tokens=None)])
    write_log(tmp_path, "n2x", [*launch(0), request(1)])
    out = session_stats(tmp_path, "n1")
    assert out["faults"]["total"] == 0 and out["faults"]["events"] == {}
    assert out["requests"]["failed"] == 1 and out["requests"]["status"] == {"429": 1}
    assert out["requests"]["aborted"] == 1 and out["requests"]["in_flight_at_stop"] == 2
    assert "faults      none" in "\n".join(render(out)) and "1 failed (429 x1)" in "\n".join(render(out))
    # The exit summary leaves the request line to `contexto stats`, unless a request failed.
    assert "1 failed (429 x1)" in "\n".join(render(out, brief=True))
    assert not [line for line in render(session_stats(tmp_path, "n2x"), brief=True) if "requests" in line.split()[:1]]
    wrong_top = [{"event": name} for name in ("transform_error", "proxy_error", "request_too_deep", "page_quarantined",
                                              "page_unreadable", "ledger_damaged", "turn_room_missing",
                                              "engine_unavailable", "websocket_rejected", "observe_usage_error",
                                              "encoded_body_rejected", "request_refused", "ledger_repaired",
                                              "pruned_while_opening", "search_cache_invalid", "watchdog_unavailable",
                                              "affinity_salt_unavailable", "tool_calls_hook_error")]
    wrong_nested = tuple({"event": name} for name in (
        "fallback_failed", "retained_stale", "epoch_record_stale", "mcp_tools_missing", "mcp_tools_denied",
        "toolcheck_error", "cli_version_error", "side_room_close_failed", "unclassified_system_message",
        "carried_over_budget", "bound_too_small", "session_start_unrecognised"))
    write_log(tmp_path, "n2", [*launch(0), *wrong_top, request(1, events=wrong_nested),
                               {"event": "mcp_tools_missing", "at": "exit", "repeated": False}])
    out = session_stats(tmp_path, "n2")
    assert out["faults"]["events"] == {**{e["event"]: 1 for e in [*wrong_top, *wrong_nested]}, "mcp_tools_missing": 2}
    assert out["faults"]["total"] == 31
    assert set(out["faults"]["events"]) | {"degraded_enter", "store_error", "page_conflict"} == set(FAULT_EVENTS)
    assert "31 events" in "\n".join(render(out)) and "transform_error 1" in "\n".join(render(out))


def test_a_fault_nothing_else_warned_about_outlives_quiet(tmp_path):
    from contexto.stats import NOTICED_EVENTS, exit_summary

    write_log(tmp_path, "w1", [*launch(0), {"event": "transform_error", "level": "error", "error": "KeyError"},
                               request(1, events=({"event": "mcp_tools_missing", "at": "request"},)),
                               request(2, events=({"event": "retained_stale", "error": "OSError"},))])
    warning = ("contexto: warning: this run logged 2 events that mean Contexto left its designed path "
               "(retained_stale 1, transform_error 1). Details: contexto stats w1")
    assert exit_summary(tmp_path, "w1", "claude", 5.0, quiet=True) == [warning]
    loud = exit_summary(tmp_path, "w1", "claude", 5.0)
    assert loud[0] == "contexto: run of session w1 ended after 5s" and loud[-1] == warning
    assert ("  faults      3 events (mcp_tools_missing 1, retained_stale 1, transform_error 1); details: contexto "
            "stats w1") == loud[-2]
    # A run whose only faults the launcher's own warnings described adds no second warning.
    denied = ({"event": "mcp_tools_denied"}, {"event": "bound_too_small"})
    write_log(tmp_path, "w2", [*launch(0), request(1, degraded=True, events=(store_error(), ENTER)),
                               request(2, degraded=True, events=denied),
                               {"event": "page_quarantined", "page_id": "pg_00aa", "ts": ts(3)}])
    assert exit_summary(tmp_path, "w2", "claude", 5.0, quiet=True) == []
    write_log(tmp_path, "w3", [*launch(0), request(1), {"event": "proxy_error", "ts": ts(2)}])
    assert exit_summary(tmp_path, "w3", "claude", 5.0, quiet=True) == [
        "contexto: warning: this run logged 1 event that means Contexto left its designed path (proxy_error 1). "
        "Details: contexto stats w3"]
    assert NOTICED_EVENTS < set(FAULT_EVENTS)


def test_the_engine_s_own_fault_list_is_covered():
    """The contract's acceptance check (M2, check 1) fails a run on these events; all but one are faults
    here. ``turn_room_low`` is left out on purpose: at a small bound every epoch logs it (docs/stats.md)."""
    contract = (Path(__file__).resolve().parent.parent / "docs" / "milestones" / "M2-contexto-proxy.md").read_text()
    section = contract[contract.index("means the product left its designed path"):]
    listed = set(re.findall(r"`([a-z_]+)`", section[:section.index("2. **Fidelity")]))
    assert {"degraded_enter", "store_error", "transform_error", "turn_room_low"} <= listed
    assert listed - set(FAULT_EVENTS) == {"turn_room_low"}


def test_every_event_the_source_writes_is_a_fault_or_is_left_out_on_purpose():
    """L1: an event name added to the source must be put in ``FAULT_EVENTS`` or in ``NOT_FAULT_EVENTS``,
    and docs/stats.md must say which, so no new event is left uncounted by oversight."""
    source = Path(stats.__file__).resolve().parent
    written = set()
    for path in source.rglob("*.py"):
        if path.name != "stats.py":
            written |= set(re.findall(r'"event": ?"([a-z_0-9]+)"', path.read_text()))
    written |= {"store_error", "page_conflict"}  # built from a variable in rewrite.store_failure
    assert len(written) > 60 and {"request", "rotation", "encoded_body_rejected"} <= written
    decided = set(FAULT_EVENTS) | set(stats.NOT_FAULT_EVENTS)
    assert written - decided == set(), "decide these in contexto.stats and docs/stats.md"
    assert decided - written == set(), "no longer written by the source"
    assert set(FAULT_EVENTS) & set(stats.NOT_FAULT_EVENTS) == set()
    docs = DOCS.read_text()
    faults = docs[docs.index("### Which events count as a fault"):docs.index("## What the numbers cannot show")]
    for name in decided:
        assert f"`{name}`" in faults, f"docs/stats.md does not say whether {name} is a fault"


def test_the_paths_stats_counts_are_the_proxy_s():
    assert stats.MODEL_PATHS == proxy.USAGE_FEEDBACK_PATHS
    assert stats.COUNT_TOKENS_PATH == proxy.COUNT_TOKENS_PATH


# The documented JSON shape ---------------------------------------------------------------------------

DOCUMENTED = {
    "": ["version", "session", "scope", "runs", "cli", "bound", "started", "ended", "span_seconds", "run_seconds",
         "log", "requests", "prompt", "over_bound", "paging", "pages", "tools", "pins", "cache", "bytes", "compaction",
         "faults"],
    "log": ["missing", "lines", "unreadable_lines", "ignored_values"],
    "requests": ["model", "main", "side", "with_usage", "failed", "status", "aborted", "cut_short", "not_transformed",
                 "rejected_encoding", "in_flight_at_stop", "count_tokens", "other"],
    "prompt": ["peak_tokens", "peak_source", "peak_estimate_without_usage", "last_tokens", "last_source", "bound"],
    "over_bound": ["requests", "measured", "estimate_only", "in_tool_loop", "at_turn_start", "degraded"],
    "paging": ["rotations", "pages_added", "carried_items_paged"],
    "pages": ["count", "tokens_estimate", "file_bytes", "unreadable"],
    "tools": ["recall", "search", "state", "pin", "close", "out_of_room"],
    **{f"tools.{name}": ["calls", "failed", "charged_tokens"] for name in stats.TOOL_NAMES},
    "pins": ["readable", "active_records", "open_obligations"],
    "cache": ["requests", "unreported_requests", "input_tokens", "cache_read_tokens", "cache_write_tokens",
              "read_share"],
    "bytes": ["cli_sent", "forwarded", "forwarded_share", "largest_cli_request", "largest_forwarded_request"],
    "compaction": ["seen", "detectable", "reasons", "cli_versions"],
    "faults": ["total", "events", "degraded", "store_errors", "messages"],
    "faults.degraded": ["entered", "left", "now", "since", "unpaged_requests", "largest_unpaged_request_bytes"],
}
STORE_ERROR_KEYS = ["event", "operation", "error", "errno", "cause", "count", "first", "last"]


def test_the_json_shape_is_the_documented_one(tmp_path):
    write_log(tmp_path, "j1", [*launch(0), request(1, degraded=True, events=(store_error(), ENTER)),
                               mcp("recall", 2, charged=5)])
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
    # L9: an entry of faults.store_errors has its documented keys too, each named in the docs.
    [entry] = session_stats(tmp_path, "j1")["faults"]["store_errors"]
    assert sorted(entry) == sorted(STORE_ERROR_KEYS)
    row = next(line for line in docs.splitlines() if line.startswith("| `faults.store_errors`"))
    for key in STORE_ERROR_KEYS:
        assert f"`{key}`" in row, f"docs/stats.md does not name the store_errors key {key}"
    assert out["version"] == stats.STATS_VERSION == 1
    for where, keys in DOCUMENTED.items():
        for key in keys:
            name = f"{where}.{key}" if where else key
            if where.startswith("tools.") and where != "tools.recall":
                continue  # the docs define a tool's keys once, under tools.recall, for every tool
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
    assert out["tools"]["recall"]["calls"] + out["tools"]["search"]["calls"] == 445
    # Measured at about 50 ms on a loaded laptop, half of it JSON parsing (docs/stats.md); the limit only
    # catches a change that makes it many times slower, on any CI machine.
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
    assert contexto(tmp_path, "state", "--session", "st1", "--raw").stdout == store.state_text() + "\n"
    write_log(tmp_path, "st1", [*launch(0), request(1)])
    assert "Warning:" not in contexto(tmp_path, "state", "--session", "st1").stdout


# Re-review of the fixes (PR #263, Reviewer B) ------------------------------------------------------


@pytest.mark.parametrize("stamp", ["2026-11-13T10:00:05", "2026-11-13"])
def test_a_time_without_a_utc_offset_is_ignored_not_a_traceback(tmp_path, stamp):
    """A `ts` with no offset could not be subtracted from one with `Z`: `contexto stats` and `contexto
    state` exited 1 with a TypeError. Contexto always writes `Z`, so such a line was edited or damaged:
    its time is not used, and it is counted as an ignored value."""
    naive = request(5)
    naive["ts"] = stamp
    write_log(tmp_path, "t1", [*launch(0, pages=0), request(1), naive, request(9), *stop(10, pages=0)])
    for last_run in (False, True):
        out = session_stats(tmp_path, "t1", last_run=last_run)
        assert out["log"]["ignored_values"] >= 1 and out["requests"]["model"] == 3
    assert render(session_stats(tmp_path, "t1"))  # the text is built too


def test_messages_requests_no_engine_judged_make_compaction_undetectable(tmp_path):
    """A bare `contexto proxy` and a curl with no engine `request` event printed "none seen: Claude Code did
    not compact" with `detectable: true`. A Messages request the engine did not read (no engine, a skipped
    or failed transform) gives the reason `not_read`, and the text makes no claim."""
    unread = {"event": "request", "method": "POST", "path": "/v1/messages", "api": "messages", "status": 200,
              "original_request_bytes": 10, "request_bytes": 10, "transform_skipped": "request body is not JSON",
              "usage": None, "ts": ts(2)}
    write_log(tmp_path, "c1", [*launch(0), request(1, events=(version(),)), unread])
    out = session_stats(tmp_path, "c1")
    assert out["compaction"]["detectable"] is False and "not_read" in out["compaction"]["reasons"]
    text = "\n".join(render(out))
    assert "did not compact" not in text and "not every Messages request was read by Contexto" in text
    write_log(tmp_path, "c2", [{"event": "proxy_start", "port": 1, "ts": ts(0)}, unread])  # a bare proxy, no engine
    assert session_stats(tmp_path, "c2")["compaction"]["detectable"] is False


def test_a_stop_line_from_another_run_does_not_end_the_next_one(tmp_path):
    """Interleaved launches: a `proxy_stop` that arrives when no run is open, or before the next run's start,
    was kept and closed the next run at its count. Each run's gain is now from its own lines."""
    lines = [*launch(0, pages=0), *launch(1, pages=0), {"event": "proxy_stop", "pages": 4, "ts": ts(2)},
             {"event": "exit", "code": 0, "ts": ts(2)}, {"event": "proxy_stop", "pages": 4, "ts": ts(3)},
             *launch(4, pages=4)]
    write_log(tmp_path, "r1", lines)
    fill_pages(tmp_path, "r1", 10)  # the third run, killed, added 6
    assert session_stats(tmp_path, "r1", last_run=True)["paging"]["pages_added"] == 6
    whole = session_stats(tmp_path, "r1")["paging"]["pages_added"]
    assert whole is None or whole <= 10


@pytest.mark.parametrize("read,total,text", [(4, 100_000, "under 1%"), (99_996, 100_000, "over 99%")])
def test_a_share_that_rounds_to_none_or_all_is_not_printed_as_0_or_100_percent(tmp_path, read, total, text):
    write_log(tmp_path, "s1", [*launch(0), request(1, tokens=total, cache_read=read)])
    out = session_stats(tmp_path, "s1")
    assert 0 < out["cache"]["read_share"] < 1
    assert f"{text} of input" in "\n".join(render(out))


def test_one_request_refused_with_415_is_said_in_the_singular(tmp_path):
    refused = {"event": "encoded_body_rejected", "endpoint": "/v1/messages", "encoding": "br", "ts": ts(2)}
    write_log(tmp_path, "e1", [*launch(0), request(1), refused])
    text = "\n".join(render(session_stats(tmp_path, "e1")))
    assert "1 more refused with 415 before it was read" in text



def test_state_json_carries_the_faults_stats_json_gives(tmp_path, capsys, monkeypatch):
    """#257 added `state --json` and this pull request the fault report: the JSON carries it under `faults`,
    the same object `session_faults` gives (what `contexto stats --json` has under that key), so a script
    sees a stopped store too."""
    from contexto import cli

    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    write_log(tmp_path, "f9", [*launch(0), request(1, degraded=True, events=(store_error(), ENTER)),
                               request(2, degraded=True)])
    assert cli.main(["state", "--json", "--session", "f9"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["faults"] == session_faults(tmp_path, "f9")
    assert data["faults"]["degraded"]["now"] is True and data["faults"]["messages"]
