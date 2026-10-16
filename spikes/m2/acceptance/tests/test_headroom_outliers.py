"""Headroom candidates (packet A33), on synthetic Messages sessions only."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import headroom_outliers as ho  # noqa: E402
from contexto import rewrite  # noqa: E402
from contexto.rewrite import Config, EpochState, RewriteEngine  # noqa: E402
from contexto.store.memory import MemoryStore  # noqa: E402

WORDS = "synthetic words "


def body(messages: list[dict]) -> dict:
    return {"model": "m", "system": "head", "tools": [], "max_tokens": 64, "stream": True, "messages": messages}


def growths(sizes: list[int], rule: dict, config: Config = Config(bound=150_000, **ho.BEFORE_RULE_17)) -> list[int]:
    """max_turn_growth after each turn start of a session whose turns carry ``sizes`` words."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=config)
    messages: list[dict] = []
    out = []
    with ho.headroom_rule(**rule):
        for t, size in enumerate(sizes):
            messages = messages + [{"role": "user", "content": f"turn {t} " + WORDS * size}]
            engine(body(messages), {}, "/v1/messages")
            out.append(next(iter(engine.states.values())).max_turn_growth)
            messages = messages + [{"role": "assistant", "content": [{"type": "text", "text": f"reply {t}"}]}]
    return out


SIZES = [200, 200, 6_000, 200, 200, 200, 200, 200]  # one outlier turn


def test_today_keeps_the_outlier_for_the_rest_of_the_session():
    g = growths(SIZES, {})
    peak = max(g)
    assert g.index(peak) == 2 and g[-1] == peak  # counted at the outlier's own turn start, then kept


def test_decay_forgets_the_outlier_turn_by_turn():
    g = growths(SIZES, {"decay": 0.5})
    peak = g[2]
    assert g[3] == peak // 2 and g[4] == peak // 4 and g[-1] < peak // 8
    assert max(g) == max(growths(SIZES, {}))  # the outlier itself is still counted when it happens


def test_window_keeps_the_largest_of_the_last_n_turns():
    g = growths(SIZES, {"window": 3})
    peak = g[2]
    assert g[3] == g[4] == peak and g[5] < peak  # gone once it leaves the last 3 turn starts


def test_cap_bounds_headroom_at_a_share_of_the_bound():
    config = Config(bound=100_000, **ho.BEFORE_RULE_17)
    outlier = EpochState(max_turn_growth=10**6)
    assert rewrite.headroom(outlier, config) == 1_250_000
    with ho.headroom_rule(cap=0.25):
        assert rewrite.headroom(outlier, config) == 25_000
        assert rewrite.headroom(EpochState(max_turn_growth=0), config) == config.headroom_floor
    assert rewrite.headroom(outlier, config) == 1_250_000  # the patch ends with the replay


def test_capture_records_each_calls_events_and_turn_start():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config())
    start = [{"role": "user", "content": "hi"}]
    loop = start + [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
    with ho.capture() as calls:
        engine(body(start), {}, "/v1/messages")
        engine(body(loop), {}, "/v1/messages")
    assert [c["start"] for c in calls] == [True, False]
    assert all(any(e["event"] == "request" for e in c["events"]) for c in calls)


def test_the_candidates_are_inert_without_an_outlier():
    steady = [200] * 8
    today = growths(steady, {})
    for rule in ho.CANDIDATES.values():
        g = growths(steady, rule)
        assert [rewrite.headroom(EpochState(max_turn_growth=x), Config()) for x in g] == \
               [rewrite.headroom(EpochState(max_turn_growth=x), Config()) for x in today]


def test_a_retried_turn_start_does_not_decay_twice():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config(bound=150_000, **ho.BEFORE_RULE_17))
    messages: list[dict] = []
    values = []
    with ho.headroom_rule(decay=0.5):
        for t, size in enumerate(SIZES[:5]):
            messages = messages + [{"role": "user", "content": f"turn {t} " + WORDS * size}]
            engine(body(messages), {}, "/v1/messages")
            values.append(next(iter(engine.states.values())).max_turn_growth)
            if t == 3:  # the same turn start sent again (a CLI retry): no new growth, no decay
                engine(body(messages), {}, "/v1/messages")
                assert next(iter(engine.states.values())).max_turn_growth == values[-1]
            messages = messages + [{"role": "assistant", "content": [{"type": "text", "text": f"reply {t}"}]}]
    assert values[3] == values[2] // 2 and values[4] == values[2] // 4  # one decay per new turn start


def test_a_resume_on_the_turn_start_that_wrote_the_record_does_not_decay_again():
    store = MemoryStore(key=b"k" * 32)
    messages: list[dict] = []
    with ho.headroom_rule(decay=0.5):
        first = RewriteEngine(store, config=Config(bound=150_000, **ho.BEFORE_RULE_17))
        for t, size in enumerate(SIZES[:4]):
            messages = messages + [{"role": "user", "content": f"turn {t} " + WORDS * size}]
            first(body(messages), {}, "/v1/messages")
            if t < 3:
                messages = messages + [{"role": "assistant", "content": [{"type": "text", "text": f"reply {t}"}]}]
        written = next(iter(first.states.values())).max_turn_growth
        second = RewriteEngine(store, config=Config(bound=150_000, **ho.BEFORE_RULE_17))  # a restart
        _, _, events = second(body(messages), {}, "/v1/messages")
    assert any(e.get("event") == "epoch_resumed" for e in events)
    assert next(iter(second.states.values())).max_turn_growth == written


def test_the_engines_rule_17_reproduces_the_recommended_row():
    # Rule 17 (contract PR #186) in the engine itself, no patch: A33's decay 0.5 + cap 25% row on A32's
    # seat session with the 149,140-token read (92,000 head, bound 150,000).
    from acceptance import seat_bound as sb

    config = Config(bound=150_000)
    s = sb.summarize(sb.replay(sb.session(92_000, 56, big_read_at=28), config), config)
    assert (s["rotations"], s["window_minimal"], s["bound_too_small"]) == (15, 1, 0)
    assert s["window_budget"][0] == 18_907 and s["usd"] == 7.3125
