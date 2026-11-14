"""Metering for the live benchmark: one ``requests.jsonl`` record per billed request, for every arm.

``Meter`` is a pass-through on 127.0.0.1 between an arm and the real upstream. All three arms send
their Messages requests through one: the ``contexto`` arm as Contexto's ``--upstream``, the ``plain``
arm as Claude Code's base URL, the ``full`` arm as the base URL of its own requests. So the three
arms' counts come from one piece of code reading the same field of the same kind of response.

What it forwards. Every request goes to the real upstream with its method, path, query, headers and
body as received, and the response comes back with its status, headers and body as received, each
chunk written to the client as soon as it is read, so a stream keeps its timing; only the bytes that
end a response wait until its record is written, which takes microseconds. When the client
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

Request headers are forwarded as received, a repeated header as often as it came; a response with
no body (a 204 or 304, or the answer to ``HEAD``) is relayed with none.

To a vendor's host Claude Code also adds ``x-cc-context-compacted`` and ``x-cc-compaction-request``
on its own (it gates them on the base URL being the vendor's, read in the 2.1.322 bundle). Through
the meter the base URL is 127.0.0.1, so neither Claude Code arm sends them; the manifest's
``arm_differences`` says so.

What it records, for each ``POST /v1/messages`` that got a response: the arm, seed, repeat and model
of the unit, the request's order in the unit, the script turn in flight (the arm sets ``Meter.turn``
before it sends a turn), whether it is a main request, the input tokens by class and the output
tokens from the response's ``usage``, the HTTP status, and whether it carried out a compaction. It
reads the request body in memory for ``max_tokens`` and the presence of a tool list, and keeps
nothing of it. ``count_tokens`` and every other path are forwarded and not recorded.

Usage that cannot be read. When a 2xx response to a billed request ends without a usage the meter
can read in full (an encoded body, a stream cut short before its final ``message_delta`` usage, a
body that is not JSON), the vendor may still have billed up to the request's ``max_tokens``. The
record then carries ``usage_unreadable: true`` and ``reserved_tokens``, the request's reservation,
and the request counts as that reservation (or what was read, if more) everywhere: in the guard, in
the unit's tokens and when a resumed run reads ``requests.jsonl`` (``record_tokens``). The guard
never counts less than a request could have cost.

``Ledger`` is the spend guard of the whole run. Before a billed request is forwarded the meter
reserves its worst case: the request's bytes at 1.5 bytes per token, a rate that overstates every
content this benchmark sends, plus the request's ``max_tokens``. If what is spent, what is reserved
by requests in flight and this reservation together would pass the budget, the request is not
forwarded: the client gets a local error, ``Ledger.stop`` becomes ``"budget"`` and the run ends. So
the metered tokens cannot pass the budget unless a request costs more than its reservation. The
first 401 or 403 on a model path sets ``Ledger.stop`` to ``"auth"``; after that nothing more is
forwarded, ``count_tokens`` included.

Runaway caps, per unit. A model that keeps calling tools can spend a unit's share of the budget in
one turn. Each meter can be given ``turn_request_cap`` (the most billed requests in one script turn
that the upstream answered with a 2xx, counting those still in flight) and ``token_cap`` (the most
tokens the unit may count, with requests in flight reserved as above). The request that would pass
either is refused locally, ``Meter.unit_stop`` becomes ``"runaway"`` and the arm stops that unit; the
run goes on with the next. A response that failed (a 429, 529 or other status that is not 2xx, or no
response) is not counted toward the per-turn cap, because Claude Code retries it and it is the
upstream's doing, not the model's; such responses count toward ``turn_failure_cap`` instead, and the
request after that many in one turn stops the unit as ``"api_error"``: the upstream kept failing.
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
NO_BODY_STATUSES = (204, 304)
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
    try:  # as the socket layer resolves it, and as contexto.proxy.is_vendor_host reads it
        host = host.encode("idna").decode("ascii").lower().rstrip(".") if host else host
    except UnicodeError:
        return "custom"
    if host == "anthropic.com" or host.endswith(".anthropic.com"):
        return "anthropic"
    return "local" if host in LOOPBACK_HOSTS else "custom"


def record_tokens(record: dict[str, Any]) -> int:
    """The tokens a record counts: its four classes, or for a response whose usage could not be read,
    the request's reservation when that is more."""
    counted = sum(int(record.get(name) or 0) for name in TOKEN_CLASSES)
    if record.get("usage_unreadable"):
        reserved = record.get("reserved_tokens")
        if isinstance(reserved, int) and not isinstance(reserved, bool):
            counted = max(counted, reserved)
    return counted


