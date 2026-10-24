"""Recall room under subagents and side requests (D40).

Each conversation the CLI runs (the main one, each subagent, each side request) is its own upstream
request with its own context, and a tool result lands only in the window of the conversation that
called the tool. The MCP server cannot tell which conversation called it, so a charge must fit every
conversation that may receive it and is counted against all of them.
"""

from __future__ import annotations

import io
import json
import math
import threading
from http.server import ThreadingHTTPServer

import pytest

from contexto import mcp
from contexto.proxy import ContextoProxy, StreamHold, UsageReader, _takes_keyword, is_client_call
from contexto.rewrite import MESSAGES, Config, RewriteEngine, agent_runs, first_prompt
from contexto.store import SIDE_ROOM_TTL_S, SessionStore
from contexto.store.memory import MemoryStore

from test_codex import call as codex_call
from test_codex import history as codex_history
from test_codex import lead as codex_lead
from test_codex import output as codex_output
from test_codex import prompt as codex_prompt
from test_codex import request as codex_request
from test_rewrite import assistant, conversation, request, tool_result, tool_use, user
from test_stream_hold import DELTA, START, STOP, Upstream, ev, read_with_times, text_block, tool_block

RESPONSES_PATH = "/v1/responses"

BOUND, FLOOR = 150_000, 25_000
CONFIG = Config(bound=BOUND, headroom_floor=FLOOR)
RATIO = 1.5  # set_window(200_000, 2.0): min(measured 2.0, 1.5) characters per token


def room_for(input_tokens: int, output_tokens: int) -> int:
    return max(0, BOUND - input_tokens - output_tokens - FLOOR)


@pytest.fixture(params=["session", "memory"])
def kind(request):
    return request.param


def make_store(kind, tmp_path):
    return SessionStore(tmp_path, "s1") if kind == "session" else MemoryStore("s1", key=b"k" * 32)


def recall(store, page_id, offset=0):
    """One contexto_recall through the MCP server; (reply text, charged tokens)."""
    events: list[dict] = []
    real_log = store.log
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_recall", "arguments": {"page_id": page_id, "offset": offset}}})
    store.log = lambda event: (events.append(event), real_log(event))  # type: ignore[method-assign]
    try:
        result = server.handle_line(line)["result"]
    finally:
        del store.log
    [event] = [e for e in events if e.get("event") == "mcp_call"]
    text = "".join(block["text"] for block in result["content"])
    return text, event.get("charged_tokens", 0)


def big_page(store, chars=60_000):
    (pid,) = store.page_out([[{"role": "user", "content": "z" * chars}]], 1)
    return pid


def respond(engine, body, input_tokens, output_tokens=1_000, **kwargs):
    return engine.observe_usage(MESSAGES, {"input_tokens": input_tokens, "output_tokens": output_tokens}, body,
                                **kwargs)


def session(store):
    """A main conversation (3 turn starts, small) and two subagents (1 turn start each): A near the
    bound, B with some room. Returns the engine and the three request bodies."""
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    sub_a = request(conversation("subagent A", 1))
    sub_b = request(conversation("subagent B", 1))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)  # main's room: 104,000
    engine(sub_a, {}, MESSAGES)
    respond(engine, sub_a, 120_000)  # A's own room: 4,000
    engine(sub_b, {}, MESSAGES)
    respond(engine, sub_b, 94_000)  # B's own room: 30,000
    store.set_window(200_000, 2.0)  # fix the charge ratio at 1.5 characters per token for the test
    return engine, main, sub_a, sub_b


def test_a_subagent_recall_fits_the_subagents_own_window(kind, tmp_path):
    """The overspend: main's room was 104,000 tokens, the subagent's own window had 4,000 left, and
    one recall charged against main's room put tens of thousands of tokens into the subagent's window."""
    store = make_store(kind, tmp_path)
    session(store)
    pid = big_page(store)
    text, charged = recall(store, pid)  # issued by subagent A; the server cannot tell
    assert charged <= room_for(120_000, 1_000)
    assert math.ceil(len(text) / RATIO) <= room_for(120_000, 1_000)


def test_concurrent_subagent_recalls_stay_inside_every_open_window(tmp_path):
    """Two subagents call recall at once from separate store handles (as separate MCP processes
    would). However the calls interleave, no conversation can receive more than its own room."""
    store = SessionStore(tmp_path, "s1")
    session(store)
    pids = [big_page(store, 20_000 + i) for i in range(12)]
    start = threading.Barrier(len(pids))
    charged = [0] * len(pids)

    def worker(i):
        own = SessionStore(tmp_path, "s1")
        start.wait()
        charged[i] = recall(own, pids[i])[1]

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(pids))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    a_calls, b_calls = charged[::2], charged[1::2]  # even calls came from A, odd from B
    assert sum(a_calls) <= room_for(120_000, 1_000)
    assert sum(b_calls) <= room_for(94_000, 1_000)
    assert sum(charged) <= room_for(120_000, 1_000)  # unattributed: every charge counts against A too


def test_a_side_publish_resets_only_its_own_spend(kind, tmp_path):
    """B's next response measures B's input, which holds B's own recalls but none of A's. A's
    unmeasured recalls must stay counted against A."""
    store = make_store(kind, tmp_path)
    engine, _, _, sub_b = session(store)
    pid = big_page(store)
    _, first = recall(store, pid)
    respond(engine, sub_b, 94_000)  # B republishes
    _, second = recall(store, pid)
    assert first + second <= room_for(120_000, 1_000)


