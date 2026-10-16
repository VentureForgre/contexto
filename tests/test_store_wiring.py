import hashlib
import json
import subprocess
import sys

import pytest

from contexto.store import SessionStore
from contexto.store.ledger import LedgerCorruptError
from contexto.store.memory import MemoryStore
from contexto.store.pinned import UNCHECKED_LINE


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def log_events(store):
    path = store.dir / "log.jsonl"
    return [json.loads(x)["event"] for x in path.read_text().splitlines()] if path.exists() else []


# N1: repair at store open


def test_open_repairs_a_crash_truncated_ledger_and_logs_it(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.pin("event", "before the crash")
    with open(store.ledger.path, "ab") as f:
        f.write(b'{"kind":"event","body":"torn')
    reopened = SessionStore(tmp_path, "s1")
    assert reopened.ledger.verify() == []
    assert "ledger_repaired" in log_events(reopened)
    assert len(list((reopened.dir / "quarantine").iterdir())) == 1
    reopened.pin("event", "after the crash")
    assert [r.body for r in reopened.ledger.records()] == ["before the crash", "after the crash"]


def test_open_leaves_mid_chain_damage_for_a_human_and_logs_it(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.pin("event", "one")
    store.pin("event", "two")
    data = store.ledger.path.read_bytes().replace(b'"body":"one"', b'"body":"0ne"')
    store.ledger.path.write_bytes(data)
    reopened = SessionStore(tmp_path, "s1")
    assert "ledger_damaged" in log_events(reopened)
    assert reopened.ledger.path.read_bytes() == data  # nothing rewritten
    with pytest.raises(LedgerCorruptError):
        reopened.pin("event", "three")
    reopened.page_out([[{"role": "user", "content": "pages still work"}]], 1)


def test_open_on_a_clean_ledger_logs_nothing(tmp_path):
    SessionStore(tmp_path, "s1").pin("event", "x")
    assert log_events(SessionStore(tmp_path, "s1")) == []


# N2: staleness against the recorded working directory


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path / "home", "s1") if request.param == "session" else MemoryStore("s1")


def test_modified_and_missing_file_sources_show_as_stale(store, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_bytes(b"v1")
    (work / "b.py").write_bytes(b"keep")
    a = store.pin("event", "a checked", epistemic="verified",
                  provenance=[{"type": "file", "locator": "a.py", "sha256": sha(b"v1")}])
    b = store.pin("event", "b checked", epistemic="verified",
                  provenance=[{"type": "file", "locator": "b.py", "sha256": sha(b"keep")}])
    store.set_workdir(work)
    assert "STALE" not in store.pinned_text(6000)
    (work / "a.py").write_bytes(b"v2")
    text = store.pinned_text(6000)
    assert f"{a} event, verified STALE: a checked" in text and f"{b} event, verified: b checked" in text
    (work / "b.py").unlink()
    assert f"{b} event, verified STALE: b checked" in store.state_text()
    assert UNCHECKED_LINE not in store.state_text()


def test_without_a_workdir_staleness_is_marked_unchecked(store):
    store.pin("event", "no file sources")
    assert UNCHECKED_LINE not in store.pinned_text(6000)
    store.pin("event", "x", epistemic="verified",
              provenance=[{"type": "file", "locator": "gone.py", "sha256": sha(b"x")}])
    for text in (store.pinned_text(6000), store.state_text()):
        assert text.splitlines()[1] == UNCHECKED_LINE and "STALE" not in text


def test_workdir_is_stored_privately_in_meta_json(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.set_workdir(tmp_path)
    assert json.loads((store.dir / "meta.json").read_text()) == {"workdir": str(tmp_path.resolve())}
    assert (store.dir / "meta.json").stat().st_mode & 0o777 == 0o600
    assert SessionStore(tmp_path, "s1").workdir() == tmp_path.resolve()


# N3: locators that escape the root


@pytest.mark.parametrize("locator", ["../x.py", "a/../../x.py", "..", "/etc/passwd", "a\\..\\..\\x"])
def test_file_locators_that_escape_the_root_are_rejected(store, locator):
    with pytest.raises(ValueError):
        store.pin("event", "x", provenance=[{"type": "file", "locator": locator, "sha256": sha(b"x")}])
    assert store.ledger.records() == []


def test_a_locator_that_stays_inside_the_root_is_accepted(store):
    store.pin("event", "x", provenance=[{"type": "file", "locator": "a/../b.py", "sha256": sha(b"x")}])


# N4: close checks openness inside the ledger lock

CLOSER = """
import sys, time
from pathlib import Path
from contexto.store import SessionStore
home, ob, go = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
store = SessionStore(home, "s1")
while not go.exists():
    time.sleep(0.0005)
try:
    store.close(ob, "closed by " + sys.argv[4])
    print("closed")
except ValueError:
    print("refused")
"""


def test_concurrent_closes_close_an_obligation_once(tmp_path):
    ob = SessionStore(tmp_path, "s1").pin("obligation", "ship it")
    go = tmp_path / "go"
    procs = [subprocess.Popen([sys.executable, "-c", CLOSER, str(tmp_path), ob, str(go), str(i)],
                              stdout=subprocess.PIPE, text=True) for i in range(4)]
    go.touch()
    outcomes = sorted(p.communicate(timeout=60)[0].strip() for p in procs)
    assert outcomes == ["closed", "refused", "refused", "refused"]
    closures = [r for r in SessionStore(tmp_path, "s1").ledger.records() if r.kind == "closure"]
    assert len(closures) == 1


def test_close_precondition_also_applies_in_memory():
    mem = MemoryStore()
    ob = mem.pin("obligation", "x")
    mem.close(ob, "done")
    with pytest.raises(ValueError):
        mem.close(ob, "again")
