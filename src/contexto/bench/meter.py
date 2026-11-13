"""Metering for the live benchmark: one ``requests.jsonl`` record per billed request, for every arm.

``Meter`` is a pass-through on 127.0.0.1 between an arm and the real upstream. All three arms send
their Messages requests through one: the ``contexto`` arm as Contexto's ``--upstream``, the ``plain``
arm as Claude Code's base URL, the ``full`` arm as the base URL of its own requests. So the three
arms' counts come from one piece of code reading the same field of the same kind of response.

What it forwards. Every request goes to the real upstream with its method, path, query, headers and
body as received, and the response comes back with its status, headers and body as received, each
chunk written to the client as soon as it is read, so a stream keeps its timing. When the client
closes its connection the upstream connection is closed at once. Nothing is retried. It changes four
things, and nothing else:

- ``Host`` names the real upstream, and hop-by-hop headers are not forwarded (as any proxy).
- On ``POST /v1/messages`` and its ``count_tokens`` the request's ``Accept-Encoding`` becomes
  ``identity``, so the usage in the response can be read. Contexto's own proxy does the same to the
  requests it forwards, so this changes the ``plain`` arm's requests only.
- ``X-Session-Affinity`` is removed when the real upstream is a vendor's host. Contexto adds that
  header for any upstream that is not a vendor's (D36), and the meter is one; without the meter
  Contexto would not have sent it to a vendor.
- The request-class headers that ``CLAUDE_CODE_GATEWAY_HINT_HEADERS`` makes Claude Code send (the
  benchmark sets it so that the meter can tell a main request from a side request without reading
  the conversation) are removed, unless the arm is ``plain`` and the real upstream is a vendor's
  host, where Claude Code sends them without being asked.

What it records, for each ``POST /v1/messages`` that got a response: the arm, seed, repeat and model
of the unit, the request's order in the unit, the script turn in flight (the arm sets ``Meter.turn``
before it sends a turn), whether it is a main request, the input tokens by class and the output
tokens from the response's ``usage``, the HTTP status, and whether it carried out a compaction. It
reads the request body in memory for ``max_tokens`` and the presence of a tool list, and keeps
nothing of it. ``count_tokens`` and every other path are forwarded and not recorded.

``Ledger`` is the spend guard of the whole run. Before a billed request is forwarded the meter
reserves its worst case: the request's bytes at 1.5 bytes per token, a rate that overstates every
content this benchmark sends, plus the request's ``max_tokens``. If what is spent, what is reserved
by requests in flight and this reservation together would pass the budget, the request is not
forwarded: the client gets a local error, ``Ledger.stop`` becomes ``"budget"`` and the run ends. So
the metered tokens cannot pass the budget unless a request costs more than its reservation. The
first 401 or 403 on a model path sets ``Ledger.stop`` to ``"auth"``.
"""

from __future__ import annotations

import http.client
import json
import math
import select
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
RESERVE_BYTES_PER_TOKEN = 1.5
DEFAULT_MAX_TOKENS = 32_000  # reserved for a request whose body names no max_tokens
UPSTREAM_TIMEOUT_S = 900
STOP_DRAIN_S = 2.0
CLASS_HEADER = "x-claude-code-request-class"
COMPACTION_HEADER = "x-claude-code-compaction"
AGENT_HEADER = "x-claude-code-agent-id"
AFFINITY_HEADER = "x-session-affinity"
# What CLAUDE_CODE_GATEWAY_HINT_HEADERS adds (read in the Claude Code 2.1.322 bundle).
HINT_HEADERS = frozenset({CLASS_HEADER, COMPACTION_HEADER, "x-claude-code-agent-type",
                          "x-claude-code-context-compacted", "x-claude-code-prev-tool-durations"})
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
TOKEN_CLASSES = ("uncached", "cache_write", "cache_read", "output")
USAGE_FIELDS = {"uncached": "input_tokens", "cache_write": "cache_creation_input_tokens",
                "cache_read": "cache_read_input_tokens", "output": "output_tokens"}


def history_tokens(script: Any) -> list[int]:
    """For each turn index t, the conversation the user has had by turn t, in estimated tokens: the
    x axis of the request-size chart. The sum over turns 0..t of each turn's estimate, at the rates
    ``contexto.bench.script.estimate`` uses, so the last value equals its ``tokens``."""
    from contexto.bench import script as bench_script

    out, total = [], 0
    for index, kind in enumerate(script.kinds):
        if kind == "probe":
            total += int(script.turn_bytes[index] / 3.5) + 1
        else:
            total += bench_script.turn_tokens(bench_script.load_bytes(script, index)) + bench_script.REPLY_TOKENS
        out.append(total)
    return out


