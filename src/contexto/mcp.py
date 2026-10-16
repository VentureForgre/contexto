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
import math
import sys
from collections.abc import Callable, Iterable
from typing import IO, Any, Protocol

from contexto.store.render import MAX_CHARS_PER_TOKEN, RECALL_LIMIT, recall_chunk, recall_limit
from contexto.store.render import render_page as render_page  # re-exported: contexto.mcp.render_page

SERVER_NAME = "contexto"
SERVER_VERSION = "0.1.0"
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_VERSION = "io.modelcontextprotocol/protocolVersion"
SEARCH_LIMIT = 50  # page ids per contexto_search result
# Per-turn recall budget (M2 contract PR #69): what recall, search, and state return this turn.
BUDGET_SPENT = ("Recall budget for this turn is used up; answer from what is in view, or say the detail is "
                "unknown.")
UNMEASURED_CHARS_PER_TOKEN = 1.0  # before the engine records bytes per token: charges more, never less
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
    def window(self) -> tuple[int | None, float | None]: ...
    # Optional: charge_turn_room(charge) (contract PR #69); without it, calls are not budgeted per turn.


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

    # Tools: each returns (content blocks, structuredContent or None, log fields without text). Recall,
    # search, and state return text only, so the per-turn charge counts exactly what they return.

    def _tool_recall(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], None, dict[str, Any]]:
        page_id = _require_str(args, "page_id")
        offset = args.get("offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ToolError("offset must be a non-negative integer")
        _reject_extra(args, {"page_id", "offset"})
        page = self.store.get_page(page_id)
        if page is None:
            raise ToolError(f"Unknown page id: {page_id}. Page ids appear in the paged-history index.")
        limit, note = recall_limit(*self._window())
        try:
            part = recall_chunk(page, offset, limit)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        full, total = part["text"], part["total_chars"]

        def hint(end: int) -> str:
            return (f"[{total - end} more characters: call contexto_recall with page_id {page_id} and offset {end}; "
                    f"{note}]")

        def build(room: int | None) -> list[str] | None:
            texts = [full] + ([hint(offset + len(full))] if offset + len(full) < total else [])
            if room is None or sum(map(len, texts)) <= room:
                return texts
            size = room - len(hint(offset))  # a later offset's hint is never shorter
            while size > 0 and size + len(hint(offset + size)) > room:
                size -= 1
            return [full[:size], hint(offset + size)] if size > 0 else None

        texts, budget = self._within_room(build, "contexto_recall")
        texts = texts or [BUDGET_SPENT]
        chunk = texts[0] if budget["turn_budget"] != "spent" else ""
        return ([{"type": "text", "text": t} for t in texts], None,
                {"page_id": page_id, "offset": offset, "chars": len(chunk), "total_chars": total, **budget})

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
        full = self.store.state_text()

        def build(room: int | None) -> list[str] | None:
            if room is None or len(full) <= room:
                return [full]
            lines, kept = full.split("\n"), []
            for line in lines:
                shown = len("\n".join(kept + [line]))
                rest = len(full) - shown - 1
                if shown + len(_state_trailer(rest)) + 1 > room:
                    break
                kept.append(line)
            if not kept:
                return None
            return ["\n".join(kept) + "\n" + _state_trailer(len(full) - len("\n".join(kept)) - 1)]

        texts, budget = self._within_room(build, "contexto_state")
        text = texts[0] if texts else BUDGET_SPENT
        return [{"type": "text", "text": text}], None, {"chars": len(text) if texts else 0, **budget}

    def _tool_search(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], None, dict[str, Any]]:
        _reject_extra(args, {"query"})
        query = _require_str(args, "query")
        hits: list[dict[str, Any]] = []
        found = self.store.search(query)
        for page_id in found[:SEARCH_LIMIT]:
            page = self.store.get_page(page_id) or {}
            hits.append({"page_id": page_id, "turn": page.get("turn"), "stub": _stub_line(page.get("stub"))})
        lines = [f"{h['page_id']} (turn {h['turn']}): {h['stub']}" for h in hits]

        def render(shown: int, why: str = "use a longer or more specific query") -> str:
            if not hits:
                return "No paged-out turn contains that exact text."
            text = "\n".join(lines[:shown])
            if len(found) > shown:
                text += f"\n[{len(found) - shown} more matching pages not shown; {why}]"
            return text

        picked = {"shown": len(hits)}

        def build(room: int | None) -> list[str] | None:
            full = render(len(hits))
            if room is None or len(full) <= room:
                picked["shown"] = len(hits)
                return [full]
            for count in range(len(hits) - 1, 0, -1):
                text = render(count, "the recall budget for this turn is used up")
                if len(text) <= room:
                    picked["shown"] = count
                    return [text]
            return None

        texts, budget = self._within_room(build, "contexto_search")
        shown = hits[: picked["shown"]] if texts else []
        text = texts[0] if texts else BUDGET_SPENT
        return ([{"type": "text", "text": text}], None,
                {"query_chars": len(query), "hits": len(shown), "total_matches": len(found), **budget})

    # Per-turn recall budget (M2 contract PR #69)

    def _window(self) -> tuple[int | None, float | None]:
        window = getattr(self.store, "window", None)
        return window() if callable(window) else (None, None)

    def _within_room(self, build: Callable[[int | None], list[str] | None],
                     tool: str) -> tuple[list[str] | None, dict[str, Any]]:
        """Build a reply inside this turn's room and charge it, atomically.

        ``build(room_chars)`` returns the reply's text blocks, at most ``room_chars`` characters in
        all (None: no limit), or None when nothing useful fits. Tokens are the characters divided by
        the per-call limit's ratio, the smaller of the measured bytes per token and 1.5.

        When nothing useful fits, the tool replies ``BUDGET_SPENT``, charged at the room that is
        left, up to its own size. That text is required, so a call with no room left still returns
        it, uncharged: at most about 67 tokens per such call (100 characters at 1.5), about 1,000
        for 15 parallel calls, well inside the engine's headroom floor (25,000 by default).

        With no room published only the per-call limit applies. If the engine is present (it has
        recorded the window) but published no room, a ``turn_room_missing`` event makes the missed
        publish visible.
        """
        window_tokens, ratio = self._window()
        ratio = min(ratio, MAX_CHARS_PER_TOKEN) if ratio else UNMEASURED_CHARS_PER_TOKEN
        out: dict[str, Any] = {"texts": None, "budget": {"turn_budget": "unbudgeted"}}

        def charge(remaining: int | None) -> int:
            texts = build(None if remaining is None else int(remaining * ratio))
            out["texts"] = texts
            if remaining is None:
                return 0
            used = min(remaining, math.ceil(sum(map(len, texts or [BUDGET_SPENT])) / ratio))
            out["budget"] = {"turn_budget": "spent" if texts is None else "charged", "charged_tokens": used,
                             "room_left": remaining - used}
            return used

        charge_turn_room = getattr(self.store, "charge_turn_room", None)
        if callable(charge_turn_room):
            charge_turn_room(charge)
        else:
            charge(None)
        if out["budget"]["turn_budget"] == "unbudgeted" and window_tokens is not None:
            try:
                self.store.log({"event": "turn_room_missing", "tool": tool})
            except Exception as exc:  # logging must never break a call
                self._diag(f"store.log failed: {type(exc).__name__}")
        return out["texts"], out["budget"]

    def _diag(self, message: str) -> None:
        self.stderr.write(f"contexto mcp: {message}\n")
        self.stderr.flush()


def _state_trailer(rest: int) -> str:
    return f"[{rest} more characters of the pinned state not shown; the recall budget for this turn is used up]"


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
