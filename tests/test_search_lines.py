"""contexto_search returns matching lines with their offsets (M2 B15)."""

import io
import json
import math
import re
import sys
from pathlib import Path

import pytest

from contexto import mcp
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.recall import EXAMPLE_LINE
from contexto.store.render import MATCH_CONTEXT, MATCH_LINE_CHARS, MATCHES_PER_PAGE, match_lines, render_page

SPENT = mcp.BUDGET_SPENT
LOG = "\n".join(f"s0 line {i:04d} level=INFO shard={i % 7} ok" for i in range(400))


def events(store, tool):
    log = store.events if isinstance(store, MemoryStore) else [
        json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
    return [e for e in log if e.get("event") == "mcp_call" and e.get("tool") == tool]


def credited(store, page_id, page_text, line):
    """Builder-4's acceptance 3 rule: the plant's whole line lies inside one logged returned span."""
    pos = page_text.index(line)
    return any(span["page_id"] == page_id and span["start"] <= pos and pos + len(line) <= span["end"]
               for tool in ("contexto_search", "contexto_recall") for e in events(store, tool) for span in e["returned"])


def search(store, query):
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_search", "arguments": {"query": query}}})
    result = server.handle_line(line)["result"]
    assert result["isError"] is False and "structuredContent" not in result
    [block] = result["content"]
    return block["text"]


def recall(store, page_id, offset):
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "contexto_recall", "arguments": {"page_id": page_id, "offset": offset}}})
    return server.handle_line(line)["result"]["content"][0]["text"]


# match_lines


def test_each_match_comes_with_its_bounded_context_and_exact_offsets():
    text = LOG.replace("s0 line 0200 level=INFO", "s0 line 0200 level=INFO cache.ttl_seconds=short")
    shown, total = match_lines(text, "cache.ttl_seconds")
    assert total == 1 and len(shown) == 2 * MATCH_CONTEXT + 1
    assert [is_match for _, _, is_match, _ in shown] == [False] * MATCH_CONTEXT + [True] + [False] * MATCH_CONTEXT
    for start, end, _, line in shown:
        assert text[start:end] == line  # every span is exactly the line it shows  # every offset is where its line starts in the page
    assert "cache.ttl_seconds=short" in shown[MATCH_CONTEXT][3]


def test_matches_per_page_are_capped_counted_and_their_contexts_merge():
    text = LOG.replace("shard=3 ok", "shard=3 ok NEEDLE")
    shown, total = match_lines(text, "NEEDLE")
    assert total == text.count("NEEDLE") and total > MATCHES_PER_PAGE
    assert sum(is_match for _, _, is_match, _ in shown) == MATCHES_PER_PAGE
    offsets = [start for start, _, _, _ in shown]
    assert offsets == sorted(set(offsets))  # page order, overlapping context shown once
    close = match_lines("a\nNEEDLE\nb\nNEEDLE\nc", "NEEDLE")[0]
    assert [line for _, _, _, line in close] == ["a", "NEEDLE", "b", "NEEDLE", "c"]


def test_a_long_line_is_cut_around_its_match():
    text = "head\n" + "x" * 5_000 + "cfg deploy.target=blue" + "y" * 5_000 + "\ntail"
    shown, _ = match_lines(text, "deploy.target")
    offset, end, is_match, line = shown[1]
    assert is_match and "cfg deploy.target=blue" in line
    assert len(line) == MATCH_LINE_CHARS + len(" ...") and line.endswith(" ...")
    assert end - offset == MATCH_LINE_CHARS and text[offset:end] == line.removesuffix(" ...")


def test_no_match_shows_nothing():
    assert match_lines(LOG, "absent") == ([], 0)


def test_matching_is_linear_in_the_page_size():
    import time

    text = LOG * 500  # about 18 MB
    started = time.perf_counter()
    match_lines(text, "line 0399")
    assert time.perf_counter() - started < 5


# The tool


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")


def test_a_hit_shows_the_page_then_offset_lines_matching_the_worked_example(store):
    text = LOG.replace("s0 line 0100 level=INFO", "s0 line 0100 level=INFO cache.ttl_seconds=short")
    [pid] = store.page_out([[{"role": "user", "content": text}]], 3)
    reply = search(store, "cache.ttl_seconds")
    head, *lines = reply.split("\n")
    assert head.startswith(f"{pid} (turn 3): ")
    [match] = [line for line in lines if line.startswith(">")]
    offset = int(re.match(r"> (\d+): ", match).group(1))
    page = render_page(store.get_page(pid))
    assert page[offset:].startswith("s0 line 0100 level=INFO cache.ttl_seconds=short")
    assert recall(store, pid, offset).startswith("s0 line 0100 level=INFO cache.ttl_seconds=short")
    assert re.search(r'"> \d+: cache\.ttl_seconds=\.\.\."', EXAMPLE_LINE)  # the example shows this shape