def test_concurrent_recalls_racing_side_publishes_never_reset_another_room(tmp_path):
    """B republishes its room over and over while recalls run. B's publishes reset B's spend only;
    A's room keeps every charge, so A never receives more than its room."""
    store = SessionStore(tmp_path, "s1")
    session(store)
    rooms = store.side_rooms()
    a_key = next(key for key, (room, _) in rooms.items() if room == room_for(120_000, 1_000))
    b_key = next(key for key in rooms if key != a_key)
    pids = [big_page(store, 3_000 + i) for i in range(16)]
    start, stop = threading.Barrier(len(pids) + 1), threading.Event()
    charged = [0] * len(pids)

    def worker(i):
        own = SessionStore(tmp_path, "s1")
        start.wait()
        charged[i] = recall(own, pids[i])[1]

    def publisher():
        own = SessionStore(tmp_path, "s1")
        start.wait()
        while not stop.is_set():
            own.set_side_room(b_key, room_for(94_000, 1_000))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(pids))]
    side = threading.Thread(target=publisher)
    for t in threads + [side]:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    side.join()
    a_room, a_spent = store.side_rooms()[a_key]
    assert a_room == room_for(120_000, 1_000)
    assert sum(charged) == a_spent <= a_room  # every charge is on A's record, none was reset away
    assert store.turn_room()[2] == a_spent  # and on main's: main did not publish meanwhile


# The store side


def test_a_charge_gets_the_smallest_open_room_and_counts_against_all(kind, tmp_path):
    store = make_store(kind, tmp_path)
    store.set_turn_room("3", 10_000)
    store.set_side_room("anthropic:a", 4_000)
    store.set_side_room("anthropic:b", 7_000)
    seen = []
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 1_000)
    assert seen == [4_000]
    assert store.turn_room() == ("3", 10_000, 1_000)
    assert store.side_rooms() == {"anthropic:a": (4_000, 1_000), "anthropic:b": (7_000, 1_000)}
    with pytest.raises(ValueError):
        store.charge_turn_room(lambda remaining: remaining + 1)  # never past the smallest room
    store.set_side_room("anthropic:a", 9_000)  # A's next response: A's spend resets, B's stays
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)
    assert seen[-1] == 6_000 and store.side_rooms()["anthropic:b"] == (7_000, 1_000)
    store.set_turn_room("3", 10_000)  # main's same-turn publish resets main only
    assert store.turn_room()[2] == 0 and store.side_rooms()["anthropic:a"] == (9_000, 0)


def test_closing_a_side_room_and_main_turns(kind, tmp_path):
    store = make_store(kind, tmp_path)
    store.set_turn_room("3", 10_000)
    store.set_side_room("anthropic:a", 0)
    seen = []
    assert store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)["limited_by"] == "side"
    store.set_side_room("anthropic:a", None)  # A's response held no tool call
    store.set_side_room("anthropic:never", None)  # closing an unknown room is a no-op
    summary = store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)
    assert summary == {"limited_by": "main", "side_rooms": 0}
    assert seen == [0, 10_000] and store.side_rooms() == {}
    store.set_side_room("anthropic:b", 2_000)
    store.set_turn_room("3", 9_000)
    store.set_turn_room("4", 9_000)  # main's turns leave side rooms alone: only expiry or their holder ends them
    assert store.side_rooms() == {"anthropic:b": (2_000, 0)}


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_a_side_room_expires_unless_republished(kind, tmp_path):
    store = make_store(kind, tmp_path)
    store.clock = clock = Clock()
    store.set_turn_room("3", 10_000)
    store.set_side_room("anthropic:a", 1_000)
    store.set_side_room("anthropic:b", 3_000)
    clock.now += SIDE_ROOM_TTL_S - 1
    store.set_side_room("anthropic:b", 3_000)  # B's next response restarts its time
    seen = []
    summary = store.charge_turn_room(lambda remaining: seen.append(remaining) or 100)
    assert seen == [1_000]  # A still bounds the call just inside its time
    assert summary == {"limited_by": "side", "side_rooms": 2, "limiting_age_s": int(SIDE_ROOM_TTL_S - 1)}
    clock.now += 2
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)
    assert seen[-1] == 2_900 and store.side_rooms() == {"anthropic:b": (3_000, 100)}  # A expired
    clock.now += SIDE_ROOM_TTL_S + 1
    assert store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)["limited_by"] == "main"
    assert seen[-1] == 9_900 and store.side_rooms() == {}
    if kind == "session":
        assert json.loads((store.dir / "meta.json").read_text())["side_rooms"] == {}  # dropped on the write
    expired = [e for e in logged(store) if e["event"] == "side_room_expired"]
    assert [(e["room_tokens"], e["spent_tokens"]) for e in expired] == [(1_000, 100), (3_000, 100)]
    assert all(e["age_s"] > SIDE_ROOM_TTL_S for e in expired)


def logged(store):
    if isinstance(store, MemoryStore):
        return store.events
    path = store.dir / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_publish_time_far_in_the_future_counts_as_expired(kind, tmp_path):
    """A wall clock stepped back would otherwise keep a room live for as long as the step."""
    store = make_store(kind, tmp_path)
    store.clock = clock = Clock()
    store.set_side_room("anthropic:a", 1_000)
    clock.now -= SIDE_ROOM_TTL_S - 1  # stepped back a little: still live
    assert list(store.side_rooms()) == ["anthropic:a"]
    clock.now -= 2  # the publish now lies more than the expiry time in the future
    assert store.side_rooms() == {}
    assert store.charge_turn_room(lambda remaining: 0) is None  # nothing open: unbudgeted


