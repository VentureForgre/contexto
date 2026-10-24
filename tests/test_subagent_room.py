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
from contexto.rewrite import MESSAGES, Config, RewriteEngine
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
    assert store.charge_turn_room(lambda remaining: seen.append(remaining) or 0) == "side"
    store.set_side_room("anthropic:a", None)  # A's response held no tool call
    store.set_side_room("anthropic:never", None)  # closing an unknown room is a no-op
    assert store.charge_turn_room(lambda remaining: seen.append(remaining) or 0) == "main"
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
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 100)
    assert seen == [1_000]  # A still bounds the call just inside its time
    clock.now += 2
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)
    assert seen[-1] == 2_900 and store.side_rooms() == {"anthropic:b": (3_000, 100)}  # A expired
    clock.now += SIDE_ROOM_TTL_S + 1
    assert store.charge_turn_room(lambda remaining: seen.append(remaining) or 0) == "main"
    assert seen[-1] == 9_900 and store.side_rooms() == {}
    if kind == "session":
        assert json.loads((store.dir / "meta.json").read_text())["side_rooms"] == {}  # dropped on the write


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
    assert [e.get("limited_by") for e in events if e.get("event") == "mcp_call"] == ["side", "main"]


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
])
def test_codex_hosted_calls_do_not_count_as_tool_calls(tool_call_proxy, items, expected):
    proxy, seen = tool_call_proxy
    Upstream.stream = responses_stream(*items)
    read_with_times(proxy, path="/v1/responses")
    assert seen == [expected]
    client = [f"call_{i}" for i, t in enumerate(items) if t.endswith("_call") and is_client_call({"type": t})]
    assert proxy.seen_ids == [tuple(client)]  # the client calls' ids, once each (added and completed)


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
