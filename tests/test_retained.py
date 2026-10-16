"""The retained-window snapshot for in-view search (M2 B19 b, contract PR #125; lane A's A14 calls it)."""

import json
from pathlib import Path

import pytest

from contexto.store import SessionStore
from contexto.store.memory import MemoryStore

ENTRIES = [{"turn": 16, "message_index": 0, "role": "user", "text": "cfg cache.ttl=short"},
           {"turn": 16, "message_index": 1, "role": "assistant", "text": "OK"}]


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")


def request(store, entries=ENTRIES, room=10_000, turn="5"):
    """One main-conversation request as lane A drives it: snapshot, then the request-time room."""
    store.set_retained(2, 9, entries)
    store.set_turn_room(turn, room)


def test_a_snapshot_and_its_rooms_share_the_request_stamp(store):
    assert store.retained() is None and store.room_stamp() is None
    request(store)
    epoch, k, stamp, entries = store.retained()
    assert (epoch, k, entries) == (2, 9, ENTRIES) and stamp == store.room_stamp() == 1
    store.set_turn_room("5", 8_000)  # the response-time publish of the same request
    assert store.room_stamp() == 1
    store.charge_turn_room(lambda remaining: 10)  # a charge keeps the stamp
    assert store.room_stamp() == 1
    request(store)  # the next request: a new snapshot, a new stamp
    assert store.retained()[2] == store.room_stamp() == 2


@pytest.mark.parametrize("bad", [
    [{"turn": 1, "message_index": 0, "role": "system", "text": "x"}],
    [{"turn": 1, "message_index": 0, "role": "user"}],
    [{"turn": -1, "message_index": 0, "role": "user", "text": "x"}],
    [{"turn": 1, "message_index": 0, "role": "user", "text": 3}],
    "not a list",
])
def test_bad_entries_are_rejected_and_nothing_changes(store, bad):
    request(store)
    with pytest.raises(ValueError):
        store.set_retained(2, 9, bad)
    assert store.retained()[2] == store.room_stamp() == 1


def test_the_snapshot_is_private_atomic_and_survives_a_restart(tmp_path):
    store = SessionStore(tmp_path, "s1")
    request(store)
    path = store.dir / "retained.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(store.dir.glob(".retained.json.*"))  # no temp file left behind
    reopened = SessionStore(tmp_path, "s1")
    assert reopened.retained() == store.retained()
    request(reopened)  # the counter is persisted: a new store object continues it
    assert reopened.retained()[2] == reopened.room_stamp() == 2
    assert json.loads((store.dir / "meta.json").read_text())["request_seq"] == 2


def test_a_failed_increment_removes_the_old_snapshot(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, "s1")
    request(store)
    monkeypatch.setattr(store, "_modify_meta", lambda change: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.set_retained(2, 9, ENTRIES)
    assert store.retained() is None


def test_a_failed_increment_and_failed_unlink_still_never_match(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, "s1")
    request(store)
    real_modify = store._modify_meta
    monkeypatch.setattr(store, "_modify_meta", lambda change: (_ for _ in ()).throw(OSError("disk full")))
    real_unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("ro")))
    with pytest.raises(OSError):
        store.set_retained(2, 9, ENTRIES)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    monkeypatch.setattr(store, "_modify_meta", real_modify)
    store.set_turn_room("5", 10_000)  # lane A publishes the room anyway
    assert store.retained()[2] == 1  # the old snapshot is still on disk ...
    assert store.room_stamp() is None  # ... but the room is unstamped, so it cannot match


def test_a_failed_snapshot_write_leaves_the_old_snapshot_stale_by_stamp(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, "s1")
    request(store)
    monkeypatch.setattr(store, "_write_atomic", lambda path, data: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.set_retained(2, 9, ENTRIES)
    store.set_turn_room("5", 10_000)
    assert store.retained()[2] == 1 and store.room_stamp() == 2  # mismatch: in view not checked


def test_a_failed_room_publish_after_a_good_snapshot_never_matches_the_old_room(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, "s1")
    request(store)
    store.set_retained(2, 9, ENTRIES)  # the next request's snapshot, stamp 2
    monkeypatch.setattr(store, "_update_meta", lambda updates: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.set_turn_room("6", 10_000)
    assert store.retained()[2] == 2 and store.room_stamp() == 1  # the old room's stamp: mismatch


def test_a_non_main_conversation_leaves_both_untouched(tmp_path):
    store = SessionStore(tmp_path, "s1")
    request(store)
    before = ((store.dir / "retained.json").read_bytes(), json.loads((store.dir / "meta.json").read_text()))
    store.charge_turn_room(lambda remaining: 0)  # a tool call; lane A calls neither method here
    assert ((store.dir / "retained.json").read_bytes(), json.loads((store.dir / "meta.json").read_text())) == before


def test_a_malformed_snapshot_reads_as_none(tmp_path):
    store = SessionStore(tmp_path, "s1")
    for bad in (b"{not json", b"[]", b'{"stamp": "1", "epoch": 1, "k": 1, "entries": []}'):
        (store.dir / "retained.json").write_bytes(bad)
        assert store.retained() is None


def test_the_snapshot_is_never_logged(tmp_path):
    store = SessionStore(tmp_path, "s1")
    request(store)
    log = store.dir / "log.jsonl"
    assert not log.exists() or "cache.ttl" not in log.read_text()