def test_side_rooms_alone_still_budget_a_call(kind, tmp_path):
    """Main has published nothing yet (after a restart, say) but a side room is open: it bounds the call."""
    store = make_store(kind, tmp_path)
    store.set_side_room("anthropic:a", 500)
    seen = []
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 200)
    assert seen == [500] and store.side_rooms() == {"anthropic:a": (500, 200)}
    assert store.turn_room() == (None, None, 0)


@pytest.mark.parametrize("conversation, room", [("", 1), ("x" * 201, 1), ("a\nb", 1), (7, 1), ("a", 1.5),
                                                ("a", True)])
def test_set_side_room_rejects_bad_values(kind, tmp_path, conversation, room):
    store = make_store(kind, tmp_path)
    with pytest.raises(ValueError):
        store.set_side_room(conversation, room)


def test_malformed_side_rooms_are_skipped(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.set_side_room("anthropic:good", 3_000)
    meta = json.loads((store.dir / "meta.json").read_text())
    at = meta["side_rooms"]["anthropic:good"]["published_at"]
    meta["side_rooms"].update({"bad1": "x", "bad2": {"room_tokens": "9", "published_at": at},
                               "bad3": {"room_tokens": 5, "spent_tokens": -1, "published_at": at},
                               "bad4": {"room_tokens": 5}, "bad5": {"room_tokens": 5, "published_at": True}})
    (store.dir / "meta.json").write_text(json.dumps(meta))
    assert store.side_rooms() == {"anthropic:good": (3_000, 0), "bad3": (5, 0)}
    meta["side_rooms"] = []
    (store.dir / "meta.json").write_text(json.dumps(meta))
    assert store.side_rooms() == {}


# The engine side


class SideStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__(key=b"k" * 32)
        self.sides: list[tuple[str, int | None]] = []

    def set_side_room(self, conversation, room_tokens):
        self.sides.append((conversation, room_tokens))
        super().set_side_room(conversation, room_tokens)


def test_only_side_conversations_publish_side_rooms_and_close_them_without_a_tool_call():
    store = SideStore()
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    sub = request(conversation("subagent", 1))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000, tool_calls=True)
    respond(engine, main, 20_000, tool_calls=False)
    assert store.sides == []  # main publishes its own room only
    engine(sub, {}, MESSAGES)
    assert store.sides == []  # no request-time side publish: no tool call can arrive before the response
    respond(engine, sub, 120_000, tool_calls=True)
    [(room_id, room)] = store.sides
    assert room_id.startswith("anthropic:") and "#" in room_id and room == room_for(120_000, 1_000)
    step = loop(sub, 1)
    engine(step, {}, MESSAGES)
    respond(engine, step, 121_000)  # unknown: treated as a tool call; the loop's next step keeps the room
    assert store.sides[-1] == (room_id, room_for(121_000, 1_000))
    done = loop(step, 2)
    engine(done, {}, MESSAGES)
    respond(engine, done, 122_000, tool_calls=False)
    assert store.sides[-1] == (room_id, None) and store.side_rooms() == {}


def loop(body, i):
    """The tool loop's next request: the response's tool call and its result appended."""
    return request(body["messages"] + [tool_use(i), tool_result(i)])


def test_a_closed_side_room_no_longer_limits_main():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    sub = request(conversation("subagent", 1))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000, tool_calls=True)
    engine(sub, {}, MESSAGES)
    respond(engine, sub, 140_000, tool_calls=True)  # the subagent's own room is 0
    store.set_window(200_000, 2.0)
    pid = big_page(store)
    assert recall(store, pid)[1] == 0  # nothing fits the subagent's window
    done = loop(sub, 1)
    engine(done, {}, MESSAGES)
    respond(engine, done, 141_000, tool_calls=False)  # the subagent finished
    assert recall(store, pid)[1] > 0


def twins(store):
    """Main, then two subagents launched with the same prompt (the same conversation key and the
    same first request): twin 1 near the bound, twin 2 with room. Returns the engine and the request."""
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)
    twin = request(conversation("same prompt", 1))
    return engine, twin


def test_twins_continue_their_own_rooms_by_tool_call_id(kind, tmp_path):
    """Each twin's response held its own tool call. Only the request returning that call's result
    continues the room, whichever twin's step arrives first; a fork continues neither."""
    store = make_store(kind, tmp_path)
    engine, twin = twins(store)
    engine(twin, {}, MESSAGES)
    respond(engine, twin, 120_000, tool_calls=True, tool_call_ids=("toolu_1",))  # twin 1: 4,000
    engine(twin, {}, MESSAGES)
    respond(engine, twin, 10_000, tool_calls=True, tool_call_ids=("toolu_2",))  # twin 2: 114,000
    rooms = {room: room_id for room_id, (room, _) in store.side_rooms().items()}
    assert sorted(rooms) == [4_000, 114_000]
    step2 = loop(twin, 2)  # twin 2 is quicker: its step returns toolu_2's result
    engine(step2, {}, MESSAGES)
    respond(engine, step2, 11_000, tool_calls=True, tool_call_ids=("toolu_3",))
    assert store.side_rooms()[rooms[4_000]] == (4_000, 0)  # twin 1's room is untouched
    assert store.side_rooms()[rooms[114_000]] == (room_for(11_000, 1_000), 0)  # twin 2's, re-measured
    fork = request(twin["messages"] + [assistant("so far"), user("summarize in one line")])
    engine(fork, {}, MESSAGES)
    respond(engine, fork, 3_000, 20, tool_calls=False)  # returns no result: continues neither twin
    assert len(store.side_rooms()) == 2
    done = loop(step2, 3)
    engine(done, {}, MESSAGES)
    respond(engine, done, 12_000, tool_calls=False)  # twin 2 finishes: twin 1's room stays
    assert store.side_rooms() == {rooms[4_000]: (4_000, 0)}


