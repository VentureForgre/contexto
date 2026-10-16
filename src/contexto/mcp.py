"""Contexto MCP server: recall, pin, close, state, and search over one session's store, on stdio.

Claude Code spawns ``contexto mcp --session S`` from the launcher's ``--mcp-config``. The server
speaks JSON-RPC 2.0, one message per line on stdin and stdout, and serves both protocol eras:

- legacy clients open with ``initialize`` (protocol revisions up to 2025-11-25);
- modern clients carry ``_meta["io.modelcontextprotocol/protocolVersion"]`` on every request and
  may probe with ``server/discover`` (revision 2026-07-28).

Only protocol messages are written to stdout. Diagnostics go to stderr, and every tool call is
recorded through ``store.log`` with names, ids, and sizes, never text content.

The store is the session's ``contexto.store.SessionStore`` (lane B packets B1 and B2): it provides
``get_page(page_id) -> dict | None`` (keys ``id``, ``turn``, ``messages``, ``stub``),
``search(query) -> list[str]`` (exact, case-sensitive, newest first), ``pin(kind, text, *,
supersedes, epistemic, provenance) -> str``, ``close(obligation_id, note) -> str``,
``state_text() -> str``, and ``log(event)``. ``pin`` and ``close`` raise ``ValueError`` on
validation failures, which become tool errors the model can act on.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from typing import IO, Any, Protocol

SERVER_NAME = "contexto"
SERVER_VERSION = "0.1.0"
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_VERSION = "io.modelcontextprotocol/protocolVersion"
RECALL_LIMIT = 32_000  # characters per contexto_recall call
SEARCH_LIMIT = 50  # page ids per contexto_search result
# Modern-era cacheable results (server/discover, tools/list) must carry these; Claude Code 2.1.301
# rejects a tools/list result without them (reviewer-2, real client, 2026-10-16). The tool list is
# per-session and never cached by the client.
CACHE_FIELDS = {"ttlMs": 0, "cacheScope": "private"}
_CACHEABLE = frozenset({"server/discover", "tools/list"})
RECORD_KINDS = ("event", "decision", "obligation", "artifact", "reference")
EPISTEMICS = ("verified", "reported", "summary", "hypothesis")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_VERSION = -32022

INSTRUCTIONS = (
    "Contexto keeps this session's older turns in a local store. Use contexto_recall with a page id "
    "from the paged-history index to read a paged-out turn word for word before relying on its "
    "details. Use contexto_pin to record decisions and obligations as you make them, contexto_close "
    "when an obligation is done, contexto_state for the full pinned state, and contexto_search for "
    "an exact substring when you do not know the page id."
)


class Store(Protocol):
    def get_page(self, page_id: str) -> dict[str, Any] | None: ...
    def search(self, query: str) -> list[str]: ...
    def pin(self, kind: str, text: str, *, supersedes: Iterable[str] = (), epistemic: str = "reported",
            provenance: Iterable[dict[str, Any]] = ()) -> str: ...
    def close(self, obligation_id: str, note: str) -> str: ...
    def state_text(self) -> str: ...
    def log(self, event: dict[str, Any]) -> None: ...


class ToolError(Exception):
    """A tool execution error the model can act on: returned as a result with isError true."""


TOOLS: list[dict[str, Any]] = [
    {
        "name": "contexto_recall",
        "description": (
            "Read a paged-out turn of this session word for word, by its page id (pg_...) from the "
            f"paged-history index. Returns at most {RECALL_LIMIT} characters per call; when more "
            "remain, call again with the offset given at the end of the result. Thinking is omitted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Page id, for example pg_0123456789abcdef"},
                "offset": {"type": "integer", "minimum": 0, "default": 0,
                           "description": "Character offset to continue from"},
            },
            "required": ["page_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "contexto_pin",
        "description": (
            "Record a decision, obligation, fact, artifact, or reference in this session's ledger so it "
            "stays in the pinned state after older turns are paged out. To replace an earlier record, "
            "list its id in supersedes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(RECORD_KINDS)},
                "text": {"type": "string", "minLength": 1},
                "supersedes": {"type": "array", "items": {"type": "string"}},
                "epistemic": {"type": "string", "enum": list(EPISTEMICS), "default": "reported"},
                "provenance": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "locator": {"type": "string"},
                            "sha256": {"type": ["string", "null"]},
                            "span": {"type": ["string", "null"]},
                        },
                        "required": ["type", "locator"],
                    },
                },
            },
            "required": ["kind", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "contexto_close",
        "description": "Close an open obligation by its record id, with a short note on how it was done.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "obligation_id": {"type": "string"},
                "note": {"type": "string", "minLength": 1},
            },
            "required": ["obligation_id", "note"],
            "additionalProperties": False,
        },
    },
    {
        "name": "contexto_state",
        "description": "Return the full pinned state (open obligations, active decisions, verified facts) with no size cap.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    {
        "name": "contexto_search",
        "description": (
            "Find paged-out turns containing an exact substring (case-sensitive) in their text or stub. "
            f"Returns up to {SEARCH_LIMIT} page ids, newest first. No fuzzy or similarity matching."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]
_TOOL_NAMES = {t["name"] for t in TOOLS}


def render_page(page: dict[str, Any]) -> str:
    """Render a page's messages as plain text, thinking omitted. Deterministic for a given page."""
    lines: list[str] = []
    turn = page.get("turn")
    for message in page.get("messages") or []:
        if not isinstance(message, dict):
            lines.append("[malformed message omitted]")
            continue
        role = message.get("role", "unknown")
        lines.append(f"[turn {turn}] {role}:")
        content = message.get("content")
        if isinstance(content, str):
            lines.append(content)
            continue
        if not isinstance(content, list):
            lines.append("[malformed content omitted]" if content is not None else "")
            continue
        for block in content:
            if not isinstance(block, dict):
                lines.append("[malformed block omitted]")
                continue
            kind = block.get("type")
            if kind == "text":
                lines.append(block.get("text", ""))
            elif kind in ("thinking", "redacted_thinking"):
                continue
            elif kind == "tool_use":
                args = json.dumps(block.get("input", {}), ensure_ascii=False, sort_keys=True)
                lines.append(f"tool_use {block.get('name', '')} {args}")
            elif kind == "tool_result":
                lines.append("tool_result:")
                inner = block.get("content")
                if isinstance(inner, str):
                    lines.append(inner)
                elif isinstance(inner, list):
                    for part in inner:
                        if not isinstance(part, dict):
                            lines.append("[malformed block omitted]")
                        elif part.get("type") == "text":
                            lines.append(part.get("text", ""))
                        else:
                            lines.append(f"[{part.get('type', 'block')} omitted]")
            else:
                lines.append(f"[{kind} omitted]")
    return "\n".join(lines)


