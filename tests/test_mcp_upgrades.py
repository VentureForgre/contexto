"""Tests for MCP bug fixes, robustness, schema/provenance upgrades, and fast-path startup."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from contexto import cli, mcp
from contexto.store import SessionStore
from contexto.store import project as project_module
from contexto.store.memory import MemoryStore


def call(server: mcp.Server, method: str, params=None, msg_id=1):
    line = json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
    return server.handle_line(line)


def tool(server: mcp.Server, name: str, arguments: dict, meta=None):
    params = {"name": name, "arguments": arguments}
    if meta:
        params["_meta"] = meta
    return call(server, "tools/call", params)["result"]


# 1. contexto_pin topic & owner + conflict detection + auto sha256 file provenance


def test_pin_schema_exposes_topic_and_owner_and_conflict_detection_works():
    store = MemoryStore("s1")
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    pin_schema = next(t for t in call(server, "tools/list")["result"]["tools"]
                      if t["name"] == "contexto_pin")["inputSchema"]
    assert "topic" in pin_schema["properties"]
    assert "owner" in pin_schema["properties"]

    r1 = tool(server, "contexto_pin", {"kind": "decision", "text": "Use Postgres 16", "topic": "database"})
    r2 = tool(server, "contexto_pin", {"kind": "decision", "text": "Use SQLite 3", "topic": "database"})
    assert r1["isError"] is False and r2["isError"] is False
    state = store.state()
    assert len(state.conflicts) == 1
    assert state.conflicts[0].topic == "database"
    assert set(state.conflicts[0].record_ids) == {
        r1["structuredContent"]["record_id"],
        r2["structuredContent"]["record_id"],
    }
    state_text = tool(server, "contexto_state", {})["content"][0]["text"]
    assert "CONFLICT" in state_text or "conflict" in state_text.lower()

    # Obligation with explicit owner
    ob = tool(server, "contexto_pin", {"kind": "obligation", "text": "Deploy staging", "owner": "user"})
    assert ob["isError"] is False
    ob_rec = next(r for r in store.state().open_obligations if r.id == ob["structuredContent"]["record_id"])
    assert ob_rec.owner == "user"

    # Owner on non-obligation is refused cleanly
    bad = tool(server, "contexto_pin", {"kind": "decision", "text": "nope", "owner": "user"})
    assert bad["isError"] is True and "owner" in bad["content"][0]["text"]


def test_pin_auto_computes_sha256_for_file_provenance(tmp_path):
    workdir = tmp_path / "repo"
    (workdir / "src").mkdir(parents=True)
    target = workdir / "src" / "config.py"
    target.write_text("PORT = 8080\n", encoding="utf-8")
    expected_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    store = SessionStore(tmp_path / "home", "s1")
    store.set_workdir(workdir)
    server = mcp.Server(store, io.StringIO(), io.StringIO())

    res = tool(server, "contexto_pin", {
        "kind": "artifact",
        "text": "Config port is 8080",
        "epistemic": "verified",
        "provenance": [{"type": "file", "locator": "src/config.py"}],
    })
    assert res["isError"] is False
    rid = res["structuredContent"]["record_id"]
    rec = next(r for r in store.ledger.records() if r.id == rid)
    assert rec.provenance[0].sha256 == expected_sha
    assert rid not in store.state(workdir).stale

    # Modifying the file makes the auto-hashed record stale!
    target.write_text("PORT = 9090\n", encoding="utf-8")
    assert rid in store.state(workdir).stale

    # Missing file or path escape still fails validation
    missing = tool(server, "contexto_pin", {
        "kind": "artifact", "text": "missing", "epistemic": "verified",
        "provenance": [{"type": "file", "locator": "src/does_not_exist.py"}],
    })
    assert missing["isError"] is True

    escape = tool(server, "contexto_pin", {
        "kind": "artifact", "text": "escape", "epistemic": "verified",
        "provenance": [{"type": "file", "locator": "../outside.txt"}],
    })
    assert escape["isError"] is True


# 2. MCP server robustness


def test_broken_pipe_on_stdout_exits_cleanly():
    class BrokenStdout(io.StringIO):
        def write(self, s):
            raise BrokenPipeError("pipe closed")

    server = mcp.Server(MemoryStore("s1"), BrokenStdout(), io.StringIO())
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
    assert server.serve([ping, ping]) == 0


def test_max_stdin_line_size_cap_rejects_oversized_line_and_keeps_serving():
    out = io.StringIO()
    server = mcp.Server(MemoryStore("s1"), out, io.StringIO())
    server.max_line_chars = 256
    oversized = '{"jsonrpc":"2.0","id":1,"method":"ping","pad":"' + ("a" * 1000) + '"}\n'
    ok = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
    assert server.serve(io.StringIO(oversized + ok)) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == mcp.INVALID_REQUEST
    assert "exceeds" in replies[0]["error"]["message"]
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_notifications_cancelled_handled_cleanly():
    stderr = io.StringIO()
    server = mcp.Server(MemoryStore("s1"), io.StringIO(), stderr)
    cancelled = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 42, "reason": "user aborted"},
    })
    assert server.handle_line(cancelled) is None
    assert "cancelled request 42" in stderr.getvalue()
    assert call(server, "ping")["result"] == {}


@pytest.mark.parametrize("version,expect_structured", [
    ("2024-11-05", False),
    ("2025-03-26", False),
    ("2025-06-18", True),
    ("2025-11-25", True),
])
def test_structured_content_only_emitted_for_2025_06_18_and_newer(version, expect_structured):
    store = MemoryStore("s1")
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    init = call(server, "initialize", {"protocolVersion": version})["result"]
    assert init["protocolVersion"] == version
    res = tool(server, "contexto_pin", {"kind": "decision", "text": "test version"})
    assert res["isError"] is False
    assert ("structuredContent" in res) is expect_structured


def test_protocol_version_negotiation_accepts_future_modern_and_legacy_meta():
    server = mcp.Server(MemoryStore("s1"), io.StringIO(), io.StringIO())
    # Future modern protocol version date >= 2026-07-28 succeeds
    future = call(server, "tools/list", {"_meta": {mcp.META_VERSION: "2026-10-01"}})
    assert "result" in future and future["result"]["resultType"] == "complete"
    # Legacy version carried in _meta also succeeds instead of hard-failing
    legacy_meta = call(server, "tools/list", {"_meta": {mcp.META_VERSION: "2025-11-25"}})
    assert "result" in legacy_meta and "resultType" not in legacy_meta["result"]
    # Ancient/invalid version still returns UNSUPPORTED_VERSION
    ancient = call(server, "tools/list", {"_meta": {mcp.META_VERSION: "1900-01-01"}})
    assert ancient["error"]["code"] == mcp.UNSUPPORTED_VERSION


# 3. MCP fast-path startup


def test_cli_mcp_bypasses_build_parser(tmp_path, monkeypatch):
    SessionStore(tmp_path, "s1").release()
    monkeypatch.setattr(cli, "build_parser", lambda: (_ for _ in ()).throw(AssertionError("build_parser called!")))
    called = []
    monkeypatch.setattr("contexto.mcp.serve_stdio", lambda store: called.append(store.session_id) or 0)
    assert cli.main(["mcp", "--session", "s1", "--home", str(tmp_path)]) == 0
    assert called == ["s1"]


def test_mcp_startup_and_initialize_do_not_scan_historical_sessions(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    home = tmp_path / "home"
    for i in range(15):
        s = SessionStore(home, f"hist-{i:02d}")
        s.set_workdir(root)
        s.release()
    cur = SessionStore(home, "cur")
    cur.set_workdir(root)

    def fail_scan(*args, **kwargs):
        raise AssertionError("project_sessions full-home scan should not run on MCP startup/initialize!")

    monkeypatch.setattr(project_module, "project_sessions", fail_scan)
    server = mcp.Server(cur, io.StringIO(), io.StringIO())
    assert server.project_tools is True
    init = call(server, "initialize", {"protocolVersion": "2025-11-25"})["result"]
    assert init["serverInfo"]["name"] == "contexto"
    listed = call(server, "tools/list")["result"]["tools"]
    assert any("scope" in t["inputSchema"]["properties"] for t in listed if t["name"] == "contexto_search")
    assert isinstance(Path(cur.dir), Path)
