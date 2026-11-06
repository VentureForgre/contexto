"""Which conversation is main after `/clear`, `/resume`, and at a fresh session's start (D50).

The request shapes are the ones Claude Code 2.1.302, 2.1.310, 2.1.314, and 2.1.318 sent to a local
stub (zero spend): every Messages request carries the CLI's session id in ``X-Claude-Code-Session-Id``
and in ``metadata.user_id``; ``/clear`` starts a new id, ``/resume`` sends the resumed
conversation's old id again, and subagents and the tool-less side request sent before each prompt
carry the id of the conversation they belong to. On 2.1.314 an agent-team teammate adds
``parent_session_id`` (the lead's id) to ``metadata.user_id``, keeping the lead's own id in process
and having its own in a tmux pane, and a ``claude -p`` run from the Bash tool has its own id and
the User-Agent entrypoint ``sdk-cli`` where the interactive CLI's is ``cli``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from contexto import mcp
from contexto.rewrite import (
    ANTHROPIC,
    CLAUDE_CODE_COMPACTION_SUMMARY,
    MESSAGES,
    RewriteEngine,
    compaction_summary,
    keyed,
)
from contexto.store.memory import MemoryStore
from test_codex import CONFIG as CODEX_CONFIG
from test_codex import history as codex_history
from test_codex import prompt as codex_prompt
from test_codex import request as codex_request
from test_rewrite import (
    CONFIG,
    FILL,
    assistant,
    conversation,
    ctx_block,
    history,
    request,
    settings_only,
    tool_result,
    tool_use,
    user,
)

RESPONSES_PATH = "/v1/responses"
OLD, NEW = "237dedfa-8ca9-43b9-ad3c-4a6ebcb1a084", "916e1695-b1be-43de-abe8-eb4ffa7e0931"
SUBAGENT_TOOLS = [{"name": "Bash", "input_schema": {"type": "object"}}]


def headers(session: str | None, entrypoint: str = "cli", agent: bool = False) -> dict[str, str]:
    found = {"User-Agent": f"claude-cli/2.1.314 (external, {entrypoint})"}
    if agent:
        found["X-Claude-Code-Agent-Id"] = "a36c784c53af45cc0"
    return {**found, "X-Claude-Code-Session-Id": session} if session else found


def metadata(session: str, parent: str | None = None) -> dict:
    fields = {"device_id": "0" * 64, "account_uuid": "", "session_id": session}
    return {"user_id": json.dumps({**fields, "parent_session_id": parent} if parent else fields)}


def main_request(messages: list[dict], session: str, **extra) -> dict:
    return request(messages, metadata=metadata(session), **extra)


def side_request(text: str, session: str) -> dict:
    """Claude Code's side request before a prompt: one user message, no tool list."""
    side = request([user(text)], metadata=metadata(session), max_tokens=512)
    del side["tools"]
    return side


def subagent_request(session: str, turns: int = 1) -> dict:
    return request(conversation("SUB-TASK do it", turns), tools=SUBAGENT_TOOLS, metadata=metadata(session))


