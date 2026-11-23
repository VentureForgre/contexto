"""Tests for SQLite FTS5 indexing, code-aware search, expanded snippets, and pages/recall/render fixes."""

from __future__ import annotations

import io
import json
import math
import os
import stat

from contexto import mcp
from contexto.store import SessionStore, catalog, fts
from contexto.store.memory import MemoryStore
from contexto.store.pages import build_stub, index_line
from contexto.store.recall import salient_identifiers
from contexto.store.render import EXPAND_MAX_CHARS_PER_TOKEN, own_call_ids, recall_limit
from contexto.store.search import code_tokens, split_identifier


def turn(user: str, assistant: str = "ok") -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": user}]},
        {"role": "assistant", "content": [{"type": "text", "text": assistant}]},
    ]


def tool(server: mcp.Server, name: str, arguments: dict) -> dict:
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return server.handle_line(line)["result"]


# 1. SQLite FTS5 lazy build, incremental updates, read-only earlier sessions, fallback


def test_fts5_lazy_build_incremental_sync_and_zero_rescan(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, "s1")
    ids = store.page_out([turn(f"service module {i} unique_key_{i}") for i in range(10)], 0)
    db_path = store.dir / fts.FTS_FILE
    # Lazily built: search.sqlite does not exist until first search
    assert not db_path.exists()

    assert store.search("unique_key_3") == [ids[3]]
    assert db_path.is_file()
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    # Track page reads on subsequent searches
    reads: list[str] = []
    real_read = catalog.read_page_text

    def counting_read(path):
        reads.append(path.name)
        return real_read(path)

    monkeypatch.setattr(catalog, "read_page_text", counting_read)

    # Second search reads ZERO page files from disk!
    assert store.search("unique_key_7") == [ids[7]]
    assert reads == []

    # Adding 1 new page reads ONLY that 1 new page on next search!
    [new_id] = store.page_out([turn("brand new deployment_marker_99")], 10)
    assert store.search("deployment_marker_99") == [new_id]
    assert reads == [f"{new_id}.json"]

    # Quarantining a page removes it from FTS5 results cleanly
    store.quarantine_page(ids[3])
    assert store.search("unique_key_3") == []


def test_fts5_recovers_from_corrupt_db_and_falls_back_when_unavailable(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, "s1")
    [p1, p2] = store.page_out([turn("alpha needle here"), turn("beta needle here")], 0)
    assert set(store.search("needle")) == {p1, p2}

    # Corrupt search.sqlite with garbage bytes: writable store recovers transparently!
    store._fts.close()
    (store.dir / fts.FTS_FILE).write_bytes(b"not a valid sqlite database" * 10)
    assert set(store.search("alpha")) == {p1}
    assert set(store.search("beta")) == {p2}

    # Fallback when FTS5 is unavailable
    monkeypatch.setattr(fts, "fts5_available", lambda: False)
    store2 = SessionStore(tmp_path, "s2")
    [p3] = store2.page_out([turn("fallback search needle")], 0)
    assert store2.search("fallback needle") == [p3]
    assert not (store2.dir / fts.FTS_FILE).exists()


