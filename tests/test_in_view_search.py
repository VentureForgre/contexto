"""contexto_search also matches the conversation in view (M2 B19 b, contract PR #125)."""

import io
import json
import math

import pytest

from contexto import mcp
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore

LOG = "\n".join(f"s0 line {i:04d} level=INFO shard={i % 7} ok" for i in range(200))
RATIO = 1.5  # set_window(200_000, 2.0)


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    s = SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")
    s.set_window(200_000, 2.0)
    return s


def snapshot(store, entries, room=10_000, question="what changed?"):
    """One request as lane A drives it (A14): the snapshot, then the request-time room publish.

    The snapshot ends with the current turn's own question (a user entry in the newest turn), which
    in-view search skips (B22); pass question=None to send the entries as they are.
    """
    if question is not None:
        newest = max((e["turn"] for e in entries), default=0) + 1
        entries = [*entries, entry(newest, 0, question)]
    store.set_retained(3, 12, entries)
    store.set_turn_room("7", room)


def entry(turn, index, text, role="user"):
    return {"turn": turn, "message_index": index, "role": role, "text": text}


def search(store, query):
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_search", "arguments": {"query": query}}})
    result = server.handle_line(line)["result"]
    assert result["isError"] is False and "structuredContent" not in result
    body, coverage = (block["text"] for block in result["content"])
    return body, coverage


