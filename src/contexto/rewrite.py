"""Rewrite engine: rules 1 to 12 of docs/milestones/M2-contexto-proxy.md (rule text as aligned in
PR #25; placement and system-message evidence in spikes/m2/carried/).

The proxy calls one ``RewriteEngine`` per session: ``engine(request, headers, endpoint)`` returns the
upstream request, the headers (unchanged), and content-free events; ``engine.observe_usage(endpoint,
usage, request)`` feeds real usage back after each ``/v1/messages`` response. The work is done by
the pure function ``rewrite(request, state, store, config, estimator)``, which never mutates its
input, returns ``system`` and ``tools`` as the same objects, and is deterministic, so the proxy's
deterministic serializer sends byte-identical prefixes within an epoch. The store is touched only on
rotation (``page_out``, ``pinned_text``, ``index_text``); the estimator only through
``estimate``, ``estimate_messages`` and ``observe``.

Terms and readings agreed with manager-1 (2026-10-16), each covered by a test:

- A turn-start message is a user message with user-authored content (a text block that is not an
  injected context block, a plain string, an image, or a document) and no ``tool_result``. An
  injected context block is a text block that starts with ``<system-reminder>``.
- A turn is a turn-start message plus every following assistant, ``tool_result`` user, and
  ``role: "system"`` message up to the next turn start; a trailing system message after the new
  prompt belongs to the new turn.
- Carried content is never paged: the context blocks of paged turn-start messages and every system
  message of a paged turn, both in the form the CLI sends now (rule 12: settings-only system messages
  with empty content are carried like any other). Settings-only system messages accrue per settings
  change (measured), so only the latest per settings key is carried; the carried set is computed from
  the paged region, so it changes only at a rotation. Rule 13 (PR #27): carried content is never
  paged; when a rotation leaves it above ``carried_budget`` (4,000 tokens) a content-free
  ``carried_over_budget`` event reports its estimate and system-message count. Placement (rule 1, measured): carried context blocks
  and then the preamble go ahead of the first retained turn-start message's own content; carried
  system messages go immediately after that message (a system message before it returns 400).
- Rotation (rules 3 and 4) happens only when the newest turn has no assistant message yet and the
  request adds messages (a resent request never rotates). It picks the smallest ``k`` at a turn start
  whose retained turns fit ``window_target``, never below the current ``k``, always keeping the newest
  complete turn and the new turn.
- Thinking (rule 2): retained messages that existed when the epoch was created lose their thinking and
  redacted-thinking blocks on every request; an assistant message left empty gets one text block
  ``[thinking omitted]``; other blocks keep their order.
- Headroom is the running maximum of per-turn growth times 1.25, floor 25,000 tokens; per-turn growth
  is the estimator's token count of the CLI history added between consecutive turn starts.
- History matching (rule 10) hashes canonical JSON with every ``cache_control`` removed and a single
  text block normalised to a plain string. A mismatch inside the retained window resynchronises from
  the longest matching prefix; a mismatch in paged turns starts a new epoch at the current turn start
  with everything before it paged. In that forced epoch an in-flight tool loop keeps its thinking
  (only messages before the turn start count as existing), and the full history is never sent.
- ``count_tokens`` (rule 11) gets the current epoch's transform with no side effects.
"""

from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from contexto.canonical import canonical_bytes, canonical_json
from contexto.tokens import TokenEstimator

MESSAGES = "/v1/messages"
COUNT_TOKENS = "/v1/messages/count_tokens"
THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})
THINKING_OMITTED = "[thinking omitted]"
CONTEXT_TAG = "<system-reminder>"


class Store(Protocol):
    def page_out(self, turns: list[list[dict]], first_turn: int) -> list[str]: ...
    def pinned_text(self, budget_tokens: int) -> str: ...
    def index_text(self, budget_tokens: int) -> str: ...


@dataclass(frozen=True)
class Config:
    bound: int = 150_000
    window_target: int | None = None  # default: half the bound
    headroom_floor: int = 25_000
    headroom_factor: float = 1.25
    pin_budget: int = 6_000
    index_budget: int = 4_000
    carried_budget: int = 4_000  # rule 13: carried content above this is reported, never paged

    @property
    def target(self) -> int:
        return self.window_target if self.window_target is not None else self.bound // 2


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
    last_turn_start_tokens: int | None = None


# Message classification -------------------------------------------------------------------------

def _blocks(message: dict) -> list:
    content = message.get("content")
    return content if isinstance(content, list) else []


def is_context_block(block: Any) -> bool:
    return (isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str) and block["text"].lstrip().startswith(CONTEXT_TAG))


def is_turn_start(message: dict) -> bool:
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
    return all(m.get("role") != "assistant" for m in messages[starts[-1]:])


# Normalisation and hashing (rule 10) ------------------------------------------------------------

