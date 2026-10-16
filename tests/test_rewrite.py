"""Rewrite engine tests (M2 contract rules 1 to 12). Request shapes come from Claude Code 2.1.301 as
measured with zero-spend stubs (spikes/m0/interception, spikes/m2/carried): a context block plus the
prompt in a turn-start message, role "system" messages with output_config and cache_control, tool
loops, signed and redacted thinking, settings-only system messages, and the CLI's conversion of a
single text block to a plain string. All content is synthetic."""

import copy
import json

import pytest

from contexto.canonical import canonical_bytes
from contexto.rewrite import (
    COUNT_TOKENS,
    EpochState,
    StoreCalls,
    store_failure,
    MESSAGES,
    THINKING_OMITTED,
    Config,
    RewriteEngine,
    at_turn_start,
    is_turn_start,
    message_hash,
    classify_system,
    rewrite,
    window_tokens,
)
from contexto.store import KeyFileError, PageConflictError, PageNotFoundError, SessionNotFoundError, SessionStore
from contexto.store.ledger import LedgerCorruptError, LedgerError
from contexto.store.memory import MemoryStore
from contexto.tokens import TokenEstimator

FILL = "synthetic filler text " * 90  # about 2,000 bytes, about 570 tokens at 3.5 bytes per token
CONFIG = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200)


def ctx_block(tag="ctx"):
    return {"type": "text", "text": f"<system-reminder>synthetic {tag} context</system-reminder>"}


def user(text, context=False):
    blocks = [ctx_block()] if context else []
    return {"role": "user", "content": [*blocks, {"type": "text", "text": text}]}


def system(text="synthetic system note", cache=True):
    block = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return {"role": "system", "content": [block], "output_config": {"effort": "medium"}}


def settings_only(effort="medium"):
    return {"role": "system", "content": [], "output_config": {"effort": effort}}


def thinking(sig):
    return {"type": "thinking", "thinking": "", "signature": f"sig-{sig}"}


def assistant(text, think=None, redacted=False):
    blocks = []
    if think:
        blocks.append(thinking(think))
    if redacted:
        blocks.append({"type": "redacted_thinking", "data": "opaque-synthetic"})
    blocks.append({"type": "text", "text": text})
    return {"role": "assistant", "content": blocks}


def tool_use(i, think=None):
    blocks = [thinking(think)] if think else []
    blocks.append({"type": "tool_use", "id": f"toolu_{i}", "name": "Read", "input": {"file_path": f"f{i}.txt"}})
    return {"role": "assistant", "content": blocks}


def tool_result(i, text="synthetic tool output"):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": text}]}


def request(messages, **extra):
    return {"model": "claude-haiku-5-5", "max_tokens": 64, "stream": True,
            "system": [{"type": "text", "text": "synthetic head", "cache_control": {"type": "ephemeral"}}],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
            "messages": messages, **extra}


def history(turns, think=False):
    """Claude Code shape: messages[0] = context + prompt, messages[1] = system, then turns."""
    msgs = [user(f"prompt 0 {FILL}", context=True), system()]
    msgs.append(assistant(f"answer 0 {FILL}", think="a0" if think else None))
    for t in range(1, turns):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"answer {t} {FILL}", think=f"a{t}" if think else None)]
    return msgs


def run(messages, state=None, store=None, config=CONFIG, estimator=None):
    store = store if store is not None else MemoryStore(key=b"k" * 32)
    estimator = estimator or TokenEstimator()
    up, st, ev = rewrite(request(messages), state, store, config, estimator)
    return up, st, ev, store


def names(events):
    return [e["event"] for e in events]


# Classification ----------------------------------------------------------------------------------

def test_turn_start_detection_on_claude_code_shapes():
    assert is_turn_start(user("hi", context=True))
    assert is_turn_start({"role": "user", "content": "plain string prompt"})
    assert not is_turn_start({"role": "user", "content": [ctx_block()]})  # context only
    assert not is_turn_start(tool_result(1))
    assert not is_turn_start(system())
    # The fresh-session shape ends with a system message after the prompt: still a turn start.
    assert at_turn_start([user("p", context=True), system()])
    assert at_turn_start(history(2) + [user("new"), system()])  # trailing system joins the new turn
    assert not at_turn_start(history(2) + [user("new"), tool_use(1), tool_result(1)])


# Rule 1: first epoch, pass-through, purity ------------------------------------------------------

def test_first_epoch_passes_everything_through_unchanged():
    msgs = history(2) + [user("new turn")]
    req = request(msgs, thinking={"type": "adaptive"}, metadata={"user_id": "synthetic"})
    before = copy.deepcopy(req)
    up, st, ev = rewrite(req, None, MemoryStore(key=b"k" * 32), Config(), TokenEstimator())
    assert req == before  # input never mutated
    assert up["messages"] == msgs and up["messages"][0] is msgs[0]
    assert up["system"] is req["system"] and up["tools"] is req["tools"]
    for field in ("stream", "thinking", "metadata", "model", "max_tokens"):
        assert up[field] == req[field]
    assert st.k == 0 and names(ev) == ["request"]
    assert all(isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) < 40) for e in ev for v in e.values())


# Rules 3 and 4: rotation ------------------------------------------------------------------------

def test_rotation_at_turn_start_pages_turns_and_merges_preamble():
    msgs = history(6) + [user("new prompt"), system("trailing system")]
    up, st, ev, store = run(msgs)
    assert "rotation" in names(ev) and "page_out" in names(ev)
    first = up["messages"][0]
    assert first["role"] == "user" and is_turn_start(msgs[st.k])
    expected = "\n\n".join(t for t in (store.pinned_text(CONFIG.pin_budget), store.index_text(CONFIG.index_budget)) if t)
    assert first["content"][0] == {"type": "text", "text": expected} and expected  # the preamble block first (V2)
    assert first["content"][1] == ctx_block()  # then the carried context
    assert first["content"][2:] == msgs[st.k]["content"]  # then its own content
    assert all(b.get("type") != "tool_result" for b in first["content"])
    assert len(store.pages) == sum(1 for i in range(0, st.k) if is_turn_start(msgs[i]))


def test_smallest_k_keeps_as_many_turns_as_fit_and_never_less_than_newest_complete():
    msgs = history(6) + [user("new prompt")]
    _, st, ev, _ = run(msgs)
    rotation = next(e for e in ev if e["event"] == "rotation")
    target = int(CONFIG.window_fraction * rotation["window_budget"])
    starts = [i for i, m in enumerate(msgs) if is_turn_start(m)]
    est = TokenEstimator()
    fits = [k for k in starts[:-1] if window_tokens(msgs, est, k) <= target]
    assert st.k == (min(fits) if fits else starts[-2])
    tight = Config(bound=4_000, headroom_floor=500, window_fraction=0.001)  # nothing fits the target
    _, st2, _, _ = run(msgs, config=tight)
    assert st2.k == starts[-2]  # newest complete turn plus the new prompt are kept


def test_second_rotation_never_unpages():
    store = MemoryStore(key=b"k" * 32)
    msgs = history(6) + [user("new prompt")]
    _, st, _, _ = run(msgs, store=store)
    more = msgs + [assistant(f"reply {FILL}")]
    for t in range(4):
        more += [user(f"later {t} {FILL}"), assistant(f"later answer {t} {FILL}")]
    more.append(user("another new prompt"))
    _, st2, ev2, _ = run(more, state=st, store=store)
    assert "rotation" in names(ev2) and st2.k >= st.k and st2.turns_paged > st.turns_paged


def test_no_rotation_inside_a_tool_loop_marks_over_bound():
    msgs = history(8) + [user("go"), tool_use(1), tool_result(1, FILL * 3)]
    up, st, ev, store = run(msgs)
    assert "rotation" not in names(ev) and not store.pages
    assert ev[-1]["over_bound"] is True and up["messages"] == msgs


def test_resent_request_never_rotates_twice():
    msgs = history(6) + [user("new prompt")]
    store = MemoryStore(key=b"k" * 32)
    up1, st, _, _ = run(msgs, store=store)
    up2, st2, ev2, _ = run(msgs, state=st, store=store)
    assert "rotation" not in names(ev2) and st2.epoch == st.epoch
    assert canonical_bytes(up1) == canonical_bytes(up2)


# Rule 2: thinking ----------------------------------------------------------------------------------

