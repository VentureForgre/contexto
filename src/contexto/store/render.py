"""Plain-text rendering of a page and the per-call recall size, shared by the MCP ``contexto_recall``
tool and ``contexto recall``.

``render_page`` is builder-4's function from PR #24 (head 6cda187), moved here so the MCP server and
the CLI render a page identically. ``recall_chunk`` is the offset and limit rule of ``contexto_recall``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from contexto.canonical import loads_bounded
from contexto.store.search import Query, matching_lines, text_matches

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


# Contexto's own MCP tools whose results copy older pages or the pinned state (D51): search, recall, and
# state. contexto_pin and contexto_close are not among them: their inputs are the model's own words, and
# stay searchable. Claude Code, and Codex calling one directly, prefix the server's name; Codex's code mode
# calls them from the JavaScript of its exec tool, as tools["mcp__contexto__contexto_search"](...) or
# tools.mcp__contexto__contexto_search(...).
OWN_TOOL_PREFIX = "mcp__contexto__"
ECHO_TOOLS = tuple(OWN_TOOL_PREFIX + name for name in ("contexto_search", "contexto_recall", "contexto_state"))
CODE_MODE_TOOL = "exec"
_ECHO_NAME = "(?:" + "|".join(ECHO_TOOLS) + ")"
_TOOLS_WORD = re.compile(r"\btools\b")
# A literal call of one of those tools on the code-mode tools object: the whole reference and its "(".
_ECHO_CODE_CALL = re.compile(r"""\btools\s*(?:\[\s*(["'`])""" + _ECHO_NAME + r"""\1\s*\]|\.""" + _ECHO_NAME
                             + r""")\s*\(""")
_CALL_TYPES = ("function_call", "custom_tool_call")
_OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output")


def _code(arguments: Any) -> str | None:
    """The code of a code-mode call: a custom tool's input as it is, or the string fields of a function
    call's JSON arguments (their quotes unescaped), None for anything else."""
    if not isinstance(arguments, str):
        return None
    try:
        decoded = loads_bounded(arguments)
    except ValueError:
        return arguments
    if isinstance(decoded, dict):
        return "\n".join(v for v in decoded.values() if isinstance(v, str))
    return decoded if isinstance(decoded, str) else arguments


def _own_call(name: Any, code: str | None) -> bool:
    """A call of search, recall, or state. An exec call counts only when every mention of ``tools`` in its
    code is a literal call of one of them (at least one) and the code has no comment: any other way of
    reaching a tool (destructuring, an alias, a computed or optional member, another tool) may run a
    tool whose output must stay searchable, so such a call is not counted (D51)."""
    if isinstance(name, str) and name in ECHO_TOOLS:
        return True
    if name != CODE_MODE_TOOL or code is None or "//" in code or "/*" in code:
        return False
    echo = len(_ECHO_CODE_CALL.findall(code))
    return echo > 0 and echo == len(_TOOLS_WORD.findall(code))


def own_call_ids(messages: Any) -> set[str]:
    """The ids of a turn's calls of Contexto's search, recall, and state tools (``ECHO_TOOLS``):
    ``tool_use`` ids (Messages API) and ``call_id`` of calls (Responses API). Their inputs and results
    are kept verbatim in the page, for recall, but search, the stub, and the index do not read them: a
    search result is a copy of older pages, and as the newest page holding it, it outranked the pages
    it quoted (D51). Pin and close calls are the model's own words and stay searchable."""
    ids: set[str] = set()
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        if is_responses_item(message):
            call_id = message.get("call_id")
            kind = message.get("type")
            if kind in _CALL_TYPES and isinstance(call_id, str) and _own_call(
                    message.get("name"), _code(message.get("arguments" if kind == "function_call" else "input"))):
                ids.add(call_id)
            continue
        content = message.get("content") if message.get("role") == "assistant" else None
        for block in content if isinstance(content, list) else []:
            if (isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str)
                    and _own_call(block.get("name"), None)):
                ids.add(block["id"])
    return ids


def is_own_item(item: Any, own: set[str]) -> bool:
    """A Responses item that is one of ``own``'s calls or its output."""
    call_id = item.get("call_id") if isinstance(item, dict) else None
    return (bool(own) and isinstance(call_id, str) and call_id in own
            and item.get("type") in (*_CALL_TYPES, *_OUTPUT_TYPES))


def is_own_block(block: Any, own: set[str]) -> bool:
    """A Messages API ``tool_use`` or ``tool_result`` block of one of ``own``'s calls."""
    if not own or not isinstance(block, dict):
        return False
    key = block.get("id") if block.get("type") == "tool_use" else (
        block.get("tool_use_id") if block.get("type") == "tool_result" else None)
    return isinstance(key, str) and key in own


def _blank(lines: list[str]) -> list[str]:
    return [re.sub(r"[^\n]", " ", line) for line in lines]


def render_page(page: dict[str, Any], searchable: bool = False) -> str:
    """Render a page's messages as plain text, thinking omitted. Deterministic for a given page.

    Any value inside ``messages`` may be damaged in a page file a person edited: a message, content,
    block, text, tool output, or Codex item type of the wrong type is shown as ``[malformed ...
    omitted]`` in its place, so this never raises for a page ``check_page`` passed (rule 18), and a
    partly damaged output does not read as complete.

    A page from a Codex session holds Responses API items (M2 B20); each is rendered by
    ``_responses_lines``, the same way the engine's retained-window snapshot renders it (A14).

    With ``searchable``, the lines of Contexto's search, recall, and state calls and results (``own_call_ids``) are
    blanked to spaces, so search matches no line in them while every offset stays the offset of the
    same line in the verbatim text that recall reads (D51)."""
    lines: list[str] = []
    turn = page.get("turn")
    messages = page.get("messages")
    own = own_call_ids(messages) if searchable else set()
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            lines.append("[malformed message omitted]")
            continue
        if is_responses_item(message):
            item_lines = _responses_lines(turn, message)
            lines.extend(_blank(item_lines) if is_own_item(message, own) else item_lines)
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
            mark = len(lines)
            if kind == "text":
                lines.append(_text(block))
            elif kind in ("thinking", "redacted_thinking"):
                continue
            elif kind == "tool_use":
                tool_input = block.get("input", {})
                try:
                    args = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
                except (ValueError, TypeError, RecursionError):  # cannot be written: shown as its text
                    args = tool_input if isinstance(tool_input, str) else f"<{type(tool_input).__name__}>"
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
                            lines.append(_text(part))
                        else:
                            lines.append(f"[{part.get('type', 'block')} omitted]")
                elif inner is not None:  # a number or an object: damaged, not an empty output
                    lines.append("[malformed content omitted]")
            else:
                lines.append(f"[{kind} omitted]")
            if is_own_block(block, own):
                lines[mark:] = _blank(lines[mark:])
    return "\n".join(lines)


def _text(block: dict[str, Any]) -> str:
    """A text block's text; a placeholder when it is not a string (a damaged page file, which search
    does not read either: ``contexto.store.pages``)."""
    text = block.get("text", "")
    return text if isinstance(text, str) else "[malformed text omitted]"


def is_responses_item(message: dict[str, Any]) -> bool:
    """A Responses API item rather than a Messages API message: it has a top-level ``type`` (a Messages API
    message never does), or, typeless like a message the CLI sent without one, its content parts use the
    Responses part types (``input_*`` or ``output_text``), which the Messages API never uses."""
    if "type" in message:
        return True
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and isinstance(part.get("type"), str)
        and (part["type"].startswith("input_") or part["type"] == "output_text") for part in content)


def _responses_lines(turn: Any, item: dict[str, Any]) -> list[str]:
    """One Responses item's lines (M2 B20). The item is mapped to Messages API blocks by lane A's
    ``ResponsesDialect.retained_blocks`` (A14), the mapping the retained-window snapshot uses, and those
    blocks are rendered by ``render_page`` itself, so a page line and an in-view line read the same:
    message text parts as the text (Codex context sections, images, and files left out), a
    ``function_call`` or ``custom_tool_call`` as ``tool_use <name> <sorted JSON input>``, and an output
    as ``tool_result:`` and its text. Reasoning, other item types, and system-like items (developer
    and system messages, context-only user messages, ``additional_tools``) render nothing."""
    message = as_message(item)
    return [] if message is None else render_page({"turn": turn, "messages": [message]}).split("\n")


def as_message(message: Any) -> dict[str, Any] | None:
    """A page message as a Messages API message: a Messages API message as it is (the same object),
    a Responses item through ``ResponsesDialect.retained_blocks`` (B20), or None for an item that is
    not searchable (system-like, reasoning, other types). Rendering, search, and the stub all read
    pages through this one mapping (B23), so what search matches is what the page shows."""
    if not isinstance(message, dict) or not is_responses_item(message):
        return message
    from contexto.codex import RESPONSES  # imported here: contexto.codex builds on the engine, which imports this

    if RESPONSES.is_system_like(message):
        return None
    blocks = RESPONSES.retained_blocks(message)
    return {"role": blocks[0][0], "content": [block for _, block in blocks]} if blocks else None


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


def match_lines(text: str, query: str | Query, context: int = MATCH_CONTEXT, per_page: int = MATCHES_PER_PAGE,
                line_chars: int = MATCH_LINE_CHARS, last: bool = False) -> tuple[list[tuple[int, int, bool, str]], int]:
    """The lines of rendered page ``text`` that match ``query``, with bounded context (M2 B15, D34).

    The matching lines are those holding the most of the query (``contexto.store.search``): the
    whole query, case-insensitively, else all its words, else the most of them. Returns ``(shown,
    total)``: ``shown`` holds ``(start, end, is_match, line)`` for the first ``per_page`` matching
    lines (the last ``per_page`` with ``last``, as search shows them: a turn's latest statements,
    B21) and up to ``context`` lines either side, in page order, overlaps merged; ``total`` counts
    every matching line. ``text[start:end]`` is the part of the page a line shows, so
    ``contexto_recall`` can read on from ``start``. A line longer than ``line_chars`` is cut to that
    many characters, marked with a trailing " ...". A matching line is cut around its match instead,
    widened when the match is long, so the whole match stays in view. Linear in the size of ``text``
    for a query whose words are on few lines.
    """
    q = Query.parse(query) if isinstance(query, str) else query
    if q is None:
        return [], 0
    chosen, where = matching_lines(text, q)
    if not chosen:
        return [], 0
    starts, ends = _line_bounds(text)
    picked = chosen[-per_page:] if last else chosen[:per_page]
    first_match = {n: where[n] for n in picked}
    wanted = sorted({n for m in first_match for n in range(max(0, m - context), min(len(starts), m + context + 1))})
    shown: list[tuple[int, int, bool, str]] = []
    for n in wanted:
        start, end = starts[n], ends[n]
        width = line_chars
        if n in first_match:
            pos, size = first_match[n]
            if pos + size > start + line_chars:
                start = max(start, pos - line_chars // 3)
                width = max(line_chars, pos + size - start)  # the whole match, however long
        stop = min(end, start + width)
        shown.append((start, stop, n in first_match, text[start:stop] + (" ..." if stop < end else "")))
    return shown, len(chosen)


def retained_matches(entries: list[dict[str, Any]], query: str | Query, *,
                     asked: bool = True) -> list[tuple[int, int, int, int, str]]:
    """The matching lines of a retained-window snapshot's ``entries`` (``{turn, message_index, role, text}``,
    contract PR #125), newest first: ``(turn, message_index, start, end, line)``, every matching line of
    each entry that matches as a page does (D34), with no context, ``text[start:end]`` the part shown.

    With ``asked`` (the current session's own window) the newest turn's user entries are left out: they
    are the question being asked, not a statement (B22, PR #143). An ended session's last window has no
    question being asked, so its search passes ``asked=False`` (D69)."""
    q = Query.parse(query) if isinstance(query, str) else query
    if q is None:
        return []
    found = []
    newest = max((e["turn"] for e in entries), default=None)
    for entry in entries:
        if asked and entry["turn"] == newest and entry["role"] == "user":
            continue
        if not text_matches(entry["text"], q):
            continue
        every = entry["text"].count("\n") + 1  # ALL matching lines: the newest are chosen by the caller
        shown, _ = match_lines(entry["text"], q, context=0, per_page=every)
        found += [(entry["turn"], entry["message_index"], start, end, text) for start, end, is_match, text in shown
                  if is_match]
    found.sort(key=lambda m: (m[0], m[1], m[2]), reverse=True)
    return found


def matching_line_starts(text: str, query: str | Query) -> list[int]:
    """Where each matching line of ``text`` starts (see ``match_lines``), in page order."""
    q = Query.parse(query) if isinstance(query, str) else query
    if q is None:
        return []
    chosen, _ = matching_lines(text, q)
    if not chosen:
        return []
    starts, _ = _line_bounds(text)
    return [starts[n] for n in chosen]


def _line_bounds(text: str) -> tuple[list[int], list[int]]:
    starts = [0]
    newline = text.find("\n")
    while newline != -1:
        starts.append(newline + 1)
        newline = text.find("\n", newline + 1)
    return starts, [s - 1 for s in starts[1:]] + [len(text)]