def send(engine: RewriteEngine, req: dict, session: str | None, input_tokens: int = 300,
         entrypoint: str = "cli", agent: bool = False, tool_calls: bool | None = None) -> list[dict]:
    _, _, events = engine(req, headers(session, entrypoint, agent), MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": input_tokens}, req, tool_calls=tool_calls)
    return events


def tool(store, name: str, arguments: dict) -> tuple[str, dict]:
    """One MCP tool call: (reply text, the call's mcp_call event)."""
    logged: list[dict] = []
    real_log = store.log
    store.log = lambda event: (logged.append(event), real_log(event))  # type: ignore[method-assign]
    try:
        server = mcp.Server(store, io.StringIO(), io.StringIO())
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
        result = server.handle_line(line)["result"]
    finally:
        store.log = real_log  # type: ignore[method-assign]
    return result["content"][0]["text"], next(e for e in logged if e.get("event") == "mcp_call")


def old_conversation() -> list[dict]:
    """Eight turns, one of them naming the codename, then a new prompt: it rotates at CONFIG's bound."""
    msgs = history(8) + [user("old new prompt")]
    msgs[3] = user(f"the codename is BLUEFIN {FILL}")
    return msgs


def test_after_clear_the_new_conversation_publishes_the_turn_room():
    """The QA test (verify-clear): after `/clear` the dead conversation must not stay main."""
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    old = main_request(conversation("old conversation", 5), OLD)
    send(engine, old, OLD, input_tokens=900)
    old_turn = store.turn_room()[0]
    new = main_request(conversation("after clear", 1), NEW)
    events = send(engine, new, NEW)
    assert store.turn_room()[0] != old_turn and events[-1]["main"] is True
    assert {"event": "main_changed", "reason": "new_cli_session", "turn_starts": 1} in events


def test_after_clear_pins_and_paged_history_are_reachable_again():
    """The live failure: the old conversation's last room is spent, `/clear`, then contexto_state and
    contexto_search returned nothing. The new conversation's first turn publishes its own room."""
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    old = main_request(old_conversation(), OLD)
    assert "rotation" in [e["event"] for e in send(engine, old, OLD, input_tokens=3_600)]
    tool(store, "contexto_pin", {"kind": "decision", "text": "release codename BLUEFIN"})
    text, event = tool(store, "contexto_state", {})
    assert event["chars"] == 0 and event["turn_budget"] == "spent"  # the old room: 4,000 - 3,600 - 500
    send(engine, side_request("synthetic side request", NEW), NEW)
    send(engine, main_request(conversation("after clear", 1), NEW), NEW)
    text, event = tool(store, "contexto_state", {})
    assert "release codename BLUEFIN" in text and event["chars"] > 0
    text, event = tool(store, "contexto_search", {"query": "BLUEFIN"})
    assert event["hits"] >= 1 and "BLUEFIN" in text


def test_the_users_first_prompt_is_main_although_a_side_request_arrived_first():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    assert send(engine, side_request("synthetic side request", OLD), OLD)[-1]["main"] is True  # first seen
    events = send(engine, main_request(conversation("first prompt", 1), OLD), OLD)
    assert events[-1]["main"] is True
    assert {"event": "main_changed", "reason": "after_side_request", "turn_starts": 1} in events
    assert store.retained_current()[3][0]["text"] == "first prompt"
    # Before every later prompt Claude Code sends another side request: it never takes main back.
    assert send(engine, side_request("synthetic side request 2", OLD), OLD)[-1]["main"] is False
    assert send(engine, main_request(conversation("first prompt", 2), OLD), OLD)[-1]["main"] is True


def test_a_tool_less_request_of_main_itself_does_not_hand_main_to_a_subagent():
    """A conversation seen with a tool list once keeps counting as one (a compaction request, say,
    replays main's messages without tools): the side-request tie-break never applies to it."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    first = conversation("first prompt", 1)
    send(engine, main_request(first, OLD), OLD)
    tool_less = main_request(first, OLD)
    del tool_less["tools"]
    send(engine, tool_less, OLD)
    assert send(engine, subagent_request(OLD), OLD)[-1]["main"] is False


def test_subagents_and_side_requests_never_take_over_in_either_session():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    send(engine, main_request(conversation("old conversation", 2), OLD), OLD)
    assert send(engine, subagent_request(OLD), OLD)[-1]["main"] is False  # the parent's session id
    send(engine, side_request("synthetic side request", NEW), NEW)  # sent before the prompt after /clear
    assert engine._main == keyed(ANTHROPIC, main_request(conversation("old conversation", 2), OLD))
    new = main_request(conversation("after clear", 1), NEW)
    assert send(engine, new, NEW)[-1]["main"] is True
    turn = store.turn_room()[0]
    assert send(engine, subagent_request(NEW), NEW)[-1]["main"] is False
    # A background subagent the old conversation launched before /clear still runs, with the old id.
    old_agent = request(conversation("SUB-OLD background task", 1), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    assert send(engine, old_agent, OLD)[-1]["main"] is False
    assert store.turn_room()[0] == turn and engine._main == keyed(ANTHROPIC, new)


def test_resume_of_the_old_conversation_takes_main_back_whatever_the_turn_counts():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    send(engine, main_request(conversation("old conversation", 2), OLD), OLD)
    send(engine, main_request(conversation("after clear", 4), NEW), NEW)  # now longer than the old one
    events = send(engine, main_request(conversation("old conversation", 3), OLD), OLD)  # /resume
    assert events[-1]["main"] is True
    assert {"event": "main_changed", "reason": "resumed_cli_session", "turn_starts": 3} in events
    assert store.retained_current()[3][0]["text"] == "old conversation"
    events = send(engine, main_request(conversation("after clear", 5), NEW), NEW)  # /resume of the newer one
    assert events[-1]["main"] is True and store.retained_current()[3][0]["text"] == "after clear"


def test_inside_a_tool_loop_a_new_session_waits_for_its_turn_start():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 3), OLD), OLD)
    mid = main_request(conversation("after clear", 1, mid_loop=True), NEW)
    assert send(engine, mid, NEW)[-1]["main"] is False


def test_metadata_user_id_alone_identifies_the_session():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 5), OLD), None)
    assert send(engine, main_request(conversation("after clear", 1), NEW), None)[-1]["main"] is True


def test_without_a_session_id_main_moves_only_on_more_turn_starts_as_before():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, request(conversation("old conversation", 3)), None)
    assert send(engine, request(conversation("after clear", 1)), None)[-1]["main"] is False
    unreadable = request(conversation("after clear", 2), metadata={"user_id": "not json"})
    assert send(engine, unreadable, None)[-1]["main"] is False
    assert send(engine, request(conversation("after clear", 4)), None)[-1]["main"] is True


def test_the_header_wins_over_metadata_and_the_event_names_no_session():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 3), OLD), OLD)
    same = main_request(conversation("after clear", 1), NEW)  # metadata says new, the header says old
    assert send(engine, same, OLD)[-1]["main"] is False
    events = send(engine, main_request(conversation("after clear", 2), OLD), NEW)
    assert events[-1]["main"] is True
    assert OLD not in json.dumps(events) and NEW not in json.dumps(events)


def test_codex_session_ids_are_not_read():
    """Codex sends its own ``session-id``; main stays the conversation with the most turn starts."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CODEX_CONFIG)
    first = codex_request(codex_history(2) + [codex_prompt("next")])
    engine(first, {"session-id": OLD}, RESPONSES_PATH)
    other = codex_history(0) + [codex_prompt("another conversation")]
    _, _, events = engine(codex_request(other), {"session-id": NEW}, RESPONSES_PATH)
    assert events[-1]["main"] is False and "main_changed" not in [e["event"] for e in events]



# Other Claude Code sessions on the same proxy (reviewer A, PR #238) ----------------------------------

TEAM, NESTED, OTHER = ("aaaaaaaa-0000-4000-8000-00000000000" + d for d in "123")


def teammate(session: str, turns: int) -> dict:
    """A teammate's request: its inbox messages are new turn starts; ``parent_session_id`` is the lead's."""
    return request(conversation("<teammate-message>TEAMMATE research</teammate-message>", turns),
                   tools=SUBAGENT_TOOLS, metadata=metadata(session, parent=OLD))


def changes(events: list[dict]) -> list[str]:
    return [e["reason"] for e in events if e["event"] == "main_changed"]


def test_a_teammate_never_takes_main_in_a_pane_or_in_process():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    lead = lambda turns: main_request(conversation("lead conversation", turns), OLD)  # noqa: E731
    send(engine, lead(3), OLD, input_tokens=2_500)
    room, record = store.turn_room(), store.epoch_record()
    for turns in (1, 2, 5):  # a tmux teammate's own id, then more turn starts than the lead
        assert send(engine, teammate(TEAM, turns), TEAM)[-1]["main"] is False
        assert send(engine, teammate(OLD, turns), OLD)[-1]["main"] is False  # in process: the lead's id
    assert store.turn_room()[:2] == room[:2] and store.epoch_record() == record
    assert record["conversation"] == keyed(ANTHROPIC, lead(3))
    assert send(engine, lead(4), OLD)[-1]["main"] is True


def test_a_claude_p_run_from_the_bash_tool_never_takes_main():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    send(engine, main_request(conversation("lead conversation", 3, mid_loop=True), OLD), OLD)
    room = store.turn_room()
    nested = main_request(conversation("NESTED-P hello", 1), NESTED)
    for entrypoint in ("sdk-cli", "sdk-ts", "sdk-py"):
        assert send(engine, nested, NESTED, entrypoint=entrypoint)[-1]["main"] is False
    assert store.turn_room() == room
    assert send(engine, main_request(conversation("lead conversation", 3, mid_loop=True), OLD), OLD)[-1]["main"]


def test_two_live_sessions_cannot_trade_main_at_each_turn_start():
    """Another interactive session sharing the proxy takes main once, at its first turn start; the
    lead keeps running, so the lead returns only by the turn-start rule, and the other session never
    regains main by its id."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    lead = lambda turns, loop=False: main_request(conversation("lead", turns, mid_loop=loop), OLD)  # noqa: E731
    other = lambda turns: main_request(conversation("other", turns), OTHER)  # noqa: E731
    reasons = changes(send(engine, lead(3), OLD))
    reasons += changes(send(engine, other(1), OTHER))
    assert reasons == ["new_cli_session"]
    assert send(engine, lead(3, loop=True), OLD)[-1]["main"] is False  # the lead was mid tool loop
    for turns in range(4, 8):
        reasons += changes(send(engine, lead(turns), OLD))
        reasons += changes(send(engine, other(turns - 2), OTHER))
    assert reasons == ["new_cli_session", "more_turn_starts"]


def test_resume_after_a_subagent_took_main_by_turn_starts_goes_back_to_the_users_conversation():
    """Reviewer A's finding 2: a SendMessage-resumed subagent outgrew main, then /clear and /resume."""
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    send(engine, main_request(conversation("old conversation", 2), OLD), OLD)
    assert changes(send(engine, subagent_request(OLD, turns=3), OLD)) == ["more_turn_starts"]
    send(engine, main_request(conversation("after clear", 4), NEW), NEW)
    events = send(engine, main_request(conversation("old conversation", 3), OLD), OLD)
    assert changes(events) == ["resumed_cli_session"]
    assert store.retained_current()[3][0]["text"] == "old conversation"


def test_clear_and_resume_can_go_back_and_forth():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    sessions = {OLD: "old conversation", NEW: "after clear", OTHER: "cleared again"}
    reasons = changes(send(engine, main_request(conversation(sessions[OLD], 2), OLD), OLD))
    turns = 2
    for session in (NEW, OTHER, OLD, NEW, OLD, OTHER):
        turns += 1
        events = send(engine, main_request(conversation(sessions[session], turns if session == OLD else 1), session),
                      session)
        assert events[-1]["main"] is True
        reasons += changes(events)
    assert reasons == ["new_cli_session", "new_cli_session", "resumed_cli_session", "resumed_cli_session",
                       "resumed_cli_session", "resumed_cli_session"]


def test_a_claude_p_under_a_headless_lead_never_takes_main():
    """Reviewer A round 2, captured on 2.1.314: under `contexto run claude -p` the lead's entrypoint is
    sdk-cli and a `claude -p` run from its Bash tool inherits it, with its own id and no parent."""
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    lead = lambda loop: main_request(conversation("NESTED-RUN run claude -p", 1, mid_loop=loop), OLD)  # noqa: E731
    send(engine, lead(False), OLD, input_tokens=3_000, entrypoint="sdk-cli", tool_calls=True)  # the Bash call
    room, record = store.turn_room(), store.epoch_record()
    events = send(engine, main_request(conversation("NESTED-P hello", 1), NESTED), NESTED, entrypoint="sdk-cli")
    assert events[-1]["main"] is False and changes(events) == []
    assert store.turn_room() == room and store.epoch_record() == record
    assert send(engine, lead(True), OLD, input_tokens=3_000, entrypoint="sdk-cli")[-1]["main"] is True


def test_a_headless_session_never_takes_main_by_its_id_from_an_interactive_one():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 3), OLD), OLD, entrypoint="sdk-cli")
    assert send(engine, main_request(conversation("after clear", 1), NEW), NEW)[-1]["main"] is False
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 3), OLD), OLD)
    assert send(engine, main_request(conversation("other", 1), NEW), NEW, entrypoint="sdk-cli")[-1]["main"] is False


