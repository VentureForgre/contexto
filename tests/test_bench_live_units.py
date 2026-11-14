"""The parts of the live benchmark on their own: credentials, the meter, the plan, the full arm's
requests, and the command line of the live run and ``--dry-run``. No Claude Code and no model."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto.bench import credentials as bench_credentials
from contexto.bench import meter as bench_meter
from contexto.bench import plan as bench_plan
from contexto.bench import runner
from contexto.bench import script as bench_script
from contexto.bench.arms import ARMS
from contexto.bench.arms import full as full_arm
from contexto.bench.arms import plain as plain_arm
from contexto.cli import build_parser, main

KEY = "sk-ant-api03-UNIT-test-key-55aa"


# -- credentials -------------------------------------------------------------------------------------


def test_the_key_comes_from_the_environment_a_helper_or_a_settings_file(tmp_path):
    env = bench_credentials.resolve(None, None, {"ANTHROPIC_API_KEY": f" {KEY}\n"})
    assert (env.source, env.key(), env.headers()) == ("env", KEY, {"x-api-key": KEY})
    assert env.child_env() == {"ANTHROPIC_API_KEY": KEY} and env.claude_settings() == {}

    helper = bench_credentials.resolve(f"printf %s {KEY}", None, {"ANTHROPIC_API_KEY": "the-option-outranks-this"})
    assert (helper.source, helper.key()) == ("helper", KEY)
    assert helper.headers() == {"x-api-key": KEY, "authorization": f"Bearer {KEY}"}
    assert helper.child_env() == {bench_credentials.HELPER_VAR: f"printf %s {KEY}"}

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"apiKeyHelper": f"echo {KEY}"}), encoding="utf-8")
    from_file = bench_credentials.resolve(None, settings, {})
    assert (from_file.source, from_file.key()) == ("settings", KEY)
    assert from_file.child_env() == {bench_credentials.SETTINGS_VAR: str(settings.resolve())}
    # The isolated Claude Code settings name the shim, never the command, the path or the key.
    for source in (helper, from_file):
        shim = source.claude_settings()["apiKeyHelper"]
        assert shim.endswith("-m contexto.bench.credentials") and KEY not in shim and "settings.json" not in shim
    assert KEY not in repr(env) and KEY not in repr(helper) and str(settings) not in repr(from_file)


def test_a_credential_error_never_shows_the_helpers_output(tmp_path):
    with pytest.raises(bench_credentials.CredentialError, match="no key"):
        bench_credentials.resolve(None, None, {})
    with pytest.raises(bench_credentials.CredentialError, match="not both"):
        bench_credentials.resolve("true", tmp_path / "x.json", {})
    with pytest.raises(bench_credentials.CredentialError, match="cannot be read"):
        bench_credentials.resolve(None, tmp_path / "missing.json", {})
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    with pytest.raises(bench_credentials.CredentialError, match="no apiKeyHelper"):
        bench_credentials.resolve(None, empty, {})
    failing = bench_credentials.resolve(f"echo {KEY}; echo leaked-detail >&2; exit 7", None, {})
    with pytest.raises(bench_credentials.CredentialError) as caught:
        failing.key()
    assert "exit 7" in str(caught.value) and KEY not in str(caught.value) and "leaked" not in str(caught.value)
    with pytest.raises(bench_credentials.CredentialError, match="failed"):
        bench_credentials.resolve("true", None, {}).key()  # printed nothing


def test_the_shim_claude_code_runs_prints_the_users_helpers_key(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"apiKeyHelper": f"printf %s {KEY}"}), encoding="utf-8")
    base = {k: v for k, v in os.environ.items() if not k.startswith("CONTEXTO_BENCH_")}
    for extra in ({bench_credentials.HELPER_VAR: f"printf %s {KEY}"}, {bench_credentials.SETTINGS_VAR: str(settings)}):
        shim = bench_credentials.Credentials("helper", _helper="unused").claude_settings()["apiKeyHelper"]
        proc = subprocess.run(["/bin/sh", "-c", shim], env={**base, **extra}, capture_output=True, text=True,
                              cwd=tmp_path)
        assert (proc.returncode, proc.stdout.strip()) == (0, KEY), proc.stderr
    proc = subprocess.run(["/bin/sh", "-c", shim], env=base, capture_output=True, text=True, cwd=tmp_path)
    assert proc.returncode == 1 and proc.stdout == "" and "no key helper" in proc.stderr


# -- the meter ---------------------------------------------------------------------------------------


class Upstream(BaseHTTPRequestHandler):
    """Records what it receives and answers what the test queued."""

    protocol_version = "HTTP/1.1"
    received: list[dict] = []
    answer: dict = {}
    gone = threading.Event()

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        Upstream.received.append({"method": self.command, "path": self.path, "body": body,
                                  "headers": {k.lower(): v for k, v in self.headers.items()},
                                  "header_list": [(k.lower(), v) for k, v in self.headers.items()]})
        answer = Upstream.answer
        self.send_response(answer.get("status", 200))
        for name, value in answer.get("headers", {"content-type": "text/event-stream"}).items():
            self.send_header(name, value)
        chunks = answer.get("chunks", [])
        if answer.get("no_body"):  # a 204: headers only
            self.end_headers()
            return
        if answer.get("length"):  # "declared": a Content-Length larger than what is sent
            self.send_header("content-length", str(answer.get("declared") or sum(len(c) for c in chunks)))
        else:
            self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        try:
            for chunk in chunks:
                self.wfile.write(chunk if answer.get("length") else b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                time.sleep(answer.get("pause", 0))
            if answer.get("hang"):
                while True:  # a stream that never ends: only the client leaving stops it
                    self.wfile.write(b"5\r\n: hi\n\r\n")
                    self.wfile.flush()
                    time.sleep(0.02)
            if answer.get("cut"):  # the upstream closes without ending the response
                self.close_connection = True
                return
            if not answer.get("length"):
                self.wfile.write(b"0\r\n\r\n")
        except OSError:
            Upstream.gone.set()

    do_GET = do_HEAD = do_POST  # noqa: N815

    def log_message(self, *args) -> None:
        pass


def sse(*events: dict) -> list[bytes]:
    return [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events]


USAGE_EVENTS = sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 7, "cache_creation_input_tokens": 100,
                                                    "cache_read_input_tokens": 900, "output_tokens": 1}}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}},
    {"type": "message_stop"})


@pytest.fixture
def upstream():
    Upstream.received, Upstream.answer = [], {"chunks": USAGE_EVENTS}
    Upstream.gone = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    server.daemon_threads = True
    server.handle_error = lambda *args: None  # a client that left early is part of some tests
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def metered(upstream: str, tmp_path: Path, budget: int | None = None, arm: str = "plain", **options):
    ledger = bench_meter.Ledger(tmp_path / "requests.jsonl", budget)
    meter = bench_meter.Meter(upstream, ledger, arm=arm, seed=5, repeat=1, model="m", history=[10, 20, 30],
                              bound=500, attempt=2, **options)
    meter.start()
    return meter, ledger


def post(meter, path: str, body: bytes, headers: dict | None = None) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", int(meter.url.rsplit(":", 1)[1]), timeout=10)
    conn.request("POST", path, body=body, headers={"content-type": "application/json", **(headers or {})})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, data


def test_the_meter_forwards_unchanged_and_records_counts_only(upstream, tmp_path):
    meter, ledger = metered(upstream + "/base", tmp_path)
    body = json.dumps({"model": "m", "max_tokens": 64, "tools": [{"name": "t"}],
                       "messages": [{"role": "user", "content": "a SECRET sentence with unicode é"}]},
                      ensure_ascii=False).encode("utf-8")
    meter.turn = 1
    status, headers, data = post(meter, "/v1/messages?beta=true", body, {
        "x-api-key": KEY, "anthropic-beta": "one,two", "accept-encoding": "gzip, br", "x-custom": "kept",
        "x-claude-code-request-class": "main", "x-claude-code-agent-type": "main",
        "x-session-affinity": "contexto-abc"})
    meter.stop()
    assert status == 200 and data == b"".join(USAGE_EVENTS) and headers["content-type"] == "text/event-stream"
    (got,) = Upstream.received
    assert got["path"] == "/base/v1/messages?beta=true" and got["body"] == body  # byte for byte
    sent = got["headers"]
    assert sent["x-api-key"] == KEY and sent["anthropic-beta"] == "one,two" and sent["x-custom"] == "kept"
    assert sent["host"] == upstream.split("//")[1]
    assert sent["accept-encoding"] == "identity"  # the one change to a Messages request's own headers
    # To an upstream that is not a vendor's: the affinity header stays, the hint headers stop here.
    assert sent["x-session-affinity"] == "contexto-abc"
    assert "x-claude-code-request-class" not in sent and "x-claude-code-agent-type" not in sent
    record = {"arm": "plain", "seed": 5, "repeat": 1, "model": "m", "n": 0, "turn": 1, "main": True,
              "history_tokens": 20, "uncached": 7, "cache_write": 100, "cache_read": 900, "output": 12, "status": 200,
              "compaction": False, "basis": "measured", "attempt": 2}
    assert meter.records == [record]
    written = (tmp_path / "requests.jsonl").read_text(encoding="utf-8")
    assert [json.loads(x) for x in written.splitlines()] == [record]
    assert "SECRET" not in written and KEY not in written
    assert ledger.spent == 1019 and ledger.reserved == 0
    assert meter.summary() == {"requests": 1, "max_input": 1007, "over_bound": 1, "tokens": 1019,
                               "compaction_requests": 0, "compaction_tokens": 0, "compaction_max_input": 0,
                               "usage_unreadable": 0}


def test_the_meter_classifies_requests_from_claude_codes_headers_or_the_tool_list(upstream, tmp_path):
    meter, _ = metered(upstream, tmp_path)
    with_tools = json.dumps({"tools": [{"name": "t"}], "messages": []}).encode()
    without = json.dumps({"messages": []}).encode()
    post(meter, "/v1/messages", without, {"x-claude-code-request-class": "auxiliary"})
    post(meter, "/v1/messages", with_tools,
         {"x-claude-code-request-class": "compaction", "x-claude-code-compaction": "auto"})
    post(meter, "/v1/messages", with_tools, {"x-claude-code-request-class": "subagent"})
    assert meter.classes_seen
    post(meter, "/v1/messages", with_tools)  # no class header: a tool list and no subagent id is a main request
    post(meter, "/v1/messages", without)
    post(meter, "/v1/messages", with_tools, {"x-claude-code-agent-id": "a1"})
    meter.stop()
    assert [(r["main"], r["compaction"]) for r in meter.records] == [
        (False, False), (False, True), (False, False), (True, False), (False, False), (False, False)]
    assert [r["n"] for r in meter.records] == [0, 1, 2, 3, 4, 5] and all(r["turn"] is None for r in meter.records)
    assert all(r["history_tokens"] == 0 for r in meter.records)


def test_count_tokens_and_other_paths_are_forwarded_and_not_recorded(upstream, tmp_path):
    meter, ledger = metered(upstream, tmp_path)
    as_json = {"content-type": "application/json"}
    Upstream.answer = {"chunks": [b'{"input_tokens": 41}'], "length": True, "headers": as_json}
    status, headers, data = post(meter, "/v1/messages/count_tokens", b'{"messages": []}', {"accept-encoding": "gzip"})
    assert (status, data, headers["content-length"]) == (200, b'{"input_tokens": 41}', "20")
    assert Upstream.received[-1]["headers"]["accept-encoding"] == "identity"
    post(meter, "/api/other", b"{}", {"accept-encoding": "gzip"})
    assert Upstream.received[-1]["headers"]["accept-encoding"] == "gzip"  # not a model path: untouched
    # A response that is not a stream: usage from the JSON body.
    usage = json.dumps({"usage": {"input_tokens": 5, "output_tokens": 2}}).encode()
    Upstream.answer = {"chunks": [usage], "length": True, "headers": as_json}
    post(meter, "/v1/messages/", b'{"messages": []}')
    meter.stop()
    assert [(r["uncached"], r["output"]) for r in meter.records] == [(5, 2)] and ledger.spent == 7


def test_what_the_meter_forwards_depends_on_whose_host_the_upstream_is(tmp_path):
    ledger = bench_meter.Ledger(None, None)

    def flags(url: str, arm: str) -> tuple[bool, bool]:
        meter = bench_meter.Meter(url, ledger, arm=arm, seed=1, repeat=0, model="m", history=[], bound=1)
        return meter.forward_affinity, meter.forward_hints

    # A vendor's host: no affinity header (Contexto would not have sent it), and the hint headers only
    # from the plain arm, where Claude Code sends them to the vendor without being asked.
    assert flags("https://api.anthropic.com", "contexto") == (False, False)
    assert flags("https://api.anthropic.com", "plain") == (False, True)
    for url in ("https://gateway.example.test/anthropic", "http://127.0.0.1:8317"):
        assert flags(url, "contexto") == (True, False) and flags(url, "plain") == (True, False)
    assert [bench_meter.upstream_kind(u) for u in (
        "https://api.anthropic.com", "https://gateway.example.test/x", "http://127.0.0.1:1", "http://localhost:2",
        "https://anthropic.com.evil.test")] == ["anthropic", "custom", "local", "local", "custom"]
    with pytest.raises(ValueError):
        bench_meter.Meter("ftp://x", ledger, arm="plain", seed=1, repeat=0, model="m", history=[], bound=1)


def test_the_guard_refuses_the_request_that_could_pass_the_budget(upstream, tmp_path):
    body = json.dumps({"max_tokens": 1_000, "messages": [{"role": "user", "content": "x" * 3_000}]}).encode()
    reserve = -(-len(body) * 2 // 3) + 1_000  # the body at 1.5 bytes per token, plus max_tokens
    meter, ledger = metered(upstream, tmp_path, budget=1_019 + reserve - 1)
    assert post(meter, "/v1/messages", body)[0] == 200 and ledger.spent == 1_019 and ledger.stop is None
    status, _, data = post(meter, "/v1/messages", body)  # what is spent plus this reservation is one token over
    meter.stop()
    assert status == 400 and b"stopped" in data and ledger.stop == "budget" and ledger.stopped.is_set()
    assert len(Upstream.received) == 1 and len(meter.records) == 1 and meter.refused == 1
    assert ledger.spent == 1_019 <= ledger.budget and ledger.remaining == reserve - 1
    assert bench_meter.read_spent(tmp_path / "requests.jsonl") == 1_019
    assert bench_meter.read_spent(tmp_path / "missing.jsonl") == 0


def test_a_401_on_a_model_path_halts_the_run_and_other_statuses_are_only_recorded(upstream, tmp_path):
    meter, ledger = metered(upstream, tmp_path)
    Upstream.answer = {"status": 429, "chunks": [b'{"type":"error"}'], "length": True,
                       "headers": {"content-type": "application/json", "retry-after": "3"}}
    status, headers, _ = post(meter, "/v1/messages", b"{}")
    assert (status, headers["retry-after"]) == (429, "3") and ledger.stop is None
    Upstream.answer = {"status": 401, "chunks": [b'{"type":"error"}'], "length": True}
    assert post(meter, "/api/hello", b"{}")[0] == 401 and ledger.stop is None  # not a model path
    assert post(meter, "/v1/messages/count_tokens", b"{}")[0] == 401
    meter.stop()
    assert ledger.stop == "auth" and meter.auth_status == 401
    assert [(r["status"], r["uncached"]) for r in meter.records] == [(429, 0)]


def test_a_stream_is_relayed_as_it_arrives_and_a_client_that_leaves_ends_it_upstream(upstream, tmp_path):
    meter, ledger = metered(upstream, tmp_path)
    Upstream.answer = {"chunks": USAGE_EVENTS[:1], "hang": True}
    sock = socket.create_connection(("127.0.0.1", int(meter.url.rsplit(":", 1)[1])), timeout=10)
    sock.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2\r\n\r\n{}")
    started = time.monotonic()
    data = b""
    while b"message_start" not in data:  # the first event arrives while the stream is still open
        data += sock.recv(65536)
    assert time.monotonic() - started < 5 and not Upstream.gone.is_set()
    sock.close()
    assert Upstream.gone.wait(5), "the upstream connection was not closed when the client left"
    meter.stop()
    assert [(r["status"], r["uncached"], r["cache_read"]) for r in meter.records] == [(200, 7, 900)]
    # The stream was cut before its final usage: the request counts as its reservation, not the 1,008
    # tokens reported so far (the 2-byte body and the default max_tokens).
    assert meter.records[0]["usage_unreadable"] and ledger.spent == 2 + bench_meter.DEFAULT_MAX_TOKENS


def test_an_unreachable_upstream_is_a_local_502_and_no_record(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    meter, ledger = metered(f"http://127.0.0.1:{free}", tmp_path)
    status, _, data = post(meter, "/v1/messages", b"{}")
    meter.stop()
    assert status == 502 and b"upstream unreachable" in data
    assert meter.records == [] and meter.unreachable == 1 and ledger.spent == 0 and ledger.reserved == 0


def raw_exchange(meter, request: bytes) -> bytes:
    """Send ``request`` as is and return every byte of the answer, until the meter closes."""
    sock = socket.create_connection(("127.0.0.1", int(meter.url.rsplit(":", 1)[1])), timeout=10)
    sock.sendall(request)
    data = b""
    try:
        while chunk := sock.recv(65536):
            data += chunk
    except TimeoutError:
        pass
    sock.close()
    return data


def test_a_header_sent_twice_is_forwarded_twice(upstream, tmp_path):
    meter, _ = metered(upstream, tmp_path)
    body = b'{"messages": []}'
    request = (b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
               b"anthropic-beta: one\r\nanthropic-beta: two\r\nX-Api-Key: k\r\n"
               b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
    assert raw_exchange(meter, request).startswith(b"HTTP/1.1 200")
    meter.stop()
    (got,) = Upstream.received
    assert [v for k, v in got["header_list"] if k == "anthropic-beta"] == ["one", "two"]
    assert [v for k, v in got["header_list"] if k == "accept-encoding"] == ["identity"]


def test_a_204_is_relayed_with_no_body_and_no_chunking(upstream, tmp_path):
    meter, _ = metered(upstream, tmp_path)
    Upstream.answer = {"status": 204, "no_body": True, "headers": {"x-kept": "yes"}}
    data = raw_exchange(meter, b"POST /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
                               b"Content-Length: 0\r\n\r\n")
    meter.stop()
    head, _, rest = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 204") and b"x-kept: yes" in head.lower()
    assert b"transfer-encoding" not in head.lower() and rest == b""


def test_the_upstream_kind_reads_an_international_name_as_the_socket_layer_does():
    # A soft hyphen maps away under IDNA: the meter treats this host as the vendor's, so the manifest does too.
    assert bench_meter.upstream_kind("https://api.anthro\xadpic.com") == "anthropic"
    assert bench_meter.upstream_kind("https://API.ANTHROPIC.COM.") == "anthropic"
    assert bench_meter.upstream_kind("https://xn--nxasmq6b.example") == "custom"
    assert bench_meter.upstream_kind("https://\u2025.example") == "custom"  # cannot be normalised


def test_after_a_refused_credential_nothing_more_is_forwarded(upstream, tmp_path):
    meter, ledger = metered(upstream, tmp_path)
    Upstream.answer = {"status": 401, "chunks": [b'{"type":"error"}'], "length": True}
    assert post(meter, "/v1/messages", b"{}")[0] == 401 and ledger.stop == "auth"
    Upstream.answer = {"chunks": [b'{"input_tokens": 1}'], "length": True}
    assert post(meter, "/v1/messages/count_tokens", b"{}")[0] == 400
    assert post(meter, "/v1/messages", b"{}")[0] == 400
    assert post(meter, "/api/other", b"{}")[0] == 400
    meter.stop()
    assert len(Upstream.received) == 1


@pytest.mark.parametrize("answer", [
    {"chunks": [b"\x1f\x8b compressed bytes"], "headers": {"content-type": "text/event-stream",
                                                          "content-encoding": "gzip"}},
    {"chunks": USAGE_EVENTS[:1], "cut": True},  # the upstream closes after message_start
    {"chunks": USAGE_EVENTS[:1]},  # it ends the response cleanly, but with no final usage
    {"chunks": [b"not json"], "length": True, "headers": {"content-type": "application/json"}},
], ids=["encoded", "cut", "no-final-usage", "not-json"])
def test_a_2xx_whose_usage_cannot_be_read_counts_as_its_whole_reservation(upstream, tmp_path, answer):
    body = json.dumps({"max_tokens": 5_000, "messages": [{"role": "user", "content": "x" * 300}]}).encode()
    reserve = -(-len(body) * 2 // 3) + 5_000
    meter, ledger = metered(upstream, tmp_path, budget=10 * reserve)
    Upstream.answer = answer
    try:
        post(meter, "/v1/messages", body)
    except (http.client.HTTPException, OSError):
        pass  # a cut stream: the client sees it end early
    meter.stop()
    (record,) = meter.records
    assert record["usage_unreadable"] is True and record["reserved_tokens"] == reserve
    assert ledger.spent == reserve and ledger.reserved == 0
    assert bench_meter.read_spent(tmp_path / "requests.jsonl") == reserve  # a resumed run counts it the same
    assert meter.summary()["usage_unreadable"] == 1 and meter.summary()["tokens"] == reserve


def test_a_response_with_its_whole_usage_is_not_marked_unreadable(upstream, tmp_path):
    meter, ledger = metered(upstream, tmp_path)
    post(meter, "/v1/messages", b"{}")
    Upstream.answer = {"status": 529, "chunks": [b'{"type":"error"}'], "length": True}
    post(meter, "/v1/messages", b"{}")  # not a 2xx: nothing was billed
    meter.stop()
    assert [r.get("usage_unreadable") for r in meter.records] == [None, None] and ledger.spent == 1_019


def test_a_response_cut_short_of_its_length_ends_the_clients_connection_at_once(upstream, tmp_path):
    """The upstream declares 5,000 bytes, sends 40 and closes: the client is told at once (its read
    fails) instead of waiting out its own timeout for bytes that will not come."""
    meter, ledger = metered(upstream, tmp_path)
    Upstream.answer = {"chunks": [b'{"usage": {"input_tokens": 1, "output_tok'], "length": True, "declared": 5_000,
                       "cut": True, "headers": {"content-type": "application/json"}}
    started = time.monotonic()
    with pytest.raises(http.client.IncompleteRead):
        post(meter, "/v1/messages", b"{}")  # the client's timeout is 10 seconds
    assert time.monotonic() - started < 5
    meter.stop()
    (record,) = meter.records
    assert record["usage_unreadable"] is True and ledger.spent == record["reserved_tokens"]


def test_compaction_requests_count_in_the_largest_input_and_over_the_bound(upstream, tmp_path):
    meter, _ = metered(upstream, tmp_path)  # bound 500; each answer reports 1,007 input tokens
    big = sse({"type": "message_start", "message": {"usage": {"input_tokens": 3, "cache_creation_input_tokens": 0,
                                                              "cache_read_input_tokens": 1_997}}},
              {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 50}}, {"type": "message_stop"})
    post(meter, "/v1/messages", b"{}", {"x-claude-code-request-class": "main"})
    Upstream.answer = {"chunks": big}
    post(meter, "/v1/messages", b"{}", {"x-claude-code-request-class": "compaction"})
    Upstream.answer = {"chunks": USAGE_EVENTS}
    post(meter, "/v1/messages", b"{}", {"x-claude-code-request-class": "auxiliary"})
    meter.stop()
    summary = meter.summary()
    assert (summary["max_input"], summary["over_bound"]) == (2_000, 2)  # the side request is not counted
    assert (summary["compaction_requests"], summary["compaction_tokens"], summary["compaction_max_input"]) == (
        1, 2_050, 2_000)
    assert summary["tokens"] == 1_019 + 2_050 + 1_019


def test_a_unit_that_asks_for_too_many_requests_in_one_turn_is_stopped(upstream, tmp_path):
    meter, ledger = metered(upstream, tmp_path, turn_request_cap=3)
    meter.turn = 0
    assert [post(meter, "/v1/messages", b"{}")[0] for _ in range(4)] == [200, 200, 200, 400]
    assert meter.unit_stop == "runaway" and "more than 3 billed requests" in meter.unit_stop_reason
    assert "the model kept calling tools" in meter.unit_stop_reason and ledger.stop is None
    meter.turn = 1
    assert post(meter, "/v1/messages", b"{}")[0] == 400  # the unit stays stopped
    meter.stop()
    assert len(Upstream.received) == 3 and meter.turn_requests(0) == 4 and meter.turn_requests(1) == 1


def test_failed_responses_do_not_count_toward_the_per_turn_cap(upstream, tmp_path):
    """A 529 or another failed response cost nothing and is the upstream's doing, not the model's: it
    counts toward its own cap, and the per-turn cap counts the requests the upstream answered."""
    meter, ledger = metered(upstream, tmp_path, turn_request_cap=3, turn_failure_cap=50)
    meter.turn = 0
    statuses = []
    failed = {"chunks": [b'{"type":"error"}'], "length": True, "headers": {"content-type": "application/json"}}
    for status in (200, 529, 529, 500, 200, 200):
        Upstream.answer = {"chunks": USAGE_EVENTS} if status == 200 else {**failed, "status": status}
        statuses.append(post(meter, "/v1/messages", b"{}")[0])
    assert statuses == [200, 529, 529, 500, 200, 200] and meter.unit_stop is None
    Upstream.answer = {"chunks": USAGE_EVENTS}
    assert post(meter, "/v1/messages", b"{}")[0] == 400  # the fourth answered request of the turn
    meter.stop()
    reason = meter.unit_stop_reason
    assert meter.unit_stop == "runaway" and "more than 3 billed requests" in reason
    assert "3 more failed" in reason and "the model kept calling tools" in reason and ledger.stop is None


def test_a_turn_whose_requests_keep_failing_stops_on_its_own_cap(upstream, tmp_path):
    meter, ledger = metered(upstream, tmp_path, turn_request_cap=3, turn_failure_cap=4)
    meter.turn = 0
    Upstream.answer = {"status": 529, "chunks": [b'{"type":"error"}'], "length": True,
                       "headers": {"content-type": "application/json"}}
    assert [post(meter, "/v1/messages", b"{}")[0] for _ in range(5)] == [529, 529, 529, 529, 400]
    meter.stop()
    reason = meter.unit_stop_reason
    assert meter.unit_stop == "api_error" and "4 failed responses" in reason and "the upstream kept failing" in reason
    assert "model" not in reason and ledger.stop is None and len(Upstream.received) == 4


def test_a_unit_that_would_pass_its_token_cap_is_stopped_and_the_run_is_not(upstream, tmp_path):
    body = json.dumps({"max_tokens": 1_000, "messages": []}).encode()
    reserve = -(-len(body) * 2 // 3) + 1_000
    meter, ledger = metered(upstream, tmp_path, budget=10**9, token_cap=1_019 + reserve - 1)
    assert post(meter, "/v1/messages", body)[0] == 200
    assert post(meter, "/v1/messages", body)[0] == 400
    meter.stop()
    assert meter.unit_stop == "runaway" and "cap of" in meter.unit_stop_reason
    assert ledger.stop is None and ledger.reserved == 0 and ledger.spent == 1_019 and len(Upstream.received) == 1


def test_history_tokens_follow_the_scripts_own_estimate():
    for protocol in ("v2", "d27"):
        script = bench_script.build(3, protocol, bound=4_000, turn_bytes=2_000)
        history = bench_meter.history_tokens(script)
        assert len(history) == len(script.turns) and history == sorted(history)
        assert history[-1] == bench_script.estimate(script)["tokens"]


# -- the plan ----------------------------------------------------------------------------------------


def test_units_run_seed_by_seed_with_the_arms_rotated():
    units = bench_plan.units([7, 8, 9], 2, ARMS)
    assert units == [
        ("contexto", 7, 0), ("full", 7, 0), ("plain", 7, 0),
        ("full", 7, 1), ("plain", 7, 1), ("contexto", 7, 1),
        ("plain", 8, 0), ("contexto", 8, 0), ("full", 8, 0),
        ("contexto", 9, 0), ("full", 9, 0), ("plain", 9, 0)]
    firsts = [u[0] for u in bench_plan.units(list(range(6)), 1, ARMS)[::3]]
    assert firsts == ["contexto", "full", "plain"] * 2  # no arm always runs first
    assert bench_plan.units([1], 1, ("full",)) == [("full", 1, 0)]


@pytest.fixture(scope="module")
def d27():
    return bench_script.build(protocol="d27")


def test_the_plan_reproduces_the_recorded_runs_of_the_54_probe_session(d27):
    """Run #3 and run #4 (docs/research/M2-usage-savings-run3.md, M2-acceptance-run4.md): the same
    script, bound and seed. The plan's expected case must sit near what they measured."""
    contexto = bench_plan.unit_estimate("contexto", d27, 150_000)
    assert 44 <= contexto.requests_expected <= 48  # 44 and 45 measured
    assert 3_422_988 * 0.9 <= contexto.expected_total <= 3_748_084 * 1.1  # run #4 and run #3
    assert contexto.expected["cache_read"] > 3 * contexto.expected["cache_write"]  # run #4: 2,800,741 and 608,355
    assert contexto.over_bound_expected == 0 and contexto.worst_total > 2 * contexto.expected_total
    full = bench_plan.unit_estimate("full", d27, 150_000)
    assert full.requests_expected == full.requests_worst == 6
    assert 2_578_830 * 0.85 <= full.expected_total <= 2_578_830 * 1.1  # run #4's baseline, less Claude Code's head
    plain = bench_plan.unit_estimate("plain", d27, 150_000)
    # One request a turn, one summary request a compaction, and the two content turns that take a second request.
    assert plain.compactions_expected >= 2
    assert plain.requests_expected == len(d27.turns) + plain.compactions_expected + 2
    assert plain.requests_worst > 3 * plain.requests_expected
    for est in (contexto, full, plain):
        assert est.requests_worst >= est.requests_expected and est.worst_total > est.expected_total
        assert est.worst["cache_read"] == est.worst["uncached"] == 0  # the worst case reads no cache
    with pytest.raises(ValueError):
        bench_plan.unit_estimate("other", d27, 150_000)


