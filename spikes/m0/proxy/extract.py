"""Content-free metrics for one model API request and its response.

Everything returned here is a size, count, hash, field name, or usage number. No prompt text,
tool output, header value, or credential leaves this module.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import zlib
from typing import Any

try:  # Python 3.14+ ships zstd in the standard library; Codex compresses requests with it.
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - depends on interpreter version
    _zstd = None

# Normalized block classes from the M0 brief.
_ANTHROPIC_CLASS = {
    "text": "text",
    "tool_use": "tool_use",
    "server_tool_use": "tool_use",
    "tool_result": "tool_result",
    "thinking": "thinking",
    "redacted_thinking": "thinking",
    "image": "image",
}
_RESPONSES_CLASS = {
    "input_text": "text",
    "output_text": "text",
    "text": "text",
    "refusal": "text",
    "function_call": "tool_use",
    "custom_tool_call": "tool_use",
    "local_shell_call": "tool_use",
    "web_search_call": "tool_use",
    "function_call_output": "tool_result",
    "custom_tool_call_output": "tool_result",
    "local_shell_call_output": "tool_result",
    "reasoning": "thinking",
    "input_image": "image",
    "additional_tools": "tool_defs",
}
# Codex 0.154 sends its instructions as developer-role messages instead of top-level instructions.
_INSTRUCTION_ROLES = {"developer", "system"}

SUMMARY_FIELDS = (
    "turn",
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "request_bytes",
    "message_count",
    "cli",
    "model",
    "ts",
    "endpoint",
)


def compact_size(value: Any) -> int:
    """Byte length of the compact JSON encoding of value (0 when absent)."""
    if value is None:
        return 0
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def prefix_hmac(key: bytes, *parts: Any) -> str:
    """Keyed HMAC-SHA256 of the parts. With a per-run key that is never stored, equal values
    show prefix stability within a run without exposing a guessable plain hash."""
    blob = json.dumps(list(parts), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return hmac.new(key, blob.encode("utf-8"), hashlib.sha256).hexdigest()


def decode_body(raw: bytes, content_encoding: str | None) -> bytes | None:
    """Undo request Content-Encoding. Returns None when the encoding cannot be decoded."""
    encoding = (content_encoding or "identity").strip().lower()
    if encoding in ("", "identity"):
        return raw
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            return zlib.decompress(raw)
        if encoding == "zstd" and _zstd is not None:
            return _zstd.decompress(raw)
    except Exception:
        return None
    return None


# Header values that are never recorded, even partially. Every other header is recorded by name only.
SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}


def header_facts(headers: list[tuple[str, str]]) -> dict[str, Any]:
    """Header names, the auth scheme word, and anthropic-beta flags. No secret values."""
    names = sorted({name.lower() for name, _ in headers})
    facts: dict[str, Any] = {"header_names": names}
    for name, value in headers:
        lower = name.lower()
        if lower == "authorization":
            facts["auth_scheme"] = value.split(" ", 1)[0].lower() if " " in value else "opaque"
        elif lower == "x-api-key":
            facts["auth_scheme"] = "x-api-key"
        elif lower == "anthropic-beta":
            facts["anthropic_beta"] = sorted(v.strip() for v in value.split(",") if v.strip())
    return facts


def endpoint_of(path: str) -> str:
    """Request path without its query string."""
    return path.split("?", 1)[0]


def _bump(counts: dict[str, dict[str, int]], role: str, block_type: str) -> None:
    by_role = counts.setdefault(role, {})
    by_role[block_type] = by_role.get(block_type, 0) + 1


def _cache_control_positions(body: dict[str, Any]) -> list[str]:
    positions = []
    system = body.get("system")
    if isinstance(system, list):
        positions += [f"system[{i}]" for i, b in enumerate(system) if isinstance(b, dict) and "cache_control" in b]
    for i, tool in enumerate(body.get("tools") or []):
        if isinstance(tool, dict) and "cache_control" in tool:
            positions.append(f"tools[{i}]")
    for i, message in enumerate(body.get("messages") or []):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and "cache_control" in block:
                    positions.append(f"messages[{i}].content[{j}]")
    if "cache_control" in body:
        positions.append("top")
    return positions


def block_hmacs(body: dict[str, Any], key: bytes) -> list[list[Any]]:
    """[position, type, keyed hash, has cache_control] for every prompt block in wire order:
    system blocks, tool definitions, then each message content block. Hashes are truncated keyed
    HMACs: equal within a run means the block did not change; nothing about content is exposed."""
    def entry(position: str, block: Any) -> list[Any]:
        kind = block.get("type", "tool") if isinstance(block, dict) else "text"
        has_cc = isinstance(block, dict) and "cache_control" in block
        stripped = {k: v for k, v in block.items() if k != "cache_control"} if isinstance(block, dict) else block
        return [position, str(kind), prefix_hmac(key, stripped)[:16], has_cc]

    out = []
    system = body.get("system")
    for i, block in enumerate(system if isinstance(system, list) else ([system] if system else [])):
        out.append(entry(f"system[{i}]", block))
    for i, tool in enumerate(body.get("tools") or []):
        out.append(entry(f"tools[{i}]", tool))
    for i, message in enumerate(body.get("messages") or []):
        content = message.get("content") if isinstance(message, dict) else None
        role = message.get("role", "?") if isinstance(message, dict) else "?"
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for j, block in enumerate(blocks):
            out.append(entry(f"messages[{i}]({role}).content[{j}]", block))
    return out


def first_changed_block(previous: list[list[Any]], current: list[list[Any]]) -> dict[str, Any] | None:
    """The first position, in wire order, whose hash differs between two requests' block lists.
    The cache prefix cannot extend past it."""
    for index, (before, after) in enumerate(zip(previous, current)):
        if before[0] != after[0] or before[2] != after[2]:
            return {"index": index, "position": after[0], "type": after[1], "previous_position": before[0]}
    if len(current) < len(previous):
        return {"index": len(current), "position": "end", "type": "truncated"}
    return None


def anthropic_request_metrics(body: dict[str, Any], key: bytes) -> dict[str, Any]:
    raw_types: dict[str, dict[str, int]] = {}
    classes: dict[str, dict[str, int]] = {}
    class_bytes: dict[str, int] = {}
    messages = body.get("messages") or []
    for message in messages:
        role = str(message.get("role", "?"))
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for block in blocks:
            block_type = str(block.get("type", "?")) if isinstance(block, dict) else "?"
            block_class = _ANTHROPIC_CLASS.get(block_type, "other")
            _bump(raw_types, role, block_type)
            _bump(classes, role, block_class)
            class_bytes[block_class] = class_bytes.get(block_class, 0) + compact_size(block)
    thinking = body.get("thinking")
    return {
        "api": "anthropic-messages",
        "model": body.get("model"),
        "stream": bool(body.get("stream")),
        "message_count": len(messages),
        "block_types": raw_types,
        "block_classes": classes,
        "class_bytes": class_bytes,
        "messages_bytes": compact_size(messages),
        "first_message_bytes": compact_size(messages[0]) if messages else 0,
        "system_bytes": compact_size(body.get("system")),
        "tools_bytes": compact_size(body.get("tools")),
        "tool_count": len(body.get("tools") or []),
        "prefix_hmac": prefix_hmac(key, body.get("system"), body.get("tools")),
        "cache_control": _cache_control_positions(body),
        "thinking": thinking.get("type") if isinstance(thinking, dict) else None,
        "top_level_fields": sorted(body),
    }


def responses_request_metrics(body: dict[str, Any], key: bytes) -> dict[str, Any]:
    raw_types: dict[str, dict[str, int]] = {}
    classes: dict[str, dict[str, int]] = {}
    class_bytes: dict[str, int] = {}
    items = body.get("input")
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text"}]}] if isinstance(items, str) else (items or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", "message"))
        if item_type == "message":
            role = str(item.get("role", "?"))
            content = item.get("content")
            parts = [{"type": "input_text"}] if isinstance(content, str) else (content or [])
            for part in parts:
                part_type = str(part.get("type", "?")) if isinstance(part, dict) else "?"
                part_class = "instructions" if role in _INSTRUCTION_ROLES else _RESPONSES_CLASS.get(part_type, "other")
                _bump(raw_types, role, part_type)
                _bump(classes, role, part_class)
                class_bytes[part_class] = class_bytes.get(part_class, 0) + compact_size(part)
        else:
            item_class = _RESPONSES_CLASS.get(item_type, "other")
            _bump(raw_types, "item", item_type)
            _bump(classes, "item", item_class)
            class_bytes[item_class] = class_bytes.get(item_class, 0) + compact_size(item)
    head_items = []
    for item in items:
        if isinstance(item, dict) and item.get("type", "message") == "message" and item.get("role") == "user":
            break
        head_items.append(item)
    reasoning = body.get("reasoning")
    tool_defs = [i for i in items if isinstance(i, dict) and i.get("type") == "additional_tools"]
    instructions = [i for i in items if isinstance(i, dict) and i.get("role") in _INSTRUCTION_ROLES
                    and i.get("type", "message") == "message"]
    return {
        "api": "openai-responses",
        "model": body.get("model"),
        "stream": bool(body.get("stream")),
        "message_count": len(items),
        "block_types": raw_types,
        "block_classes": classes,
        "class_bytes": class_bytes,
        "messages_bytes": compact_size(items),
        "first_message_bytes": compact_size(items[0]) if items else 0,
        # Head sizes include both the classic top-level fields and the in-input forms.
        "system_bytes": compact_size(body.get("instructions")) + compact_size(instructions),
        "tools_bytes": compact_size(body.get("tools")) + compact_size(tool_defs),
        "tool_count": len(body.get("tools") or []),
        "head_item_count": len(head_items),
        "prefix_hmac": prefix_hmac(key, body.get("instructions"), body.get("tools"), head_items),
        "cache_control": [],
        "prompt_cache_key_present": "prompt_cache_key" in body,
        "previous_response_id_present": bool(body.get("previous_response_id")),
        "store": body.get("store") if isinstance(body.get("store"), bool) else None,
        "reasoning_effort": reasoning.get("effort") if isinstance(reasoning, dict) else None,
        "top_level_fields": sorted(body),
    }


def request_metrics(path: str, raw: bytes, content_encoding: str | None, key: bytes,
                    with_blocks: bool = False) -> dict[str, Any]:
    """Metrics for one request body. Unknown or undecodable bodies yield sizes only."""
    metrics: dict[str, Any] = {"endpoint": endpoint_of(path), "wire_bytes": len(raw), "content_encoding": content_encoding}
    decoded = decode_body(raw, content_encoding)
    if decoded is None:
        metrics["decode_error"] = "undecodable content-encoding"
        return metrics
    metrics["request_bytes"] = len(decoded)
    if not decoded:
        return metrics
    try:
        body = json.loads(decoded)
    except ValueError:
        metrics["decode_error"] = "not json"
        return metrics
    metrics.update(body_metrics(body, key))
    if with_blocks and isinstance(body, dict) and "messages" in body:
        metrics["blocks"] = block_hmacs(body, key)
    return metrics


def body_metrics(body: Any, key: bytes) -> dict[str, Any]:
    """Metrics for a parsed request body. A WebSocket response.create message may wrap the
    request fields in a "response" object; both shapes are accepted."""
    metrics: dict[str, Any] = {}
    if not isinstance(body, dict):
        return metrics
    if body.get("type"):
        metrics["message_type"] = str(body["type"])
    if isinstance(body.get("response"), dict) and "input" not in body:
        body = body["response"]
    if "messages" in body and "input" not in body:
        metrics.update(anthropic_request_metrics(body, key))
    elif "input" in body:
        metrics.update(responses_request_metrics(body, key))
    else:
        metrics["top_level_fields"] = sorted(body)
    return metrics


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def normalize_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Map a provider usage object onto the Anthropic field names.

    Anthropic usage passes through. OpenAI Responses usage counts cached tokens inside
    input_tokens, so input_tokens becomes the uncached part and cached_tokens becomes
    cache_read_input_tokens. Responses does not report cache writes, so that stays None.
    """
    # Anthropic usage may also carry output_tokens_details, so key on the cache fields instead.
    is_anthropic = "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
    if not is_anthropic and "input_tokens_details" in usage:
        total_in = _int_or_none(usage.get("input_tokens"))
        cached = _int_or_none((usage.get("input_tokens_details") or {}).get("cached_tokens")) or 0
        return {
            "input_tokens": None if total_in is None else total_in - cached,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": cached,
            "output_tokens": _int_or_none(usage.get("output_tokens")),
            "reasoning_output_tokens": _int_or_none((usage.get("output_tokens_details") or {}).get("reasoning_tokens")),
            "raw_input_tokens": total_in,
        }
    return {
        "input_tokens": _int_or_none(usage.get("input_tokens")),
        "cache_creation_input_tokens": _int_or_none(usage.get("cache_creation_input_tokens")),
        "cache_read_input_tokens": _int_or_none(usage.get("cache_read_input_tokens")),
        "output_tokens": _int_or_none(usage.get("output_tokens")),
    }