class Server:
    def __init__(self, store: Store, stdout: IO[str], stderr: IO[str] | None = None) -> None:
        self.store = store
        self.stdout = stdout
        self.stderr = stderr or sys.stderr
        self.legacy_version: str | None = None  # set by initialize
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "initialize": self._initialize,
            "server/discover": self._discover,
            "ping": lambda params: {},
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }

    # Transport

    def serve(self, stdin: Iterable[str]) -> int:
        for line in stdin:
            if not line.strip():
                continue
            response = self.handle_line(line)
            if response is not None:
                self.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                self.stdout.flush()
        return 0  # end of input is the shutdown signal

    def handle_line(self, line: str) -> dict[str, Any] | None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return _error(None, PARSE_ERROR, "Parse error")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "method" not in message and ("result" in message or "error" in message):
                return None  # a response from the client: nothing to answer
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "Invalid request")
        is_notification = "id" not in message
        try:
            response = self._dispatch(message)
        except Exception as exc:  # never let one bad request kill the server
            self._diag(f"internal error in {message['method']}: {type(exc).__name__}")
            response = _error(message.get("id"), INTERNAL_ERROR, "Internal error")
        return None if is_notification else response

    # Dispatch

    def _dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message["method"]
        msg_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        if method.startswith("notifications/"):
            return None
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        requested = meta.get(META_VERSION)
        modern = requested is not None
        if modern and requested not in MODERN_VERSIONS:
            return _error(msg_id, UNSUPPORTED_VERSION, "Unsupported protocol version",
                          {"supported": list(MODERN_VERSIONS) + list(LEGACY_VERSIONS), "requested": requested})
        handler = self.handlers.get(method)
        if handler is None:
            return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        if method == "tools/call" and params.get("name") not in _TOOL_NAMES:
            return _error(msg_id, INVALID_PARAMS, f"Unknown tool: {params.get('name')}")
        result = handler(params)
        if modern and method != "initialize":
            result = {"resultType": "complete", **result}
            if method in _CACHEABLE:
                result.update(CACHE_FIELDS)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        self.legacy_version = version
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    def _discover(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "supportedVersions": list(MODERN_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}},
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}},
            "instructions": INSTRUCTIONS,
        }

    def _tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": TOOLS}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params["name"]
        args = params.get("arguments") or {}
        event: dict[str, Any] = {"event": "mcp_call", "tool": name}
        try:
            if not isinstance(args, dict):
                raise ToolError("arguments must be an object")
            content, structured, extra = getattr(self, "_tool_" + name.removeprefix("contexto_"))(args)
            event.update(extra, ok=True)
            result: dict[str, Any] = {"content": content, "isError": False}
            if structured is not None:
                result["structuredContent"] = structured
        except ToolError as exc:
            event.update(ok=False, error=type(exc).__name__)
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        try:
            self.store.log(event)
        except Exception as exc:  # logging must never break a call
            self._diag(f"store.log failed: {type(exc).__name__}")
        return result

    # Tools: each returns (content blocks, structuredContent or None, log fields without text)

    def _tool_recall(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        page_id = _require_str(args, "page_id")
        offset = args.get("offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ToolError("offset must be a non-negative integer")
        _reject_extra(args, {"page_id", "offset"})
        page = self.store.get_page(page_id)
        if page is None:
            raise ToolError(f"Unknown page id: {page_id}. Page ids appear in the paged-history index.")
        text = render_page(page)
        if offset > len(text):
            raise ToolError(f"offset {offset} is past the end of the page ({len(text)} characters)")
        chunk = text[offset : offset + RECALL_LIMIT]
        end = offset + len(chunk)
        next_offset = end if end < len(text) else None
        content = [{"type": "text", "text": chunk}]
        if next_offset is not None:
            content.append({"type": "text", "text": f"[{len(text) - end} more characters: call contexto_recall "
                                                    f"with page_id {page_id} and offset {next_offset}]"})
        structured = {"page_id": page_id, "turn": page.get("turn"), "offset": offset,
                      "next_offset": next_offset, "total_chars": len(text)}
        return content, structured, {"page_id": page_id, "offset": offset, "chars": len(chunk), "total_chars": len(text)}

    def _tool_pin(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        _reject_extra(args, {"kind", "text", "supersedes", "epistemic", "provenance"})
        kind = _require_str(args, "kind")
        if kind not in RECORD_KINDS:
            raise ToolError(f"kind must be one of {', '.join(RECORD_KINDS)}")
        text = _require_str(args, "text")
        epistemic = args.get("epistemic", "reported")
        if epistemic not in EPISTEMICS:
            raise ToolError(f"epistemic must be one of {', '.join(EPISTEMICS)}")
        supersedes = args.get("supersedes")
        if supersedes is not None and (not isinstance(supersedes, list) or not all(isinstance(s, str) for s in supersedes)):
            raise ToolError("supersedes must be a list of record ids")
        provenance = args.get("provenance")
        if provenance is not None and (not isinstance(provenance, list) or not all(
                isinstance(p, dict) and isinstance(p.get("type"), str) and isinstance(p.get("locator"), str)
                for p in provenance)):
            raise ToolError("provenance must be a list of objects with string type and locator")
        try:
            record_id = self.store.pin(kind, text, supersedes=tuple(supersedes or ()), epistemic=epistemic,
                                       provenance=tuple(provenance or ()))
        except ValueError as exc:
            raise ToolError(f"Not recorded: {exc}") from None
        return ([{"type": "text", "text": f"Recorded {kind} {record_id}."}], {"record_id": record_id},
                {"kind": kind, "record_id": record_id, "chars": len(text), "supersedes": len(supersedes or [])})

    def _tool_close(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        _reject_extra(args, {"obligation_id", "note"})
        obligation_id = _require_str(args, "obligation_id")
        note = _require_str(args, "note")
        try:
            record_id = self.store.close(obligation_id, note)
        except ValueError as exc:
            raise ToolError(f"Not closed: {exc}") from None
        return ([{"type": "text", "text": f"Closed {obligation_id} with {record_id}."}], {"record_id": record_id},
                {"obligation_id": obligation_id, "record_id": record_id, "chars": len(note)})

    def _tool_state(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], None, dict[str, Any]]:
        _reject_extra(args, set())
        text = self.store.state_text()
        return [{"type": "text", "text": text}], None, {"chars": len(text)}

    def _tool_search(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        _reject_extra(args, {"query"})
        query = _require_str(args, "query")
        hits: list[dict[str, Any]] = []
        found = self.store.search(query)
        for page_id in found[:SEARCH_LIMIT]:
            page = self.store.get_page(page_id) or {}
            hits.append({"page_id": page_id, "turn": page.get("turn"), "stub": _stub_line(page.get("stub"))})
        if hits:
            text = "\n".join(f"{h['page_id']} (turn {h['turn']}): {h['stub']}" for h in hits)
            if len(found) > len(hits):
                text += (f"\n[{len(found) - len(hits)} more matching pages not shown; "
                         "use a longer or more specific query]")
        else:
            text = "No paged-out turn contains that exact text."
        return ([{"type": "text", "text": text}], {"page_ids": [h["page_id"] for h in hits], "total_matches": len(found)},
                {"query_chars": len(query), "hits": len(hits), "total_matches": len(found)})

    def _diag(self, message: str) -> None:
        self.stderr.write(f"contexto mcp: {message}\n")
        self.stderr.flush()


def _stub_line(stub: Any) -> str:
    """One line from a page stub: the opening user text, then tool names and paths if any."""
    if not isinstance(stub, dict):
        return str(stub or "")
    parts = [" ".join(str(stub.get("user_text", "")).split())]
    if stub.get("tools"):
        parts.append("tools: " + ", ".join(map(str, stub["tools"])))
    if stub.get("paths"):
        parts.append("paths: " + ", ".join(map(str, stub["paths"])))
    return " | ".join(p for p in parts if p)


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError(f"{key} is required and must be a non-empty string")
    return value


def _reject_extra(args: dict[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(args) - allowed)
    if extra:
        raise ToolError(f"unexpected argument(s): {', '.join(extra)}")


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def serve_stdio(store: Store) -> int:
    """Run the server on this process's stdin and stdout until end of input."""
    return Server(store, sys.stdout, sys.stderr).serve(sys.stdin)
