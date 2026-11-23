"""Regression tests for proxy HTTP/1.1 framing, inbound body safety, connection pooling, TCP_NODELAY,
StreamHold edge cases, UsageReader fixes, and _ClientWatch shutdown."""

from __future__ import annotations

import gzip
import http.client
import json
import socket
import ssl
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

import contexto.proxy as proxy_module
from contexto.proxy import (
    MAX_CALL_IDS,
    ContextoProxy,
    StreamHold,
    UsageReader,
    _ClientWatch,
    _UpstreamHTTP,
    _UpstreamHTTPS,
    decode_request_body,
)
from contexto.store.memory import MemoryStore


class _Log(MemoryStore):
    def requests(self, count: int = 1, timeout: float = 5.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while True:
            found = [e for e in self.events if e["event"] == "request"]
            if len(found) >= count or time.monotonic() > deadline:
                return found
            time.sleep(0.01)


def _sse(*events: dict) -> bytes:
    return b"".join(b"event: " + e["type"].encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)


def _raw_exchange(port: int, payload: bytes, *, half_close: bool = False) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(payload)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        out = b""
        while chunk := sock.recv(65536):
            out += chunk
        return out


# 1. HTTP/1.1 204 / 304 / 1xx framing & keep-alive synchronization ---------------------------------


@pytest.mark.parametrize("status, reason", [(204, "No Content"), (304, "Not Modified")])
def test_204_and_304_responses_are_not_chunked_and_preserve_keep_alive(status, reason):
    """204 and 304 responses have no body; sending Transfer-Encoding: chunked or 0\\r\\n\\r\\n
    desynchronizes the next request on the same HTTP/1.1 keep-alive connection."""
    class NoBodyUpstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/empty":
                self.send_response(status, reason)
                self.send_header("X-Test", "no-body")
                self.end_headers()
                return
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), NoBodyUpstream)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    log = _Log()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", log)
    proxy.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        conn.request("GET", "/empty")
        r1 = conn.getresponse()
        b1 = r1.read()
        assert r1.status == status and b1 == b""
        assert r1.getheader("Transfer-Encoding") is None
        if status == 204:
            assert r1.getheader("Content-Length") is None
        # Second request on the exact same keep-alive socket must succeed cleanly.
        conn.request("GET", "/ok")
        r2 = conn.getresponse()
        b2 = r2.read()
        assert r2.status == 200 and b2 == b'{"ok":true}'
        conn.close()
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()


# 2. Inbound body framing safety (chunked EOF, short read, uncompressed byte cap) ------------------


