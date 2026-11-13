"""``contexto_recall`` with ``session`` (D69, design section 7): a page of an earlier session of the same
project, read without changing that session, under the same limits as a page of the current one.

The sessions and the server are built as in ``tests/test_project_search.py``.
"""

from __future__ import annotations

import math
import os
import shutil

import pytest

from contexto import cli, mcp
from contexto.store import SessionStore
from contexto.store import project as project_module
from contexto.store.render import recall_limit, render_page
from test_project_search import (
    RATIO,
    at,
    call,
    damage,
    date,
    earlier,
    error_of,
    events,
    new_session,
    pages,
    repo,
    serve,
    session_lines,
    snapshot,
    text_of,
)

NOT_EARLIER = "is not an earlier session of this project"
LONG = "\n".join(f"line {i} of a long turn about the migration" for i in range(2_500))  # about 110,000 characters


@pytest.fixture
def home(tmp_path):
    return tmp_path / "contexto-home"


@pytest.fixture
def root(tmp_path):
    return repo(tmp_path)


@pytest.fixture
def two(home, root):
    """An ended session with two pages, and the current one with one."""
    old, old_ids = earlier(home, "e1aaaaaa" + "0" * 24, root, 1, "the deploy key was amber-17 back then",
                           "lunch plans")
    cur = new_session(home, "c3cccccc" + "0" * 24, root, 3)
    cur_ids = pages(cur, "the deploy key is now cedar-31")
    return {"old": old, "old_ids": old_ids, "cur": cur, "cur_ids": cur_ids}


def recall(server, page_id, **arguments) -> dict:
    return call(server, "contexto_recall", {"page_id": page_id, **arguments})


# Which session


def test_recall_reads_an_earlier_sessions_page_by_its_label_its_id_or_a_unique_prefix(two):
    old, cur, (page_id, _) = two["old"], two["cur"], two["old_ids"]
    server = serve(cur)
    expected = render_page(old.get_page(page_id))
    assert "amber-17" in expected
    for name in ("e1aaaaaa", old.session_id, "e1", "e1aaaaaa00"):
        assert text_of(recall(server, page_id, session=name)) == expected, name
    event = events(cur)[-1]
    assert event["ok"] and event["earlier"] is True and event["chars"] == event["total_chars"] == len(expected)
    assert event["returned"] == [{"source": "earlier", "page_id": page_id, "start": 0, "end": len(expected)}]
    assert "e1aaaaaa" not in (cur.dir / "log.jsonl").read_text()  # never an id of another session
    # Without the argument the page is looked for in the current session only.
    assert error_of(recall(server, page_id)).startswith(f"Unknown page id: {page_id}.")
    assert events(cur)[-1]["ok"] is False


def test_a_project_scope_search_gives_what_recall_takes(two):
    cur, old = two["cur"], two["old"]
    server = serve(cur)
    lines = text_of(call(server, "contexto_search", {"query": "amber-17", "scope": "project"})).splitlines()
    start = lines.index(session_lines("\n".join(lines))[0])
    label = lines[start].removeprefix("[session ").split(",")[0]
    page_id = lines[start + 1].split()[0]
    offset, shown = next(line for line in lines[start + 2:] if line.startswith(">"))[2:].split(": ", 1)
    assert label == "e1aaaaaa" and lines[start] == f"[session e1aaaaaa, {date(1)}:]"
    text = text_of(recall(server, page_id, session=label, offset=int(offset)))
    assert text.startswith(shown) and text == render_page(old.get_page(page_id))[int(offset):]


def test_the_current_sessions_own_id_means_the_current_session(two):
    cur, (page_id,) = two["cur"], two["cur_ids"]
    server = serve(cur)
    assert recall(server, page_id, session=cur.session_id) == recall(server, page_id)
    assert [e["earlier"] for e in events(cur)[-2:]] == [False, False]
    assert events(cur)[-1]["returned"][0]["source"] == "paged"
    # Only the whole id: a prefix of it names no earlier session.
    assert NOT_EARLIER in error_of(recall(server, page_id, session=cur.session_id[:8]))
    # And an earlier session's page is not found under the current session's id.
    assert error_of(recall(server, two["old_ids"][0], session=cur.session_id)).startswith("Unknown page id")


