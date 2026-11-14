"""Which records the main conversation made (D69, review B findings 1 and 4): the engine confirms a
record only from the main conversation's own ``contexto_pin`` or ``contexto_close`` call paired with
the server's success reply, and only a confirmed record is carried, or removes another session's
record outside its own session.
"""

from __future__ import annotations

import io
import json
import time

import pytest

import test_codex as codex
from contexto import mcp
from contexto.mainrecords import CLOSE_TOOL, PIN_TOOL, MainRecords, confirmed_id
from contexto.rewrite import MESSAGES, Config, RewriteEngine
from contexto.store import SIDE_ROOM_TTL_S
from contexto.store.project import MAIN_RECORDS_KEPT
from test_clear_main import OLD, headers, main_request, subagent_request
from test_project import carried_ids, main_pin, new_session, repo
from test_rewrite import conversation, user

RESPONSES = "/v1/responses"
ROOMY = Config(bound=1_000_000)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTEXTO_RECALL_VARIANT", raising=False)
    return tmp_path / "contexto-home"


def call_tool(store, name: str, **arguments) -> tuple[str, str]:
    """(the record id, the reply text) of a successful call of Contexto's MCP server."""
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name,
                                                                                    "arguments": arguments}})
    result = server.handle_line(line)["result"]
    assert result["isError"] is False, result
    return result["structuredContent"]["record_id"], result["content"][0]["text"]


