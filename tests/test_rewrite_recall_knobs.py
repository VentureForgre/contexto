"""Recall knobs (M2 lane A packet A7, H1 recall fix): defaults unchanged, each knob as designed."""

from __future__ import annotations

from contexto.canonical import canonical_bytes
from contexto.rewrite import COUNT_TOKENS, MESSAGES, Config, RewriteEngine, is_turn_start, recall_knobs
from contexto.store.memory import MemoryStore
from contexto.tokens import TokenEstimator

from test_rewrite import CONFIG, ctx_block, history, request, run, system, tool_result, tool_use, user

RULE = "Earlier turns are paged out; search then recall before saying you do not know."
EXAMPLE = "Example: contexto_search('build id') then contexto_recall(page id)."
REMINDER = "(If a detail is not in view, search and recall it.)"
KNOBS = Config(bound=CONFIG.bound, headroom_floor=CONFIG.headroom_floor, pin_budget=CONFIG.pin_budget,
               index_budget=CONFIG.index_budget, recall_rule=RULE, recall_example=EXAMPLE, turn_reminder=REMINDER)


def rotated(knobs=None, msgs=None):
    msgs = msgs or history(6) + [user("new prompt"), system("trailing system")]
    if knobs is None:
        return run(msgs)
    with recall_knobs(knobs):
        return run(msgs, config=knobs)


def test_defaults_leave_the_request_unchanged():
    plain = rotated()[0]
    with recall_knobs(CONFIG):
        knobless = run(history(6) + [user("new prompt"), system("trailing system")])[0]
    assert canonical_bytes(plain) == canonical_bytes(knobless)


def test_rule_and_example_lead_the_preamble_before_store_text():
    up, st, _, store = rotated(KNOBS)
    text = up["messages"][0]["content"][1]["text"]
    pinned = store.pinned_text(KNOBS.pin_budget)
    assert text.startswith(RULE + "\n\n" + EXAMPLE + "\n\n") and pinned in text
    assert text.index(EXAMPLE) < text.index(pinned)


def test_preamble_first_puts_the_preamble_ahead_of_carried_context():
    knobs = Config(**{**KNOBS.__dict__, "preamble_first": True})
    up, _, _, _ = rotated(knobs)
    content = up["messages"][0]["content"]
    assert content[0]["text"].startswith(RULE) and content[1] == ctx_block()


def test_turn_reminder_ends_every_retained_turn_start_and_nothing_else():
    msgs = history(6) + [user("new prompt"), system("trailing system")]
    up, st, _, _ = rotated(KNOBS, msgs)
    starts = [m for m in up["messages"] if is_turn_start(m)]
    assert starts and all(m["content"][-1] == {"type": "text", "text": REMINDER} for m in starts)
    others = [m for m in up["messages"] if not is_turn_start(m)]
    assert all(REMINDER not in str(m) for m in others)


def test_no_reminder_or_rule_before_anything_is_paged():
    with recall_knobs(KNOBS):
        up, st, _, _ = run(history(2), config=Config(bound=1_000_000))
    assert st.k == 0 and REMINDER not in str(up) and RULE not in str(up)


def test_knobs_keep_the_prefix_byte_identical_within_an_epoch():
    store = MemoryStore(key=b"k" * 32)
    msgs = history(6, think=True) + [user("new prompt"), system("trailing")]
    with recall_knobs(KNOBS):
        up1, st, _, _ = run(msgs, store=store, config=KNOBS)
        req2 = msgs + [tool_use(5, think="t5"), tool_result(5)]
        up2, _, _, _ = run(req2, state=st, store=store, config=KNOBS)
    n1 = len(up1["messages"])
    assert [canonical_bytes(m) for m in up2["messages"][:n1]] == [canonical_bytes(m) for m in up1["messages"]]
    # the tool_result added in the loop carries no reminder (protocol and cache safe)
    assert REMINDER not in str(up2["messages"][-1])


def test_engine_applies_its_config_knobs_and_count_tokens_sees_the_same_view():
    store, estimator = MemoryStore(key=b"k" * 32), TokenEstimator()
    engine = RewriteEngine(store, config=KNOBS, estimator=estimator)
    msgs = history(6) + [user("new prompt"), system("trailing system")]
    up, _, events = engine(request(msgs), {}, MESSAGES)
    assert any(e["event"] == "rotation" for e in events)
    counted, _, _ = engine(request(msgs), {}, COUNT_TOKENS)
    assert canonical_bytes(counted["messages"]) == canonical_bytes(up["messages"])
    assert RULE in str(up["messages"][0]) and REMINDER in str(up["messages"][0])
    # outside the engine call nothing leaks: a plain transform uses defaults again
    assert RULE not in str(run(msgs)[0])
