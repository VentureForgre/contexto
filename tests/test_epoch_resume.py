"""Rule 16 (contract PR #176): a new engine on an existing session resumes the main conversation's epoch.

The store side is lane B's B31 (``set_epoch`` / ``epoch_record``); ``EpochStore`` stands in for it here
with the agreed interface: the store stamps ``version`` and hands back a copy.
"""

from __future__ import annotations

import copy
import json

import pytest

from contexto.canonical import canonical_bytes
from contexto.codex import RESPONSES
from contexto.rewrite import Config, RewriteEngine, anchor_hash, message_hash
from contexto.store.memory import MemoryStore

import test_codex as codex
from test_rewrite import FILL, history, request, tool_result, tool_use, user

CONFIG = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300)
MESSAGES = "/v1/messages"


class EpochStore(MemoryStore):
    def __init__(self, record: dict | None = None, fail_write: bool = False, fail_read: bool = False,
                 fail_pages: bool = False) -> None:
        super().__init__(key=b"k" * 32)
        self.record = record
        self.writes: list[dict] = []
        self.fail_write, self.fail_read, self.fail_pages = fail_write, fail_read, fail_pages

    def set_epoch(self, record: dict) -> None:
        if self.fail_write:
            self.record = None  # B31: a failed write leaves no usable record
            raise OSError("disk full")
        self.writes.append(copy.deepcopy(record))
        self.record = {**copy.deepcopy(record), "version": 1}

    def epoch_record(self) -> dict | None:
        if self.fail_read:
            raise OSError("unreadable")
        return {k: v for k, v in copy.deepcopy(self.record).items() if k != "version"} if self.record else None

    def page_out(self, turns, first_turn):
        if self.fail_pages:
            raise OSError("store unavailable")
        return super().page_out(turns, first_turn)


def turns(n: int) -> list[dict]:
    """Claude Code requests at n successive turn starts, each an append to the one before."""
    return [history(t) + [user(f"prompt {t} {FILL}")] for t in range(1, n + 1)]


def codex_turns(n: int) -> list[dict]:
    return [codex.history(t) + [codex.prompt(f"prompt {t} {FILL}")] for t in range(1, n + 1)]


def send(engine: RewriteEngine, messages: list[dict], endpoint: str = MESSAGES, **extra) -> tuple[bytes, list[dict]]:
    body = codex.request(messages, **extra) if endpoint == "/responses" else request(messages, **extra)
    upstream, _, events = engine(body, {}, endpoint)
    return canonical_bytes(upstream), events


