"""Stdlib-only RFC 6455 WebSocket upgrade and frame relay for OpenAI Responses WebSocket transport.

Kept separate from ``contexto.proxy`` so the HTTP proxy stays compact and the framing primitives are
directly unit-tested. Only Responses endpoints (``/responses``, ``/v1/responses``) are accepted, and
only when ``ContextoProxy.allow_websocket`` is True (opt-in; HTTP remains the default).

How the relay keeps a WebSocket conversation bounded:

- Client ``response.create`` text frames carrying an ``input`` list (without ``previous_response_id``)
  are rewritten through the proxy's ``transform`` under ``transform_lock``, with the same fail-open
  rules as HTTP POST (rule 14: on a transform or serialization failure the client's original frame is
  forwarded unchanged).
- Server frames from the first tool call (``response.output_item.added`` whose item type ends in
  ``_call``) are held until the turn's terminal event (``response.completed``,
  ``response.incomplete``, ``response.failed``, or an error / close), so ``observe_usage`` publishes
  the real-usage recall room before the CLI sees any tool call (matching ``StreamHold`` on SSE).
- ``Sec-WebSocket-Extensions`` is stripped from the upstream upgrade request so permessage-deflate
  is not negotiated and frames remain readable without compression extensions.
- A non-101 upstream response (for example 401, 404, 426, or 501) is forwarded back to the client as
  an HTTP response so Codex's built-in HTTP fallback works seamlessly.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

from contexto.canonical import MAX_JSON_DEPTH, JSONTooDeepError, loads_bounded
from contexto.hints import without_hint_headers

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_WS_MESSAGE_BYTES = 64 * 1024 * 1024  # matches proxy.MAX_DECODED_BODY_BYTES
MAX_HANDSHAKE_BYTES = 64 * 1024
MAX_FALLBACK_BODY_BYTES = 16 * 1024 * 1024
WS_JOIN_TIMEOUT_S = 2.0

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA
DATA_OPCODES = frozenset({OP_TEXT, OP_BINARY})
CONTROL_OPCODES = frozenset({OP_CLOSE, OP_PING, OP_PONG})

# Headers removed before forwarding the upgrade request upstream (hop-by-hop + WS handshake headers
# that the proxy regenerates or intentionally withholds, such as Sec-WebSocket-Extensions).
WS_SKIP_HEADERS = frozenset({
    "connection", "upgrade", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "host", "content-length",
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-accept", "accept-encoding",
})


class WSProtocolError(Exception):
    """RFC 6455 framing or handshake error."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def accept_key(client_key: str) -> str:
    """RFC 6455 §4.2.2 Sec-WebSocket-Accept value for ``client_key``."""
    digest = hashlib.sha1(client_key.strip().encode("ascii") + WS_GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def valid_client_key(key: str | None) -> bool:
    """True when ``Sec-WebSocket-Key`` is a valid base64-encoded 16-byte nonce."""
    if not key or not key.isascii():
        return False
    try:
        raw = base64.b64decode(key.strip().encode("ascii"), validate=True)
    except ValueError:
        return False
    return len(raw) == 16


def apply_mask(data: bytes, mask: bytes) -> bytes:
    """XOR ``data`` with the 4-byte RFC 6455 masking key."""
    if not data:
        return b""
    n = len(data)
    repeated = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes = b"", *, fin: bool = True, mask: bytes | None = None) -> bytes:
    """Encode one RFC 6455 WebSocket frame. Pass a 4-byte ``mask`` for client-to-server frames."""
    first = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)
    mask_bit = 0x80 if mask is not None else 0x00
    if length < 126:
        header = struct.pack("!BB", first, mask_bit | length)
    elif length < 65536:
        header = struct.pack("!BBH", first, mask_bit | 126, length)
    else:
        header = struct.pack("!BBQ", first, mask_bit | 127, length)
    if mask is not None:
        return header + mask + apply_mask(payload, mask)
    return header + payload


def close_payload(code: int, reason: str = "") -> bytes:
    """RFC 6455 §5.5.1 close frame payload (2-byte status code + UTF-8 reason, capped to 125 bytes)."""
    body = reason.encode("utf-8", "replace")[:123]
    return struct.pack("!H", code) + body


def parse_close_code(payload: bytes) -> int | None:
    """The 2-byte status code of a close frame payload, or None when absent."""
    return struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None