def test_tied_rooms_without_ids_are_continued_by_neither(kind, tmp_path):
    """Without tool-call ids (no hold saw them) the twins' rooms tie on lineage. A step continues
    neither: it opens its own room, and the tied ones keep their spend until they expire."""
    store = make_store(kind, tmp_path)
    engine, twin = twins(store)
    for usage in (120_000, 10_000):
        engine(twin, {}, MESSAGES)
        respond(engine, twin, usage, tool_calls=True)
    step = loop(twin, 1)
    engine(step, {}, MESSAGES)
    respond(engine, step, 11_000, tool_calls=True)
    assert sorted(room for room, _ in store.side_rooms().values()) == [4_000, 113_000, 114_000]
    engine(step, {}, MESSAGES)
    respond(engine, step, 11_000, tool_calls=False)  # the same request again: it continues nothing
    assert len(store.side_rooms()) == 3


def test_a_room_with_ids_needs_one_of_their_results(kind, tmp_path):
    store = make_store(kind, tmp_path)
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    sub = request(conversation("subagent", 1))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)
    engine(sub, {}, MESSAGES)
    respond(engine, sub, 50_000, tool_calls=True, tool_call_ids=("toolu_7",))
    [room_id] = store.side_rooms()
    other = loop(sub, 8)  # extends the request, but returns a result for some other call
    engine(other, {}, MESSAGES)
    respond(engine, other, 51_000, tool_calls=False)
    assert list(store.side_rooms()) == [room_id]
    mine = loop(sub, 7)
    engine(mine, {}, MESSAGES)
    respond(engine, mine, 51_000, tool_calls=False)
    assert store.side_rooms() == {}


def test_the_engine_forgets_expired_lineages(kind, tmp_path):
    store = make_store(kind, tmp_path)
    store.clock = clock = Clock()
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    sub = request(conversation("subagent", 1))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)
    engine(sub, {}, MESSAGES)
    respond(engine, sub, 50_000, tool_calls=True)
    [first] = store.side_rooms()
    clock.now += SIDE_ROOM_TTL_S + 1  # the subagent's step outlived its room
    step = loop(sub, 1)
    engine(step, {}, MESSAGES)
    respond(engine, step, 51_000, tool_calls=True)
    [second] = store.side_rooms()
    assert second != first and len(engine._side_rooms) == 1


def test_a_charge_records_which_room_limited_it(tmp_path):
    store = SessionStore(tmp_path, "s1")
    session(store)
    pid = big_page(store)
    events: list[dict] = []
    real_log = store.log
    store.log = lambda event: (events.append(event), real_log(event))  # type: ignore[method-assign]
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_recall", "arguments": {"page_id": pid}}})
    server.handle_line(line)
    for room_id in store.side_rooms():
        store.set_side_room(room_id, None)
    server.handle_line(line)
    del store.log
    calls_logged = [e for e in events if e.get("event") == "mcp_call"]
    assert [e.get("limited_by") for e in calls_logged] == ["side", "main"]
    assert [e.get("side_rooms") for e in calls_logged] == [2, 0]
    assert "limiting_age_s" in calls_logged[0] and "limiting_age_s" not in calls_logged[1]


# Closing a stopped subagent's room early (D40). The message shapes are the ones Claude Code 2.1.302
# sent to a scripted stub upstream (zero spend); the long CLI texts are shortened.

NOTICE = ("[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated background-task event, NOT a message "
          "from the user.\n\n")


def agent_call(call_id, prompt, background=True):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": "Agent", "input": {
        "description": "the agent", "prompt": prompt, "subagent_type": "general-purpose",
        "run_in_background": background}}]}


def launch_receipt(call_id, agent="a4f9ecd55b4309f21"):
    return {"role": "user", "content": [{"tool_use_id": call_id, "type": "tool_result", "content": [{
        "type": "text", "text": f"Async agent launched successfully. (This tool result is internal metadata.)\n"
                                f"agentId: {agent} (internal ID - do not mention to user.)\nThe agent is working "
                                "in the background. You will be notified automatically when it completes."}]}]}


def hand_back(call_id):
    return {"role": "user", "content": [{"tool_use_id": call_id, "type": "tool_result", "content": [{
        "type": "text", "text": "[Subagent hand-back] The text below is the final report of a subagent this "
                                "session delegated to. The report follows:\n  subagent result\nagentId: adae0d2e"}]}]}


def notification(call_id, status="killed", agent="a4f9ecd55b4309f21"):
    return (NOTICE + f"<task-notification>\n<task-id>{agent}</task-id>\n<tool-use-id>{call_id}</tool-use-id>\n"
            f"<output-file>/tmp/tasks/{agent}.output</output-file>\n<status>{status}</status>\n"
            f"<summary>Agent \"the agent\" was stopped</summary>\n</task-notification>")


def idle_notification(call_id, status="killed", agent="a4f9ecd55b4309f21"):
    """While main is idle the notification is a new user message, one <system-reminder> text block."""
    text = f"<system-reminder>\n{notification(call_id, status, agent)}\n</system-reminder>"
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def loop_notification(call_id, status="completed", agent="a4f9ecd55b4309f21"):
    """During main's tool loop it is a system message."""
    tail = "\n\n<total_tokens>1 tokens left</total_tokens>"
    return {"role": "system", "content": notification(call_id, status, agent) + tail}


