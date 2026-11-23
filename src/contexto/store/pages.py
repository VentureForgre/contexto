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
from contexto.store.render import (
    as_message,
    is_developer_only_page,
    is_own_block,
    is_own_item,
    is_responses_item,
    own_call_ids,
)
from contexto.store.search import Query, SearchEntry, rank, search_text, searchable
from contexto.tokens import estimate_message_tokens, estimate_tokens, message_bytes

STUB_TEXT_CHARS = 200
SHORT_USER_TEXT_CHARS = 20  # short prompts ("continue", "fix test") also record a concise assistant outcome
STUB_ASSISTANT_CHARS = 120
# D63: the kinds of page in-turn paging writes (``contexto.inturn``). A page without ``kind`` is a whole
# turn, as every page written before D63 is. A result page holds one tool call and its result verbatim;
# a rounds page holds tool rounds folded out of a running turn. ``kind`` is not part of the page id.
RESULT_PAGE = "result"
ROUNDS_PAGE = "rounds"
FOLDS_PAGE = "folds"  # the detailed fold notes of a turn's earlier folds, once its note reached its cap
ROUNDS_STUB = "[earlier tool rounds]"
RESULT_STUB = "[paged tool result]"
STUB_MAX_PATHS = 20
EMPTY_SYSTEM_STUB = "(empty system message)"
EMPTY_REMINDER_STUB = "(empty reminder)"
# Claude Code wraps injected context in this tag (context blocks of turn-start messages on 4.x ids,
# the system text of claude-sonnet-5 and claude-fable-5); it is ``contexto.rewrite.CONTEXT_TAG``. A
# stub shows the first line inside it: the tag line alone says nothing, and in the preamble's index it
# reads as a live instruction. Search also uses it to tell a paged context block from a turn (D34).
REMINDER_TAG = "<system-reminder>"

# The mandatory first line of the pinned text (default variant); see contexto.store.recall.
PINNED_FIRST_LINE = DEFAULT_LINES[0]

_PATH_KEYS = ("file_path", "path", "notebook_path")
# Absolute or relative paths with at least one separator and a file name, as written in text.
_PATH_RE = re.compile(r"(?<![\w/.-])(?:~?/|\./|\.\./)?(?:[\w.-]+/)+[\w.-]*\w")
_URL_RE = re.compile(r"(?:https?|ftp|wss?|ssh|file|mailto)://[^\s<>\"'`)\]}]+", re.IGNORECASE)
_MIME_PREFIXES = frozenset({
    "application", "audio", "font", "image", "message", "model", "multipart", "text", "video",
})
_WRITE_TOOL_TOKENS = frozenset({
    "edit", "write", "patch", "create", "delete", "rename", "move", "replace",
    "multiedit", "notebookedit",
})
_PATCH_FILE_RE = re.compile(r"^\*\*\*\s+(?:Update|Add|Delete|Move to)\s+File:\s*(.+?)\s*$", re.MULTILINE)
_TRIVIAL_OUTCOMES = frozenset({"ok", "okay", "done", "noted", "yes", "sure", "got it", "understood"})

# Optional secret-pattern redaction before persisting raw pages (opt-in via CONTEXTO_REDACT_SECRETS).
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"), "[REDACTED]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s/@]+:)[^@\s/]+(@)"),
     r"\1[REDACTED]\2"),
)


def redact_secrets_text(text: str) -> str:
    """Replace high-confidence secret patterns in ``text`` with ``[REDACTED]``."""
    out = text
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_page_messages(value: Any) -> Any:
    """Copy ``value`` with secret patterns redacted from all string values, iteratively (safe at MAX_JSON_DEPTH)."""
    if isinstance(value, str):
        return redact_secrets_text(value)
    if not isinstance(value, (dict, list)):
        return value
    root: Any = {} if isinstance(value, dict) else []
    stack: list[tuple[Any, Any]] = [(value, root)]
    while stack:
        src, dst = stack.pop()
        if isinstance(src, dict):
            for k, v in src.items():
                if isinstance(v, str):
                    dst[k] = redact_secrets_text(v)
                elif isinstance(v, dict):
                    child: dict[str, Any] = {}
                    dst[k] = child
                    stack.append((v, child))
                elif isinstance(v, list):
                    child_list: list[Any] = []
                    dst[k] = child_list
                    stack.append((v, child_list))
                else:
                    dst[k] = v
        else:
            for v in src:
                if isinstance(v, str):
                    dst.append(redact_secrets_text(v))
                elif isinstance(v, dict):
                    child = {}
                    dst.append(child)
                    stack.append((v, child))
                elif isinstance(v, list):
                    child_list = []
                    dst.append(child_list)
                    stack.append((v, child_list))
                else:
                    dst.append(v)
    return root


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
    return "\n".join(b.get("text", "") for b in (content if isinstance(content, list) else []) if _is_text(b))


