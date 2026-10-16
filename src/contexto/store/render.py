"""Plain-text rendering of a page, shared by the MCP ``contexto_recall`` tool and ``contexto recall``.

``render_page`` is builder-4's function from PR #24 (head 6cda187), moved here so the MCP server and
the CLI render a page identically. ``recall_chunk`` is the offset and limit rule of ``contexto_recall``.
"""

from __future__ import annotations

import json
from typing import Any

RECALL_LIMIT = 32_000  # characters per contexto_recall call


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