@pytest.mark.parametrize("raw_body", [
    b"Transfer-Encoding: chunked\r\n\r\n",  # immediate EOF instead of chunk size 0
    b"Transfer-Encoding: chunked\r\n\r\n5\r\nabc",  # truncated chunk payload (3 of 5 bytes)
    b"Transfer-Encoding: chunked\r\n\r\n5\r\nabcdeBAD\r\n0\r\n\r\n",  # malformed chunk terminator
    b"Transfer-Encoding: chunked\r\n\r\n0\r\n",  # EOF before final blank trailer line
    b"Content-Length: 20\r\n\r\nshort",  # truncated Content-Length body
    b"Content-Length: 5\r\nContent-Length: 6\r\n\r\nhello",  # conflicting Content-Length
], ids=["chunked_eof", "chunked_short_payload", "chunked_bad_crlf", "chunked_eof_trailer", "cl_short", "cl_conflict"])
def test_truncated_or_malformed_request_bodies_return_400_and_never_forward(raw_body):
    seen: list[bytes] = []

    class Recorder(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    log = _Log()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", log)
    proxy.start()
    try:
        head = f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{proxy.port}\r\n".encode()
        reply = _raw_exchange(proxy.port, head + raw_body, half_close=True)
        assert reply.startswith(b"HTTP/1.1 400")
        assert seen == []
        assert log.requests()[0]["status"] == 400
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("chunked", [False, True], ids=["content_length", "chunked"])
def test_uncompressed_inbound_body_over_cap_returns_413(monkeypatch, chunked):
    monkeypatch.setattr(proxy_module, "MAX_REQUEST_BODY_BYTES", 100)
    seen: list[bytes] = []

    class Recorder(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    log = _Log()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", log)
    proxy.start()
    try:
        payload = b"x" * 150
        if chunked:
            wire = (f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{proxy.port}\r\n"
                    f"Transfer-Encoding: chunked\r\n\r\n{len(payload):x}\r\n").encode() + payload + b"\r\n0\r\n\r\n"
        else:
            wire = (f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{proxy.port}\r\n"
                    f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload
        reply = _raw_exchange(proxy.port, wire)
        assert reply.startswith(b"HTTP/1.1 413") and b"request_too_large" in reply
        assert seen == []
        assert log.requests()[0]["status"] == 413
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()


# 3. Repeated request headers & stacked Content-Encoding -------------------------------------------


def test_repeated_request_headers_are_preserved_on_passthrough_and_transformed_paths():
    recorded_headers: list[list[tuple[str, str]]] = []

    class HeaderSpy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            recorded_headers.append(list(self.headers.items()))
            body = b'{"id":"m1","type":"message","content":[],"usage":{"input_tokens":1,"output_tokens":1}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), HeaderSpy)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    log = _Log()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", log, observe_thinking=True)
    proxy.start()
    try:
        body = json.dumps({"model": "m", "stream": False, "messages": [{"role": "user", "content": "hi"}]}).encode()
        for path in ("/v1/other", "/v1/messages"):
            req = (
                f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{proxy.port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                "X-Multi: first\r\nX-Multi: second\r\n"
                "Cookie: a=1\r\nCookie: b=2\r\n"
                "anthropic-beta: beta-1\r\nanthropic-beta: beta-2\r\n"
                "Connection: close\r\n\r\n"
            ).encode() + body
            reply = _raw_exchange(proxy.port, req)
            assert reply.startswith(b"HTTP/1.1 200")
        # Both pass-through and transformed preserve untouched repeated headers.
        for hdrs in recorded_headers:
            assert [v for k, v in hdrs if k.lower() == "x-multi"] == ["first", "second"]
            assert [v for k, v in hdrs if k.lower() == "cookie"] == ["a=1", "b=2"]
        # On /v1/messages with observe_thinking=True, both original betas survive alongside the added beta.
        betas = ",".join(v for k, v in recorded_headers[1] if k.lower() == "anthropic-beta")
        assert "beta-1" in betas and "beta-2" in betas and proxy_module.THINKING_BINDING_BETA in betas
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("header_lines", [
    "Content-Encoding: deflate, gzip\r\n",
    "Content-Encoding: deflate\r\nContent-Encoding: gzip\r\n",
], ids=["comma_stacked", "repeated_header_stacked"])
def test_stacked_content_encoding_is_decoded_in_reverse_order(header_lines):
    seen: list[tuple[bytes, list[str]]] = []

    class Spy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append((body, self.headers.get_all("Content-Encoding") or []))
            resp = b'{"id":"m1","type":"message","content":[],"usage":{"input_tokens":5,"output_tokens":1}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Spy)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    log = _Log()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", log)
    proxy.start()
    try:
        plain = json.dumps({"model": "m", "stream": False, "messages": [{"role": "user", "content": "hi"}]}).encode()
        # Applied deflate first, then gzip -> decoded gzip first, then deflate.
        wire = gzip.compress(zlib.compress(plain))
        req = (
            f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{proxy.port}\r\n"
            f"Content-Type: application/json\r\n{header_lines}"
            f"Content-Length: {len(wire)}\r\nConnection: close\r\n\r\n"
        ).encode() + wire
        reply = _raw_exchange(proxy.port, req)
        assert reply.startswith(b"HTTP/1.1 200")
        assert seen == [(plain, [])]
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()


def test_excessive_stacked_content_encodings_are_rejected():
    with pytest.raises(proxy_module.EncodedBodyError, match="too many stacked"):
        decode_request_body(b"abc", ", ".join(["gzip"] * (proxy_module.MAX_CONTENT_ENCODINGS + 1)))


# 4. Dead-client finally bug, UsageReader bools/call_ids, Responses counts, StreamHold fixes -------


def test_dead_client_during_hold_does_not_call_observe_usage():
    """When watch.gone is True mid-hold, finally must NOT call write_events(hold.eof()) or observe()."""
    observed: list[dict] = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]

    def upstream():
        sock, _ = server.accept()
        with sock:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += sock.recv(65536)
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
            start = _sse({"type": "message_start", "message": {"usage": {"input_tokens": 50, "output_tokens": 1}}})
            tool = _sse({"type": "content_block_start", "index": 0,
                         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}})
            for raw in (start, tool):
                sock.sendall(b"%x\r\n%s\r\n" % (len(raw), raw))
            time.sleep(2.0)

    threading.Thread(target=upstream, daemon=True).start()
    log = _Log()
    proxy = ContextoProxy(f"http://127.0.0.1:{port}", log,
                          observe_usage=lambda ep, u, req: observed.append(u) or [])
    proxy.start()
    try:
        body = json.dumps({"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode()
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as client:
            client.sendall(
                f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{proxy.port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
            )
            head = b""
            while b"\r\n\r\n" not in head:
                head += client.recv(65536)
            time.sleep(0.15)  # tool_use is now held in StreamHold
        # Client disconnected while StreamHold held tool_use.
        entry = log.requests()[0]
        assert entry["client_aborted"] is True
        assert observed == []  # observe_usage must NOT have fired on the aborted stream
    finally:
        proxy.stop()
        server.close()


def test_usage_reader_rejects_booleans_as_ints():
    reader = UsageReader("application/json", "messages")
    body = json.dumps({"usage": {"input_tokens": True, "output_tokens": False,
                                 "cache_read_input_tokens": 42}}).encode()
    reader.feed(body)
    reader.finish()
    assert reader.usage == {"cache_read_input_tokens": 42}


def test_usage_reader_keeps_more_than_256_call_ids_and_deduplicates():
    reader = UsageReader("text/event-stream", "messages")
    for i in range(300):
        ev = _sse({"type": "content_block_start", "index": i,
                   "content_block": {"type": "tool_use", "id": f"toolu_{i}", "name": "Read", "input": {}}})
        reader.feed(ev)
    # Duplicate id ignored
    reader.feed(_sse({"type": "content_block_start", "index": 300,
                      "content_block": {"type": "tool_use", "id": "toolu_0", "name": "Read", "input": {}}}))
    reader.finish()
    assert len(reader.call_ids) == 300 and MAX_CALL_IDS >= 4096


def test_codex_responses_logs_input_item_count_and_guards_non_list_fields():
    class Echo(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b'{"id":"r1","status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    log = _Log()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", log)
    proxy.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        # 1. Codex /responses with 4 items in "input"
        body1 = json.dumps({"model": "gpt-6", "stream": False, "input": [{"type": "message"}] * 4}).encode()
        conn.request("POST", "/responses", body=body1, headers={"Content-Type": "application/json"})
        r1 = conn.getresponse()
        assert r1.status == 200 and r1.read()
        # 2. Malformed non-list "messages" (int) must not crash _transform
        body2 = json.dumps({"model": "m", "stream": False, "messages": 42}).encode()
        conn.request("POST", "/v1/messages", body=body2, headers={"Content-Type": "application/json"})
        r2 = conn.getresponse()
        assert r2.status == 200 and r2.read()
        conn.close()
        reqs = log.requests(2)
        assert reqs[0]["original_message_count"] == 4 and reqs[0]["message_count"] == 4
        assert reqs[1]["original_message_count"] == 0 and reqs[1]["message_count"] == 0
        assert not [e for e in log.events if e["event"] == "proxy_error"]
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()


def test_stream_hold_does_not_lock_up_on_early_message_delta():
    """An early message_delta before a tool_use must not trap subsequent events in StreamHold."""
    triggers: list[int] = []
    hold = StreamHold("messages", lambda: triggers.append(1))
    start = _sse({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})
    early_delta = _sse({"type": "message_delta", "delta": {"stop_reason": None}, "usage": {"output_tokens": 1}})
    tool_start = _sse({"type": "content_block_start", "index": 0,
                       "content_block": {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}})
    final_delta = _sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}})
    stop = _sse({"type": "message_stop"})

    assert hold.feed(start) == [start]
    assert hold.feed(early_delta) == [early_delta]
    assert triggers == [1] and hold.triggered
    # Tool call arrives after trigger: client_calls is recorded, but stream is NOT locked in hold.
    assert hold.feed(tool_start) == [tool_start]
    assert hold.client_calls and not hold.holding and hold.held == []
    assert hold.feed(final_delta) == [final_delta]
    assert hold.feed(stop) == [stop]
    assert triggers == [1]  # on_trigger called once


# 5. Latency & concurrency: TCP_NODELAY, SSLContext cache, pooling, stale retry, _ClientWatch ------


def test_tcp_nodelay_enabled_on_client_and_upstream_sockets():
    server_nodelay: list[int] = []

    class NodSpy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), NodSpy)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    log = _Log()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", log)
    proxy.start()
    try:
        orig_get_request = proxy._server.get_request

        def spy_get_request():
            conn, addr = orig_get_request()
            server_nodelay.append(conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
            return conn, addr

        proxy._server.get_request = spy_get_request  # type: ignore[method-assign]
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        conn.request("GET", "/ping")
        assert conn.getresponse().read() == b"ok"
        conn.close()
        assert server_nodelay and server_nodelay[0] != 0
        # Upstream HTTP socket also has TCP_NODELAY set.
        up = _UpstreamHTTP("127.0.0.1", server.server_address[1], timeout=5)
        up.connect()
        try:
            assert up.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
        finally:
            up.close()
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()


def test_ssl_context_is_cached_and_reused_across_connections(monkeypatch):
    created = 0
    real = ssl.create_default_context

    def counting_ctx(*args, **kwargs):
        nonlocal created
        created += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", counting_ctx)
    proxy = ContextoProxy("https://example.invalid", _Log())
    c1 = proxy.connection()
    c2 = proxy.connection()
    c3 = proxy.connection()
    assert created == 1
    assert isinstance(c1, _UpstreamHTTPS) and isinstance(c2, _UpstreamHTTPS)
    assert c1.tls_context is c2.tls_context is c3.tls_context


def test_upstream_connection_pool_reuses_sockets_and_closes_on_stop():
    accepted = 0

    class CountingServer(ThreadingHTTPServer):
        def get_request(self):
            nonlocal accepted
            accepted += 1
            return super().get_request()

    class KeepAliveHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = CountingServer(("127.0.0.1", 0), KeepAliveHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", _Log())
    proxy.start()
    try:
        for _ in range(5):
            conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
            conn.request("GET", "/v1/models")
            assert conn.getresponse().read() == b'{"ok":true}'
            conn.close()
        assert accepted == 1  # all 5 requests reused a single pooled upstream TCP socket
        assert len(proxy._pool._idle) == 1
    finally:
        proxy.stop()
        assert proxy._pool._idle == []
        server.shutdown()
        server.server_close()


def test_stale_pooled_keep_alive_socket_retries_cleanly_on_fresh_connection():
    """Even when a pooled socket passes poll(0) and is closed by upstream mid-request (RemoteDisconnected
    race), the proxy transparently opens a fresh connection without failing or logging upstream_retry."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    accept_count = 0

    def run_upstream():
        nonlocal accept_count
        # Connection 1: serve request 1, wait briefly for request 2 to be written, then close without answering!
        s1, _ = server.accept()
        accept_count += 1
        with s1:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += s1.recv(65536)
            s1.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\none")
            # Wait for second request bytes on the keep-alive socket, then drop cleanly (EOF -> RemoteDisconnected)
            s1.settimeout(2.0)
            try:
                s1.recv(65536)
            except OSError:
                pass
        # Connection 2: fresh connection answers request 2 cleanly
        s2, _ = server.accept()
        accept_count += 1
        with s2:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += s2.recv(65536)
            s2.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\ntwo")

    t = threading.Thread(target=run_upstream, daemon=True)
    t.start()
    log = _Log()
    proxy = ContextoProxy(f"http://127.0.0.1:{port}", log)
    proxy.start()
    try:
        c1 = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        c1.request("GET", "/first")
        assert c1.getresponse().read() == b"one"
        c1.close()

        c2 = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        c2.request("GET", "/second")
        assert c2.getresponse().read() == b"two"
        c2.close()

        assert accept_count == 2
        assert [e for e in log.events if e["event"] == "upstream_retry"] == []
    finally:
        proxy.stop()
        server.close()
        t.join(timeout=2.0)


def test_client_watch_wakes_immediately_and_joins_on_close():
    a, b = socket.socketpair()
    try:
        watch = _ClientWatch(a)
        time.sleep(0.01)  # let thread enter poller.poll()
        started = time.monotonic()
        watch.close()
        elapsed = time.monotonic() - started
        assert not watch._thread.is_alive()
        assert elapsed < 0.25  # event-driven pipe wakeup: well below the 5000 ms poll timeout
        assert watch._wake_r is None and watch._wake_w is None
        # Idempotent second close
        watch.close()
    finally:
        a.close()
        b.close()


def test_transform_flush_deferred_runs_outside_lock_and_regular_flush_is_not_called():
    locked_during_flush: list[bool] = []
    regular_flush_calls: list[int] = []

    class FlushingTransform:
        preserves_input = True

        def __call__(self, req, headers, endpoint):
            return req, headers, []

        def flush(self):
            # Must NEVER be auto-called per request (collides with SessionRecorder.flush).
            regular_flush_calls.append(1)

        def flush_deferred(self):
            locked_during_flush.append(proxy.transform_lock.locked())

    class Ok(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b'{"id":"m1","type":"message","content":[],"usage":{"input_tokens":1,"output_tokens":1}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", _Log(), FlushingTransform())
    proxy.start()
    try:
        body = json.dumps({"model": "m", "stream": False, "messages": [{"role": "user", "content": "hi"}]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        conn.request("POST", "/v1/messages", body=body, headers={"Content-Type": "application/json"})
        assert conn.getresponse().status == 200
        conn.close()
        assert locked_during_flush == [False]
        assert regular_flush_calls == []
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()


def test_stream_hold_streams_hosted_responses_calls_live_and_holds_only_client_calls():
    triggers: list[int] = []
    hold = StreamHold("responses", lambda: triggers.append(1))
    hosted = (
        b"event: response.output_item.added\n"
        b'data: {"type":"response.output_item.added","item":{"type":"web_search_call","id":"ws_1"}}\n\n'
    )
    hosted_delta = (
        b"event: response.web_search_call.completed\n"
        b'data: {"type":"response.web_search_call.completed","item_id":"ws_1"}\n\n'
    )
    # Hosted tool calls stream live without holding.
    assert hold.feed(hosted) == [hosted]
    assert hold.feed(hosted_delta) == [hosted_delta]
    assert not hold.holding and not hold.client_calls and hold.held_events == 0

    # A subsequent client function_call DOES activate StreamHold.
    client_call = (
        b"event: response.output_item.added\n"
        b'data: {"type":"response.output_item.added","item":{"type":"function_call","call_id":"fc_1"}}\n\n'
    )
    completed = (
        b"event: response.completed\n"
        b'data: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'
    )
    assert hold.feed(client_call) == []
    assert hold.holding and hold.client_calls
    assert hold.feed(completed) == [client_call, completed]
    assert triggers == [1]


def test_post_is_never_retried_in_wait_headers_on_reused_pooled_socket():
    """RFC 9112 §9.8 & Contract Rule 8: a non-idempotent POST that fails in wait_headers after
    endheaders() completes on a reused pooled socket returns 502 instead of silently re-executing."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    posts_seen = 0

    def run_upstream():
        nonlocal posts_seen
        s1, _ = server.accept()
        with s1:
            # 1st request (GET): answer with keep-alive so proxy pools s1.
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += s1.recv(65536)
            s1.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            # 2nd request (POST) arrives on pooled s1: read full request, then drop before headers!
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s1.recv(65536)
                if not chunk:
                    return
                buf += chunk
            posts_seen += 1
            # Close without sending a response (RemoteDisconnected in wait_headers).

    t = threading.Thread(target=run_upstream, daemon=True)
    t.start()
    log = _Log()
    proxy = ContextoProxy(f"http://127.0.0.1:{port}", log)
    proxy.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        c.request("GET", "/warm")
        assert c.getresponse().read() == b"ok"
        c.request("POST", "/v1/messages", body=b'{"messages":[]}', headers={"Content-Type": "application/json"})
        resp = c.getresponse()
        assert resp.status == 502
        resp.read()
        c.close()
        assert posts_seen == 1
    finally:
        proxy.stop()
        server.close()
        t.join(timeout=2.0)


def test_head_304_1xx_and_chunked_plus_cl_upstream_response_framing():
    """Verify HEAD, 304 (with CL), 103 interim (never pooled), and TE:chunked+CL responses."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]

    def read_req(sock: socket.socket) -> bytes:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf

    def run_upstream():
        # Connection 1: HEAD with CL, 304 with CL, TE:chunked + bogus CL
        s1, _ = server.accept()
        with s1:
            read_req(s1)
            s1.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 123\r\n\r\n")
            read_req(s1)
            s1.sendall(b"HTTP/1.1 304 Not Modified\r\nContent-Length: 45\r\n\r\n")
            read_req(s1)
            s1.sendall(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 999\r\n\r\n"
                b"5\r\nhello\r\n0\r\n\r\n"
            )
            # 103 interim response: proxy must NOT pool s1 after this!
            read_req(s1)
            s1.sendall(b"HTTP/1.1 103 Early Hints\r\nLink: </a.css>; rel=preload\r\n\r\n")
        # Connection 2 (fresh, proving 103 did not poison the pool): normal 200 OK
        s2, _ = server.accept()
        with s2:
            read_req(s2)
            s2.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nfresh")

    t = threading.Thread(target=run_upstream, daemon=True)
    t.start()
    proxy = ContextoProxy(f"http://127.0.0.1:{port}", _Log())
    proxy.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        c.request("HEAD", "/meta")
        r1 = c.getresponse()
        assert r1.status == 200 and r1.getheader("Content-Length") == "123"
        assert r1.getheader("Transfer-Encoding") is None
        assert r1.read() == b""

        c.request("GET", "/cached")
        r2 = c.getresponse()
        assert r2.status == 304 and r2.getheader("Content-Length") == "45"
        assert r2.getheader("Transfer-Encoding") is None
        assert r2.read() == b""

        c.request("GET", "/chunked-plus-cl")
        r3 = c.getresponse()
        assert r3.status == 200 and r3.getheader("Transfer-Encoding") == "chunked"
        assert r3.getheader("Content-Length") is None
        assert r3.read() == b"hello"
        c.close()

        # 103 interim response is not pooled and closes client connection cleanly.
        raw_sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        try:
            raw_sock.sendall(b"GET /early HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = raw_sock.recv(4096)
                if not chunk:
                    break
                head += chunk
            assert head.startswith(b"HTTP/1.1 103 ")
        finally:
            raw_sock.close()

        # Next request gets a fresh upstream connection and reads "fresh", never stale bytes.
        c2 = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
        c2.request("GET", "/after-103")
        r4 = c2.getresponse()
        assert r4.status == 200 and r4.read() == b"fresh"
        c2.close()
    finally:
        proxy.stop()
        server.close()
        t.join(timeout=2.0)


@pytest.mark.parametrize("raw_request", [
    b"POST /x HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n0\r\n\r\n",
    b"POST /x HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: notchunked\r\n\r\n0\r\n\r\n",
    b"POST /x HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: gzip\r\n\r\nabc",
    b"POST /x HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n" + b"X-T: 1\r\n" * 70 + b"\r\n",
], ids=["te-and-cl", "te-notchunked", "te-nonchunked", "excessive-trailers"])
def test_strict_transfer_encoding_and_trailer_caps_return_400_with_connection_close(raw_request):
    proxy = ContextoProxy("http://127.0.0.1:1", _Log())
    proxy.start()
    try:
        raw_resp = _raw_exchange(proxy.port, raw_request)
        head = raw_resp.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        assert head.startswith("HTTP/1.1 400 ")
        assert "connection: close" in head.lower()
    finally:
        proxy.stop()


def test_upstream_pool_eviction_ttl_dirty_discard_and_unstarted_proxy_stop():
    from contexto.proxy import _UpstreamPool

    class FakeSock:
        def __init__(self, fileno_val: int = -1, dirty: bool = False):
            self._fd = fileno_val
            self._dirty = dirty
            self.closed = False

        def fileno(self) -> int:
            return self._fd

    class FakeConn:
        def __init__(self, sock=None):
            self.sock = sock
            self.closed = False

        def close(self):
            self.closed = True
            self.sock = None

    # Max-idle eviction closes oldest connection.
    a1, b1 = socket.socketpair()
    a2, b2 = socket.socketpair()
    a3, b3 = socket.socketpair()
    try:
        pool = _UpstreamPool(max_idle=2, idle_ttl_s=30.0)
        c1, c2, c3 = FakeConn(a1), FakeConn(a2), FakeConn(a3)
        pool.release(c1)
        pool.release(c2)
        pool.release(c3)
        assert c1.closed and not c2.closed and not c3.closed
        # LIFO reuse: c3 then c2
        assert pool.acquire() is c3
        assert pool.acquire() is c2
        assert pool.acquire() is None

        # Dirty socket (unconsumed bytes on peer) is discarded and closed on acquire.
        b2.sendall(b"garbage")
        pool.release(c2)
        assert pool.acquire() is None
        assert c2.closed

        # Expired TTL connection is discarded on acquire.
        pool_ttl = _UpstreamPool(max_idle=2, idle_ttl_s=0.01)
        pool_ttl.release(c3)
        time.sleep(0.03)
        assert pool_ttl.acquire() is None
        assert c3.closed

        # Unstarted ContextoProxy.stop() closes pooled sockets (serve.py path).
        a4, b4 = socket.socketpair()
        try:
            unstarted = ContextoProxy("http://127.0.0.1:1", _Log())
            c4 = FakeConn(a4)
            unstarted._pool.release(c4)
            unstarted.stop()
            assert c4.closed
        finally:
            a4.close()
            b4.close()
    finally:
        for s in (a1, b1, a2, b2, a3, b3):
            s.close()


def test_side_decoder_stacked_encodings_and_bomb_limit_and_responses_bool_usage():
    from contexto.proxy import _SideDecoder

    plain = b'{"usage":{"input_tokens":42}}'
    stacked = gzip.compress(zlib.compress(plain))
    dec = _SideDecoder("deflate, gzip")
    assert dec.supported and dec.decode(stacked) == plain

    # Bomb past limit marks decoder unsupported instead of exhausting memory.
    bomb = gzip.compress(b"A" * 100_000)
    capped = _SideDecoder("gzip", limit=1024)
    assert capped.decode(bomb) == b""
    assert not capped.supported

    # UsageReader rejects bool on Responses API too.
    reader = UsageReader("application/json", api="responses")
    reader.feed(json.dumps({
        "status": "completed",
        "output": [],
        "usage": {"input_tokens": True, "output_tokens": 5, "input_tokens_details": {"cached_tokens": False}},
    }).encode())
    reader.finish()
    assert reader.usage == {"output_tokens": 5, "input_tokens_details": {}}


def test_serve_routes_repeated_content_encoding_without_bypass_and_closes_pool(tmp_path):
    from contexto.serve import ServeService
    from contexto.upstream import Upstream as ResolvedUpstream

    upstream_bodies: list[dict] = []

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            upstream_bodies.append(json.loads(raw))
            resp = b'{"id":"m1","type":"message","content":[],"usage":{"input_tokens":10,"output_tokens":2}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    home = tmp_path / "home"
    svc = ServeService(
        home,
        ResolvedUpstream(url=f"http://127.0.0.1:{up.server_address[1]}", source="flag"),
        "a" * 32,
        reaper=False,
    )
    svc.start(0)
    try:
        payload = json.dumps({
            "model": "claude-sonnet-5",
            "max_tokens": 64,
            "stream": False,
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"user_id": "u_session_11111111-1111-4111-8111-111111111111"},
        }).encode()
        wire = gzip.compress(zlib.compress(payload))
        req = (
            f"POST /{svc.token}/v1/messages HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{svc.port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Encoding: deflate\r\n"
            f"Content-Encoding: gzip\r\n"
            f"Content-Length: {len(wire)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode() + wire
        raw_resp = _raw_exchange(svc.port, req)
        assert raw_resp.startswith(b"HTTP/1.1 200 ")
        # Must have been routed to a managed session (not unrouted passthrough).
        assert len(svc._sessions) == 1
        session = next(iter(svc._sessions.values()))
        # Pooled upstream socket is open before stop and closed by svc.stop().
        assert len(session.proxy._pool._idle) == 1
        svc.stop()
        assert len(session.proxy._pool._idle) == 0
        assert len(svc.passthrough._pool._idle) == 0
    finally:
        svc.stop()
        up.shutdown()
        up.server_close()
