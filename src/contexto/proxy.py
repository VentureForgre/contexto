"""Local Anthropic Messages API proxy with a request transform hook (M2 contract, lane A).

The proxy listens on 127.0.0.1 only. ``POST /v1/messages`` and ``POST /v1/messages/count_tokens``
bodies go through a caller-supplied transform (contract rule 11, so the CLI's own counts match what
is sent); every other path and method, such as ``HEAD /api/hello``, is forwarded unchanged. Auth
headers are forwarded as received and are never stored or logged.

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
entry's ``over_bound``; this is the only place they are logged (packet A2e). A callable that takes a
``tool_calls`` keyword also gets whether the response holds a tool call the client runs (D40): from
the stream hold when there is one (whether a client call started it; a hosted Responses call such
as ``web_search_call`` streams live and does not hold or count), else from a Messages ``stop_reason`` or
a Responses ``output`` list, else None for unknown. One that takes ``tool_call_ids`` gets the ids of
the client calls seen (``tool_use`` ids, Responses ``call_id``), so the engine can tell which request
continues which side room. Calls to the
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
``input_transformations`` and logged, merged across the response's events (at most
``MAX_INPUT_TRANSFORMATIONS`` items, with ``input_transformations_truncated`` when more were
reported; see ``UsageReader._take_transformations``). It changes the request: one more beta flag
upstream.

Upstream affinity (D36): with ``affinity_key`` set and an upstream whose host name is not a vendor
API host (``VENDOR_HOSTS``, compared after IDNA normalization), every forwarded request carries
``X-Session-Affinity: <affinity_key>`` unless the CLI already sent that header. A routing proxy such
as CLIProxyAPI uses it to send every request of a session to one upstream credential when the request
has no stronger session signal; the CLI's own signals (Claude Code's ``X-Claude-Code-Session-Id`` and
``metadata.user_id``, Codex's ``session-id`` and ``prompt_cache_key``) are forwarded unchanged.
``affinity_key(session_id, salt)`` is an HMAC of the session id under a random per-install salt
(``contexto.private.affinity_salt``), so the session id is not sent and cannot be recovered from the
key by guessing names.

Gateway hint headers (D70): with ``strip_hint_headers`` the six headers Claude Code sends only when
``CLAUDE_CODE_GATEWAY_HINT_HEADERS`` is on (``contexto.hints.GATED_HINT_HEADERS``) are left out of
every request the proxy sends upstream: a transformed one, one forwarded unchanged, and the CLI's own
request on each fail-open path. The transform still gets them, since it reads the request's class from
them. ``contexto run claude`` turns the hints on for the engine and sets this, so the upstream gets
the headers it got before; it leaves it off when the user turned the hints on themselves, or turned
them off so that Contexto does not ask for them (``contexto.hints.hint_headers_state``). Off by
default: ``contexto proxy`` and any other
caller forward every header as received. ``X-Claude-Code-Session-Id``, ``x-claude-code-agent-id`` and
``x-claude-code-parent-agent-id`` are never removed.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import inspect
import json
import os
import re
import select
import socket
import socketserver
import ssl
import threading
import time
import zlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from contexto.canonical import MAX_JSON_DEPTH, JSONTooDeepError, has_lone_surrogate, loads_bounded
from contexto.dialects import (
    dialect_for as registry_dialect_for,
)
from contexto.dialects import (
    model_from_endpoint,
)
from contexto.hints import GATED_HINT_HEADERS

try:  # Python 3.14+ ships zstd in the standard library; feature-detected (contract rule 15)
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - depends on the interpreter
    _zstd = None

LISTEN_HOST = "127.0.0.1"
# The Host values the local CLI sends (D53, D59): the loopback address or localhost, any port or none.
LOCAL_HOST_HEADER = re.compile(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?")
# Contract rule 15 (PR #58): decoded request bodies are capped. A request carries the CLI's whole
# local history, which may legitimately be many times the bound, so the cap guards against a
# decompression bomb rather than enforcing the bound.
MAX_DECODED_BODY_BYTES = 64 * 1024 * 1024
# Uncompressed/wire inbound request body cap (configurable independently of MAX_DECODED_BODY_BYTES).
MAX_REQUEST_BODY_BYTES = MAX_DECODED_BODY_BYTES
MAX_CONTENT_ENCODINGS = 8  # cap on stacked Content-Encoding layers
MAX_CHUNK_TRAILERS = 64  # cap on trailer header lines in a chunked request body
MAX_CHUNK_TRAILER_BYTES = 65536  # cap on total trailer bytes in a chunked request body
MAX_CALL_IDS = 4096  # per-response client tool call id cap
UPSTREAM_POOL_MAX_IDLE = 8  # idle keep-alive upstream connections kept per proxy
UPSTREAM_POOL_IDLE_TTL_S = 30.0  # evict pooled upstream connections idle longer than this
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_STALE_POOL_ERRORS = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                      http.client.CannotSendRequest)
_HEX_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]+")
MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
# OpenAI Responses API for the Codex adapter (lane A packet A5). Codex's base_url is the proxy root
# without /v1 and the upstream base carries the version path, so Codex sends POST /responses; the
# /v1 spelling is accepted too. The engine dispatches on endpoint.endswith("/responses").
RESPONSES_PATHS = frozenset({"/responses", "/v1/responses"})
CHAT_COMPLETIONS_PATHS = frozenset({"/chat/completions", "/v1/chat/completions"})
TRANSFORMED_PATHS = frozenset({MESSAGES_PATH, COUNT_TOKENS_PATH}) | RESPONSES_PATHS | CHAT_COMPLETIONS_PATHS
USAGE_FEEDBACK_PATHS = frozenset({MESSAGES_PATH}) | RESPONSES_PATHS | CHAT_COMPLETIONS_PATHS
# Vertex AI and Microsoft Foundry Anthropic Messages paths (measured on Claude Code 2.1.328):
# - Vertex sends /projects/<p>/locations/<loc>/publishers/anthropic/models/<model>:streamRawPredict
#   or :rawPredict (and count-tokens:rawPredict), optionally prefixed with /v1.
# - Foundry sends /v1/messages (already MESSAGES_PATH) or /anthropic/v1/messages (and count_tokens).
VERTEX_COUNT_TOKENS_RE = re.compile(
    r"^/(?:v1/)?projects/[A-Za-z0-9][A-Za-z0-9._-]*/locations/[A-Za-z0-9][A-Za-z0-9._-]*"
    r"/publishers/anthropic/models/count-tokens:rawPredict\Z"
)
VERTEX_MESSAGES_RE = re.compile(
    r"^/(?:v1/)?projects/[A-Za-z0-9][A-Za-z0-9._-]*/locations/[A-Za-z0-9][A-Za-z0-9._-]*"
    r"/publishers/anthropic/models/[A-Za-z0-9][A-Za-z0-9._@-]*:(?:streamRawPredict|rawPredict)\Z"
)
FOUNDRY_MESSAGES_PATHS = frozenset({"/anthropic/v1/messages"})
FOUNDRY_COUNT_TOKENS_PATHS = frozenset({"/anthropic/v1/messages/count_tokens"})


def classify_path(path: str) -> tuple[str | None, str | None]:
    """Classify a POST path into ``(api, canonical_endpoint)``: ``("messages", "/v1/messages")``,
    ``("messages", "/v1/messages/count_tokens")``, ``("responses", path)``,
    ``("chat_completions", path)``, ``("gemini", path)``, or ``(None, None)``.

    Recognises direct Anthropic Messages paths, Vertex AI ``:rawPredict`` / ``:streamRawPredict`` /
    ``count-tokens:rawPredict`` paths, Microsoft Foundry ``/anthropic/v1/messages`` paths, and any
    dialect registered in ``contexto.dialects`` (Responses, Chat Completions, Gemini)."""
    if path == MESSAGES_PATH or path in FOUNDRY_MESSAGES_PATHS:
        return "messages", MESSAGES_PATH
    if path == COUNT_TOKENS_PATH or path in FOUNDRY_COUNT_TOKENS_PATHS:
        return "messages", COUNT_TOKENS_PATH
    if VERTEX_COUNT_TOKENS_RE.match(path):
        return "messages", COUNT_TOKENS_PATH
    if VERTEX_MESSAGES_RE.match(path):
        return "messages", MESSAGES_PATH
    dialect = registry_dialect_for(path)
    if dialect is not None:
        return dialect.api_name, path
    return None, None


def is_transformed_path(path: str) -> bool:
    """Whether ``path`` carries a model or token-count body the proxy transforms."""
    return classify_path(path)[0] is not None


def is_usage_feedback_path(path: str) -> bool:
    """Whether ``path`` returns model usage fed back to the rewrite engine."""
    api, canonical = classify_path(path)
    if api is None or canonical is None:
        return False
    dialect = registry_dialect_for(canonical)
    return dialect is not None and not dialect.is_count_endpoint(canonical)
RESPONSE_END_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})
# D40: Responses tool calls that run on OpenAI's servers (the Responses API reference's hosted tools;
# ``mcp_call`` is the hosted remote-MCP tool, which OpenAI calls itself). They never reach Contexto's
# MCP server, do not open a side room, and stream live without activating StreamHold. Any other ``*_call``
# item, known (``function_call``, ``custom_tool_call``, ``local_shell_call``, ``computer_call``) or
# not, counts as a call the client runs, so a new type fails safe.
HOSTED_CALL_TYPES = frozenset({"web_search_call", "file_search_call", "image_generation_call",
                               "code_interpreter_call", "mcp_call"})


def is_client_call(item: Any) -> bool:
    """D40: a Responses output item that is a tool call the client runs (see ``HOSTED_CALL_TYPES``).
    A ``tool_search_call`` runs hosted unless its ``execution`` is ``client`` (OpenAI tool search)."""
    item_type = item.get("type") if isinstance(item, dict) else None
    if item_type == "tool_search_call":
        return item.get("execution") == "client"
    return isinstance(item_type, str) and item_type.endswith("_call") and item_type not in HOSTED_CALL_TYPES


THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
UPSTREAM_TIMEOUT_S = 900
STOP_DRAIN_S = 2.0  # how long stop() waits for in-flight requests to finish and log
RETRYABLE_PHASES = frozenset({"connect", "tls", "send"})  # before the body is fully written (rule 8, PR #95)
EARLY_RESPONSE_WAIT_S = 1.0  # how long a failed send waits for an answer upstream sent before it closed (D53)
CLIENT_WATCH_INTERVAL_S = 0.05  # how often a request in flight checks that its client is still there (D53)
LISTEN_BACKLOG = 128  # socketserver's default of 5 dropped 13 to 21% of a 200-connection burst
MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_INPUT_TRANSFORMATIONS = 32  # input_transformations items logged per response
MAX_SUMMARY_DEPTH = 16  # _scalar_summary: deeper dicts and lists become a placeholder
# Hop-by-hop headers (RFC 9110 7.6.1) plus Host and Content-Length, which the proxy sets itself.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
})
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
AFFINITY_HEADER = "X-Session-Affinity"
# The one answer to a request the proxy refuses to serve (D53), whatever the reason: the body never says why.
REFUSED_MESSAGE = "contexto proxy: request refused (not from the local CLI)"
# Host names (and their subdomains) that serve the vendor APIs directly, including Azure OpenAI,
# Azure AI Foundry, and Google Vertex AI / Gemini: each has one identity per credential, so the
# affinity header is not sent to them (D36). Matching is by host name only; an IP literal upstream
# gets the header.
VENDOR_HOSTS = ("anthropic.com", "openai.com", "chatgpt.com", "openai.azure.com", "services.ai.azure.com",
                "cognitiveservices.azure.com", "generativelanguage.googleapis.com", "aiplatform.googleapis.com")
# Regional Vertex AI endpoints use a hyphenated single-label prefix (e.g. us-east5-aiplatform.googleapis.com).
_VERTEX_REGIONAL_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-aiplatform\.googleapis\.com\Z")
_ALLOWED_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$|^\[[0-9a-f:.]+\]$")
_LOCAL_ORIGIN_SCHEMES = frozenset({"vscode-webview", "cursor-webview", "jetbrains", "file"})


def normalize_allowed_host(host: str) -> str:
    """Validate and normalize one opt-in ``--allow-host`` entry (lowercase hostname or bracketed IPv6,
    no port, no whitespace, no wildcards)."""
    value = host.strip().lower()
    if not value or not _ALLOWED_HOST_RE.fullmatch(value):
        raise ValueError(f"invalid --allow-host value {host!r}: expected a hostname or IP without scheme or port")
    return value


def _host_without_port(host_header: str) -> str:
    value = host_header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    host, colon, port = value.rpartition(":")
    if colon and port.isdigit() and 1 <= len(port) <= 5:
        return host
    return value


def is_local_origin(origin: str, allowed_hosts: frozenset[str] = frozenset()) -> bool:
    """True when ``Origin`` names a local webview/IDE scheme or an http(s) origin on loopback or one of
    ``allowed_hosts``. Used only when opt-in ``allow_local_origin`` is enabled on a token-gated service."""
    value = origin.strip()
    if not value or value == "null":
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme in _LOCAL_ORIGIN_SCHEMES:
        return True
    if scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if host in ("127.0.0.1", "localhost", "::1") or host.endswith(".localhost"):
        return True
    return host in allowed_hosts or f"[{host}]" in allowed_hosts


def _normalize_base_path(raw_path: str) -> str:
    """Encode and normalize an upstream base URL path as Claude Code (WHATWG URL) sends it:
    backslashes become forward slashes, ``.`` and ``..`` segments are resolved, ``|``, ``[``, and ``]``
    stay literal, and non-ASCII bytes are UTF-8 percent-encoded."""
    if not raw_path:
        return ""
    parts = raw_path.replace("\\", "/").split("/")
    if parts and parts[0] == "":
        parts = parts[1:]
    segments: list[str] = []
    for seg in parts:
        if seg == "..":
            if segments:
                segments.pop()
        elif seg == ".":
            continue
        else:
            segments.append(seg)
    while segments and segments[-1] == "":
        segments.pop()
    if not segments:
        return ""
    return "/" + "/".join(quote(seg, safe="%!$&'()*+,;=:@-._~|[]") for seg in segments)

Headers = dict[str, str]
HeaderList = list[tuple[str, str]]
TransformResult = tuple[dict[str, Any], Headers, list[dict[str, Any]]]
Transform = Callable[[dict[str, Any], Headers, str], TransformResult]
ObserveUsage = Callable[[str, dict[str, int], dict[str, Any]], None]


class BodyTooLargeError(ValueError):
    """An inbound request body exceeded ``MAX_REQUEST_BODY_BYTES``."""


def _headers_to_dict(header_list: HeaderList | Headers) -> Headers:
    """Single-value view of ``header_list`` for transforms, combining repeated headers per RFC 9110."""
    if isinstance(header_list, dict):
        return dict(header_list)
    names: dict[str, str] = {}
    values: dict[str, list[str]] = {}
    for name, value in header_list:
        lower = name.lower()
        names.setdefault(lower, name)
        values.setdefault(lower, []).append(value)
    return {names[lower]: ("; " if lower == "cookie" else ", ").join(vals) for lower, vals in values.items()}


def _merge_headers(raw_headers: HeaderList | Headers, before: Headers, after: Headers) -> HeaderList:
    """Apply transform header changes (``before`` -> ``after``) onto ``raw_headers`` while preserving
    untouched repeated headers verbatim."""
    raw_list: HeaderList = list(raw_headers.items()) if isinstance(raw_headers, dict) else raw_headers
    before_lower = {k.lower(): v for k, v in before.items()}
    after_lower: dict[str, tuple[str, str]] = {}
    for k, v in after.items():
        after_lower[k.lower()] = (k, v)
    out: HeaderList = []
    emitted: set[str] = set()
    for name, value in raw_list:
        lower = name.lower()
        if lower not in after_lower:
            continue  # removed by transform
        new_name, new_value = after_lower[lower]
        if new_value == before_lower.get(lower):
            out.append((name, value))  # unchanged: keep every repeated occurrence verbatim
        elif lower not in emitted:
            out.append((new_name, new_value))  # modified: emit updated value once
            emitted.add(lower)
    seen = {name.lower() for name, _ in raw_list}
    for name, value in after.items():
        if name.lower() not in seen:
            out.append((name, value))
    return out


def _strip_hint_header_list(header_list: HeaderList) -> HeaderList:
    """``header_list`` without the six gated hint headers (D70), preserving repeated headers."""
    return [(name, value) for name, value in header_list if name.lower() not in GATED_HINT_HEADERS]


def _remove_header(header_list: HeaderList, lower_name: str) -> HeaderList:
    return [(name, value) for name, value in header_list if name.lower() != lower_name]


def _parse_content_encodings(values: list[str]) -> list[str]:
    """Split one or more ``Content-Encoding`` header values into ordered lowercase coding names."""
    codings: list[str] = []
    for raw in values:
        for part in raw.split(","):
            token = part.strip().lower()
            if token:
                codings.append(token)
    return codings


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
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def affinity_key(session_id: str, salt: bytes) -> str:
    """The ``X-Session-Affinity`` value for a Contexto session: stable for the session id under one
    install's ``salt`` (across restarts and resumes), and an HMAC, so neither the id (which may be a
    name the user chose) nor a guess of it can be checked against the key without the salt."""
    digest = hmac.new(salt, b"contexto-affinity\0" + session_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return "contexto-" + digest[:32]


def is_vendor_host(host: str) -> bool:
    """True for a vendor API host name or a subdomain of one (including regional Google Vertex AI
    ``<region>-aiplatform.googleapis.com`` endpoints). The name is IDNA-normalized first, as the
    socket layer does when it connects (a soft hyphen in ``api.anthro\xadpic.com`` maps away); a
    name that cannot be normalized counts as a vendor host, so it gets no header."""
    try:
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return True
    if _VERTEX_REGIONAL_HOST_RE.match(host):
        return True
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


def _decode_one_encoding(body: bytes, encoding: str, limit: int) -> bytes:
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


def decode_request_body(body: bytes, encoding: str, limit: int = MAX_DECODED_BODY_BYTES) -> bytes:
    """Decode a gzip, deflate, or (Python 3.14+) zstd request body (including stacked comma-separated
    encodings applied in reverse order per RFC 9110), refusing anything over ``limit``.

    gzip members (RFC 1952) and zstd frames may be concatenated; every one is decoded and the
    results joined. Anything after the last complete member or frame that is not another one,
    zero padding included, is refused (intake, A1c). A zlib or raw deflate stream is single: trailing
    bytes after it are refused. The ``limit`` applies to every layer. Raises EncodedBodyError for an
    unsupported encoding, a corrupt or truncated body, trailing data, or a body over ``limit``; a
    partial body is never returned."""
    codings = _parse_content_encodings([encoding])
    if len(codings) > MAX_CONTENT_ENCODINGS:
        raise EncodedBodyError(f"too many stacked content encodings (at most {MAX_CONTENT_ENCODINGS})")
    layers = [c for c in codings if c != "identity"]
    if not layers:
        if len(body) > limit:
            raise EncodedBodyError(f"the body decodes to more than {limit:,} bytes")
        return body
    current = body
    for coding in reversed(layers):
        current = _decode_one_encoding(current, coding, limit)
    return current


def _scalar_summary(value: Any, depth: int = 0) -> Any:
    """Keep numbers, booleans, and short strings; reduce anything else to its size. A dict or list
    nested deeper than ``MAX_SUMMARY_DEPTH`` becomes its type name (``<dict>``, ``<list>``), so a
    deeply nested upstream value cannot raise RecursionError out of the relay loop."""
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value if len(value) <= 80 else f"<{len(value)} chars>"
    if isinstance(value, (dict, list)) and depth >= MAX_SUMMARY_DEPTH:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        return {str(k): _scalar_summary(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_scalar_summary(v, depth + 1) for v in value[:50]]
    return f"<{type(value).__name__}>"


class _SideDecoder:
    """Decompresses a side copy of the response for usage parsing; relayed bytes are untouched."""

    def __init__(self, encoding: str | None, *, limit: int = MAX_DECODED_BODY_BYTES) -> None:
        raw = (encoding or "identity").strip().lower()
        codings = _parse_content_encodings([raw])
        layers = [c for c in codings if c != "identity"]
        self.encoding = ", ".join(layers) if layers else "identity"
        self.supported = len(layers) <= MAX_CONTENT_ENCODINGS and all(c in ("gzip", "deflate") for c in layers)
        self._remaining = limit
        self._objs = (
            [zlib.decompressobj(16 + zlib.MAX_WBITS) if c == "gzip" else zlib.decompressobj()
             for c in reversed(layers)]
            if self.supported else []
        )

    def decode(self, chunk: bytes) -> bytes:
        if not self.supported or not self._objs:
            return chunk if self.supported else b""
        data = chunk
        for obj in self._objs:
            if not data:
                break
            if self._remaining <= 0:
                self.supported = False
                return b""
            try:
                out = obj.decompress(data, self._remaining + 1)
            except zlib.error:
                self.supported = False
                return b""
            if len(out) > self._remaining:
                self.supported = False
                return b""
            data = out
        self._remaining -= len(data)
        return data


def _numeric(value: Any, depth: int = 0) -> Any:
    """Numbers and nested dicts of numbers only (Responses usage keeps input_tokens_details). A dict
    nested deeper than ``MAX_SUMMARY_DEPTH`` is dropped, so a deeply nested upstream ``usage`` cannot
    raise RecursionError out of the relay loop."""
    if isinstance(value, dict):
        if depth >= MAX_SUMMARY_DEPTH:
            return None
        return {str(k): v for k, v in ((k, _numeric(v, depth + 1)) for k, v in value.items()) if v is not None}
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class UsageReader:
    """Reads usage and input_transformations from SSE events or a JSON body, nothing else.

    Messages API (``api="messages"``): usage from ``message_start`` and ``message_delta``, limited to
    the four Anthropic keys. Responses API (``api="responses"``): ``response.usage`` from
    ``response.completed``, ``response.incomplete``, or ``response.failed`` (or a JSON body), kept
    as given: ``input_tokens`` includes cached tokens, with ``input_tokens_details.cached_tokens``,
    ``cache_write_tokens``, ``output_tokens``, and ``output_tokens_details.reasoning_tokens``.

    It also records how the response ended (packet A23), from enum fields only, each checked against
    its allowlist (``END_ALLOWLISTS``, packet A26), never text or error messages: for Responses, the
    end event (completed, incomplete, failed, or none when a stream ended without one),
    ``response.status``, ``incomplete_details.reason``, and ``error.code``; for Messages,
    ``stop_reason`` and an ``error`` event's ``error.type``.
    """

    def __init__(self, content_type: str | None, api: str = "messages") -> None:
        self.api = api
        self.sse = "text/event-stream" in (content_type or "").lower()
        self.usage: dict[str, Any] = {}
        self.input_transformations: list[Any] | None = None  # merged across events, see _take_transformations
        self.input_transformations_truncated = False  # True when the cap left an item out
        self._transformation_counts: dict[str, int] = {}  # summary key -> how many times it is logged
        self.end_event: str | None = None  # Responses stream: completed, incomplete, or failed
        self.ended = False  # a stream's last event was read: message_stop, or a Responses end event
        self.ending: dict[str, str] = {}  # response_status, incomplete_reason, error_code, stop_reason, error_type
        self.responses_client_calls: bool | None = None  # D40: from a Responses output list, when read
        self.chat_client_calls: bool | None = None  # Chat Completions tool_calls / finish_reason
        self.gemini_client_calls: bool | None = None  # Gemini functionCall parts
        self.call_ids: list[str] = []  # D40: ids of the client tool calls seen, in order
        self._call_id_set: set[str] = set()
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

    @property
    def overflowed(self) -> bool:
        """A JSON body went past ``MAX_JSON_RESPONSE_BYTES``: nothing will be read from it."""
        return self._overflow

    def finish(self) -> None:
        if self.sse:
            # A final event that upstream ended the stream on without its blank line is still what
            # upstream sent (its message_delta usage is the response's real usage), so it is read.
            self.feed_event(self._events.flush())
        elif self._buffer and not self._overflow:
            try:
                body = loads_bounded(bytes(self._buffer))
            except ValueError:  # invalid, or nested deeper than MAX_JSON_DEPTH: unreadable, not fatal
                body = None
            if isinstance(body, dict):
                if self.api == "responses":
                    self._take_responses_usage(body)
                    self._take_responses_end(body)
                elif self.api == "chat_completions":
                    self._take_chat_body(body)
                elif self.api == "gemini":
                    self._take_gemini_chunk(body)
                else:
                    self._take_usage(body.get("usage"))
                    self._take_transformations(body)
                    self._take_messages_end(body)
                    for block in body.get("content") if isinstance(body.get("content"), list) else []:
                        self._take_call_id(block)
            elif isinstance(body, list) and self.api == "gemini":
                for item in body:
                    if isinstance(item, dict):
                        self._take_gemini_chunk(item)
        self._buffer.clear()

    def feed_event(self, raw: bytes) -> None:
        """One raw SSE event, as split by ``SSEEventSplitter``. The stream hold's path calls this
        directly, so one splitter serves both the hold and this reader."""
        data = sse_data(raw)
        if self.api == "chat_completions" and data.strip() == b"[DONE]":
            self.ended = True
            return
        try:
            payload = loads_bounded(data) if data else None
        except ValueError:  # invalid, or nested deeper than MAX_JSON_DEPTH: unreadable, not fatal
            return
        if not isinstance(payload, dict):
            return
        self.feed_payload(payload)

    def feed_payload(self, payload: dict[str, Any]) -> None:
        """One parsed JSON event dict (shared by SSE streams and WebSocket frames)."""
        if self.api == "responses":
            if payload.get("type") in RESPONSE_END_EVENTS:
                self.end_event = payload["type"].split(".", 1)[1]
                self.ended = True
                if isinstance(payload.get("response"), dict):
                    self._take_responses_usage(payload["response"])
                    self._take_responses_end(payload["response"])
            elif payload.get("type") in ("response.output_item.added", "response.output_item.done"):
                self._take_call_id(payload.get("item"))
            elif payload.get("type") in ("error", "response.error"):
                code = payload.get("code")
                if code is None and isinstance(payload.get("error"), dict):
                    code = payload["error"].get("code")
                self._keep("error_code", code)
        elif self.api == "chat_completions":
            self._take_chat_chunk(payload)
        elif self.api == "gemini":
            self._take_gemini_chunk(payload)
        elif payload.get("type") == "message_stop":
            self.ended = True
        elif payload.get("type") == "content_block_start":
            self._take_call_id(payload.get("content_block"))
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

    def tool_calls(self) -> bool | None:
        """Whether the response holds a call the client runs (D40), for a response without a stream
        hold: from a Messages ``stop_reason`` (``tool_use`` or not), from a Responses ``output`` list
        (a client call, see ``HOSTED_CALL_TYPES``, or not), Chat Completions ``tool_calls`` /
        ``finish_reason``, or Gemini ``functionCall`` parts; else None for unknown."""
        if self.api == "responses":
            return self.responses_client_calls
        if self.api == "chat_completions":
            return self.chat_client_calls
        if self.api == "gemini":
            return self.gemini_client_calls
        reason = self.ending.get("stop_reason")
        return None if reason is None else reason == "tool_use"

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
        output = response.get("output")
        if isinstance(output, list):
            self.responses_client_calls = any(is_client_call(item) for item in output)
            for item in output:
                self._take_call_id(item)
        self._keep("response_status", response.get("status"))
        if isinstance(response.get("incomplete_details"), dict):
            self._keep("incomplete_reason", response["incomplete_details"].get("reason"))
        if isinstance(response.get("error"), dict):
            self._keep("error_code", response["error"].get("code"))

    def _take_call_id(self, item: Any) -> None:
        """D40: keep the id of a client tool call (Messages ``tool_use``, Responses ``call_id``,
        Chat Completions ``tool_calls[].id``, Gemini ``functionCall`` id)."""
        if not isinstance(item, dict):
            return
        if self.api == "responses":
            call_id = item.get("call_id", item.get("id")) if is_client_call(item) else None
        elif self.api == "chat_completions":
            call_id = item.get("id")
        elif self.api == "gemini":
            call_id = item.get("id")
        else:
            call_id = item.get("id") if item.get("type") == "tool_use" else None
        if (isinstance(call_id, str) and call_id and call_id not in self._call_id_set
                and len(self.call_ids) < MAX_CALL_IDS):
            self._call_id_set.add(call_id)
            self.call_ids.append(call_id)

    def _take_chat_body(self, body: dict[str, Any]) -> None:
        if isinstance(body.get("usage"), dict):
            self.usage = _numeric(body["usage"])
        if isinstance(body.get("error"), dict):
            self._keep("error_type", body["error"].get("type"))
            self._keep("error_code", body["error"].get("code"))
        choices = body.get("choices")
        if isinstance(choices, list):
            has_calls = False
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                self._keep("stop_reason", choice.get("finish_reason"))
                message = choice.get("message")
                if isinstance(message, dict) and isinstance(message.get("tool_calls"), list):
                    for tc in message["tool_calls"]:
                        self._take_call_id(tc)
                        if isinstance(tc, dict):
                            has_calls = True
                if choice.get("finish_reason") == "tool_calls":
                    has_calls = True
            self.chat_client_calls = has_calls

    def _take_chat_chunk(self, payload: dict[str, Any]) -> None:
        if isinstance(payload.get("usage"), dict) and payload["usage"]:
            self.usage = _numeric(payload["usage"])
        if isinstance(payload.get("error"), dict):
            self._keep("error_type", payload["error"].get("type"))
            self._keep("error_code", payload["error"].get("code"))
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("tool_calls"), list):
                    for tc in delta["tool_calls"]:
                        self._take_call_id(tc)
                    if delta["tool_calls"]:
                        self.chat_client_calls = True
                finish = choice.get("finish_reason")
                if finish is not None:
                    self._keep("stop_reason", finish)
                    if self.chat_client_calls is None:
                        self.chat_client_calls = finish == "tool_calls"

    def _take_gemini_chunk(self, payload: dict[str, Any]) -> None:
        meta = payload.get("usageMetadata") or payload.get("usage_metadata")
        if isinstance(meta, dict) and meta:
            self.usage = _numeric(meta)
        if isinstance(payload.get("error"), dict):
            self._keep("error_code", payload["error"].get("status"))
        candidates = payload.get("candidates")
        if isinstance(candidates, list):
            found_call = False
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                content = cand.get("content")
                parts = content.get("parts") if isinstance(content, dict) else None
                if isinstance(parts, list):
                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        fc = part.get("functionCall") or part.get("function_call")
                        if isinstance(fc, dict):
                            self._take_call_id(fc)
                            found_call = True
                finish = cand.get("finishReason") or cand.get("finish_reason")
                if isinstance(finish, str) and finish and finish != "FINISH_REASON_UNSPECIFIED":
                    self._keep("stop_reason", finish)
                    self.ended = True
            if found_call:
                self.gemini_client_calls = True
            elif self.gemini_client_calls is None and self.ended:
                self.gemini_client_calls = False

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
            self.usage.update({
                k: v for k, v in usage.items()
                if k in USAGE_KEYS and isinstance(v, int) and not isinstance(v, bool)
            })

    def _take_transformations(self, container: dict[str, Any]) -> None:
        """Merge an ``input_transformations`` list (at the top level or under ``usage``) into the
        response's record.

        The reader accepts the field in ``message_start``, ``message_delta``, a JSON body, and under
        ``usage`` in each. The live O6 check (PR #220) saw streams carry it only in ``message_start``,
        so no erasure has been observed; the merge is defensive, in case a later event repeats the
        field with fewer or other items. Lists merge by multiplicity: each distinct item summary is
        logged as many times as the most it appears in any one list, in first-seen order. So a single
        list is logged as given, and a report repeated in a later event is not counted twice. An empty
        later list never erases earlier items. A field seen only as empty lists records ``[]``: its
        presence shows the beta reached the API.

        At most ``MAX_INPUT_TRANSFORMATIONS`` items are logged. When the cap leaves an item out,
        ``input_transformations_truncated`` is set and the entry carries
        ``input_transformations_truncated: true``."""
        for holder in (container, container.get("usage") if isinstance(container.get("usage"), dict) else {}):
            value = holder.get("input_transformations")
            if not isinstance(value, list):
                continue
            if self.input_transformations is None:
                self.input_transformations = []
            seen: dict[str, int] = {}  # occurrences in this list so far
            for item in value:
                summary = _scalar_summary(item)
                key = json.dumps(summary, sort_keys=True)  # 1 and True are equal in Python, not here
                seen[key] = seen.get(key, 0) + 1
                if seen[key] <= self._transformation_counts.get(key, 0):
                    continue  # already logged this many times
                if len(self.input_transformations) >= MAX_INPUT_TRANSFORMATIONS:
                    self.input_transformations_truncated = True
                    return
                self._transformation_counts[key] = seen[key]
                self.input_transformations.append(summary)


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
    "stop_reason": frozenset({
        "end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal",
        "model_context_window_exceeded",
        "stop", "length", "tool_calls", "content_filter", "function_call",
        "STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "LANGUAGE", "OTHER", "BLOCKLIST",
        "PROHIBITED_CONTENT", "SPII", "MALFORMED_FUNCTION_CALL", "IMAGE_SAFETY",
    }),
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


def sse_is_comment(raw: bytes) -> bool:
    """True when one raw SSE event is only comment lines (each non-blank line starts with ``:``),
    such as CLIProxyAPI's ``: keep-alive`` heartbeat. A browser ignores it; it carries no content."""
    lines = [line for line in sse_lines(raw) if line]
    return bool(lines) and all(line.startswith(b":") for line in lines)


def _sse_payload(raw: bytes) -> dict[str, Any]:
    data = sse_data(raw)
    try:
        payload = loads_bounded(data) if data else {}
    except ValueError:  # invalid, or nested deeper than MAX_JSON_DEPTH: unreadable, not fatal
        return {}
    return payload if isinstance(payload, dict) else {}


def _takes_keyword(callback: Callable[..., Any] | None, name: str) -> bool:
    """Whether ``callback`` accepts the keyword argument ``name`` (or any keyword)."""
    if callback is None:
        return False
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is p.VAR_KEYWORD or (p.name == name and p.kind is not p.POSITIONAL_ONLY) for p in parameters)


