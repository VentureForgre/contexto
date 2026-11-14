"""Project memory, the store foundation (D69): which sessions are one project, which of them a session
sees, and which of their records it carries.

Sessions here are created at chosen times, all of them a year from now: ``new_session(..., n)`` is
created ``n`` hundred seconds after that. So a record pinned while a test runs is older than every
session, as a pin is older than the sessions a user starts after it, however long the suite takes;
``append(..., n)`` writes a record with a chosen time among the sessions'.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from contexto.store import SessionNotFoundError, SessionStore
from contexto.store import project as project_module
from contexto.store.ledger import Ledger, LedgerError, MemoryLedger
from contexto.store.memory import MemoryStore
from contexto.store.model import utc_timestamp
from contexto.store.project import (
    Project,
    forget,
    project_id,
    project_root,
    project_sessions,
    read_meta,
    read_records,
)
from contexto.store.sessions import last_activity

BASE = time.time() + 365 * 86400

# The pin line of the preamble as v0.5.24 sent it, before review B's finding 8 added that only rules the
# user states are pinned. The tests that compare bytes with v0.5.24 put it back, so they check that
# project memory itself changes nothing.
V0_5_24_PIN_LINE = (
    "As you work, record decisions and obligations with contexto_pin, and close finished obligations "
    "with contexto_close, so they stay in view. Pin each standing rule, preference, or obligation the user "
    "states (whenever, from now on, always, never) as an obligation as soon as it is stated, even if your "
    "reply is one word, and keep it open until the user withdraws it: one not pinned leaves view when its "
    "turn is paged.")


# The example line as v0.5.24 sent it, before D63 (PR #274) said that search covers tool results paged out
# of the running turn too. Put back with the pin line for the same reason.
V0_5_24_EXAMPLE_LINE = (
    'Example: to find the value of cache.ttl_seconds, call contexto_search with {"query": '
    '"cache.ttl_seconds"}. Each hit names a page, such as "pg_0123456789abcdef (turn 3)", then '
    'shows its matching lines with their character offsets, such as "> 18240: cache.ttl_seconds=...". '
    'Answer from that line, or call contexto_recall with {"page_id": "pg_0123456789abcdef", "offset": '
    '18240} to read on from it. Search covers only paged-out turns, so when the conversation in view states '
    'the same thing, the conversation in view is newer.')


def v0_5_24_guidance(monkeypatch) -> tuple[str, ...]:
    """Put the preamble's opening lines back to v0.5.24's; returns them."""
    from contexto.store import recall

    lines = (*recall.RULE_LINES[:2], V0_5_24_PIN_LINE, V0_5_24_EXAMPLE_LINE)
    monkeypatch.setattr(recall, "DEFAULT_LINES", lines)
    return lines


def at(n: float) -> float:
    return BASE + 100 * n


def repo(tmp_path: Path, name: str = "repo") -> Path:
    """A directory with a ``.git`` entry, so its project root does not depend on where pytest's tmp is."""
    root = tmp_path.resolve() / name
    (root / ".git").mkdir(parents=True)
    return root


def new_session(home: Path, sid: str, cwd: Path, n: float, *, memory: bool | None = None) -> SessionStore:
    store = SessionStore(home, sid)
    store.set_workdir(cwd)
    if memory is not None:
        store.set_project_memory(memory)
    os.utime(store.dir / "page.key", (at(n), at(n)))
    return store


def confirm(store, record_id: str) -> str:
    """Record that the main conversation made ``record_id``, as the engine does when it sees the main
    conversation's own call and the server's reply (``contexto.mainrecords``)."""
    store.add_main_records([record_id])
    return record_id


def main_pin(store, kind: str, text: str, **kwargs) -> str:
    """A pin of the main conversation: recorded, and confirmed."""
    return confirm(store, store.pin(kind, text, **kwargs))


def main_close(store, obligation_id: str, note: str) -> str:
    """A close of the main conversation: recorded, and confirmed."""
    return confirm(store, store.close(obligation_id, note))


def append(store, kind: str, body: str, n: float, *, main: bool = True, **extra) -> str:
    """Append a record whose ``created_at`` is ``at(n)``, confirmed as the main conversation's unless
    ``main`` is false."""
    draft = {"kind": kind, "epistemic": "reported", "author": extra.pop("author", "model"), "body": body,
             "provenance": [], "session": store.session_id,
             "created_at": utc_timestamp(datetime.fromtimestamp(at(n), UTC)), **extra}
    record_id = store.ledger.append(draft).id
    return confirm(store, record_id) if main else record_id


def carried_ids(store) -> list[str]:
    return [c.record.id for c in store.project().carried()]


