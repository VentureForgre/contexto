"""Bounded-context policy from docs/research/M0-paging-policy.md, as runnable code.

A request is rebuilt from the full local history as: pinned state block (system), an optional page
index of stubs, and a verbatim window of the newest turns. Paging is turn-granular: a turn starts
at a user message that holds no tool_result block, so a tool_use and its tool_result always land on
the same side of the cut. Large tool outputs inside the window are paged by replacing only the
tool_result content with a stub, which keeps the block and its tool_use_id intact.

Token counts are estimates (characters / 4, rounded up). They are good enough to compare
conditions; they are not billing numbers.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from fidelity.session import Session

CONDITIONS = ("full", "window", "pinned", "pinned_recall")

RECALL_TOOL_NAME = "recall"
RECALL_MAX_CHARS = 32_000  # per call, about 8k tokens
RECALL_MAX_CALLS_PER_TURN = 8
PAGE_ID_PREFIX = "pg_"
# Synthetic sessions only. A real store generates a random key once and keeps it beside its pages.
SIMULATION_KEY = b"m0-fidelity-simulation-key"


class PinnedBudgetError(Exception):
    """Open obligations alone exceed the pinned-state budget."""


class RecallError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Settings:
    window_high: int = 24_000  # tokens; page when the window grows past this
    window_low: int = 12_000  # tokens; after paging, the window is at most this
    pinned_budget: int = 2_000  # tokens for the pinned block
    tool_output_page_over: int = 1_500  # tokens; larger tool_result content is paged
    headline_chars: int = 40


def estimate_tokens(text: str) -> int:
    return -(-len(text) // 4)


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def message_text(message: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in message["content"]:
        if block["type"] == "text":
            parts.append(block["text"])
        elif block["type"] == "tool_use":
            parts.append(f"{block['name']}({canonical(block['input'])})")
        elif block["type"] == "tool_result":
            content = block["content"]
            parts.append(content if isinstance(content, str) else canonical(content))
    return "\n".join(parts)


def messages_tokens(messages: Sequence[dict[str, Any]]) -> int:
    return sum(estimate_tokens(message_text(m)) for m in messages)


def page_id(payload: Any, key: bytes) -> str:
    """Keyed content address: the same page under the same store key always gets the same id.

    HMAC rather than a plain hash, so an id cannot be confirmed against guessed content without
    the store's key.
    """
    digest = hmac.new(key, canonical(payload).encode("utf-8"), hashlib.sha256).hexdigest()
    return PAGE_ID_PREFIX + digest[:16]


def _headline(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]  # never cut inside a word, so no partial values leak
    return cut + " ..."


@dataclass(frozen=True)
class Page:
    id: str
    kind: str  # "turns" or "tool_output"
    first_turn: int
    last_turn: int
    text: str  # rendered transcript text, what recall returns
    stub: str


@dataclass
class Store:
    """Local page store. In the proxy this is disk; here it is a dict."""

    key: bytes = SIMULATION_KEY
    pages: dict[str, Page] = field(default_factory=dict)

    def put(self, page: Page) -> None:
        existing = self.pages.get(page.id)
        if existing is not None and existing.text != page.text:
            raise RuntimeError(f"page id collision for {page.id}")
        self.pages[page.id] = page

    def recall(self, page_ref: str, offset: int = 0) -> dict[str, Any]:
        """The recall tool contract. Returns the page text in slices of RECALL_MAX_CHARS."""
        if not isinstance(page_ref, str) or not page_ref.startswith(PAGE_ID_PREFIX):
            raise RecallError("bad_id", f"expected an id starting with {PAGE_ID_PREFIX!r}")
        page = self.pages.get(page_ref)
        if page is None:
            raise RecallError("unknown_id", f"no page {page_ref}")
        if not isinstance(offset, int) or offset < 0 or offset > len(page.text):
            raise RecallError("bad_offset", f"offset must be 0..{len(page.text)}")
        chunk = page.text[offset : offset + RECALL_MAX_CHARS]
        end = offset + len(chunk)
        return {
            "id": page.id,
            "turns": [page.first_turn, page.last_turn],
            "offset": offset,
            "text": chunk,
            "next_offset": end if end < len(page.text) else None,
        }


def recall_tool_definition() -> dict[str, Any]:
    return {
        "name": RECALL_TOOL_NAME,
        "description": (
            "Fetch a paged-out part of this conversation by its exact id (pg_...). "
            "Ids appear in [paged ...] stubs. Returns the original text verbatim, at most "
            f"{RECALL_MAX_CHARS} characters per call; pass next_offset to continue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "pattern": "^pg_[0-9a-f]{16}$"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    }


def render_pinned(session: Session, upto_turn: int, budget: int) -> tuple[str, dict[str, int]]:
    """Pinned state as of ``upto_turn`` (exclusive): open obligations, active decisions, facts.

    Priority follows the M1 checkpoint order: obligations are never dropped (error instead), then
    active decisions, then verified facts, newest first. Returns the block and omission counts.
    """
    current: dict[str, dict[str, Any]] = {}
    for rec in session.ledger:
        if rec["turn"] >= upto_turn:
            break
        current[rec["key"]] = rec  # later records supersede earlier ones for the same key
    obligations = [r for r in current.values() if r["kind"] == "obligation"]
    decisions = sorted((r for r in current.values() if r["kind"] == "decision"), key=lambda r: -r["turn"])
    facts = sorted((r for r in current.values() if r["kind"] == "fact"), key=lambda r: -r["turn"])

    head = ["PINNED STATE (from the session ledger; authoritative over older turns)"]
    ob_lines = ["OPEN OBLIGATIONS (complete list; an item not listed here is closed):"]
    ob_lines += [f"- {r['subject']}: {r['text']} [since turn {r['turn']}]" for r in obligations] or ["- none"]
    lines = head + ob_lines
    if estimate_tokens("\n".join(lines)) > budget:
        raise PinnedBudgetError(f"{len(obligations)} open obligations exceed {budget} tokens")

    omitted = {"decision": 0, "fact": 0}
    for title, kind, recs in (("ACTIVE DECISIONS:", "decision", decisions), ("VERIFIED FACTS:", "fact", facts)):
        lines.append(title)
        for r in recs:
            line = f"- the {r['subject']} is {r['value']} [turn {r['turn']}]"
            # Reserve room for the omission footer so the bound holds for the full block.
            if estimate_tokens("\n".join([*lines, line, "OMITTED: decision=000 fact=000"])) > budget:
                omitted[kind] += 1
            else:
                lines.append(line)
    if any(omitted.values()):
        lines.append(f"OMITTED: decision={omitted['decision']} fact={omitted['fact']}")
    return "\n".join(lines), omitted


@dataclass(frozen=True)
class Request:
    condition: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    window_first_turn: int
    # Visible text segments with the turn they came from; readers order claims by this turn.
    segments: list[tuple[int, str]]
    pinned_count: int = 0  # the first pinned_count segments are pinned-state lines

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.system) + messages_tokens(self.messages) + estimate_tokens(
            canonical(self.tools) if self.tools else ""
        )


@dataclass
class WindowState:
    """Hysteresis: the window start only moves at paging events, so the prefix stays stable between them."""

    first_turn: int = 0


def turn_tokens(session: Session, turn: int) -> int:
    return messages_tokens(session.turn_messages(turn))


def advance_window(session: Session, upto_turn: int, state: WindowState, settings: Settings) -> int:
    """Return the first window turn for a request covering turns [0, upto_turn).

    When the window exceeds window_high, drop whole turns from its start until it is at most
    window_low. The newest turn is always kept, even if it alone exceeds the bound.
    """
    sizes = [turn_tokens(session, t) for t in range(state.first_turn, upto_turn)]
    if sum(sizes) > settings.window_high:
        total = sum(sizes)
        first = state.first_turn
        while total > settings.window_low and first < upto_turn - 1:
            total -= sizes[first - state.first_turn]
            first += 1
        state.first_turn = first
    return state.first_turn


def _turns_page(session: Session, first: int, last: int, settings: Settings, key: bytes) -> Page:
    msgs = [m for t in range(first, last + 1) for m in session.turn_messages(t)]
    text = "\n".join(
        f"[turn {_turn_of(session, i)}] {m['role']}: {message_text(m)}" for i, m in _indexed(session, first, last)
    )
    pid = page_id({"kind": "turns", "first": first, "last": last, "messages": msgs}, key)
    # One short extractive headline per turn (its opening user text), so a reader can choose a page
    # by the words in it without similarity search.
    heads = " / ".join(
        _headline(message_text(session.turn_messages(t)[0]), settings.headline_chars)
        for t in range(first, last + 1)
    )
    stub = f"[paged {pid} | turns {first}-{last} | {len(msgs)} msgs | ~{estimate_tokens(text)} tokens | {heads}]"
    return Page(pid, "turns", first, last, text, stub)


def _indexed(session: Session, first: int, last: int) -> list[tuple[int, dict[str, Any]]]:
    start = session.turn_starts[first]
    end = session.turn_starts[last + 1] if last + 1 < session.turns else len(session.messages)
    return [(i, session.messages[i]) for i in range(start, end)]


def _turn_of(session: Session, index: int) -> int:
    # Binary search over turn starts.
    lo, hi = 0, session.turns - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if session.turn_starts[mid] <= index:
            lo = mid
        else:
            hi = mid - 1
    return lo


def page_out(session: Session, upto_turn: int, settings: Settings, store: Store, page_turns: int = 8) -> list[Page]:
    """Page turns [0, upto_turn) in fixed groups of ``page_turns``.

    Fixed group boundaries make ids stable: a group's id never changes once the group is complete.
    The trailing partial group is paged too, as its own page.
    """
    pages = []
    for first in range(0, upto_turn, page_turns):
        last = min(first + page_turns, upto_turn) - 1
        page = _turns_page(session, first, last, settings, store.key)
        store.put(page)
        pages.append(page)
    return pages


def _page_tool_outputs(
    session: Session, messages: list[dict[str, Any]], indices: list[int], settings: Settings, store: Store
) -> list[dict[str, Any]]:
    out = []
    for index, message in zip(indices, messages, strict=True):
        if message["role"] != "user" or not any(b["type"] == "tool_result" for b in message["content"]):
            out.append(message)
            continue
        message = copy.deepcopy(message)
        turn = _turn_of(session, index)
        for block in message["content"]:
            if block["type"] != "tool_result" or not isinstance(block["content"], str):
                continue
            if estimate_tokens(block["content"]) <= settings.tool_output_page_over:
                continue
            call = _tool_use_for(session, index, block["tool_use_id"])
            label = f"{call['name']}({canonical(call['input'])})"
            pid = page_id({"kind": "tool_output", "tool_use_id": block["tool_use_id"], "content": block["content"]}, store.key)
            stub = (
                f"[paged {pid} | tool_result of {label} | ~{estimate_tokens(block['content'])} tokens | "
                f"\"{_headline(block['content'], settings.headline_chars)}\"]"
            )
            store.put(Page(pid, "tool_output", turn, turn, block["content"], stub))
            block["content"] = stub  # the block and its tool_use_id stay; only the content shrinks
        out.append(message)
    return out


def _tool_use_for(session: Session, result_index: int, use_id: str) -> dict[str, Any]:
    for block in session.messages[result_index - 1]["content"]:
        if block["type"] == "tool_use" and block["id"] == use_id:
            return block
    raise ValueError(f"tool_result {use_id} has no tool_use in the previous message")


def build_request(
    session: Session,
    condition: str,
    upto_turn: int,
    settings: Settings | None = None,
    state: WindowState | None = None,
    store: Store | None = None,
) -> Request:
    """Build the upstream request for a call made after turns [0, upto_turn) exist."""
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}")
    settings = settings or Settings()
    state = state if state is not None else WindowState()
    store = store if store is not None else Store()
    system = "You are a coding agent in a long session."

    if condition == "full":
        first = 0
    else:
        first = advance_window(session, upto_turn, state, settings)

    start = session.turn_starts[first] if first < session.turns else len(session.messages)
    end = session.turn_starts[upto_turn] if upto_turn < session.turns else len(session.messages)
    indices = list(range(start, end))
    messages = [copy.deepcopy(session.messages[i]) for i in indices]
    tools: list[dict[str, Any]] = []
    prefix_lines: list[str] = []
    segments: list[tuple[int, str]] = []

    pinned_count = 0
    if condition in ("pinned", "pinned_recall"):
        pinned, _ = render_pinned(session, first, settings.pinned_budget)
        system = f"{system}\n\n{pinned}"
        # Pinned lines carry their record turn so a reader can order them against window text.
        for line in pinned.split("\n"):
            turn = int(line.rsplit("turn ", 1)[1].rstrip("]")) if "[turn " in line or "[since turn " in line else -1
            segments.append((turn, line))
        pinned_count = len(segments)
    if condition == "pinned_recall":
        tools = [recall_tool_definition()]
        messages = _page_tool_outputs(session, messages, indices, settings, store)
        if first > 0:
            pages = page_out(session, first, settings, store)
            prefix_lines.append("PAGED HISTORY (use the recall tool with an id to read a page verbatim):")
            prefix_lines += [p.stub for p in pages]
            segments += [(p.first_turn, p.stub) for p in pages]
    elif condition in ("window", "pinned") and first > 0:
        prefix_lines.append(f"[{first} earlier turns omitted]")

    if prefix_lines and messages:
        # Keep roles alternating: the index rides on the window's first user message.
        messages[0]["content"].insert(0, {"type": "text", "text": "\n".join(prefix_lines)})

    for index, message in zip(indices, messages, strict=True):
        segments.append((_turn_of(session, index), message_text(message)))
    return Request(condition, system, messages, tools, first, segments, pinned_count)


def check_protocol(messages: Sequence[dict[str, Any]]) -> list[str]:
    """Return protocol violations: role alternation, first role, and tool_use/tool_result pairing."""
    problems: list[str] = []
    if messages and messages[0]["role"] != "user":
        problems.append("first message is not from the user")
    for i, m in enumerate(messages):
        if i and m["role"] == messages[i - 1]["role"]:
            problems.append(f"message {i}: role {m['role']} repeats")
        uses = [b["id"] for b in m["content"] if b["type"] == "tool_use"]
        if uses:
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            results = [b["tool_use_id"] for b in (nxt["content"] if nxt else []) if b["type"] == "tool_result"]
            if sorted(results) != sorted(uses):
                problems.append(f"message {i}: tool_use {uses} not answered by the next message")
        results_here = [b["tool_use_id"] for b in m["content"] if b["type"] == "tool_result"]
        if results_here:
            prev = messages[i - 1] if i else None
            prev_uses = [b["id"] for b in (prev["content"] if prev else []) if b["type"] == "tool_use"]
            if not set(results_here) <= set(prev_uses):
                problems.append(f"message {i}: tool_result {results_here} has no tool_use before it")
    return problems