def test_main_never_moves_at_a_response():
    """Reviewer A round 2: A's turn-start request is in flight when B's /clear session takes main; A's
    response must not move main back without a main_changed event."""
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    send(engine, main_request(conversation("old conversation", 2), OLD), OLD)
    in_flight = main_request(conversation("old conversation", 3), OLD)
    engine(in_flight, headers(OLD), MESSAGES)
    new = main_request(conversation("after clear", 1), NEW)
    assert changes(send(engine, new, NEW)) == ["new_cli_session"]
    turn = store.turn_room()[0]
    engine.observe_usage(MESSAGES, {"input_tokens": 300}, in_flight)
    assert engine._main == keyed(ANTHROPIC, new) and store.turn_room()[0] == turn
    assert send(engine, main_request(conversation("after clear", 2), NEW), NEW)[-1]["main"] is True


def test_two_sessions_taking_turns_follow_the_one_prompted():
    """Not a supported setup: two interactive sessions on one proxy, each idle while the other is
    prompted, pass the quiet check at every switch, and each switch is logged."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    reasons: list[str] = []
    for turns in range(1, 5):
        for first, session in (("lead", OLD), ("other", OTHER)):
            events = send(engine, main_request(conversation(first, turns), session), session)
            assert events[-1]["main"] is True
            reasons += changes(events)
    assert reasons == ["new_cli_session"] + ["resumed_cli_session"] * 6


def test_a_deeply_nested_user_id_is_no_session_id():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 3), OLD), OLD)
    deep = request(conversation("after clear", 1), metadata={"user_id": "[" * 100_000 + "]" * 100_000})
    _, _, events = engine(deep, headers(None), MESSAGES)
    assert events[-1]["event"] == "request" and events[-1]["main"] is False


# /compact and auto-compaction (reviewer B round 2, PR #238) ------------------------------------------

SUMMARY = (CLAUDE_CODE_COMPACTION_SUMMARY
           + "\n<system-reminder>\n<tools-used-before-summary>Agent</tools-used-before-summary>\n</system-reminder>"
           "\nSummary:\nsynthetic summary of the earlier turns\n\nThe messages after this summary are the most recent "
           "messages from before compaction, kept verbatim.")


def compacted(prompt: str, manual: bool = True, kept: str = "synthetic last reply") -> list[dict]:
    """A compacted conversation in the form D50 recorded (2.1.314 and 2.1.318 with the attribution reminder
    off): the summary (a plain string), a role system message, the last message from before compaction
    kept verbatim, then the new prompt, which after /compact also holds the command's blocks. The forms
    the CLI sends by default are in the capture fixture below (D59)."""
    if manual:
        command = [{"type": "text", "text": "<local-command-caveat>synthetic caveat</local-command-caveat>\n"},
                   {"type": "text", "text": "<command-name>/compact</command-name>\n"},
                   {"type": "text", "text": "<local-command-stdout>Compacted</local-command-stdout>\n"}]
        middle = settings_only()
    else:
        command, middle = [], {"role": "system", "content": "synthetic date note"}
    new = {"role": "user", "content": [ctx_block(), *command, {"type": "text", "text": prompt}]}
    return [{"role": "user", "content": SUMMARY}, middle, assistant(kept), new]


def compaction_request(messages: list[dict]) -> list[dict]:
    """Claude Code's request for the summary: main's own history, the ask appended to its last prompt."""
    last = messages[-1]
    ask = {"type": "text", "text": "CRITICAL: Respond with TEXT ONLY. synthetic summary instructions"}
    return [*messages[:-1], {"role": "user", "content": [*last["content"], ask]}]


def test_after_compact_the_compacted_conversation_is_main_with_pins_and_pages():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    old = main_request(old_conversation(), OLD)
    send(engine, old, OLD, input_tokens=3_600)
    tool(store, "contexto_pin", {"kind": "decision", "text": "release codename BLUEFIN"})
    assert tool(store, "contexto_state", {})[1]["turn_budget"] == "spent"
    send(engine, main_request(compaction_request(old["messages"]), OLD), OLD, input_tokens=3_700)
    send(engine, side_request("synthetic side request", OLD), OLD)
    events = send(engine, main_request(compacted("AFTER-COMPACT turn"), OLD), OLD)
    assert events[-1]["main"] is True and changes(events) == ["compacted"]
    assert "over_bound" not in [e["event"] for e in events] and store.turn_room()[1] > 0
    text, event = tool(store, "contexto_state", {})
    assert "release codename BLUEFIN" in text and event["chars"] > 0
    text, event = tool(store, "contexto_search", {"query": "BLUEFIN"})
    assert event["hits"] >= 1 and "BLUEFIN" in text
    later = main_request([*compacted("AFTER-COMPACT turn"), assistant("a"), user("THREE next turn")], OLD)
    assert send(engine, later, OLD)[-1]["main"] is True


def test_auto_compaction_hands_over_the_same_way():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 6), OLD), OLD)
    events = send(engine, main_request(compacted("AFTER-COMPACT turn", manual=False), OLD), OLD)
    assert events[-1]["main"] is True and changes(events) == ["compacted"]


def test_resume_after_compact_returns_to_the_compacted_conversation():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 6), OLD), OLD)
    send(engine, main_request(compacted("AFTER-COMPACT turn"), OLD), OLD)
    assert changes(send(engine, main_request(conversation("after clear", 1), NEW), NEW)) == ["new_cli_session"]
    resumed = main_request([*compacted("AFTER-COMPACT turn"), assistant("a"), user("after resume")], OLD)
    assert changes(send(engine, resumed, OLD)) == ["resumed_cli_session"]


def test_only_claude_codes_own_compaction_summary_hands_over():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 6), OLD), OLD)
    quoted = main_request(conversation(CLAUDE_CODE_COMPACTION_SUMMARY + "typed by the user", 1), OLD)
    assert send(engine, quoted, OLD)[-1]["main"] is False  # a prompt, in a list with its context blocks
    shapes = [compacted("p1"), compacted("p2")]
    shapes[1][0] = {"role": "user", "content": SUMMARY.replace("ran out of context", "ended")}
    assert send(engine, main_request(shapes[1], OLD), OLD)[-1]["main"] is False
    subagent = request(compacted("SUB-TASK continued"), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    assert send(engine, subagent, OLD, agent=True)[-1]["main"] is False  # a compacted subagent
    assert send(engine, main_request(shapes[0], OLD), OLD, entrypoint="sdk-cli")[-1]["main"] is False
    teammate_compacted = request(compacted("p3"), metadata=metadata(TEAM, parent=OLD))
    assert send(engine, teammate_compacted, TEAM)[-1]["main"] is False
    assert send(engine, main_request(shapes[0], OLD), OLD)[-1]["main"] is True


def test_a_subagent_never_takes_main_even_with_more_turn_starts():
    """Every subagent request carries X-Claude-Code-Agent-Id (2.1.302 to 2.1.318)."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 2), OLD), OLD)
    assert send(engine, subagent_request(OLD, turns=5), OLD, agent=True)[-1]["main"] is False