def build_project(home, tmp_path):
    """One project with two earlier sessions whose ids share a start, a later one and one with memory
    off, and another project beside it. Returns the current session and every other session's store."""
    one, two = repo(tmp_path, "app"), repo(tmp_path, "app2")
    others = {
        "first": earlier(home, "ab120000", one, 1, "needle of the first")[0],
        "second": earlier(home, "ab130000", one, 2, "needle of the second")[0],
        "off": earlier(home, "off00000", one, 3, "needle with memory off", memory=False)[0],
        "foreign": earlier(home, "zz000000", two, 4, "needle of another project")[0],
    }
    cur = new_session(home, "cur00000", one, 5)
    others["later"] = earlier(home, "late0000", one, 6, "needle from a later session")[0]
    return cur, others


@pytest.mark.parametrize("value", [
    "zz000000", "zz", "late0000", "off00000", "nope", "ab", "ab1", "cur", "AB120000", " ab120000", "ab120000 ",
    "../ab120000", "ab120000/", "ab120000/pages", "sessions/ab120000", "./ab120000", ".", "..", "/", "/etc/passwd",
    "~", "HOME/sessions/ab120000", "HOME/sessions/zz000000", "ab120000\x00", "ab12*", "x" * 5_000])
def test_anything_that_is_not_an_earlier_session_of_the_project_is_a_tool_error_that_opens_nothing(
        home, tmp_path, monkeypatch, value):
    cur, others = build_project(home, tmp_path)
    value = value.replace("HOME", str(home))
    page_ids = {name: store.page_ids()[0] for name, store in others.items()}
    before = {name: snapshot(store.dir) for name, store in others.items()}
    server = serve(cur)

    def read_page(pages_dir, page_id):
        raise AssertionError("a page was opened for a session that is not an earlier session of the project")

    monkeypatch.setattr(project_module, "read_page", read_page)
    for page_id in page_ids.values():
        message = error_of(recall(server, page_id, session=value))
        assert NOT_EARLIER in message or "is the start of 2 earlier sessions of this project" in message
        assert str(home) not in message and str(tmp_path) not in message and "/" not in message
        assert "needle" not in message
        assert events(cur)[-1]["ok"] is False
    assert ("is the start of 2" in message) == (value in ("ab", "ab1"))
    assert {name: snapshot(store.dir) for name, store in others.items()} == before
    assert value not in (cur.dir / "log.jsonl").read_text()


@pytest.mark.parametrize("value", ["", 5, None, True, ["ab120000"], {"id": "ab120000"}])
def test_a_session_that_is_not_a_string_is_a_tool_error(home, tmp_path, value):
    cur, others = build_project(home, tmp_path)
    message = error_of(recall(serve(cur), others["first"].page_ids()[0], session=value))
    assert message == "session must be a non-empty string"


def test_a_session_id_that_starts_a_session_of_another_project_reads_only_its_own(home, tmp_path):
    one, two = repo(tmp_path, "app"), repo(tmp_path, "app2")
    mine, (mine_id,) = earlier(home, "abc00000", one, 1, "needle of this project")
    theirs, (their_id,) = earlier(home, "abc00000ffff", two, 2, "needle of the other project")
    earlier(home, "abd00000", two, 3, "another needle of the other project")
    cur = new_session(home, "cur00000", one, 5)
    before = snapshot(theirs.dir)
    server = serve(cur)
    for name in ("abc00000", "abc", "a"):  # unique among this project's earlier sessions, whatever the other holds
        assert "this project" in text_of(recall(server, mine_id, session=name))
        assert error_of(recall(server, their_id, session=name)).startswith("Unknown page id")
    for name in ("abc00000f", "abc00000ffff", "abd", "abd00000"):
        assert NOT_EARLIER in error_of(recall(server, their_id, session=name))
        assert NOT_EARLIER in error_of(recall(server, mine_id, session=name))
    assert snapshot(theirs.dir) == before


def test_an_earlier_session_with_the_same_page_id_as_the_current_one(home, root):
    cur = new_session(home, "cur00000", root, 5)
    (page_id,) = pages(cur, "the text both sessions hold")
    shutil.copytree(cur.dir, home / "sessions" / "copy0000")
    os.utime(home / "sessions" / "copy0000" / "page.key", (at(1), at(1)))
    damage(cur, page_id, (cur.pages_dir / f"{page_id}.json").read_text().replace("both sessions", "the copy does not"))
    server = serve(cur)
    assert "the copy does not" in text_of(recall(server, page_id))
    assert "both sessions hold" in text_of(recall(server, page_id, session="copy0000"))
    assert [e["returned"][0]["source"] for e in events(cur)[-2:]] == ["paged", "earlier"]


