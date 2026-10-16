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
    "preamble_sha256": "a" * 64, "head_fingerprint": "f" * 64, "dialect": "anthropic",
    "conversation": "anthropic:" + "c" * 64, "model": "claude-opus-5-5", "paged_carried": ["d" * 64],
    "config": {"bound": 150_000, "headroom_floor": 25_000}, "anchor": {"n": 41, "hash": "e" * 64},
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
    lambda r: {**r, "conversation": "c" * 64}, lambda r: {**r, "dialect": "openai"},
    lambda r: {**r, "head_fingerprint": "F" * 64}, lambda r: {**r, "paged_carried": ["short"]},
    lambda r: {**r, "anchor": {"n": 1, "hash": "x"}}, lambda r: {**r, "head_tokens": -1},
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



def test_the_engines_record_as_lane_a_sends_it(tmp_path):
    """Lane A's A30 writer (builder-1, 16:19Z): exactly these 24 keys and value shapes; no version."""
    import dataclasses

    from contexto.rewrite import Config

    record = {
        "epoch": 2, "k": 37, "strip_upto": 35, "turns_paged": 9, "max_turn_growth": 8_200, "last_turn_count": 12,
        "carried_text_streak": 1, "window_minimal_count": 0, "last_turn_start_n": 36, "head_tokens": 47_600,
        "initial_carried": 2_400, "last_carried_text_count": 3, "turn_room_low_epoch": None,
        "window_budget": -1_250,  # W can be negative (bound_too_small)
        "pinned_text": "pinned", "index_text": "", "preamble_sha256": "0" * 64, "head_fingerprint": "1" * 64,
        "dialect": "responses", "conversation": "responses:" + "2" * 64, "model": None,
        "paged_carried": [], "config": dataclasses.asdict(Config()), "anchor": {"n": 37, "hash": "3" * 64},
    }
    assert len(record) == 24
    for store in (MemoryStore("s1"), SessionStore(tmp_path, "s1")):
        store.set_epoch(record)
        assert store.epoch_record() == {**record, "version": 1}



# Reviewer-1 on #178: the version is exactly the integer 1, and paged_carried is sorted and unique


@pytest.mark.parametrize("version", [True, 1.0, "1", 2, None])
def test_a_version_other_than_the_integer_one_is_rejected_and_reads_as_none(tmp_path, version):
    store = SessionStore(tmp_path, "s1")
    with pytest.raises(ValueError):
        store.set_epoch({**RECORD, "version": version})
    (store.dir / "epoch.json").write_text(json.dumps({**RECORD, "version": version}))
    assert store.epoch_record() is None


def test_an_explicit_integer_version_one_is_accepted(store):
    store.set_epoch({**RECORD, "version": 1})
    assert store.epoch_record()["version"] == 1


@pytest.mark.parametrize("hashes", [["b" * 64, "a" * 64], ["a" * 64, "a" * 64]])
def test_paged_carried_must_be_sorted_and_unique(tmp_path, hashes):
    store = SessionStore(tmp_path, "s1")
    with pytest.raises(ValueError):
        store.set_epoch({**RECORD, "paged_carried": hashes})
    (store.dir / "epoch.json").write_text(json.dumps({**RECORD, "version": 1, "paged_carried": hashes}))
    assert store.epoch_record() is None
    store.set_epoch({**RECORD, "paged_carried": sorted(set(hashes))})
    assert store.epoch_record()["paged_carried"] == sorted(set(hashes))


@pytest.mark.parametrize("name", ["epoch", "k", "head_tokens", "window_budget"])
@pytest.mark.parametrize("value", [True, 1.0])
def test_integer_fields_reject_bools_and_floats(store, name, value):
    with pytest.raises(ValueError):
        store.set_epoch({**RECORD, name: value})
