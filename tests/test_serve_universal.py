"""End-to-end tests for universal `contexto serve`: multi-upstream provider routing, pluggable
session routing chain, workdir/project-memory/stats parity, opt-in --allow-host & local Origin,
Streamable HTTP MCP, and non-Claude stdio MCP fallback."""

from __future__ import annotations

import http.client
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import cli, mcp, serve, servecontract, stats
from contexto.commands import mcp as mcp_command
from contexto.store import SessionStore
from contexto.upstream import Upstream as UpstreamChoice


class RecordingUpstream:
    """Minimal HTTP upstream returning canned responses per request."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.received: list[tuple[str, str, dict[str, str], bytes]] = []
        self.next_body: bytes | None = None
        parent = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parent.received.append((self.command, self.path, dict(self.headers.items()), body))
                payload = parent.next_body
                if payload is None:
                    if "/chat/completions" in self.path:
                        payload = json.dumps({
                            "id": "chatcmpl-1",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"from {parent.tag}"},
                                         "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 120, "completion_tokens": 15,
                                      "prompt_tokens_details": {"cached_tokens": 40}},
                        }).encode()
                    elif ":generateContent" in self.path:
                        payload = json.dumps({
                            "candidates": [{"content": {"role": "model", "parts": [{"text": f"from {parent.tag}"}]},
                                            "finishReason": "STOP"}],
                            "usageMetadata": {"promptTokenCount": 140, "candidatesTokenCount": 20,
                                              "cachedContentTokenCount": 50},
                        }).encode()
                    elif self.path.endswith("/responses"):
                        payload = json.dumps({
                            "id": "resp_1", "status": "completed", "output": [],
                            "usage": {"input_tokens": 110, "output_tokens": 12,
                                      "input_tokens_details": {"cached_tokens": 30}},
                        }).encode()
                    else:
                        payload = json.dumps({
                            "id": "msg_1", "type": "message", "role": "assistant",
                            "content": [{"type": "text", "text": f"from {parent.tag}"}],
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 100, "cache_read_input_tokens": 20,
                                      "cache_creation_input_tokens": 0, "output_tokens": 10},
                        }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _http(port: int, method: str, target: str, body: bytes = b"", headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
    head = dict(headers or {})
    head.setdefault("Host", f"127.0.0.1:{port}")
    for k, v in head.items():
        conn.putheader(k, v)
    if body or method == "POST":
        conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body or None)
    resp = conn.getresponse()
    data = resp.read()
    resp_headers = dict(resp.getheaders())
    conn.close()
    return resp.status, resp_headers, data


def _settle(service: serve.ServeService) -> None:
    with service._idle:
        while service._in_flight:
            service._idle.wait(0.05)


@pytest.fixture
def multi_upstreams():
    anth = RecordingUpstream("anthropic")
    oai = RecordingUpstream("openai")
    gem = RecordingUpstream("gemini")
    yield anth, oai, gem
    anth.stop()
    oai.stop()
    gem.stop()


def test_multi_upstream_provider_routing_and_backward_compat(tmp_path, multi_upstreams):
    anth, oai, gem = multi_upstreams
    home = tmp_path / "home"
    token = serve.ensure_token(home)
    service = serve.ServeService(
        home,
        UpstreamChoice(anth.url, "flag"),
        token,
        reaper=False,
        upstreams={
            "anthropic": UpstreamChoice(anth.url, "flag:anthropic"),
            "openai": UpstreamChoice(oai.url, "flag:openai"),
            "gemini": UpstreamChoice(gem.url, "flag:gemini"),
        },
    )
    service.start(0)
    try:
        # 1. Backward-compatible /<token>/v1/messages
        msg_body = json.dumps({"model": "claude-haiku-5-5", "max_tokens": 50,
                               "messages": [{"role": "user", "content": "hi claude"}]}).encode()
        st, _, data = _http(service.port, "POST", f"/{token}/v1/messages", msg_body,
                            {"Content-Type": "application/json", "X-Claude-Code-Session-Id": "sess-claude-1"})
        assert st == 200 and b"from anthropic" in data
        assert anth.received[-1][1] == "/v1/messages"

        # 2. /<token>/openai/v1/chat/completions strips /openai and routes to OpenAI upstream
        chat_body = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "hi openai"}]}).encode()
        st, _, data = _http(service.port, "POST", f"/{token}/openai/v1/chat/completions", chat_body,
                            {"Content-Type": "application/json"})
        assert st == 200 and b"from openai" in data
        assert oai.received[-1][1] == "/v1/chat/completions"

        # 3. /<token>/gemini/v1beta/models/gemini-2.5-pro:generateContent strips /gemini and routes to Gemini upstream
        gem_body = json.dumps({"contents": [{"role": "user", "parts": [{"text": "hi gemini"}]}]}).encode()
        st, _, data = _http(service.port, "POST", f"/{token}/gemini/v1beta/models/gemini-2.5-pro:generateContent",
                            gem_body, {"Content-Type": "application/json"})
        assert st == 200 and b"from gemini" in data
        assert gem.received[-1][1] == "/v1beta/models/gemini-2.5-pro:generateContent"

        # 4. /<token>/anthropic/v1/messages strips /anthropic and routes to Anthropic upstream
        st, _, data = _http(service.port, "POST", f"/{token}/anthropic/v1/messages", msg_body,
                            {"Content-Type": "application/json", "X-Contexto-Session": "sess-anth-prefix"})
        assert st == 200 and b"from anthropic" in data
        assert anth.received[-1][1] == "/v1/messages"
        # Internal header stripped before forwarding upstream
        assert "X-Contexto-Session" not in anth.received[-1][2]
    finally:
        service.stop()


def test_pluggable_session_routing_chain(tmp_path, multi_upstreams):
    anth, oai, gem = multi_upstreams
    home = tmp_path / "home"
    token = serve.ensure_token(home)
    service = serve.ServeService(
        home, UpstreamChoice(anth.url, "flag"), token, reaper=False,
        upstreams={"openai": UpstreamChoice(oai.url, "flag:openai"), "gemini": UpstreamChoice(gem.url, "flag:gemini")},
    )
    service.start(0)
    try:
        # 1. Explicit URL segment /<token>/s/<session_id>/...
        req = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "turn 1"}]}).encode()
        st, _, _ = _http(service.port, "POST", f"/{token}/s/url-sess-1/openai/v1/chat/completions", req,
                         {"Content-Type": "application/json"})
        assert st == 200
        _settle(service)
        sid_url = service.resolve_session_key("url-sess-1")
        assert sid_url is not None

        # 2. Codex prompt_cache_key in body & session-id header
        resp_req = json.dumps({"model": "gpt-5", "prompt_cache_key": "codex-cache-key-1",
                               "input": [{"type": "message", "role": "user",
                                          "content": [{"type": "input_text", "text": "hello codex"}]}]}).encode()
        st, _, _ = _http(service.port, "POST", f"/{token}/openai/v1/responses", resp_req,
                         {"Content-Type": "application/json"})
        assert st == 200
        _settle(service)
        assert service.resolve_session_key("codex-cache-key-1") is not None

        # 3. Deterministic content-prefix fingerprint fallback across turns for stateless Chat Completions & Gemini
        t1 = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "stateless topic A"}]}).encode()
        t2 = json.dumps({"model": "gpt-4o", "messages": [
            {"role": "user", "content": "stateless topic A"},
            {"role": "assistant", "content": "reply A"},
            {"role": "user", "content": "follow-up A"},
        ]}).encode()
        _http(service.port, "POST", f"/{token}/openai/v1/chat/completions", t1, {"Content-Type": "application/json"})
        _settle(service)
        count_after_t1 = len(service.active_session_ids())
        _http(service.port, "POST", f"/{token}/openai/v1/chat/completions", t2, {"Content-Type": "application/json"})
        _settle(service)
        # Same first user turn -> same fingerprint -> reused the same session!
        assert len(service.active_session_ids()) == count_after_t1

        # Different first user turn -> new session
        other = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "stateless topic B"}]}).encode()
        _http(service.port, "POST", f"/{token}/openai/v1/chat/completions", other, {"Content-Type": "application/json"})
        _settle(service)
        assert len(service.active_session_ids()) == count_after_t1 + 1
    finally:
        service.stop()


def test_workdir_project_memory_and_stats_parity(tmp_path, multi_upstreams):
    anth, oai, _ = multi_upstreams
    home = tmp_path / "home"
    project = tmp_path / "my-project"
    project.mkdir()
    (project / ".git").mkdir()
    token = serve.ensure_token(home)
    service = serve.ServeService(
        home, UpstreamChoice(anth.url, "flag"), token, config={"bound": 80_000}, reaper=False,
        upstreams={"openai": UpstreamChoice(oai.url, "flag:openai")},
    )
    service.start(0)
    try:
        # Via X-Contexto-Workdir header
        req = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "workdir test"}]}).encode()
        st, _, _ = _http(service.port, "POST", f"/{token}/s/wd-header/openai/v1/chat/completions", req,
                         {"Content-Type": "application/json", "X-Contexto-Workdir": str(project)})
        assert st == 200
        assert "X-Contexto-Workdir" not in oai.received[-1][2]  # stripped before upstream
        _settle(service)
        sid1 = service.resolve_session_key("wd-header")
        assert sid1 is not None

        # Via /w/<encoded>/ URL prefix
        encoded = servecontract.encode_workdir_segment(str(project))
        st, _, _ = _http(service.port, "POST", f"/{token}/w/{encoded}/s/wd-url/openai/v1/chat/completions", req,
                         {"Content-Type": "application/json"})
        assert st == 200
        assert oai.received[-1][1] == "/v1/chat/completions"
        _settle(service)
        sid2 = service.resolve_session_key("wd-url")
        assert sid2 is not None
    finally:
        service.stop()

    # Both sessions recorded workdir and kept project memory enabled
    for sid in (sid1, sid2):
        meta = json.loads((home / "sessions" / sid / "meta.json").read_text())
        assert meta["workdir"] == str(project)
        assert meta.get("project_memory") is not False

    # contexto stats on a serve session reports bound, cli, runs, and usage!
    st_out = stats.session_stats(home, sid1)
    assert st_out["bound"] == 80_000
    assert st_out["cli"] == "openai"
    assert st_out["runs"] == 1
    assert st_out["requests"]["model"] == 1
    assert st_out["prompt"]["peak_tokens"] == 120


def test_allow_host_and_local_origin_opt_in(tmp_path, multi_upstreams):
    anth, _, _ = multi_upstreams
    home = tmp_path / "home"
    token = serve.ensure_token(home)
    service = serve.ServeService(
        home, UpstreamChoice(anth.url, "flag"), token, reaper=False,
        allowed_hosts=["host.docker.internal"], allow_local_origin=True,
    )
    service.start(0)
    try:
        # Allowed extra host + valid token passes
        st, _, _ = _http(service.port, "GET", f"/{token}/contexto/health",
                         headers={"Host": f"host.docker.internal:{service.port}"})
        assert st == 200

        # Unlisted host refused with 403
        st, _, _ = _http(service.port, "GET", f"/{token}/contexto/health",
                         headers={"Host": f"evil.example:{service.port}"})
        assert st == 403

        # Local webview Origin with valid token passes and gets CORS header
        st, resp_h, _ = _http(service.port, "GET", f"/{token}/contexto/health",
                              headers={"Host": f"127.0.0.1:{service.port}",
                                       "Origin": "vscode-webview://abc123"})
        assert st == 200
        assert resp_h.get("Access-Control-Allow-Origin") == "vscode-webview://abc123"

        # Local webview Origin with BAD token is still refused (403)
        st, _, _ = _http(service.port, "GET", "/wrong-token/contexto/health",
                         headers={"Host": f"127.0.0.1:{service.port}",
                                  "Origin": "vscode-webview://abc123"})
        assert st == 403

        # Remote non-local Origin is refused even with valid token (403)
        st, _, _ = _http(service.port, "GET", f"/{token}/contexto/health",
                         headers={"Host": f"127.0.0.1:{service.port}",
                                  "Origin": "https://evil.example"})
        assert st == 403
    finally:
        service.stop()


def test_streamable_http_mcp_in_serve(tmp_path, multi_upstreams):
    anth, oai, _ = multi_upstreams
    home = tmp_path / "home"
    token = serve.ensure_token(home)
    service = serve.ServeService(
        home, UpstreamChoice(anth.url, "flag"), token, reaper=False,
        upstreams={"openai": UpstreamChoice(oai.url, "flag:openai")},
    )
    service.start(0)
    try:
        # Create two sessions via model traffic
        for key in ("alpha-key", "beta-key"):
            req = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": f"hello {key}"}]}).encode()
            st, _, _ = _http(service.port, "POST", f"/{token}/s/{key}/openai/v1/chat/completions", req,
                             {"Content-Type": "application/json"})
            assert st == 200
        _settle(service)

        def rpc(path: str, payload: dict, extra_headers: dict[str, str] | None = None):
            h = {"Content-Type": "application/json", **(extra_headers or {})}
            return _http(service.port, "POST", path, json.dumps(payload).encode(), h)

        # initialize
        st, _, raw = rpc(f"/{token}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {"protocolVersion": "2025-11-25"}})
        assert st == 200
        init = json.loads(raw)
        assert init["result"]["serverInfo"]["name"] == "contexto"

        # notifications/initialized -> 202 Accepted
        st, _, raw = rpc(f"/{token}/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert st == 202 and raw == b""

        # tools/list
        st, _, raw = rpc(f"/{token}/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert st == 200
        tools = [t["name"] for t in json.loads(raw)["result"]["tools"]]
        assert "contexto_pin" in tools and "contexto_search" in tools

        # tools/call bound via URL segment /<token>/s/alpha-key/mcp
        st, resp_h, raw = rpc(f"/{token}/s/alpha-key/mcp", {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "contexto_pin", "arguments": {"kind": "decision", "text": "alpha uses postgres"}},
        })
        assert st == 200 and json.loads(raw)["result"]["isError"] is False
        assert resp_h.get("Mcp-Session-Id") == service.resolve_session_key("alpha-key")

        # tools/call bound via X-Contexto-Session header -> beta does NOT see alpha's pin
        st, _, raw = rpc(f"/{token}/mcp", {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "contexto_state", "arguments": {}},
        }, extra_headers={"X-Contexto-Session": "beta-key"})
        assert st == 200
        beta_text = json.loads(raw)["result"]["content"][0]["text"]
        assert "alpha uses postgres" not in beta_text

        # tools/call on alpha via X-Contexto-Session DOES see alpha's pin
        st, _, raw = rpc(f"/{token}/mcp", {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "contexto_state", "arguments": {}},
        }, extra_headers={"X-Contexto-Session": "alpha-key"})
        assert st == 200
        assert "alpha uses postgres" in json.loads(raw)["result"]["content"][0]["text"]

        # Without binding when 2 sessions are open -> fail-closed ambiguous error
        st, _, raw = rpc(f"/{token}/mcp", {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "contexto_state", "arguments": {}},
        })
        assert st == 200
        ambig = json.loads(raw)["result"]
        assert ambig["isError"] is True and "ambiguous" in ambig["content"][0]["text"].lower()
    finally:
        service.stop()


def test_stdio_mcp_serve_non_claude_fallback_resolution(tmp_path, multi_upstreams):
    anth, oai, _ = multi_upstreams
    home = tmp_path / "home"
    token = serve.ensure_token(home)
    service = serve.ServeService(
        home, UpstreamChoice(anth.url, "flag"), token, reaper=False,
        upstreams={"openai": UpstreamChoice(oai.url, "flag:openai")},
    )
    service.start(0)
    try:
        # Open one session via Chat Completions
        req = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "solo session"}]}).encode()
        st, _, _ = _http(service.port, "POST", f"/{token}/s/solo-key/openai/v1/chat/completions", req,
                         {"Content-Type": "application/json"})
        assert st == 200
        _settle(service)

        # Non-Claude stdio ServeServer resolves the single active session automatically (no claudecode/toolUseId!)
        server = mcp_command.serve_server(home, None, io.StringIO(), io.StringIO(),
                                          environ={"HOME": str(tmp_path)}, cwd=tmp_path)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25",
                                  "clientInfo": {"name": "opencode", "version": "1.0"}}})
        pin = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "contexto_pin",
                                        "arguments": {"kind": "decision", "text": "active fallback works"}}})
        assert pin["result"]["isError"] is False

        # Open a second session -> active fallback now fails closed (ambiguous), while explicit session arg works!
        req2 = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "second session"}]}).encode()
        _http(service.port, "POST", f"/{token}/s/second-key/openai/v1/chat/completions", req2,
              {"Content-Type": "application/json"})
        _settle(service)

        ambig = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "contexto_state", "arguments": {}}})
        assert ambig["result"]["isError"] is True and "ambiguous" in ambig["result"]["content"][0]["text"].lower()

        explicit = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "contexto_state", "arguments": {"session": "solo-key"}}})
        assert explicit["result"]["isError"] is False
        assert "active fallback works" in explicit["result"]["content"][0]["text"]

        # X-Contexto-Session env var binds the server directly
        bound_server = mcp_command.serve_server(
            home, None, io.StringIO(), io.StringIO(),
            environ={"HOME": str(tmp_path), "X_CONTEXTO_SESSION": "solo-key"}, cwd=tmp_path,
        )
        bound_res = bound_server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                         "params": {"name": "contexto_state", "arguments": {}}})
        assert bound_res["result"]["isError"] is False
        assert "active fallback works" in bound_res["result"]["content"][0]["text"]
    finally:
        service.stop()


def test_print_mcp_http_config_cli(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    assert cli.main(["serve", "--print-mcp-http-config", "--port", "7899"]) == 0
    payload = json.loads(capsys.readouterr().out)
    entry = payload["mcpServers"][mcp.SERVER_NAME]
    assert entry["type"] == "http"
    assert entry["url"].startswith("http://127.0.0.1:7899/") and entry["url"].endswith("/mcp")
    assert Path(home / "serve.token").exists()
    assert SessionStore.open_existing is not None