def _is_text(block: Any) -> bool:
    """A text block whose text is a string. Search, the stub, and the index skip any other text, as
    ``render_page`` does (a damaged page file, rule 18)."""
    return isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text", ""), str)


def _view(turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The turn as Messages API messages: Messages pages unchanged, Responses items mapped (B23)."""
    dev_only = is_developer_only_page(turn)
    return [m for m in (as_message(item, include_developer=dev_only) for item in turn) if isinstance(m, dict)]


def _walk_strings(value: Any):
    """Yield every string inside nested dicts and lists, iteratively (safe against deep nesting)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def _texts(turn: list[dict[str, Any]]):
    """Every human-readable string in a turn: text, tool inputs (including nested dicts/lists), and tool
    results (both dialects), except the inputs and results of Contexto's search, recall, and state calls
    (``own_call_ids``, D51)."""
    own = own_call_ids(turn)
    dev_only = is_developer_only_page(turn)
    for item in turn:
        message = as_message(item, include_developer=dev_only)
        if not isinstance(message, dict) or is_own_item(item, own):
            continue
        content = message.get("content")
        if isinstance(content, str):
            yield content
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or is_own_block(block, own):
                continue
            kind = block.get("type")
            if kind == "text":
                if _is_text(block):
                    yield block.get("text", "")
            elif kind == "tool_use":
                tool_input = block.get("input")
                if isinstance(tool_input, (str, dict, list)):
                    yield from _walk_strings(tool_input)
            elif kind == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    yield inner
                for b in inner if isinstance(inner, list) else []:
                    if _is_text(b):
                        yield b.get("text", "")


def _first_line(text: str) -> str | None:
    """First non-empty line of ``text`` with ``<system-reminder>`` tags removed, None if there is none."""
    for line in text.replace(REMINDER_TAG, "\n").replace("</system-reminder>", "\n").splitlines():
        if line.strip():
            return line.strip()[:STUB_TEXT_CHARS]
    return None


def _system_first_line(turn: list[dict[str, Any]]) -> str | None:
    """First non-empty line of the turn's system (or standalone Codex developer) text (inside the tag,
    if tagged), a placeholder if it is empty, None if no system/developer message."""
    dev_only = is_developer_only_page(turn)
    if dev_only:
        systems = _view(turn)
        if not systems:
            return EMPTY_SYSTEM_STUB
    else:
        systems = [m for m in turn if isinstance(m, dict) and m.get("role") == "system"]
        if not systems:
            return None
    for message in systems:
        line = _first_line(_user_text(message))
        if line is not None:
            return line
    return EMPTY_SYSTEM_STUB


def _is_reminder(block: Any) -> bool:
    return (isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
            and block["text"].lstrip().startswith(REMINDER_TAG))


def _reminder_first_line(turn: list[dict[str, Any]]) -> str | None:
    """For a page whose only message is a user message made entirely of context blocks (the bounded
    fallback pages a context block as such a message, D32): the first non-empty line inside the tag,
    or a placeholder when it is empty. None for any other page."""
    if len(turn) != 1 or not isinstance(turn[0], dict) or turn[0].get("role") != "user":
        return None
    content = turn[0].get("content")
    if not isinstance(content, list) or not content or not all(_is_reminder(b) for b in content):
        return None
    for block in content:
        line = _first_line(block["text"])
        if line is not None:
            return line
    return EMPTY_REMINDER_STUB


def _is_write_tool(name: str) -> bool:
    """Whether a tool name edits, writes, patches, creates, or moves files."""
    bare = name.rsplit("__", 1)[-1]
    tokens = {t for t in re.split(r"[^a-z0-9]+", bare.lower()) if t}
    return bool(tokens & _WRITE_TOOL_TOKENS)


def _walk_tool_paths(tool_input: Any, is_write: bool) -> list[str]:
    """Extract file paths from a tool_use input (including nested dicts/lists and apply_patch headers)."""
    found: list[str] = []
    stack = [tool_input]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in _PATH_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value and value not in found:
                    found.append(value)
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, str) and is_write and "*** " in item:
            for m in _PATCH_FILE_RE.finditer(item):
                p = m.group(1).strip()
                if p and p not in found:
                    found.append(p)
    return found


