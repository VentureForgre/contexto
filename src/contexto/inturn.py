"""In-turn paging (D63; rule 5 of docs/milestones/M2-contexto-proxy.md): keep a request under the bound
inside a running turn, where the turn-start rotation cannot run.

A tool loop adds a tool round per request and has no turn start, so the engine used to send it whole
(D17): one prompt and 120 tool rounds reached 775,851 tokens at a 150,000 bound, and a subagent, which
has one turn start, never paged at all. Here the engine edits the request inside the turn, in batches
that are large and rare, because each one costs the loop its thinking and a cache rewrite.

**The thinking rule.** On 5.x models the API checks that nothing before a kept thinking block has
changed. So an edit whose first changed message is at index ``p`` strips every thinking and
redacted-thinking block in messages ``[p, n)``, ``n`` being the request's message count at that moment,
on that request and on every later one (``InTurn.strips``), exactly as rule 2 strips at a rotation.
Thinking before ``p`` and in messages added afterwards passes through byte for byte. No request carries
a thinking block with anything changed before it.

**A batch** (``page``) runs in a tool loop when the pre-send estimate passes the trigger ``T = bound -
headroom_floor - min_turn_room``, the point where the recall room the request publishes would fall under
``min_turn_room``. It pages down to a target between what cannot be paged and ``T``
(``window_fraction``), in this order, stopping as soon as the target is met:

0. An unseen result (one after the last assistant message) that alone cannot fit under ``T`` with
   everything else paged is replaced by a stub. It sits after the last thinking block, so nothing is
   stripped and the cached prefix stays.
1. Older tool results are replaced by stubs, oldest first, across the retained window. The originals
   go to result pages; the results of Contexto's own search, recall and state calls are copies of
   pages, so they get a stub and no page.
2. If the estimate is still over the escalation line: the whole earlier turns of the window are paged
   through the rotation machinery (``new_epoch`` at the current turn's start). While the head is still
   unmeasured at ``k == 0`` (rule 4 measures it only there) this waits until step 3 has left the request
   over the bound itself.
3. If still over it: the oldest complete tool rounds of the running turn are folded into one page and
   removed from the request, with a note in the turn-start message.
4. If the request is still over the bound itself: unseen results are stubbed, largest first.
5. Still over the bound: ``over_bound`` as before, and ``in_turn_exhausted``.

The escalation line is the higher of the target and ``T - min_turn_room``: a batch that ends just under
``T`` would be followed by another at the next request, so a step that leaves less than
``min_turn_room`` of growth before the trigger hands over to the next step. A request over ``T`` starts
no batch when the whole ladder could free less than ``min_turn_room`` and the request is under the
bound.

**State.** Everything the engine needs to send the same bytes again is in ``EpochState.inturn``: which
result blocks are stubs and their page ids, which messages are folded and into which page, and the
strip ranges. ``view`` applies it to the CLI's messages; the result has the same length and the same
indexes, with a folded message replaced by ``FOLDED``. A stub and a fold note are pure functions of the
CLI's own blocks and the page id, so they are byte-identical on every request. The epoch record carries
the state (rule 16), written when a batch runs.

**Dialects.** The protocol-specific parts are behind ``Dialect.in_turn`` (``MessagesInTurn`` here). A
dialect whose ``in_turn`` is None, as the Responses dialect's is in this release, never pages inside a
turn.
"""

from __future__ import annotations

import bisect
import math
import sys
import threading
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, NamedTuple

from contexto.canonical import canonical_bytes, canonical_json, utf8
from contexto.store.render import OWN_TOOL_PREFIX, own_call_ids