# Headless /clear and /compact (reviewer A round 3, PR #238) -------------------------------------------

def cleared(prompt: str) -> list[dict]:
    """The first request after /clear (2.1.314 and 2.1.318, interactive and stream-json alike): the
    command's blocks, then the prompt, in the first message."""
    blocks = [ctx_block(), {"type": "text", "text": "<command-name>/clear</command-name>\n"},
              {"type": "text", "text": "<local-command-stdout></local-command-stdout>\n"},
              {"type": "text", "text": prompt}]
    return [{"role": "user", "content": blocks}, settings_only()]


def test_a_headless_clear_hands_over_while_main_is_idle():
    """`claude -p --input-format stream-json` on 2.1.318 and 2.1.314: /clear starts a new session id
    under sdk-cli, after main's final response."""
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    old = main_request(old_conversation(), OLD)
    send(engine, old, OLD, input_tokens=3_600, entrypoint="sdk-cli", tool_calls=False)
    tool(store, "contexto_pin", {"kind": "decision", "text": "release codename BLUEFIN"})
    events = send(engine, main_request(cleared("TWO-A after clear"), NEW), NEW, entrypoint="sdk-cli")
    assert events[-1]["main"] is True and changes(events) == ["new_cli_session"]
    text, event = tool(store, "contexto_state", {})
    assert "release codename BLUEFIN" in text and event["chars"] > 0
    assert tool(store, "contexto_search", {"query": "BLUEFIN"})[1]["hits"] >= 1


