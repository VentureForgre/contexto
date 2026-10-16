"""Ledger tests by builder-2 (M1 packet A2, commit 1ab5f29), ported to the session-directory layout."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from contexto.store.ledger import Ledger, LedgerCorruptError, LedgerError
from contexto.store.model import GENESIS_PREV as GENESIS

SRC = Path(__file__).resolve().parents[1] / "src"


def draft(body="something happened", kind="event", **extra):
    d = {"kind": kind, "epistemic": "reported", "author": "builder-2", "body": body, "provenance": []}
    d.update(extra)
    return d


def ledger_path(root: Path) -> Path:
    return root / "ledger.jsonl"


def raw_lines(root: Path) -> list[bytes]:
    return ledger_path(root).read_bytes().split(b"\n")[:-1]


def recompute_id(line: bytes) -> str:
    d = json.loads(line)
    d.pop("id")
    d.pop("prev")
    canon = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "rec_" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24]


def assert_chain(root: Path, expected_count: int) -> None:
    lines = raw_lines(root)
    assert len(lines) == expected_count
    prev = GENESIS
    for line in lines:
        d = json.loads(line)
        assert d["prev"] == prev
        assert d["id"] == recompute_id(line)
        prev = hashlib.sha256(line).hexdigest()


# Acceptance 1


def test_append_then_records_in_order_with_valid_chain(tmp_path):
    led = Ledger(tmp_path)
    appended = [led.append(draft(f"event {i}")) for i in range(10)]
    got = led.records()
    assert [r.id for r in got] == [r.id for r in appended]
    assert [r.body for r in got] == [f"event {i}" for i in range(10)]
    assert got[0].prev == GENESIS
    assert led.verify() == []
    assert_chain(tmp_path, 10)


def test_line_is_canonical_json_of_to_dict(tmp_path):
    led = Ledger(tmp_path)
    rec = led.append(draft(session="s1", topic="t"))
    (line,) = raw_lines(tmp_path)
    canon = json.dumps(rec.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert line == canon.encode("utf-8")


def test_created_at_filled_when_missing_and_kept_when_given(tmp_path):
    led = Ledger(tmp_path)
    filled = led.append(draft("a"))
    assert filled.created_at.endswith("Z")
    assert len(filled.created_at) == len("2026-10-15T22:15:29.123456Z")
    given = led.append(draft("b", created_at="2026-01-02T03:04:05Z"))
    assert given.created_at == "2026-01-02T03:04:05Z"


def test_id_independent_of_absent_field_spelling(tmp_path):
    a = Ledger(tmp_path / "a").append(draft(created_at="2026-01-02T03:04:05Z"))
    b = Ledger(tmp_path / "b").append(
        draft(created_at="2026-01-02T03:04:05Z", session=None, topic=None, supersedes=[])
    )
    assert a.id == b.id


def test_head_is_genesis_when_empty_and_tracks_last_line(tmp_path):
    led = Ledger(tmp_path)
    assert led.head() == GENESIS == "0" * 64
    led.append(draft("a"))
    second = led.append(draft("b"))
    assert led.head() == hashlib.sha256(raw_lines(tmp_path)[-1]).hexdigest()
    third = led.append(draft("c"))
    assert third.prev != second.prev
    assert led.records()[-1].prev == hashlib.sha256(raw_lines(tmp_path)[-2]).hexdigest()


def test_reads_on_missing_ledger_are_empty_and_create_nothing(tmp_path):
    led = Ledger(tmp_path / "session")
    assert led.records() == []
    assert led.verify() == []
    assert not (tmp_path / "session").exists()


# Acceptance 2


@pytest.mark.parametrize(
    "bad",
    [
        draft(kind="rumor"),
        draft(epistemic="certain"),
        {"kind": "event", "epistemic": "reported", "author": "x", "provenance": []},
        {"kind": "event", "epistemic": "reported", "body": "x", "provenance": []},
        {"kind": "event", "epistemic": "reported", "author": "x", "body": "x"},
        {"kind": "event", "author": "x", "body": "x", "provenance": []},
        draft(kind="closure"),
        draft(kind="closure", target="rec_" + "0" * 24),
        draft(supersedes=["rec_" + "1" * 24]),
        draft(id="rec_" + "2" * 24),
        draft(prev="f" * 64),
        draft(epistemic="verified"),
        draft(kind="obligation"),
        draft(provenance=[{"type": "file", "locator": "notes.md"}]),
        draft(provenance=[{"type": "file", "locator": "/etc/hosts", "sha256": "a" * 64}]),
        draft(provenance=[{"type": "rumor", "locator": "x"}]),
        "not a dict",
    ],
)
def test_invalid_drafts_rejected_and_nothing_written(tmp_path, bad):
    led = Ledger(tmp_path)
    led.append(draft("seed"))
    before = ledger_path(tmp_path).read_bytes()
    with pytest.raises((ValueError, LedgerError)):
        led.append(bad)
    assert ledger_path(tmp_path).read_bytes() == before


def test_closure_to_non_obligation_rejected(tmp_path):
    led = Ledger(tmp_path)
    ev = led.append(draft("not an obligation"))
    before = ledger_path(tmp_path).read_bytes()
    with pytest.raises(LedgerError):
        led.append(draft("close it", kind="closure", target=ev.id))
    assert ledger_path(tmp_path).read_bytes() == before


def test_closure_to_obligation_and_known_supersedes_accepted(tmp_path):
    led = Ledger(tmp_path)
    ob = led.append(draft("ship ledger", kind="obligation", owner="builder-2"))
    led.append(draft("done", kind="closure", target=ob.id))
    d1 = led.append(draft("use flock", kind="decision", topic="lock"))
    d2 = led.append(draft("use flock on a lock file", kind="decision", topic="lock", supersedes=[d1.id]))
    assert d2.supersedes == (d1.id,)
    assert led.verify() == []


def test_duplicate_id_rejected(tmp_path):
    led = Ledger(tmp_path)
    fixed = draft("same", created_at="2026-01-02T03:04:05Z")
    led.append(fixed)
    before = ledger_path(tmp_path).read_bytes()
    with pytest.raises(LedgerError, match="already exists"):
        led.append(dict(fixed))
    assert ledger_path(tmp_path).read_bytes() == before


def test_closure_to_superseded_obligation_rejected(tmp_path):
    led = Ledger(tmp_path)
    old = led.append(draft("old plan", kind="obligation", owner="builder-2"))
    led.append(draft("new plan", kind="obligation", owner="builder-2", supersedes=[old.id]))
    before = ledger_path(tmp_path).read_bytes()
    with pytest.raises(LedgerError, match="superseded"):
        led.append(draft("close old", kind="closure", target=old.id))
    assert ledger_path(tmp_path).read_bytes() == before


# Acceptance 3


@pytest.mark.parametrize("field", ["body", "prev"])
def test_edited_middle_byte_reported_for_that_line(tmp_path, field):
    led = Ledger(tmp_path)
    for i in range(5):
        led.append(draft(f"event number {i}"))
    lines = raw_lines(tmp_path)
    target = 2  # zero-based, reported as line 3
    line = lines[target]
    needle = b'"body":"event number' if field == "body" else b'"prev":"'
    pos = line.index(needle) + len(needle) + 1
    flipped = bytes([line[pos] ^ 0x01])
    lines[target] = line[:pos] + flipped + line[pos + 1 :]
    ledger_path(tmp_path).write_bytes(b"\n".join(lines) + b"\n")
    problems = led.verify()
    assert problems, "tampering was not detected"
    assert any(p.startswith("line 3:") for p in problems), problems


def test_unparseable_middle_line_reported(tmp_path):
    led = Ledger(tmp_path)
    for i in range(3):
        led.append(draft(f"e{i}"))
    lines = raw_lines(tmp_path)
    lines[1] = lines[1][:-1]  # drop closing brace
    ledger_path(tmp_path).write_bytes(b"\n".join(lines) + b"\n")
    problems = led.verify()
    assert any(p.startswith("line 2:") and "unparseable" in p for p in problems), problems
    with pytest.raises(LedgerCorruptError):
        led.records()
    with pytest.raises(LedgerCorruptError):
        led.append(draft("refused"))


# Acceptance 4


def truncate_last_line(root: Path, keep: int) -> bytes:
    data = ledger_path(root).read_bytes()
    start = data.rstrip(b"\n").rfind(b"\n") + 1
    torn = data[start : start + keep]
    ledger_path(root).write_bytes(data[:start] + torn)
    return torn


def test_truncated_tail_reported_repaired_and_chain_continues(tmp_path):
    led = Ledger(tmp_path)
    for i in range(4):
        led.append(draft(f"e{i}"))
    torn = truncate_last_line(tmp_path, keep=20)
    problems = led.verify()
    assert problems == [p for p in problems if p.startswith("line 4:")]
    assert any("truncated" in p for p in problems), problems
    # The incomplete final line was never committed; records() excludes it.
    assert [r.body for r in led.records()] == ["e0", "e1", "e2"]
    with pytest.raises(LedgerCorruptError):
        led.append(draft("refused until repaired"))

    qpath = led.repair()
    assert qpath is not None and qpath.parent == tmp_path / "quarantine"
    assert qpath.read_bytes() == torn
    assert led.verify() == []

    nxt = led.append(draft("after repair"))
    assert nxt.prev == hashlib.sha256(raw_lines(tmp_path)[-2]).hexdigest()
    assert led.verify() == []
    assert_chain(tmp_path, 4)


def test_corrupt_tail_with_newline_is_quarantined(tmp_path):
    led = Ledger(tmp_path)
    led.append(draft("good"))
    with ledger_path(tmp_path).open("ab") as f:
        f.write(b'{"kind":"ev\n')
    assert any(p.startswith("line 2:") for p in led.verify())
    qpath = led.repair()
    assert qpath.read_bytes() == b'{"kind":"ev\n'
    assert led.verify() == []


def test_repair_is_noop_on_clean_ledger(tmp_path):
    led = Ledger(tmp_path)
    led.append(draft("ok"))
    before = ledger_path(tmp_path).read_bytes()
    assert led.repair() is None
    assert ledger_path(tmp_path).read_bytes() == before
    assert not (tmp_path / "quarantine").exists()


def test_repair_refuses_when_corruption_not_confined_to_tail(tmp_path):
    led = Ledger(tmp_path)
    for i in range(4):
        led.append(draft(f"e{i}"))
    lines = raw_lines(tmp_path)
    lines[1] = lines[1].replace(b"e1", b"eX")
    ledger_path(tmp_path).write_bytes(b"\n".join(lines) + b"\n")
    before = ledger_path(tmp_path).read_bytes()
    with pytest.raises(LedgerCorruptError):
        led.repair()
    assert ledger_path(tmp_path).read_bytes() == before
    assert not (tmp_path / "quarantine").exists()


def test_mid_chain_damage_with_torn_tail_is_not_repaired(tmp_path):
    led = Ledger(tmp_path)
    for i in range(3):
        led.append(draft(f"e{i}"))
    lines = raw_lines(tmp_path)
    lines[2] = lines[2][:-1]  # damage the complete line just above the torn tail
    ledger_path(tmp_path).write_bytes(b"\n".join(lines) + b"\n" + b'{"kind":')
    problems = led.verify()
    middle = [p for p in problems if p.startswith("line 3:")]
    assert middle and all("truncated" not in p for p in middle), problems
    assert any(p.startswith("line 4:") and "truncated" in p for p in problems), problems
    before = ledger_path(tmp_path).read_bytes()
    with pytest.raises(LedgerCorruptError):
        led.repair()
    assert ledger_path(tmp_path).read_bytes() == before


def test_repeated_repairs_never_overwrite_quarantine(tmp_path):
    led = Ledger(tmp_path)
    led.append(draft("ok"))
    paths = []
    for i in range(3):
        with ledger_path(tmp_path).open("ab") as f:
            f.write(f"torn-{i}".encode())
        paths.append(led.repair())
    assert len(set(paths)) == 3
    assert sorted(p.read_bytes() for p in paths) == [b"torn-0", b"torn-1", b"torn-2"]


# Acceptance 5

WRITER = """
import sys, time
from pathlib import Path
from contexto.store.ledger import Ledger
root, tag, n, go = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
led = Ledger(root)
while not go.exists():
    time.sleep(0.001)
