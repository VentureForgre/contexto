"""Project memory against concurrency and damage (D69, design section 2): reading an earlier session
changes nothing in it, and an earlier store that is damaged, half written or deleted mid-read never
breaks the session that reads it.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from contexto.store import SessionStore
from contexto.store import project as project_module
from contexto.store.project import Project, project_sessions
from test_project import append, carried_ids, main_close, main_pin, new_session, repo, snapshot


@pytest.fixture
def home(tmp_path):
    return tmp_path / "contexto-home"


@pytest.fixture
def trio(tmp_path, home):
    """A sound earlier session, the earlier session a test damages, and the session that reads them."""
    root = repo(tmp_path)
    sound = new_session(home, "sound", root, 1)
    kept = main_pin(sound, "decision", "from the sound session")
    victim = new_session(home, "victim", root, 2)
    first = main_pin(victim, "obligation", "the first record")
    second = main_pin(victim, "decision", "the second record")
    victim.release()
    return root, kept, victim, (first, second), new_session(home, "reader", root, 3)


def events(store: SessionStore) -> list[dict]:
    path = store.dir / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def within(seconds: float, action):
    """``action()``'s result; fails the test instead of hanging when it does not return in time."""
    result: list = []
    thread = threading.Thread(target=lambda: result.append(action()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert result, f"did not return within {seconds} s"
    return result[0]


def test_reading_an_earlier_session_changes_nothing_in_it(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    duty = main_pin(first, "obligation", "ship the notes")
    choice = main_pin(first, "decision", "use postgres")
    first.page_out([[{"role": "user", "content": "a paged turn"}]], 1)
    first.release()
    for name in ("ledger.lock", "meta.lock", "live.lock", "log.jsonl"):  # as a session that never took them
        (first.dir / name).unlink(missing_ok=True)
    before = snapshot(first.dir)
    second = new_session(home, "s2", root, 2)
    view = second.project()
    assert len(view.carried()) == 2 and view.state().open_obligations[0].id == duty
    second.pinned_text(6000), second.state_text(), second.carried_text(6000)
    second.close(duty, "shipped")
    second.pin("decision", "use sqlite", supersedes=[choice])
    Project(home, root).carried()
    project_sessions(home, root)
    assert snapshot(first.dir) == before
    assert sorted(p.name for p in first.dir.iterdir()) == ["ledger.jsonl", "meta.json", "page.key", "pages"]
    assert not any(e["event"].startswith("project") for e in events(second))  # nothing to report


def test_a_ledger_cut_mid_line_gives_the_records_before_the_cut(trio):
    _, kept, victim, (first, second), reader = trio
    data = victim.ledger.path.read_bytes()
    victim.ledger.path.write_bytes(data[:-40])  # as a writer that has not finished its line leaves it
    before = snapshot(victim.dir)
    view = reader.project()
    assert [c.record.id for c in view.carried()] == [first, kept]
    assert view.read == 2 and view.skipped == ()
    assert snapshot(victim.dir) == before  # the tail is not repaired or quarantined by a reader
    victim.ledger.path.write_bytes(data + b'{"kind": "decision", "half')  # a complete ledger and a new half line
    assert carried_ids(reader) == [first, second, kept]


@pytest.mark.parametrize("damage", ["corrupt middle line", "not a record", "changed body", "broken chain",
                                    "symlink", "fifo", "directory", "unreadable"])
def test_a_damaged_earlier_ledger_is_skipped_whole_and_counted(trio, tmp_path, damage):
    root, kept, victim, (first, second), reader = trio
    path = victim.ledger.path
    lines = path.read_bytes().split(b"\n")
    if damage == "corrupt middle line":
        path.write_bytes(b"\n".join([b"{not json", *lines[1:]]))
    elif damage == "not a record":
        path.write_bytes(b"\n".join([b'{"a": 1}', *lines[1:]]))
    elif damage == "changed body":  # still JSON and still a record, but not the one its id was computed for
        path.write_bytes(b"\n".join([lines[0].replace(b"the first record", b"send the keys"), *lines[1:]]))
    elif damage == "broken chain":
        path.write_bytes(b"\n".join([lines[1], lines[0], b""]))
    elif damage == "symlink":
        target = tmp_path / "elsewhere.jsonl"
        shutil.move(path, target)
        path.symlink_to(target)
    elif damage == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif damage == "directory":
        path.unlink()
        path.mkdir()
    else:
        if os.geteuid() == 0:
            pytest.skip("root reads any file")
        path.chmod(0)
    before = snapshot(victim.dir) if damage != "unreadable" else None
    view = within(10, reader.project)
    carried = within(10, view.carried)
    assert [c.record.id for c in carried] == [kept]  # the sound session is still carried
    assert view.read == 1 and [s.session for s in view.skipped] == ["victim"]
    assert view.skipped[0].error and "/" not in view.skipped[0].error  # an error class, never a path or text
    assert [m.id for m in view.earlier] == ["victim", "sound"]  # still earlier, records left out
    for text in (reader.pinned_text(6000), reader.state_text(), reader.carried_text(6000)):
        assert kept in text and first not in text and second not in text
    for rid in (first, second):
        with pytest.raises(ValueError):
            reader.pin("decision", "x", supersedes=[rid])
    assert before is None or snapshot(victim.dir) == before
    reader.pinned_text(6000)
    logged = [e for e in events(reader) if e["event"] == "project_session_skipped"]
    assert len(logged) == 1 and set(logged[0]) == {"event", "error", "ts"}  # once, content-free
    assert logged[0]["error"] == view.skipped[0].error
    if damage == "unreadable":
        path.chmod(0o600)


def test_a_session_opened_read_only_logs_nothing_about_a_skipped_session(trio):
    root, kept, victim, _, reader = trio
    victim.ledger.path.write_bytes(b"{not json\n" + victim.ledger.path.read_bytes())
    reader.release()
    before = snapshot(reader.home)
    looking = SessionStore.open_existing(reader.home, "reader")
    assert [s.session for s in looking.project().skipped] == ["victim"]
    assert kept in looking.pinned_text(6000) and kept in looking.state_text() and kept in looking.carried_text(6000)
    assert snapshot(reader.home) == before and events(reader) == []


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2]", b'"a string"', b"\xff\xfe", b""])
def test_an_earlier_session_whose_metadata_cannot_be_read_is_left_out_and_counted(trio, content):
    root, kept, victim, _, reader = trio
    (victim.dir / "meta.json").write_bytes(content)
    view = reader.project()
    assert [m.id for m in view.earlier] == ["sound"] and view.unreadable == 1
    assert [c.record.id for c in view.carried()] == [kept] and view.skipped == ()
    assert project_sessions(reader.home, root).unreadable == 1


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_metadata_that_is_not_a_regular_file_is_never_followed_or_waited_for(trio, tmp_path, kind):
    root, kept, victim, _, reader = trio
    path = victim.dir / "meta.json"
    if kind == "symlink":
        target = tmp_path / "meta-elsewhere.json"
        shutil.move(path, target)
        path.symlink_to(target)
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    else:
        path.unlink()
        path.mkdir()
    view = within(10, reader.project)
    assert [m.id for m in view.earlier] == ["sound"] and view.unreadable == 1
    assert [c.record.id for c in within(10, view.carried)] == [kept]


def test_metadata_of_the_wrong_shape_is_no_project_rather_than_damage(trio):
    root, kept, victim, _, reader = trio
    victim_ids = victim.main_records()
    for meta in ({}, {"project": 7}, {"project": ""}, {"workdir": ["x"]}, {"project": str(root) + "-2"},
                 {"project": str(root).upper()}, {"project": str(root.parent)}, {"workdir": "\0"}):
        (victim.dir / "meta.json").write_text(json.dumps(meta))
        view = reader.project()
        assert [m.id for m in view.earlier] == ["sound"] and view.unreadable == 0, meta
    (victim.dir / "meta.json").write_text(json.dumps({"project": str(root), "project_memory": "false",
                                                      "forgotten": "rec_x", "main_records": list(victim_ids)}))
    view = reader.project()  # only the value false turns memory off; a forgotten list that is no list is empty
    assert [m.id for m in view.earlier] == ["victim", "sound"] and len(view.carried()) == 3


def test_a_session_entry_that_is_a_symlink_is_never_a_member(tmp_path, home):
    root = repo(tmp_path)
    outside = new_session(tmp_path / "other-home", "s1", root, 1)
    main_pin(outside, "decision", "from outside the data directory")
    reader = new_session(home, "reader", root, 3)
    (home / "sessions" / "linked").symlink_to(outside.dir, target_is_directory=True)
    before = snapshot(outside.dir)
    view = reader.project()
    assert view.earlier == () and view.carried() == () and view.unreadable == 0
    assert [m.id for m in project_sessions(home, root).sessions] == ["reader"] == [m.id for m in view.sessions]
    assert reader.carried_text(6000) == "" and snapshot(outside.dir) == before


def test_a_file_in_the_sessions_directory_is_not_a_session(tmp_path, home):
    root = repo(tmp_path)
    reader = new_session(home, "reader", root, 3)
    (home / "sessions" / "stray.txt").write_text("not a session")
    assert reader.project().earlier == () and reader.project().unreadable == 0


def test_a_session_removed_between_the_listing_and_its_read_reads_as_absent(trio, monkeypatch):
    root, kept, victim, _, reader = trio
    view = reader.project()  # the listing: victim is a member
    assert [m.id for m in view.earlier] == ["victim", "sound"]
    shutil.rmtree(victim.dir)  # `contexto sessions prune` removes it before its ledger is read
    assert [c.record.id for c in view.carried()] == [kept]
    assert view.skipped == () and view.read == 2  # nothing to read is not damage
    assert not victim.dir.exists()


def test_a_session_removed_while_the_listing_reads_it_is_not_a_member(trio, monkeypatch):
    root, kept, victim, _, reader = trio
    real = project_module.read_regular

    def vanishing(path):
        if Path(path).parent.name == "victim" and victim.dir.exists():
            shutil.rmtree(victim.dir)
        return real(path)

    monkeypatch.setattr(project_module, "read_regular", vanishing)
    project_module.forget_cache()
    view = reader.project()
    assert [m.id for m in view.earlier] == ["sound"] and view.unreadable == 0
    assert [c.record.id for c in view.carried()] == [kept] and not victim.dir.exists()


def test_a_ledger_removed_between_its_stat_and_its_read_reads_as_absent(trio, monkeypatch):
    root, kept, victim, _, reader = trio
    real = project_module.read_regular

    def vanishing(path):
        if Path(path) == victim.ledger.path:
            shutil.rmtree(victim.dir)
        return real(path)

    monkeypatch.setattr(project_module, "read_regular", vanishing)
    view = reader.project()
    assert [c.record.id for c in view.carried()] == [kept] and view.skipped == ()


def test_a_missing_or_unlistable_sessions_directory_is_an_empty_project(tmp_path, home):
    root = repo(tmp_path)
    assert Project(tmp_path / "no-home", root).carried() == ()
    (tmp_path / "file-home").mkdir()
    (tmp_path / "file-home" / "sessions").write_text("a file")
    view = Project(tmp_path / "file-home", root)
    assert view.earlier == () and view.carried() == () and view.read == 0


def test_two_running_sessions_the_later_sees_the_earlier_and_never_the_reverse(tmp_path, home):
    root = repo(tmp_path)
    early, late = new_session(home, "early", root, 1), new_session(home, "late", root, 2)
    before_late = main_pin(early, "obligation", "pinned before the later session started")
    during = append(early, "decision", "pinned by the earlier session while both run", 2.5)
    main_pin(late, "decision", "pinned by the later session")
    assert carried_ids(late) == [before_late]  # nothing the earlier one pins mid-session is fed to the later
    assert early.project().carried() == ()
    main_close(late, before_late, "done by the later session")
    assert carried_ids(late) == [] and len(early.ledger.records()) == 2  # the earlier ledger is never written
    assert [r.id for r in early.state().open_obligations] == [before_late]  # nor does the earlier see the closure
    third = new_session(home, "third", root, 3)
    assert sorted(c.record.body for c in third.project().carried()) == [
        "pinned by the earlier session while both run", "pinned by the later session"]
    assert during in carried_ids(third)


def test_a_copied_session_directory_does_not_double_a_record(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rid = main_pin(first, "decision", "once")
    first.release()
    shutil.copytree(first.dir, home / "sessions" / "s1-copy")
    os.utime(home / "sessions" / "s1-copy" / "page.key", (os.stat(first.dir / "page.key").st_mtime + 1,) * 2)
    reader = new_session(home, "reader", root, 3)
    assert carried_ids(reader) == [rid] and reader.project().carried()[0].session == "s1"
    assert reader.carried_text(6000).count(rid) == 1


def test_a_deeply_nested_line_in_an_earlier_ledger_is_damage_that_breaks_nothing_later(tmp_path, home, monkeypatch,
                                                                                         capsys):
    """Review B, poc11: a RecursionError from one earlier ledger never escapes into a later session."""
    root = repo(tmp_path)
    earlier = new_session(home, "earlier", root, 1)
    main_pin(earlier, "decision", "fine")
    earlier.release()
    with open(earlier.ledger.path, "ab") as f:
        f.write(b"[" * 200_000 + b"]" * 200_000 + b"\n")
    sound = new_session(home, "sound", root, 2)
    kept = main_pin(sound, "decision", "from the sound session")
    later = new_session(home, "later", root, 3)
    own = later.pin("obligation", "own obligation")
    assert [c.record.id for c in later.carried()] == [kept]
    assert [s.session for s in later.project().skipped] == ["earlier"]
    assert "own obligation" in later.pinned_text(6000) and "own obligation" in later.state_text()
    assert kept in later.carried_text(6000)
    later.close(own, "done")
    later.pin("decision", "replaced", supersedes=[kept])
    from contexto import cli

    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    assert cli.main(["project", "show", "--dir", str(root)]) == 0
    assert "skipped as damaged (LedgerCorruptError)" in capsys.readouterr().out


@pytest.mark.parametrize("failing", ["project", "session_state", "carried"])
def test_any_failure_in_the_project_part_reads_as_nothing_carried(tmp_path, home, monkeypatch, failing):
    root = repo(tmp_path)
    kept = main_pin(new_session(home, "earlier", root, 1), "obligation", "carried")
    later = new_session(home, "later", root, 2)
    own = later.pin("obligation", "own obligation")

    def broken(*args, **kwargs):
        raise RuntimeError(f"text that must not be logged: {root}")

    if failing == "project":
        monkeypatch.setattr(SessionStore, "project", broken)
    else:
        monkeypatch.setattr(Project, failing, broken)
    assert "own obligation" in later.pinned_text(6000) and "own obligation" in later.state_text()
    if failing != "session_state":  # what carries is read through Project.carried alone
        assert later.carried_text(6000) == "" and later.carried() == ()
        with pytest.raises(ValueError):  # a carried record cannot be closed when nothing is carried
            later.close(kept, "done")
    later.pin("decision", "an own pin works")
    later.close(own, "done")
    logged = [e for e in events(later) if e["event"] == "project_unavailable"]
    assert logged and all(e == {"ts": e["ts"], "event": "project_unavailable", "error": "RuntimeError"}
                          for e in logged)
    assert str(root) not in (later.dir / "log.jsonl").read_text()


@pytest.mark.parametrize("name", ["ledger.jsonl", "meta.json"])
def test_an_oversized_file_of_another_session_is_skipped_unread(trio, monkeypatch, name):
    """Review B, finding 10: a ledger over 64 MB or a meta.json over 1 MB is never read whole."""
    root, kept, victim, _, reader = trio
    limit = project_module.LEDGER_LIMIT if name == "ledger.jsonl" else project_module.META_LIMIT
    assert limit == (64 << 20 if name == "ledger.jsonl" else 1 << 20)
    os.truncate(victim.dir / name, limit + 1)  # sparse: no real write of that size
    project_module.forget_cache()
    reads = []
    real = project_module.read_regular

    def counting(path, *args, **kwargs):
        reads.append(Path(path).name if Path(path).parent.name == "victim" else None)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(project_module, "read_regular", counting)
    view = reader.project()
    assert [c.record.id for c in view.carried()] == [kept] and name not in reads
    if name == "ledger.jsonl":
        assert [(s.session, s.error) for s in view.skipped] == [("victim", "FileTooLargeError")]
    else:
        assert view.unreadable == 1 and [m.id for m in view.earlier] == ["sound"]
