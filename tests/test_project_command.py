"""``contexto project show`` and ``contexto project forget`` (D69): what a person sees of project
memory and how they take a record or a session out of it.

Sessions are built as ``test_project`` builds them (real stores of one project under a temporary
``CONTEXTO_HOME``, created at chosen times a year from now, pins of the main conversation) and the
commands run through ``contexto.cli.main``. A store is released once it is written, as a session that
ended is, unless the test is about a session in use.
"""

from __future__ import annotations

import io
import json
import os
import select
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from contexto import cli
from contexto.store import SessionStore
from contexto.store.project import project_id
from contexto.store.sessions import last_activity
from test_project import at, carried_ids, main_pin, new_session, repo, snapshot

SESSION_FIELDS = {"id", "created", "last_activity", "pages", "records", "carried", "project_memory", "skipped",
                  "skipped_error", "in_use"}
CARRIED_FIELDS = {"id", "kind", "owner", "topic", "session", "label", "date", "created_at", "text"}
TABLE_HEAD = ["ID", "CREATED", "LAST", "ACTIVITY", "PAGES", "RECORDS", "CARRIED", "MEMORY", "NOTES"]
NO_TERMINAL = "there is no terminal to confirm on; rerun with --yes"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "contexto-home"
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    return home


def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(store: SessionStore, rid: str):
    return next(r for r in store.ledger.records() if r.id == rid)


def show(capsys, root: Path) -> dict:
    code, out, err = run(capsys, "project", "show", "--dir", root, "--json")
    assert code == 0 and err == "", err
    return json.loads(out)


def shown_carried(capsys, root: Path) -> list[str]:
    return [c["id"] for c in show(capsys, root)["carried"]]


def memory(capsys, root: Path) -> dict[str, bool]:
    return {s["id"]: s["project_memory"] for s in show(capsys, root)["sessions"]}