def test_the_plain_plan_is_near_what_the_real_claude_did_against_the_stub():
    """Claude Code 2.1.322 against the stub upstream, the v2 script of seed 3 at the defaults, bound
    150,000 (2026-11-12): 52 requests (44 main, 8 summary requests for 4 compactions) and 5,386,722
    modelled tokens. The plan's expected case is fitted to that run's two findings."""
    plain = bench_plan.unit_estimate("plain", bench_script.build(3), 150_000)
    assert 46 <= plain.requests_expected <= 56 and 4 <= plain.compactions_expected <= 5
    assert 5_386_722 * 0.9 <= plain.expected_total <= 5_386_722 * 1.1


def test_the_plan_adds_up_per_arm_and_in_total(d27):
    todo = bench_plan.units([20261016], 2, ARMS)
    plan = bench_plan.estimate({20261016: d27}, todo, 150_000)
    assert plan["units"] == 6 and plan["turns"] == 6 * len(d27.turns)
    assert set(plan["arms"]) == set(ARMS) and all(row["units"] == 2 for row in plan["arms"].values())
    for case in ("expected", "worst"):
        for name in bench_plan.CLASSES:
            assert getattr(plan["total"], case)[name] == sum(getattr(row["estimate"], case)[name]
                                                              for row in plan["arms"].values())
    one = bench_plan.unit_estimate("full", d27, 150_000)
    assert plan["arms"]["full"]["estimate"].expected_total == 2 * one.expected_total
    assert bench_plan.budget_to_pass(1) == 10_000 and bench_plan.budget_to_pass(30_540_856) == 30_550_000
    table = bench_plan.table(plan, "worst")
    assert table[0].startswith("| Arm | Units | Turns | Requests |") and table[-1].startswith("| total | 6 |")
    assert f"{plan['total'].worst_total:,}" in table[-1]


