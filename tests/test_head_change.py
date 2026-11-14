"""D49: a head change (a byte change to ``system`` or ``tools``) takes the next turn start through the same
sizing as any other rotation: rule 4's minimal-window fallback and ``bound_too_small``, and packet A12's
room rotation. The pre-send bound check covers every forwarded request, rotated or not.

QA 2026-11-05 (verify-headchange, live-realworld F1): the head-change branch kept the newest complete turn
whatever its size, so a turn started with recall room -4,243 live, and at bound 8,000 a request of 17,172
estimated tokens went out with no pre-send event."""

import dataclasses

import pytest

from contexto.canonical import canonical_bytes
from contexto.rewrite import MESSAGES, Config, RewriteEngine, rewrite
from contexto.store.memory import MemoryStore
from contexto.tokens import TokenEstimator
from test_rewrite import FILL, assistant, history, request, tool_result, tool_use, user

FAILURES = {"bound_too_small", "over_bound"}
SMALL = Config(bound=8_000, headroom_floor=1_000, min_turn_room=1_000, pin_budget=200, index_budget=200)
# D63: a tool loop reaches its turn's end over the bound only with in-turn paging off. The reproductions of
# a turn start after such a loop (the minimal window, a head change sized by it) use this.
UNPAGED = dataclasses.replace(SMALL, in_turn_paging=False)
ROOM = Config(bound=30_000, headroom_floor=2_000, min_turn_room=6_000, pin_budget=200, index_budget=200)
# The live run's settings: bound 60,000, the default floor and minimum room, a head near 20,000 tokens.
LIVE = Config(bound=60_000)
LIVE_SYSTEM = "synthetic system prompt line, model claude-haiku-5-5\n" * 1_300  # about 20,000 tokens
SEAT_SYSTEM = "synthetic system prompt line, model claude-haiku-5-5\n" * 850  # about 13,100 tokens


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


BASE_SYSTEM = "synthetic head, model claude-haiku-5-5"


def base(messages, system_text=BASE_SYSTEM):
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
    _, _, ev = turn_start_after_loop(UNPAGED, 12, no_change)
    assert "window_minimal" in {e["event"] for e in ev} and sent_estimate(ev) <= SMALL.bound


@pytest.mark.parametrize("change", KINDS)
def test_head_change_at_a_small_bound_takes_the_minimal_window(change):
    # Before D49: the newest complete turn (the 12-read loop) was kept, 17,172 estimated tokens at 8,000.
    _, st, ev = turn_start_after_loop(UNPAGED, 12, change)
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

RATIO = 3.5  # the fixtures' real bytes per token: usage is what the engine sent, at this ratio
SLACK = 1_000  # what rule 4 also counts in the head: the request's JSON around the messages, carried text


def drive(engine, msgs, change=no_change, system_text=BASE_SYSTEM, measured=True, ratio=RATIO):
    """One request through the engine. Its usage matches the estimator at ``ratio`` bytes per token, or
    is never read if not ``measured`` (a 200 whose usage Contexto cannot read, Esc before the usage; a
    429 or 529 is retried with the same body, and the retry is measured), or has no input count if
    ``measured`` is ``"output only"``."""
    req = change(base(msgs, system_text))
    _, _, ev = engine(req, {}, MESSAGES)
    if measured == "output only":
        engine.observe_usage(MESSAGES, {"output_tokens": 5}, req)
    elif measured:
        real = request_event(ev)["sent_bytes"] / ratio
        engine.observe_usage(MESSAGES, {"input_tokens": int(real), "output_tokens": 5}, req)
    return ev


def big_server(req):
    """An MCP server with many tools connects: about 7,600 tokens of tool definitions."""
    return {**req, "tools": [*req["tools"], *(mcp_tool(i, size=60) for i in range(30))]}


def real_head(req, ratio=RATIO):
    """The head's real tokens: its ``system`` and ``tools`` at the fixtures' ratio."""
    return len(canonical_bytes({"system": req["system"], "tools": req["tools"]})) / ratio


def main_state(engine):
    (state,) = engine.states.values()
    return state