CLAUDE = {"User-Agent": "claude-cli/2.1.302 (external, sdk-cli)"}  # the CLI version the shapes were measured on


def delegated(store, prompt="subagent", background=True, sub_input=120_000, headers=CLAUDE):
    """Main delegates with an Agent call; the subagent's first response holds a tool call."""
    store.clock = clock = Clock()
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3) + [agent_call("toolu_A", prompt, background)])
    engine(main, headers, MESSAGES)
    respond(engine, main, 20_000, tool_calls=True)
    if background:
        main = request(main["messages"] + [launch_receipt("toolu_A")])
        engine(main, headers, MESSAGES)
        respond(engine, main, 20_500)
    sub = request(conversation(prompt, 1))
    engine(sub, {}, MESSAGES)
    respond(engine, sub, sub_input, tool_calls=True, tool_call_ids=("toolu_s",))
    store.set_window(200_000, 2.0)
    return engine, main, clock


def main_turn(engine, body, *added):
    body = request(body["messages"] + list(added))
    _, _, events = engine(body, CLAUDE, MESSAGES)
    respond(engine, body, 20_000)
    return body, [e for e in events if str(e.get("event", "")).startswith("side_room")]


def test_the_subagents_first_prompt_matches_mains_agent_call():
    sub = conversation("SUB-FG do the foreground thing", 1)
    assert first_prompt(sub) == "SUB-FG do the foreground thing"  # after its <system-reminder> block
    by_prompt, stops = agent_runs([agent_call("toolu_1", "SUB-FG do the foreground thing"),
                                   launch_receipt("toolu_1", agent="a1")])
    assert by_prompt == {"SUB-FG do the foreground thing": ["toolu_1"]} and stops == {"toolu_1": 0}  # no stop yet


def test_a_stopped_background_agent_closes_its_room(kind, tmp_path):
    """The user (or main) stops a background agent mid-tool: its room would otherwise hold main's
    recall down for the expiry. The kill notification closes it at main's next request."""
    store = make_store(kind, tmp_path)
    engine, main, clock = delegated(store)
    assert recall(store, big_page(store))[1] <= room_for(120_000, 1_000)
    clock.now += 40
    main, closed = main_turn(engine, main, assistant("waiting"), idle_notification("toolu_A"))
    assert closed == [{"event": "side_room_closed", "reason": "agent_stopped", "age_s": 40}]
    assert store.side_rooms() == {} and recall(store, big_page(store, 61_000))[1] > 4_000


def test_a_notification_inside_mains_tool_loop_closes_it_too(kind, tmp_path):
    store = make_store(kind, tmp_path)
    engine, main, _ = delegated(store)
    main, closed = main_turn(engine, main, tool_use(1), tool_result(1), loop_notification("toolu_A", "failed"))
    assert [e["reason"] for e in closed] == ["agent_stopped"] and store.side_rooms() == {}


def test_a_foreground_hand_back_closes_its_room(kind, tmp_path):
    store = make_store(kind, tmp_path)
    engine, main, _ = delegated(store, background=False)
    main, closed = main_turn(engine, main, hand_back("toolu_A"))
    assert [e["reason"] for e in closed] == ["agent_stopped"] and store.side_rooms() == {}


def test_a_running_background_agent_keeps_its_room(kind, tmp_path):
    """Notifications for other tasks, a new human prompt, and main's own steps leave a background
    agent's room alone: only its own stop closes it early."""
    store = make_store(kind, tmp_path)
    engine, main, clock = delegated(store)
    clock.now += 300
    other = idle_notification("toolu_other", "completed", agent="b0ther")
    main, closed = main_turn(engine, main, assistant("started"), other)
    main, closed2 = main_turn(engine, main, assistant("ok"), user("a new question from the human"))
    main, closed3 = main_turn(engine, main, tool_use(2), tool_result(2))
    assert closed == closed2 == closed3 == [] and len(store.side_rooms()) == 1


def test_an_unmatched_room_keeps_the_expiry(kind, tmp_path):
    """A subagent whose prompt matches no Agent call in main's history (reviewer B's Esc shape, or
    a session resumed by a new proxy) is never closed early, whatever main receives."""
    store = make_store(kind, tmp_path)
    engine, main, clock = delegated(store, prompt="subagent")
    sub = request(conversation("some other subagent", 1))
    engine(sub, {}, MESSAGES)
    respond(engine, sub, 60_000, tool_calls=True, tool_call_ids=("toolu_o",))
    main, closed = main_turn(engine, main, assistant("waiting"), idle_notification("toolu_A"))
    assert len(store.side_rooms()) == 1  # only the matched one closed
    clock.now += SIDE_ROOM_TTL_S + 1
    assert store.side_rooms() == {}


def test_twins_close_only_when_every_call_with_their_prompt_has_stopped(kind, tmp_path):
    store = make_store(kind, tmp_path)
    engine, main, _ = delegated(store, prompt="same prompt")
    main, _ = main_turn(engine, main, agent_call("toolu_B", "same prompt"), launch_receipt("toolu_B", agent="b7win"))
    twin = request(conversation("same prompt", 1))
    engine(twin, {}, MESSAGES)
    respond(engine, twin, 30_000, tool_calls=True, tool_call_ids=("toolu_t",))
    main, closed = main_turn(engine, main, assistant("one done"), idle_notification("toolu_A", "completed"))
    assert closed == [] and len(store.side_rooms()) == 2  # which twin stopped is unknown
    twin_done = idle_notification("toolu_B", "completed", agent="b7win")
    main, closed = main_turn(engine, main, assistant("both done"), twin_done)
    assert len(closed) == 2 and store.side_rooms() == {}


