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
engine events (contract rule 10) are logged as given, reduced to scalars. A transform may mutate
its argument: the proxy then parses an untouched copy for ``observe_usage``. A transform with a true
``preserves_input`` attribute (the rewrite engine, ``identity_transform``) declares that it never does,
and the parsed request itself is that original.

Usage feedback: an optional ``observe_usage(endpoint, usage, request)`` callable is called after each
successful (2xx) ``/v1/messages`` or ``/responses`` response, before the client sees the response
complete, under the same lock, with the upstream usage dict
(``input_tokens``, ``cache_creation_input_tokens``, ``cache_read_input_tokens``,
``output_tokens``; for ``/responses`` the Responses usage as given) and the CLI's original request
dict, so a per-conversation engine can attribute
it. It is not called for ``count_tokens``, errors, or responses without usage. The engine's
estimator (rule 4) uses it. The launcher passes a ``contexto.rewrite.RewriteEngine`` instance as
the transform (it is callable) and its ``observe_usage`` method (lane A packet A2). The events the
callable returns (real-usage ``over_bound``, ``estimate_error``, rule 14 store failures) are written,
reduced to scalars, on that request's log entry as ``usage_events``, and a real ``over_bound`` sets the
entry's ``over_bound``; this is the only place they are logged (packet A2e). Calls to the
transform are serialized under a lock, so a stateful engine sees one request at a time.

Byte stability: the transformed request is serialized deterministically (original key order,
compact separators, non-ASCII kept as UTF-8, the same choices Claude Code's JSON.stringify
makes), so an unchanged prefix stays byte-identical across requests and the prompt cache can
read it. When the transform leaves the request unchanged (its serialization would equal that of the
parsed request; ``same_wire``), the CLI's original bytes are sent.

Responses are relayed as they arrive, including SSE streams. A side copy is read only for
``message_start`` and ``message_delta`` usage and any ``input_transformations``; usage reaches
the CLI unchanged (rule 6). Upstream errors are forwarded unchanged and never retried (rule 7).

``observe_thinking`` (off by default) adds the ``thinking-binding-controls-2026-08-01`` beta to
``anthropic-beta`` so that silently dropped thinking blocks are reported back as
``input_transformations`` and logged. It changes the request: one more beta flag upstream.

Upstream affinity (D36): with ``affinity_key`` set and an upstream that is not a vendor API host
(``VENDOR_HOSTS``), every forwarded request carries ``X-Session-Affinity: <affinity_key>`` unless the
CLI already sent that header. A routing proxy such as CLIProxyAPI uses it to send every request of a
session to one upstream credential when the request has no stronger session signal; the CLI's own
signals (Claude Code's ``X-Claude-Code-Session-Id`` and ``metadata.user_id``, Codex's ``session-id`` and
``prompt_cache_key``) are forwarded unchanged and rank first. ``affinity_key(session_id)`` derives the
value from a hash, so a chosen session name never leaves the machine.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import ssl
import threading
import time
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

try:  # Python 3.14+ ships zstd in the standard library; feature-detected (contract rule 15)
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - depends on the interpreter
    _zstd = None

LISTEN_HOST = "127.0.0.1"
# Contract rule 15 (PR #58): decoded request bodies are capped. A request carries the CLI's whole
# local history, which may legitimately be many times the bound, so the cap guards against a
# decompression bomb rather than enforcing the bound.
MAX_DECODED_BODY_BYTES = 64 * 1024 * 1024
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
STOP_DRAIN_S = 2.0  # how long stop() waits for in-flight requests to finish and log
RETRYABLE_PHASES = frozenset({"connect", "tls", "send"})  # before the body is fully written (rule 8, PR #95)
MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024
# Hop-by-hop headers (RFC 9110 7.6.1) plus Host and Content-Length, which the proxy sets itself.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
})
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
AFFINITY_HEADER = "X-Session-Affinity"
# Hosts (and their subdomains) that serve the vendor APIs directly: they have one identity per
# key, so the affinity header is not sent to them (D36).
VENDOR_HOSTS = ("anthropic.com", "openai.com", "chatgpt.com")

Headers = dict[str, str]
TransformResult = tuple[dict[str, Any], Headers, list[dict[str, Any]]]
Transform = Callable[[dict[str, Any], Headers, str], TransformResult]
ObserveUsage = Callable[[str, dict[str, int], dict[str, Any]], None]


class SessionLog(Protocol):
    def log(self, event: dict[str, Any]) -> None: ...


def identity_transform(request: dict[str, Any], headers: Headers, endpoint: str) -> TransformResult:
    return request, headers, []


identity_transform.preserves_input = True  # type: ignore[attr-defined]


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