def measured_turn_start(cfg, server, first_usage_at=0, steps=12, system_text=BASE_SYSTEM):
    """A turn of ``steps`` reads whose loop connects ``server`` at read 6; usage arrives from read
    ``first_usage_at`` on (0: from the turn's first request). Returns the engine and the next turn start's
    events, with the turn start's request."""
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    msgs = history(2) + [user("long task")]
    drive(engine, msgs, system_text=system_text, measured=first_usage_at == 0)
    for i in range(steps):
        msgs = msgs + [tool_use(i), tool_result(i, FILL)]
        drive(engine, msgs, server if i >= 6 else no_change, system_text, measured=i + 1 >= first_usage_at)
    msgs = msgs + [assistant("done"), user("memory question")]
    return engine, drive(engine, msgs, server, system_text), server(base(msgs, system_text))


def assert_measured_once(engine, req, ev, cfg):
    """Rule 4 as amended (D49): the measured head is at least the real head, the head change counted once,
    and the turn start goes out under the bound by the estimate and by the real count, with the minimum
    room or ``turn_room_low`` logged."""
    head = main_state(engine).head_tokens
    assert real_head(req) <= head < real_head(req) + SLACK, (head, real_head(req))
    assert_turn_start_bounded(cfg, ev)


def assert_turn_start_bounded(cfg, ev):
    real = request_event(ev)["sent_bytes"] / RATIO
    assert sent_estimate(ev) <= cfg.bound and real <= cfg.bound, (sent_estimate(ev), real)
    names = {e["event"] for e in ev}
    assert cfg.bound - real - cfg.headroom_floor >= cfg.min_turn_room or names & {"turn_room_low",
                                                                                  "bound_too_small"}, real


def test_a_measured_head_grows_with_the_tools_an_mcp_server_adds():
    # With real usage the head is measured once (rule 4); a server that adds several thousand tokens of
    # tools must count in F at the head-change turn start, or the window is sized for the old head.
    cfg = Config(bound=30_000, headroom_floor=2_000, min_turn_room=6_000, pin_budget=200, index_budget=200)
    _, ev, _ = measured_turn_start(cfg, big_server)
    rotation = next(e for e in ev if e["event"] == "rotation")
    added = (len(canonical_bytes(big_server(base([]))["tools"])) - len(canonical_bytes(base([])["tools"]))) / RATIO
    assert rotation["reason"] == "head_change" and added > 7_000
    assert rotation["fixed_overhead"] > added  # before D49: the head measured at the first request alone
    assert sent_estimate(ev) <= cfg.bound and room(cfg, ev) >= cfg.min_turn_room


# Rule 4 measures the head right whenever the first usage arrives ------------------------------------
# Before the D49 amendment the head was the first measured input minus the window's estimate made before
# the session's ratio was known (tool output at 1.5 bytes per token), so a first usage after a long loop
# measured it thousands of tokens low, for the rest of the session.

@pytest.mark.parametrize("late", [False, True])
def test_a_late_first_usage_keeps_turn_starts_under_the_bound(late):
    # PR #244 review: no usage until read 26 measured a head of 490 against a real 20,095; turn starts
    # 2 to 4 kept the window (rotation_noop) and turn starts 4 and 5 went out at 32,047 and 32,031 real.
    cfg = Config(bound=30_000, headroom_floor=5_000, min_turn_room=1_000, pin_budget=200, index_budget=200)
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    msgs = history(2) + [user("long task")]
    drive(engine, msgs, system_text=LIVE_SYSTEM, measured=not late)
    n = 0
    for t, steps in enumerate([26, 6, 6, 6, 6]):
        for i in range(steps):
            msgs = msgs + [tool_use(n), tool_result(n, FILL)]
            n += 1
            drive(engine, msgs, system_text=LIVE_SYSTEM, measured=not late or t > 0 or i == steps - 1)
        msgs = msgs + [assistant("done"), user(f"next {t}")]
        ev = drive(engine, msgs, system_text=LIVE_SYSTEM)
        assert_measured_once(engine, base(msgs, LIVE_SYSTEM), ev, cfg)
        rotation = next((e for e in ev if e["event"] == "rotation"), None)
        assert rotation is None or rotation["fixed_overhead"] >= real_head(base(msgs, LIVE_SYSTEM)), t + 1