class Terminal(io.StringIO):
    """A stdin that is a terminal, with the answers a person types."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def three(tmp_path, home):
    """A project of three ended sessions: ``s1`` (an open obligation of the main conversation, a pin
    that is not the main conversation's, one page), ``s2`` (a decision) and ``s3`` (memory off)."""
    root = repo(tmp_path)
    s1 = new_session(home, "s1", root, 1)
    duty = main_pin(s1, "obligation", "ship the migration notes")
    s1.pin("decision", "pinned by a subagent")
    s1.page_out([[{"role": "user", "content": "a paged turn"}, {"role": "assistant", "content": "ok"}]], 1)
    s2 = new_session(home, "s2", root, 2)
    choice = main_pin(s2, "decision", "use sqlite", topic="db")
    s3 = new_session(home, "s3", root, 3, memory=False)
    main_pin(s3, "decision", "kept to itself")
    for store in (s1, s2, s3):
        store.release()
    return root, (s1, s2, s3), duty, choice


# show


def test_show_prints_the_root_the_id_the_sessions_newest_first_and_what_a_new_session_would_carry(three, capsys):
    root, (s1, s2, _), duty, choice = three
    code, out, err = run(capsys, "project", "show", "--dir", root)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[:3] == [f"Project: {root}", f"Project id: {project_id(root)}", "Sessions, newest first:"]
    assert lines[3].split() == TABLE_HEAD
    rows = [line.split() for line in lines[4:7]]
    assert [(r[0], r[1]) for r in rows] == [("s3", iso(at(3))), ("s2", iso(at(2))), ("s1", iso(at(1)))]
    assert [r[2] for r in rows] == [iso(last_activity(s.dir)) for s in three[1][::-1]]
    assert [r[3:] for r in rows] == [["0", "1", "0", "off"], ["0", "1", "1", "on"], ["1", "2", "1", "on"]]
    assert lines[7] == "Records a session started now would carry (2), in the order they are carried:"
    assert lines[8] == (f"  {duty} obligation, owner assistant (session s1, {record(s1, duty).created_at[:10]}): "
                        "ship the migration notes")
    assert lines[9] == f"  {choice} decision [db] (session s2, {record(s2, choice).created_at[:10]}): use sqlite"
    assert len(lines) == 10 and "pinned by a subagent" not in out and "kept to itself" not in out


def test_show_json_gives_the_same_facts_under_stable_names(three, capsys):
    root, (s1, s2, s3), duty, choice = three
    code, out, err = run(capsys, "project", "show", "--dir", root, "--json")
    data = json.loads(out)
    assert code == 0 and err == ""
    assert set(data) == {"root", "id", "sessions", "carried", "unreadable_sessions"}
    assert data["root"] == str(root) and data["id"] == project_id(root) and data["unreadable_sessions"] == 0
    assert [s["id"] for s in data["sessions"]] == ["s3", "s2", "s1"]
    assert all(set(s) == SESSION_FIELDS for s in data["sessions"])
    assert data["sessions"][2] == {
        "id": "s1", "created": iso(at(1)), "last_activity": iso(last_activity(s1.dir)), "pages": 1, "records": 2,
        "carried": 1, "project_memory": True, "skipped": False, "skipped_error": None, "in_use": False}
    assert data["sessions"][0]["project_memory"] is False and data["sessions"][0]["carried"] == 0
    assert all(set(c) == CARRIED_FIELDS for c in data["carried"])
    assert data["carried"] == [
        {"id": duty, "kind": "obligation", "owner": "assistant", "topic": None, "session": "s1", "label": "s1",
         "date": record(s1, duty).created_at[:10], "created_at": record(s1, duty).created_at,
         "text": "ship the migration notes"},
        {"id": choice, "kind": "decision", "owner": None, "topic": "db", "session": "s2", "label": "s2",
         "date": record(s2, choice).created_at[:10], "created_at": record(s2, choice).created_at,
         "text": "use sqlite"}]


def test_show_says_which_sessions_are_in_use_now(three, capsys, home):
    root = three[0]
    held = SessionStore(home, "s2")  # as a running launch holds it
    code, out, _ = run(capsys, "project", "show", "--dir", root)
    row = next(line for line in out.splitlines() if line.split()[:1] == ["s2"])
    assert code == 0 and row.endswith("in use")
    assert {s["id"]: s["in_use"] for s in show(capsys, root)["sessions"]} == {"s3": False, "s2": True, "s1": False}
    held.release()
    assert not any(s["in_use"] for s in show(capsys, root)["sessions"])


def test_show_changes_nothing_in_any_session(three, capsys, home, tmp_path):
    root = three[0]
    damaged = new_session(home, "damaged", root, 4)
    main_pin(damaged, "decision", "in a ledger that does not verify")
    damaged.release()
    damaged.ledger.path.write_bytes(b"{not json\n" + damaged.ledger.path.read_bytes())
    unreadable = new_session(home, "no-meta", root, 5)
    unreadable.release()
    (unreadable.dir / "meta.json").write_text("{not json")
    never_opened = new_session(home, "no-lock", root, 6)
    main_pin(never_opened, "obligation", "from a session no current release has opened")
    never_opened.release()
    for lock in ("live.lock", "ledger.lock", "meta.lock"):  # a reader that takes a lock would make the file again
        (never_opened.dir / lock).unlink()
    new_session(home, "no-ledger", root, 6.5).release()  # nothing pinned: no ledger, and no lock file for one
    assert not list((home / "sessions" / "no-ledger").glob("ledger*"))
    elsewhere = new_session(home, "elsewhere", repo(tmp_path, "other"), 7)
    main_pin(elsewhere, "decision", "another project")
    elsewhere.release()
    held = new_session(home, "held", root, 8)  # in use while it is looked at
    main_pin(held, "decision", "from a running session")
    before = snapshot(home)
    for argv in (["--dir", root], ["--dir", root, "--json"], ["--dir", tmp_path / "other"]):
        code, out, err = run(capsys, "project", "show", *argv)
        assert code == 0 and err == "" and out
    assert snapshot(home) == before  # the same entries, bytes, modes and modification times
    held.release()


def test_show_of_a_project_with_no_session_is_one_line_and_creates_no_home(tmp_path, home, capsys, monkeypatch):
    root = repo(tmp_path)
    code, out, err = run(capsys, "project", "show", "--dir", root)
    assert (code, err) == (0, "")
    assert out == f"No sessions of project {root} (id {project_id(root)}) under {home / 'sessions'}.\n"
    assert show(capsys, root) == {"root": str(root), "id": project_id(root), "sessions": [], "carried": [],
                                  "unreadable_sessions": 0}
    monkeypatch.chdir(root)  # the default is the current directory
    assert run(capsys, "project", "show") == (0, out, "")
    assert not home.exists()
    main_pin(new_session(home, "s1", repo(tmp_path, "other"), 1), "decision", "another project")
    assert run(capsys, "project", "show") == (0, out, "")  # sessions of other projects are not this one's


def test_show_dir_in_a_subdirectory_is_the_repository_and_in_a_linked_worktree_is_the_worktree(tmp_path, home,
                                                                                               capsys, monkeypatch):
    root = repo(tmp_path)
    worktree = root / "trees" / "feature"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../../.git/worktrees/feature\n")  # a file, as git writes it
    (root / "src" / "deep").mkdir(parents=True)
    in_repo = main_pin(new_session(home, "s1", root / "src", 1), "decision", "pinned in a subdirectory")
    in_tree = main_pin(new_session(home, "s2", worktree / "src", 2), "decision", "pinned in the worktree")
    for given in (root, root / "src" / "deep", root / "trees"):
        data = show(capsys, given)
        assert data["root"] == str(root) and [s["id"] for s in data["sessions"]] == ["s1"]
        assert [c["id"] for c in data["carried"]] == [in_repo]
    for given in (worktree, worktree / "src"):
        data = show(capsys, given)
        assert data["root"] == str(worktree) and data["id"] == project_id(worktree) != project_id(root)
        assert [s["id"] for s in data["sessions"]] == ["s2"] and [c["id"] for c in data["carried"]] == [in_tree]
    monkeypatch.chdir(worktree / "src")
    code, out, _ = run(capsys, "project", "show")
    assert code == 0 and out.startswith(f"Project: {worktree}\n") and "pinned in a subdirectory" not in out
    link = tmp_path / "link-to-deep"
    link.symlink_to(root / "src" / "deep", target_is_directory=True)
    assert show(capsys, link)["root"] == str(root)  # a symlinked path is its target's project


def test_two_projects_that_share_a_path_prefix_are_shown_apart(tmp_path, home, capsys):
    one, two = repo(tmp_path, "proj"), repo(tmp_path, "proj-2")
    first = main_pin(new_session(home, "s1", one, 1), "decision", "for proj")
    second = main_pin(new_session(home, "s2", two, 2), "decision", "for proj-2")
    assert [s["id"] for s in show(capsys, one)["sessions"]] == ["s1"] and shown_carried(capsys, one) == [first]
    assert [s["id"] for s in show(capsys, two)["sessions"]] == ["s2"] and shown_carried(capsys, two) == [second]
    code, out, _ = run(capsys, "project", "show", "--dir", one)
    assert code == 0 and "for proj-2" not in out and "s2" not in out.split()


@pytest.mark.parametrize("damage", ["a line that is not a record", "a fifo", "a directory"])
def test_show_lists_a_session_with_a_damaged_ledger_as_skipped_and_counts_unreadable_directories(
        three, capsys, home, damage):
    root, (s1, s2, _), duty, choice = three
    path = s2.ledger.path
    if damage == "a line that is not a record":
        path.write_bytes(b'{"a": 1}\n' + path.read_bytes())
    else:
        path.unlink()
        if damage == "a fifo":
            os.mkfifo(path)
        else:
            path.mkdir()
    for n, content in enumerate((b"{not json", b"\xff\xfe")):
        broken = new_session(home, f"broken{n}", root, 4 + n)
        broken.release()
        (broken.dir / "meta.json").write_bytes(content)
    code, out, err = run(capsys, "project", "show", "--dir", root)
    assert code == 0 and err == "" and "Traceback" not in out
    row = next(line for line in out.splitlines() if line.split()[:1] == ["s2"])
    assert row.split()[3:7] == ["0", "?", "0", "on"] and "skipped as damaged" in row
    assert out.splitlines()[-1] == (f"2 session directories under {home / 'sessions'} could not be read and are "
                                    "in no project.")
    assert duty in out and choice not in out and "broken" not in out
    data = show(capsys, root)
    assert data["unreadable_sessions"] == 2 and [c["id"] for c in data["carried"]] == [duty]
    skipped = next(s for s in data["sessions"] if s["id"] == "s2")
    assert skipped["skipped"] is True and skipped["records"] is None and skipped["carried"] == 0
    assert skipped["skipped_error"] and "/" not in skipped["skipped_error"]  # an error class, never a path


def test_show_and_forget_report_a_home_or_a_directory_that_cannot_be_used_in_one_line(tmp_path, monkeypatch, capsys):
    root = repo(tmp_path)
    (tmp_path / "file").write_text("")
    for argv in (["show"], ["show", "--json"], ["forget", "--all", "--yes"]):
        for given in (tmp_path / "missing", tmp_path / "file", ""):
            monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "contexto-home"))
            code, out, err = run(capsys, "project", *argv, "--dir", given)
            assert (code, out) == (1, "") and err == f"contexto: error: --dir {str(given)!r} is not a directory\n"
        monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "file"))
        code, out, err = run(capsys, "project", *argv, "--dir", root)
        assert (code, out) == (1, "")
        assert err == f"contexto: error: {tmp_path / 'file'} is not a directory (check $CONTEXTO_HOME)\n"
    assert not (tmp_path / "contexto-home").exists() and not (tmp_path / "missing").exists()
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()  # the current directory was deleted under the shell
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "contexto-home"))
    code, out, err = run(capsys, "project", "show")
    assert (code, out) == (1, "") and len(err.splitlines()) == 1 and "Traceback" not in err
    assert err.startswith("contexto: error: the current directory cannot be resolved: ")
    monkeypatch.chdir(tmp_path)


