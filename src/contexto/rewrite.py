"""Rewrite engine: rules 1 to 14 of docs/milestones/M2-contexto-proxy.md (rule text as aligned in
PR #25; placement and system-message evidence in spikes/m2/carried/).

The proxy calls one ``RewriteEngine`` per session: ``engine(request, headers, endpoint)`` returns the
upstream request, the headers (unchanged), and content-free events; ``engine.observe_usage(endpoint,
usage, request)`` feeds real usage back after each ``/v1/messages`` response. The work is done by
the pure function ``rewrite(request, state, store, config, estimator)``, which never mutates its
input, returns ``system`` and ``tools`` as the same objects, and is deterministic, so the proxy's
deterministic serializer sends byte-identical prefixes within an epoch. The store is touched only at
a turn start's rotation and a paged_changed resync (``page_out``, ``pinned_text``, ``index_text``,
``set_window``, ``quarantine_page``) and by ``observe_usage``
(``log``, ``set_window``); the estimator only through ``estimate``, ``estimate_messages``,
``observe`` and ``measured_bytes_per_token``.

Terms and readings agreed with manager-1 (2026-10-16), each covered by a test:

- A turn-start message is a user message with user-authored content (a text block that is not an
  injected context block, a plain string, an image, or a document) and no ``tool_result``. An
  injected context block is a text block that starts with ``<system-reminder>``.
- A turn is a turn-start message plus every following assistant, ``tool_result`` user, and
  ``role: "system"`` message up to the next turn start; a trailing system message after the new
  prompt belongs to the new turn.
- Carried content is never paged: the context blocks of paged turn-start messages and the carried
  system messages of paged turns (contract PR #35), in the form the CLI sends now. ``classify_system``
  sorts every system message deterministically (contract PR #38): ``settings`` (empty content plus
  settings fields; rule 12), ``session_start`` (content plus settings fields), ``per_turn`` (any other
  text-only content, wherever it sits; per-turn hook context) and ``unclassified`` (non-text content).
  Session-start, settings-only (latest per key) and unclassified messages are carried, an unclassified
  one also logging ``unclassified_system_message``; per-turn ones are paged with their turn. Settings-only system messages accrue per settings
  change (measured), so only the latest per settings key is carried; the carried set is computed from
  the paged region, so it changes only at a rotation. Rule 13 (PR #27): carried content is never
  paged; when a rotation finds it grown by more than ``carried_budget`` (4,000 tokens) over its size at
  the conversation's first request, a content-free ``carried_over_budget`` event reports the estimate,
  the growth, and the system-message count. Placement (rule 1, measured): carried context blocks
  and then the preamble go ahead of the first retained turn-start message's own content; carried
  system messages go immediately after that message (a system message before it returns 400).
- Rotation (rules 3 and 4, PR #27) happens only when the newest turn has no assistant message yet and
  the request adds messages (a resent request never rotates). The fixed overhead ``F`` (system, tools,
  carried content, preamble) comes from the conversation's first measured usage (its input minus its
  non-carried messages), else from the estimator; the window budget is ``W = bound - headroom - F``.
  Rotation picks the smallest ``k`` at a turn start whose retained turns fit ``window_fraction * W``,
  never below the current ``k``, always keeping the newest complete turn and the new turn. A ``k`` equal
  to the current one is a no-op (``rotation_noop``: nothing paged, nothing stripped); a ``W`` that is
  not positive or cannot hold the newest complete turn logs ``bound_too_small`` and sends the request
  unrotated. ``F`` and ``W`` appear in these events.
- Thinking (rule 2): retained messages that existed when the epoch was created lose their thinking and
  redacted-thinking blocks on every request; an assistant message left empty gets one text block
  ``[thinking omitted]``; other blocks keep their order.
- Headroom is the running maximum of per-turn growth times 1.25, floor 25,000 tokens; per-turn growth
  is the estimator's token count, at its current ratio, of the messages added between consecutive
  turn starts, divided by the number of turns added when several arrive at once.
- History matching (rule 10) hashes canonical JSON with every ``cache_control`` removed and a single
  text block normalised to a plain string. A mismatch inside the retained window resynchronises from
  the longest matching prefix; a mismatch in paged turns starts a new epoch at the current turn start
  with everything before it paged. In that forced epoch an in-flight tool loop keeps its thinking
  (only messages before the turn start count as existing), and the full history is never sent.
- ``count_tokens`` (rule 11) gets the current epoch's transform with no side effects.
- Transform failures (rule 14, PR #52; manager-1 2026-10-16 02:00Z): a ``PageConflictError`` is a
  ``page_conflict``; ``OSError``, ``LedgerError``, ``KeyFileError``, ``SessionNotFoundError`` and
  ``PageNotFoundError`` (from ``quarantine_page``) are a ``store_error``. Either one at a turn start keeps the state from before the attempt and enters
  degraded mode (``degraded_enter``): no paging, no rotation, the current epoch's transform extended
  append-only, and every request event marked ``degraded: true``. Each later turn start tries the
  store once (a read of the pinned and index text, then the normal rotation); success logs
  ``degraded_leave``, failure stays degraded. A conflict on the same page at that retry is
  persistent: the store quarantines the page (``quarantine_page``, lane B PR #59, which logs
  ``page_quarantined`` itself) and the rotation re-pages it once from the current messages. A paged_changed resync whose
  paging fails, or a failure building the degraded transform, is terminal for the request: the
  original request is forwarded and ``fallback_failed`` logged. Any other exception propagates to
  the proxy as ``transform_error``.
- The window budget ``W`` and the measured bytes-per-token ratio are published through
  ``set_window`` (lane B B9, PR #56, for ``contexto_recall``'s per-call cap) when ``W`` changes at a
  rotation or head change, when the first response measures the head, or when the ratio moves more
  than 1% after real usage; never before the
  ratio is measured, never for a ``W`` that is not positive (``bound_too_small`` reports that case;
  the store accepts positive values only), never from ``observe_usage`` while degraded, and never
  from ``count_tokens``.
- An engine started on an existing session seeds its estimator with the ratio the previous engine
  published (``store.window()``, packet A8), so a proxy restart sizes like the process before it.
"""

from __future__ import annotations

import hashlib
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from contexto.canonical import canonical_bytes, canonical_json
from contexto.store import KeyFileError, PageConflictError, PageNotFoundError, SessionNotFoundError
from contexto.store.ledger import LedgerError
from contexto.tokens import TokenEstimator

MESSAGES = "/v1/messages"
COUNT_TOKENS = "/v1/messages/count_tokens"
THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})
THINKING_OMITTED = "[thinking omitted]"
CONTEXT_TAG = "<system-reminder>"
STORE_ERRORS = (OSError, LedgerError, KeyFileError, SessionNotFoundError, PageNotFoundError)  # rule 14 store_error
STORE_FAILURES = (PageConflictError, *STORE_ERRORS)
WINDOW_RATIO_CHANGE = 0.01  # set_window: republish when the measured ratio moves more than this share
RESTORED_RATIO_WEIGHT = 10_000  # tokens: a restored ratio counts like one mid-sized request (A8)
RESTORED_RATIO_RANGE = (1.0, 6.0)  # bytes per token a restore accepts; outside it the seed is safer (A2e)