def test_fts5_earlier_session_is_strictly_read_only(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    home = tmp_path / "home"
    old = SessionStore(home, "e1aaaaaa")
    old.set_workdir(root)
    [old_pid] = old.page_out([turn("earlier session secret_token_xyz")], 0)
    # Build old's FTS5 index while old is alive
    assert old.search("secret_token_xyz") == [old_pid]
    old.release()
    os.utime(old.dir / "page.key", (1_700_000_000, 1_700_000_000))

    before = {str(p.relative_to(old.dir)): (p.stat().st_size, p.stat().st_mtime_ns)
              for p in sorted(old.dir.rglob("*"))}

    cur = SessionStore(home, "c2bbbbbb")
    cur.set_workdir(root)
    server = mcp.Server(cur, io.StringIO(), io.StringIO())
    res = tool(server, "contexto_search", {"query": "secret_token_xyz", "scope": "project"})
    assert old_pid in res["content"][0]["text"]

    after = {str(p.relative_to(old.dir)): (p.stat().st_size, p.stat().st_mtime_ns)
             for p in sorted(old.dir.rglob("*"))}
    assert after == before


# 2. Code-aware tokenization & approximate fallback in project-scope & in-view


def test_code_aware_tokenization_and_cross_style_matching(tmp_path):
    assert split_identifier("refreshToken") == ("refresh", "token")
    assert split_identifier("refresh_token") == ("refresh", "token")
    assert split_identifier("refresh-token") == ("refresh", "token")
    assert split_identifier("HTTPTimeout") == ("http", "timeout")
    assert "refresh" in code_tokens("refreshToken") and "token" in code_tokens("refreshToken")

    store = SessionStore(tmp_path, "s1")
    [p_prose] = store.page_out([turn("The auth service rotates the refresh token every 24h.")], 0)
    [p_camel] = store.page_out([turn("Store refreshToken securely in the keychain.")], 1)

    # Space-separated query matches both camelCase and prose
    assert set(store.search("refresh token")) == {p_prose, p_camel}
    # camelCase and snake_case queries find prose "refresh token" via approximate code-split!
    approx_camel = store.search_approximate("refreshToken")
    assert {h.id for h in approx_camel} == {p_prose, p_camel}
    approx_snake = store.search_approximate("refresh_token")
    assert {h.id for h in approx_snake} == {p_prose, p_camel}


def test_approximate_fallback_works_in_view_and_project_scope(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    home = tmp_path / "home"

    old = SessionStore(home, "e1aaaaaa")
    old.set_workdir(root)
    [old_pid] = old.page_out([turn("The OAuth handler caches refreshToken for 15 minutes.")], 0)
    old.release()
    os.utime(old.dir / "page.key", (1_700_000_000, 1_700_000_000))

    cur = SessionStore(home, "c2bbbbbb")
    cur.set_workdir(root)
    cur.set_retained(1, 0, [
        {"turn": 1, "message_index": 0, "role": "user", "text": "Current auth uses refreshToken rotation"},
        {"turn": 2, "message_index": 1, "role": "user", "text": "asking now"},
    ])
    cur.set_turn_room("2", 10_000)

    server = mcp.Server(cur, io.StringIO(), io.StringIO())
    # In-view approximate match when strict finds nothing!
    res = tool(server, "contexto_search", {"query": "refresh_token", "scope": "project"})
    text = res["content"][0]["text"]
    assert "in view (approximate)" in text
    assert "refreshToken rotation" in text
    # Project-scope approximate fallback finds earlier session's camelCase match too!
    assert mcp.EARLIER_APPROX_HEADER in text
    assert old_pid in text


def test_optional_approximate_parameter_forces_approximate_when_strict_exists():
    store = MemoryStore("s1")
    [p_camel] = store.page_out([turn("Use refreshToken for session renewal.")], 0)
    [p_snake] = store.page_out([turn("Config key refresh_token is deprecated.")], 1)
    server = mcp.Server(store, io.StringIO(), io.StringIO())

    # Strict search for refresh_token finds only p_snake
    strict_text = tool(server, "contexto_search", {"query": "refresh_token"})["content"][0]["text"]
    assert p_snake in strict_text and p_camel not in strict_text

    # Passing approximate=True finds BOTH p_snake and p_camel!
    approx_text = tool(server, "contexto_search", {"query": "refresh_token", "approximate": True})["content"][0]["text"]
    assert p_snake in approx_text and p_camel in approx_text


# 3. expand=True, context_lines, and measured bytes_per_token


def test_search_expand_and_context_lines_and_measured_budget():
    lines = [f"line_{i:02d}: filler text here" for i in range(20)]
    lines[10] = "line_10: TARGET_NEEDLE_VALUE = 42"
    store = MemoryStore("s1")
    store.set_window(200_000, 3.2)  # measured 3.2 bytes/token (> 1.5 floor)
    store.page_out([turn("\n".join(lines))], 0)
    server = mcp.Server(store, io.StringIO(), io.StringIO())

    # Default: 1 context line either side (lines 9, 10, 11)
    default_text = tool(server, "contexto_search", {"query": "TARGET_NEEDLE_VALUE"})["content"][0]["text"]
    assert "line_09:" in default_text and "line_11:" in default_text
    assert "line_06:" not in default_text and "line_14:" not in default_text

    # expand=True: 5 context lines either side (lines 5..15)
    store.set_turn_room("1", 1_000)
    expanded = tool(server, "contexto_search", {"query": "TARGET_NEEDLE_VALUE", "expand": True})["content"][0]["text"]
    assert "line_05:" in expanded and "line_15:" in expanded
    # Charged using measured ratio (3.2) instead of 1.5!
    expected_charged = math.ceil(len(expanded) / min(3.2, EXPAND_MAX_CHARS_PER_TOKEN))
    assert store.turn_room()[2] == expected_charged

    # Explicit context_lines=3
    ctx3 = tool(server, "contexto_search", {"query": "TARGET_NEEDLE_VALUE", "context_lines": 3})["content"][0]["text"]
    assert "line_07:" in ctx3 and "line_13:" in ctx3
    assert "line_06:" not in ctx3 and "line_14:" not in ctx3

    # recall_limit(measured=True) uses measured ratio up to EXPAND_MAX_CHARS_PER_TOKEN
    assert recall_limit(20_000, 3.0, measured=False)[0] == 7_500  # 5000 * 1.5
    assert recall_limit(20_000, 3.0, measured=True)[0] == 15_000  # 5000 * 3.0


# 4. pages.py, recall.py, and render.py blind spots


def test_nested_tool_use_inputs_are_indexed_and_searchable():
    nested_turn = [
        {"role": "user", "content": "apply edits"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "MultiEdit", "input": {
                "file_path": "src/auth.py",
                "edits": [
                    {"old_string": "legacy_token", "new_string": "nested_super_marker_77"},
                    {"meta": {"tags": ["deep_nested_list_marker"]}},
                ],
            }},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "applied"}]},
    ]
    store = MemoryStore("s1")
    [pid] = store.page_out([nested_turn], 0)
    assert store.search("nested_super_marker_77") == [pid]
    assert store.search("deep_nested_list_marker") == [pid]


