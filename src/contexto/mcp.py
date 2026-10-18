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
``search(query) -> list[str]`` (every word of the query, case-insensitively, best first; D34), ``pin(kind, text, *,
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

from contexto.store.pages import is_context_page
from contexto.store.render import (MATCH_CONTEXT, MATCHES_PER_PAGE, MAX_CHARS_PER_TOKEN, RECALL_LIMIT, match_lines,
                                   matching_line_starts, recall_chunk, recall_limit, render_page)
from contexto.store.search import SHOWN_IN_DETAIL, Query, text_matches

SERVER_NAME = "contexto"
SERVER_VERSION = "0.1.0"
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_VERSION = "io.modelcontextprotocol/protocolVersion"
SEARCH_LIMIT = SHOWN_IN_DETAIL  # the best pages, shown with their matching lines per contexto_search reply, newest first (B17, D34)
# The reply's first line about paged pages, so the model sees why a page that holds only the words can
# sit above one that holds the whole query (reviewer-2 on PR #204). In the reply, not the tool
# description, so the head the description adds to every request is unchanged.
SEARCH_BEST_HEADER = "[best matching pages by relevance, shown newest first:]"
# Put before the first paged context block in each part of a search reply. A context page's turn is
# when the proxy paged the block, which can be long after the turn that sent it and after turns that
# supersede it, so context pages are shown after every turn page (reviewer-2 on PR #204, D34).
SEARCH_CONTEXT_HEADER = "[paged reminders and system messages, not dated by turn:]"
SEARCH_MORE_LIMIT = 50  # the next best pages, listed compactly newest first (page id, turn, match offsets)
# Per-turn recall budget (M2 contract PR #69): what recall, search, and state return this turn.
BUDGET_SPENT = ("Recall budget for this turn is used up; answer from what is in view, or say the detail is "
                "unknown.")
# Intake (2026-10-16 05:20Z): some room is left, but half of it cannot hold RECALL_MIN_CHARS of page text
# and the offset hint, so a recall would return too little to be worth its call.
RECALL_MIN_CHARS = 200
RECALL_NEARLY_SPENT = ("The recall budget for this turn is nearly used up (about {tokens} tokens left), too little "
                       "for a recall. Use contexto_search with an exact key to get the matching lines, answer from "
                       "what is in view, or say the detail is unknown.")
SEARCH_TOO_LONG = "This search result is longer than one call can return; use a longer or more specific query."
NO_HIT = "No paged-out turn contains every word of that query."
# Fixed control replies are protocol text, never charged, and returned even when the room is smaller than they
# are (contract PR #103); the longest is about 225 characters. RECALL_NEARLY_SPENT is a template, checked apart.
CONTROL_REPLIES = frozenset({BUDGET_SPENT, SEARCH_TOO_LONG, NO_HIT})
# B19 (a): every search reply ends with this, as its own text block. Like the control replies it is fixed
# protocol text and not charged. The live run showed answers taken from the newest search hit while a newer
# statement sat in the conversation in view, which search cannot see.
SEARCH_COVERAGE = ("[Paged hits come only from paged-out turns; the conversation in view is newer than any of "
                   "them.]")  # contract PR #125
# B19 (b): when the retained-window snapshot is missing or not from the current room publish.
SEARCH_COVERAGE_UNCHECKED = SEARCH_COVERAGE[:-1] + " In view not checked.]"
IN_VIEW_LIMIT = 3  # matching lines from the conversation in view per search reply, newest first
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


def _lines(n: int) -> str:
    return "1 line" if n == 1 else f"{n} lines"


TOOLS: list[dict[str, Any]] = [
    {
        "name": "contexto_recall",
        "description": (
            "Read a paged-out turn of this session word for word, by its page id (pg_...) from the "
            f"paged-history index. Returns at most {RECALL_LIMIT} characters per call, and at most half "
            "of this turn's remaining recall budget; when more remain, call again with the offset given "
            "at the end of the result. Thinking is omitted."
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
            "Find paged-out turns holding every word of the query, in any case and order, in text or "
            "stub; best first: whole query, then relevance, newer among similar. "
            f"For the best {SEARCH_LIMIT} pages, shown newest first, returns up to {MATCHES_PER_PAGE} matching lines "
            f"(marked >) with {_lines(MATCH_CONTEXT)} of context either side, each with its page offset; the next "
            f"{SEARCH_MORE_LIMIT} are listed as page id, turn, @offsets. Answer from a "
            "matching line, or call contexto_recall with its page id and offset to read on. Search covers only "
            "paged-out turns: the conversation in view is newer than any hit. No fuzzy or similarity matching."
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
        self.listed = False  # tools/list answered and recorded (see _tools_list)
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
        if not self.listed:
            # The client now has the tools: recorded once per server so the proxy can tell that this
            # launch's CLI received them (contexto.toolcheck; Codex's requests never name them).
            self.listed = True
            recorder = getattr(self.store, "set_mcp_listed", None)
            try:
                if callable(recorder):
                    recorder()
            except Exception as exc:  # the record is a diagnostic; never fail tools/list for it
                try:
                    self._diag(f"set_mcp_listed failed: {type(exc).__name__}")
                except (OSError, ValueError):  # a closed or broken stderr
                    pass
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
            return [full[:size], hint(offset + size)] if size >= RECALL_MIN_CHARS else None

        texts, budget = self._within_room(build, "contexto_recall", half=True)
        chunk = texts[0] if texts else ""
        if texts is None:
            texts = [RECALL_NEARLY_SPENT.format(tokens=budget["room_left"]) if budget["turn_budget"] == "nearly_spent"
                     else BUDGET_SPENT]
        return ([{"type": "text", "text": t} for t in texts], None,
                {"page_id": page_id, "offset": offset, "chars": len(chunk), "total_chars": total,
                 "returned": [{"source": "paged", "page_id": page_id, "start": offset, "end": offset + len(chunk)}]
                 if chunk else [],
                 **budget})

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
        found = self.store.search(query)  # best first (D34)
        pages = [(page_id, self.store.get_page(page_id) or {}) for page_id in found[:SEARCH_LIMIT + SEARCH_MORE_LIMIT]]
        # The best pages are chosen by relevance and shown newest first, so among them the newest statement
        # of a fact comes first (D28); the next best are listed newest first too. In each part turn pages
        # come first and paged context blocks after them, under their own line (D34).
        hits = _with_context_header([(_hit_rows(page_id, page, query), page)
                                     for page_id, page in _newest_first(pages[:SEARCH_LIMIT])])
        more = _with_context_header([([_more_row(page_id, page, query)], page)
                                     for page_id, page in _newest_first(pages[SEARCH_LIMIT:])])
        in_view_found = [0]
        in_view = self._in_view_rows(query, in_view_found)  # None: in view not checked
        lead = in_view or []
        limit, _ = recall_limit(*self._window())  # one search reply is held to the per-call recall limit
        specific = "use a longer or more specific query"

        def render(rows: list[Row], detailed: int, listed: int, why: str) -> str:
            lines = [t for t, _ in lead] + [SEARCH_BEST_HEADER] + [t for t, _ in rows]
            if listed:
                lines.append("[more matching pages, newest first; recall one at its offset:]")
                lines += [t for entry in more[:listed] for t, _ in entry]
            if len(found) > detailed + listed:
                lines.append(f"[{len(found) - detailed - listed} more matching pages not shown; {why}]")
            return "\n".join(lines)

        picked: dict[str, Any] = {"rows": [], "pages": 0}

        def build(room: int | None) -> list[str] | None:
            if not hits and not lead:
                return [NO_HIT]
            if not hits:  # matches in view only
                text = "\n".join(t for t, _ in lead)
                cap_only = limit if room is None else min(room, limit)
                if len(text) <= cap_only:
                    picked.update(rows=[], pages=0)
                    return [text]
                return None
            cap = limit if room is None else min(room, limit)
            by_cap = room is None or limit <= room  # the per-call limit binds, not the turn's room
            why = specific if by_cap else "the recall budget for this turn is used up"
            for detailed in range(len(hits), 0, -1):  # whole hits first, then as many compact entries as fit
                rows = [row for hit in hits[:detailed] for row in hit]
                for listed in range(len(more) if detailed == len(hits) else 0, -1, -1):
                    complete = detailed == len(hits) and listed == len(more)
                    text = render(rows, detailed, listed, specific if complete else why)
                    if len(text) <= cap:
                        picked.update(rows=rows, pages=detailed + listed)
                        return [text]
            # Not even one whole hit: its page line and matching lines, without context, as far as they fit.
            context = [row for row in hits[0] if row[0] == SEARCH_CONTEXT_HEADER]
            header, *lines = [row for row in hits[0] if row[0] != SEARCH_CONTEXT_HEADER]
            matches = [row for row in lines if row[0].startswith(">")]
            for count in range(len(matches), 0, -1):
                rows = [*context, header, *matches[:count], (f"  [the rest of this hit is not shown; {why}]", None)]
                text = render(rows, 1, 0, why)
                if len(text) <= cap:
                    picked.update(rows=rows, pages=1)
                    return [text]
            return [SEARCH_TOO_LONG] if by_cap and len(SEARCH_TOO_LONG) <= cap else None

        texts, budget = self._within_room(build, "contexto_search")
        text = texts[0] if texts else BUDGET_SPENT
        shown = texts is not None and text not in CONTROL_REPLIES
        coverage = SEARCH_COVERAGE if in_view is not None else SEARCH_COVERAGE_UNCHECKED
        return ([{"type": "text", "text": text}, {"type": "text", "text": coverage}], None,
                {"query_chars": len(query), "hits": picked["pages"] if texts else 0, "total_matches": len(found),
                 "in_view_hits": sum(1 for _, span in lead if span) if shown else 0,
                 "in_view_more": max(0, in_view_found[0] - IN_VIEW_LIMIT) if shown else 0,
                 "in_view_checked": in_view is not None,
                 "returned": _spans(lead + picked["rows"]) if shown else [], **budget})

    def _in_view_rows(self, query: str, total: list[int]) -> list[Row] | None:
        """Matching lines of the conversation in view, newest first (B19 b, contract PR #125).

        Uses the retained-window snapshot only when the store says it is the current request's
        (``retained_current``); otherwise returns None and the reply says in view was not checked. Each
        line is the matching line only, labelled in view, with no offset (recall reads pages, not the
        window); its span is logged content-free with source in_view, which never credits recall.
        """
        current = getattr(self.store, "retained_current", None)  # stamp == room stamp == request sequence
        snapshot = current() if callable(current) else None
        if snapshot is None:
            return None
        found = []
        q = Query.parse(query)
        newest = max((e["turn"] for e in snapshot[3]), default=None)
        for entry in snapshot[3]:
            if entry["turn"] == newest and entry["role"] == "user":
                continue  # the current turn's own question is what is asked, not a statement (B22, PR #143)
            if q is None or not text_matches(entry["text"], q):  # a message matches as a page does (D34)
                continue
            every = entry["text"].count("\n") + 1  # ALL matching lines: the newest are chosen below
            shown, _ = match_lines(entry["text"], q, context=0, per_page=every)
            found += [(entry["turn"], entry["message_index"], start, end, text)
                      for start, end, is_match, text in shown if is_match]
        found.sort(key=lambda m: (m[0], m[1], m[2]), reverse=True)
        total[0] = len(found)
        if not found:
            return []
        rows: list[Row] = [("[in view, newest first:]", None)]
        rows += [(f"> in view (turn {turn}): {text}", ("in_view", turn, index, start, end))
                 for turn, index, start, end, text in found[:IN_VIEW_LIMIT]]
        if len(found) > IN_VIEW_LIMIT:
            rows.append((f"  [{len(found) - IN_VIEW_LIMIT} more matching lines in view]", None))
        return rows

    # Per-turn recall budget (M2 contract PR #69)

    def _window(self) -> tuple[int | None, float | None]:
        window = getattr(self.store, "window", None)
        return window() if callable(window) else (None, None)

    def _within_room(self, build: Callable[[int | None], list[str] | None], tool: str,
                     half: bool = False) -> tuple[list[str] | None, dict[str, Any]]:
        """Build a reply inside this turn's room and charge it, atomically.

        ``build(room_chars)`` returns the reply's text blocks, at most ``room_chars`` characters in
        all (None: no limit), or None when nothing useful fits. Tokens are the characters divided by
        the per-call limit's ratio, the smaller of the measured bytes per token and 1.5. With
        ``half``, ``room_chars`` is half the room that remains (``floor(remaining / 2)`` tokens), so
        one recall cannot consume the turn (contract PR #96); the rest is read by offset.

        When nothing useful fits, the tool replies ``BUDGET_SPENT`` (or, for recall with some room
        left, ``RECALL_NEARLY_SPENT``). These and the other fixed control replies are protocol text:
        never charged, and returned even when the room is smaller than they are (contract PR #103).
        The longest, the nearly-used-up reply, is about 225 characters: about 60 tokens of real usage
        (150 at the conservative 1.5 characters per token used for charging), so 15 parallel calls
        add about 900, well inside the engine's headroom floor (25,000 by default).

        With no room published only the per-call limit applies. If the engine is present (it has
        recorded the window) but published no room, a ``turn_room_missing`` event makes the missed
        publish visible.
        """
        window_tokens, ratio = self._window()
        ratio = min(ratio, MAX_CHARS_PER_TOKEN) if ratio else UNMEASURED_CHARS_PER_TOKEN
        out: dict[str, Any] = {"texts": None, "budget": {"turn_budget": "unbudgeted"}}

        def charge(remaining: int | None) -> int:
            texts = build(None if remaining is None else int((remaining // 2 if half else remaining) * ratio))
            out["texts"] = texts
            if remaining is None:
                return 0
            if texts is None or (len(texts) == 1 and texts[0] in CONTROL_REPLIES):  # protocol text: uncharged
                state = ("nearly_spent" if half and remaining > 0 else "spent") if texts is None else "control"
                out["budget"] = {"turn_budget": state, "charged_tokens": 0, "room_left": remaining}
                return 0
            used = min(remaining, math.ceil(sum(map(len, texts)) / ratio))
            out["budget"] = {"turn_budget": "charged", "charged_tokens": used, "room_left": remaining - used}
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


# A line of a search reply and the span it shows: (page_id, start, end) in a page, or
# ("in_view", turn, message_index, start, end) in the retained window, or None.
Row = tuple[str, "tuple[Any, ...] | None"]


def _newest_first(pages: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """Turn pages newest first, then pages of paged context blocks newest first (D34).

    A context page's turn is the turn at which the proxy paged the block (PR #203 pages a block at a
    rotation, numbered after every turn paged by then), not the turn that sent it, so it says nothing
    about which of a context page and a turn page is newer."""
    def key(item: tuple[str, dict[str, Any]]) -> tuple[bool, int, str]:
        turn = item[1].get("turn")
        return (not is_context_page(item[1]), turn if isinstance(turn, int) and not isinstance(turn, bool) else -1,
                item[0])

    return sorted(pages, key=key, reverse=True)


def _with_context_header(entries: list[tuple[list[Row], dict[str, Any]]]) -> list[list[Row]]:
    """Each entry's rows, with ``SEARCH_CONTEXT_HEADER`` put before the first context page's rows, so
    the line is shown exactly when a context page is. ``entries`` are in ``_newest_first`` order."""
    out: list[list[Row]] = []
    headed = False
    for rows, page in entries:
        if not headed and is_context_page(page):
            rows, headed = [(SEARCH_CONTEXT_HEADER, None), *rows], True
        out.append(rows)
    return out


def _hit_rows(page_id: str, page: dict[str, Any], query: str) -> list[Row]:
    """One search hit: the page line, then its matching lines (>) and context, each with its offset.

    Each line carries the page span it shows, ``(page_id, start, end)`` with ``end`` exclusive, for
    the content-free log that acceptance 3 reads (contract PR #95). A query from a tool input is
    matched in its JSON-escaped form too, as ``render_page`` prints it (B18).
    """
    rows: list[Row] = [(f"{page_id} (turn {page.get('turn')})", None)]  # no stub: the lines carry the content (B17)
    text = render_page(page) if page.get("messages") is not None else ""
    q = Query.parse(query)
    on_page = q is not None and text_matches(text, q)  # else the rest of the query is in the stub only
    shown, total = match_lines(text, q, last=True) if on_page else ([], 0)  # the LAST matching lines (B21)
    rows += [(f"{'>' if is_match else ' '} {start}: {text}", (page_id, start, end)) for start, end, is_match, text in shown]
    if not shown:  # the match is in the stub only: show the stub line as the matching line
        rows.append((f"> stub: {_stub_line(page.get('stub'))}", None))
    elif total > MATCHES_PER_PAGE:  # earlier statements stay reachable from the first match's offset
        first = matching_line_starts(text, q)[0]
        rows.append((f"  [{total - MATCHES_PER_PAGE} more matching lines in this page, the first at offset "
                     f"{first}; call contexto_recall with page_id {page_id} and that offset to read them]", None))
    return rows


def _more_row(page_id: str, page: dict[str, Any], query: str) -> Row:
    """A further matching page, compactly: page id, turn, and where its first and last matching lines
    start (``@first..last``, one offset when they coincide; B21, contract PR #134)."""
    text = render_page(page) if page.get("messages") is not None else ""
    q = Query.parse(query)
    starts = matching_line_starts(text, q) if q is not None and text_matches(text, q) else []
    if not starts:
        return (f"{page_id} (turn {page.get('turn')}) @stub", None)
    first, last = starts[0], starts[-1]
    where = str(first) if first == last else f"{first}..{last}"
    return (f"{page_id} (turn {page.get('turn')}) @{where}", None)


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


def _spans(rows: list[Row]) -> list[dict[str, Any]]:
    """The spans a reply's rows show, content-free, each with its source (contract PR #125, agreed with builder-4).

    Paged spans are ``{source: paged, page_id, start, end}`` into ``render_page``; lines that follow
    each other merge. In-view spans are ``{source: in_view, turn, message_index, start, end}`` into
    that retained entry's text, with no page id, so they never credit recall (acceptance 3).
    """
    spans: list[dict[str, Any]] = []
    for _, span in rows:
        if span is None:
            continue
        if span[0] == "in_view":
            _, turn, index, start, end = span
            spans.append({"source": "in_view", "turn": turn, "message_index": index, "start": start, "end": end})
            continue
        page_id, start, end = span
        if spans and spans[-1].get("page_id") == page_id and spans[-1]["end"] + 1 == start:  # the newline is shown too
            spans[-1]["end"] = end
        else:
            spans.append({"source": "paged", "page_id": page_id, "start": start, "end": end})
    return spans


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
