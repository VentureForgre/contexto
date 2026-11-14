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
from contexto.rewrite import ANTHROPIC, MESSAGES, Config, RewriteEngine, keyed
from contexto.store import SIDE_ROOM_TTL_S, TRUSTED_CONVERSATIONS_KEPT
from contexto.store.project import MAIN_RECORDS_KEPT
from test_clear_main import NEW, OLD, SUBAGENT_TOOLS, headers, main_request, metadata, side_request, subagent_request
from test_project import carried_ids, main_pin, new_session, repo
from test_rewrite import assistant, conversation, request, user

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


def send(engine: RewriteEngine, endpoint: str, request: dict, agent: bool = False, session: str = OLD) -> list[dict]:
    _, _, events = engine(request, headers(session, agent=agent) if endpoint == MESSAGES else {}, endpoint)
    engine.observe_usage(endpoint, {"input_tokens": 300}, request)
    return events


def pair_for(endpoint: str):
    return messages_call if endpoint == MESSAGES else responses_call


def first_launch(store, session: str = OLD) -> RewriteEngine:
    """A first launch's engine on ``store``, built before the launcher records the launch's Claude Code
    session id (``SessionRecorder`` records it at the first request), so the engine sees a store that has
    served no session: a new store (review B round 8, N21)."""
    engine = RewriteEngine(store, config=ROOMY)
    store.record_cli_session("claude", session)
    return engine


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


@pytest.mark.parametrize("endpoint", [MESSAGES, RESPONSES])
def test_a_write_that_fails_once_is_tried_again_at_the_next_request(endpoint, home, tmp_path):
    """Review A round 2, finding 5: the ids found are not dropped with the failed write."""
    first = new_session(home, "s1", repo(tmp_path), 1)
    rid, reply = call_tool(first, "contexto_pin", kind="decision", text="x")
    real, failures = first.add_main_records, [OSError("disk full")]

    def once(ids):
        if failures:
            raise failures.pop()
        return real(ids)

    first.add_main_records = once
    engine = RewriteEngine(first, config=ROOMY)
    pair = pair_for(endpoint)
    events = send(engine, endpoint, main_turn(endpoint, pair("c1", PIN_TOOL, {}, reply)))
    assert {"event": "main_records_unavailable", "error": "OSError"} in events and first.main_records() == []
    other, other_reply = call_tool(first, "contexto_pin", kind="decision", text="y")
    events = send(engine, endpoint, main_turn(endpoint, pair("c1", PIN_TOOL, {}, reply),
                                              pair("c2", PIN_TOOL, {}, other_reply)))
    assert "main_records_unavailable" not in names_of(events) and first.main_records() == [rid, other]


