"""Per-turn recall budget (M2 B11, contract PR #69): the room the engine publishes, and its charges."""

import io
import json
import math
import threading

import pytest

from contexto import mcp
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore

SPENT = "Recall budget for this turn is used up; answer from what is in view, or say the detail is unknown."
RATIO = 1.5  # set_window(200_000, 2.0): min(measured 2.0, 1.5) characters per token


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")


def call(store, name, arguments=None):
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments or {}}})
    return server.handle_line(line)["result"]


def text_of(result):
    assert result["isError"] is False and "structuredContent" not in result
    return "".join(block["text"] for block in result["content"])


def page(store, chars=6_000, turn=1):
    (pid,) = store.page_out([[{"role": "user", "content": "a" * chars}]], turn)
    return pid


def calls(store):
    events = store.events if isinstance(store, MemoryStore) else [
        json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
    return [e for e in events if e.get("event") in ("mcp_call", "turn_room_missing")]


# The store side


def test_spent_resets_only_when_the_turn_id_changes(store):
    assert store.turn_room() == (None, None, 0)
    store.set_turn_room("7", 1_000)
    store.charge_turn_room(lambda remaining: 300)
    store.set_turn_room("7", 800)  # the same turn's real usage arrives: room updated, spent kept
    assert store.turn_room() == ("7", 800, 300)
    store.set_turn_room("8", 900)
    assert store.turn_room() == ("8", 900, 0)
    store.set_turn_room(8, 500)  # an integer id is the same turn as its string
    assert store.turn_room() == ("8", 500, 0)


@pytest.mark.parametrize("turn_id, room", [("", 10), (None, 10), (True, 10), ("1", 1.5), ("1", "10"), ("1", False)])
def test_set_turn_room_rejects_bad_values(store, turn_id, room):
    with pytest.raises(ValueError):
        store.set_turn_room(turn_id, room)


def test_a_charge_sees_what_remains_and_may_not_overspend(store):
    seen = []
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)  # nothing published
    store.set_turn_room("1", 100)
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 60)
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)
    assert seen == [None, 100, 40]
    with pytest.raises(ValueError):
        store.charge_turn_room(lambda remaining: 41)
    store.set_turn_room("1", 50)  # a room below what was spent leaves nothing, never a negative charge
    store.charge_turn_room(lambda remaining: seen.append(remaining) or 0)
    assert seen[-1] == 0 and store.turn_room() == ("1", 50, 60)