class Store(Protocol):
    def page_out(self, turns: list[list[dict]], first_turn: int) -> list[str]: ...
    def pinned_text(self, budget_tokens: int) -> str: ...
    def index_text(self, budget_tokens: int) -> str: ...
    def set_window(self, window_tokens: int, bytes_per_token: float) -> None: ...
    def window(self) -> tuple[int | None, float | None]: ...
    def quarantine_page(self, page_id: str) -> Any: ...


@dataclass(frozen=True)
class Config:
    bound: int = 150_000
    window_fraction: float = 0.5  # rule 4: retained window target as a share of the window budget W
    headroom_floor: int = 25_000
    headroom_factor: float = 1.25
    pin_budget: int = 6_000
    index_budget: int = 4_000
    carried_budget: int = 4_000  # rule 13: carried growth above this is reported
    carried_text_budget: int = 8_000  # Terms fallback: carried growth above this pages old text messages
    # Recall knobs (packet A7, H1 recall fix). Defaults are intake's V2 (A7b): preamble_first on, the rest off.
    recall_rule: str = ""  # placed before the store's pinned and index text in the preamble
    recall_example: str = ""  # one line placed after the rule
    preamble_first: bool = True  # V2 (intake 03:26Z): the preamble block goes before carried context blocks
    turn_reminder: str = ""  # appended to each retained turn-start user message while turns are paged


@dataclass(frozen=True)
class EpochState:
    epoch: int = 0
    k: int = 0  # message index of the first retained turn-start message; M[0..k) are paged
    pinned_text: str = ""
    index_text: str = ""
    head_fingerprint: str = ""
    strip_upto: int = 0  # M[0..strip_upto) existed when the epoch was created
    turns_paged: int = 0  # number of turns written to the store
    head_changed: bool = False  # a new epoch is due at the next turn start
    hashes: tuple[str, ...] = ()  # normalised hashes of the previous request's messages
    last_sent_bytes: int = 0
    last_input_tokens: int | None = None  # real upstream input of the previous request
    max_turn_growth: int = 0
    last_turn_start_n: int | None = None  # history length at the previous turn start
    last_turn_count: int = 0  # turn starts in the history at the previous turn start
    head_tokens: int | None = None  # rule 4: measured head (system, tools, initial carried)
    initial_carried: int | None = None  # carried content at the conversation's first request
    last_window_tokens: int = 0  # estimate of the previous upstream request's non-carried messages
    paged_carried: frozenset[str] = frozenset()  # hashes of carried text messages paged by the fallback
    last_carried_text_count: int | None = None  # carried text messages at the previous turn start
    carried_text_streak: int = 0  # consecutive turn starts at which that count grew
    window_minimal_count: int = 0  # rotations that kept only the new turn (A2c, contract PR #50)
    last_estimate: int | None = None  # pre-send estimate of the previous request (A2b)
    estimate_ratio_sum: float = 0.0  # running sum of estimate / real over observed requests
    estimate_ratio_count: int = 0
    degraded: bool = False  # rule 14: the store failed; no paging and no rotation until a retry succeeds
    conflict_page: str | None = None  # page id of the page_conflict that entered degraded mode
    window_budget: int | None = None  # W at the latest rotation decision
    published_window: tuple[int, float] | None = None  # (W, bytes per token) last given to set_window


# Message classification -------------------------------------------------------------------------

def _blocks(message: dict) -> list:
    content = message.get("content")
    return content if isinstance(content, list) else []


def _anthropic_is_context_block(block: Any) -> bool:
    return (isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str) and block["text"].lstrip().startswith(CONTEXT_TAG))