def names_of(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


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


def test_trusted_conversations_are_kept_once_each_newest_last_and_the_oldest_are_dropped(home, tmp_path):
    """Review A round 7, finding 5 (r6, r7): the list's bound and its dedupe. A key added again moves to the
    end; empty and non-string keys are ignored; a field that is not a list of strings reads as empty."""
    store = new_session(home, "s1", repo(tmp_path), 1)
    keys = [f"anthropic:{i:064x}" for i in range(TRUSTED_CONVERSATIONS_KEPT + 6)]
    for key in keys:
        store.add_trusted_conversation(key)
    assert store.trusted_conversations() == keys[6:]
    store.add_trusted_conversation(keys[10])
    assert store.trusted_conversations() == [*keys[6:10], *keys[11:], keys[10]]
    store.add_trusted_conversation(keys[10])
    for junk in ("", None, 7):
        store.add_trusted_conversation(junk)
    assert store.trusted_conversations() == [*keys[6:10], *keys[11:], keys[10]]
    meta = json.loads((store.dir / "meta.json").read_text())
    (store.dir / "meta.json").write_text(json.dumps({**meta, "trusted_conversations": {"not": "a list"}}))
    assert store.trusted_conversations() == []
    (store.dir / "meta.json").write_text(json.dumps({**meta, "trusted_conversations": [keys[0], 3, None]}))
    assert store.trusted_conversations() == [keys[0]]
    store.add_trusted_conversation(keys[1])
    assert store.trusted_conversations() == keys[:2]


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


# Which conversation's history is trusted (review B round 2, N1)


def pin_pair(call_id: str, reply: str) -> list[dict]:
    return messages_call(call_id, PIN_TOOL, {"kind": "obligation", "text": "x"}, reply)


def reasons(events: list[dict]) -> list[str]:
    return [e["reason"] for e in events if e.get("event") == "main_changed"]


@pytest.mark.parametrize("agent", [True, False])
def test_a_subagent_that_takes_main_by_turn_starts_gets_no_pin_confirmed(agent, home, tmp_path):
    """Review B's pocA_takeover: a subagent resumed by SendMessage has more turn starts than main.
    With X-Claude-Code-Agent-Id it never takes main; without it, it takes main by the turn-start rule,
    and its pins are still not confirmed. The user's conversation, main since its first request, stays
    trusted when it takes main back."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    engine = RewriteEngine(first, config=ROOMY)
    send(engine, MESSAGES, main_request([user("the user's first prompt", context=True)], OLD))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="the subagent's pin")
    sub_items = [user("SUB-TASK read the README"), *pin_pair("toolu_sub1", reply), assistant("done"),
                 user("SendMessage: continue"), assistant("ok"), user("SendMessage: again")]
    events = send(engine, MESSAGES, request(sub_items, tools=SUBAGENT_TOOLS, metadata=metadata(OLD)), agent=agent)
    assert reasons(events) == ([] if agent else ["more_turn_starts"])
    later = [*sub_items, assistant("ok"), *pin_pair("toolu_sub2", reply), user("SendMessage: once more")]
    send(engine, MESSAGES, request(later, tools=SUBAGENT_TOOLS, metadata=metadata(OLD)), agent=agent)
    assert first.main_records() == []
    own, own_reply = call_tool(first, "contexto_pin", kind="obligation", text="the user's rule")
    lead = [user("the user's first prompt", context=True), *pin_pair("toolu_main1", own_reply)]
    for turn in range(4):
        lead += [assistant(f"a{turn}"), user(f"prompt {turn}")]
    events = send(engine, MESSAGES, main_request(lead, OLD))
    assert events[-1]["main"] is True and reasons(events) == ([] if agent else ["more_turn_starts"])
    assert first.main_records() == [own]
    assert carried_ids(new_session(home, "s2", root, 2)) == [own]


def test_a_codex_child_conversation_that_takes_main_by_turn_starts_gets_no_pin_confirmed(home, tmp_path):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    engine = RewriteEngine(first, config=ROOMY)
    send(engine, RESPONSES, codex.request([*codex.lead(), codex.prompt("the user's first prompt")]))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="the child's pin")
    child = [*codex.lead(), codex.prompt("child task: read the README"),
             *responses_call("call_c1", PIN_TOOL, {"kind": "obligation", "text": "x"}, reply),
             codex.answer("done"), codex.prompt("follow-up input to the child")]
    events = send(engine, RESPONSES, codex.request(child))
    assert reasons(events) == ["more_turn_starts"] and events[-1]["main"] is True
    child += [codex.answer("ok"), *responses_call("call_c2", CLOSE_TOOL, {}, reply), codex.prompt("more")]
    send(engine, RESPONSES, codex.request(child))
    assert first.main_records() == [] and carried_ids(new_session(home, "s2", root, 2)) == []


def test_a_conversation_that_takes_main_by_a_session_rule_is_trusted(home, tmp_path):
    """/clear (new_cli_session) and a first prompt after a side request (after_side_request)."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    engine = RewriteEngine(first, config=ROOMY)
    send(engine, MESSAGES, side_request("synthetic side request", OLD))
    rule, rule_reply = call_tool(first, "contexto_pin", kind="obligation", text="the first rule")
    events = send(engine, MESSAGES, main_request([user("first prompt", context=True), *pin_pair("t1", rule_reply),
                                                  user("next prompt")], OLD))
    assert reasons(events) == ["after_side_request"]
    send(engine, MESSAGES, main_request(conversation("old conversation", 5), OLD))
    after, after_reply = call_tool(first, "contexto_pin", kind="decision", text="after clear")
    events = send(engine, MESSAGES, main_request([user("after clear", context=True), *pin_pair("t2", after_reply),
                                                  user("next")], NEW), session=NEW)
    assert reasons(events) == ["new_cli_session"]
    assert first.main_records() == [rule, after]


# A reply that does not match (review A round 2, findings 6 and 9)


def unmatched(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event"] == "main_record_unmatched"]


def test_an_error_result_holding_the_success_text_confirms_nothing_and_is_logged(home, tmp_path):
    first = new_session(home, "s1", repo(tmp_path), 1)
    rid, reply = call_tool(first, "contexto_pin", kind="decision", text="x")
    engine = RewriteEngine(first, config=ROOMY)
    errored = messages_call("e1", PIN_TOOL, {}, reply)
    errored[1]["content"][0]["is_error"] = True
    events = send(engine, MESSAGES, main_turn(MESSAGES, errored))
    assert first.main_records() == []
    assert unmatched(events) == [{"event": "main_record_unmatched", "tool": "contexto_pin", "reason": "is_error"}]


@pytest.mark.parametrize("endpoint", [MESSAGES, RESPONSES])
def test_a_reply_of_another_shape_is_one_content_free_event_per_call(endpoint, home, tmp_path):
    first = new_session(home, "s1", repo(tmp_path), 1)
    rid, reply = call_tool(first, "contexto_pin", kind="decision", text="x")
    done, done_reply = call_tool(first, "contexto_close", obligation_id=call_tool(
        first, "contexto_pin", kind="obligation", text="y")[0], note="z")
    engine = RewriteEngine(first, config=ROOMY)
    pair = pair_for(endpoint)
    calls = [pair("u1", PIN_TOOL, {}, [{"type": "text", "text": reply}, {"type": "text", "text": json.dumps(
                 {"record_id": rid})}]),  # two parts (review A, N4)
             pair("u2", PIN_TOOL, {}, {"record_id": rid}),  # an object, not text
             pair("u3", PIN_TOOL, {}, [{"type": "image", "source": {}}]),
             pair("u4", CLOSE_TOOL, {}, done_reply + " And more."),
             pair("u5", PIN_TOOL, {}, json.dumps({"record_id": rid, "session": "s1"}))]  # an unknown structured form
    events = send(engine, endpoint, main_turn(endpoint, *calls))
    assert first.main_records() == []
    expected = (("contexto_pin", "parts"), ("contexto_pin", "shape"), ("contexto_pin", "part_type"),
                ("contexto_close", "reply"), ("contexto_pin", "reply"))
    assert unmatched(events) == [{"event": "main_record_unmatched", "tool": t, "reason": r} for t, r in expected]
    assert rid not in json.dumps(events) and done not in json.dumps(events) and "u1" not in json.dumps(events)
    assert unmatched(send(engine, endpoint, main_turn(endpoint, *calls))) == []  # read already
    assert unmatched(send(engine, endpoint, main_turn(endpoint, calls[0]))) == []  # read again: once per call id


@pytest.mark.parametrize("request_class", ["subagent", "workflow", "auxiliary"])
def test_a_first_conversation_classed_as_a_side_request_gets_no_pin_confirmed(request_class, home, tmp_path):
    """D69 with D70: the first conversation an engine sees is main whatever its class, but a request whose
    ``x-claude-code-request-class`` says subagent, workflow or auxiliary is never the main conversation's
    evidence. With the class ``main`` the same history is confirmed (the control)."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rid, reply = call_tool(first, "contexto_pin", kind="decision", text="pinned by a side request")
    engine = RewriteEngine(first, config=ROOMY)
    hinted = {**headers(OLD), "x-claude-code-request-class": request_class}
    engine(main_turn(MESSAGES, pin_pair("c1", reply)), hinted, MESSAGES)
    assert first.main_records() == []
    control = new_session(home, "s2", root, 2)
    rid, reply = call_tool(control, "contexto_pin", kind="decision", text="pinned by main")
    engine = RewriteEngine(control, config=ROOMY)
    engine(main_turn(MESSAGES, pin_pair("c1", reply)), {**headers(OLD), "x-claude-code-request-class": "main"},
           MESSAGES)
    assert control.main_records() == [rid]


@pytest.mark.parametrize("request_class", [None, "main"])
def test_a_fork_of_main_has_main_s_key_but_its_own_pins_are_not_confirmed(request_class, home, tmp_path):
    """Review B round 3, N5: a fork of the main conversation has main's conversation key, so its requests
    are main's for the room (D40), and main is trusted. Its own pin, made in a request that carries the
    subagent's ``X-Claude-Code-Agent-Id``, is still never confirmed: with the hint headers off (no class)
    or with a class of ``main``. Main's own later pin, in a request without the header, is."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    engine = RewriteEngine(first, config=ROOMY)
    main_hint = {"x-claude-code-request-class": "main"} if request_class else {}
    history = [user("the user's first prompt", context=True), assistant("a0"), user("please research X")]
    engine(main_request(history, OLD), {**headers(OLD), **main_hint}, MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 300}, main_request(history, OLD))
    forked, fork_reply = call_tool(first, "contexto_pin", kind="obligation", text="injected standing rule")
    fork = [*history, *messages_call("toolu_agent", "Agent", {"prompt": "read the page"}, "forked"),
            *messages_call("toolu_fork_pin", PIN_TOOL, {"kind": "obligation"}, fork_reply)]
    _, _, events = engine(main_request(fork, OLD), {**headers(OLD, agent=True), **main_hint}, MESSAGES)
    assert events[-1]["main"] is True  # main's for the room, as D40 says
    assert first.main_records() == []
    own, own_reply = call_tool(first, "contexto_pin", kind="decision", text="main's own decision")
    later = [*history, *messages_call("toolu_main_pin", PIN_TOOL, {"kind": "decision"}, own_reply), user("next")]
    engine(main_request(later, OLD), {**headers(OLD), **main_hint}, MESSAGES)
    assert first.main_records() == [own]
    assert carried_ids(new_session(home, "s2", root, 2)) == [own]


@pytest.mark.parametrize("endpoint", [MESSAGES, RESPONSES])
def test_a_conversation_resumed_from_the_epoch_record_is_trusted(endpoint, home, tmp_path):
    """Review A round 3, finding 1: a new engine (a restarted proxy, ``contexto run --resume``) that resumes
    the main conversation from its epoch record makes it main before ``publishes`` runs; it is the engine's
    first main and is trusted, so its pins are confirmed and carried."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rule, reply = call_tool(first, "contexto_pin", kind="obligation", text="whenever you give a date, use ISO")
    pair = pair_for(endpoint)
    send(RewriteEngine(first, config=ROOMY), endpoint, main_turn(endpoint))
    resumed = RewriteEngine(first, config=ROOMY)
    events = send(resumed, endpoint, main_turn(endpoint, pair("c1", PIN_TOOL, {}, reply)))
    assert any(e["event"] == "epoch_resumed" for e in events)
    assert first.main_records() == [rule]
    assert carried_ids(new_session(home, "s2", root, 2)) == [rule]


def test_a_resumed_conversation_the_previous_engine_did_not_trust_stays_untrusted(home, tmp_path):
    """Review B round 4, N9: the epoch record of a main conversation the engine did not trust (one that took
    main only by turn starts) says so, and a new engine that resumes it does not trust it: the pins it
    made while untrusted are not confirmed after a restart."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    engine = RewriteEngine(first, config=ROOMY)
    send(engine, MESSAGES, main_request(conversation("the user's prompt", 1), OLD))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned by the child")
    child = conversation("CHILD TASK do it", 3)
    events = send(engine, MESSAGES, main_request([*child, *pin_pair("c1", reply), user("go on")], OLD))
    assert reasons(events) == ["more_turn_starts"] and first.main_records() == []
    assert first.epoch_record().get("untrusted") is True
    resumed = RewriteEngine(first, config=ROOMY)
    events = send(resumed, MESSAGES, main_request([*child, *pin_pair("c1", reply), user("go on"), assistant("ok"),
                                                   user("more")], OLD))
    assert any(e["event"] == "epoch_resumed" for e in events)
    assert first.main_records() == [] and carried_ids(new_session(home, "s2", root, 2)) == []


def test_a_request_with_the_agent_header_and_no_session_id_is_not_read(home, tmp_path):
    """Review B round 4, N10: the fork guard reads the agent header itself, so a fork of main that sends
    X-Claude-Code-Agent-Id but no session id has its own pin left out."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    engine = RewriteEngine(first, config=ROOMY)
    history = [user("the user's first prompt", context=True), assistant("a0"), user("please research X")]
    engine(request(history), headers(None), MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 300}, request(history))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="injected")
    fork = [*history, *messages_call("toolu_fork_pin", PIN_TOOL, {"kind": "obligation"}, reply)]
    engine(request(fork), headers(None, agent=True), MESSAGES)
    assert first.main_records() == []


def test_a_request_with_a_parent_session_id_is_not_read(home, tmp_path):
    """Review A round 4, nit c: the guard's ``parent_session_id`` half (an agent-team teammate on main's key)."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    engine = RewriteEngine(first, config=ROOMY)
    history = [user("the user's first prompt", context=True), assistant("a0"), user("please research X")]
    engine(main_request(history, OLD), headers(OLD), MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 300}, main_request(history, OLD))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="from a teammate")
    teammate = request([*history, *messages_call("toolu_team_pin", PIN_TOOL, {"kind": "obligation"}, reply)],
                       metadata=metadata(OLD, parent="0" * 32))
    engine(teammate, headers(OLD), MESSAGES)
    assert first.main_records() == []