def _drop_cache_control(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_cache_control(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_drop_cache_control(v) for v in obj]
    return obj


def normalise(message: dict) -> dict:
    m = _drop_cache_control(message)
    content = m.get("content")
    if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and set(content[0]) == {"type", "text"} and content[0]["type"] == "text"):
        m = {**m, "content": content[0]["text"]}
    return m


def message_hash(message: dict) -> str:
    return hashlib.sha256(canonical_bytes(normalise(message))).hexdigest()


def head_fingerprint(request: dict) -> str:
    return hashlib.sha256(canonical_bytes({"system": request.get("system"), "tools": request.get("tools")})).hexdigest()


def conversation_key(request: dict) -> str:
    """Per-conversation key: the normalised first message, so subagent and other side requests keep
    their own state and a head change does not split the main conversation."""
    messages = request.get("messages") or []
    return message_hash(messages[0]) if messages else ""


# Transform pieces ------------------------------------------------------------------------------

def strip_thinking(message: dict) -> dict:
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


def carried_content(messages: list[dict], k: int) -> tuple[list[dict], list[dict]]:
    """(context blocks of paged turn starts, system messages of paged turns), in original order and
    in the form the CLI sends now. Settings-only system messages are deduplicated: only the latest
    per settings key is carried (they accrue per settings change, spikes/m2/carried/README.md)."""
    blocks, systems = [], []
    for m in messages[:k]:
        if m.get("role") == "system":
            systems.append(m)
        elif is_turn_start(m):
            blocks.extend(b for b in _blocks(m) if is_context_block(b))
    latest = {}
    for i, m in enumerate(systems):
        key = settings_key(m)
        if key is not None:
            latest[key] = i
    systems = [m for i, m in enumerate(systems) if settings_key(m) is None or latest[settings_key(m)] == i]
    return blocks, systems


def page_turn(turn: list[dict]) -> list[dict]:
    """A turn as stored: thinking kept as received, carried content removed."""
    out = []
    for m in turn:
        if m.get("role") == "system":
            continue
        if is_turn_start(m) and any(is_context_block(b) for b in _blocks(m)):
            m = {**m, "content": [b for b in _blocks(m) if not is_context_block(b)]}
        out.append(m)
    return out


def split_turns(messages: list[dict], start: int, end: int) -> list[list[dict]]:
    turns: list[list[dict]] = []
    for i in range(start, end):
        if not turns or (i != start and is_turn_start(messages[i])):
            turns.append([])
        turns[-1].append(messages[i])
    return turns


def preamble(state: EpochState) -> str:
    return "\n\n".join(part for part in (state.pinned_text, state.index_text) if part)


def transform_messages(messages: list[dict], state: EpochState) -> list[dict]:
    out = []
    for i in range(state.k, len(messages)):
        m = messages[i]
        if i < state.strip_upto:
            m = strip_thinking(m)
        out.append(m)
    if state.k == 0 or not out:
        return out
    blocks, systems = carried_content(messages, state.k)
    head = [*blocks]
    text = preamble(state)
    if text:
        head.append({"type": "text", "text": text})
    first = out[0]
    own = first.get("content")
    own_blocks = [{"type": "text", "text": own}] if isinstance(own, str) else list(own)
    return [{**first, "content": head + own_blocks}, *systems, *out[1:]]


def headroom(state: EpochState, config: Config) -> int:
    return max(config.headroom_floor, math.ceil(state.max_turn_growth * config.headroom_factor))


def estimate_upstream(request: dict, messages: list[dict], previous_n: int, state: EpochState,
                      estimator: TokenEstimator) -> int:
    """Rule 3: last real input plus the messages added since, else an estimate of the whole request."""
    if state.last_input_tokens is not None and previous_n <= len(messages):
        return state.last_input_tokens + estimator.estimate_messages(messages[previous_n:])
    head = estimator.estimate(canonical_json({"system": request.get("system"), "tools": request.get("tools")}))
    return head + estimator.estimate_messages(transform_messages(messages, state))


def choose_k(messages: list[dict], state: EpochState, config: Config, estimator: TokenEstimator) -> int:
    starts = [i for i in turn_starts(messages) if i >= state.k]
    if len(starts) < 2:
        return state.k
    newest_complete = starts[-2]
    for k in starts:
        if k > newest_complete:
            break
        window = [strip_thinking(m) for m in messages[k:] if m.get("role") != "system"]
        if estimator.estimate_messages(window) <= config.target:
            return k
    return newest_complete