def test_thinking_stripped_from_existing_messages_and_untouched_after_rotation():
    msgs = history(6, think=True) + [user("new prompt")]
    store = MemoryStore(key=b"k" * 32)
    up, st, _, _ = run(msgs, store=store)
    assert not any(b.get("type") in ("thinking", "redacted_thinking")
                   for m in up["messages"] for b in (m["content"] if isinstance(m["content"], list) else []))
    later = msgs + [tool_use(9, think="new"), tool_result(9)]
    up2, _, ev2, _ = run(later, state=st, store=store)
    assert up2["messages"][-2] is later[-2]  # added after the rotation: passes through untouched
    assert up2["messages"][-2]["content"][0]["type"] == "thinking"
    assert "rotation" not in names(ev2)


def test_thinking_only_assistant_gets_placeholder_and_tool_use_keeps_order():
    only = {"role": "assistant", "content": [thinking("x"), {"type": "redacted_thinking", "data": "d"}]}
    mixed = {"role": "assistant", "content": [thinking("y"), {"type": "text", "text": "t"}, tool_use(3)["content"][0]]}
    msgs = history(6) + [user("p"), only, user("q"), mixed, tool_result(3), assistant("done"), user("new")]
    up, st, ev, _ = run(msgs)
    assert "rotation" in names(ev) and st.k <= msgs.index(only) - 1
    by_id = {id(m): m for m in up["messages"]}
    stripped = [m for m in up["messages"] if m.get("role") == "assistant"]
    assert {"type": "text", "text": THINKING_OMITTED} in [b for m in stripped for b in m["content"]]
    kept = next(m for m in stripped if any(b.get("type") == "tool_use" for b in m["content"]))
    assert [b["type"] for b in kept["content"]] == ["text", "tool_use"]
    assert by_id  # transform built new dicts only where needed


# Rule 1 placement: carried content ----------------------------------------------------------------

def test_carried_system_messages_follow_first_retained_turn_start_and_pages_exclude_carried():
    msgs = [user(f"prompt 0 {FILL}", context=True), system("sys A"), assistant(f"a0 {FILL}")]
    msgs += [user(f"prompt 1 {FILL}"), settings_only(), assistant(f"a1 {FILL}")]
    for t in range(2, 6):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("new prompt"))
    up, st, _, store = run(msgs)
    assert st.k >= 3  # turns 0 and 1 paged
    out = up["messages"]
    assert out[0]["role"] == "user" and out[1]["role"] == "system" and out[2]["role"] == "system"
    assert out[1] is msgs[1] and out[2] is msgs[4]  # carried in current form, original order
    for page in store.pages.values():
        dumped = canonical_bytes(page).decode()
        assert "sys A" not in dumped and "system-reminder" not in dumped and '"role":"system"' not in dumped


def test_preamble_merges_into_string_content_turn_start():
    msgs = history(6)
    starts = [i for i, m in enumerate(msgs) if is_turn_start(m)]
    msgs[starts[3]] = {"role": "user", "content": msgs[starts[3]]["content"][-1]["text"]}  # string form
    msgs.append(user("new prompt"))
    up, st, _, _ = run(msgs)
    first = up["messages"][0]
    assert isinstance(first["content"], list) and first["content"][-1]["type"] == "text"
    if isinstance(msgs[st.k]["content"], str):
        assert first["content"][-1]["text"] == msgs[st.k]["content"]


def test_cache_control_on_first_retained_message_stays_in_place():
    msgs = history(6) + [user("new prompt")]
    starts = [i for i, m in enumerate(msgs) if is_turn_start(m)]
    for i in starts:
        msgs[i] = {**msgs[i], "content": [{**msgs[i]["content"][-1], "cache_control": {"type": "ephemeral"}}]}
    up, st, _, _ = run(msgs)
    assert up["messages"][0]["content"][-1].get("cache_control") == {"type": "ephemeral"}


# Byte-identical prefix within an epoch -------------------------------------------------------------

def test_prefix_is_byte_identical_across_requests_in_an_epoch():
    store = MemoryStore(key=b"k" * 32)
    msgs = history(6, think=True) + [user("new prompt"), system("trailing")]
    up1, st, _, _ = run(msgs, store=store)
    req2 = msgs + [tool_use(5, think="t5"), tool_result(5)]
    up2, st2, ev2, _ = run(req2, state=st, store=store)
    req3 = req2 + [assistant("final", think="t6")]
    req3 += [user("next turn")]
    up3, _, _, _ = run(req3, state=st2, store=store,
                       config=Config(bound=100_000, headroom_floor=500))
    n1 = len(up1["messages"])
    assert [canonical_bytes(m) for m in up2["messages"][:n1]] == [canonical_bytes(m) for m in up1["messages"]]
    assert [canonical_bytes(m) for m in up3["messages"][:n1]] == [canonical_bytes(m) for m in up1["messages"]]
    assert canonical_bytes(up2["system"]) == canonical_bytes(up1["system"])


# Rule 10: history matching ------------------------------------------------------------------------

def test_measured_form_change_and_moving_cache_control_are_not_mismatches():
    store = MemoryStore(key=b"k" * 32)
    msgs = history(6) + [user("new prompt")]
    _, st, _, _ = run(msgs, store=store)
    later = copy.deepcopy(msgs)
    later[1] = {"role": "system", "content": later[1]["content"][0]["text"], "output_config": {"effort": "medium"}}
    later[-1] = {"role": "user", "content": "new prompt"}  # single text block became a string
    later.append({**assistant("reply"), "content": [{"type": "text", "text": "reply", "cache_control": {"type": "ephemeral"}}]})
    _, st2, ev2, _ = run(later, state=st, store=store)
    assert "resync" not in names(ev2) and st2.epoch == st.epoch
    assert message_hash(msgs[1]) == message_hash(later[1])


def test_change_in_retained_window_resyncs_without_new_epoch():
    store = MemoryStore(key=b"k" * 32)
    msgs = history(6) + [user("new prompt")]
    _, st, _, _ = run(msgs, store=store)
    later = copy.deepcopy(msgs)
    later[-2] = assistant("edited synthetic answer")
    later += [assistant("reply")]
    _, st2, ev2, _ = run(later, state=st, store=store)
    resync = [e for e in ev2 if e["event"] == "resync"]
    assert resync and resync[0]["kind"] == "window_changed" and st2.epoch == st.epoch


def test_change_in_paged_turns_starts_new_epoch_without_sending_full_history():
    store = MemoryStore(key=b"k" * 32)
    msgs = history(6) + [user("new prompt")]
    _, st, _, _ = run(msgs, store=store)
    compacted = [user(f"local compaction summary {FILL}", context=True), system()] + msgs[st.k:]
    compacted += [assistant("reply"), user("after compaction")]
    up, st2, ev2, _ = run(compacted, state=st, store=store)
    resync = [e for e in ev2 if e["event"] == "resync"]
    assert resync and resync[0]["kind"] == "paged_changed"
    assert st2.epoch > st.epoch and st2.k > 0
    assert len(up["messages"]) < len(compacted)


def test_paged_change_mid_tool_loop_keeps_in_flight_thinking():
    store = MemoryStore(key=b"k" * 32)
    msgs = history(6) + [user("new prompt")]
    _, st, _, _ = run(msgs, store=store)
    changed = [user(f"replaced {FILL}", context=True), system()] + msgs[2:]
    changed += [tool_use(7, think="live"), tool_result(7)]
    up, st2, _, _ = run(changed, state=st, store=store)
    assert up["messages"][-2]["content"][0]["type"] == "thinking"  # in-flight turn untouched


def test_shorter_history_is_a_mismatch():
    store = MemoryStore(key=b"k" * 32)
    msgs = history(6) + [user("new prompt")]
    _, st, _, _ = run(msgs, store=store)
    _, _, ev, _ = run(msgs[:-3] + [user("again")], state=st, store=store)
    assert "resync" in names(ev)


# Rule 12: settings-only system messages ----------------------------------------------------------

def test_settings_only_system_message_is_forwarded_as_sent():
    msgs = [user(f"p0 {FILL}"), settings_only(), assistant("a0")] + history(2)[2:] + [user("new"), system()]
    up, _, _, _ = run(msgs, config=Config())
    assert up["messages"][1] is msgs[1] and up["messages"][1]["content"] == []


# Rule 6: head change ------------------------------------------------------------------------------

def test_head_change_waits_for_next_turn_start_and_never_rewrites_head():
    store = MemoryStore(key=b"k" * 32)
    big = Config(bound=1_000_000, headroom_floor=500)
    msgs = history(3) + [user("p")]
    req = request(msgs)
    est = TokenEstimator()
    _, st, _ = rewrite(req, None, store, big, est)
    loop = request(msgs + [tool_use(1), tool_result(1)], tools=[*request([])["tools"], {"name": "mcp__contexto__recall", "input_schema": {"type": "object"}}])
    up, st2, ev = rewrite(loop, st, store, big, est)
    assert "head_changed" in names(ev) and "rotation" not in names(ev) and up["tools"] is loop["tools"]
    nxt = request(loop["messages"] + [assistant("done"), user("next")], tools=loop["tools"])
    up3, st3, ev3 = rewrite(nxt, st2, store, big, est)
    assert any(e.get("reason") == "head_change" for e in ev3) and st3.epoch == st2.epoch + 1
    assert st3.strip_upto == len(nxt["messages"]) and up3["tools"] is nxt["tools"]