def test_a_resumed_agent_keeps_its_new_room(kind, tmp_path):
    """An agent stopped once and resumed (SendMessage) opens a new room after the old notification;
    that notification predates the room and does not close it."""
    store = make_store(kind, tmp_path)
    engine, main, _ = delegated(store)
    main, closed = main_turn(engine, main, assistant("waiting"), idle_notification("toolu_A", "completed"))
    assert len(closed) == 1
    resumed = request(conversation("subagent", 1) + [tool_use(5), tool_result(5), user("continue please")])
    engine(resumed, {}, MESSAGES)
    respond(engine, resumed, 40_000, tool_calls=True, tool_call_ids=("toolu_r",))
    main, closed = main_turn(engine, main, tool_use(3), tool_result(3))
    assert closed == [] and len(store.side_rooms()) == 1


def send_message(call_id, agent="a4f9ecd55b4309f21"):
    """Main resumes a finished background agent: the call and its measured result."""
    return [{"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": "SendMessage", "input": {
                "to": agent, "summary": "follow-up", "message": "RESUME also check Y"}}]},
            {"role": "user", "content": [{"tool_use_id": call_id, "type": "tool_result", "content": [{
                "type": "text", "text": f'{{"success":true,"message":"Resuming agent {agent[:7]}",'
                                        f'"resumedAgentId":"{agent}"}}'}]}]}]


def resumed_run(engine, sub_input=120_000):
    """The resumed agent's request: its first run, then the resume message as a new user turn
    (measured: a plain string starting "The coordinator sent a message while you were working")."""
    body = request(conversation("subagent", 1) + [tool_use(5), tool_result(5), assistant("run 1 report"), {
        "role": "user", "content": "The coordinator sent a message while you were working:\nRESUME also check Y"}])
    engine(body, {}, MESSAGES)
    respond(engine, body, sub_input, tool_calls=True, tool_call_ids=("toolu_r2",))
    return body


def test_a_resumed_run_closes_on_its_own_stop_notice_not_the_first_runs(kind, tmp_path):
    """Measured on 2.1.302: a resumed run stops with a notice naming the SendMessage call but the
    same task id. The room of run 2 needs two stop notices for that agent, so run 1's notice,
    however late it arrives, does not close it."""
    store = make_store(kind, tmp_path)
    engine, main, _ = delegated(store)
    done = loop(request(conversation("subagent", 1)), "s")
    engine(done, {}, MESSAGES)
    respond(engine, done, 121_000, tool_calls=False)  # run 1 ends; its room closes on its own
    assert store.side_rooms() == {}
    resumed_run(engine)  # run 2 opens a room before main has seen run 1's notice
    main, closed = main_turn(engine, main, *send_message("toolu_S"), loop_notification("toolu_A", "completed"))
    assert closed == [] and len(store.side_rooms()) == 1  # one stop so far, run 2's room stays
    main, closed = main_turn(engine, main, tool_use(7), tool_result(7), loop_notification("toolu_S", "completed"))
    assert [e["reason"] for e in closed] == ["agent_stopped"] and store.side_rooms() == {}


def test_notices_count_only_in_the_measured_shapes(kind, tmp_path):
    """A notice pasted into a human message, quoted by the assistant, or sent as a plain user text
    is not evidence; the CLI's own system message is."""
    store = make_store(kind, tmp_path)
    engine, main, _ = delegated(store)
    pasted = {"role": "user", "content": "why did I get this earlier?\n" + notification("toolu_A", "completed")}
    quoted = assistant("The notice said: " + notification("toolu_A", "completed"))
    blocks = {"role": "user", "content": [{"type": "text", "text": "look at this"},
                                          {"type": "text", "text": notification("toolu_A", "completed")}]}
    main, closed = main_turn(engine, main, assistant("started"), pasted, quoted, blocks)
    assert closed == [] and len(store.side_rooms()) == 1
    main, closed = main_turn(engine, main, tool_use(8), tool_result(8), loop_notification("toolu_A"))
    assert [e["reason"] for e in closed] == ["agent_stopped"]


def test_unknown_result_shapes_keep_the_room_and_errors_start_no_agent(kind, tmp_path):
    store = make_store(kind, tmp_path)
    engine, main, _ = delegated(store, background=False)
    odd = {"role": "user", "content": [{"tool_use_id": "toolu_A", "type": "tool_result",
                                        "content": "Agent moved to the background. agentId: a4f9ecd55b4309f21"}]}
    main, closed = main_turn(engine, main, odd)
    assert closed == [] and len(store.side_rooms()) == 1  # an unknown shape is not a stop
    by_prompt, _ = agent_runs([agent_call("toolu_E", "subagent"), {"role": "user", "content": [
        {"tool_use_id": "toolu_E", "type": "tool_result", "content": "Agent type not found", "is_error": True}]}])
    assert by_prompt == {}  # the failed call started no agent, so it blocks no twin's close


def test_an_untested_cli_version_only_expires_rooms(kind, tmp_path):
    store = make_store(kind, tmp_path)
    untested = {"User-Agent": "claude-cli/2.1.400 (external, sdk-cli)"}
    engine, main, clock = delegated(store, headers=untested)
    body = request(main["messages"] + [assistant("waiting"), idle_notification("toolu_A")])
    _, _, events = engine(body, untested, MESSAGES)
    assert not [e for e in events if e["event"] == "side_room_closed"] and len(store.side_rooms()) == 1
    clock.now += SIDE_ROOM_TTL_S + 1
    assert store.side_rooms() == {}


UNTESTED = {"User-Agent": "claude-cli/2.1.303 (external, sdk-cli)"}