# Offsets, the per-call limit, the half-room rule, the hint


def test_offsets_the_limit_and_the_hint_work_as_for_the_current_session(home, root):
    old, (page_id,) = earlier(home, "e1aaaaaa", root, 1, LONG)
    cur = new_session(home, "cur00000", root, 2)
    (own_id,) = pages(cur, LONG)
    cur.set_window(200_000, 2.0)
    limit, note = recall_limit(*cur.window())
    full = render_page(old.get_page(page_id))
    assert len(full) > 3 * limit and full == render_page(cur.get_page(own_id))
    server = serve(cur)
    got, offset = "", 0
    while True:
        blocks = [b["text"] for b in recall(server, page_id, session="e1aaaaaa", offset=offset)["content"]]
        own = [b["text"] for b in recall(server, own_id, offset=offset)["content"]]
        got += blocks[0]
        assert blocks[0] == own[0] and len(blocks[0]) <= limit  # the same chunks as the current session's page
        offset += len(blocks[0])
        if len(blocks) == 1:
            break
        rest = len(full) - offset
        assert own[1] == (f"[{rest} more characters: call contexto_recall with page_id {own_id} and offset "
                          f"{offset}; {note}]")
        assert blocks[1] == (f"[{rest} more characters: call contexto_recall with page_id {page_id}, session "
                             f"e1aaaaaa and offset {offset}; {note}]")  # the hint repeats the session
    assert got == full and offset == len(full)
    assert text_of(recall(server, page_id, session="e1aaaaaa", offset=len(full))) == ""


def test_a_recall_offset_past_the_end_of_an_earlier_sessions_page_is_a_tool_error(two):
    old, cur, (page_id, _) = two["old"], two["cur"], two["old_ids"]
    total = len(render_page(old.get_page(page_id)))
    server = serve(cur)
    assert error_of(recall(server, page_id, session="e1aaaaaa", offset=total + 1)) == \
        f"offset {total + 1} is past the end of the page ({total} characters)"
    assert error_of(recall(server, page_id, session="e1aaaaaa", offset=-1)) == "offset must be a non-negative integer"
    assert error_of(recall(server, page_id, session="e1aaaaaa", bogus=1)) == "unexpected argument(s): bogus"