# Rule 3 estimate and headroom ---------------------------------------------------------------------

def test_real_usage_refines_the_estimate_and_can_trigger_rotation():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=Config(bound=100_000, headroom_floor=500))
    msgs = history(3) + [user("p")]
    first = request(msgs[:2])  # the same conversation's first request
    engine(first, {}, MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 2_000}, first)  # the head, measured
    req = request(msgs)
    _, _, ev = engine(req, {}, MESSAGES)
    assert "rotation" not in names(ev)  # the byte estimate is small
    engine.observe_usage(MESSAGES, {"input_tokens": 1_000, "cache_read_input_tokens": 98_000,
                                    "cache_creation_input_tokens": 0, "output_tokens": 5}, req)
    _, _, ev2 = engine(request(msgs + [assistant("a"), user("next")]), {}, MESSAGES)
    assert "rotation" in names(ev2)  # real usage says the window is nearly full


def test_headroom_tracks_running_max_turn_growth():
    store = MemoryStore(key=b"k" * 32)
    big = Config(bound=10_000_000, headroom_floor=25_000)
    est = TokenEstimator()
    msgs = [user("p0")]
    _, st, _ = rewrite(request(msgs), None, store, big, est)
    msgs = msgs + [assistant(FILL * 20), user("p1")]
    _, st, _ = rewrite(request(msgs), st, store, big, est)
    assert st.max_turn_growth > 0
    msgs = msgs + [assistant("x"), user("p2")]
    _, st2, _ = rewrite(request(msgs), st, store, big, est)
    assert st2.max_turn_growth == st.max_turn_growth  # running maximum, not the last value


# The adapter: count_tokens, endpoints, conversations ------------------------------------------------

def test_count_tokens_uses_current_epoch_with_no_side_effects():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    msgs = history(6) + [user("new prompt")]
    up, _, _ = engine(request(msgs), {}, MESSAGES)
    states, pages = copy.deepcopy(engine.states), dict(store.pages)
    bigger = msgs + [assistant(FILL * 5)] + [user("x")]
    counted, headers, ev = engine(request(bigger), {"h": "v"}, COUNT_TOKENS)
    assert engine.states == states and store.pages == pages and headers == {"h": "v"}
    assert counted["messages"][: len(up["messages"])] == up["messages"]
    assert ev == [{"event": "count_tokens", "epoch": 1}]


def test_other_endpoints_pass_through():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32))
    req = request([user("p")])
    assert engine(req, {}, "/api/hello") == (req, {}, [])


def test_side_requests_keep_their_own_state():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    main = history(6) + [user("new prompt")]
    engine(request(main), {}, MESSAGES)
    epoch = next(iter(engine.states.values())).epoch
    side = request([user("synthetic title request")], max_tokens=16)
    up, _, ev = engine(side, {}, MESSAGES)
    assert up["messages"] == side["messages"] and len(engine.states) == 2
    assert max(s.epoch for s in engine.states.values()) == epoch
    engine.observe_usage(COUNT_TOKENS, {"input_tokens": 5}, side)  # ignored
    engine.observe_usage(MESSAGES, {"input_tokens": 7}, side)
    assert engine.states[next(k for k in engine.states if engine.states[k].k == 0)].last_input_tokens == 7


@pytest.mark.parametrize("bad", [None, "", []])
def test_empty_or_missing_messages_do_not_crash(bad):
    req = {"model": "m", "messages": bad} if bad is not None else {"model": "m"}
    up, st, _ = rewrite(req, None, MemoryStore(key=b"k" * 32), Config(), TokenEstimator())
    assert up["messages"] == [] and st.k == 0


def test_carried_settings_only_messages_keep_latest_per_key():
    # Measured accrual: one settings-only message per effort change, appended after the new prompt.
    msgs = [user(f"prompt 0 {FILL}", context=True), system("sys A"), assistant(f"a0 {FILL}")]
    for t, effort in ((1, "high"), (2, "medium"), (3, "low")):
        msgs += [user(f"prompt {t} {FILL}"), settings_only(effort), assistant(f"a{t} {FILL}")]
    for t in range(4, 8):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("new prompt"))
    up, st, _, _ = run(msgs)
    assert st.k > msgs.index(msgs[10])  # all three settings changes are in paged turns
    carried = [m for m in up["messages"] if m["role"] == "system"]
    assert carried == [msgs[1], msgs[10]]  # session-start system kept; only the latest effort (low)
    assert carried[1]["output_config"] == {"effort": "low"}


def test_carried_growth_over_budget_is_reported_not_paged():
    # Rule 13: carried content that grows after the first request is reported, never paged; the initial
    # size is normal. A later message with content and settings fields (session_start, carried) is one
    # way carried content can still grow.
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config(bound=14_000, headroom_floor=500, carried_budget=1_000))
    start = [user(f"prompt 0 {FILL}", context=True), system("sys A")]
    engine(request(start), {}, MESSAGES)
    big = system("synthetic carried tool list " * 800)
    msgs = start + [assistant(f"a0 {FILL}"), user(f"prompt 1 {FILL}"), assistant(f"a1 {FILL}"), big]
    for t in range(2, 7):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("new prompt"))
    up, _, ev = engine(request(msgs), {}, MESSAGES)
    over = [e for e in ev if e["event"] == "carried_over_budget"]
    assert "rotation" in names(ev) and over and over[0]["carried_growth"] > 1_000
    assert "unclassified_system_message" not in names(ev)  # content plus output_config: session_start
    assert big in up["messages"]  # still carried, never paged
    assert all(isinstance(v, (int, str)) and (not isinstance(v, str) or len(v) < 40) for v in over[0].values())


def test_initial_carried_size_is_not_over_budget():
    big = system("synthetic large CLAUDE.md-like system text " * 800)
    msgs = [user(f"prompt 0 {FILL}", context=True), big, assistant(f"a0 {FILL}")]
    for t in range(1, 6):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("new prompt"))
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config(bound=14_000, headroom_floor=500, carried_budget=1_000))
    engine(request(msgs[:2]), {}, MESSAGES)
    _, _, ev = engine(request(msgs), {}, MESSAGES)
    assert "rotation" in names(ev) and "carried_over_budget" not in names(ev)


# Rule 4 sizing (PR #27) ---------------------------------------------------------------------------

def test_rotation_events_carry_fixed_overhead_and_window_budget():
    _, _, ev, _ = run(history(6) + [user("new prompt")])
    rotation = next(e for e in ev if e["event"] == "rotation")
    assert rotation["fixed_overhead"] > 0 and rotation["window_budget"] == CONFIG.bound - CONFIG.headroom_floor - rotation["fixed_overhead"]


def test_rotation_that_keeps_k_is_a_noop():
    # Real usage puts the request over the trigger, but the estimated window still fits the target, so
    # the smallest k is the current one: nothing is paged, nothing is stripped, no new epoch.
    from dataclasses import replace
    store = MemoryStore(key=b"k" * 32)
    cfg = Config(bound=100_000, headroom_floor=500)
    est = TokenEstimator()
    msgs = history(3, think=True) + [user("p")]
    _, st, _ = rewrite(request(msgs), None, store, cfg, est)
    st = replace(st, last_input_tokens=99_900, head_tokens=1_000)
    nxt = msgs + [assistant("a", think="z"), user("next")]
    up, st2, ev = rewrite(request(nxt), st, store, cfg, est)
    assert "rotation_noop" in names(ev) and "rotation" not in names(ev)
    assert not store.pages and up["messages"] == nxt and st2.epoch == st.epoch
    noop = next(e for e in ev if e["event"] == "rotation_noop")
    assert {"fixed_overhead", "window_budget", "k"} <= set(noop) and noop["k"] == 0


def test_estimator_recalibration_does_not_inflate_headroom():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config(bound=10_000_000, headroom_floor=500))
    msgs = [user("p0")]
    req = request(msgs)
    engine(req, {}, MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 50}, req)
    msgs = msgs + [assistant("a0"), user("p1")]
    req = request(msgs)
    engine(req, {}, MESSAGES)
    before = next(iter(engine.states.values())).max_turn_growth
    engine.observe_usage(MESSAGES, {"input_tokens": 5_000}, req)  # ratio changes a lot
    engine(request(msgs + [assistant("a1"), user("p2")]), {}, MESSAGES)
    after = next(iter(engine.states.values())).max_turn_growth
    assert after < 1_000 and before < 1_000  # growth reflects the small added turn, not the ratio change


