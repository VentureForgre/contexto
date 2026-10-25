"""Page files a person edited by hand (contract rule 18, PR #225 review).

``check_page`` decides which page files are page records. A file that is not one is logged
``page_unreadable`` and skipped by the index, search, and recall alike. A file that is one is read by
every reader without raising, whatever is damaged inside its messages.
"""

from __future__ import annotations

import copy
import io
import json

import pytest

from contexto import cli, mcp
from contexto.store import PageConflictError, PageUnreadableError, SessionStore
from contexto.store.catalog import check_page, load_page
from contexto.store.pages import (
    build_page,
    build_stub,
    index_line,
    is_context_page,
    page_matches,
    search_entry,
)
from contexto.store.render import recall_chunk, render_page

KEY = b"k" * 32

MESSAGES_TURN = [
    {"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>\nsynthetic context\n</system-reminder>"},
        {"type": "text", "text": "Read conf/app.toml and set the port to garnet-4821."},
    ]},
    {"role": "system", "content": "synthetic carried note"},
    {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "synthetic thought", "signature": "sig"},
        {"type": "text", "text": "Reading it."},
        {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "conf/app.toml"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "port = 1"}]},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "plain output"},
    ]},
]

RESPONSES_TURN = [
    {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "synthetic rules"}]},
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix src/app.py please."}]},
    {"type": "reasoning", "summary": [], "encrypted_content": "synthetic"},
    {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": '{"cmd": ["cat", "src/app.py"]}'},
    {"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "print(1)"}]},
    {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2", "input": "*** Begin Patch"},
    {"type": "custom_tool_call_output", "call_id": "c2", "output": "Done"},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Fixed."}]},
]

REMINDER_TURN = [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>\nkept\n</system-reminder>"}]}]


def old_codex_page() -> dict:
    """A page written by v0.1.0 to v0.1.2: its Codex stub is empty, and ``current_stub`` rebuilds it."""
    page = build_page(KEY, RESPONSES_TURN, 2)
    return {**page, "stub": {"user_text": "", "tools": [], "paths": []}}


SAMPLES = {
    "messages": build_page(KEY, MESSAGES_TURN, 1),
    "responses": build_page(KEY, RESPONSES_TURN, 2),
    "reminder": build_page(KEY, REMINDER_TURN, 3),
    "old codex stub": old_codex_page(),
}

DAMAGE = [5, None, True, 1.5, "x", "", [], {}, [5], [None], [[]], {"type": 5}, {"type": ["text"]},
          {"type": "text", "text": 5}, {"type": "text", "text": None}, {"type": "input_text", "text": [1]}]


def paths(node, at=()):
    """Every place inside ``node``, as a key path (not the root itself)."""
    children = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for key, child in children:
        yield (*at, key)
        yield from paths(child, (*at, key))


def damaged(page: dict):
    """``page`` with one value replaced by each ``DAMAGE`` value, or one key removed, at every place."""
    for path in paths(page):
        for value in [*DAMAGE, KeyError]:
            copy_ = copy.deepcopy(page)
            parent = copy_
            for key in path[:-1]:
                parent = parent[key]
            if value is KeyError:
                if not isinstance(parent, dict):
                    continue
                del parent[path[-1]]
            else:
                parent[path[-1]] = copy.deepcopy(value)
            yield copy_
    for name in ("system_text", "reminder_text"):  # the stub's optional fields, added with a wrong type
        for value in DAMAGE:
            if not isinstance(value, str):
                yield {**page, "stub": {**page["stub"], name: value}}


def read_every_way(page: dict) -> None:
    """Every reader of a page record; none may raise for a page ``check_page`` passed."""
    text = render_page(page)
    assert isinstance(text, str)
    assert recall_chunk(page, 0, 100)["total_chars"] == len(text)
    entry = search_entry(page)
    assert isinstance(entry.text, bytes) and entry.id == page["id"] and entry.turn == page["turn"]
    for salient in (False, True):
        assert index_line(page, salient).startswith(f"{page['id']} turn {page['turn']}: ")
    is_context_page(page)
    page_matches(page, "port")
    mcp._hit_rows(page["id"], page, "port")
    mcp._more_row(page["id"], page, "port")
    check_page({**page, "stub": build_stub(page["messages"])})  # Contexto never writes a stub it refuses


@pytest.mark.parametrize("name", SAMPLES)
def test_a_page_check_page_passes_is_read_by_every_reader_whatever_its_messages_hold(name):
    passed = refused = 0
    for page in damaged(SAMPLES[name]):
        try:
            page = load_page(json.dumps(page), SAMPLES[name]["id"])
        except (TypeError, KeyError, ValueError):
            refused += 1
            continue
        read_every_way(page)
        passed += 1
    assert passed > 100 and refused > 20  # both sides of the check were exercised


@pytest.mark.parametrize("change", [
    {"messages": 5}, {"messages": None}, {"stub": {}}, {"stub": None}, {"stub": []}, {"id": 5}, {"turn": None},
    {"turn": True}, {"turn": "1"},
    {"stub": {"user_text": 5, "tools": [], "paths": []}},
    {"stub": {"user_text": "x", "tools": [5], "paths": []}},
    {"stub": {"user_text": "x", "tools": [], "paths": "a/b"}},
    {"stub": {"user_text": "", "tools": [], "paths": [], "system_text": None}},
    {"stub": {"user_text": "", "tools": [], "paths": [], "reminder_text": 5}},
])
def test_check_page_refuses_a_record_whose_own_fields_have_the_wrong_type(change):
    with pytest.raises((TypeError, KeyError)):
        check_page({**SAMPLES["messages"], **change})


@pytest.mark.parametrize("missing", ["id", "turn", "messages", "stub"])
def test_check_page_refuses_a_record_missing_a_field(missing):
    page = dict(SAMPLES["messages"])
    del page[missing]
    with pytest.raises(KeyError):
        check_page(page)


def test_a_stub_built_from_a_tool_call_named_with_a_number_lists_no_such_tool():
    turn = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": 5, "input": {}},
                                              {"type": "tool_use", "id": "u", "name": "Read", "input": {}}]}]
    assert build_page(KEY, turn, 1)["stub"]["tools"] == ["Read"]


