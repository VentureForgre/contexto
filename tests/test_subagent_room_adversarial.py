"""Reviewer A's adversarial probes for PR #209 (D40), kept as regression tests.

The stale-room probe originally made 40 main recalls at one instant. Side rooms now expire
``SIDE_ROOM_TTL_S`` after their publish, so it advances a clock by 30 seconds per main step, and also
checks that the stale room still bounds main while it is live. The round-2 Bash probe used Claude
Code's default 2-minute Bash timeout; it is kept with the 10-minute maximum, and a second test pins
the documented limit for a call that starts after the expiry. Of the round-3 placeholder-fork
probes, the first passes and the second is a documented limitation (strict xfail). The round-4
probe (a CLI-injected turn closing a running background subagent's room) passes since rooms close
early only on evidence that their own agent stopped.
"""

from __future__ import annotations

import json

import pytest

from contexto.rewrite import MESSAGES, RewriteEngine
from contexto.store import SIDE_ROOM_TTL_S, SessionStore

from test_rewrite import assistant, conversation, request, tool_result, tool_use, user
from test_subagent_room import CONFIG, Clock, big_page, recall, respond, room_for, session


def test_probe_fork_with_the_same_first_message_closes_a_running_subagents_room(tmp_path):
    """A side request that forks subagent A (same first message, so the same conversation key) and
    answers without a tool call must not close A's room."""
    store = SessionStore(tmp_path, "s1")
    engine, _, sub_a, _ = session(store)
    fork = request(sub_a["messages"] + [assistant("so far"), user("summarize your progress in one line")])
    engine(fork, {}, MESSAGES)
    respond(engine, fork, 3_000, 20, tool_calls=False)
    pid = big_page(store)
    _, charged = recall(store, pid)  # A's call, released by A's last response
    assert charged <= room_for(120_000, 1_000)


def test_probe_two_subagents_with_the_same_prompt_share_one_room(tmp_path):
    """Two parallel subagents launched with the same prompt have the same key; the second one's
    publish must not replace (and reset) the first one's tight room."""
    store = SessionStore(tmp_path, "s1")
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)  # main's room 104,000
    store.set_window(200_000, 2.0)
    a1 = request(conversation("same prompt", 1, mid_loop=True))
    a2 = request(conversation("same prompt", 1))
    engine(a1, {}, MESSAGES)
    respond(engine, a1, 120_000, 1_000, tool_calls=True)  # A1's own room: 4,000
    engine(a2, {}, MESSAGES)
    respond(engine, a2, 10_000, 1_000, tool_calls=True)  # A2's own room: 114,000
    pid = big_page(store)
    _, charged = recall(store, pid)  # A1's call
    assert charged <= room_for(120_000, 1_000)


def test_probe_a_stale_side_room_starves_main_for_the_rest_of_its_turn(tmp_path):
    """A side room that is never closed (its last response held a tool call or was unknown, and the
    conversation never answers again) bounds main only until it expires, not for the rest of the turn."""
    store = SessionStore(tmp_path, "s1")
    store.clock = clock = Clock()
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    side = request(conversation("one-shot side", 1))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)  # main's room 104,000
    engine(side, {}, MESSAGES)
    respond(engine, side, 2_000, 50, tool_calls=None)  # unknown: opens a room that is never closed
    store.set_window(200_000, 2.0)
    side_room = room_for(2_000, 50)
    total_while_live = 0
    last = None
    for i in range(40):
        pid = big_page(store, 60_000 + i)
        _, last = recall(store, pid)
        if clock.now - 1_000_000.0 <= SIDE_ROOM_TTL_S:
            total_while_live += last
        respond(engine, main, 20_000)  # main republishes at each response: its own spend resets
        clock.now += 30  # one main step; 40 steps outlast the expiry
    assert total_while_live <= side_room  # while live, the side room bounds every charge
    assert last > 0 and store.side_rooms() == {}  # after it expired, main's own room is back


