"""Tests for ``contexto mcp --serve`` (D70): one MCP server for every conversation of a Claude Code
process, which asks the Contexto service which session each tool call belongs to.

The service is a fake: a ``ThreadingHTTPServer`` on 127.0.0.1 that implements the resolve contract
(``contexto.serveclient``) and records what it received. The stores are real ``SessionStore``s in a
temporary home. No test waits: the retry's clock and sleep are injected.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import cli, mcp, serveclient
from contexto.commands import mcp as mcp_command
from contexto.store import SessionStore

TOKEN = "tok-Zq7Lw2Xn9Rb4Vd6Hs8Kp3Mf5Jt1Yc0Ga"
ID_A = "toolu_01D70001axQ7mZp4LkTr9VbYs"  # the shape Claude Code 2.1.322 sends (docs/research/D70-serve-probes.md)
ID_B = "toolu_01D70001bxQ7mZp4LkTr9VbYs"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        service: FakeService = self.server.service  # type: ignore[attr-defined]
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        service.received.append({"method": self.command, "path": self.path, "headers": self.headers.items(),
                                 "body": raw})
        status, answer = service.answer(self.path, raw)
        body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class FakeService:
    """The resolve contract: 200 ``{"session": ID}``, 404 ``{"error": "unknown_tool_use"}``, 400 for a
    malformed body, 403 for a bad token. ``script`` holds answers given before the contract applies."""

    def __init__(self, home: Path, token: str = TOKEN) -> None:
        self.token = token
        self.known: dict[str, str] = {}
        self.script: list[tuple[int, object]] = []
        self.received: list[dict] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.service = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.announce(home)

    def announce(self, home: Path, token: str | None = None) -> None:
        home.mkdir(parents=True, exist_ok=True)
        (home / serveclient.SERVE_FILE).write_text(json.dumps({"port": self.port, "pid": os.getpid()}))
        path = home / serveclient.TOKEN_FILE
        path.write_text((self.token if token is None else token) + "\n")
        path.chmod(0o600)

    def answer(self, path: str, raw: bytes) -> tuple[int, object]:
        if self.script:
            return self.script.pop(0)
        if path != f"/{self.token}{serveclient.RESOLVE_PATH}":
            return 403, {"error": "forbidden"}
        try:
            tool_use_id = json.loads(raw)["tool_use_id"]
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "bad_request"}
        if tool_use_id in self.known:
            return 200, {"session": self.known[tool_use_id]}
        return 404, {"error": "unknown_tool_use"}

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Clock:
    """A clock that only ``sleep`` advances, so the retry runs without waiting."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def home(tmp_path, monkeypatch):
    # Nothing in the developer's environment may decide a test: no provider switch, no proxy, no session id.
    for name in (*mcp.PROVIDER_SWITCHES, "CLAUDE_CODE_SESSION_ID", "CONTEXTO_SEARCH_CACHE_MB"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "home"


@pytest.fixture
def service(home):
    fake = FakeService(home)
    yield fake
    fake.stop()


@pytest.fixture
def clock():
    return Clock()


def make_server(home, clock, environ=None, budget=None):
    err = io.StringIO()
    server = mcp_command.serve_server(home, budget, io.StringIO(), err, environ=environ or {}, clock=clock,
                                      sleep=clock.sleep)
    return server, err


@pytest.fixture
def served(home, clock):
    return make_server(home, clock)


def call(server, method, params=None, msg_id=1):
    return server.handle_line(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}))


def tool(server, name, arguments, tool_use_id=ID_A, meta=None):
    params = {"name": name, "arguments": arguments}
    if tool_use_id is not None or meta is not None:
        params["_meta"] = {**({"claudecode/toolUseId": tool_use_id, "progressToken": 2}
                              if tool_use_id is not None else {}), **(meta or {})}
    return call(server, "tools/call", params)["result"]


def text(result) -> str:
    return result["content"][0]["text"]


