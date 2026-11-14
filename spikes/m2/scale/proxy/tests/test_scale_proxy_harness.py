"""Fast checks of the scale harness helpers (synthetic traffic, fake upstream SSE, summary math).

No network beyond 127.0.0.1 and no model calls; the scale run itself is not part of the test suite.
"""

from __future__ import annotations

import errno
import http.client
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scale.proxy import run, summarize, synth  # noqa: E402
from scale.proxy.upstream import FakeUpstream, reply_events  # noqa: E402

from contexto import rewrite  # noqa: E402
from contexto.proxy import SSEEventSplitter, StreamHold, UsageReader  # noqa: E402

SHAPE = synth.Shape(head_tokens=20_000, turn_tokens=8_000)


def test_wire_builder_matches_serialized_request():
    head = synth.head(SHAPE)
    wire = synth.WireBuilder(head)
    history = []
    for t in range(3):
        messages = synth.turn(SHAPE, t)
        for end, _ in synth.request_points(SHAPE, t, messages):
            assert wire.body(messages[:end]) == synth.serialize(synth.request(head, history, messages[:end]))
        wire.extend(messages)
        history.extend(messages)


def test_turns_are_deterministic_and_shaped_like_claude_code():
    assert synth.turn(SHAPE, 5) == synth.turn(SHAPE, 5)
    first = synth.turn(SHAPE, 0)
    assert first[0]["content"][0]["text"].startswith("<system-reminder>")
    assert first[1]["role"] == "system" and "output_config" in first[1]
    for t in range(12):
        messages = synth.turn(SHAPE, t)
        assert rewrite.is_turn_start(messages[0])
        assert sum(rewrite.is_turn_start(m) for m in messages) == 1
        assert messages[-1]["role"] == "assistant"
        points = synth.request_points(SHAPE, t, messages)
        tool_results = sum(1 for m in messages if m["role"] == "user" and not rewrite.is_turn_start(m))
        assert len(points) == 1 + tool_results
        assert [kind for _, kind in points] == ["tool_use"] * tool_results + ["end_turn"]


def test_mixed_reminders_only_on_later_turn_starts():
    mixed = synth.Shape(head_tokens=20_000, turn_tokens=8_000, reminders="mixed")
    later = [synth.turn(mixed, t)[0] for t in range(1, 60)]
    with_reminder = [m for m in later if any(rewrite.is_context_block(b) for b in m["content"])]
    assert 0 < len(with_reminder) < len(later)
    assert all(not any(rewrite.is_context_block(b) for b in synth.turn(SHAPE, t)[0]["content"]) for t in range(1, 20))


def test_head_size_tracks_the_requested_tokens():
    for tokens in (20_000, 50_000, 90_000):
        head = synth.head(synth.Shape(head_tokens=tokens))
        size = synth.tokens_of(len(synth.serialize({"system": head["system"], "tools": head["tools"]})))
        assert abs(size - tokens) / tokens < 0.05


def _parse(events: list[bytes]) -> tuple[UsageReader, StreamHold]:
    reader = UsageReader("text/event-stream")
    triggered = []
    hold = StreamHold("messages", lambda: triggered.append(True))
    splitter = SSEEventSplitter()
    for raw in splitter.feed(b"".join(events)):
        reader.feed_event(raw)
        hold.feed(raw)
    return reader, hold