def damage_text(store: SessionStore, page_id: str, value) -> None:
    """Set the assistant's text ("Reading it.") on a stored page to ``value``: still a page record (rule 18)."""
    path = store.pages_dir / f"{page_id}.json"
    record = json.loads(path.read_text())
    record["messages"][2]["content"][1]["text"] = value
    path.write_text(json.dumps(record))


@pytest.mark.parametrize("value", [5, None, ["x"]])
def test_a_page_with_a_damaged_text_block_is_recalled_and_searched_alike(tmp_path, value):
    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([MESSAGES_TURN], 1)
    damage_text(store, pid, value)
    fresh = SessionStore(tmp_path, "s1")
    page = fresh.get_page(pid)
    text = render_page(page)
    assert "[malformed text omitted]" in text and "Reading it." not in text
    assert "garnet-4821" in text  # the rest of the page is still there
    assert fresh.search("Reading") == [] and fresh.search("garnet-4821") == [pid]
    server = mcp.Server(fresh, io.StringIO(), io.StringIO())
    recalled = server_call(server, "contexto_recall", {"page_id": pid})
    assert recalled["isError"] is False and "[malformed text omitted]" in recalled["content"][0]["text"]
    found = server_call(server, "contexto_search", {"query": "garnet-4821"})
    assert found["isError"] is False and pid in found["content"][0]["text"]
    assert not [e for e in log(fresh) if e["event"] == "page_unreadable"]