# The types json.loads produces. Values of two different ones never serialize alike (a float always
# serializes with ".", "e", "Infinity" or "NaN"; bool is checked by exact type, not as an int).
_JSON_TYPES = frozenset({dict, list, str, int, float, bool, type(None)})


class _Undecided(Exception):
    """A value outside the JSON types: the structural comparison cannot decide."""


def _same_wire(a: Any, b: Any) -> bool:
    if a is b:
        return True  # one object serializes to one byte string
    ta, tb = type(a), type(b)
    if ta is not tb or ta not in _JSON_TYPES:
        if ta in _JSON_TYPES and tb in _JSON_TYPES:
            return False
        raise _Undecided
    if ta is dict:
        if len(a) != len(b):
            return False
        for (ka, va), (kb, vb) in zip(a.items(), b.items()):
            if type(ka) is not str or type(kb) is not str:
                raise _Undecided  # json.dumps converts other keys to strings
            if ka != kb or not _same_wire(va, vb):
                return False
        return True
    if ta is list:
        return len(a) == len(b) and all(_same_wire(x, y) for x, y in zip(a, b))
    if ta is float:
        return repr(a) == repr(b)  # json.dumps writes float.__repr__ (0.0 and -0.0 differ)
    return a == b


def same_wire(a: Any, b: Any) -> bool:
    """``serialize_request(a) == serialize_request(b)``, decided without serializing when it can be.

    Insertion order counts, as it does on the wire. A transform's output shares most objects with its
    input (the rewrite engine returns the same ``system``, ``tools`` and message dicts), so the
    comparison mostly checks identity and is linear in the number of messages, not their bytes. JSON
    values serialize prefix-free, so the first difference found at a position decides. A value json.loads
    cannot produce, or nesting too deep to walk, falls back to serializing both; a value that cannot be
    serialized compares unequal (the caller's own serialization then reports it)."""
    try:
        return _same_wire(a, b)
    except (_Undecided, RecursionError):
        pass
    try:
        return serialize_request(a) == serialize_request(b)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return False


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def affinity_key(session_id: str) -> str:
    """The ``X-Session-Affinity`` value for a Contexto session: stable for the session id, and a
    hash, so the id (which may be a name the user chose) is not sent."""
    digest = hashlib.sha256(b"contexto-affinity\0" + session_id.encode("utf-8")).hexdigest()
    return "contexto-" + digest[:32]


def is_vendor_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == vendor or host.endswith("." + vendor) for vendor in VENDOR_HOSTS)


def add_beta(headers: Headers, beta: str) -> Headers:
    """Return headers with ``beta`` appended to anthropic-beta (case-insensitive), once."""
    out = dict(headers)
    name = next((k for k in out if k.lower() == "anthropic-beta"), "anthropic-beta")
    present = [v.strip() for v in out.get(name, "").split(",") if v.strip()]
    if beta not in present:
        present.append(beta)
    out[name] = ",".join(present)
    return out