def _restart_with_first(home, tmp_path, first_messages, first_session, served, config=ROOMY, agent=False):
    """Engine 1 sees the user's conversation (it writes the record); then a new engine on the same store
    gets ``first_messages`` first, in Claude Code session ``first_session``; the store had served
    ``served``. Returns the store, the pinned record's id and the reply the first request's history holds."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = RewriteEngine(first, config=ROOMY)
    for cli_session in served:  # recorded after the engine is built, as the launcher does
        first.record_cli_session("claude", cli_session)
    send(one, MESSAGES, main_request(conversation("the user's prompt", 1), OLD))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned after the restart")
    two = RewriteEngine(first, config=config)
    two(main_request([*first_messages, *pin_pair("c1", reply), user("next")], first_session),
        headers(first_session, agent=agent), MESSAGES)
    return first, rid, root


def test_after_a_restart_a_headerless_subagent_seen_first_is_not_trusted(home, tmp_path):
    """Review B round 5, N13 (pocJ J1): the record names the user's conversation; after a restart a
    subagent without its agent id, in the user's own Claude Code session, reaches the new engine first.
    It is main there, but not trusted, so its pin is not confirmed."""
    first, rid, root = _restart_with_first(home, tmp_path, conversation("SUB-TASK read the page", 2), OLD,
                                           served=[OLD])
    assert first.main_records() == [] and carried_ids(new_session(home, "s2", root, 2)) == []


def test_after_a_restart_an_untrusted_recorded_main_that_does_not_resume_is_not_trusted(home, tmp_path):
    """N13 (pocJ J3): the record names a main the first engine did not trust; the new engine runs with
    other settings, so the record does not resume, and that conversation is still not trusted."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    send(one, MESSAGES, main_request(conversation("the user's prompt", 1), OLD))
    child = conversation("CHILD TASK do it", 3)
    send(one, MESSAGES, main_request([*child, user("go on")], OLD))
    assert first.epoch_record().get("untrusted") is True
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned by the child")
    two = RewriteEngine(first, config=Config(bound=900_000))
    events = send(two, MESSAGES, main_request([*child, user("go on"), *pin_pair("c1", reply), user("more")], OLD))
    assert not any(e["event"] == "epoch_resumed" for e in events)
    assert first.main_records() == []


