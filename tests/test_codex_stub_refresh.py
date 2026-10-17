"""Codex pages written before v0.1.3 get their stub rebuilt when read (known limitation of B23).

v0.1.0 to v0.1.2 stored every Codex (Responses) page with the stub
``{"user_text": "", "tools": [], "paths": []}``: the stub rule read only Messages API blocks. A page
keeps its exact messages, so the store rebuilds that stub with the current rule when it reads the
page. Nothing is written: the page file, its id, and its content stay as they are (pages are
immutable, contract Store section), so there is nothing to corrupt and no command to run.
"""

import io
import json
import os

import pytest

from contexto import mcp
from contexto.canonical import canonical_bytes
from contexto.paths import contexto_home
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.pages import build_stub, current_stub, index_line, page_id

# The stub v0.1.2's rule stored for every Codex page (checked by running def6e8c's build_stub).
OLD_STUB = {"user_text": "", "tools": [], "paths": []}

CODEX_TURN = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "check the config"}]},
              {"type": "function_call", "name": "read_file", "call_id": "c1",
               "arguments": json.dumps({"path": "conf/app.toml"})},
              {"type": "function_call_output", "call_id": "c1", "output": "retention_days = 30"},
              {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]
CODEX_STUB = {"user_text": "check the config", "tools": ["read_file"], "paths": ["conf/app.toml"]}

CLAUDE_TURN = [{"role": "user", "content": [{"type": "text", "text": "fix the parser"}]},
               {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                                  "input": {"file_path": "/repo/parser.py"}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]


def make_old(store, turn, number=0):
    """Page ``turn`` with the current code, then overwrite its file the way v0.1.2 wrote it."""
    [pid] = store.page_out([turn], number)
    old = {**store.get_page(pid), "stub": dict(OLD_STUB)}
    if isinstance(store, MemoryStore):
        store.pages[pid] = old
    else:
        (store.pages_dir / f"{pid}.json").write_bytes(canonical_bytes(old))
    return pid


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore(key=b"k" * 32)


def test_an_old_codex_stub_is_rebuilt_on_read(store):
    pid = make_old(store, CODEX_TURN)
    page = store.get_page(pid)
    assert page["stub"] == CODEX_STUB == build_stub(CODEX_TURN)
    assert page["id"] == pid and page["messages"] == CODEX_TURN and page["turn"] == 0


def test_the_index_line_of_an_old_codex_page_shows_user_text_tools_and_paths(store):
    pid = make_old(store, CODEX_TURN)
    line = store.index_text(4000)
    assert line.startswith(f"{pid} turn 0: check the config [tools: read_file] [files: conf/app.toml]")


def test_search_finds_an_old_codex_page_by_its_tool_name(store):
    pid = make_old(store, CODEX_TURN)
    assert store.search("read_file") == [pid]
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_search", "arguments": {"query": "check the config"}}})
    reply = server.handle_line(line)["result"]["content"][0]["text"]
    assert pid in reply


def test_a_current_codex_stub_is_returned_as_stored(store):
    [pid] = store.page_out([CODEX_TURN], 0)
    stored = store.get_page(pid)
    assert stored["stub"] == CODEX_STUB
    assert current_stub(stored) is stored  # nothing rebuilt, not even a copy


def test_only_the_old_empty_shape_is_rebuilt():
    """A Codex stub with any field filled was written by v0.1.3 or later: kept even if the rule changes."""
    for stub in ({"user_text": "kept as stored", "tools": [], "paths": []},
                 {"user_text": "", "tools": ["other"], "paths": []},
                 {"user_text": "", "tools": [], "paths": ["a/b"]}):
        page = {"id": "pg_0", "turn": 0, "messages": CODEX_TURN, "stub": stub}
        assert current_stub(page) is page


def test_a_codex_page_with_nothing_to_index_keeps_its_stub():
    turn = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]}]
    page = {"id": "pg_0", "turn": 0, "messages": turn, "stub": build_stub(turn)}
    assert page["stub"] == OLD_STUB
    assert current_stub(page) is page  # rebuilt to the same value: the stored record is returned


def test_claude_pages_are_never_rebuilt(store):
    """An empty stub on a Messages API page is left alone, whatever the current rule would produce."""
    [pid] = store.page_out([CLAUDE_TURN], 0)
    page = {**store.get_page(pid), "stub": dict(OLD_STUB)}
    assert current_stub(page) is page
    assert index_line(page) == f"{pid} turn 0: "
    assert store.get_page(pid)["stub"] == build_stub(CLAUDE_TURN)  # a normal Claude page reads as written


def test_the_rebuild_is_idempotent(store):
    pid = make_old(store, CODEX_TURN)
    first = store.get_page(pid)
    assert current_stub(first) is first
    assert store.get_page(pid) == first and store.index_text(4000) == store.index_text(4000)


def test_the_rebuild_never_changes_the_page_id():
    page = {"turn": 3, "messages": CODEX_TURN, "stub": dict(OLD_STUB)}
    key = b"k" * 32
    page["id"] = page_id(key, page)
    assert page_id(key, current_stub(page)) == page["id"] == current_stub(page)["id"]


def test_reading_writes_nothing(tmp_path):
    """The stub is rebuilt in memory only: the page file keeps its bytes and mtime, no temp file appears,
    and a page directory that cannot be written to still reads (so no crash can leave a partial page)."""
    store = SessionStore(tmp_path, "s1")
    pid = make_old(store, CODEX_TURN)
    path = store.pages_dir / f"{pid}.json"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    names = sorted(os.listdir(store.pages_dir))
    os.chmod(store.pages_dir, 0o500)
    try:
        reader = SessionStore.open_existing(tmp_path, "s1")
        assert reader.get_page(pid)["stub"] == CODEX_STUB
        assert "[tools: read_file]" in reader.index_text(4000)
        assert reader.search("read_file") == [pid]
    finally:
        os.chmod(store.pages_dir, 0o700)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert sorted(os.listdir(store.pages_dir)) == names


def test_pages_under_contexto_home_are_read_with_the_rebuilt_stub(tmp_path, monkeypatch):
    """Commands and the MCP server open stores under contexto_home(); CONTEXTO_HOME needs nothing extra."""
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "home"))
    pid = make_old(SessionStore(contexto_home(), "s1"), CODEX_TURN)
    reader = SessionStore.open_existing(contexto_home(), "s1")
    assert reader.get_page(pid)["stub"] == CODEX_STUB
    assert reader.index_text(4000).startswith(f"{pid} turn 0: check the config [tools: read_file]")