def _anthropic_is_turn_start(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    blocks = _blocks(message)
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
        return False
    return any(isinstance(b, dict) and not is_context_block(b) for b in blocks)


def turn_starts(messages: list[dict]) -> list[int]:
    return [i for i, m in enumerate(messages) if is_turn_start(m)]


def at_turn_start(messages: list[dict]) -> bool:
    """The newest turn has no assistant message yet (only its turn start and system messages)."""
    starts = turn_starts(messages)
    if not starts:
        return False
    return all(not is_assistant(m) for m in messages[starts[-1]:])


# Normalisation and hashing (rule 10) ------------------------------------------------------------

def _drop_cache_control(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_cache_control(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_drop_cache_control(v) for v in obj]
    return obj


def _anthropic_normalise(message: dict) -> dict:
    m = _drop_cache_control(message)
    content = m.get("content")
    if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and set(content[0]) == {"type", "text"} and content[0]["type"] == "text"):
        m = {**m, "content": content[0]["text"]}
    return m


def message_hash(message: dict) -> str:
    return hashlib.sha256(canonical_bytes(normalise(message))).hexdigest()


def _anthropic_head_fingerprint(request: dict) -> str:
    return hashlib.sha256(canonical_bytes({"system": request.get("system"), "tools": request.get("tools")})).hexdigest()


def conversation_key(request: dict) -> str:
    """Per-conversation key: the normalised first message, so subagent and other side requests keep
    their own state and a head change does not split the main conversation."""
    items = request.get(_dialect().field) or []
    starts = turn_starts(items) if _dialect().lead_count(items) else [0]
    return message_hash(items[starts[0]]) if items and starts else ""


# Dialects (A5): the same engine for the Anthropic Messages API and the OpenAI Responses API ---------

class Dialect:
    """Protocol-specific primitives the engine core uses. ``AnthropicDialect`` is the behavior above;
    ``contexto.codex.ResponsesDialect`` implements the Responses API (Codex)."""

    name = "anthropic"
    field = "messages"

    def is_context_block(self, block: Any) -> bool:
        return _anthropic_is_context_block(block)

    def is_turn_start(self, item: dict) -> bool:
        return _anthropic_is_turn_start(item)

    def is_assistant(self, item: dict) -> bool:
        return item.get("role") == "assistant"

    def is_system_like(self, item: dict) -> bool:
        return item.get("role") == "system"

    def classify_system(self, items: list[dict], i: int) -> str:
        return _anthropic_classify_system(items, i)

    def normalise(self, item: dict) -> dict:
        return _anthropic_normalise(item)

    def head_fingerprint(self, request: dict) -> str:
        return _anthropic_head_fingerprint(request)

    def head_estimate_source(self, request: dict) -> dict:
        return {"system": request.get("system"), "tools": request.get("tools")}

    def strip_thinking(self, item: dict) -> dict | None:
        return _anthropic_strip_thinking(item)

    def merge_first(self, first: dict, blocks: list, text: str) -> dict:
        head = [*blocks]
        if text:
            head.append({"type": "text", "text": text})
        own = first.get("content")
        own_blocks = [{"type": "text", "text": own}] if isinstance(own, str) else list(own)
        return {**first, "content": head + own_blocks}

    def countable(self, item: dict) -> bool:
        return bool(item.get("content"))

    def estimate_items(self, estimator: TokenEstimator, items: list[dict]) -> int:
        return estimator.estimate_messages(items)

    def lead_count(self, items: list[dict]) -> int:
        return 0

    def usage_tokens(self, usage: dict) -> int:
        return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


ANTHROPIC = Dialect()
_DIALECT: ContextVar[Dialect | None] = ContextVar("contexto_rewrite_dialect", default=None)


def _dialect() -> Dialect:
    return _DIALECT.get() or ANTHROPIC


def is_context_block(block: Any) -> bool:
    return _dialect().is_context_block(block)


def is_turn_start(item: dict) -> bool:
    return _dialect().is_turn_start(item)


def is_assistant(item: dict) -> bool:
    return _dialect().is_assistant(item)


def is_system_like(item: dict) -> bool:
    return _dialect().is_system_like(item)


def classify_system(items: list[dict], i: int) -> str:
    return _dialect().classify_system(items, i)


def normalise(item: dict) -> dict:
    return _dialect().normalise(item)


def head_fingerprint(request: dict) -> str:
    return _dialect().head_fingerprint(request)


def strip_thinking(item: dict) -> dict | None:
    return _dialect().strip_thinking(item)


def estimate_items(estimator: TokenEstimator, items: list[dict]) -> int:
    return _dialect().estimate_items(estimator, items)


# Transform pieces ------------------------------------------------------------------------------

def _anthropic_strip_thinking(message: dict) -> dict:
    blocks = _blocks(message)
    if not any(isinstance(b, dict) and b.get("type") in THINKING_TYPES for b in blocks):
        return message
    kept = [b for b in blocks if not (isinstance(b, dict) and b.get("type") in THINKING_TYPES)]
    if not kept:
        kept = [{"type": "text", "text": THINKING_OMITTED}]
    return {**message, "content": kept}


def settings_key(message: dict) -> tuple | None:
    """For a settings-only system message (empty content, settings fields such as output_config),
    the names of its settings fields and their keys; None for any other message."""
    if message.get("role") != "system" or message.get("content") not in ([], "", None):
        return None
    fields = sorted(k for k in message if k not in ("role", "content"))
    if not fields:
        return None
    return tuple((f, tuple(sorted(message[f])) if isinstance(message[f], dict) else ()) for f in fields)


SESSION_START = "session_start"
SETTINGS = "settings"
HOOK_OUTPUT = "hook_output"
CARRIED_TEXT = "carried_text"
UNCLASSIFIED = "unclassified"

# Documented hook-output signatures (measured on Claude Code 2.1.301 with zero-spend stubs,
# spikes/m2/carried/README.md findings 7 and 9): the CLI wraps a hook's additionalContext in a role
# "system" message whose whole text starts with one of these prefixes.
HOOK_SIGNATURES_CLAUDE_CODE_VERSION = "2.1.301"
HOOK_SIGNATURES = (
    re.compile(r"UserPromptSubmit hook additional context: "),
    re.compile(r"PostToolUse:[^\s]+ hook additional context: "),
)


def is_text_only(message: dict) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in content)


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def is_hook_output(message: dict) -> bool:
    return is_text_only(message) and any(sig.match(message_text(message)) for sig in HOOK_SIGNATURES)


def mode_kind(message: dict) -> str | None:
    """Recognisable kind of a carried text message (measured on 2.1.301): output-style changes and
    resets, and plan-mode entry and exit. None when not recognised."""
    text = message_text(message).lower()
    if "output style" in text:
        return "output_style"
    if "plan mode" in text:
        return "plan_mode"
    return None


def _anthropic_classify_system(messages: list[dict], i: int) -> str:
    """Deterministic class of the system message at ``messages[i]``, in the contract's order (Terms,
    PR #38, merged): session-start (content plus settings fields), settings-only (empty content plus
    settings fields), hook output (text-only, matching a documented signature; paged with its turn),
    other text-only content (carried: output styles and plan mode are standing instructions), and
    non-text content (carried, logged as ``unclassified_system_message``). Position is not used;
    ``messages`` is accepted for a uniform signature."""
    m = messages[i]
    if settings_key(m) is not None:
        return SETTINGS
    if any(k not in ("role", "content") for k in m) and m.get("content") not in ([], "", None):
        return SESSION_START
    if is_hook_output(m):
        return HOOK_OUTPUT
    if is_text_only(m):
        return CARRIED_TEXT
    return UNCLASSIFIED


def carried_systems(messages: list[dict], k: int, paged: frozenset[str] = frozenset()) -> tuple[list[int], int]:
    """Indexes of the carried system messages in M[0..k), after deduplicating settings-only messages
    (latest per settings key) and leaving out text messages the fallback has paged, and the number of
    unclassified ones among them."""
    carried, latest, unclassified = [], {}, 0
    for i in range(k):
        if not is_system_like(messages[i]):
            continue
        kind = classify_system(messages, i)
        if kind == HOOK_OUTPUT or (kind == CARRIED_TEXT and message_hash(messages[i]) in paged):
            continue
        if kind == UNCLASSIFIED:
            unclassified += 1
        if kind == SETTINGS:
            latest[settings_key(messages[i])] = i
        carried.append(i)
    keep = [i for i in carried if classify_system(messages, i) != SETTINGS or latest[settings_key(messages[i])] == i]
    return keep, unclassified


def carried_content(messages: list[dict], k: int, paged: frozenset[str] = frozenset()) -> tuple[list[dict], list[dict]]:
    """(context blocks of paged turn starts, carried system messages of paged turns), in original order
    and in the form the CLI sends now. Hook output is paged with its turn, not carried."""
    blocks = [b for m in messages[:k] if is_turn_start(m) for b in _blocks(m) if is_context_block(b)]
    indexes, _ = carried_systems(messages, k, paged)
    return blocks, [messages[i] for i in indexes]


def strip_carried(messages: list[dict], start: int, end: int) -> list[dict]:
    """M[start..end) as stored in a page: thinking kept as received; carried system messages and the
    context blocks of turn starts removed; per-turn system messages kept."""
    out = []
    for i in range(start, end):
        m = messages[i]
        if is_system_like(m) and classify_system(messages, i) != HOOK_OUTPUT:
            continue
        if is_turn_start(m) and any(is_context_block(b) for b in _blocks(m)):
            m = {**m, "content": [b for b in _blocks(m) if not is_context_block(b)]}
        out.append(m)
    return out


_DEFAULT_CONFIG = Config()
_KNOBS: ContextVar[Config | None] = ContextVar("contexto_rewrite_knobs", default=None)