def test_after_a_restart_a_new_cli_session_seen_first_is_trusted(home, tmp_path):
    """N13's control: a Claude Code session id the store had never served, with no agent id, is a new CLI
    session (``contexto run --session ID claude`` with a new conversation): it is trusted though the record
    names another conversation."""
    first, rid, root = _restart_with_first(home, tmp_path, [user("a new conversation", context=True)], NEW,
                                           served=[OLD])
    assert first.main_records() == [rid]


def test_a_later_launch_continuing_the_users_own_conversation_is_trusted(home, tmp_path):
    """Review A round 6, finding 1: launch 1 records conversation A; launch 2 starts a new conversation B
    (trusted: a new session id); launch 3 continues B, which the epoch record does not name. B was
    trusted before, so it is trusted again and its pins are carried."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    send(one, MESSAGES, main_request(conversation("conversation A", 6), OLD))
    b = conversation("conversation B", 2)
    two = RewriteEngine(first, config=ROOMY)  # built before the launcher records the launch's session id
    first.record_cli_session("claude", NEW)
    send(two, MESSAGES, main_request(b, NEW), session=NEW)
    assert first.epoch_record()["conversation"] != first.trusted_conversations()[-1]  # the record names A
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned when B is continued")
    three = RewriteEngine(first, config=ROOMY)
    send(three, MESSAGES, main_request([*b, *pin_pair("c1", reply), user("next")], NEW), session=NEW)
    assert first.main_records() == [rid]
    assert carried_ids(new_session(home, "s2", root, 2)) == [rid]


@pytest.mark.parametrize("variant", ["agent", "parent"])
def test_after_a_restart_a_request_with_an_agent_id_or_a_parent_seen_first_is_not_trusted(variant, home, tmp_path):
    """Review A round 6, nit 6: the agent-id and parent clauses of the new-session case."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    send(one, MESSAGES, main_request(conversation("the user's prompt", 1), OLD))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned after the restart")
    two = RewriteEngine(first, config=ROOMY)
    messages = [*conversation("SUB-TASK in a new session id", 2), *pin_pair("c1", reply), user("next")]
    if variant == "agent":
        two(main_request(messages, NEW), headers(NEW, agent=True), MESSAGES)
    else:
        two(request(messages, metadata=metadata(NEW, parent=OLD)), headers(NEW), MESSAGES)
    assert first.main_records() == []