STUB_MIN_BYTES = 2_048  # a result is stubbed only when its content is at least this many bytes
STUB_MIN_RATIO = 2  # and at least this many times its stub
LOCATOR_CHARS = 200  # characters of a path, command or input a stub shows
NOTE_NAMES = 12  # tools and files a fold note lists
LOCATOR_KEYS = ("file_path", "path", "notebook_path", "command", "pattern", "url", "query")
PATH_KEYS = ("file_path", "path", "notebook_path")
PLACEHOLDER_PAGE = "pg_" + "0" * 16  # sizes a stub before its page is written; ids have one length
RESULT_PAGE = "result"  # page kinds; a page without one is a whole turn
ROUNDS_PAGE = "rounds"
ROTATION_REASON = "in_turn"
# What stands in a view for a message folded into a rounds page: no content, so it estimates as 0 and
# has no searchable block; ``transform_messages`` and ``strip_carried`` leave it out by identity.
FOLDED: dict[str, Any] = {"role": "assistant", "content": []}
_NOTE_CACHE_SIZE = 256


class InTurn(NamedTuple):
    """The in-turn edits of an epoch. Indexes are positions in the CLI's history, which is append-only
    (rule 10), so they stay valid until a resync or a rotation that pages them."""

    # (message index, block index, page id): result blocks sent as stubs; "" for one of Contexto's own
    # calls, which has no page. Sorted.
    stubs: tuple[tuple[int, int, str], ...] = ()
    # (turn-start index, ((first, end), ...) of the removed messages, page id): folded rounds, in the
    # order they were folded.
    folds: tuple[tuple[int, tuple[tuple[int, int], ...], str], ...] = ()
    strips: tuple[tuple[int, int], ...] = ()  # sorted, disjoint [first, end): thinking stripped there
    batches: int = 0  # batches run in this conversation; part of the ratio's scope (D48)


EMPTY = InTurn()

# D63: the turn number a side conversation's in-turn pages carry: the main conversation's current turn,
# so search's recency prior ranks a subagent's pages as recent. None: the conversation's own numbering.
TURN: ContextVar[int | None] = ContextVar("contexto_inturn_turn", default=None)


def _rw() -> Any:
    """``contexto.rewrite``, which imports this module: looked up when called, not at import."""
    return sys.modules["contexto.rewrite"]


def _blocks(message: dict) -> list:
    content = message.get("content")
    return content if isinstance(content, list) else []


# The Messages API's primitives ------------------------------------------------------------------

class Size(NamedTuple):
    bytes: int  # UTF-8 bytes of the result's text
    lines: int
    images: int


class Round(NamedTuple):
    """One tool round of a turn: an assistant message and what follows up to the next one."""

    removed: tuple[int, ...]  # the indexes a fold takes out; a carried system message stays in place
    foldable: bool  # False when a message of the round holds carried content, or is already folded