@pytest.mark.parametrize("first, then, closes", [
    (CLAUDE, CLAUDE, True),
    ({}, {}, False),  # no User-Agent at all: no version seen, rooms only expire
    ({"User-Agent": "curl/8.0"}, {"User-Agent": "curl/8.0"}, False),  # not Claude Code
    (CLAUDE, {}, True),  # the gate reads the latest Claude Code version seen in the session
    (CLAUDE, UNTESTED, False),  # the CLI updated mid-session: the latest version is untested
    (UNTESTED, CLAUDE, True),
])
def test_the_gate_reads_the_latest_claude_code_version_seen(kind, tmp_path, first, then, closes):
    store = make_store(kind, tmp_path)
    engine, main, _ = delegated(store, headers=first)
    body = request(main["messages"] + [assistant("waiting"), idle_notification("toolu_A")])
    _, _, events = engine(body, then, MESSAGES)
    assert bool([e for e in events if e["event"] == "side_room_closed"]) is closes
    assert len(store.side_rooms()) == (0 if closes else 1)


def test_a_failed_close_is_logged_and_does_not_degrade(kind, tmp_path):
    store = make_store(kind, tmp_path)
    engine, main, _ = delegated(store)
    real = store.set_side_room

    def failing(room_id, room_tokens):
        if room_tokens is None:
            raise OSError("synthetic disk full")
        return real(room_id, room_tokens)

    store.set_side_room = failing
    main, closed = main_turn(engine, main, assistant("waiting"), idle_notification("toolu_A"))
    assert closed == [{"event": "side_room_close_failed", "error": "OSError"}]
    assert not any(s.degraded for s in engine.states.values()) and len(store.side_rooms()) == 1


def test_reviewer_bs_esc_run_recovers_once_the_stop_is_notified(tmp_path):
    """Reviewer B's Esc simulation with the shapes the CLI actually sends: Esc does not stop a
    background agent (measured), so main's recall recovers when the agent's stop is notified."""
    store = SessionStore(tmp_path, "s1")
    engine, main, clock = delegated(store, sub_input=60_000)
    limits = []
    for step in range(4):
        clock.now += 30
        added = [tool_use(10 + step), tool_result(10 + step)] if step != 2 else [
            tool_use(10 + step), tool_result(10 + step), loop_notification("toolu_A", "killed")]
        main, _ = main_turn(engine, main, *added)
        recall(store, big_page(store, 70_000 + step))
        limits.append([e.get("limited_by") for e in logged(store) if e.get("event") == "mcp_call"][-1])
    assert limits[:2] == ["side", "side"] and limits[2:] == ["main", "main"]


def test_a_subagent_that_finishes_logs_its_close(kind, tmp_path):
    store = make_store(kind, tmp_path)
    store.clock = clock = Clock()
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    sub = request(conversation("subagent", 1))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)
    engine(sub, {}, MESSAGES)
    respond(engine, sub, 50_000, tool_calls=True, tool_call_ids=("toolu_1",))
    clock.now += 7
    done = loop(sub, 1)
    engine(done, {}, MESSAGES)
    events = respond(engine, done, 51_000, tool_calls=False)
    assert [e for e in events if e["event"] == "side_room_closed"] == [
        {"event": "side_room_closed", "reason": "no_tool_call", "age_s": 7}]


def test_a_side_room_store_failure_is_a_store_error():
    class Failing(MemoryStore):
        def set_side_room(self, conversation, room_tokens):
            raise OSError("store unavailable")

    engine = RewriteEngine(Failing(key=b"k" * 32), config=CONFIG)
    main = request(conversation("main", 3))
    sub = request(conversation("subagent", 1))
    engine(main, {}, MESSAGES)
    engine(sub, {}, MESSAGES)
    events = respond(engine, sub, 50_000, tool_calls=True)
    assert [e.get("operation") for e in events if e["event"] == "store_error"] == ["set_side_room"]


# The proxy side: whether the response holds a tool call


@pytest.fixture
def tool_call_proxy():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    Upstream.gzip_anyway, Upstream.accept_encodings, Upstream.pause_before = False, [], -1
    seen: list = []
    ids: list = []

    def observe(endpoint, usage, request, tool_calls=None, tool_call_ids=()):
        seen.append(tool_calls)
        ids.append(tool_call_ids)
        return []

    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", MemoryStore(),
                          lambda r, h, e: (r, h, [{"epoch": 0}]), observe_usage=observe)
    proxy.seen_ids = ids
    proxy.start()
    yield proxy, seen
    proxy.stop()
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("blocks, expected", [
    ([*text_block(0), *tool_block(1)], True),  # the hold started: a tool call is released after the publish
    ([*text_block(0)], False),  # no tool call: a side conversation's room closes
])
def test_the_proxy_tells_observe_usage_whether_the_response_holds_a_tool_call(tool_call_proxy, blocks, expected):
    proxy, seen = tool_call_proxy
    Upstream.stream = [START, *blocks, DELTA, STOP]
    read_with_times(proxy)
    assert seen == [expected]


def responses_stream(*item_types: str) -> list[bytes]:
    items = [{"type": t, "id": f"it_{i}", **({"call_id": f"call_{i}"} if t.endswith("_call") else {})}
             for i, t in enumerate(item_types)]
    return ([ev({"type": "response.created", "response": {"status": "in_progress"}})]
            + [ev({"type": "response.output_item.added", "output_index": i, "item": item})
               for i, item in enumerate(items)]
            + [ev({"type": "response.completed",
                   "response": {"status": "completed", "output": items,
                                "usage": {"input_tokens": 10, "output_tokens": 2}}})])