def test_under_a_headless_lead_a_new_session_waits_for_main_to_be_idle():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    lead = main_request(conversation("NESTED-RUN run claude -p", 1), OLD)
    nested = main_request(conversation("NESTED-P hello", 1), NESTED)
    for released in (True, None):  # a client call released, or the proxy could not tell
        send(engine, lead, OLD, entrypoint="sdk-cli", tool_calls=released)
        assert send(engine, nested, NESTED, entrypoint="sdk-cli")[-1]["main"] is False
    engine(lead, headers(OLD, "sdk-cli"), MESSAGES)  # main's request in flight, no response yet
    assert send(engine, nested, NESTED, entrypoint="sdk-cli")[-1]["main"] is False


def test_a_headless_compaction_hands_over():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 6), OLD), OLD, entrypoint="sdk-cli", tool_calls=True)
    events = send(engine, main_request(compacted("AFTER-COMPACT turn"), OLD), OLD, entrypoint="sdk-cli")
    assert events[-1]["main"] is True and changes(events) == ["compacted"]


@pytest.mark.parametrize("order", ["retry_answered_first", "late_end_first", "duplicate"])
def test_a_late_or_duplicate_response_never_makes_a_headless_main_idle(order):
    """Reviewer A round 4: the CLI gives up on a stalled request R1 and retries it as R1' (the same
    body); R1' releases the Bash running a nested `claude -p`. R1's late end, with no client call,
    must not make the lead idle, in either order, and neither may a duplicate of an answered one."""
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    tool(store, "contexto_pin", {"kind": "fact", "text": "synthetic pinned fact " * 90})  # about 2 KB
    lead = main_request(conversation("HEADLESS lead prompt", 1), OLD)
    if order == "duplicate":
        send(engine, lead, OLD, input_tokens=3_000, entrypoint="sdk-cli", tool_calls=True)
        engine.observe_usage(MESSAGES, {"input_tokens": 3_000}, lead, tool_calls=False)  # usage_unattributed
    else:
        engine(lead, headers(OLD, "sdk-cli"), MESSAGES)  # R1, abandoned
        engine(lead, headers(OLD, "sdk-cli"), MESSAGES)  # R1'
        responses = [({"input_tokens": 3_000}, True), ({"input_tokens": 3_000}, False)]  # R1', R1's late end
        for usage, released in responses if order == "retry_answered_first" else responses[::-1]:
            engine.observe_usage(MESSAGES, usage, lead, tool_calls=released)
    nested = send(engine, main_request(conversation("NESTED-P hello", 1), NESTED), NESTED, entrypoint="sdk-cli")
    assert nested[-1]["main"] is False and changes(nested) == []
    text, _ = tool(store, "contexto_state", {})
    loop = main_request([*conversation("HEADLESS lead prompt", 1), tool_use(1), tool_result(1, text)], OLD)
    events = send(engine, loop, OLD, entrypoint="sdk-cli")
    assert events[-1]["main"] is True and "over_bound" not in [e["event"] for e in events]


