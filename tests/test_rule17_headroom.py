"""Rule 17 (contract PR #186): headroom that forgets outliers, capped at a share of the bound."""

from __future__ import annotations

import copy
import dataclasses

import pytest

from contexto.rewrite import Config, EpochState, RewriteEngine, headroom
from contexto.store.memory import MemoryStore
from test_rewrite import FILL, assistant, request, tool_result, tool_use, user

MESSAGES = "/v1/messages"


@pytest.mark.parametrize("growth, expected", [
    (1_000, 25_000),  # the floor binds
    (30_000, 37_500),  # the factor binds: ceil(1.25 x 30,000)
    (100_000, 50_000),  # the cap binds: floor(0.25 x 200,000)
])
def test_the_formula(growth, expected):
    assert headroom(EpochState(max_turn_growth=growth), Config(bound=200_000)) == expected


def test_the_floor_wins_over_a_cap_below_it():
    assert headroom(EpochState(max_turn_growth=10**6), Config(bound=64_000)) == 25_000  # cap 16,000 < floor


def test_both_settings_are_in_the_rule_16_configuration():
    fields = dataclasses.asdict(Config())
    assert fields["headroom_cap"] == 0.25 and fields["headroom_decay"] == 0.5


def session() -> list[list[dict]]:
    """Turn-start requests: a small turn, then an outlier turn, then small turns."""
    msgs = [user("p0")]
    out = [msgs]
    for t, text in enumerate([FILL * 20, "x", "x", "x"], start=1):
        msgs = msgs + [assistant(text), user(f"p{t}")]
        out.append(msgs)
    return out


def growth_after(engine: RewriteEngine) -> int:
    return next(iter(engine.states.values())).max_turn_growth


def test_the_decay_applies_once_per_turn_start_that_measures_growth_and_never_on_a_retry():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config(bound=10_000_000))
    values = []
    for i, messages in enumerate(session()):
        engine(request(messages), {}, MESSAGES)
        values.append(growth_after(engine))
        if i == 2:
            engine(request(messages), {}, MESSAGES)  # the same turn start again: a retry
            assert growth_after(engine) == values[-1]
    outlier = values[1]
    assert outlier > 1_000 and values[2] == outlier // 2 and values[3] == outlier // 4
    assert values[4] == outlier // 8


def test_a_resume_on_the_writing_turn_start_keeps_the_value_and_the_next_one_decays():
    store = MemoryStore(key=b"k" * 32)
    config = Config(bound=10_000_000)
    first = RewriteEngine(store, config=config)
    turns = session()
    for messages in turns[:3]:
        first(request(messages), {}, MESSAGES)
    written = growth_after(first)
    second = RewriteEngine(store, config=config)  # a restart on the turn start that wrote the record
    _, _, events = second(request(turns[2]), {}, MESSAGES)
    assert any(e.get("event") == "epoch_resumed" for e in events)
    assert growth_after(second) == written
    second(request(turns[3]), {}, MESSAGES)
    assert growth_after(second) == written // 2


def test_a_record_from_an_engine_without_the_new_settings_does_not_resume():
    store = MemoryStore(key=b"k" * 32)
    first = RewriteEngine(store, config=Config(bound=10_000_000))
    turns = session()
    for messages in turns[:3]:
        first(request(messages), {}, MESSAGES)
    record = {k: v for k, v in copy.deepcopy(store.epoch_record()).items() if k != "version"}
    for key in ("headroom_cap", "headroom_decay"):  # a v0.2.0 engine's configuration
        del record["config"][key]
    store.set_epoch(record)
    _, _, events = RewriteEngine(store, config=Config(bound=10_000_000))(request(turns[3]), {}, MESSAGES)
    reasons = [e["reason"] for e in events if e.get("event") == "epoch_not_resumed"]
    assert reasons == ["settings_changed"]


def test_rule_5_is_unchanged_nothing_rotates_inside_a_tool_loop():
    config = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300)
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=config)
    msgs = [user("p0"), assistant("a0"), user("p1")]
    engine(request(msgs), {}, MESSAGES)
    loop = msgs + [tool_use(1), tool_result(1, FILL * 20)]  # a large tool result inside the loop
    _, _, events = engine(request(loop), {}, MESSAGES)
    names = [e["event"] for e in events]
    assert "rotation" not in names
    assert next(e for e in events if e["event"] == "request").get("over_bound") is True