def test_bound_too_small_still_rotates_to_the_minimal_window():
    # Intake 2026-10-16 (folded into A2c): when even the new turn exceeds W, rotate to the minimal window
    # anyway (smaller than sending unrotated) and log bound_too_small as well.
    msgs = history(6) + [user("new prompt")]
    huge_head = request(msgs, system=[{"type": "text", "text": FILL * 8}])  # F above the bound
    up, st, ev = rewrite(huge_head, None, MemoryStore(key=b"k" * 32), CONFIG, TokenEstimator())
    small = [e for e in ev if e["event"] == "bound_too_small"]
    assert small and small[0]["window_budget"] <= 0
    assert "window_minimal" in names(ev) and st.k == len(msgs) - 1  # only the new turn is retained
    assert up["messages"][0]["content"][-1] == msgs[-1]["content"][-1]  # the new turn, preamble merged

def test_seat_sized_head_does_not_thrash():
    # Scaled-down OpenRig seat: head about 61% of the bound (92k of 150k measured). Half the bound as
    # the window (the old rule) would re-trigger every turn; half of W leaves room to grow.
    cfg = Config(bound=15_000, headroom_floor=2_500)
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    head = [{"type": "text", "text": FILL * 16}]  # about 32 KB of system prompt, about 9,200 tokens
    first = request([user("p0", context=True), system()], system=head)
    engine(first, {}, MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 9_200}, first)  # measured head
    msgs = [user("p0", context=True), system(), assistant(f"a0 {FILL}")]
    for t in range(1, 8):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("new prompt"))
    req = request(msgs, system=head)
    up, _, ev = engine(req, {}, MESSAGES)
    rotation = next(e for e in ev if e["event"] == "rotation")
    w = rotation["window_budget"]
    assert 0 < w < cfg.bound // 2
    assert window_tokens(up["messages"], TokenEstimator()) <= w  # the retained window fits W
    # The next turn grows by one ordinary turn: no second rotation.
    est = TokenEstimator()
    sent = window_tokens(up["messages"], est)
    engine.observe_usage(MESSAGES, {"input_tokens": 9_200 + sent}, req)
    more = msgs + [assistant(f"reply {FILL}"), user("next prompt")]
    _, _, ev2 = engine(request(more, system=head), {}, MESSAGES)
    assert "rotation" not in names(ev2) and "rotation_noop" not in names(ev2)


def hook_system(t):
    # Measured on Claude Code 2.1.301 (live_probe.sh hook case): text only, no output_config, right
    # after the prompt, wrapped as "UserPromptSubmit hook additional context: <payload>".
    return {"role": "system", "content": [{"type": "text",
                                           "text": f"UserPromptSubmit hook additional context: Synthetic hook context {t}"}]}


def post_tool_hook(t, tool="Read"):
    # Measured: right after the tool_result, wrapped as "PostToolUse:<tool> hook additional context: ".
    return {"role": "system", "content": f"PostToolUse:{tool} hook additional context: Synthetic hook context {t}"}


def style_message(name="Explanatory"):
    # Measured: an output-style change is a text-only system message starting "# Output Style: <name>".
    return {"role": "system", "content": f"# Output Style: {name}\n" + "synthetic style instructions " * 60}


def non_text_system():
    return {"role": "system", "content": [{"type": "image", "source": {"type": "base64", "data": "AA=="}}]}


def test_system_message_classes_follow_contract_pr_38():
    msgs = [user("p0", context=True), system("session start text"), hook_system(0), assistant("a0"),
            user("p1"), hook_system(1), tool_use(1), tool_result(1), post_tool_hook(11), assistant("a1"),
            settings_only("high"), user("p2"), settings_only("low"), assistant("a2"),
            system("resumed full system text"), non_text_system(), style_message(),
            {"role": "system", "content": "Synthetic hook context without the documented wrapper"}]
    kinds = {i: classify_system(msgs, i) for i, m in enumerate(msgs) if m["role"] == "system"}
    assert kinds == {1: "session_start", 2: "hook_output", 5: "hook_output", 8: "hook_output", 10: "settings",
                     12: "settings", 14: "session_start", 15: "unclassified", 16: "carried_text",
                     17: "carried_text"}


def test_session_start_with_text_and_output_config_is_not_settings_only():
    # reviewer-1: the turn-1 message carries BOTH text and output_config; only empty content counts
    # as settings-only.
    start = system("session start text")
    assert "output_config" in start and start["content"]
    msgs = [user("p0", context=True), start]
    assert classify_system(msgs, 1) == "session_start"
    assert classify_system([user("p0"), settings_only()], 1) == "settings"


def test_per_turn_hook_messages_are_paged_and_carried_stays_flat():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    msgs = [user(f"prompt 0 {FILL}", context=True), system("session start"), assistant(f"a0 {FILL}")]
    engine(request(msgs[:2]), {}, MESSAGES)
    carried_counts = []
    for t in range(1, 25):
        msgs = msgs + [user(f"prompt {t} {FILL}"), hook_system(t)]
        up, _, ev = engine(request(msgs), {}, MESSAGES)
        state = next(iter(engine.states.values()))
        if state.k:
            first_retained = up["messages"]
            carried_counts.append(sum(1 for m in first_retained[1:]
                                      if m["role"] == "system" and m is not None and m.get("output_config")))
        assert "carried_over_budget" not in names(ev) and "unclassified_system_message" not in names(ev)
        msgs = msgs + [assistant(f"a{t} {FILL}")]
    assert carried_counts and set(carried_counts) == {1}  # only the session-start message is carried
    paged_systems = sum(1 for page in store.pages.values()
                        if '"role":"system"' in canonical_bytes(page).decode())
    assert paged_systems > 5  # hook context went into the pages with its turns


def test_paged_change_resync_reports_kept_inflight_thinking():
    store = MemoryStore(key=b"k" * 32)
    msgs = history(6) + [user("new prompt")]
    _, st, _, _ = run(msgs, store=store)
    changed = [user(f"replaced {FILL}", context=True), system()] + msgs[2:] + [tool_use(7, think="live"), tool_result(7)]
    _, _, ev, _ = run(changed, state=st, store=store)
    resync = next(e for e in ev if e["event"] == "resync")
    assert resync["kind"] == "paged_changed" and resync["inflight_thinking_kept"] is True


def test_unclassified_non_text_system_message_is_carried_and_logged():
    msgs = [user(f"prompt 0 {FILL}", context=True), system("sys A"), assistant(f"a0 {FILL}"),
            user(f"prompt 1 {FILL}"), assistant(f"a1 {FILL}"), non_text_system()]
    for t in range(2, 7):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("new prompt"))
    up, st, ev, _ = run(msgs)
    assert st.k > 5 and [e["count"] for e in ev if e["event"] == "unclassified_system_message"] == [1]
    assert msgs[5] in up["messages"]  # carried, never paged


def test_resumed_full_system_text_after_new_prompt_is_carried():
    # Measured on a hand-written session-file resume: the full system text (with output_config)
    # follows the new prompt; it is session_start and carried, not paged with its turn.
    resumed = system("resumed full system text")
    msgs = [user(f"prompt 0 {FILL}"), settings_only(), assistant(f"a0 {FILL}"),
            user(f"prompt 1 {FILL}", context=True), resumed, assistant(f"a1 {FILL}")]
    for t in range(2, 7):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("new prompt"))
    up, st, _, store = run(msgs)
    assert st.k > 4 and resumed in up["messages"]
    assert all("resumed full system text" not in canonical_bytes(p).decode() for p in store.pages.values())


def test_post_tool_use_hook_messages_are_paged_and_carried_stays_flat():
    # reviewer-2 measured (two real-CLI turns): PostToolUse additionalContext lands as a text-only
    # system message right after the tool_result.
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=Config(bound=12_000, headroom_floor=500, pin_budget=200, index_budget=200))
    msgs = [user(f"prompt 0 {FILL}", context=True), system("session start"), assistant(f"a0 {FILL}")]
    engine(request(msgs[:2]), {}, MESSAGES)
    for t in range(1, 20):
        msgs = msgs + [user(f"prompt {t} {FILL}")]
        engine(request(msgs), {}, MESSAGES)
        msgs = msgs + [tool_use(t), tool_result(t, f"out {t} {FILL}"), post_tool_hook(100 + t)]
        _, _, ev = engine(request(msgs), {}, MESSAGES)
        assert "carried_over_budget" not in names(ev) and "unclassified_system_message" not in names(ev)
        msgs = msgs + [assistant(f"a{t} {FILL}")]
    paged = "".join(canonical_bytes(p).decode() for p in store.pages.values())
    assert paged.count("Synthetic hook context 1") >= 5  # PostToolUse context paged with its turns


