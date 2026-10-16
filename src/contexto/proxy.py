"""Local Anthropic Messages API proxy with a request transform hook (M2 contract, lane A).

The proxy listens on 127.0.0.1 only. ``POST /v1/messages`` and ``POST /v1/messages/count_tokens``
bodies go through a caller-supplied transform (contract rule 11, so the CLI's own counts match what
is sent); every other path and method, such as ``HEAD /api/hello``, is forwarded unchanged. Auth headers are forwarded as
received and are never stored or logged.

Transform interface (the rewrite engine plugs in here)::

    transform(request: dict, headers: dict[str, str], endpoint: str) -> (request, headers, events)

``request`` is the parsed JSON body with its key order preserved; ``headers`` holds the request
headers that will be forwarded (hop-by-hop headers removed); ``endpoint`` is ``/v1/messages`` or
``/v1/messages/count_tokens``, so a stateful engine can apply the same rewrite to a count request
without treating it as a turn. ``events`` is a list of
content-free dicts the proxy copies into the session log entry for that request. Keys the
proxy reads from events: ``over_bound`` (truthy marks the request over the bound) and
``epoch`` (the epoch state, repeated in the log entry of any 4xx response). Resync and other
engine events (contract rule 10) are logged as given, reduced to scalars.

Usage feedback: an optional ``observe_usage(endpoint, usage, request)`` callable is called after each
successful (2xx) ``/v1/messages`` or ``/responses`` response, before the client sees the response
complete, under the same lock, with the upstream usage dict
(``input_tokens``, ``cache_creation_input_tokens``, ``cache_read_input_tokens``,
``output_tokens``; for ``/responses`` the Responses usage as given) and the CLI's original request
dict, so a per-conversation engine can attribute
it. It is not called for ``count_tokens``, errors, or responses without usage. The engine's
estimator (rule 4) uses it. The launcher passes a ``contexto.rewrite.RewriteEngine`` instance as
the transform (it is callable) and its ``observe_usage`` method (lane A packet A2). Calls to the
transform are serialized under a lock, so a stateful engine sees one request at a time.

Byte stability: the transformed request is serialized deterministically (original key order,
compact separators, non-ASCII kept as UTF-8, the same choices Claude Code's JSON.stringify
makes), so an unchanged prefix stays byte-identical across requests and the prompt cache can
read it. When the transform leaves the request unchanged, the CLI's original bytes are sent.

Responses are relayed as they arrive, including SSE streams. A side copy is read only for
``message_start`` and ``message_delta`` usage and any ``input_transformations``; usage reaches
the CLI unchanged (rule 6). Upstream errors are forwarded unchanged and never retried (rule 7).

``observe_thinking`` (off by default) adds the ``thinking-binding-controls-2026-08-01`` beta to
``anthropic-beta`` so that silently dropped thinking blocks are reported back as
``input_transformations`` and logged. It changes the request: one more beta flag upstream.
"""

from __future__ import annotations

import http.client
import json
import ssl
import threading
import time
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

LISTEN_HOST = "127.0.0.1"
MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
# OpenAI Responses API for the Codex adapter (lane A packet A5). Codex's base_url is the proxy root
# without /v1 and the upstream base carries the version path, so Codex sends POST /responses; the
# /v1 spelling is accepted too. The engine dispatches on endpoint.endswith("/responses").
RESPONSES_PATHS = frozenset({"/responses", "/v1/responses"})
TRANSFORMED_PATHS = frozenset({MESSAGES_PATH, COUNT_TOKENS_PATH}) | RESPONSES_PATHS
USAGE_FEEDBACK_PATHS = frozenset({MESSAGES_PATH}) | RESPONSES_PATHS
RESPONSE_END_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
UPSTREAM_TIMEOUT_S = 900
MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024
# Hop-by-hop headers (RFC 9110 7.6.1) plus Host and Content-Length, which the proxy sets itself.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
})
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")

Headers = dict[str, str]
TransformResult = tuple[dict[str, Any], Headers, list[dict[str, Any]]]
Transform = Callable[[dict[str, Any], Headers, str], TransformResult]
ObserveUsage = Callable[[str, dict[str, int], dict[str, Any]], None]


class SessionLog(Protocol):
    def log(self, event: dict[str, Any]) -> None: ...


def identity_transform(request: dict[str, Any], headers: Headers, endpoint: str) -> TransformResult:
    return request, headers, []


