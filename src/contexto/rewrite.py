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
- Carried content is never paged except under the bounded fallback: the context blocks of paged
  turn-start messages and the carried system messages of paged turns (contract PR #35), in the form
  the CLI sends now. ``classify_system`` sorts every system message deterministically (contract PR
  #38, amended by D33): ``session_start`` (the ``# Environment`` signature, with or without settings
  fields), ``settings`` (empty content plus settings fields; rule 12), ``settings_change`` (text-only
  content plus settings fields without the signature: an effort change riding on a reminder),
  ``hook_output`` (paged with its turn), ``carried_text`` (any other text-only content) and
  ``unclassified`` (non-text content, with or without settings fields, also logging
  ``unclassified_system_message``). Settings-only
  messages accrue per settings change (measured), so only the latest per settings key is carried; the
  carried set is computed from the paged region, so it changes only at a rotation. The bounded
  fallback (``page_carried_text``, D32 and D33) pages, at a rotation, carried text, superseded
  settings changes and the context blocks of turn starts after the first, never the session-start
  message, the first turn start's blocks, the latest settings change per key or the latest item of a
  recognisable kind, until carried growth is back under ``carried_text_budget``. Rule 13 (PR #27,
  amended in PR #203): when carried content after the fallback is still grown by more than the
  larger of ``carried_budget`` and ``carried_text_budget`` (8,000 tokens with the defaults) over its
  size at the conversation's first request, so the fallback could not bound it, a content-free
  ``carried_over_budget`` event reports the estimate, the growth, the system-message count, and
  that threshold. Placement (rule 1, measured): carried context blocks
  and then the preamble go ahead of the first retained turn-start message's own content; carried
  system messages go immediately after that message (a system message before it returns 400).
- Rotation (rules 3 and 4, PR #27) happens only when the newest turn has no assistant message yet and
  the request adds messages (a resent request never rotates). The fixed overhead ``F`` (system, tools,
  carried content, preamble) comes from the conversation's first measured usage (its input minus its
  non-carried messages), else from the estimator; the window budget is ``W = bound - headroom - F``.
  Rotation picks the smallest ``k`` at a turn start whose retained turns fit ``window_fraction * W``,
  never below the current ``k``, always keeping the newest complete turn and the new turn. A ``k`` equal
  to the current one is a no-op (``rotation_noop``: nothing paged, nothing stripped); a ``W`` that
  cannot hold the newest complete turn and the new turn rotates to the minimal window (only the new
  turn; ``window_minimal``, contract PR #50), and a ``W`` that is not positive or cannot hold even the
  new turn also logs ``bound_too_small`` and still rotates there, so such a request can exceed the
  bound. ``F`` and ``W`` appear in these events.
- A second rotation condition (packet A12, contract PR #115): at a turn start, when the recall room
  the request-time publish would give the new turn (``bound - estimate - headroom_floor``) is below
  ``min_turn_room`` (default 12,000), the engine rotates with the window target ``0.5 * (W -
  min_turn_room)`` (minimal window as the fallback), reason ``min_turn_room``, but only if that
  restores at least ``min_turn_room``; when the size trigger fires too, the room target is used when it
  restores the room, so the next turn does not rotate again. A turn that still starts below
  ``min_turn_room`` logs ``turn_room_low`` once per epoch.
- The retained-window snapshot (packet A14, contract PR #125): after each request's transform, at the
  request-time room publish and before its ``set_turn_room``, the main conversation writes
  ``set_retained(epoch, k, entries)``, one ``{turn, message_index, role, text}`` per searchable block of
  ``M[k:]`` rendered by ``render_page`` (no thinking, images, context blocks, or carried system
  messages), so lane B's search can list in-view lines first. The store stamps it with the request
  sequence that both of the request's room publishes carry. A failed write logs ``retained_stale``
  (not degraded mode). Only the main conversation publishes the room and the snapshot: the first one
  seen, replaced at a turn start only by a conversation with strictly more turn starts. Every other
  conversation (a subagent, a side request) publishes a side room at each response instead (D40):
  ``set_side_room(room_id, bound - (input + output) - headroom_floor)`` when the response may hold a
  client tool call, ``set_side_room(room_id, None)`` when it holds none, where ``room_id`` is the
  room the request continues (``_publish_side_room``) or a new one. A tool call is charged against
  the smallest live room and counted against all of them, since the MCP server cannot tell which
  conversation made it.
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
  Within one engine call each message is hashed once and the request's turn starts are found once
  (``request_scope``), since the input is never mutated.
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
  than 1% after real usage; only for the main conversation (as for the room and the snapshot), never
  before the
  ratio is measured, never for a ``W`` that is not positive (``bound_too_small`` reports that case;
  the store accepts positive values only), never from ``observe_usage`` while degraded, and never
  from ``count_tokens``.
- An engine started on an existing session seeds its estimator with the ratio the previous engine
  published (``store.window()``, packet A8), so a proxy restart sizes like the process before it. The
  record is read at the engine's first request; when the recorded model differs from the request's
  (``window_model()``, lane B B12, packet A2f) the engine logs ``sizing_not_restored`` once and
  restores at the first later request on the recorded model, blended with what was measured
  meanwhile. Every ``set_window`` call passes the request's model,
  because a call without one clears the recorded model.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import re
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from typing import Any, ClassVar, NamedTuple, Protocol

from contexto.canonical import canonical_bytes, canonical_json
from contexto.cliversions import ClientVersions
from contexto.store import KeyFileError, PageConflictError, PageNotFoundError, SessionNotFoundError, side_room_live
from contexto.store.ledger import LedgerError
from contexto.store.render import render_page
from contexto.tokens import TokenEstimator
from contexto.toolcheck import ToolCheck

MESSAGES = "/v1/messages"
COUNT_TOKENS = "/v1/messages/count_tokens"
THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})
THINKING_OMITTED = "[thinking omitted]"
CONTEXT_TAG = "<system-reminder>"
STORE_ERRORS = (OSError, LedgerError, KeyFileError, SessionNotFoundError, PageNotFoundError)  # rule 14 store_error
STORE_FAILURES = (PageConflictError, *STORE_ERRORS)
WINDOW_RATIO_CHANGE = 0.01  # set_window: republish when the measured ratio moves more than this share
RESTORED_RATIO_WEIGHT = 10_000  # tokens: a restored ratio counts like one mid-sized request (A8)
RETAINED_ROLES = ("user", "assistant", "tool_result")  # A14: the roles set_retained accepts (lane B PR #128)
RESTORED_RATIO_RANGE = (1.0, 6.0)  # bytes per token a restore accepts; outside it the seed is safer (A2e)


class Store(Protocol):
    def page_out(self, turns: list[list[dict]], first_turn: int) -> list[str]: ...
    def pinned_text(self, budget_tokens: int) -> str: ...
    def index_text(self, budget_tokens: int) -> str: ...
    def set_window(self, window_tokens: int, bytes_per_token: float, model: str | None = None) -> None: ...
    def window(self) -> tuple[int | None, float | None]: ...
    def window_model(self) -> str | None: ...
    def set_turn_room(self, turn_id: str, room_tokens: int) -> None: ...
    # Optional (D40): set_side_room(room_id, room_tokens | None); without it side rooms are not published.
    def quarantine_page(self, page_id: str) -> Any: ...
    def set_retained(self, epoch: int, k: int, turns: list[dict]) -> None: ...  # lane B PR #128, contract PR #125


@dataclass(frozen=True)
class Config:
    bound: int = 150_000
    window_fraction: float = 0.5  # rule 4: retained window target as a share of the window budget W
    headroom_floor: int = 25_000
    min_turn_room: int = 12_000  # A12 (contract PR #115; 12,000 per intake/m2-min-room-12k): a turn's minimum recall room
    headroom_factor: float = 1.25
    headroom_cap: float = 0.25  # rule 17: headroom at most this share of the bound
    headroom_decay: float = 0.5  # rule 17: max_turn_growth's weight per later turn start that measures growth
    pin_budget: int = 6_000
    index_budget: int = 4_000
    # Rule 13: carried growth after the fallback above both budgets is reported. The default stays 4,000
    # so asdict(config), which a resume compares, is unchanged; with the defaults the report threshold
    # is carried_text_budget's 8,000, so the report means the fallback could not bound the growth.
    carried_budget: int = 4_000
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
    turn_room_low_epoch: int | None = None  # A12: the epoch whose turn_room_low was logged (once per epoch)
    window_minimal_count: int = 0  # rotations that kept only the new turn (A2c, contract PR #50)
    last_estimate: int | None = None  # pre-send estimate of the previous request (A2b)
    estimate_ratio_sum: float = 0.0  # running sum of estimate / real over observed requests
    estimate_ratio_count: int = 0
    degraded: bool = False  # rule 14: the store failed; no paging and no rotation until a retry succeeds
    conflict_page: str | None = None  # page id of the page_conflict that entered degraded mode
    window_budget: int | None = None  # W at the latest rotation decision
    published_window: tuple[int, float, str | None] | None = None  # (W, bytes per token, model) last given to set_window
    # Rule 3: (messages, hash of the last message) of the newest request sent whose usage has not arrived;
    # only that request's usage is paired with last_sent_bytes and last_estimate. Per-request, never recorded.
    pending_usage: tuple[int, str] | None = None


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
    return _turn_starts(messages, _dialect())


def _turn_starts(messages: list[dict], dialect: Dialect) -> list[int]:
    """Indexes of the turn-start messages under ``dialect``. Inside a ``request_scope`` the request's own
    message list is scanned once per dialect; every other list is scanned each time."""
    memo = _MEMO.get()
    if memo is None or messages is not memo.root:
        return [i for i, m in enumerate(messages) if dialect.is_turn_start(m)]
    hit = memo.starts.get(id(dialect))
    if hit is None or hit[0] is not dialect or hit[1] != len(messages):
        hit = (dialect, len(messages), tuple(i for i, m in enumerate(messages) if dialect.is_turn_start(m)))
        memo.starts[id(dialect)] = hit
    return list(hit[2])


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


def _is_single_text(message: dict) -> bool:
    content = message.get("content")
    return (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and set(content[0]) == {"type", "text"} and content[0]["type"] == "text")


def _message_hash(message: dict, dialect: Dialect) -> str:
    if dialect is ANTHROPIC and not _is_single_text(message):
        # Fast path, the same value: without a cache_control key anywhere and with no single text block
        # to unwrap, normalisation copies the message unchanged. A cache_control key would appear in
        # the canonical JSON as the quoted name; the name inside a string value only takes the slow path.
        # A message that cannot be encoded as it stands (a lone surrogate only inside cache_control,
        # which normalisation drops) also takes the slow path, so it fails or hashes as before.
        try:
            data = canonical_bytes(message)
        except UnicodeEncodeError:
            data = None
        if data is not None and b'"cache_control"' not in data:
            return hashlib.sha256(data).hexdigest()
    return hashlib.sha256(canonical_bytes(dialect.normalise(message))).hexdigest()


def message_hash(message: dict) -> str:
    """Rule 10's normalised hash. Inside a ``request_scope`` each message object is hashed once."""
    dialect = _dialect()
    memo = _MEMO.get()
    if memo is None:
        return _message_hash(message, dialect)
    hit = memo.hashes.get(id(message))
    if hit is not None and hit[0] is message and hit[1] is dialect:
        return hit[2]
    value = _message_hash(message, dialect)
    memo.hashes[id(message)] = (message, dialect, value)  # holding the message keeps its id unique
    return value


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

    def result_ids(self, item: dict) -> list[str]:
        """D40: the tool-call ids this item returns results for (its ``tool_result`` blocks)."""
        if item.get("role") != "user":
            return []
        return [b["tool_use_id"] for b in _blocks(item) if isinstance(b, dict) and b.get("type") == "tool_result"
                and isinstance(b.get("tool_use_id"), str)]

    def retained_blocks(self, item: dict) -> list[tuple[str, dict]]:
        """A14: the item's searchable blocks as (role, Messages API block): text that is not an injected
        context block, tool_use, and tool_result. Thinking and images are not searchable."""
        role = item.get("role", "unknown")
        content = item.get("content")
        if isinstance(content, str):
            return [(role, {"type": "text", "text": content})] if content else []
        return [(role, b) for b in content or [] if isinstance(b, dict)
                and b.get("type") in ("text", "tool_use", "tool_result") and not self.is_context_block(b)]

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

    def judges_session_start(self, request: dict) -> bool:
        """D38: Claude Code's title, topic, and quota side requests have no tool list and no session start."""
        return bool(request.get("tools"))

    def session_start_seen(self, items: list[dict]) -> bool | None:
        return _anthropic_session_start_seen(items)

    @property
    def session_start_version(self) -> str:
        """The CLI version the session-start recognition was measured on (D38's warning names it)."""
        return SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION


ANTHROPIC = Dialect()
_DIALECT: ContextVar[Dialect | None] = ContextVar("contexto_rewrite_dialect", default=None)


def _dialect() -> Dialect:
    return _DIALECT.get() or ANTHROPIC


class _RequestMemo:
    """Per-request caches. The engine never mutates the request it transforms, so within one engine
    call a message's normalised hash and the request list's turn starts do not change: each is computed
    once and reused by history matching, the epoch's hashes, usage attribution, and the turn scans."""

    __slots__ = ("root", "hashes", "starts")

    def __init__(self, root: Any) -> None:
        self.root = root  # the request's message list, the only list whose turn starts are cached
        self.hashes: dict[int, tuple[Any, Dialect, str]] = {}  # id(message) -> (message, dialect, hash)
        self.starts: dict[int, tuple[Dialect, int, tuple[int, ...]]] = {}  # id(dialect) -> (dialect, len, starts)


_MEMO: ContextVar[_RequestMemo | None] = ContextVar("contexto_rewrite_memo", default=None)


@contextmanager
def request_scope(messages: Any):
    """Cache hashes and turn starts for one request whose message list is ``messages``. A nested scope
    (``rewrite`` inside ``RewriteEngine``) reuses the outer one. A message dict hashed inside a scope
    must not be changed afterwards in that scope: the cache returns the hash it computed first."""
    if _MEMO.get() is not None:
        yield
        return
    token = _MEMO.set(_RequestMemo(messages))
    try:
        yield
    finally:
        _MEMO.reset(token)


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


def settings_fields(message: dict) -> tuple | None:
    """The names of a system message's settings fields (any field but role and content, such as
    output_config) and their keys; None when it has none."""
    if message.get("role") != "system":
        return None
    fields = sorted(k for k in message if k not in ("role", "content"))
    if not fields:
        return None
    return tuple((f, tuple(sorted(message[f])) if isinstance(message[f], dict) else ()) for f in fields)


def settings_key(message: dict) -> tuple | None:
    """For a settings-only system message (empty content, settings fields such as output_config),
    the names of its settings fields and their keys; None for any other message."""
    if message.get("content") not in ([], "", None):
        return None
    return settings_fields(message)


SESSION_START = "session_start"
SETTINGS = "settings"
SETTINGS_CHANGE = "settings_change"
HOOK_OUTPUT = "hook_output"
CARRIED_TEXT = "carried_text"
UNCLASSIFIED = "unclassified"

# D33: the session-start system message is recognised by its content, not by its settings fields or
# its position. Measured on Claude Code 2.1.302 (spikes/m2/scale/reminders, every main request of 13
# runs): on the 5.x ids the session's environment message is the only system message whose first
# line is "# Environment", with output_config (claude-opus-5-5, claude-haiku-5-5) or without it and
# wrapped in <system-reminder> (claude-sonnet-5, claude-fable-5); an effort change attaches
# output_config to whatever system message the turn already has (a countdown, a file note, the
# plan-mode text). On 2.1.301 the session-start message was the same size (about 8,470 characters,
# spikes/m2/carried finding 1); its text was not kept, so that version is inferred, not measured.
SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION = "2.1.302"
SESSION_START_SIGNATURE = re.compile(r"\s*(?:<system-reminder>\s*)?# Environment[ \t]*(?:\r?\n|\Z)")

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


def is_session_start(message: dict) -> bool:
    """D33: text-only content whose first line is the session-start signature (``# Environment``,
    optionally inside ``<system-reminder>``), with or without settings fields."""
    return is_text_only(message) and bool(SESSION_START_SIGNATURE.match(message_text(message)))


SESSION_START_SCAN_CHARS = 4096  # D38: a block's first line is all session_start_seen needs


def _anthropic_session_start_seen(messages: list[dict]) -> bool | None:
    """D38: does this request carry a session start Contexto recognises? True for a role ``system``
    message with the session-start signature (5.x ids) or a context block of kind ``environment``
    (4.x ids, in ``messages[0]``): measured on Claude Code 2.1.302, every main request of the 13
    reminders-spike runs has one of the two. False when the request carries the CLI's injected
    context (a role ``system`` message or a ``<system-reminder>`` block) but neither, which is what a
    CLI that changed its ``# Environment`` text would send. None when it carries no injected context,
    so it cannot tell (a client other than Claude Code). Only each block's head is read."""
    injected = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "system":
            injected = True
            if is_session_start(message):
                return True
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)):
                continue
            head = block["text"][:SESSION_START_SCAN_CHARS]
            if not head.lstrip().startswith(CONTEXT_TAG):
                continue
            injected = True
            if context_block_kind({"text": head}) == "environment":
                return True
    return False if injected else None


def _anthropic_classify_system(messages: list[dict], i: int) -> str:
    """Deterministic class of the system message at ``messages[i]``, in the contract's order (Terms,
    PR #38, amended by D33): session-start (the session-start signature, with or without settings
    fields; carried), settings-only (empty content plus settings fields; carried, latest per key),
    settings change (text-only content plus settings fields without the signature: an effort change
    riding on a reminder; carried while it is the latest per settings key, then pageable by the
    fallback), hook output (text-only, matching a documented signature; paged with its turn), other
    text-only content (carried, pageable by the fallback: output styles and plan mode are standing
    instructions), and non-text content, with or without settings fields (carried, never paged,
    logged as ``unclassified_system_message``). Position is not used;
    ``messages`` is accepted for a uniform signature."""
    m = messages[i]
    if is_session_start(m):
        return SESSION_START
    if settings_key(m) is not None:
        return SETTINGS
    if settings_fields(m) is not None and is_text_only(m):
        return SETTINGS_CHANGE
    if is_hook_output(m):
        return HOOK_OUTPUT
    if is_text_only(m):
        return CARRIED_TEXT
    return UNCLASSIFIED


# D32: kinds of injected context blocks, recognised by the first line inside <system-reminder>.
# Measured on Claude Code 2.1.302 with 4.x model ids (spikes/m2/scale/reminders/results/features.md):
# the first four arrive in later turn-start messages; the rest make up messages[0] and are listed so
# that a later copy (a date change, or a resume that puts session context in the resumed turn's
# message, spikes/m2/carried finding 6) keeps its latest copy. The fallback keeps the latest block of
# each kind and may page older ones; blocks of no recognised kind are pageable.
CONTEXT_BLOCK_KINDS_CLAUDE_CODE_VERSION = "2.1.302"
CONTEXT_BLOCK_KINDS = (
    ("token_countdown", re.compile(r"<total_tokens>")),
    ("plan_mode", re.compile(r"Plan mode is active|## Exited Plan Mode")),
    ("file_changed", re.compile(r"Note: .+ changed on disk")),
    ("task_reminder", re.compile(r"The task tools haven't been used recently")),
    ("environment", re.compile(r"# Environment[ \t]*\Z")),
    ("model", re.compile(r"You are powered by the model")),
    ("agent_types", re.compile(r"Available agent types")),
    ("mcp_instructions", re.compile(r"# MCP Server Instructions[ \t]*\Z")),
    ("skills", re.compile(r"The following skills are available")),
    ("instructions", re.compile(r"Codebase and user instructions are shown below")),
    ("date", re.compile(r"Today's date is")),
    ("attribution", re.compile(r"Attribution for git commits")),
)


def context_block_kind(block: dict) -> str | None:
    """The recognisable kind of an injected context block (D32), from the first non-empty line after
    the ``<system-reminder>`` tag; None when not recognised."""
    text = block.get("text", "").lstrip()
    if text.startswith(CONTEXT_TAG):
        text = text[len(CONTEXT_TAG):]
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    for kind, signature in CONTEXT_BLOCK_KINDS:
        if signature.match(first):
            return kind
    return None


def block_id(i: int, j: int, block: dict) -> str:
    """D32: the identity a paged context block has in ``paged_carried``: block ``j`` of message ``i``,
    with ``cache_control`` removed. The position keeps byte-identical blocks in different turn starts
    (the countdown repeats) apart; the history is append-only, so it is stable."""
    return hashlib.sha256(canonical_bytes({"carried_block": [i, j], "block": _drop_cache_control(block)})).hexdigest()


PAGEABLE_SYSTEM = (CARRIED_TEXT, SETTINGS_CHANGE)  # the classes the fallback may page (D33)


def _system_classes(messages: list[dict], k: int) -> dict[int, str]:
    return {i: classify_system(messages, i) for i in range(k) if is_system_like(messages[i])}


def protected_systems(messages: list[dict], k: int, classes: dict[int, str] | None = None) -> set[int]:
    """Indexes in M[0..k) the fallback never pages: the latest settings change per settings key (D33)
    and the latest text message of each recognisable kind (``mode_kind``). Computed over the messages,
    not the paged set, so a paged older copy with the same hash never hides the latest one."""
    classes = _system_classes(messages, k) if classes is None else classes
    latest: dict[tuple, int] = {}
    for i, kind in classes.items():
        if kind == SETTINGS_CHANGE:
            latest[("settings", settings_fields(messages[i]))] = i
        if kind in PAGEABLE_SYSTEM:
            mode = mode_kind(messages[i])
            if mode is not None:
                latest[("mode", mode)] = i
    return set(latest.values())


def carried_systems(messages: list[dict], k: int, paged: frozenset[str] = frozenset()) -> tuple[list[int], int]:
    """Indexes of the carried system messages in M[0..k), after deduplicating settings-only messages
    (latest per settings key) and leaving out messages the fallback has paged (never a protected one),
    and the number of unclassified ones among them."""
    classes = _system_classes(messages, k)
    protected = protected_systems(messages, k, classes) if paged else set()
    carried, latest, unclassified = [], {}, 0
    for i, kind in classes.items():
        if kind == HOOK_OUTPUT:
            continue
        if kind in PAGEABLE_SYSTEM and i not in protected and paged and message_hash(messages[i]) in paged:
            continue
        if kind == UNCLASSIFIED:
            unclassified += 1
        if kind == SETTINGS:
            latest[settings_key(messages[i])] = i
        carried.append(i)
    keep = [i for i in carried if classes[i] != SETTINGS or latest[settings_key(messages[i])] == i]
    return keep, unclassified


def reclassified_carried(messages: list[dict], k: int, paged: frozenset[str]) -> int:
    """Rule 16 across the D32 and D33 upgrade: how many system messages in M[0..k) this version carries
    although the resumed record's paged set names them by hash, and that v0.2.8 hid. v0.2.8 hid any
    carried text message whose hash was paged; this version carries one anyway when it is the
    session-start message (the claude-sonnet-5 and claude-fable-5 environment message, which v0.2.8
    classified as carried text) or the latest of its kind (protected even when an older byte-identical
    copy was paged). Messages with settings fields are left out: v0.2.8 classified them as
    session-start and never paged them. The record does not say which version wrote it, so on a record
    this version wrote the count can include a protected latest copy that was already carried; it is
    an upper bound on what changed. Context blocks need no check: v0.2.8 never paged one."""
    if not paged:
        return 0
    indexes, _ = carried_systems(messages, k, paged)
    return sum(1 for i in indexes if settings_fields(messages[i]) is None and message_hash(messages[i]) in paged)


def carried_blocks(messages: list[dict], k: int, paged: frozenset[str] = frozenset()) -> list[tuple[int, int, dict]]:
    """(message index, block index, block) for every context block of the turn starts in M[0..k) that the
    fallback has not paged (D32), in original order."""
    out = []
    for i in range(k):
        m = messages[i]
        if not is_turn_start(m):
            continue
        for j, b in enumerate(_blocks(m)):
            if is_context_block(b) and not (paged and block_id(i, j, b) in paged):
                out.append((i, j, b))
    return out


def carried_content(messages: list[dict], k: int, paged: frozenset[str] = frozenset()) -> tuple[list[dict], list[dict]]:
    """(context blocks of paged turn starts, carried system messages of paged turns), in original order
    and in the form the CLI sends now, without what the fallback paged. Hook output is paged with its
    turn, not carried."""
    blocks = [b for _, _, b in carried_blocks(messages, k, paged)]
    indexes, _ = carried_systems(messages, k, paged)
    return blocks, [messages[i] for i in indexes]


def fallback_candidates(messages: list[dict], k: int,
                        paged: frozenset[str] = frozenset()) -> list[tuple[tuple[int, int], str, dict]]:
    """What the bounded fallback may page from M[0..k), in the order it pages: (position, identity in
    ``paged_carried``, the page's one message). Superseded items of a recognisable kind come first,
    then unrecognised ones, each oldest first by position.

    System messages (D33): carried text and settings changes, never the session-start message, a
    settings-only message, the latest settings change per settings key, or the latest text message of
    each ``mode_kind``. Context blocks (D32): those of every turn start after the first, never the first
    turn start's blocks or the latest block of each ``context_block_kind``. A paged block is stored as a
    user message holding that one block, as the CLI sent it."""
    classes = _system_classes(messages, k)
    protected = protected_systems(messages, k, classes)
    superseded: list[tuple[tuple[int, int], str, dict]] = []
    unrecognised: list[tuple[tuple[int, int], str, dict]] = []
    for i in carried_systems(messages, k, paged)[0]:
        if classes[i] not in PAGEABLE_SYSTEM or i in protected:
            continue
        item = ((i, 0), message_hash(messages[i]), messages[i])
        (superseded if mode_kind(messages[i]) is not None else unrecognised).append(item)
    starts = turn_starts(messages[:k])
    later = [(i, j, b) for i, j, b in carried_blocks(messages, k, paged) if starts and i != starts[0]]
    newest = {}
    for i, j, b in later:
        kind = context_block_kind(b)
        if kind is not None:
            newest[kind] = (i, j)
    for i, j, b in later:
        kind = context_block_kind(b)
        if kind is not None and newest[kind] == (i, j):
            continue
        item = ((i, j), block_id(i, j, b), {"role": "user", "content": [b]})
        (superseded if kind is not None else unrecognised).append(item)
    order = lambda item: item[0]  # noqa: E731
    return sorted(superseded, key=order) + sorted(unrecognised, key=order)


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
    """Rule 17 (contract PR #186): one outlier turn raises headroom by at most ``headroom_cap`` of the
    bound, and ``max_turn_growth`` forgets it at ``headroom_decay`` per turn start that measures growth."""
    grown = math.ceil(state.max_turn_growth * config.headroom_factor)
    return max(config.headroom_floor, min(grown, math.floor(config.headroom_cap * config.bound)))


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
    budget = max(config.carried_budget, config.carried_text_budget)  # the fallback pages down to the latter
    if growth > budget:
        events.append({"event": "carried_over_budget", "epoch": new.epoch, "carried_tokens": carried,
                       "carried_growth": growth,
                       "system_messages": len(carried_systems(messages, k, new.paged_carried)[0]),
                       "budget": budget})
    return new


def page_carried_text(messages: list[dict], state: EpochState, store: Store, config: Config,
                      estimator: TokenEstimator, events: list[dict]) -> EpochState:
    """Terms fallback (amended by D32 and D33): when carried content has grown more than
    ``carried_text_budget`` over its size at the first request, page the carried items of paged turns
    that ``fallback_candidates`` lists, in its order (superseded ones of a recognisable kind first,
    then unrecognised ones, oldest first; never the session-start message, the first turn start's
    context blocks, the latest settings change per key, or the latest of a recognisable kind), until
    the growth is back under the budget. Runs only at a rotation, so carried content changes only
    there. Each item is its own page, written in position order; logs ``carried_paged``."""
    growth = carried_tokens(messages, estimator, state.paged_carried) - (state.initial_carried or 0)
    if growth <= config.carried_text_budget:
        return state
    chosen, freed = [], 0
    for item in fallback_candidates(messages, state.k, state.paged_carried):
        if growth - freed <= config.carried_text_budget:
            break
        chosen.append(item)
        freed += estimate_items(estimator, [item[2]])
    if not chosen:
        return state
    chosen.sort(key=lambda item: item[0])
    store.page_out([[page] for _, _, page in chosen], state.turns_paged)
    blocks = sum(1 for (i, _), _, page in chosen if page is not messages[i])  # a block's page is a new message
    # Content-free: how many were of no recognisable kind, so a CLI that adds a kind shows up in the log.
    def kind(i: int, page: dict) -> str | None:
        return context_block_kind(page["content"][0]) if page is not messages[i] else mode_kind(page)

    unrecognised = sum(1 for (i, _), _, page in chosen if kind(i, page) is None)
    events.append({"event": "carried_paged", "epoch": state.epoch, "count": len(chosen), "tokens": freed,
                   "context_blocks": blocks, "unrecognised": unrecognised})
    return replace(state, paged_carried=state.paged_carried | {ident for _, ident, _ in chosen},
                   turns_paged=state.turns_paged + len(chosen))


def usage_identity(messages: list[dict]) -> tuple[int, str]:
    """Which request a response's usage measured: its message count and its last message's hash."""
    return len(messages), message_hash(messages[-1]) if messages else ""


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
    store failures (page_out, pinned_text, index_text, set_window); ``guarded_rotation`` handles them.

    Two conditions (contract PR #115): the size trigger (the estimate passes ``bound - headroom``) and,
    packet A12, the room condition: the recall room the request-time publish would give the new turn,
    ``bound - estimate - headroom_floor``, is below ``min_turn_room``. A rotation for the room sizes its
    window target as ``0.5 * (W - min_turn_room)`` (minimal window as the fallback) and fires only if it
    would restore at least ``min_turn_room``; otherwise there is no rotation for the room and
    ``turn_room_low`` is logged once per epoch, so a small bound does not rotate on every turn."""
    trigger = estimate > config.bound - headroom(state, config)
    room_low = config.bound - estimate - config.headroom_floor < config.min_turn_room
    if state.head_changed or trigger or room_low:
        f = fixed_overhead(request, messages, state, store, config, estimator)
        w = config.bound - headroom(state, config) - f
        starts = turn_starts(messages)
        new_start = starts[-1]
        newest_start = starts[-2] if len(starts) >= 2 else new_start
        sizing = {"epoch": state.epoch, "fixed_overhead": f, "window_budget": w}
        target = int(config.window_fraction * w)
        room_k, room = state.k, 0
        if room_low:
            room_target = int(config.window_fraction * (w - config.min_turn_room))
            if room_target > 0 and window_tokens(messages, estimator, newest_start) <= room_target:
                room_k = choose_k(messages, state, room_target, estimator)
            else:
                room_k = new_start  # the minimal window: only the new turn
            room = config.bound - config.headroom_floor - f - window_tokens(messages, estimator, room_k)
        restores = room_low and room_k > state.k and room >= config.min_turn_room
        epoch_before = state.epoch
        if room_low and not (state.head_changed or trigger):
            if restores:
                state = new_epoch(messages, request, state, store, config, estimator, room_k, n, events,
                                  "min_turn_room", sizing=(f, w))
        elif state.head_changed:  # rule 6: always a new epoch; page only if the budget allows
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
            if restores and room_k > k:
                # Both conditions: the room target keeps the turn's room, so the next turn start does not
                # rotate again for it (no back-to-back rotations).
                k = room_k
            if k == state.k:  # rule 4: nothing to page, so no strip and no rebuild
                events.append({"event": "rotation_noop", **sizing, "k": k})
            else:
                state = new_epoch(messages, request, state, store, config, estimator, k, n, events, "bound",
                                  sizing=(f, w))
        if room_low and state.turn_room_low_epoch != state.epoch:
            # The turn starts with less than min_turn_room even after this decision (no rotation could
            # restore it, or a size rotation could not): say so once per epoch, never per turn.
            sent_room = (config.bound - estimate - config.headroom_floor if state.epoch == epoch_before
                         else config.bound - config.headroom_floor - f - window_tokens(messages, estimator, state.k))
            if sent_room < config.min_turn_room:
                events.append({"event": "turn_room_low", "epoch": state.epoch, "room": sent_room,
                               "best_room": room, "min_turn_room": config.min_turn_room})
                state = replace(state, turn_room_low_epoch=state.epoch)
        state = publish_window(store, replace(state, window_budget=w), estimator, window_model_name(request))
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


def window_model_name(request: dict) -> str | None:
    """The request's model as ``set_window`` accepts it (1 to 200 printable characters), else None."""
    model = request.get("model")
    return model if isinstance(model, str) and 0 < len(model) <= 200 and model.isprintable() else None


# Whether the conversation being transformed is the main one, which alone publishes the window (the
# engine sets it per request; a bare ``rewrite`` call is a single conversation, so it defaults to True).
_WINDOW_OWNER: ContextVar[bool] = ContextVar("contexto_rewrite_window_owner", default=True)


def publish_window(store: Store, state: EpochState, estimator: TokenEstimator, model: str | None) -> EpochState:
    """Lane B B9 and B12: give ``W``, the measured ratio, and the request's model to ``set_window`` when
    ``W`` or the model changed or the ratio moved more than ``WINDOW_RATIO_CHANGE``. Every call passes
    the model, since a call without one clears the recorded model (reviewer-1 on PR #82). Skipped
    before the ratio is measured, for a ``W`` that is not positive, which ``set_window`` rejects, and
    for a conversation that is not main (``published_window`` stays as it was, so a side conversation
    that later becomes main publishes at its first chance as main)."""
    ratio = estimator.measured_bytes_per_token
    if not _WINDOW_OWNER.get() or state.window_budget is None or state.window_budget <= 0 or ratio is None:
        return state
    published = state.published_window
    if (published is not None and published[0] == state.window_budget and published[2] == model
            and abs(ratio - published[1]) <= WINDOW_RATIO_CHANGE * published[1]):
        return state
    store.set_window(state.window_budget, ratio, model=model)
    return replace(state, published_window=(state.window_budget, ratio, model))


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
    return request, replace(state, degraded=True, last_sent_bytes=sent, last_estimate=None, last_input_tokens=None,
                            pending_usage=usage_identity(messages)), events


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
    if state.pending_usage is not None and state.last_input_tokens is not None:
        # Rule 3: the previous request sent got no usage (a 4xx or 5xx, a dropped stream, a response
        # without usage, or one still in flight), so the last real input predates it and would leave its
        # growth out: estimate the whole request instead. Logged only when it changes the estimate.
        events.append({"event": "usage_missing", "epoch": state.epoch})
        state = replace(state, last_input_tokens=None)
    estimate = estimate_upstream(request, messages, previous_n, state, estimator)
    if n > previous_n and at_turn_start(messages):
        count = len(turn_starts(messages))
        if state.last_turn_start_n is not None and count > state.last_turn_count and n > state.last_turn_start_n:
            # Per-turn growth: the messages added since the previous turn start, estimated at the current
            # ratio, averaged when several turns arrive at once (a resume or a missed request).
            added = estimate_items(estimator, messages[state.last_turn_start_n:])
            growth = added // (count - state.last_turn_count)
            # Rule 17: the previous maximum decays once per turn start that measures growth, before the
            # rotation decision. A retry or a resumed engine's first request (the turn start that wrote
            # the record) measures nothing here, so it never decays twice.
            decayed = math.floor(state.max_turn_growth * config.headroom_decay)
            state = replace(state, max_turn_growth=max(decayed, growth))
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
                    last_window_tokens=window_tokens(upstream[field_name], estimator),
                    pending_usage=usage_identity(messages))
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
        with request_scope(request.get(_dialect().field)):
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
    return str(len(_turn_starts(messages, dialect)))


def retained_entries(messages: list[dict], k: int, dialect: Dialect) -> list[dict]:
    """Packet A14 (contract PR #125): the retained window ``M[k:]`` as search entries, one per searchable
    block, in history order: ``{turn, message_index, role, text}``. ``turn`` counts turn starts through
    that message (the newest equals ``turn_id``); ``message_index`` is the position in the CLI's full
    history; ``text`` is the block rendered by ``render_page``, the renderer recall uses, so a line
    found in view reads like the same line in a page. Carried system messages and injected context
    blocks are not turn text and are left out; thinking and images are never included. The entries
    hold conversation text: they go to the store only, never to a log or an event."""
    turn = bisect.bisect_left(_turn_starts(messages, dialect), k)  # turn starts in M[:k]
    entries = []
    for i in range(k, len(messages)):
        item = messages[i]
        if dialect.is_turn_start(item):
            turn += 1
        if dialect.is_system_like(item):
            continue
        for role, block in dialect.retained_blocks(item):
            # The store's roles (lane B PR #128): a tool result is its own role, whoever carried it.
            role = "tool_result" if block.get("type") == "tool_result" else role
            if role not in RETAINED_ROLES:
                continue
            _, _, text = render_page({"turn": turn, "messages": [{"role": role, "content": [block]}]}).partition("\n")
            if text:
                entries.append({"turn": turn, "message_index": i, "role": role, "text": text})
    return entries


def keyed(dialect: Dialect, request: dict) -> str:
    token = _DIALECT.set(dialect)
    try:
        return f"{dialect.name}:{conversation_key(request)}"
    finally:
        _DIALECT.reset(token)


def window_record(store: Store) -> tuple[float, str | None] | None:
    """The ratio and model the previous engine gave ``set_window``, or None when there is no ratio or
    the store cannot be read."""
    try:
        _, ratio = store.window()
        recorded_model = store.window_model() if ratio else None
    except STORE_FAILURES:
        return None
    return (ratio, recorded_model) if ratio else None


def restore_ratio(store: Store, estimator: TokenEstimator, model: str | None, *, allow_measured: bool = False,
                  record: tuple[float, str | None] | None = None) -> dict | None:
    """Packet A8 (c): an engine started on an existing session (a proxy restart, ``codex exec resume``,
    ``contexto run claude --session S``) begins from the bytes-per-token ratio the previous engine
    published through ``set_window`` instead of the seed, which the A6 smoke measured 1.34 to 1.40 times
    high (F 21.3k estimated against 15.3k measured, so W 14.7k instead of about 20.7k). The ratio enters
    the estimator with the weight of one mid-sized request, so the session's own usage soon dominates.
    It runs at the engine's first request, so the ratio is skipped when the recorded model differs from
    that request's (packet A2f; a ratio measured on another model's tokenizer would mis-size every
    request); a record with no model (written before lane B B12) is restored as before. Returns the
    content-free ``sizing_restored`` or ``sizing_not_restored`` event for that request's events, or
    None when there is nothing to restore or the store cannot be read (the next turn start meets the
    store again under rule 14).

    ``record`` is a ``window_record`` read earlier (else the store is read now). ``allow_measured`` is
    the engine's deferred restore after ``model_changed``: a later request on the recorded model blends
    the recorded ratio with what other requests measured meanwhile, instead of being skipped."""
    if estimator.measured_bytes_per_token is not None and not allow_measured:
        return None
    if record is None:
        record = window_record(store)
    if record is None:
        return None
    ratio, recorded_model = record
    low, high = RESTORED_RATIO_RANGE
    if recorded_model is not None and model is not None and recorded_model != model:
        return {"event": "sizing_not_restored", "reason": "model_changed", "bytes_per_token": round(ratio, 4)}
    if not low <= ratio <= high:
        # Reviewer-2 on PR #72: a recorded ratio no real session produces (corrupt metadata, or a usage
        # report that undercounted) must not size every request; the seed is the safer start.
        event = {"event": "sizing_not_restored", "reason": "ratio_out_of_range", "bytes_per_token": round(ratio, 4)}
    else:
        estimator.observe(round(ratio * RESTORED_RATIO_WEIGHT), RESTORED_RATIO_WEIGHT)
        event = {"event": "sizing_restored", "bytes_per_token": round(ratio, 4), "weight_tokens": RESTORED_RATIO_WEIGHT}
    return event


# Rule 16 (contract PR #176): the main conversation's epoch record, so a new engine on the session resumes
# the epoch instead of rotating again from epoch 0 ---------------------------------------------------------
RECORD_INT = ("epoch", "k", "strip_upto", "turns_paged", "max_turn_growth", "last_turn_count",
              "carried_text_streak", "window_minimal_count", "last_turn_start_n", "head_tokens",
              "initial_carried", "last_carried_text_count", "turn_room_low_epoch", "window_budget")


RECORD_NULLABLE = frozenset({"last_turn_start_n", "head_tokens", "initial_carried", "last_carried_text_count",
                             "turn_room_low_epoch", "window_budget"})


def anchor_hash(hashes: tuple[str, ...] | list[str]) -> str:
    """The anchor over a request's first ``n`` normalised message hashes (rule 10)."""
    return hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()


# D40: Claude Code's subagents, as measured on 2.1.302 against a scripted stub (zero spend). Main
# launches one with an ``Agent`` tool call (``Task`` in earlier releases); the subagent's first user
# message carries a ``<system-reminder>`` block and then the call's ``prompt`` as its own text block.
AGENT_TOOLS = frozenset({"Agent", "Task"})
ASYNC_LAUNCH = "Async agent launched successfully."  # the tool result of a background launch
TASK_NOTIFICATION = "<task-notification>"
NOTIFIED_CALL = re.compile(r"<tool-use-id>([^<\s]+)</tool-use-id>")


class _SideRoom(NamedTuple):
    """D40: what the engine remembers about a side room it opened."""

    owner: str  # the conversation key
    n: int  # the publishing request's message count
    anchor: str  # its anchor hash over those messages
    at: float  # the publish time, on the store's clock
    ids: frozenset[str]  # the client tool-call ids its response held
    prompt: str | None  # the conversation's first prompt, to match main's Agent call (Messages only)
    main_len: int  # main's message count at the publish


def _texts(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    return [b["text"] for b in _blocks(message) if isinstance(b, dict) and isinstance(b.get("text"), str)]


def first_prompt(messages: list[dict]) -> str | None:
    """A Messages conversation's first prompt: its first user message's text blocks that are not
    injected context, joined by newlines; None when it has none."""
    for message in messages:
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content or None
            parts = [b["text"] for b in _blocks(message) if isinstance(b, dict) and b.get("type") == "text"
                     and isinstance(b.get("text"), str) and not _anthropic_is_context_block(b)]
            return "\n".join(parts) or None
    return None


def agent_stops(messages: list[dict]) -> tuple[dict[str, list[str]], dict[str, int]]:
    """D40: main's ``Agent`` calls by prompt (prompt -> tool-use ids), and the calls whose agent has
    stopped, each with the index of the latest message that says so: a result that is not the
    asynchronous launch receipt (a foreground hand-back, completed or interrupted), or a
    ``<task-notification>`` naming the call (Claude Code sends one, in a ``system`` message during
    main's tool loop or a ``<system-reminder>`` user message while main is idle, each time a
    background agent or command stops)."""
    prompts: dict[str, list[str]] = {}
    stopped: dict[str, int] = {}
    for i, message in enumerate(messages):
        for block in _blocks(message):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in AGENT_TOOLS:
                prompt = (block.get("input") or {}).get("prompt")
                if isinstance(prompt, str) and isinstance(block.get("id"), str):
                    prompts.setdefault(prompt, []).append(block["id"])
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                content = block.get("content")
                text = content if isinstance(content, str) else "".join(
                    b.get("text", "") for b in content or [] if isinstance(b, dict))
                if not text.lstrip().startswith(ASYNC_LAUNCH):
                    stopped.setdefault(block["tool_use_id"], i)  # the hand-back of a foreground agent
        for text in _texts(message):
            if TASK_NOTIFICATION in text:
                for call_id in NOTIFIED_CALL.findall(text):
                    stopped[call_id] = i
    agent_ids = {i for ids in prompts.values() for i in ids}
    return prompts, {call_id: i for call_id, i in stopped.items() if call_id in agent_ids}


def preamble_sha256(config: Config, pinned_text: str, index_text: str) -> str:
    return hashlib.sha256(compose_preamble(config, pinned_text, index_text).encode("utf-8")).hexdigest()


def epoch_record(state: EpochState, request: dict, messages: list[dict], key: str, dialect: Dialect,
                 config: Config) -> dict:
    """The record ``set_epoch`` writes: every field of ``state`` that decides later request bytes or
    rotation decisions, the configuration, and the anchor. Per-request values (the previous request's
    hashes, sizes, and estimates) are not in it."""
    hashes = state.hashes if len(state.hashes) == len(messages) else tuple(message_hash(m) for m in messages)
    return {**{name: getattr(state, name) for name in RECORD_INT},
            "pinned_text": state.pinned_text, "index_text": state.index_text,
            "preamble_sha256": preamble_sha256(config, state.pinned_text, state.index_text),
            "head_fingerprint": state.head_fingerprint, "dialect": dialect.name, "conversation": key,
            "model": request.get("model") if isinstance(request.get("model"), str) else None,
            "paged_carried": sorted(state.paged_carried), "config": asdict(config),
            "anchor": {"n": len(messages), "hash": anchor_hash(hashes)}}


def resume_state(record: dict, request: dict, messages: list[dict], dialect: Dialect,
                 config: Config) -> tuple[EpochState | None, str]:
    """Rule 16's checks, in order; the resumed state, or None and the ``epoch_not_resumed`` reason. Call
    with the dialect set. The resumed state seeds rule 10's previous-request hashes with the first ``n``
    messages, so the request is matched as an append."""
    for name in RECORD_INT:  # the engine validates meaning; a value it cannot use is an unreadable record
        value = record[name]
        if isinstance(value, bool) or not (isinstance(value, int) or (value is None and name in RECORD_NULLABLE)):
            raise ValueError(f"epoch record field {name}")
    if not all(isinstance(record[name], str) for name in ("pinned_text", "index_text", "head_fingerprint")):
        raise ValueError("epoch record text field")
    if record.get("dialect") != dialect.name:
        return None, "dialect_changed"
    model = request.get("model") if isinstance(request.get("model"), str) else None
    if record.get("model") != model:
        return None, "model_changed"
    if record.get("config") != asdict(config):
        return None, "settings_changed"
    if record.get("preamble_sha256") != preamble_sha256(config, record["pinned_text"], record["index_text"]):
        return None, "preamble_changed"
    if record.get("head_fingerprint") != head_fingerprint(request):
        return None, "head_changed"
    n = record["anchor"]["n"]
    if len(messages) < n:
        return None, "history_shorter"
    hashes = tuple(message_hash(m) for m in messages[:n])
    if anchor_hash(hashes) != record["anchor"]["hash"]:
        return None, "history_diverged"
    state = EpochState(pinned_text=record["pinned_text"], index_text=record["index_text"],
                       head_fingerprint=record["head_fingerprint"], hashes=hashes,
                       paged_carried=frozenset(record["paged_carried"]),
                       **{name: record[name] for name in RECORD_INT})
    return state, ""


@dataclass
class RewriteEngine:
    """Per-session adapter for the proxy: one epoch state per conversation (see ``conversation_key``).

    Rule 16 (contract PR #176): the main conversation writes its epoch record at every turn start and
    on leaving degraded mode, after the transform and before the request is sent; a new engine reads
    the record once and, at the first request of the matching conversation, resumes the epoch when
    every check holds. A resumed engine sends the same request bytes as the old one would have, except
    once after an upgrade that reclassifies carried content (``carried_reclassified``). Its
    first rule 4 estimate is not the same: the previous request's real input (``last_input_tokens``)
    is per-request and not recorded, so that first decision sizes from a full estimate."""

    store: Store
    config: Config = field(default_factory=Config)
    estimator: TokenEstimator | None = None
    states: dict[str, EpochState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.estimator is None:
            self.estimator = TokenEstimator()
        self.restored: dict | None = None  # the sizing_restored event, once the first request restored it
        self._turn_starts: dict[str, int] = {}  # A14: conversation key -> turn starts at its latest request
        self._main: str | None = None  # A14: the conversation that publishes the room and the snapshot
        self._restore_pending = True  # A2f: the restore waits for a request on the recorded model
        self._restore_record: tuple[float, str | None] | None = None  # the previous engine's, read once
        self._restore_mismatch_logged = False  # sizing_not_restored model_changed is logged once
        # Rule 16: the epoch record, read once at the engine's first request.
        self._record_read = False
        self._record: dict | None = None
        self._record_reason = "no_record"  # why there is no record to resume from
        self._record_consumed = False  # the matching conversation has been checked (resumed or not)
        self._resumed = False
        # Are Contexto's MCP tools reaching the main conversation? (contexto.toolcheck)
        self.tools = ToolCheck(self.store)
        # D38: which CLI version sends the requests, and was the session start recognised?
        self.clients = ClientVersions()
        self._side_rooms: dict[str, _SideRoom] = {}  # D40: the side rooms this engine opened, by id
        self._main_len = 0  # D40: the message count of main's latest request

    # The engine never mutates the request it is given (the proxy then skips its defensive copy).
    preserves_input: ClassVar[bool] = True

    def __call__(self, request: dict, headers: dict[str, str], endpoint: str) -> tuple[dict, dict[str, str], list[dict]]:
        dialect = dialect_for(endpoint)
        with recall_knobs(self.config), request_scope(request.get(dialect.field) if dialect else None):
            return self._call(request, headers, endpoint)

    def _call(self, request: dict, headers: dict[str, str], endpoint: str) -> tuple[dict, dict[str, str], list[dict]]:
        dialect = dialect_for(endpoint)
        if dialect is None:
            return request, headers, []
        key = keyed(dialect, request)
        state = self.states.get(key)
        if endpoint == COUNT_TOKENS:  # never publishes a room, here or on usage (contract PR #89, 6ffc22b)
            epoch = state.epoch if state else 0
            return count_tokens_view(request, state), headers, [{"event": "count_tokens", "epoch": epoch}]
        restore = self._restore(window_model_name(request)) if self._restore_pending else None
        resumed: list[dict] = []
        if state is None and not self._record_consumed:
            state, resumed = self._resume(key, request, dialect)
        # Only the main conversation publishes the window (W, ratio, model): the store holds one, which
        # sizes contexto_recall's per-call limit and the next engine's restore. Main is committed before
        # the transform, whose turn-start rotation may publish the window, so a request that fails after
        # that publish (a genuine bug, or degraded mode's forwarded original) still leaves it recorded
        # as main and the previous main's publish forgotten. publishes() below agrees for this request.
        main = self.publishes(key, request.get(dialect.field) or [], dialect)
        owner = _WINDOW_OWNER.set(main)
        try:
            upstream, self.states[key], events = rewrite(request, state, self.store, self.config, self.estimator,
                                                         dialect)
        finally:
            _WINDOW_OWNER.reset(owner)
        events[0:0] = resumed
        # The CLI's own request (rewrite() leaves it unchanged); the engine's request event stays last.
        # A diagnostic: if it ever raised, the proxy's catch-all would discard this successful rewrite.
        try:
            checked = self.tools.observe(request, dialect.name, main=main)
        except Exception as exc:
            checked = [{"event": "toolcheck_error", "error": type(exc).__name__}]  # content-free
        try:
            checked = [*checked, *self.clients.observe(request, headers, dialect)]
        except Exception as exc:  # a diagnostic, like the tool check
            checked = [*checked, {"event": "cli_version_error", "error": type(exc).__name__}]
        events[-1:-1] = checked
        events[-1].update(main=main, conversations=len(self.states))  # content-free, for side requests
        if main and endpoint != COUNT_TOKENS:
            closes: list[dict] = []
            self._close_finished_side_rooms(request.get(dialect.field) or [], dialect, closes)
            events[-1:-1] = closes  # the engine's request event stays last
        # Per-turn recall budget, request-time publish (contract PR #69, kept by 367916a): before forwarding, from the
        # pre-send estimate. No max_tokens reservation (intake 03:22Z): Claude Code sends max_tokens up
        # to 128,000, which would leave no room at all; the response-time publish adds the real output.
        state = self.states[key]
        if state.last_estimate is not None and not state.degraded:
            messages = request.get(dialect.field) or []
            if self.publishes(key, messages, dialect):
                failures: list[dict] = []
                # Contract PR #125: the snapshot first, so the store's request sequence stamps it and
                # both of this request's room publishes alike.
                self._publish_retained(state, messages, dialect, failures)
                self.states[key] = self._publish_room(state, turn_id(messages, dialect), state.last_estimate,
                                                      failures)
                events[-1:-1] = failures  # the engine's request event stays last
        self._write_epoch(key, request, dialect, events)
        return upstream, headers, [restore, *events] if restore else events

    def tools_verdict(self) -> dict | None:
        """At exit: ``mcp_tools_missing`` if the main conversation ended without Contexto's tools and that
        was not reported during the run (``contexto.toolcheck.ToolCheck.verdict``); the launcher logs it."""
        return self.tools.verdict()

    def _restore(self, model: str | None) -> dict | None:
        """Packet A8 (c) and A2f: restore the previous engine's ratio at the first request on its model.
        The record is read once, at the engine's first request, before this engine can overwrite it. A
        request on another model (often a side request that arrives first after a restart) logs
        ``sizing_not_restored`` ``model_changed`` once and leaves the restore pending; the first later
        request on the recorded model restores it, blended with what was measured meanwhile."""
        deferred = self._restore_record is not None
        if not deferred:
            if self.estimator.measured_bytes_per_token is not None:
                self._restore_pending = False
                return None
            self._restore_record = window_record(self.store)
            if self._restore_record is None:
                self._restore_pending = False
                return None
        event = restore_ratio(self.store, self.estimator, model, allow_measured=deferred, record=self._restore_record)
        if event is not None and event.get("reason") == "model_changed":
            if self._restore_mismatch_logged:
                return None
            self._restore_mismatch_logged = True
            return event
        self._restore_pending = False
        if event is not None and event["event"] == "sizing_restored":
            self.restored = event
        return event

    def _resume(self, key: str, request: dict, dialect: Dialect) -> tuple[EpochState | None, list[dict]]:
        """Rule 16: at the first request of a conversation new to this engine, resume the recorded epoch
        if the record is this conversation's and every check holds. The store is read once."""
        if not self._record_read:
            self._record_read = True
            reader = getattr(self.store, "epoch_record", None)
            try:
                self._record = reader() if callable(reader) else None
            except Exception:  # the store could not read it; never a reason to fail the request
                self._record, self._record_reason = None, "unreadable"
        if self._record is None:
            self._record_consumed = True
            return None, [{"event": "epoch_not_resumed", "epoch": 0, "reason": self._record_reason}]
        if not isinstance(self._record.get("conversation"), str):  # unusable for every conversation
            self._record_consumed = True
            return None, [{"event": "epoch_not_resumed", "epoch": 0, "reason": "unreadable"}]
        if self._record.get("conversation") != key:
            return None, [{"event": "epoch_not_resumed", "epoch": 0, "reason": "other_conversation"}]
        self._record_consumed = True
        token = _DIALECT.set(dialect)
        messages = request.get(dialect.field) or []
        reclassified = 0
        try:
            state, reason = resume_state(self._record, request, messages, dialect, self.config)
            if state is not None:
                reclassified = reclassified_carried(messages, state.k, state.paged_carried)
        except (KeyError, TypeError, ValueError, AttributeError):
            state, reason = None, "unreadable"  # a field the store let through but the engine cannot use
        finally:
            _DIALECT.reset(token)
        if state is None:
            return None, [{"event": "epoch_not_resumed", "epoch": 0, "reason": reason}]
        if not self._pages_present(state.turns_paged):
            return None, [{"event": "epoch_not_resumed", "epoch": 0, "reason": "pages_missing"}]
        self._set_main(key)
        self._resumed = True
        events = [{"event": "epoch_resumed", "epoch": state.epoch, "k": state.k}]
        if reclassified:
            # D33: a record written before D32 and D33 can have paged a message this version never
            # pages; it is carried again from this request on (one cache write), and the count says so.
            events.append({"event": "carried_reclassified", "epoch": state.epoch, "count": reclassified})
        return state, events

    def _pages_present(self, turns_paged: int) -> bool:
        """Packet A31: the resumed epoch's index and recall need its paged turns, one page per turn, so
        the store must hold at least ``turns_paged`` pages. A store that cannot list them does not resume."""
        if turns_paged == 0:
            return True
        lister = getattr(self.store, "page_ids", None)
        try:
            return callable(lister) and len(lister()) >= turns_paged
        except Exception:  # the pages cannot be counted: resuming would be a guess
            return False

    def _may_write(self, turn_starts: int) -> bool:
        """Rule 16, who writes: after a restart nothing writes until a conversation has resumed from the
        record or has strictly more turn starts than the record's, so a side request cannot overwrite it."""
        if self._record is None or self._resumed:
            return True
        count = self._record.get("last_turn_count")
        return isinstance(count, int) and turn_starts > count

    def _write_epoch(self, key: str, request: dict, dialect: Dialect, events: list[dict]) -> None:
        """Rule 16, record: the main conversation, at every turn start and on leaving degraded mode,
        after the transform and before the send. A degraded engine writes nothing; a failed write logs
        ``epoch_record_stale`` and never blocks the request or degrades the engine."""
        state = self.states[key]
        writer = getattr(self.store, "set_epoch", None)
        if state.degraded or not callable(writer):
            return
        messages = request.get(dialect.field) or []
        token = _DIALECT.set(dialect)
        try:
            due = at_turn_start(messages) or "degraded_leave" in names_of(events)
            if not (due and self.publishes(key, messages, dialect) and self._may_write(self._turn_starts.get(key, 0))):
                return
            record = epoch_record(state, request, messages, key, dialect, self.config)
        finally:
            _DIALECT.reset(token)
        try:
            writer(record)
        except Exception as error:  # the record only saves cache; losing it costs one rotation
            events[-1:-1] = [{"event": "epoch_record_stale", "epoch": state.epoch, "error": type(error).__name__}]

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

    def publishes(self, key: str, messages: list[dict], dialect: Dialect) -> bool:
        """Contract PR #125 (477151e): only the main conversation publishes the room and the retained
        snapshot; the session store holds one of each and the MCP server cannot tell which conversation
        called it. The first conversation seen is main. At each of a conversation's turn starts the
        choice is re-evaluated: it becomes main only with strictly more turn starts than the current
        main (a tie keeps the current one), so a subagent or side request, with about one turn start,
        never takes over, and main never changes inside a tool loop. The window (``set_window``) follows
        the same choice, committed before the transform; when main moves away from a conversation, its
        last publish is forgotten, since the new main replaces the store's window, so it publishes again
        if it becomes main again."""
        main, self._turn_starts[key] = self._main_after(key, messages, dialect)
        self._set_main(main)
        return key == self._main

    def _set_main(self, key: str) -> None:
        previous = self._main
        self._main = key
        if previous is not None and previous != key and previous in self.states:
            self.states[previous] = replace(self.states[previous], published_window=None)

    def _main_after(self, key: str, messages: list[dict], dialect: Dialect) -> tuple[str, int]:
        """The main conversation after this request, and this request's turn-start count."""
        starts = _turn_starts(messages, dialect)
        main = key if self._main is None else self._main
        at_start = bool(starts) and not any(dialect.is_assistant(m) for m in messages[starts[-1]:])
        if at_start and key != main and len(starts) > self._turn_starts.get(main, 0):
            main = key
        return main, len(starts)

    def _publish_retained(self, state: EpochState, messages: list[dict], dialect: Dialect,
                          events: list[dict]) -> None:
        """Contract PR #125: the retained window's snapshot, once per request after the transform, a full
        overwrite each time. A failed write leaves the snapshot stale (search then says "in view not
        checked"): it logs the content-free retained_stale, exactly once, and is not degraded mode
        (lane B PR #128 removes the old snapshot, so ``retained_current()`` serves none)."""
        try:
            self.store.set_retained(state.epoch, state.k, retained_entries(messages, state.k, dialect))
        except STORE_FAILURES as error:
            events.append({"event": "retained_stale", "epoch": state.epoch, "error": type(error).__name__})

    def _now(self) -> float:
        clock = getattr(self.store, "clock", None)  # the store's clock, which expires its rooms
        return clock() if callable(clock) else time.time()

    def _close_finished_side_rooms(self, messages: list[dict], dialect: Dialect, events: list[dict]) -> None:
        """D40: called for each main request. A side room closes early only on evidence in main's own
        history that the subagent holding it has stopped. The subagent is matched by its first
        prompt, which Claude Code 2.1.302 sends exactly as the ``prompt`` of main's ``Agent`` (or
        ``Task``) call. The evidence (``agent_stops``) is a result for that call that is not the
        asynchronous launch receipt (a foreground hand-back, completed or interrupted), or a
        ``<task-notification>`` naming the call's tool-use id (sent when the agent stops: completed,
        failed, or killed). The evidence must come after the room's publish (at a message index at
        least main's message count then), so an agent resumed after an earlier stop keeps its new
        room, and every ``Agent`` call with that prompt must have stopped (twins). Rooms matching no
        call, and agents still running in the background, keep the 15-minute expiry. Logged as
        ``side_room_closed`` with reason ``agent_stopped``."""
        self._main_len = len(messages)
        if not self._side_rooms or dialect.name != "anthropic":
            return
        prompts, stopped = agent_stops(messages)
        doomed = []
        for room_id, room in self._side_rooms.items():
            ids = prompts.get(room.prompt) if room.prompt else None
            if ids and all(i in stopped for i in ids) and max(stopped[i] for i in ids) >= room.main_len:
                doomed.append(room_id)
        if not doomed:
            return
        now = self._now()
        for room_id in doomed:
            try:
                self.store.set_side_room(room_id, None)
            except STORE_FAILURES as error:  # the room expires instead; not degraded mode
                events.append({"event": "side_room_close_failed", "error": type(error).__name__})
                continue
            age = int(now - self._side_rooms.pop(room_id).at)
            events.append({"event": "side_room_closed", "reason": "agent_stopped", "age_s": age})

    def _publish_side_room(self, calls: StoreCalls, key: str, messages: list[dict], dialect: Dialect,
                           room: int | None, call_ids: tuple[str, ...], events: list[dict]) -> None:
        """D40: open, replace, or close the side room of the request ``messages``.

        Rooms are per request lineage, not per conversation key: two subagents launched with the same
        prompt, or a side request that forks one, share a key but must not share a room. Each room
        remembers the request that published it (its message count and anchor hash), the ids of the
        client tool calls its response held (``call_ids``, from the proxy), the conversation's first
        prompt (to match main's ``Agent`` call, see ``_close_finished_side_rooms``), and main's
        message count at the publish. A request continues a room when it extends that room's
        request, adds no turn start, and (for a room recorded with ids) returns a result
        (``tool_result``, ``*_call_output``) for one of those ids. A twin carries its own ids and
        cannot match. A fork that copies results for the room's calls does match; that is harmless
        once those calls finished, but a fork that closes a still-running call with a placeholder
        result and asks its question inside that result message cannot be told apart from the next
        step (documented). Exactly one room must match: when two or more tie, the request continues
        none of them, they keep their spend until they expire, and it opens its own room (or with
        ``room`` None changes nothing). The lineage is kept in memory and forgotten once the room has
        expired in the store (``SIDE_ROOM_TTL_S``)."""
        token = _DIALECT.set(dialect)
        try:
            hashes = [message_hash(m) for m in messages]
        finally:
            _DIALECT.reset(token)
        now = self._now()
        self._side_rooms = {r: v for r, v in self._side_rooms.items() if side_room_live(v.at, now)}
        results: dict[str, int] = {}  # tool-call id -> the last index of a message returning its result
        for i, item in enumerate(messages):
            for call_id in dialect.result_ids(item):
                results[call_id] = i
        by_id: list[str] = []
        by_lineage: list[tuple[int, str]] = []
        for room_id, entry in self._side_rooms.items():
            n = entry.n
            if (entry.owner != key or n >= len(messages) or anchor_hash(hashes[:n]) != entry.anchor
                    or any(dialect.is_turn_start(m) for m in messages[n:])):
                continue
            if entry.ids:
                if any(results.get(call_id, -1) >= n for call_id in entry.ids):
                    by_id.append(room_id)
            else:
                by_lineage.append((n, room_id))
        continued = None
        if by_id:
            continued = by_id[0] if len(by_id) == 1 else None
        elif by_lineage:
            longest = max(n for n, _ in by_lineage)
            tied = [room_id for n, room_id in by_lineage if n == longest]
            continued = tied[0] if len(tied) == 1 else None
        if room is None:
            if continued is not None:
                calls.set_side_room(continued, None)
                age = int(now - self._side_rooms.pop(continued).at)
                events.append({"event": "side_room_closed", "reason": "no_tool_call", "age_s": age})
            return
        room_id = continued or f"{key}#{secrets.token_hex(4)}"
        calls.set_side_room(room_id, room)
        prompt = first_prompt(messages) if dialect.name == "anthropic" else None
        self._side_rooms[room_id] = _SideRoom(key, len(messages), anchor_hash(hashes), now, frozenset(call_ids),
                                              prompt, self._main_len)

    def observe_usage(self, endpoint: str, usage: dict, request: dict, tool_calls: bool | None = None,
                      tool_call_ids: tuple[str, ...] = ()) -> list[dict]:
        dialect = dialect_for(endpoint)
        # preamble() must see the same knobs as the request did
        with recall_knobs(self.config), request_scope(request.get(dialect.field) if dialect else None):
            return self._observe_usage(endpoint, usage, request, tool_calls, tool_call_ids)

    def _observe_usage(self, endpoint: str, usage: dict, request: dict, tool_calls: bool | None = None,
                       tool_call_ids: tuple[str, ...] = ()) -> list[dict]:
        """Feed a response's real usage back (a transformed endpoint; never ``count_tokens``). Returns the
        content-free events it raised (packet A2b; contract rule 3 as amended by PR #44). The proxy writes
        them on that request's log entry (``usage_events``), the one place they are logged (A2e,
        agreed with builder-1); this method does not log them itself:

        - ``over_bound`` with ``source: "real"``: the real upstream input (input + cache read + cache
          write) exceeded the bound, whatever the pre-send estimate said, so acceptance 1 can rely on
          the session log (the pre-send flag stays on the request event);
        - ``estimate_error``: the real input exceeded the pre-send estimate by more than 10%, with the
          running mean of estimate / real for the conversation;
        - ``usage_unattributed``: the usage is not for the newest request the engine sent in this
          conversation (a request it never transformed, after ``transform_error``, or an older request
          still in flight), so it is not paired with that request's bytes or estimate: the ratio, the
          last real input, and the estimate checks are left alone. ``over_bound`` and the room still
          use it, since they describe what the model received.

        ``tool_calls`` (D40) says whether the response holds a tool call the client runs: True or
        False from the proxy's stream hold when there is one, else from the response's stop reason
        or output list, None when unknown (treated as True). The main conversation publishes its room
        either way; any other conversation publishes the side room of its request lineage
        (``_publish_side_room``) when the response may hold a client call and closes it when not.
        ``tool_call_ids`` are the ids of those client calls, recorded with the room so that only a
        request returning their results continues it.
        """
        dialect = dialect_for(endpoint)
        if dialect is None or endpoint == COUNT_TOKENS:
            return []
        key = keyed(dialect, request)
        state = self.states.get(key)
        if state is None:
            return []
        token = _DIALECT.set(dialect)
        try:
            identity = usage_identity(request.get(dialect.field) or [])
        finally:
            _DIALECT.reset(token)
        real = usage_tokens(usage, dialect)
        events: list[dict] = []
        estimate = None
        if identity != state.pending_usage:
            pending = state.pending_usage
            events.append({"event": "usage_unattributed", "epoch": state.epoch, "messages": identity[0],
                           "expected_messages": pending[0] if pending else None})
        else:
            self.estimator.observe(state.last_sent_bytes, real)  # the session's measured ratio from now on
            state = replace(state, last_input_tokens=real, pending_usage=None)
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
                messages = request.get(dialect.field) or []
                if self.publishes(key, messages, dialect):
                    calls.set_turn_room(turn_id(messages, dialect), self.turn_room(real + output))
                    state = publish_window(calls, state, self.estimator, window_model_name(request))
                elif callable(getattr(self.store, "set_side_room", None)):
                    # D40: this conversation's own room, from its own usage, for the tool calls this
                    # response releases; every call is charged against it and main's room alike.
                    room = None if tool_calls is False else self.turn_room(real + output)
                    self._publish_side_room(calls, key, messages, dialect, room, tuple(tool_call_ids), events)
        except STORE_FAILURES as error:
            # Rule 14: a store that cannot be written after a response is a store_error like any other;
            # the next turn start retries it. The event reaches the log with the others, via the proxy.
            state = enter_degraded(state, store_failure(error, calls, state), events)
        self.states[key] = state
        return events