@pytest.mark.parametrize("server", [no_change, big_server])
@pytest.mark.parametrize("steps", [12, 20, 24])
def test_a_late_first_usage_with_a_20000_token_system_prompt_counts_the_whole_head(server, steps):
    # PR #244 review: with the live head, first usage at the loop's last read, F came out 236 to 9,346
    # under the real head even with the head change counted twice.
    engine, ev, req = measured_turn_start(LIVE, server, first_usage_at=steps, steps=steps, system_text=LIVE_SYSTEM)
    assert_measured_once(engine, req, ev, LIVE)


@pytest.mark.parametrize("system_text", [BASE_SYSTEM, SEAT_SYSTEM])
@pytest.mark.parametrize("server", [no_change, big_server])
@pytest.mark.parametrize("first_usage_at", [0, 1, 4, 7, 8, 12])
def test_a_head_measured_at_any_step_is_at_least_the_real_head_and_counts_a_change_once(server, first_usage_at,
                                                                                         system_text):
    # Usage first arriving after the server connected (read 6) measures the new head: re-based on the
    # measured request, the next head-change turn start adds nothing (before: the change twice). PR #244
    # review B: with a 13,100-token head and the server, first usage at read 12 measured 2,873 against
    # 15,777 through the proxy, and turn_room_low was not logged.
    engine, ev, req = measured_turn_start(ROOM, server, first_usage_at=first_usage_at, system_text=system_text)
    assert_measured_once(engine, req, ev, ROOM)


@pytest.mark.parametrize("system_text, cfg", [(SEAT_SYSTEM, ROOM), (LIVE_SYSTEM, LIVE),
                                              (LIVE_SYSTEM, Config(bound=30_000, headroom_floor=5_000,
                                                                   min_turn_room=1_000, pin_budget=200,
                                                                   index_budget=200))])
@pytest.mark.parametrize("first", [1, 4])
def test_usage_without_an_input_count_measures_nothing(system_text, cfg, first):
    # PR #244 review B: the first response's usage had output_tokens alone; rule 4 took the head as 0 and
    # the last real input as 0, and turn starts at bound 30,000 went out at 30,586 to 33,692 real tokens.
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    msgs = history(2) + [user("long task")]
    drive(engine, msgs, system_text=system_text, measured="output only")
    n = 0
    for t, steps in enumerate([12, 6, 6, 6, 6]):
        for _ in range(steps):
            msgs = msgs + [tool_use(n), tool_result(n, FILL)]
            n += 1
            drive(engine, msgs, system_text=system_text, measured="output only" if n < first else True)
            assert n < first or main_state(engine).head_tokens is not None
            assert n >= first or main_state(engine).head_tokens is None
        msgs = msgs + [assistant("done"), user(f"next {t}")]
        ev = drive(engine, msgs, system_text=system_text)
        assert_measured_once(engine, base(msgs, system_text), ev, cfg)


def test_usage_without_an_input_count_is_a_response_without_usage():
    # Rule 3: the next request is estimated whole and logs usage_missing, as after a 200 with no usage;
    # before, the input was read as 0 and the next estimate was 0 plus its new messages.
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=ROOM)
    msgs = history(2) + [user("long task"), tool_use(0), tool_result(0, FILL)]
    drive(engine, msgs, system_text=SEAT_SYSTEM)
    measured = main_state(engine)
    msgs = msgs + [tool_use(1), tool_result(1, FILL)]
    drive(engine, msgs, system_text=SEAT_SYSTEM, measured="output only")
    assert main_state(engine).last_input_tokens == measured.last_input_tokens
    assert main_state(engine).head_tokens == measured.head_tokens
    msgs = msgs + [tool_use(2), tool_result(2, FILL)]
    ev = drive(engine, msgs, system_text=SEAT_SYSTEM)
    assert "usage_missing" in {e["event"] for e in ev}
    assert request_event(ev)["estimate"] > 0.95 * request_event(ev)["sent_bytes"] / RATIO  # not 0 plus a read


