"""Page records, page ids, stubs, and the index and pinned text built from them (M2 contract, Store).

Shared by ``SessionStore`` (files) and ``MemoryStore`` (tests), so both produce identical pages and
ids for the same key.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from contexto.canonical import canonical_bytes
from contexto.tokens import estimate_message_tokens, estimate_tokens, message_bytes

STUB_TEXT_CHARS = 200
STUB_MAX_PATHS = 20

PINNED_FIRST_LINE = (
    "Earlier turns of this conversation are paged out. Each line of the page index below is one "
    "paged turn; call contexto_recall with its page id to read it, or contexto_search to find one."
)

_PATH_KEYS = ("file_path", "path", "notebook_path")
# Absolute or relative paths with at least one separator and a file name, as written in text.
_PATH_RE = re.compile(r"(?<![\w/.-])(?:~?/|\./|\.\./)?(?:[\w.-]+/)+[\w.-]*\w")


def page_id(key: bytes, page: dict[str, Any]) -> str:
    """``pg_`` + first 16 hex of HMAC-SHA256 over the canonical JSON of the page without its id."""
    body = {k: v for k, v in page.items() if k != "id"}
    return "pg_" + hmac.new(key, canonical_bytes(body), hashlib.sha256).hexdigest()[:16]


def _user_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _texts(turn: list[dict[str, Any]]):
    """Every human-readable string in a turn: text, tool inputs, and tool results."""
    for message in turn:
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
                for v in (block.get("input") or {}).values():
                    if isinstance(v, str):
                        yield v
            elif kind == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    yield inner
                for b in inner if isinstance(inner, list) else []:
                    if isinstance(b, dict) and b.get("type") == "text":
                        yield b.get("text", "")


def build_stub(turn: list[dict[str, Any]]) -> dict[str, Any]:
    """First 200 characters of the turn-start user text, tool names used, and file paths mentioned."""
    first_user = next((m for m in turn if m.get("role") == "user"), {})
    tools: list[str] = []
    paths: list[str] = []
    for message in turn:
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if name and name not in tools:
                    tools.append(name)
                for key in _PATH_KEYS:
                    value = (block.get("input") or {}).get(key)
                    if isinstance(value, str) and value and value not in paths:
                        paths.append(value)
    for text in _texts(turn):
        for match in _PATH_RE.findall(text):
            if match not in paths:
                paths.append(match)
    return {
        "user_text": _user_text(first_user)[:STUB_TEXT_CHARS],
        "tools": tools,
        "paths": paths[:STUB_MAX_PATHS],
    }


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


def page_matches(page: dict[str, Any], query: str) -> bool:
    """Exact, case-sensitive substring match over the stub and every text in the page's turn."""
    if not query:
        return False
    stub = page["stub"]
    haystacks = [stub["user_text"], *stub["tools"], *stub["paths"], *_texts(page["messages"])]
    return any(query in h for h in haystacks)


def search_pages(pages: list[dict[str, Any]], query: str) -> list[str]:
    """Ids of pages containing ``query``, newest first. No ranking and no similarity (not RAG)."""
    hits = [p for p in pages if page_matches(p, query)]
    return [p["id"] for p in sorted(hits, key=lambda p: (p["turn"], p["id"]), reverse=True)]


def index_line(page: dict[str, Any]) -> str:
    stub = page["stub"]
    text = " ".join(stub["user_text"].split())
    line = f"{page['id']} turn {page['turn']}: {text}"
    if stub["tools"]:
        line += f" [tools: {', '.join(stub['tools'])}]"
    if stub["paths"]:
        line += f" [files: {', '.join(stub['paths'])}]"
    return line


def render_index(pages: list[dict[str, Any]], budget_tokens: int) -> str:
    """One line per page, newest first, within the budget, then a count of older pages omitted."""
    ordered = sorted(pages, key=lambda p: (p["turn"], p["id"]), reverse=True)
    lines: list[str] = []
    used = 0
    for i, page in enumerate(ordered):
        line = index_line(page)
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


def render_pinned(budget_tokens: int) -> str:
    """Pinned text without a ledger (B1): only the mandatory first line. B2 adds ledger items."""
    return PINNED_FIRST_LINE
