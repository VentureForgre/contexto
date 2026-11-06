"""D49: a head change (a byte change to ``system`` or ``tools``) takes the next turn start through the same
sizing as any other rotation: rule 4's minimal-window fallback and ``bound_too_small``, and packet A12's
room rotation. The pre-send bound check covers every forwarded request, rotated or not.

QA 2026-11-05 (verify-headchange, live-realworld F1): the head-change branch kept the newest complete turn
whatever its size, so a turn started with recall room -4,243 live, and at bound 8,000 a request of 17,172
estimated tokens went out with no pre-send event."""

import pytest

from contexto.canonical import canonical_bytes
from contexto.rewrite import MESSAGES, Config, RewriteEngine, rewrite
from contexto.store.memory import MemoryStore
from contexto.tokens import TokenEstimator
from test_rewrite import FILL, assistant, history, request, tool_result, tool_use, user

FAILURES = {"bound_too_small", "over_bound"}
SMALL = Config(bound=8_000, headroom_floor=1_000, min_turn_room=1_000, pin_budget=200, index_budget=200)
ROOM = Config(bound=30_000, headroom_floor=2_000, min_turn_room=6_000, pin_budget=200, index_budget=200)
# The live run's settings: bound 60,000, the default floor and minimum room, a head near 20,000 tokens.
LIVE = Config(bound=60_000)
LIVE_SYSTEM = "synthetic system prompt line, model claude-haiku-5-5\n" * 1_300  # about 20,000 tokens


def mcp_tool(i, size=40):
    return {"name": f"mcp__synthetic__tool_{i}", "description": "synthetic tool " * size,
            "input_schema": {"type": "object"}}


def tools_added(req):
    """Tool search loads a tool, or an MCP server connects: ``tools`` grows."""
    return {**req, "tools": [*req["tools"], mcp_tool(0)]}


def system_changed(req):
    """A hook or setting rewrites the system prompt."""
    return {**req, "system": [*req["system"], {"type": "text", "text": "synthetic added system note"}]}


def model_switched(req):
    """Plan mode on another model: the system prompt names the model."""
    system = [{**b, "text": b["text"].replace("claude-haiku-5-5", "claude-sonnet-5-5")} for b in req["system"]]
    return {**req, "model": "claude-sonnet-5-5", "system": system}


def no_change(req):
    return req


KINDS = [tools_added, system_changed, model_switched]


def base(messages, system_text="synthetic head, model claude-haiku-5-5"):
    return request(messages, system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}])


def request_event(events):
    return next(e for e in events if e["event"] == "request")


def sent_estimate(events):
    event = request_event(events)
    return event.get("sent_estimate", event["estimate"])


def turn_start_after_loop(config, steps, change, fill=FILL, system_text=None, at=1):
    """Two short turns, a tool loop of ``steps`` reads with the head changed from read ``at`` on, then a new
    prompt. Returns the turn start's (upstream, state, events)."""
    store, est = MemoryStore(key=b"k" * 32), TokenEstimator()
    make = (lambda m: base(m, system_text)) if system_text else base
    msgs = history(2) + [user("long task")]
    _, st, _ = rewrite(make(msgs), None, store, config, est)
    for i in range(steps):
        msgs = msgs + [tool_use(i), tool_result(i, fill)]
        req = make(msgs)
        _, st, _ = rewrite(change(req) if i >= at else req, st, store, config, est)
    msgs = msgs + [assistant("done"), user("memory question")]
    return rewrite(change(make(msgs)), st, store, config, est)


def assert_bounded(config, events):
    """Acceptance 1 at a turn start: under the bound, or a logged failure."""
    names = {e["event"] for e in events}
    assert sent_estimate(events) <= config.bound or names & FAILURES, [e["event"] for e in events]


def room(config, events):
    return config.bound - sent_estimate(events) - config.headroom_floor


# The QA reproductions ------------------------------------------------------------------------------

def test_control_without_head_change_rotates_to_the_minimal_window():
    _, _, ev = turn_start_after_loop(SMALL, 12, no_change)
    assert "window_minimal" in {e["event"] for e in ev} and sent_estimate(ev) <= SMALL.bound


@pytest.mark.parametrize("change", KINDS)
def test_head_change_at_a_small_bound_takes_the_minimal_window(change):
    # Before D49: the newest complete turn (the 12-read loop) was kept, 17,172 estimated tokens at 8,000.
    _, st, ev = turn_start_after_loop(SMALL, 12, change)
    names = [e["event"] for e in ev]
    assert "window_minimal" in names, names
    assert next(e for e in ev if e["event"] == "rotation")["reason"] == "head_change"
    assert sent_estimate(ev) <= SMALL.bound and room(SMALL, ev) >= SMALL.min_turn_room
    assert not FAILURES & set(names) and "turn_room_low" not in names
    assert not st.head_changed


