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

from contexto import __version__, cli, serve
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
    assert sessions(home) == [lead] and served(home) == {A: lead, B: lead}
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
    post(service, A)
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
    assert post(service, A, extra=turn_two)[0].status == 200
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
        post(service, session)
        clock.now += 1
    post(service, A)  # B is now the least recently used
    clock.now += 1
    post(service, C)
    mapping = served(home)
    assert service.health()["sessions"] == 2 and len(sessions(home)) == 3
    assert [e["reason"] for e in session_log(home, mapping[B]) if e["event"] == "serve_close"] == ["cap"]
    assert not any(e["event"] == "serve_close" for e in session_log(home, mapping[A]))
    post(service, B)  # reopens B's own store, closing A's
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
    assert json.loads(data) == {"ok": True, "version": __version__, "port": service.port, "sessions": 0}
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
    marker = b'"content_block_stop", "index": 1'  # the tool call's block is complete

    def client():
        conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=30)
        conn.request("POST", f"/{service.token}/v1/messages?beta=true", body(A, stream=True), headers(A))
        response = conn.getresponse()
        while chunk := response.read1(65536):
            seen.extend(chunk)
            if not at_block and marker in seen:
                at_block.append(resolve(service, ToolUpstream.tool_use_id))  # the moment the call is complete
        conn.close()

    thread = threading.Thread(target=client)
    thread.start()
    try:
        assert ToolUpstream.block_sent.wait(10)
        time.sleep(0.3)
        # Upstream has sent the whole tool call; the proxy holds it until the response-time work is done.
        assert b"calling" in seen and ToolUpstream.tool_use_id.encode() not in seen and not at_block
    finally:
        ToolUpstream.gate.set()
        thread.join(15)
    assert marker in seen, bytes(seen[-300:])
    assert at_block == [(200, {"session": served(home)[A]})]


def test_the_tool_call_map_keeps_the_most_recent_ids(make_service, monkeypatch):
    monkeypatch.setattr(serve, "TOOL_CALLS_KEPT", 3)
    service = make_service()
    for n in range(5):
        service.register_tool_calls((f"toolu_{n}",), "s1")
    service.register_tool_calls(("toolu_2",), "s2")  # seen again: now the newest
    service.register_tool_calls(("toolu_5",), "s1")
    assert [service.resolve(f"toolu_{n}") for n in range(6)] == [None, None, "s2", None, "s1", "s1"]


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


@pytest.mark.parametrize("env, named", [
    ({}, []), ({"CLAUDE_CODE_USE_BEDROCK": "1"}, ["CLAUDE_CODE_USE_BEDROCK"]),
    ({"CLAUDE_CODE_USE_VERTEX": "true", "CLAUDE_CODE_USE_FOUNDRY": "0", "CLAUDE_CODE_USE_MANTLE": ""},
     ["CLAUDE_CODE_USE_VERTEX"]),
    ({"CLAUDE_CODE_USE_ANTHROPIC_AWS": " Yes ", "CLAUDE_CODE_USE_MANTLE": "on", "CLAUDE_CODE_USE_OTHER": "1"},
     ["CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS"]),
])
def test_cloud_provider_variables_are_found(env, named):
    assert serve.cloud_provider_variables(env) == named


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
                   "--print-mcp-config", "--rotate-token", "--status"):
        assert option in out
    assert "--host" not in out


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


def test_status_without_a_service_makes_no_request(home):
    status = run_cli(home, "--status")
    assert status.returncode == 1 and status.stdout.strip() == "contexto serve: not running"
    # A record left by a killed service names a pid that is gone: still not running, and said so.
    (home / serve.STATE_FILE).write_text(json.dumps({"port": 1, "pid": 2 ** 22 + 12345}))
    status = run_cli(home, "--status")
    assert status.returncode == 1 and "not running" in status.stdout


def test_the_service_warns_about_a_cloud_provider_and_a_small_bound(home, running):
    service = running("--bound", "60000", env={"CLAUDE_CODE_USE_VERTEX": "1"})
    text = "\n".join(service.stderr_lines)
    assert "(bound 60,000, upstream flag)" in service.stderr_lines[0]
    time.sleep(0.2)
    text = "\n".join(service.stderr_lines)
    assert "CLAUDE_CODE_USE_VERTEX" in text and "window budget" in text
    assert service.stop() == 0