def test_the_turn_room_lives_in_private_meta_beside_the_window(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.set_window(33_000, 1.2)
    store.set_turn_room("3", 2_000)
    store.charge_turn_room(lambda remaining: 5)
    meta = json.loads((store.dir / "meta.json").read_text())
    assert meta["turn_room"] == {"turn_id": "3", "room_tokens": 2_000, "spent_tokens": 5}
    assert (meta["window_tokens"], meta["bytes_per_token"]) == (33_000, 1.2)
    assert (store.dir / "meta.json").stat().st_mode & 0o777 == 0o600
    assert SessionStore(tmp_path, "s1").turn_room() == ("3", 2_000, 5)  # survives a restart


@pytest.mark.parametrize("bad", [[], "x", {"turn_id": 3, "room_tokens": 10}, {"turn_id": "3"},
                                 {"turn_id": "3", "room_tokens": 10, "spent_tokens": -2}])
def test_a_malformed_record_reads_as_unpublished_or_unspent(tmp_path, bad):
    store = SessionStore(tmp_path, "s1")
    store._update_meta({"turn_room": bad})
    turn_id, room, spent = store.turn_room()
    assert spent == 0 and (turn_id is None) == (room is None)


def test_parallel_charges_and_engine_publishes_never_overspend(tmp_path):
    """Threads hold separate meta.lock descriptors, so flock serializes them as it does processes."""
    store = SessionStore(tmp_path, "s1")
    store.set_window(200_000, 2.0)
    store.set_turn_room("1", 1_000)
    granted: list[int] = []
    guard = threading.Lock()

    def charge(remaining):
        used = min(remaining, 7)
        with guard:
            granted.append(used)
        return used

    def worker(i):
        s = SessionStore(tmp_path, "s1")
        for _ in range(25):
            s.charge_turn_room(charge)
            if i == 0:
                s.set_turn_room("1", 1_000)  # the engine republishing the same turn keeps the counter
                s.set_window(200_000, 2.0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]  # V2 fired 15 searches at once
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 1_000 and store.turn_room() == ("1", 1_000, 1_000)
    assert store.window() == (200_000, 2.0)


class RecordingStore(SessionStore):
    """A SessionStore that also keeps the events its own server logged, to pair replies with charges."""

    def __init__(self, *args):
        super().__init__(*args)
        self.mine = []

    def log(self, event):
        self.mine.append(event)
        super().log(event)


@pytest.mark.parametrize("room", [3_000, 20_000, 0])
def test_fifteen_concurrent_tool_calls_charge_exactly_the_room(tmp_path, room):
    """Intake (03:26Z): V2 issued 15 parallel searches in one response. 15 servers, each with its own
    store on one session directory, call recall and search at once while the engine republishes."""
    store = SessionStore(tmp_path, "s1")
    store.set_window(200_000, 2.0)
    pids = [store.page_out([[{"role": "user", "content": f"needle {i} " + "x" * 5_000}]], i + 1)[0]
            for i in range(20)]
    store.set_turn_room("7", room)
    jobs = [("contexto_search", {"query": "needle"}) if i % 2 else ("contexto_recall", {"page_id": pids[i]})
            for i in range(15)]
    start, stop = threading.Barrier(len(jobs) + 1), threading.Event()
    replies = [None] * len(jobs)

    def worker(i):
        own = RecordingStore(tmp_path, "s1")
        start.wait()
        replies[i] = (text_of(call(own, *jobs[i])), [e for e in own.mine if e["event"] == "mcp_call"])

    def engine():
        own = SessionStore(tmp_path, "s1")
        start.wait()
        while not stop.is_set():
            own.set_turn_room("7", room)  # the same turn: the counter is kept
            own.set_window(200_000, 2.0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(jobs))]
    publisher = threading.Thread(target=engine)
    for t in threads + [publisher]:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    publisher.join()

    charges = []
    for text, events in replies:
        [event] = events
        charges.append(event["charged_tokens"])
        if text == SPENT:  # nothing useful fit: the exact reply, charged at what was left, up to its size
            assert event["turn_budget"] == "spent" and event["charged_tokens"] <= math.ceil(len(SPENT) / RATIO)
        else:  # what came back is exactly what was charged
            assert event["turn_budget"] == "charged" and event["charged_tokens"] == math.ceil(len(text) / RATIO)
    assert sum(charges) == store.turn_room()[2] <= room
    assert store.turn_room()[:2] == ("7", room)
    spent_replies = sum(text == SPENT for text, _ in replies)
    if room == 0:
        assert spent_replies == 15
    elif room == 3_000:  # one full recall alone is more than this room: someone gets the reply
        assert 0 < spent_replies < 15
    assert not any(e.get("event") == "turn_room_missing" for e in calls(store))


# The tools


def test_message_text_is_exact():
    assert mcp.BUDGET_SPENT == SPENT


def test_recall_is_cut_to_the_room_with_the_usual_offset_hint(store):
    store.set_window(200_000, 2.0)
    pid = page(store)
    store.set_turn_room("1", 1_000)  # 1,500 characters
    result = call(store, "contexto_recall", {"page_id": pid})
    chunk, hint = (block["text"] for block in result["content"])
    assert len(chunk) + len(hint) <= 1_500 and len(chunk) > 1_000
    assert f"call contexto_recall with page_id {pid} and offset {len(chunk)};" in hint
    assert store.turn_room()[2] == math.ceil((len(chunk) + len(hint)) / RATIO)
    assert calls(store)[-1]["turn_budget"] == "charged"


def test_recall_returns_the_message_once_the_room_is_spent_and_resets_next_turn(store):
    store.set_window(200_000, 2.0)
    pid = page(store)
    store.set_turn_room("1", 1_000)
    offset, replies = 0, []
    for _ in range(20):  # each partial reply spends room; the hint alone soon no longer fits
        reply = text_of(call(store, "contexto_recall", {"page_id": pid, "offset": offset}))
        if reply == SPENT:
            break
        replies.append(reply)
        offset = int(reply.rsplit("and offset ", 1)[1].split(";")[0])
    assert reply == SPENT and replies and store.turn_room()[2] <= 1_000
    assert calls(store)[-1]["turn_budget"] == "spent" and calls(store)[-1]["chars"] == 0
    store.set_turn_room("2", 1_000)  # a new turn: the counter resets
    again = text_of(call(store, "contexto_recall", {"page_id": pid, "offset": offset}))
    assert again != SPENT and again.startswith("a")