def last_event(store):
    log = store.events if isinstance(store, MemoryStore) else [
        json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
    return [e for e in log if e.get("event") == "mcp_call" and e.get("tool") == "contexto_search"][-1]


def test_in_view_lines_come_first_newest_first_and_labelled(store):
    store.page_out([[{"role": "user", "content": LOG.replace("line 0050 level=INFO", "line 0050 cohort=a")}]], 1)
    snapshot(store, [entry(16, 0, "intro\nthe cohort=b now\nmore"), entry(17, 2, "cohort=c latest", "tool_result")])
    body, coverage = search(store, "cohort=")
    lines = body.split("\n")
    assert lines[:3] == ["[in view, newest first:]", "> in view (turn 17): cohort=c latest",
                         "> in view (turn 16): the cohort=b now"]  # the matching line only, no context, no offset
    assert lines[3].startswith("pg_")  # then the paged hits
    assert coverage == mcp.SEARCH_COVERAGE  # in view was checked
    event = last_event(store)
    assert event["in_view_checked"] is True and event["in_view_hits"] == 2 and event["in_view_more"] == 0


def test_in_view_lines_are_charged_with_the_reply(store):
    snapshot(store, [entry(16, 0, "cfg cache.ttl=short")])
    body, _ = search(store, "cache.ttl")
    assert body == "[in view, newest first:]\n> in view (turn 16): cfg cache.ttl=short"  # in view only: no paged hit
    assert store.turn_room()[2] == math.ceil(len(body) / RATIO)


def test_in_view_spans_are_logged_content_free_with_their_own_source(store):
    store.page_out([[{"role": "user", "content": "paged cfg cache.ttl=long"}]], 1)
    text = "noise\ncfg cache.ttl=short"
    snapshot(store, [entry(16, 1, text)])
    search(store, "cache.ttl")
    event = last_event(store)
    in_view = [s for s in event["returned"] if s["source"] == "in_view"]
    paged = [s for s in event["returned"] if s["source"] == "paged"]
    assert in_view == [{"source": "in_view", "turn": 16, "message_index": 1, "start": 6, "end": len(text)}]
    assert text[6:] == "cfg cache.ttl=short" and all("page_id" not in s for s in in_view)
    assert len(paged) == 1 and "page_id" in paged[0]
    assert "cache.ttl" not in json.dumps(event)  # ids and integers only


@pytest.mark.parametrize("case", ["missing", "stale write", "unstamped room"])
def test_in_view_is_not_checked_without_a_current_snapshot(store, case, monkeypatch):
    store.page_out([[{"role": "user", "content": "paged cfg cache.ttl=long"}]], 1)
    if case == "missing":
        store.set_turn_room("7", 10_000)
    elif case == "stale write":
        snapshot(store, [entry(16, 0, "cfg cache.ttl=short")])
        if isinstance(store, SessionStore):
            monkeypatch.setattr(store, "_write_atomic", lambda path, data: (_ for _ in ()).throw(OSError("full")))
            with pytest.raises(OSError):
                store.set_retained(3, 12, [entry(17, 0, "cfg cache.ttl=newer")])
        else:
            store._retained = (3, 12, 1, [entry(16, 0, "cfg cache.ttl=short")])
            store._request_seq, store._retained_stamp = 2, 2
        store.set_turn_room("7", 10_000)  # the room carries the new stamp, the snapshot the old one
    else:
        snapshot(store, [entry(16, 0, "cfg cache.ttl=short")])
        other = SessionStore(store.dir.parent.parent, store.session_id) if isinstance(store, SessionStore) else store
        if other is store:
            store._retained_stamp = None
        other.set_turn_room("7", 10_000)  # a publish from a store that wrote no snapshot: unstamped
    body, coverage = search(store, "cache.ttl")
    assert "in view" not in body.split("\n")[0] and body.startswith("pg_")  # paged hits still work
    assert coverage == mcp.SEARCH_COVERAGE_UNCHECKED
    assert last_event(store)["in_view_checked"] is False


def test_no_match_anywhere_is_still_the_no_hit_reply(store):
    snapshot(store, [entry(16, 0, "nothing relevant")])
    body, coverage = search(store, "absent-key")
    assert body == mcp.NO_HIT and coverage == mcp.SEARCH_COVERAGE


def test_more_in_view_matches_than_shown_are_counted(store):
    snapshot(store, [entry(10 + i, 0, f"NEEDLE {i}") for i in range(5)])
    body, _ = search(store, "NEEDLE")
    lines = body.split("\n")
    assert lines[1:4] == ["> in view (turn 14): NEEDLE 4", "> in view (turn 13): NEEDLE 3",
                          "> in view (turn 12): NEEDLE 2"]
    assert lines[4] == f"  [2 more matching lines in view]" and mcp.IN_VIEW_LIMIT == 3


def test_a_tool_input_in_view_matches_through_its_escaped_form(store):
    snapshot(store, [entry(16, 1, 'tool_use run {"note": "say \\"hi\\" twice"}', "assistant")])
    body, _ = search(store, 'say "hi" twice')
    assert body.split("\n")[1].startswith("> in view (turn 16): tool_use run")



# Reviewer-1 on PR #131: the newest matches win, within one message and across messages


def test_the_newest_in_view_matches_win_within_one_message(store):
    text = "\n".join(f"deploy.target=v{i}" for i in range(1, 7))  # v1 oldest ... v6 newest, one message
    snapshot(store, [entry(16, 0, text)])
    body, _ = search(store, "deploy.target")
    lines = body.split("\n")
    assert lines[1:4] == ["> in view (turn 16): deploy.target=v6", "> in view (turn 16): deploy.target=v5",
                          "> in view (turn 16): deploy.target=v4"]
    assert lines[4] == "  [3 more matching lines in view]"
    event = last_event(store)
    assert event["in_view_hits"] == 3 and event["in_view_more"] == 3  # shown hits only; the rest counted apart
    assert [s["start"] for s in event["returned"] if s["source"] == "in_view"] == [
        text.index("deploy.target=v6"), text.index("deploy.target=v5"), text.index("deploy.target=v4")]


def test_the_newest_in_view_matches_win_across_messages(store):
    snapshot(store, [entry(16, 0, "deploy.target=a1\ndeploy.target=a2\ndeploy.target=a3\ndeploy.target=a4"),
                     entry(17, 1, "deploy.target=b1\ndeploy.target=b2", "tool_result")])
    body, _ = search(store, "deploy.target")
    assert body.split("\n")[1:5] == ["> in view (turn 17): deploy.target=b2", "> in view (turn 17): deploy.target=b1",
                                      "> in view (turn 16): deploy.target=a4", "  [3 more matching lines in view]"]
    assert last_event(store)["in_view_hits"] == 3



# B22 (contract PR #143): the current turn's own question is not an in-view hit


def test_the_current_question_is_not_returned_in_view_but_earlier_turns_and_tool_results_are(store):
    snapshot(store, [entry(16, 0, "earlier: deploy.target=blue"),
                     entry(18, 0, "What is deploy.target now?"),  # the question, in the newest turn
                     entry(18, 1, "tool_result deploy.target=green", "tool_result"),  # a tool result in it
                     entry(17, 0, "the user said deploy.target=red")], question=None)
    body, _ = search(store, "deploy.target")
    lines = body.split("\n")
    assert lines[1:4] == ["> in view (turn 18): tool_result deploy.target=green",
                          "> in view (turn 17): the user said deploy.target=red",
                          "> in view (turn 16): earlier: deploy.target=blue"]
    assert "What is deploy.target now?" not in body
    assert last_event(store)["in_view_hits"] == 3


def test_a_snapshot_holding_only_the_current_question_has_no_in_view_hit(store):
    snapshot(store, [entry(18, 0, "What is deploy.target now?")], question=None)
    body, coverage = search(store, "deploy.target")
    assert body == mcp.NO_HIT and coverage == mcp.SEARCH_COVERAGE