class _ByteReader:
    """Buffered byte reader over a recv-like callable (``socket.recv`` or ``BufferedReader.read1``)."""

    def __init__(self, recv: Callable[[int], bytes], initial: bytes = b"") -> None:
        self._recv = recv
        self._buf = bytearray(initial)

    def read_exact(self, n: int) -> bytes:
        """Read exactly ``n`` bytes, or return ``b""`` on clean EOF before any byte, or raise EOFError."""
        if n == 0:
            return b""
        while len(self._buf) < n:
            chunk = self._recv(max(4096, n - len(self._buf)))
            if not chunk:
                if not self._buf:
                    return b""
                raise EOFError("unexpected end of WebSocket stream")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def read_until(self, marker: bytes, limit: int) -> bytes:
        """Read up to and including ``marker`` (at most ``limit`` bytes)."""
        while True:
            idx = self._buf.find(marker)
            if idx != -1:
                end = idx + len(marker)
                out = bytes(self._buf[:end])
                del self._buf[:end]
                return out
            if len(self._buf) >= limit:
                raise WSProtocolError(1002, "HTTP handshake header too large")
            chunk = self._recv(min(4096, limit - len(self._buf)))
            if not chunk:
                raise EOFError("unexpected EOF during HTTP handshake")
            self._buf += chunk

    def read_some(self, n: int = 65536) -> bytes:
        if self._buf:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out
        return self._recv(n)


def read_frame(reader: _ByteReader, *, expect_masked: bool | None = None,
               max_bytes: int = MAX_WS_MESSAGE_BYTES) -> tuple[bool, int, bytes] | None:
    """Read one RFC 6455 frame. Returns ``(fin, opcode, payload)``, or None on clean EOF."""
    head = reader.read_exact(2)
    if not head:
        return None
    b0, b1 = head[0], head[1]
    fin = bool(b0 & 0x80)
    if b0 & 0x70:
        raise WSProtocolError(1002, "non-zero RSV bits without negotiated extension")
    opcode = b0 & 0x0F
    if opcode not in (OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG):
        raise WSProtocolError(1002, f"unsupported opcode 0x{opcode:x}")
    masked = bool(b1 & 0x80)
    if expect_masked is not None and masked != expect_masked:
        raise WSProtocolError(1002, "unexpected frame masking")
    length = b1 & 0x7F
    if opcode in CONTROL_OPCODES and (not fin or length > 125):
        raise WSProtocolError(1002, "invalid control frame")
    if length == 126:
        ext = reader.read_exact(2)
        if not ext:
            raise EOFError("truncated 16-bit frame length")
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = reader.read_exact(8)
        if not ext:
            raise EOFError("truncated 64-bit frame length")
        length = struct.unpack("!Q", ext)[0]
        if length & (1 << 63):
            raise WSProtocolError(1002, "invalid 64-bit frame length")
    if length > max_bytes:
        raise WSProtocolError(1009, "WebSocket frame exceeds size limit")
    mask = b""
    if masked:
        mask = reader.read_exact(4)
        if not mask:
            raise EOFError("truncated masking key")
    payload = reader.read_exact(length) if length else b""
    if length and not payload:
        raise EOFError("truncated frame payload")
    if masked and payload:
        payload = apply_mask(payload, mask)
    return fin, opcode, payload


def read_message(reader: _ByteReader, *, expect_masked: bool | None = None,
                 on_control: Callable[[int, bytes], None] | None = None,
                 max_bytes: int = MAX_WS_MESSAGE_BYTES) -> tuple[int, bytes] | None:
    """Read one complete (possibly fragmented) WebSocket data message, dispatching interleaved control
    frames through ``on_control``. Returns ``(opcode, payload)`` for data messages, or None on EOF
    or after a close frame."""
    active_op: int | None = None
    chunks = bytearray()
    while True:
        frame = read_frame(reader, expect_masked=expect_masked, max_bytes=max_bytes)
        if frame is None:
            return None
        fin, opcode, payload = frame
        if opcode in CONTROL_OPCODES:
            if on_control is not None:
                on_control(opcode, payload)
            if opcode == OP_CLOSE:
                return None
            continue
        if opcode == OP_CONT:
            if active_op is None:
                raise WSProtocolError(1002, "unexpected continuation frame")
            if len(chunks) + len(payload) > max_bytes:
                raise WSProtocolError(1009, "WebSocket message exceeds size limit")
            chunks += payload
            if fin:
                return active_op, bytes(chunks)
            continue
        # OP_TEXT or OP_BINARY
        if active_op is not None:
            raise WSProtocolError(1002, "new data frame while fragmented message is active")
        if fin:
            return opcode, payload
        active_op = opcode
        chunks = bytearray(payload)