def test_show_of_a_session_pruned_while_it_is_read_is_not_an_error(three, capsys, home, monkeypatch):
    from contexto.commands import project as command

    root, (s1, s2, _), duty, choice = three
    real = command.last_activity

    def pruning(path):  # s2 is deleted after the project's sessions were listed
        if path == s2.dir:
            shutil.rmtree(s2.dir)
        return real(path)

    monkeypatch.setattr(command, "last_activity", pruning)
    code, out, err = run(capsys, "project", "show", "--dir", root)
    assert code == 0 and err == "" and "Traceback" not in out
    row = next(line for line in out.splitlines() if line.split()[:1] == ["s2"])
    # Its record was read before it went, so this one listing still counts it as carried; its files are gone.
    assert row.split()[2:] == ["?", "0", "0", "1", "on"]
    assert not s2.dir.exists()  # and nothing is made again
    assert list(memory(capsys, root)) == ["s3", "s1"] and shown_carried(capsys, root) == [duty]


@pytest.mark.parametrize("argv", [
    ["project"], ["project", "forget"], ["project", "forget", "--yes"], ["project", "forget", "--record"],
    ["project", "forget", "--session"], ["project", "forget", "--all", "--session", "s1"],
    ["project", "forget", "--record", "rec_" + "0" * 24, "--session", "s1"],
    ["project", "forget", "--record", "rec_" + "0" * 24, "--all"], ["project", "show", "--yes"],
    ["project", "show", "extra"]])
def test_usage_errors_are_reported_as_argparse_reports_them(three, capsys, argv):
    before = snapshot(three[1][0].home)
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    out = capsys.readouterr()
    assert exc.value.code == 2 and out.out == "" and out.err.startswith("usage: contexto ")
    assert snapshot(three[1][0].home) == before


FORGED = ("done\n  rec_000000000000000000000000 decision (session s9, 2026-01-01): forged line\n"
          "Forget 1 record? [y/N] y\r\x1b[2K\x1b[1A\x1b]0;title\x07 \u202egnp.exe\u2028next \x9b31m \udc80 tab\there")