def _is_mime_type(candidate: str) -> bool:
    """True when ``candidate`` looks like a MIME media type (``application/json``, ``text/plain``)."""
    parts = candidate.split("/")
    return len(parts) == 2 and parts[0].lower() in _MIME_PREFIXES and "." not in parts[1]


def _assistant_outcome(view: list[dict[str, Any]]) -> str | None:
    """Concise outcome line from the final assistant text block when user text is short."""
    for message in reversed(view):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if _is_text(block):
                    texts.append(block.get("text", ""))
        for text in reversed(texts):
            for raw_line in text.splitlines():
                stripped = raw_line.strip()
                if stripped.startswith("```"):
                    continue
                line = stripped.lstrip("#-*• ").strip()
                if len(line) < 4:
                    continue
                if line.lower().rstrip(".!") in _TRIVIAL_OUTCOMES:
                    continue
                return line[:STUB_ASSISTANT_CHARS]
    return None


def build_stub(turn: list[dict[str, Any]]) -> dict[str, Any]:
    """First 200 characters of the turn-start user text, tool names used, and file paths mentioned.

    Edited/written file paths from write/edit tools are prioritized ahead of read tool paths and
    regex-scraped paths (which exclude URLs and MIME types). When user text is short (for example
    ``"continue"`` or ``"fix test"``), adds a concise ``assistant_text`` outcome line if available.

    A page with no user text but a system message (lane A's carried-text fallback pages each such
    message on its own) adds ``system_text``: the first non-empty line of the system text (inside
    the ``<system-reminder>`` tag when the CLI wraps it), or a placeholder when it is empty. A page
    whose only message is a user message made entirely of context blocks (a paged context block,
    D32) is treated the same way: empty ``user_text`` and ``reminder_text``, the first non-empty line
    inside the tag. Other pages with longer user text keep the three base fields. Stubs never reach
    page ids, which hash only the turn number and the messages.
    """
    view = _view(turn)  # Responses (Codex) items as Messages API messages, the same view search reads (B23)
    first_user = next((m for m in view if m.get("role") == "user"), {})
    tools: list[str] = []
    edited_paths: list[str] = []
    read_paths: list[str] = []
    for message in view:
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str) and name and name not in tools:
                    tools.append(name)
                is_write = isinstance(name, str) and _is_write_tool(name)
                target = edited_paths if is_write else read_paths
                for p in _walk_tool_paths(block.get("input"), is_write):
                    if p not in target:
                        target.append(p)
    paths: list[str] = []
    for p in (*edited_paths, *read_paths):
        if p not in paths:
            paths.append(p)
    for text in _texts(turn):
        cleaned = _URL_RE.sub(" ", text)
        for match in _PATH_RE.findall(cleaned):
            if _is_mime_type(match):
                continue
            if match not in paths:
                paths.append(match)
    reminder = _reminder_first_line(turn)
    user_text = "" if reminder is not None else _user_text(first_user)[:STUB_TEXT_CHARS]
    stub: dict[str, Any] = {
        "user_text": user_text,
        "tools": tools,
        "paths": paths[:STUB_MAX_PATHS],
    }
    if reminder is not None:
        stub["reminder_text"] = reminder
    elif not user_text.strip():
        system = _system_first_line(turn)
        if system is not None:
            stub["system_text"] = system
    elif 0 < len(user_text.strip()) <= SHORT_USER_TEXT_CHARS and len(user_text.strip().split()) <= 2:
        outcome = _assistant_outcome(view)
        if outcome is not None:
            stub["assistant_text"] = outcome
    return stub