def test_usage_that_never_has_an_input_count_keeps_turn_starts_under_the_bound():
    # Nothing is measured, so every request is estimated whole at the seeds, and the pre-send check is the
    # only bound check (real usage over the bound cannot be logged without an input count).
    cfg = Config(bound=30_000, headroom_floor=5_000, min_turn_room=1_000, pin_budget=200, index_budget=200)
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    msgs = history(2) + [user("long task")]
    n = 0
    for t in range(5):
        for _ in range(8):
            msgs = msgs + [tool_use(n), tool_result(n, FILL)]
            n += 1
            drive(engine, msgs, system_text=LIVE_SYSTEM, measured="output only")
        msgs = msgs + [assistant("done"), user(f"next {t}")]
        ev = drive(engine, msgs, system_text=LIVE_SYSTEM, measured="output only")
        assert main_state(engine).head_tokens is None and main_state(engine).last_input_tokens is None
        assert_turn_start_bounded(cfg, ev)


def test_a_resumed_first_request_with_a_long_history_measures_the_whole_head():
    # PR #244 review: an engine's first request can carry a long history at k == 0 (a restarted proxy with
    # no epoch record it can resume, in the middle of a loop). With 18 reads the head was measured at 6,582
    # against 20,095, and W at 38,117 instead of about 24,400.
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=LIVE)
    msgs = history(2) + [user("long task")]
    for i in range(18):
        msgs = msgs + [tool_use(i), tool_result(i, FILL)]
    drive(engine, msgs, system_text=LIVE_SYSTEM)
    head = real_head(base(msgs, LIVE_SYSTEM))
    assert main_state(engine).k == 0 and head <= main_state(engine).head_tokens < head + SLACK
    assert main_state(engine).window_budget <= LIVE.bound - LIVE.headroom_floor - head
    n = 18
    for t in range(4):
        msgs = msgs + [assistant("done"), user(f"next {t}")]
        assert_measured_once(engine, base(msgs, LIVE_SYSTEM), drive(engine, msgs, system_text=LIVE_SYSTEM), LIVE)
        for _ in range(8):
            msgs = msgs + [tool_use(n), tool_result(n, FILL)]
            n += 1
            drive(engine, msgs, system_text=LIVE_SYSTEM)


def test_unattributed_usage_before_the_first_measurement_leaves_the_head_to_it():
    # Usage for an older request (still in flight when the next was sent) is usage_unattributed: it is
    # neither the head's measurement nor paired with the newest request's bytes.
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=ROOM)
    msgs = history(2) + [user("long task")]
    drive(engine, msgs, measured=False)
    sent = []
    for i in range(12):
        msgs = msgs + [tool_use(i), tool_result(i, FILL)]
        if i == 9:  # the usage of read 7's request arrives after read 8's was sent
            events = engine.observe_usage(MESSAGES, {"input_tokens": 9_000, "output_tokens": 5}, sent[-2])
            assert [e["event"] for e in events] == ["usage_unattributed"]
            assert main_state(engine).head_tokens is None
        drive(engine, msgs, big_server if i >= 6 else no_change, measured=i == 11)
        sent.append(big_server(base(msgs)) if i >= 6 else base(msgs))
    msgs = msgs + [assistant("done"), user("memory question")]
    ev = drive(engine, msgs, big_server)
    assert_measured_once(engine, big_server(base(msgs)), ev, ROOM)