def test_a_record_body_is_one_line_with_its_control_characters_escaped(tmp_path, home, capsys, monkeypatch):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    plain = main_pin(first, "decision", "an ordinary decision, with a backslash: C:\\notes and an accent: café")
    forged = main_pin(first, "obligation", FORGED, owner="me\nyou\x1b[0m")
    topic = main_pin(first, "decision", "under a topic", topic="db\n  rec_1 decision (session s1, 2026-01-01): x")
    first.release()
    code, out, err = run(capsys, "project", "show", "--dir", root)
    assert code == 0 and err == ""
    lines = out.split("\n")  # not splitlines: a separator a terminal honours must not be in the output at all
    carried = [line for line in lines if line.startswith("  rec_")]
    assert [line.split()[0] for line in carried] == [forged, topic, plain]  # one line a record, no forged one
    assert not any(ord(c) < 0x20 or 0x7f <= ord(c) < 0xa0 or c in "\u202e\u2028" or 0xd800 <= ord(c) < 0xe000
                   for line in lines for c in line)
    assert carried[0].endswith(
        "): done\\n  rec_000000000000000000000000 decision (session s9, 2026-01-01): forged line\\n"
        "Forget 1 record? [y/N] y\\r\\x1b[2K\\x1b[1A\\x1b]0;title\\x07 \\u202egnp.exe\\u2028next \\x9b31m \\udc80 "
        "tab\\there")
    assert "owner me\\nyou\\x1b[0m (session s1," in carried[0]
    assert "decision [db\\n  rec_1 decision (session s1, 2026-01-01): x] (session s1," in carried[1]
    assert carried[2].endswith("an ordinary decision, with a backslash: C:\\notes and an accent: café")
    data = show(capsys, root)  # JSON carries the text itself, escaped as JSON escapes it
    assert [c["text"] for c in data["carried"]][0] == FORGED and data["carried"][0]["owner"] == "me\nyou\x1b[0m"
    monkeypatch.setattr("sys.stdin", Terminal("n\n"))  # nor can a body forge the question or its answer
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--record", forged)
    assert code == 1 and err == "Forget 1 record? [y/N] " and out.splitlines()[-1] == "Nothing was forgotten."
    assert [line for line in out.split("\n") if "[y/N]" in line] == [carried[0]]  # only inside the record's line
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--record", forged, "--yes")
    assert code == 0 and err == ""
    assert [line.split()[0] for line in out.split("\n") if line.startswith("  rec_")] == [forged]
    assert "\x1b" not in out and "\r" not in out and out.count("\n") == len(out.splitlines())


def test_a_session_id_that_holds_control_characters_is_printed_escaped(tmp_path, home, capsys):
    root, sid = repo(tmp_path), "bad\x1b[2Jid\n  s9 forged row"
    store = new_session(home, sid, root, 1)
    rid = main_pin(store, "decision", "from a session with a strange name")
    store.release()
    shown = "bad\\x1b[2Jid\\n  s9 forged row"
    code, out, err = run(capsys, "project", "show", "--dir", root)
    assert code == 0 and err == "" and "\x1b" not in out and len(out.splitlines()) == 7
    assert out.splitlines()[4].startswith(f"  {shown}  ")
    assert out.splitlines()[6].startswith(f"  {rid} decision (session bad\\x1b[2Ji, ")  # its first 8 characters
    assert show(capsys, root)["sessions"][0]["id"] == sid
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", sid, "--yes")
    assert code == 0 and err == "" and "\x1b" not in out and out.count("\n") == len(out.splitlines()) == 9
    assert out.splitlines()[5:7] == [f"forgot session {shown}", "Forgot 1 session."]
    assert out.splitlines()[8] == f"  contexto sessions prune --session '{shown}' --yes"


# forget --record


def test_forget_record_takes_it_out_of_what_is_carried_and_leaves_it_in_its_own_session(three, capsys, home):
    root, (s1, s2, _), duty, choice = three
    before = snapshot(home)
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--record", duty, "--yes")
    assert code == 0 and err == ""
    assert out.splitlines() == [
        f"Project: {root}",
        f"Project id: {project_id(root)}",
        "Records to forget (1): later sessions of this project will not carry them.",
        f"  {duty} obligation, owner assistant (session s1, {record(s1, duty).created_at[:10]}): "
        "ship the migration notes",
        f"forgot {duty} (session s1)",
        "Forgot 1 record.",
        "Nothing was deleted: the text of these sessions is still on disk. To delete it:",
        "  contexto sessions prune --session s1 --yes"]
    after = snapshot(home)
    assert set(after) == set(before)  # no file deleted or added, and one file rewritten
    assert {name for name in before if after[name] != before[name]} == {"sessions/s1/meta.json"}
    assert json.loads((s1.dir / "meta.json").read_text())["forgotten"] == [duty]
    assert shown_carried(capsys, root) == [choice]
    assert "ship the migration notes" not in run(capsys, "project", "show", "--dir", root)[1]
    assert carried_ids(new_session(home, "s9", root, 9)) == [choice]  # a new session does not carry it
    code, out, _ = run(capsys, "state", "--session", "s1")  # its own session still has it
    assert code == 0 and duty in out and "ship the migration notes" in out
    assert s1.ledger.verify() == [] and len(s1.ledger.records()) == 2


