"""Content-only page ids (M2 B8; contract PR #47).

A page id is ``pg_`` + the first 16 hex of HMAC-SHA256, keyed per store, over the canonical JSON of
``{turn, messages}``. Nothing derived (stub, byte or token estimates) is part of it.
"""

import copy
import hashlib
import hmac

import pytest

import contexto.store.pages as pages
import contexto.tokens as tokens
from contexto.canonical import canonical_bytes
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.pages import build_page

KEY = bytes(range(32))
TOOL_TURN = [
    {"role": "user", "content": [{"type": "text", "text": "fix the parser in src/p.py"}]},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                       "input": {"file_path": "src/p.py"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
]
SYSTEM_TURN = [{"role": "system", "content": "Tool added: grep_files"}]


def by_definition(turn_number, messages, key=KEY):
    body = canonical_bytes({"turn": turn_number, "messages": messages})
    return "pg_" + hmac.new(key, body, hashlib.sha256).hexdigest()[:16]


@pytest.mark.parametrize("turn, number, golden", [(TOOL_TURN, 3, "pg_ddfe6e1db2f05ad6"),
                                                  (SYSTEM_TURN, 4, "pg_4bf3c1553eb6eddb")])
def test_golden_ids_follow_the_content_only_rule(turn, number, golden):
    assert build_page(KEY, turn, number)["id"] == golden == by_definition(number, turn)


def test_ids_do_not_change_with_the_tool_seed(monkeypatch):
    before = build_page(KEY, TOOL_TURN, 3)
    monkeypatch.setattr(tokens, "TOOL_RESULT_BYTES_PER_TOKEN", 0.5)
    after = build_page(KEY, TOOL_TURN, 3)
    assert after["tokens"] != before["tokens"] and after["id"] == before["id"]


def test_ids_do_not_change_with_the_stub_rule(monkeypatch):
    before = build_page(KEY, SYSTEM_TURN, 4)
    monkeypatch.setattr(pages, "build_stub", lambda turn: {"user_text": "different", "tools": [], "paths": []})
    after = build_page(KEY, SYSTEM_TURN, 4)
    assert after["stub"] != before["stub"] and after["id"] == before["id"]


def test_identical_content_same_id_and_any_change_a_different_id():
    base = build_page(KEY, TOOL_TURN, 3)["id"]
    assert build_page(KEY, copy.deepcopy(TOOL_TURN), 3)["id"] == base
    assert build_page(KEY, TOOL_TURN, 4)["id"] != base
    edited = copy.deepcopy(TOOL_TURN)
    edited[2]["content"][0]["content"] = "ok!"  # one byte added inside a tool_result
    assert build_page(KEY, edited, 3)["id"] != base
    thinking = copy.deepcopy(TOOL_TURN)
    thinking[1]["content"].insert(0, {"type": "thinking", "thinking": "hm", "signature": "s"})
    assert build_page(KEY, thinking, 3)["id"] != base  # thinking is part of the exact messages
    assert build_page(bytes(32), TOOL_TURN, 3)["id"] != base  # a different store key


@pytest.mark.parametrize("make", [lambda tmp: SessionStore(tmp, "s1"), lambda tmp: MemoryStore("s1")])
def test_stores_use_the_rule_and_rewrites_stay_idempotent(tmp_path, make):
    store = make(tmp_path)
    key = bytes.fromhex((store.dir / "page.key").read_text()) if isinstance(store, SessionStore) else store._key
    ids = store.page_out([TOOL_TURN, SYSTEM_TURN], 7)
    assert ids == [by_definition(7, TOOL_TURN, key), by_definition(8, SYSTEM_TURN, key)]
    assert store.page_out([TOOL_TURN, SYSTEM_TURN], 7) == ids


def test_repaging_after_an_upgrade_keeps_the_stored_page(tmp_path):
    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([TOOL_TURN], 3)
    path = store.pages_dir / f"{pid}.json"
    old = store.get_page(pid)
    old["stub"] = {"user_text": "written by an older stub rule", "tools": [], "paths": []}
    old["tokens"] += 99  # and an older estimator
    from contexto.canonical import canonical_bytes as cb
    path.write_bytes(cb(old))
    stored = path.read_bytes()
    assert store.page_out([TOOL_TURN], 3) == [pid]  # no PageConflictError
    assert path.read_bytes() == stored  # the stored page is kept as written


def test_repaging_after_an_upgrade_keeps_the_stored_page_in_memory():
    store = MemoryStore("s1", key=KEY)
    [pid] = store.page_out([TOOL_TURN], 3)
    store.pages[pid] = {**store.pages[pid], "tokens": 1, "stub": {"user_text": "old", "tools": [], "paths": []}}
    kept = dict(store.pages[pid])
    assert store.page_out([TOOL_TURN], 3) == [pid] and store.pages[pid] == kept


@pytest.mark.parametrize("make", [lambda tmp: SessionStore(tmp, "s1"), lambda tmp: MemoryStore("s1", key=KEY)])
def test_conflict_only_when_turn_or_messages_differ_under_the_same_id(tmp_path, make):
    from contexto.store import PageConflictError

    store = make(tmp_path)
    [pid] = store.page_out([TOOL_TURN], 3)
    if isinstance(store, SessionStore):
        path = store.pages_dir / f"{pid}.json"
        path.write_bytes(path.read_bytes().replace(b'"content":"ok"', b'"content":"KO"'))
    else:
        tampered = copy.deepcopy(store.pages[pid])
        tampered["messages"][2]["content"][0]["content"] = "KO"
        store.pages[pid] = tampered
    with pytest.raises(PageConflictError):
        store.page_out([TOOL_TURN], 3)


def test_an_unreadable_stored_page_is_a_conflict(tmp_path):
    from contexto.store import PageConflictError

    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([TOOL_TURN], 3)
    (store.pages_dir / f"{pid}.json").write_bytes(b"{not json")
    with pytest.raises(PageConflictError):
        store.page_out([TOOL_TURN], 3)
