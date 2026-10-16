"""One contexto_recall returns at most half of the room that remains (M2 B16, contract PR #96)."""

import io
import json
import math

import pytest

from contexto import mcp
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.render import RECALL_LIMIT, render_page

SPENT = mcp.BUDGET_SPENT
RATIO = 1.5  # set_window(200_000, 2.0): min(2.0, 1.5) characters per token


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    s = SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")
    s.set_window(200_000, 2.0)
    return s


def page(store, chars):
    text = "".join(f"line {i:05d} level=INFO shard={i % 9} ok\n" for i in range(chars // 36 + 1))[:chars]
    (pid,) = store.page_out([[{"role": "user", "content": text}]], 1)
    return pid


def recall(store, page_id, offset=0):
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "contexto_recall", "arguments": {"page_id": page_id, "offset": offset}}})
    result = server.handle_line(line)["result"]
    assert result["isError"] is False and "structuredContent" not in result
    return [block["text"] for block in result["content"]]


def next_offset(hint):
    return int(hint.rsplit("and offset ", 1)[1].split(";")[0])


def remaining(store):
    _, room, spent = store.turn_room()
    return room - spent


def test_a_page_larger_than_half_the_room_returns_at_most_half_and_the_next_offset(store):
    pid = page(store, 40_000)
    store.set_turn_room("1", 10_000)
    chunk, hint = recall(store, pid)
    assert len(chunk) + len(hint) <= (10_000 // 2) * RATIO
    assert len(chunk) > (10_000 // 2) * RATIO - 200  # and close to it: the hint takes the rest
    assert next_offset(hint) == len(chunk)
    assert remaining(store) >= 10_000 // 2  # one recall cannot consume the turn


def test_consecutive_recalls_each_take_at_most_half_of_what_remains_then(store):
    pid = page(store, 40_000)
    store.set_turn_room("1", 10_000)
    offset = 0
    for _ in range(4):
        before = remaining(store)
        chunk, hint = recall(store, pid, offset)
        taken = before - remaining(store)
        assert taken == math.ceil((len(chunk) + len(hint)) / RATIO) and taken <= before // 2
        offset = next_offset(hint)
    assert remaining(store) >= 10_000 // 16  # four halvings leave a sixteenth


def test_offset_paging_reaches_the_end_of_the_page_byte_exact(store):
    pid = page(store, 30_000)
    full = render_page(store.get_page(pid))
    pieces, offset = [], 0
    for turn in range(1, 50):
        store.set_turn_room(str(turn), 3_000)  # a new turn each time: a fresh room
        blocks = recall(store, pid, offset)
        pieces.append(blocks[0])
        if len(blocks) == 1:
            break
        offset = next_offset(blocks[1])
        assert offset == sum(map(len, pieces))
    assert "".join(pieces) == full


def test_a_page_smaller_than_half_the_room_returns_whole(store):
    pid = page(store, 1_000)
    store.set_turn_room("1", 10_000)
    [whole] = recall(store, pid)
    assert whole == render_page(store.get_page(pid))
    assert remaining(store) == 10_000 - math.ceil(len(whole) / RATIO)


def test_a_room_published_mid_turn_recomputes_the_half(store):
    pid = page(store, 40_000)
    store.set_turn_room("1", 10_000)
    chunk, hint = recall(store, pid)
    store.set_turn_room("1", 4_000)  # real usage arrives for the same turn (B14 reset)
    chunk2, hint2 = recall(store, pid, next_offset(hint))
    assert len(chunk2) + len(hint2) <= (4_000 // 2) * RATIO < len(chunk) + len(hint)
    assert remaining(store) == 4_000 - math.ceil((len(chunk2) + len(hint2)) / RATIO)


def test_the_per_call_cap_still_bounds_a_large_room(store):
    pid = page(store, 80_000)
    store.set_turn_room("1", 1_000_000)  # half is far above the B9 cap
    chunk, hint = recall(store, pid)
    assert len(chunk) == RECALL_LIMIT and next_offset(hint) == RECALL_LIMIT


NEARLY = ("The recall budget for this turn is nearly used up (about {} tokens left), too little for a recall. "
          "Use contexto_search with an exact key to get the matching lines, answer from what is in view, or say "
          "the detail is unknown.")


def test_a_small_room_gets_the_nearly_used_up_reply_uncharged_and_search_still_fits(store):
    """Intake (05:20Z): room left, but half of it cannot hold RECALL_MIN_CHARS of page text and the hint."""
    pid = page(store, 40_000)
    store.set_turn_room("1", 300)  # half is 150 tokens, 225 characters: the hint leaves under 200 for text
    assert recall(store, pid) == [NEARLY.format(300)]
    assert store.turn_room() == ("1", 300, 0)  # charged nothing
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    reply = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "contexto_search", "arguments": {"query": "line 00500 "}}}))["result"]["content"][0]["text"]
    assert reply.startswith(f"{pid} (turn 1): ") and "> " in reply  # a search hit that fits still returns
    assert 0 < store.turn_room()[2] <= 300
    left = 300 - store.turn_room()[2]
    assert recall(store, pid) == [NEARLY.format(left)]  # N is the room that remains
    store.charge_turn_room(lambda remaining: remaining)  # spend the rest
    assert recall(store, pid) == [SPENT]  # an empty room keeps the used-up reply