@contextmanager
def recall_knobs(config: Config):
    """Apply ``config``'s recall knobs to transforms in this context (RewriteEngine does this per call)."""
    token = _KNOBS.set(config)
    try:
        yield
    finally:
        _KNOBS.reset(token)


def _knobs() -> Config:
    return _KNOBS.get() or _DEFAULT_CONFIG


def compose_preamble(config: Config, pinned_text: str, index_text: str) -> str:
    """The preamble text: the recall knobs (A7, off by default), then pinned and index text. The one
    composition used for what is sent (``preamble``) and for every F estimate (``fixed_overhead``, and
    ``observe_usage``'s first W), so the knobs are always counted (A8, after PR #70)."""
    parts = (config.recall_rule, config.recall_example, pinned_text, index_text)
    return "\n\n".join(part for part in parts if part)


def preamble(state: EpochState) -> str:
    return compose_preamble(_knobs(), state.pinned_text, state.index_text)


def _with_reminder(message: dict, reminder: str) -> dict:
    content = message.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
    return {**message, "content": [*blocks, {"type": "text", "text": reminder}]}


def transform_messages(messages: list[dict], state: EpochState) -> list[dict]:
    d = _dialect()
    out = []
    for i in range(state.k, len(messages)):
        m = messages[i]
        if i < state.strip_upto:
            m = strip_thinking(m)
        if m is not None:  # a dialect may drop an item entirely (Responses reasoning items)
            out.append(m)
    if state.k == 0 or not out:
        return out
    lead = list(messages[: d.lead_count(messages)])  # items before the first turn start stay first
    blocks, systems = carried_content(messages, state.k, state.paged_carried)
    systems = [m for m in systems if not any(m is x for x in lead)]
    knobs = _knobs()
    if knobs.preamble_first:  # the preamble ahead of the carried context blocks
        first = d.merge_first(d.merge_first(out[0], blocks, ""), [], preamble(state))
    else:
        first = d.merge_first(out[0], blocks, preamble(state))
    result = [*lead, first, *systems, *out[1:]]
    if knobs.turn_reminder and d is ANTHROPIC:
        # Every retained turn start carries the reminder, so the newest one ends the window. A
        # message's form changes only at a rotation (when k first becomes positive or moves), where
        # rule 2 strips thinking anyway: no content changes under a kept signed thinking block, and
        # the prefix stays byte-identical within an epoch (cache cost: the reminder's tokens per
        # retained turn, not a moving tail). Messages API only; other dialects ignore it.
        result = [_with_reminder(m, knobs.turn_reminder) if is_turn_start(m) else m for m in result]
    return result


def headroom(state: EpochState, config: Config) -> int:
    return max(config.headroom_floor, math.ceil(state.max_turn_growth * config.headroom_factor))


def estimate_upstream(request: dict, messages: list[dict], previous_n: int, state: EpochState,
                      estimator: TokenEstimator) -> int:
    """Rule 3: last real input plus the messages added since, else an estimate of the whole request."""
    if state.last_input_tokens is not None and previous_n <= len(messages):
        return state.last_input_tokens + estimate_items(estimator, messages[previous_n:])
    head = estimator.estimate(canonical_json(_dialect().head_estimate_source(request)))
    return head + estimate_items(estimator, transform_messages(messages, state))


def window_tokens(messages: list[dict], estimator: TokenEstimator, start: int = 0) -> int:
    """Tokens of the non-carried part of M[start..) (no carried system messages, no context blocks)."""
    kept = strip_carried(messages, start, len(messages))
    d = _dialect()
    return estimate_items(estimator, [x for x in (strip_thinking(m) for m in kept if d.countable(m)) if x is not None])


def carried_tokens(messages: list[dict], estimator: TokenEstimator, paged: frozenset[str] = frozenset()) -> int:
    blocks, systems = carried_content(messages, len(messages), paged)
    parts = ([{"role": "user", "content": blocks}] if blocks else []) + systems
    return estimate_items(estimator, parts) if parts else 0


def fixed_overhead(request: dict, messages: list[dict], state: EpochState, store: Store, config: Config,
                   estimator: TokenEstimator) -> int:
    """Rule 4: F = system + tools + carried content + preamble. The head comes from measured usage once
    available (the conversation's first request), else from the estimator; carried growth since the
    first request and the current pinned and index text are estimated."""
    carried = carried_tokens(messages, estimator, state.paged_carried)
    if state.head_tokens is not None:
        head = state.head_tokens + max(0, carried - (state.initial_carried or 0))
    else:
        head = estimator.estimate(canonical_json(_dialect().head_estimate_source(request))) + carried
    pre = compose_preamble(config, store.pinned_text(config.pin_budget), store.index_text(config.index_budget))
    return head + (estimator.estimate(pre) if pre else 0)


def choose_k(messages: list[dict], state: EpochState, target: int, estimator: TokenEstimator) -> int:
    starts = [i for i in turn_starts(messages) if i >= state.k]
    if len(starts) < 2:
        return state.k
    newest_complete = starts[-2]
    for k in starts:
        if k > newest_complete:
            break
        if window_tokens(messages, estimator, k) <= target:
            return k
    return newest_complete


def new_epoch(messages: list[dict], request: dict, state: EpochState, store: Store, config: Config,
              estimator: TokenEstimator, k: int, strip_upto: int, events: list[dict], reason: str,
              sizing: tuple[int, int] | None = None) -> EpochState:
    if k > state.k:
        bounds = [i for i in turn_starts(messages) if state.k <= i < k]
        if not bounds or bounds[0] != state.k:
            bounds = [state.k, *bounds]
        edges = [*bounds, k]
        turns = [strip_carried(messages, a, b) for a, b in zip(edges, edges[1:])]
        ids = store.page_out(turns, state.turns_paged)
        events.append({"event": "page_out", "epoch": state.epoch + 1, "turns": len(turns),
                       "pages": len(ids), "first_turn": state.turns_paged})
        turns_paged = state.turns_paged + len(turns)
    else:
        turns_paged = state.turns_paged
    new = replace(state, epoch=state.epoch + 1, k=k, pinned_text=store.pinned_text(config.pin_budget),
                  index_text=store.index_text(config.index_budget), head_fingerprint=head_fingerprint(request),
                  strip_upto=strip_upto, turns_paged=turns_paged, head_changed=False, last_input_tokens=None)
    rotation = {"event": "rotation", "epoch": new.epoch, "reason": reason, "k": k,
                "retained_messages": len(messages) - k, "turns_paged": turns_paged}
    if sizing is not None:
        rotation.update({"fixed_overhead": sizing[0], "window_budget": sizing[1]})
    events.append(rotation)
    _, unclassified = carried_systems(messages, k, new.paged_carried)
    if unclassified:
        events.append({"event": "unclassified_system_message", "epoch": new.epoch, "count": unclassified})
    new = page_carried_text(messages, new, store, config, estimator, events)
    carried = carried_tokens(messages, estimator, new.paged_carried)
    growth = carried - (state.initial_carried or 0)
    if growth > config.carried_budget:
        events.append({"event": "carried_over_budget", "epoch": new.epoch, "carried_tokens": carried,
                       "carried_growth": growth,
                       "system_messages": len(carried_systems(messages, k, new.paged_carried)[0]),
                       "budget": config.carried_budget})
    return new


