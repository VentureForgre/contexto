"""The model a session's window was measured on (M2 B12, for lane A's A2e ratio restore)."""

import json
import threading

import pytest

from contexto.store import MODEL_CHARS, SessionStore
from contexto.store.memory import MemoryStore


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")


def test_set_replace_and_clear_the_model(store):
    assert store.window_model() is None
    store.set_window(33_000, 1.2, model="claude-opus-5-5")
    assert store.window_model() == "claude-opus-5-5" and store.window() == (33_000, 1.2)
    store.set_window(40_000, 1.3, "claude-sonnet-5-5")
    assert store.window_model() == "claude-sonnet-5-5" and store.window() == (40_000, 1.3)
    store.set_window(40_000, 1.3)  # an existing call without a model clears it
    assert store.window_model() is None and store.window() == (40_000, 1.3)


@pytest.mark.parametrize("bad", ["", "x" * (MODEL_CHARS + 1), "claude\nopus", "model\x00", "tab\there", "\x1b[31m",
                                 42, b"claude", True])
def test_bad_models_are_rejected_and_nothing_is_written(store, bad):
    store.set_window(33_000, 1.2, model="claude-opus-5-5")
    with pytest.raises(ValueError):
        store.set_window(50_000, 2.0, model=bad)
    assert store.window_model() == "claude-opus-5-5" and store.window() == (33_000, 1.2)


def test_the_longest_allowed_model_is_kept(store):
    store.set_window(33_000, 1.2, model="m" * MODEL_CHARS)
    assert store.window_model() == "m" * MODEL_CHARS


def test_the_model_lives_in_private_meta_and_clearing_removes_the_key(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.set_window(33_000, 1.2, model="claude-opus-5-5")
    meta = json.loads((store.dir / "meta.json").read_text())
    assert meta["window_model"] == "claude-opus-5-5"
    assert (store.dir / "meta.json").stat().st_mode & 0o777 == 0o600
    assert SessionStore(tmp_path, "s1").window_model() == "claude-opus-5-5"  # survives a restart
    store.set_window(33_000, 1.2)
    assert "window_model" not in json.loads((store.dir / "meta.json").read_text())


@pytest.mark.parametrize("bad", [7, "", "a\nb", ["claude"]])
def test_a_malformed_recorded_model_reads_as_unknown(tmp_path, bad):
    store = SessionStore(tmp_path, "s1")
    store._update_meta({"window_model": bad, "window_tokens": 33_000, "bytes_per_token": 1.2})
    assert store.window_model() is None and store.window() == (33_000, 1.2)


def test_window_writes_and_turn_room_charges_do_not_drop_each_other(tmp_path):
    """15 workers on one session directory: the engine's window writes and the tools' charges.

    (A room publish resets the charges by design; charges racing publishes are in test_turn_room.)"""
    SessionStore(tmp_path, "s1").set_turn_room("1", 10_000)
    start = threading.Barrier(15)

    def worker(i):
        own = SessionStore(tmp_path, "s1")
        start.wait()
        for n in range(20):
            if i % 2 == 0:
                own.set_window(33_000 + n, 1.2, model=f"model-{i}")
            else:
                own.charge_turn_room(lambda remaining: min(remaining, 3))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store = SessionStore(tmp_path, "s1")
    assert store.turn_room() == ("1", 10_000, 7 * 20 * 3)  # every charge kept despite the window writes
    assert store.window() == (33_000 + 19, 1.2)
    assert store.window_model() in {f"model-{i}" for i in range(0, 15, 2)}
