"""Plain-text rendering of a page and the per-call recall size, shared by the MCP ``contexto_recall``
tool and ``contexto recall``.

``render_page`` is builder-4's function from PR #24 (head 6cda187), moved here so the MCP server and
the CLI render a page identically. ``recall_chunk`` is the offset and limit rule of ``contexto_recall``.
"""

from __future__ import annotations

import json
from typing import Any

RECALL_LIMIT = 32_000  # characters per contexto_recall call, at most
DEFAULT_RECALL_LIMIT = 12_000  # characters per call before the engine has recorded the window
MAX_CHARS_PER_TOKEN = 1.5  # W converts to characters at no more than this many per token


def recall_limit(window_tokens: int | None, bytes_per_token: float | None) -> tuple[int, str]:
    """Characters per recall call and a note saying how the limit was set (M2 contract PR #53).

    Recall results arrive inside a tool loop, where the engine cannot rotate, so one call is kept to
    about a quarter of the session's window budget ``W``: ``W / 4`` tokens at the smaller of the
    measured bytes per token and 1.5, and never more than 32,000 characters. Before the engine has
    recorded ``W`` (``SessionStore.set_window``), the limit is 12,000 characters.
    """
    if not window_tokens or not bytes_per_token:
        return DEFAULT_RECALL_LIMIT, (f"chunks use the {DEFAULT_RECALL_LIMIT:,}-character default because this "
                                      "session's window budget is not recorded yet")
    chars = int(window_tokens / 4 * min(bytes_per_token, MAX_CHARS_PER_TOKEN))
    limit = max(1, min(RECALL_LIMIT, chars))
    if limit == RECALL_LIMIT:
        return limit, f"chunks are at most {RECALL_LIMIT:,} characters"
    return limit, f"chunks are {limit:,} characters, about a quarter of this session's window budget"


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


def recall_chunk(page: dict[str, Any], offset: int = 0, limit: int = RECALL_LIMIT) -> dict[str, Any]:
    """Up to ``limit`` characters of the rendered page from ``offset``.

    Returns ``{text, offset, next_offset, total_chars}``; ``next_offset`` is None at the end. An
    offset equal to the length is allowed and returns empty text. Raises ValueError for a negative
    offset or one past the end.
    """
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    text = render_page(page)
    if offset > len(text):
        raise ValueError(f"offset {offset} is past the end of the page ({len(text)} characters)")
    chunk = text[offset : offset + limit]
    end = offset + len(chunk)
    return {"text": chunk, "offset": offset, "next_offset": end if end < len(text) else None,
            "total_chars": len(text)}


MATCH_CONTEXT = 1  # lines shown before and after each matching line (B17: 1, down from 2)
MATCHES_PER_PAGE = 3  # matching lines shown per page, each with its context
MATCH_LINE_CHARS = 200  # characters shown of any one line


def match_lines(text: str, query: str, context: int = MATCH_CONTEXT, per_page: int = MATCHES_PER_PAGE,
                line_chars: int = MATCH_LINE_CHARS) -> tuple[list[tuple[int, int, bool, str]], int]:
    """The lines of rendered page ``text`` that contain ``query``, with bounded context (M2 B15).

    Returns ``(shown, total)``: ``shown`` holds ``(start, end, is_match, line)`` for the first
    ``per_page`` matching lines and up to ``context`` lines either side, in page order, overlaps
    merged; ``total`` counts every matching line. ``text[start:end]`` is the part of the page a
    line shows, so ``contexto_recall`` can read on from ``start``. A line longer than
    ``line_chars`` is cut to that many characters, marked with a trailing " ...". A matching line
    is cut around its first match instead, widened when the query is long, so the whole match stays
    in view. Linear in the size of ``text``.
    """
    starts, matching = [0], []
    newline = text.find("\n")
    while newline != -1:
        starts.append(newline + 1)
        newline = text.find("\n", newline + 1)
    ends = [s - 1 for s in starts[1:]] + [len(text)]
    pos, line = text.find(query), 0
    while pos != -1:
        while ends[line] < pos:
            line += 1
        if not matching or matching[-1][0] != line:
            matching.append((line, pos))
        pos = text.find(query, pos + 1)
    shown: list[tuple[int, int, bool, str]] = []
    first_match = dict(matching[:per_page])
    wanted = sorted({n for m in first_match for n in range(max(0, m - context), min(len(starts), m + context + 1))})
    for n in wanted:
        start, end = starts[n], ends[n]
        width = line_chars
        if n in first_match and first_match[n] + len(query) > start + line_chars:
            start = max(start, first_match[n] - line_chars // 3)
            width = max(line_chars, first_match[n] + len(query) - start)  # the whole match, however long
        stop = min(end, start + width)
        shown.append((start, stop, n in first_match, text[start:stop] + (" ..." if stop < end else "")))
    return shown, len(matching)