def test_probe_main_only_with_toolless_side_requests_is_unchanged(tmp_path):
    """Common case: main plus side requests without tool calls. No side_rooms key is ever written
    and main's charges are what they were before D40."""
    store = SessionStore(tmp_path, "s1")
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)
    for i in range(5):
        side = request(conversation(f"title {i}", 1))
        engine(side, {}, MESSAGES)
        respond(engine, side, 500, 10, tool_calls=False)
    store.set_window(200_000, 2.0)
    meta = json.loads((store.dir / "meta.json").read_text())
    assert "side_rooms" not in meta
    plain = SessionStore(tmp_path / "plain", "s1")
    other = RewriteEngine(plain, config=CONFIG)
    other(main, {}, MESSAGES)
    respond(other, main, 20_000)
    plain.set_window(200_000, 2.0)
    totals = []
    for s in (store, plain):
        pid = big_page(s, 400_000)
        totals.append(recall(s, pid)[1])
    assert totals[0] == totals[1]


def _twins(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.clock = clock = Clock()
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)  # main's room 104,000
    store.set_window(200_000, 2.0)
    first = conversation("same prompt", 1)
    a1 = request(first)
    a2 = request(conversation("same prompt", 1))
    engine(a1, {}, MESSAGES)
    respond(engine, a1, 120_000, 1_000, tool_calls=True)  # A1's room: 4,000; A1's slow tools run now
    clock.now += 1
    engine(a2, {}, MESSAGES)
    respond(engine, a2, 10_000, 1_000, tool_calls=True)  # A2's room: 114,000
    clock.now += 1
    a2_next = request(first + [tool_use(1), tool_result(1)])  # A2's quick tool finished: its step 2
    engine(a2_next, {}, MESSAGES)
    return engine, store, clock, a2_next


def test_probe_twin_step_two_takes_over_the_other_twins_room(tmp_path):
    """Both twins' rooms have the same lineage (the same first request). A2's second step must not
    continue A1's room and reset it while A1's calls are still in flight."""
    engine, store, clock, a2_next = _twins(tmp_path)
    respond(engine, a2_next, 11_000, 1_000, tool_calls=True)
    pid = big_page(store)
    _, charged = recall(store, pid)  # A1's call, released by A1's first response
    assert charged <= room_for(120_000, 1_000)


def test_probe_twin_last_step_closes_the_other_twins_room(tmp_path):
    engine, store, clock, a2_next = _twins(tmp_path)
    respond(engine, a2_next, 11_000, 50, tool_calls=False)  # A2 is done
    pid = big_page(store)
    _, charged = recall(store, pid)
    assert charged <= room_for(120_000, 1_000)


def test_probe_a_call_after_a_ten_minute_bash_still_has_its_room(tmp_path):
    """[Bash (runs to Claude Code's 10-minute maximum timeout), contexto_recall] in one response."""
    store = SessionStore(tmp_path, "s1")
    store.clock = clock = Clock()
    session(store)
    clock.now += 600.5
    pid = big_page(store)
    _, charged = recall(store, pid)
    assert charged <= room_for(120_000, 1_000)


def test_a_call_after_the_expiry_is_charged_against_main_only(tmp_path):
    """The documented limit: a call held past the expiry (an unanswered permission prompt, say)
    finds its room gone and is charged against main's room, as before D40."""
    store = SessionStore(tmp_path, "s1")
    store.clock = clock = Clock()
    session(store)
    clock.now += SIDE_ROOM_TTL_S + 1
    pid = big_page(store)
    _, charged = recall(store, pid)
    assert charged > room_for(120_000, 1_000)


def _subagent_with_ids(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.clock = Clock()
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)  # main's room 104,000
    store.set_window(200_000, 2.0)
    a = request(conversation("subagent A", 1))
    engine(a, {}, MESSAGES)
    respond(engine, a, 120_000, 1_000, tool_calls=True, tool_call_ids=("toolu_9",))  # A's room 4,000
    return engine, store, a