def snapshot(path: Path) -> dict[str, tuple]:
    """Every entry under ``path`` with its type and mode, modification time, size and content."""
    out = {}
    for p in [path, *sorted(path.rglob("*"))]:
        st = os.lstat(p)
        content = p.read_bytes() if stat.S_ISREG(st.st_mode) else os.readlink(p) if stat.S_ISLNK(st.st_mode) else None
        out[str(p.relative_to(path))] = (st.st_mode, st.st_mtime_ns, st.st_size, content)
    return out


@pytest.fixture
def home(tmp_path):
    return tmp_path / "contexto-home"


# Identity


def test_a_subdirectory_of_a_repository_and_its_root_are_one_project(tmp_path):
    root = repo(tmp_path)
    (root / "src" / "deep").mkdir(parents=True)
    assert project_root(root) == root
    assert project_root(root / "src" / "deep") == root
    assert project_root(root / "not" / "there" / "yet") == root  # a directory that does not exist still has a root


def test_a_linked_worktree_and_a_submodule_are_their_own_projects(tmp_path):
    root = repo(tmp_path)
    worktree = root / "trees" / "feature"
    (worktree / "pkg").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../../.git/worktrees/feature\n")  # a file, as git writes it
    assert project_root(worktree / "pkg") == worktree
    assert project_root(root / "trees") == root


def test_a_directory_outside_any_repository_is_its_own_project(tmp_path):
    plain = tmp_path.resolve() / "notes"
    (plain / "sub").mkdir(parents=True)
    if any((p / ".git").exists() for p in plain.parents):
        pytest.skip("pytest's temporary directory is inside a git repository")
    assert project_root(plain) == plain
    assert project_root(plain / "sub") == plain / "sub"


def test_a_git_entry_at_the_home_directory_is_ignored(tmp_path, monkeypatch):
    fake_home = tmp_path.resolve() / "home" / "user"
    (fake_home / ".git").mkdir(parents=True)  # dotfiles kept in a repository at ~
    (fake_home / "work" / "notes").mkdir(parents=True)
    assert project_root(fake_home / "work" / "notes", home=fake_home) == fake_home / "work" / "notes"
    assert project_root(fake_home, home=fake_home) == fake_home
    (fake_home / "work" / ".git").mkdir()
    assert project_root(fake_home / "work" / "notes", home=fake_home) == fake_home / "work"
    monkeypatch.setenv("HOME", str(fake_home))  # the default home is the user's
    (fake_home / "plain").mkdir()
    assert project_root(fake_home / "plain") == fake_home / "plain"
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    assert project_root(fake_home / "plain") == fake_home  # the same entry counts when it is not the home


def test_a_git_entry_at_the_filesystem_root_is_ignored(tmp_path, monkeypatch):
    real = os.path.lexists
    monkeypatch.setattr(project_module.os.path, "lexists", lambda p: str(p) == "/.git" or real(p))
    plain = tmp_path.resolve() / "plain"
    plain.mkdir()
    if any(real(p / ".git") for p in plain.parents):
        pytest.skip("pytest's temporary directory is inside a git repository")
    assert project_root(plain) == plain