def test_salient_identifiers_keep_technical_digit_tokens_and_filter_noise():
    text = (
        "oauth2=enabled sha256=required http2=true s3=primary v2=active k8s=cluster utf8=yes\n"
        "550e8400-e29b-41d4-a716-446655440000=uuid deadbeef1234=hash AI-93532=ticket "
        "v2.13.0=ver 2026-11-22=date 12345=num onyx-4681=cohort\n"
    )
    ids = salient_identifiers([text])
    for kept in ("oauth2", "sha256", "http2", "s3", "v2", "k8s", "utf8"):
        assert kept in ids, (kept, ids)
    for dropped in ("550e8400-e29b-41d4-a716-446655440000", "deadbeef1234", "AI-93532",
                    "v2.13.0", "2026-11-22", "12345", "onyx-4681"):
        assert dropped not in ids, (dropped, ids)


def test_stub_prioritizes_edited_paths_filters_urls_mimes_and_adds_short_prompt_outcome():
    sample_turn = [
        {"role": "user", "content": "fix test"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "docs/guide.md"}},
            {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "src/core/parser.py"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "Fetch https://api.example.com/v1/items with Content-Type: application/json and text/plain"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "updated"},
        ]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Fixed null check in src/core/parser.py; pytest passes now."},
        ]},
    ]
    stub = build_stub(sample_turn)
    assert stub["paths"][0] == "src/core/parser.py"  # Edit prioritized ahead of Read!
    assert "docs/guide.md" in stub["paths"]
    assert "application/json" not in stub["paths"]
    assert "text/plain" not in stub["paths"]
    assert not any("api.example.com" in p for p in stub["paths"])
    assert stub["assistant_text"] == "Fixed null check in src/core/parser.py; pytest passes now."

    page = {"id": "pg_0123456789abcdef", "turn": 4, "stub": stub, "messages": sample_turn}
    line = index_line(page)
    assert "fix test -> Fixed null check in src/core/parser.py; pytest passes now." in line


def test_self_echo_suppression_recognizes_any_mcp_prefix(monkeypatch):
    for name in ("mcp__contexto__contexto_search", "mcp__my-ctx__contexto_recall",
                 "mcp__prod_contexto__contexto_state", "contexto_search"):
        t = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "u1", "name": name, "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u1", "content": "echo"}]},
        ]
        assert own_call_ids(t) == {"u1"}, name

    # Custom env prefix without mcp__ also recognized
    monkeypatch.setenv("CONTEXTO_MCP_PREFIX", "custom_srv:")
    t_env = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "u2", "name": "custom_srv:contexto_search", "input": {}}
        ]},
    ]
    assert own_call_ids(t_env) == {"u2"}


# 6. Review regression tests: FTS5 O(N) sync, concurrency, locked fallback, parity, D69 safety


def test_fts_initial_sync_does_not_rescan_directory_per_page(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, "s-fts-linear")
    store.page_out([turn(f"page payload alpha_{i}") for i in range(25)], 0)

    calls = 0
    orig_listing = catalog.PageCatalog._listing

    def counting_listing(self):
        nonlocal calls
        calls += 1
        return orig_listing(self)

    monkeypatch.setattr(catalog.PageCatalog, "_listing", counting_listing)
    hits = store.search("alpha_12")
    assert len(hits) == 1
    # Should list the directory once per search, never once per page (O(N^2)).
    assert calls <= 2