def test_the_plain_arms_window_is_the_bound_plus_the_reserve_where_claude_code_allows():
    assert plain_arm.compaction_setting(150_000) == {
        "variable": "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "value": 183_000, "intended_trigger": 150_000, "forced": True,
        "note": plain_arm.compaction_setting(150_000)["note"]}
    assert "not documented" in plain_arm.compaction_setting(150_000)["note"]
    for bound in (67_000, 100_000, 967_000):
        assert plain_arm.compaction_setting(bound)["value"] == bound + 33_000
        assert plain_arm.compaction_setting(bound)["forced"] is True
    low, high = plain_arm.compaction_setting(66_999), plain_arm.compaction_setting(967_001)
    assert (low["value"], low["intended_trigger"], low["forced"]) == (100_000, 67_000, False)
    assert (high["value"], high["intended_trigger"], high["forced"]) == (1_000_000, 967_000, False)
    assert "cannot be forced" in low["note"] and "cannot be forced" in high["note"]


# -- the full arm's requests -------------------------------------------------------------------------


def test_the_full_arms_request_is_built_from_the_script_alone():
    script = bench_script.build(3, "v2", bound=4_000, turn_bytes=2_000)
    base = full_arm.history(script)
    content = [i for i, k in enumerate(script.kinds) if k != "probe"]
    assert len(base) == 2 * len(content) + 2 * len(script.reads)
    assert [m["role"] for m in base[:2]] == ["user", "assistant"] and base[0]["content"][0]["text"] == script.turns[0]
    replies = [m["content"][0]["text"] for m in base if m["role"] == "assistant" and m["content"][0]["type"] == "text"]
    assert replies == [script.scripted_reply(i) for i in content]
    calls = [b for m in base for b in m["content"] if b["type"] == "tool_use"]
    results = [b for m in base for b in m["content"] if b["type"] == "tool_result"]
    assert [c["input"]["path"] for c in calls] == list(script.reads)
    assert [r["content"] for r in results] == [script.files[p] for p in script.reads]
    assert [r["tool_use_id"] for r in results] == [c["id"] for c in calls]
    marked = [b for m in base for b in m["content"] if "cache_control" in b]
    assert marked == [base[-1]["content"][-1]] and marked[0]["cache_control"] == {"type": "ephemeral"}
    assert full_arm.history(script, "1h")[-1]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    third = full_arm.request(script, "m", base, ["first answers", ""], 2)
    assert third["tool_choice"] == {"type": "none"} and [t["name"] for t in third["tools"]] == ["read_file"]
    assert third["system"] == [{"type": "text", "text": full_arm.SYSTEM}] and "thinking" not in third
    tail = third["messages"][len(base):]
    assert [m["role"] for m in tail] == ["user", "assistant", "user", "assistant", "user"]
    assert [m["content"][0]["text"] for m in tail] == [
        script.probe_texts[0], "first answers", script.probe_texts[1], "(no answer)", script.probe_texts[2]]

    plain_script = bench_script.build(3, "d27", bound=4_000, turn_bytes=2_000)
    first = full_arm.request(plain_script, "m", full_arm.history(plain_script), [], 0)
    assert "tools" not in first and "tool_choice" not in first  # no tool-loop turn, no tool
    assert full_arm.context_window("claude-opus-5-5") == 1_000_000 and full_arm.context_window("other") == 200_000
    assert full_arm.context_window("other", 64_000) == 64_000


