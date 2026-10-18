"""Page records, page ids, stubs, and the index and pinned text built from them (M2 contract, Store).

Shared by ``SessionStore`` (files) and ``MemoryStore`` (tests), so both produce identical pages and
ids for the same key.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from typing import Any

from contexto.canonical import canonical_bytes
from contexto.store.recall import DEFAULT_LINES, salient_identifiers
from contexto.store.render import as_message, is_responses_item
from contexto.store.search import Query, SearchEntry, rank, search_key, searchable
from contexto.tokens import estimate_message_tokens, estimate_tokens, message_bytes

STUB_TEXT_CHARS = 200
STUB_MAX_PATHS = 20
EMPTY_SYSTEM_STUB = "(empty system message)"
# The tag Claude Code wraps an injected context block in (``contexto.rewrite.CONTEXT_TAG``).
CONTEXT_TAG = "<system-reminder>"

# The mandatory first line of the pinned text (default variant); see contexto.store.recall.
PINNED_FIRST_LINE = DEFAULT_LINES[0]

_PATH_KEYS = ("file_path", "path", "notebook_path")
# Absolute or relative paths with at least one separator and a file name, as written in text.
_PATH_RE = re.compile(r"(?<![\w/.-])(?:~?/|\./|\.\./)?(?:[\w.-]+/)+[\w.-]*\w")


def same_content(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when two page records hold the same hashed content, ``{turn, messages}``."""
    return canonical_bytes({"turn": a.get("turn"), "messages": a.get("messages")}) == canonical_bytes(
        {"turn": b.get("turn"), "messages": b.get("messages")})


def page_id(key: bytes, page: dict[str, Any]) -> str:
    """``pg_`` + first 16 hex of HMAC-SHA256 over the canonical JSON of the page's turn and messages.

    Only the turn number and the exact messages are hashed (contract PR #47). Derived fields (byte
    and token estimates, the stub) are left out, so a change to the estimator or the stub rule never
    changes the id of the same content.
    """
    body = {"turn": page["turn"], "messages": page["messages"]}
    return "pg_" + hmac.new(key, canonical_bytes(body), hashlib.sha256).hexdigest()[:16]