def page_carried_text(messages: list[dict], state: EpochState, store: Store, config: Config,
                      estimator: TokenEstimator, events: list[dict]) -> EpochState:
    """Terms fallback: when carried content has grown more than ``carried_text_budget`` over its size at
    the first request, page carried text messages (step 4) of paged turns, superseded ones of a
    recognisable kind first, then unrecognised ones, oldest first, never the latest of a recognisable
    kind, until the growth is back under the budget. Each is its own page; logs ``carried_paged``."""
    growth = carried_tokens(messages, estimator, state.paged_carried) - (state.initial_carried or 0)
    if growth <= config.carried_text_budget:
        return state
    texts = [i for i in carried_systems(messages, state.k, state.paged_carried)[0]
             if classify_system(messages, i) == CARRIED_TEXT]
    latest = {}
    for i in texts:
        kind = mode_kind(messages[i])
        if kind is not None:
            latest[kind] = i
    superseded = [i for i in texts if mode_kind(messages[i]) is not None and latest[mode_kind(messages[i])] != i]
    unrecognised = [i for i in texts if mode_kind(messages[i]) is None]
    chosen, freed = [], 0
    for i in superseded + unrecognised:
        if growth - freed <= config.carried_text_budget:
            break
        chosen.append(i)
        freed += estimate_items(estimator, [messages[i]])
    if not chosen:
        return state
    chosen.sort()
    store.page_out([[messages[i]] for i in chosen], state.turns_paged)
    events.append({"event": "carried_paged", "epoch": state.epoch, "count": len(chosen), "tokens": freed})
    return replace(state, paged_carried=state.paged_carried | {message_hash(messages[i]) for i in chosen},
                   turns_paged=state.turns_paged + len(chosen))


def matching_prefix(hashes: tuple[str, ...], messages: list[dict]) -> int:
    n = 0
    for old, m in zip(hashes, messages):
        if old != message_hash(m):
            break
        n += 1
    return n


# The engine --------------------------------------------------------------------------------------

def rotate_at_turn_start(request: dict, messages: list[dict], n: int, estimate: int, state: EpochState,
                         store: Store, config: Config, estimator: TokenEstimator, events: list[dict]) -> EpochState:
    """Rules 3, 4, and 6 at a turn start: rotate, rotate to the minimal window, or do nothing. May raise
    store failures (page_out, pinned_text, index_text, set_window); ``guarded_rotation`` handles them."""
    trigger = estimate > config.bound - headroom(state, config)
    if state.head_changed or trigger:
        f = fixed_overhead(request, messages, state, store, config, estimator)
        w = config.bound - headroom(state, config) - f
        starts = turn_starts(messages)
        new_start = starts[-1]
        newest_start = starts[-2] if len(starts) >= 2 else new_start
        sizing = {"epoch": state.epoch, "fixed_overhead": f, "window_budget": w}
        target = int(config.window_fraction * w)
        if state.head_changed:  # rule 6: always a new epoch; page only if the budget allows
            k = choose_k(messages, state, target, estimator) if w > 0 else state.k
            state = new_epoch(messages, request, state, store, config, estimator, k, n, events, "head_change",
                              sizing=(f, w))
        elif w <= 0 or window_tokens(messages, estimator, newest_start) > w:
            # Rule 4 window_minimal (contract PR #50, intake 2026-10-16): W cannot hold the newest
            # complete turn plus the new turn, so retain only the new turn; the previous turn is paged
            # and recallable. If even the new turn exceeds W, still rotate to this minimal window (it
            # is smaller than sending unrotated) and log bound_too_small as well.
            new_tokens = window_tokens(messages, estimator, new_start)
            previous_tokens = (window_tokens(messages, estimator, newest_start) - new_tokens
                               if newest_start < new_start else 0)
            if new_start > state.k:
                state = new_epoch(messages, request, state, store, config, estimator, new_start, n, events,
                                  "window_minimal", sizing=(f, w))
                state = replace(state, window_minimal_count=state.window_minimal_count + 1)
                events.append({"event": "window_minimal", "epoch": state.epoch, "window_budget": w,
                               "new_turn_tokens": new_tokens, "previous_turn_tokens": previous_tokens,
                               "k": new_start, "count": state.window_minimal_count})
            if w <= 0 or new_tokens > w:
                events.append({"event": "bound_too_small", **sizing, "epoch": state.epoch, "bound": config.bound,
                               "new_turn_tokens": new_tokens})
        else:
            k = choose_k(messages, state, target, estimator)
            if k == state.k:  # rule 4: nothing to page, so no strip and no rebuild
                events.append({"event": "rotation_noop", **sizing, "k": k})
            else:
                state = new_epoch(messages, request, state, store, config, estimator, k, n, events, "bound",
                                  sizing=(f, w))
        state = publish_window(store, replace(state, window_budget=w), estimator)
    return state