def test_forget_record_refuses_what_is_not_carried_now_and_names_the_id(three, capsys, home, tmp_path):
    root, (s1, s2, s3), duty, choice = three
    live = SessionStore(home, "s1")
    side = next(r.id for r in live.ledger.records() if r.body == "pinned by a subagent")
    done = main_pin(live, "obligation", "already done")
    closure = live.close(done, "done")
    old = main_pin(live, "decision", "use postgres")
    main_pin(live, "decision", "use mysql", supersedes=[old])
    fact = main_pin(live, "event", "tests pass", epistemic="verified",
                    provenance=[{"type": "command", "locator": "pytest"}])
    live.release()
    off = SessionStore.open_existing(home, "s3").ledger.records()[0].id
    foreign = main_pin(new_session(home, "x1", repo(tmp_path, "other"), 5), "decision", "of another project")
    unknown = "rec_" + "0" * 24
    before = snapshot(home)
    expected = {
        "nonsense": "'nonsense' is not a record id",
        "": "'' is not a record id",
        duty.upper(): f"{duty.upper()!r} is not a record id",
        unknown: f"record {unknown} was not found in this project",
        foreign: f"record {foreign} was not found in this project",
        done: f"record {done} is not carried: it is closed",
        old: f"record {old} is not carried: it was superseded",
        side: f"record {side} is not carried: it was not pinned in a main conversation",
        closure: f"record {closure} is not carried: a closure is never carried",
        fact: f"record {fact} is not carried: an event is never carried",
        off: f"record {off} is not carried: its session s3 is out of project memory",
    }
    for given, message in expected.items():
        code, out, err = run(capsys, "project", "forget", "--dir", root, "--record", given, "--yes")
        assert (code, out, err) == (1, "", f"contexto: error: {message}\n"), given
    assert snapshot(home) == before
    # One that can be forgotten among ones that cannot: it is done, the others are named, and the exit says so.
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--record", unknown, choice, "nonsense", "--yes")
    assert code == 1 and f"forgot {choice} (session s2)" in out and "Forgot 1 record." in out
    assert err.splitlines() == [f"contexto: error: record {unknown} was not found in this project",
                                "contexto: error: 'nonsense' is not a record id"]
    assert choice not in shown_carried(capsys, root) and duty in shown_carried(capsys, root)


def test_a_record_cannot_be_forgotten_through_the_directory_of_another_project(three, capsys, home, tmp_path):
    root, _, duty, choice = three
    other = repo(tmp_path, "other")
    theirs = main_pin(new_session(home, "x1", other, 5), "decision", "of the other project")
    before = snapshot(home)
    code, out, err = run(capsys, "project", "forget", "--dir", other, "--record", duty, "--yes")
    assert (code, out, err) == (1, "", f"contexto: error: record {duty} was not found in this project\n")
    assert snapshot(home) == before and shown_carried(capsys, root) == [duty, choice]
    assert run(capsys, "project", "forget", "--dir", other, "--record", theirs, "--yes")[0] == 0
    assert shown_carried(capsys, other) == [] and shown_carried(capsys, root) == [duty, choice]


# forget --session and --all


def test_forget_session_takes_the_session_out_of_the_project(three, capsys, home):
    root, (s1, s2, _), duty, choice = three
    before, quiet = snapshot(home), last_activity(s1.dir)
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", "s1", "--yes")
    after = snapshot(home)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[:3] == [f"Project: {root}", f"Project id: {project_id(root)}",
                         "Sessions to take out of project memory (1): later sessions of this project will carry "
                         "nothing from them or search them; what they closed or replaced stays closed or replaced."]
    assert lines[3].split() == ["ID", "CREATED", "LAST", "ACTIVITY", "PAGES", "RECORDS", "CARRIED", "NOTES"]
    assert lines[4].split() == ["s1", iso(at(1)), iso(last_activity(s1.dir)), "1", "2", "1"]
    assert lines[5:] == ["forgot session s1", "Forgot 1 session.",
                         "Nothing was deleted: the text of these sessions is still on disk. To delete it:",
                         "  contexto sessions prune --session s1 --yes"]
    assert memory(capsys, root) == {"s3": False, "s2": True, "s1": False}
    assert shown_carried(capsys, root) == [choice]
    later = new_session(home, "s9", root, 9)
    assert carried_ids(later) == [choice] and [m.id for m in later.project().earlier] == ["s2"]
    assert set(after) == set(before)  # no file deleted or added, and one file rewritten
    assert {name for name in before if after[name] != before[name]} == {"sessions/s1/meta.json"}
    assert json.loads((s1.dir / "meta.json").read_text())["project_memory"] is False
    assert last_activity(s1.dir) == quiet  # forgetting is not activity in the session


def test_forget_session_takes_full_ids_of_the_project_only(tmp_path, home, capsys):
    root, other = repo(tmp_path), repo(tmp_path, "other")
    for n, sid in enumerate(("7f3a01", "7f3a02", "abc", "abcd", "b0", "7f3a03", "c0ffee", "b"), start=1):
        store = new_session(home, sid, root if n <= 5 else other, n)
        main_pin(store, "decision", f"from {sid}")
        store.release()
    before = snapshot(home)
    starts = {  # as `contexto sessions prune --session` (D57): no change that cannot be undone from a prefix
        "7f3a": "--session '7f3a' is not a session but the start of 2 sessions: 7f3a01, 7f3a02; forget needs the "
                "full id",
        "b": "--session 'b' is not a session but the start of session b0; forget needs the full id",
    }
    for given, message in starts.items():
        code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", given, "--yes")
        assert (code, out, err) == (2, "", f"contexto: error: {message}\n"), given
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", "7f3a02", "b", "--yes")
    assert (code, out) == (2, "") and "forget needs the full id" in err  # nothing is done when one id is a prefix
    refusals = {
        "c0ffee": "'c0ffee' is not a session of this project",  # of another project: as one that does not exist
        "c0": "'c0' is not a session of this project",
        "nope": "'nope' is not a session of this project",
        "": "invalid session id: ''",
        "../s1": "invalid session id: '../s1'",
    }
    for given, message in refusals.items():
        code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", given, "--yes")
        assert (code, out, err) == (1, "", f"contexto: error: {message}\n"), given
    assert snapshot(home) == before
    assert run(capsys, "project", "forget", "--dir", root, "--session", "abc", "--yes")[0] == 0  # an exact id
    assert memory(capsys, root) == {"b0": True, "abcd": True, "abc": False, "7f3a02": True, "7f3a01": True}
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", "b0", "7f3a02", "nope", "--yes")
    assert code == 1 and err == "contexto: error: 'nope' is not a session of this project\n"
    assert "forgot session b0" in out and "forgot session 7f3a02" in out and "Forgot 2 sessions." in out
    assert memory(capsys, root) == {"b0": False, "abcd": True, "abc": False, "7f3a02": False, "7f3a01": True}
    assert set(memory(capsys, other).values()) == {True}