def names(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


def by_name(events: list[dict], name: str) -> dict:
    return next(e for e in events if e["event"] == name)


@pytest.mark.parametrize("endpoint, sequence", [(MESSAGES, turns(9)), ("/responses", codex_turns(9))])
def test_a_restarted_engine_sends_the_same_bytes_as_one_that_never_stopped(endpoint, sequence):
    config = CONFIG if endpoint == MESSAGES else Config(bound=8_000, headroom_floor=1_000, pin_budget=200,
                                                        index_budget=200, min_turn_room=500)
    steady = RewriteEngine(EpochStore(), config=config)
    first, second = EpochStore(), None
    before = RewriteEngine(first, config=config)
    for messages in sequence[:-1]:
        send(steady, messages, endpoint)
        send(before, messages, endpoint)
    assert steady.states and any(s.epoch > 0 for s in steady.states.values()), "the run must have rotated"
    second = RewriteEngine(first, config=config)  # a restart: a new engine on the same store
    expected, _ = send(steady, sequence[-1], endpoint)
    got, events = send(second, sequence[-1], endpoint)
    assert got == expected
    resumed = by_name(events, "epoch_resumed")
    state = next(iter(second.states.values()))
    assert resumed["epoch"] >= 1 and resumed["k"] > 0 and state.epoch == next(iter(steady.states.values())).epoch


def test_the_record_holds_exactly_the_agreed_fields():
    store = EpochStore()
    engine = RewriteEngine(store, config=CONFIG)
    for messages in turns(6):
        send(engine, messages)
    record = store.writes[-1]
    assert sorted(record) == sorted([
        "epoch", "k", "strip_upto", "turns_paged", "max_turn_growth", "last_turn_count", "carried_text_streak",
        "window_minimal_count", "last_turn_start_n", "head_tokens", "initial_carried", "last_carried_text_count",
        "turn_room_low_epoch", "window_budget", "pinned_text", "index_text", "preamble_sha256", "head_fingerprint",
        "dialect", "conversation", "model", "paged_carried", "config", "anchor"])
    last = turns(6)[-1]
    assert record["anchor"] == {"n": len(last), "hash": anchor_hash([message_hash(m) for m in last])}
    assert record["dialect"] == "anthropic" and record["conversation"].startswith("anthropic:")
    assert record["model"] == "claude-haiku-5-5" and isinstance(record["paged_carried"], list)
    assert json.loads(json.dumps(record)) == record  # JSON as is


def test_writes_happen_at_turn_starts_only():
    store = EpochStore()
    engine = RewriteEngine(store, config=CONFIG)
    msgs = turns(2)[-1]
    send(engine, msgs)
    assert len(store.writes) == 1
    send(engine, msgs + [tool_use(1)] + [tool_result(1)])  # inside the tool loop
    assert len(store.writes) == 1


def test_the_first_estimate_after_a_resume_is_a_full_estimate():
    # Request bytes are the same after a resume; the first rule 4 estimate is not, because the previous
    # request's real input (last_input_tokens) is per-request and not recorded (intake's note on #176).
    sequence = turns(7)
    steady, store = RewriteEngine(EpochStore(), config=CONFIG), EpochStore()
    before = RewriteEngine(store, config=CONFIG)
    for messages in sequence[:-1]:
        for engine in (steady, before):
            _, events = send(engine, messages)
            estimate = by_name(events, "request")["estimate"]
            usage = {"input_tokens": 2 * estimate, "output_tokens": 10}  # real input well above the estimate
            engine.observe_usage(MESSAGES, usage, request(messages))
    after = RewriteEngine(store, config=CONFIG)
    steady_bytes, steady_events = send(steady, sequence[-1])
    resumed_bytes, resumed_events = send(after, sequence[-1])
    assert "epoch_resumed" in names(resumed_events)
    assert by_name(steady_events, "request")["estimate"] != by_name(resumed_events, "request")["estimate"]


def record_after(sequence: list[dict], config: Config = CONFIG) -> dict:
    store = EpochStore()
    engine = RewriteEngine(store, config=config)
    for messages in sequence:
        send(engine, messages)
    return copy.deepcopy(store.writes[-1])


@pytest.mark.parametrize("change, reason", [
    (lambda r, m: (None, m, CONFIG, {}), "no_record"),
    (lambda r, m: ({**r, "dialect": "other"}, m, CONFIG, {}), "dialect_changed"),
    (lambda r, m: (r, m, CONFIG, {"model": "another-model"}), "model_changed"),
    (lambda r, m: (r, m, Config(bound=4_001, headroom_floor=500, pin_budget=200, index_budget=200,
                                min_turn_room=300), {}), "settings_changed"),
    (lambda r, m: ({**r, "preamble_sha256": "0" * 64}, m, CONFIG, {}), "preamble_changed"),
    (lambda r, m: (r, m, CONFIG, {"system": [{"type": "text", "text": "another head"}]}), "head_changed"),
    (lambda r, m: ({**r, "anchor": {**r["anchor"], "n": len(m) + 5}}, m, CONFIG, {}), "history_shorter"),
    (lambda r, m: ({**r, "anchor": {**r["anchor"], "hash": "f" * 64}}, m, CONFIG, {}), "history_diverged"),
    (lambda r, m: ({**r, "conversation": "anthropic:" + "a" * 64}, m, CONFIG, {}), "other_conversation"),
    (lambda r, m: ({**r, "k": "seven"}, m, CONFIG, {}), "unreadable"),
])
def test_each_failed_check_starts_from_epoch_zero_with_its_reason(change, reason):
    sequence = turns(7)
    record, messages, config, extra = change(record_after(sequence[:-1]), sequence[-1])
    engine = RewriteEngine(EpochStore(record), config=config)
    _, events = send(engine, messages, **extra)
    assert by_name(events, "epoch_not_resumed") == {"event": "epoch_not_resumed", "epoch": 0, "reason": reason}
    assert "epoch_resumed" not in names(events)
    state = next(iter(engine.states.values()))
    assert state.k == 0 or any(e["event"] == "rotation" and e["epoch"] == 1 for e in events)


def test_an_unreadable_store_record_does_not_resume():
    engine = RewriteEngine(EpochStore(fail_read=True), config=CONFIG)
    _, events = send(engine, turns(3)[-1])
    assert by_name(events, "epoch_not_resumed")["reason"] == "unreadable"


def test_a_failed_write_logs_epoch_record_stale_and_never_blocks_or_degrades():
    store = EpochStore(fail_write=True)
    engine = RewriteEngine(store, config=CONFIG)
    upstream, events = send(engine, turns(3)[-1])
    stale = by_name(events, "epoch_record_stale")
    assert stale["error"] == "OSError" and names(events)[-1] == "request" and upstream
    assert not next(iter(engine.states.values())).degraded and store.record is None


def test_a_degraded_engine_writes_no_record():
    store = EpochStore(fail_pages=True)  # the first rotation's page_out fails: rule 14 degraded mode
    engine = RewriteEngine(store, config=CONFIG)
    counts = []
    for messages in turns(10):
        send(engine, messages)
        counts.append((next(iter(engine.states.values())).degraded, len(store.writes)))
    degraded = [n for d, n in counts if d]
    assert degraded and counts[0] == (False, 1)  # it wrote while healthy
    assert len(set(degraded)) == 1  # and not once after entering degraded mode


def test_a_side_request_never_writes_and_cannot_overwrite_after_a_restart():
    sequence = turns(6)
    store = EpochStore()
    engine = RewriteEngine(store, config=CONFIG)
    for messages in sequence[:-1]:
        send(engine, messages)
    count = len(store.writes)
    side = [user("a side question with its own first message")]
    send(engine, side)  # a side conversation, one turn start: not main
    assert len(store.writes) == count
    restarted = RewriteEngine(store, config=CONFIG)
    _, events = send(restarted, side)  # the side request arrives first after the restart
    assert by_name(events, "epoch_not_resumed")["reason"] == "other_conversation"
    assert len(store.writes) == count  # the gate: no write until a resume or more turn starts
    _, events = send(restarted, sequence[-1])
    assert "epoch_resumed" in names(events) and len(store.writes) == count + 1


def test_a_resume_at_a_turn_start_applies_rule_4_to_that_request():
    sequence = turns(9)
    steady = RewriteEngine(EpochStore(), config=CONFIG)
    rotated_at = []
    for i, messages in enumerate(sequence):
        _, events = send(steady, messages)
        if "rotation" in names(events):
            rotated_at.append(i)
    restart = rotated_at[-1]  # restart just before a request the steady engine rotates on
    store = EpochStore()
    before = RewriteEngine(store, config=CONFIG)
    for messages in sequence[:restart]:
        send(before, messages)
    after = RewriteEngine(store, config=CONFIG)
    _, events = send(after, sequence[restart])
    assert "epoch_resumed" in names(events)
    # The same decision the steady engine took on that request: same reason, same k.
    again = RewriteEngine(EpochStore(), config=CONFIG)
    steady_events = [send(again, m)[1] for m in sequence[:restart + 1]][-1]
    keys = ("reason", "k", "epoch")
    assert {k: by_name(events, "rotation")[k] for k in keys} == {k: by_name(steady_events, "rotation")[k] for k in keys}


def test_a_resume_mid_turn_sends_without_rotating():
    sequence = turns(6)
    store = EpochStore()
    before = RewriteEngine(store, config=CONFIG)
    for messages in sequence:
        send(before, messages)
    after = RewriteEngine(store, config=CONFIG)
    _, events = send(after, sequence[-1] + [tool_use(9), tool_result(9)])
    assert "epoch_resumed" in names(events) and "rotation" not in names(events)


@pytest.mark.skipif(not hasattr(MemoryStore, "set_epoch"), reason="lane B's B31 store side not on this base")
@pytest.mark.parametrize("kind", ["session", "memory"])
def test_the_real_store_round_trips_the_record_and_the_engine_resumes(tmp_path, kind):
    import shutil

    from contexto.store import SessionStore

    if kind == "session":
        restarted_store = SessionStore(tmp_path / "a", "s1")
        keys = list((tmp_path / "a").rglob("page.key"))
        if keys:  # the steady engine's store pages with the same key, so page ids match
            target = tmp_path / "b" / keys[0].relative_to(tmp_path / "a")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(keys[0], target)
        steady_store = SessionStore(tmp_path / "b", "s1")
    else:
        restarted_store, steady_store = MemoryStore(key=b"k" * 32), MemoryStore(key=b"k" * 32)
    sequence = turns(9)
    steady = RewriteEngine(steady_store, config=CONFIG)
    before = RewriteEngine(restarted_store, config=CONFIG)
    for messages in sequence[:-1]:
        send(steady, messages)
        send(before, messages)
    after = RewriteEngine(restarted_store, config=CONFIG)
    expected, _ = send(steady, sequence[-1])
    got, events = send(after, sequence[-1])
    assert "epoch_resumed" in names(events) and got == expected