class StoreCalls:
    """The store as the engine sees it, recording the last method looked up so a failure can name its
    operation in a content-free event (the exception text may hold paths and is never logged)."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.last: str | None = None

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._store, name)
        if callable(value):
            self.last = name
        return value


def publish_window(store: Store, state: EpochState, estimator: TokenEstimator) -> EpochState:
    """Lane B B9: give ``W`` and the measured ratio to ``set_window`` when ``W`` changed or the ratio moved
    more than ``WINDOW_RATIO_CHANGE``. Skipped before the ratio is measured and for a ``W`` that is not
    positive, which ``set_window`` rejects."""
    ratio = estimator.measured_bytes_per_token
    if state.window_budget is None or state.window_budget <= 0 or ratio is None:
        return state
    published = state.published_window
    if (published is not None and published[0] == state.window_budget
            and abs(ratio - published[1]) <= WINDOW_RATIO_CHANGE * published[1]):
        return state
    store.set_window(state.window_budget, ratio)
    return replace(state, published_window=(state.window_budget, ratio))


def store_failure(error: Exception, calls: StoreCalls, state: EpochState) -> dict:
    """The content-free ``page_conflict`` or ``store_error`` event for ``error`` (rule 14)."""
    kind = "page_conflict" if isinstance(error, PageConflictError) else "store_error"
    failure = {"event": kind, "epoch": state.epoch, "operation": calls.last, "error": type(error).__name__}
    if state.degraded:
        failure["degraded"] = True
    if kind == "page_conflict" and error.page_id:  # set by both stores (lane B B11, PR #73)
        failure["page_id"] = error.page_id
    return failure


def enter_degraded(state: EpochState, failure: dict, events: list[dict]) -> EpochState:
    events.append(failure)
    if not state.degraded:
        events.append({"event": "degraded_enter", "epoch": state.epoch, "reason": failure["event"]})
    conflict = failure.get("page_id") if failure["event"] == "page_conflict" else state.conflict_page
    return replace(state, degraded=True, conflict_page=conflict)


def guarded_rotation(request: dict, messages: list[dict], n: int, estimate: int, state: EpochState,
                     store: Store, config: Config, estimator: TokenEstimator, events: list[dict]) -> EpochState:
    """Rule 14 around a turn start's rotation. A store failure keeps the state from before the attempt
    (its partial events are dropped; pages already written are rewritten identically on the next
    attempt) and enters degraded mode. In degraded mode this call is the once-per-turn-start retry: it
    reads the store even when nothing is due to page, and leaves degraded mode when that and the
    rotation succeed. A conflict on the page that entered degraded mode is persistent: the store
    quarantines the page and the rotation is attempted once more, re-paging it from the current
    messages (manager-1 2026-10-16 02:00Z; the store logs ``page_quarantined``)."""
    calls, attempt = StoreCalls(store), []
    try:
        if state.degraded:
            calls.pinned_text(config.pin_budget)
            calls.index_text(config.index_budget)
        new = rotate_at_turn_start(request, messages, n, estimate, state, calls, config, estimator, attempt)
    except STORE_FAILURES as error:
        failure = store_failure(error, calls, state)
        page = failure.get("page_id")
        if not (state.degraded and page is not None and page == state.conflict_page):
            return enter_degraded(state, failure, events)
        events.append(failure)
        calls, attempt = StoreCalls(store), []
        try:
            calls.quarantine_page(page)
            new = rotate_at_turn_start(request, messages, n, estimate, state, calls, config, estimator, attempt)
        except STORE_FAILURES as error:
            return enter_degraded(state, store_failure(error, calls, state), events)
        events.append({"event": "degraded_leave", "epoch": state.epoch, "quarantined": page})
    else:
        if state.degraded:
            events.append({"event": "degraded_leave", "epoch": state.epoch})
    events.extend(attempt)
    return replace(new, degraded=False, conflict_page=None)


def forward_original(request: dict, messages: list[dict], state: EpochState, reason: str,
                     events: list[dict]) -> tuple[dict, EpochState, list[dict]]:
    """Rule 14 terminal step: send the CLI's request unchanged and log ``fallback_failed``. The epoch keeps
    its previous hashes, so the next request detects the same mismatch and tries once more."""
    sent = len(canonical_bytes(request))
    events.append({"event": "fallback_failed", "epoch": state.epoch, "reason": reason, "messages": len(messages)})
    events.append({"event": "request", "epoch": state.epoch, "k": state.k, "messages": len(messages),
                   "upstream_messages": len(messages), "sent_bytes": sent, "degraded": True,
                   "fallback_failed": True})
    return request, replace(state, degraded=True, last_sent_bytes=sent, last_estimate=None, last_input_tokens=None), events


def send(request: dict, messages: list[dict], previous_n: int, fingerprint: str, state: EpochState, store: Store,
         config: Config, estimator: TokenEstimator, events: list[dict]) -> tuple[dict, EpochState, list[dict]]:
    """Rules 3 to 6 after history matching: head change, the turn-start rotation, and the request event."""
    field_name = _dialect().field
    n = len(messages)
    # Rule 6: a head change waits for the next turn start.
    if fingerprint != state.head_fingerprint and not state.head_changed:
        state = replace(state, head_changed=True)
        events.append({"event": "head_changed", "epoch": state.epoch})

    # Rule 13's baseline is the carried content at the conversation's first request, so it is taken
    # before that request can rotate; set after, a first request that rotates (a resumed session, or a
    # new engine on an existing one) measured its growth from zero (A6 smoke, PR #67). The baseline is
    # per engine: after a proxy restart it is the carried content at the new engine's first request,
    # so growth before the restart is not counted again (accepted by manager-1, 2026-10-16).
    if state.initial_carried is None:
        state = replace(state, initial_carried=carried_tokens(messages, estimator, state.paged_carried))
    estimate = estimate_upstream(request, messages, previous_n, state, estimator)
    if n > previous_n and at_turn_start(messages):
        count = len(turn_starts(messages))
        if state.last_turn_start_n is not None and count > state.last_turn_count and n > state.last_turn_start_n:
            # Per-turn growth: the messages added since the previous turn start, estimated at the current
            # ratio, averaged when several turns arrive at once (a resume or a missed request).
            added = estimate_items(estimator, messages[state.last_turn_start_n:])
            growth = added // (count - state.last_turn_count)
            state = replace(state, max_turn_growth=max(state.max_turn_growth, growth))
        state = replace(state, last_turn_start_n=n, last_turn_count=count)
        # Early warning (reviewers): carried text messages (contract step 4) growing at consecutive turn
        # starts means a new hook or mode is adding standing messages per turn.
        texts = sum(1 for i, m in enumerate(messages) if is_system_like(m)
                    and classify_system(messages, i) == CARRIED_TEXT and message_hash(m) not in state.paged_carried)
        if state.last_carried_text_count is not None and texts > state.last_carried_text_count:
            streak = state.carried_text_streak + 1
            if streak == 2:
                events.append({"event": "carried_text_growing", "epoch": state.epoch, "messages": texts})
        else:
            streak = 0
        state = replace(state, last_carried_text_count=texts, carried_text_streak=streak)
        state = guarded_rotation(request, messages, n, estimate, state, store, config, estimator, events)
    rotated = any(e["event"] == "rotation" for e in events)
    # Pre-send over-bound is the allowed exception only inside a tool loop (rule 5): the newest turn
    # already has model output. At a turn start an over-bound request is bound_too_small instead.
    in_tool_loop = bool(turn_starts(messages)) and not at_turn_start(messages)
    # Rule 14: a degraded request may exceed the bound at a turn start too; it is logged the same way.
    over = estimate > config.bound and not rotated and (in_tool_loop or state.degraded)
    # The estimate observe_usage compares with real usage must describe what is actually sent: after a
    # rotation that is the transformed request, not the history that triggered it (A2b review).
    sent_estimate = estimate_upstream(request, messages, len(messages), state, estimator) if rotated else estimate

    upstream = {**request, field_name: transform_messages(messages, state)}
    sent = len(canonical_bytes(upstream))
    state = replace(state, hashes=tuple(message_hash(m) for m in messages), last_sent_bytes=sent, last_estimate=sent_estimate,
                    last_window_tokens=window_tokens(upstream[field_name], estimator))
    event = {"event": "request", "epoch": state.epoch, "k": state.k, "messages": n,
             "upstream_messages": len(upstream[field_name]), "estimate": estimate, "sent_bytes": sent}
    if rotated:
        event["sent_estimate"] = sent_estimate
    if state.degraded:
        event["degraded"] = True
    if over:
        # Pre-send over-bound (rule 5: a turn that grows past the bound inside a tool loop): the allowed,
        # logged exception for acceptance 1, kept distinct from observe_usage's source "real" record.
        event["over_bound"] = True
        bound_event = {"event": "over_bound", "source": "estimate", "in_tool_loop": in_tool_loop, "epoch": state.epoch,
                       "estimate": estimate, "bound": config.bound}
        if state.degraded:
            bound_event["degraded"] = True
        events.append(bound_event)
    events.append(event)
    return upstream, state, events


def names_of(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


def rewrite(request: dict, state: EpochState | None, store: Store, config: Config,
            estimator: TokenEstimator, dialect: Dialect | None = None) -> tuple[dict, EpochState, list[dict]]:
    """Return (upstream_request, new_state, events) for one request (``/v1/messages`` by default;
    pass ``dialect`` for another API)."""
    token = _DIALECT.set(dialect or ANTHROPIC)
    try:
        return _rewrite(request, state, store, config, estimator)
    finally:
        _DIALECT.reset(token)


def _rewrite(request: dict, state: EpochState | None, store: Store, config: Config,
             estimator: TokenEstimator) -> tuple[dict, EpochState, list[dict]]:
    field_name = _dialect().field
    messages = request.get(field_name) or []
    n = len(messages)
    events: list[dict] = []
    fingerprint = head_fingerprint(request)
    if state is None:
        state = EpochState(head_fingerprint=fingerprint)
    previous_n = len(state.hashes)

    # Rule 10: the stored messages must still match.
    match = matching_prefix(state.hashes, messages)
    if match < previous_n:
        if match < state.k:
            # Paged turns changed (for example a local compaction): page everything before the
            # current turn start, keep an in-flight turn's thinking, never send the full history.
            starts = turn_starts(messages)
            current = starts[-1] if starts else 0
            # Known limitation (reviewer-2): an in-flight tool loop keeps its thinking although the prefix
            # before it changed; on an enforcing account those blocks are invalid.
            inflight = any(isinstance(b, dict) and b.get("type") in THINKING_TYPES
                           for m in messages[current:] for b in _blocks(m))
            events.append({"event": "resync", "epoch": state.epoch, "kind": "paged_changed",
                           "matched": match, "previous_messages": previous_n, "messages": n,
                           "inflight_thinking_kept": inflight})
            fresh = replace(state, k=0, turns_paged=state.turns_paged)
            calls, attempt = StoreCalls(store), []
            try:
                resynced = new_epoch(messages, request, fresh, calls, config, estimator, current, current, attempt,
                                     "paged_changed")
            except STORE_FAILURES as error:
                # Rule 14: the changed turns cannot be paged and no earlier transform covers them.
                state = enter_degraded(state, store_failure(error, calls, state), events)
                return forward_original(request, messages, state, "resync_paged_changed", events)
            if state.degraded:
                events.append({"event": "degraded_leave", "epoch": state.epoch})
            events.extend(attempt)
            state = replace(resynced, degraded=False, conflict_page=None)
        else:
            events.append({"event": "resync", "epoch": state.epoch, "kind": "window_changed",
                           "matched": match, "previous_messages": previous_n, "messages": n})
            state = replace(state, last_input_tokens=None)
        previous_n = match

    try:
        return send(request, messages, previous_n, fingerprint, state, store, config, estimator, events)
    except Exception as error:
        if not (state.degraded or "degraded_enter" in names_of(events)):
            raise  # a genuine bug: the proxy forwards the original request and logs transform_error
        # Rule 14: a failure inside degraded mode is terminal for this request.
        return forward_original(request, messages, replace(state, degraded=True),
                                f"degraded_transform:{type(error).__name__}", events)


def count_tokens_view(request: dict, state: EpochState | None, dialect: Dialect | None = None) -> dict:
    """Rule 11: the current epoch's transform for ``count_tokens``; no rotation and no side effects."""
    if state is None:
        return request
    token = _DIALECT.set(dialect or ANTHROPIC)
    try:
        field_name = _dialect().field
        messages = request.get(field_name) or []
        if state.k and (state.k >= len(messages) or matching_prefix(state.hashes, messages) < state.k):
            return request  # history no longer matches the epoch; count what the CLI sent
        return {**request, field_name: transform_messages(messages, state)}
    finally:
        _DIALECT.reset(token)