def serialize_request(request: Any) -> bytes:
    """Deterministic wire bytes: insertion key order, no spaces, UTF-8 without escaping.

    This is deliberately not ``contexto.canonical`` (sorted keys, for hashes and ids): keeping
    the CLI's own key order reproduces its ``system`` and ``tools`` bytes exactly, so the first
    rewritten request shares its cached prefix with the unrewritten requests before it.

    A lone UTF-16 surrogate (for example tool output cut in the middle of an emoji) cannot be
    encoded as UTF-8; ``backslashreplace`` writes it as a backslash-u escape, byte-identical to
    what JSON.stringify emits for the same string.
    """
    return json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "backslashreplace")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def add_beta(headers: Headers, beta: str) -> Headers:
    """Return headers with ``beta`` appended to anthropic-beta (case-insensitive), once."""
    out = dict(headers)
    name = next((k for k in out if k.lower() == "anthropic-beta"), "anthropic-beta")
    present = [v.strip() for v in out.get(name, "").split(",") if v.strip()]
    if beta not in present:
        present.append(beta)
    out[name] = ",".join(present)
    return out


def _scalar_summary(value: Any) -> Any:
    """Keep numbers, booleans, and short strings; reduce anything else to its size."""
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value if len(value) <= 80 else f"<{len(value)} chars>"
    if isinstance(value, dict):
        return {str(k): _scalar_summary(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scalar_summary(v) for v in value[:50]]
    return f"<{type(value).__name__}>"


class _SideDecoder:
    """Decompresses a side copy of the response for usage parsing; relayed bytes are untouched."""

    def __init__(self, encoding: str | None) -> None:
        encoding = (encoding or "identity").strip().lower()
        self.supported = encoding in ("", "identity", "gzip", "deflate")
        self._obj = (zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip"
                     else zlib.decompressobj() if encoding == "deflate" else None)
        self.encoding = encoding

    def decode(self, chunk: bytes) -> bytes:
        if not self.supported:
            return b""
        if self._obj is None:
            return chunk
        try:
            return self._obj.decompress(chunk)
        except zlib.error:
            self.supported = False
            return b""


def _numeric(value: Any) -> Any:
    """Numbers and nested dicts of numbers only (Responses usage keeps input_tokens_details)."""
    if isinstance(value, dict):
        return {str(k): v for k, v in ((k, _numeric(v)) for k, v in value.items()) if v is not None}
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class UsageReader:
    """Reads usage and input_transformations from SSE events or a JSON body, nothing else.

    Messages API (``api="messages"``): usage from ``message_start`` and ``message_delta``, limited to
    the four Anthropic keys. Responses API (``api="responses"``): ``response.usage`` from
    ``response.completed``, ``response.incomplete``, or ``response.failed`` (or a JSON body), kept
    as given: ``input_tokens`` includes cached tokens, with ``input_tokens_details.cached_tokens``,
    ``cache_write_tokens``, ``output_tokens``, and ``output_tokens_details.reasoning_tokens``.
    """

    def __init__(self, content_type: str | None, api: str = "messages") -> None:
        self.api = api
        self.sse = "text/event-stream" in (content_type or "").lower()
        self.usage: dict[str, Any] = {}
        self.input_transformations: list[Any] | None = None
        self._buffer = bytearray()
        self._overflow = False

    def feed(self, chunk: bytes) -> None:
        if not self.sse:
            if len(self._buffer) + len(chunk) > MAX_JSON_RESPONSE_BYTES:
                self._overflow = True
                self._buffer.clear()
            elif not self._overflow:
                self._buffer += chunk
            return
        self._buffer += chunk.replace(b"\r\n", b"\n")
        while (end := self._buffer.find(b"\n\n")) >= 0:
            event = bytes(self._buffer[:end])
            del self._buffer[: end + 2]
            self._event(event)

    def finish(self) -> None:
        if self.sse:
            if self._buffer.strip():
                self._event(bytes(self._buffer))
        elif self._buffer and not self._overflow:
            try:
                body = json.loads(bytes(self._buffer))
            except ValueError:
                body = None
            if isinstance(body, dict):
                if self.api == "responses":
                    self._take_responses_usage(body)
                else:
                    self._take_usage(body.get("usage"))
                    self._take_transformations(body)
        self._buffer.clear()

    def _event(self, raw: bytes) -> None:
        data = b"\n".join(line[5:].lstrip() for line in raw.split(b"\n") if line.startswith(b"data:"))
        try:
            payload = json.loads(data) if data else None
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        if self.api == "responses":
            if payload.get("type") in RESPONSE_END_EVENTS and isinstance(payload.get("response"), dict):
                self._take_responses_usage(payload["response"])
        elif payload.get("type") == "message_start":
            message = payload.get("message") or {}
            self._take_usage(message.get("usage"))
            self._take_transformations(message)
        elif payload.get("type") == "message_delta":
            self._take_usage(payload.get("usage"))
            self._take_transformations(payload)

    def _take_responses_usage(self, response: dict[str, Any]) -> None:
        usage = response.get("usage")
        if isinstance(usage, dict):
            self.usage = _numeric(usage)

    def _take_usage(self, usage: Any) -> None:
        if isinstance(usage, dict):
            self.usage.update({k: v for k, v in usage.items() if k in USAGE_KEYS and isinstance(v, int)})

    def _take_transformations(self, container: dict[str, Any]) -> None:
        for holder in (container, container.get("usage") if isinstance(container.get("usage"), dict) else {}):
            value = holder.get("input_transformations")
            if isinstance(value, list):
                self.input_transformations = [_scalar_summary(v) for v in value]


class ContextoProxy:
    def __init__(self, upstream: str, log: SessionLog, transform: Transform = identity_transform, *,
                 observe_usage: ObserveUsage | None = None, observe_thinking: bool = False,
                 upstream_source: str | None = None) -> None:
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("upstream must be an http(s) base URL")
        self.scheme = parts.scheme
        self.upstream_host = parts.hostname
        self.upstream_port = parts.port or (443 if parts.scheme == "https" else 80)
        self.upstream_netloc = parts.netloc.rsplit("@", 1)[-1]
        self.base_path = parts.path.rstrip("/")
        self.session_log = log
        self.transform = transform
        self.observe_usage = observe_usage
        self.observe_thinking = observe_thinking
        self.upstream_source = upstream_source
        self.transform_lock = threading.Lock()
        self.last_epoch: Any = None  # the engine's epoch from its last successful transform
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("proxy is not started")
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{LISTEN_HOST}:{self.port}"

    def start(self, port: int = 0) -> int:
        """Bind 127.0.0.1:port (0 picks a free port), serve in a thread, return the bound port."""
        proxy = self

        class Handler(_Handler):
            owner = proxy

        self._server = ThreadingHTTPServer((LISTEN_HOST, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="contexto-proxy", daemon=True)
        self._thread.start()
        self.log({"event": "proxy_start", "port": self.port, "upstream_source": self.upstream_source,
                  "observe_thinking": self.observe_thinking})
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self.log({"event": "proxy_stop"})

    def serve_forever(self, port: int = 0) -> None:
        """Block until interrupted, starting the server first if needed."""
        if self._server is None:
            self.start(port)
        try:
            assert self._thread is not None
            self._thread.join()
        except KeyboardInterrupt:
            self.stop()

    def connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.upstream_host, self.upstream_port,
                                               timeout=UPSTREAM_TIMEOUT_S, context=ssl.create_default_context())
        return http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=UPSTREAM_TIMEOUT_S)

    def log(self, event: dict[str, Any]) -> None:
        try:
            self.session_log.log({"ts": utc_now(), **event})
        except Exception:  # a broken log must never break the user's session
            pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    owner: ContextoProxy

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # the default access log prints paths to stderr; the session log is the record

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True  # a client dropping an idle keep-alive connection

    def do_GET(self) -> None:
        self._handle()

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

    def _handle(self) -> None:
        self._response_started = False
        self._upstream_sent = False
        self._raw: tuple[bytes, Headers] | None = None
        try:
            self._handle_request()
        except Exception as exc:
            # Last resort, never drop the client's connection without an answer. If the CLI's
            # request was read intact and nothing has reached upstream or the client yet, forward
            # the CLI's own bytes unchanged: a local 502 would make Claude Code retry into the same
            # error and stall the session. Otherwise answer a local 502.
            path = self.path.split("?", 1)[0]
            fallback = self._raw is not None and not self._upstream_sent and not self._response_started
            self.owner.log({"event": "proxy_error", "method": self.command, "path": path,
                            "error": type(exc).__name__, "fallback": "forward_original" if fallback else "local_502"})
            if fallback:
                body, headers = self._raw
                entry = {"event": "request", "method": self.command, "path": path, "fallback": True,
                         "original_request_bytes": len(body), "request_bytes": len(body)}
                try:
                    self._forward(body, headers, entry, time.monotonic(), None)
                    return
                except Exception as again:
                    self.owner.log({"event": "proxy_error", "path": path, "error": type(again).__name__,
                                    "fallback": "local_502"})
            if not self._response_started:
                self._send_local_error(502, f"contexto proxy: internal error ({type(exc).__name__})")
            self.close_connection = True

    def _handle_request(self) -> None:
        started = time.monotonic()
        path_only = self.path.split("?", 1)[0]
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            # Not tunneled: a WebSocket session (Codex's default transport) would bypass the bound.
            # The launcher keeps Codex on HTTP; anything else is refused visibly, not mangled.
            self.owner.log({"event": "websocket_rejected", "path": path_only})
            self._send_local_error(501, "contexto proxy: WebSocket is not supported; use HTTP",
                                   "invalid_request_error")
            self.close_connection = True
            return
        try:
            body = self._read_body()
        except ValueError:
            self.owner.log({"event": "request", "method": self.command, "path": path_only, "status": 400,
                            "error": "malformed request framing"})
            self._send_local_error(400, "contexto proxy: malformed request framing", "invalid_request_error")
            self.close_connection = True
            return
        headers: Headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        self._raw = (body, dict(headers))
        entry: dict[str, Any] = {"event": "request", "method": self.command, "path": path_only,
                                 "original_request_bytes": len(body)}
        original = None
        if self.command == "POST" and path_only in TRANSFORMED_PATHS:
            entry["api"] = "responses" if path_only in RESPONSES_PATHS else "messages"
            body, headers, original = self._transform(body, headers, entry, path_only)
        entry["request_bytes"] = len(body)
        self._forward(body, headers, entry, started, original)

    def _transform(self, body: bytes, headers: Headers, entry: dict[str, Any],
                   endpoint: str) -> tuple[bytes, Headers, dict[str, Any] | None]:
        owner = self.owner
        encoding = self.headers.get("Content-Encoding")
        if encoding:
            # Forwarded untouched (the proxy never re-encodes), but unbounded: say so loudly.
            entry["transform_skipped"] = f"request content-encoding {encoding.strip().lower()}"
            entry["unbounded"] = True
            self.owner.log({"event": "request_not_transformed", "path": entry["path"],
                            "reason": entry["transform_skipped"]})
            return body, headers, None
        try:
            request = json.loads(body)
        except (ValueError, RecursionError):
            entry["transform_skipped"] = "request body is not JSON"
            return body, headers, None
        if not isinstance(request, dict):
            entry["transform_skipped"] = "request body is not an object"
            return body, headers, None
        entry["model"] = request.get("model")
        entry["stream"] = bool(request.get("stream"))
        entry["original_message_count"] = len(request.get("messages") or [])
        try:
            before = serialize_request(request)
            original = json.loads(before)  # an untouched copy; the transform may mutate its argument
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            entry["transform_skipped"] = f"cannot serialize request: {type(exc).__name__}"
            return body, headers, None
        try:
            with owner.transform_lock:
                new_request, new_headers, events = owner.transform(request, dict(headers), endpoint)
            if not isinstance(new_request, dict) or not isinstance(new_headers, dict):
                raise TypeError("transform must return (dict, dict, list)")
        except Exception as exc:
            # Contract rule 14, genuine bugs: never alter the request on a failed transform, even
            # one that mutated its argument before raising. Forward the CLI's own bytes and headers
            # and log transform_error at error level (acceptance counts it as a failure).
            entry["transform_error"] = type(exc).__name__
            self._log_transform_error(type(exc).__name__, "transform", endpoint)
            entry["message_count"] = entry["original_message_count"]
            if owner.observe_thinking:
                headers = add_beta(headers, THINKING_BINDING_BETA)
            return body, headers, original
        entry["transform_events"] = [_scalar_summary(e) for e in events if isinstance(e, dict)]
        entry["over_bound"] = any(bool(e.get("over_bound")) for e in events if isinstance(e, dict))
        epochs = [e["epoch"] for e in events if isinstance(e, dict) and "epoch" in e]
        if epochs:
            entry["epoch"] = owner.last_epoch = _scalar_summary(epochs[-1])
        if owner.observe_thinking:
            new_headers = add_beta(new_headers, THINKING_BINDING_BETA)
        entry["message_count"] = len(new_request.get("messages") or [])
        try:
            after = serialize_request(new_request)
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            # The engine produced something that cannot be sent: forward the CLI's own bytes.
            entry["transform_error"] = f"serialize: {type(exc).__name__}"
            self._log_transform_error(type(exc).__name__, "serialize", endpoint)
            entry["message_count"] = entry["original_message_count"]
            return body, headers, original
        return (body if after == before else after), new_headers, original

    def _forward(self, body: bytes, headers: Headers, entry: dict[str, Any], started: float,
                 original: dict[str, Any] | None) -> None:
        owner = self.owner
        conn = owner.connection()
        try:
            conn.putrequest(self.command, owner.base_path + self.path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", owner.upstream_netloc)
            for name, value in headers.items():
                conn.putheader(name, value)
            if body or self.command in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", str(len(body)))
            self._upstream_sent = True
            conn.endheaders(body or None)
            response = conn.getresponse()
        except OSError as exc:
            conn.close()
            entry.update(status=502, error=f"upstream unreachable: {type(exc).__name__}", duration_ms=_ms(started))
            self._send_local_error(502, f"contexto proxy: upstream unreachable ({type(exc).__name__})")
            owner.log(entry)
            return

        content_type = response.getheader("Content-Type")
        reader = UsageReader(content_type, entry.get("api", "messages"))
        decoder = _SideDecoder(response.getheader("Content-Encoding"))
        length = response.getheader("Content-Length")
        chunked = length is None and self.command != "HEAD"
        self._response_started = True
        self.send_response_only(response.status, response.reason)
        for name, value in response.getheaders():
            if name.lower() not in HOP_BY_HOP:
                self.send_header(name, value)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        elif length is not None:
            self.send_header("Content-Length", length)
        self.end_headers()
        response_bytes = 0
        # The client must not see the response complete before the engine has its usage and the
        # session log has the entry: Claude Code sends its next request right away, and the engine
        # sizes it from the last real usage (rule 4). So the chunk terminator, or the last chunk of
        # a Content-Length response, is written only after usage feedback and logging.
        held = b""
        relay_ok = True
        try:
            while chunk := response.read1(65536):
                response_bytes += len(chunk)
                reader.feed(decoder.decode(chunk))
                if chunked:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    if held:
                        self.wfile.write(held)
                    held = chunk
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            entry["client_aborted"] = True
            self.close_connection = True
            relay_ok = False
        except OSError as exc:
            entry["error"] = f"relay: {type(exc).__name__}"
            self.close_connection = True
            relay_ok = False
        finally:
            conn.close()
        reader.finish()
        if entry.get("api") == "responses":
            usage = reader.usage or None  # Responses usage kept as given (see UsageReader)
        else:
            usage = {k: reader.usage.get(k) for k in USAGE_KEYS} if reader.usage else None
        entry.update(status=response.status, response_bytes=response_bytes, duration_ms=_ms(started), usage=usage)
        if reader.input_transformations is not None:
            entry["input_transformations"] = reader.input_transformations
        if not decoder.supported:
            entry["usage_unavailable"] = f"response content-encoding {decoder.encoding}"
        if (original is not None and entry["path"] in USAGE_FEEDBACK_PATHS and 200 <= response.status < 300
                and reader.usage):
            self._observe_usage(entry["path"], reader.usage, original)
        owner.log(entry)
        if 400 <= response.status < 500:
            owner.log({"event": "upstream_4xx", "status": response.status, "path": entry["path"],
                       "model": entry.get("model"), "epoch": entry.get("epoch"),
                       "transform_events": entry.get("transform_events")})
        if relay_ok:
            try:
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                elif held:
                    self.wfile.write(held)
                self.wfile.flush()
            except OSError:
                self.close_connection = True

    def _log_transform_error(self, error: str, stage: str, endpoint: str) -> None:
        self.owner.log({"event": "transform_error", "level": "error", "error": error, "stage": stage,
                        "endpoint": endpoint, "epoch": self.owner.last_epoch})

    def _observe_usage(self, endpoint: str, usage: dict[str, Any], original: dict[str, Any]) -> None:
        callback = self.owner.observe_usage
        if callback is None:
            return
        try:
            with self.owner.transform_lock:
                callback(endpoint, dict(usage), original)
        except Exception as exc:
            self.owner.log({"event": "observe_usage_error", "error": type(exc).__name__})

    def _send_local_error(self, status: int, message: str, error_type: str = "api_error") -> None:
        body = json.dumps({"type": "error", "error": {"type": error_type, "message": message}}).encode()
        try:
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