def test_documented_hook_signatures_and_version():
    from contexto.rewrite import HOOK_SIGNATURES_CLAUDE_CODE_VERSION, is_hook_output
    assert HOOK_SIGNATURES_CLAUDE_CODE_VERSION == "2.1.301"
    assert is_hook_output(hook_system(1)) and is_hook_output(post_tool_hook(2, "Bash"))
    assert not is_hook_output({"role": "system", "content": "Some hook additional context: x"})
    assert not is_hook_output({"role": "system", "content": [{"type": "image", "source": {}}]})


def test_output_style_and_plan_mode_messages_are_carried():
    plan = {"role": "system", "content": "Plan mode is active. " + "synthetic plan instructions " * 40}
    msgs = [user(f"prompt 0 {FILL}", context=True), system("sys A"), assistant(f"a0 {FILL}"),
            user(f"prompt 1 {FILL}"), style_message(), assistant(f"a1 {FILL}"),
            user(f"prompt 2 {FILL}"), plan, assistant(f"a2 {FILL}")]
    for t in range(3, 8):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("new prompt"))
    up, st, _, store = run(msgs, config=Config(bound=8_000, headroom_floor=500))
    assert st.k > 7 and msgs[4] in up["messages"] and plan in up["messages"]
    assert all("Output Style" not in canonical_bytes(p).decode() for p in store.pages.values())


def test_fallback_pages_superseded_carried_text_first_and_keeps_latest_of_each_kind():
    store = MemoryStore(key=b"k" * 32)
    cfg = Config(bound=30_000, headroom_floor=500, carried_text_budget=1_000, carried_budget=1_000_000)
    engine = RewriteEngine(store, config=cfg)
    msgs = [user(f"prompt 0 {FILL}", context=True), system("sys A")]
    engine(request(msgs), {}, MESSAGES)
    msgs.append(assistant(f"a0 {FILL}"))
    styles = []
    for t in range(1, 10):
        style = style_message(f"Style{t}")
        styles.append(style)
        msgs += [user(f"prompt {t} {FILL}"), style, assistant(f"a{t} {FILL}")]
    for t in range(10, 30):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("new prompt"))
    up, _, ev = engine(request(msgs), {}, MESSAGES)
    paged = [e for e in ev if e["event"] == "carried_paged"]
    assert "rotation" in names(ev) and paged and paged[0]["count"] >= 1
    assert styles[-1] in up["messages"]  # the latest output style is never paged
    assert styles[0] not in up["messages"]  # the oldest superseded one went first
    pages = "".join(canonical_bytes(p).decode() for p in store.pages.values())
    assert "Output Style: Style1" in pages


def test_carried_text_growing_per_turn_is_reported_early():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config(bound=10_000_000, headroom_floor=500))
    msgs = [user("p0"), system("sys A")]
    engine(request(msgs), {}, MESSAGES)
    seen = []
    for t in range(1, 5):
        msgs = msgs + [assistant(f"a{t}"), user(f"p{t}"), {"role": "system", "content": f"New standing note {t}"}]
        _, _, ev = engine(request(msgs), {}, MESSAGES)
        seen.append("carried_text_growing" in names(ev))
    assert True in seen and seen.count(True) == 1  # reported once, when the growth starts per turn


# A2b: over-bound accounting from real usage -------------------------------------------------------

def test_real_usage_over_the_bound_is_flagged_even_when_the_estimate_was_low():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=Config(bound=100_000, headroom_floor=500))
    req = request(history(2) + [user("p")])
    _, _, ev = engine(req, {}, MESSAGES)
    estimate = ev[-1]["estimate"]
    assert estimate < 100_000 and not ev[-1].get("over_bound")  # the pre-send estimate was under the bound
    out = engine.observe_usage(MESSAGES, {"input_tokens": 1_000, "cache_read_input_tokens": 100_500,
                                          "cache_creation_input_tokens": 0, "output_tokens": 9}, req)
    over = [e for e in out if e["event"] == "over_bound"]
    assert over and over[0]["over_bound"] is True and over[0]["source"] == "real"
    assert over[0]["real_tokens"] == 101_500 and over[0]["estimate"] == estimate
    assert [e.get("source") for e in store.events if e["event"] == "over_bound"] == ["real"]


def test_estimate_error_is_reported_when_real_exceeds_the_estimate_by_more_than_ten_percent():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=Config(bound=10_000_000))
    req = request([user("p0")])
    _, _, ev = engine(req, {}, MESSAGES)
    estimate = ev[-1]["estimate"]
    close = engine.observe_usage(MESSAGES, {"input_tokens": estimate}, req)
    assert close == []
    req2 = request([user("p0"), assistant("a0"), user("p1")])
    _, _, ev2 = engine(req2, {}, MESSAGES)
    low = engine.observe_usage(MESSAGES, {"input_tokens": int(ev2[-1]["estimate"] * 1.5) + 10}, req2)
    err = [e for e in low if e["event"] == "estimate_error"]
    assert err and err[0]["ratio"] < 0.91 and 0 < err[0]["mean_ratio"] <= 1.0
    assert not any(e["event"] == "over_bound" for e in low)


def test_count_tokens_usage_raises_nothing():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=Config(bound=1_000))
    req = request([user("p0")])
    engine(req, {}, MESSAGES)
    assert engine.observe_usage(COUNT_TOKENS, {"input_tokens": 999_999}, req) == []
    assert not any(e["event"] in ("over_bound", "estimate_error") for e in store.events)


def test_estimate_after_a_rotation_is_of_the_transformed_request():
    for factor, expect_error in ((1.0, False), (1.2, True)):
        store = MemoryStore(key=b"k" * 32)
        engine = RewriteEngine(store, config=CONFIG)
        req = request(history(6) + [user("new prompt")])
        _, _, ev = engine(req, {}, MESSAGES)
        rotation = [e for e in ev if e["event"] == "rotation"]
        sent = ev[-1]["sent_estimate"]
        assert rotation and sent < ev[-1]["estimate"]  # the transformed request is smaller than the history
        out = engine.observe_usage(MESSAGES, {"input_tokens": int(sent * factor)}, req)
        assert ("estimate_error" in [e["event"] for e in out]) is expect_error, factor


def test_pre_send_and_real_over_bound_records_are_distinct():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    loop = request(history(8) + [user("go"), tool_use(1), tool_result(1, FILL * 3)])
    _, _, ev = engine(loop, {}, MESSAGES)
    pre = [e for e in ev if e["event"] == "over_bound"]
    assert [e["source"] for e in pre] == ["estimate"] and ev[-1]["over_bound"] is True  # rule 5, allowed
    assert pre[0]["in_tool_loop"] is True
    out = engine.observe_usage(MESSAGES, {"input_tokens": CONFIG.bound + 1}, loop)
    assert [e["source"] for e in out if e["event"] == "over_bound"] == ["real"]  # acceptance 1 failure



def test_over_bound_at_a_turn_start_is_never_the_tool_loop_exemption():
    # A turn-start request whose estimate is over the bound but which cannot rotate (rotation_noop or
    # bound_too_small) must not emit the source "estimate" exemption.
    from dataclasses import replace
    store = MemoryStore(key=b"k" * 32)
    cfg = Config(bound=100_000, headroom_floor=500)
    est = TokenEstimator()
    msgs = history(3) + [user("p")]
    _, st, _ = rewrite(request(msgs), None, store, cfg, est)
    st = replace(st, last_input_tokens=150_000, head_tokens=1_000)
    up, _, ev = rewrite(request(msgs + [assistant("a"), user("next")]), st, store, cfg, est)
    assert ev[-1]["estimate"] > cfg.bound
    assert not any(e["event"] == "over_bound" for e in ev) and not ev[-1].get("over_bound")


# A2c: window_minimal (contract PR #50) --------------------------------------------------------------

BIG = "synthetic large turn content " * 300  # about 9,000 bytes, about 2,600 tokens


def big_history(turns):
    msgs = [user(f"prompt 0 {BIG}", context=True), system("sys A"), assistant(f"a0 {BIG}")]
    for t in range(1, turns):
        msgs += [user(f"prompt {t} {BIG}"), assistant(f"a{t} {BIG}")]
    return msgs