@pytest.mark.parametrize("change", KINDS)
def test_head_change_with_no_window_budget_logs_bound_too_small(change):
    # A head bigger than bound - headroom: the plain path logs bound_too_small and keeps the minimal
    # window; before D49 the head-change path paged nothing and logged nothing before the send.
    big = "synthetic head, model claude-haiku-5-5 " * 800  # about 8,900 tokens

    def go(kind):
        store, est = MemoryStore(key=b"k" * 32), TokenEstimator()
        msgs = history(4) + [user("p")]
        _, st, _ = rewrite(base(msgs, big), None, store, SMALL, est)
        msgs = msgs + [tool_use(1), tool_result(1)]
        _, st, _ = rewrite(kind(base(msgs, big)), st, store, SMALL, est)
        msgs = msgs + [assistant("done"), user("next")]
        return rewrite(kind(base(msgs, big)), st, store, SMALL, est)

    plain = [e["event"] for e in go(no_change)[2]]
    assert "bound_too_small" in plain
    up, st, ev = go(change)
    names = [e["event"] for e in ev]
    assert "bound_too_small" in names and "window_minimal" in names, names
    assert st.k > 0 and len(up["messages"]) < 8  # the earlier turns are paged
    # The request is still over the bound, so the pre-send check flags it as well.
    over = [e for e in ev if e["event"] == "over_bound"]
    assert over and over[0]["source"] == "estimate" and request_event(ev)["over_bound"] is True


@pytest.mark.parametrize("change", KINDS)
def test_head_change_restores_the_recall_room(change):
    # Under the bound but short of room (packet A12): before D49 the head-change path kept the 18-read loop
    # and logged turn_room_low with room 2,673 against the minimum 6,000.
    _, _, plain = turn_start_after_loop(ROOM, 18, no_change)
    assert "turn_room_low" not in {e["event"] for e in plain}
    _, _, ev = turn_start_after_loop(ROOM, 18, change)
    names = [e["event"] for e in ev]
    assert "turn_room_low" not in names, [e for e in ev if e["event"] == "turn_room_low"]
    assert sent_estimate(ev) <= ROOM.bound and room(ROOM, ev) >= ROOM.min_turn_room
    assert next(e for e in ev if e["event"] == "rotation")["reason"] == "head_change"


@pytest.mark.parametrize("change", KINDS)
def test_head_change_at_the_live_bound_keeps_the_room(change):
    # The live F1 shape: bound 60,000, a head near 20,000, and a long implement turn (22 reads of about
    # 1,330 tokens each). Live, the head-change turn start kept that turn: room -4,243.
    _, _, plain = turn_start_after_loop(LIVE, 22, no_change, system_text=LIVE_SYSTEM)
    assert room(LIVE, plain) >= LIVE.min_turn_room
    _, _, ev = turn_start_after_loop(LIVE, 22, change, system_text=LIVE_SYSTEM)
    assert_bounded(LIVE, ev)
    assert sent_estimate(ev) <= LIVE.bound and room(LIVE, ev) >= LIVE.min_turn_room
    assert "turn_room_low" not in {e["event"] for e in ev}


# Cache-friendly behaviour kept where it is safe -----------------------------------------------------

@pytest.mark.parametrize("change", KINDS)
def test_head_change_with_room_to_spare_still_starts_an_epoch_without_paging(change):
    # Rule 6: the cache is lost anyway, so the epoch starts at this turn start; with the window well inside
    # the budget nothing is paged, as before D49.
    big = Config(bound=1_000_000, headroom_floor=500)
    _, before, _ = turn_start_after_loop(big, 3, no_change)
    up, st, ev = turn_start_after_loop(big, 3, change)
    rotation = next(e for e in ev if e["event"] == "rotation")
    assert rotation["reason"] == "head_change" and st.k == before.k == 0
    assert "page_out" not in {e["event"] for e in ev} and st.epoch == before.epoch + 1
    assert st.strip_upto == len(up["messages"])


@pytest.mark.parametrize("change", KINDS)
def test_head_change_pages_to_half_the_window_as_before(change):
    # Inside the budget but with the newest complete turn larger than the 0.5 * W target: the head-change
    # rotation keeps the same k the bound rotation would choose (rule 4), not more and not less.
    cfg = Config(bound=20_000, headroom_floor=1_000, min_turn_room=1_000, pin_budget=200, index_budget=200)
    _, st, ev = turn_start_after_loop(cfg, 8, change)
    names = [e["event"] for e in ev]
    assert "window_minimal" not in names and not FAILURES & set(names)
    rotation = next(e for e in ev if e["event"] == "rotation")
    assert rotation["reason"] == "head_change" and rotation["k"] == st.k > 0
    assert sent_estimate(ev) <= cfg.bound and room(cfg, ev) >= cfg.min_turn_room