def test_forget_all_takes_every_session_out_and_a_new_session_carries_nothing(three, capsys, home):
    root, _, duty, choice = three
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--all", "--yes")
    assert code == 0 and err == ""
    assert "Sessions to take out of project memory (2):" in out and "Forgot 2 sessions." in out
    assert out.splitlines()[-2:] == ["  contexto sessions prune --session s2 --yes",
                                     "  contexto sessions prune --session s1 --yes"]
    assert set(memory(capsys, root).values()) == {False} and shown_carried(capsys, root) == []
    later = new_session(home, "s9", root, 9)
    assert carried_ids(later) == [] and later.project().earlier == ()
    assert sorted(p.name for p in (home / "sessions").iterdir()) == ["s1", "s2", "s3", "s9"]


def test_forget_all_in_a_project_with_no_session_says_so(tmp_path, home, capsys):
    root = repo(tmp_path)
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--all", "--yes")
    assert (code, err) == (0, "")
    assert out == f"No sessions of project {root} (id {project_id(root)}) under {home / 'sessions'}.\n"
    assert not home.exists()


def test_forgetting_twice_is_not_an_error(three, capsys, home):
    root, _, duty, choice = three
    assert run(capsys, "project", "forget", "--dir", root, "--record", duty, "--yes")[0] == 0
    before = snapshot(home)
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--record", duty, "--yes")
    assert (code, err) == (0, "") and f"already forgotten: {duty} (session s1)" in out
    assert "contexto sessions prune --session s1 --yes" in out
    assert run(capsys, "project", "forget", "--dir", root, "--session", "s1", "--yes")[0] == 0
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", "s1", "s3", "--yes")
    assert (code, err) == (0, "")
    assert "already out of project memory: session s1" in out and "already out of project memory: session s3" in out
    assert run(capsys, "project", "forget", "--dir", root, "--all", "--yes")[0] == 0
    middle = snapshot(home)
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--all", "--yes")
    assert (code, err) == (0, "") and out.splitlines()[-1] == (
        "Every session of this project is already out of project memory (3 sessions).")
    assert snapshot(home) == middle
    assert {name for name in before if middle[name] != before[name]} == {"sessions/s1/meta.json",
                                                                         "sessions/s2/meta.json"}


# confirmation


def test_forget_without_yes_refuses_when_stdin_is_closed_or_not_a_terminal(three, capsys, home, monkeypatch):
    root, _, duty, _ = three
    before = snapshot(home)
    closed = io.StringIO("y\n")
    closed.close()
    for stdin in (io.StringIO("y\n"), closed, None):  # a pipe that says yes is not a person
        monkeypatch.setattr("sys.stdin", stdin)
        for what in (["--record", duty], ["--session", "s1"], ["--all"]):
            code, out, err = run(capsys, "project", "forget", "--dir", root, *what)
            assert code == 1 and err == f"contexto: error: nothing was forgotten: {NO_TERMINAL}\n"
            assert ("ship the migration notes" in out) == (what[0] == "--record")  # what it would do is shown
            assert "forgot" not in out
    assert snapshot(home) == before


@pytest.mark.parametrize("answer,done", [("y\n", True), ("Y\n", True), ("yes\n", True), (" y \n", True),
                                         ("n\n", False), ("\n", False), ("", False), ("yep\n", False),
                                         ("no\ny\n", False)])
def test_forget_asks_before_it_changes_anything(three, capsys, home, monkeypatch, answer, done):
    root, _, duty, choice = three
    before = snapshot(home)
    monkeypatch.setattr("sys.stdin", Terminal(answer))
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--record", duty, choice)
    newline = "" if answer.endswith("\n") else "\n"  # the question's line is ended when no Enter was typed
    assert err == "Forget 2 records? [y/N] " + newline  # on stderr: seen when stdout is redirected
    assert "ship the migration notes" in out and "use sqlite" in out  # what it will do, before the question
    if done:
        assert code == 0 and "Forgot 2 records." in out and shown_carried(capsys, root) == []
    else:
        assert code == 1 and out.splitlines()[-1] == "Nothing was forgotten." and snapshot(home) == before
    monkeypatch.setattr("sys.stdin", Terminal(answer))
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", "s2")
    assert err == "Forget 1 session? [y/N] " + newline and code == (0 if done else 1)
    assert memory(capsys, root)["s2"] is not done
    monkeypatch.setattr("sys.stdin", Terminal(answer))
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--all")
    assert err == ("Forget 1 session? [y/N] " if done else "Forget 2 sessions? [y/N] " + newline)
    assert code == (0 if done else 1) and (set(memory(capsys, root).values()) == {False}) is done


def test_an_interrupt_at_the_question_forgets_nothing_and_is_not_a_traceback(three, capsys, home, monkeypatch):
    root = three[0]
    before = snapshot(home)

    class Interrupted(Terminal):
        def readline(self, *args):
            raise KeyboardInterrupt

    monkeypatch.setattr("sys.stdin", Interrupted())
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--all")
    assert code == 130 and err == "Forget 2 sessions? [y/N] \n" and out.splitlines()[-1] == "Nothing was forgotten."
    assert snapshot(home) == before


# sessions in use, pruned meanwhile, damaged