def test_window_minimal_retains_only_the_new_turn_when_both_do_not_fit():
    # builder-4's dry-run shape: turns so large that the newest complete turn plus the new turn exceed W,
    # while the new turn alone fits.
    cfg = Config(bound=12_000, headroom_floor=6_000, pin_budget=200, index_budget=200)
    store = MemoryStore(key=b"k" * 32)
    msgs = big_history(4) + [user(f"new prompt {BIG}")]
    up, st, ev = rewrite(request(msgs), None, store, cfg, TokenEstimator())
    minimal = [e for e in ev if e["event"] == "window_minimal"]
    assert minimal and "bound_too_small" not in names(ev)
    m = minimal[0]
    assert m["k"] == len(msgs) - 1 == st.k and m["count"] == 1
    assert m["new_turn_tokens"] <= m["window_budget"] < m["new_turn_tokens"] + m["previous_turn_tokens"]
    assert ev[-1]["sent_estimate"] < cfg.bound  # the request sent is under the bound
    pages = "".join(canonical_bytes(p).decode() for p in store.pages.values())
    assert "prompt 3 " in pages  # the previous turn is paged and recallable
    assert any(line for line in store.index_text(cfg.index_budget).splitlines())  # listed in the index


def test_window_minimal_count_grows_and_normal_rotation_does_not_use_it():
    cfg = Config(bound=12_000, headroom_floor=6_000, pin_budget=200, index_budget=200)
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=cfg)
    msgs = big_history(4) + [user(f"new prompt {BIG}")]
    engine(request(msgs), {}, MESSAGES)
    msgs = msgs + [assistant(f"reply {BIG}"), user(f"next prompt {BIG}")]
    _, _, ev = engine(request(msgs), {}, MESSAGES)
    assert [e["count"] for e in ev if e["event"] == "window_minimal"] == [2]
    _, _, normal, _ = run(history(6) + [user("new prompt")])  # both fit: ordinary rotation
    assert "rotation" in names(normal) and "window_minimal" not in names(normal)


# Rule 14: degraded mode and the terminal step (A2d) ---------------------------------------------

class FaultyStore:
    """A ``MemoryStore`` with fault injection: ``fail[method]`` is raised by that method while present;
    ``fail_at = (method, n, error)`` raises on the n-th call of ``method`` counted in ``calls``."""

    def __init__(self):
        self.inner = MemoryStore(key=b"k" * 32)
        self.fail: dict[str, Exception] = {}
        self.fail_at: tuple[str, int, Exception] | None = None
        self.calls: list[str] = []
        self.windows: list[tuple[int, float]] = []

    def _call(self, name, *args):
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]
        if self.fail_at and self.fail_at[:2] == (name, self.calls.count(name)):
            raise self.fail_at[2]
        return getattr(self.inner, name)(*args)

    def page_out(self, turns, first_turn):
        return self._call("page_out", turns, first_turn)

    def pinned_text(self, budget):
        return self._call("pinned_text", budget)

    def index_text(self, budget):
        return self._call("index_text", budget)

    def log(self, event):
        return self._call("log", event)

    def quarantine_page(self, page_id):
        return self._call("quarantine_page", page_id)

    def window(self):
        return self._call("window")

    def set_window(self, window_tokens, bytes_per_token):
        self._call("set_window", window_tokens, bytes_per_token)
        self.windows.append((window_tokens, bytes_per_token))


def turn_start_after(msgs, t):
    return msgs + [assistant(f"answer {t} {FILL}"), user(f"prompt {t + 1} {FILL}")]


def seeded_conflict(msgs, store):
    """Store a corrupt copy of the first page a rotation of ``msgs`` writes, under its real id."""
    _, _, _, reference = run(msgs)
    page_id = next(iter(reference.pages))
    store.inner.pages[page_id] = {**reference.pages[page_id], "messages": [user("corrupted synthetic page")]}
    return page_id, reference.pages[page_id]


def test_page_conflict_enters_degraded_mode_and_sends_the_last_good_transform():
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    page_id, _ = seeded_conflict(msgs, store)
    up, st, ev, _ = run(msgs, store=store)
    assert names(ev) == ["page_conflict", "degraded_enter", "over_bound", "request"]
    assert ev[0] == {"event": "page_conflict", "epoch": 0, "operation": "page_out", "error": "PageConflictError",
                     "page_id": page_id}
    assert ev[1]["reason"] == "page_conflict" and ev[-1]["degraded"] is True
    assert st.degraded and st.conflict_page == page_id and st.k == 0 and st.epoch == 0
    assert up["messages"] == msgs  # epoch 0's transform: nothing paged, nothing lost
    # Mid tool loop the engine only appends, touches no store, and marks every request degraded.
    store.calls.clear()
    loop = msgs + [tool_use(1), tool_result(1)]
    up2, _, ev2, _ = run(loop, state=st, store=store)
    assert up2["messages"][:len(up["messages"])] == up["messages"] and len(up2["messages"]) == len(loop)
    assert names(ev2) == ["over_bound", "request"] and ev2[-1]["degraded"] is True and store.calls == []
    assert ev2[0]["degraded"] is True and ev2[0]["in_tool_loop"] is True


@pytest.mark.parametrize("method,error", [
    ("page_out", OSError("synthetic disk full")),
    ("page_out", SessionNotFoundError("synthetic")),
    ("pinned_text", LedgerError("synthetic")),
    ("pinned_text", LedgerCorruptError("synthetic")),
    ("index_text", OSError("synthetic")),
    ("index_text", KeyFileError("synthetic")),
])
def test_store_error_at_a_rotation_keeps_the_last_good_epoch(method, error):
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    up, st, ev, _ = run(msgs, store=store)
    assert "rotation" in names(ev) and not st.degraded
    store.fail[method] = error
    msgs2 = turn_start_after(msgs, 6)
    msgs2 = turn_start_after(msgs2, 7)
    up2, st2, ev2, _ = run(msgs2, state=st, store=store)
    assert names(ev2)[:2] == ["store_error", "degraded_enter"] and "rotation" not in names(ev2)
    assert ev2[0] == {"event": "store_error", "epoch": st.epoch, "operation": method, "error": type(error).__name__}
    assert (st2.epoch, st2.k, st2.pinned_text, st2.index_text) == (st.epoch, st.k, st.pinned_text, st.index_text)
    assert st2.degraded and ev2[-1]["degraded"] is True
    assert up2["messages"][:len(up["messages"])] == up["messages"]  # append-only from the last good transform
    assert len(up2["messages"]) == len(up["messages"]) + len(msgs2) - len(msgs)
    assert str(error) not in repr(ev2)  # exception text (paths) is never logged


def test_degraded_retry_succeeds_once_and_leaves_degraded_mode():
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    store.fail["page_out"] = OSError("synthetic")
    _, st, _, _ = run(msgs, store=store)
    del store.fail["page_out"]
    msgs2 = turn_start_after(msgs, 6)
    _, st2, ev2, _ = run(msgs2, state=st, store=store)
    assert names(ev2)[0] == "degraded_leave" and "rotation" in names(ev2)
    assert not st2.degraded and "degraded" not in ev2[-1] and st2.k > 0


def test_degraded_retry_failure_stays_degraded_and_tries_once():
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    store.fail["page_out"] = OSError("synthetic")
    _, st, _, _ = run(msgs, store=store)
    store.calls.clear()
    msgs2 = turn_start_after(msgs, 6)
    up2, st2, ev2, _ = run(msgs2, state=st, store=store)
    assert names(ev2) == ["store_error", "over_bound", "request"]
    assert ev2[0]["degraded"] is True and "degraded_enter" not in names(ev2)
    assert store.calls.count("page_out") == 1 and st2.degraded and up2["messages"] == msgs2


def test_degraded_retry_reads_the_store_when_no_rotation_is_due():
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    store.fail["page_out"] = OSError("synthetic")
    _, st, _, _ = run(msgs, store=store)
    roomy = Config(bound=10_000_000, headroom_floor=500, pin_budget=200, index_budget=200)
    store.fail = {"pinned_text": OSError("synthetic")}
    _, st2, ev2, _ = run(turn_start_after(msgs, 6), state=st, store=store, config=roomy)
    assert ev2[0]["operation"] == "pinned_text" and st2.degraded
    store.fail = {}
    _, st3, ev3, _ = run(turn_start_after(turn_start_after(msgs, 6), 7), state=st2, store=store, config=roomy)
    assert names(ev3) == ["degraded_leave", "request"] and not st3.degraded