# -- the command line --------------------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_the_budget_without_a_key_or_a_call(capsys, monkeypatch, tmp_path):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("a dry run opened a connection"))
    assert main(["bench", "--dry-run", "--model", "claude-haiku-5-5", "--protocol", "d27", "--seeds", "2",
                 "--repeats", "3"]) == 0
    out = capsys.readouterr().out
    assert "No calls were made" in out and "12 units to run" in out
    assert "2 seeds, 20261016 to 20261017, the first seed 3 times" in out
    for arm in ARMS:
        assert f"| {arm} | 4 | 96 |" in out
    assert out.count("| total | 12 | 288 |") == 2  # the expected case and the worst case
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW=183000" in out and "M2-acceptance-run4.md" in out
    scripts = {s: bench_script.build(s, "d27") for s in (20261016, 20261017)}
    plan = bench_plan.estimate(scripts, bench_plan.units([20261016, 20261017], 3, ARMS), 150_000)
    need = bench_plan.budget_to_pass(plan["total"].worst_total)
    assert f"pass --budget-tokens {need} " in out and f"{plan['total'].expected_total:,} tokens" in out

    assert main(["bench", "--dry-run", "--protocol", "d27", "--arms", "full", "--budget-tokens", "5"]) == 0
    out = capsys.readouterr().out
    assert "--budget-tokens 5 is below the worst case" in out and "| plain |" not in out and "(none given)" in out