def _user_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _view(turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The turn as Messages API messages: Messages pages unchanged, Responses items mapped (B23)."""
    return [m for m in (as_message(item) for item in turn) if isinstance(m, dict)]


def _texts(turn: list[dict[str, Any]]):
    """Every human-readable string in a turn: text, tool inputs, and tool results (both dialects)."""
    for message in _view(turn):
        content = message.get("content")
        if isinstance(content, str):
            yield content
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                yield block.get("text", "")
            elif kind == "tool_use":
                tool_input = block.get("input")
                if isinstance(tool_input, str):  # a Codex custom_tool_call's raw input (B23)
                    yield tool_input
                for v in (tool_input.values() if isinstance(tool_input, dict) else ()):
                    if isinstance(v, str):
                        yield v
            elif kind == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    yield inner
                for b in inner if isinstance(inner, list) else []:
                    if isinstance(b, dict) and b.get("type") == "text":
                        yield b.get("text", "")


def _system_first_line(turn: list[dict[str, Any]]) -> str | None:
    """First non-empty line of the turn's system text, a placeholder if it is empty, None if no system message."""
    systems = [m for m in turn if isinstance(m, dict) and m.get("role") == "system"]
    if not systems:
        return None
    for message in systems:
        for line in _user_text(message).splitlines():
            if line.strip():
                return line.strip()[:STUB_TEXT_CHARS]
    return EMPTY_SYSTEM_STUB


def build_stub(turn: list[dict[str, Any]]) -> dict[str, Any]:
    """First 200 characters of the turn-start user text, tool names used, and file paths mentioned.

    A page with no user text but a system message (lane A's carried-text fallback pages each such
    message on its own) adds ``system_text``: the first non-empty line of the system text, or a
    placeholder when it is empty. Pages with user text keep exactly the three fields, so their ids
    are unchanged.
    """
    view = _view(turn)  # Responses (Codex) items as Messages API messages, the same view search reads (B23)
    first_user = next((m for m in view if m.get("role") == "user"), {})
    tools: list[str] = []
    paths: list[str] = []
    for message in view:
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if name and name not in tools:
                    tools.append(name)
                for key in _PATH_KEYS:
                    tool_input = block.get("input")
                    value = tool_input.get(key) if isinstance(tool_input, dict) else None
                    if isinstance(value, str) and value and value not in paths:
                        paths.append(value)
    for text in _texts(turn):
        for match in _PATH_RE.findall(text):
            if match not in paths:
                paths.append(match)
    stub = {
        "user_text": _user_text(first_user)[:STUB_TEXT_CHARS],
        "tools": tools,
        "paths": paths[:STUB_MAX_PATHS],
    }
    if not stub["user_text"].strip():
        system = _system_first_line(turn)
        if system is not None:
            stub["system_text"] = system
    return stub


def current_stub(page: dict[str, Any]) -> dict[str, Any]:
    """The page with its stub rebuilt if it is a pre-v0.1.3 Codex stub, else the page itself.

    v0.1.0 to v0.1.2 stored every Responses (Codex) page with empty user text, tools, and paths: the
    stub rule read only Messages API blocks (fixed by B23). A page keeps its exact messages, so the
    stub is rebuilt from them with the current rule, in memory only: the page file, its id, and its
    content are never changed (pages are immutable). Only that empty shape on a page holding a
    Responses item is rebuilt; Messages API pages and any stub with a field filled are returned as
    stored, and so is a rebuild that comes out the same. Idempotent.
    """
    stub = page.get("stub")
    if not isinstance(stub, dict) or stub.get("user_text") or stub.get("tools") or stub.get("paths"):
        return page
    messages = page.get("messages")
    if not isinstance(messages, list) or not any(isinstance(m, dict) and is_responses_item(m) for m in messages):
        return page
    rebuilt = build_stub(messages)
    return page if rebuilt == stub else {**page, "stub": rebuilt}


def build_page(key: bytes, turn: list[dict[str, Any]], turn_number: int) -> dict[str, Any]:
    """A page record for one turn, messages kept exactly as received (thinking included)."""
    text, tool = message_bytes(turn)
    page = {
        "turn": turn_number,
        "messages": turn,
        "bytes": text + tool,
        "tokens": estimate_message_tokens(turn),
        "stub": build_stub(turn),
    }
    page["id"] = page_id(key, page)
    return page


def searchable_text(page: dict[str, Any]) -> str:
    """What search reads on a page, folded: the stub fields and every text of the page's turn (D34)."""
    stub = page["stub"]
    return searchable([stub["user_text"], stub.get("system_text", ""), *stub["tools"], *stub["paths"],
                       *_texts(page["messages"])])


def _is_context_text(text: Any) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(CONTEXT_TAG)


def is_context_page(page: dict[str, Any]) -> bool:
    """True for a page that holds no turn: only system messages, or user messages made only of
    ``<system-reminder>`` context blocks. Lane A's fallback pages carried system messages one per
    page, and PR #203 pages each paged context block as a user message holding that block. Read from
    the messages, so the stub's form (PR #203 may give such pages a system-like stub) does not matter.
    Search ranks these pages after every turn page (D34)."""
    messages = page.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    for message in messages:
        if not isinstance(message, dict):
            return False
        role, content = message.get("role"), message.get("content")
        if role == "system":
            continue
        if role != "user":
            return False
        if isinstance(content, str):
            if not _is_context_text(content):
                return False
        elif not (isinstance(content, list) and content and all(
                isinstance(b, dict) and b.get("type") == "text" and _is_context_text(b.get("text"))
                for b in content)):
            return False
    return True


def search_entry(page: dict[str, Any]) -> SearchEntry:
    """What search reads of a page (``contexto.store.search.rank``)."""
    return SearchEntry(page["id"], page["turn"], search_key(searchable_text(page)), is_context_page(page))


def page_matches(page: dict[str, Any], query: str) -> bool:
    """Every word of ``query`` occurs in the page's stub or text, case-insensitively, in any order (D34)."""
    q = Query.parse(query)
    if q is None:
        return False
    text = searchable_text(page)
    return all(w in text for w in q.words)


def search_pages(pages: list[dict[str, Any]], query: str) -> list[str]:
    """Ids of the pages matching ``query``, best first (``contexto.store.search``; D34). Not RAG: no
    embeddings and no similarity, only the query's own words, ranked by BM25 and recency."""
    return rank([search_entry(p) for p in pages], query)


_TOKEN = re.compile(r"[\w.\-/]+")


def index_line(page: dict[str, Any], salient: bool = False) -> str:
    stub = page["stub"]
    text = " ".join(stub["user_text"].split())
    if not text and stub.get("system_text"):
        text = "[system] " + stub["system_text"]
    line = f"{page['id']} turn {page['turn']}: {text}"
    if stub["tools"]:
        line += f" [tools: {', '.join(stub['tools'])}]"
    if stub["paths"]:
        line += f" [files: {', '.join(stub['paths'])}]"
    if salient:
        shown = set(_TOKEN.findall(line))  # an identifier already on the line adds nothing
        ids = [i for i in salient_identifiers(_texts(page["messages"])) if i not in shown]
        if ids:
            line += f" [ids: {', '.join(ids)}]"
    return line


def render_index(pages: list[dict[str, Any]], budget_tokens: int, salient: bool = False,
                 line_for: Callable[[dict[str, Any]], str] | None = None) -> str:
    """One line per page, newest first, within the budget, then a count of older pages omitted.

    Only ``id``, ``turn``, and ``stub`` of each page are read, and ``messages`` for the ``salient``
    identifiers. ``line_for(page)`` overrides ``index_line(page, salient)``; ``SessionStore`` passes
    one that keeps each page's line, so a page is read for its identifiers once per process (D34).
    """
    ordered = sorted(pages, key=lambda p: (p["turn"], p["id"]), reverse=True)
    lines: list[str] = []
    used = 0
    for i, page in enumerate(ordered):
        line = line_for(page) if line_for is not None else index_line(page, salient)
        rest = len(ordered) - i - 1
        tail = f"({rest} older pages omitted; use contexto_search to find them)" if rest else ""
        cost = estimate_tokens(line + "\n") + (estimate_tokens(tail) if tail else 0)
        if used + cost > budget_tokens:
            omitted = len(ordered) - i
            lines.append(f"({omitted} older pages omitted; use contexto_search to find them)")
            break
        lines.append(line)
        used += estimate_tokens(line + "\n")
    return "\n".join(lines)