def test_degraded_turn_start_over_the_bound_is_logged_over_bound():
    store = FaultyStore()
    msgs = history(12) + [user("new prompt")]
    store.fail["page_out"] = OSError("synthetic")
    _, _, ev, _ = run(msgs, store=store)
    bound = [e for e in ev if e["event"] == "over_bound"]
    assert bound == [{"event": "over_bound", "source": "estimate", "in_tool_loop": False, "epoch": 0,
                      "estimate": bound[0]["estimate"], "bound": CONFIG.bound, "degraded": True}]


def test_persistent_conflict_is_quarantined_and_repaged_leaving_degraded_mode():
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    page_id, good = seeded_conflict(msgs, store)
    _, st, ev, _ = run(msgs, store=store)
    assert names(ev)[:2] == ["page_conflict", "degraded_enter"] and not store.inner.quarantined  # first time
    msgs2 = turn_start_after(msgs, 6)
    _, st2, ev2, _ = run(msgs2, state=st, store=store)
    assert names(ev2)[:2] == ["page_conflict", "degraded_leave"] and "rotation" in names(ev2)
    assert ev2[0]["degraded"] is True and ev2[1]["quarantined"] == page_id
    assert "page_quarantined" not in names(ev2)  # the store logs it, never the engine
    assert [e["page_id"] for e in store.inner.events if e["event"] == "page_quarantined"] == [page_id]
    assert store.inner.quarantined[page_id][-1][1]["messages"] == [user("corrupted synthetic page")]  # kept
    assert store.inner.pages[page_id] == good  # re-paged under the same id from the current messages
    assert not st2.degraded and st2.conflict_page is None and st2.k > 0


def test_failed_quarantine_stays_degraded():
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    seeded_conflict(msgs, store)
    _, st, _, _ = run(msgs, store=store)
    store.fail["quarantine_page"] = OSError("synthetic")
    _, st2, ev2, _ = run(turn_start_after(msgs, 6), state=st, store=store)
    assert names(ev2)[:2] == ["page_conflict", "store_error"] and ev2[1]["operation"] == "quarantine_page"
    assert st2.degraded and not store.inner.quarantined and "degraded_leave" not in names(ev2)


def test_paged_changed_resync_with_a_broken_store_forwards_the_original_request():
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    _, st, _, _ = run(msgs, store=store)
    assert st.k > 0
    changed = [user("compacted synthetic summary", context=True), *msgs[1:]] + [assistant("a"), user("next")]
    store.fail["page_out"] = OSError("synthetic")
    req = request(changed)
    up, st2, ev = rewrite(req, st, store, CONFIG, TokenEstimator())
    assert up is req  # the CLI's request, unchanged
    assert names(ev) == ["resync", "store_error", "degraded_enter", "fallback_failed", "request"]
    assert ev[3]["reason"] == "resync_paged_changed" and ev[-1]["fallback_failed"] is True
    assert ev[-1]["degraded"] is True and store.calls.count("page_out") == 2  # one attempt in this request
    assert st2.hashes == st.hashes and st2.k == st.k and st2.degraded  # the next request tries once more
    del store.fail["page_out"]
    up3, st3, ev3 = rewrite(request(changed + [assistant("b"), user("again")]), st2, store, CONFIG, TokenEstimator())
    assert names(ev3)[:2] == ["resync", "degraded_leave"] and not st3.degraded and up3["messages"] != changed


def test_failure_building_the_degraded_transform_forwards_the_original_request(monkeypatch):
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    store.fail["page_out"] = OSError("synthetic")
    _, st, _, _ = run(msgs, store=store)

    def broken(messages, state):
        raise RuntimeError("synthetic")

    monkeypatch.setattr("contexto.rewrite.transform_messages", broken)
    req = request(msgs + [tool_use(1), tool_result(1)])
    up, st2, ev = rewrite(req, st, store, CONFIG, TokenEstimator())
    assert up is req and "fallback_failed" in names(ev) and st2.degraded
    assert next(e for e in ev if e["event"] == "fallback_failed")["reason"] == "degraded_transform:RuntimeError"
    # Outside degraded mode the same exception is a genuine bug: it reaches the proxy (transform_error).
    with pytest.raises(RuntimeError):
        rewrite(request(history(2) + [user("new")]), None, MemoryStore(key=b"k" * 32), CONFIG, TokenEstimator())


def test_unlisted_store_exception_is_a_genuine_bug_and_propagates():
    store = FaultyStore()
    store.fail["page_out"] = ValueError("synthetic bug")
    with pytest.raises(ValueError):
        run(history(6) + [user("new prompt")], store=store)


def test_store_log_failure_in_observe_usage_enters_degraded_mode_without_raising():
    store = FaultyStore()
    engine = RewriteEngine(store, config=Config(bound=1_000))
    req = request(history(2) + [user("new")])
    engine(req, {}, MESSAGES)
    store.fail["log"] = OSError("synthetic")
    events = engine.observe_usage(MESSAGES, {"input_tokens": 5_000}, req)
    assert names(events) == ["over_bound", "estimate_error", "store_error", "degraded_enter"]
    assert events[2]["operation"] == "log"
    assert engine.states[next(iter(engine.states))].degraded