_RESPONSE_END = ("response.completed", "response.incomplete", "response.failed", "response.done")


class UsageCollector:
    """Incrementally reads a response (SSE or JSON) and keeps only usage numbers."""

    MAX_JSON_BYTES = 8 * 1024 * 1024

    def __init__(self, content_type: str | None) -> None:
        self.sse = "text/event-stream" in (content_type or "").lower()
        self._sniff = not content_type
        self._buffer = bytearray()
        self._json_overflow = False
        self.usage: dict[str, Any] = {}
        self.events: dict[str, int] = {}
        self.stop_reason: str | None = None

    def feed(self, chunk: bytes) -> None:
        if self._sniff and chunk:
            # Some backends stream SSE without a Content-Type; decide from the first bytes.
            self._sniff = False
            self.sse = chunk.lstrip()[:6] in (b"event:", b"data: ", b"data:{") or chunk.lstrip().startswith(b"data:")
        if not self.sse:
            if len(self._buffer) + len(chunk) > self.MAX_JSON_BYTES:
                self._json_overflow = True
                self._buffer.clear()
            elif not self._json_overflow:
                self._buffer += chunk
            return
        self._buffer += chunk.replace(b"\r\n", b"\n")
        while True:
            end = self._buffer.find(b"\n\n")
            if end < 0:
                return
            event = bytes(self._buffer[:end])
            del self._buffer[: end + 2]
            self._event(event)

    def finish(self) -> None:
        if self.sse:
            if self._buffer.strip():
                self._event(bytes(self._buffer))
        elif not self._json_overflow and self._buffer:
            try:
                body = json.loads(bytes(self._buffer))
            except ValueError:
                body = None
            if isinstance(body, dict):
                if isinstance(body.get("usage"), dict):
                    self.usage = dict(body["usage"])
                elif isinstance(body.get("response"), dict) and isinstance(body["response"].get("usage"), dict):
                    self.usage = dict(body["response"]["usage"])
                elif set(body) == {"input_tokens"}:
                    self.events["count_tokens"] = 1
                self.stop_reason = body.get("stop_reason")
        self._buffer.clear()

    def _event(self, raw: bytes) -> None:
        data = b"\n".join(line[5:].lstrip() for line in raw.split(b"\n") if line.startswith(b"data:"))
        if not data or data == b"[DONE]":
            return
        try:
            payload = json.loads(data)
        except ValueError:
            return
        self.handle(payload)

    def handle(self, payload: Any) -> bool:
        """Account one decoded event. Returns True when it ends a response."""
        if not isinstance(payload, dict):
            return False
        kind = str(payload.get("type", "?"))
        self.events[kind] = self.events.get(kind, 0) + 1
        if kind == "message_start":
            usage = (payload.get("message") or {}).get("usage")
            if isinstance(usage, dict):
                self.usage.update(usage)
        elif kind == "message_delta":
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self.usage.update({k: v for k, v in usage.items() if v is not None})
            self.stop_reason = (payload.get("delta") or {}).get("stop_reason") or self.stop_reason
        elif kind in _RESPONSE_END:
            response = payload.get("response") or {}
            if isinstance(response.get("usage"), dict):
                self.usage = dict(response["usage"])
            self.stop_reason = response.get("status") or self.stop_reason
            return True
        elif kind == "error":
            return True
        return False

    def normalized(self) -> dict[str, Any]:
        return normalize_usage(self.usage) if self.usage else {}