def test_fake_sse_is_read_like_the_messages_api():
    reader, hold = _parse(reply_events("tool_use", 12_345, "x"))
    assert sum(reader.usage[k] for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")) == 12_345
    assert reader.usage["output_tokens"] == 180
    assert reader.ending["stop_reason"] == "tool_use"
    assert hold.triggered and hold.held_events > 0
    reader, hold = _parse(reply_events("end_turn", 10, "y"))
    assert reader.ending["stop_reason"] == "end_turn" and hold.held_events == 0


def test_fake_upstream_counts_the_forwarded_body():
    upstream = FakeUpstream()
    port = upstream.start()
    try:
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/v1/messages", body=body, headers={"x-scale-id": "a1", "x-scale-reply": "end_turn",
                                                                   "Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        conn.close()
    finally:
        upstream.stop()
    assert response.status == 200 and b"message_stop" in data
    record = upstream.take("a1")
    assert record["bytes"] == len(body) and record["tokens"] == synth.tokens_of(len(body)) and record["messages"] == 1


class _KeepAliveServer:
    """A stdlib HTTP/1.1 server on 127.0.0.1 that keeps connections open and counts them."""

    def __init__(self) -> None:
        owner = self
        self.connections = 0

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                owner.connections += 1
                super().setup()

            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                payload = b"event: message_stop\ndata: {}\n\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_the_driver_sends_every_request_over_one_kept_open_connection():
    """Claude Code keeps its connection to the proxy open. A connection per request left about 850
    sockets in TIME_WAIT per short run, and a few runs at once ran out of local ports (EADDRNOTAVAIL)."""
    server = _KeepAliveServer()
    client = run.Client(server.port)
    try:
        results = [client.send(b"{}", f"k{i}", "end_turn") for i in range(3)]
    finally:
        client.close()
        server.stop()
    assert [r["status"] for r in results] == [200, 200, 200]
    assert client.connections == 1 and server.connections == 1


def test_the_driver_reconnects_before_the_clock_when_the_server_closed():
    """The fake upstream closes each connection after its reply; the next send opens a new one before
    its clock starts, so the connect is not in ``e2e_s`` (``added_ms`` sends straight to it)."""
    upstream = FakeUpstream()
    port = upstream.start()
    client = run.Client(port)
    try:
        results = [client.send(b'{"messages": []}', f"r{i}", "end_turn") for i in range(2)]
    finally:
        client.close()
        upstream.stop()
    assert [r["status"] for r in results] == [200, 200]
    assert client.connections == 2 and upstream.connections == 2


def test_a_connect_is_retried_only_while_no_local_port_is_free(monkeypatch):
    failures = [OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")] * 3
    calls = []

    def connect(self):
        calls.append(1)
        if failures:
            raise failures.pop()
        self.sock = socket.socket()  # unconnected: enough for the driver's TCP_NODELAY

    monkeypatch.setattr(http.client.HTTPConnection, "connect", connect)
    monkeypatch.setattr(run.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(run, "PATIENCE", run.ConnectPatience())
    client = run.Client(9)
    client.connect()
    assert len(calls) == 4 and client.connections == 1
    client.close()

    def refused(self):
        calls.append(1)
        raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", refused)
    calls.clear()
    with pytest.raises(ConnectionRefusedError):
        run.Client(9).connect()
    assert len(calls) == 1, "only EADDRNOTAVAIL is worth waiting out"


def test_the_wait_for_a_free_port_is_a_total_for_the_run(monkeypatch):
    """Every connect of a run spends one ``CONNECT_PATIENCE_S``. With a wait per connect, a machine whose
    ports stayed used up took about 22 minutes to fail a short run (42 sequential connects of 31 s each)."""
    slept: list[float] = []
    calls = []

    def no_port(self):
        calls.append(1)
        raise OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")

    now = [0.0]

    def sleep(seconds: float) -> None:  # one connect at a time: the clock moves with each wait
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(http.client.HTTPConnection, "connect", no_port)
    monkeypatch.setattr(run.time, "sleep", sleep)
    monkeypatch.setattr(run, "PATIENCE", run.ConnectPatience(clock=lambda: now[0]))
    with pytest.raises(OSError):
        run.Client(9).connect()
    assert sum(slept) == pytest.approx(run.CONNECT_PATIENCE_S) and max(slept) <= 2.0
    first = len(calls)
    for _ in range(3):  # later connects of the same run find the patience spent and raise at once
        with pytest.raises(OSError):
            run.Client(9).connect()
    assert len(calls) == first + 3 and sum(slept) == pytest.approx(run.CONNECT_PATIENCE_S)
    run.PATIENCE.reset()  # a new run (``short.measure`` calls this) has the whole wait again
    with pytest.raises(OSError):
        run.Client(9).connect()
    assert sum(slept) == pytest.approx(2 * run.CONNECT_PATIENCE_S)


def test_waits_that_overlap_count_once():
    """#262 Reviewer B low 1: the burst's clients wait for a port at the same time. Counted in
    thread-seconds, 100 of them spent the run's whole wait in under half a second; the wait is a window
    of wall-clock time, and waits that overlap count once."""
    now = [0.0]
    patience = run.ConnectPatience(seconds=30.0, clock=lambda: now[0])
    assert [patience.take(2.0) for _ in range(100)] == [2.0] * 100  # at once: 2 s of the 30 s
    assert patience.left == pytest.approx(28.0)
    now[0] = 1.0
    assert patience.take(2.0) == 2.0 and patience.left == pytest.approx(27.0)  # 1 s of it overlaps
    now[0] = 10.0
    assert patience.take(30.0) == pytest.approx(27.0) and patience.left == 0  # one at a time: they add up
    now[0] = 36.0  # inside the window already granted (to 37 s): free, and nothing is left to add
    assert patience.take(2.0) == pytest.approx(1.0)
    now[0] = 38.0
    assert patience.take(0.05) == 0.0


def test_every_client_of_a_burst_waits_out_the_runs_whole_wait(monkeypatch):
    """With real threads and real sleeps: each of 20 clients short of a port waits about the whole of a
    0.5 s window before it gives up, instead of a twentieth of it."""
    def no_port(self):
        raise OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", no_port)
    monkeypatch.setattr(run, "PATIENCE", run.ConnectPatience(seconds=0.5))
    waited: list[float] = []

    def client() -> None:
        start = time.monotonic()
        with pytest.raises(OSError):
            run.Client(9).connect()
        waited.append(time.monotonic() - start)

    threads = [threading.Thread(target=client) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(waited) == 20 and min(waited) >= 0.4, waited


def test_the_driver_sends_without_waiting_for_acks(monkeypatch):
    """The driver sets ``TCP_NODELAY`` on every connection itself, so that its timings do not rest on
    ``http.client``, which sets it too (in ``HTTPConnection.connect`` on every Python CI runs). The
    41 ms each request waits on Linux is the proxy's, not the driver's (README.md, "The 40 ms wait on
    Linux"; issue #284). To show the driver's own line works, ``http.client`` here clears the option."""
    real = http.client.HTTPConnection.connect

    def connect_without_nodelay(self):
        real(self)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 0)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", connect_without_nodelay)
    server = _KeepAliveServer()
    client = run.Client(server.port)
    try:
        client.send(b"{}", "n0", "end_turn")
        assert client.conn.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        client.close()  # and on a connection opened again
        client.send(b"{}", "n1", "end_turn")
        assert client.conn.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
    finally:
        client.close()
        server.stop()


def test_the_fake_upstream_closes_each_connection_first():
    """The side that closes first keeps the TIME_WAIT. When the fake upstream closes, it is held on the
    upstream's listening port and not on one of the proxy's local ports, which the proxy (a new upstream
    connection per request, by design) would otherwise use up in a few runs at once."""
    upstream = FakeUpstream()
    port = upstream.start()
    try:
        body = json.dumps({"messages": []}).encode()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\nx-scale-id: c1\r\n"
                         b"x-scale-reply: end_turn\r\n\r\n%s" % (len(body), body))
            data = b""
            while chunk := sock.recv(65536):  # returns b"" only once the upstream has closed
                data += chunk
    finally:
        upstream.stop()
    head = data.split(b"\r\n\r\n", 1)[0].lower()
    assert b"connection: close" in head and data.endswith(b"0\r\n\r\n") and b"message_stop" in data
    assert upstream.connections == 1


def test_a_short_run_that_fails_reports_what_it_measured(monkeypatch, tmp_path):
    """conftest.py prints this when the fixture errors, so a CI log holds the numbers measured up to the
    failure, and the step it failed in."""
    from scale.proxy import short

    def broken_burst(*args, **kwargs):
        raise RuntimeError("burst broke")

    monkeypatch.setattr(short, "burst", broken_burst)
    progress: dict = {}
    with pytest.raises(RuntimeError, match="burst broke"):
        short.measure(2, startups=1, clients=1, per_client=1, home=tmp_path, progress=progress)
    assert progress["stage"] == "burst" and len(progress["startup_all_ms"]) == 1
    assert progress["first_request_added_ms"] > 0 and progress["first_request_direct_ms"] > 0
    text = short.failure_report(RuntimeError("burst broke"), progress)
    assert "burst broke" in text and "stage burst" in text and "first_request_added_ms" in text


def test_the_short_run_takes_a_turn_size():
    from scale.proxy import short

    assert short.short_args().turn_tokens == short.TURN_TOKENS == 3_000
    assert short.short_args(turn_tokens=26_000).turn_tokens == 26_000
    assert short.main(["--turn-tokens", "26000", "--spread", "/dev/null"]) == 0


def test_summary_statistics():
    assert summarize.p95([1.0, 2.0, 3.0, 4.0, 100.0]) == 100.0
    assert summarize.p95(list(map(float, range(101)))) == 95.0
    assert summarize.median_of_medians([[1.0, 9.0, 2.0], [5.0], []]) == 3.5
