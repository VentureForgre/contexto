import hashlib
import itertools
import os
from pathlib import Path

import pytest

from contexto.model import Record, Source
from contexto.state import Conflict, derive, stale_sources

_counter = itertools.count()


def rec(kind: str = "event", epistemic: str = "reported", **fields) -> Record:
    n = next(_counter)
    fields.setdefault("body", f"{kind} {n}")
    if kind == "obligation":
        fields.setdefault("owner", "builder")
    return Record(
        id="rec_" + hashlib.sha256(f"test-{n}".encode()).hexdigest()[:24],
        kind=kind,
        epistemic=epistemic,
        created_at=f"2026-10-15T00:00:{n % 60:02d}.{n:06d}Z",
        author="tester",
        prev=hashlib.sha256(f"prev-{n}".encode()).hexdigest(),
        **fields,
    )


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ids(records) -> list[str]:
    return [r.id for r in records]


# Supersession


def test_superseded_record_is_inactive_and_mapped(tmp_path):
    a = rec("decision")
    b = rec("decision", supersedes=(a.id,))
    state = derive([a, b], tmp_path)
    assert ids(state.active) == [b.id]
    assert dict(state.superseded_by) == {a.id: (b.id,)}
    assert ids(state.records) == [a.id, b.id]


def test_chained_supersession(tmp_path):
    a = rec("decision")
    b = rec("decision", supersedes=(a.id,))
    c = rec("decision", supersedes=(b.id,))
    state = derive([a, b, c], tmp_path)
    assert ids(state.active) == [c.id]
    assert dict(state.superseded_by) == {a.id: (b.id,), b.id: (c.id,)}


def test_record_superseded_by_several(tmp_path):
    a = rec("event")
    b = rec("event", supersedes=(a.id,))
    c = rec("event", supersedes=(a.id,))
    state = derive([a, b, c], tmp_path)
    assert state.superseded_by[a.id] == (b.id, c.id)
    assert ids(state.active) == [b.id, c.id]


def test_supersedes_pointing_forward_or_unknown_is_ignored(tmp_path):
    later = rec("event")
    a = rec("event", supersedes=(later.id, "rec_" + "f" * 24))
    state = derive([a, later], tmp_path)
    assert ids(state.active) == [a.id, later.id]
    assert dict(state.superseded_by) == {}


# Obligations


def test_obligation_open_until_closed(tmp_path):
    ob = rec("obligation")
    other = rec("obligation")
    assert ids(derive([ob, other], tmp_path).open_obligations) == [ob.id, other.id]
    close = rec("closure", target=ob.id)
    assert ids(derive([ob, other, close], tmp_path).open_obligations) == [other.id]


def test_superseded_obligation_is_not_open(tmp_path):
    ob = rec("obligation")
    replacement = rec("obligation", supersedes=(ob.id,))
    state = derive([ob, replacement], tmp_path)
    assert ids(state.open_obligations) == [replacement.id]


def test_superseding_closure_reopens_active_obligation(tmp_path):
    ob = rec("obligation")
    close = rec("closure", target=ob.id)
    retract = rec("event", body="closure was premature", supersedes=(close.id,))
    assert derive([ob, close], tmp_path).open_obligations == ()
    assert ids(derive([ob, close, retract], tmp_path).open_obligations) == [ob.id]


def test_superseding_closure_does_not_reopen_superseded_obligation(tmp_path):
    ob = rec("obligation")
    close = rec("closure", target=ob.id)
    replacement = rec("obligation", supersedes=(ob.id,))
    retract = rec("event", supersedes=(close.id,))
    state = derive([ob, close, replacement, retract], tmp_path)
    assert ids(state.open_obligations) == [replacement.id]


def test_closure_replaced_by_new_closure_keeps_obligation_closed(tmp_path):
    ob = rec("obligation")
    close = rec("closure", target=ob.id)
    corrected = rec("closure", target=ob.id, supersedes=(close.id,))
    assert derive([ob, close, corrected], tmp_path).open_obligations == ()


# Conflicts


def test_two_active_decisions_same_topic_conflict(tmp_path):
    a = rec("decision", topic="db")
    other = rec("decision", topic="lang")
    b = rec("decision", topic="db")
    state = derive([a, other, b], tmp_path)
    assert state.conflicts == (Conflict(topic="db", record_ids=(a.id, b.id)),)