def test_dry_run_says_what_its_figures_are_and_calibrates_on_a_recorded_model(capsys, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert main(["bench", "--dry-run", "--model", "claude-haiku-5-5", "--seeds", "2"]) == 0
    haiku = capsys.readouterr().out
    assert "a planning figure and not a ceiling" in haiku and "the ceiling is --budget-tokens" in haiku
    assert "32,000 tokens on every Claude Code request and 8,192 on every full-arm request" in haiku
    assert "up to 128,000 output tokens" in haiku and "full output reservation" not in haiku
    assert "fitted on recorded runs of claude-haiku-5-5, the model given" in haiku

    assert main(["bench", "--dry-run", "--model", "claude-opus-5-5", "--seeds", "2"]) == 0
    opus = capsys.readouterr().out
    assert "4,726,388 tokens on the Contexto arm against 3,422,988" in opus and "scaled by 1.381" in opus
    assert "[assumed: no run of the plain arm has been recorded on either model]" in opus
    worst = next(line for line in opus.splitlines() if line.startswith("Worst case"))
    assert "up to 128,000 output tokens" in worst and "claude-haiku-5-5" not in worst
    assert main(["bench", "--dry-run", "--model", "claude-sonnet-5-5", "--seeds", "2"]) == 0
    assert "the plan is calibrated on claude-haiku-5-5; no run of claude-sonnet-5-5" in capsys.readouterr().out

    script = bench_script.build(20261016, "v2")
    factor = 4_726_388 / 3_422_988
    for arm in ARMS:
        base = bench_plan.unit_estimate(arm, script, 150_000)
        scaled = bench_plan.unit_estimate(arm, script, 150_000, model="claude-opus-5-5")
        assert scaled.worst == base.worst  # the worst case is not scaled
        if arm == "full":  # built from the script alone
            assert scaled.expected == base.expected
        else:
            assert abs(scaled.expected_total - factor * base.expected_total) <= 4
            assert scaled.expected_total < scaled.worst_total
    assert bench_plan.model_factor("claude-haiku-5-5") == bench_plan.model_factor(None) == 1.0


def test_the_live_runs_own_options_are_refused_beside_a_mode_and_the_plan_options_are_not(capsys, monkeypatch):
    from contexto.bench.modes import live

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for option, value in (("--key-helper", "cat key"), ("--upstream", "http://127.0.0.1:9"), ("--work", "w"),
                          ("--max-turn-requests", "5"), ("--max-turn-failures", "5"), ("--max-unit-tokens", "5"),
                          ("--keep-work", None)):
        assert option in live.OWN_OPTIONS
        assert main(["bench", "--dry-run", "--model", "claude-haiku-5-5", option, *([value] if value else [])]) == 2
        err = capsys.readouterr().err
        assert f"{option} belongs to the live run" in err and "leave it out with --dry-run" in err
    # What the plan depends on is shared: the dry run of a live command keeps these.
    assert main(["bench", "--dry-run", "--model", "claude-haiku-5-5", "--arms", "full", "--budget-tokens",
                 "10000000"]) == 0
    assert "--budget-tokens 10,000,000 covers the worst case" in capsys.readouterr().out


def test_the_live_run_refuses_to_start_without_what_it_needs(capsys, monkeypatch, tmp_path):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runner, "run", lambda *a, **k: pytest.fail("the run started"))
    out = tmp_path / "out"
    cases = [
        (["bench", "--out", str(out)], "needs --model"),
        (["bench", "--model", "m"], "needs --out"),
        (["bench", "--model", "m", "--out", str(out)], "no key: set ANTHROPIC_API_KEY"),
        (["bench", "--model", "m", "--out", str(out), "--key-helper", "true", "--settings", str(out)], "not both"),
        (["bench", "--model", "m", "--out", str(Path(__file__).parent / "bench-out"), "--key-helper", "true"],
         "inside a git work tree"),
    ]
    for argv, said in cases:
        assert main(argv) == 2
        err = capsys.readouterr().err
        assert "contexto bench: refusing:" in err and said in err, err
    assert not out.exists()
    for bad in (["--arms", "contexto,other"], ["--arms", ""], ["--budget-tokens", "0"], ["--cache-ttl", "2h"]):
        with pytest.raises(SystemExit):
            main(["bench", "--model", "m", "--out", str(out), *bad])
        capsys.readouterr()