def new_epoch(messages: list[dict], request: dict, state: EpochState, store: Store, config: Config,
              estimator: TokenEstimator, k: int, strip_upto: int, events: list[dict], reason: str) -> EpochState:
    if k > state.k:
        turns = [page_turn(t) for t in split_turns(messages, state.k, k)]
        ids = store.page_out(turns, state.turns_paged)
        events.append({"event": "page_out", "epoch": state.epoch + 1, "turns": len(turns),
                       "pages": len(ids), "first_turn": state.turns_paged})
        turns_paged = state.turns_paged + len(turns)
    else:
        turns_paged = state.turns_paged
    new = replace(state, epoch=state.epoch + 1, k=k, pinned_text=store.pinned_text(config.pin_budget),
                  index_text=store.index_text(config.index_budget), head_fingerprint=head_fingerprint(request),
                  strip_upto=strip_upto, turns_paged=turns_paged, head_changed=False, last_input_tokens=None)
    events.append({"event": "rotation", "epoch": new.epoch, "reason": reason, "k": k,
                   "retained_messages": len(messages) - k, "turns_paged": turns_paged})
    blocks, systems = carried_content(messages, k)
    carried = estimator.estimate_messages([{"role": "user", "content": blocks}, *systems]) if (blocks or systems) else 0
    if carried > config.carried_budget:
        events.append({"event": "carried_over_budget", "epoch": new.epoch, "carried_tokens": carried,
                       "system_messages": len(systems), "budget": config.carried_budget})
    return new


def matching_prefix(hashes: tuple[str, ...], messages: list[dict]) -> int:
    n = 0
    for old, m in zip(hashes, messages):
        if old != message_hash(m):
            break
        n += 1
    return n


# The engine --------------------------------------------------------------------------------------

def rewrite(request: dict, state: EpochState | None, store: Store, config: Config,
            estimator: TokenEstimator) -> tuple[dict, EpochState, list[dict]]:
    """Return (upstream_request, new_state, events) for one ``/v1/messages`` request."""
    messages = request.get("messages") or []
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
            events.append({"event": "resync", "epoch": state.epoch, "kind": "paged_changed",
                           "matched": match, "previous_messages": previous_n, "messages": n})
            fresh = replace(state, k=0, turns_paged=state.turns_paged)
            state = new_epoch(messages, request, fresh, store, config, estimator, current, current, events,
                              "paged_changed")
        else:
            events.append({"event": "resync", "epoch": state.epoch, "kind": "window_changed",
                           "matched": match, "previous_messages": previous_n, "messages": n})
            state = replace(state, last_input_tokens=None)
        previous_n = match

    # Rule 6: a head change waits for the next turn start.
    if fingerprint != state.head_fingerprint and not state.head_changed:
        state = replace(state, head_changed=True)
        events.append({"event": "head_changed", "epoch": state.epoch})

    estimate = estimate_upstream(request, messages, previous_n, state, estimator)
    if n > previous_n and at_turn_start(messages):
        history = estimator.estimate_messages(messages)
        if state.last_turn_start_tokens is not None:
            growth = max(0, history - state.last_turn_start_tokens)
            state = replace(state, max_turn_growth=max(state.max_turn_growth, growth))
        state = replace(state, last_turn_start_tokens=history)
        if state.head_changed:
            state = new_epoch(messages, request, state, store, config, estimator,
                              choose_k(messages, state, config, estimator), n, events, "head_change")
        elif estimate > config.bound - headroom(state, config):
            state = new_epoch(messages, request, state, store, config, estimator,
                              choose_k(messages, state, config, estimator), n, events, "bound")
    over = estimate > config.bound and not any(e["event"] == "rotation" for e in events)

    upstream = {**request, "messages": transform_messages(messages, state)}
    sent = len(canonical_bytes(upstream))
    state = replace(state, hashes=tuple(message_hash(m) for m in messages), last_sent_bytes=sent)
    event = {"event": "request", "epoch": state.epoch, "k": state.k, "messages": n,
             "upstream_messages": len(upstream["messages"]), "estimate": estimate, "sent_bytes": sent}
    if over:
        event["over_bound"] = True
    events.append(event)
    return upstream, state, events


def count_tokens_view(request: dict, state: EpochState | None) -> dict:
    """Rule 11: the current epoch's transform for ``count_tokens``; no rotation and no side effects."""
    if state is None:
        return request
    messages = request.get("messages") or []
    if state.k and (state.k >= len(messages) or matching_prefix(state.hashes, messages) < state.k):
        return request  # history no longer matches the epoch; count what the CLI sent
    return {**request, "messages": transform_messages(messages, state)}


def usage_tokens(usage: dict) -> int:
    return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


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

    def __call__(self, request: dict, headers: dict[str, str], endpoint: str) -> tuple[dict, dict[str, str], list[dict]]:
        key = conversation_key(request)
        state = self.states.get(key)
        if endpoint == COUNT_TOKENS:
            epoch = state.epoch if state else 0
            return count_tokens_view(request, state), headers, [{"event": "count_tokens", "epoch": epoch}]
        if endpoint != MESSAGES:
            return request, headers, []
        upstream, self.states[key], events = rewrite(request, state, self.store, self.config, self.estimator)
        return upstream, headers, events

    def observe_usage(self, endpoint: str, usage: dict, request: dict) -> None:
        if endpoint != MESSAGES:
            return
        key = conversation_key(request)
        state = self.states.get(key)
        if state is None:
            return
        real = usage_tokens(usage)
        self.estimator.observe(state.last_sent_bytes, real)
        self.states[key] = replace(state, last_input_tokens=real)