# The measured head follows a head change -----------------------------------------------------------

def drive(engine, msgs, change=no_change, ratio=3.5):
    """One request through the engine with usage that matches the estimator at ``ratio`` bytes per token."""
    req = change(base(msgs))
    _, _, ev = engine(req, {}, MESSAGES)
    real = request_event(ev)["sent_bytes"] / ratio
    engine.observe_usage(MESSAGES, {"input_tokens": int(real), "output_tokens": 5}, req)
    return ev


def big_server(req):
    """An MCP server with many tools connects: about 7,600 tokens of tool definitions."""
    return {**req, "tools": [*req["tools"], *(mcp_tool(i, size=60) for i in range(30))]}


def measured_turn_start(cfg, server):
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    msgs = history(2) + [user("long task")]
    drive(engine, msgs)
    for i in range(12):
        msgs = msgs + [tool_use(i), tool_result(i, FILL)]
        drive(engine, msgs, server if i >= 6 else no_change)
    msgs = msgs + [assistant("done"), user("memory question")]
    return drive(engine, msgs, server)


def test_a_measured_head_grows_with_the_tools_an_mcp_server_adds():
    # With real usage the head is measured once (rule 4); a server that adds several thousand tokens of
    # tools must count in F at the head-change turn start, or the window is sized for the old head.
    cfg = Config(bound=30_000, headroom_floor=2_000, min_turn_room=6_000, pin_budget=200, index_budget=200)
    ev = measured_turn_start(cfg, big_server)
    rotation = next(e for e in ev if e["event"] == "rotation")
    added = (len(canonical_bytes(big_server(base([]))["tools"])) - len(canonical_bytes(base([])["tools"]))) / 3.5
    assert rotation["reason"] == "head_change" and added > 7_000
    assert rotation["fixed_overhead"] > added  # before D49: the head measured at the first request alone
    assert sent_estimate(ev) <= cfg.bound and room(cfg, ev) >= cfg.min_turn_room


# The pre-send check on every forwarded request ------------------------------------------------------

def test_a_rotated_request_over_the_bound_is_flagged_before_the_send():
    # A single new prompt bigger than the bound: the turn start rotates to the minimal window and logs
    # bound_too_small; the request is still over the bound and now carries the pre-send flag too.
    store, est = MemoryStore(key=b"k" * 32), TokenEstimator()
    msgs = history(4) + [user("p")]
    _, st, _ = rewrite(base(msgs), None, store, SMALL, est)
    msgs = msgs + [assistant("done"), user("huge " + FILL * 20)]
    _, _, ev = rewrite(base(msgs), st, store, SMALL, est)
    names = [e["event"] for e in ev]
    assert "rotation" in names and "bound_too_small" in names
    over = next(e for e in ev if e["event"] == "over_bound")
    assert over["estimate"] == sent_estimate(ev) > SMALL.bound and over["in_tool_loop"] is False


def test_a_rotated_request_under_the_bound_is_not_flagged():
    _, _, ev = turn_start_after_loop(SMALL, 12, no_change)
    assert "over_bound" not in {e["event"] for e in ev} and "over_bound" not in request_event(ev)


# Side requests and subagents -------------------------------------------------------------------------

def test_a_side_conversation_head_change_sizes_only_its_own_window():
    # A subagent or other side request is its own conversation (its own first message and epoch): its
    # head change sizes its own window and leaves main's epoch and head alone.
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=SMALL)
    main = history(2) + [user("main task")]
    engine(base(main), {}, MESSAGES)
    side = [user("side task " + FILL)]
    engine(base(side), {}, MESSAGES)
    for i in range(12):
        side = side + [tool_use(i), tool_result(i, FILL)]
        engine(tools_added(base(side)), {}, MESSAGES)
    side = side + [assistant("done"), user("side next")]
    _, _, ev = engine(tools_added(base(side)), {}, MESSAGES)
    assert request_event(ev)["main"] is False
    assert "window_minimal" in {e["event"] for e in ev} and sent_estimate(ev) <= SMALL.bound
    _, _, main_ev = engine(base(main + [tool_use(0), tool_result(0)]), {}, MESSAGES)
    assert request_event(main_ev)["main"] is True and request_event(main_ev)["epoch"] == 0
    assert "head_changed" not in {e["event"] for e in main_ev}