def current_stub(page: dict[str, Any]) -> dict[str, Any]:
    """The page with its stub rebuilt if it is a pre-v0.1.3 Codex stub or a pre-D51 stub of a page
    holding a Contexto search, recall, or state call, else the page itself.

    v0.1.0 to v0.1.2 stored every Responses (Codex) page with empty user text, tools, and paths: the
    stub rule read only Messages API blocks (fixed by B23). Up to v0.5.8 a page's stub also took paths
    from the results of Contexto's search, recall, and state calls, copies of older pages, so a path
    search ranked such a page above the page it quoted (D51). A page keeps its exact messages, so the
    stub is rebuilt from them with the current rule, in memory only: the page file, its id, and its
    content are never changed (pages are immutable). Only those two cases are rebuilt: an empty stub
    on a page holding a Responses item, and any stub of a page with such a call; other stubs are
    returned as stored, and so is a rebuild that comes out the same. Idempotent.
    """
    stub = page.get("stub")
    messages = page.get("messages")
    if not isinstance(stub, dict) or not isinstance(messages, list):
        return page
    codex_v010 = (not (stub.get("user_text") or stub.get("system_text") or stub.get("tools") or stub.get("paths"))
                  and any(isinstance(m, dict) and is_responses_item(m) for m in messages))
    if not codex_v010 and not own_call_ids(messages):
        return page
    rebuilt = build_stub(messages)
    return page if rebuilt == stub else {**page, "stub": rebuilt}


def build_page(key: bytes, turn: list[dict[str, Any]], turn_number: int, kind: str | None = None,
               seq: int | None = None, block: int | None = None) -> dict[str, Any]:
    """A page record for one turn, messages kept exactly as received (thinking included). ``kind`` marks
    an in-turn page (D63); a whole turn has none, and the id never depends on it. ``seq`` (D63, Reviewer B):
    an in-turn page's position in its turn (the index of its first message in the CLI's history), so the
    pages of one turn list in order; a result page also gets ``locator``, what its call ran or read, and
    ``block``, its result's block index in that message, so parallel results list in their calls' order
    (Reviewer A at 7575c76f). None is part of the id, and all are optional: a page without them sorts as
    before."""
    text, tool = message_bytes(turn)
    page = {
        "turn": turn_number,
        "messages": turn,
        "bytes": text + tool,
        "tokens": estimate_message_tokens(turn),
        "stub": build_stub(turn),
    }
    if kind is not None:
        page["kind"] = kind
    if seq is not None:
        page["seq"] = seq
    if block is not None:
        page["block"] = block
    if kind == RESULT_PAGE:
        page["locator"] = _result_locator(turn)
    page["id"] = page_id(key, page)
    return page


def _result_locator(turn: list[dict[str, Any]]) -> str:
    """A result page's call as a stub names it: the tool and what it ran or read."""
    from contexto import inturn  # inturn imports the store's render module: looked up when called

    resp_call = next((m for m in turn if isinstance(m, dict) and is_responses_item(m)
                      and str(m.get("type", "")).endswith("_call")), None)
    if resp_call is not None:
        name, locator, _ = inturn.RESPONSES.describe(resp_call)
        return f"{name} {locator}"
    call = next((b for m in turn if isinstance(m, dict) and m.get("role") == "assistant"
                 for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                 if isinstance(b, dict) and b.get("type") == "tool_use"), None)
    name, locator, _ = inturn.MESSAGES.describe(call)
    return f"{name} {locator}"


def page_order(page: dict[str, Any]) -> tuple:
    """The sort key of a page, oldest first: its turn, then an in-turn page by its position in the turn
    (``seq``) and among the results of one message by ``block``, and a page without ``seq`` (a whole turn,
    or an in-turn page written before it) after them."""
    seq, block = page.get("seq"), page.get("block")
    return (page["turn"], 0 if isinstance(seq, int) else 1, seq if isinstance(seq, int) else 0,
            block if isinstance(block, int) else 0, page["id"])