def test_superseding_one_side_clears_conflict(tmp_path):
    a = rec("decision", topic="db")
    b = rec("decision", topic="db")
    c = rec("decision", topic="db", supersedes=(a.id, b.id))
    assert derive([a, b, c], tmp_path).conflicts == ()
    d = rec("decision", topic="db", supersedes=(a.id,))
    assert derive([a, b, d], tmp_path).conflicts == (Conflict("db", (b.id, d.id)),)


def test_untopiced_and_non_decision_records_never_conflict(tmp_path):
    records = [
        rec("decision"),
        rec("decision"),
        rec("event", topic="db"),
        rec("event", topic="db"),
        rec("decision", topic="db"),
    ]
    assert derive(records, tmp_path).conflicts == ()


# Staleness


def test_file_source_with_changed_hash_is_stale(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"original")
    src = Source("file", "notes.txt", sha(b"original"))
    r = rec(provenance=(src,))
    assert stale_sources(r, tmp_path) == []
    assert derive([r], tmp_path).stale == frozenset()
    f.write_bytes(b"changed")
    assert stale_sources(r, tmp_path) == [src]
    assert derive([r], tmp_path).stale == {r.id}


def test_missing_file_is_stale(tmp_path):
    src = Source("file", "gone.txt", sha(b"x"))
    r = rec(provenance=(src,))
    assert stale_sources(r, tmp_path) == [src]
    assert derive([r], tmp_path).stale == {r.id}


def test_directory_in_place_of_file_is_stale(tmp_path):
    (tmp_path / "d").mkdir()
    r = rec(provenance=(Source("file", "d", sha(b"x")),))
    assert derive([r], tmp_path).stale == {r.id}


def test_non_file_sources_are_never_stale(tmp_path):
    r = rec(provenance=(Source("url", "https://example.invalid/x", sha(b"x")),))
    assert derive([r], tmp_path).stale == frozenset()


def test_relative_locator_resolves_against_root_not_cwd(tmp_path, monkeypatch):
    root = tmp_path / "root"
    elsewhere = tmp_path / "elsewhere"
    (root / "sub").mkdir(parents=True)
    (elsewhere / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_bytes(b"root copy")
    (elsewhere / "sub" / "a.txt").write_bytes(b"other copy")
    monkeypatch.chdir(elsewhere)
    r = rec(provenance=(Source("file", "sub/a.txt", sha(b"root copy")),))
    assert stale_sources(r, root) == []
    assert derive([r], root).stale == frozenset()


def test_only_changed_sources_are_reported(tmp_path):
    (tmp_path / "keep.txt").write_bytes(b"k")
    (tmp_path / "edit.txt").write_bytes(b"new")
    keep = Source("file", "keep.txt", sha(b"k"))
    edit = Source("file", "edit.txt", sha(b"old"))
    r = rec(provenance=(keep, edit))
    assert stale_sources(r, tmp_path) == [edit]


def test_superseded_records_still_report_staleness(tmp_path):
    a = rec(provenance=(Source("file", "gone.txt", sha(b"x")),))
    b = rec(supersedes=(a.id,))
    assert derive([a, b], tmp_path).stale == {a.id}


# Read-only


def _snapshot(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = p.stat()
            snap[str(p.relative_to(root))] = (
                st.st_mode,
                st.st_mtime_ns,
                p.read_bytes() if p.is_file() else None,
            )
    return snap


def test_derive_never_writes_to_disk(tmp_path, monkeypatch):
    (tmp_path / "fresh.txt").write_bytes(b"fresh")
    (tmp_path / "edited.txt").write_bytes(b"after")
    ob = rec("obligation", provenance=(Source("file", "fresh.txt", sha(b"fresh")),))
    records = [
        ob,
        rec("decision", topic="t", provenance=(Source("file", "edited.txt", sha(b"before")),)),
        rec("decision", topic="t", provenance=(Source("file", "missing.txt", sha(b"m")),)),
        rec("closure", target=ob.id),
    ]
    monkeypatch.chdir(tmp_path)
    before = _snapshot(tmp_path)
    state = derive(records, tmp_path)
    assert len(state.stale) == 2 and len(state.conflicts) == 1
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / ".contexto").exists()


def test_state_is_immutable(tmp_path):
    a = rec("decision")
    b = rec("decision", supersedes=(a.id,))
    state = derive([a, b], tmp_path)
    with pytest.raises(TypeError):
        state.superseded_by[b.id] = ()  # type: ignore[index]
    with pytest.raises(AttributeError):
        state.active = ()  # type: ignore[misc]