def test_the_nearly_used_up_boundary_is_at_recall_min_chars_of_page_text(store):
    """At the room where half holds exactly RECALL_MIN_CHARS of page text and its hint: a recall; one
    token-pair below: the nearly-used-up reply, uncharged (contract PR #103)."""
    from contexto.store.render import recall_limit

    pid = page(store, 40_000)
    total = len(render_page(store.get_page(pid)))
    minimum = mcp.RECALL_MIN_CHARS
    hint = (f"[{total - minimum} more characters: call contexto_recall with page_id {pid} and offset {minimum}; "
            f"{recall_limit(*store.window())[1]}]")
    half = math.ceil((minimum + len(hint)) / RATIO)  # the fewest half-room tokens that hold both
    store.set_turn_room("1", 2 * half)
    chunk, _ = recall(store, pid)
    assert len(chunk) >= minimum
    store.set_turn_room("2", 2 * half - 2)  # one token less of half-room
    assert recall(store, pid) == [NEARLY.format(2 * half - 2)]
    assert store.turn_room() == ("2", 2 * half - 2, 0)


def test_a_short_rest_of_the_page_is_returned_even_below_the_threshold(store):
    pid = page(store, 1_000)
    full = render_page(store.get_page(pid))
    store.set_turn_room("1", 300)  # 225 characters of half-room
    [rest] = recall(store, pid, len(full) - 50)  # 50 characters left: they fit whole, no hint needed
    assert rest == full[-50:]


def test_an_empty_room_keeps_the_used_up_reply(store):
    pid = page(store, 40_000)
    store.set_turn_room("1", 0)
    assert recall(store, pid) == [SPENT]


def test_no_room_published_keeps_only_the_per_call_cap():
    store = MemoryStore("s1")  # no engine: no window, no room
    pid = page(store, 40_000)
    chunk, _ = recall(store, pid)
    assert len(chunk) == 12_000  # the B9 default before a window is recorded



def test_a_no_hit_search_at_a_room_smaller_than_its_text_returns_it_uncharged(store):
    page(store, 1_000)
    store.set_turn_room("1", 5)  # 7 characters, far less than the reply
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    reply = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "contexto_search", "arguments": {"query": "absent-key"}}}))["result"]["content"][0]["text"]
    assert reply == mcp.NO_HIT
    assert store.turn_room() == ("1", 5, 0)


def test_the_used_up_reply_is_never_charged(store):
    pid = page(store, 40_000)
    store.set_turn_room("1", 0)
    assert recall(store, pid) == [SPENT]
    assert store.turn_room() == ("1", 0, 0)