def _search_fields(page: dict[str, Any]) -> list[str]:
    """The texts search reads on a page, each counted on its own by ``rank`` (D41): the stub fields
    (``user_text``, ``system_text``, ``reminder_text``, each tool name, each path), then every text of the
    turn (``_texts``: each text block, each string field of a tool input, each tool output). The stub
    repeats the prompt's first 200 characters and the paths, so a prompt or a path that names a key
    counts as two texts. Dropping those repeats was measured and lost 2 of 86 probes from the top 3 at
    6,000 pages, on both page-size models, so they are kept (reviewer A on PR #216)."""
    stub = page["stub"]
    return [stub["user_text"], stub.get("system_text", ""), stub.get("reminder_text", ""), *stub["tools"],
            *stub["paths"], *_texts(page["messages"])]


def searchable_text(page: dict[str, Any]) -> str:
    """What search reads on a page, folded: the stub fields and every text of the page's turn (D34)."""
    return searchable(_search_fields(page))


def _is_context_text(text: Any) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(REMINDER_TAG)


def is_context_page(page: dict[str, Any]) -> bool:
    """True for a page that holds no turn: only system messages (Codex: ``developer`` items), or user
    messages made only of ``<system-reminder>`` context blocks. The bounded fallback pages carried
    system messages and Codex developer items one per page (lane A), and each paged context block as
    a user message holding that block (D32). Read from the messages, not the stub (a paged block's
    stub has ``reminder_text``). Search ranks these pages after every turn page, and its reply shows
    them after every turn page (D34). Standalone paged Codex ``developer`` pages are rendered and
    searched through ``as_message(..., include_developer=True)`` and ranked here as context pages."""
    messages = page.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    for message in messages:
        if not isinstance(message, dict):
            return False
        role, content = message.get("role"), message.get("content")
        if role in ("system", "developer"):  # developer: a Codex (Responses) item
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
    return SearchEntry(page["id"], page["turn"], search_text(_search_fields(page)), is_context_page(page))


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
    if stub.get("assistant_text"):
        outcome = " ".join(stub["assistant_text"].split())
        if outcome:
            text = f"{text} -> {outcome}" if text else outcome
    if not text and stub.get("system_text"):
        text = "[earlier system message] " + stub["system_text"]
    if not text and stub.get("reminder_text"):
        text = "[earlier reminder] " + stub["reminder_text"]
    if not text and page.get("kind") == ROUNDS_PAGE:
        text = ROUNDS_STUB
    if not text and page.get("kind") == RESULT_PAGE:
        locator = page.get("locator")
        text = RESULT_STUB + (f" {locator}" if isinstance(locator, str) and locator else "")
    line = f"{page['id']} turn {page['turn']}: {text}"
    if stub["tools"]:
        line += f" [tools: {', '.join(stub['tools'])}]"
    if stub["paths"]:
        line += f" [files: {', '.join(stub['paths'])}]"
    if salient:
        shown = set(_TOKEN.findall(line))  # an identifier already on the line adds nothing
        # A stub's or fold note's own words (file_path, was, page) are not what the page is about.
        texts = (t for t in _texts(page["messages"]) if not t.startswith("[Contexto "))
        ids = [i for i in salient_identifiers(texts) if i not in shown]
        if ids:
            line += f" [ids: {', '.join(ids)}]"
    return line


def render_index(pages: list[dict[str, Any]], budget_tokens: int, salient: bool = False,
                 line_for: Callable[[dict[str, Any]], str] | None = None) -> str:
    """One line per page, newest first, within the budget, then a count of older pages omitted.

    Only ``id``, ``turn``, ``stub``, and ``kind`` of each page are read, and ``messages`` for the
    ``salient`` identifiers. ``line_for(page)`` overrides ``index_line(page, salient)``; ``SessionStore``
    passes one that keeps each page's line, so a page is read for its identifiers once per process (D34).
    Result pages (D63) are not listed: one per stubbed tool result would flood the budget, and each is
    named by its stub in the request; search and recall reach them.
    """
    ordered = sorted((p for p in pages if p.get("kind") not in (RESULT_PAGE, FOLDS_PAGE)), key=page_order,
                     reverse=True)
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

