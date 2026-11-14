"""``contexto serve`` (D70): the engine as a standing local service. Fake upstreams and plain HTTP
clients only; no model, no real credential."""

from __future__ import annotations

import http.client
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import __version__, cli, serve, serveclient, servecontract
from contexto.hints import GATED_HINT_HEADERS
from contexto.launcher import build_engine, contexto_tool_rules
from contexto.proxy import REFUSED_MESSAGE
from contexto.store import SessionStore
from contexto.upstream import Upstream as UpstreamChoice
from test_proxy_server import SECRET, sse
from test_proxy_server import Upstream as StubUpstream

A = "11111111-2222-4333-8444-aaaaaaaaaaaa"
B = "11111111-2222-4333-8444-bbbbbbbbbbbb"
C = "11111111-2222-4333-8444-cccccccccccc"
REFUSED = {"type": "error", "error": {"type": "permission_error", "message": REFUSED_MESSAGE}}


class Clock:
    """A monotonic clock the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def body(session: str | None, text: str = "hello", *, parent: str | None = None, extra: list | None = None,
         stream: bool = False) -> bytes:
    """A Messages request shaped like Claude Code 2.1.322's: ``metadata.user_id`` is a JSON string."""
    request = {"model": "claude-haiku-5-5", "max_tokens": 100,
               "system": [{"type": "text", "text": "You are Claude Code."}],
               "tools": [{"name": "Read", "description": "Reads a file",
                          "input_schema": {"type": "object", "properties": {}}}],
               "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}, *(extra or [])],
               "stream": stream}
    if session is not None:
        user_id = {"device_id": "d" * 64, "account_uuid": "", "session_id": session}
        if parent is not None:
            user_id["parent_session_id"] = parent
        request["metadata"] = {"user_id": json.dumps(user_id)}
    return json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()


def headers(session: str | None = None, **more: str) -> dict[str, str]:
    out = {"Content-Type": "application/json", "x-api-key": SECRET,
           "User-Agent": "claude-cli/2.1.322 (external, cli)", **more}
    if session is not None:
        out["X-Claude-Code-Session-Id"] = session
    return out


def raw_call(port: int, method: str, target: str, data: bytes = b"", head: dict | None = None,
             timeout: float = 20):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
    head = dict(head or {})
    head.setdefault("Host", f"127.0.0.1:{port}")
    for name, value in head.items():
        conn.putheader(name, value)
    if data or method == "POST":
        conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data or None)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response, payload


def call(service, method: str, path: str, data: bytes = b"", head: dict | None = None, **kwargs):
    """A request under the service's token prefix."""
    return raw_call(service.port, method, f"/{service.token}{path}", data, head, **kwargs)


def post(service, session: str | None, text: str = "hello", path: str = "/v1/messages?beta=true", **kwargs):
    return call(service, "POST", path, body(session, text, **kwargs), headers(session))


def settle(service, timeout: float = 30) -> None:
    """Wait until the service has no request in flight. A client has its whole response a moment
    before the handler returns its session's lease, so a test that goes on to count leases, close idle
    sessions or fill the session cap waits for this state, not for a length of time."""
    deadline = time.monotonic() + timeout
    with service._idle:
        while service._in_flight:
            left = deadline - time.monotonic()
            assert left > 0, f"{service._in_flight} requests still in flight"
            service._idle.wait(left)


def turn(service, session: str | None, text: str = "hello", **kwargs):
    """``post``, then ``settle``: the request is over for the service too."""
    answer = post(service, session, text, **kwargs)
    settle(service)
    return answer


def until(condition, what: str, timeout: float = 30):
    """Poll ``condition`` until it is true; fail naming ``what`` at the deadline."""
    deadline = time.monotonic() + timeout
    while not (found := condition()):
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.005)
    return found


def sessions(home: Path) -> list[str]:
    try:
        return sorted(p.name for p in (home / "sessions").iterdir())
    except FileNotFoundError:
        return []


def served(home: Path) -> dict[str, str]:
    """Claude Code session id -> the Contexto session whose metadata records it."""
    out = {}
    for name in sessions(home):
        meta = json.loads((home / "sessions" / name / "meta.json").read_text())
        for cli_session in (meta.get("cli_sessions") or {}).get("claude", {}):
            out[cli_session] = name
    return out