@pytest.mark.parametrize("released", [True, False, None])
def test_a_response_without_an_input_count_still_sets_busy_or_idle(released):
    """D49 amended (#244) returns early from observe_usage on usage with no input count; D50 (#238) reads
    every response for a client call. A response without usage must still mark the lead idle (it
    released no call) or busy (a call may be pending), or a headless /clear could never hand over. For
    the busy case the lead is made idle first, by a complete response (review of #244): a fresh request
    is already busy, so otherwise a busy mark skipped by the early return would go unnoticed."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    lead = main_request(conversation("HEADLESS lead prompt", 1), OLD)
    key = keyed(ANTHROPIC, lead)
    engine(lead, headers(OLD, "sdk-cli"), MESSAGES)
    if released is False:
        engine.observe_usage(MESSAGES, {"output_tokens": 5}, lead, tool_calls=False)
        assert engine._idle[key] is True
        return
    engine.observe_usage(MESSAGES, {"input_tokens": 300, "output_tokens": 5}, lead, tool_calls=False)
    assert engine._idle[key] is True
    engine.observe_usage(MESSAGES, {"output_tokens": 5}, lead, tool_calls=released)
    assert engine._idle[key] is False


# Auto-compaction inside a tool loop (review of #238, D59) ----------------------------------------------

def compacted_loop() -> list[dict]:
    """The request that continues a tool loop after Claude Code auto-compacts it (2.1.314 and 2.1.318): the
    summary, a role system message, then the pending tool_use and its result, with no new prompt."""
    return [{"role": "user", "content": SUMMARY}, {"role": "system", "content": "synthetic date note"},
            tool_use(1), tool_result(1, "ok")]


@pytest.mark.parametrize("entrypoint", ["cli", "sdk-cli"])
def test_an_auto_compaction_inside_a_tool_loop_hands_over_at_once(entrypoint):
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    old = main_request(old_conversation(), OLD)
    send(engine, old, OLD, input_tokens=3_600, entrypoint=entrypoint, tool_calls=True)
    tool(store, "contexto_pin", {"kind": "decision", "text": "release codename BLUEFIN"})
    send(engine, main_request(compaction_request(old["messages"]), OLD), OLD, input_tokens=3_700,
         entrypoint=entrypoint, tool_calls=False)
    assert tool(store, "contexto_state", {})[1]["turn_budget"] == "spent"
    events = send(engine, main_request(compacted_loop(), OLD), OLD, entrypoint=entrypoint, tool_calls=True)
    assert events[-1]["main"] is True and changes(events) == ["compacted"]
    assert "over_bound" not in [e["event"] for e in events] and store.turn_room()[1] > 0
    text, event = tool(store, "contexto_state", {})
    assert "release codename BLUEFIN" in text and event["chars"] > 0
    assert tool(store, "contexto_search", {"query": "BLUEFIN"})[1]["hits"] >= 1


@pytest.mark.parametrize("summary_answered", [True, None])
def test_a_compaction_inside_a_tool_loop_waits_while_main_may_have_a_call_out(summary_answered):
    """Not while main's newest request may hold a client call (its response released one, the proxy
    could not tell, or it is unanswered): that call would be charged against the new room."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    old = main_request(old_conversation(), OLD)
    send(engine, old, OLD, input_tokens=3_600, tool_calls=True)
    summary = main_request(compaction_request(old["messages"]), OLD)
    if summary_answered:
        send(engine, summary, OLD, input_tokens=3_700, tool_calls=None)
    else:
        engine(summary, headers(OLD), MESSAGES)  # in flight
    events = send(engine, main_request(compacted_loop(), OLD), OLD, tool_calls=True)
    assert events[-1]["main"] is False and changes(events) == []
    events = send(engine, main_request([*compacted_loop(), assistant("done"), user("next prompt")], OLD), OLD)
    assert events[-1]["main"] is True and changes(events) == ["compacted"]  # its next prompt, as before