def _parse_http_response_head(raw: bytes) -> tuple[int, str, list[tuple[str, str]]]:
    """Parse an HTTP/1.x status line and headers from raw bytes ending in ``\\r\\n\\r\\n``."""
    lines = raw.decode("iso-8859-1").split("\r\n")
    status_parts = lines[0].split(" ", 2)
    if len(status_parts) < 2 or not status_parts[0].startswith("HTTP/"):
        raise WSProtocolError(1002, "malformed upstream HTTP status line")
    status = int(status_parts[1])
    reason = status_parts[2] if len(status_parts) > 2 else ""
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers.append((k.strip(), v.strip()))
    return status, reason, headers


def _read_http_fallback_body(reader: _ByteReader, headers: list[tuple[str, str]],
                             limit: int = MAX_FALLBACK_BODY_BYTES) -> bytes:
    """Read a non-101 HTTP response body so it can be forwarded cleanly to the client."""
    lower = {k.lower(): v for k, v in headers}
    if "chunked" in lower.get("transfer-encoding", "").lower():
        out = bytearray()
        while True:
            line = reader.read_until(b"\r\n", 1024)
            size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                while reader.read_until(b"\r\n", 1024) != b"\r\n":
                    pass
                return bytes(out)
            if len(out) + size > limit:
                return bytes(out)
            out += reader.read_exact(size)
            reader.read_exact(2)  # trailing CRLF
    if "content-length" in lower:
        try:
            length = max(0, min(int(lower["content-length"]), limit))
        except ValueError:
            length = 0
        return reader.read_exact(length) if length else b""
    out = bytearray()
    while len(out) < limit:
        try:
            chunk = reader.read_some(min(65536, limit - len(out)))
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return bytes(out)


def _open_upstream_socket(owner: Any) -> socket.socket:
    from contexto.proxy import UPSTREAM_TIMEOUT_S

    sock = socket.create_connection((owner.upstream_host, owner.upstream_port), timeout=UPSTREAM_TIMEOUT_S)
    if owner.scheme == "https":
        try:
            ctx = ssl.create_default_context()
            return ctx.wrap_socket(sock, server_hostname=owner.upstream_host)
        except BaseException:
            sock.close()
            raise
    return sock


def _shut_socket(sock: socket.socket) -> None:
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass


def _ws_header_tokens(value: str | None) -> set[str]:
    return {part.strip().lower() for part in (value or "").split(",") if part.strip()}


