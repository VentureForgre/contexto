"""Tests for the Contexto MCP server (M2 packet B3): protocol, tools, paging, logging, stdio framing."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mcp_fakes import MCP_PATH, FakeStore, load_mcp, page

mcp = load_mcp()
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
          "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
          "io.modelcontextprotocol/clientCapabilities": {}}

LONG = "x" * (mcp.RECALL_LIMIT + 1_234)


def make_store() -> FakeStore:
    return FakeStore({
        "pg_000000000000aaaa": page("pg_000000000000aaaa", 3, [
            {"role": "user", "content": [{"type": "text", "text": "Remember: the staging port is garnet-4821."}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "SECRET-THOUGHT", "signature": "sig"},
                {"type": "text", "text": "Noted."},
                {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "conf/app.toml"}},
            ]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                          "content": [{"type": "text", "text": "port = 4821"}]}]},
        ], user_text="Remember: the staging port is garnet-4821.", tools=["read_file"], paths=["conf/app.toml"]),
        "pg_000000000000bbbb": page("pg_000000000000bbbb", 7, [
            {"role": "user", "content": [{"type": "text", "text": LONG}]},
        ], user_text="long page"),
    })


def call(server, method, params=None, msg_id=1):
    line = json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
    return server.handle_line(line)


def tool(server, name, arguments, meta=None):
    params = {"name": name, "arguments": arguments}
    if meta:
        params["_meta"] = meta
    return call(server, "tools/call", params)


@pytest.fixture
def server():
    return mcp.Server(make_store(), io.StringIO(), io.StringIO())


# Protocol


def test_legacy_initialize_negotiates_version(server):
    result = call(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "claude-code", "version": "2.1.301"}})["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "contexto"
    newer = call(server, "initialize", {"protocolVersion": "2099-01-01"})["result"]
    assert newer["protocolVersion"] == mcp.LEGACY_VERSIONS[0]


def test_modern_discover_and_version_check(server):
    result = call(server, "server/discover", {"_meta": MODERN})["result"]
    assert result["resultType"] == "complete" and result["supportedVersions"] == ["2026-07-28"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "contexto"
    bad = call(server, "tools/list", {"_meta": {**MODERN, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}})
    assert bad["error"]["code"] == mcp.UNSUPPORTED_VERSION
    assert bad["error"]["data"]["requested"] == "1900-01-01" and "2026-07-28" in bad["error"]["data"]["supported"]
    listed = call(server, "tools/list", {"_meta": MODERN})["result"]
    assert listed["resultType"] == "complete"


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


def test_notifications_and_client_responses_get_no_reply(server):
    assert server.handle_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None
    assert server.handle_line(json.dumps({"jsonrpc": "2.0", "method": "ping"})) is None
    assert server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 9, "result": {}})) is None
    assert call(server, "ping")["result"] == {}


# Tools


def test_recall_renders_the_page_without_thinking(server):
    result = tool(server, "contexto_recall", {"page_id": "pg_000000000000aaaa"})["result"]
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "SECRET-THOUGHT" not in text
    assert "[turn 3] user:\nRemember: the staging port is garnet-4821." in text
    assert 'tool_use read_file {"path": "conf/app.toml"}' in text
    assert "tool_result:\nport = 4821" in text
    assert result["structuredContent"]["next_offset"] is None
    assert len(result["content"]) == 1


def test_recall_pages_at_the_32000_boundary(server):
    first = tool(server, "contexto_recall", {"page_id": "pg_000000000000bbbb"})["result"]
    total = first["structuredContent"]["total_chars"]
    assert len(first["content"][0]["text"]) == mcp.RECALL_LIMIT
    assert first["structuredContent"]["next_offset"] == mcp.RECALL_LIMIT
    assert "offset 32000" in first["content"][1]["text"]
    second = tool(server, "contexto_recall", {"page_id": "pg_000000000000bbbb", "offset": mcp.RECALL_LIMIT})["result"]
    assert second["structuredContent"]["next_offset"] is None
    assert len(second["content"][0]["text"]) == total - mcp.RECALL_LIMIT
    whole = mcp.render_page(make_store().get_page("pg_000000000000bbbb"))
    assert first["content"][0]["text"] + second["content"][0]["text"] == whole
    past = tool(server, "contexto_recall", {"page_id": "pg_000000000000bbbb", "offset": total + 1})["result"]
    assert past["isError"] is True


def test_recall_exactly_at_the_limit_has_no_continuation():
    store = FakeStore({"pg_x": page("pg_x", 1, [{"role": "user", "content": "y"}])})
    exact = mcp.RECALL_LIMIT - len("[turn 1] user:\n")
    store.pages["pg_x"]["messages"][0]["content"] = "y" * exact
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    result = tool(server, "contexto_recall", {"page_id": "pg_x"})["result"]
    assert len(result["content"][0]["text"]) == mcp.RECALL_LIMIT
    assert result["structuredContent"]["next_offset"] is None and len(result["content"]) == 1


@pytest.mark.parametrize("args", [{"page_id": "pg_ffffffffffffffff"}, {"page_id": ""}, {},
                                  {"page_id": "pg_000000000000aaaa", "offset": -1},
                                  {"page_id": "pg_000000000000aaaa", "offset": True},
                                  {"page_id": "pg_000000000000aaaa", "extra": 1}])
def test_recall_argument_and_unknown_id_errors_are_tool_errors(server, args):
    result = tool(server, "contexto_recall", args)["result"]
    assert result["isError"] is True and result["content"][0]["text"]


def test_pin_close_and_state(server):
    pinned = tool(server, "contexto_pin", {"kind": "obligation", "text": "rotate the staging key"})["result"]
    assert pinned["isError"] is False
    oid = pinned["structuredContent"]["record_id"]
    state = tool(server, "contexto_state", {})["result"]["content"][0]["text"]
    assert oid in state and "rotate the staging key" in state
    closed = tool(server, "contexto_close", {"obligation_id": oid, "note": "rotated"})["result"]
    assert closed["isError"] is False
    again = tool(server, "contexto_close", {"obligation_id": oid, "note": "twice"})["result"]
    assert again["isError"] is True and "not an open obligation" in again["content"][0]["text"]
    assert oid not in tool(server, "contexto_state", {})["result"]["content"][0]["text"]


@pytest.mark.parametrize("args,needle", [
    ({"kind": "closure", "text": "x"}, "kind must be one of"),
    ({"kind": "decision", "text": ""}, "text is required"),
    ({"kind": "decision", "text": "x", "epistemic": "certain"}, "epistemic must be one of"),
    ({"kind": "decision", "text": "x", "epistemic": "verified"}, "needs provenance"),
    ({"kind": "decision", "text": "x", "supersedes": "rec_1"}, "supersedes must be a list"),
    ({"kind": "decision", "text": "x", "provenance": [{"type": "page"}]}, "provenance must be a list"),
])
def test_pin_validation_errors_are_tool_errors(server, args, needle):
    result = tool(server, "contexto_pin", args)["result"]
    assert result["isError"] is True and needle in result["content"][0]["text"]


def test_pin_passes_keyword_arguments_to_the_store():
    store = make_store()
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    prov = [{"type": "page", "locator": "pg_000000000000aaaa", "sha256": None, "span": None}]
    result = tool(server, "contexto_pin", {"kind": "decision", "text": "use port garnet-4821",
                                           "epistemic": "verified", "provenance": prov})["result"]
    assert result["isError"] is False and store.records[-1]["kind"] == "decision"


def test_search_is_exact_substring_newest_first(server):
    hit = tool(server, "contexto_search", {"query": "garnet-4821"})["result"]
    assert hit["structuredContent"]["page_ids"] == ["pg_000000000000aaaa"]
    assert "pg_000000000000aaaa (turn 3): Remember: the staging port is garnet-4821." in hit["content"][0]["text"]
    assert tool(server, "contexto_search", {"query": "GARNET"})["result"]["structuredContent"]["page_ids"] == []
    both = tool(server, "contexto_search", {"query": "x"})["result"]["structuredContent"]["page_ids"]
    assert both[0] == "pg_000000000000bbbb"  # turn 7 before turn 3
    assert tool(server, "contexto_search", {"query": ""})["result"]["isError"] is True


def test_every_call_is_logged_without_text_content():
    store = make_store()
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    tool(server, "contexto_recall", {"page_id": "pg_000000000000aaaa"})
    tool(server, "contexto_pin", {"kind": "decision", "text": "TOP-SECRET-DECISION"})
    tool(server, "contexto_search", {"query": "garnet-4821"})
    tool(server, "contexto_recall", {"page_id": "pg_missing"})
    assert [e["tool"] for e in store.events] == ["contexto_recall", "contexto_pin", "contexto_search", "contexto_recall"]
    assert store.events[0]["page_id"] == "pg_000000000000aaaa" and store.events[0]["ok"] is True
    assert store.events[-1]["ok"] is False
    dumped = json.dumps(store.events)
    for secret in ("TOP-SECRET-DECISION", "garnet-4821", "port = 4821"):
        assert secret not in dumped


# Stdio round trip in a real subprocess

SERVER_SCRIPT = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, {tests!r})
    from mcp_fakes import FakeStore, load_mcp
    pages = json.load(open({pages!r}))
    mcp = load_mcp()
    sys.stderr.write("diagnostics go here\\n")
    sys.exit(mcp.serve_stdio(FakeStore(pages)))
""")


