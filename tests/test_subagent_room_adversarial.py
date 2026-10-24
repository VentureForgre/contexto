"""Reviewer A's adversarial probes for PR #209 (D40), kept as regression tests.

The stale-room probe originally made 40 main recalls at one instant. Side rooms now expire
``SIDE_ROOM_TTL_S`` after their publish, so it advances a clock by 5 seconds per main step, and also
checks that the stale room still bounds main while it is live.
"""

from __future__ import annotations

import json

from contexto.rewrite import MESSAGES, RewriteEngine
from contexto.store import SIDE_ROOM_TTL_S, SessionStore

from test_rewrite import assistant, conversation, request, user
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
        clock.now += 5  # one main step
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