def upstream_kind(url: str) -> str:
    """How the manifest names an upstream: ``anthropic``, ``local`` (this machine) or ``custom``.
    Never the host name."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "anthropic.com" or host.endswith(".anthropic.com"):
        return "anthropic"
    return "local" if host in LOOPBACK_HOSTS else "custom"


def record_tokens(record: dict[str, Any]) -> int:
    return sum(int(record.get(name) or 0) for name in TOKEN_CLASSES)


class Ledger:
    """The run's spend: the budget, what is spent, what requests in flight have reserved, and the
    ``requests.jsonl`` file every record is appended to as soon as its response has ended."""

    def __init__(self, path: Path | None, budget: int | None, spent: int = 0) -> None:
        self.path = path
        self.budget = budget
        self.spent = spent
        self.reserved = 0
        self.stop: str | None = None  # "budget" or "auth": the run must end
        self.stopped = threading.Event()
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int | None:
        """Tokens that may still be spent, after the reservations of requests in flight."""
        with self._lock:
            return None if self.budget is None else self.budget - self.spent - self.reserved

    def halt(self, reason: str) -> None:
        with self._lock:
            if self.stop is None:
                self.stop = reason
        self.stopped.set()

    def reserve(self, tokens: int) -> bool:
        """Reserve a request's worst case. False, with the run stopped, when it could pass the budget."""
        with self._lock:
            if self.stop is not None:
                return False
            if self.budget is not None and self.spent + self.reserved + tokens > self.budget:
                self.stop = "budget"
            else:
                self.reserved += tokens
                return True
        self.stopped.set()
        return False

    def settle(self, reserved: int, record: dict[str, Any] | None) -> None:
        """Release a reservation and, when the request got a response, count and write its record."""
        with self._lock:
            self.reserved -= reserved
            if record is None:
                return
            self.spent += record_tokens(record)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")


def read_spent(path: Path) -> int:
    """The tokens already recorded in a ``requests.jsonl`` file (0 when there is none)."""
    total = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn last line from a killed run
        if isinstance(record, dict):
            total += record_tokens(record)
    return total


class _UsageReader:
    """Reads ``usage`` from a Messages response as it passes: SSE ``message_start`` and
    ``message_delta`` events, or a JSON body. It keeps the four counts and nothing else."""

    def __init__(self, content_type: str | None) -> None:
        self.sse = "text/event-stream" in (content_type or "").lower()
        self.usage: dict[str, int] = {}
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        if not self.sse:
            return
        while True:
            ends = [(i, n) for i, n in ((self._buffer.find(b"\n\n"), 2), (self._buffer.find(b"\r\n\r\n"), 4)) if i >= 0]
            if not ends:
                return
            at, width = min(ends)
            event = bytes(self._buffer[:at])
            del self._buffer[:at + width]
            self._event(event)

    def finish(self) -> None:
        if self.sse:
            if self._buffer:
                self._event(bytes(self._buffer))
        elif self._buffer:
            try:
                body = json.loads(self._buffer)
            except ValueError:
                body = None
            if isinstance(body, dict):
                self._take(body.get("usage"))
        self._buffer.clear()

    def _event(self, raw: bytes) -> None:
        data = b"\n".join(line[5:].lstrip(b" ") for line in raw.splitlines() if line.startswith(b"data:"))
        if b'"usage"' not in data:
            return
        try:
            payload = json.loads(data)
        except ValueError:
            return
        if isinstance(payload, dict):
            message = payload.get("message")
            self._take(message.get("usage") if isinstance(message, dict) else None)
            self._take(payload.get("usage"))

    def _take(self, usage: Any) -> None:
        if isinstance(usage, dict):
            for field in USAGE_FIELDS.values():
                if isinstance(usage.get(field), int) and not isinstance(usage.get(field), bool):
                    self.usage[field] = usage[field]


