"""The rule 16 epoch record (M2 B31, contract PR #176): the store side of resuming after a restart."""

import json
from pathlib import Path

import pytest

from contexto.store import EPOCH_FIELDS, SessionStore
from contexto.store.memory import MemoryStore

RECORD = {
    "epoch": 3, "k": 41, "strip_upto": 39, "turns_paged": 12, "max_turn_growth": 9_100, "last_turn_count": 14,
    "carried_text_streak": 0, "window_minimal_count": 0, "last_turn_start_n": 40, "head_tokens": 47_600,
    "initial_carried": 2_100, "last_carried_text_count": None, "turn_room_low_epoch": None, "window_budget": 81_737,
    "pinned_text": "Earlier turns ...", "index_text": "pg_0000000000000000 turn 0: ...",
    "preamble_sha256": "a" * 64, "head_fingerprint": "f" * 64, "dialect": "anthropic", "conversation": "c" * 64,
    "model": "claude-opus-5-5", "paged_carried": ["d" * 64], "config": {"bound": 150_000, "headroom_floor": 25_000},
    "anchor": {"n": 41, "hash": "e" * 64},
}


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")


def test_the_record_round_trips_with_version_one(store):
    assert store.epoch_record() is None
    store.set_epoch(RECORD)
    assert store.epoch_record() == {**RECORD, "version": 1}
    newer = {**RECORD, "epoch": 4, "k": 52}
    store.set_epoch(newer)  # a full overwrite
    assert store.epoch_record() == {**newer, "version": 1}


def test_a_new_store_on_the_same_session_reads_it(tmp_path):
    SessionStore(tmp_path, "s1").set_epoch(RECORD)
    assert SessionStore(tmp_path, "s1").epoch_record() == {**RECORD, "version": 1}  # a restarted proxy


def test_the_record_is_private_atomic_and_never_logged(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.set_epoch(RECORD)
    path = store.dir / "epoch.json"
    assert path.stat().st_mode & 0o777 == 0o600 and not list(store.dir.glob(".epoch.json.*"))
    log = store.dir / "log.jsonl"
    assert not log.exists() or "Earlier turns" not in log.read_text()


@pytest.mark.parametrize("change", [
    lambda r: {**r, "version": 2}, lambda r: {k: v for k, v in r.items() if k != "anchor"},
    lambda r: {**r, "extra": 1}, lambda r: {**r, "k": -1}, lambda r: {**r, "k": True},
    lambda r: {**r, "preamble_sha256": "short"}, lambda r: {**r, "paged_carried": "x"},
    lambda r: {**r, "anchor": {"n": 1}}, lambda r: {**r, "model": 5}, lambda r: {**r, "config": []},
])
def test_a_wrong_shape_or_version_is_rejected_on_write(store, change):
    store.set_epoch(RECORD)
    with pytest.raises(ValueError):
        store.set_epoch(change(dict(RECORD)))
    assert store.epoch_record()["epoch"] == 3  # nothing changed


@pytest.mark.parametrize("stored", [
    b"{not json", b"[]", json.dumps({**RECORD, "version": 2}).encode(), json.dumps(RECORD).encode(),  # no version
    json.dumps({**RECORD, "version": 1, "k": "41"}).encode(), json.dumps({**RECORD, "version": 1, "x": 0}).encode(),
])
def test_a_malformed_or_unknown_version_record_reads_as_none(tmp_path, stored):
    store = SessionStore(tmp_path, "s1")
    (store.dir / "epoch.json").write_bytes(stored)
    assert store.epoch_record() is None


def test_a_failed_write_unlinks_the_previous_record_and_raises(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, "s1")
    store.set_epoch(RECORD)
    monkeypatch.setattr("os.fsync", lambda fd: (_ for _ in ()).throw(OSError("ENOSPC")))
    with pytest.raises(OSError):
        store.set_epoch({**RECORD, "epoch": 4})
    assert store.epoch_record() is None and not list(store.dir.glob(".epoch.json.*"))  # no stale epoch, no temp


def test_when_the_unlink_also_fails_the_older_record_is_the_newest_written(tmp_path, monkeypatch):
    """#176: 'the newest record written is trusted'; rules 4 and 5 bound a resume with a smaller k."""
    store = SessionStore(tmp_path, "s1")
    store.set_epoch(RECORD)
    monkeypatch.setattr(store, "_write_atomic", lambda path, data: (_ for _ in ()).throw(OSError("ENOSPC")))
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("ro")))
    with pytest.raises(OSError):
        store.set_epoch({**RECORD, "epoch": 4})
    assert store.epoch_record()["epoch"] == 3  # the last record actually written, still well-formed


def test_memory_store_returns_a_copy():
    store = MemoryStore("s1")
    store.set_epoch(RECORD)
    store.epoch_record()["anchor"]["n"] = 0
    assert store.epoch_record()["anchor"]["n"] == 41


def test_the_validated_fields_are_exactly_the_contracts():
    assert set(EPOCH_FIELDS) == set(RECORD)