def run_server(tmp_path: Path, lines: list[str]) -> subprocess.CompletedProcess:
    pages = tmp_path / "pages.json"
    pages.write_text(json.dumps(make_store().pages))
    script = SERVER_SCRIPT.format(tests=str(Path(__file__).resolve().parent), pages=str(pages))
    return subprocess.run([sys.executable, "-c", script], input="".join(line + "\n" for line in lines),
                          capture_output=True, text=True, timeout=30)


def test_stdio_round_trip_and_stdout_carries_only_protocol_frames(tmp_path):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                                                         "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "contexto_recall", "arguments": {"page_id": "pg_000000000000aaaa"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "contexto_pin", "arguments": {"kind": "obligation", "text": "ship it"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "contexto_state", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "contexto_search", "arguments": {"query": "port"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "contexto_close", "arguments": {"obligation_id": "rec_x", "note": "n"}}},
        {"jsonrpc": "2.0", "id": 8, "method": "server/discover", "params": {"_meta": MODERN}},
    ]
    lines = [json.dumps(r) for r in requests] + ["{broken", ""]
    proc = run_server(tmp_path, lines)
    assert proc.returncode == 0  # end of input shuts it down cleanly
    out_lines = proc.stdout.splitlines()
    frames = [json.loads(line) for line in out_lines]  # every stdout line is a JSON-RPC frame
    assert all(f["jsonrpc"] == "2.0" and "id" in f and ("result" in f) != ("error" in f) for f in frames)
    by_id = {f["id"]: f for f in frames}
    assert sorted(i for i in by_id if i is not None) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert by_id[None]["error"]["code"] == mcp.PARSE_ERROR
    assert by_id[3]["result"]["isError"] is False and "garnet-4821" in by_id[3]["result"]["content"][0]["text"]
    assert "ship it" in by_id[5]["result"]["content"][0]["text"]
    assert by_id[6]["result"]["structuredContent"]["page_ids"] == ["pg_000000000000aaaa"]
    assert by_id[7]["result"]["isError"] is True
    assert by_id[8]["result"]["resultType"] == "complete"
    assert "diagnostics go here" in proc.stderr and "diagnostics" not in proc.stdout


def test_module_imports_only_the_standard_library():
    source = MCP_PATH.read_text()
    imports = [line.split()[1].split(".")[0] for line in source.splitlines()
               if line.startswith(("import ", "from ")) and not line.startswith("from __future__")]
    assert set(imports) <= {"json", "sys", "collections", "typing"}
