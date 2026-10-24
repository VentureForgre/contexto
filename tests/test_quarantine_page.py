"""SessionStore.quarantine_page (M2 B10, contract rule 14, PR #52)."""

import json
import os
import stat
import subprocess
import sys
import time

import pytest

from contexto.store import PageNotFoundError, SessionStore
from contexto.store.memory import MemoryStore

TURN = [{"role": "user", "content": "PRIVATE-PAGE-TEXT about the parser"}]


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_moves_the_page_privately_and_keeps_its_bytes(tmp_path):
    old = os.umask(0)
    try:
        store = SessionStore(tmp_path, "s1")
        [pid] = store.page_out([TURN], 1)
        source = store.pages_dir / f"{pid}.json"
        before = source.read_bytes()
        target = store.quarantine_page(pid)
    finally:
        os.umask(old)
    assert not source.exists() and target.read_bytes() == before
    assert target.parent == store.dir / "quarantine" and target.name.startswith(f"{pid}.")
    assert mode(target) == 0o600 and mode(target.parent) == 0o700
    assert store.get_page(pid) is None


def test_the_same_id_can_be_paged_afresh(tmp_path):
    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([TURN], 1)
    path = store.pages_dir / f"{pid}.json"
    path.write_bytes(path.read_bytes().replace(b"PRIVATE", b"CORRUPT"))  # a persistent page_conflict
    store.quarantine_page(pid)
    assert store.page_out([TURN], 1) == [pid]
    assert store.get_page(pid)["messages"] == TURN


def test_idempotent_and_clear_when_never_stored(tmp_path):
    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([TURN], 1)
    first = store.quarantine_page(pid)
    assert store.quarantine_page(pid) == first
    assert len(list((store.dir / "quarantine").iterdir())) == 1
    with pytest.raises(PageNotFoundError):
        store.quarantine_page("pg_" + "0" * 16)


def test_quarantining_twice_keeps_both_copies(tmp_path):
    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([TURN], 1)
    a = store.quarantine_page(pid)
    store.page_out([TURN], 1)
    b = store.quarantine_page(pid)
    assert a != b and a.exists() and b.exists()
    assert store.quarantine_page(pid) == b  # the most recent


def test_quarantine_names_sort_in_call_order_across_a_second_boundary(tmp_path, monkeypatch):
    """The stamp's seconds and nanoseconds come from one clock reading. Read separately, a call
    just before a second boundary and one just after it could name the later copy lower, so the
    lookup returned the older copy (seen once on CI, Python 3.14)."""
    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([TURN], 1)
    second = 1_793_000_000 * 1_000_000_000
    monkeypatch.setattr(time, "time_ns", lambda: second - 1)
    a = store.quarantine_page(pid)
    monkeypatch.undo()
    store.page_out([TURN], 1)
    monkeypatch.setattr(time, "time_ns", lambda: second + 1)
    b = store.quarantine_page(pid)
    monkeypatch.undo()
    assert a.name < b.name
    assert store.quarantine_page(pid) == b


@pytest.mark.parametrize("bad", ["../pages/x", "pg_123", "pg_" + "A" * 16, "pg_" + "0" * 16 + ".json",
                                 "pg_0000000000000000/../x", "", None])
def test_bad_ids_are_rejected_before_any_path_is_built(tmp_path, bad):
    store = SessionStore(tmp_path, "s1")
    with pytest.raises(ValueError):
        store.quarantine_page(bad)
    assert not (store.dir / "quarantine").exists()


def test_the_log_names_the_page_and_path_only(tmp_path):
    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([TURN], 1)
    target = store.quarantine_page(pid)
    [event] = [json.loads(x) for x in (store.dir / "log.jsonl").read_text().splitlines()]
    assert event == {"event": "page_quarantined", "page_id": pid, "path": f"quarantine/{target.name}",
                     "ts": event["ts"]}
    assert "PRIVATE" not in (store.dir / "log.jsonl").read_text()


RACER = """
import sys, time
from pathlib import Path
from contexto.store import SessionStore
home, pid, go = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
store = SessionStore(home, "s1")
while not go.exists():
    time.sleep(0.0005)
print(store.quarantine_page(pid).name)
"""


def test_concurrent_calls_move_the_page_once(tmp_path):
    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([TURN], 1)
    go = tmp_path / "go"
    procs = [subprocess.Popen([sys.executable, "-c", RACER, str(tmp_path), pid, str(go)],
                              stdout=subprocess.PIPE, text=True) for _ in range(6)]
    go.touch()
    names = {p.communicate(timeout=60)[0].strip() for p in procs}
    assert all(p.returncode == 0 for p in procs)
    files = list((store.dir / "quarantine").iterdir())
    assert len(files) == 1 and names == {files[0].name}


def test_memory_store_follows_the_same_rule():
    store = MemoryStore("s1")
    [pid] = store.page_out([TURN], 1)
    first = store.quarantine_page(pid)
    assert store.get_page(pid) is None and store.quarantine_page(pid) == first
    assert store.page_out([TURN], 1) == [pid] and store.get_page(pid) is not None
    with pytest.raises(PageNotFoundError):
        store.quarantine_page("pg_" + "1" * 16)
    with pytest.raises(ValueError):
        store.quarantine_page("../x")
    assert store.events[-1] == {"event": "page_quarantined", "page_id": pid, "path": str(first),
                                "ts": store.events[-1]["ts"]}