def test_contexto_recall_of_a_damaged_text_block_prints_the_page(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([MESSAGES_TURN], 1)
    damage_text(store, pid, 5)
    code = cli.main(["recall", pid, "--session", "s1"])
    out = capsys.readouterr()
    assert code == 0 and "Traceback" not in out.err
    assert "[malformed text omitted]" in out.out and "garnet-4821" in out.out


def server_call(server, name: str, arguments: dict) -> dict:
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return server.handle_line(line)["result"]


def log(store: SessionStore) -> list[dict]:
    return [json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("change", [{"messages": 5}, {"stub": {}}, {"stub": {"user_text": "x", "tools": [5],
                                                                              "paths": []}}, {"turn": None}])
def test_a_page_that_is_no_page_record_is_logged_and_skipped_everywhere(tmp_path, change):
    store = SessionStore(tmp_path, "s1")
    bad, good = store.page_out([MESSAGES_TURN, RESPONSES_TURN], 1)
    path = store.pages_dir / f"{bad}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **change}))
    fresh = SessionStore(tmp_path, "s1")
    index = fresh.index_text(2_000)
    assert good in index and bad not in index
    assert fresh.page_ids() == [good]
    assert fresh.search("app.py") == [good] and fresh.search("garnet-4821") == []
    with pytest.raises(PageUnreadableError):
        fresh.get_page(bad)
    assert {e["path"] for e in log(fresh) if e["event"] == "page_unreadable"} == {f"{bad}.json"}


@pytest.mark.parametrize("content", ["5", "[]", '"page"', "null", "STUB", "NO ID", "ID 5", "OTHER ID"])
def test_repaging_onto_a_page_file_that_holds_no_page_record_is_a_page_conflict(tmp_path, content):
    store = SessionStore(tmp_path, "s1")
    pid, other = store.page_out([MESSAGES_TURN, RESPONSES_TURN], 1)
    path = store.pages_dir / f"{pid}.json"
    record = json.loads(path.read_text())
    edits = {"STUB": {**record, "stub": {}}, "NO ID": {k: v for k, v in record.items() if k != "id"},
             "ID 5": {**record, "id": 5}, "OTHER ID": {**record, "id": other}}
    path.write_text(json.dumps(edits[content]) if content in edits else content)
    with pytest.raises(PageConflictError) as e:
        SessionStore(tmp_path, "s1").page_out([MESSAGES_TURN], 1)
    assert e.value.page_id == pid


def test_a_page_file_whose_record_names_another_id_is_skipped_everywhere(tmp_path):
    """Search and the index would list it under the id it names, which recall cannot find (reviewer A on
    PR #228): it is no page record of the file it is in."""
    store = SessionStore(tmp_path, "s1")
    bad, good = store.page_out([MESSAGES_TURN, RESPONSES_TURN], 1)
    path = store.pages_dir / f"{bad}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "id": "pg_00000000000000ff"}))
    fresh = SessionStore(tmp_path, "s1")
    assert fresh.page_ids() == [good] and "pg_00000000000000ff" not in fresh.index_text(2_000)
    assert fresh.search("garnet-4821") == []
    with pytest.raises(PageUnreadableError):
        fresh.get_page(bad)
    assert fresh.get_page("pg_00000000000000ff") is None


def test_a_damaged_tool_result_or_codex_output_shows_a_marker_not_an_empty_output():
    page = {"turn": 1, "messages": [
        {"role": "user", "content": [{"type": "tool_result", "content": 5},
                                     {"type": "tool_result", "content": {"text": "x"}},
                                     {"type": "tool_result", "content": None}]},
        {"type": "function_call_output", "call_id": "c1", "output": 5},
        {"type": "function_call_output", "call_id": "c2", "output": [{"type": "input_text", "text": 5},
                                                                    {"type": "input_text", "text": "ok"}]},
        {"type": "function_call_output", "call_id": "c3", "output": None},
        {"type": "message", "role": "user", "content": [{"type": ["input_text"], "text": "x"},
                                                        {"type": "input_image", "image_url": "data:"},
                                                        {"type": "input_text", "text": "kept"}]},
        {"type": "message", "role": "assistant", "content": 5},
        {"type": 5, "role": "assistant", "content": [{"type": "output_text", "text": "x"}]},
    ]}
    assert render_page(page).split("\n") == [
        "[turn 1] user:",
        "tool_result:", "[malformed content omitted]",
        "tool_result:", "[malformed content omitted]",
        "tool_result:",  # no content: an empty output
        "[turn 1] user:", "tool_result:", "[malformed text omitted]",
        "[turn 1] user:", "tool_result:", "[malformed text omitted]", "ok",
        "[turn 1] user:", "tool_result:",
        "[turn 1] user:", "[malformed text omitted]", "kept",  # the image is left out, as before
        "[turn 1] assistant:", "[malformed text omitted]",
        "[turn 1] assistant:", "[malformed text omitted]",  # an item whose type is not a string
    ]
    text = search_entry({**page, "id": "pg_0000000000000001", "stub": build_stub(page["messages"])}).text
    assert b"malformed" not in text and b"ok" in text and b"kept" in text