def test_a_store_with_a_record_and_no_served_sessions_counts_no_session_as_new(home, tmp_path):
    """Review A round 6, finding 3: with no served session ids recorded (a store from before D55), a first
    request in a session id the record does not name is not trusted as a new session."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    send(RewriteEngine(first, config=ROOMY), MESSAGES, main_request(conversation("the user's prompt", 1), OLD))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned by a headerless subagent")
    two = RewriteEngine(first, config=ROOMY)
    two(main_request([*conversation("SUB-TASK do it", 2), *pin_pair("c1", reply), user("next")], OLD),
        headers(OLD), MESSAGES)
    assert first.main_records() == []


# A takeover after a restart (review B round 7, N17; review A round 7, finding 2)


def key_of(messages: list[dict]) -> str:
    return keyed(ANTHROPIC, {"messages": messages})


def _after_side_request_subagent(home, tmp_path, agent: bool):
    """Engine 1 sees the user's conversation in OLD; after a restart a tool-less side request arrives first,
    then a subagent's first request, whose later request carries a pin. Returns the store, the second
    engine, the subagent's history, the pinned id and the repo."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    users = conversation("the user's prompt", 3)
    send(one, MESSAGES, main_request(users, OLD))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned by the subagent")
    two = RewriteEngine(first, config=ROOMY)
    send(two, MESSAGES, side_request("synthetic title request", OLD))
    sub = [user("SUB-TASK read the page", context=True), *pin_pair("s1", reply), assistant("done"),
           user("SendMessage: continue")]
    events = send(two, MESSAGES, request(sub[:1], tools=SUBAGENT_TOOLS, metadata=metadata(OLD)), agent=agent)
    assert reasons(events) == ([] if agent else ["after_side_request"])
    send(two, MESSAGES, request(sub, tools=SUBAGENT_TOOLS, metadata=metadata(OLD)), agent=agent)
    return first, users, sub, rid, root


@pytest.mark.parametrize("agent", [False, True])
def test_after_a_restart_a_subagent_that_takes_main_after_a_side_request_is_not_trusted(agent, home, tmp_path):
    """Review B round 7, N17 (pocM M5): after a restart a side request is main untrusted, and a subagent
    without its agent id that comes next takes main by ``after_side_request``. That rule trusts it only
    when the first-main rules would (``_first_trusted``), so its pin is not confirmed and it is not listed."""
    first, users, sub, rid, root = _after_side_request_subagent(home, tmp_path, agent)
    assert first.main_records() == [] and carried_ids(new_session(home, "s2", root, 2)) == []
    assert first.trusted_conversations() == [key_of(users)]


def test_a_subagent_that_took_main_after_a_side_request_stays_untrusted_in_later_engines(home, tmp_path):
    """N17 (pocM M6): engine 3 resumes the user's conversation, then the same subagent takes main by turn
    starts and writes an ``untrusted`` record; engine 4 resumes that record and still does not trust it,
    because no engine listed the subagent."""
    first, users, sub, rid, root = _after_side_request_subagent(home, tmp_path, agent=False)
    three = RewriteEngine(first, config=ROOMY)
    send(three, MESSAGES, main_request([*users, assistant("u"), user("u next")], OLD))
    rid3, reply3 = call_tool(first, "contexto_pin", kind="obligation", text="engine 3's pin of the subagent")
    sub3 = [*sub]
    for turn in range(5):
        sub3 += [assistant(f"s{turn}"), user(f"SendMessage {turn}")]
    sub3 += [*pin_pair("s3", reply3), assistant("ok"), user("SendMessage: more")]
    events = send(three, MESSAGES, request(sub3, tools=SUBAGENT_TOOLS, metadata=metadata(OLD)))
    assert reasons(events) == ["more_turn_starts"]
    assert first.epoch_record()["conversation"] == key_of(sub) and first.epoch_record()["untrusted"] is True
    rid4, reply4 = call_tool(first, "contexto_pin", kind="obligation", text="engine 4's pin of the subagent")
    four = RewriteEngine(first, config=ROOMY)
    sub4 = [*sub3, *pin_pair("s4", reply4), assistant("ok4"), user("SendMessage: four")]
    events = send(four, MESSAGES, request(sub4, tools=SUBAGENT_TOOLS, metadata=metadata(OLD)))
    assert any(e["event"] == "epoch_resumed" for e in events)
    assert first.main_records() == [] and key_of(sub) not in first.trusted_conversations()
    assert carried_ids(new_session(home, "s2", root, 2)) == []