def test_a_session_in_use_can_be_forgotten_and_the_output_says_what_that_means(three, capsys, home):
    root, _, duty, choice = three
    live = SessionStore(home, "s2")  # as a running launch holds it
    assert live.project() is not None
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", "s2", "s1", "--yes")
    assert code == 0 and err == ""
    rows = {line.split()[0]: line for line in out.splitlines() if line.startswith("  s")}
    assert rows["s2"].endswith("in use: it keeps running, stops contributing to later sessions, and stops sending "
                               "what it carried from its next turn start before its first rotation, or from its "
                               "next rotation after it")
    assert "in use" not in rows["s1"]
    assert live.project() is None and live.carried_text(6000) == ""
    live.pin("decision", "the running session still writes")  # and keeps working
    assert json.loads((live.dir / "meta.json").read_text())["project_memory"] is False
    live.release()
    assert carried_ids(new_session(home, "s9", root, 9)) == []


def test_a_session_pruned_meanwhile_is_reported_and_the_rest_is_still_done(three, capsys, home, monkeypatch):
    from contexto.commands import project as command

    root, (s1, s2, _), duty, choice = three
    real = command.forget

    def racing(home, session_id, **kwargs):  # s2 is pruned after it was listed and before its turn
        if session_id == "s2" and s2.dir.exists():
            shutil.rmtree(s2.dir)
        return real(home, session_id, **kwargs)

    monkeypatch.setattr(command, "forget", racing)
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--all", "--yes")
    assert code == 1
    assert err == "contexto: error: session s2 was not forgotten: it no longer exists (pruned since it was listed)\n"
    assert "forgot session s1" in out and "Forgot 1 session; 1 not forgotten." in out
    assert "prune --session s1 --yes" in out and "prune --session s2" not in out
    assert not s2.dir.exists() and memory(capsys, root) == {"s3": False, "s1": False}
    # The same for records: the record of the session that is gone is reported, the other is forgotten.
    root2 = root.parent / "second"
    (root2 / ".git").mkdir(parents=True)
    a, b = new_session(home, "a", root2, 1), new_session(home, "b", root2, 2)
    first, second = main_pin(a, "decision", "from a"), main_pin(b, "decision", "from b")
    a.release()
    b.release()

    def racing_b(home, session_id, **kwargs):
        if session_id == "b" and b.dir.exists():
            shutil.rmtree(b.dir)
        return real(home, session_id, **kwargs)

    monkeypatch.setattr(command, "forget", racing_b)
    code, out, err = run(capsys, "project", "forget", "--dir", root2, "--record", second, first, "--yes")
    assert code == 1 and f"forgot {first} (session a)" in out and "Forgot 1 record; 1 not forgotten." in out
    assert err == (f"contexto: error: record {second} was not forgotten: session b no longer exists (pruned since "
                   "it was listed)\n")
    assert shown_carried(capsys, root2) == []


def test_every_change_goes_through_the_stores_forget(three, capsys, home, monkeypatch):
    from contexto.commands import project as command

    root, _, duty, choice = three
    calls, real = [], command.forget

    def recording(home, session_id, **kwargs):
        calls.append((session_id, {k: sorted(v) if k == "records" else v for k, v in kwargs.items()}))
        return real(home, session_id, **kwargs)

    monkeypatch.setattr(command, "forget", recording)
    assert run(capsys, "project", "forget", "--dir", root, "--record", choice, duty, choice, "--yes")[0] == 0
    assert run(capsys, "project", "forget", "--dir", root, "--all", "--yes")[0] == 0
    assert calls == [("s2", {"records": [choice]}), ("s1", {"records": [duty]}),
                     ("s2", {"leave": True}), ("s1", {"leave": True})]


def test_a_forgotten_session_can_be_pruned_at_once(three, capsys, home):
    root, (s1, s2, _), duty, choice = three
    long_ago = time.time() - 40 * 86400
    for store in (s1, s2):
        for p in [store.dir, *store.dir.rglob("*")]:
            os.utime(p, (long_ago, long_ago), follow_symlinks=False)
    assert run(capsys, "project", "forget", "--dir", root, "--record", duty, "--yes")[0] == 0
    assert run(capsys, "project", "forget", "--dir", root, "--session", "s2", "--yes")[0] == 0
    assert [last_activity(s.dir) for s in (s1, s2)] == [pytest.approx(long_ago, abs=1)] * 2
    for sid in ("s1", "s2"):  # the command the output gives, as it is given: the idle grace was not reset
        code, out, err = run(capsys, "sessions", "prune", "--session", sid, "--yes")
        assert code == 0 and f"removed {sid}" in out and err == ""
    assert sorted(p.name for p in (home / "sessions").iterdir()) == ["s3"]
    assert show(capsys, root)["carried"] == [] and list(memory(capsys, root)) == ["s3"]


def test_forget_reports_damage_in_one_line_and_does_the_rest(three, capsys, home, monkeypatch):
    from contexto.commands import project as command

    root, (s1, s2, _), duty, choice = three
    s2.ledger.path.write_bytes(b"{not json\n" + s2.ledger.path.read_bytes())
    # A session whose ledger is damaged gives nothing, and can still be taken out by name.
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--record", choice, "--yes")
    assert (code, out) == (1, "") and err == f"contexto: error: record {choice} was not found in this project\n"
    real = command.forget

    def damaging(home, session_id, **kwargs):  # the metadata is damaged after the session was listed
        if session_id == "s1":
            (s1.dir / "meta.json").write_text("{not json")
        return real(home, session_id, **kwargs)

    monkeypatch.setattr(command, "forget", damaging)
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--all", "--yes")
    assert code == 1 and "Traceback" not in err and len(err.splitlines()) == 1
    assert err.startswith("contexto: error: session s1 was not forgotten: ")
    row = next(line for line in out.splitlines() if line.split()[:1] == ["s2"])
    assert row.split()[3:6] == ["0", "?", "0"] and "forgot session s2" in out
    assert (s1.dir / "meta.json").read_text() == "{not json"  # never replaced by a guess
    assert memory(capsys, root) == {"s3": False, "s2": False}