def test_more_matches_than_shown_say_how_to_read_on(store):
    [pid] = store.page_out([[{"role": "user", "content": LOG.replace("shard=3 ok", "shard=3 ok NEEDLE")}]], 1)
    reply = search(store, "NEEDLE")
    more = LOG.count("shard=3 ok") - MATCHES_PER_PAGE
    assert reply.endswith(f"  [{more} more matching lines in this page; call contexto_recall with page_id {pid} "
                          "and an offset to read further]")


def test_a_search_reply_is_charged_exactly(store):
    store.set_window(200_000, 2.0)
    store.page_out([[{"role": "user", "content": LOG}]], 1)
    store.set_turn_room("1", 5_000)
    reply = search(store, "line 0123")
    assert store.turn_room()[2] == math.ceil(len(reply) / 1.5)


def test_a_search_reply_is_held_to_the_per_call_limit():
    store = MemoryStore("s1")
    store.set_window(4_000, 1.0)  # a 1,000-character per-call limit
    store.page_out([[{"role": "user", "content": f"needle {i}\n" + LOG[:400]}] for i in range(30)], 1)
    reply = search(store, "needle")
    assert len(reply) <= 1_000
    assert reply.endswith("more matching pages not shown; use a longer or more specific query]")


# The live H1 rerun (7da8f78): the four paged-only facts within a 23,000-token room


