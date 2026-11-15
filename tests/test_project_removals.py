"""Project memory: removals are project-wide (D69). A closure, or a record's ``supersedes``, made in any
session of the project that still exists takes effect for every session of it: one made in an earlier
session, in a later one (created after the reader, running at the same time or resumed later), and in
one whose project memory is off or that ``contexto project forget --session`` took out. Only additions
are frozen to the reader's earlier sessions. Pruning the session that holds a removal is the one way a
closed or replaced record comes back. Supersession across sessions does not depend on the clocks, and a
conflict on a topic that involves a carried decision is shown.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

import pytest

from contexto import cli
from contexto.store import SessionStore
from contexto.store.model import utc_timestamp
from contexto.store.pinned import CARRIED_HEADING, READABLE_CARRIED_HEADING
from contexto.store.project import forget
from contexto.store.recall import DEFAULT_LINES
from test_project import append, at, carried_ids, confirm, main_close, main_pin, new_session, repo


@pytest.fixture
def home(tmp_path):
    return tmp_path / "contexto-home"


def carried_bodies(store) -> list[str]:
    return [c.record.body for c in store.carried()]


# Findings 1 and 2: removals made in any session of the project


def test_a_closure_in_a_later_session_running_at_once_takes_the_record_from_the_earlier_one(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    duty = main_pin(first, "obligation", "keep the changelog current")
    second, third = new_session(home, "s2", root, 2), new_session(home, "s3", root, 3)
    assert carried_ids(second) == [duty] and carried_ids(third) == [duty]
    main_close(third, duty, "done in the third session")
    assert carried_ids(second) == [] and "keep the changelog current" not in second.pinned_text(6000)
    assert "keep the changelog current" not in second.state_text()
    with pytest.raises(ValueError, match="not an open obligation"):  # as for an obligation it closed itself
        second.close(duty, "done again")
    assert second.ledger.records() == []
    second.release()
    resumed = SessionStore(home, "s2")  # `contexto run --session s2`, after the third session closed it
    assert resumed.carried_text(6000) == "" and duty not in resumed.pinned_text(6000)
    # The session that pinned it sees it closed as well: its own state takes the project's removals.
    assert duty not in first.pinned_text(6000) and duty not in first.state_text()
    with pytest.raises(ValueError, match="not an open obligation"):
        first.close(duty, "and again")
    assert [r.id for r in first.ledger.records()] == [duty]


def test_a_supersession_in_a_later_session_takes_the_decision_from_the_earlier_one(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    old = main_pin(first, "decision", "use postgres", topic="db")
    second, third = new_session(home, "s2", root, 2), new_session(home, "s3", root, 3)
    new = main_pin(third, "decision", "use sqlite", topic="db", supersedes=[old])
    assert carried_ids(second) == []  # the old one is replaced, and the new one is no addition of an earlier session
    assert "use postgres" not in second.pinned_text(6000) and "use sqlite" not in second.pinned_text(6000)
    with pytest.raises(ValueError, match="supersedes unknown record ids"):
        second.pin("decision", "use mysql", supersedes=[old])
    assert "use postgres" not in first.pinned_text(6000)
    assert carried_ids(new_session(home, "s4", root, 4)) == [new]


@pytest.mark.parametrize("how", ["forget --session", "resumed with --no-project-memory"])
def test_a_closure_stays_when_its_session_leaves_project_memory(tmp_path, home, how):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    duty = main_pin(first, "obligation", "run the migration tests before merging")
    choice = main_pin(first, "decision", "the codename is Obsidian", topic="name")
    first.release()
    second = new_session(home, "s2", root, 2)
    main_close(second, duty, "done: tests ran")
    replacement = main_pin(second, "decision", "the codename is Basalt", topic="name", supersedes=[choice])
    second.release()
    if how == "forget --session":
        forget(home, "s2", leave=True)
    else:
        SessionStore(home, "s2").set_project_memory(False)
    fourth = new_session(home, "s4", root, 4)
    assert carried_ids(fourth) == []  # neither the closed nor the replaced record, nor the left session's own
    assert duty not in fourth.pinned_text(6000) and replacement not in fourth.pinned_text(6000)


def test_a_session_with_project_memory_off_closes_nothing_carried_and_gives_nothing(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    duty = main_pin(first, "obligation", "ship the notes")
    off = new_session(home, "s2", root, 2, memory=False)
    assert off.carried() == () and off.carried_text(6000) == ""
    with pytest.raises(ValueError, match="not an open obligation"):
        off.close(duty, "it carries none, so it cannot close one")
    assert off.ledger.records() == []
    own = main_pin(off, "decision", "kept to itself")
    assert carried_ids(new_session(home, "s3", root, 3)) == [duty]
    assert own not in new_session(home, "s4", root, 4).pinned_text(6000)


def test_pruning_the_session_that_closed_a_record_reopens_it_for_later_sessions(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    duty = main_pin(first, "obligation", "ship the notes")
    second, third = new_session(home, "s2", root, 2), new_session(home, "s3", root, 3)
    main_close(second, duty, "shipped")
    assert carried_ids(third) == []
    second.release()
    shutil.rmtree(second.dir)  # as `contexto sessions prune --session s2 --yes` leaves it
    assert carried_ids(third) == [duty] and carried_ids(new_session(home, "s5", root, 5)) == [duty]


def test_a_damaged_later_session_is_skipped_like_a_damaged_earlier_one(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    duty = main_pin(first, "obligation", "ship the notes")
    second, third = new_session(home, "s2", root, 2), new_session(home, "s3", root, 3)
    main_close(third, duty, "shipped")
    third.pin("decision", "a second line, so the first is above the tail")
    third.release()
    lines = third.ledger.path.read_bytes().split(b"\n")
    third.ledger.path.write_bytes(b"\n".join([b"{not json", *lines[1:]]))
    view = second.project()
    assert [c.record.id for c in view.carried()] == [duty]  # its closure no longer counts
    assert [s.session for s in view.skipped] == ["s3"] and view.read == 1


# Finding 7: supersession across sessions does not depend on the clocks


def test_a_supersession_across_sessions_counts_whatever_the_clocks_say(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    old = append(first, "decision", "codename Obsidian", 1.5, topic="name")
    second = new_session(home, "s2", root, 2)
    # The second session's clock is behind the first's: its replacement is stamped before the decision.
    new = second.ledger.append({"kind": "decision", "epistemic": "reported", "author": "model",
                                "body": "codename Basalt", "provenance": [], "session": "s2", "topic": "name",
                                "supersedes": [old], "created_at": utc_timestamp(datetime.fromtimestamp(at(1.2), UTC))},
                               known=[c.record for c in second.carried()]).id
    confirm(second, new)
    third = new_session(home, "s3", root, 3)
    assert carried_ids(third) == [new]
    assert [r.id for r in third.project().state().active] == [new]
    assert carried_ids(second) == [] and "Obsidian" not in first.pinned_text(6000)


# Finding 8: a conflict that involves a carried decision is shown


def test_a_conflict_between_carried_decisions_is_marked_and_listed(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    a = main_pin(first, "decision", "use postgres", topic="db")
    second = new_session(home, "s2", root, 2)
    b = main_pin(second, "decision", "use sqlite", topic="db")
    third = new_session(home, "s3", root, 3)
    pinned = third.pinned_text(6000).split("\n")
    assert [line for line in pinned if line.startswith("- ")] == [
        f'- {b} decision ["db"] CONFLICT (session s2, {date(second, b)}): "use sqlite"',
        f'- {a} decision ["db"] CONFLICT (session s1, {date(first, a)}): "use postgres"']
    assert third.carried_text(6000).split("\n")[2:] == [line for line in pinned if line.startswith("- ")]
    state = third.state_text().split("\n")[len(DEFAULT_LINES):]
    assert state[0] == f"Conflict on topic db: {a}, {b}" and state[1] == CARRIED_HEADING
    own = third.pin("decision", "use mysql", topic="db")
    state = third.state_text().split("\n")[len(DEFAULT_LINES):]
    assert state[:3] == ["Active decisions:", f"- {own} decision [db] CONFLICT: use mysql",
                         f"Conflict on topic db: {a}, {b}, {own}"]
    assert all(" CONFLICT" in line for line in state if line.startswith("- "))


def test_a_conflict_with_a_carried_decision_is_listed_by_contexto_state(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    carried = main_pin(first, "decision", "use postgres", topic="db")
    second = new_session(home, "s2", root, 2)
    own = second.pin("decision", "use sqlite", topic="db")
    assert cli.main(["state", "--session", "s2"]) == 0
    out = capsys.readouterr().out.split("\n")
    assert out[:5] == ["Active decisions:", f"- {own} decision [db] CONFLICT: use sqlite",
                       f"Conflict on topic db: {carried}, {own}", READABLE_CARRIED_HEADING, "Active decisions:"]
    assert out[5] == f"- {carried} decision [db] CONFLICT (session s1, {date(first, carried)}): use postgres"


def test_a_decision_on_another_topic_or_of_a_later_session_makes_no_conflict(tmp_path, home):
    root = repo(tmp_path)
    first = new_session(home, "s1", root, 1)
    main_pin(first, "decision", "use postgres", topic="db")
    second, third = new_session(home, "s2", root, 2), new_session(home, "s3", root, 3)
    main_pin(third, "decision", "use sqlite", topic="db")  # not carried by the second: not shown to it
    second.pin("decision", "two spaces", topic="indent")
    assert "CONFLICT" not in second.state_text() and "Conflict" not in second.state_text()


def date(store, record_id: str) -> str:
    record = next(r for r in store.ledger.records() if r.id == record_id)
    return record.created_at[:10]