def total_tokens(usage: dict[str, Any]) -> int:
    """All usage categories, cache reads included, as the M0 token caps count them."""
    return sum(usage.get(k) or 0 for k in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))


def summary_row(record: dict[str, Any]) -> dict[str, Any]:
    """The lane 2 summary row: exactly SUMMARY_FIELDS, numbers and labels only."""
    usage = record.get("usage") or {}
    return {
        "turn": record.get("turn"),
        "input_tokens": usage.get("input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "request_bytes": record.get("request_bytes"),
        "message_count": record.get("message_count"),
        "cli": record.get("cli"),
        "model": record.get("model"),
        "ts": record.get("ts"),
        "endpoint": record.get("endpoint"),
    }


class StreamDecoder:
    """Decodes a side copy of a compressed response stream for usage parsing.

    The proxy relays the original bytes; only this copy is decompressed. Unsupported encodings
    (for example br, which the standard library lacks) make the decoder report itself unusable.
    """

    def __init__(self, content_encoding: str | None) -> None:
        encoding = (content_encoding or "identity").strip().lower()
        self.encoding = encoding
        self.supported = True
        self._obj: Any = None
        if encoding == "gzip":
            self._obj = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._obj = zlib.decompressobj()
        elif encoding == "zstd" and _zstd is not None:
            self._obj = _zstd.ZstdDecompressor()
        elif encoding not in ("", "identity"):
            self.supported = False

    def decode(self, chunk: bytes) -> bytes:
        if not self.supported:
            return b""
        if self._obj is None:
            return chunk
        try:
            return self._obj.decompress(chunk)
        except Exception:
            self.supported = False
            return b""


def permessage_deflate(extensions: str | None) -> tuple[bool, bool, bool]:
    """(enabled, client_no_context_takeover, server_no_context_takeover) from the negotiated
    Sec-WebSocket-Extensions response header."""
    for offer in (extensions or "").split(","):
        params = [p.strip().lower() for p in offer.split(";")]
        if params and params[0] == "permessage-deflate":
            return True, "client_no_context_takeover" in params, "server_no_context_takeover" in params
    return False, False, False


def _unmask(payload: bytes, key: bytes) -> bytes:
    n = len(payload)
    if not n:
        return payload
    mask = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(mask, "big")).to_bytes(n, "big")


