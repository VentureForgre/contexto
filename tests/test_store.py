import hashlib
import hmac
import json
import os

import pytest

from contexto.canonical import canonical_bytes
from contexto.store import PageConflictError, SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.pages import PINNED_FIRST_LINE, build_stub, page_id


def turn(prompt, tool="Read", path="/repo/src/app.py", result="ok", thinking="hmm"):
    return [
        {"role": "user", "content": [{"type": "text", "text": prompt}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": thinking, "signature": "sig"},
            {"type": "tool_use", "id": "t1", "name": tool, "input": {"file_path": path}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": result}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done, see docs/notes.md"}]},
    ]


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path, "s1")


def test_page_files_hold_the_exact_turn_and_metadata(store):
    t = turn("fix the bug in the parser")
    [pid] = store.page_out([t], 7)
    page = json.loads((store.pages_dir / f"{pid}.json").read_text())
    assert page["id"] == pid and page["turn"] == 7
    assert page["messages"] == t  # thinking kept as received
    assert page["bytes"] > 0 and page["tokens"] > 0
    assert page["stub"] == {"user_text": "fix the bug in the parser", "tools": ["Read"],
                            "paths": ["/repo/src/app.py", "docs/notes.md"]}
    assert (store.pages_dir / f"{pid}.json").read_bytes() == canonical_bytes(page)


def test_page_id_is_keyed_hmac_over_canonical_json(store):
    [pid] = store.page_out([turn("a")], 1)
    key = bytes.fromhex((store.dir / "page.key").read_text())
    page = store.get_page(pid)
    body = {"turn": page["turn"], "messages": page["messages"]}  # content only (contract PR from intake/m2-pageid)
    assert pid == "pg_" + hmac.new(key, canonical_bytes(body), hashlib.sha256).hexdigest()[:16]
    # A plain SHA-256 of the same content would make short turns guessable; the id must differ.
    assert pid != "pg_" + hashlib.sha256(canonical_bytes(body)).hexdigest()[:16]


def test_key_is_private_stable_and_per_store(tmp_path):
    a = SessionStore(tmp_path, "a")
    assert (a.dir / "page.key").stat().st_mode & 0o777 == 0o600
    again = SessionStore(tmp_path, "a")
    t = turn("same")
    assert a.page_out([t], 1) == again.page_out([t], 1)
    b = SessionStore(tmp_path, "b")
    assert a.page_out([t], 1) != b.page_out([t], 1)


def test_rewrite_is_idempotent_and_pages_are_immutable(store):
    t = turn("x")
    [pid] = store.page_out([t], 3)
    assert store.page_out([t], 3) == [pid]
    path = store.pages_dir / f"{pid}.json"
    path.write_bytes(path.read_bytes().replace(b'"turn":3', b'"turn":4'))
    with pytest.raises(PageConflictError):
        store.page_out([t], 3)


def test_turn_numbers_follow_first_turn(store):
    ids = store.page_out([turn("one"), turn("two"), turn("three")], 10)
    assert [store.get_page(i)["turn"] for i in ids] == [10, 11, 12]
    assert store.page_ids() == ids[::-1]
    mem = MemoryStore()
    mids = mem.page_out([turn("one"), turn("two")], 1)
    assert mem.page_ids() == mids[::-1]


def test_stub_truncates_user_text_and_reads_string_content():
    long = "w" * 250
    stub = build_stub([{"role": "user", "content": long}])
    assert stub == {"user_text": "w" * 200, "tools": [], "paths": []}


def test_index_is_newest_first_capped_with_omitted_count(store):
    store.page_out([turn(f"prompt number {i}") for i in range(30)], 1)
    full = store.index_text(100_000).splitlines()
    assert len(full) == 30
    assert full[0].split()[1:3] == ["turn", "30:"] and full[-1].split()[1:3] == ["turn", "1:"]
    capped = store.index_text(200).splitlines()
    assert 1 < len(capped) < 30
    assert capped[-1] == f"({30 - (len(capped) - 1)} older pages omitted; use contexto_search to find them)"
    assert capped[0] == full[0]


def test_index_of_an_empty_store_is_empty(store):
    assert store.index_text(4000) == ""


def test_pinned_text_starts_with_the_mandatory_line(store):
    assert store.pinned_text(6000).splitlines()[0] == PINNED_FIRST_LINE
    assert "contexto_recall" in PINNED_FIRST_LINE


def test_search_is_exact_substring_over_stub_and_text(store):
    a, b = store.page_out([turn("alpha task", result="needle in result"), turn("beta task")], 1)
    assert store.search("needle") == [a]
    assert store.search("task") == [b, a]
    assert store.search("Needle") == []
    assert store.search("") == []
    assert store.get_page("pg_nothere00000000") is None
    assert store.get_page("../page.key") is None


def test_log_appends_jsonl_with_credentials_redacted(store):
    store.log({"event": "upstream_4xx", "status": 400,
               "headers": {"Authorization": "Bearer s", "x-api-key": "k", "content-type": "json"},
               "epoch": {"k": 3}})
    store.log({"event": "rotation", "ts": "fixed"})
    lines = (store.dir / "log.jsonl").read_text().splitlines()
    first, second = (json.loads(x) for x in lines)
    assert first["headers"] == {"Authorization": "[redacted]", "x-api-key": "[redacted]", "content-type": "json"}
    assert first["epoch"] == {"k": 3} and "ts" in first
    assert second["ts"] == "fixed"


def test_memory_store_matches_session_store_for_the_same_key(store):
    key = bytes.fromhex((store.dir / "page.key").read_text())
    mem = MemoryStore("s1", key=key)
    turns = [turn("one"), turn("two")]
    assert mem.page_out(turns, 1) == store.page_out(turns, 1)
    assert mem.index_text(4000) == store.index_text(4000)
    assert mem.pinned_text(6000) == store.pinned_text(6000)
    assert mem.search("two") == store.search("two")
    mem.log({"event": "x", "cookie": "c"})
    assert mem.events[0]["cookie"] == "[redacted]"
    assert page_id(key, mem.get_page(mem.page_out([turns[0]], 1)[0])) == mem.page_out([turns[0]], 1)[0]


def test_session_store_rejects_unsafe_session_ids(tmp_path):
    with pytest.raises(ValueError):
        SessionStore(tmp_path, "../escape")
    assert not os.path.exists(tmp_path.parent / "escape")