class EncodedBodyError(Exception):
    """A request body that cannot be decoded; ``reason`` explains it to the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _UpstreamReadError(Exception):
    """An upstream read failed mid-relay. Its own type keeps an upstream ConnectionResetError from
    being logged as a client abort; the cause names the failure."""


class _NotThisForm(Exception):
    """The first stream did not parse as this candidate form; try the next one."""


def _decode_streams(make: Callable[[], Any], body: bytes, limit: int, multi: bool) -> bytes:
    """Decode ``body`` as one stream, or as concatenated members/frames when ``multi``. Never returns
    a partial body: a truncated stream, data left after the last stream (when not ``multi``), or an
    undecodable later member raises; ``limit`` applies to the total across members."""
    out = bytearray()
    rest = body
    first = True
    while True:
        decompressor = make()
        try:
            out += decompressor.decompress(rest, limit + 1 - len(out))
        except MemoryError:
            raise
        except Exception as exc:  # zlib.error, or zstd's own ZstdError
            if first:
                raise _NotThisForm from exc
            raise EncodedBodyError("data after the first compressed member could not be decoded") from exc
        if len(out) > limit:
            raise EncodedBodyError(f"the body decodes to more than {limit:,} bytes")
        if not decompressor.eof:
            raise EncodedBodyError("the body is truncated" if not first or out else "the body could not be decoded")
        rest = decompressor.unused_data
        first = False
        if not rest:
            return bytes(out)
        if not multi:
            raise EncodedBodyError("there is data after the end of the compressed stream")


def decode_request_body(body: bytes, encoding: str, limit: int = MAX_DECODED_BODY_BYTES) -> bytes:
    """Decode a gzip, deflate, or (Python 3.14+) zstd request body, refusing anything over ``limit``.

    gzip members (RFC 1952) and zstd frames may be concatenated; every one is decoded and the
    results joined. Anything after the last complete member or frame that is not another one,
    zero padding included, is refused (intake, A1c). A zlib or raw deflate stream is single: trailing
    bytes after it are refused. The ``limit`` applies to the total. Raises EncodedBodyError for an
    unsupported encoding, a corrupt or truncated body, trailing data, or a body over ``limit``; a
    partial body is never returned."""
    if encoding == "gzip":
        forms = [(lambda: zlib.decompressobj(16 + zlib.MAX_WBITS), True)]
    elif encoding == "deflate":  # HTTP deflate is zlib-wrapped; some clients send raw deflate
        forms = [(lambda: zlib.decompressobj(zlib.MAX_WBITS), False),
                 (lambda: zlib.decompressobj(-zlib.MAX_WBITS), False)]
    elif encoding == "zstd":
        if _zstd is None:
            raise EncodedBodyError("zstd decoding needs Python 3.14 or newer")
        forms = [(_zstd.ZstdDecompressor, True)]
    else:
        raise EncodedBodyError("this encoding is not supported (gzip, deflate, and zstd on Python 3.14+ are)")
    if not body:
        raise EncodedBodyError("the body is empty")
    for make, multi in forms:
        try:
            return _decode_streams(make, body, limit, multi)
        except _NotThisForm:
            continue
    raise EncodedBodyError("the body could not be decoded")


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

    It also records how the response ended (packet A23), from enum fields only, each checked against
    its allowlist (``END_ALLOWLISTS``, packet A26), never text or error messages: for Responses, the end event (completed, incomplete, failed, or none when
    a stream ended without one), ``response.status``, ``incomplete_details.reason``, and
    ``error.code``; for Messages, ``stop_reason`` and an ``error`` event's ``error.type``.
    """

    def __init__(self, content_type: str | None, api: str = "messages") -> None:
        self.api = api
        self.sse = "text/event-stream" in (content_type or "").lower()
        self.usage: dict[str, Any] = {}
        self.input_transformations: list[Any] | None = None
        self.end_event: str | None = None  # Responses stream: completed, incomplete, or failed
        self.ending: dict[str, str] = {}  # response_status, incomplete_reason, error_code, stop_reason, error_type
        self._buffer = bytearray()
        self._events = SSEEventSplitter()
        self._overflow = False

    def feed(self, chunk: bytes) -> None:
        if not self.sse:
            if len(self._buffer) + len(chunk) > MAX_JSON_RESPONSE_BYTES:
                self._overflow = True
                self._buffer.clear()
            elif not self._overflow:
                self._buffer += chunk
            return
        for raw in self._events.feed(chunk):
            self.feed_event(raw)

    def finish(self) -> None:
        if self.sse:
            # A final event that upstream ended the stream on without its blank line is still what
            # upstream sent (its message_delta usage is the response's real usage), so it is read.
            self.feed_event(self._events.flush())
        elif self._buffer and not self._overflow:
            try:
                body = json.loads(bytes(self._buffer))
            except ValueError:
                body = None
            if isinstance(body, dict):
                if self.api == "responses":
                    self._take_responses_usage(body)
                    self._take_responses_end(body)
                else:
                    self._take_usage(body.get("usage"))
                    self._take_transformations(body)
                    self._take_messages_end(body)
        self._buffer.clear()

    def feed_event(self, raw: bytes) -> None:
        """One raw SSE event, as split by ``SSEEventSplitter``. The stream hold's path calls this
        directly, so one splitter serves both the hold and this reader."""
        data = sse_data(raw)
        try:
            payload = json.loads(data) if data else None
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        if self.api == "responses":
            if payload.get("type") in RESPONSE_END_EVENTS:
                self.end_event = payload["type"].split(".", 1)[1]
                if isinstance(payload.get("response"), dict):
                    self._take_responses_usage(payload["response"])
                    self._take_responses_end(payload["response"])
            elif payload.get("type") == "error":
                self._keep("error_code", payload.get("code"))
        elif payload.get("type") == "error":
            self._take_messages_end(payload)
        elif payload.get("type") == "message_start":
            message = payload.get("message") or {}
            self._take_usage(message.get("usage"))
            self._take_transformations(message)
        elif payload.get("type") == "message_delta":
            self._take_usage(payload.get("usage"))
            self._take_transformations(payload)
            if isinstance(payload.get("delta"), dict):
                self._keep("stop_reason", payload["delta"].get("stop_reason"))

    def end_fields(self) -> dict[str, str]:
        """How the response ended, for the request's log entry (packet A23): enum strings only."""
        out = dict(self.ending)
        if self.api == "responses" and self.sse:
            out = {"end_event": self.end_event or "none", **out}
        return out

    def _keep(self, key: str, value: Any) -> None:
        """Record ``value`` for ``key`` if it is on the field's allowlist, else the literal ``other``
        (packet A26), so the field still shows a non-completed end. An absent or null value is skipped."""
        if value is not None:
            self.ending[key] = value if isinstance(value, str) and value in END_ALLOWLISTS[key] else "other"

    def _take_responses_end(self, response: dict[str, Any]) -> None:
        self._keep("response_status", response.get("status"))
        if isinstance(response.get("incomplete_details"), dict):
            self._keep("incomplete_reason", response["incomplete_details"].get("reason"))
        if isinstance(response.get("error"), dict):
            self._keep("error_code", response["error"].get("code"))

    def _take_messages_end(self, body: dict[str, Any]) -> None:
        self._keep("stop_reason", body.get("stop_reason"))
        if isinstance(body.get("error"), dict):
            self._keep("error_type", body["error"].get("type"))

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