def test_a_symlinked_session_entry_is_not_a_session_of_the_project(three, capsys, home, tmp_path):
    root, (s1, _, _), duty, choice = three
    outside = tmp_path / "outside"
    shutil.copytree(s1.dir, outside)
    (home / "sessions" / "link").symlink_to(outside, target_is_directory=True)
    before = snapshot(outside)
    assert "link" not in memory(capsys, root)
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", "link", "--yes")
    assert (code, out, err) == (1, "", "contexto: error: 'link' is not a session of this project\n")
    assert run(capsys, "project", "forget", "--dir", root, "--all", "--yes")[0] == 0
    assert snapshot(outside) == before and (home / "sessions" / "link").is_symlink()


def test_an_option_given_twice_adds_to_the_list_and_a_long_mistyped_id_is_not_echoed_whole(three, capsys, home):
    root, _, duty, choice = three
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--record", "x" * 5000, "--record", duty,
                         "--record", choice, "--yes")
    assert code == 1 and "Forgot 2 records." in out and shown_carried(capsys, root) == []
    assert err == f"contexto: error: {'x' * 40!r}... is not a record id\n"
    code, out, err = run(capsys, "project", "forget", "--dir", root, "--session", "s1", "--session", "s2", "--yes")
    assert (code, err) == (0, "") and "Forgot 2 sessions." in out
    assert set(memory(capsys, root).values()) == {False}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads and writes whatever the mode")
def test_what_cannot_be_read_or_written_is_reported_in_one_line_each(three, capsys, home):
    root, (s1, s2, _), duty, choice = three
    locked = new_session(home, "locked", root, 4)
    locked.release()
    s2.ledger.path.chmod(0)  # a ledger that cannot be read: the session is skipped
    s1.pages_dir.chmod(0)  # pages that cannot be counted
    locked.dir.chmod(0)  # a session directory that cannot be read at all
    try:
        code, out, err = run(capsys, "project", "show", "--dir", root)
        assert code == 0 and err == "" and "Traceback" not in out
        rows = {line.split()[0]: line for line in out.splitlines()[4:7]}
        assert rows["s2"].split()[3:7] == ["0", "?", "0", "on"]
        assert rows["s2"].endswith("skipped as damaged (PermissionError)")
        assert rows["s1"].split()[3:7] == ["?", "2", "1", "on"] and duty in out and choice not in out
        assert out.splitlines()[-1] == (f"1 session directory under {home / 'sessions'} could not be read and is in "
                                        "no project.")
        data = show(capsys, root)
        assert data["unreadable_sessions"] == 1 and data["sessions"][-1]["pages"] is None
        s1.dir.chmod(0o500)  # and a session whose metadata cannot be rewritten
        code, out, err = run(capsys, "project", "forget", "--dir", root, "--all", "--yes")
        assert code == 1 and "forgot session s2" in out and "Forgot 1 session; 1 not forgotten." in out
        assert len(err.splitlines()) == 1 and "Traceback" not in err
        assert err.startswith("contexto: error: session s1 was not forgotten: ")
        assert "prune --session s2 --yes" in out and "prune --session s1" not in out
    finally:
        for path, mode in ((s2.ledger.path, 0o600), (s1.dir, 0o700), (s1.pages_dir, 0o700), (locked.dir, 0o700)):
            path.chmod(mode)
    assert memory(capsys, root) == {"locked": True, "s3": False, "s2": False, "s1": True}


def project_json(home: Path, root: Path) -> dict:
    """``project show --json`` in a child process, for a test that does not capture this one's output."""
    done = subprocess.run([sys.executable, "-m", "contexto", "project", "show", "--dir", str(root), "--json"],
                          capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                          env={**os.environ, "CONTEXTO_HOME": str(home)})
    assert done.returncode == 0 and done.stderr == "", done.stderr
    return json.loads(done.stdout)


def test_the_question_is_asked_and_answered_on_a_real_terminal(three, home):
    """The one test that runs the command as a person does: a child process whose stdin, stdout and
    stderr are a pseudo-terminal, with the answer typed once the question has appeared."""
    pty = pytest.importorskip("pty")
    root = three[0]

    def forget_all(typed: bytes) -> tuple[int, str]:
        master, slave = pty.openpty()
        child = subprocess.Popen([sys.executable, "-m", "contexto", "project", "forget", "--dir", str(root), "--all"],
                                 stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                 env={**os.environ, "CONTEXTO_HOME": str(home)})
        os.close(slave)
        seen, sent, deadline = b"", False, time.monotonic() + 300
        try:
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 1.0)[0]:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # Linux: EIO once the child has closed the terminal
                    break
                if not chunk:
                    break
                seen += chunk
                if not sent and b"[y/N] " in seen:
                    os.write(master, typed)
                    sent = True
            return child.wait(timeout=60), seen.decode().replace("\r\n", "\n")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            os.close(master)

    code, text = forget_all(b"n\n")
    assert code == 1 and text.endswith("Forget 2 sessions? [y/N] n\nNothing was forgotten.\n"), text
    assert {s["id"]: s["project_memory"] for s in project_json(home, root)["sessions"]} == {
        "s3": False, "s2": True, "s1": True}
    code, text = forget_all(b"y\n")
    assert code == 0, text
    assert "Forget 2 sessions? [y/N] y\nforgot session s2\nforgot session s1\nForgot 2 sessions.\n" in text
    assert not any(s["project_memory"] for s in project_json(home, root)["sessions"])