def test_an_earlier_sessions_page_is_charged_and_held_to_half_the_room(home, root):
    old, (page_id,) = earlier(home, "e1aaaaaa", root, 1, LONG)
    cur = new_session(home, "cur00000", root, 2)
    cur.set_window(200_000, 2.0)
    full = render_page(old.get_page(page_id))
    server = serve(cur)
    cur.set_turn_room("1", 2_000)
    blocks = [b["text"] for b in recall(server, page_id, session="e1aaaaaa")["content"]]
    chars = sum(map(len, blocks))
    event = events(cur)[-1]
    assert chars <= (2_000 // 2) * RATIO and blocks[0] == full[:len(blocks[0])]
    assert "session e1aaaaaa and offset " + str(len(blocks[0])) in blocks[1]
    assert event["charged_tokens"] == math.ceil(chars / RATIO) == cur.turn_room()[2] <= 1_000
    assert event["turn_budget"] == "charged" and event["earlier"] is True
    # The second call gets half of what the first left.
    left = event["room_left"]
    blocks = [b["text"] for b in recall(server, page_id, session="e1aaaaaa", offset=len(blocks[0]))["content"]]
    assert sum(map(len, blocks)) <= (left // 2) * RATIO
    # Too little room for a recall, then none.
    cur.set_turn_room("1", 100)
    assert text_of(recall(server, page_id, session="e1aaaaaa")).startswith(
        "The recall budget for this turn is nearly used up (about 100 tokens left)")
    assert events(cur)[-1]["charged_tokens"] == 0 and events(cur)[-1]["returned"] == []
    cur.set_turn_room("1", 0)
    assert text_of(recall(server, page_id, session="e1aaaaaa")) == mcp.BUDGET_SPENT
    assert cur.turn_room() == ("1", 0, 0)


# Damage and pruning; nothing is written to the earlier session


def test_a_damaged_page_of_an_earlier_session_is_a_clean_tool_error_and_nothing_is_written(two):
    old, cur, (page_id, other_id) = two["old"], two["cur"], two["old_ids"]
    damage(old, page_id)
    before = snapshot(old.dir)
    server = serve(cur)
    for _ in range(2):
        assert error_of(recall(server, page_id, session="e1aaaaaa")) == \
            f"Page {page_id} of session e1aaaaaa cannot be read: its stored file is damaged."
    assert "lunch plans" in text_of(recall(server, other_id, session="e1aaaaaa"))
    assert snapshot(old.dir) == before and not (old.dir / "log.jsonl").exists()
    assert not events(cur, "page_unreadable")
    assert [e["ok"] for e in events(cur)[-3:]] == [False, False, True]


def test_a_page_or_a_session_that_is_gone_is_an_error_not_a_failure(two, home):
    old, cur, (page_id, other_id) = two["old"], two["cur"], two["old_ids"]
    server = serve(cur)
    assert error_of(recall(server, two["cur_ids"][0], session="e1aaaaaa")).startswith(
        f"Unknown page id: {two['cur_ids'][0]} in session e1aaaaaa.")  # a page of the current session is not there
    # No path is built from a page id that is not one: these name files that exist, and none is opened.
    for bad in ("pg_0000000000000000", "../meta", "../page.key", f"../../{cur.session_id}/pages/{two['cur_ids'][0]}",
                f"{page_id}.json", f"./{page_id}", page_id.upper(), page_id + "\n"):
        assert error_of(recall(server, bad, session="e1aaaaaa")).startswith("Unknown page id: "), bad
    assert error_of(recall(server, "", session="e1aaaaaa")) == "page_id is required and must be a non-empty string"
    os.unlink(old.pages_dir / f"{page_id}.json")  # a prune of the page
    assert error_of(recall(server, page_id, session="e1aaaaaa")).startswith(f"Unknown page id: {page_id} in session")
    elsewhere = home / "elsewhere"
    os.rename(old.pages_dir, elsewhere)
    os.symlink(elsewhere, old.pages_dir)  # a pages directory that is a link is never read through
    message = error_of(recall(server, other_id, session="e1aaaaaa"))
    assert message == f"Page {other_id} of session e1aaaaaa cannot be read: reading its stored file failed."
    shutil.rmtree(old.dir)  # a prune of the session
    assert NOT_EARLIER in error_of(recall(server, other_id, session="e1aaaaaa"))


# The command


def test_contexto_recall_from_prints_an_earlier_sessions_page_read_only(two, home, monkeypatch, capsys):
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    old, cur, (page_id, other_id) = two["old"], two["cur"], two["old_ids"]
    cur.release()
    full = render_page(old.get_page(page_id))
    damage(old, other_id)
    before = snapshot(old.dir), snapshot(cur.dir)
    assert cli.main(["recall", page_id, "--session", cur.session_id, "--from", "e1aaaaaa"]) == 0
    captured = capsys.readouterr()
    assert captured.out == full + "\n"
    assert f"earlier session {old.session_id} (e1aaaaaa, {date(1)})" in captured.err
    assert cli.main(["recall", page_id, "--session", cur.session_id, "--from", old.session_id, "--offset", "20"]) == 0
    assert capsys.readouterr().out == full[20:] + "\n"
    for argv, said in (
            (["recall", page_id, "--session", cur.session_id, "--from", "nope"], NOT_EARLIER),
            (["recall", page_id, "--session", cur.session_id, "--from", "../" + old.session_id], NOT_EARLIER),
            (["recall", page_id, "--session", old.session_id, "--from", cur.session_id], NOT_EARLIER),  # a later one
            (["recall", two["cur_ids"][0], "--session", cur.session_id, "--from", "e1aaaaaa"], "no page"),
            (["recall", other_id, "--session", cur.session_id, "--from", "e1aaaaaa"], "its stored file is damaged"),
            (["recall", page_id, "--session", cur.session_id, "--from", "e1aaaaaa", "--offset", "99999"],
             "past the end")):
        assert cli.main(argv) == 1, argv
        captured = capsys.readouterr()
        assert captured.out == "" and said in captured.err, argv
    assert (snapshot(old.dir), snapshot(cur.dir)) == before  # the damaged page was not logged in either
    # The current session's own id is the current session, as for the tool.
    assert cli.main(["recall", two["cur_ids"][0], "--session", cur.session_id, "--from", cur.session_id]) == 0
    assert "cedar-31" in capsys.readouterr().out
    lone = SessionStore(home, "lone")
    lone.release()
    assert cli.main(["recall", page_id, "--session", "lone", "--from", "e1aaaaaa"]) == 1
    assert "not in a project" in capsys.readouterr().err