@pytest.mark.parametrize("ratio", [2.5, 3.5, 4.5])
def test_a_late_first_usage_on_the_responses_dialect_measures_the_whole_head(ratio):
    # Codex: the window's estimate before the first usage is at 3.5 bytes per token for every item, so a
    # session whose real ratio is higher measured the head low. At a lower real ratio that estimate is the
    # smaller window and the head errs high (6,601 tokens over at 2.5), which pages more but never overruns.
    from test_codex import call, lead, output, prompt
    from test_codex import request as codex_request

    cfg = Config(bound=60_000, headroom_floor=10_000, min_turn_room=1_000, pin_budget=200, index_budget=200)
    tools = [{"type": "function", "name": f"synthetic_tool_{i}", "description": "synthetic tool " * 60,
              "parameters": {"type": "object"}} for i in range(30)]

    def make(items):
        return codex_request(items, instructions=LIVE_SYSTEM, tools=tools)

    def send(items, measured=True):
        _, _, ev = engine(make(items), {}, "/responses")
        if measured:
            real = int(request_event(ev)["sent_bytes"] / ratio)
            engine.observe_usage("/responses", {"input_tokens": real, "output_tokens": 5}, make(items))
        return ev

    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    head = len(canonical_bytes({"instructions": LIVE_SYSTEM, "tools": tools})) / ratio
    items = lead() + [prompt("long task")]
    send(items, measured=False)
    n = 0
    for t, steps in enumerate([26, 6, 6, 6]):
        for i in range(steps):
            items = items + [call(n), output(n, FILL)]
            n += 1
            send(items, measured=t > 0 or i == steps - 1)
        items = items + [prompt(f"next {t}")]
        ev = send(items)
        measured = main_state(engine).head_tokens
        assert head <= measured and (ratio < 3.5 or measured < head + SLACK), (t, measured, head)
        assert sent_estimate(ev) <= cfg.bound and request_event(ev)["sent_bytes"] / ratio <= cfg.bound


@pytest.mark.parametrize("head_ratio,window_ratio", [(2.5, 1.8), (2.5, 2.0), (3.0, 1.8), (3.0, 2.0)])
def test_codex_tool_output_counts_at_the_tool_output_seed_before_any_usage(head_ratio, window_ratio):
    # PR #244 review B, round 2 (D61): Codex responses that carry no input count until request 30, at bound
    # 30,000, with a 20k JSON-dense head and dense tool output (code, non-ASCII). function_call_output was
    # estimated at the 3.5 text seed, so turn starts went out at up to 36,011 real tokens with nothing
    # logged (here 30,851 to 39,983 on main). Counted at the 1.5 tool-output seed, as Claude's tool_result is,
    # no turn start goes over the bound unflagged.
    from test_codex import call, lead, output, prompt
    from test_codex import request as codex_request

    cfg = Config(bound=30_000, headroom_floor=5_000, min_turn_room=1_000, pin_budget=200, index_budget=200)
    tools = [{"type": "function", "name": f"synthetic_tool_{i}", "description": "synthetic tool " * 30,
              "parameters": {"type": "object", "properties": {f"p{j}": {"type": "string", "enum": ["a", "b"]}
                                                               for j in range(12)}}} for i in range(40)]
    code = "for (int i = 0; i < n; ++i) { a[i] = b[i] * c[i]; }\n" * 40

    def make(items):
        return codex_request(items, instructions="synthetic system prompt\n" * 20, tools=tools)

    def real(upstream):  # the stub tokenizer: the head and the items at their own ratios
        head = canonical_bytes({"instructions": upstream["instructions"], "tools": upstream["tools"]})
        return int(len(head) / head_ratio + len(canonical_bytes(upstream["input"])) / window_ratio)

    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    assert real(make([])) > 14_000  # the head alone, before any item
    items, sent, unflagged = lead() + [prompt("task")], 0, []
    for turn in range(6):
        for step in range(6):
            upstream, _, ev = engine(make(items), {}, "/responses")
            sent += 1
            if step == 0 and real(upstream) > cfg.bound and not FAILURES & {e["event"] for e in ev}:
                unflagged.append((sent, real(upstream)))
            if sent >= 30:
                engine.observe_usage("/responses", {"input_tokens": real(upstream), "output_tokens": 5}, make(items))
            items = items + [call(sent), output(sent, code)]
        items = items + [prompt(f"next {turn}")]
    assert sent == 36 and not unflagged
    assert main_state(engine).epoch > 0  # the pre-send estimate paged instead


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
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=UNPAGED)
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