def _fork_of(a, separate_prompt=True):
    """A fork of A made while A's call toolu_9 runs: it copies A's history and, to stay a valid
    request, closes the dangling tool_use with a placeholder result."""
    msgs = a["messages"] + [tool_use(9), tool_result(9, "[still running]")]
    if separate_prompt:
        msgs.append(user("In one line, what are you doing?"))
    else:
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] + [{"type": "text", "text": "In one line?"}]}
    return request(msgs)


def test_probe_fork_with_placeholder_results_closes_the_subagents_room(tmp_path):
    """The fork adds its own user prompt (a turn start), so it does not continue A's room."""
    engine, store, a = _subagent_with_ids(tmp_path)
    fork = _fork_of(a)
    engine(fork, {}, MESSAGES)
    respond(engine, fork, 120_500, 30, tool_calls=False)
    pid = big_page(store)
    _, charged = recall(store, pid)  # A's in-flight call
    assert charged <= room_for(120_000, 1_000)


@pytest.mark.xfail(strict=True, reason="documented D40 limitation: a fork that answers A's running call with a "
                                       "placeholder result and asks inside that message looks like A's next step")
def test_probe_fork_with_placeholder_results_resets_the_subagents_spend(tmp_path):
    engine, store, a = _subagent_with_ids(tmp_path)
    total = 0
    for i in range(6):  # A's parallel recalls before the fork's response
        total += recall(store, big_page(store, 60_000 + i))[1]
    fork = _fork_of(a, separate_prompt=False)
    engine(fork, {}, MESSAGES)
    respond(engine, fork, 120_500, 30, tool_calls=True, tool_call_ids=("toolu_f1",))
    for i in range(6):  # A's remaining recalls of the same response
        total += recall(store, big_page(store, 70_000 + i))[1]
    assert total <= room_for(120_000, 1_000)


def test_probe_legit_next_step_continues_by_id(tmp_path):
    """Control: A's own next request (results for toolu_9) continues and replaces A's room: one room."""
    engine, store, a = _subagent_with_ids(tmp_path)
    nxt = request(a["messages"] + [tool_use(9), tool_result(9)])
    engine(nxt, {}, MESSAGES)
    respond(engine, nxt, 121_000, 1_000, tool_calls=True, tool_call_ids=("toolu_10",))
    assert len(store.side_rooms()) == 1


def test_probe_idle_close_takes_a_running_background_subagents_room(tmp_path):
    """Background subagent B's response holds [Bash (3 minutes), contexto_recall]. Meanwhile main
    gets a new turn start that no human typed (Claude Code's notification that another background
    agent finished) and keeps working past B's 2-minute idle mark. B's room is closed while its
    recall is still queued behind the Bash."""
    store = SessionStore(tmp_path, "s1")
    store.clock = clock = Clock()
    engine = RewriteEngine(store, config=CONFIG)
    main = request(conversation("main", 3))
    engine(main, {}, MESSAGES)
    respond(engine, main, 20_000)
    store.set_window(200_000, 2.0)
    b = request(conversation("background B", 1))
    engine(b, {}, MESSAGES)
    respond(engine, b, 120_000, 1_000, tool_calls=True, tool_call_ids=("toolu_b1", "toolu_b2"))  # B: 4,000
    clock.now += 30
    notified = request(main["messages"] + [assistant("started both agents"),
                                           user("<task-notification>agent C completed</task-notification>")])
    engine(notified, {}, MESSAGES)  # main's new turn, no human
    respond(engine, notified, 21_000)
    clock.now += 95  # 125 s after B's publish; B's Bash still runs
    step = request(notified["messages"] + [assistant("reading C's report")])
    engine(step, {}, MESSAGES)
    clock.now += 60  # B's Bash ends at 3 minutes; its recall starts
    _, charged = recall(store, big_page(store))
    assert charged <= room_for(120_000, 1_000)