def usage_tokens(usage: dict, dialect: Dialect | None = None) -> int:
    return (dialect or ANTHROPIC).usage_tokens(usage)


def dialect_for(endpoint: str) -> Dialect | None:
    """The dialect for a proxied endpoint, or None when the endpoint is passed through unchanged."""
    if endpoint in (MESSAGES, COUNT_TOKENS):
        return ANTHROPIC
    if endpoint.endswith("/responses"):
        from contexto.codex import RESPONSES  # imported lazily: contexto.codex builds on this module
        return RESPONSES
    return None


def turn_id(messages: list[dict], dialect: Dialect) -> str:
    """The current turn's id for the per-turn recall budget (contract PR #69): the number of
    turn-start messages in the CLI's full history, as a string. Stable across rotation, tool loops,
    retries, and proxy restarts; it changes at each new user turn start."""
    return str(sum(1 for m in messages if dialect.is_turn_start(m)))


def keyed(dialect: Dialect, request: dict) -> str:
    token = _DIALECT.set(dialect)
    try:
        return f"{dialect.name}:{conversation_key(request)}"
    finally:
        _DIALECT.reset(token)


def restore_ratio(store: Store, estimator: TokenEstimator) -> dict | None:
    """Packet A8 (c): an engine started on an existing session (a proxy restart, ``codex exec resume``,
    ``contexto run claude --session S``) begins from the bytes-per-token ratio the previous engine
    published through ``set_window`` instead of the seed, which the A6 smoke measured 1.34 to 1.40 times
    high (F 21.3k estimated against 15.3k measured, so W 14.7k instead of about 20.7k). The ratio enters
    the estimator with the weight of one mid-sized request, so the session's own usage soon dominates.
    The ratio is not checked against the model: ``set_window`` records no model yet (asked of lane B,
    A2e), so a session that changes model restores the previous model's ratio until its own usage
    dominates. Returns the content-free ``sizing_restored`` event (also logged), or None when there is nothing to
    restore or the store cannot be read (the next turn start meets the store again under rule 14)."""
    if estimator.measured_bytes_per_token is not None:
        return None
    try:
        _, ratio = store.window()
    except STORE_FAILURES:
        return None
    if not ratio:
        return None
    low, high = RESTORED_RATIO_RANGE
    if not low <= ratio <= high:
        # Reviewer-2 on PR #72: a recorded ratio no real session produces (corrupt metadata, or a usage
        # report that undercounted) must not size every request; the seed is the safer start.
        event = {"event": "sizing_not_restored", "reason": "ratio_out_of_range", "bytes_per_token": round(ratio, 4)}
    else:
        estimator.observe(round(ratio * RESTORED_RATIO_WEIGHT), RESTORED_RATIO_WEIGHT)
        event = {"event": "sizing_restored", "bytes_per_token": round(ratio, 4), "weight_tokens": RESTORED_RATIO_WEIGHT}
    log = getattr(store, "log", None)
    if callable(log):
        try:
            log(event)
        except STORE_FAILURES:
            pass  # the restore is an optimization; rule 14 handles a broken store at the next turn start
    return event if event["event"] == "sizing_restored" else None