for i in range(n):
    led.append({"kind": "event", "epistemic": "reported", "author": tag,
                "body": f"{tag} {i}", "provenance": []})
"""


def test_two_processes_appending_concurrently_keep_chain_intact(tmp_path):
    n = 250
    go = tmp_path / "go"
    env = dict(os.environ, PYTHONPATH=str(SRC))
    procs = [
        subprocess.Popen([sys.executable, "-c", WRITER, str(tmp_path), tag, str(n), str(go)], env=env)
        for tag in ("alpha", "beta")
    ]
    go.touch()  # release both writers together so they contend for the lock
    for p in procs:
        assert p.wait(timeout=120) == 0
    led = Ledger(tmp_path)
    assert led.verify() == []
    assert_chain(tmp_path, 2 * n)
    bodies = [r.body for r in led.records()]
    for tag in ("alpha", "beta"):
        mine = [b for b in bodies if b.startswith(tag + " ")]
        assert mine == [f"{tag} {i}" for i in range(n)]
    # Interleaving is the point: both writers must actually have overlapped.
    authors = [r.author for r in led.records()]
    switches = sum(1 for a, b in zip(authors, authors[1:]) if a != b)
    assert switches > 1, "writers ran sequentially; the test did not exercise contention"


# Acceptance 6


def test_reopen_continues_chain(tmp_path):
    first = Ledger(tmp_path)
    for i in range(3):
        first.append(draft(f"e{i}"))
    head = first.head()
    second = Ledger(tmp_path)
    assert second.head() == head
    rec = second.append(draft("after reopen"))
    assert rec.prev == head
    assert second.verify() == []
    assert [r.body for r in Ledger(tmp_path).records()] == ["e0", "e1", "e2", "after reopen"]