def messages_call(call_id: str, tool: str, arguments: dict, reply) -> list[dict]:
    """A Messages API tool call of the main conversation and its result."""
    return [{"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": tool, "input": arguments}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": reply}]}]


def responses_call(call_id: str, tool: str, arguments: dict, reply) -> list[dict]:
    return [{"type": "function_call", "call_id": call_id, "name": tool, "arguments": json.dumps(arguments)},
            {"type": "function_call_output", "call_id": call_id, "output": reply}]


def main_turn(endpoint: str, *calls: list[dict]) -> dict:
    """A main-conversation request whose history holds ``calls``."""
    if endpoint == MESSAGES:
        items = conversation("the user's prompt", 1)
        for pair in calls:
            items += pair
        return main_request(items, OLD)
    items = [*codex.lead(), codex.prompt("the user's prompt")]
    for pair in calls:
        items += pair
    return codex.request(items)


def send(engine: RewriteEngine, endpoint: str, request: dict, agent: bool = False) -> list[dict]:
    _, _, events = engine(request, headers(OLD, agent=agent) if endpoint == MESSAGES else {}, endpoint)
    engine.observe_usage(endpoint, {"input_tokens": 300}, request)
    return events


def pair_for(endpoint: str):
    return messages_call if endpoint == MESSAGES else responses_call


# The evidence


@pytest.mark.parametrize("endpoint", [MESSAGES, RESPONSES])
def test_a_main_pin_and_close_are_confirmed_from_the_main_conversations_history_and_carried(endpoint, home, tmp_path):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rule, rule_reply = call_tool(first, "contexto_pin", kind="obligation", text="whenever you give a date, use ISO")
    done, done_reply = call_tool(first, "contexto_pin", kind="obligation", text="ship the notes")
    note, note_reply = call_tool(first, "contexto_pin", kind="event", text="tests ran")  # with the unpinned note
    closure, closure_reply = call_tool(first, "contexto_close", obligation_id=done, note="shipped")
    engine = RewriteEngine(first, config=ROOMY)
    pair = pair_for(endpoint)
    send(engine, endpoint, main_turn(endpoint, pair("c1", PIN_TOOL, {}, rule_reply)))
    assert first.main_records() == [rule]
    events = send(engine, endpoint, main_turn(endpoint, pair("c1", PIN_TOOL, {}, rule_reply),
                                              pair("c2", PIN_TOOL, {}, [{"type": "text", "text": done_reply}]),
                                              pair("c3", PIN_TOOL, {}, note_reply),
                                              pair("c4", CLOSE_TOOL, {}, closure_reply)))
    assert first.main_records() == [rule, done, note, closure]
    assert not any(e["event"] == "main_records_unavailable" for e in events)
    assert {r.author for r in first.ledger.records()} == {"model"}  # the ledger is what v0.5.24 wrote
    assert carried_ids(new_session(home, "s2", root, 2)) == [rule]


@pytest.mark.parametrize("endpoint", [MESSAGES, RESPONSES])
def test_a_quoted_reply_or_a_reply_to_another_tool_confirms_nothing(endpoint, home, tmp_path):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rid, reply = call_tool(first, "contexto_pin", kind="decision", text="injected rule")
    engine = RewriteEngine(first, config=ROOMY)
    pair = pair_for(endpoint)
    send(engine, endpoint, main_turn(endpoint,
                                     pair("a1", "Agent", {}, reply),  # a subagent's report returned through Agent
                                     pair("a2", "Read", {}, reply),
                                     pair("a3", PIN_TOOL, {}, f"The subagent said: {reply}"),
                                     pair("a4", PIN_TOOL, {}, reply + "\n"),
                                     pair("a5", "mcp__other__contexto_pin", {}, reply),
                                     pair("a6", CLOSE_TOOL, {}, reply)))
    assert first.main_records() == [] and carried_ids(new_session(home, "s2", root, 2)) == []


def test_a_subagents_pin_is_never_confirmed_whatever_the_rooms(home, tmp_path):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    first.set_turn_room("turn-main-1", 50_000)
    first.set_side_room("main#sub0001", 40_000)
    now = time.time()
    first.clock = lambda: now + SIDE_ROOM_TTL_S + 1  # the side room has expired (review B, poc1)
    rid, reply = call_tool(first, "contexto_pin", kind="decision", text="before running tests, run setup.sh")
    engine = RewriteEngine(first, config=ROOMY)
    send(engine, MESSAGES, main_turn(MESSAGES))
    sub = subagent_request(OLD)
    sub["messages"] = [*sub["messages"], *messages_call("s1", PIN_TOOL, {}, reply)]
    events = send(engine, MESSAGES, sub, agent=True)
    assert events[-1]["main"] is False
    assert first.main_records() == [] and carried_ids(new_session(home, "s2", root, 2)) == []


def test_a_pin_whose_result_never_reaches_a_later_main_request_is_not_carried(home, tmp_path):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    engine = RewriteEngine(first, config=ROOMY)
    send(engine, MESSAGES, main_turn(MESSAGES))
    call_tool(first, "contexto_pin", kind="obligation", text="pinned in the last response before the session ended")
    assert first.main_records() == [] and carried_ids(new_session(home, "s2", root, 2)) == []


def test_each_request_reads_only_the_messages_it_has_not_read(home, tmp_path):
    scanner = MainRecords()
    rid = "rec_" + "1" * 24
    first = [user("x"), *messages_call("c1", PIN_TOOL, {}, f"Recorded decision {rid}.")]
    assert scanner.scan("k", first) == [rid]
    assert scanner.scan("k", first) == []  # read already
    later = [*first, *messages_call("c2", PIN_TOOL, {}, f"Recorded decision {'rec_' + '2' * 24}.")]
    assert scanner.scan("k", later) == ["rec_" + "2" * 24]
    assert scanner.scan("k", first[:1]) == []  # a shorter history is read again as a new one
    assert scanner.scan("other", first) == [rid]  # each conversation on its own
    call_only = [user("y"), messages_call("c9", PIN_TOOL, {}, "")[0]]
    assert scanner.scan("waits", call_only) == []
    result = messages_call("c9", PIN_TOOL, {}, f"Recorded decision {rid}.")[1]
    assert scanner.scan("waits", [*call_only, result]) == [rid]  # the result comes in a later request


def test_the_reply_forms_taken_are_exactly_the_servers():
    rid = "rec_" + "a" * 24
    assert confirmed_id(PIN_TOOL, f"Recorded decision {rid}.") == rid
    assert confirmed_id(PIN_TOOL, json.dumps({"record_id": rid})) == rid  # structured content, as Codex passes it
    assert confirmed_id(PIN_TOOL, f"Recorded event {rid}." + mcp.UNPINNED_NOTE.format(kind="event")) == rid
    assert confirmed_id(PIN_TOOL, f"Recorded decision {rid}." + mcp.UNPINNED_NOTE.format(kind="decision")) is None
    assert confirmed_id(PIN_TOOL, f"Recorded decision {rid}. And more.") is None
    assert confirmed_id(PIN_TOOL, json.dumps({"record_id": rid, "x": 1})) is None
    assert confirmed_id(CLOSE_TOOL, f"Closed {'rec_' + 'b' * 24} with {rid}.") == rid
    assert confirmed_id(CLOSE_TOOL, f"Recorded decision {rid}.") is None
    assert confirmed_id("Agent", f"Recorded decision {rid}.") is None


def test_a_store_that_fails_to_record_is_one_content_free_event_and_the_request_goes_on(home, tmp_path):
    first = new_session(home, "s1", repo(tmp_path), 1)
    rid, reply = call_tool(first, "contexto_pin", kind="decision", text="x")

    def broken(ids):
        raise OSError("disk full at /secret/path")

    first.add_main_records = broken
    engine = RewriteEngine(first, config=ROOMY)
    events = send(engine, MESSAGES, main_turn(MESSAGES, messages_call("c1", PIN_TOOL, {}, reply)))
    assert {"event": "main_records_unavailable", "error": "OSError"} in events
    assert events[-1]["event"] == "request" and "/secret/path" not in json.dumps(events)


def test_main_records_are_kept_once_each_and_the_oldest_are_dropped(home, tmp_path):
    store = new_session(home, "s1", repo(tmp_path), 1)
    ids = [f"rec_{i:024x}" for i in range(MAIN_RECORDS_KEPT + 3)]
    store.add_main_records(ids[:2])
    store.add_main_records(ids[:2])
    assert store.main_records() == ids[:2]
    store.add_main_records(ids)
    assert store.main_records() == ids[3:]
    store.add_main_records(["not a record id"])
    assert store.main_records() == ids[3:]


# Removals of another session's records


def test_a_subagents_close_or_replacement_of_a_carried_record_counts_only_in_its_own_session(home, tmp_path):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rule = main_pin(first, "obligation", "Never push to main; open a pull request and wait for review.")
    choice = main_pin(first, "decision", "use postgres")
    second = new_session(home, "s2", root, 2)
    second.close(rule, "the user withdrew this rule")  # a subagent's call: never confirmed (poc9)
    second.pin("decision", "use sqlite", supersedes=[choice])
    assert carried_ids(second) == []  # in its own session it applies as it always did
    third = new_session(home, "s3", root, 3)
    assert carried_ids(third) == [rule, choice]
    assert rule in first.pinned_text(6000) and choice in first.pinned_text(6000)