@dataclass
class RewriteEngine:
    """Per-session adapter for the proxy: one epoch state per conversation (see ``conversation_key``)."""

    store: Store
    config: Config = field(default_factory=Config)
    estimator: TokenEstimator | None = None
    states: dict[str, EpochState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.estimator is None:
            self.estimator = TokenEstimator()
        self.restored = restore_ratio(self.store, self.estimator)

    def __call__(self, request: dict, headers: dict[str, str], endpoint: str) -> tuple[dict, dict[str, str], list[dict]]:
        with recall_knobs(self.config):
            return self._call(request, headers, endpoint)

    def _call(self, request: dict, headers: dict[str, str], endpoint: str) -> tuple[dict, dict[str, str], list[dict]]:
        dialect = dialect_for(endpoint)
        if dialect is None:
            return request, headers, []
        key = keyed(dialect, request)
        state = self.states.get(key)
        if endpoint == COUNT_TOKENS:
            epoch = state.epoch if state else 0
            return count_tokens_view(request, state), headers, [{"event": "count_tokens", "epoch": epoch}]
        upstream, self.states[key], events = rewrite(request, state, self.store, self.config, self.estimator, dialect)
        # Per-turn recall budget, request-time publish (contract PR #69): before forwarding, from the
        # pre-send estimate. No max_tokens reservation (intake 03:22Z): Claude Code sends max_tokens up
        # to 128,000, which would leave no room at all; the response-time publish adds the real output.
        state = self.states[key]
        if state.last_estimate is not None and not state.degraded:
            failures: list[dict] = []
            self.states[key] = self._publish_room(state, turn_id(request.get(dialect.field) or [], dialect),
                                                  state.last_estimate, failures)
            events[-1:-1] = failures  # the engine's request event stays last
        return upstream, headers, events

    def turn_room(self, used_tokens: int) -> int:
        """room_tokens = max(0, bound - (input + output) - headroom_floor) (contract PR #69)."""
        return max(0, self.config.bound - used_tokens - self.config.headroom_floor)

    def _publish_room(self, state: EpochState, current: str, used_tokens: int, events: list[dict]) -> EpochState:
        """set_turn_room through the rule 14 machinery: a store failure is the usual content-free
        store_error (operation set_turn_room) and enters degraded mode; nothing is logged on success,
        where the store itself records the room (lane B packet B11)."""
        calls = StoreCalls(self.store)
        try:
            calls.set_turn_room(current, self.turn_room(used_tokens))
        except STORE_FAILURES as error:
            return enter_degraded(state, store_failure(error, calls, state), events)
        return state

    def observe_usage(self, endpoint: str, usage: dict, request: dict) -> list[dict]:
        with recall_knobs(self.config):  # preamble() must see the same knobs as the request did
            return self._observe_usage(endpoint, usage, request)

    def _observe_usage(self, endpoint: str, usage: dict, request: dict) -> list[dict]:
        """Feed a response's real usage back (a transformed endpoint; never ``count_tokens``). Returns the
        content-free events it raised (packet A2b; contract rule 3 as amended by PR #44). The proxy writes
        them on that request's log entry (``usage_events``), the one place they are logged (A2e,
        agreed with builder-1); this method does not log them itself:

        - ``over_bound`` with ``source: "real"``: the real upstream input (input + cache read + cache
          write) exceeded the bound, whatever the pre-send estimate said, so acceptance 1 can rely on
          the session log (the pre-send flag stays on the request event);
        - ``estimate_error``: the real input exceeded the pre-send estimate by more than 10%, with the
          running mean of estimate / real for the conversation.
        """
        dialect = dialect_for(endpoint)
        if dialect is None or endpoint == COUNT_TOKENS:
            return []
        key = keyed(dialect, request)
        state = self.states.get(key)
        if state is None:
            return []
        real = usage_tokens(usage, dialect)
        self.estimator.observe(state.last_sent_bytes, real)  # the session's measured ratio from now on
        state = replace(state, last_input_tokens=real)
        if state.head_tokens is None and state.k == 0:
            # Rule 4: the first measured request gives the head (its input minus its non-carried
            # messages, which also covers a resumed first request with a long history).
            state = replace(state, head_tokens=max(0, real - state.last_window_tokens))
        if state.window_budget is None and state.head_tokens is not None:
            # A8 (c): W from the measured head, so set_window publishes W and the ratio after the first
            # response rather than only after a rotation decision, and a restarted engine can restore
            # the ratio even when the previous one never rotated. The epoch's in-memory pinned and index
            # text stand in for the store's (no store read here), composed with this engine's recall
            # knobs as sent; the next rotation decision recomputes W.
            pre = compose_preamble(self.config, state.pinned_text, state.index_text)
            f = state.head_tokens + (self.estimator.estimate(pre) if pre else 0)
            state = replace(state, window_budget=self.config.bound - headroom(state, self.config) - f)
        events: list[dict] = []
        estimate = state.last_estimate
        if estimate is not None and real > 0:
            state = replace(state, estimate_ratio_sum=state.estimate_ratio_sum + estimate / real,
                            estimate_ratio_count=state.estimate_ratio_count + 1)
        if real > self.config.bound:
            events.append({"event": "over_bound", "source": "real", "epoch": state.epoch, "over_bound": True,
                           "real_tokens": real, "estimate": estimate, "bound": self.config.bound})
        if estimate is not None and real > estimate * 1.1:
            events.append({"event": "estimate_error", "epoch": state.epoch, "real_tokens": real, "estimate": estimate,
                           "ratio": round(estimate / real, 3) if real else None,
                           "mean_ratio": round(state.estimate_ratio_sum / state.estimate_ratio_count, 3)})
        calls = StoreCalls(self.store)
        try:
            if not state.degraded:  # a degraded store is retried at the next turn start, not here
                # Per-turn recall budget, response-time publish (contract PR #69): the real input plus
                # this response's output (thinking and tool_use are passed back in the loop). The proxy
                # calls this at message_delta, inside the stream hold (PR #74), so the room lands before
                # any held tool_use reaches the CLI.
                output = usage.get("output_tokens") or 0
                calls.set_turn_room(turn_id(request.get(dialect.field) or [], dialect),
                                    self.turn_room(real + output))
                state = publish_window(calls, state, self.estimator)
        except STORE_FAILURES as error:
            # Rule 14: a store that cannot be written after a response is a store_error like any other;
            # the next turn start retries it. The event reaches the log with the others, via the proxy.
            state = enter_degraded(state, store_failure(error, calls, state), events)
        self.states[key] = state
        return events