def test_only_main_s_own_compaction_takes_over_inside_a_tool_loop():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    send(engine, main_request(conversation("old conversation", 6), OLD), OLD, tool_calls=False)
    subagent = request(compacted_loop(), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    assert send(engine, subagent, OLD, agent=True)[-1]["main"] is False
    assert send(engine, main_request(compacted_loop(), NEW), NEW)[-1]["main"] is False  # another session
    assert send(engine, main_request(compacted_loop(), OLD), OLD, entrypoint="sdk-cli")[-1]["main"] is False


def test_a_late_request_of_an_older_compaction_does_not_retake_main():
    """Reviewer A, PR #246: the mid-loop handover held for any compaction of main's session, so a late
    request of an older one took main back and pointed a later /resume at it."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    first = [{"role": "user", "content": SUMMARY}, assistant("ok"), user("prompt one " + FILL)]
    send(engine, main_request(first, OLD), OLD, input_tokens=900, tool_calls=False)
    second = [{"role": "user", "content": SUMMARY + " second compaction"}, assistant("ok"), user("prompt two")]
    assert changes(send(engine, main_request(second, OLD), OLD, tool_calls=False)) == ["compacted"]
    late = main_request([*first, tool_use(3), tool_result(3, "x")], OLD)
    events = send(engine, late, OLD, tool_calls=False)
    assert events[-1]["main"] is False and changes(events) == []


# Claude Code's real compaction requests (review of #246, D59) ------------------------------------------
# Captured from 2.1.314 and 2.1.318 against a loopback stub (tests/fixtures/make_compaction_shapes.py):
# headless /compact, headless auto-compaction and interactive /compact with the default attribution
# reminder on, and with it off headless /compact (2.1.318) and auto-compaction (2.1.314).

SHAPES = json.loads((Path(__file__).parent / "fixtures" / "compaction_shapes_2_1_31x.json").read_text())
SESSION_IDS = {1: OLD, 2: NEW}


def replay(captured: list[dict]) -> list[tuple[bool, list[str], bool]]:
    """Each captured request through one engine, in order: (main, main_changed reasons, compacted shape).
    The response usage is what the stub reported; only the auto-compaction's first response held a call."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32))
    out = []
    for captured_request in captured:
        req = {"model": "claude-sonnet-5-5", "max_tokens": 32_000, "messages": captured_request["messages"],
               "metadata": metadata(SESSION_IDS[captured_request["session"]])}
        if captured_request["tools"]:
            req["tools"] = SUBAGENT_TOOLS
        session = SESSION_IDS[captured_request["session"]]
        found = {"User-Agent": captured_request["user_agent"], "X-Claude-Code-Session-Id": session}
        _, _, events = engine(req, found, MESSAGES)
        engine.observe_usage(MESSAGES, {"input_tokens": 1_200}, req, tool_calls=False)
        out.append((events[-1]["main"], changes(events), compaction_summary(req["messages"])))
    return out


@pytest.mark.parametrize("version, scenario", [(v, s) for v in sorted(SHAPES) for s in sorted(SHAPES[v])])
def test_claude_codes_real_compaction_hands_main_over(version, scenario):
    """On 2.1.314 and 2.1.318 the compacted conversation's first user message starts with Claude Code's
    attribution <system-reminder> block, then the summary block: D50's plain-string test never matched
    it, so the compacted conversation stayed a side conversation (reviewer B, PR #246)."""
    captured = SHAPES[version][scenario]
    rows = replay(captured)
    assert [row[2] for row in rows] == [False] * (len(rows) - 1) + [True], rows  # only the last is compacted
    assert rows[-1][:2] == (True, ["compacted"]), rows
    # Before it, main's own requests (the prompt and the summary request) were main; the interactive
    # CLI's title requests carry no tool list and are side.
    assert all(row[0] for row, req in zip(rows[:-1], captured) if req["tools"]), rows


@pytest.mark.parametrize("content, matches", [
    (CLAUDE_CODE_COMPACTION_SUMMARY + "\nSummary:\nx", True),  # attribution off, auto-compaction
    ([{"type": "text", "text": CLAUDE_CODE_COMPACTION_SUMMARY + "\nSummary:\nx"}], True),  # off, /compact
    ([ctx_block("attribution"), ctx_block(), {"type": "text", "text": CLAUDE_CODE_COMPACTION_SUMMARY + "\nx"}], True),
    ([ctx_block(), {"type": "text", "text": CLAUDE_CODE_COMPACTION_SUMMARY + "typed by the user"}], False),
    (CLAUDE_CODE_COMPACTION_SUMMARY + "typed by the user", False),  # a prompt that quotes the sentence
    ([{"type": "text", "text": "a prompt"}, {"type": "text", "text": CLAUDE_CODE_COMPACTION_SUMMARY + "\nx"}], False),
    ([{"type": "image", "source": {}}, {"type": "text", "text": CLAUDE_CODE_COMPACTION_SUMMARY + "\nx"}], False),
    ([ctx_block()], False), ([], False), (None, False),
])
def test_the_compaction_summary_is_found_after_leading_reminder_blocks(content, matches):
    assert compaction_summary([{"role": "user", "content": content}, settings_only()]) is matches
    assert compaction_summary([{"role": "assistant", "content": content}]) is False
