"""Usage is attributed only to the request it measured (contract rule 3): the engine pairs a response's
real input with the bytes it sent for that same request, and a request whose usage never arrived (a
4xx or 5xx, a dropped stream, a response without usage) makes the next estimate a full one."""

from __future__ import annotations

from contexto.rewrite import ANTHROPIC, MESSAGES, Config, RewriteEngine, keyed
from contexto.store.memory import MemoryStore

from test_rewrite import FILL, assistant, history, names, request, tool_result, tool_use, user


def engine_with(config: Config) -> RewriteEngine:
    return RewriteEngine(MemoryStore(key=b"k" * 32), config=config)


def state_of(engine: RewriteEngine, req: dict):
    return engine.states[keyed(ANTHROPIC, req)]


def respond(engine: RewriteEngine, req: dict, rate: int = 3) -> list[dict]:
    """Real usage at exactly ``rate`` bytes per token of what the engine sent for ``req``."""
    return engine.observe_usage(MESSAGES, {"input_tokens": state_of(engine, req).last_sent_bytes // rate}, req)


def test_a_request_without_usage_makes_the_next_turn_start_estimate_a_full_one():
    """R2's large tool result never got usage (a 529). Sized from R1's real input plus the messages
    added since R2, R3 would look small and not rotate; its true size passes ``bound - headroom``."""
    engine = engine_with(Config(bound=40_000, headroom_floor=2_000))
    r1 = request(history(3) + [user("p")])
    engine(r1, {}, MESSAGES)
    respond(engine, r1)
    r2 = request(r1["messages"] + [tool_use(1), tool_result(1, "9" * 90_000)])
    engine(r2, {}, MESSAGES)  # upstream answers 529: no usage
    r3 = request(r2["messages"] + [assistant("done"), user("next")])
    _, _, events = engine(r3, {}, MESSAGES)
    assert "usage_missing" in names(events)
    assert "rotation" in names(events)
    request_event = events[-1]
    assert request_event["estimate"] > 30_000  # the full estimate counts the 30,000-token tool result


def test_usage_missing_is_content_free_and_absent_when_every_response_had_usage():
    engine = engine_with(Config(bound=1_000_000))
    r1 = request(history(2) + [user("p")])
    engine(r1, {}, MESSAGES)
    respond(engine, r1)
    r2 = request(r1["messages"] + [tool_use(1), tool_result(1)])
    _, _, events = engine(r2, {}, MESSAGES)
    assert "usage_missing" not in names(events)
    r3 = request(r2["messages"] + [tool_use(2), tool_result(2)])
    _, _, events = engine(r3, {}, MESSAGES)  # r2's usage never arrived
    missing = [e for e in events if e["event"] == "usage_missing"]
    assert missing == [{"event": "usage_missing", "epoch": 0}]


def test_usage_for_a_request_the_engine_never_transformed_is_not_paired():
    """A transform_error forwards the CLI's original request; its usage measures bytes the engine never
    sent, so the ratio and the last real input stay as they were. The real over_bound still counts."""
    config = Config(bound=10_000, headroom_floor=500)
    engine = engine_with(config)
    r1 = request(history(2) + [user("p")])
    engine(r1, {}, MESSAGES)
    respond(engine, r1)
    ratio, last = engine.estimator.measured_bytes_per_token, state_of(engine, r1).last_input_tokens
    original = request(r1["messages"] + [assistant(f"a {FILL}"), user("never transformed")])
    events = engine.observe_usage(MESSAGES, {"input_tokens": 50_000}, original)
    assert engine.estimator.measured_bytes_per_token == ratio
    assert state_of(engine, r1).last_input_tokens == last
    over = [e for e in events if e["event"] == "over_bound"]
    assert len(over) == 1 and over[0]["source"] == "real" and over[0]["real_tokens"] == 50_000
    assert [e for e in events if e["event"] == "usage_unattributed"] == [
        {"event": "usage_unattributed", "epoch": 0, "messages": len(original["messages"]),
         "expected_messages": None}]
    assert "estimate_error" not in names(events)


def test_interleaved_requests_pair_only_the_newest_transform_with_its_usage():
    """Two requests of one conversation in flight: the engine's state holds Rb's bytes, so Ra's usage is
    skipped and Rb's is used."""
    engine = engine_with(Config(bound=1_000_000))
    ra = request(history(2) + [user("p")])
    rb = request(ra["messages"] + [tool_use(1), tool_result(1)])
    engine(ra, {}, MESSAGES)
    engine(rb, {}, MESSAGES)
    sent_b = state_of(engine, rb).last_sent_bytes
    events = engine.observe_usage(MESSAGES, {"input_tokens": 1_234}, ra)
    assert [e["event"] for e in events] == ["usage_unattributed"]
    assert events[0]["messages"] == len(ra["messages"]) and events[0]["expected_messages"] == len(rb["messages"])
    assert engine.estimator.measured_bytes_per_token is None
    assert state_of(engine, rb).last_input_tokens is None and state_of(engine, rb).head_tokens is None
    engine.observe_usage(MESSAGES, {"input_tokens": 2_000}, rb)
    assert engine.estimator.measured_bytes_per_token == sent_b / 2_000
    assert state_of(engine, rb).last_input_tokens == 2_000
    assert state_of(engine, rb).pending_usage is None


def test_a_second_usage_for_the_same_request_is_not_paired_again():
    engine = engine_with(Config(bound=1_000_000))
    r1 = request(history(2) + [user("p")])
    engine(r1, {}, MESSAGES)
    respond(engine, r1)
    ratio = engine.estimator.measured_bytes_per_token
    events = engine.observe_usage(MESSAGES, {"input_tokens": 99}, r1)
    assert names(events) == ["usage_unattributed"] and engine.estimator.measured_bytes_per_token == ratio


def test_a_degraded_forward_of_the_original_is_paired_with_the_original_bytes():
    """Rule 14's terminal step sends the CLI's request unchanged; its usage measures exactly those bytes."""
    from test_rewrite import FaultyStore
    from contexto.store import PageConflictError

    store = FaultyStore()
    engine = RewriteEngine(store, config=Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200))
    msgs = history(6) + [user("p")]
    engine(request(msgs), {}, MESSAGES)
    assert state_of(engine, request(msgs)).k > 2  # rotated: answer 0 is paged
    store.fail["page_out"] = PageConflictError("synthetic", page_id="pg_x")
    changed = [*msgs[:2], assistant(f"rewritten {FILL}"), *msgs[3:]]  # a paged turn changed: resync must page
    req = request(changed)
    _, _, events = engine(req, {}, MESSAGES)
    assert "fallback_failed" in names(events)
    state = state_of(engine, req)
    events = engine.observe_usage(MESSAGES, {"input_tokens": 1_000}, req)
    assert "usage_unattributed" not in names(events)
    assert engine.estimator.measured_bytes_per_token == state.last_sent_bytes / 1_000