class MessagesInTurn:
    """What in-turn paging needs to know about the Messages API. Another dialect implements the same
    methods on its own items and sets ``Dialect.in_turn``; the batch logic above it is shared."""

    def results(self, item: dict) -> list[tuple[int, str]]:
        """(block index, call id) of each tool result the item returns."""
        if not isinstance(item, dict) or item.get("role") != "user":
            return []
        return [(j, b["tool_use_id"]) for j, b in enumerate(_blocks(item))
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)]

    def result(self, item: dict, j: int) -> dict | None:
        blocks = _blocks(item)
        block = blocks[j] if 0 <= j < len(blocks) else None
        return block if isinstance(block, dict) and block.get("type") == "tool_result" else None

    def call(self, items: list[dict], i: int, call_id: str) -> dict | None:
        """The call a result at ``items[i]`` answers: the ``tool_use`` block with that id in the nearest
        assistant message before it."""
        for x in range(i - 1, -1, -1):
            item = items[x]
            if isinstance(item, dict) and item.get("role") == "assistant":
                return next((b for b in _blocks(item) if isinstance(b, dict) and b.get("type") == "tool_use"
                             and b.get("id") == call_id), None)
        return None

    def stubbable(self, result: dict) -> bool:
        """A result whose content a text stub can stand for: a string, or text and image blocks only. Any
        other block (a tool reference from tool search, a document) may be what the CLI or the API needs."""
        content = result.get("content")
        if isinstance(content, str):
            return True
        return isinstance(content, list) and all(
            isinstance(b, dict) and (b.get("type") == "image" or (b.get("type") == "text"
                                                                  and isinstance(b.get("text"), str)))
            for b in content)

    def size(self, result: dict) -> Size:
        content = result.get("content")
        if isinstance(content, str):
            texts, images = [content], 0
        else:
            parts = content if isinstance(content, list) else []
            texts = [b["text"] for b in parts if isinstance(b, dict) and b.get("type") == "text"
                     and isinstance(b.get("text"), str)]
            images = sum(1 for b in parts if isinstance(b, dict) and b.get("type") == "image")
        text = "\n".join(texts)
        return Size(len(utf8(text)), text.count("\n") + 1 if text else 0, images)

    def content_bytes(self, result: dict) -> int:
        return len(canonical_bytes(result.get("content")))

    def with_stubs(self, item: dict, texts: dict[int, str]) -> dict:
        """The item with the content of each result block in ``texts`` replaced by that one string; the
        block's other keys (``tool_use_id``, ``is_error``, ``cache_control``) and every other block stay."""
        return {**item, "content": [{**b, "content": texts[j]} if j in texts else b
                                    for j, b in enumerate(_blocks(item))]}

    def result_page(self, call: dict | None, result: dict) -> list[dict]:
        """A result page's messages: the call and the result verbatim, as an assistant message and a
        user message, so rendering, search and ``own_call_ids`` read it like any turn."""
        page = [{"role": "assistant", "content": [call]}] if call is not None else []
        return [*page, {"role": "user", "content": [result]}]

    def describe(self, call: dict | None) -> tuple[str, str, str]:
        """(tool name, locator line, arguments) of a call, for a stub."""
        name = call.get("name") if call is not None else None
        name = name if isinstance(name, str) and name else "unknown"
        tool_input = call.get("input") if call is not None else None
        arguments = canonical_json(tool_input) if isinstance(tool_input, (dict, list, str)) else "{}"
        locator = f"input: {_clip(arguments)}"
        if isinstance(tool_input, dict):
            for key in LOCATOR_KEYS:
                if isinstance(tool_input.get(key), str) and tool_input[key]:
                    locator = f"{key}: {_clip(tool_input[key])}"
                    break
        return name, locator, _clip(arguments)

    def with_note(self, item: dict, note: str) -> dict:
        """The turn-start message with a fold note after its own content."""
        content = item.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        return {**item, "content": [*blocks, {"type": "text", "text": note}]}

    def rounds(self, items: list[dict], start: int, end: int) -> list[Round]:
        """The tool rounds of ``items[start:end]`` in order, the newest included. A fold removes a
        round's assistant message, its tool-result message, any other user message and hook output; a
        carried system message stays where it is. A round that holds carried content next to its results
        (nested memory, D46) is not foldable: its rules would leave the request."""
        rw = _rw()
        rounds: list[Round] = []
        current: list[int] | None = None
        foldable = True
        for i in range(start, end):
            item = items[i]
            if item is FOLDED:
                continue
            if rw.is_assistant(item):
                if current is not None:
                    rounds.append(Round(tuple(current), foldable))
                current, foldable = [i], True
                continue
            if current is None:
                continue  # before the first assistant message: the turn start and what rides on it
            if rw.is_system_like(item):
                if rw.classify_system(items, i) == rw.HOOK_OUTPUT:
                    current.append(i)
                continue
            if rw._is_tool_round(item) and rw._carried_at(item, False):
                foldable = False
            current.append(i)
        if current is not None:
            rounds.append(Round(tuple(current), foldable))
        return rounds

    def note_facts(self, items: list[dict]) -> tuple[int, list[str], list[str]]:
        """(rounds, tool names, file paths) of folded messages, in order of first use."""
        rounds, tools, paths = 0, [], []
        for item in items:
            if item.get("role") != "assistant":
                continue
            rounds += 1
            for b in _blocks(item):
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                if isinstance(b.get("name"), str) and b["name"]:
                    tools.append(b["name"])
                tool_input = b.get("input")
                for key in PATH_KEYS:
                    value = tool_input.get(key) if isinstance(tool_input, dict) else None
                    if isinstance(value, str) and value:
                        paths.append(value)
        return rounds, tools, paths


