"""Pass-through measuring proxy for the M0 interception study (phase 1).

Listens on 127.0.0.1 only and forwards every request to one upstream base URL with the body and
headers unchanged (hop-by-hop headers and Host are the only ones a proxy must replace). Responses,
including SSE streams, are relayed chunk by chunk as they arrive. A side copy of each response is
decoded only to read usage numbers.

Per request it appends one detailed line to requests.jsonl and one lane 2 summary line to
summary.jsonl. Neither contains header values, prompt text, tool output, or credentials.

Usage (standalone): python3 proxy.py --upstream URL --out DIR --cli NAME [--port N]
The drivers in run_session.py run it in-process so they can label user turns.
"""

from __future__ import annotations

import argparse
import http.client
import json
import secrets
import select
import selectors
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from extract import (
    StreamDecoder,
    UsageCollector,
    WsFrameParser,
    body_metrics,
    endpoint_of,
    header_facts,
    permessage_deflate,
    request_metrics,
    summary_row,
    total_tokens,
)

LISTEN_HOST = "127.0.0.1"
# Local control paths, answered by the proxy itself and never forwarded.
CONTROL_PREFIX = "/__m0proxy/"
UPSTREAM_TIMEOUT_S = 900
WS_IDLE_TIMEOUT_S = 900
# Hop-by-hop headers (RFC 9110 7.6.1) plus Host and Content-Length, which the proxy sets itself.
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def numeric_leaves(value: Any, prefix: str = "") -> dict[str, int | float]:
    """Flatten a usage object to its numeric leaves, dropping strings such as service tier."""
    out: dict[str, int | float] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(numeric_leaves(v, f"{prefix}{k}."))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out[prefix.rstrip(".")] = value
    return out