def test_fts_concurrent_writable_stores_sync_without_error(tmp_path):
    import threading

    s1 = SessionStore(tmp_path, "s-fts-concurrent")
    s1.page_out([turn(f"concurrent payload token_{i}") for i in range(20)], 0)
    s2 = SessionStore(tmp_path, "s-fts-concurrent")

    errors: list[BaseException] = []
    results: list[list[str]] = []

    def worker(store: SessionStore, q: str) -> None:
        try:
            results.append(store.search(q))
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(s1, "token_5")),
        threading.Thread(target=worker, args=(s2, "token_5")),
        threading.Thread(target=worker, args=(s1, "token_12")),
        threading.Thread(target=worker, args=(s2, "token_12")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors
    assert all(len(r) == 1 for r in results)
    assert (s1.dir / fts.FTS_FILE).is_file()


def test_fts_locked_db_falls_back_without_deleting_search_sqlite(tmp_path, monkeypatch):
    import sqlite3

    store = SessionStore(tmp_path, "s-fts-locked")
    [pid] = store.page_out([turn("exclusive lock test needle_xyz")], 0)
    assert store.search("needle_xyz") == [pid]
    db_path = store.dir / fts.FTS_FILE
    ino_before = db_path.stat().st_ino

    # Add a new page so the next search needs to write to search.sqlite.
    [pid2] = store.page_out([turn("second page needle_abc")], 1)
    monkeypatch.setattr(fts, "_BUSY_TIMEOUT_MS", 50)

    locker = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        # Must fall back cleanly to PageCatalog AND NOT delete search.sqlite!
        assert store.search("needle_abc") == [pid2]
        assert db_path.is_file()
        assert db_path.stat().st_ino == ino_before
    finally:
        locker.execute("ROLLBACK")
        locker.close()


def test_fts_corrupt_db_and_journal_rebuild_on_first_search(tmp_path):
    store = SessionStore(tmp_path, "s-fts-corrupt-rebuild")
    [pid] = store.page_out([turn("corrupt rebuild target_777")], 0)
    db_path = store.dir / fts.FTS_FILE
    journal_path = store.dir / (fts.FTS_FILE + "-journal")
    db_path.write_bytes(b"corrupt header bytes not sqlite")
    journal_path.write_bytes(b"stale corrupt journal bytes")

    assert store.search("target_777") == [pid]
    assert db_path.is_file()
    assert not journal_path.exists()
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_fts_vs_catalog_exact_parity_on_unicode_and_short_words(tmp_path):
    store_fts = SessionStore(tmp_path, "s-parity", use_fts=True)
    pages = [
        turn("Istanbul İSTANBUL ſervice db io alpha beta"),
        turn("alpha beta alpha beta db"),
        turn("alpha gamma io"),
        turn("lone surrogate \ud800 alpha"),
    ]
    store_fts.page_out(pages, 0)
    store_scan = SessionStore(tmp_path, "s-parity", use_fts=False)

    for query in ("alpha beta", "db io", "istanbul", "service", "alpha"):
        assert store_fts.search(query) == store_scan.search(query), query


def test_split_form_never_matches_across_newlines():
    from contexto.store.search import _split_form

    pat = _split_form(("refresh", "token"))
    assert pat.search(b"refresh token") is not None
    assert pat.search(b"refresh\ttoken") is not None
    assert pat.search(b"refresh_token") is not None
    assert pat.search(b"refresh\ntoken") is None
    assert pat.search(b"refresh\r\ntoken") is None


def test_cli_project_search_remains_strict_without_approx_view(tmp_path, monkeypatch, capsys):
    from contexto import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path))
    s1 = SessionStore(tmp_path, "s-cli-1")
    s1.set_workdir(repo)
    s1.page_out([turn("configured refreshToken expiry to 3600s")], 0)
    s2 = SessionStore(tmp_path, "s-cli-2")
    s2.set_workdir(repo)

    code = cli.main(["search", "refresh_token", "--session", "s-cli-2", "--project", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    # CLI --project is strict: must never return unlabelled approximate hits with empty matches.
    assert payload.get("earlier", {}).get("sessions", []) == []


def test_redact_page_messages_handles_deep_nesting_iteratively():
    from contexto.store.pages import redact_page_messages

    nested: dict = {"secret": "sk-ant-api03-1234567890abcdef1234567890"}
    cur = nested
    for _ in range(400):
        nxt: dict = {}
        cur["child"] = nxt
        cur = nxt
    cur["leaf"] = "ghp_1234567890abcdef1234567890"

    out = redact_page_messages(nested)
    assert out["secret"] == "[REDACTED]"
    node = out
    for _ in range(400):
        node = node["child"]
    assert node["leaf"] == "[REDACTED]"