@pytest.mark.parametrize("config", [ROOMY, Config(bound=900_000)])
def test_after_a_restart_the_users_conversation_after_a_side_request_is_still_trusted(config, home, tmp_path):
    """N17's control: the conversation the record names (resumed or not), and an older conversation on the
    list that the record does not name (``--resume`` of A after ``/clear`` to B), each after a side
    request, are trusted."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    a = conversation("conversation A", 3)
    send(one, MESSAGES, main_request(a, OLD))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned in A after the restart")
    two = RewriteEngine(first, config=config)
    send(two, MESSAGES, side_request("synthetic title request", OLD))
    send(two, MESSAGES, main_request([*a, assistant("x"), user("A again"), *pin_pair("c1", reply), user("more")],
                                     OLD))
    assert first.main_records() == [rid]
    first.record_cli_session("claude", NEW)
    b = [user("conversation B", context=True)]
    send(one, MESSAGES, main_request(b, NEW), session=NEW)
    assert first.epoch_record()["conversation"] == key_of(b)
    later, later_reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned in A again")
    three = RewriteEngine(first, config=config)
    send(three, MESSAGES, side_request("synthetic title request", OLD))
    events = send(three, MESSAGES, main_request([*a, assistant("x"), user("A again"), *pin_pair("c1", reply),
                                                 user("more"), *pin_pair("c2", later_reply), user("again")], OLD))
    assert reasons(events) == ["after_side_request"]
    assert first.main_records() == [rid, later]


@pytest.mark.parametrize("agent", [True, False])
def test_after_a_restart_a_listed_conversation_that_takes_main_by_turn_starts_is_trusted(agent, home, tmp_path):
    """Review A round 7, finding 2 (round 6 minor 5) and review B round 7, N20 (pocM M7): the user's
    conversation A is listed; after a restart where its record does not resume, a subagent (with its agent
    id or without) reaches the engine first, and A takes main back by turn starts. A is trusted, since an
    earlier engine trusted it, so its pin is confirmed and the record it writes is not ``untrusted``."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    a = conversation("conversation A", 3)
    send(one, MESSAGES, main_request(a, OLD))
    assert first.trusted_conversations() == [key_of(a)]
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned in A")
    two = RewriteEngine(first, config=Config(bound=900_000))
    send(two, MESSAGES, subagent_request(OLD, turns=2), agent=agent)
    a = [*a, assistant("x"), user("go"), *pin_pair("c1", reply), assistant("y"), user("next")]
    events = send(two, MESSAGES, main_request(a, OLD))
    assert reasons(events) == ["more_turn_starts"] and events[-1]["main"] is True
    for turn in range(3):
        a = [*a, assistant(f"z{turn}"), user(f"more {turn}")]
        send(two, MESSAGES, main_request(a, OLD))
    assert first.main_records() == [rid]
    assert first.epoch_record()["conversation"] == key_of(a) and "untrusted" not in first.epoch_record()
    assert carried_ids(new_session(home, "s2", root, 2)) == [rid]


# A restart on a store with no readable record, and the session rules after a restart (review B round 8, N21)

THIRD = "5b0a3c1e-1111-4222-8333-444455556666"


def _damage_record(store, damage: str) -> None:
    path = store.dir / "epoch.json"
    if damage == "unreadable":
        path.write_text("{not json")
    elif damage == "missing":
        path.unlink()
    else:
        data = json.loads(path.read_text())
        path.write_text(json.dumps({**data, "version": 999}))
    assert store.epoch_record() is None


@pytest.mark.parametrize("damage", ["unreadable", "missing", "unknown version"])
def test_a_store_that_served_sessions_and_lost_its_record_is_not_a_new_store(damage, home, tmp_path):
    """Review B round 8, N21a (pocO O1): the store served the user's session, then its epoch record became
    unreadable, missing or of an unknown version. After a restart a side request comes first and a subagent
    without its agent id takes main by ``after_side_request``. No record means a new store only when the
    store never served a session, so the subagent is not trusted, its pin is not confirmed and it is not
    listed."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    users = conversation("the user's prompt", 3)
    send(one, MESSAGES, main_request(users, OLD))
    _damage_record(first, damage)
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned by the subagent")
    two = RewriteEngine(first, config=ROOMY)
    send(two, MESSAGES, side_request("synthetic title request", OLD))
    sub = [user("SUB-TASK read the page", context=True), *pin_pair("s1", reply), assistant("done"),
           user("SendMessage: continue")]
    events = send(two, MESSAGES, request(sub[:1], tools=SUBAGENT_TOOLS, metadata=metadata(OLD)))
    assert reasons(events) == ["after_side_request"]
    send(two, MESSAGES, request(sub, tools=SUBAGENT_TOOLS, metadata=metadata(OLD)))
    assert first.main_records() == [] and key_of(sub) not in first.trusted_conversations()
    assert carried_ids(new_session(home, "s2", root, 2)) == []


@pytest.mark.parametrize("first_is", ["listed", "new session id", "headerless subagent"])
def test_with_no_readable_record_the_other_first_main_rules_still_decide(first_is, home, tmp_path):
    """N21a's control: on a store that served sessions and has no readable record, the first conversation
    is trusted when an earlier engine listed it or when it is a new Claude Code session; a subagent without
    its agent id that reaches the engine first is not."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    users = conversation("the user's prompt", 3)
    send(one, MESSAGES, main_request(users, OLD))
    _damage_record(first, "missing")
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned after the restart")
    two = RewriteEngine(first, config=ROOMY)
    if first_is == "listed":
        send(two, MESSAGES, main_request([*users, *pin_pair("c1", reply), user("next")], OLD))
    elif first_is == "new session id":
        send(two, MESSAGES, main_request([user("a new conversation", context=True), *pin_pair("c1", reply),
                                          user("next")], NEW), session=NEW)
    else:
        send(two, MESSAGES, main_request([*conversation("SUB-TASK read the page", 2), *pin_pair("c1", reply),
                                          user("next")], OLD))
    assert first.main_records() == ([] if first_is == "headerless subagent" else [rid])


