"""The bound with a start text (D69, contract rule 19): the carried records sent before the first
rotation are counted wherever the engine sizes a request, and counted once.

Each test runs a synthetic session through the real engine and a real ``SessionStore`` twice: with the
records of an earlier session carried, and with the same earlier session's records pinned by a
subagent, which are not carried (the control). The stub upstream reports each request's bytes at a fixed
ratio, as ``test_head_measure_proxy`` does. The numbers the tests measure are printed (``pytest -s``).
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import NamedTuple

import pytest

from contexto.rewrite import MESSAGES, Config, RewriteEngine, compose_preamble
from contexto.tokens import estimate_tokens
from test_project_start import (
    ENDPOINTS,
    RESPONSES,
    body,
    field,
    later_session,
    marks,
    names,
    script,
    state_of,
    usage,
)

# The pin budget is the default 6,000, so a carried section of about 5,500 tokens fits whole.
CONFIG = Config(bound=50_000, headroom_floor=6_000, min_turn_room=3_000)
TURNS = {MESSAGES: 64, RESPONSES: 92}  # a Codex turn of the script is the smaller one
RULE = "keep the changelog current and name the migration in it "
SIZES = {1_000: (10, 150), 5_500: (34, 400)}  # tokens of the carried section: (pins, characters added to each)
# A head of about 4,600 tokens: Claude Code's tool list, Codex's instructions.
TOOLS = [{"name": f"tool_{i}", "description": "a synthetic tool that reads a file " * 20,
          "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}} for i in range(22)]
HEAD = {MESSAGES: {"tools": TOOLS}, RESPONSES: {"instructions": "synthetic base instructions for the agent. " * 380}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTEXTO_RECALL_VARIANT", raising=False)
    return tmp_path / "contexto-home"


def project(home, tmp_path, tokens: int, carry: bool = True):
    pins, added = SIZES[tokens]
    return later_session(home, tmp_path, pins=pins, text=(RULE * 8)[:added], author_main=carry)


class Row(NamedTuple):
    upstream: dict
    events: list[dict]
    usage_events: list[dict]
    real: int  # what the stub upstream counted
    estimate: int  # the engine's estimate of what it sent
    room: int  # the recall room published before the send
    k: int
    margin: int  # D63: what the request's fresh tool results may add to the estimate, taken off its room


class Run(NamedTuple):
    engine: RewriteEngine
    rows: list[Row]
    head: int  # the head the first usage measured
    rotation: dict  # the first rotation: its request's index, F, W, and the preamble's estimate there


def run_session(store, endpoint: str, config: Config, turns: int, ratio: float) -> Run:
    engine = RewriteEngine(store, config=config)
    # What rule 4 reads for F at the first rotation: the pinned text, and an index with no page yet.
    pre = compose_preamble(config, store.pinned_text(config.pin_budget), store.index_text(config.index_budget))
    rows, rotation, head = [], {}, None
    requests = script(endpoint, turns)
    for items in requests:
        sent = body(endpoint, items, **HEAD[endpoint])
        upstream, _, events = engine(sent, {}, endpoint)
        event = events[-1]
        rotated = next((e for e in events if e["event"] == "rotation"), None)
        if rotated and not rotation:
            rotation = {"index": len(rows), "reason": rotated["reason"]}
            if "fixed_overhead" in rotated:  # a rotation at a turn start sizes F and W; D63's in a tool loop does not
                rotation.update({"F": rotated["fixed_overhead"], "W": rotated["window_budget"],
                                 "preamble": engine.estimator.estimate(pre)})
        room = store.turn_room()[1]
        margin = engine.states[next(iter(engine.states))].room_margin
        real = usage(upstream, ratio)
        usage_events = engine.observe_usage(endpoint, real, sent)
        state = engine.states[next(iter(engine.states))]
        if head is None:
            head = state.head_tokens
        rows.append(Row(upstream, events, usage_events, real["input_tokens"],
                        event.get("sent_estimate", event["estimate"]), room, event["k"], margin))
    return Run(engine, rows, head, rotation)


def every_event(run: Run) -> list[str]:
    return [name for row in run.rows for name in names(row.events) + names(row.usage_events)]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("tokens", sorted(SIZES))
def test_every_request_of_a_long_session_stays_under_the_bound_and_its_estimate_counts_the_start_text(
        endpoint, tokens, home, tmp_path):
    ratio = 3.2  # denser than the estimator's seed of 3.5, so the estimate before the first usage runs low
    store = project(home, tmp_path / "carry", tokens)
    text = store.carried_text(CONFIG.pin_budget)
    start = estimate_tokens(text)
    assert 0.9 * tokens <= start <= 1.1 * tokens and "omitted" not in text
    carry = run_session(store, endpoint, CONFIG, TURNS[endpoint], ratio)
    control = run_session(project(home / "control", tmp_path / "control", tokens, carry=False), endpoint, CONFIG,
                          TURNS[endpoint], ratio)
    local = usage(body(endpoint, script(endpoint, TURNS[endpoint])[-1], **HEAD[endpoint]), ratio)["input_tokens"]
    assert local >= 2.4 * CONFIG.bound  # the session is long: its own history is far over the bound
    rotations = every_event(carry).count("rotation")
    assert rotations >= 3
    # The bound: every request forwarded is under it, as estimated before the send and as the upstream counted.
    largest = max(row.real for row in carry.rows)
    assert largest <= CONFIG.bound and max(row.estimate for row in carry.rows) <= CONFIG.bound
    off_path = {"over_bound", "bound_too_small", "window_minimal", "estimate_error", "degraded_enter", "store_error"}
    assert not off_path & set(every_event(carry))
    # Before the first rotation the text is in every request, and in its estimate and its published room.
    first, first_control = carry.rotation["index"], control.rotation["index"]
    assert 0 < first <= first_control  # the turn-start trigger counts it: never later than without it
    real_start = len(text.encode()) / ratio
    for with_text, without in zip(carry.rows[:first], control.rows[:first]):
        assert marks(with_text.upstream) == 1 and with_text.k == 0
        counted = with_text.estimate - without.estimate
        assert 0.9 * real_start <= counted <= 1.1 * real_start
        assert with_text.room == CONFIG.bound - with_text.estimate - CONFIG.headroom_floor - with_text.margin
        assert with_text.margin == without.margin  # the same tool results in both
        assert without.room - with_text.room == counted
    assert all(marks(row.upstream) == 1 for row in carry.rows[first:])  # then the preamble holds it, once
    print(f"\n{endpoint} carried section {start} tokens ({len(text.encode())} bytes): first estimate "
          f"{carry.rows[0].estimate} (control {control.rows[0].estimate}), first room {carry.rows[0].room} "
          f"(control {control.rows[0].room}), first rotation at request {first} (control {first_control}), "
          f"{rotations} rotations, largest request {largest} of {CONFIG.bound} "
          f"(control {max(row.real for row in control.rows)}), local history {local}")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("tokens", sorted(SIZES))
@pytest.mark.parametrize("ratio", [4.0, 3.0])
def test_the_start_text_is_counted_once_in_f_and_w_at_the_first_rotation(endpoint, tokens, ratio, home, tmp_path):
    store = project(home, tmp_path / "carry", tokens)
    text = store.carried_text(CONFIG.pin_budget)
    turns = TURNS[endpoint] // 2  # enough for the first rotation
    # Rule 4's F and W at a turn-start rotation. On Messages this script's tool rounds would have D63 page the
    # earlier turns inside a turn first, a rotation that sizes neither, so paging inside the turn is off here.
    config = replace(CONFIG, in_turn_paging=False) if endpoint == MESSAGES else CONFIG
    carry = run_session(store, endpoint, config, turns, ratio)
    control = run_session(project(home / "control", tmp_path / "control", tokens, carry=False), endpoint, config,
                          turns, ratio)
    with_text, without = carry.rotation, control.rotation
    assert with_text["reason"] == without["reason"] and with_text["index"] > 0
    # The measured head does not hold the start text. The window counts at its byte share of the request, or
    # at the estimate made before the send when that is lower (rule 4, so the head errs high): at 4 bytes a
    # token the share is the lower one and the two heads agree to the rounding; at 3 the seed's estimate is,
    # and the head keeps what that estimate left out of the start text, never less than the control's.
    size = len(text.encode())
    shortfall = math.ceil(size / ratio - size / 3.5) if ratio < 3.5 else 0
    absorbed = carry.head - control.head
    assert -2 <= absorbed <= 1.02 * shortfall + 5  # the text's JSON escapes are bytes too
    # F is the head, the growth of the CLI's carried content and the preamble; the preamble's own difference
    # is the carried section, which the pinned text holds from the first rotation on. Nothing else differs.
    difference = with_text["preamble"] - without["preamble"]
    assert 0.85 * size / ratio <= difference <= 1.05 * size / ratio + 2
    assert abs(with_text["F"] - without["F"] - difference - absorbed) <= 2
    growth = with_text["F"] - carry.head - with_text["preamble"]
    assert 0 <= growth <= 60 and abs(growth - (without["F"] - control.head - without["preamble"])) <= 2
    assert with_text["W"] == CONFIG.bound - CONFIG.headroom_floor - with_text["F"]
    assert without["W"] == CONFIG.bound - CONFIG.headroom_floor - without["F"]
    print(f"\n{endpoint} at {ratio} bytes a token, carried section {estimate_tokens(text)} tokens: head "
          f"{carry.head} (control {control.head}, absorbed {absorbed}, allowed {shortfall}), preamble "
          f"{with_text['preamble']} (control {without['preamble']}), F {with_text['F']} (control {without['F']}), "
          f"W {with_text['W']} (control {without['W']})")


TOO_SMALL = Config(bound=9_000, headroom_floor=1_000, min_turn_room=500)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_bound_too_small_for_the_start_text_and_the_head_behaves_as_a_bound_too_small_does(endpoint, home, tmp_path):
    carry = run_session(project(home, tmp_path / "carry", 5_500), endpoint, TOO_SMALL, 3, 3.5)
    assert all(marks(row.upstream) == 1 for row in carry.rows)  # every request is still forwarded, with the records
    first, second = carry.rows[0], carry.rows[2]  # the first two turn starts
    assert second.k > first.k  # Codex's first request already moves k past its lead items, as without records
    too_small = next(e for e in first.events if e["event"] == "bound_too_small")
    assert too_small["window_budget"] <= 0 and too_small["bound"] == TOO_SMALL.bound
    over = next(e for e in first.events if e["event"] == "over_bound")
    assert over["source"] == "estimate" and over["in_tool_loop"] is False and first.estimate > TOO_SMALL.bound
    # The same bound holds the head alone: it is the carried records that do not fit.
    control = run_session(project(home / "control", tmp_path / "control", 5_500, carry=False), endpoint, TOO_SMALL,
                          3, 3.5)
    assert "bound_too_small" not in every_event(control) and "over_bound" not in names(control.rows[0].events)
    # And the events are those of a session whose head alone does not fit its bound.
    small = Config(bound=4_500, headroom_floor=1_000, min_turn_room=500)
    plain = run_session(project(home / "plain", tmp_path / "plain", 5_500, carry=False), endpoint, small, 3, 3.5)
    engine_events = ("rotation", "page_out", "window_minimal", "bound_too_small", "over_bound", "turn_room_low",
                     "rotation_noop", "request")
    for with_text, without in zip(carry.rows, plain.rows):
        assert ([n for n in names(with_text.events) if n in engine_events]
                == [n for n in names(without.events) if n in engine_events])
    assert "window_minimal" in names(second.events) and "bound_too_small" in names(second.events)
    assert not {"degraded_enter", "store_error", "fallback_failed", "transform_error"} & set(every_event(carry))
    state = state_of(carry.engine, endpoint, script(endpoint, 1)[0])
    assert not state.degraded and len(carry.rows[-1].upstream[field(endpoint)]) > 0


class _Log:
    def log(self, event):
        pass


def notices(run: Run) -> str:
    """What the launcher prints for the engine events of ``run``'s requests."""
    import io

    from contexto.launcher import NoticeLog

    err = io.StringIO()
    log = NoticeLog(_Log(), err)
    for row in run.rows:
        log.log({"event": "request", "transform_events": row.events})
    return err.getvalue()


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_the_bound_notice_names_project_memory_when_carried_records_take_part_of_the_bound(endpoint, home, tmp_path):
    carry = run_session(project(home / "carry", tmp_path / "carry", 5_500), endpoint, TOO_SMALL, 3, 3.5)
    tokens = next(e for e in carry.rows[0].events if e["event"] == "start_text")["tokens"]
    said = [line for line in notices(carry).splitlines() if "too small" in line]
    assert len(said) == 1 and said[0].endswith(
        f"Restart with a larger --bound. Records carried by project memory take about {tokens:,} tokens of the bound; "
        "--no-project-memory turns them off.")
    small = Config(bound=4_500, headroom_floor=1_000, min_turn_room=500)
    plain = run_session(project(home / "plain", tmp_path / "plain", 5_500, carry=False), endpoint, small, 3, 3.5)
    said = [line for line in notices(plain).splitlines() if "too small" in line]
    assert len(said) == 1 and said[0].endswith("Restart with a larger --bound.")  # unchanged without a start text
