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
from contexto.proxy import ContextoProxy, UsageReader, _takes_keyword
from contexto.rewrite import MESSAGES, Config, RewriteEngine
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore

from test_rewrite import conversation, request
from test_stream_hold import DELTA, START, STOP, Upstream, read_with_times, text_block, tool_block

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
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)
    store.set_side_room("anthropic:a", None)  # A's response held no tool call
    store.set_side_room("anthropic:never", None)  # closing an unknown room is a no-op
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)
    assert seen == [0, 10_000] and store.side_rooms() == {}
    store.set_side_room("anthropic:b", 2_000)
    store.set_turn_room("3", 9_000)  # the same main turn keeps side rooms open
    assert store.side_rooms() == {"anthropic:b": (2_000, 0)}
    store.set_turn_room("4", 9_000)  # a new main turn closes them
    assert store.side_rooms() == {}


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
    meta["side_rooms"].update({"bad1": "x", "bad2": {"room_tokens": "9"},
                               "bad3": {"room_tokens": 5, "spent_tokens": -1}})
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
    [(key, room)] = store.sides
    assert key.startswith("anthropic:") and room == room_for(120_000, 1_000)
    respond(engine, sub, 120_000)  # unknown: treated as a tool call
    assert store.sides[-1] == (key, room)
    respond(engine, sub, 120_000, tool_calls=False)
    assert store.sides[-1] == (key, None) and store.side_rooms() == {}


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
    respond(engine, sub, 140_000, tool_calls=False)  # the subagent finished
    assert recall(store, pid)[1] > 0


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

    def observe(endpoint, usage, request, tool_calls=None):
        seen.append(tool_calls)
        return []

    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", MemoryStore(),
                          lambda r, h, e: (r, h, [{"epoch": 0}]), observe_usage=observe)
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
    responses = UsageReader("application/json", api="responses")
    responses.feed(json.dumps({"usage": {"input_tokens": 5}, "status": "completed"}).encode())
    responses.finish()
    assert responses.tool_calls() is None  # Responses tool calls are seen by the hold only


def test_only_callbacks_that_take_the_keyword_get_it():
    assert _takes_keyword(lambda endpoint, usage, request, tool_calls=None: None, "tool_calls")
    assert _takes_keyword(lambda *args, **kwargs: None, "tool_calls")
    assert not _takes_keyword(lambda endpoint, usage, request: None, "tool_calls")
    assert not _takes_keyword(None, "tool_calls")
    assert _takes_keyword(RewriteEngine(MemoryStore()).observe_usage, "tool_calls")