class WsFrameParser:
    """Reassembles WebSocket messages from one direction of a relayed byte stream (RFC 6455),
    unmasking client frames and inflating permessage-deflate (RFC 7692) messages. It reads a
    side copy only; the relayed bytes are never touched."""

    def __init__(self, deflate: bool = False, no_context_takeover: bool = False) -> None:
        self.deflate = deflate
        self.no_context_takeover = no_context_takeover
        self._inflater = zlib.decompressobj(-15) if deflate else None
        self._buffer = bytearray()
        self._fragments: list[bytes] = []
        self._opcode = 0
        self._compressed = False
        self._wire = 0
        self.broken = False

    def feed(self, data: bytes) -> list[tuple[int, bytes, int]]:
        """Returns completed (opcode, payload, wire_payload_bytes) messages; control frames included."""
        if self.broken:
            return []
        self._buffer += data
        messages = []
        while len(self._buffer) >= 2:
            b0, b1 = self._buffer[0], self._buffer[1]
            length, offset = b1 & 0x7F, 2
            if length == 126:
                if len(self._buffer) < 4:
                    break
                length, offset = int.from_bytes(self._buffer[2:4], "big"), 4
            elif length == 127:
                if len(self._buffer) < 10:
                    break
                length, offset = int.from_bytes(self._buffer[2:10], "big"), 10
            key = b""
            if b1 & 0x80:
                key, offset = bytes(self._buffer[offset : offset + 4]), offset + 4
            if len(self._buffer) < offset + length:
                break
            payload = bytes(self._buffer[offset : offset + length])
            del self._buffer[: offset + length]
            if key:
                payload = _unmask(payload, key)
            opcode, fin = b0 & 0x0F, bool(b0 & 0x80)
            if opcode >= 8:
                messages.append((opcode, payload, length))
                continue
            if opcode in (1, 2):
                self._opcode, self._compressed, self._fragments, self._wire = opcode, bool(b0 & 0x40), [], 0
            self._fragments.append(payload)
            self._wire += length
            if fin:
                message = b"".join(self._fragments)
                if self._compressed:
                    message = self._inflate(message)
                    if message is None:
                        return messages
                messages.append((self._opcode, message, self._wire))
        return messages

    def _inflate(self, message: bytes) -> bytes | None:
        if self._inflater is None:
            self.broken = True
            return None
        try:
            out = self._inflater.decompress(message + b"\x00\x00\xff\xff")
        except zlib.error:
            self.broken = True
            return None
        if self.no_context_takeover:
            self._inflater = zlib.decompressobj(-15)
        return out