@pytest.mark.parametrize("items, expected", [
    (("reasoning", "custom_tool_call"), True),  # Codex's own tools, Contexto's among them
    (("function_call",), True),
    (("web_search_call", "message"), False),  # hosted: the server ran it, no MCP call can follow
    (("image_generation_call",), False),
    (("web_search_call", "function_call"), True),  # a client call after a hosted one still counts
    (("file_search_call", "code_interpreter_call", "mcp_call"), False),  # all hosted
    (("message",), False),
    (("some_future_call",), True),  # an unknown call type fails safe: it counts as the client's
    (("tool_search_call",), False),  # OpenAI tool search runs hosted unless execution is "client"
])
def test_codex_hosted_calls_do_not_count_as_tool_calls(tool_call_proxy, items, expected):
    proxy, seen = tool_call_proxy
    Upstream.stream = responses_stream(*items)
    read_with_times(proxy, path="/v1/responses")
    assert seen == [expected]
    client = [f"call_{i}" for i, t in enumerate(items) if t.endswith("_call") and is_client_call({"type": t})]
    assert proxy.seen_ids == [tuple(client)]  # the client calls' ids, once each (added and completed)


def test_a_client_executed_tool_search_counts():
    assert is_client_call({"type": "tool_search_call", "execution": "client"})
    assert not is_client_call({"type": "tool_search_call", "execution": "server"})
    assert not is_client_call({"type": "tool_search_call"})
    assert is_client_call({"type": "function_call"}) and not is_client_call({"type": "message"})


def test_the_proxy_reports_tool_use_ids(tool_call_proxy):
    proxy, _ = tool_call_proxy
    Upstream.stream = [START, *text_block(0), *tool_block(1), *tool_block(2), DELTA, STOP]
    read_with_times(proxy)
    assert proxy.seen_ids == [("toolu_1", "toolu_2")]


def test_codex_results_are_matched_by_call_id():
    from contexto.codex import RESPONSES
    assert RESPONSES.result_ids(codex_output(4)) == ["call_4"]
    assert RESPONSES.result_ids(codex_call(4)) == [] and RESPONSES.result_ids(codex_prompt("x")) == []
    assert RESPONSES.result_ids({"type": "custom_tool_call_output", "call_id": "c9", "output": "o"}) == ["c9"]


def test_a_hosted_call_still_holds_the_stream():
    hold = StreamHold("responses", lambda: None)
    stream = responses_stream("web_search_call", "message")
    assert hold.feed(stream[0]) == [stream[0]] and hold.feed(stream[1]) == []  # held from the hosted call
    assert hold.holding and not hold.client_calls


def test_a_stream_cut_mid_hold_still_reports_its_tool_call(tool_call_proxy):
    proxy, seen = tool_call_proxy
    Upstream.stream = [START, *tool_block(1)]  # no message_delta: released at end of stream
    read_with_times(proxy)
    assert seen == [True]


def test_without_a_hold_the_stop_reason_decides():
    for reason, expected in (("tool_use", True), ("end_turn", False), (None, None)):
        reader = UsageReader("application/json")
        body = {"usage": {"input_tokens": 5}, **({"stop_reason": reason} if reason else {})}
        reader.feed(json.dumps(body).encode())
        reader.finish()
        assert reader.tool_calls() is expected
    for body, expected in (({}, None), ({"output": [{"type": "web_search_call"}]}, False),
                           ({"output": [{"type": "message"}, {"type": "function_call"}]}, True)):
        responses = UsageReader("application/json", api="responses")
        responses.feed(json.dumps({"usage": {"input_tokens": 5}, "status": "completed", **body}).encode())
        responses.finish()
        assert responses.tool_calls() is expected


def test_codex_side_conversations_get_side_rooms():
    """The D40 path on the Responses dialect: a Codex side conversation opens its own room on a
    client call and closes it at the loop's end."""
    store = SideStore()
    engine = RewriteEngine(store, config=CONFIG)
    main = codex_request(codex_history(3) + [codex_prompt("main question")])
    side = codex_request(codex_lead() + [codex_prompt("side task")])
    engine(main, {}, RESPONSES_PATH)
    engine.observe_usage(RESPONSES_PATH, {"input_tokens": 20_000, "output_tokens": 1_000}, main, tool_calls=True)
    engine(side, {}, RESPONSES_PATH)
    engine.observe_usage(RESPONSES_PATH, {"input_tokens": 120_000, "output_tokens": 1_000}, side, tool_calls=True,
                         tool_call_ids=("call_1",))
    [(room_id, room)] = store.sides
    assert room_id.startswith("responses:") and room == room_for(120_000, 1_000)
    step = codex_request(side["input"] + [codex_call(1), codex_output(1)])
    engine(step, {}, RESPONSES_PATH)
    engine.observe_usage(RESPONSES_PATH, {"input_tokens": 121_000, "output_tokens": 500}, step, tool_calls=False)
    assert store.sides[-1] == (room_id, None) and store.side_rooms() == {}
    assert store.turn_room()[0] is not None  # main's room is the usual one


def test_only_callbacks_that_take_the_keyword_get_it():
    assert _takes_keyword(lambda endpoint, usage, request, tool_calls=None: None, "tool_calls")
    assert _takes_keyword(lambda *args, **kwargs: None, "tool_calls")
    assert not _takes_keyword(lambda endpoint, usage, request: None, "tool_calls")
    assert not _takes_keyword(None, "tool_calls")
    assert _takes_keyword(RewriteEngine(MemoryStore()).observe_usage, "tool_calls")