def test_set_window_publishes_w_and_the_measured_ratio_only_when_they_change():
    store = FaultyStore()
    engine = RewriteEngine(store, config=CONFIG)
    msgs = history(6) + [user("new prompt")]
    req = request(msgs)
    engine(req, {}, MESSAGES)
    assert "set_window" not in store.calls  # no measured ratio yet
    state = next(iter(engine.states.values()))
    sent = state.last_sent_bytes
    engine.observe_usage(MESSAGES, {"input_tokens": sent // 3}, req)
    assert store.windows == [(state.window_budget, engine.estimator.measured_bytes_per_token)]
    engine.observe_usage(MESSAGES, {"input_tokens": sent // 3}, req)  # ratio unchanged
    assert len(store.windows) == 1
    engine(req, {}, COUNT_TOKENS)  # never from count_tokens
    assert len(store.windows) == 1
    engine.observe_usage(MESSAGES, {"input_tokens": sent // 2}, req)  # ratio moves well over 1%
    assert len(store.windows) == 2 and store.windows[-1][1] == engine.estimator.measured_bytes_per_token
    assert store.inner.window() == store.windows[-1]  # the real MemoryStore accepted both


def test_set_window_is_skipped_for_a_non_positive_w_and_failing_enters_degraded_mode():
    store = FaultyStore()
    tight = Config(bound=4_000, headroom_floor=6_000, pin_budget=200, index_budget=200)  # W < 0
    engine = RewriteEngine(store, config=tight)
    req = request(history(6) + [user("new prompt")])
    _, _, events = engine(req, {}, MESSAGES)
    assert "bound_too_small" in names(events)
    engine.observe_usage(MESSAGES, {"input_tokens": 3_000}, req)
    assert next(iter(engine.states.values())).published_window is None and "set_window" not in store.calls
    req = request(history(6) + [user("new prompt")])
    failing = FaultyStore()
    failing.fail["set_window"] = OSError("synthetic")
    engine = RewriteEngine(failing, config=CONFIG)
    engine(req, {}, MESSAGES)
    events = engine.observe_usage(MESSAGES, {"input_tokens": 3_000}, req)
    assert [e["operation"] for e in events if e["event"] == "store_error"] == ["set_window"]
    assert next(iter(engine.states.values())).degraded


def test_quarantine_of_a_vanished_page_is_a_store_error_and_stays_degraded():
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    page_id, _ = seeded_conflict(msgs, store)
    _, st, _, _ = run(msgs, store=store)
    store.fail["quarantine_page"] = PageNotFoundError(f"no stored or quarantined page {page_id}")
    _, st2, ev2, _ = run(turn_start_after(msgs, 6), state=st, store=store)
    assert ev2[1] == {"event": "store_error", "epoch": st.epoch, "operation": "quarantine_page",
                      "error": "PageNotFoundError", "degraded": True}
    assert st2.degraded


def test_persistent_conflict_on_a_session_store_quarantines_the_file_and_repages(tmp_path):
    store = SessionStore(tmp_path, "a2d-test")
    msgs = history(6) + [user("new prompt")]
    run(msgs, store=store)  # a first conversation writes real page files
    first = min(store.pages_dir.glob("pg_*.json"), key=lambda p: json.loads(p.read_text())["turn"])
    good = first.read_text()
    page = json.loads(good)
    first.write_text(json.dumps({**page, "messages": [user("corrupted synthetic page")]}))
    # The same conversation from a fresh state pages turn 0 again under the same id: a conflict.
    _, st, ev, _ = run(msgs, store=store)
    assert ev[0]["event"] == "page_conflict" and ev[0]["page_id"] == page["id"] and st.degraded
    assert str(tmp_path) not in repr(ev)  # the exception's path is never logged
    _, st2, ev2, _ = run(turn_start_after(msgs, 6), state=st, store=store)
    assert names(ev2)[:2] == ["page_conflict", "degraded_leave"] and not st2.degraded
    assert json.loads(first.read_text()) == page  # re-paged under the same id from the current messages
    assert list((store.dir / "quarantine").glob(f"{page['id']}.*.json"))  # moved aside, never deleted


def test_failure_after_pages_were_written_rewrites_them_identically_on_the_retry():
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    store.fail_at = ("pinned_text", 2, OSError("synthetic"))  # fixed_overhead reads it, then new_epoch
    _, st, ev, _ = run(msgs, store=store)
    assert ev[0]["operation"] == "pinned_text" and "page_out" in store.calls and store.inner.pages
    assert st.degraded and st.k == 0 and st.turns_paged == 0  # the written pages are not counted
    store.fail_at = None
    _, st2, ev2, _ = run(turn_start_after(msgs, 6), state=st, store=store)
    assert names(ev2)[0] == "degraded_leave" and "page_conflict" not in names(ev2) and not st2.degraded


def test_carried_text_paging_failure_enters_degraded_mode():
    store = FaultyStore()
    cfg = Config(bound=30_000, headroom_floor=500, carried_text_budget=1_000, carried_budget=1_000_000)
    msgs = [user(f"prompt 0 {FILL}", context=True), system("sys A")]
    _, st0, _, _ = run(msgs, store=store, config=cfg)  # the carried baseline (rule 13) is this first request
    msgs.append(assistant(f"a0 {FILL}"))
    for t in range(1, 10):
        msgs += [user(f"prompt {t} {FILL}"), style_message(f"Style{t}"), assistant(f"a{t} {FILL}")]
    for t in range(10, 30):
        msgs += [user(f"prompt {t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("new prompt"))
    store.fail_at = ("page_out", 2, OSError("synthetic"))  # the turns page, then the carried text fails
    up, st, ev, _ = run(msgs, state=st0, store=store, config=cfg)
    assert ev[0] == {"event": "store_error", "epoch": 0, "operation": "page_out", "error": "OSError"}
    assert "carried_paged" not in names(ev) and "rotation" not in names(ev) and up["messages"] == msgs
    store.fail_at = None
    _, st2, ev2, _ = run(msgs + [assistant("a"), user("next")], state=st, store=store, config=cfg)
    assert names(ev2)[:2] == ["degraded_leave", "page_out"] and "rotation" in names(ev2) and not st2.degraded


def test_set_window_failure_at_a_rotation_enters_degraded_mode():
    store = FaultyStore()
    engine = RewriteEngine(store, config=CONFIG)
    first = request(history(2) + [user("new")])
    engine(first, {}, MESSAGES)
    sent = next(iter(engine.states.values())).last_sent_bytes
    engine.observe_usage(MESSAGES, {"input_tokens": sent // 3}, first)  # publishes W from the measured head
    assert store.calls.count("set_window") == 1
    store.fail["set_window"] = OSError("synthetic")  # the rotation's W differs (headroom grew), so it republishes
    _, _, events = engine(request(history(6) + [user("new prompt")]), {}, MESSAGES)
    assert [e["operation"] for e in events if e["event"] == "store_error"] == ["set_window"]
    assert "rotation" not in names(events) and next(iter(engine.states.values())).degraded


def test_repage_failure_after_quarantine_stays_degraded_with_the_page_moved_aside():
    store = FaultyStore()
    msgs = history(6) + [user("new prompt")]
    page_id, good = seeded_conflict(msgs, store)
    _, st, _, _ = run(msgs, store=store)
    store.calls.clear()
    store.fail_at = ("page_out", 2, OSError("synthetic"))  # the conflict, the quarantine, then the re-page
    _, st2, ev2, _ = run(turn_start_after(msgs, 6), state=st, store=store)
    assert names(ev2)[:2] == ["page_conflict", "store_error"] and ev2[1]["operation"] == "page_out"
    assert st2.degraded and page_id in store.inner.quarantined and "degraded_leave" not in names(ev2)
    store.fail_at = None
    _, st3, ev3, _ = run(turn_start_after(turn_start_after(msgs, 6), 7), state=st2, store=store)
    assert names(ev3)[0] == "degraded_leave" and not st3.degraded and store.inner.pages[page_id] == good


def test_first_request_that_rotates_does_not_report_carried_growth():
    """Rule 13 baseline (A8 b): measured in the A6 smoke, a new engine's first request rotated and logged
    carried_over_budget with growth equal to the whole carried content."""
    cfg = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, carried_budget=100)
    msgs = history(6) + [user("new prompt")]
    msgs.insert(1, system("synthetic session start " * 60))  # carried content far above carried_budget
    _, st, ev, _ = run(msgs, config=cfg)
    assert "rotation" in names(ev) and "carried_over_budget" not in names(ev)
    assert st.initial_carried > cfg.carried_budget  # the baseline is the first request's carried size


def test_a_second_engine_on_the_same_session_sizes_like_the_first():
    """A8 (c): measured in the A6 smoke, each restarted engine began from the seed ratio. Now the first
    response publishes W and the ratio, and a new engine on the same store starts from that ratio."""
    store = MemoryStore(key=b"k" * 32)
    cfg = Config(bound=20_000, headroom_floor=500, pin_budget=200, index_budget=200)
    first = RewriteEngine(store, config=cfg)
    assert first.restored is None  # a new session has nothing to restore
    msgs = history(4) + [user("new prompt")]
    req = request(msgs)
    first(req, {}, MESSAGES)
    sent = next(iter(first.states.values())).last_sent_bytes
    first.observe_usage(MESSAGES, {"input_tokens": sent // 2}, req)  # real ratio 2.0, seed 3.5
    ratio = first.estimator.measured_bytes_per_token
    assert store.window()[1] == ratio and store.window()[0] > 0  # published after the first response
    second = RewriteEngine(store, config=cfg)
    assert second.restored == {"event": "sizing_restored", "bytes_per_token": round(ratio, 4),
                               "weight_tokens": 10_000}
    assert second.estimator.measured_bytes_per_token == pytest.approx(ratio, rel=1e-3)
    assert [e for e in store.events if e["event"] == "sizing_restored"]
    # The restarted engine's first estimate of the same request matches the measured one.
    _, _, events = second(req, {}, MESSAGES)
    assert events[-1]["estimate"] == pytest.approx(sent // 2, rel=0.05)
    seeded = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)  # what a restart did before A8
    _, _, events = seeded(req, {}, MESSAGES)
    assert events[-1]["estimate"] < 0.75 * (sent // 2)


def test_ratio_restore_is_skipped_when_the_store_cannot_be_read():
    store = FaultyStore()
    store.fail["window"] = OSError("synthetic")
    engine = RewriteEngine(store, config=CONFIG)
    assert engine.restored is None and engine.estimator.measured_bytes_per_token is None


def test_first_usage_w_counts_the_recall_knobs_like_fixed_overhead():
    """A8 after PR #70: observe_usage's first W uses the same preamble composition as what is sent, so a
    recall rule (A7 knob) is part of F there too."""
    rule = "Synthetic recall rule. " * 40
    widths = {}
    for knob in ("", rule):
        cfg = Config(bound=20_000, headroom_floor=500, pin_budget=200, index_budget=200, recall_rule=knob)
        engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
        req = request(history(2) + [user("new")])
        engine(req, {}, MESSAGES)
        engine.observe_usage(MESSAGES, {"input_tokens": 3_000}, req)
        widths[knob] = (next(iter(engine.states.values())).window_budget, engine.estimator.estimate(rule))
    (plain, _), (with_rule, rule_tokens) = widths[""], widths[rule]
    assert plain - with_rule == rule_tokens > 0


def test_page_conflict_id_prefers_the_structured_attribute_and_falls_back_to_the_message():
    """A2e (reviewer-2 note 1 on PR #65): the page id comes from PageConflictError.page_id when the store
    sets it; until lane B adds it, from the message both stores write."""
    structured, quoted = "pg_" + "a" * 16, "pg_" + "b" * 16
    error = PageConflictError(f"page {quoted} exists with different turn or messages")
    error.page_id = structured
    state = EpochState()
    calls = StoreCalls(MemoryStore(key=b"k" * 32))
    assert store_failure(error, calls, state)["page_id"] == structured
    assert store_failure(PageConflictError(f"page {quoted} exists"), calls, state)["page_id"] == quoted
    assert "page_id" not in store_failure(PageConflictError("no id here"), calls, state)
    malformed = PageConflictError(f"page {quoted} exists")
    malformed.page_id = "not-a-page-id"
    assert store_failure(malformed, calls, state)["page_id"] == quoted