def handle_websocket(handler: Any, owner: Any, path_only: str) -> None:
    """Upgrade a local client WebSocket request on a Responses endpoint and relay frames through the
    proxy's transform and usage hooks."""
    from contexto.proxy import (
        AFFINITY_HEADER,
        HOP_BY_HOP,
        RESPONSE_END_EVENTS,
        UsageReader,
        _ms,
        _scalar_summary,
        is_client_call,
        same_wire,
        serialize_request,
    )

    started = time.monotonic()
    handler.close_connection = True
    client_key = handler.headers.get("Sec-WebSocket-Key")
    ws_version = (handler.headers.get("Sec-WebSocket-Version") or "").strip()
    if (handler.command != "GET" or "upgrade" not in _ws_header_tokens(handler.headers.get("Connection"))
            or ws_version != "13" or not valid_client_key(client_key)):
        handler._send_local_error(400, "contexto proxy: invalid WebSocket upgrade request", "invalid_request_error")
        return
    assert client_key is not None

    req_headers: dict[str, str] = {k: v for k, v in handler.headers.items() if k.lower() not in WS_SKIP_HEADERS}
    if owner.affinity_header and not any(k.lower() == AFFINITY_HEADER.lower() for k in req_headers):
        req_headers[AFFINITY_HEADER] = owner.affinity_header
    forward_headers = without_hint_headers(req_headers) if owner.strip_hint_headers else req_headers

    try:
        up_sock = _open_upstream_socket(owner)
    except OSError as exc:
        owner.log({"event": "websocket_session", "path": path_only, "status": 502, "turns": 0,
                   "duration_ms": _ms(started), "error": f"connect: {type(exc).__name__}"})
        handler._send_local_error(502, f"contexto proxy: upstream unreachable ({type(exc).__name__})")
        return

    upstream_key = base64.b64encode(os.urandom(16)).decode("ascii")
    try:
        req_lines = [
            f"GET {owner.base_path}{handler.path} HTTP/1.1",
            f"Host: {owner.upstream_netloc}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {upstream_key}",
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in forward_headers.items():
            req_lines.append(f"{name}: {value}")
        req_lines.extend(["", ""])
        up_sock.sendall("\r\n".join(req_lines).encode("iso-8859-1"))
        up_reader = _ByteReader(up_sock.recv)
        raw_head = up_reader.read_until(b"\r\n\r\n", MAX_HANDSHAKE_BYTES)
        status, reason, up_headers = _parse_http_response_head(raw_head)
    except (OSError, EOFError, ValueError, WSProtocolError) as exc:
        up_sock.close()
        owner.log({"event": "websocket_session", "path": path_only, "status": 502, "turns": 0,
                   "duration_ms": _ms(started), "error": f"handshake: {type(exc).__name__}"})
        handler._send_local_error(502, f"contexto proxy: upstream WebSocket handshake failed ({type(exc).__name__})")
        return

    if status != 101:
        body = b""
        try:
            body = _read_http_fallback_body(up_reader, up_headers)
        except (OSError, EOFError, ValueError, WSProtocolError):
            pass
        finally:
            up_sock.close()
        handler._response_started = True
        handler.send_response_only(status, reason)
        for name, value in up_headers:
            if name.lower() not in HOP_BY_HOP:
                handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if body:
            handler.wfile.write(body)
        handler.wfile.flush()
        owner.log({"event": "websocket_session", "path": path_only, "status": status, "turns": 0,
                   "duration_ms": _ms(started)})
        return

    up_lower = {k.lower(): v for k, v in up_headers}
    if up_lower.get("sec-websocket-accept", "").strip() != accept_key(upstream_key):
        up_sock.close()
        owner.log({"event": "websocket_session", "path": path_only, "status": 502, "turns": 0,
                   "duration_ms": _ms(started), "error": "invalid_accept"})
        handler._send_local_error(502, "contexto proxy: upstream sent invalid Sec-WebSocket-Accept")
        return

    # Complete the 101 handshake with the local client.
    handler._response_started = True
    handler.send_response_only(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(client_key))
    if "sec-websocket-protocol" in up_lower:
        handler.send_header("Sec-WebSocket-Protocol", up_lower["sec-websocket-protocol"])
    handler.end_headers()
    handler.wfile.flush()

    client_sock = handler.connection
    client_reader = _ByteReader(handler.rfile.read1)
    client_write_lock = threading.Lock()
    upstream_write_lock = threading.Lock()
    state_lock = threading.Lock()
    pending: collections.deque[tuple[dict[str, Any] | None, dict[str, Any]]] = collections.deque()
    closed = threading.Event()
    session_info: dict[str, Any] = {"turns": 0, "close_code": None, "error": None}

    def send_client(frame: bytes) -> None:
        with client_write_lock:
            client_sock.sendall(frame)

    def send_upstream(frame: bytes) -> None:
        with upstream_write_lock:
            up_sock.sendall(frame)

    def stop_both() -> None:
        if not closed.is_set():
            closed.set()
            _shut_socket(up_sock)
            _shut_socket(client_sock)

    def log_turn(meta: dict[str, Any], reader: UsageReader, usage_events: list[dict[str, Any]]) -> None:
        turn_entry: dict[str, Any] = {
            "event": "websocket_turn",
            "path": path_only,
            "model": meta["model"],
            "original_request_bytes": meta["original_request_bytes"],
            "request_bytes": meta["request_bytes"],
            "duration_ms": _ms(meta["started"]),
            "usage": reader.usage or None,
            "over_bound": bool(meta.get("over_bound")) or any(
                bool(e.get("over_bound")) for e in usage_events if isinstance(e, dict)),
        }
        if meta.get("epoch") is not None:
            turn_entry["epoch"] = meta["epoch"]
        if meta.get("transform_events"):
            turn_entry["transform_events"] = meta["transform_events"]
        if meta.get("transform_error"):
            turn_entry["transform_error"] = meta["transform_error"]
        if meta.get("transform_skipped"):
            turn_entry["transform_skipped"] = meta["transform_skipped"]
        if usage_events:
            turn_entry["usage_events"] = [_scalar_summary(e) for e in usage_events]
        end = reader.end_fields()
        if "end_event" in end:
            turn_entry["end_event"] = end["end_event"]
        if "response_status" in end:
            turn_entry["response_status"] = end["response_status"]
        if "incomplete_reason" in end:
            turn_entry["incomplete_reason"] = end["incomplete_reason"]
        if "error_code" in end:
            turn_entry["error_code"] = end["error_code"]
        owner.log(turn_entry)

    def transform_client_frame(payload: bytes) -> bytes:
        try:
            msg = loads_bounded(payload)
        except JSONTooDeepError:
            owner.log({"event": "request_too_deep", "level": "warning", "endpoint": path_only,
                       "limit": MAX_JSON_DEPTH, "epoch": owner.last_epoch})
            return payload
        except ValueError:
            return payload
        if not (isinstance(msg, dict) and msg.get("type") == "response.create"):
            return payload
        turn_started = time.monotonic()
        meta: dict[str, Any] = {
            "started": turn_started,
            "model": _scalar_summary(msg.get("model")),
            "original_request_bytes": len(payload),
            "request_bytes": len(payload),
            "over_bound": False,
        }
        if not isinstance(msg.get("input"), list) or msg.get("previous_response_id"):
            meta["transform_skipped"] = ("previous_response_id" if msg.get("previous_response_id")
                                         else "input is not a list")
            with state_lock:
                pending.append((None, meta))
            return payload

        preserves = getattr(owner.transform, "preserves_input", False) is True
        before: bytes | None = None
        if preserves:
            original: dict[str, Any] = msg
        else:
            try:
                before = serialize_request(msg)
                original = json.loads(before)
            except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
                meta["transform_skipped"] = f"cannot serialize request: {type(exc).__name__}"
                with state_lock:
                    pending.append((None, meta))
                return payload

        prime = getattr(owner.transform, "prime", None)
        if callable(prime):
            try:
                prime(msg)
            except Exception:
                pass
        try:
            with owner.transform_lock:
                new_msg, _, events = owner.transform(msg, dict(req_headers), path_only)
            if not isinstance(new_msg, dict):
                raise TypeError("transform must return (dict, dict, list)")
        except Exception as exc:
            meta["transform_error"] = type(exc).__name__
            handler._log_transform_error(type(exc).__name__, "transform", path_only)
            with state_lock:
                pending.append((original, meta))
            return payload

        meta["transform_events"] = [_scalar_summary(e) for e in events if isinstance(e, dict)]
        meta["over_bound"] = any(bool(e.get("over_bound")) for e in events if isinstance(e, dict))
        epochs = [e["epoch"] for e in events if isinstance(e, dict) and "epoch" in e]
        if epochs:
            meta["epoch"] = owner.last_epoch = _scalar_summary(epochs[-1])

        out = payload
        if not (before is None and same_wire(new_msg, msg)):
            try:
                after = serialize_request(new_msg)
            except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
                meta["transform_error"] = f"serialize: {type(exc).__name__}"
                handler._log_transform_error(type(exc).__name__, "serialize", path_only)
                out = payload
            else:
                out = payload if after == before else after
        meta["request_bytes"] = len(out)
        with state_lock:
            pending.append((original, meta))
        return out

    def client_to_upstream() -> None:
        def on_ctrl(opcode: int, payload: bytes) -> None:
            if opcode == OP_CLOSE:
                code = parse_close_code(payload)
                with state_lock:
                    if session_info["close_code"] is None:
                        session_info["close_code"] = code
            try:
                send_upstream(encode_frame(opcode, payload, mask=os.urandom(4)))
            except OSError:
                pass

        try:
            while not closed.is_set():
                msg = read_message(client_reader, expect_masked=True, on_control=on_ctrl)
                if msg is None:
                    break
                opcode, payload = msg
                if opcode == OP_TEXT:
                    payload = transform_client_frame(payload)
                send_upstream(encode_frame(opcode, payload, mask=os.urandom(4)))
        except WSProtocolError as exc:
            with state_lock:
                session_info["error"] = f"client_protocol: {exc.reason}"
                session_info["close_code"] = exc.code
            try:
                send_client(encode_frame(OP_CLOSE, close_payload(exc.code, exc.reason)))
            except OSError:
                pass
        except (OSError, EOFError):
            pass
        finally:
            stop_both()

    def upstream_to_client() -> None:
        reader = UsageReader("text/event-stream", api="responses")
        holding = False
        held: list[bytes] = []
        client_calls = False
        announced = 0

        def announce() -> None:
            nonlocal announced
            hook = owner.on_tool_calls
            if hook is None or len(reader.call_ids) == announced:
                return
            new_ids, announced = tuple(reader.call_ids[announced:]), len(reader.call_ids)
            try:
                hook(new_ids)
            except Exception as exc:
                owner.log({"event": "tool_calls_hook_error", "error": type(exc).__name__})

        def finish_turn() -> None:
            nonlocal reader, holding, held, client_calls, announced
            with state_lock:
                item = pending.popleft() if pending else None
                if item is not None:
                    session_info["turns"] += 1
            usage_events: list[dict[str, Any]] = []
            if item is not None:
                original_msg, meta = item
                if original_msg is not None and reader.usage:
                    has_calls = client_calls or reader.tool_calls()
                    usage_events = handler._observe_usage(
                        path_only, reader.usage, original_msg, has_calls, tuple(reader.call_ids))
                log_turn(meta, reader, usage_events)
            reader = UsageReader("text/event-stream", api="responses")
            holding = False
            held = []
            client_calls = False
            announced = 0

        def release_held() -> None:
            nonlocal holding, held
            frames, held, holding = held, [], False
            for frame in frames:
                send_client(frame)

        def on_ctrl(opcode: int, payload: bytes) -> None:
            if opcode == OP_CLOSE:
                code = parse_close_code(payload)
                with state_lock:
                    if session_info["close_code"] is None:
                        session_info["close_code"] = code
                if held:
                    try:
                        release_held()
                    except OSError:
                        pass
            try:
                send_client(encode_frame(opcode, payload))
            except OSError:
                pass

        try:
            while not closed.is_set():
                msg = read_message(up_reader, expect_masked=False, on_control=on_ctrl)
                if msg is None:
                    break
                opcode, payload = msg
                wire_frame = encode_frame(opcode, payload)
                if opcode != OP_TEXT:
                    if holding:
                        held.append(wire_frame)
                    else:
                        send_client(wire_frame)
                    continue
                try:
                    event_obj = loads_bounded(payload)
                except ValueError:
                    event_obj = None
                if not isinstance(event_obj, dict):
                    if holding:
                        held.append(wire_frame)
                    else:
                        send_client(wire_frame)
                    continue
                kind = event_obj.get("type")
                reader.feed_payload(event_obj)
                announce()
                if kind in RESPONSE_END_EVENTS:
                    to_flush = list(held)
                    finish_turn()
                    for frame in to_flush:
                        send_client(frame)
                    send_client(wire_frame)
                    continue
                if kind in ("error", "response.error"):
                    to_flush = list(held)
                    finish_turn()
                    for frame in to_flush:
                        send_client(frame)
                    send_client(wire_frame)
                    continue
                if kind == "response.output_item.added":
                    out_item = event_obj.get("item")
                    item_type = str(out_item.get("type", "")) if isinstance(out_item, dict) else ""
                    if item_type.endswith("_call"):
                        holding = True
                        if is_client_call(out_item):
                            client_calls = True
                if holding:
                    held.append(wire_frame)
                else:
                    send_client(wire_frame)
        except WSProtocolError as exc:
            with state_lock:
                session_info["error"] = f"upstream_protocol: {exc.reason}"
                session_info["close_code"] = exc.code
            try:
                send_client(encode_frame(OP_CLOSE, close_payload(exc.code, exc.reason)))
            except OSError:
                pass
        except (OSError, EOFError):
            pass
        finally:
            if held:
                try:
                    to_flush = list(held)
                    finish_turn()
                    for frame in to_flush:
                        send_client(frame)
                except OSError:
                    pass
            stop_both()

    t_c2u = threading.Thread(target=client_to_upstream, name="contexto-ws-c2u", daemon=True)
    t_u2c = threading.Thread(target=upstream_to_client, name="contexto-ws-u2c", daemon=True)
    t_c2u.start()
    t_u2c.start()
    t_c2u.join()
    t_u2c.join(timeout=WS_JOIN_TIMEOUT_S)
    try:
        up_sock.close()
    except OSError:
        pass
    session_event: dict[str, Any] = {
        "event": "websocket_session",
        "path": path_only,
        "status": 101,
        "turns": session_info["turns"],
        "duration_ms": _ms(started),
    }
    if session_info["close_code"] is not None:
        session_event["close_code"] = session_info["close_code"]
    if session_info["error"] is not None:
        session_event["error"] = session_info["error"]
    owner.log(session_event)
