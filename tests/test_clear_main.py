"""Which conversation is main after `/clear`, `/resume`, and at a fresh session's start (D50).

The request shapes are the ones Claude Code 2.1.302, 2.1.310, and 2.1.314 sent to a local stub
(zero spend): every Messages request carries the CLI's session id in ``X-Claude-Code-Session-Id``
and in ``metadata.user_id``; ``/clear`` starts a new id, ``/resume`` sends the resumed
conversation's old id again, and subagents and the tool-less side request sent before each prompt
carry the id of the conversation they belong to.
"""

from __future__ import annotations

import io
import json

from contexto import mcp
from contexto.rewrite import ANTHROPIC, MESSAGES, RewriteEngine, keyed
from contexto.store.memory import MemoryStore
from test_codex import CONFIG as CODEX_CONFIG
from test_codex import history as codex_history
from test_codex import prompt as codex_prompt
from test_codex import request as codex_request
from test_rewrite import CONFIG, FILL, conversation, history, request, user

RESPONSES_PATH = "/v1/responses"
OLD, NEW = "237dedfa-8ca9-43b9-ad3c-4a6ebcb1a084", "916e1695-b1be-43de-abe8-eb4ffa7e0931"
SUBAGENT_TOOLS = [{"name": "Bash", "input_schema": {"type": "object"}}]


def headers(session: str | None) -> dict[str, str]:
    found = {"User-Agent": "claude-cli/2.1.314 (external, cli)"}
    return {**found, "X-Claude-Code-Session-Id": session} if session else found


def metadata(session: str) -> dict:
    return {"user_id": json.dumps({"device_id": "0" * 64, "account_uuid": "", "session_id": session})}


def main_request(messages: list[dict], session: str, **extra) -> dict:
    return request(messages, metadata=metadata(session), **extra)


def side_request(text: str, session: str) -> dict:
    """Claude Code's side request before a prompt: one user message, no tool list."""
    side = request([user(text)], metadata=metadata(session), max_tokens=512)
    del side["tools"]
    return side


def subagent_request(session: str, turns: int = 1) -> dict:
    return request(conversation("SUB-TASK do it", turns), tools=SUBAGENT_TOOLS, metadata=metadata(session))


def send(engine: RewriteEngine, req: dict, session: str | None, input_tokens: int = 300) -> list[dict]:
    _, _, events = engine(req, headers(session), MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": input_tokens}, req)
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

