import hashlib
import os
import stat

import pytest

from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.pages import PINNED_FIRST_LINE
from contexto.store.recall import DEFAULT_LINES


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def a_page(store):
    return store.page_out([[{"role": "user", "content": "we decided to use sqlite"}]], 1)[0]


def test_pin_appends_a_chained_record_for_this_session(store):
    rid = store.pin("event", "build passed")
    [rec] = store.ledger.records()
    assert rec.id == rid and rec.session == "s1" and rec.author == "model" and rec.epistemic == "reported"
    store.pin("event", "second")
    assert store.ledger.verify() == []
    assert store.ledger.records()[1].prev != "0" * 64


def test_obligation_is_open_until_closed(store):
    ob = store.pin("obligation", "write the migration")
    assert [r.id for r in store.state().open_obligations] == [ob]
    assert store.ledger.records()[0].owner == "assistant"
    close_id = store.close(ob, "migration written")
    assert store.state().open_obligations == ()
    assert store.ledger.records()[-1].target == ob and close_id == store.ledger.records()[-1].id
    with pytest.raises(ValueError):
        store.close(ob, "again")
    with pytest.raises(ValueError):
        store.close("rec_" + "0" * 24, "unknown")


def test_superseded_obligation_is_not_open_and_cannot_be_closed(store):
    ob = store.pin("obligation", "old plan")
    store.pin("obligation", "new plan", supersedes=[ob])
    assert ob not in {r.id for r in store.state().open_obligations}
    with pytest.raises(ValueError):
        store.close(ob, "done")


def test_supersession_and_conflicts_in_pinned_text(store):
    a = store.pin("decision", "use postgres", topic="db")
    b = store.pin("decision", "use sqlite", topic="db")
    text = store.pinned_text(6000)
    assert f"{a} decision [db] CONFLICT" in text and f"{b} decision [db] CONFLICT" in text
    assert f"Conflict on topic db: {a}, {b}" in store.state_text()
    c = store.pin("decision", "sqlite, final", topic="db", supersedes=[a, b])
    text = store.pinned_text(6000)
    assert a not in text and b not in text and f"{c} decision [db]: sqlite, final" in text
    assert "CONFLICT" not in text


def test_pinned_text_order_groups_and_first_line(store):
    page = a_page(store)
    e_unverified = store.pin("event", "rumor")
    f1 = store.pin("event", "tests pass", epistemic="verified",
                   provenance=[{"type": "command", "locator": "pytest", "sha256": None, "span": None}])
    f2 = store.pin("artifact", "report written", epistemic="verified",
                   provenance=[{"type": "page", "locator": page, "sha256": None, "span": None}])
    d1 = store.pin("decision", "first decision")
    o1 = store.pin("obligation", "first duty")
    o2 = store.pin("obligation", "second duty", owner="user")
    lines = store.pinned_text(6000).splitlines()
    assert lines[0] == PINNED_FIRST_LINE and tuple(lines[:len(DEFAULT_LINES)]) == DEFAULT_LINES
    assert lines[len(DEFAULT_LINES):] == [
        "Open obligations:",
        f"- {o2} obligation, owner user: second duty",
        f"- {o1} obligation, owner assistant: first duty",
        "Active decisions:",
        f"- {d1} decision: first decision",
        "Verified facts:",
        f"- {f2} artifact, verified: report written",
        f"- {f1} event, verified: tests pass",
    ]
    assert e_unverified not in store.state_text()


def test_pinned_text_budget_and_omitted_count(store):
    ids = [store.pin("obligation", f"duty number {i} with some words") for i in range(40)]
    full = store.state_text().splitlines()
    assert len(full) == len(DEFAULT_LINES) + 1 + 40  # opening lines, the group heading, the items
    capped = store.pinned_text(400).splitlines()
    shown = [line for line in capped if line.startswith("- ")]
    assert capped[0] == PINNED_FIRST_LINE and 0 < len(shown) < 40
    assert capped[-1] == f"({40 - len(shown)} more pinned items omitted; call contexto_state for the full list)"
    assert shown[0].startswith(f"- {ids[-1]} ")  # newest first
    assert store.pinned_text(1).splitlines()[0] == PINNED_FIRST_LINE


def test_page_provenance_must_cite_a_known_page(store):
    page = a_page(store)
    rid = store.pin("decision", "sqlite", provenance=[{"type": "page", "locator": page}])
    assert store.ledger.records()[-1].provenance[0].locator == page and rid
    with pytest.raises(ValueError):
        store.pin("decision", "x", provenance=[{"type": "page", "locator": "pg_" + "0" * 16}])
    with pytest.raises(ValueError):
        store.pin("decision", "x", provenance=[{"type": "page", "locator": "not-a-page"}])


def test_validation_errors_are_value_errors_and_write_nothing(store):
    for bad in [dict(kind="nope", text="x"), dict(kind="event", text="x", epistemic="verified"),
                dict(kind="event", text="x", epistemic="sure"), dict(kind="event", text="x", owner="me"),
                dict(kind="event", text="x", supersedes=["rec_" + "1" * 24])]:
        kind, text = bad.pop("kind"), bad.pop("text")
        with pytest.raises(ValueError):
            store.pin(kind, text, **bad)
    assert store.ledger.records() == []


def test_file_sources_require_sha256_and_missing_files_are_stale(store, tmp_path):
    with pytest.raises(ValueError):
        store.pin("event", "x", provenance=[{"type": "file", "locator": "a.txt"}])
    (tmp_path / "a.txt").write_bytes(b"one")
    (tmp_path / "b.txt").write_bytes(b"two")
    fresh = store.pin("event", "a", provenance=[{"type": "file", "locator": "a.txt", "sha256": sha(b"one")}])
    changed = store.pin("event", "b", provenance=[{"type": "file", "locator": "b.txt", "sha256": sha(b"old")}])
    gone = store.pin("event", "c", provenance=[{"type": "file", "locator": "c.txt", "sha256": sha(b"x")}])
    stale = store.state(tmp_path).stale
    assert fresh not in stale and changed in stale and gone in stale
    assert store.state().stale == frozenset()  # no project root, no staleness check


def test_ledger_files_are_private_under_a_permissive_umask(tmp_path):
    old = os.umask(0)
    try:
        s = SessionStore(tmp_path, "s1")
        s.pin("event", "x")
        with open(s.ledger.path, "ab") as f:
            f.write(b"torn")
        q = s.ledger.repair()
    finally:
        os.umask(old)
    for f in (s.ledger.path, s.ledger.lock_path, q):
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600, f
    assert stat.S_IMODE(os.stat(q.parent).st_mode) == 0o700
