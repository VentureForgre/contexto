"""Per-turn recall budget, engine side (M2 lane A packet A9, contract PR #69 at 632aeb2)."""

from __future__ import annotations

from contexto.rewrite import ANTHROPIC, COUNT_TOKENS, MESSAGES, Config, RewriteEngine, turn_id
from contexto.store.memory import MemoryStore

from test_rewrite import assistant, history, request, system, tool_result, tool_use, user

CONFIG = Config(bound=150_000, headroom_floor=25_000)
USAGE = {"input_tokens": 1_000, "cache_read_input_tokens": 60_000, "cache_creation_input_tokens": 4_000,
         "output_tokens": 2_000}


class RoomStore(MemoryStore):
    def __init__(self, fail: bool = False) -> None:
        super().__init__(key=b"k" * 32)
        self.rooms: list[tuple[str, int]] = []
        self.fail = fail

    def set_turn_room(self, turn: str, room_tokens: int) -> None:
        if self.fail:
            raise OSError("store unavailable")
        self.rooms.append((turn, room_tokens))


def test_turn_id_counts_turn_starts_in_the_full_history():
    msgs = history(3) + [user("question")]
    assert turn_id(msgs, ANTHROPIC) == "4"
    loop = msgs + [tool_use(1), tool_result(1)]
    assert turn_id(loop, ANTHROPIC) == "4"  # a tool loop is the same turn
    assert turn_id(loop + [assistant("done"), system("note")], ANTHROPIC) == "4"
    assert turn_id(loop + [assistant("done"), user("next")], ANTHROPIC) == "5"  # a new turn start
    assert turn_id([], ANTHROPIC) == "0"


def test_request_time_publish_uses_the_estimate_without_reserving_max_tokens():
    store = RoomStore()
    engine = RewriteEngine(store, config=CONFIG)
    msgs = history(2) + [user("question")]
    body = request(msgs, max_tokens=128_000)  # Claude Code's real max_tokens must not zero the room
    _, _, events = engine(body, {}, MESSAGES)
    estimate = engine.states[next(iter(engine.states))].last_estimate
    assert store.rooms == [("3", max(0, 150_000 - estimate - 25_000))] and store.rooms[0][1] > 0
    assert events[-1]["event"] == "request"  # nothing logged on success; the request event stays last


def test_response_time_publish_uses_real_input_plus_output():
    store = RoomStore()
    engine = RewriteEngine(store, config=CONFIG)
    msgs = history(2) + [user("question")]
    engine(request(msgs), {}, MESSAGES)
    events = engine.observe_usage(MESSAGES, USAGE, request(msgs))
    assert store.rooms[-1] == ("3", 150_000 - (65_000 + 2_000) - 25_000)
    assert not [e for e in events if e["event"] in ("store_error", "degraded_enter")]


def test_every_request_of_the_turn_publishes_under_the_same_id():
    store = RoomStore()
    engine = RewriteEngine(store, config=CONFIG)
    msgs = history(2) + [user("question")]
    engine(request(msgs), {}, MESSAGES)
    engine.observe_usage(MESSAGES, USAGE, request(msgs))
    loop = msgs + [tool_use(1), tool_result(1)]
    engine(request(loop), {}, MESSAGES)
    engine.observe_usage(MESSAGES, USAGE, request(loop))
    assert [turn for turn, _ in store.rooms] == ["3", "3", "3", "3"]
    nxt = loop + [assistant("done"), user("next")]
    engine(request(nxt), {}, MESSAGES)
    assert store.rooms[-1][0] == "4"


def test_room_is_clamped_at_zero():
    store = RoomStore()
    engine = RewriteEngine(store, config=CONFIG)
    msgs = history(2) + [user("question")]
    engine(request(msgs), {}, MESSAGES)
    engine.observe_usage(MESSAGES, dict(USAGE, cache_read_input_tokens=200_000), request(msgs))
    assert store.rooms[-1][1] == 0


def test_count_tokens_never_publishes():
    store = RoomStore()
    engine = RewriteEngine(store, config=CONFIG)
    msgs = history(2) + [user("question")]
    engine(request(msgs), {}, COUNT_TOKENS)
    assert engine.observe_usage(COUNT_TOKENS, USAGE, request(msgs)) == []
    assert store.rooms == []


def test_a_new_engine_on_the_same_history_uses_the_same_turn_id():
    msgs = history(2) + [user("question")]
    a, b = RoomStore(), RoomStore()
    RewriteEngine(a, config=CONFIG)(request(msgs), {}, MESSAGES)
    RewriteEngine(b, config=CONFIG)(request(msgs), {}, MESSAGES)  # a proxy restart
    assert a.rooms[0][0] == b.rooms[0][0] == "3"


def test_a_store_failure_is_a_rule_14_store_error_and_enters_degraded_mode():
    failing = RewriteEngine(RoomStore(fail=True), config=CONFIG)
    msgs = history(2) + [user("question")]
    _, _, events = failing(request(msgs), {}, MESSAGES)
    errors = [e for e in events if e["event"] == "store_error"]
    assert errors == [{"event": "store_error", "epoch": 0, "operation": "set_turn_room", "error": "OSError"}]
    assert "degraded_enter" in [e["event"] for e in events] and events[-1]["event"] == "request"
    assert next(iter(failing.states.values())).degraded
    # degraded: no further publish attempts until a turn start retries the store
    assert failing.observe_usage(MESSAGES, USAGE, request(msgs)) is not None


def test_the_real_store_records_the_published_room():
    store = MemoryStore(key=b"k" * 32)  # lane B's B11 store API
    engine = RewriteEngine(store, config=CONFIG)
    msgs = history(2) + [user("question")]
    engine(request(msgs), {}, MESSAGES)
    engine.observe_usage(MESSAGES, USAGE, request(msgs))
    turn, room, spent = store.turn_room()
    assert (turn, room, spent) == ("3", 150_000 - (65_000 + 2_000) - 25_000, 0)


def test_responses_dialect():
    from contexto.codex import RESPONSES

    def item(role, text):
        kind = "input_text" if role == "user" else "output_text"
        return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}

    items = [item("user", "first"), item("assistant", "ok"), item("user", "second")]
    store = RoomStore()
    engine = RewriteEngine(store, config=CONFIG)
    body = {"model": "gpt-6-astra", "instructions": "synthetic", "input": items, "stream": True}
    engine(body, {}, "/responses")
    assert turn_id(items, RESPONSES) == "2" and store.rooms[0][0] == "2"
    engine.observe_usage("/responses", {"input_tokens": 40_000, "input_tokens_details": {"cached_tokens": 30_000},
                                        "output_tokens": 1_000}, body)
    assert store.rooms[-1] == ("2", 150_000 - (40_000 + 1_000) - 25_000)  # input already includes cached