class _ClientWatch:
    """Closes the upstream connection when the client goes away while its request is in flight, so
    the upstream stops generating a response nobody reads."""

    def __init__(self, client: socket.socket) -> None:
        self.client = client
        self.gone = False
        self._upstream: http.client.HTTPConnection | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        threading.Thread(target=self._run, name="contexto-bench-meter-watch", daemon=True).start()

    def attach(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            self._upstream = conn
            if self.gone:
                _shut(conn)

    def close(self) -> None:
        self._done.set()
        with self._lock:
            if self._upstream is not None:
                self._upstream.close()

    def _run(self) -> None:
        poller = select.poll()
        try:
            poller.register(self.client, select.POLLIN)
        except (OSError, ValueError):
            return
        while not self._done.is_set():
            try:
                if not poller.poll(50):
                    continue
                if self.client.recv(1, socket.MSG_PEEK):
                    return  # the client sent more: it is still there
            except OSError:
                pass
            if self._done.is_set():
                return
            with self._lock:
                self.gone = True
                if self._upstream is not None:
                    _shut(self._upstream)
            return


def _shut(conn: http.client.HTTPConnection) -> None:
    sock = conn.sock
    if sock is not None:
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def server_bind(self) -> None:  # HTTPServer.server_bind does a reverse lookup that can take seconds
        socket.socket.setsockopt(self.socket, socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class Meter:
    """The pass-through of one unit. ``turn`` is the script turn in flight, set by the arm."""

    def __init__(self, upstream: str, ledger: Ledger, *, arm: str, seed: int, repeat: int, model: str,
                 history: list[int], bound: int, attempt: int = 0) -> None:
        from contexto.proxy import HOP_BY_HOP, is_vendor_host  # here, so importing this module stays light

        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the upstream must be an http or https base URL")
        self.scheme, self.host = parts.scheme, parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.netloc = parts.netloc.rsplit("@", 1)[-1]
        self.base_path = parts.path.rstrip("/")
        vendor = is_vendor_host(parts.hostname)
        self.forward_affinity = not vendor
        self.forward_hints = vendor and arm == "plain"
        self.hop_by_hop = HOP_BY_HOP
        self.ledger = ledger
        self.arm, self.seed, self.repeat, self.model, self.attempt = arm, seed, repeat, model, attempt
        self.history, self.bound = history, bound
        self.turn: int | None = None
        self.records: list[dict[str, Any]] = []  # this unit's, in the order their responses ended
        self.refused = 0  # billed requests not forwarded because of the budget
        self.unreachable = 0  # requests that got no response from the upstream
        self.classes_seen = False  # a request carried Claude Code's request-class header
        self.auth_status: int | None = None
        self._count = 0
        self._lock = threading.Lock()
        self._in_flight = 0
        self._idle = threading.Condition()
        self._server: _Server | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("the meter is not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> str:
        meter = self

        class Handler(_Handler):
            owner = meter

        self._server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                         name="contexto-bench-meter", daemon=True).start()
        return self.url

    def stop(self) -> None:
        """Stop accepting requests, then wait up to ``STOP_DRAIN_S`` for those in flight to end and be
        recorded: their clients are gone by now, so their upstream connections are being closed."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            deadline = time.monotonic() + STOP_DRAIN_S
            with self._idle:
                while self._in_flight and (left := deadline - time.monotonic()) > 0:
                    self._idle.wait(left)

    def connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=UPSTREAM_TIMEOUT_S,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=UPSTREAM_TIMEOUT_S)

    def next_index(self) -> int:
        with self._lock:
            self._count += 1
            return self._count - 1

    def summary(self) -> dict[str, Any]:
        """Counts over this unit's records, for the manifest."""
        with self._lock:
            records = list(self.records)
        inputs = [r["uncached"] + r["cache_write"] + r["cache_read"] for r in records if r["main"]]
        return {"requests": len(records), "max_input": max(inputs, default=0),
                "over_bound": sum(1 for x in inputs if x > self.bound),
                "tokens": sum(record_tokens(r) for r in records)}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    owner: Meter

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        owner = self.owner
        with owner._idle:
            owner._in_flight += 1
        try:
            self._handle()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True
        finally:
            with owner._idle:
                owner._in_flight -= 1
                owner._idle.notify_all()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            body = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size <= 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(body)
                body += self.rfile.read(size)
                self.rfile.readline()
        length = max(0, int(self.headers.get("Content-Length") or 0))
        return self.rfile.read(length) if length else b""

    def _local_error(self, status: int, message: str) -> None:
        data = json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}).encode()
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _handle(self) -> None:
        owner = self.owner
        try:
            body = self._read_body()
        except ValueError:
            self._local_error(400, "contexto bench meter: malformed request framing")
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        model_path = self.command == "POST" and path in (MESSAGES_PATH, COUNT_TOKENS_PATH)
        billed = self.command == "POST" and path == MESSAGES_PATH
        received = {k.lower(): v for k, v in self.headers.items()}
        headers = {k: v for k, v in self.headers.items() if k.lower() not in owner.hop_by_hop}
        if model_path:
            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
            headers["Accept-Encoding"] = "identity"
        if not owner.forward_affinity:
            headers = {k: v for k, v in headers.items() if k.lower() != AFFINITY_HEADER}
        if not owner.forward_hints:
            headers = {k: v for k, v in headers.items() if k.lower() not in HINT_HEADERS}
        reserved, record = 0, None
        if billed:
            shape = _shape(body, received.get("content-encoding"))
            reserved = math.ceil(shape["bytes"] / RESERVE_BYTES_PER_TOKEN) + shape["max_tokens"]
            if not owner.ledger.reserve(reserved):
                with owner._lock:
                    owner.refused += 1
                self._local_error(400, "contexto bench: the run was stopped (token budget or credential)")
                return
            request_class = received.get(CLASS_HEADER)
            if request_class is not None:
                owner.classes_seen = True
                main = request_class == "main"
            else:  # no class header: a main request has a tool list and no subagent id
                main = owner.arm == "full" or (shape["tools"] and AGENT_HEADER not in received)
            turn = owner.turn
            record = {"arm": owner.arm, "seed": owner.seed, "repeat": owner.repeat, "model": owner.model,
                      "n": owner.next_index(), "turn": turn, "main": bool(main),
                      "history_tokens": owner.history[turn] if turn is not None and turn < len(owner.history) else 0,
                      "uncached": 0, "cache_write": 0, "cache_read": 0, "output": 0, "status": 0,
                      "compaction": COMPACTION_HEADER in received or request_class == "compaction",
                      "basis": "measured", "attempt": owner.attempt}
        reached = False
        try:
            reached = self._relay(body, headers, record)
        finally:
            if billed:
                if reached and record is not None:
                    with owner._lock:
                        owner.records.append(record)
                else:
                    with owner._lock:
                        owner.unreachable += 1
                owner.ledger.settle(reserved, record if reached else None)

    def _relay(self, body: bytes, headers: dict[str, str], record: dict[str, Any] | None) -> bool:
        """Forward one request and relay its response. True when the upstream answered."""
        owner = self.owner
        watch = _ClientWatch(self.connection)
        conn = owner.connection()
        try:
            try:
                conn.connect()
                watch.attach(conn)
                conn.putrequest(self.command, owner.base_path + self.path, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", owner.netloc)
                for name, value in headers.items():
                    conn.putheader(name, value)
                if body or self.command in ("POST", "PUT", "PATCH"):
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body or None)
                response = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                if not watch.gone:
                    self._local_error(502, f"contexto bench meter: upstream unreachable ({type(exc).__name__})")
                self.close_connection = True
                return False
            model_path = self.command == "POST" and self.path.split("?", 1)[0].rstrip("/") in (
                MESSAGES_PATH, COUNT_TOKENS_PATH)
            if model_path and response.status in (401, 403):
                owner.auth_status = response.status
                owner.ledger.halt("auth")
            reader = _UsageReader(response.getheader("Content-Type")) if record is not None else None
            if record is not None:
                record["status"] = response.status
            length = response.getheader("Content-Length")
            chunked = length is None and self.command != "HEAD"
            try:
                self.send_response_only(response.status, response.reason)
                for name, value in response.getheaders():
                    if name.lower() not in owner.hop_by_hop:
                        self.send_header(name, value)
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                elif length is not None:
                    self.send_header("Content-Length", length)
                self.end_headers()
                while chunk := response.read1(65536):
                    if reader is not None:
                        reader.feed(chunk)
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk) if chunked else chunk)
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except (OSError, http.client.HTTPException):
                # The upstream ended early or the client left: the client gets no terminator, and the
                # record keeps whatever usage had arrived.
                self.close_connection = True
            if reader is not None and record is not None:
                reader.finish()
                for name, field in USAGE_FIELDS.items():
                    record[name] = int(reader.usage.get(field) or 0)
            return True
        finally:
            watch.close()
            conn.close()


def _shape(body: bytes, encoding: str | None) -> dict[str, Any]:
    """What the meter needs of a Messages request body, read in memory: its decoded size, its
    ``max_tokens`` and whether it has a tool list."""
    encoding = (encoding or "").strip().lower()
    if encoding not in ("", "identity"):
        from contexto.proxy import EncodedBodyError, decode_request_body

        try:
            body = decode_request_body(body, encoding)
        except EncodedBodyError:
            return {"bytes": len(body) * 20, "max_tokens": DEFAULT_MAX_TOKENS, "tools": False}
    try:
        request = json.loads(body) if body else {}
    except (ValueError, RecursionError):
        request = {}
    if not isinstance(request, dict):
        request = {}
    max_tokens = request.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        max_tokens = DEFAULT_MAX_TOKENS
    return {"bytes": len(body), "max_tokens": max_tokens, "tools": bool(request.get("tools"))}