def tree(root: Path) -> list[tuple]:
    """Every entry under ``root`` with its mode and a digest of its content: what a write would change."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            st = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else ""
            out.append((str(path.relative_to(root)), st.st_mode, digest))
    return sorted(out)


def refused(result, *needles) -> str:
    assert result["isError"] is True and "structuredContent" not in result
    message = text(result)
    for needle in needles:
        assert needle in message, message
    assert TOKEN not in message
    return message


# Argument rules


def test_session_and_serve_together_are_a_usage_error(home, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["mcp", "--session", "s1", "--serve", "--home", str(home)])
    err = capsys.readouterr().err
    assert exc.value.code == 2 and "--serve" in err and "--session" in err and "not allowed with" in err
    assert not home.exists()


def test_neither_session_nor_serve_is_a_usage_error(home, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["mcp", "--home", str(home)])
    err = capsys.readouterr().err
    assert exc.value.code == 2 and "one of the arguments --session --serve is required" in err
    assert not home.exists()


def test_serve_starts_the_resolving_server_without_a_session_or_a_service(home, monkeypatch):
    seen = []
    monkeypatch.setattr(mcp.ServeServer, "serve", lambda self, stdin: seen.append(self) or 0)
    assert cli.main(["mcp", "--serve", "--home", str(home), "--search-cache-mb", "3"]) == 0
    assert isinstance(seen[0], mcp.ServeServer) and not home.exists()


def test_session_mode_still_serves_one_store_and_ignores_the_tool_use_id(home, monkeypatch):
    SessionStore(home, "s1")
    seen = []
    monkeypatch.setattr(mcp, "serve_stdio", lambda store: seen.append(store) or 0)
    assert cli.main(["mcp", "--session", "s1", "--home", str(home)]) == 0
    assert isinstance(seen[0], SessionStore) and seen[0].session_id == "s1"
    server = mcp.Server(seen[0], io.StringIO(), io.StringIO())  # no service: --session never asks one
    assert type(server) is mcp.Server
    pinned = tool(server, "contexto_pin", {"kind": "decision", "text": "session mode"}, tool_use_id=ID_A)
    assert pinned["isError"] is False and "session mode" in seen[0].state_text()


# Listing without a session and without the service


def test_the_protocol_and_the_tool_list_need_no_session_and_no_service(home, served):
    server, err = served
    plain = mcp.Server(mcp.UnopenedStore("x", None), io.StringIO(), io.StringIO())  # what --session lists
    assert call(server, "initialize", {"protocolVersion": "2025-11-25"})["result"]["serverInfo"]["name"] == "contexto"
    discovered = call(server, "server/discover", {"_meta": {mcp.META_VERSION: "2026-07-28"}})["result"]
    assert discovered["supportedVersions"] == ["2026-07-28"]
    assert call(server, "ping")["result"] == {}
    listed = call(server, "tools/list")["result"]
    assert listed == {"tools": mcp.TOOLS} and listed["tools"] == plain._tools_list({})["tools"]
    modern = call(server, "tools/list", {"_meta": {mcp.META_VERSION: "2026-07-28"}})["result"]
    assert modern["ttlMs"] == 0 and modern["cacheScope"] == "private" and len(modern["tools"]) == 5
    assert not home.exists() and err.getvalue() == ""


def test_the_cli_lists_tools_and_answers_a_call_with_no_service_running(tmp_path):
    home = tmp_path / "absent-home"
    requests = [
        {"jsonrpc": "2.0", "id": "server-discover-probe-1", "method": "server/discover",
         "params": {"_meta": {mcp.META_VERSION: "2026-07-28"}}},
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "claude-code", "version": "2.1.322"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "contexto_state", "arguments": {},
                    "_meta": {"claudecode/toolUseId": ID_A, "progressToken": 3}}},
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CONTEXTO_"))}
    proc = subprocess.run([sys.executable, "-m", "contexto", "mcp", "--serve", "--home", str(home)],
                          input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr[-400:]
    by_id = {f["id"]: f for f in map(json.loads, proc.stdout.splitlines())}  # every stdout line is a frame
    assert sorted(by_id, key=str) == [1, 2, 3, "server-discover-probe-1"]
    assert [t["name"] for t in by_id[2]["result"]["tools"]] == [t["name"] for t in mcp.TOOLS]
    refused(by_id[3]["result"], "contexto serve", "not running")
    assert "serve.json is missing" in proc.stderr and not home.exists()


# Resolution


def test_two_tool_use_ids_reach_two_sessions_in_one_server(home, service, served):
    server, err = served
    alpha, beta = SessionStore(home, "alpha"), SessionStore(home, "beta")
    service.known.update({ID_A: "alpha", ID_B: "beta"})
    pinned = tool(server, "contexto_pin", {"kind": "decision", "text": "alpha keeps garnet"}, ID_A)
    assert pinned["isError"] is False and pinned["structuredContent"]["record_id"]
    in_alpha = tool(server, "contexto_state", {}, ID_A)
    in_beta = tool(server, "contexto_state", {}, ID_B)
    assert "alpha keeps garnet" in text(in_alpha) and "alpha keeps garnet" not in text(in_beta)
    assert text(in_alpha) == alpha.state_text() and text(in_beta) == beta.state_text()
    assert "alpha keeps garnet" in SessionStore(home, "alpha").state_text()  # durable, in alpha's ledger only
    assert not (beta.dir / "ledger.jsonl").exists()
    logged = {name: [e["tool"] for e in map(json.loads, (store.dir / "log.jsonl").read_text().splitlines())
                     if e["event"] == "mcp_call"] for name, store in (("alpha", alpha), ("beta", beta))}
    assert logged == {"alpha": ["contexto_pin", "contexto_state"], "beta": ["contexto_state"]}
    assert sorted(p.name for p in (home / "sessions").iterdir()) == ["alpha", "beta"] and err.getvalue() == ""


def test_the_request_carries_the_token_in_the_path_a_loopback_host_and_no_origin(home, service, served, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):  # http.client reads no proxy setting
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    SessionStore(home, "alpha")
    service.known[ID_A] = "alpha"
    assert tool(served[0], "contexto_state", {}, ID_A)["isError"] is False
    (request,) = service.received
    headers = {k.lower(): v for k, v in request["headers"]}
    assert request["method"] == "POST" and request["path"] == f"/{TOKEN}/contexto/resolve"
    assert headers["host"] == f"127.0.0.1:{service.port}" and "origin" not in headers
    assert headers["content-type"] == "application/json" and json.loads(request["body"]) == {"tool_use_id": ID_A}
    assert not any(TOKEN in v for v in headers.values())


def test_a_call_that_races_its_response_is_resolved_by_the_retry(home, service, served, clock):
    SessionStore(home, "alpha")
    service.known[ID_A] = "alpha"
    service.script += [(404, {"error": "unknown_tool_use"})] * 2
    assert tool(served[0], "contexto_state", {}, ID_A)["isError"] is False
    assert len(service.received) == 3 and clock.slept == list(serveclient.RETRY_DELAYS_S[:2])


def test_the_retry_gives_up_inside_its_window(home, service, served, clock):
    refused(tool(served[0], "contexto_state", {}, ID_A), "ANTHROPIC_BASE_URL")
    assert len(service.received) == len(serveclient.RETRY_DELAYS_S) + 1
    assert clock.slept == list(serveclient.RETRY_DELAYS_S) and sum(clock.slept) <= serveclient.RETRY_WINDOW_S


def test_a_slow_service_ends_the_retry_at_the_window(home, service, clock):
    real = serveclient._ask

    def slow(*args, **kwargs):  # each answer takes 0.9 s on the injected clock
        clock.now += 0.9
        return real(*args, **kwargs)

    serveclient._ask, restore = slow, real
    try:
        with pytest.raises(serveclient.ResolveError) as exc:
            serveclient.resolve(home, ID_A, clock=clock, sleep=clock.sleep)
    finally:
        serveclient._ask = restore
    assert exc.value.kind == serveclient.UNKNOWN_TOOL_USE and len(service.received) == 2
    assert clock.now - 1_000.0 <= serveclient.RETRY_WINDOW_S + 0.9


# Failing closed: each failure is a tool error with its own message, and nothing is written


@pytest.fixture
def bystander(home, monkeypatch):
    """An existing session that no failing call may fall back to, named by the variable Claude Code sets."""
    store = SessionStore(home, "bystander")
    store.pin("decision", "the bystander's own decision")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "bystander")
    return store


def unchanged_after(home, do):
    before = tree(home)
    result = do()
    assert tree(home) == before
    return result


@pytest.mark.parametrize("meta", [None, {}, {"claudecode/toolUseId": ""}, {"claudecode/toolUseId": 7},
                                  {"claudecode/toolUseId": ["toolu_1"]}, {"claudecode/toolUseId": None},
                                  {"claudecode/toolUseId": "t" * (mcp.TOOL_USE_ID_MAX + 1)}, "not an object"])
def test_a_call_without_a_tool_use_id_is_refused_and_the_service_is_not_asked(home, service, bystander, clock, meta):
    server, err = make_server(home, clock, environ=dict(os.environ))
    params = {"name": "contexto_pin", "arguments": {"kind": "decision", "text": "must not land anywhere"}}
    if meta is not None:
        params["_meta"] = meta
    result = unchanged_after(home, lambda: call(server, "tools/call", params)["result"])
    refused(result, "tool-use id", "contexto mcp --serve", "2.1.322")
    assert service.received == [] and "must not land" not in bystander.state_text()
    assert "no tool-use id" in err.getvalue()


def test_no_service_files_means_start_contexto_serve(home, bystander, served):
    result = unchanged_after(home, lambda: tool(served[0], "contexto_state", {}))
    refused(result, "not running", "contexto serve", "serve.json is missing")
    assert "bystander" not in text(result)


def test_a_service_that_is_gone_is_reported_as_not_running(home, service, bystander, served):
    service.stop()  # serve.json and serve.token remain, and nothing listens on the port
    result = unchanged_after(home, lambda: tool(served[0], "contexto_state", {}))
    refused(result, "not running", "contexto serve", f"port {service.port}")


def test_a_recorded_service_process_that_is_not_running_is_not_asked(home, service, served):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    (home / serveclient.SERVE_FILE).write_text(json.dumps({"port": service.port, "pid": gone.pid}))
    refused(tool(served[0], "contexto_state", {}), "not running", "contexto serve")
    assert service.received == []  # the token is not sent to whatever holds the port now


@pytest.mark.parametrize("content", ["", "{", "[]", '{"port": "80"}', '{"port": 0}', '{"port": 70000}',
                                     '{"port": true}', '{"pid": 1}', "x" * 100_000])
def test_a_serve_json_that_names_no_port_is_reported_without_its_content(home, service, served, content):
    (home / serveclient.SERVE_FILE).write_text(content)
    message = refused(tool(served[0], "contexto_state", {}), "contexto serve", "serve.json")
    assert service.received == [] and (not content or content[:20] not in message)


def test_an_id_the_service_does_not_know_names_the_base_url(home, service, bystander, served):
    result = unchanged_after(home, lambda: tool(served[0], "contexto_pin", {"kind": "decision", "text": "lost"}))
    message = refused(result, "no record of this tool call", "ANTHROPIC_BASE_URL")
    assert "CLAUDE_CODE_USE" not in message and "lost" not in bystander.state_text()


@pytest.mark.parametrize("name", mcp.PROVIDER_SWITCHES)
def test_an_unknown_id_names_the_provider_switch_that_bypasses_the_service(home, service, clock, name):
    server, _ = make_server(home, clock, environ={name: "1"})
    message = refused(tool(server, "contexto_state", {}), "ANTHROPIC_BASE_URL", name, "bypasses the service")
    assert [n for n in mcp.PROVIDER_SWITCHES if n in message] == [name]


@pytest.mark.parametrize("value", ["", "0", "false", "FALSE", " 0 "])
def test_a_provider_switch_that_is_off_is_not_named(home, service, clock, value):
    server, _ = make_server(home, clock, environ={"CLAUDE_CODE_USE_BEDROCK": value})
    assert "CLAUDE_CODE_USE" not in refused(tool(server, "contexto_state", {}), "ANTHROPIC_BASE_URL")


def test_serve_server_reads_the_switch_from_the_process_environment_by_default(home, service, clock, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "true")
    server = mcp_command.serve_server(home, None, io.StringIO(), io.StringIO(), clock=clock, sleep=clock.sleep)
    refused(tool(server, "contexto_state", {}), "CLAUDE_CODE_USE_VERTEX")


def test_a_refused_token_is_its_own_error_and_is_not_retried(home, service, bystander, served, clock):
    service.announce(home, token="stale-token-from-an-earlier-run")
    result = unchanged_after(home, lambda: tool(served[0], "contexto_state", {}))
    message = refused(result, "refused", "token", "contexto serve")
    assert "stale-token" not in message and len(service.received) == 1 and clock.slept == []


@pytest.mark.parametrize("status,answer", [(400, {"error": "bad_request"}), (500, b"oops"), (404, b"<html>nope</html>"),
                                           (200, b"not json"), (200, {"session": 7}), (200, {"session": ""}),
                                           (200, ["alpha"]), (200, {}), (302, b""), (200, b"x" * 200_000)])
def test_an_answer_outside_the_contract_is_refused(home, service, bystander, served, clock, status, answer):
    SessionStore(home, "alpha")
    service.script.append((status, answer))
    result = unchanged_after(home, lambda: tool(served[0], "contexto_state", {}))
    refused(result, "does not understand", "contexto serve")
    assert len(service.received) == 1 and clock.slept == []


@pytest.mark.parametrize("session", ["absent", "../x", "..", ".", "a/b", "a\\b", "nul\0byte", "s" * 300])
def test_a_session_the_service_names_that_cannot_be_one_is_refused(home, service, bystander, served, session):
    (home / "x").mkdir()  # where ../x would land
    service.known[ID_A] = session
    result = unchanged_after(home, lambda: tool(served[0], "contexto_pin", {"kind": "decision", "text": "stray"}))
    refused(result, "cannot be opened", "never creates a session")
    assert sorted(p.name for p in (home / "sessions").iterdir()) == ["bystander"]
    assert list((home / "x").iterdir()) == [] and "stray" not in bystander.state_text()


def test_a_symlinked_session_directory_is_refused(home, service, bystander, served, tmp_path):
    elsewhere = SessionStore(tmp_path / "other-home", "real")
    (home / "sessions" / "linked").symlink_to(elsewhere.dir, target_is_directory=True)
    service.known[ID_A] = "linked"
    before = tree(tmp_path / "other-home")
    result = unchanged_after(home, lambda: tool(served[0], "contexto_pin", {"kind": "decision", "text": "stray"}))
    refused(result, "cannot be opened", "symlink")
    assert tree(tmp_path / "other-home") == before


def test_a_session_deleted_while_cached_is_refused_and_not_recreated(home, service, served):
    import shutil

    server, _ = served
    doomed = SessionStore(home, "doomed")
    service.known[ID_A] = "doomed"
    assert tool(server, "contexto_state", {})["isError"] is False
    shutil.rmtree(doomed.dir)
    refused(tool(server, "contexto_pin", {"kind": "decision", "text": "late"}), "cannot be opened")
    assert list((home / "sessions").iterdir()) == [] and "doomed" not in server.servers


def test_a_damaged_live_lock_gets_the_reply_session_mode_gives(home, service, served):
    store = SessionStore(home, "alpha")
    store.release()
    (store.dir / "live.lock").unlink()
    (store.dir / "live.lock").mkdir()
    service.known[ID_A] = "alpha"
    server, _ = served
    message = refused(tool(server, "contexto_state", {}), "live.lock is damaged", "every tool is refused")
    assert message == mcp.LOCK_DAMAGED.format(tool="contexto_state", file="live.lock", reason="it is a directory",
                                              effect=mcp.LOCK_EFFECTS["live.lock"])
    (store.dir / "live.lock").rmdir()  # deleting the file is the whole repair, with no restart
    assert tool(server, "contexto_state", {})["isError"] is False


# The token file


def test_a_symlinked_token_file_is_not_read(home, service, bystander, served, tmp_path):
    real = tmp_path / "real.token"
    real.write_text(TOKEN + "\n")
    (home / serveclient.TOKEN_FILE).unlink()
    (home / serveclient.TOKEN_FILE).symlink_to(real)
    result = unchanged_after(home, lambda: tool(served[0], "contexto_state", {}))
    refused(result, "contexto serve", "serve.token")
    assert service.received == []


def test_a_symlinked_serve_json_is_not_read(home, service, served, tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"port": service.port, "pid": os.getpid()}))
    (home / serveclient.SERVE_FILE).unlink()
    (home / serveclient.SERVE_FILE).symlink_to(real)
    refused(tool(served[0], "contexto_state", {}), "contexto serve", "serve.json")
    assert service.received == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
def test_an_unreadable_token_file_means_the_service_cannot_be_asked(home, service, served):
    (home / serveclient.TOKEN_FILE).chmod(0o000)
    try:
        refused(tool(served[0], "contexto_state", {}), "contexto serve", "serve.token cannot be read")
    finally:
        (home / serveclient.TOKEN_FILE).chmod(0o600)
    assert service.received == []


def test_a_missing_token_file_means_the_service_cannot_be_asked(home, service, served):
    (home / serveclient.TOKEN_FILE).unlink()
    refused(tool(served[0], "contexto_state", {}), "contexto serve", "serve.token is missing")
    assert service.received == []


@pytest.mark.parametrize("content", [b"", b"\n", b"two\nlines\n", b"with space\n", b"a/b\n", b"tok\xc3\xa9n\n",
                                     b"q?x=1\n", b"t" * 5_000, b"tok\r\nHost: evil\r\n"])
def test_a_token_file_that_is_not_one_ascii_line_is_never_sent(home, service, served, content):
    (home / serveclient.TOKEN_FILE).write_bytes(content)
    message = refused(tool(served[0], "contexto_state", {}), "contexto serve", "serve.token")
    assert service.received == [] and "evil" not in message and "lines" not in message


def test_the_token_is_in_no_tool_result_and_no_diagnostic(home, service, clock, capfd):
    """Every failure mode in one server, then a success: the token appears nowhere a reader could see."""
    server, err = make_server(home, clock, environ={"CLAUDE_CODE_USE_BEDROCK": "1"})
    SessionStore(home, "alpha")
    results = [tool(server, "contexto_state", {}, tool_use_id=None)]  # no id
    results.append(tool(server, "contexto_state", {}))  # unknown id
    service.known[ID_A] = "absent"
    results.append(tool(server, "contexto_state", {}))  # a session that does not exist
    service.script += [(500, b"boom"), (200, b"\xff\xfe"), (403, {"error": "forbidden"})]
    results += [tool(server, "contexto_state", {}) for _ in range(3)]
    service.known[ID_A] = "alpha"
    results.append(tool(server, "contexto_pin", {"kind": "decision", "text": "kept"}))
    service.stop()
    results.append(tool(server, "contexto_state", {}))  # nothing listening
    assert [r["isError"] for r in results] == [True, True, True, True, True, True, False, True]
    out, process_err = capfd.readouterr()
    seen = json.dumps(results) + err.getvalue() + out + process_err
    assert TOKEN not in seen and err.getvalue().count("\n") == 7
    log = (home / "sessions" / "alpha" / "log.jsonl").read_text()
    assert TOKEN not in log and "kept" not in log


@pytest.mark.parametrize("error", [
    http.client.InvalidURL(f"URL can't contain control characters. '/{TOKEN}/contexto/resolve'"),
    OSError(f"cannot send POST /{TOKEN}/contexto/resolve"),
    http.client.BadStatusLine(f"/{TOKEN}/contexto/resolve"),
])
def test_a_failed_request_is_reported_by_its_class_never_its_text(home, service, served, monkeypatch, error):
    def fail(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(http.client.HTTPConnection, "request", fail)
    server, err = served
    message = refused(tool(server, "contexto_state", {}), "not running", "contexto serve", type(error).__name__)
    assert TOKEN not in err.getvalue() and "contexto/resolve" not in message + err.getvalue()
    with pytest.raises(serveclient.ResolveError) as exc:
        serveclient.resolve(home, ID_A)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__  # a traceback would not quote it either


def test_a_service_that_accepts_and_never_answers_times_out(home, service, clock):
    silent = socket.create_server(("127.0.0.1", 0))
    try:
        port = silent.getsockname()[1]
        (home / serveclient.SERVE_FILE).write_text(json.dumps({"port": port, "pid": os.getpid()}))
        with pytest.raises(serveclient.ResolveError) as exc:
            serveclient.resolve(home, ID_A, clock=clock, sleep=clock.sleep, timeout=0.2)
    finally:
        silent.close()
    assert exc.value.kind == serveclient.NOT_RUNNING and exc.value.reason == f"no answer from port {port} within 0.2 s"
    assert clock.slept == [] and service.received == []


def test_a_service_that_hangs_up_is_reported_as_not_running(home, service, served):
    silent = socket.create_server(("127.0.0.1", 0))

    def hang_up():
        conn, _ = silent.accept()
        conn.recv(65536)
        conn.close()

    thread = threading.Thread(target=hang_up, daemon=True)
    thread.start()
    try:
        (home / serveclient.SERVE_FILE).write_text(json.dumps({"port": silent.getsockname()[1], "pid": os.getpid()}))
        message = refused(tool(served[0], "contexto_state", {}), "not running", "the request to port")
        # the close arrives as an end of stream or, when unread request bytes remain, as a reset
        assert "(RemoteDisconnected)" in message or "(ConnectionResetError)" in message
    finally:
        thread.join(5)
        silent.close()


def test_resolve_errors_never_carry_the_token(home, service, clock):
    service.script.append((403, {"error": "forbidden"}))
    with pytest.raises(serveclient.ResolveError) as exc:
        serveclient.resolve(home, ID_A, clock=clock, sleep=clock.sleep)
    assert exc.value.kind == serveclient.TOKEN_REFUSED
    assert TOKEN not in str(exc.value) + repr(exc.value) + repr(exc.value.__cause__) + repr(exc.value.__context__)


# The store cache and the tools/list record


def test_at_most_eight_stores_stay_open_and_an_evicted_one_is_released(home, service, served):
    server, _ = served
    names = [f"s{i:02d}" for i in range(mcp.STORE_CACHE + 3)]
    for i, name in enumerate(names):
        SessionStore(home, name).release()
        service.known[f"toolu_{i:02d}"] = name
    opened = {}
    for i, name in enumerate(names):
        assert tool(server, "contexto_pin", {"kind": "decision", "text": f"for {name}"}, f"toolu_{i:02d}")[
            "isError"] is False
        opened[name] = server.servers[name].store
    assert mcp.STORE_CACHE == 8 and list(server.servers) == names[3:]
    assert [opened[name].release.alive for name in names] == [False] * 3 + [True] * 8
    assert tool(server, "contexto_state", {}, "toolu_04")["isError"] is False  # a hit makes it the newest
    assert list(server.servers)[-1] == "s04" and server.servers["s04"].store is opened["s04"]
    again = tool(server, "contexto_state", {}, "toolu_00")  # an evicted session is opened again
    assert "for s00" in text(again) and list(server.servers) == [*names[5:], "s04", "s00"]
    assert opened["s03"].release.alive is False and server.servers["s00"].store is not opened["s00"]


def test_each_store_gets_the_search_cache_budget(home, service, clock, monkeypatch):
    mb = 1024 * 1024
    SessionStore(home, "alpha")
    service.known[ID_A] = "alpha"
    server, _ = make_server(home, clock, budget=3 * mb)
    assert tool(server, "contexto_state", {})["isError"] is False
    assert server.servers["alpha"].store._catalog.text_budget == 3 * mb
    monkeypatch.setenv("CONTEXTO_SEARCH_CACHE_MB", "16")
    server, _ = make_server(home, clock)
    assert tool(server, "contexto_state", {})["isError"] is False
    assert server.servers["alpha"].store._catalog.text_budget == 16 * mb


def test_the_tools_list_record_is_written_at_a_stores_first_resolved_call(home, service, served):
    server, _ = served
    alpha, beta = SessionStore(home, "alpha"), SessionStore(home, "beta")
    service.known[ID_A] = "alpha"
    call(server, "tools/list")
    assert alpha.mcp_listed_at() is None and beta.mcp_listed_at() is None  # no store is known at tools/list
    refused(tool(server, "contexto_state", {}, ID_B))
    assert alpha.mcp_listed_at() is None and beta.mcp_listed_at() is None  # nor at a call that fails
    assert tool(server, "contexto_state", {}, ID_A)["isError"] is False
    first = alpha.mcp_listed_at()
    assert first is not None and beta.mcp_listed_at() is None
    assert tool(server, "contexto_state", {}, ID_A)["isError"] is False
    assert alpha.mcp_listed_at() == first  # once per open store


# The module boundary


def test_serveclient_imports_only_the_standard_library_and_no_url_opener():
    source = open(serveclient.__file__, encoding="utf-8").read()
    imports = {line.split()[1].split(".")[0] for line in source.splitlines()
               if line.startswith(("import ", "from ")) and not line.startswith("from __future__")}
    assert imports <= {"http", "json", "os", "re", "time", "collections", "pathlib", "contexto"}
    assert "urllib" not in source  # urllib.request would honor HTTP_PROXY