class StreamHold:
    """The stream hold of contract PR #74 (at a64f70b). Claude Code starts an MCP tool call as soon
    as a ``tool_use`` block completes, before the stream ends (measured: 5 to 16 ms after
    ``content_block_stop``, seconds before ``message_stop``), so the per-turn recall room must be
    published before the CLI sees any tool call:

    - Messages: events stream live until the first ``tool_use`` ``content_block_start``; from there
      EVERY event is held in order until ``message_delta`` (the usage publish runs first), an
      ``error`` event, or end of stream, then released in its original order.
    - Responses: the hold starts at the first ``response.output_item.added`` whose item type is a
      client tool call (``is_client_call``: ``custom_tool_call``, ``function_call``, and others;
      hosted calls such as ``web_search_call`` stream live without activating the hold) and ends at
      ``response.completed``, ``response.incomplete``, ``response.failed``, an error, or end of stream.
    - Keepalives pass during the hold: ``ping`` events, and comment-only events such as the
      ``: keep-alive`` lines CLIProxyAPI sends when its ``streaming.keepalive-seconds`` is set (D35).
      They are not content, so content order is unchanged, and the CLI keeps seeing bytes while a
      long tool input is held.
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
        self.client_calls = False  # D40: the response holds a call the client runs (see HOSTED_CALL_TYPES)

    def _call(self, kind: str | None, raw: bytes) -> str | None:
        """``"client"`` or ``"hosted"`` for an event that starts a tool call, else None."""
        if self.api == "responses":
            if kind != "response.output_item.added":
                return None
            item = _sse_payload(raw).get("item")
            item_type = str(item.get("type", "")) if isinstance(item, dict) else ""
            if not item_type.endswith("_call"):
                return None
            return "client" if is_client_call(item) else "hosted"
        if self.api == "chat_completions":
            choices = _sse_payload(raw).get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    delta = choice.get("delta") if isinstance(choice, dict) else None
                    if isinstance(delta, dict) and delta.get("tool_calls"):
                        return "client"
            return None
        if self.api == "gemini":
            candidates = _sse_payload(raw).get("candidates")
            if isinstance(candidates, list):
                for cand in candidates:
                    content = cand.get("content") if isinstance(cand, dict) else None
                    parts = content.get("parts") if isinstance(content, dict) else None
                    if isinstance(parts, list) and any(
                        isinstance(p, dict) and isinstance(p.get("functionCall") or p.get("function_call"), dict)
                        for p in parts
                    ):
                        return "client"
            return None
        if kind != "content_block_start":
            return None
        block = _sse_payload(raw).get("content_block")
        return "client" if isinstance(block, dict) and block.get("type") == "tool_use" else None

    def _is_trigger(self, kind: str | None, raw: bytes = b"") -> bool:
        if self.api == "messages":
            return kind == "message_delta"
        if self.api == "responses":
            return kind in RESPONSE_END_EVENTS
        if self.api == "chat_completions":
            if sse_data(raw).strip() == b"[DONE]":
                return True
            payload = _sse_payload(raw)
            usage = payload.get("usage")
            if not (isinstance(usage, dict) and bool(usage)):
                return False
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                return True
            return any(isinstance(c, dict) and c.get("finish_reason") is not None for c in choices)
        if self.api == "gemini":
            payload = _sse_payload(raw)
            meta = payload.get("usageMetadata") or payload.get("usage_metadata")
            has_usage = isinstance(meta, dict) and bool(meta)
            cands = payload.get("candidates")
            has_finish = isinstance(cands, list) and any(
                isinstance(c, dict)
                and isinstance((fr := c.get("finishReason") or c.get("finish_reason")), str)
                and bool(fr) and fr != "FINISH_REASON_UNSPECIFIED"
                for c in cands
            )
            return has_usage and has_finish
        return False

    def _is_error(self, kind: str | None, raw: bytes = b"") -> bool:
        if kind in ("error", "response.error"):
            return True
        if self.api in ("chat_completions", "gemini"):
            return isinstance(_sse_payload(raw).get("error"), dict)
        return False

    def feed(self, raw: bytes) -> list[bytes]:
        """One complete raw SSE event in; the raw events to write now, in order."""
        kind = sse_event_type(raw)
        if self._is_trigger(kind, raw):
            if not self.triggered:
                # A Gemini or Chat Completions chunk can carry both a tool call and finishReason/usage
                # in the same final SSE event: record client_calls before on_trigger() observes usage.
                if not self.client_calls and self.api in ("chat_completions", "gemini"):
                    call = self._call(kind, raw)
                    if call == "client":
                        self.client_calls = True
                self.triggered = True
                self.on_trigger()  # publish the real-usage room before anything held reaches the CLI
            return self._release() + [raw]
        if self._is_error(kind, raw):
            return self._release_untriggered() + [raw]
        # A comment passes at once only when it ends with its line ending: a tail without one (a stream
        # cut after ": keep-alive") is held and released last, so no held event is glued onto it.
        if kind == "ping" or (sse_is_comment(raw) and raw.endswith((b"\n", b"\r"))):
            return [raw]
        if not self.client_calls and (kind in ("content_block_start", "response.output_item.added")
                                      or self.api in ("chat_completions", "gemini")):
            call = self._call(kind, raw)
            if call == "client":
                self.client_calls = True
                if not self.triggered:
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


def _set_nodelay(sock: socket.socket | None) -> None:
    """Disable Nagle's algorithm so small HTTP/SSE writes do not stall on Linux's 40 ms delayed ACK."""
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


class _UpstreamHTTP(http.client.HTTPConnection):
    """HTTPConnection with TCP_NODELAY set on connect."""

    def connect(self) -> None:
        super().connect()
        _set_nodelay(self.sock)


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
        _set_nodelay(self.sock)
        self.phase = "tls"
        self.sock = self.tls_context.wrap_socket(self.sock, server_hostname=self.host)


def _is_socket_dirty(conn: http.client.HTTPConnection) -> bool:
    """True when an idle pooled connection is closed, errored, or has unread/EOF bytes waiting."""
    sock = conn.sock
    if sock is None:
        return True
    try:
        if isinstance(sock, ssl.SSLSocket) and sock.pending() > 0:
            return True
        poller = select.poll()
        poller.register(sock, select.POLLIN)
        return bool(poller.poll(0))
    except (OSError, ValueError):
        return True


class _UpstreamPool:
    """Thread-safe LIFO pool of idle HTTP/HTTPS upstream keep-alive connections."""

    def __init__(self, *, max_idle: int = UPSTREAM_POOL_MAX_IDLE,
                 idle_ttl_s: float = UPSTREAM_POOL_IDLE_TTL_S) -> None:
        self._max_idle = max_idle
        self._idle_ttl_s = idle_ttl_s
        self._lock = threading.Lock()
        self._idle: list[tuple[http.client.HTTPConnection, float]] = []
        self._closed = False

    def acquire(self) -> http.client.HTTPConnection | None:
        """Return the most recently used clean idle connection, or None if none is available."""
        while True:
            with self._lock:
                if self._closed or not self._idle:
                    return None
                conn, since = self._idle.pop()
            if time.monotonic() - since > self._idle_ttl_s or _is_socket_dirty(conn):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            return conn

    def release(self, conn: http.client.HTTPConnection) -> None:
        """Return a clean keep-alive connection to the pool, evicting the oldest if full."""
        if _is_socket_dirty(conn):
            try:
                conn.close()
            except OSError:
                pass
            return
        evicted: http.client.HTTPConnection | None = None
        with self._lock:
            if self._closed or self._max_idle <= 0:
                evicted = conn
            else:
                self._idle.append((conn, time.monotonic()))
                if len(self._idle) > self._max_idle:
                    evicted = self._idle.pop(0)[0]
        if evicted is not None:
            try:
                evicted.close()
            except OSError:
                pass

    def close_all(self) -> None:
        """Close every idle connection and prevent further pooling."""
        with self._lock:
            self._closed = True
            conns, self._idle = [conn for conn, _ in self._idle], []
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass


def _shut(conn: http.client.HTTPConnection) -> None:
    """End an upstream connection another thread may be blocked on: its read returns at once.
    The plain socket's shutdown, not ``SSLSocket.shutdown``, which drops TLS state that read uses."""
    sock = conn.sock
    if sock is not None:
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass


class _ClientWatch:
    """Watches the client's socket while its request is in flight (D53). When the client goes away
    (Esc closes the connection), the upstream connection is shut at once, so upstream stops
    generating. Otherwise the proxy notices only when a write to the client fails, and nothing is
    written while it waits for the response head or holds a tool call: upstream then ran, and
    billed, to the end of a response nobody reads. The watch only peeks, so a byte the client sends
    (a pipelined request) ends it and the request is served as before. ``close`` wakes the watch
    thread via an internal pipe, joins it before the handler can close the client socket (preventing
    fd-reuse races), and closes any attached upstream connection."""

    def __init__(self, client: socket.socket) -> None:
        self.client = client
        self.gone = False
        self._upstream: http.client.HTTPConnection | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._closed = False
        self._wake_r: int | None = None
        self._wake_w: int | None = None
        try:
            wake_r, wake_w = os.pipe()
            try:
                os.set_blocking(wake_r, False)
                os.set_blocking(wake_w, False)
            except OSError:
                for fd in (wake_r, wake_w):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                raise
            self._wake_r, self._wake_w = wake_r, wake_w
        except OSError:
            self._wake_r = self._wake_w = None
        self._thread = threading.Thread(target=self._run, name="contexto-client-watch", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._close_wake_fds()
            raise

    def _close_wake_fds(self) -> None:
        with self._lock:
            fds = (self._wake_r, self._wake_w)
            self._wake_r = self._wake_w = None
        for fd in fds:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def attach(self, conn: http.client.HTTPConnection) -> None:
        """The connected upstream connection to shut if the client goes away (or has already)."""
        with self._lock:
            self._upstream = conn
            if self.gone:
                _shut(conn)

    def detach(self) -> http.client.HTTPConnection | None:
        """Atomically detach and return the upstream connection for pooling if the client is still
        present, else return None so ``close`` shuts/closes it."""
        with self._lock:
            if self.gone:
                return None
            conn, self._upstream = self._upstream, None
            return conn

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._done.set()
            if self._wake_w is not None:
                try:
                    os.write(self._wake_w, b"\x00")
                except OSError:
                    pass
        self._thread.join(timeout=1.0)
        with self._lock:
            upstream, self._upstream = self._upstream, None
            alive = self._thread.is_alive()
            fds = (None, None) if alive else (self._wake_r, self._wake_w)
            if not alive:
                self._wake_r = self._wake_w = None
        if upstream is not None:
            upstream.close()
        for fd in fds:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _run(self) -> None:
        poller = select.poll()  # not select.select, which cannot watch a descriptor above 1023
        try:
            try:
                client_fd = self.client.fileno()
                if client_fd < 0:
                    return
                poller.register(client_fd, select.POLLIN)  # a close or a reset is reported as readable
                if self._wake_r is not None:
                    poller.register(self._wake_r, select.POLLIN)
            except (OSError, ValueError):  # the client's socket is already closed: nothing to watch
                return
            poll_ms = 5000 if self._wake_r is not None else int(CLIENT_WATCH_INTERVAL_S * 1000)
            peek_flags = socket.MSG_PEEK | getattr(socket, "MSG_DONTWAIT", 0)
            while not self._done.is_set():
                try:
                    events = poller.poll(poll_ms)
                except OSError:
                    break
                if not events:
                    continue
                if self._done.is_set() or any(fd == self._wake_r for fd, _ in events):
                    return
                try:
                    if self.client.recv(1, peek_flags):
                        return  # the client sent more, so it is still there
                except BlockingIOError:
                    continue  # spurious wakeup: keep watching
                except OSError:  # a reset, or a socket the handler has closed
                    pass
                if self._done.is_set():
                    return  # the request is over: a close now is the client ending a kept-alive connection
                with self._lock:
                    self.gone = True
                    if self._upstream is not None:
                        _shut(self._upstream)
                return
        finally:
            if self._closed:
                self._close_wake_fds()


class _LoopbackServer(ThreadingHTTPServer):
    """``HTTPServer.server_bind`` names the server with ``socket.getfqdn(host)``, a reverse lookup
    that took 35 seconds per process on a GitHub macOS runner and delayed every launch by as much.
    Nothing reads the name, so the bound address stands in for it and no lookup is made."""

    request_queue_size = LISTEN_BACKLOG

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port

    def get_request(self) -> tuple[socket.socket, Any]:
        conn, addr = super().get_request()
        _set_nodelay(conn)
        return conn, addr


class ContextoProxy:
    def __init__(self, upstream: str, log: SessionLog, transform: Transform = identity_transform, *,
                 observe_usage: ObserveUsage | None = None, observe_thinking: bool = False,
                 upstream_source: str | None = None, affinity_key: str | None = None,
                 on_tool_calls: Callable[[tuple[str, ...]], None] | None = None,
                 strip_hint_headers: bool = False, allowed_hosts: Iterable[str] = (),
                 allow_local_origin: bool = False, transform_lock: threading.Lock | None = None,
                 allow_websocket: bool = False) -> None:
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("upstream must be an http(s) base URL")
        if has_lone_surrogate(upstream):  # no request line can carry it
            raise ValueError("upstream holds a lone surrogate")
        self.scheme = parts.scheme
        self.upstream_host = parts.hostname
        # The X-Session-Affinity value added to each request, or None (D36): never for a vendor host.
        self.affinity_header = affinity_key if affinity_key and not is_vendor_host(parts.hostname) else None
        self.upstream_port = parts.port or (443 if parts.scheme == "https" else 80)
        self.upstream_netloc = parts.netloc.rsplit("@", 1)[-1]
        self.base_path = _normalize_base_path(parts.path)
        self.session_log = log
        self.transform = transform
        self.observe_usage = observe_usage
        # Called with the ids of a response's client tool calls as they are read, before the bytes that
        # carry them are written to the client (``_Handler._relay``); None for no hook.
        self.on_tool_calls = on_tool_calls
        self.observe_thinking = observe_thinking
        self.upstream_source = upstream_source
        # D70: leave Claude Code's gated hint headers out of what is sent upstream (module docstring).
        self.strip_hint_headers = strip_hint_headers
        self.allowed_hosts: frozenset[str] = frozenset(normalize_allowed_host(h) for h in allowed_hosts)
        self.allow_local_origin = bool(allow_local_origin)
        # Opt-in RFC 6455 WebSocket relay on Responses endpoints (contexto.ws); HTTP remains default.
        self.allow_websocket = allow_websocket
        self.transform_lock = transform_lock or threading.Lock()
        self._io_lock = threading.Lock()
        self._ssl_lock = threading.Lock()
        self._ssl_context: ssl.SSLContext | None = None
        self._pool = _UpstreamPool()
        self._observe_sig_cache: tuple[ObserveUsage | None, bool, bool, bool] = (
            observe_usage,
            _takes_keyword(observe_usage, "tool_calls"),
            _takes_keyword(observe_usage, "tool_call_ids"),
            _takes_keyword(observe_usage, "headers"),
        )
        self.last_epoch: Any = None  # the engine's epoch from its last successful transform
        self._in_flight = 0  # requests being handled; idle keep-alive connections are not counted
        self._idle = threading.Condition()
        self._ws_sockets: set[Any] = set()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def register_ws_sockets(self, *socks: Any) -> None:
        with self._idle:
            self._ws_sockets.update(socks)

    def unregister_ws_sockets(self, *socks: Any) -> None:
        with self._idle:
            for sock in socks:
                self._ws_sockets.discard(sock)

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

        self._server = _LoopbackServer((LISTEN_HOST, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="contexto-proxy", daemon=True)
        self._thread.start()
        self.log({"event": "proxy_start", "port": self.port, "upstream_source": self.upstream_source,
                  "observe_thinking": self.observe_thinking, **self._store_pages()})
        return self.port

    def _store_pages(self) -> dict[str, int]:
        """D64: ``{"pages": n}``, the page files the session's store holds now, for the start and stop
        lines: ``contexto stats`` takes the pages a run added from the two, because the engine's
        ``page_out`` counts a turn again each time a launch pages it again. On these lines, not the
        launcher's ``launch`` and ``exit``, so ``contexto proxy`` and both launchers record it in one
        place. Empty when the log is not a session store's or the pages cannot be listed. The files are
        counted by name and never opened, and a failure here never stops the proxy."""
        try:
            from contexto.stats import count_pages  # here, so the proxy module loads no reader it may not need

            store = getattr(self.session_log, "store", self.session_log)  # the launcher wraps the store
            pages_dir = getattr(store, "pages_dir", None)
            count = count_pages(pages_dir) if pages_dir is not None else None
        except Exception:
            count = None
        return {} if count is None else {"pages": count}

    def stop(self) -> None:
        """Stop accepting connections, then wait up to STOP_DRAIN_S for in-flight requests to finish
        and log their entries (handler threads are daemons, so a process exit would cut them off)."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            with self._idle:
                active_ws = list(self._ws_sockets)
            for sock in active_ws:
                try:
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)
                except OSError:
                    pass
            deadline = time.monotonic() + STOP_DRAIN_S
            with self._idle:
                try:
                    while self._in_flight and (left := deadline - time.monotonic()) > 0:
                        self._idle.wait(left)
                except KeyboardInterrupt:
                    pass  # a second Ctrl+C ends the wait early, with no traceback; the rest is logged below
                in_flight = self._in_flight
            if in_flight:
                self.log({"event": "proxy_stop_incomplete", "in_flight": in_flight})
            self._pool.close_all()
            self.log({"event": "proxy_stop", **self._store_pages()})
        else:
            self._pool.close_all()

    def serve_forever(self, port: int = 0) -> None:
        """Block until interrupted, starting the server first if needed."""
        if self._server is None:
            self.start(port)
        try:
            assert self._thread is not None
            self._thread.join()
        except KeyboardInterrupt:
            self.stop()

    def _get_ssl_context(self) -> ssl.SSLContext:
        ctx = self._ssl_context
        if ctx is not None:
            return ctx
        with self._ssl_lock:
            if self._ssl_context is None:
                self._ssl_context = ssl.create_default_context()
            return self._ssl_context

    def connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return _UpstreamHTTPS(self.upstream_host, self.upstream_port,
                                  timeout=UPSTREAM_TIMEOUT_S, context=self._get_ssl_context())
        return _UpstreamHTTP(self.upstream_host, self.upstream_port, timeout=UPSTREAM_TIMEOUT_S)

    def log(self, event: dict[str, Any]) -> None:
        try:
            self.session_log.log({"ts": utc_now(), **event})
        except Exception:  # a broken log must never break the user's session
            pass


def _no_body_response(status: int, method: str) -> bool:
    """RFC 9110 §6.4.1 / §8.6: responses that never carry a message body."""
    return method == "HEAD" or status in (204, 304) or 100 <= status < 200


def _request_item_count(request: dict[str, Any], endpoint: str) -> int:
    """Count of messages/items in a parsed request body for ``endpoint``'s dialect."""
    dialect = registry_dialect_for(endpoint)
    field = dialect.field if dialect is not None else "messages"
    items = request.get(field)
    return len(items) if isinstance(items, list) else 0


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

    def _read_exact(self, size: int) -> bytes:
        data = self.rfile.read(size)
        if len(data) != size:
            raise ValueError("unexpected EOF in request body")
        return data

    def _read_body(self, limit: int | None = None) -> bytes:
        cap = MAX_REQUEST_BODY_BYTES if limit is None else limit
        te_values = self.headers.get_all("Transfer-Encoding") or []
        cl_values = self.headers.get_all("Content-Length") or []
        if te_values:
            if cl_values:
                raise ValueError("Transfer-Encoding and Content-Length both present")
            te_codings = _parse_content_encodings(te_values)
            if te_codings != ["chunked"]:
                raise ValueError("unsupported Transfer-Encoding")
            body = bytearray()
            while True:
                line = self.rfile.readline(65536)
                if not line or not line.endswith(b"\n"):
                    raise ValueError("unexpected EOF in chunked body")
                raw_size = line.split(b";", 1)[0].strip()
                if not raw_size or not _HEX_CHUNK_SIZE.fullmatch(raw_size):
                    raise ValueError("invalid chunk size")
                size = int(raw_size, 16)
                if size == 0:
                    trailer_lines = 0
                    trailer_bytes = 0
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b"\r\n", b"\n"):
                            return bytes(body)
                        if not trailer or not trailer.endswith(b"\n"):
                            raise ValueError("unexpected EOF in chunked trailer")
                        trailer_lines += 1
                        trailer_bytes += len(trailer)
                        if trailer_lines > MAX_CHUNK_TRAILERS or trailer_bytes > MAX_CHUNK_TRAILER_BYTES:
                            raise ValueError("chunked trailers too large")
                if size > cap - len(body):
                    raise BodyTooLargeError(f"request body exceeds {cap:,} bytes")
                body += self._read_exact(size)
                crlf = self.rfile.readline(65536)
                if crlf not in (b"\r\n", b"\n"):
                    raise ValueError("malformed chunk terminator")
        if not cl_values:
            return b""
        length = _parse_content_length(cl_values)
        if length is None:
            raise ValueError("invalid or conflicting Content-Length")
        if length > cap:
            raise BodyTooLargeError(f"request body exceeds {cap:,} bytes")
        return self._read_exact(length) if length else b""

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
        self._raw: tuple[bytes, HeaderList] | None = None
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

    def _host_allowed(self, host_value: str) -> bool:
        lower = host_value.strip().lower()
        if LOCAL_HOST_HEADER.fullmatch(lower):
            return True
        owner = getattr(self, "owner", None)
        allowed = getattr(owner, "allowed_hosts", frozenset())
        if not allowed:
            return False
        host_only = _host_without_port(lower)
        # Validate optional port when matching an allowed host
        if host_only != lower:
            port_part = lower[len(host_only):]
            if not (port_part.startswith(":") and port_part[1:].isdigit() and 1 <= len(port_part) - 1 <= 5):
                return False
        return host_only in allowed

    def _refusal(self) -> str | None:
        """Why the request is not from the local CLI (``"origin"`` or ``"host"``), or None (D53).
        Claude Code and Codex send one Host, the proxy's own address, and no Origin. A browser sends
        Origin on every POST and preflight, a no-cors text/plain POST included, and a DNS-rebound
        page sends its own name as Host, so neither can drive the proxy or write to the store. The
        name is checked, not the port (D59): a browser cannot put another port in Host on a
        connection to this socket, and an SSH or container forward to it sends its own port."""
        origins = self.headers.get_all("Origin") or []
        if len(origins) > 1:
            return "origin"
        if len(origins) == 1:
            owner = getattr(self, "owner", None)
            if not (getattr(owner, "allow_local_origin", False)
                    and is_local_origin(origins[0], getattr(owner, "allowed_hosts", frozenset()))):
                return "origin"
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or not self._host_allowed(hosts[0]):
            return "host"
        return None

    def _handle_request(self) -> None:
        started = time.monotonic()
        path_only = self.path.split("?", 1)[0]
        if (reason := self._refusal()) is not None:
            self.owner.log({"event": "request_refused", "method": self.command, "path": path_only, "reason": reason})
            try:
                self._read_body()  # closing on unread bytes sends a reset, which can discard the 403
            except (ValueError, OSError):
                pass
            self._send_local_error(403, REFUSED_MESSAGE, "permission_error")
            self.close_connection = True
            return
        upgrade_tokens = {tok.strip().lower() for val in (self.headers.get_all("Upgrade") or [])
                          for tok in val.split(",") if tok.strip()}
        if "websocket" in upgrade_tokens:
            if self.owner.allow_websocket and path_only in RESPONSES_PATHS:
                from contexto.ws import handle_websocket

                handle_websocket(self, self.owner, path_only)
                return
            # Not tunneled unless opted in on a Responses endpoint: an uninspected WebSocket would
            # bypass the bound. The launcher keeps Codex on HTTP by default.
            self.owner.log({"event": "websocket_rejected", "path": path_only})
            self._send_local_error(501, "contexto proxy: WebSocket is not supported; use HTTP",
                                   "invalid_request_error")
            self.close_connection = True
            return
        try:
            body = self._read_body()
        except BodyTooLargeError:
            self.owner.log({"event": "request", "method": self.command, "path": path_only, "status": 413,
                            "error": "request body too large"})
            self.close_connection = True
            self._send_local_error(413, "contexto proxy: request body too large", "request_too_large", close=True)
            return
        except ValueError:
            self.owner.log({"event": "request", "method": self.command, "path": path_only, "status": 400,
                            "error": "malformed request framing"})
            self.close_connection = True
            self._send_local_error(400, "contexto proxy: malformed request framing",
                                   "invalid_request_error", close=True)
            return
        headers: HeaderList = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP]
        affinity = self.owner.affinity_header
        if affinity and not any(k.lower() == AFFINITY_HEADER.lower() for k, _ in headers):
            headers.append((AFFINITY_HEADER, affinity))
        self._raw = (body, list(headers))
        entry: dict[str, Any] = {"event": "request", "method": self.command, "path": path_only,
                                 "original_request_bytes": len(body)}
        original = None
        api, canonical_endpoint = classify_path(path_only) if self.command == "POST" else (None, None)
        if api is not None and canonical_endpoint is not None:
            entry["api"] = api
            # The proxy must read these responses (usage, and the stream hold of PR #74), so it asks
            # upstream for an unencoded body instead of forwarding the CLI's Accept-Encoding.
            headers = _remove_header(headers, "accept-encoding")
            headers.append(("Accept-Encoding", "identity"))
            codings = _parse_content_encodings(self.headers.get_all("Content-Encoding") or [])
            if any(c != "identity" for c in codings):
                encoding = ", ".join(codings)
                # Contract rule 15 (PR #58): decode, transform as usual, forward uncompressed.
                try:
                    decoded = decode_request_body(body, encoding, MAX_DECODED_BODY_BYTES)
                except EncodedBodyError as exc:
                    self._reject_encoded(encoding, exc.reason, path_only, entry["api"])
                    return
                entry.update(request_content_encoding=encoding, wire_request_bytes=len(body),
                             original_request_bytes=len(decoded))
                body = decoded
                headers = _remove_header(headers, "content-encoding")
                self._raw = (body, list(headers))
            body, headers, original = self._transform(body, headers, entry, canonical_endpoint)
        entry["request_bytes"] = len(body)
        self._forward(body, headers, entry, started, original)

    def _transform(self, body: bytes, headers: HeaderList | Headers, entry: dict[str, Any],
                   endpoint: str) -> tuple[bytes, HeaderList, dict[str, Any] | None]:
        headers = list(headers.items()) if isinstance(headers, dict) else headers
        owner = self.owner
        try:
            request = self._parse(body)
        except JSONTooDeepError:
            # Rule 18: past MAX_JSON_DEPTH not every Python can serialize or walk the request, so it
            # is forwarded untransformed (decoded, if it arrived compressed: rule 15) and logged.
            entry["transform_skipped"] = f"request body nests deeper than {MAX_JSON_DEPTH} levels"
            owner.log({"event": "request_too_deep", "level": "warning", "endpoint": endpoint,
                       "limit": MAX_JSON_DEPTH, "epoch": owner.last_epoch})
            return body, headers, None
        except ValueError:
            entry["transform_skipped"] = "request body is not JSON"
            return body, headers, None
        if not isinstance(request, dict):
            entry["transform_skipped"] = "request body is not an object"
            return body, headers, None
        raw_model = request.get("model") if request.get("model") is not None else model_from_endpoint(endpoint)
        entry["model"] = _scalar_summary(raw_model)  # a client value: never logged raw
        entry["stream"] = bool(request.get("stream")) or endpoint.endswith(":streamGenerateContent")
        entry["original_message_count"] = _request_item_count(request, endpoint)
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
        prime = getattr(owner.transform, "prime", None)
        if callable(prime):
            try:
                prime(request)  # D60: count the request's PDFs before taking the lock that every session waits on
            except Exception:  # an optimisation only: the transform counts them under the lock instead
                pass
        header_view = _headers_to_dict(headers)
        try:
            if owner.transform is identity_transform:
                new_request, new_headers_dict, events = identity_transform(request, dict(header_view), endpoint)
            else:
                transform_headers = dict(header_view)
                with owner.transform_lock:
                    new_request, new_headers_dict, events = owner.transform(request, transform_headers, endpoint)
            if not isinstance(new_request, dict) or not isinstance(new_headers_dict, dict):
                raise TypeError("transform must return (dict, dict, list)")
        except Exception as exc:
            # Contract rule 14, genuine bugs: never alter the request on a failed transform, even
            # one that mutated its argument before raising. Forward the CLI's own bytes and headers
            # and log transform_error at error level (acceptance counts it as a failure).
            entry["transform_error"] = type(exc).__name__
            self._log_transform_error(type(exc).__name__, "transform", endpoint)
            entry["message_count"] = entry["original_message_count"]
            if owner.observe_thinking:
                headers = _merge_headers(headers, header_view, add_beta(header_view, THINKING_BINDING_BETA))
            return body, headers, original
        flush = getattr(owner.transform, "flush_deferred", None)
        if callable(flush):
            try:
                with owner._io_lock:
                    flush()
            except Exception:
                pass
        entry["transform_events"] = [_scalar_summary(e) for e in events if isinstance(e, dict)]
        entry["over_bound"] = any(bool(e.get("over_bound")) for e in events if isinstance(e, dict))
        epochs = [e["epoch"] for e in events if isinstance(e, dict) and "epoch" in e]
        if epochs:
            entry["epoch"] = owner.last_epoch = _scalar_summary(epochs[-1])
        if owner.observe_thinking:
            new_headers_dict = add_beta(new_headers_dict, THINKING_BINDING_BETA)
        new_headers = _merge_headers(headers, header_view, new_headers_dict)
        entry["message_count"] = _request_item_count(new_request, endpoint)
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

    def _parse(self, body: bytes) -> Any:
        """The request body as JSON (ValueError when it is not, JSONTooDeepError past the depth limit).
        Its own method so that ``contexto serve``, which reads the body to route the request, parses
        it once (``contexto.serve``)."""
        return loads_bounded(body)

    def _forward(self, body: bytes, headers: HeaderList, entry: dict[str, Any], started: float,
                 original: dict[str, Any] | None) -> None:
        watch = _ClientWatch(self.connection)
        try:
            self._relay(body, headers, entry, started, original, watch)
        finally:
            watch.close()  # also closes upstream on a path that raised before the relay's own close

    def _relay(self, body: bytes, headers: HeaderList, entry: dict[str, Any], started: float,
               original: dict[str, Any] | None, watch: _ClientWatch) -> None:
        owner = self.owner
        # Contract rule 8 (PR #95): each upstream step is named, so a failure says where it happened:
        # "connect" (TCP), "tls", "send" (headers and body), "wait_headers" (only once endheaders has
        # returned, so the whole body is written). A failure before that point is retried once, byte
        # for byte, on a new connection: upstream cannot act on a request it never fully received,
        # unless it has already answered it (D53: an early 413 is forwarded, not retried).
        # From wait_headers on, upstream may be running it, so that failure is a 502 and the CLI decides.
        # Stale keep-alive sockets taken from the pool are transparently replaced with a fresh connection
        # per RFC 9112 §9.8 when they drop before send completes, or in wait_headers for idempotent methods.
        retried = False
        pool_retried = False
        early_response = False
        allow_pool = True
        outgoing_headers = _strip_hint_header_list(headers) if owner.strip_hint_headers else headers
        while True:
            conn = owner._pool.acquire() if allow_pool else None
            reused = conn is not None
            if conn is None:
                conn = owner.connection()
            phase, phase_started = ("send" if reused else "connect"), time.monotonic()
            try:
                if not reused:
                    conn.connect()
                watch.attach(conn)
                phase, phase_started = "send", time.monotonic()
                path_only = urlsplit(self.path).path
                if (owner.base_path == "/v1" and path_only.startswith("/v1/projects/")
                        and classify_path(path_only)[0] == "messages"):
                    upstream_path = self.path
                elif (owner.base_path == "/anthropic"
                      and path_only in (FOUNDRY_MESSAGES_PATHS | FOUNDRY_COUNT_TOKENS_PATHS)):
                    upstream_path = self.path
                else:
                    upstream_path = owner.base_path + self.path
                conn.putrequest(self.command, upstream_path, skip_host=True,
                                skip_accept_encoding=True)
                conn.putheader("Host", owner.upstream_netloc)
                # D70: the one place a request's headers are written upstream, so this also covers
                # the CLI's own request on every fail-open path.
                for name, value in outgoing_headers:
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
                if phase == "connect":
                    phase = getattr(conn, "phase", phase)
                if watch.gone:  # the client left and upstream was shut for it: nothing to answer or retry
                    conn.close()
                    entry.update(client_aborted=True, upstream_phase=phase, duration_ms=_ms(started))
                    if retried:
                        entry["upstream_retried"] = True
                    self.close_connection = True
                    owner.log(entry)
                    return
                if phase == "send" and isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                    early = self._early_response(conn)
                    if early is not None:
                        response = early
                        early_response = True
                        entry["upstream_answered_early"] = type(exc).__name__
                        break
                conn.close()
                if reused and not pool_retried and isinstance(exc, _STALE_POOL_ERRORS) and (
                    phase == "send" or self.command in IDEMPOTENT_METHODS
                ):
                    pool_retried = True
                    allow_pool = False
                    continue  # stale keep-alive socket closed by upstream: open fresh connection
                failure = {"upstream_phase": phase, "phase_ms": _ms(phase_started),
                           "errno": getattr(exc, "errno", None)}
                if not retried and phase in RETRYABLE_PHASES:
                    retried = True
                    allow_pool = False
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
        resp_encodings = response.headers.get_all("Content-Encoding") or []
        decoder = _SideDecoder(", ".join(resp_encodings) if resp_encodings else None)
        raw_cl_values = response.headers.get_all("Content-Length") or []
        valid_cl = _parse_content_length(raw_cl_values)
        no_body = _no_body_response(response.status, self.command)
        interim_1xx = 100 <= response.status < 200
        chunked = (not no_body) and (bool(response.chunked) or valid_cl is None or response.length != valid_cl)
        expected = None if (chunked or no_body) else valid_cl
        response_bytes = 0
        # The client must not see the response complete before the engine has its usage and the
        # session log has the entry: Claude Code sends its next request right away, and the engine
        # sizes it from the last real usage (rule 4). So the chunk terminator, or the last chunk of
        # a Content-Length response, is written only after usage feedback and logging. A JSON body is
        # read only once it has ended (``UsageReader.finish``), so in either framing what completes it
        # is held too (``hold_json``) and written after its tool-call ids are handed on (``announce``).
        held = b""
        json_body = not reader.sse
        identity = decoder.encoding in ("", "identity")
        relay_ok = True
        observed = False
        # D59: the client has the stream's last event, so a close after it (before the terminator) is
        # not an abort: a CLI may return on message_stop or response.completed.
        delivered_end = False
        _, feedback_endpoint = classify_path(entry["path"])
        feedback = (original is not None and is_usage_feedback_path(entry["path"])
                    and 200 <= response.status < 300)

        def observe() -> None:
            nonlocal observed
            if feedback and not observed and reader.usage and feedback_endpoint is not None:
                observed = True
                # Packet A2e: the returned events go on this request's entry, logged once with it.
                usage_events = self._observe_usage(feedback_endpoint, reader.usage, original,
                                                   hold.client_calls if hold is not None else reader.tool_calls(),
                                                   tuple(reader.call_ids), headers)
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
        announced = 0

        def announce() -> None:
            """Hand the owner's ``on_tool_calls`` hook the client tool-call ids read since the last
            call. It runs after each read of the side copy and before the write that follows, so the
            hook has an id before the client has the bytes that carry it, whether the response is held,
            relayed chunk by chunk or a JSON body, and whether or not it has usage."""
            nonlocal announced
            hook = owner.on_tool_calls
            if hook is None or len(reader.call_ids) == announced:
                return
            new, announced = tuple(reader.call_ids[announced:]), len(reader.call_ids)
            try:
                hook(new)
            except Exception as exc:
                owner.log({"event": "tool_calls_hook_error", "error": type(exc).__name__})

        def read_upstream() -> bytes:
            try:
                return response.read1(65536)
            except (OSError, http.client.HTTPException) as exc:  # IncompleteRead is not an OSError
                raise _UpstreamReadError from exc

        json_started = False  # an identity JSON body has sent a byte that is not JSON whitespace

        def hold_json(held: bytes, chunk: bytes) -> tuple[bytes, bytes]:
            """(bytes to write now, bytes to hold) for one more chunk of a JSON body. The last chunk is
            held; for an identity body, so is every chunk after the last one with a byte that is not
            JSON whitespace, so the client never has the body's closing bracket before the end. A body
            past the reader's limit registers nothing, and is relayed as a Content-Length body is.
            Whitespace before an identity body's first value completes nothing and goes at once: a
            gateway that keeps a slow request alive with it reaches the client as before the hold."""
            nonlocal json_started
            blank = not chunk.strip(b" \t\r\n")
            if identity and not json_started:
                if blank:
                    return held + chunk, b""
                json_started = True
            if identity and held and blank and not reader.overflowed:
                return b"", held + chunk
            return held, chunk

        def write_events(events: list[bytes]) -> None:
            nonlocal delivered_end
            for raw in events:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
            if events:
                self.wfile.flush()
            delivered_end = reader.ended and (hold is None or not hold.held)

        try:
            # The head is written inside the relay's try, so a client gone before it arrives is
            # logged, and upstream closed, like one gone mid-stream (D53).
            self._response_started = True
            self.send_response_only(response.status, response.reason)
            for name, value in response.getheaders():
                if name.lower() not in HOP_BY_HOP:
                    self.send_header(name, value)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            elif valid_cl is not None and not interim_1xx and response.status != 204:
                self.send_header("Content-Length", str(valid_cl))
            if interim_1xx:
                self.close_connection = True
            self.end_headers()
            if no_body:
                try:
                    response.read()  # marks http.client response closed so keep-alive socket can be pooled
                except (OSError, http.client.HTTPException) as exc:
                    raise _UpstreamReadError from exc
            else:
                while chunk := read_upstream():
                    response_bytes += len(chunk)
                    if hold is not None:
                        for raw in splitter.feed(chunk):
                            reader.feed_event(raw)  # usage is read before the hold can trigger on it
                            announce()
                            write_events(hold.feed(raw))
                        continue
                    reader.feed(decoder.decode(chunk))
                    announce()
                    if json_body:
                        pending, held = hold_json(held, chunk)
                        if pending:
                            self.wfile.write(b"%x\r\n%s\r\n" % (len(pending), pending) if chunked else pending)
                    elif chunked:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    else:
                        if held:
                            self.wfile.write(held)
                        held = chunk
                    self.wfile.flush()
                    delivered_end = chunked and reader.ended
            if hold is not None:
                if tail := splitter.flush():  # a final event without its blank line (see SSEEventSplitter)
                    reader.feed_event(tail)
                    announce()
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
            if not delivered_end:
                entry["client_aborted"] = True
            self.close_connection = True
            relay_ok = False
        except OSError as exc:
            entry["error"] = f"relay: {type(exc).__name__}"
            self.close_connection = True
            relay_ok = False
        finally:
            if watch.gone:  # the client left and upstream was shut for it: whatever failed after is that
                entry.pop("error", None)
                if not delivered_end:
                    entry["client_aborted"] = True
                self.close_connection = True
                relay_ok = False
            pooled = None
            if relay_ok and not early_response and not interim_1xx and not response.will_close and response.isclosed():
                pooled = watch.detach()
            if pooled is not None:
                owner._pool.release(pooled)
            else:
                conn.close()
            if not watch.gone and not entry.get("client_aborted") and hold is not None and hold.held:
                # upstream failed mid-hold while client is still listening: observe, then release in order
                try:
                    write_events(hold.eof())
                except OSError:
                    relay_ok = False
        if hold is not None:
            entry["stream_hold"] = {"held_events": hold.held_events, "triggered": hold.triggered}
        reader.finish()
        announce()  # a JSON body's ids are known only now, and what completes it is still held (hold_json)
        if entry.get("api") in ("responses", "chat_completions", "gemini"):
            usage = reader.usage or None  # Responses, Chat Completions, and Gemini usage kept as given
        else:
            usage = {k: reader.usage.get(k) for k in USAGE_KEYS} if reader.usage else None
        entry.update(status=response.status, response_bytes=response_bytes, duration_ms=_ms(started), usage=usage)
        if reader.input_transformations is not None:
            entry["input_transformations"] = reader.input_transformations
            if reader.input_transformations_truncated:
                entry["input_transformations_truncated"] = True
        if decoder.supported:
            entry.update(reader.end_fields())  # packet A23: how the response ended, content-free
        else:
            entry["usage_unavailable"] = f"response content-encoding {decoder.encoding}"
        if not entry.get("client_aborted"):
            observe()  # streams without a trigger event and non-SSE responses: after the body, before completion
        owner.log(entry)
        if 400 <= response.status < 500:
            owner.log({"event": "upstream_4xx", "status": response.status, "path": entry["path"],
                       "model": entry.get("model"), "epoch": entry.get("epoch"),
                       "transform_events": entry.get("transform_events")})
        if relay_ok:
            try:
                if chunked:
                    if held:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(held), held))
                    self.wfile.write(b"0\r\n\r\n")
                elif held:
                    self.wfile.write(held)
                self.wfile.flush()
            except OSError:
                self.close_connection = True

    @staticmethod
    def _early_response(conn: http.client.HTTPConnection) -> http.client.HTTPResponse | None:
        """The answer upstream sent before it stopped reading the body, or None (D53). A server
        with a body limit (nginx, Go) answers 413 after the head and closes, so the send fails while
        the answer is already on its way: that is upstream's error, forwarded unchanged (rule 8),
        not a 502 that makes the CLI resend the same body."""
        sock = conn.sock
        if sock is None:
            return None
        try:
            sock.settimeout(EARLY_RESPONSE_WAIT_S)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException):
            return None
        try:
            sock.settimeout(UPSTREAM_TIMEOUT_S)  # the body is read like any other response's
        except OSError:
            pass
        return response

    def _reject_encoded(self, encoding: str, reason: str, endpoint: str, api: str) -> None:
        """Contract rule 15 (PR #58): a body the proxy cannot decode is a client problem, not a
        Contexto defect, so it is refused with HTTP 415 in the API's own error format and never
        forwarded, because an unread request cannot be kept bounded."""
        message = (f"contexto: cannot read a request body with Content-Encoding {encoding}: {reason}. Run the CLI "
                   f"through 'contexto run', or disable request compression.")
        if api in ("responses", "chat_completions"):
            payload = {"error": {"message": message, "type": "invalid_request_error", "param": None,
                                 "code": "unsupported_content_encoding"}}
        elif api == "gemini":
            payload = {"error": {"code": 415, "message": message, "status": "INVALID_ARGUMENT"}}
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

    def _observe_usage(self, endpoint: str, usage: dict[str, Any], original: dict[str, Any],
                       tool_calls: bool | None = None, tool_call_ids: tuple[str, ...] = (),
                       headers: HeaderList | Headers | None = None) -> list[dict[str, Any]]:
        """Run the usage callback; return the dict events it reports (anything else is ignored)."""
        callback = self.owner.observe_usage
        if callback is None:
            return []
        cached_cb, takes_tool_calls, takes_tool_call_ids, takes_headers = self.owner._observe_sig_cache
        if callback is not cached_cb:
            takes_tool_calls = _takes_keyword(callback, "tool_calls")
            takes_tool_call_ids = _takes_keyword(callback, "tool_call_ids")
            takes_headers = _takes_keyword(callback, "headers")
            self.owner._observe_sig_cache = (callback, takes_tool_calls, takes_tool_call_ids, takes_headers)
        extra: dict[str, Any] = {}
        if takes_tool_calls:  # D40
            extra["tool_calls"] = tool_calls
        if takes_tool_call_ids:
            extra["tool_call_ids"] = tool_call_ids
        if headers is not None and takes_headers:
            extra["headers"] = _headers_to_dict(headers)
        usage_copy = dict(usage)
        try:
            with self.owner.transform_lock:
                if extra:
                    events = callback(endpoint, usage_copy, original, **extra)
                else:
                    events = callback(endpoint, usage_copy, original)
        except Exception as exc:
            self.owner.log({"event": "observe_usage_error", "error": type(exc).__name__})
            return []
        flush = getattr(self.owner.transform, "flush_deferred", None)
        if callable(flush):
            try:
                with self.owner._io_lock:
                    flush()
            except Exception:
                pass
        return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []

    def _send_local_error(self, status: int, message: str, error_type: str = "api_error",
                          *, close: bool = False) -> None:
        body = json.dumps({"type": "error", "error": {"type": error_type, "message": message}}).encode()
        try:
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


def _parse_content_length(values: list[str]) -> int | None:
    """Parse one or more ``Content-Length`` header values; None when absent, invalid, or conflicting."""
    if not values:
        return None
    parsed: set[int] = set()
    for raw in values:
        for part in raw.split(","):
            text = part.strip()
            if not text or not text.isascii() or not text.isdigit():
                return None
            parsed.add(int(text))
    return parsed.pop() if len(parsed) == 1 else None


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