def test_the_live_run_gets_its_configuration_from_the_command_line(capsys, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(runner, "run", lambda config: seen.setdefault("config", config) and 0)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.test/v")
    inside = Path(__file__).parent / "bench-out"
    assert main(["bench", "--model", "claude-opus-5-5", "--seeds", "2", "--repeats", "2", "--bound", "100000",
                 "--arms", "plain,contexto", "--budget-tokens", "1,000,000", "--out", str(inside), "--allow-git-out",
                 "--cache-ttl", "1h", "--turn-timeout", "90", "--context-window", "500000"]) == 0
    config = seen["config"]
    assert (config.model, config.bound, config.seeds, config.repeats, config.budget) == (
        "claude-opus-5-5", 100_000, [20261016, 20261017], 2, 1_000_000)
    assert config.arms == ("contexto", "plain") and config.upstream == "https://gateway.example.test/v"
    assert (config.cache_ttl, config.turn_timeout, config.context_window, config.out) == ("1h", 90.0, 500_000, inside)
    assert config.credentials.source == "env" and config.work is None and not config.keep_work
    assert KEY not in capsys.readouterr().err

    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    seen.clear()
    assert main(["bench", "--model", "m", "--budget-tokens", "9", "--out", str(tmp_path / "o"), "--upstream",
                 "http://127.0.0.1:9", "--key-helper", "true"]) == 0
    assert seen["config"].upstream == "http://127.0.0.1:9" and seen["config"].credentials.source == "helper"
    assert seen["config"].arms == ARMS
    seen.clear()
    assert main(["bench", "--model", "m", "--budget-tokens", "9", "--out", str(tmp_path / "o")]) == 0
    assert seen["config"].upstream == "https://api.anthropic.com"


def test_an_output_or_work_directory_inside_a_git_work_tree_is_recognised(tmp_path):
    assert runner.inside_git(Path(__file__).parent / "not" / "yet" / "there")
    assert not runner.inside_git(tmp_path / "out")
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert runner.inside_git(tmp_path / "repo" / "deep" / "out")


def test_starting_any_contexto_command_does_not_import_the_benchmarks_machinery():
    code = ("import sys\nfrom contexto.cli import main\n"
            "try:\n    main(['bench', '--help'])\nexcept SystemExit:\n    pass\n"
            "heavy = [m for m in ('contexto.bench.runner', 'contexto.bench.meter', 'contexto.bench.plan', "
            "'contexto.bench.credentials', 'contexto.bench.arms', 'contexto.bench.arms.cli', 'urllib.request') "
            "if m in sys.modules]\nprint('HEAVY', heavy, file=sys.stderr)")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "HEAVY []" in proc.stderr, proc.stderr
    assert "--budget-tokens" in proc.stdout and "--dry-run" in proc.stdout and "--key-helper" in proc.stdout


def test_this_builds_modes_pass_the_mode_bug_check():
    # The contract's check (contexto.commands.bench): every option in a mode's OWN_OPTIONS is one that mode
    # adds itself; the options the live run shares with --dry-run are added by both with add_option.
    from contexto.bench.modes import dry_run, live

    args = build_parser().parse_args(["bench"])
    assert args._bench_bugs == []
    own = {option for module, option, _dest, _default in args._bench_own if module is live}
    assert own == set(live.OWN_OPTIONS)
    assert not getattr(dry_run, "OWN_OPTIONS", ())


def test_the_option_defaults_repeat_the_runners_caps():
    from contexto.bench.modes import _live_options

    assert (_live_options.TURN_REQUEST_CAP, _live_options.TURN_FAILURE_CAP) == (
        runner.TURN_REQUEST_CAP, runner.TURN_FAILURE_CAP)
    assert _live_options.ARMS == ARMS
