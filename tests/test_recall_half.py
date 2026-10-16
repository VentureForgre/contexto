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


def test_a_tiny_room_gets_the_used_up_reply(store):
    pid = page(store, 40_000)
    store.set_turn_room("1", 150)  # half is 75 tokens: no room for a chunk and its offset hint
    assert recall(store, pid) == [SPENT]


def test_no_room_published_keeps_only_the_per_call_cap():
    store = MemoryStore("s1")  # no engine: no window, no room
    pid = page(store, 40_000)
    chunk, _ = recall(store, pid)
    assert len(chunk) == 12_000  # the B9 default before a window is recorded
