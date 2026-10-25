"""Tests for the Contexto MCP server: protocol, tools, paging, logging, stdio framing.

Everything runs against the real stores: MemoryStore in process, SessionStore through the CLI.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from contexto import mcp
from contexto.canonical import MAX_JSON_DEPTH
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore

MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
          "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
          "io.modelcontextprotocol/clientCapabilities": {}}

TURN_A = [
    {"role": "user", "content": [{"type": "text", "text": "Remember: the staging port is garnet-4821."}]},
    {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "SECRET-THOUGHT", "signature": "sig"},
        {"type": "text", "text": "Noted."},
        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "conf/app.toml"}},
    ]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                  "content": [{"type": "text", "text": "port = 4821"}]}]},
]
LONG_TEXT = "x" * (mcp.RECALL_LIMIT + 1_234)
TURN_B = [{"role": "user", "content": [{"type": "text", "text": LONG_TEXT}]}]

BEST = mcp.SEARCH_BEST_HEADER + "\n"  # the first line of a reply with paged hits


def make_store() -> tuple[MemoryStore, str, str]:
    store = MemoryStore("s1")
    a, b = store.page_out([TURN_A, TURN_B], first_turn=3)
    return store, a, b


def call(server, method, params=None, msg_id=1):
    line = json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
    return server.handle_line(line)


def tool(server, name, arguments, meta=None):
    params = {"name": name, "arguments": arguments}
    if meta:
        params["_meta"] = meta
    return call(server, "tools/call", params)


@pytest.fixture
def stores():
    return make_store()


@pytest.fixture
def server(stores):
    return mcp.Server(stores[0], io.StringIO(), io.StringIO())


# Protocol


def test_legacy_initialize_negotiates_version(server):
    result = call(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "claude-code", "version": "2.1.301"}})["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "contexto"
    assert (call(server, "initialize", {"protocolVersion": "2099-01-01"})["result"]["protocolVersion"]
            == mcp.LEGACY_VERSIONS[0])


def test_modern_discover_and_version_check(server):
    result = call(server, "server/discover", {"_meta": MODERN})["result"]
    assert result["resultType"] == "complete" and result["supportedVersions"] == ["2026-07-28"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "contexto"
    bad = call(server, "tools/list", {"_meta": {**MODERN, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}})
    assert bad["error"]["code"] == mcp.UNSUPPORTED_VERSION
    assert bad["error"]["data"]["requested"] == "1900-01-01" and "2026-07-28" in bad["error"]["data"]["supported"]


def test_modern_cacheable_results_carry_ttl_and_scope(server):
    # Claude Code 2.1.301 rejects a modern tools/list result without these (PR #24 review B1).
    listed = call(server, "tools/list", {"_meta": MODERN})["result"]
    assert listed["ttlMs"] == 0 and listed["cacheScope"] == "private"
    discovered = call(server, "server/discover", {"_meta": MODERN})["result"]
    assert discovered["ttlMs"] == 0 and discovered["cacheScope"] == "private"
    called = tool(server, "contexto_state", {}, meta=MODERN)["result"]
    assert called["resultType"] == "complete" and "ttlMs" not in called and called["isError"] is False
    legacy = call(server, "tools/list")["result"]
    assert "ttlMs" not in legacy and "resultType" not in legacy


def test_tools_list_is_the_contract_set_in_stable_order(server):
    tools = call(server, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["contexto_recall", "contexto_pin", "contexto_close",
                                          "contexto_state", "contexto_search"]
    assert all(t["inputSchema"]["type"] == "object" for t in tools)
    assert call(server, "tools/list")["result"]["tools"] == tools


@pytest.mark.parametrize("line,code", [
    ("{not json", mcp.PARSE_ERROR),
    (json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]), mcp.INVALID_REQUEST),
    (json.dumps({"jsonrpc": "1.0", "id": 1, "method": "ping"}), mcp.INVALID_REQUEST),
    (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "no/such"}), mcp.METHOD_NOT_FOUND),
    (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}}), mcp.INVALID_PARAMS),
    (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}), mcp.INVALID_PARAMS),
])
def test_malformed_input_and_unknowns_are_protocol_errors(server, line, code):
    assert server.handle_line(line)["error"]["code"] == code


def test_a_line_too_deep_for_json_is_a_parse_error_and_the_server_keeps_serving(server):
    deep = '{"jsonrpc":"2.0","id":1,"method":"ping","params":' + "[" * 1_000_000 + "]" * 1_000_000 + "}"
    out = io.StringIO()
    server.stdout = out
    assert server.serve([deep + "\n", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"]) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == mcp.PARSE_ERROR and replies[0]["id"] is None
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_an_id_nested_past_the_limit_is_a_parse_error_with_a_null_id(server):
    """From Python 3.14 json.loads parses deeper than json.dumps writes; a line nested past
    MAX_JSON_DEPTH is refused at the parse, so a reply never echoes a value it cannot write."""
    line = '{"jsonrpc":"2.0","method":"ping","id":' + "[" * MAX_JSON_DEPTH + "]" * MAX_JSON_DEPTH + "}"
    assert server.handle_line(line) == {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": mcp.PARSE_ERROR, "message": "Parse error"}}
    ok = '{"jsonrpc":"2.0","method":"ping","id":1,"params":{"_meta":' + "[" * 500 + "]" * 500 + "}}"
    assert server.handle_line(ok) == {"jsonrpc": "2.0", "id": 1, "result": {}}  # 502 levels: read


def test_a_reply_that_cannot_be_written_becomes_an_internal_error_and_serving_continues(server, monkeypatch):
    out = io.StringIO()
    server.stdout = out
    real = server.handle_line
    monkeypatch.setattr(server, "handle_line", lambda line: {"jsonrpc": "2.0", "id": {1, 2}, "result": {}}
                        if '"id":1' in line else real(line))
    ping = '{"jsonrpc":"2.0","method":"ping","id":%d}\n'
    assert server.serve([ping % 1, ping % 2]) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    internal = {"code": mcp.INTERNAL_ERROR, "message": "Internal error"}
    assert replies == [{"jsonrpc": "2.0", "id": None, "error": internal}, {"jsonrpc": "2.0", "id": 2, "result": {}}]


def test_notifications_and_client_responses_get_no_reply(server):
    assert server.handle_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None
    assert server.handle_line(json.dumps({"jsonrpc": "2.0", "method": "ping"})) is None
    assert server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 9, "result": {}})) is None
    assert call(server, "ping")["result"] == {}


# Recall and search


def test_recall_renders_the_page_without_thinking(server, stores):
    _, a, _ = stores
    result = tool(server, "contexto_recall", {"page_id": a})["result"]
    text = result["content"][0]["text"]
    assert result["isError"] is False and "structuredContent" not in result
    assert "SECRET-THOUGHT" not in text
    assert "[turn 3] user:\nRemember: the staging port is garnet-4821." in text
    assert 'tool_use read_file {"path": "conf/app.toml"}' in text
    assert "tool_result:\nport = 4821" in text
    assert len(result["content"]) == 1  # no continuation hint: the page ended


def test_recall_pages_at_the_32000_boundary(server, stores):
    store, _, b = stores
    store.set_window(200_000, 2.0)  # a large window, so the 32,000-character cap applies (B9)
    first = tool(server, "contexto_recall", {"page_id": b})["result"]
    total = len(mcp.render_page(store.get_page(b)))
    assert len(first["content"][0]["text"]) == mcp.RECALL_LIMIT
    assert "offset 32000" in first["content"][1]["text"]
    second = tool(server, "contexto_recall", {"page_id": b, "offset": mcp.RECALL_LIMIT})["result"]
    assert len(second["content"]) == 1
    assert first["content"][0]["text"] + second["content"][0]["text"] == mcp.render_page(store.get_page(b))
    past = tool(server, "contexto_recall", {"page_id": b, "offset": total + 1})["result"]
    assert past["isError"] is True


def test_recall_exactly_at_the_limit_has_no_continuation():
    store = MemoryStore("s2")
    store.set_window(200_000, 2.0)  # a large window, so the 32,000-character cap applies (B9)
    exact = mcp.RECALL_LIMIT - len("[turn 1] user:\n")
    (pid,) = store.page_out([[{"role": "user", "content": [{"type": "text", "text": "y" * exact}]}]], first_turn=1)
    result = tool(mcp.Server(store, io.StringIO(), io.StringIO()), "contexto_recall", {"page_id": pid})["result"]
    assert len(result["content"][0]["text"]) == mcp.RECALL_LIMIT
    assert len(result["content"]) == 1


def damage(store: SessionStore, page_id: str, depth: int = 20_000) -> None:
    """Rewrite a page file nested past MAX_JSON_DEPTH (and past json.loads on 3.12): rule 18 unreadable."""
    deep = "[" * depth + "]" * depth
    (store.pages_dir / f"{page_id}.json").write_text(f'{{"id":"{page_id}","extra":{deep}}}')


@pytest.mark.parametrize("shape", ["deep", "not a page"])
def test_an_unreadable_page_is_a_tool_error_logged_and_left_out_of_search(tmp_path, monkeypatch, shape):
    store = SessionStore(tmp_path, "s1")
    bad, good = store.page_out([TURN_A, TURN_B], first_turn=1)
    if shape == "deep":
        damage(store, bad)
    else:  # valid JSON with a page's id, turn and stub, but no list of messages
        (store.pages_dir / f"{bad}.json").write_text(json.dumps({"id": bad, "turn": 1, "stub": {}, "messages": 5}))
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    result = tool(server, "contexto_recall", {"page_id": bad})["result"]
    assert result["isError"] is True and result["content"][0]["text"] == \
        f"Page {bad} cannot be read: its stored file is damaged."
    pinned = tool(server, "contexto_pin", {"kind": "decision", "text": "keep it", "provenance": [
        {"type": "page", "locator": bad}]})["result"]
    assert pinned["isError"] is True and "cannot be read" in pinned["content"][0]["text"]
    monkeypatch.setattr(store, "search", lambda query: [bad, good])  # listed before the file was damaged
    found = tool(server, "contexto_search", {"query": "x"})["result"]
    assert found["isError"] is False and good in found["content"][0]["text"]
    log = [json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
    assert [e["path"] for e in log if e["event"] == "page_unreadable"] == [f"{bad}.json"] * 3


@pytest.mark.parametrize("args", [{"page_id": "pg_ffffffffffffffff"}, {"page_id": ""}, {},
                                  {"page_id": "PAGE", "offset": -1}, {"page_id": "PAGE", "offset": True},
                                  {"page_id": "PAGE", "extra": 1}])
def test_recall_argument_and_unknown_id_errors_are_tool_errors(server, stores, args):
    args = {k: (stores[1] if v == "PAGE" else v) for k, v in args.items()}
    result = tool(server, "contexto_recall", args)["result"]
    assert result["isError"] is True and result["content"][0]["text"]


def test_search_matches_words_case_insensitively(server, stores):
    store, a, b = stores
    hit = tool(server, "contexto_search", {"query": "garnet-4821"})["result"]
    assert "structuredContent" not in hit
    text = hit["content"][0]["text"]
    page_text = mcp.render_page(store.get_page(a))
    offset = page_text.index("Remember: the staging port is garnet-4821.")
    best, head, *lines = text.split("\n")
    assert best == mcp.SEARCH_BEST_HEADER and head == f"{a} (turn 3)"  # one hit: page id and turn, no stub (B17)
    assert f"> {offset}: Remember: the staging port is garnet-4821." in lines  # the line, at its offset
    assert all(line.startswith(("> ", "  ")) for line in lines)
    assert tool(server, "contexto_search", {"query": "GARNET-4821"})["result"]["content"][0]["text"] == text  # D34
    assert (tool(server, "contexto_search", {"query": "staging garnet-4821"})["result"]["content"][0]["text"]
            .startswith(f"{BEST}{a}"))
    assert (tool(server, "contexto_search", {"query": "garnet-4821 absent-word"})["result"]["content"][0]["text"]
            == "No paged-out turn contains every word of that query.")
    assert tool(server, "contexto_search", {"query": "x"})["result"]["content"][0]["text"].startswith(f"{BEST}{b}")
    assert tool(server, "contexto_search", {"query": ""})["result"]["isError"] is True


def test_search_says_when_results_are_cut():
    store = MemoryStore("s3")
    store.set_window(200_000, 2.0)  # a large window, so the 32,000-character cap applies (B9)
    store.page_out([[{"role": "user", "content": [{"type": "text", "text": f"needle {i}"}]}]
                    for i in range(mcp.SEARCH_LIMIT + mcp.SEARCH_MORE_LIMIT + 5)], first_turn=1)
    result = tool(mcp.Server(store, io.StringIO(), io.StringIO()), "contexto_search", {"query": "needle"})["result"]
    lines = result["content"][0]["text"].split("\n")
    assert sum(line.startswith("pg_") for line in lines) == mcp.SEARCH_LIMIT + mcp.SEARCH_MORE_LIMIT
    assert lines[-1] == "[5 more matching pages not shown; use a longer or more specific query]"


def test_render_page_survives_malformed_blocks():
    text = mcp.render_page({"turn": 2, "messages": [
        "not a message",
        {"role": "user", "content": 42},
        {"role": "assistant", "content": ["bare string", {"type": "text", "text": "fine"},
                                         {"type": "tool_result", "content": [7, {"type": "image"}]},
                                         {"type": "tool_result", "content": None}]},
    ]})
    assert "[malformed message omitted]" in text and "[malformed content omitted]" in text
    assert "[malformed block omitted]" in text and "fine" in text and "[image omitted]" in text


# Pin, close, and state on the real ledger


def test_pin_through_mcp_appears_in_pinned_text_and_state_matches(server, stores):
    store, a, _ = stores
    decision = tool(server, "contexto_pin", {"kind": "decision", "text": "use port garnet-4821",
                                             "epistemic": "verified",
                                             "provenance": [{"type": "page", "locator": a}]})["result"]
    assert decision["isError"] is False
    obligation = tool(server, "contexto_pin", {"kind": "obligation", "text": "rotate the staging key"})["result"]
    oid = obligation["structuredContent"]["record_id"]
    pinned = store.pinned_text(6_000)
    assert "use port garnet-4821" in pinned and "rotate the staging key" in pinned
    assert decision["structuredContent"]["record_id"] in pinned and oid in pinned
    state = tool(server, "contexto_state", {})["result"]["content"][0]["text"]
    assert state == store.state_text()
    closed = tool(server, "contexto_close", {"obligation_id": oid, "note": "rotated"})["result"]
    assert closed["isError"] is False
    again = tool(server, "contexto_close", {"obligation_id": oid, "note": "twice"})["result"]
    assert again["isError"] is True and "not an open obligation" in again["content"][0]["text"]
    assert "rotate the staging key" not in store.pinned_text(6_000)
    assert tool(server, "contexto_state", {})["result"]["content"][0]["text"] == store.state_text()


def test_supersession_through_mcp_replaces_the_pinned_decision(server, stores):
    store = stores[0]
    first = tool(server, "contexto_pin", {"kind": "decision", "text": "queue backend is basalt"})["result"]
    rid = first["structuredContent"]["record_id"]
    tool(server, "contexto_pin", {"kind": "decision", "text": "queue backend is harbor", "supersedes": [rid]})
    pinned = store.pinned_text(6_000)
    assert "queue backend is harbor" in pinned and "queue backend is basalt" not in pinned


@pytest.mark.parametrize("args,needle", [
    ({"kind": "closure", "text": "x"}, "kind must be one of"),
    ({"kind": "decision", "text": ""}, "text is required"),
    ({"kind": "decision", "text": "x", "epistemic": "certain"}, "epistemic must be one of"),
    ({"kind": "decision", "text": "x", "epistemic": "verified"}, "Not recorded"),  # the store needs provenance
    ({"kind": "decision", "text": "x", "supersedes": "rec_1"}, "supersedes must be a list"),
    ({"kind": "decision", "text": "x", "provenance": [{"type": "page"}]}, "provenance must be a list"),
    ({"kind": "decision", "text": "x", "provenance": [{"type": "page", "locator": "pg_ffffffffffffffff"}]},
     "unknown page"),
])
def test_pin_validation_errors_are_tool_errors(server, args, needle):
    result = tool(server, "contexto_pin", args)["result"]
    assert result["isError"] is True and needle in result["content"][0]["text"]


def test_every_call_is_logged_without_text_content(server, stores):
    store, a, _ = stores
    tool(server, "contexto_recall", {"page_id": a})
    tool(server, "contexto_pin", {"kind": "decision", "text": "TOP-SECRET-DECISION"})
    tool(server, "contexto_search", {"query": "garnet-4821"})
    tool(server, "contexto_recall", {"page_id": "pg_ffffffffffffffff"})
    mcp_events = [e for e in store.events if e.get("event") == "mcp_call"]
    assert [e["tool"] for e in mcp_events] == ["contexto_recall", "contexto_pin", "contexto_search", "contexto_recall"]
    assert mcp_events[0]["page_id"] == a and mcp_events[0]["ok"] is True and mcp_events[-1]["ok"] is False
    dumped = json.dumps(mcp_events)
    for secret in ("TOP-SECRET-DECISION", "garnet-4821", "port = 4821"):
        assert secret not in dumped


# Through the CLI: python -m contexto mcp with a real SessionStore on disk


def test_contexto_mcp_cli_round_trip_and_stdout_carries_only_frames(tmp_path):
    seeded = SessionStore(tmp_path, "cli-test")
    (pid,) = seeded.page_out([TURN_A], first_turn=1)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": MODERN}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "contexto_recall", "arguments": {"page_id": pid}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "contexto_pin", "arguments": {"kind": "obligation", "text": "ship it"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "contexto_state", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "contexto_search", "arguments": {"query": "port"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "contexto_close", "arguments": {"obligation_id": "rec_x", "note": "n"}}},
    ]
    lines = "".join(json.dumps(r) + "\n" for r in requests) + "{broken\n"
    proc = subprocess.run([sys.executable, "-m", "contexto", "mcp", "--session", "cli-test", "--home", str(tmp_path)],
                          input=lines, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-400:]
    frames = [json.loads(line) for line in proc.stdout.splitlines()]  # every stdout line is a frame
    assert all(f["jsonrpc"] == "2.0" and "id" in f and ("result" in f) != ("error" in f) for f in frames)
    by_id = {f["id"]: f for f in frames}
    assert sorted(i for i in by_id if i is not None) == [1, 2, 3, 4, 5, 6, 7]
    assert by_id[None]["error"]["code"] == mcp.PARSE_ERROR
    assert by_id[2]["result"]["cacheScope"] == "private" and len(by_id[2]["result"]["tools"]) == 5
    assert "garnet-4821" in by_id[3]["result"]["content"][0]["text"]
    assert "ship it" in by_id[5]["result"]["content"][0]["text"]
    assert by_id[6]["result"]["content"][0]["text"].startswith(f"{BEST}{pid} (turn ")
    assert by_id[7]["result"]["isError"] is True
    reopened = SessionStore(tmp_path, "cli-test")  # the pin is durable in the session's ledger
    assert "ship it" in reopened.state_text()


def test_module_imports_only_the_standard_library():
    source = open(mcp.__file__, encoding="utf-8").read()
    imports = [line.split()[1].split(".")[0] for line in source.splitlines()
               if line.startswith(("import ", "from ")) and not line.startswith("from __future__")]
    # Standard library plus the contexto package itself (the shared page rendering in contexto.store.render).
    assert set(imports) <= {"json", "math", "sys", "collections", "typing", "contexto"}