def acceptance_pages():
    """The acceptance run's pages: the committed script reproduces the run's pages byte for byte."""
    spikes = Path(__file__).resolve().parents[1] / "spikes"
    for path in (spikes / "m2", spikes / "m0"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from acceptance import run

    script, _ = run.plan()
    turns = [[{"role": "user", "content": [{"type": "text", "text": text}]},
              {"role": "assistant", "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "OK"}]}]
             for text in script.turns[:-1]]
    return script, turns


def test_the_paged_only_facts_are_answerable_from_search_hits_within_the_live_room(tmp_path):
    script, turns = acceptance_pages()
    store = SessionStore(tmp_path, "acc")
    ids = store.page_out(turns, 0)
    store.set_window(81_737, 2.123986182634373)  # the live run's recorded window and ratio
    first = render_page(store.get_page(ids[0]))
    assert len(first) == 39_405  # the live run's turn-0 page
    assert math.ceil(len(first) / 1.5) > 23_000  # a whole-page read alone overflows the room
    store.set_turn_room("19", 23_000)
    paged_only = [p for p in script.probes if p.id in script.paged_only]
    assert len(paged_only) == 4
    for probe in paged_only:
        [key] = probe.subjects
        reply = search(store, key)
        assert reply != SPENT
        [hit] = [line for line in reply.split("\n") if line.startswith(">")]
        assert hit.endswith(f"cfg {key}={probe.answer}")  # answerable straight from the hit
        offset = int(re.match(r"> (\d+): ", hit).group(1))
        assert first[offset:].startswith(f"cfg {key}={probe.answer}")
        assert credited(store, ids[0], first, f"cfg {key}={probe.answer}")  # the log shows it was retrieved
    assert store.turn_room()[2] <= 23_000 // 10  # all four searches together use a small part of the room


# The content-free log of what was returned (contract PR #95; field names agreed with builder-4)


def test_search_logs_the_spans_it_returned_and_nothing_else(store):
    text = LOG.replace("s0 line 0100 level=INFO", "s0 line 0100 level=INFO cache.ttl_seconds=short")
    [pid] = store.page_out([[{"role": "user", "content": text}]], 3)
    reply = search(store, "cache.ttl_seconds")
    page = render_page(store.get_page(pid))
    [event] = events(store, "contexto_search")
    [span] = event["returned"]  # the match and its context are contiguous: one span
    assert set(span) == {"page_id", "start", "end"} and span["page_id"] == pid
    shown = [line.split(": ", 1)[1] for line in reply.split("\n")[1:]]
    assert page[span["start"]:span["end"]] == "\n".join(shown)  # exactly the returned lines
    assert all(isinstance(v, int) for k, v in span.items() if k != "page_id")
    assert "cache.ttl_seconds" not in json.dumps(event)  # ids and integers only, no content


def test_separate_matches_log_separate_spans_and_a_cut_line_only_its_part(store):
    text = "\n".join(["intro"] + ["filler"] * 20 + ["NEEDLE one"] + ["filler"] * 20 + ["x" * 900 + " NEEDLE"])
    [pid] = store.page_out([[{"role": "user", "content": text}]], 1)
    search(store, "NEEDLE")
    [event] = events(store, "contexto_search")
    page = render_page(store.get_page(pid))
    first, before_long, long_part = event["returned"]
    assert page[first["start"]:first["end"]] == "filler\nfiller\nNEEDLE one\nfiller\nfiller"
    assert page[before_long["start"]:before_long["end"]] == "filler\nfiller"
    # The long line is shown from just before its match, so its span starts mid-line: a span of its own.
    assert page[long_part["start"]:long_part["end"]].endswith("x NEEDLE")
    assert long_part["end"] - long_part["start"] <= MATCH_LINE_CHARS


def test_recall_logs_its_span_and_a_spent_reply_logs_none(store):
    store.set_window(200_000, 2.0)
    [pid] = store.page_out([[{"role": "user", "content": LOG}]], 1)
    store.set_turn_room("1", 500)  # half of it: 375 characters
    reply = recall(store, pid, 1_000)
    [event] = events(store, "contexto_recall")
    [span] = event["returned"]
    page = render_page(store.get_page(pid))
    assert span == {"page_id": pid, "start": 1_000, "end": 1_000 + event["chars"]}
    assert reply.startswith(page[span["start"]:span["end"]])
    store.set_turn_room("1", 0)
    assert recall(store, pid, 2_000) == SPENT
    assert events(store, "contexto_recall")[-1]["returned"] == []


def test_only_hits_actually_returned_are_logged(store):
    store.set_window(200_000, 2.0)
    for i in range(30):
        store.page_out([[{"role": "user", "content": f"needle {i}\n" + LOG[:300]}]], i + 1)
    store.set_turn_room("1", 400)  # room for a few hits only
    reply = search(store, "needle")
    [event] = events(store, "contexto_search")
    shown = [line.split(" ", 1)[0] for line in reply.split("\n") if line.startswith("pg_")]
    assert 0 < len(shown) < 30 and [span["page_id"] for span in event["returned"]] == shown
    store.set_turn_room("1", 0)
    assert search(store, "needle") == SPENT and events(store, "contexto_search")[-1]["returned"] == []


# Reviewer-1's PR #97 findings, fixed in B16


def test_a_long_query_stays_whole_in_its_matching_line():
    query = "cfg." + "segment_" * 40 + "end"  # over 300 characters
    text = "head\n" + "x" * 3_000 + query + "=value" + "y" * 3_000 + "\ntail"
    shown, _ = match_lines(text, query)
    [(start, end, is_match, line)] = [row for row in shown if row[2]]
    assert query in line and query in text[start:end]
    assert end - start >= len(query)
    short = match_lines("x" * 3_000 + "NEEDLE" + "y" * 3_000, "NEEDLE")[0][0]
    assert short[1] - short[0] == MATCH_LINE_CHARS  # a short query keeps the usual cut


def test_a_hit_over_the_per_call_cap_returns_its_matching_lines_not_the_budget_message():
    store = MemoryStore("s1")
    store.set_window(2_000, 1.0)  # a 500-character per-call limit, and no room published
    filler = "\n".join(f"context line {i} " + "z" * 120 for i in range(10))
    [pid] = store.page_out([[{"role": "user", "content": filler + "\ncfg deploy.target=blue\n" + filler}]], 1)
    reply = search(store, "deploy.target")
    assert reply != SPENT and len(reply) <= 500
    lines = reply.split("\n")
    assert lines[0].startswith(f"{pid} (turn 1): ")
    assert any(line.startswith("> ") and line.endswith("cfg deploy.target=blue") for line in lines)
    assert lines[-1] == "  [the rest of this hit is not shown; use a longer or more specific query]"
    [event] = events(store, "contexto_search")
    page = render_page(store.get_page(pid))
    [span] = event["returned"]
    assert page[span["start"]:span["end"]] == "cfg deploy.target=blue"  # only the line actually shown


def test_a_cap_too_small_for_even_the_page_line_says_so():
    store = MemoryStore("s1")
    store.set_window(400, 1.0)  # a 100-character per-call limit
    store.page_out([[{"role": "user", "content": "a stub line long enough " * 10 + "\ncfg deploy.target=blue"}]], 1)
    assert search(store, "deploy.target") == mcp.SEARCH_TOO_LONG
    assert events(store, "contexto_search")[-1]["returned"] == []


def test_a_room_too_small_for_a_hit_still_gets_the_budget_message(store):
    store.set_window(200_000, 2.0)
    store.page_out([[{"role": "user", "content": LOG}]], 1)
    store.set_turn_room("1", 20)  # 30 characters: the room binds, not the per-call limit
    assert search(store, "line 0123") == SPENT