class SpikeProxy:
    def __init__(self, upstream: str, out_dir: Path, cli: str, cap_tokens: int | None = None,
                 block_hashes: bool = False) -> None:
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"upstream must be an http(s) base URL, got scheme {parts.scheme!r}")
        self.scheme = parts.scheme
        self.upstream_host = parts.hostname
        self.upstream_port = parts.port or (443 if parts.scheme == "https" else 80)
        self.upstream_netloc = parts.netloc.rsplit("@", 1)[-1]
        self.base_path = parts.path.rstrip("/")
        self.cli = cli
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = out_dir / "requests.jsonl"
        self.summary_path = out_dir / "summary.jsonl"
        # Per-run HMAC key for prefix stability checks. Held in memory only, never written.
        self.hmac_key = secrets.token_bytes(32)
        self.turn: int | None = None
        # Safety stop: past this many tokens (all categories) new model calls are refused with a
        # local error. Requests are never modified; in-flight ones finish normally.
        self.cap_tokens = cap_tokens
        # Per-block keyed hashes (for locating what breaks the cache prefix); off by default.
        self.block_hashes = block_hashes
        self.spent = 0
        self.refused = 0
        self._seq = 0
        self._inflight = 0
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.records: list[dict[str, Any]] = []

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://{LISTEN_HOST}:{self._server.server_address[1]}"

    def start(self, port: int = 0) -> str:
        proxy = self

        class Handler(_Handler):
            spike = proxy

        self._server = ThreadingHTTPServer((LISTEN_HOST, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="m0-proxy", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def begin(self) -> int:
        with self._lock:
            self._seq += 1
            self._inflight += 1
            return self._seq

    def note_parser_break(self) -> None:
        with self._lock:
            with (self.out_dir / "notes.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": utc_now(), "note": "websocket frame parser lost sync; relay unaffected"}) + "\n")

    def over_cap(self) -> bool:
        with self._lock:
            return self.cap_tokens is not None and self.spent >= self.cap_tokens

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"turn": self.turn, "spent": self.spent, "cap_tokens": self.cap_tokens,
                    "inflight": self._inflight, "requests": self._seq, "refused": self.refused,
                    "last_ts": self.records[-1]["ts"] if self.records else None,
                    "last_stop_reason": self.records[-1].get("stop_reason") if self.records else None}

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until no request is in flight, so a driver reads complete records."""
        with self._idle:
            return self._idle.wait_for(lambda: self._inflight == 0, timeout)

    def connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.upstream_host, self.upstream_port, timeout=UPSTREAM_TIMEOUT_S, context=ssl.create_default_context()
            )
        return http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=UPSTREAM_TIMEOUT_S)

    def write(self, record: dict[str, Any]) -> None:
        """Append the finished record and mark its request done."""
        with self._lock:
            self._inflight -= 1
            self._idle.notify_all()
            self.spent += total_tokens(record.get("usage") or {})
            self.records.append(record)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
            with self.summary_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(summary_row(record)) + "\n")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    spike: SpikeProxy

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # the default access log would print paths to stderr; requests.jsonl is the log

    def do_GET(self) -> None:
        self._forward()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            body = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(body)
                body += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _control(self) -> None:
        """POST /__m0proxy/turn {"turn": n} labels later requests; GET /__m0proxy/status reports."""
        spike = self.spike
        if self.command == "POST" and self.path == CONTROL_PREFIX + "turn":
            try:
                turn = json.loads(self._read_body() or b"{}").get("turn")
            except ValueError:
                turn = None
            if turn is not None and not isinstance(turn, int):
                self._reply_json(400, {"error": "turn must be an integer or null"})
                return
            spike.turn = turn
        elif not (self.command == "GET" and self.path == CONTROL_PREFIX + "status"):
            self._reply_json(404, {"error": "unknown control path"})
            return
        self._reply_json(200, spike.status())

    def _refuse_over_cap(self) -> None:
        with self.spike._lock:
            self.spike.refused += 1
        self._read_body()
        self._reply_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message":
                         "M0 spike proxy: experiment token cap reached; request refused locally, not forwarded"}})

    def _forward(self) -> None:
        spike = self.spike
        if self.path.startswith(CONTROL_PREFIX):
            self._control()
            return
        if self.command == "POST" and spike.over_cap():
            self._refuse_over_cap()
            return
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            if spike.over_cap():
                self._reply_json(400, {"error": "M0 spike proxy: experiment token cap reached"})
                return
            self._websocket()
            return
        started = time.monotonic()
        record: dict[str, Any] = {
            "ts": utc_now(),
            "seq": spike.begin(),
            "turn": spike.turn,
            "cli": spike.cli,
            "method": self.command,
        }
        body = self._read_body()
        record.update(header_facts(list(self.headers.items())))
        record.update(request_metrics(self.path, body, self.headers.get("Content-Encoding"), spike.hmac_key,
                                      with_blocks=spike.block_hashes))

        conn = spike.connection()
        try:
            conn.putrequest(self.command, spike.base_path + self.path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", spike.upstream_netloc)
            for name, value in self.headers.items():
                if name.lower() not in _HOP:
                    conn.putheader(name, value)
            if body or self.command in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body or None)
            response = conn.getresponse()
        except OSError as exc:
            record.update(status=502, error=f"upstream: {type(exc).__name__}", duration_ms=_ms(started))
            conn.close()
            self._reply_error(502)
            spike.write(record)
            return

        content_type = response.getheader("Content-Type")
        content_encoding = response.getheader("Content-Encoding")
        record.update(status=response.status, response_content_type=content_type, response_content_encoding=content_encoding)
        collector = UsageCollector(content_type)
        decoder = StreamDecoder(content_encoding)
        length_header = response.getheader("Content-Length")
        chunked_out = length_header is None and self.command != "HEAD"

        self.send_response_only(response.status, response.reason)
        for name, value in response.getheaders():
            if name.lower() not in _HOP:
                self.send_header(name, value)
        if chunked_out:
            self.send_header("Transfer-Encoding", "chunked")
        elif length_header is not None:
            self.send_header("Content-Length", length_header)
        self.end_headers()

        response_bytes = 0
        first_byte_ms = None
        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                if first_byte_ms is None:
                    first_byte_ms = _ms(started)
                response_bytes += len(chunk)
                if chunked_out:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
                collector.feed(decoder.decode(chunk))
            if chunked_out:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            record["client_aborted"] = True
            self.close_connection = True
        except OSError as exc:
            record["error"] = f"relay: {type(exc).__name__}"
            self.close_connection = True
        finally:
            conn.close()

        collector.finish()
        record.update(
            response_bytes=response_bytes,
            ttfb_ms=first_byte_ms,
            duration_ms=_ms(started),
            usage=collector.normalized(),
            usage_raw=numeric_leaves(collector.usage),
            sse_events=collector.events,
            stop_reason=collector.stop_reason,
        )
        if not decoder.supported:
            record["usage_unavailable"] = f"response content-encoding {decoder.encoding}"
        spike.write(record)

    def _websocket(self) -> None:
        """Tunnel a WebSocket upgrade: the handshake and every frame are relayed byte for byte,
        and a side copy is parsed into one record per client message (Codex response.create)."""
        spike = self.spike
        started = time.monotonic()
        handshake: dict[str, Any] = {"ts": utc_now(), "seq": spike.begin(), "turn": spike.turn, "cli": spike.cli,
                                     "method": self.command, "endpoint": endpoint_of(self.path),
                                     "transport": "websocket-handshake"}
        handshake.update(header_facts(list(self.headers.items())))
        try:
            upstream = socket.create_connection((spike.upstream_host, spike.upstream_port), timeout=30)
            if spike.scheme == "https":
                upstream = ssl.create_default_context().wrap_socket(upstream, server_hostname=spike.upstream_host)
        except OSError as exc:
            handshake.update(status=502, error=f"upstream: {type(exc).__name__}", duration_ms=_ms(started))
            self._reply_error(502)
            spike.write(handshake)
            return
        lines = [f"{self.command} {spike.base_path}{self.path} HTTP/1.1", f"Host: {spike.upstream_netloc}"]
        lines += [f"{name}: {value}" for name, value in self.headers.items() if name.lower() != "host"]
        head = bytearray()
        try:
            upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            while b"\r\n\r\n" not in head:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                head += chunk
        except OSError as exc:
            handshake.update(status=502, error=f"handshake: {type(exc).__name__}", duration_ms=_ms(started))
            upstream.close()
            self._reply_error(502)
            spike.write(handshake)
            return
        response_head, _, rest = bytes(head).partition(b"\r\n\r\n")
        status_line, _, header_block = response_head.partition(b"\r\n")
        parts = status_line.split(b" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 502
        extensions = None
        for line in header_block.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"sec-websocket-extensions":
                extensions = value.strip().decode("latin-1")
        self.close_connection = True
        try:
            self.wfile.write(response_head + b"\r\n\r\n")
            self.wfile.flush()
        except OSError:
            upstream.close()
            handshake.update(status=status, client_aborted=True, duration_ms=_ms(started))
            spike.write(handshake)
            return
        handshake.update(status=status, duration_ms=_ms(started), ws_extensions=extensions)
        spike.write(handshake)
        session = _WsSession(spike, endpoint_of(self.path), extensions)
        if status != 101:
            try:
                self.wfile.write(rest)
            except OSError:
                pass
            upstream.close()
            return
        try:
            if rest:
                _send_all(self.connection, rest)
                session.from_server(rest)
            _relay(self.connection, upstream, session)
        finally:
            upstream.close()
            session.close()

    def _reply_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True  # a client dropping an idle keep-alive socket

    def _reply_error(self, status: int) -> None:
        try:
            self.send_response_only(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            pass


class _WsSession:
    """Per-connection accounting: client messages open records; the server events that end a
    response (response.completed, error, ...) close the oldest open one."""

    def __init__(self, spike: SpikeProxy, endpoint: str, extensions: str | None) -> None:
        deflate, client_nct, server_nct = permessage_deflate(extensions)
        self.spike = spike
        self.endpoint = endpoint
        self.client = WsFrameParser(deflate, client_nct)
        self.server = WsFrameParser(deflate, server_nct)
        self.open: list[tuple[dict[str, Any], UsageCollector, float]] = []

    def from_client(self, data: bytes) -> None:
        for opcode, payload, wire in self.client.feed(data):
            if opcode not in (1, 2):
                continue
            spike = self.spike
            record: dict[str, Any] = {"ts": utc_now(), "seq": spike.begin(), "turn": spike.turn, "cli": spike.cli,
                                      "method": "WS", "endpoint": self.endpoint, "transport": "websocket",
                                      "wire_bytes": wire, "request_bytes": len(payload)}
            try:
                record.update(body_metrics(json.loads(payload), spike.hmac_key))
            except ValueError:
                record["decode_error"] = "not json"
            self.open.append((record, UsageCollector("websocket"), time.monotonic()))

    def from_server(self, data: bytes) -> None:
        for opcode, payload, _ in self.server.feed(data):
            if opcode not in (1, 2) or not self.open:
                continue
            record, collector, started = self.open[0]
            record["response_bytes"] = record.get("response_bytes", 0) + len(payload)
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if collector.handle(event):
                self.open.pop(0)
                self._finish(record, collector, started)

    def close(self) -> None:
        for record, collector, started in self.open:
            record["incomplete"] = True
            self._finish(record, collector, started)
        self.open = []
        if self.client.broken or self.server.broken:
            self.spike.note_parser_break()

    def _finish(self, record: dict[str, Any], collector: UsageCollector, started: float) -> None:
        record.update(status=101, duration_ms=_ms(started), usage=collector.normalized(),
                      usage_raw=numeric_leaves(collector.usage), sse_events=collector.events,
                      stop_reason=collector.stop_reason)
        self.spike.write(record)


def _send_all(sock: socket.socket, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            sent = sock.send(view)
            view = view[sent:]
        except (BlockingIOError, ssl.SSLWantWriteError, ssl.SSLWantReadError):
            select.select([], [sock], [], 30)


def _relay(client: socket.socket, upstream: socket.socket, session: _WsSession) -> None:
    """Single-threaded relay; one TLS socket must not be read and written from two threads."""
    client.setblocking(False)
    upstream.setblocking(False)
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, "client")
    selector.register(upstream, selectors.EVENT_READ, "upstream")
    try:
        while True:
            events = selector.select(timeout=WS_IDLE_TIMEOUT_S)
            if not events:
                return
            for key, _ in events:
                source = key.fileobj
                try:
                    data = source.recv(65536)
                except (BlockingIOError, ssl.SSLWantReadError):
                    continue
                except OSError:
                    return
                if not data:
                    return
                pending = getattr(source, "pending", lambda: 0)
                while pending():
                    data += source.recv(pending())
                if key.data == "client":
                    _send_all(upstream, data)
                    session.from_client(data)
                else:
                    _send_all(client, data)
                    session.from_server(data)
    finally:
        selector.close()


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--upstream", help="base URL the CLI would otherwise use")
    source.add_argument("--upstream-inherited", choices=("claude",),
                        help="resolve the upstream at run time from this worktree's inherited Claude Code "
                             "settings, so the URL never appears in argv, files, or logs")
    parser.add_argument("--cap-tokens", type=int, default=None,
                        help="refuse new POSTs once this many tokens (all categories) were used")
    parser.add_argument("--out", required=True, type=Path, help="output dir under .contexto/spikes/")
    parser.add_argument("--cli", required=True, help="label written to every row, e.g. claude or codex")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    upstream = args.upstream
    if args.upstream_inherited == "claude":
        from run_session import inherited_claude_base

        upstream = inherited_claude_base()
        if not upstream:
            parser.error("no inherited ANTHROPIC_BASE_URL found")
    proxy = SpikeProxy(upstream, args.out, args.cli, cap_tokens=args.cap_tokens)
    print(f"listening on {proxy.start(args.port)}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        proxy.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