def test_a_first_engine_with_no_record_still_trusts_its_first_conversation(home, tmp_path):
    """N21a's control: a store that never served a session and has no record is a new one, so its first
    conversation is trusted whatever it is, as before."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned in a new session")
    one = first_launch(first)
    send(one, MESSAGES, main_request([*conversation("the user's prompt", 1), *pin_pair("c1", reply),
                                      user("next")], OLD))
    assert first.main_records() == [rid]


def _cleared_then_restarted(home, tmp_path):
    """Engine 1: conversation A in OLD, then ``/clear`` to B in NEW, which writes the record. Engine 2 after
    a restart: B goes on first (resumed). Returns the store, engine 2, A, B and the repo."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    a = conversation("the user's prompt A", 3)
    send(one, MESSAGES, main_request(a, OLD))
    first.record_cli_session("claude", NEW)
    b = conversation("the user's prompt B", 1)
    send(one, MESSAGES, main_request(b, NEW), session=NEW)
    assert first.epoch_record()["conversation"] == key_of(b)
    two = RewriteEngine(first, config=ROOMY)
    b = [*b, assistant("b"), user("b next")]
    events = send(two, MESSAGES, main_request(b, NEW), session=NEW)
    assert any(e["event"] == "epoch_resumed" for e in events)
    return first, two, a, b, root


def test_after_a_restart_an_earlier_sessions_subagent_taking_main_as_a_new_session_is_not_trusted(home, tmp_path):
    """Review B round 8, N21b (pocO O3): ``/clear`` from OLD to NEW, a restart, NEW goes on, then a subagent
    of OLD without its agent id reaches a turn start. The new engine has no owner for OLD, so the subagent
    takes main as ``new_cli_session``; that rule trusts it after a restart only when the first-main rules
    would, and OLD is a session id the store had served, so its pin is not confirmed and it is not listed."""
    first, two, a, b, root = _cleared_then_restarted(home, tmp_path)
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned by OLD's subagent")
    sub = [user("OLD SUB-TASK", context=True), *pin_pair("o3", reply), assistant("done"), user("continue")]
    events = send(two, MESSAGES, request(sub, tools=SUBAGENT_TOOLS, metadata=metadata(OLD)))
    assert reasons(events) == ["new_cli_session"] and events[-1]["main"] is True
    assert first.main_records() == [] and key_of(sub) not in first.trusted_conversations()
    assert carried_ids(new_session(home, "s2", root, 2)) == []


def test_after_a_restart_clear_to_a_new_session_and_resume_of_a_listed_one_are_trusted(home, tmp_path):
    """N21b's control: after the restart, ``/clear`` to a session id the store never served is trusted
    (``new_cli_session``), and so is ``/resume`` of the listed conversation A (``resumed_cli_session``
    once A is its session's owner in this engine; here A first retakes main as a new owner of OLD)."""
    first, two, a, b, root = _cleared_then_restarted(home, tmp_path)
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned after /clear")
    first.record_cli_session("claude", THIRD)
    c = [user("conversation C", context=True), *pin_pair("c1", reply), assistant("c"), user("c next")]
    events = send(two, MESSAGES, main_request(c, THIRD), session=THIRD)
    assert reasons(events) == ["new_cli_session"]
    assert first.main_records() == [rid]
    later, later_reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned in A")
    a = [*a, assistant("a"), user("A again"), *pin_pair("c2", later_reply), user("more")]
    events = send(two, MESSAGES, main_request(a, OLD))
    assert reasons(events) == ["new_cli_session"]
    assert first.main_records() == [rid, later]


def test_after_a_restart_an_untrusted_session_owner_resumed_is_still_not_trusted(home, tmp_path):
    """N21b, ``resumed_cli_session``: after a restart a subagent without its agent id reaches the engine
    first (main, untrusted, and the owner of OLD in this engine); ``/clear`` to a new session id takes main;
    the subagent then sends again at a turn start and takes main back as ``resumed_cli_session``. It was
    never trusted, so it is not trusted now and its pin is not confirmed."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    send(one, MESSAGES, main_request(conversation("the user's prompt", 3), OLD))
    two = RewriteEngine(first, config=ROOMY)
    sub = conversation("SUB-TASK read the page", 1)
    send(two, MESSAGES, request(sub, tools=SUBAGENT_TOOLS, metadata=metadata(OLD)))
    first.record_cli_session("claude", THIRD)
    events = send(two, MESSAGES, main_request([user("conversation C", context=True)], THIRD), session=THIRD)
    assert reasons(events) == ["new_cli_session"]
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned by the resumed subagent")
    sub = [*sub, assistant("s"), user("go on"), *pin_pair("s1", reply), assistant("ok"), user("more")]
    events = send(two, MESSAGES, request(sub, tools=SUBAGENT_TOOLS, metadata=metadata(OLD)))
    assert reasons(events) == ["resumed_cli_session"]
    assert first.main_records() == [] and key_of(sub) not in first.trusted_conversations()


# The first-main rules' agent and parent clauses, and the list at a resume (review A round 7, finding 5)


@pytest.mark.parametrize("variant", ["agent", "parent", "agent header twice"])
def test_a_first_request_with_an_agent_id_or_a_parent_in_a_new_session_id_is_not_trusted_later(variant, home,
                                                                                              tmp_path):
    """After a restart, a conversation's first request in a session id the store had not served carries an
    agent id or a parent, so it is main untrusted; its next request, without either, carries a pin, which
    the per-request guard would let through, so only ``_first_trusted``'s clauses keep it out. In "agent
    header twice" the first of two spellings of X-Claude-Code-Agent-Id is empty: the session's ``agent`` is
    False and only the header clause sees the agent id."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    send(one, MESSAGES, main_request(conversation("the user's prompt", 1), OLD))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned after the restart")
    two = RewriteEngine(first, config=ROOMY)
    messages = conversation("SUB-TASK in a new session id", 2)
    if variant == "agent":
        two(main_request(messages, NEW), headers(NEW, agent=True), MESSAGES)
    elif variant == "parent":
        two(request(messages, metadata=metadata(NEW, parent=OLD)), headers(NEW), MESSAGES)
    else:
        twice = {**headers(NEW), "X-Claude-Code-Agent-Id": "", "x-claude-code-agent-id": "a36c784c53af45cc0"}
        two(main_request(messages, NEW), twice, MESSAGES)
    two.observe_usage(MESSAGES, {"input_tokens": 300}, main_request(messages, NEW))
    send(two, MESSAGES, main_request([*messages, *pin_pair("c1", reply), user("next")], NEW), session=NEW)
    assert first.main_records() == [] and key_of(messages) not in first.trusted_conversations()