def events(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def serve_log(home: Path, event: str | None = None, count: int = 0) -> list[dict]:
    """The service log, or its ``event`` entries, waiting briefly for ``count`` of them: the proxy
    logs a request after it has relayed the response."""
    deadline = time.monotonic() + 5
    while True:
        found = [e for e in events(home / "serve.log") if event is None or e["event"] == event]
        if len(found) >= count or time.monotonic() > deadline:
            return found
        time.sleep(0.01)


def session_log(home: Path, session: str) -> list[dict]:
    return events(home / "sessions" / session / "log.jsonl")


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def make_service(home, upstream):
    started = []

    def make(**kwargs):
        kwargs.setdefault("stderr", io.StringIO())
        kwargs.setdefault("reaper", False)  # the tests that close idle sessions call reap themselves
        target = kwargs.pop("upstream", upstream)
        service = serve.ServeService(kwargs.pop("home", home), UpstreamChoice(target, "flag"),
                                     serve.ensure_token(home), **kwargs)
        service.start(0)
        started.append(service)
        return service

    yield make
    for service in started:
        service.stop()


# The token


def test_a_request_under_the_token_reaches_upstream_without_it(make_service, home):
    service = make_service()
    response, _ = post(service, A)
    assert response.status == 200
    method, path, sent, _ = StubUpstream.received[0]
    assert (method, path) == ("POST", "/base/v1/messages?beta=true")
    assert service.token not in path and not any(service.token in v for v in sent.values())
    assert sent["X-Claude-Code-Session-Id"] == A and sent["x-api-key"] == SECRET


def wrong_targets(token: str) -> dict[str, str]:
    """Request targets that must be refused, by what is wrong with each."""
    encoded = f"%{ord(token[0]):02X}" + token[1:]
    return {
        "missing": "/v1/messages",
        "wrong": "/" + "x" * len(token) + "/v1/messages",
        "shorter": f"/{token[:-1]}/v1/messages",
        "longer": f"/{token}x/v1/messages",
        "prefix of the token": f"/{token[:8]}/v1/messages",
        "percent-encoded": f"/{encoded}/v1/messages",
        "double slash": f"//{token}/v1/messages",
        "later segment": f"/v1/{token}/messages",
        "second segment": f"/x/{token}/v1/messages",
        "no slash after": f"/{token}",
        "query after the token": f"/{token}?beta=true",
        "token in the query": f"/v1/messages?token={token}",
        "token as the query": f"/?{token}/v1/messages",
        "dot segment": f"/./{token}/v1/messages",
        "empty": "/",
        "absolute form": f"http://127.0.0.1/{token}/v1/messages",
        "upper case": f"/{token.swapcase()}/v1/messages",
        "doubled": f"/{token}/{token}/v1/messages",  # the second one would be forwarded upstream
        "again in the query": f"/{token}/v1/messages?key={token}",
    }


def test_every_request_without_the_exact_token_prefix_gets_the_same_403(make_service, home):
    service = make_service()
    for what, target in wrong_targets(service.token).items():
        response, data = raw_call(service.port, "POST", target, body(A), headers(A))
        assert response.status == 403, what
        assert json.loads(data) == REFUSED, what
    assert StubUpstream.received == [] and sessions(home) == []
    # The same body as a refused Host, so a caller cannot tell which check failed.
    response, data = raw_call(service.port, "POST", f"/{service.token}/v1/messages", body(A),
                              {**headers(A), "Host": "evil.example"})
    assert response.status == 403 and json.loads(data) == REFUSED


def test_a_query_string_after_the_path_is_kept(make_service):
    service = make_service()
    response, data = call(service, "GET", "/v1/models?limit=1000", head=headers())
    assert response.status == 200 and json.loads(data)["path"] == "/base/v1/models?limit=1000"


def test_host_and_origin_are_still_refused_with_the_right_token(make_service, home):
    service = make_service()
    for head in ({"Origin": "http://127.0.0.1"}, {"Host": "contexto.example:80"}, {"Host": "[::1]:1"}):
        response, data = call(service, "POST", "/v1/messages", body(A), {**headers(A), **head})
        assert response.status == 403 and json.loads(data) == REFUSED, head
    response, _ = call(service, "POST", "/v1/messages", body(A), {**headers(A), "Host": f"localhost:{service.port}"})
    assert response.status == 200
    reasons = [e["reason"] for e in serve_log(home) if e["event"] == "request_refused"]
    assert reasons and set(reasons) <= {"origin", "host"}


def test_a_refused_request_with_a_large_body_still_gets_its_403(make_service):
    service = make_service()
    response, data = raw_call(service.port, "POST", "/nope/v1/messages", b"x" * (3 * 1024 * 1024), headers(A))
    assert response.status == 403 and json.loads(data) == REFUSED


def test_refusals_are_logged_by_reason_only_and_at_most_once_a_second(make_service, home):
    clock = Clock()
    service = make_service(clock=clock)
    for _ in range(40):
        raw_call(service.port, "GET", "/not-the-token/secret-looking-path")
    refused = [e for e in serve_log(home) if e["event"] == "request_refused"]
    assert len(refused) == 1 and refused[0]["reason"] == "token" and "path" not in refused[0]
    clock.now += 1.5
    raw_call(service.port, "GET", "/again", head={"Origin": "null"})
    refused = [e for e in serve_log(home) if e["event"] == "request_refused"]
    assert len(refused) == 2 and refused[1]["reason"] == "origin" and refused[1]["not_logged"] == 39
    assert "secret-looking-path" not in (home / "serve.log").read_text()


def test_the_token_is_in_no_file_no_log_and_no_stderr(make_service, home):
    stderr = io.StringIO()
    service = make_service(stderr=stderr)
    token = service.token
    post(service, A)
    post(service, None)  # no id: logged in the service log
    call(service, "HEAD", "/api/hello")
    call(service, "POST", "/v1/messages", b"not json", headers(A))
    call(service, "POST", "/v1/messages", body(B), headers(A))  # mismatch
    call(service, "GET", "/contexto/health")
    call(service, "POST", "/contexto/resolve", b'{"tool_use_id":"toolu_none"}')
    raw_call(service.port, "POST", f"/{token}x/v1/messages", body(A), headers(A))
    raw_call(service.port, "POST", f"/{token}/v1/messages", body(A), {**headers(A), "Origin": "http://x"})
    raw_call(service.port, "GET", f"/{token}")
    service.stop()
    assert len(serve_log(home, "request", 3)) == 3  # the no-id POST, the HEAD and the mismatch
    found = [str(p) for p in home.rglob("*")
             if p.is_file() and p.name != serve.TOKEN_FILE and token.encode() in p.read_bytes()]
    assert found == []
    assert token not in stderr.getvalue()
    assert all(token not in path for _, path, _, _ in StubUpstream.received)


# Routing


def test_two_session_ids_get_two_stores(make_service, home):
    service = make_service()
    assert post(service, A)[0].status == 200 and post(service, B)[0].status == 200
    assert post(service, A, extra=[{"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                                   {"role": "user", "content": [{"type": "text", "text": "more"}]}])[0].status == 200
    mapping = served(home)
    assert set(mapping) == {A, B} and mapping[A] != mapping[B] and len(sessions(home)) == 2
    assert all(len(name) == 32 and name not in (A, B) for name in sessions(home))  # uuid4().hex, never the client's
    requests = {name: [e for e in session_log(home, name) if e["event"] == "request"] for name in sessions(home)}
    assert len(requests[mapping[A]]) == 2 and len(requests[mapping[B]]) == 1
    assert service.health()["sessions"] == 2


def test_a_subagent_request_with_the_same_id_lands_in_the_same_store(make_service, home):
    service = make_service()
    post(service, A)
    response, _ = call(service, "POST", "/v1/messages?beta=true", body(A, "D70 from the subagent"),
                       headers(A, **{"x-claude-code-agent-id": "a800b6f5b41866e3f"}))
    assert response.status == 200
    assert len(sessions(home)) == 1
    assert len([e for e in session_log(home, sessions(home)[0]) if e["event"] == "request"]) == 2


@pytest.mark.parametrize("method, path, data", [
    ("HEAD", "/api/hello", b""),
    ("GET", "/v1/models?limit=1000", b""),
    ("POST", "/v1/messages?beta=true", json.dumps(json.loads(body(None)), indent=1).encode()),
    ("POST", "/responses", b'{"model": "gpt-6-astra", "input": [], "stream": false}'),
    ("POST", "/v1/other", b'{"spaced":  [1,  2]}'),
])
def test_a_request_without_a_session_id_is_forwarded_unchanged_and_makes_no_session(make_service, home, method,
                                                                                    path, data):
    service = make_service()
    response, _ = call(service, method, path, data, headers())
    assert response.status == 200
    sent_method, sent_path, _, sent = StubUpstream.received[0]
    assert (sent_method, sent_path, sent) == (method, "/base" + path, data)
    assert sessions(home) == [] and service.health()["sessions"] == 0
    assert [e["path"] for e in serve_log(home, "request", 1)] == [path.split("?")[0]]


# Gateway hint headers (D70): read by the engine, left out of what goes upstream unless asked for.

HINTS = {"x-claude-code-request-class": "main", "x-claude-code-compaction": "manual",
         "x-claude-code-context-compacted": "manual", "x-claude-code-prompt-id": "6f1d0b52-4b8e-4d6a-9c1e-2f0a7e3b5d10",
         "x-claude-code-agent-type": "Explore", "x-claude-code-prev-tool-durations": "Bash=742;Read=9"}


def hinted(session: str | None) -> dict[str, str]:
    return {**headers(session), **HINTS}


def gated(sent: dict[str, str]) -> set[str]:
    return {name.lower() for name in sent} & GATED_HINT_HEADERS


def test_the_engine_reads_the_hint_headers_and_the_upstream_never_gets_them(make_service, home):
    service = make_service()
    response, _ = call(service, "POST", "/v1/messages?beta=true", body(A), hinted(A))
    assert response.status == 200
    _, _, sent, _ = StubUpstream.received[0]
    assert gated(sent) == set() and sent["X-Claude-Code-Session-Id"] == A
    settle(service)
    [store] = sessions(home)
    [logged] = [e for e in session_log(home, store) if e["event"] == "request"]
    assert logged["transform_events"][-1]["request_class"] == "main"
    assert serve_log(home, "serve_start")[0]["hint_headers"] == "stripped"


def test_a_request_of_no_session_is_forwarded_without_the_hint_headers(make_service, home):
    service = make_service()
    response, _ = call(service, "POST", "/v1/messages?beta=true", body(None), hinted(None))
    assert response.status == 200
    _, _, sent, _ = StubUpstream.received[0]
    assert gated(sent) == set() and sessions(home) == []


def test_forward_hint_headers_passes_them_upstream(make_service, home):
    service = make_service(forward_hint_headers=True)
    for session in (A, None):
        response, _ = call(service, "POST", "/v1/messages?beta=true", body(session), hinted(session))
        assert response.status == 200
    assert [gated(sent) for _, _, sent, _ in StubUpstream.received] == [set(HINTS)] * 2
    assert serve_log(home, "serve_start")[0]["hint_headers"] == "forwarded"


def test_a_header_and_body_that_disagree_are_forwarded_unchanged(make_service, home):
    service = make_service()
    data = json.dumps(json.loads(body(B)), indent=2).encode()  # bytes the serializer would not reproduce
    response, _ = call(service, "POST", "/v1/messages", data, headers(A))
    assert response.status == 200 and StubUpstream.received[0][3] == data
    assert sessions(home) == []
    assert [e["reason"] for e in serve_log(home) if e["event"] == "unrouted"] == ["session_id_mismatch"]


@pytest.mark.parametrize("value", ["../../etc", "a/b", "", "x" * 200, "has space"])
def test_an_id_that_is_not_a_plain_id_never_names_a_session(make_service, home, value):
    service = make_service()
    response, _ = call(service, "POST", "/v1/messages", body(None), headers(**{"X-Claude-Code-Session-Id": value}))
    assert response.status == 200 and sessions(home) == []
    assert not (home / "etc").exists()


def test_a_routed_body_is_parsed_once(make_service, home, monkeypatch):
    """The service reads the body to route it; the proxy's transform then takes that parse."""
    import contexto.proxy as proxy_module

    data = body(A)
    parsed: list[str] = []

    def counting(module):
        original = module.loads_bounded

        def loads(value):
            if value == data:
                parsed.append(module.__name__)
            return original(value)
        monkeypatch.setattr(module, "loads_bounded", loads)

    counting(proxy_module)
    counting(serve)
    service = make_service()
    assert call(service, "POST", "/v1/messages", data, headers(A))[0].status == 200
    assert call(service, "POST", "/v1/messages", data, headers())[0].status == 200  # routed by the body's id
    assert parsed == ["contexto.serve", "contexto.serve"]
    assert len([e for e in session_log(home, served(home)[A]) if e["event"] == "request"]) == 2


def test_a_compressed_body_is_routed_like_any_other(make_service, home):
    import gzip

    service = make_service()
    response, _ = call(service, "POST", "/v1/messages", gzip.compress(body(A)),
                       {**headers(A), "Content-Encoding": "gzip"})
    assert response.status == 200 and set(served(home)) == {A}
    assert StubUpstream.received[0][3] == body(A)  # forwarded decoded (rule 15)
    # A body that cannot be decoded gets the proxy's own 415 and makes no session.
    response, _ = call(service, "POST", "/v1/messages", b"not gzip", {**headers(B), "Content-Encoding": "gzip"})
    assert response.status == 415 and set(served(home)) == {A}


def test_the_body_id_routes_when_the_header_is_absent(make_service, home):
    service = make_service()
    response, _ = call(service, "POST", "/v1/messages", body(A), headers())
    assert response.status == 200 and set(served(home)) == {A}


def test_a_teammate_with_a_parent_session_reaches_the_parents_store(make_service, home):
    service = make_service()
    post(service, A)
    lead = served(home)[A]
    response, _ = call(service, "POST", "/v1/messages", body(B, "teammate turn", parent=A), headers(B))
    assert response.status == 200
    # The store records the id the request was routed by, the lead's, and never the teammate's own.
    assert sessions(home) == [lead] and served(home) == {A: lead}
    # Its count_tokens carries the header only (measured: no metadata), and follows the remembered parent.
    count = json.dumps({"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "x"}], "tools": []})
    response, data = call(service, "POST", "/v1/messages/count_tokens?beta=true", count.encode(), headers(B))
    assert response.status == 200 and json.loads(data) == {"input_tokens": 1234}
    paths = [e["path"] for e in session_log(home, lead) if e["event"] == "request"]
    assert paths == ["/v1/messages", "/v1/messages", "/v1/messages/count_tokens"]
    assert sessions(home) == [lead]


def test_count_tokens_before_any_session_is_forwarded_unchanged(make_service, home):
    service = make_service()
    count = json.dumps({"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "x"}], "tools": []},
                       indent=1).encode()
    for _ in range(2):
        response, _ = call(service, "POST", "/v1/messages/count_tokens?beta=true", count, headers(A))
        assert response.status == 200
    assert [r[3] for r in StubUpstream.received] == [count, count]
    assert sessions(home) == []
    assert [e["reason"] for e in serve_log(home) if e["event"] == "unrouted"] == ["no_session"] * 2
    # Once a Messages request has made the session, the count goes through its engine.
    post(service, A)
    call(service, "POST", "/v1/messages/count_tokens?beta=true", count, headers(A))
    paths = [e["path"] for e in session_log(home, served(home)[A]) if e["event"] == "request"]
    assert paths == ["/v1/messages", "/v1/messages/count_tokens"]


def test_a_session_the_launcher_made_for_the_id_is_reused(make_service, home, tmp_path):
    older = SessionStore(home, "older-session")
    older.record_cli_session("claude", A, 100.0)
    older.release()
    made = SessionStore(home, "launcher-made")
    made.set_workdir(tmp_path)
    made.record_cli_session("claude", A, 200.0)
    made.release()
    service = make_service()
    post(service, A)
    assert sessions(home) == ["launcher-made", "older-session"]
    assert [e["event"] for e in session_log(home, "launcher-made") if e["event"] in ("serve_open", "request")] \
        == ["serve_open", "request"]
    assert not any(e["event"] == "request" for e in session_log(home, "older-session"))
    assert SessionStore.open_existing(home, "launcher-made").workdir() == tmp_path.resolve()  # kept as recorded
    # A session the service makes records no working directory: it does not run in the client's.
    post(service, B)
    assert SessionStore.open_existing(home, served(home)[B]).workdir() is None


def test_a_session_the_service_makes_stays_out_of_project_memory_after_a_resume(make_service, home, tmp_path):
    """D70 with D69: the service cannot know a client's directory and routes by session id, so a session
    it makes records ``project_memory: false``. ``contexto run claude --resume <id>`` then records a
    directory (``set_workdir``) but keeps memory off (``record_project_memory`` never writes true over
    false), so the session neither carries a project's records nor gives its own to later sessions."""
    from contexto.launcher import record_project_memory
    from contexto.store.project import project_root, project_sessions, session_project

    made = SessionStore(home, "launcher-made")
    made.set_workdir(tmp_path)
    made.record_cli_session("claude", A, 100.0)
    made.release()
    service = make_service()
    post(service, A)
    post(service, B)
    by_service = served(home)[B]
    meta = lambda name: json.loads((home / "sessions" / name / "meta.json").read_text())  # noqa: E731
    assert meta(by_service)["project_memory"] is False
    assert "project_memory" not in meta("launcher-made")  # a session the launcher made keeps its own choice
    service.stop()
    resumed = SessionStore(home, by_service)  # as `contexto run claude --resume <id>` opens it
    resumed.set_workdir(tmp_path)
    record_project_memory(resumed, True)
    resumed.release()
    after = meta(by_service)
    assert after["project_memory"] is False and after["workdir"] == str(tmp_path.resolve())
    assert session_project(home, by_service, after, own=lambda: []) is None  # carries nothing in
    members = {m.id: m for m in project_sessions(home, project_root(tmp_path)).sessions}
    assert members[by_service].memory is False and members["launcher-made"].memory is True  # gives nothing out


def test_two_ids_one_launcher_session_share_one_engine(make_service, home):
    made = SessionStore(home, "launcher-made")
    made.record_cli_session("claude", A, 100.0)
    made.record_cli_session("claude", B, 200.0)  # the launcher's session served both (a /clear)
    made.release()
    opened = []

    def factory(store, config):
        opened.append(store.session_id)
        return build_engine(store, config)

    service = make_service(engine_factory=factory)
    post(service, A)
    post(service, B)
    assert opened == ["launcher-made"] and service.health()["sessions"] == 1


def test_first_requests_of_one_id_open_one_store(make_service, home):
    opened = []

    def factory(store, config):
        opened.append(store.session_id)
        time.sleep(0.2)  # a slow open: the others must wait for it, not open their own
        return build_engine(store, config)

    service = make_service(engine_factory=factory)
    threads = [threading.Thread(target=post, args=(service, A, f"prompt {i}")) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(opened) == 1 and len(sessions(home)) == 1
    assert len([e for e in session_log(home, opened[0]) if e["event"] == "request"]) == 6


def slow_engine(gate: threading.Event, entered: threading.Event, slow: str):
    """An engine factory whose transform waits on ``gate`` for requests of Claude Code session ``slow``."""
    def factory(store, config):
        engine, observe, name = build_engine(store, config)

        class Slow:
            preserves_input = True

            def __call__(self, request, head, endpoint):
                if slow in json.dumps(request.get("metadata")):
                    entered.set()
                    gate.wait(30)
                return engine(request, head, endpoint)

            def __getattr__(self, attribute):
                return getattr(engine, attribute)

        return Slow(), observe, name
    return factory


def test_a_long_transform_in_one_session_does_not_stall_another(make_service, home):
    gate, entered = threading.Event(), threading.Event()
    service = make_service(engine_factory=slow_engine(gate, entered, A))
    done: dict[str, float] = {}
    started = time.monotonic()

    def run(session):
        post(service, session)
        done[session] = time.monotonic() - started

    first = threading.Thread(target=run, args=(A,))
    first.start()
    assert entered.wait(10)
    run(B)  # completes while A's transform holds A's lock
    b_done, a_still_held = done[B], A not in done
    time.sleep(0.3)
    held_for = time.monotonic() - started
    gate.set()
    first.join(10)
    assert a_still_held and A in done, done
    assert done[B] < held_for <= done[A], f"B finished at {b_done:.3f} s, A was held to {held_for:.3f} s: {done}"


# Eviction


def test_an_idle_session_is_closed_and_a_later_request_reopens_it(make_service, home):
    clock = Clock()
    service = make_service(clock=clock, idle_s=600)
    turn(service, A)
    name = served(home)[A]
    clock.now += 599
    assert service.reap() == 0 and service.health()["sessions"] == 1
    clock.now += 2
    assert service.reap() == 1 and service.health()["sessions"] == 0
    closes = [e for e in session_log(home, name) if e["event"] == "serve_close"]
    assert len(closes) == 1 and closes[0]["reason"] == "idle"
    assert [e["event"] for e in serve_log(home) if e["event"].startswith("session_")] \
        == ["session_open", "session_close"]
    turn_two = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                {"role": "user", "content": [{"type": "text", "text": "and again"}]}]
    assert turn(service, A, extra=turn_two)[0].status == 200
    assert sessions(home) == [name] and service.health()["sessions"] == 1
    log = session_log(home, name)
    assert [e["event"] for e in log if e["event"] in ("serve_open", "serve_close")] \
        == ["serve_open", "serve_close", "serve_open"]
    # The new engine read the epoch record the first one wrote, as after a launcher restart (rule 16).
    reopened = [e for e in log if e["event"] == "request"][-1]
    assert any(e.get("event") == "epoch_resumed" for e in reopened["transform_events"]), reopened["transform_events"]


def test_the_cap_closes_the_least_recently_used_session(make_service, home):
    clock = Clock()
    service = make_service(clock=clock, max_sessions=2)
    for session in (A, B):
        turn(service, session)
        clock.now += 1
    turn(service, A)  # B is now the least recently used
    clock.now += 1
    turn(service, C)
    mapping = served(home)
    assert service.health()["sessions"] == 2 and len(sessions(home)) == 3
    assert [e["reason"] for e in session_log(home, mapping[B]) if e["event"] == "serve_close"] == ["cap"]
    assert not any(e["event"] == "serve_close" for e in session_log(home, mapping[A]))
    turn(service, B)  # reopens B's own store, closing A's
    assert served(home)[B] == mapping[B] and len(sessions(home)) == 3 and service.health()["sessions"] == 2


def test_a_session_with_a_request_in_flight_is_never_closed(make_service, home):
    gate, entered = threading.Event(), threading.Event()
    clock = Clock()
    stderr = io.StringIO()
    service = make_service(engine_factory=slow_engine(gate, entered, A), clock=clock, max_sessions=1, idle_s=10,
                           stderr=stderr)
    first = threading.Thread(target=post, args=(service, A))
    first.start()
    try:
        assert entered.wait(10)
        clock.now += 3600
        assert service.reap() == 0 and service.health()["sessions"] == 1  # idle by the clock, but in flight
        # The cap is reached and nothing can be closed: B is forwarded unchanged, not queued behind A.
        data = json.dumps(json.loads(body(B)), indent=1).encode()
        response, _ = call(service, "POST", "/v1/messages", data, headers(B))
        assert response.status == 200 and StubUpstream.received[-1][3] == data
        assert set(served(home)) == {A} and service.health()["sessions"] == 1
        assert [e["reason"] for e in serve_log(home) if e["event"] == "unrouted"] == ["session_cap"]
        assert "--max-sessions" in stderr.getvalue()
    finally:
        gate.set()
        first.join(10)
    settle(service)
    assert service.reap() == 0  # just used
    clock.now += 11
    assert service.reap() == 1


def test_stop_closes_every_session_and_flushes_the_last_seen_times(make_service, home):
    service = make_service()
    post(service, A)
    post(service, B)
    time.sleep(0.02)
    before = time.time()  # the first request of an id records it at once; the last one's time is flushed
    for session in (A, B):
        post(service, session, extra=[{"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                                      {"role": "user", "content": [{"type": "text", "text": "more"}]}])
    service.stop()
    for name in sessions(home):
        closes = [e for e in session_log(home, name) if e["event"] == "serve_close"]
        assert [e["reason"] for e in closes] == ["shutdown"]
        assert all(seen >= before for seen in SessionStore.open_existing(home, name).cli_sessions("claude").values())
    stopped = [e for e in serve_log(home) if e["event"] == "serve_stop"]
    assert len(stopped) == 1 and stopped[0]["sessions_closed"] == 2
    assert not (home / serve.STATE_FILE).exists()


def test_session_warnings_go_to_stderr_with_the_session_id(make_service, home):
    stderr = io.StringIO()
    service = make_service(stderr=stderr, config={"bound": 26_000, "headroom_floor": 25_000})
    post(service, A, "x " * 4000)
    post(service, A, "x " * 4000, extra=[{"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                                         {"role": "user", "content": [{"type": "text", "text": "more"}]}])
    service.stop()
    name = served(home)[A]
    lines = [line for line in stderr.getvalue().splitlines() if line]
    assert lines and all(line.startswith(f"contexto serve: session {name}: ") for line in lines), lines


# The local endpoints


def test_health_reports_version_port_and_open_sessions(make_service):
    service = make_service()
    response, data = call(service, "GET", "/contexto/health")
    assert response.status == 200 and response.getheader("Content-Type") == "application/json"
    assert json.loads(data) == {"ok": True, "version": __version__, "port": service.port, "sessions": 0,
                                "resolve": {"found": 0, "unknown": 0}}
    post(service, A)
    assert json.loads(call(service, "GET", "/contexto/health")[1])["sessions"] == 1


def tool_response(tool_use_id: str) -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": "mcp__contexto__contexto_state", "input": {}}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}


class ToolUpstream(BaseHTTPRequestHandler):
    """Answers every Messages request with one tool call. Streamed: everything up to the tool call's
    ``content_block_stop`` is sent at once, and the rest only when ``gate`` is set."""

    protocol_version = "HTTP/1.1"
    gate = threading.Event()
    block_sent = threading.Event()
    received: list[str] = []
    tool_use_id = "toolu_01D70resolve"

    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        ToolUpstream.received.append(self.path)
        answer = tool_response(self.tool_use_id)
        if not request.get("stream"):
            data = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        block = answer["content"][0]
        first = [{"type": "message_start", "message": {**answer, "content": [], "stop_reason": None}},
                 {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                 {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "calling"}},
                 {"type": "content_block_stop", "index": 0},
                 {"type": "content_block_start", "index": 1, "content_block": block},
                 {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta",
                                                                       "partial_json": "{}"}},
                 {"type": "content_block_stop", "index": 1}]
        last = [{"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
                {"type": "message_stop"}]
        for event in first:
            self._chunk(sse(event))
        ToolUpstream.block_sent.set()
        ToolUpstream.gate.wait(30)
        for event in last:
            self._chunk(sse(event))
        self.wfile.write(b"0\r\n\r\n")

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()


@pytest.fixture
def tool_upstream():
    ToolUpstream.gate, ToolUpstream.block_sent, ToolUpstream.received = threading.Event(), threading.Event(), []
    server = ThreadingHTTPServer(("127.0.0.1", 0), ToolUpstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    ToolUpstream.gate.set()
    server.shutdown()
    server.server_close()


def resolve(service, tool_use_id):
    response, data = call(service, "POST", "/contexto/resolve", json.dumps({"tool_use_id": tool_use_id}).encode(),
                          {"Content-Type": "application/json"})
    return response.status, json.loads(data)


def test_resolve_names_the_session_of_a_tool_call(make_service, home, tool_upstream):
    service = make_service(upstream=tool_upstream)
    assert resolve(service, ToolUpstream.tool_use_id) == (404, {"error": "unknown_tool_use"})
    assert post(service, A)[0].status == 200
    assert resolve(service, ToolUpstream.tool_use_id) == (200, {"session": served(home)[A]})
    assert resolve(service, "toolu_other") == (404, {"error": "unknown_tool_use"})
    assert ToolUpstream.received == ["/v1/messages?beta=true"]  # resolve itself never reached upstream


def test_a_tool_call_resolves_before_the_client_receives_the_event_that_completes_it(make_service, home,
                                                                                     tool_upstream):
    """Claude Code starts an MCP call 5 to 16 ms after the tool call's ``content_block_stop`` (see
    ``StreamHold``), and the MCP server then asks which session the call belongs to."""
    service = make_service(upstream=tool_upstream)
    seen = bytearray()
    at_block: list = []
    arrived: list[tuple[float, bool]] = []  # when each read returned, and whether the tool call was in it by then
    marker = b'"content_block_stop", "index": 1'  # the tool call's block is complete

    def client():
        conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=30)
        conn.request("POST", f"/{service.token}/v1/messages?beta=true", body(A, stream=True), headers(A))
        response = conn.getresponse()
        while chunk := response.read1(65536):
            seen.extend(chunk)
            arrived.append((time.monotonic(), ToolUpstream.tool_use_id.encode() in seen))
            if not at_block and marker in seen:
                at_block.append(resolve(service, ToolUpstream.tool_use_id))  # the moment the call is complete
        conn.close()

    thread = threading.Thread(target=client)
    thread.start()
    try:
        assert ToolUpstream.block_sent.wait(30)
        # Upstream has sent the whole tool call. The service has read it once the id resolves, and the
        # client has what came before it; neither is waited for by the clock.
        until(lambda: service.resolve(ToolUpstream.tool_use_id) and b"calling" in seen,
              "the service to read the tool call and the client to get the text before it")
        assert ToolUpstream.tool_use_id.encode() not in seen and not at_block
    finally:
        opened = time.monotonic()
        ToolUpstream.gate.set()
        thread.join(30)
    assert marker in seen, bytes(seen[-300:])
    # The proxy held the call until upstream sent the rest: no read before that had any of it.
    assert [at for at, has_call in arrived if has_call and at < opened] == []
    assert at_block == [(200, {"session": served(home)[A]})]


def test_the_tool_call_map_keeps_the_most_recent_ids(make_service, monkeypatch):
    monkeypatch.setattr(serve, "TOOL_CALLS_KEPT", 3)
    service = make_service()
    for n in range(5):
        service.register_tool_calls((f"toolu_{n}",), "s1")
    service.register_tool_calls(("toolu_2",), "s1")  # seen again in its session: now the newest
    service.register_tool_calls(("toolu_5",), "s2")
    assert [service.resolve(f"toolu_{n}") for n in range(6)] == [None, None, "s1", None, "s1", "s2"]


@pytest.mark.parametrize("data, status", [
    (b"", 400), (b"not json", 400), (b"[]", 400), (b'{"tool_use_id": 5}', 400), (b'{"tool_use_id": ""}', 400),
    (b'{"other": "x"}', 400), (json.dumps({"tool_use_id": "t" * 300}).encode(), 400),
    (b'{"tool_use_id": "' + b"t" * (64 * 1024) + b'"}', 413),
])
def test_resolve_refuses_a_malformed_or_oversized_body(make_service, data, status):
    service = make_service()
    response, answer = call(service, "POST", "/contexto/resolve", data)
    assert response.status == status and "error" in json.loads(answer)


def test_local_endpoints_never_reach_upstream(make_service, home):
    service = make_service()
    statuses = [call(service, method, path, data)[0].status for method, path, data in [
        ("GET", "/contexto/health", b""), ("POST", "/contexto/health", b"{}"), ("GET", "/contexto/resolve", b""),
        ("POST", "/contexto/resolve", b'{"tool_use_id":"x"}'), ("GET", "/contexto", b""),
        ("GET", "/contexto/other", b""), ("POST", "/contexto/v1/messages", body(A)),
        ("HEAD", "/contexto/health", b""), ("DELETE", "/contexto/resolve", b"")]]
    assert statuses == [200, 405, 405, 404, 404, 404, 404, 405, 405]
    assert StubUpstream.received == [] and sessions(home) == []
    # A local endpoint needs the token like everything else.
    response, data = raw_call(service.port, "GET", "/contexto/health")
    assert response.status == 403 and json.loads(data) == REFUSED


# State files


def modes(home: Path) -> dict[str, int]:
    return {str(p.relative_to(home)): stat.S_IMODE(p.stat().st_mode) for p in home.rglob("*") if p.is_file()}


def test_every_file_is_0600_under_a_permissive_umask(make_service, home):
    previous = os.umask(0)
    try:
        service = make_service()
        post(service, A)
        post(service, None)
        assert json.loads((home / serve.STATE_FILE).read_text()) == {"port": service.port, "pid": os.getpid()}
        found = modes(home)
    finally:
        os.umask(previous)
    assert {serve.TOKEN_FILE, serve.STATE_FILE, serve.LOG_FILE} <= set(found)
    assert {name: oct(mode) for name, mode in found.items() if mode != 0o600} == {}
    assert (home / serve.TOKEN_FILE).read_text() == service.token + "\n"


def test_the_token_is_created_once_and_read_back(home):
    token = serve.ensure_token(home)
    assert serve.TOKEN_PATTERN.fullmatch(token) and len(token) == 43
    assert serve.ensure_token(home) == token == serve.read_token(home)
    rotated = serve.rotate_token(home)
    assert rotated != token and serve.read_token(home) == rotated
    assert stat.S_IMODE((home / serve.TOKEN_FILE).stat().st_mode) == 0o600


@pytest.mark.parametrize("damage", ["symlink", "dangling symlink", "group readable", "world writable", "directory",
                                    "fifo", "empty", "not a token"])
def test_a_token_file_that_is_not_private_is_refused(home, tmp_path, damage):
    token = serve.ensure_token(home)
    path = home / serve.TOKEN_FILE
    if damage in ("symlink", "dangling symlink"):
        elsewhere = tmp_path / "elsewhere"
        if damage == "symlink":
            elsewhere.write_text(token + "\n")
            elsewhere.chmod(0o600)
        path.unlink()
        path.symlink_to(elsewhere)
    elif damage == "group readable":
        path.chmod(0o640)
    elif damage == "world writable":
        path.chmod(0o602)
    elif damage == "directory":
        path.unlink()
        path.mkdir()
    elif damage == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif damage == "empty":
        path.write_text("")
    else:
        path.write_text("two words\n")
    for read in (serve.read_token, serve.ensure_token):
        with pytest.raises(serve.ServeError) as refused:
            read(home)
        assert "contexto serve --rotate-token" in str(refused.value) and token not in str(refused.value)
    if damage != "directory":
        fresh = serve.rotate_token(home)  # the remedy the message names
        assert serve.read_token(home) == fresh and not path.is_symlink()
        assert not (tmp_path / "elsewhere").exists() or (tmp_path / "elsewhere").read_text() == token + "\n"


def test_the_log_rotates_once_at_its_limit(home, monkeypatch):
    monkeypatch.setattr(serve, "MAX_LOG_BYTES", 2000)
    log = serve.ServeLog(home, "t" * 43)
    for n in range(100):
        log.log({"event": "filler", "n": n, "token": "t" * 43})
    current, rotated = home / serve.LOG_FILE, home / (serve.LOG_FILE + ".1")
    assert 0 < current.stat().st_size <= 2000 and 0 < rotated.stat().st_size <= 2000
    assert sorted(p.name for p in home.iterdir()) == [serve.LOG_FILE, serve.LOG_FILE + ".1"]
    assert events(current)[-1]["n"] == 99 and "ts" in events(current)[-1]
    assert "t" * 43 not in current.read_text() + rotated.read_text()  # the token is never written, whatever is logged
    assert stat.S_IMODE(current.stat().st_mode) == stat.S_IMODE(rotated.stat().st_mode) == 0o600


# Guards


@pytest.mark.parametrize("url, loops", [
    ("http://127.0.0.1:7837", True), ("http://127.0.0.1:7837/TOKEN", True), ("http://localhost:7837/x", True),
    ("http://[::1]:7837", True), ("http://127.9.9.9:7837/", True), ("https://127.0.0.1:7837", True),
    ("http://0.0.0.0:7837", True), ("http://LOCALHOST:7837", True),
    ("https://gateway.example/TOKEN", True),  # the service's own token, wherever it points
    ("http://127.0.0.1:7838", False), ("http://127.0.0.1", False), ("https://api.anthropic.com", False),
    ("http://gateway.example:7837", False),
])
def test_an_upstream_that_is_the_service_itself_is_refused(url, loops):
    token = "T" * 43
    upstream = UpstreamChoice(url.replace("TOKEN", token), "settings:user")
    if not loops:
        serve.check_upstream_loop(upstream, 7837, token)
        return
    with pytest.raises(serve.ServeError) as refused:
        serve.check_upstream_loop(upstream, 7837, token)
    message = str(refused.value)
    assert "--upstream URL" in message and "settings:user" in message
    assert token not in message and "7837/" not in message  # never the URL


# The command


def child_env(home: Path, env: dict | None = None) -> dict[str, str]:
    """The environment of a ``contexto serve`` child: nothing of the developer's Claude Code setup (its
    config directory, base URL or provider switches), and a home directory under the test's."""
    full = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC_", "CONTEXTO_"))}
    full.update({"CONTEXTO_HOME": str(home), "HOME": str(home.parent / "user-home"), **(env or {})})
    return full


def run_cli(home: Path, *args: str, env: dict | None = None, cwd: Path | None = None):
    full = child_env(home, env)
    return subprocess.run([sys.executable, "-m", "contexto", "serve", *args], capture_output=True, text=True,
                          env=full, cwd=cwd or home.parent, timeout=120)


class Running:
    """``contexto serve`` in a child process, started on a free port."""

    def __init__(self, home: Path, *args: str, env: dict | None = None, hangup=signal.SIG_DFL) -> None:
        full = child_env(home, env)
        self.home = home
        # A suite started under nohup hands every child an ignored SIGHUP: give the service the
        # disposition the test means it to have.
        self.process = subprocess.Popen([sys.executable, "-m", "contexto", "serve", "--port", "0", *args],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=full,
                                        cwd=home.parent, preexec_fn=lambda: signal.signal(signal.SIGHUP, hangup))
        self.stderr_lines: list[str] = []
        self.listening = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        if not self.listening.wait(60):
            self.kill()
            raise AssertionError(f"contexto serve did not start: {self.stderr_lines}")
        state = json.loads((home / serve.STATE_FILE).read_text())
        self.port, self.pid = state["port"], state["pid"]
        self.token = (home / serve.TOKEN_FILE).read_text().strip()

    def _read(self) -> None:
        for line in self.process.stderr:
            self.stderr_lines.append(line.rstrip("\n"))
            if "listening on" in line:
                self.listening.set()

    def stop(self, signum: int = signal.SIGTERM) -> int:
        self.process.send_signal(signum)
        return self.process.wait(timeout=60)

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()


@pytest.fixture
def running(home, upstream):
    started = []

    def start(*args, **kwargs):
        if "--upstream" not in args:
            args = ("--upstream", upstream, *args)
        started.append(Running(home, *args, **kwargs))
        return started[-1]

    yield start
    for process in started:
        process.kill()


def test_help_lists_the_serve_options(capsys):
    with pytest.raises(SystemExit) as done:
        cli.main(["serve", "--help"])
    out = capsys.readouterr().out
    assert done.value.code == 0
    for option in ("--port", "--upstream", "--bound", "--max-sessions", "--idle-minutes", "--print-settings",
                   "--print-mcp-config", "--rotate-token", "--status", "--forward-hint-headers"):
        assert option in out
    assert "--host" not in out


def test_the_command_forwards_the_hint_headers_only_when_asked(home, running, upstream):
    for args, state in (((), "stripped"), (("--forward-hint-headers",), "forwarded")):
        service = running(*args)
        assert serve_log(home, "serve_start")[-1]["hint_headers"] == state
        assert service.stop() == 0


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_the_command_serves_until_a_signal_and_shuts_down_cleanly(home, running, upstream, signum):
    service = running()
    assert service.pid == service.process.pid
    assert service.stderr_lines[0] == (f"contexto serve: listening on 127.0.0.1:{service.port} "
                                       "(bound 150,000, upstream flag)")
    response, _ = raw_call(service.port, "POST", f"/{service.token}/v1/messages?beta=true", body(A), headers(A))
    assert response.status == 200 and StubUpstream.received[0][1] == "/base/v1/messages?beta=true"
    status = run_cli(home, "--status")
    assert status.returncode == 0 and f"running on 127.0.0.1:{service.port}" in status.stdout
    assert "1 open session" in status.stdout
    assert service.stop(signum) == 0
    assert not (home / serve.STATE_FILE).exists()
    log = serve_log(home)
    assert log[0]["event"] == "serve_start" and log[0]["port"] == service.port and log[0]["upstream_source"] == "flag"
    assert log[-1]["event"] == "serve_stop" and log[-1]["signal"] == signal.Signals(signum).name
    assert [e["reason"] for e in session_log(home, served(home)[A]) if e["event"] == "serve_close"] == ["shutdown"]
    everything = "\n".join(service.stderr_lines) + (home / serve.LOG_FILE).read_text()
    assert service.token not in everything and upstream not in everything and "Traceback" not in everything
    assert {name: oct(mode) for name, mode in modes(home).items() if mode != 0o600} == {}
    status = run_cli(home, "--status")
    assert status.returncode == 1 and "not running" in status.stdout


class Stalled(BaseHTTPRequestHandler):
    """An upstream that never answers, so a request stays in flight."""

    protocol_version = "HTTP/1.1"
    reached = threading.Event()

    def log_message(self, *args):
        pass

    def do_POST(self):
        Stalled.reached.set()
        time.sleep(60)


def test_a_second_signal_ends_the_wait_for_requests_in_flight(home, running):
    Stalled.reached = threading.Event()
    stalled = ThreadingHTTPServer(("127.0.0.1", 0), Stalled)
    stalled.daemon_threads = True
    threading.Thread(target=stalled.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        service = running("--upstream", f"http://127.0.0.1:{stalled.server_address[1]}")
        client = socket.create_connection(("127.0.0.1", service.port))
        data = body(A)
        client.sendall(b"POST /%s/v1/messages HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Claude-Code-Session-Id: %s\r\n"
                       b"Content-Length: %d\r\n\r\n%s" % (service.token.encode(), service.port, A.encode(),
                                                          len(data), data))
        assert Stalled.reached.wait(20)
        started = time.monotonic()
        service.process.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        service.process.send_signal(signal.SIGTERM)
        assert service.process.wait(timeout=30) == 0
        waited = time.monotonic() - started
        client.close()
    finally:
        stalled.shutdown()
        stalled.server_close()
    assert "Traceback" not in "\n".join(service.stderr_lines)
    assert not (home / serve.STATE_FILE).exists()
    stopped = serve_log(home)[-1]
    assert stopped["event"] == "serve_stop" and stopped["in_flight"] == 1
    assert stopped.get("hurried") is True, f"the whole drain was waited out ({waited:.2f} s)"


class Gated(BaseHTTPRequestHandler):
    """An upstream that answers only once ``gate`` is set, so a request stays in flight until then."""

    protocol_version = "HTTP/1.1"
    reached = threading.Event()
    gate = threading.Event()

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Gated.reached.set()
        Gated.gate.wait(30)
        data = json.dumps({"type": "message", "role": "assistant", "content": [],
                           "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Recorder:
    """A listener on a port the service gave up: it records every byte a connection sends it, then
    closes that connection, so a client asking it gives up at once."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.received: list[bytes] = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    self.received.append(conn.recv(65536))
                except OSError:
                    self.received.append(b"")

    def close(self) -> None:
        self.sock.close()
        self.thread.join(10)


def bind(port: int) -> socket.socket | None:
    try:
        return socket.create_server(("127.0.0.1", port))
    except OSError:
        return None


def test_after_a_stop_signal_whoever_binds_the_port_is_never_sent_the_token(home, running):
    """Round-2 review: while the service drains, its port is closed and its lock still held. A process
    that binds the port then must get nothing from ``--status`` or the MCP server's resolve call: the
    service stops advertising its port before it closes it."""
    Gated.reached, Gated.gate = threading.Event(), threading.Event()
    gated = ThreadingHTTPServer(("127.0.0.1", 0), Gated)
    gated.daemon_threads = True
    threading.Thread(target=gated.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    recorder = None
    try:
        service = running("--upstream", f"http://127.0.0.1:{gated.server_address[1]}")
        client = socket.create_connection(("127.0.0.1", service.port))
        data = body(A)
        client.sendall(b"POST /%s/v1/messages HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Claude-Code-Session-Id: %s\r\n"
                       b"Content-Length: %d\r\n\r\n%s" % (service.token.encode(), service.port, A.encode(),
                                                          len(data), data))
        assert Gated.reached.wait(20)
        service.process.send_signal(signal.SIGTERM)
        recorder = Recorder(until(lambda: bind(service.port), "the service's port to be free", timeout=20))
        out = io.StringIO()
        status = serve.status(home, out)
        with pytest.raises(serveclient.ResolveError) as refused:
            serveclient.resolve(home, "toolu_01D70window", sleep=lambda seconds: None)
        assert servecontract.service_running(home), "the drain was over before the checks ran"
        Gated.gate.set()
        assert service.process.wait(timeout=30) == 0
        client.close()
    finally:
        Gated.gate.set()
        if recorder is not None:
            recorder.close()
        gated.shutdown()
        gated.server_close()
    assert recorder.received == []
    assert status == 1 and "starting or stopping" in out.getvalue()
    assert refused.value.kind == serveclient.NOT_RUNNING and "serve.json" in refused.value.reason


def test_a_resolve_call_retrying_when_the_service_stops_never_reaches_the_ports_next_owner(make_service, home):
    """Round-3 review (Reviewer B, minor 1): a call the service answered 404 is asked again for up to
    2 s. When the service stops during a wait and another program binds the freed port, the retry must
    not reach it: before each retry the client checks again, as before the first try, that the service
    that answered still holds the lock and that its record still names the port."""
    service = make_service()
    port, waits, recorders = service.port, [], []

    def stop_and_take_the_port(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 2:  # the second wait: the first retry was answered by the service itself
            service.stop()
            recorders.append(Recorder(until(lambda: bind(port), "the service's port to be free", timeout=20)))

    try:
        with pytest.raises(serveclient.ResolveError) as refused:
            serveclient.resolve(home, "toolu_01D70retrying", sleep=stop_and_take_the_port)
    finally:
        for recorder in recorders:
            recorder.close()
    assert len(waits) == 2 and len(recorders) == 1
    assert recorders[0].received == []
    assert refused.value.kind == serveclient.NOT_RUNNING


@pytest.mark.parametrize("change", ["record removed", "another pid", "another port", "token replaced"])
def test_a_retry_is_not_sent_once_the_services_record_changed(make_service, home, change):
    service = make_service()
    state = json.loads((home / serve.STATE_FILE).read_text())

    def change_the_record(seconds: float) -> None:
        if change == "record removed":  # the service is stopping: it removes its record before its port
            (home / serve.STATE_FILE).unlink(missing_ok=True)
        elif change == "token replaced":
            serve.rotate_token(home)
        else:
            moved = {"another pid": {**state, "pid": state["pid"] + 1},
                     "another port": {**state, "port": state["port"] + 1}}[change]
            serve.write_atomic(home / serve.STATE_FILE, json.dumps(moved).encode())

    with pytest.raises(serveclient.ResolveError) as refused:
        serveclient.resolve(home, "toolu_01D70changed", sleep=change_the_record)
    assert refused.value.kind == serveclient.NOT_RUNNING
    assert service.health()["resolve"] == {"found": 0, "unknown": 1}  # the first try only


def test_a_hangup_ignored_at_start_stays_ignored(home, running):
    """``nohup contexto serve &`` must outlive its terminal."""
    service = running(hangup=signal.SIG_IGN)
    service.process.send_signal(signal.SIGHUP)
    time.sleep(0.5)
    assert service.process.poll() is None
    assert raw_call(service.port, "GET", f"/{service.token}/contexto/health")[0].status == 200
    assert service.stop(signal.SIGTERM) == 0
    assert serve_log(home)[-1]["signal"] == "SIGTERM"


def test_a_taken_port_is_one_clean_line(home, running, upstream):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        taken = run_cli(home, "--port", str(port), "--upstream", upstream)
    finally:
        blocker.close()
    assert taken.returncode == 1 and taken.stdout == ""
    assert taken.stderr.splitlines() == [f"contexto serve: port {port} on 127.0.0.1 is in use; stop what holds it, "
                                         "or pass --port N"]
    assert not (home / serve.STATE_FILE).exists()
    # A second service on the first one's port says the service is already running.
    service = running()
    again = run_cli(home, "--port", str(service.port), "--upstream", upstream)
    assert again.returncode == 1 and len(again.stderr.splitlines()) == 1
    assert f"contexto serve is already running on 127.0.0.1:{service.port} (pid {service.pid})" in again.stderr
    assert json.loads((home / serve.STATE_FILE).read_text())["pid"] == service.pid  # the first one's record stands
    assert service.stop() == 0


def test_the_command_refuses_an_upstream_that_is_itself(home, tmp_path):
    token = serve.ensure_token(home)
    user_home = tmp_path / "user-home"
    (user_home / ".claude").mkdir(parents=True)
    # What a user who followed the docs has: the service's own URL in their Claude Code settings.
    (user_home / ".claude" / "settings.json").write_text(json.dumps(
        {"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:7911/{token}"}}))
    for args, env in ((("--port", "7911"), {}),
                      (("--port", "7912"), {}),  # another port, but the URL still carries the service's token
                      (("--port", "7911", "--upstream", "http://localhost:7911"), {}),
                      (("--port", "7913"), {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:7913/{token}"})):
        if env:
            (user_home / ".claude" / "settings.json").unlink()
        refused = run_cli(home, *args, env=env)
        assert refused.returncode == 2, refused.stderr
        assert "--upstream URL" in refused.stderr and len(refused.stderr.splitlines()) == 1
        assert token not in refused.stderr and "Traceback" not in refused.stderr
    assert not (home / serve.STATE_FILE).exists() and not (home / serve.LOG_FILE).exists()


@pytest.mark.parametrize("args, message", [
    (("--bound", "0"), "--bound must be positive"),
    (("--bound", "180000"), "--allow-large-bound"),
    (("--window-fraction", "2"), "--window-fraction must be between 0 and 1"),
    (("--headroom-floor", "150000"), "--headroom-floor must be above 0 and below the bound"),
    (("--max-sessions", "0"), "--max-sessions must be at least 1"),
    (("--idle-minutes", "0"), "--idle-minutes must be above 0"),
    (("--upstream", "ftp://x"), "invalid upstream (from flag): it must be an http(s) base URL"),
    (("--upstream", "http://[::1"), "invalid upstream (from flag)"),
])
def test_usage_errors_exit_2_before_anything_is_written(home, args, message):
    refused = run_cli(home, "--port", "0", *args)
    assert refused.returncode == 2 and message in refused.stderr and "Traceback" not in refused.stderr
    assert not (home / serve.STATE_FILE).exists() and not (home / serve.LOG_FILE).exists()


@pytest.mark.parametrize("content", [
    b'{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9/a\\ud800"}}',
    b"\xff\xfe" + '{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9/a\ud800"}}'.encode("utf-16-le", "surrogatepass"),
], ids=["escape", "utf-16-le"])
def test_a_settings_base_url_with_a_lone_surrogate_is_a_usage_error(home, tmp_path, content):
    """D70 round 4: a lone surrogate can reach the base URL from a settings file (a ``\\ud800`` escape,
    or a malformed UTF-16LE file Claude Code loads). No request line can carry it, so the command stops
    with a usage error that names the source and does not quote the URL."""
    user_home = tmp_path / "user-home"
    (user_home / ".claude").mkdir(parents=True)
    (user_home / ".claude" / "settings.json").write_bytes(content)
    refused = run_cli(home, "--port", "0")
    assert refused.returncode == 2 and "Traceback" not in refused.stderr, refused.stderr
    assert refused.stderr.strip() == ("contexto serve: invalid upstream (from settings:user): "
                                      "upstream holds a lone surrogate")
    assert not (home / serve.STATE_FILE).exists() and not (home / serve.LOG_FILE).exists()


def test_a_wide_token_file_stops_the_command(home, upstream):
    serve.ensure_token(home)
    (home / serve.TOKEN_FILE).chmod(0o644)
    for args in (("--port", "0", "--upstream", upstream), ("--print-settings",), ("--print-mcp-config",),
                 ("--status",)):
        refused = run_cli(home, *args)
        assert refused.returncode == 1 and "contexto serve --rotate-token" in refused.stderr, (args, refused.stderr)
        assert refused.stdout == ""


def test_print_settings_is_a_claude_code_settings_file(home):
    printed = run_cli(home, "--print-settings")
    assert printed.returncode == 0 and printed.stderr == ""
    token = (home / serve.TOKEN_FILE).read_text().strip()  # created on first use
    assert json.loads(printed.stdout) == {
        "env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{serve.DEFAULT_PORT}/{token}",
                "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},
        "permissions": {"allow": contexto_tool_rules()}}
    assert serve.DEFAULT_PORT == 7837
    other = run_cli(home, "--print-settings", "--port", "9001")
    assert json.loads(other.stdout)["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:9001/{token}"
    warned = run_cli(home, "--print-settings", env={"CLAUDE_CODE_USE_BEDROCK": "1"})
    assert warned.returncode == 0 and json.loads(warned.stdout)["env"]["ANTHROPIC_BASE_URL"].endswith(token)
    assert "CLAUDE_CODE_USE_BEDROCK" in warned.stderr and token not in warned.stderr


def user_settings(home: Path, env: dict) -> Path:
    """Claude Code's user settings file in the test's own user home (``child_env``'s ``HOME``)."""
    path = home.parent / "user-home" / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": env}))
    return path


@pytest.mark.parametrize("value", ["1", 1.0, ["1"]])
def test_print_settings_warns_about_a_switch_in_the_user_settings_file(home, value):
    """D70: the launcher's provider check (``contexto.upstream.cloud_provider``), with its reading of a
    settings file's value, decides the service's warning too."""
    user_settings(home, {"CLAUDE_CODE_USE_BEDROCK": value})
    warned = run_cli(home, "--print-settings")
    assert warned.returncode == 0 and "ANTHROPIC_BASE_URL" in json.loads(warned.stdout)["env"]
    assert "CLAUDE_CODE_USE_BEDROCK" in warned.stderr and "user settings" in warned.stderr
    assert "does not reach contexto serve" in warned.stderr


def test_print_settings_warns_about_a_switch_in_a_project_file_and_an_unreadable_file(home, tmp_path):
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_USE_VERTEX": "true"}}))
    warned = run_cli(home, "--print-settings", cwd=project)
    assert "CLAUDE_CODE_USE_VERTEX" in warned.stderr and ".claude/settings.json" in warned.stderr
    user_settings(home, {})
    (home.parent / "user-home" / ".claude" / "settings.json").write_text('{"env": {"CLAUDE_CODE_USE_MANTLE": 1,}}')
    unread = run_cli(home, "--print-settings")
    assert unread.returncode == 0 and "could not read" in unread.stderr


def test_print_settings_is_quiet_when_a_settings_file_turns_the_switch_off(home):
    user_settings(home, {"CLAUDE_CODE_USE_BEDROCK": "0"})
    assert run_cli(home, "--print-settings").stderr == ""


def test_print_settings_names_a_running_services_port(home, running):
    service = running()
    printed = run_cli(home, "--print-settings")
    assert json.loads(printed.stdout)["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{service.port}/{service.token}"


def test_print_mcp_config_holds_no_token(home):
    printed = run_cli(home, "--print-mcp-config")
    assert printed.returncode == 0 and printed.stderr == ""
    token = (home / serve.TOKEN_FILE).read_text().strip()
    assert json.loads(printed.stdout) == {"mcpServers": {"contexto": {
        "type": "stdio", "command": sys.executable, "args": ["-P", "-m", "contexto", "mcp", "--serve"],
        "env": {"CONTEXTO_HOME": str(home)}}}}
    assert token not in printed.stdout


def test_rotate_token_replaces_the_token_and_says_what_to_do(home, running):
    service = running()
    rotated = run_cli(home, "--rotate-token")
    fresh = (home / serve.TOKEN_FILE).read_text().strip()
    assert rotated.returncode == 0 and fresh != service.token
    assert "restart" in rotated.stdout.lower() and "--print-settings" in rotated.stdout
    assert str(service.pid) in rotated.stdout  # a service is running with the old token
    assert fresh not in rotated.stdout + rotated.stderr and service.token not in rotated.stdout + rotated.stderr
    # The running service keeps the token it started with until it is restarted.
    assert raw_call(service.port, "GET", f"/{service.token}/contexto/health")[0].status == 200
    assert raw_call(service.port, "GET", f"/{fresh}/contexto/health")[0].status == 403
    assert service.stop() == 0


class Listener:
    """A listening socket on 127.0.0.1 that accepts nothing: any connection made to it stays in its
    backlog, so ``connections`` says whether anything connected, however briefly."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]

    def connections(self) -> int:
        count = 0
        while True:
            try:
                self.sock.accept()[0].close()
            except BlockingIOError:
                return count
            count += 1

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def listener():
    made = Listener()
    yield made
    made.close()


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


def test_status_without_a_service_makes_no_request(home, listener):
    status = run_cli(home, "--status")
    assert status.returncode == 1 and status.stdout.strip() == "contexto serve: not running"
    assert not home.exists()  # a status query creates nothing: no home, no token
    # A record left by a killed service: its port now belongs to another program, and its pid to a
    # process that runs (this test). Nothing holds the lock, so nothing is sent to that port.
    token = serve.ensure_token(home)
    write_private(home / serve.STATE_FILE, json.dumps({"port": listener.port, "pid": os.getpid()}))
    status = run_cli(home, "--status")
    assert status.returncode == 1 and status.stdout.startswith("contexto serve: not running")
    assert str(os.getpid()) in status.stdout and token not in status.stdout + status.stderr
    printed = run_cli(home, "--print-settings")
    assert json.loads(printed.stdout)["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{serve.DEFAULT_PORT}/{token}"
    with pytest.raises(serveclient.ResolveError) as refused:
        serveclient.resolve(home, "toolu_01D70stale")
    assert refused.value.kind == serveclient.NOT_RUNNING and "serve.lock" in refused.value.reason
    # The same with a lock file left behind and not held.
    write_private(home / servecontract.LOCK_FILE, "")
    assert run_cli(home, "--status").returncode == 1
    with pytest.raises(serveclient.ResolveError):
        serveclient.resolve(home, "toolu_01D70stale")
    assert listener.connections() == 0


def test_the_service_warns_about_a_switch_in_the_user_settings_file(home, running):
    user_settings(home, {"CLAUDE_CODE_USE_FOUNDRY": True})
    service = running()
    text = until(lambda: (lambda lines: "CLAUDE_CODE_USE_FOUNDRY" in lines and lines)("\n".join(service.stderr_lines)),
                 "the provider warning on the service's stderr")
    assert "user settings" in text and "Microsoft Foundry" in text
    assert service.stop() == 0


def test_the_service_warns_about_a_cloud_provider_and_a_small_bound(home, running):
    service = running("--bound", "60000", env={"CLAUDE_CODE_USE_VERTEX": "1"})
    assert "(bound 60,000, upstream flag)" in service.stderr_lines[0]
    # The two warnings follow the listening line; they are waited for, not given a fixed time.
    text = until(lambda: (lambda lines: "CLAUDE_CODE_USE_VERTEX" in lines and "window budget" in lines and lines)(
        "\n".join(service.stderr_lines)), "both warnings on the service's stderr")
    assert "CLAUDE_CODE_USE_VERTEX" in text and "window budget" in text
    assert service.stop() == 0


# One service per home: the lock


def test_a_second_service_on_the_same_home_is_refused_whatever_its_port(home, running, upstream, make_service):
    first = running()
    for port in ("0", str(first.port)):
        second = run_cli(home, "--port", port, "--upstream", upstream)
        assert second.returncode == 1 and second.stdout == "" and len(second.stderr.splitlines()) == 1, second.stderr
        assert f"contexto serve is already running on 127.0.0.1:{first.port} (pid {first.pid})" in second.stderr
    with pytest.raises(serve.ServeError) as refused:  # the same from the class, in this process
        make_service()
    assert "already running" in str(refused.value) and first.token not in str(refused.value)
    # The first one's record stands, and it is still the one that answers.
    assert json.loads((home / serve.STATE_FILE).read_text()) == {"port": first.port, "pid": first.pid}
    status = run_cli(home, "--status")
    assert status.returncode == 0 and f"running on 127.0.0.1:{first.port} (pid {first.pid}" in status.stdout
    assert first.stop() == 0
    assert not (home / serve.STATE_FILE).exists() and run_cli(home, "--status").returncode == 1
    assert stat.S_IMODE((home / servecontract.LOCK_FILE).stat().st_mode) == 0o600


def test_a_killed_service_leaves_a_record_that_is_never_believed(home, running, listener):
    first = running()
    first.kill()  # SIGKILL: no cleanup runs
    assert json.loads((home / serve.STATE_FILE).read_text())["pid"] == first.pid
    assert not servecontract.service_running(home) and servecontract.running_state(home) is None
    assert run_cli(home, "--status").returncode == 1
    # The record now names a port another program holds; the next service removes it before it binds.
    write_private(home / serve.STATE_FILE, json.dumps({"port": listener.port, "pid": os.getpid()}))
    second = running()
    assert json.loads((home / serve.STATE_FILE).read_text()) == {"port": second.port, "pid": second.pid}
    assert servecontract.running_state(home) == {"port": second.port, "pid": second.pid}
    assert second.stop() == 0 and listener.connections() == 0


def test_a_record_that_is_not_the_users_own_private_file_is_not_read(home, make_service, tmp_path):
    service = make_service()
    assert servecontract.read_state(home) == {"port": service.port, "pid": os.getpid()}
    (home / serve.STATE_FILE).chmod(0o644)
    with pytest.raises(servecontract.ServeFileError) as refused:
        servecontract.read_state(home)
    assert refused.value.kind == "mode" and servecontract.running_state(home) is None
    with pytest.raises(servecontract.ServeFileError):
        servecontract.endpoint(home)
    (home / serve.STATE_FILE).unlink()
    elsewhere = tmp_path / "elsewhere.json"
    write_private(elsewhere, json.dumps({"port": service.port, "pid": os.getpid()}))
    (home / serve.STATE_FILE).symlink_to(elsewhere)
    with pytest.raises(servecontract.ServeFileError) as refused:
        servecontract.read_state(home)
    assert refused.value.kind == "symlink"


def test_a_lock_file_that_is_a_symlink_stops_the_service(home, tmp_path, upstream):
    serve.ensure_token(home)
    (home / servecontract.LOCK_FILE).symlink_to(tmp_path / "elsewhere.lock")
    refused = run_cli(home, "--port", "0", "--upstream", upstream)
    assert refused.returncode == 1 and "serve.lock" in refused.stderr and "Traceback" not in refused.stderr
    assert not (tmp_path / "elsewhere.lock").exists() and not servecontract.service_running(home)


# Routing a teammate (one rule: the request names its parent, and the store records the lead)


def test_a_teammate_before_its_lead_makes_the_one_store_the_lead_then_finds(make_service, home):
    clock = Clock()
    service = make_service(clock=clock, idle_s=10)
    assert turn(service, B, "teammate first", parent=A)[0].status == 200
    assert len(sessions(home)) == 1 and set(served(home)) == {A}  # recorded under the lead, not the teammate
    store = served(home)[A]
    clock.now += 11
    assert service.reap() == 1
    assert turn(service, A, "the lead's own turn")[0].status == 200
    assert turn(service, B, "teammate again", parent=A)[0].status == 200
    assert sessions(home) == [store] and served(home) == {A: store}
    assert len([e for e in session_log(home, store) if e["event"] == "request"]) == 3


def test_a_remembered_parent_steers_count_tokens_only(make_service, home):
    service = make_service()
    turn(service, B)  # B's own conversation
    turn(service, A, "A as a teammate of B", parent=B)
    b_store = served(home)[B]
    assert sessions(home) == [b_store]
    count = json.dumps({"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "x"}], "tools": []})
    assert call(service, "POST", "/v1/messages/count_tokens", count.encode(), headers(A))[0].status == 200
    assert [e["path"] for e in session_log(home, b_store) if e["event"] == "request"][-1] == "/v1/messages/count_tokens"
    # A /v1/messages of A that names no parent is A's own conversation: its own store, and B's is untouched.
    before = (home / "sessions" / b_store / "meta.json").read_bytes()
    assert turn(service, A, "A on its own")[0].status == 200
    mapping = served(home)
    assert set(mapping) == {A, B} and mapping[A] != b_store and mapping[B] == b_store
    assert (home / "sessions" / b_store / "meta.json").read_bytes() == before
    # Its count_tokens now follows it there, and a restart changes nothing: no store records A under B.
    call(service, "POST", "/v1/messages/count_tokens", count.encode(), headers(A))
    settle(service)
    assert [e["path"] for e in session_log(home, mapping[A]) if e["event"] == "request"] \
        == ["/v1/messages", "/v1/messages/count_tokens"]
    service.stop()
    again = make_service()
    turn(again, A, "A after a restart")
    assert served(home) == mapping


# The stores index


def test_a_store_made_after_the_service_started_is_found_and_unchanged_stores_are_not_reread(make_service, home,
                                                                                            monkeypatch):
    for n in range(5):
        store = SessionStore(home, f"old-{n}")
        store.record_cli_session("claude", f"00000000-0000-4000-8000-00000000000{n}", 100.0 + n)
        store.release()
    service = make_service()
    read: list[str] = []
    real = SessionStore.open_existing.__func__
    monkeypatch.setattr(SessionStore, "open_existing",
                        classmethod(lambda cls, home, name: read.append(name) or real(cls, home, name)))
    turn(service, A)  # a new id: nothing is read again, the index has every store as it stands
    assert read == []
    made = SessionStore(home, "made-meanwhile")  # as `contexto run claude` would, while the service runs
    made.record_cli_session("claude", B, 500.0)
    made.release()
    turn(service, B)
    # The store made meanwhile is read, and so is A's (the service changed its meta.json); no old one is.
    assert served(home)[B] == "made-meanwhile" and "made-meanwhile" in read
    assert sorted(read) == sorted(["made-meanwhile", served(home)[A]])
    turn(service, "00000000-0000-4000-8000-000000000003")
    assert [e["event"] for e in session_log(home, "old-3") if e["event"] in ("serve_open", "request")] \
        == ["serve_open", "request"]


# Requests forwarded unbounded are said so


def test_a_messages_request_forwarded_unbounded_is_logged_and_warned_about_once_a_minute(make_service, home):
    clock = Clock()
    stderr = io.StringIO()
    service = make_service(clock=clock, stderr=stderr)

    def round_of_three():
        call(service, "POST", "/v1/messages", body(B), headers(A))  # mismatch
        call(service, "POST", "/v1/messages", body(None), headers(**{"X-Claude-Code-Session-Id": "a/b"}))
        call(service, "POST", "/v1/messages?beta=true", body(None), headers())  # no id at all
        settle(service)

    round_of_three()
    reasons = ["session_id_mismatch", "invalid_session_id", "no_session_id"]
    assert [e["reason"] for e in serve_log(home, "unrouted")] == reasons
    lines = stderr.getvalue().splitlines()
    assert len(lines) == 3 and all(line.startswith("contexto serve: warning: 1 request forwarded unchanged")
                                   for line in lines)
    assert [reason for line in lines for reason in reasons if f"({reason}:" in line] == reasons
    for _ in range(4):
        round_of_three()
    assert len(stderr.getvalue().splitlines()) == 3  # inside the minute: counted, not printed
    clock.now += 61
    round_of_three()
    lines = stderr.getvalue().splitlines()
    assert len(lines) == 6 and all("5 requests forwarded unchanged" in line for line in lines[3:])
    assert len(serve_log(home, "unrouted")) == 18 and sessions(home) == []
    assert service.token not in stderr.getvalue() and A not in stderr.getvalue()
    # A count_tokens no session served is logged and not warned about: nothing unbounded reaches a model.
    call(service, "POST", "/v1/messages/count_tokens", b'{"messages": []}', headers(C))
    settle(service)
    assert serve_log(home, "unrouted")[-1]["reason"] == "no_session" and len(stderr.getvalue().splitlines()) == 6


def test_a_store_that_cannot_be_opened_leaves_no_directory_and_is_not_retried_at_once(make_service, home):
    clock = Clock()
    stderr = io.StringIO()
    tried: list[str] = []

    def failing(store, config):
        tried.append(store.session_id)
        raise OSError(28, "No space left on device")

    service = make_service(clock=clock, stderr=stderr, engine_factory=failing)
    data = json.dumps(json.loads(body(A)), indent=1).encode()
    for _ in range(5):
        response, _ = call(service, "POST", "/v1/messages", data, headers(A))
        assert response.status == 200 and StubUpstream.received[-1][3] == data  # forwarded as it was sent
    settle(service)
    assert len(tried) == 1 and sessions(home) == []  # one attempt, and the directory it made is gone
    assert [e["reason"] for e in serve_log(home, "unrouted")] == ["session_open_failed"] * 5
    failed = serve_log(home, "session_open_failed")
    assert len(failed) == 1 and failed[0]["error"] == "OSError" and failed[0]["errno"] == 28
    assert stderr.getvalue().count("session_open_failed") == 1
    clock.now += serve.OPEN_FAILED_TTL_S + 1
    call(service, "POST", "/v1/messages", data, headers(A))
    settle(service)
    assert len(tried) == 2 and sessions(home) == [] and service.health()["sessions"] == 0


def test_a_failed_open_of_an_existing_store_leaves_it_alone(make_service, home):
    kept = SessionStore(home, "kept")
    kept.record_cli_session("claude", A, 100.0)
    kept.pin("decision", "kept across a failed open")
    kept.release()

    def failing(store, config):
        raise RuntimeError("engine cannot start")

    service = make_service(engine_factory=failing)
    assert post(service, A)[0].status == 200
    settle(service)
    assert sessions(home) == ["kept"] and "kept across a failed open" in SessionStore(home, "kept").state_text()


# The session cap


def test_a_request_at_the_cap_waits_for_a_session_to_finish_and_is_then_bounded(make_service, home):
    """The window the eviction tests used to fall into: another session's response is complete and
    its lease not yet returned. The request waits for the lease, not for a length of time."""
    stderr = io.StringIO()
    service = make_service(max_sessions=1, cap_wait_s=60, stderr=stderr)
    turn(service, A)
    held = service.acquire(A, create=True)  # A has a request in flight, as far as the service knows
    done: list[int] = []
    waiter = threading.Thread(target=lambda: done.append(post(service, B)[0].status))
    waiter.start()
    try:
        until(lambda: service._cap_waiting == 1, "B's request to wait for a session")
        assert not done and set(served(home)) == {A}
    finally:
        service.release(held)
        waiter.join(30)
    settle(service)
    assert done == [200] and set(served(home)) == {A, B} and service.health()["sessions"] == 1
    # Nothing was forwarded unbounded; the only warning is the missing-tools one A's close prints.
    assert serve_log(home, "unrouted") == [] and "forwarded unchanged" not in stderr.getvalue()
    assert [e["reason"] for e in session_log(home, served(home)[A]) if e["event"] == "serve_close"] == ["cap"]


def test_waits_at_the_cap_do_not_add_up_for_requests_of_one_id(make_service, home):
    """Six first requests of one new id at once, with the one open session busy: each waits for the
    ``cap_wait_s`` it was given from when it arrived, not that long again behind each request queued
    for the same id before it."""
    service = make_service(max_sessions=1, cap_wait_s=1.0)
    turn(service, A)
    held = service.acquire(A, create=True)  # A has a request in flight
    took: list[float] = []

    def first_request() -> None:
        started = time.monotonic()
        assert post(service, B)[0].status == 200
        took.append(time.monotonic() - started)

    threads = [threading.Thread(target=first_request) for _ in range(6)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
    finally:
        service.release(held)
    assert len(took) == 6
    # One cap_wait_s for all of them; queued one after another they would take up to 6 s.
    assert max(took) < 3 * service.cap_wait_s, sorted(took)
    assert [e["reason"] for e in serve_log(home, "unrouted", 6)] == ["session_cap"] * 6


def test_a_store_made_by_a_request_that_loses_the_race_with_shutdown_is_removed(make_service, home):
    """A request that began to open a new store before ``stop()`` and finished after it: nothing is
    added to the registry, and the store, which no Claude Code id leads to yet, is removed again."""
    service = None

    def stopping(store, config):
        with service._lock:  # what stop() does between this open's start and its insertion
            service._stopping = True
        return build_engine(store, config)

    service = make_service(engine_factory=stopping)
    with pytest.raises(serve._Unrouted) as unrouted:
        service.acquire(A, create=True)
    assert unrouted.value.reason == "stopping" and service._sessions == {} and service._routes == {}
    assert sessions(home) == []
    logged = [e for e in serve_log(home) if e["event"].startswith("session_")]
    assert [(e["event"], e["reason"]) for e in logged] == [("session_discarded", "stopping")]


def test_an_existing_store_reopened_during_shutdown_is_closed_and_kept(make_service, home):
    service = None
    clock = Clock()
    stop_on_open = []

    def engine(store, config):
        if stop_on_open:
            with service._lock:
                service._stopping = True
        return build_engine(store, config)

    service = make_service(engine_factory=engine, clock=clock, idle_s=10)
    turn(service, A)
    name = served(home)[A]
    clock.now += 11
    assert service.reap() == 1
    stop_on_open.append(True)
    with pytest.raises(serve._Unrouted):
        service.acquire(A, create=True)
    assert sessions(home) == [name] and served(home) == {A: name}
    assert [e["reason"] for e in session_log(home, name) if e["event"] == "serve_close"] == ["idle", "shutdown"]


def test_during_the_drain_a_request_on_a_kept_connection_is_refused_and_routed_nowhere(make_service, home,
                                                                                    tool_upstream):
    service = make_service(upstream=tool_upstream)
    kept = http.client.HTTPConnection("127.0.0.1", service.port, timeout=30)
    kept.request("POST", f"/{service.token}/v1/messages?beta=true", body(A), headers(A))
    first = kept.getresponse()
    assert first.status == 200 and first.read()
    # A streamed request whose response upstream holds open: the drain waits for it.
    streamed: list = []
    stream = threading.Thread(target=lambda: streamed.append(post(service, B, stream=True)[0].status))
    stream.start()
    assert ToolUpstream.block_sent.wait(30)
    stopper = threading.Thread(target=service.stop)
    stopper.start()
    try:
        until(lambda: not (home / serve.STATE_FILE).exists(), "the service to stop advertising itself")
        kept.request("POST", f"/{service.token}/v1/messages?beta=true", body(C), headers(C))
        refused = kept.getresponse()
        answer = json.loads(refused.read())
        assert refused.status == 503 and refused.getheader("Connection") == "close"
        assert answer["error"]["message"] == serve.STOPPING_MESSAGE
        assert C not in served(home) and len(ToolUpstream.received) == 2  # A's, and B's held one
        assert stopper.is_alive()  # still draining: B is in flight
    finally:
        ToolUpstream.gate.set()
        stopper.join(30)
        stream.join(30)
        kept.close()
    assert streamed == [200]
    stopped = serve_log(home, "serve_stop", 1)[-1]
    assert stopped["in_flight"] == 0 and "hurried" not in stopped and stopped["sessions_closed"] == 2


def test_during_the_drain_a_request_that_expects_100_continue_is_refused_without_waiting_for_its_body(
        make_service, home, tool_upstream):
    service = make_service(upstream=tool_upstream)
    kept = socket.create_connection(("127.0.0.1", service.port), timeout=30)
    kept.sendall(health_request(service))
    assert one_response(kept).startswith(b"HTTP/1.1 200 ")
    streamed = threading.Thread(target=lambda: post(service, B, stream=True))
    streamed.start()
    assert ToolUpstream.block_sent.wait(30)
    stopper = threading.Thread(target=service.stop)
    stopper.start()
    try:
        until(lambda: not (home / serve.STATE_FILE).exists(), "the service to stop advertising itself")
        kept.sendall(b"POST /%s/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Claude-Code-Session-Id: %s\r\n"
                     b"Expect: 100-continue\r\nContent-Length: 5000\r\n\r\n" % (service.token.encode(), C.encode()))
        answer = closed_within(kept)  # the body is never sent: the answer must not wait for it
        assert answer.startswith(b"HTTP/1.1 503 ") and b"100 Continue" not in answer
        assert serve.STOPPING_MESSAGE.encode() in answer
    finally:
        ToolUpstream.gate.set()
        stopper.join(30)
        streamed.join(30)
        kept.close()


def test_shutdown_under_keep_alive_load_finishes_what_is_in_flight_and_leaves_no_stray_store(make_service, home):
    """Clients on kept-alive connections send requests back to back, a new conversation every third
    request, while the service stops: the drain covers only the requests already in flight, every
    session is closed with none in flight, and every store left has the metadata that leads to it."""
    service = make_service()
    answers: list[list] = [[] for _ in range(6)]
    stopped_at: list[int] = []  # each client's answer count when stop() returned

    def client(n: int) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=30)
        try:
            for i in range(100_000):
                if stopped_at and len(answers[n]) > max(stopped_at):  # a client that was never refused stops too
                    return
                session = f"11111111-2222-4333-8444-{n:04d}{i // 3:08d}"
                try:
                    conn.request("POST", f"/{service.token}/v1/messages?beta=true", body(session), headers(session))
                    response = conn.getresponse()
                    response.read()
                except (OSError, http.client.HTTPException) as exc:
                    answers[n].append(type(exc).__name__)
                    return
                answers[n].append(response.status)
                if response.status != 200:
                    return
        finally:
            conn.close()

    threads = [threading.Thread(target=client, args=(n,)) for n in range(len(answers))]
    for thread in threads:
        thread.start()
    until(lambda: all(len(mine) >= 3 for mine in answers), "every client to have three answers")
    started = time.monotonic()
    service.stop()
    took = time.monotonic() - started
    stopped_at.append(max(len(mine) for mine in answers))
    for thread in threads:
        thread.join(60)
    stopped = serve_log(home, "serve_stop", 1)[-1]
    assert stopped["in_flight"] == 0 and "hurried" not in stopped, stopped
    assert took < serve.STOP_DRAIN_S, f"the drain ran out ({took:.2f} s)"
    for mine in answers:  # 200s, then at most one clean refusal or a closed connection
        assert set(mine[:-1]) == {200} and mine[-1] in (200, 503, "RemoteDisconnected", "ConnectionResetError",
                                                        "BrokenPipeError"), mine
    stray = [name for name in sessions(home) if not (home / "sessions" / name / "meta.json").exists()]
    assert stray == []
    logged = [e["event"] for e in serve_log(home)]
    assert logged.count("session_open") == logged.count("session_close")  # a discarded store was never opened
    assert stopped["sessions_closed"] == logged.count("session_open") - sum(
        1 for e in serve_log(home, "session_close") if e["reason"] != "shutdown")


def test_the_missing_tools_warning_is_printed_once_per_session_however_often_it_reopens(make_service, home):
    clock = Clock()
    stderr = io.StringIO()
    service = make_service(clock=clock, stderr=stderr, idle_s=10)
    extra: list = []
    for n in range(4):
        turn(service, A, extra=list(extra))
        extra += [{"role": "assistant", "content": [{"type": "text", "text": f"reply {n}"}]},
                  {"role": "user", "content": [{"type": "text", "text": f"turn {n + 2}"}]}]
        clock.now += 11
        assert service.reap() == 1
    name = served(home)[A]
    assert len([e for e in session_log(home, name) if e["event"] == "serve_open"]) == 4
    warnings = [line for line in stderr.getvalue().splitlines() if "MCP" in line]
    assert len(warnings) == 1 and warnings[0].startswith(f"contexto serve: session {name}: warning: "), warnings


# The tool-call map: registered before the call reaches the client, whatever the framing


class Framed(BaseHTTPRequestHandler):
    """Answers a Messages request with one tool call, framed as ``framing`` says: ``chunked`` (an SSE
    stream the proxy holds), ``length`` (an SSE body with a Content-Length), ``gzip`` (an SSE stream
    upstream encoded although it was asked not to), ``json-chunked`` or ``json-length`` (a JSON body
    followed by whitespace in parts of its own, so the body is valid JSON before its last bytes). No
    event carries ``usage`` unless ``usage`` is set.

    The response goes out in three parts. After the first (a stream's events up to the tool call's
    ``content_block_stop``, or the whole JSON) upstream sets ``block_sent`` and waits for ``gates[0]``.
    A JSON body's second part is followed by a wait for ``gates[1]``, at most ``end_wait`` seconds,
    which a client that acts on the call opens once it has acted: so a client that has the whole body
    before the response has ended asks resolve while upstream still holds the end. A stream needs no
    such wait, since its call is complete at ``content_block_stop``."""

    protocol_version = "HTTP/1.1"
    framing = "chunked"
    usage = False
    tool_use_id = "toolu_01D70framed"
    gates = (threading.Event(), threading.Event())
    block_sent = threading.Event()
    end_wait = 1.0

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        cls = type(self)
        answer = tool_response(cls.tool_use_id)
        if not cls.usage:
            del answer["usage"]
        if cls.framing.startswith("json"):
            parts, content_type = [json.dumps(answer).encode(), b"\n", b" "], "application/json"
        else:
            delta = {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}
            if cls.usage:
                delta["usage"] = {"output_tokens": 5}
            parts = [b"".join(sse(event) for event in [
                         {"type": "message_start", "message": {**answer, "content": [], "stop_reason": None}},
                         {"type": "content_block_start", "index": 0, "content_block": answer["content"][0]},
                         {"type": "content_block_delta", "index": 0,
                          "delta": {"type": "input_json_delta", "partial_json": "{}"}},
                         {"type": "content_block_stop", "index": 0}]),
                     sse(delta), sse({"type": "message_stop"})]
            content_type = "text/event-stream"
        compressor = __import__("zlib").compressobj(9, 8, 16 + 15) if cls.framing == "gzip" else None
        if compressor is not None:
            parts = [compressor.compress(part) + compressor.flush(2) for part in parts]  # Z_SYNC_FLUSH
            parts[-1] += compressor.flush()
        length = cls.framing in ("length", "json-length")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if compressor is not None:
            self.send_header("Content-Encoding", "gzip")
        if length:
            self.send_header("Content-Length", str(sum(len(part) for part in parts)))
        else:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for index, part in enumerate(parts):
            self.wfile.write(part if length else b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.flush()
            if index == 0:
                cls.block_sent.set()
                cls.gates[0].wait(30)
            elif index == 1 and content_type == "application/json":
                cls.gates[1].wait(cls.end_wait)
        if not length:
            self.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def framed():
    Framed.gates, Framed.block_sent = (threading.Event(), threading.Event()), threading.Event()
    Framed.framing, Framed.usage = "chunked", False
    server = ThreadingHTTPServer(("127.0.0.1", 0), Framed)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    for gate in Framed.gates:
        gate.set()
    server.shutdown()
    server.server_close()


def open_gates() -> None:
    """Let ``Framed`` send each response straight through, for a test about something else."""
    for gate in Framed.gates:
        gate.set()


def has_call(seen: bytes, sse_body: bool) -> bool:
    """Whether a client that has read ``seen`` has the whole tool call and would start it: a stream's
    ``content_block_stop`` event with its blank line, or a JSON body that parses."""
    if not sse_body:
        try:
            return isinstance(json.loads(seen), dict)
        except ValueError:
            return False
    *complete, _ = seen.replace(b"\r\n", b"\n").split(b"\n\n")
    return any(b'"type": "content_block_stop"' in event for event in complete)


@pytest.mark.parametrize("framing, usage", [("chunked", False), ("chunked", True), ("length", False),
                                            ("length", True), ("gzip", False), ("json-chunked", False),
                                            ("json-chunked", True), ("json-length", False)])
def test_a_tool_call_resolves_the_moment_the_client_has_it_in_every_framing(make_service, home, framed,
                                                                            framing, usage):
    """The client reads the response as a real one does and asks resolve, once and without waiting,
    the moment it has the whole call; nothing in the test waits for the service to register the id.
    Registration that lags the bytes the client has fails here: upstream is slow to end every
    response, so an id registered only at the end of the response is not there yet."""
    Framed.framing, Framed.usage = framing, usage
    service = make_service(upstream=framed)
    sse_body = not framing.startswith("json")
    seen = bytearray()
    at_call: list = []

    def client():
        conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=60)
        conn.request("POST", f"/{service.token}/v1/messages?beta=true", body(A, stream=sse_body), headers(A))
        response = conn.getresponse()
        inflate = __import__("zlib").decompressobj(16 + 15) if framing == "gzip" else None
        while chunk := response.read1(65536):
            seen.extend(inflate.decompress(chunk) if inflate else chunk)
            if not at_call and has_call(bytes(seen), sse_body):
                at_call.append(resolve(service, Framed.tool_use_id))
                Framed.gates[1].set()  # acted on: upstream may end the response now
        conn.close()

    thread = threading.Thread(target=client)
    thread.start()
    try:
        assert Framed.block_sent.wait(30)
        Framed.gates[0].set()  # at once: the client gets whatever the proxy passes on
        thread.join(60)
    finally:
        for gate in Framed.gates:
            gate.set()
        thread.join(30)
    assert at_call == [(200, {"session": served(home)[A]})], bytes(seen[-300:])
    settle(service)
    logged = [e["event"] for e in session_log(home, served(home)[A])]
    assert ("hold_skipped" in logged) == (framing in ("length", "gzip"))


def test_an_id_two_sessions_registered_is_ambiguous_and_resolves_as_unknown(make_service, home, framed):
    Framed.framing = "json-length"  # an upstream that repeats its ids: every response carries the same one
    open_gates()
    service = make_service(upstream=framed)
    turn(service, A)
    assert resolve(service, Framed.tool_use_id) == (200, {"session": served(home)[A]})
    turn(service, B)
    assert resolve(service, Framed.tool_use_id) == (404, {"error": "unknown_tool_use"})
    turn(service, A)  # and it stays so: the first session does not get it back
    assert resolve(service, Framed.tool_use_id) == (404, {"error": "unknown_tool_use"})
    logged = serve_log(home, "tool_use_ambiguous")
    assert logged == [{"ts": logged[0]["ts"], "event": "tool_use_ambiguous", "count": 1}]
    assert Framed.tool_use_id not in (home / "serve.log").read_text()
    with pytest.raises(serveclient.ResolveError) as refused:  # so the MCP server fails closed
        serveclient.resolve(home, Framed.tool_use_id, sleep=lambda seconds: None)
    assert refused.value.kind == serveclient.UNKNOWN_TOOL_USE


def test_the_mcp_clients_retry_covers_an_id_registered_after_its_first_try(make_service, home):
    """The race the retry exists for: the call reaches the MCP server before the service has read it
    (a response upstream encoded, read from a decoded side copy). The id is registered while the
    client waits between tries, and the client is the real one, asking the real service."""
    service = make_service()
    turn(service, A)
    name = served(home)[A]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            service.register_tool_calls(("toolu_01D70late",), name)

    assert serveclient.resolve(home, "toolu_01D70late", sleep=sleep) == name
    assert slept == list(serveclient.RETRY_DELAYS_S[:2])
    # The whole window is 1.9 s of waiting: an id registered later than that is refused, not waited for.
    with pytest.raises(serveclient.ResolveError) as refused:
        serveclient.resolve(home, "toolu_01D70never", sleep=slept.append)
    assert refused.value.kind == serveclient.UNKNOWN_TOOL_USE and sum(serveclient.RETRY_DELAYS_S) <= 2.0
    assert sessions(home) == [name]


def test_a_tool_call_in_a_response_to_an_unrouted_request_is_never_registered(make_service, home, framed):
    Framed.framing = "json-length"
    open_gates()
    service = make_service(upstream=framed)
    assert call(service, "POST", "/v1/messages", body(None), headers())[0].status == 200  # no session id
    assert resolve(service, Framed.tool_use_id) == (404, {"error": "unknown_tool_use"})


# Before the token check: one answer, bounded logging, a deadline and a cap


def raw(port: int, data: bytes, timeout: float = 30) -> bytes:
    """Send ``data`` on a new connection and read to the end of the stream."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        if data:
            sock.sendall(data)
        got = bytearray()
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            got += chunk
        return bytes(got)


def refusal(service) -> bytes:
    """The bytes of the one 403, as an ordinary request without the token gets them."""
    got = raw(service.port, b"GET /not-the-token/x HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    head, _, payload = got.partition(b"\r\n\r\n")
    assert head.splitlines() == [b"HTTP/1.1 403 Forbidden", b"Content-Type: application/json",
                                 b"Content-Length: %d" % len(payload)] and json.loads(payload) == REFUSED
    return got


def unparseable(token: str) -> dict[str, bytes]:
    """Requests ``http.server`` answers itself, before any check: each carries the token, so an echo
    of the request line would show it."""
    target = f"/{token}/v1/messages".encode()
    host = b"Host: 127.0.0.1\r\n"
    return {
        "no version": b"GET " + target + b"\r\n\r\n",
        "no version, not GET": b"POST " + target + b"\r\n\r\n",
        "one word": b"GET\r\n\r\n",
        "four words": b"GET " + target + b" HTTP/1.1 extra\r\n" + host + b"\r\n",
        "bad version": b"GET " + target + b" HTTP/9.9\r\n" + host + b"\r\n",
        "version 2": b"GET " + target + b" HTTP/2.0\r\n" + host + b"\r\n",
        "not http": b"GET " + target + b" FTP/1.1\r\n" + host + b"\r\n",
        "unsupported method": b"CONNECT " + target + b" HTTP/1.1\r\n" + host + b"\r\n",
        "trace": b"TRACE " + target + b" HTTP/1.1\r\n" + host + b"\r\n",
        "unknown method": b"BREW " + target + b" HTTP/1.1\r\n" + host + b"\r\n",
        "too many headers": b"GET " + target + b" HTTP/1.1\r\n" + host + b"".join(
            b"X-Filler-%d: x\r\n" % n for n in range(150)) + b"\r\n",
        "over-long header": b"GET " + target + b" HTTP/1.1\r\n" + host + b"X-Long: " + b"x" * 70_000 + b"\r\n\r\n",
        "over-long target": b"GET " + target + b"?" + b"x" * 70_000 + b" HTTP/1.1\r\n" + host + b"\r\n",
        "garbage": b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + b"\r\n\r\n",
    }


def test_every_answer_before_the_checks_is_the_same_403(make_service, home):
    clock = Clock()
    service = make_service(clock=clock)
    expected = refusal(service)
    for what, data in unparseable(service.token).items():
        got = raw(service.port, data)
        assert got == expected, (what, got[:200])
    # Nothing of http.server's own: no HTML, no library or Python version, no echo of the request.
    assert b"Server:" not in expected and b"Python" not in expected and b"<" not in expected
    # Expect: 100-continue gets no interim answer unless the request passes the checks.
    expecting = (b"POST /nope/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nExpect: 100-continue\r\n"
                 b"Content-Length: 0\r\n\r\n")
    assert raw(service.port, expecting) == expected
    data = body(A)
    passing = (b"POST /%s/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Claude-Code-Session-Id: %s\r\n"
               b"Expect: 100-continue\r\nConnection: close\r\nContent-Length: %d\r\n\r\n%s"
               % (service.token.encode(), A.encode(), len(data), data))
    got = raw(service.port, passing)
    assert got.startswith(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 "), got[:120]
    settle(service)
    assert StubUpstream.received[-1][3] == data and len(StubUpstream.received) == 1
    # All of it is one line in the log (the clock stands still), with no path, method or version.
    refused = serve_log(home, "request_refused")
    assert len(refused) == 1 and set(refused[0]) == {"ts", "event", "reason"}
    assert not serve_log(home, "serve_error") and service.token not in (home / "serve.log").read_text()


def test_thousands_of_unparseable_requests_write_a_bounded_number_of_log_lines(make_service, home):
    clock = Clock()
    service = make_service(clock=clock)
    expected = refusal(service)
    before = len((home / "serve.log").read_text().splitlines())
    sent = 2000
    for n in range(sent):
        got = raw(service.port, b"GET /x\r\n\r\n" if n % 2 else b"NOPE\r\n\r\n")
        assert got == expected
    settle(service)
    assert len((home / "serve.log").read_text().splitlines()) == before  # all counted on the line already there
    clock.now += 2
    raw(service.port, b"GET /x\r\n\r\n")
    refused = serve_log(home, "request_refused")
    assert len(refused) == 2 and refused[1]["reason"] == "malformed" and refused[1]["not_logged"] == sent
    assert not serve_log(home, "serve_error")


def test_a_failure_before_the_token_check_is_refused_and_logged_through_the_limiter(make_service, home, monkeypatch):
    clock = Clock()
    service = make_service(clock=clock)
    expected = refusal(service)

    def broken(self):
        raise RuntimeError("a bug in a check")

    monkeypatch.setattr(serve._ServeHandler, "_refusal", broken)
    for _ in range(20):
        assert raw(service.port, f"GET /{service.token}/v1/models HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()) \
            == expected
    monkeypatch.undo()
    clock.now += 2
    raw(service.port, b"GET /x HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    refused = serve_log(home, "request_refused")
    assert [e["reason"] for e in refused] == ["token", "token"] and refused[1]["not_logged"] == 20
    assert not serve_log(home, "serve_error") and StubUpstream.received == []


def closed_within(sock: socket.socket, timeout: float = 30) -> bytes:
    """Everything the service sends on ``sock`` until it closes it; fails if it is still open at ``timeout``."""
    sock.settimeout(timeout)
    got = bytearray()
    while True:
        try:
            chunk = sock.recv(65536)
        except ConnectionResetError:
            break
        if not chunk:
            break
        got += chunk
    return bytes(got)


def test_a_connection_that_has_not_passed_the_check_ends_at_its_deadline(make_service, home):
    service = make_service(unchecked_deadline_s=0.4)
    expected = refusal(service)
    started = time.monotonic()
    silent = socket.create_connection(("127.0.0.1", service.port))
    slow_line = socket.create_connection(("127.0.0.1", service.port))
    slow_line.sendall(b"GET /" + service.token.encode()[:10])  # a request line that never ends
    slow_head = socket.create_connection(("127.0.0.1", service.port))
    slow_head.sendall(b"GET /%s/contexto/health HTTP/1.1\r\nHost: 127.0.0.1\r\n" % service.token.encode())
    no_body = socket.create_connection(("127.0.0.1", service.port))
    no_body.sendall(b"POST /nope/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 100000\r\n\r\n")
    no_chunks = socket.create_connection(("127.0.0.1", service.port))
    no_chunks.sendall(b"POST /nope/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n")
    try:
        assert closed_within(silent) == b""  # nothing was asked, so nothing is answered
        assert closed_within(slow_line) == expected
        # A head with the right token that never ends is not served: the connection had not passed.
        assert closed_within(slow_head) == b""
        assert closed_within(no_body) == expected and closed_within(no_chunks) == expected
    finally:
        for sock in (silent, slow_line, slow_head, no_body, no_chunks):
            sock.close()
    assert time.monotonic() - started >= 0.4
    until(lambda: not service._unchecked, "the service to forget the closed connections")
    assert StubUpstream.received == [] and sessions(home) == []
    assert {e["reason"] for e in serve_log(home, "request_refused")} <= {"token", "deadline", "malformed", "host"}


def one_response(sock: socket.socket) -> bytes:
    """One whole response with a ``Content-Length`` from a connection kept open: its head, then exactly
    the body it declares, however the bytes were split on the way."""
    got = bytearray()
    while b"\r\n\r\n" not in got:
        chunk = sock.recv(65536)
        assert chunk, f"the connection closed after {bytes(got)!r}"
        got += chunk
    head, _, rest = bytes(got).partition(b"\r\n\r\n")
    length = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                  if line.lower().startswith(b"content-length:"))
    while len(rest) < length:
        chunk = sock.recv(65536)
        assert chunk, "the connection closed inside the body"
        rest += chunk
    assert len(rest) == length, "more bytes than one response"
    return head + b"\r\n\r\n" + rest


def test_a_connection_that_passed_the_check_has_no_deadline(make_service, home, tool_upstream):
    service = make_service(upstream=tool_upstream, unchecked_deadline_s=0.3)
    token = service.token.encode()
    # One connection kept alive between requests, and one request whose response upstream holds open.
    kept = socket.create_connection(("127.0.0.1", service.port), timeout=30)
    health = b"GET /%s/contexto/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % token
    kept.sendall(health)
    assert one_response(kept).startswith(b"HTTP/1.1 200 ")
    stream = socket.create_connection(("127.0.0.1", service.port), timeout=30)
    data = body(A, stream=True)
    stream.sendall(b"POST /%s/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Claude-Code-Session-Id: %s\r\n"
                   b"Connection: close\r\nContent-Length: %d\r\n\r\n%s" % (token, A.encode(), len(data), data))
    assert ToolUpstream.block_sent.wait(30)
    # Two deadlines go by, measured by connections that say nothing and are closed for it.
    for _ in range(2):
        with socket.create_connection(("127.0.0.1", service.port)) as silent:
            assert closed_within(silent) == b""
    kept.sendall(health)
    assert one_response(kept).startswith(b"HTTP/1.1 200 ")
    ToolUpstream.gate.set()
    answer = closed_within(stream)
    assert answer.startswith(b"HTTP/1.1 200 ") and b"message_stop" in answer and answer.endswith(b"0\r\n\r\n")
    # A later request on the kept connection is still checked, and a refused one is under a deadline again.
    kept.sendall(b"POST /nope HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5000\r\n\r\n")
    assert closed_within(kept) == refusal(service)
    kept.close()
    stream.close()


def still_open(sock: socket.socket) -> bool:
    """Whether the service has not closed ``sock``: nothing to read yet, and no end of stream."""
    sock.setblocking(False)
    try:
        return sock.recv(1, socket.MSG_PEEK) != b""
    except BlockingIOError:
        return True
    except ConnectionResetError:
        return False
    finally:
        sock.setblocking(True)


def unchecked(service, count: int) -> socket.socket:
    """A new silent connection, once the service counts ``count`` unchecked ones: opened one at a time,
    so their order is the order the service saw them in."""
    sock = socket.create_connection(("127.0.0.1", service.port), timeout=30)
    until(lambda: len(service._unchecked) == count, f"{count} unchecked connections")
    return sock


def health_request(service) -> bytes:
    return b"GET /%s/contexto/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % service.token.encode()


def test_at_the_cap_the_oldest_unchecked_connection_is_closed_to_admit_a_new_one(make_service, home):
    service = make_service(unchecked_deadline_s=60, max_unchecked=4)  # no deadline comes in this test
    silent = [unchecked(service, n + 1) for n in range(4)]
    try:
        with socket.create_connection(("127.0.0.1", service.port), timeout=30) as holder:
            holder.sendall(health_request(service))
            assert one_response(holder).startswith(b"HTTP/1.1 200 ")
        assert closed_within(silent[0]) == b""  # the oldest made room, unanswered
        assert [still_open(sock) for sock in silent[1:]] == [True, True, True]
        # Once the cap is full again, the next connection takes the place of the oldest one left.
        fill = unchecked(service, 4)
        with socket.create_connection(("127.0.0.1", service.port), timeout=30):
            assert closed_within(silent[1]) == b""
        assert [still_open(sock) for sock in (*silent[2:], fill)] == [True, True, True]
        fill.close()
    finally:
        for sock in silent:
            sock.close()
    assert {e["reason"] for e in serve_log(home, "request_refused")} == {"connection_cap"}


def test_a_connection_whose_request_line_carries_the_token_is_not_the_one_closed(make_service, home):
    """The oldest unchecked connection is closed only when it has not shown the token: a client whose
    request line has arrived and carries it keeps its place, even while the rest of its head is on the
    way, and the oldest silent one goes instead."""
    service = make_service(unchecked_deadline_s=60, max_unchecked=3)
    holder = socket.create_connection(("127.0.0.1", service.port), timeout=30)
    holder.sendall(b"GET /%s/contexto/health HTTP/1.1\r\n" % service.token.encode())  # the head is not over
    until(lambda: len(service._unchecked) == 1, "the token holder to be counted")
    silent = [unchecked(service, 2), unchecked(service, 3)]
    try:
        with socket.create_connection(("127.0.0.1", service.port), timeout=30) as newer:
            assert closed_within(silent[0]) == b""
            assert still_open(holder) and still_open(silent[1]) and still_open(newer)
        holder.sendall(b"Host: 127.0.0.1\r\n\r\n")
        assert one_response(holder).startswith(b"HTTP/1.1 200 ")
    finally:
        holder.close()
        for sock in silent:
            sock.close()


def test_when_every_unchecked_connection_carries_the_token_a_new_one_is_closed(make_service, home):
    service = make_service(unchecked_deadline_s=60, max_unchecked=2)
    holders = []
    try:
        for n in range(2):
            holders.append(socket.create_connection(("127.0.0.1", service.port), timeout=30))
            holders[-1].sendall(b"GET /%s/contexto/health HTTP/1.1\r\n" % service.token.encode())
            until(lambda count=n + 1: len(service._unchecked) == count, "the token holders to be counted")
        with socket.create_connection(("127.0.0.1", service.port), timeout=30) as extra:
            assert closed_within(extra) == b""
        for holder in holders:
            holder.sendall(b"Host: 127.0.0.1\r\n\r\n")
            assert one_response(holder).startswith(b"HTTP/1.1 200 ")
    finally:
        for holder in holders:
            holder.close()


def flood(port: int, stop: threading.Event, opened: list[int], *, keep: int, burst: bool) -> None:
    """A local process without the token. It keeps ``keep`` silent connections open and reopens each
    one the service closes; with ``burst`` it also opens new ones as fast as it can, closing its own
    oldest past ``4 * keep`` so its descriptors stay bounded."""
    held: list[socket.socket] = []
    try:
        while not stop.is_set():
            for sock in [sock for sock in held if not still_open(sock)]:
                held.remove(sock)
                sock.close()
            if burst or len(held) < keep:
                try:
                    held.append(socket.create_connection(("127.0.0.1", port), timeout=5))
                    opened[0] += 1
                except OSError:
                    pass
            else:
                time.sleep(0.005)  # the cap is full and nothing was closed: look again shortly
            while len(held) > 4 * keep:
                held.pop(0).close()
    finally:
        for sock in held:
            sock.close()


@pytest.mark.parametrize("burst", [False, True], ids=["refill", "burst"])
def test_a_process_without_the_token_cannot_keep_a_token_holder_out(make_service, home, burst):
    """Round-2 review: with the service's own limits (64 unchecked connections, 5 s each), a process that
    holds the cap full and reopens every connection the service closes, or opens connections as fast
    as it can, must not keep a token holder out. Each of 20 health requests, on a new connection
    each, is answered, and within 2.5 s: half the 5 s a silent connection is given, which a client
    that had to wait for a free place would need."""
    answer_bound_s = 2.5
    service = make_service()
    assert (service.max_unchecked, service.unchecked_deadline_s) == (64, 5.0)
    stop, opened = threading.Event(), [0]
    flooders = [threading.Thread(target=flood, args=(service.port, stop, opened),
                                 kwargs={"keep": 70, "burst": burst}, daemon=True) for _ in range(2 if burst else 1)]
    for flooder in flooders:
        flooder.start()
    took: list[float] = []
    try:
        until(lambda: len(service._unchecked) >= service.max_unchecked, "the flood to fill the cap")
        for _ in range(20):
            started = time.monotonic()
            with socket.create_connection(("127.0.0.1", service.port), timeout=30) as holder:
                holder.sendall(health_request(service))
                answer = one_response(holder)
            took.append(time.monotonic() - started)
            assert answer.startswith(b"HTTP/1.1 200 ")
    finally:
        stop.set()
        for flooder in flooders:
            flooder.join(30)
    assert max(took) < answer_bound_s, f"slowest answer {max(took):.3f} s; {opened[0]} connections opened"
    assert StubUpstream.received == [] and sessions(home) == []


def test_a_request_header_that_holds_the_token_is_not_forwarded(make_service, home):
    service = make_service()
    url = serve.base_url(service.port, service.token)
    response, _ = call(service, "POST", "/v1/messages?beta=true", body(A),
                       {**headers(A), "Referer": url + "/v1/messages", "X-Debug-Url": f"see {url}", "X-Kept": "plain"})
    assert response.status == 200
    settle(service)
    _, path, sent, _ = StubUpstream.received[0]
    assert "Referer" not in sent and "X-Debug-Url" not in sent and sent["X-Kept"] == "plain"
    assert service.token not in path and not any(service.token in value for value in sent.values())
    assert sent["x-api-key"] == SECRET and sent["X-Claude-Code-Session-Id"] == A
    dropped = serve_log(home, "token_header_dropped")
    assert len(dropped) == 1 and set(dropped[0]) == {"ts", "event", "count"} and dropped[0]["count"] == 2
    log = (home / "serve.log").read_text()
    assert service.token not in log and "Referer" not in log


def test_a_request_header_whose_name_holds_the_token_is_not_forwarded(make_service, home):
    service = make_service()
    response, _ = call(service, "POST", "/v1/messages?beta=true", body(A),
                       {**headers(A), f"X-{service.token}": "1", f"{service.token}-Trace": "on", "X-Kept": "plain"})
    assert response.status == 200
    settle(service)
    _, _, sent, _ = StubUpstream.received[0]
    assert not any(service.token in name or service.token in value for name, value in sent.items())
    assert sent["X-Kept"] == "plain" and sent["X-Claude-Code-Session-Id"] == A
    dropped = serve_log(home, "token_header_dropped")
    assert len(dropped) == 1 and set(dropped[0]) == {"ts", "event", "count"} and dropped[0]["count"] == 2
    assert service.token not in (home / "serve.log").read_text()


# Recognising the service's own address


@pytest.mark.parametrize("host, loopback", [
    ("127.0.0.1", True), ("127.1", True), ("2130706433", True), ("0x7f.1", True), ("0x7f000001", True),
    ("0177.0.0.1", True), ("[::1]", True), ("::1", True), ("localhost", True), ("LOCALHOST.", True),
    ("app.localhost", True), ("127.9.9.9", True), ("0.0.0.0", True), ("::", True), ("::ffff:127.0.0.1", True),
    ("[::ffff:7f00:1]", True), ("0", True),
    ("api.anthropic.com", False), ("128.0.0.1", False), ("10.0.0.1", False), ("2130706432.example", False),
    ("localhost.example", False), ("", False), ("::2", False), ("1.2.3.4.5", False),
])
def test_loopback_addresses_are_recognised_in_every_spelling(host, loopback):
    assert servecontract.is_loopback(host) is loopback


@pytest.mark.parametrize("url", ["http://127.1:7837", "http://2130706433:7837/x", "http://0x7f.1:7837",
                                 "http://[::1]:7837", "http://localhost:7837", "http://[::ffff:127.0.0.1]:7837"])
def test_an_upstream_that_spells_this_machine_another_way_is_still_refused(url):
    with pytest.raises(serve.ServeError):
        serve.check_upstream_loop(UpstreamChoice(url, "flag"), 7837, "T" * 43)


def test_the_launcher_and_the_proxy_command_refuse_this_homes_service_as_their_upstream(home, tmp_path):
    token = serve.ensure_token(home)
    user_home = tmp_path / "user-home"
    (user_home / ".claude").mkdir(parents=True)
    # What docs/serve.md has a user without --settings do: the service's URL in their Claude Code settings.
    (user_home / ".claude" / "settings.json").write_text(json.dumps(
        {"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:7911/{token}"}}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ran = tmp_path / "claude-ran"
    (bin_dir / "claude").write_text(f"#!/bin/sh\ntouch {ran}\n")
    (bin_dir / "claude").chmod(0o755)
    env = child_env(home, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"})
    for command in (["run", "claude", "-p", "hi"], ["proxy", "--port", "0"]):
        refused = subprocess.run([sys.executable, "-m", "contexto", *command], capture_output=True, text=True,
                                 env=env, cwd=tmp_path, timeout=120)
        assert refused.returncode == 2, (command, refused.stderr)
        assert "--upstream URL" in refused.stderr and "contexto serve" in refused.stderr
        assert "settings" in refused.stderr and len(refused.stderr.splitlines()) == 1  # where it came from
        assert token not in refused.stderr + refused.stdout and "7911" not in refused.stderr
    assert not ran.exists() and not (home / "sessions").exists()
    # With an upstream of its own, neither is refused for the setting.
    explicit = subprocess.run([sys.executable, "-m", "contexto", "run", "--upstream", "http://127.0.0.1:9", "claude",
                               "-p", "hi"], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert ran.exists(), explicit.stderr


@pytest.mark.parametrize("url, named", [
    ("http://127.0.0.1:7837/TOKEN", True), ("http://127.0.0.1:9999/TOKEN/", True), ("http://localhost/TOKEN/v1", True),
    ("http://[::1]:7837/TOKEN", True), ("http://127.1:7837/TOKEN", True), ("https://127.0.0.1/TOKEN", True),
    ("http://127.0.0.1:7837", False), ("http://127.0.0.1:7837/", False), ("http://127.0.0.1:7837/other", False),
    ("http://127.0.0.1:7837/v1/TOKEN", False), ("http://127.0.0.1:7837/TOKENx", False),
    ("https://gateway.example/TOKEN", False), ("http://[::1", False), ("not a url", False),
])
def test_names_service_is_a_loopback_url_whose_first_segment_is_the_token(home, url, named):
    assert servecontract.names_service(url.replace("TOKEN", "x" * 43), home) is False  # no token file: never read
    assert not home.exists()
    token = serve.ensure_token(home)
    assert servecontract.names_service(url.replace("TOKEN", token), home) is named
    (home / serve.TOKEN_FILE).chmod(0o644)  # a token file that cannot be used names nothing
    assert servecontract.names_service(url.replace("TOKEN", token), home) is False