def test_search_and_state_are_charged_too(store):
    store.set_window(200_000, 2.0)
    for i in range(30):
        page(store, chars=40, turn=i + 1)
    store.pin("decision", "ship the blue build " * 20)
    store.set_turn_room("1", 10_000)
    found = text_of(call(store, "contexto_search", {"query": "aaa"}))
    after_search = store.turn_room()[2]
    assert after_search == math.ceil(len(found) / RATIO)
    state = text_of(call(store, "contexto_state"))
    assert store.turn_room()[2] == after_search + math.ceil(len(state) / RATIO)
    assert "ship the blue build" in state


def test_search_shows_whole_lines_that_fit_then_the_message(store):
    store.set_window(200_000, 2.0)
    for i in range(30):
        page(store, chars=40, turn=i + 1)
    full = text_of(call(store, "contexto_search", {"query": "aaa"}))
    store.set_turn_room("1", 400)  # 600 characters: some lines, not all 30
    cut = text_of(call(store, "contexto_search", {"query": "aaa"}))
    lines = cut.split("\n")
    assert len(cut) <= 600 and 1 < len(lines) < 30
    assert all(line in full.split("\n") for line in lines[:-1])
    assert lines[-1] == f"[{31 - len(lines)} more matching pages not shown; the recall budget for this turn is used up]"
    assert calls(store)[-1]["hits"] == len(lines) - 1
    store.set_turn_room("1", 10)  # same turn, room cut below what was spent
    assert text_of(call(store, "contexto_search", {"query": "aaa"})) == SPENT


def test_state_is_cut_at_a_line_and_says_so(store):
    store.set_window(200_000, 2.0)
    for i in range(20):
        store.pin("obligation", f"follow up on item {i} " + "x" * 60)
    full = store.state_text()
    store.set_turn_room("1", 400)
    cut = text_of(call(store, "contexto_state"))
    head, trailer = cut.rsplit("\n", 1)
    assert len(cut) <= 600 and full.startswith(head + "\n")
    assert trailer == (f"[{len(full) - len(head) - 1} more characters of the pinned state not shown; "
                       "the recall budget for this turn is used up]")
    store.set_turn_room("1", 0)
    assert text_of(call(store, "contexto_state")) == SPENT


def test_without_a_window_the_ratio_is_one_character_per_token(store):
    pid = page(store, chars=100)
    store.set_turn_room("1", 1_000)
    text = text_of(call(store, "contexto_recall", {"page_id": pid}))
    assert store.turn_room()[2] == len(text)


def test_no_engine_means_no_turn_budget_and_no_event(store):
    pid = page(store, chars=20_000)
    text = text_of(call(store, "contexto_recall", {"page_id": pid}))
    assert len(text) > 12_000  # the per-call default limit plus its hint, nothing smaller
    assert store.turn_room() == (None, None, 0)
    assert [e["event"] for e in calls(store)] == ["mcp_call"] and calls(store)[0]["turn_budget"] == "unbudgeted"


def test_an_engine_without_a_published_room_logs_turn_room_missing(store):
    store.set_window(200_000, 2.0)
    pid = page(store)
    assert len(text_of(call(store, "contexto_recall", {"page_id": pid}))) > 6_000  # the per-call limit only
    text_of(call(store, "contexto_search", {"query": "aaa"}))
    missing = [e for e in calls(store) if e["event"] == "turn_room_missing"]
    assert [e["tool"] for e in missing] == ["contexto_recall", "contexto_search"]


def test_pin_and_close_are_not_charged(store):
    store.set_window(200_000, 2.0)
    store.set_turn_room("1", 0)
    result = call(store, "contexto_pin", {"kind": "obligation", "text": "write the report"})
    assert result["isError"] is False and result["structuredContent"]["record_id"]
    assert store.turn_room()[2] == 0