def record_input(record: dict[str, Any]) -> int:
    return sum(int(record.get(name) or 0) for name in ("uncached", "cache_write", "cache_read"))


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


def read_recorded(path: Path) -> dict[tuple[str, int, int], dict[int, dict[str, int]]]:
    """What ``requests.jsonl`` holds per unit (arm, seed, repeat) and attempt: requests and tokens."""
    out: dict[tuple[str, int, int], dict[int, dict[str, int]]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        try:
            record = json.loads(line)
            key = (str(record["arm"]), int(record["seed"]), int(record["repeat"]))
            attempt = int(record.get("attempt") or 0)
        except (ValueError, KeyError, TypeError):
            continue  # a torn last line from a killed run
        seen = out.setdefault(key, {}).setdefault(attempt, {"requests": 0, "tokens": 0})
        seen["requests"] += 1
        seen["tokens"] += record_tokens(record)
    return out


class _UsageReader:
    """Reads ``usage`` from a Messages response as it passes: SSE ``message_start`` and
    ``message_delta`` events, or a JSON body. It keeps the four counts and nothing else."""

    def __init__(self, content_type: str | None) -> None:
        self.sse = "text/event-stream" in (content_type or "").lower()
        self.usage: dict[str, int] = {}
        self.started = False  # SSE: message_start carried a usage
        self.final = False  # the final usage arrived: a message_delta usage, or a whole JSON body's
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
            if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                self._take(body["usage"])
                self.started = self.final = all(
                    isinstance(body["usage"].get(f), int) for f in ("input_tokens", "output_tokens"))
        self._buffer.clear()

    @property
    def complete(self) -> bool:
        """Whether the usage read is the response's whole usage."""
        return self.started and self.final

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
            start = message.get("usage") if isinstance(message, dict) else None
            if isinstance(start, dict):
                self._take(start)
                self.started = self.started or isinstance(start.get("input_tokens"), int)
            if isinstance(payload.get("usage"), dict):
                self._take(payload["usage"])
                if payload.get("type") == "message_delta" and isinstance(payload["usage"].get("output_tokens"), int):
                    self.final = True

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

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # socketserver prints a traceback, with file paths, to stderr: nothing of a run is printed that way

    def server_bind(self) -> None:  # HTTPServer.server_bind does a reverse lookup that can take seconds
        socket.socket.setsockopt(self.socket, socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class Meter:
    """The pass-through of one unit. ``turn`` is the script turn in flight, set by the arm."""

    def __init__(self, upstream: str, ledger: Ledger, *, arm: str, seed: int, repeat: int, model: str,
                 history: list[int], bound: int, attempt: int = 0, turn_request_cap: int | None = None,
                 token_cap: int | None = None, turn_failure_cap: int | None = None) -> None:
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
        self.turn_request_cap, self.token_cap, self.turn_failure_cap = turn_request_cap, token_cap, turn_failure_cap
        self.unit_stop: str | None = None  # "runaway" or "api_error": this unit must end (the run goes on)
        self.unit_stop_reason: str | None = None
        self.stopped = threading.Event()
        self.usage_unreadable = 0  # records whose usage could not be read, counted at their reservation
        self._arrived: dict[int | None, int] = {}  # billed requests that reached the meter, per turn
        self._counted: dict[int | None, int] = {}  # per turn: admitted, less those that failed (the per-turn cap)
        self._failed: dict[int | None, int] = {}  # per turn: admitted requests whose response failed
        self._unit_spent = 0
        self._unit_reserved = 0
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

    def turn_requests(self, turn: int | None) -> int:
        """Billed requests that reached the meter while ``turn`` was in flight, refused ones included."""
        with self._lock:
            return self._arrived.get(turn, 0)

    def halt(self, stop: str, reason: str) -> None:
        """End this unit (not the run): every later billed request is refused."""
        with self._lock:
            if self.unit_stop is None:
                self.unit_stop, self.unit_stop_reason = stop, reason
        self.stopped.set()

    def admit(self, turn: int | None, reserved: int) -> bool:
        """Count a billed request and check the unit's caps; reserve ``reserved`` against the token cap.
        False, with the unit stopped, when the request would pass a cap."""
        with self._lock:
            self._arrived[turn] = self._arrived.get(turn, 0) + 1
            if self.unit_stop is not None:
                return False
            count, failed = self._counted.get(turn, 0) + 1, self._failed.get(turn, 0)
            where = f"turn {turn + 1}" if turn is not None else "one turn"
            stop, reason = "runaway", None
            if self.turn_failure_cap is not None and failed >= self.turn_failure_cap:
                stop, reason = "api_error", (
                    f"{where} had {failed:,} failed responses (a 429, 529 or other error status, or none; the per-turn "
                    f"cap on failed responses is {self.turn_failure_cap:,}): the upstream kept failing")
            elif self.turn_request_cap is not None and count > self.turn_request_cap:
                reason = (f"{where} asked for more than {self.turn_request_cap:,} billed requests that the upstream "
                          f"answered (the per-turn cap; {failed:,} more failed and are not counted toward it): the "
                          "model kept calling tools")
            elif self.token_cap is not None and self._unit_spent + self._unit_reserved + reserved > self.token_cap:
                reason = (f"the unit would have passed its cap of {self.token_cap:,} tokens "
                          f"({self._unit_spent:,} counted when the next request was refused)")
            if reason is None:
                self._unit_reserved += reserved
                self._counted[turn] = count
                return True
            self.unit_stop, self.unit_stop_reason = stop, reason
        self.stopped.set()
        return False

    def settle(self, reserved: int, record: dict[str, Any] | None, turn: int | None = None,
               answered: bool = True) -> None:
        """Release an admitted request's reservation and count its record; one that was not
        ``answered`` (no 2xx) leaves the per-turn count and counts as a failed response instead."""
        with self._lock:
            self._unit_reserved -= reserved
            if record is not None:
                self._unit_spent += record_tokens(record)
            if not answered:
                self._counted[turn] = self._counted.get(turn, 0) - 1
                self._failed[turn] = self._failed.get(turn, 0) + 1

    def summary(self) -> dict[str, Any]:
        """Counts over this unit's records, for the manifest. A compaction request carries the whole
        context, so ``max_input`` and ``over_bound`` count it with the main requests; it is also
        counted on its own."""
        with self._lock:
            records = list(self.records)
        sized = [record_input(r) for r in records if r["main"] or r["compaction"]]
        compactions = [r for r in records if r["compaction"]]
        return {"requests": len(records), "max_input": max(sized, default=0),
                "over_bound": sum(1 for x in sized if x > self.bound),
                "tokens": sum(record_tokens(r) for r in records),
                "compaction_requests": len(compactions),
                "compaction_tokens": sum(record_tokens(r) for r in compactions),
                "compaction_max_input": max((record_input(r) for r in compactions), default=0),
                "usage_unreadable": sum(1 for r in records if r.get("usage_unreadable"))}


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
        if owner.ledger.stop == "auth":  # the credential was refused: nothing more goes out with it
            self._local_error(400, "contexto bench: the run was stopped (the credential was refused)")
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        model_path = self.command == "POST" and path in (MESSAGES_PATH, COUNT_TOKENS_PATH)
        billed = self.command == "POST" and path == MESSAGES_PATH
        received = {k.lower(): v for k, v in self.headers.items()}
        # A list, not a dict: a header sent twice is forwarded twice.
        headers = [(k, v) for k, v in self.headers.items() if k.lower() not in owner.hop_by_hop]
        drop = set()
        if model_path:
            drop.add("accept-encoding")
        if not owner.forward_affinity:
            drop.add(AFFINITY_HEADER)
        if not owner.forward_hints:
            drop |= HINT_HEADERS
        headers = [(k, v) for k, v in headers if k.lower() not in drop]
        if model_path:
            headers.append(("Accept-Encoding", "identity"))
        reserved, record = 0, None
        turn = owner.turn
        if billed:
            shape = _shape(body, received.get("content-encoding"))
            reserved = math.ceil(shape["bytes"] / RESERVE_BYTES_PER_TOKEN) + shape["max_tokens"]
            if not owner.admit(turn, reserved):
                with owner._lock:
                    owner.refused += 1
                self._local_error(400, "contexto bench: this unit was stopped (a runaway cap)")
                return
            if not owner.ledger.reserve(reserved):
                owner.settle(reserved, None, turn, answered=False)
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
            record = {"arm": owner.arm, "seed": owner.seed, "repeat": owner.repeat, "model": owner.model,
                      "n": owner.next_index(), "turn": turn, "main": bool(main),
                      "history_tokens": owner.history[turn] if turn is not None and turn < len(owner.history) else 0,
                      "uncached": 0, "cache_write": 0, "cache_read": 0, "output": 0, "status": 0,
                      "compaction": COMPACTION_HEADER in received or request_class == "compaction",
                      "basis": "measured", "attempt": owner.attempt}
        reached, tail = False, b""
        try:
            reached, tail = self._relay(body, headers, record)
        finally:
            if billed:
                if reached and record is not None:
                    if record.pop("_unreadable", False):
                        record["usage_unreadable"], record["reserved_tokens"] = True, reserved
                    with owner._lock:
                        owner.records.append(record)
                        owner.usage_unreadable += int(bool(record.get("usage_unreadable")))
                else:
                    with owner._lock:
                        owner.unreachable += 1
                answered = reached and record is not None and 200 <= int(record.get("status") or 0) < 300
                owner.settle(reserved, record if reached else None, turn, answered)
                owner.ledger.settle(reserved, record if reached else None)
        # The client sees the response complete only now, after its record is counted and written:
        # Claude Code sends its next request at once, and the guard must already know this one's cost.
        if tail:
            try:
                self.wfile.write(tail)
                self.wfile.flush()
            except OSError:
                self.close_connection = True

    def _relay(self, body: bytes, headers: list[tuple[str, str]],
               record: dict[str, Any] | None) -> tuple[bool, bytes]:
        """Forward one request and relay its response, except its last bytes: the chunk terminator, or
        the last chunk of a response with a Content-Length. Returns whether the upstream answered, and
        those bytes for the caller to write (empty when the relay failed)."""
        owner = self.owner
        watch = _ClientWatch(self.connection)
        conn = owner.connection()
        try:
            try:
                conn.connect()
                watch.attach(conn)
                conn.putrequest(self.command, owner.base_path + self.path, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", owner.netloc)
                for name, value in headers:
                    conn.putheader(name, value)
                if body or self.command in ("POST", "PUT", "PATCH"):
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body or None)
                response = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                if not watch.gone:
                    self._local_error(502, f"contexto bench meter: upstream unreachable ({type(exc).__name__})")
                self.close_connection = True
                return False, b""
            model_path = self.command == "POST" and self.path.split("?", 1)[0].rstrip("/") in (
                MESSAGES_PATH, COUNT_TOKENS_PATH)
            if model_path and response.status in (401, 403):
                owner.auth_status = response.status
                owner.ledger.halt("auth")
            reader = _UsageReader(response.getheader("Content-Type")) if record is not None else None
            if record is not None:
                record["status"] = response.status
            length = response.getheader("Content-Length")
            no_body = (self.command == "HEAD" or response.status in NO_BODY_STATUSES
                       or 100 <= response.status < 200)
            chunked = length is None and not no_body
            encoded = (response.getheader("Content-Encoding") or "identity").strip().lower() != "identity"
            tail, held, whole = b"", b"", False
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
                while not no_body and (chunk := response.read1(65536)):
                    if reader is not None:
                        reader.feed(chunk)
                    if chunked:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    else:
                        self.wfile.write(held)
                        held = chunk
                    self.wfile.flush()
                tail = b"0\r\n\r\n" if chunked else held
                whole = True
            except (OSError, http.client.HTTPException):
                # The upstream ended early or the client left: the client gets no terminator, and the
                # record keeps whatever usage had arrived.
                self.close_connection = True
            if reader is not None and record is not None:
                if not encoded:
                    reader.finish()
                for name, field in USAGE_FIELDS.items():
                    record[name] = int(reader.usage.get(field) or 0)
                if 200 <= response.status < 300 and (encoded or not whole or not reader.complete):
                    record["_unreadable"] = True  # counted at its reservation (see the module's docstring)
            return True, tail
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