def test_a_resume_trusts_a_listed_conversation_whose_record_says_untrusted(home, tmp_path):
    """``_resume`` honours the list over the record's ``untrusted``: a record that marks a conversation an
    earlier engine on this store trusted (as one written at dea920ae could, when the listed conversation
    retook main by turn starts) is resumed and trusted, and its pin is carried."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    engine = RewriteEngine(first, config=ROOMY)
    send(engine, MESSAGES, main_request(conversation("the user's prompt", 1), OLD))
    child = conversation("CHILD TASK do it", 3)
    send(engine, MESSAGES, main_request([*child, user("go on")], OLD))
    assert first.epoch_record().get("untrusted") is True
    first.add_trusted_conversation(key_of(child))
    rid, reply = call_tool(first, "contexto_pin", kind="obligation", text="pinned after the resume")
    resumed = RewriteEngine(first, config=ROOMY)
    events = send(resumed, MESSAGES, main_request([*child, user("go on"), *pin_pair("c1", reply), user("more")], OLD))
    assert any(e["event"] == "epoch_resumed" for e in events)
    assert first.main_records() == [rid] and carried_ids(new_session(home, "s2", root, 2)) == [rid]


# Writing the list (review A round 7, finding 6; review B round 7, N20)


def test_a_failed_list_write_is_one_content_free_event_and_the_pin_is_still_carried(home, tmp_path):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rid, reply = call_tool(first, "contexto_pin", kind="decision", text="x")
    calls = []

    def broken(key):
        calls.append(key)
        raise OSError("disk full at /secret/path")

    first.add_trusted_conversation = broken
    engine = RewriteEngine(first, config=ROOMY)
    events = send(engine, MESSAGES, main_turn(MESSAGES, messages_call("c1", PIN_TOOL, {}, reply)))
    assert {"event": "trusted_conversations_unavailable", "error": "OSError"} in events
    assert events[-1]["event"] == "request" and "/secret/path" not in json.dumps(events)
    assert first.main_records() == [rid] and carried_ids(new_session(home, "s2", root, 2)) == [rid]
    events = send(engine, MESSAGES, main_turn(MESSAGES, messages_call("c1", PIN_TOOL, {}, reply)))
    assert "trusted_conversations_unavailable" not in names_of(events) and len(calls) == 1


def test_a_listed_conversation_trusted_again_moves_to_the_end_once_per_engine(home, tmp_path):
    """The list is ordered by last trust, as "the newest 64" says: a later engine that trusts a listed
    conversation (a ``--continue`` that resumes it, or one by the list) writes it again, once."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = first_launch(first)
    a = conversation("conversation A", 3)
    send(one, MESSAGES, main_request(a, OLD))
    first.record_cli_session("claude", NEW)
    b = [user("conversation B", context=True)]
    send(one, MESSAGES, main_request(b, NEW), session=NEW)
    assert first.trusted_conversations() == [key_of(a), key_of(b)]
    real, calls = first.add_trusted_conversation, []

    def counted(key):
        calls.append(key)
        real(key)

    first.add_trusted_conversation = counted
    two = RewriteEngine(first, config=ROOMY)
    a = [*a, assistant("x"), user("A again")]
    send(two, MESSAGES, main_request(a, OLD))
    assert first.trusted_conversations() == [key_of(b), key_of(a)]
    send(two, MESSAGES, main_request([*b, assistant("y"), user("B again")], NEW), session=NEW)
    assert first.trusted_conversations() == [key_of(a), key_of(b)]
    for turn in range(3):  # A and B take main from each other by the session rules
        a = [*a, assistant(f"a{turn}"), user(f"A {turn}")]
        send(two, MESSAGES, main_request(a, OLD))
        send(two, MESSAGES, main_request([*b, assistant("y"), user(f"B {turn}")], NEW), session=NEW)
    assert calls == [key_of(a), key_of(b)]


def test_a_conversation_continued_in_every_launch_stays_on_the_list(home, tmp_path):
    """Review A round 7, finding 6 (fifo): 64 other conversations trusted after A do not push A off the list
    when every launch continues A."""
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    a = conversation("conversation A", 1)
    for launch in range(TRUSTED_CONVERSATIONS_KEPT + 2):
        engine = RewriteEngine(first, config=ROOMY)
        a = [*a, assistant(f"a{launch}"), user(f"A {launch}")]
        send(engine, MESSAGES, main_request(a, OLD))
        first.add_trusted_conversation(f"anthropic:{launch:064x}")
        assert key_of(a) in first.trusted_conversations(), launch