# Server-sent events (WHATWG HTML, "Parsing an event stream"): a line ends at CRLF, LF, or a CR not
# followed by LF, and an event ends at a blank line, so at two line endings in a row in any mix. The
# stream hold's splitter and the usage reader both use this grammar, through SSEEventSplitter and
# sse_lines, so they always agree on where an event ends and what it says.
_SSE_LINE_END = rb"(?:\r\n|\n|\r(?!\n))"
_SSE_EVENT_END = re.compile(_SSE_LINE_END + _SSE_LINE_END)
_SSE_LINE_SPLIT = re.compile(rb"\r\n|\n|\r")


class SSEEventSplitter:
    """Splits an SSE byte stream into raw events, each with its terminating blank line, byte for
    byte: joining what ``feed`` and ``flush`` return gives back exactly what was fed.

    A CR that ends a read and would end an event may be the first half of a CRLF, so that event is
    returned once the next byte arrives (or by ``flush``); every other event is returned as soon as
    its blank line is read. ``flush`` returns what is left at end of stream: a final event without
    its blank line, or ``b""``. The SSE spec has a browser discard such an event; the proxy still
    relays its bytes unchanged (rule 8: the CLI decides) and reads its usage (rule 9), because it is
    what upstream actually sent and the tokens it reports were spent.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._scanned = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        events: list[bytes] = []
        start = 0
        # A match must include a new byte, and the longest unmatched end (CRLF CR) is 3 bytes.
        pos = max(0, self._scanned - 3)
        while match := _SSE_EVENT_END.search(self._buffer, pos):
            if match.end() == len(self._buffer) and self._buffer.endswith(b"\r"):
                break  # wait for the next byte: CRLF CR + LF is CRLF CRLF
            events.append(bytes(self._buffer[start:match.end()]))
            start = pos = match.end()
        del self._buffer[:start]
        self._scanned = len(self._buffer)
        return events

    def flush(self) -> bytes:
        tail, self._buffer, self._scanned = bytes(self._buffer), bytearray(), 0
        return tail


def sse_lines(raw: bytes) -> list[bytes]:
    """The lines of one raw SSE event, split at CRLF, LF, or CR (blank lines included)."""
    return _SSE_LINE_SPLIT.split(raw)


def _sse_field(line: bytes, name: bytes) -> bytes | None:
    """The value of ``line`` if it is field ``name``: after the colon, minus one leading space."""
    if not line.startswith(name + b":"):
        return None
    value = line[len(name) + 1:]
    return value[1:] if value.startswith(b" ") else value


def sse_data(raw: bytes) -> bytes:
    """The data of one raw SSE event: its ``data:`` lines joined with LF."""
    return b"\n".join(v for line in sse_lines(raw) if (v := _sse_field(line, b"data")) is not None)

# Packet A26: the only values each end field can log; anything else is logged as "other". Sources,
# read 2026-10-16:
# - Responses: openai-python 3.31.0, generated from the OpenAPI spec: ResponseStatus
#   (types/responses/response_status.py), Response.IncompleteDetails.reason (types/responses/response.py),
#   ResponseError.code (types/responses/response_error.py). An ``error`` stream event's code is a free
#   string there, so it shares ResponseError's list.
# - Messages: anthropic-python 1.15.0 StopReason (types/stop_reason.py), and the error types on
#   https://platform.claude.com/docs/en/api/errors ("HTTP errors"), which the SDK's ErrorObject union
#   matches except for conflict_error and request_too_large.
END_ALLOWLISTS: dict[str, frozenset[str]] = {
    "response_status": frozenset({"completed", "failed", "in_progress", "cancelled", "queued", "incomplete"}),
    "incomplete_reason": frozenset({"max_output_tokens", "max_messages", "content_filter", "steered"}),
    "error_code": frozenset({
        "server_error", "rate_limit_exceeded", "invalid_prompt", "data_residency_mismatch", "bio_policy",
        "misalignment_policy_violation", "vector_store_timeout", "invalid_image", "invalid_image_format",
        "invalid_base64_image", "invalid_image_url", "image_too_large", "image_too_small", "image_parse_error",
        "image_content_policy_violation", "invalid_image_mode", "image_file_too_large",
        "unsupported_image_media_type", "empty_image_file", "failed_to_download_image", "image_file_not_found"}),
    "stop_reason": frozenset({"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal",
                              "model_context_window_exceeded"}),
    "error_type": frozenset({
        "invalid_request_error", "authentication_error", "billing_error", "permission_error", "not_found_error",
        "conflict_error", "request_too_large", "rate_limit_error", "api_error", "timeout_error",
        "overloaded_error"}),
}


def sse_event_type(raw: bytes) -> str | None:
    """The ``type`` of one raw SSE event (from its data JSON, else its ``event:`` line)."""
    payload = _sse_payload(raw)
    if isinstance(payload.get("type"), str):
        return payload["type"]
    for line in sse_lines(raw):
        if (value := _sse_field(line, b"event")) is not None:
            return value.strip().decode("utf-8", "replace")
    return None


def _sse_payload(raw: bytes) -> dict[str, Any]:
    data = sse_data(raw)
    try:
        payload = json.loads(data) if data else {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


class StreamHold:
    """The stream hold of contract PR #74 (at a64f70b). Claude Code starts an MCP tool call as soon
    as a ``tool_use`` block completes, before the stream ends (measured: 5 to 16 ms after
    ``content_block_stop``, seconds before ``message_stop``), so the per-turn recall room must be
    published before the CLI sees any tool call:

    - Messages: events stream live until the first ``tool_use`` ``content_block_start``; from there
      EVERY event is held in order until ``message_delta`` (the usage publish runs first), an
      ``error`` event, or end of stream, then released in its original order.
    - Responses: the hold starts at the first ``response.output_item.added`` whose item type is a
      tool call (``*_call``: ``custom_tool_call``, ``function_call``, and others) and ends at
      ``response.completed``, ``response.incomplete``, ``response.failed``, an error, or end of stream.
    - ``ping`` keepalives pass during the hold (they are not content, so content order is unchanged).
    - Held bytes are bounded by one response: everything after the first tool call of that response.

    ``on_trigger`` is called once, at the first ``message_delta`` or terminal ``response.*`` event,
    whether or not anything is held, before that event or any held event is released.
    ``before_release`` is called when a hold ends any other way (an error event, end of stream, or
    an upstream failure, through ``eof``) with something held, before the held events are returned,
    so the usage already read is published before the CLI sees a tool call on those paths too.
    """

    def __init__(self, api: str, on_trigger: Callable[[], None],
                 before_release: Callable[[], None] | None = None) -> None:
        self.api = api
        self.on_trigger = on_trigger
        self.before_release = before_release
        self.held: list[bytes] = []
        self.holding = False
        self.triggered = False
        self.held_events = 0

    def _starts_hold(self, kind: str | None, raw: bytes) -> bool:
        if self.api == "responses":
            if kind != "response.output_item.added":
                return False
            item = _sse_payload(raw).get("item")
            return isinstance(item, dict) and str(item.get("type", "")).endswith("_call")
        if kind != "content_block_start":
            return False
        block = _sse_payload(raw).get("content_block")
        return isinstance(block, dict) and block.get("type") == "tool_use"

    def _is_trigger(self, kind: str | None) -> bool:
        return kind == "message_delta" if self.api == "messages" else kind in RESPONSE_END_EVENTS

    def _is_error(self, kind: str | None) -> bool:
        return kind in ("error", "response.error")

    def feed(self, raw: bytes) -> list[bytes]:
        """One complete raw SSE event in; the raw events to write now, in order."""
        kind = sse_event_type(raw)
        if self._is_trigger(kind) and not self.triggered:
            self.triggered = True
            self.on_trigger()  # publish the real-usage room before anything held reaches the CLI
            return self._release() + [raw]
        if self._is_error(kind):
            return self._release_untriggered() + [raw]
        if kind == "ping":
            return [raw]
        if not self.holding and self._starts_hold(kind, raw):
            self.holding = True
        if self.holding:
            self.held.append(raw)
            self.held_events += 1
            return []
        return [raw]

    def eof(self) -> list[bytes]:
        return self._release_untriggered()

    def _release_untriggered(self) -> list[bytes]:
        if self.held and self.before_release is not None:
            self.before_release()  # publish what usage there is before anything held reaches the CLI
        return self._release()

    def _release(self) -> list[bytes]:
        out, self.held, self.holding = self.held, [], False
        return out


class _UpstreamHTTPS(http.client.HTTPSConnection):
    """HTTPSConnection whose connect() records whether it reached TLS, so a failure names the step
    (contract rule 8, PR #95). Upstream connections never use a CONNECT tunnel."""

    def __init__(self, host: str, port: int, *, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self.tls_context = context
        self.phase = "connect"

    def connect(self) -> None:
        self.phase = "connect"
        http.client.HTTPConnection.connect(self)
        self.phase = "tls"
        self.sock = self.tls_context.wrap_socket(self.sock, server_hostname=self.host)


class ContextoProxy:
    def __init__(self, upstream: str, log: SessionLog, transform: Transform = identity_transform, *,
                 observe_usage: ObserveUsage | None = None, observe_thinking: bool = False,
                 upstream_source: str | None = None, affinity_key: str | None = None) -> None:
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("upstream must be an http(s) base URL")
        self.scheme = parts.scheme
        self.upstream_host = parts.hostname
        # The X-Session-Affinity value added to each request, or None (D36): never for a vendor host.
        self.affinity_header = affinity_key if affinity_key and not is_vendor_host(parts.hostname) else None
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
        self._in_flight = 0  # requests being handled; idle keep-alive connections are not counted
        self._idle = threading.Condition()
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
        """Stop accepting connections, then wait up to STOP_DRAIN_S for in-flight requests to finish
        and log their entries (handler threads are daemons, so a process exit would cut them off)."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            deadline = time.monotonic() + STOP_DRAIN_S
            with self._idle:
                while self._in_flight and (left := deadline - time.monotonic()) > 0:
                    self._idle.wait(left)
                in_flight = self._in_flight
            if in_flight:
                self.log({"event": "proxy_stop_incomplete", "in_flight": in_flight})
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
            return _UpstreamHTTPS(self.upstream_host, self.upstream_port,
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
                if size < 0:  # read(-1) would wait for the client to close the connection
                    raise ValueError("negative chunk size")
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(body)
                body += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0:
            raise ValueError("negative Content-Length")
        return self.rfile.read(length) if length else b""

    def _handle(self) -> None:
        with self.owner._idle:
            self.owner._in_flight += 1
        try:
            self._handle_counted()
        finally:
            with self.owner._idle:
                self.owner._in_flight -= 1
                self.owner._idle.notify_all()

    def _handle_counted(self) -> None:
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
                            "error": type(exc).__name__,
                            "fallback": "forward_original" if fallback else self._local_fallback()})
            if fallback:
                body, headers = self._raw
                entry = {"event": "request", "method": self.command, "path": path, "fallback": True,
                         "original_request_bytes": len(body), "request_bytes": len(body)}
                try:
                    self._forward(body, headers, entry, time.monotonic(), None)
                    return
                except Exception as again:
                    self.owner.log({"event": "proxy_error", "path": path, "error": type(again).__name__,
                                    "fallback": self._local_fallback()})
            if not self._response_started:
                self._send_local_error(502, f"contexto proxy: internal error ({type(exc).__name__})")
            self.close_connection = True

    def _local_fallback(self) -> str:
        """``local_502`` when the 502 below is sent; ``none`` once the response has started, since
        then the client only sees the connection close."""
        return "none" if self._response_started else "local_502"

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
        affinity = self.owner.affinity_header
        if affinity and not any(k.lower() == AFFINITY_HEADER.lower() for k in headers):
            headers[AFFINITY_HEADER] = affinity
        self._raw = (body, dict(headers))
        entry: dict[str, Any] = {"event": "request", "method": self.command, "path": path_only,
                                 "original_request_bytes": len(body)}
        original = None
        if self.command == "POST" and path_only in TRANSFORMED_PATHS:
            entry["api"] = "responses" if path_only in RESPONSES_PATHS else "messages"
            # The proxy must read these responses (usage, and the stream hold of PR #74), so it asks
            # upstream for an unencoded body instead of forwarding the CLI's Accept-Encoding.
            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
            headers["Accept-Encoding"] = "identity"
            encoding = (self.headers.get("Content-Encoding") or "").strip().lower()
            if encoding not in ("", "identity"):
                # Contract rule 15 (PR #58): decode, transform as usual, forward uncompressed.
                try:
                    decoded = decode_request_body(body, encoding, MAX_DECODED_BODY_BYTES)
                except EncodedBodyError as exc:
                    self._reject_encoded(encoding, exc.reason, path_only, entry["api"])
                    return
                entry.update(request_content_encoding=encoding, wire_request_bytes=len(body),
                             original_request_bytes=len(decoded))
                body = decoded
                headers = {k: v for k, v in headers.items() if k.lower() != "content-encoding"}
                self._raw = (body, dict(headers))
            body, headers, original = self._transform(body, headers, entry, path_only)
        entry["request_bytes"] = len(body)
        self._forward(body, headers, entry, started, original)

    def _transform(self, body: bytes, headers: Headers, entry: dict[str, Any],
                   endpoint: str) -> tuple[bytes, Headers, dict[str, Any] | None]:
        owner = self.owner
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
        preserves = getattr(owner.transform, "preserves_input", False) is True
        before: bytes | None = None
        if preserves:
            # The transform declares it never mutates its argument (the rewrite engine): the parsed
            # request is the untouched original, and no copy or reference serialization is made.
            original = request
        else:
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
        if before is None and same_wire(new_request, request):
            return body, new_headers, original  # unchanged: the CLI's own bytes, nothing serialized
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
        # Contract rule 8 (PR #95): each upstream step is named, so a failure says where it happened:
        # "connect" (TCP), "tls", "send" (headers and body), "wait_headers" (only once endheaders has
        # returned, so the whole body is written). A failure before that point is retried once, byte
        # for byte, on a new connection: upstream cannot act on a request it never fully received.
        # From wait_headers on, upstream may be running it, so that failure is a 502 and the CLI decides.
        retried = False
        while True:
            conn = owner.connection()  # a new connection per request: nothing is pooled or reused
            phase, phase_started = "connect", time.monotonic()
            try:
                conn.connect()
                phase, phase_started = "send", time.monotonic()
                conn.putrequest(self.command, owner.base_path + self.path, skip_host=True,
                                skip_accept_encoding=True)
                conn.putheader("Host", owner.upstream_netloc)
                for name, value in headers.items():
                    conn.putheader(name, value)
                if body or self.command in ("POST", "PUT", "PATCH"):
                    conn.putheader("Content-Length", str(len(body)))
                self._upstream_sent = True
                conn.endheaders(body or None)
                phase, phase_started = "wait_headers", time.monotonic()
                response = conn.getresponse()
                break
            except (OSError, http.client.HTTPException) as exc:
                # HTTPException: a malformed response head (BadStatusLine, LineTooLong, too many
                # headers). It is raised at wait_headers, so it is never retried, and has no errno.
                conn.close()
                if phase == "connect":
                    phase = getattr(conn, "phase", phase)
                failure = {"upstream_phase": phase, "phase_ms": _ms(phase_started),
                           "errno": getattr(exc, "errno", None)}
                if not retried and phase in RETRYABLE_PHASES:
                    retried = True
                    owner.log({"event": "upstream_retry", "path": entry["path"], "error": type(exc).__name__,
                               "elapsed_ms": _ms(started), **failure})
                    continue
                entry.update(status=502, error=f"upstream unreachable: {type(exc).__name__}",
                             duration_ms=_ms(started), **failure)
                if retried:
                    entry["upstream_retried"] = True
                self._send_local_error(502, f"contexto proxy: upstream unreachable ({type(exc).__name__})")
                owner.log(entry)
                return
        if retried:
            entry["upstream_retried"] = True

        content_type = response.getheader("Content-Type")
        reader = UsageReader(content_type, entry.get("api", "messages"))
        decoder = _SideDecoder(response.getheader("Content-Encoding"))
        length = response.getheader("Content-Length")
        chunked = length is None and self.command != "HEAD"
        expected = None if chunked else _declared_length(length, response, self.command)
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
        observed = False
        feedback = (original is not None and entry["path"] in USAGE_FEEDBACK_PATHS and 200 <= response.status < 300)

        def observe() -> None:
            nonlocal observed
            if feedback and not observed and reader.usage:
                observed = True
                # Packet A2e: the returned events go on this request's entry, logged once with it.
                usage_events = self._observe_usage(entry["path"], reader.usage, original)
                if usage_events:
                    entry["usage_events"] = [_scalar_summary(e) for e in usage_events]
                    entry["over_bound"] = bool(entry.get("over_bound")) or any(bool(e.get("over_bound"))
                                                                               for e in usage_events)

        # Contract PR #74: SSE with identity encoding on a transformed endpoint is relayed event by event
        # through StreamHold, so no tool call reaches the CLI before the real-usage room is published.
        hold = (StreamHold(entry.get("api", "messages"), observe, before_release=observe)
                if feedback and reader.sse and chunked and decoder.encoding in ("", "identity") else None)
        if feedback and reader.sse and hold is None:
            # Not silently: an upstream that encodes anyway (or does not chunk) defeats the hold.
            owner.log({"event": "hold_skipped", "path": entry["path"], "encoding": decoder.encoding or "identity",
                       "chunked": chunked})
        splitter = SSEEventSplitter()  # the hold path splits once, for both the reader and the hold

        def read_upstream() -> bytes:
            try:
                return response.read1(65536)
            except (OSError, http.client.HTTPException) as exc:  # IncompleteRead is not an OSError
                raise _UpstreamReadError from exc

        def write_events(events: list[bytes]) -> None:
            for raw in events:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
            if events:
                self.wfile.flush()

        try:
            while chunk := read_upstream():
                response_bytes += len(chunk)
                if hold is not None:
                    for raw in splitter.feed(chunk):
                        reader.feed_event(raw)  # usage is read before the hold can trigger on it
                        write_events(hold.feed(raw))
                    continue
                reader.feed(decoder.decode(chunk))
                if chunked:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    if held:
                        self.wfile.write(held)
                    held = chunk
                self.wfile.flush()
            if hold is not None:
                if tail := splitter.flush():  # a final event without its blank line (see SSEEventSplitter)
                    reader.feed_event(tail)
                    write_events(hold.feed(tail))
                write_events(hold.eof())
            elif expected is not None and response_bytes < expected:
                # read1 returns b"" at EOF instead of raising, so a Content-Length body cut short
                # ends the loop like a whole one: named like the IncompleteRead a chunked read raises.
                raise _UpstreamReadError from http.client.IncompleteRead(b"", expected - response_bytes)
        except _UpstreamReadError as exc:  # upstream ended early: the client gets no terminator
            entry["error"] = f"relay: {type(exc.__cause__).__name__}"
            self.close_connection = True
            relay_ok = False
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
            if hold is not None and hold.held:  # upstream failed mid-hold: observe, then release in order
                try:
                    write_events(hold.eof())
                except OSError:
                    relay_ok = False
        if hold is not None:
            entry["stream_hold"] = {"held_events": hold.held_events, "triggered": hold.triggered}
        reader.finish()
        if entry.get("api") == "responses":
            usage = reader.usage or None  # Responses usage kept as given (see UsageReader)
        else:
            usage = {k: reader.usage.get(k) for k in USAGE_KEYS} if reader.usage else None
        entry.update(status=response.status, response_bytes=response_bytes, duration_ms=_ms(started), usage=usage)
        if reader.input_transformations is not None:
            entry["input_transformations"] = reader.input_transformations
        if decoder.supported:
            entry.update(reader.end_fields())  # packet A23: how the response ended, content-free
        else:
            entry["usage_unavailable"] = f"response content-encoding {decoder.encoding}"
        observe()  # streams without a trigger event and non-SSE responses: after the body, before completion
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

    def _reject_encoded(self, encoding: str, reason: str, endpoint: str, api: str) -> None:
        """Contract rule 15 (PR #58): a body the proxy cannot decode is a client problem, not a
        Contexto defect, so it is refused with HTTP 415 in the API's own error format and never
        forwarded, because an unread request cannot be kept bounded."""
        message = (f"contexto: cannot read a request body with Content-Encoding {encoding}: {reason}. Run the CLI "
                   f"through 'contexto run', or disable request compression.")
        if api == "responses":
            payload = {"error": {"message": message, "type": "invalid_request_error", "param": None,
                                 "code": "unsupported_content_encoding"}}
        else:
            payload = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
        self.owner.log({"event": "encoded_body_rejected", "encoding": encoding, "endpoint": endpoint,
                        "reason": reason})
        body = json.dumps(payload).encode()
        self.send_response_only(415)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log_transform_error(self, error: str, stage: str, endpoint: str) -> None:
        self.owner.log({"event": "transform_error", "level": "error", "error": error, "stage": stage,
                        "endpoint": endpoint, "epoch": self.owner.last_epoch})

    def _observe_usage(self, endpoint: str, usage: dict[str, Any], original: dict[str, Any]) -> list[dict[str, Any]]:
        """Run the usage callback; return the dict events it reports (anything else is ignored)."""
        callback = self.owner.observe_usage
        if callback is None:
            return []
        try:
            with self.owner.transform_lock:
                events = callback(endpoint, dict(usage), original)
        except Exception as exc:
            self.owner.log({"event": "observe_usage_error", "error": type(exc).__name__})
            return []
        return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []

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


def _declared_length(length: str | None, response: http.client.HTTPResponse, method: str) -> int | None:
    """The body length a Content-Length response promises, or None when it promises none: no or an
    unparseable header (http.client then reads to close), a chunked body (the header is ignored),
    HEAD, 1xx, 204, and 304 (no body whatever the header says)."""
    if length is None or response.chunked or method == "HEAD" or response.status in (204, 304) \
            or 100 <= response.status < 200:
        return None
    try:
        declared = int(length)
    except ValueError:
        return None
    return declared if declared >= 0 else None


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