def test_a_symlinked_working_directory_and_its_target_are_one_project(tmp_path, home):
    root = repo(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    assert project_root(link) == root
    first = new_session(home, "s1", link, 1)
    main_pin(first, "decision", "the codename is Obsidian")
    second = new_session(home, "s2", root, 2)
    assert [c.record.body for c in second.project().carried()] == ["the codename is Obsidian"]


def test_the_project_id_is_twelve_hex_characters_of_the_roots_hash(tmp_path):
    root = repo(tmp_path)
    assert project_id(root) == hashlib.sha256(os.fsencode(str(root))).hexdigest()[:12]
    assert project_id(root) != project_id(Path(str(root) + "-2"))


def test_no_subprocess_is_ever_run():
    # A repository's own configuration could run code (D52), so git is never asked.
    source = Path(project_module.__file__).read_text(encoding="utf-8")
    for runner in ("import subprocess", "from subprocess", "os.system", "os.popen", "os.exec", "os.spawn",
                   "os.posix_spawn", "os.fork"):
        assert runner not in source, runner


# The store


def test_set_workdir_records_the_project_root_beside_the_working_directory(tmp_path, home):
    root = repo(tmp_path)
    (root / "src").mkdir()
    store = SessionStore(home, "s1")
    store.set_workdir(root / "src")
    meta = json.loads((store.dir / "meta.json").read_text())
    assert meta["workdir"] == str(root / "src") and meta["project"] == str(root)
    assert "project_memory" not in meta
    view = store.project()
    assert view.root == root and view.id == project_id(root) and view.earlier == ()


def test_set_project_memory_records_the_flag_and_off_means_no_project(tmp_path, home):
    root = repo(tmp_path)
    store = new_session(home, "s1", root, 1)
    store.set_project_memory(False)
    assert json.loads((store.dir / "meta.json").read_text())["project_memory"] is False
    assert store.project() is None
    store.set_project_memory(True)
    assert json.loads((store.dir / "meta.json").read_text())["project_memory"] is True
    assert store.project().root == root
    with pytest.raises(ValueError):
        store.set_project_memory("off")


def test_a_store_with_no_working_directory_is_in_no_project(tmp_path, home):
    root = repo(tmp_path)
    rid = main_pin(new_session(home, "s1", root, 1), "decision", "x")
    bare = SessionStore(home, "bare")  # as `contexto proxy` and `contexto mcp` run by hand leave it
    os.utime(bare.dir / "page.key", (at(2), at(2)))
    assert bare.project() is None and bare.carried_text(6000) == ""
    main_pin(bare, "decision", "from nowhere")
    assert carried_ids(new_session(home, "s3", root, 3)) == [rid]


def test_a_memory_store_has_no_project():
    store = MemoryStore("s1")
    store.set_workdir(Path.cwd())
    store.set_project_memory(True)
    main_pin(store, "obligation", "x")
    assert store.project() is None and store.carried_text(6000) == ""


def test_a_store_as_an_earlier_release_wrote_it_joins_by_its_working_directorys_root(tmp_path, home):
    root = repo(tmp_path)
    (root / "src").mkdir()
    old = SessionStore(home, "old")
    old._update_meta({"workdir": str(root / "src")})  # v0.5.24: workdir only
    os.utime(old.dir / "page.key", (at(1), at(1)))
    rid = main_pin(old, "decision", "use sqlite")
    assert old.project().root == root
    new = new_session(home, "new", root, 2)
    assert carried_ids(new) == [rid]
    # and the other way: an old store that is the later session sees the new one
    late = SessionStore(home, "late")
    late._update_meta({"workdir": str(root)})
    os.utime(late.dir / "page.key", (at(3), at(3)))
    assert [m.id for m in late.project().earlier] == ["new", "old"]


def test_a_session_opened_read_only_gives_the_same_view_and_writes_nothing(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    kept = main_pin(first, "obligation", "whenever you give a date, use ISO 8601")
    second = new_session(home, "s2", root, 2)
    second.pin("decision", "this session's own")
    expected = (second.pinned_text(6000), second.state_text(), second.carried_text(6000), carried_ids(second))
    before = snapshot(home)
    reader = SessionStore.open_existing(home, "s2")
    assert (reader.pinned_text(6000), reader.state_text(), reader.carried_text(6000), carried_ids(reader)) == expected
    assert carried_ids(reader) == [kept]
    assert snapshot(home) == before


# Members and the earlier-sessions rule


def test_sessions_of_one_root_are_listed_oldest_last_and_only_earlier_ones_are_seen(tmp_path, home):
    root = repo(tmp_path)
    stores = [new_session(home, f"s{n}", root, n) for n in (1, 2, 3)]
    assert [m.id for m in stores[2].project().earlier] == ["s2", "s1"]  # newest first
    assert [m.id for m in stores[1].project().earlier] == ["s1"]
    assert stores[0].project().earlier == ()
    member = stores[2].project().earlier[0]
    assert member.dir == stores[1].dir and member.pages_dir == stores[1].pages_dir
    assert member.created == pytest.approx(at(2), abs=0.01) and member.memory is True


@pytest.mark.parametrize("first", ["a", "b"])
def test_two_sessions_created_in_either_order_never_both_see_each_other(tmp_path, home, first):
    root = repo(tmp_path)
    a = new_session(home, "aaaa", root, 1 if first == "a" else 2)
    b = new_session(home, "bbbb", root, 2 if first == "a" else 1)
    main_pin(a, "decision", "from a")
    main_pin(b, "decision", "from b")
    a_sees, b_sees = [m.id for m in a.project().earlier], [m.id for m in b.project().earlier]
    assert (a_sees, b_sees) == (([], ["aaaa"]) if first == "a" else (["bbbb"], []))
    assert [c.record.body for c in (b if first == "a" else a).project().carried()] == [f"from {first}"]
    assert (a if first == "a" else b).project().carried() == ()


def test_sessions_created_at_the_same_instant_are_ordered_by_id(tmp_path, home):
    root = repo(tmp_path)
    a, b = new_session(home, "aaaa", root, 1), new_session(home, "bbbb", root, 1)
    assert a.project().earlier == () and [m.id for m in b.project().earlier] == ["aaaa"]


def test_two_projects_that_share_a_path_prefix_never_see_each_other(tmp_path, home):
    one, two = repo(tmp_path, "proj"), repo(tmp_path, "proj-2")
    main_pin(new_session(home, "s1", one, 1), "decision", "for proj")
    main_pin(new_session(home, "s2", two, 2), "decision", "for proj-2")
    third, fourth = new_session(home, "s3", one, 3), new_session(home, "s4", two, 4)
    assert [c.record.body for c in third.project().carried()] == ["for proj"]
    assert [c.record.body for c in fourth.project().carried()] == ["for proj-2"]
    assert [m.id for m in third.project().earlier] == ["s1"] and [m.id for m in fourth.project().earlier] == ["s2"]
    assert "for proj-2" not in third.pinned_text(6000) and "for proj:" not in fourth.state_text()


def test_sessions_in_a_repositorys_subdirectories_share_it_and_a_worktrees_sessions_do_not(tmp_path, home):
    root = repo(tmp_path)
    worktree = root / "trees" / "feature"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../../.git/worktrees/feature\n")
    (root / "src").mkdir()
    main_pin(new_session(home, "s1", root / "src", 1), "decision", "pinned in a subdirectory")
    main_pin(new_session(home, "s2", worktree / "src", 2), "decision", "pinned in the worktree")
    at_root, in_tree = new_session(home, "s3", root, 3), new_session(home, "s4", worktree, 4)
    assert [c.record.body for c in at_root.project().carried()] == ["pinned in a subdirectory"]
    assert [c.record.body for c in in_tree.project().carried()] == ["pinned in the worktree"]
    assert at_root.project().root == root and in_tree.project().root == worktree


def test_a_session_with_project_memory_off_neither_gives_nor_receives(tmp_path, home):
    root = repo(tmp_path)
    main_pin(new_session(home, "s1", root, 1), "decision", "given")
    off = new_session(home, "s2", root, 2, memory=False)
    main_pin(off, "decision", "kept to itself")
    assert off.project() is None and off.carried_text(6000) == "" and "given" not in off.pinned_text(6000)
    third = new_session(home, "s3", root, 3)
    assert [m.id for m in third.project().earlier] == ["s1"]
    assert [c.record.body for c in third.project().carried()] == ["given"]
    with pytest.raises(ValueError):  # a record of a session that left the project is not one this session knows
        third.pin("decision", "x", supersedes=[off.ledger.records()[0].id])


def test_project_sessions_lists_a_root_without_a_store(tmp_path, home):
    root, other = repo(tmp_path), repo(tmp_path, "other")
    new_session(home, "s1", root, 1)
    new_session(home, "s2", root, 2, memory=False)
    new_session(home, "s3", other, 3)
    SessionStore(home, "bare")
    listing = project_sessions(home, root)
    assert listing.root == root and listing.unreadable == 0
    assert [(m.id, m.memory) for m in listing.sessions] == [("s2", False), ("s1", True)]
    assert project_sessions(home, tmp_path.resolve() / "nothing").sessions == ()
    assert project_sessions(tmp_path / "no-such-home", root).sessions == ()


def test_a_project_view_without_a_session_is_what_a_session_started_now_would_carry(tmp_path, home):
    root = repo(tmp_path)
    a = main_pin(new_session(home, "s1", root, 1), "obligation", "first")
    b = main_pin(new_session(home, "s2", root, 2), "decision", "second")
    view = Project(home, root)
    assert [m.id for m in view.earlier] == ["s2", "s1"]
    assert [(c.record.id, c.session) for c in view.carried()] == [(a, "s1"), (b, "s2")]
    assert view.read == 2 and view.skipped == ()


# What is carried


def test_only_open_obligations_and_active_decisions_pinned_in_the_main_conversation_are_carried(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    duty = main_pin(first, "obligation", "whenever you give a date, use ISO 8601")
    choice = main_pin(first, "decision", "the codename is Obsidian")
    first.pin("obligation", "pinned by a subagent or an earlier release")  # never confirmed as main's
    first.pin("decision", "pinned by a person", author="user")
    first.pin("decision", "an author that looks like the main conversation's", author="model:main")
    main_pin(first, "event", "tests pass", epistemic="verified",
             provenance=[{"type": "command", "locator": "pytest"}])  # a verified fact is not carried
    done = main_pin(first, "obligation", "already done")
    first.close(done, "done")
    replaced = main_pin(first, "decision", "the codename is Onyx")
    newer = main_pin(first, "decision", "the codename is Opal", supersedes=[replaced])
    second = new_session(home, "s2", root, 2)
    carried = second.project().carried()
    assert [c.record.id for c in carried] == [duty, newer, choice]  # obligations first, then newest first
    assert {c.session for c in carried} == {"s1"} and all(c.record.author == "model" for c in carried)


def test_a_carried_obligation_closed_in_the_next_session_is_not_carried_into_the_third(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    duty = main_pin(first, "obligation", "ship the migration notes")
    untouched = snapshot(first.dir)
    second = new_session(home, "s2", root, 2)
    assert carried_ids(second) == [duty]
    closure = main_close(second, duty, "shipped")
    assert snapshot(first.dir) == untouched  # the closure is written to the closing session's ledger only
    assert [r.id for r in second.ledger.records()] == [closure] and second.ledger.verify() == []
    assert second.ledger.records()[0].target == duty
    assert carried_ids(second) == [] and "ship the migration notes" not in second.pinned_text(6000)
    with pytest.raises(ValueError, match="not an open obligation"):
        second.close(duty, "again")
    third = new_session(home, "s3", root, 3)
    assert carried_ids(third) == [] and third.carried_text(6000) == ""
    with pytest.raises(ValueError):
        third.close(duty, "a third time")


def test_a_carried_decision_superseded_in_the_next_session_is_replaced_for_the_third(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    old = main_pin(first, "decision", "use postgres")
    second = new_session(home, "s2", root, 2)
    new = main_pin(second, "decision", "use sqlite", supersedes=[old])
    assert carried_ids(second) == [] and [r.id for r in second.state().active] == [new]
    assert len(first.ledger.records()) == 1
    third = new_session(home, "s3", root, 3)
    carried = third.project().carried()
    assert [(c.record.id, c.session, c.record.body) for c in carried] == [(new, "s2", "use sqlite")]


def test_a_closure_or_a_replacement_names_only_a_record_this_session_carries(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    side = first.pin("obligation", "pinned outside the main conversation")
    fact = main_pin(first, "decision", "a decision is not an obligation")
    second = new_session(home, "s2", root, 2)
    for target in (side, fact, "rec_" + "0" * 24):
        with pytest.raises(ValueError):
            second.close(target, "x")
    with pytest.raises(ValueError, match="supersedes unknown record ids"):
        second.pin("decision", "x", supersedes=[side])
    assert second.ledger.records() == []


def test_a_record_created_after_the_later_session_started_is_not_carried_but_its_closure_is_honoured(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    duty = main_pin(first, "obligation", "open when the second session starts")
    second = new_session(home, "s2", root, 2)
    assert carried_ids(second) == [duty]
    late = append(first, "obligation", "pinned while the second session runs", 3, owner="assistant")
    assert carried_ids(second) == [duty]  # additions are frozen at the session's start
    append(first, "closure", "done in the first session", 3, target=duty)
    assert carried_ids(second) == []  # removals apply whenever they happen
    with pytest.raises(ValueError, match="not an open obligation"):
        second.close(duty, "too late")
    assert carried_ids(new_session(home, "s4", root, 4)) == [late]


def test_a_close_is_refused_when_another_session_closed_the_obligation_just_before(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    duty = main_pin(first, "obligation", "ship the notes")
    second = new_session(home, "s2", root, 2)
    real = second.ledger.append

    def racing(draft, **kwargs):  # the first session closes it after this close looked and before it locks
        first.close(duty, "done in the first session")
        return real(draft, **kwargs)

    second.ledger.append = racing
    with pytest.raises(ValueError, match="not an open obligation"):
        second.close(duty, "done in the second session")
    assert second.ledger.records() == []


def test_a_sessions_own_line_order_stands_when_its_clock_stepped_back(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    old = append(first, "decision", "use postgres", 1.9)
    new = append(first, "decision", "use sqlite", 1.5, supersedes=[old])  # written later, stamped earlier
    assert [r.id for r in first.state().active] == [new]
    second = new_session(home, "s2", root, 2)
    assert carried_ids(second) == [new] and [r.id for r in second.project().state().active] == [new]


def test_carried_records_name_their_session_with_a_label_and_the_records_date(tmp_path, home):
    root = repo(tmp_path)
    ids = ("3f2a9c1e" + "0" * 24, "7b" * 16, "7b7b7b7b" + "1" * 24, "short", "short-2")
    for n, sid in enumerate(ids, start=1):
        append(new_session(home, sid, root, n), "decision", f"from {sid}", n)
    view = new_session(home, "viewer", root, 9).project()
    labels = {c.session: c.label for c in view.carried()}
    assert labels == {ids[0]: "3f2a9c1e", ids[1]: "7b7b7b7b7", ids[2]: "7b7b7b7b1", "short": "short",
                      "short-2": "short-2"}
    assert {view.label(m.id) for m in view.earlier} == set(labels.values())
    assert [view.earlier_session(label).id for label in labels.values()] == list(labels)
    assert view.earlier_session(ids[0]).id == ids[0]
    for unknown in ("7b7b7b7b", "viewer", "nope", ""):  # ambiguous, this session itself, no such session
        with pytest.raises(LookupError):
            view.earlier_session(unknown)
    newest = view.carried()[0]
    assert newest.session == "short-2"
    assert newest.date == datetime.fromtimestamp(at(5), UTC).strftime("%Y-%m-%d")


def test_a_forgotten_record_is_not_carried_and_a_session_that_left_gives_nothing(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    old = main_pin(first, "decision", "use postgres")
    new = main_pin(first, "decision", "use sqlite", supersedes=[old])
    duty = main_pin(first, "obligation", "ship it")
    other = main_pin(new_session(home, "s2", root, 2), "decision", "from the second")
    third = new_session(home, "s3", root, 3)
    assert carried_ids(third) == [duty, other, new]
    first.release()
    (first.dir / "live.lock").unlink()  # a session no current release has opened has none
    quiet = (last_activity(first.dir), sorted(p.name for p in first.dir.iterdir()))
    forget(home, "s1", records=[new])
    assert carried_ids(third) == [duty, other]  # and the decision it replaced does not come back
    assert last_activity(first.dir) == quiet[0]  # forgetting is not activity: a prune is not put off by it
    assert sorted(p.name for p in first.dir.iterdir()) == sorted([*quiet[1], "live.lock"])  # nothing deleted
    assert json.loads((first.dir / "meta.json").read_text())["forgotten"] == [new]
    forget(home, "s1", records=[duty, new])
    assert json.loads((first.dir / "meta.json").read_text())["forgotten"] == sorted([duty, new])
    assert carried_ids(third) == [other]
    forget(home, "s2", leave=True)
    assert json.loads(read_meta_text(home, "s2"))["project_memory"] is False
    assert carried_ids(third) == [] and third.project().earlier[0].id == "s1"
    assert [(m.id, m.memory) for m in project_sessions(home, root).sessions] == [("s3", True), ("s2", False),
                                                                                 ("s1", True)]
    assert json.loads(read_meta_text(home, "s1"))["workdir"] == str(root)  # the other fields stand


def read_meta_text(home: Path, sid: str) -> str:
    return (home / "sessions" / sid / "meta.json").read_text()


def test_forget_refuses_what_it_cannot_do_and_never_recreates_a_pruned_session(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rid = main_pin(first, "decision", "x")
    first.release()
    with pytest.raises(ValueError):
        forget(home, "s1", records=["not-a-record-id"])
    with pytest.raises(ValueError):
        forget(home, "s1")  # nothing asked
    with pytest.raises(SessionNotFoundError):
        forget(home, "ghost", records=[rid])
    assert not (home / "sessions" / "ghost").exists()
    shutil.rmtree(first.dir)  # as `contexto sessions prune` leaves it
    with pytest.raises(SessionNotFoundError):
        forget(home, "s1", records=[rid])
    with pytest.raises(SessionNotFoundError):
        forget(home, "s1", leave=True)
    assert not first.dir.exists()
    linked = tmp_path / "elsewhere"
    shutil.copytree(new_session(home, "s2", root, 2).dir, linked)
    (home / "sessions" / "link").symlink_to(linked, target_is_directory=True)
    before = snapshot(linked)
    with pytest.raises(SessionNotFoundError):
        forget(home, "link", leave=True)
    assert snapshot(linked) == before
    damaged = new_session(home, "s3", root, 3)
    damaged.release()
    (damaged.dir / "meta.json").write_text("{not json")
    with pytest.raises(ValueError):
        forget(home, "s3", leave=True)
    assert (damaged.dir / "meta.json").read_text() == "{not json"  # never replaced by a guess


def test_forget_works_on_a_session_in_use_and_keeps_the_file_private(tmp_path, home):
    root = repo(tmp_path)
    live = new_session(home, "s1", root, 1)  # still open, as a running session's store is
    rid = main_pin(live, "decision", "x")
    old = os.umask(0)
    try:
        forget(home, "s1", records=[rid])
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(live.dir / "meta.json").st_mode) == 0o600
    assert live.project() is not None and json.loads(read_meta_text(home, "s1"))["forgotten"] == [rid]
    live.set_turn_room("t1", 100)  # a later write by the running session keeps the list
    assert json.loads(read_meta_text(home, "s1"))["forgotten"] == [rid]


@pytest.mark.parametrize("locks_before", [False, True])
def test_forget_meeting_a_prune_emptying_the_session_says_it_is_gone_and_leaves_no_lock_of_its_own(
        tmp_path, home, monkeypatch, locks_before):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rid = main_pin(first, "decision", "x")
    first.release()
    for name in ("live.lock", "meta.lock"):
        if not locks_before:  # prune has unlinked them already, and is about to remove the rest
            (first.dir / name).unlink()
    real = project_module.read_regular

    def pruning(path, *args, **kwargs):  # prune removes meta.json after forget took its locks
        if Path(path).name == "meta.json":
            Path(path).unlink(missing_ok=True)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(project_module, "read_regular", pruning)
    with pytest.raises(SessionNotFoundError, match="no longer exists"):
        forget(home, "s1", records=[rid])
    left = sorted(p.name for p in first.dir.iterdir())
    assert ("live.lock" in left, "meta.lock" in left) == (locks_before, locks_before)  # only its own are removed


def test_forget_keeps_the_directory_time_of_a_write_made_after_its_own(tmp_path, home, monkeypatch):
    root = repo(tmp_path)
    live = new_session(home, "s1", root, 1)
    rid = main_pin(live, "decision", "x")
    later = time.time_ns() + 7_000_000_000
    real = project_module._dir_time
    calls = []

    def running(directory):  # the running session writes in its directory just after forget's own write
        found = real(directory)
        calls.append(found)
        if len(calls) == 1:
            os.utime(directory, ns=(later, later))
        return found

    monkeypatch.setattr(project_module, "_dir_time", running)
    forget(home, "s1", records=[rid])
    assert os.stat(live.dir).st_mtime_ns == later and len(calls) >= 1


# The readers and the cache


def test_the_readers_return_what_is_stored_and_none_or_nothing_when_absent(tmp_path, home):
    root = repo(tmp_path)
    store = new_session(home, "s1", root, 1)
    rid = main_pin(store, "decision", "x")
    assert read_meta(store.dir)["project"] == str(root)
    assert [r.id for r in read_records(store.dir)] == [rid]
    empty = SessionStore(home, "empty")
    assert read_meta(empty.dir) is None and read_records(empty.dir) == ()
    assert read_meta(home / "sessions" / "ghost") is None and read_records(home / "sessions" / "ghost") == ()


def test_an_unchanged_ledger_is_read_once_per_process_and_again_when_it_changes(tmp_path, home, monkeypatch):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    one = main_pin(first, "decision", "one")
    second = new_session(home, "s2", root, 2)
    reads: list[str] = []
    real = project_module.read_regular

    def counting(path):
        reads.append(f"{Path(path).parent.name}/{Path(path).name}")
        return real(path)

    monkeypatch.setattr(project_module, "read_regular", counting)
    assert carried_ids(second) == [one]
    for _ in range(3):
        assert carried_ids(second) == [one] and one in second.pinned_text(6000)
    assert reads.count("s1/ledger.jsonl") == 1 and reads.count("s1/meta.json") == 1
    two = append(first, "decision", "two", 1.5)  # and its id is added to the session's main_records
    assert carried_ids(second) == [two, one]
    assert reads.count("s1/ledger.jsonl") == 2 and reads.count("s1/meta.json") == 2
    first.release()
    forget(home, "s1", records=[one])
    seen = reads.count("s1/meta.json")  # forget read it too
    assert carried_ids(second) == [two] and carried_ids(second) == [two]
    assert reads.count("s1/ledger.jsonl") == 2 and reads.count("s1/meta.json") == seen + 1


def test_a_view_is_a_snapshot_and_a_new_view_sees_a_pruned_session_gone(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    rid = main_pin(first, "decision", "x")
    second = new_session(home, "s2", root, 2)
    view = second.project()
    assert [c.record.id for c in view.carried()] == [rid]
    first.release()
    shutil.rmtree(first.dir)  # pruning removes its contribution
    assert [c.record.id for c in view.carried()] == [rid]  # one view answers the same every time
    assert second.project().earlier == () and carried_ids(second) == []
    assert second.carried_text(6000) == "" and rid not in second.pinned_text(6000)


# The ledger accepts records it is told about


@pytest.mark.parametrize("make", [lambda tmp: Ledger(tmp / "a"), lambda tmp: MemoryLedger()], ids=["file", "memory"])
def test_append_checks_a_closure_and_supersedes_against_extra_known_records(tmp_path, make):
    other = MemoryLedger()
    duty = other.append({"kind": "obligation", "epistemic": "reported", "author": "x", "body": "duty",
                         "provenance": [], "owner": "assistant"})
    choice = other.append({"kind": "decision", "epistemic": "reported", "author": "x", "body": "choice",
                           "provenance": []})
    ledger = make(tmp_path)
    closure = {"kind": "closure", "epistemic": "reported", "author": "x", "body": "done", "provenance": [],
               "target": duty.id}
    with pytest.raises(LedgerError, match="closure target does not exist"):
        ledger.append(dict(closure))
    with pytest.raises(LedgerError, match="not an obligation"):
        ledger.append({**closure, "target": choice.id}, known=[duty, choice])
    replacement = {"kind": "decision", "epistemic": "reported", "author": "x", "body": "new", "provenance": [],
                   "supersedes": [choice.id]}
    with pytest.raises(LedgerError, match="supersedes unknown record ids"):
        ledger.append(dict(replacement), known=[duty])
    assert ledger.records() == []
    first = ledger.append(dict(closure), known=[duty, choice])
    second = ledger.append(dict(replacement), known=(duty, choice))
    assert [r.id for r in ledger.records()] == [first.id, second.id] and ledger.verify() == []
    assert other.records() == [duty, choice]  # the known records are never written anywhere


def test_a_new_session_is_stamped_from_the_clock_its_records_use(tmp_path):
    """The key's time is the session's creation time. It is written from ``time.time_ns()``, the clock
    behind a record's ``created_at``, not left to the file system's coarser one, so a pin made just
    before a session is created is never judged newer than that session."""
    before = time.time_ns()
    store = SessionStore(tmp_path / "home", "stamped")
    after = time.time_ns()
    assert before <= os.stat(store.dir / "page.key").st_mtime_ns <= after


def test_a_session_alone_in_its_project_lists_the_sessions_once_per_pinned_text(tmp_path, home, monkeypatch):
    lone = new_session(home, "lone", repo(tmp_path), 1)
    main_pin(lone, "decision", "x")
    new_session(home, "elsewhere", repo(tmp_path, "other"), 2)
    listed = []
    real = os.scandir

    def counting(path, *args, **kwargs):
        listed.append(Path(path).name)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(project_module.os, "scandir", counting)
    for call in (lambda: lone.pinned_text(6000), lambda: lone.state_text(), lambda: lone.carried_text(6000)):
        listed.clear()
        call()
        assert listed == ["sessions"]


def test_a_session_resumed_from_another_project_stays_in_the_project_it_started_in(tmp_path, home):
    """Review B, poc2: a resume from another directory must not move the session and its records."""
    from contexto.launcher import open_session

    sandbox, work = repo(tmp_path, "sandbox"), repo(tmp_path, "work")
    x = new_session(home, "x", sandbox, 1)
    rule = main_pin(x, "obligation", "pinned while reading the sandbox")
    x.release()
    resumed = open_session(home, "x", work)  # `contexto run --session x claude` from ~/work
    assert resumed.workdir() == work  # file sources are checked where it runs now
    assert json.loads(read_meta_text(home, "x"))["project"] == str(sandbox)
    resumed.release()
    assert carried_ids(new_session(home, "in-work", work, 2)) == []
    assert carried_ids(new_session(home, "in-sandbox", sandbox, 3)) == [rule]


@pytest.mark.skipif(not hasattr(__import__("fcntl"), "F_GETPATH"), reason="F_GETPATH is macOS only")
def test_another_spelling_of_a_directory_names_the_same_project(tmp_path):
    """Review B, poc6: on a file system that ignores case or Unicode normalisation, ``--dir ~/Proj`` and
    ``~/proj``, or an NFD and an NFC name, are one directory and so one project."""
    import unicodedata

    base = tmp_path.resolve()
    (base / "Proj" / ".git").mkdir(parents=True)
    if not (base / "proj").exists():
        pytest.skip("this file system tells the cases apart")
    assert project_root(base / "proj") == project_root(base / "Proj") == base / "Proj"
    assert project_root(base / "PROJ" / ".git") == base / "Proj"
    nfc = unicodedata.normalize("NFC", "café")
    (base / nfc / ".git").mkdir(parents=True)
    nfd = unicodedata.normalize("NFD", nfc)
    assert str(project_root(base / nfd)) == str(project_root(base / nfc))
    assert project_id(project_root(base / nfd)) == project_id(project_root(base / nfc))