MESSAGES = MessagesInTurn()


# Stubs and notes: pure functions of the CLI's blocks and the page id -----------------------------

def _clip(text: str, limit: int = LOCATOR_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " ..."


def _was(size: Size) -> str:
    was = f"was: {size.bytes:,} bytes, {size.lines:,} lines"
    if size.images:
        was += (f", {size.images} image{'s' if size.images != 1 else ''} (recall shows text only; run the call "
                "again to see an image)")
    return was


def _short(name: str) -> str:
    return name.removeprefix(OWN_TOOL_PREFIX)


def stub_text(hooks: Any, call: dict | None, result: dict, page_id: str) -> str:
    """What a stubbed result's content becomes: one string saying what was there and how to get it back.
    With no page id it is one of Contexto's own search, recall or state results, a copy of stored pages:
    the stub gives the call's arguments so the model can repeat it."""
    name, locator, arguments = hooks.describe(call)
    size = hooks.size(result)
    if not page_id:
        return ("[Contexto dropped this result of its own tool to keep the request under its bound. It was a "
                "copy of stored pages.\n"
                f"tool: {_short(name)}\n"
                f"arguments: {arguments}\n"
                f"{_was(size)}\n"
                "To get it back, call the tool again with the same arguments.]")
    return ("[Contexto paged out this tool result to keep the request under its bound. Nothing was lost.\n"
            f"tool: {name}\n"
            f"{locator}\n"
            f"{_was(size)}\n"
            f"page: {page_id}\n"
            f'To read it, call contexto_recall with page_id "{page_id}" (pass the next offset to continue a long '
            "page). contexto_search finds lines in it. You can also run the tool call again.]")


def _counted(names: list[str]) -> str:
    counts: OrderedDict[str, int] = OrderedDict()
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    shown = [f"{name} x{count}" for name, count in list(counts.items())[:NOTE_NAMES]]
    if len(counts) > NOTE_NAMES:
        shown.append(f"and {len(counts) - NOTE_NAMES} more")
    return ", ".join(shown) or "none"


def _listed(names: list[str]) -> str:
    unique = list(OrderedDict.fromkeys(names))
    shown = [_clip(name) for name in unique[:NOTE_NAMES]]
    if len(unique) > NOTE_NAMES:
        shown.append(f"and {len(unique) - NOTE_NAMES} more")
    return ", ".join(shown) or "none"


_NOTES: OrderedDict[tuple[str, int], str] = OrderedDict()
_NOTES_LOCK = threading.Lock()  # engines of several sessions can share a process


def fold_note(hooks: Any, removed: list[dict], page_id: str) -> str:
    """The note a fold leaves in the turn-start message: how many rounds, which tools and files, their
    size, and the page. ``removed`` is what the page holds (a stubbed result stays a stub there). Kept
    per page id, which names its content, so the messages are measured once per process."""
    key = (page_id, len(removed))
    with _NOTES_LOCK:
        note = _NOTES.get(key)
        if note is not None:
            _NOTES.move_to_end(key)
            return note
    rounds, tools, paths = hooks.note_facts(removed)
    size = sum(len(canonical_bytes(m)) for m in removed)
    note = (f"[Contexto folded {rounds:,} earlier tool round{'s' if rounds != 1 else ''} of this turn into page "
            f"{page_id} to keep the request under its bound. Nothing was lost.\n"
            f"tools: {_counted(tools)}\n"
            f"files: {_listed(paths)}\n"
            f"was: {size:,} bytes\n"
            f'To read them, call contexto_recall with page_id "{page_id}" (pass the next offset to continue; the '
            "page is long). contexto_search finds lines in it.]")
    with _NOTES_LOCK:
        _NOTES[key] = note
        while len(_NOTES) > _NOTE_CACHE_SIZE:
            _NOTES.popitem(last=False)
    return note


# The view: the CLI's messages as the epoch sends them ---------------------------------------------

def view(messages: list[dict], state: Any, dialect: Any) -> list[dict]:
    """``messages`` with the epoch's stubs and folds applied: the same length and indexes, a stubbed
    result's content replaced, a folded message replaced by ``FOLDED``, and each fold's note in its
    turn-start message. The list itself when the epoch has none. Thinking is not stripped here
    (``strips_thinking``). Inside a request scope the view of the request's own list is built once per
    state."""
    edits: InTurn = state.inturn
    hooks = dialect.in_turn
    if hooks is None or not (edits.stubs or edits.folds):
        return messages
    memo = _rw()._MEMO.get()
    if memo is not None and messages is memo.root and memo.sent is not None:
        kept_edits, kept_dialect, kept = memo.sent
        if kept_dialect is dialect and (kept_edits is edits or kept_edits == edits):
            return kept
    out = list(messages)
    n = len(out)
    by_message: dict[int, list[tuple[int, str]]] = {}
    for i, j, page_id in edits.stubs:
        if i < n:
            by_message.setdefault(i, []).append((j, page_id))
    for i, entries in by_message.items():
        item = messages[i]
        texts = {}
        for j, page_id in entries:
            result = hooks.result(item, j)
            if result is not None:
                texts[j] = stub_text(hooks, hooks.call(messages, i, result["tool_use_id"]), result, page_id)
        if texts:
            out[i] = hooks.with_stubs(item, texts)
    for start, ranges, page_id in edits.folds:
        removed = [i for a, b in ranges for i in range(a, min(b, n))]
        if start >= n or not removed:
            continue
        out[start] = hooks.with_note(out[start], fold_note(hooks, [out[i] for i in removed], page_id))
        for i in removed:
            out[i] = FOLDED
    if memo is not None and messages is memo.root:
        memo.sent = (edits, dialect, out)
    return out


def strips_thinking(edits: InTurn, i: int) -> bool:
    """Whether message ``i`` lost its thinking to an in-turn edit (the thinking rule)."""
    return any(a <= i < b for a, b in edits.strips)


def extent(edits: InTurn) -> int:
    """One past the last message index the edits name."""
    ends = [i + 1 for i, _, _ in edits.stubs]
    ends += [b for _, ranges, _ in edits.folds for _, b in ranges]
    ends += [b for _, b in edits.strips]
    return max(ends, default=0)


def _merged(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(r for r in ranges if r[0] < r[1]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return tuple(out)


def _spans(indexes: list[int]) -> tuple[tuple[int, int], ...]:
    return _merged([(i, i + 1) for i in indexes])


def after_epoch(edits: InTurn, k: int, strip_upto: int) -> InTurn:
    """The edits a new epoch keeps: those of the turns it retains. Messages before ``k`` are paged (in
    their stubbed form), and rule 2 strips everything before ``strip_upto``."""
    if edits == EMPTY:
        return edits
    return edits._replace(
        stubs=tuple(s for s in edits.stubs if s[0] >= k),
        folds=tuple(f for f in edits.folds if f[0] >= k),
        strips=_merged([(max(a, strip_upto), b) for a, b in edits.strips]))


def clipped(edits: InTurn, matched: int) -> InTurn:
    """Rule 10's resync inside the window: the CLI changed its history from ``matched`` on, so an edit
    that names a message there no longer has one. Edits before it stand."""
    if edits == EMPTY or extent(edits) <= matched:
        return edits
    folds = []
    for start, ranges, page_id in edits.folds:
        kept = tuple((a, min(b, matched)) for a, b in ranges if a < matched)
        if start < matched and kept:
            folds.append((start, kept, page_id))
    return edits._replace(stubs=tuple(s for s in edits.stubs if s[0] < matched), folds=tuple(folds),
                          strips=_merged([(a, min(b, matched)) for a, b in edits.strips]))


# The epoch record (rule 16) -----------------------------------------------------------------------

def to_record(edits: InTurn) -> dict[str, Any]:
    return {"stubs": [list(s) for s in edits.stubs],
            "folds": [[start, [list(r) for r in ranges], page_id] for start, ranges, page_id in edits.folds],
            "strips": [list(r) for r in edits.strips], "batches": edits.batches}


def from_record(record: Any, n: int) -> InTurn:
    """The edits a record holds, for a request whose first ``n`` messages are the record's. Raises
    ValueError for a record the engine cannot use (the store checked its shape)."""
    if record is None:
        return EMPTY
    try:
        edits = InTurn(
            stubs=tuple((int(i), int(j), str(page_id)) for i, j, page_id in record["stubs"]),
            folds=tuple((int(start), tuple((int(a), int(b)) for a, b in ranges), str(page_id))
                        for start, ranges, page_id in record["folds"]),
            strips=tuple((int(a), int(b)) for a, b in record["strips"]), batches=int(record["batches"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("epoch record field in_turn") from error
    if extent(edits) > n or edits.strips != _merged(list(edits.strips)) or list(edits.stubs) != sorted(edits.stubs):
        raise ValueError("epoch record field in_turn")
    return edits


def current_turn(messages: list[dict], state: Any, dialect: Any) -> int:
    """The number the conversation's running turn gets when it is paged whole: the turns already paged
    plus the retained turns before it."""
    return _turn_of(messages, state, dialect, len(messages))


# The batch ---------------------------------------------------------------------------------------

def trigger(config: Any) -> int:
    """``T``: the estimate above which the request-time recall room would be under ``min_turn_room``."""
    return config.bound - config.headroom_floor - config.min_turn_room


class _Candidate(NamedTuple):
    i: int
    j: int
    own: bool  # one of Contexto's search, recall or state results: no page
    saving: int  # estimated tokens a stub frees
    call: dict | None
    result: dict


def page(request: dict, messages: list[dict], estimate: int, state: Any, store: Any, config: Any,
         estimator: Any, events: list[dict]) -> tuple[Any, int | None]:
    """Rule 5 for one request inside a tool loop: run a batch when one is due. Returns the state and
    the estimate of what is now sent, or the state unchanged and None when no batch ran.

    Rule 14: a store failure keeps the state from before the attempt and enters degraded mode; the
    request is sent unpaged. Pages a failed batch already wrote are rewritten identically by the next."""
    rw = _rw()
    hooks = rw._dialect().in_turn
    if (hooks is None or not config.in_turn_paging or state.degraded or estimate <= trigger(config)
            or not callable(getattr(store, "page_parts", None))):
        return state, None
    calls, attempt = rw.StoreCalls(store), []
    try:
        new, after = _batch(request, messages, estimate, state, calls, config, estimator, attempt, hooks)
    except rw.STORE_FAILURES as error:
        return rw.enter_degraded(state, rw.store_failure(error, calls, state), events), None
    events.extend(attempt)
    return new, after


def _batch(request: dict, messages: list[dict], estimate: int, state: Any, store: Any, config: Any,
           estimator: Any, events: list[dict], hooks: Any) -> tuple[Any, int | None]:
    rw = _rw()
    dialect = rw._dialect()
    n = len(messages)
    line_t = trigger(config)
    start = rw.turn_starts(messages)[-1]
    last_assistant = max(i for i in range(start, n) if rw.is_assistant(messages[i]))
    sent = view(messages, state, dialect)

    def tokens(items: list[dict]) -> int:
        return rw.estimate_items(estimator, [m for m in items if m is not FOLDED])

    # What a stub would free, for every result not yet a stub, oldest first.
    own = own_call_ids(messages[state.k:])
    done = {(i, j) for i, j, _ in state.inturn.stubs}
    seen: list[_Candidate] = []
    unseen: list[_Candidate] = []
    for i in range(state.k, n):
        if sent[i] is FOLDED:
            continue
        for j, call_id in hooks.results(messages[i]):
            result = hooks.result(messages[i], j)
            if (i, j) in done or result is None or not hooks.stubbable(result):
                continue
            call = hooks.call(messages, i, call_id)
            is_own = call_id in own
            text = stub_text(hooks, call, result, "" if is_own else PLACEHOLDER_PAGE)
            size = hooks.content_bytes(result)
            if size < STUB_MIN_BYTES or size < STUB_MIN_RATIO * len(utf8(text)):
                continue  # a byte rule, so the choice never depends on the measured ratio
            saving = tokens([sent[i]]) - tokens([hooks.with_stubs(sent[i], {j: text})])
            if saving > 0:
                (seen if i < last_assistant else unseen).append(_Candidate(i, j, is_own, saving, call, result))

    # What the whole ladder could free, and so what cannot be paged.
    rounds = [r for r in hooks.rounds(sent, start, n)[:-1] if r.foldable and r.removed]
    in_rounds = {i for r in rounds for i in r.removed}
    # Rule 4 measures the head only at k == 0, from the first usage. A new epoch before that would leave
    # it unmeasured for the rest of the session, so step 2 waits for it; the other steps keep k.
    may_page_turns = start > state.k and not (state.head_tokens is None and state.k == 0)
    earlier = tokens(rw.window_items(sent[:start], state.k)) if may_page_turns else 0
    freeable = (earlier + sum(tokens([sent[i] for i in r.removed]) for r in rounds)
                + sum(c.saving for c in seen if c.i not in in_rounds and (c.i >= start or not may_page_turns)))
    floor = estimate - freeable
    target = floor + math.ceil(config.window_fraction * max(0, line_t - floor))
    escalate = max(target, line_t - config.min_turn_room)
    over_bound = estimate > config.bound
    # Step 0: a result that cannot fit under T whatever else is paged.
    kept_floor = floor - sum(c.saving for c in unseen)
    huge = [c for c in unseen if kept_floor + c.saving > line_t]
    if not (over_bound or huge or freeable >= config.min_turn_room):
        return state, None  # nothing meaningful to page: no batch on every request (no thrash)

    base = tokens(rw.transform_messages(messages, state))
    before = state

    def measured(new: Any) -> int:
        """The anchored estimate of the request under ``new``: the pre-send estimate less what the
        edits took out of the messages, both sides counted by the estimator at one ratio."""
        return estimate - (base - tokens(rw.transform_messages(messages, new)))

    def stub(chosen: list[_Candidate], current: Any) -> Any:
        by_turn: dict[int, list[_Candidate]] = {}
        for c in chosen:
            if not c.own:
                by_turn.setdefault(_turn_of(messages, current, dialect, c.i), []).append(c)
        ids: dict[tuple[int, int], str] = {}
        for turn, group in by_turn.items():
            written = store.page_parts([hooks.result_page(c.call, c.result) for c in group], turn, RESULT_PAGE)
            ids.update({(c.i, c.j): page_id for c, page_id in zip(group, written)})
        new = [(c.i, c.j, ids.get((c.i, c.j), "")) for c in chosen]
        edits = current.inturn
        return replace(current, inturn=edits._replace(
            stubs=tuple(sorted([*edits.stubs, *new])),
            strips=_merged([*edits.strips, (min(c.i for c in chosen), n)])))

    counts = {"results": 0, "own_results": 0, "unseen_results": 0, "rounds": 0, "turns": 0, "pages": 0}
    first_changed = n
    cur = estimate

    def stubbed(chosen: list[_Candidate], unseen_step: bool) -> None:
        nonlocal state, cur, first_changed
        state = stub(chosen, state)
        cur = measured(state)
        first_changed = min(first_changed, min(c.i for c in chosen))
        counts["results"] += len(chosen)
        counts["own_results"] += sum(1 for c in chosen if c.own)
        counts["pages"] += sum(1 for c in chosen if not c.own)
        if unseen_step:
            counts["unseen_results"] += len(chosen)

    if huge:
        stubbed(huge, True)
        unseen = [c for c in unseen if c not in huge]
    # Step 1: older results, oldest first, down to the target.
    if cur > line_t and seen:
        chosen, left = [], cur
        for c in seen:
            if left <= target:
                break
            chosen.append(c)
            left -= c.saving
        if chosen:
            stubbed(chosen, False)
    def page_turns() -> None:
        nonlocal state, cur, first_changed
        paged: list[dict] = []
        state = rw.new_epoch(view(messages, state, dialect), request, state, store, config, estimator, start, n,
                             paged, ROTATION_REASON)
        events.extend(paged)
        counts["turns"] += sum(e.get("turns", 0) for e in paged if e["event"] == "page_out")
        counts["pages"] += sum(e.get("pages", 0) for e in paged if e["event"] == "page_out")
        cur = measured(state)
        first_changed = min(first_changed, start)

    # Step 2: the earlier turns of the window, through the rotation machinery.
    if cur > escalate and may_page_turns:
        page_turns()
    # Step 3: the oldest complete rounds of the running turn, into one page.
    if cur > escalate:
        sent = view(messages, state, dialect)
        rounds = [r for r in hooks.rounds(sent, start, n)[:-1] if r.foldable and r.removed]
        folded, left = [], cur
        for r in rounds:
            if left <= target:
                break
            folded.append(r)
            left -= tokens([rw.strip_thinking(sent[i]) for i in r.removed])
        if folded:
            removed = [i for r in folded for i in r.removed]
            page_id = store.page_parts([[sent[i] for i in removed]], _turn_of(messages, state, dialect, start),
                                       ROUNDS_PAGE)[0]
            edits = state.inturn
            state = replace(state, inturn=edits._replace(
                folds=(*edits.folds, (start, _spans(removed), page_id)),
                strips=_merged([*edits.strips, (start, n)])))
            cur = measured(state)
            first_changed = min(first_changed, start)
            counts["rounds"] += len(folded)
            counts["pages"] += 1
    # The bound comes before the head's measurement: still over it, the earlier turns are paged after all.
    if cur > config.bound and start > state.k:
        page_turns()
    # Step 4: over the bound itself: results the model has not seen, largest first.
    if cur > config.bound and unseen:
        chosen, left = [], cur
        for c in sorted(unseen, key=lambda c: (-c.saving, c.i, c.j)):
            if left <= config.bound:
                break
            chosen.append(c)
            left -= c.saving
        if chosen:
            stubbed(chosen, True)
    if state is before:
        if over_bound:
            events.append({"event": "in_turn_exhausted", "epoch": state.epoch, "estimate": estimate,
                           "bound": config.bound})
        return state, None
    state = replace(state, inturn=state.inturn._replace(batches=state.inturn.batches + 1))
    if first_changed <= state.k:  # the first retained message changed: everything sent is new
        resent = tokens(rw.transform_messages(messages, state))
    else:
        sent = view(messages, state, dialect)
        resent = tokens([rw.strip_thinking(sent[i]) for i in range(first_changed, n)])
    only_huge = bool(huge) and counts["results"] == len(huge) and not counts["rounds"] and not counts["turns"]
    events.append({"event": "in_turn_batch", "epoch": state.epoch,
                   "trigger": "result" if only_huge else "bound" if over_bound else "room",
                   "results_stubbed": counts["results"], "own_results_stubbed": counts["own_results"],
                   "unseen_results_stubbed": counts["unseen_results"], "rounds_folded": counts["rounds"],
                   "turns_paged": counts["turns"], "pages": counts["pages"], "estimate_before": estimate,
                   "estimate_after": cur, "first_changed": first_changed, "resent_tokens": resent,
                   "messages": n})
    if cur > config.bound:
        events.append({"event": "in_turn_exhausted", "epoch": state.epoch, "estimate": cur, "bound": config.bound})
    return state, cur


def _turn_of(messages: list[dict], state: Any, dialect: Any, index: int) -> int:
    """The turn number an in-turn page of the message at ``index`` carries: the number its turn gets when
    it is paged whole (the turns already paged plus the retained turns before it), or the main
    conversation's current turn for a side conversation (``TURN``). In-turn pages do not advance
    ``turns_paged``."""
    turn = TURN.get()
    if turn is not None:
        return turn
    starts = _rw()._turn_starts(messages, dialect)
    return state.turns_paged + bisect.bisect_right(starts, index) - bisect.bisect_right(starts, state.k)
