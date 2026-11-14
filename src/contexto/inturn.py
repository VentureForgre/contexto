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

**A batch** (``page``) runs in a tool loop when the pre-send estimate passes the line ``T - min_turn_room
/ 4``, where ``T = bound - headroom_floor - min_turn_room`` is the point at which the recall room the
request publishes would fall under ``min_turn_room`` and the quarter is an allowance for the response's
output (``page_line``). For these decisions a tool result that arrived with the request counts at 1.8
bytes a token when that is more than the session's ratio gives (``fresh_margin``): the ratio is a
blend, tool output is denser, and no usage has measured the result yet. A batch pages down to a target:
a quarter of the way from what cannot be paged up to the line (``BATCH_FRACTION``), and at least two
``min_turn_room`` under the line when that much can be paged, so the next rounds need no batch. In this
order, stopping as soon as the target is met; a step that leaves at least two ``min_turn_room`` under
the line is enough, and the next one does not run:

0. The model reads every result whole at least once unless it cannot fit. A result that has just
   arrived (one after the last assistant message) is replaced by a stub only when the head, what cannot
   be paged of the messages and the unread results could not be sent under ``T`` with everything older
   paged; then the largest go first. An unread result sits after the last thinking block, so nothing is
   stripped and the cached prefix stays.
1. Older tool results are replaced by stubs, oldest first, across the retained window. The originals
   go to result pages; the results of Contexto's own search, recall and state calls are copies of
   pages, so they get a stub and no page.
2. If less than that headway is left: the whole earlier turns of the window are paged through the rotation
   machinery (``new_epoch`` at the current turn's start). While the head is still unmeasured at ``k ==
   0`` (rule 4 measures it only there) this waits until step 3 has left the request over the bound.
3. If still less: the oldest complete tool rounds of the running turn are folded into one page and
   removed from the request, with a note in the turn-start message (never in a results message, where
   the API wants the ``tool_result`` blocks first). A round holding carried content or content the
   user typed or pasted mid-turn is never folded; it stays in place and folds run around it.
4. If the request is still over the bound itself: unread results are stubbed, largest first.
5. Still over the bound: ``over_bound`` as before, and ``in_turn_exhausted``.

Steps 1 to 3 change messages the previous request had, which rewrites the cached prefix. They run only
when they could free at least ``min_turn_room``, and never on the request right after such a rewrite,
unless the request's estimate is over ``T`` or it would be over the bound. In degraded mode (rule 14)
a request that is due a batch retries the store; success leaves degraded mode.

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

import base64
import bisect
import functools
import math
import re
import sys
import threading
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, NamedTuple

from contexto.canonical import canonical_bytes, canonical_json, utf8
from contexto.pdfpages import count_pages
from contexto.store.render import OWN_TOOL_PREFIX, own_call_ids
from contexto.tokens import TokenEstimator

STUB_MIN_BYTES = 2_048  # a result is stubbed only when its content is at least this many bytes
STUB_MIN_RATIO = 2  # and at least this many times its stub
# A tool result that has just arrived counts at this many bytes a token for in-turn decisions when the
# session's ratio would count it lower: the density measured for log-heavy tool output (contract rule 3).
FRESH_BYTES_PER_TOKEN = 1.8
OUTPUT_ALLOWANCE_SHARE = 4  # the response's output allowance: min_turn_room over this
# How deep a batch pages: to this share of the way from what cannot be paged up to the line (D63, the
# cost measurement). Under a cache that keeps a prefix only at a breakpoint, a rewrite re-sends what
# follows the head, so a deeper batch costs less per rotation and runs fewer of them: in the replay
# harness 0.25 re-sends 38% to 63% less than 0.5, with one or two fewer in-loop thinking strips.
BATCH_FRACTION = 0.25
HEADWAY_ROOMS = 2  # a batch leaves this many min_turn_room of growth before the line when it can
LOCATOR_CHARS = 200  # characters of a path, command or input a stub shows; a longer one loses its middle
NOTE_NAMES = 12  # tool names a fold note counts
NOTE_CALLS = 40  # calls a fold note lists, each with its locator
NOTE_CALL_CHARS = 120  # characters of a locator in a fold note's list
NOTE_CALLS_BUDGET = 1_600  # characters of the whole list
# What a stub shows of a call, by the first key its input has: the command of a shell call, the pattern
# or query of a search (with where it searched), else the address or the path of what it read or wrote.
LOCATOR_KEYS = ("command", "pattern", "query", "url", "file_path", "notebook_path", "path")
SEARCH_KEYS = ("pattern", "query")
PATH_KEYS = ("file_path", "notebook_path", "path")
PLACEHOLDER_PAGE = "pg_" + "0" * 16  # sizes a stub before its page is written; ids have one length
RESULT_PAGE = "result"  # page kinds; a page without one is a whole turn
ROUNDS_PAGE = "rounds"
ROTATION_REASON = "in_turn"
# What stands in a view for a message folded into a rounds page: no content, so it estimates as 0 and
# has no searchable block; ``transform_messages`` and ``strip_carried`` leave it out by identity.
FOLDED: dict[str, Any] = {"role": "assistant", "content": []}
_NOTE_CACHE_SIZE = 256
# A turn's one fold note is at most this many bytes (about 2,000 tokens, a byte rule): the newest fold in
# detail, the earlier ones as page ids and round numbers, and past the cap a page that lists them.
NOTE_BYTES = 6_000
NOTE_IDS = 60  # call ids the newest fold's part of the note lists; the rest are counted
FOLDS_PAGE = "folds"  # a page holding the detailed notes of a turn's earlier folds


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
    # The message count of the last request whose batch changed a message the request before it had (a
    # rewrite of the cached prefix); the request right after it starts no other unless over the bound.
    rewrote: int = 0
    # (turn-start index, folds covered, page id): the folds page that lists the first ``covered`` folds of
    # that turn in detail, once the turn's note would pass NOTE_BYTES. At most one per turn.
    lists: tuple[tuple[int, int, str], ...] = ()
    # One past the turn-start index of the turn whose small-bound room was logged (``turn_room_low``).
    room_low: int = 0


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
    documents: int = 0
    document_bytes: int = 0  # decoded bytes of the documents
    pages: int | None = None  # their page count, when every one is a PDF counted with certainty


@functools.lru_cache(maxsize=256)
def _document(media_type: str, data: str) -> tuple[int, int | None]:
    """(decoded bytes, certain page count or None) of a base64 document block's data."""
    try:
        raw = base64.b64decode(data, validate=False)
    except (ValueError, TypeError):
        return len(data), None
    if media_type != "application/pdf":
        return len(raw), None
    counted = count_pages(raw)
    return len(raw), counted.pages if counted.counted else None


class Round(NamedTuple):
    """One tool round of a turn: an assistant message and what follows up to the next one."""

    removed: tuple[int, ...]  # the indexes a fold takes out; a carried system message stays in place
    # False when a message of the round holds carried content or content the user put there
    # (``MessagesInTurn.user_authored``): the round then stays where it is, and folds run around it.
    foldable: bool


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
        """A result whose content a text stub can stand for: a string, or text, image and document blocks
        only. Any other block (a tool reference from tool search, which loads a deferred tool) may be
        what the CLI or the API needs."""
        content = result.get("content")
        if isinstance(content, str):
            return True
        return isinstance(content, list) and all(
            isinstance(b, dict) and (b.get("type") in ("image", "document") or (b.get("type") == "text"
                                                                               and isinstance(b.get("text"), str)))
            for b in content)

    def has_media(self, result: dict) -> bool:
        content = result.get("content")
        return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") in ("image", "document")
                                                 for b in content)

    def has_document(self, result: dict) -> bool:
        content = result.get("content")
        return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "document"
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
        documents, document_bytes, pages = 0, 0, 0
        for b in content if isinstance(content, list) else []:
            if not (isinstance(b, dict) and b.get("type") == "document"):
                continue
            documents += 1
            source = b.get("source") if isinstance(b.get("source"), dict) else {}
            data = source.get("data")
            if source.get("type") == "base64" and isinstance(data, str):
                size, count = _document(str(source.get("media_type")), data)
            else:
                size, count = len(canonical_bytes(source)), None
            document_bytes += size
            pages = None if pages is None or count is None else pages + count
        return Size(len(utf8(text)), text.count("\n") + 1 if text else 0, images, documents, document_bytes,
                    pages if documents else None)

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

    def describe(self, call: dict | None, limit: int = LOCATOR_CHARS) -> tuple[str, str, str]:
        """(tool name, locator line, arguments) of a call, for a stub: what to run again to get the result
        back. The locator is the call's command, its search pattern and where it searched, or the path
        it read, exact but on one line (``_clip``); without any of them, its whole input."""
        name = call.get("name") if call is not None else None
        name = name if isinstance(name, str) and name else "unknown"
        tool_input = call.get("input") if call is not None else None
        arguments = canonical_json(tool_input) if isinstance(tool_input, (dict, list, str)) else "{}"
        locator = f"input: {_clip(arguments, limit)}"
        if isinstance(tool_input, dict):
            def text(key: str) -> str | None:
                value = tool_input.get(key)
                return value if isinstance(value, str) and value else None

            key = next((key for key in LOCATOR_KEYS if text(key) is not None), None)
            if key is not None:
                locator = f"{key}: {_clip(text(key), limit)}"
                where = next((text(k) for k in PATH_KEYS if text(k) is not None), None)
                if key in SEARCH_KEYS and where is not None:
                    locator += f" (in {_clip(where, limit)})"
        return name, locator, _clip(arguments)

    def with_note(self, item: dict, note: str) -> dict:
        """The turn-start message with a fold note after its own content."""
        content = item.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        return {**item, "content": [*blocks, {"type": "text", "text": note}]}

    def user_authored(self, item: dict) -> bool:
        """Whether a user message inside a tool loop holds something the user put there, next to the tool
        results: a text block that is not a context block (Claude Code's ``<system-reminder>`` text) and
        not empty, an image, a document, any block that is not a ``tool_result``. Claude Code sends what
        is typed after Esc this way, after ``[Request interrupted by user ...``, which counts too."""
        if not isinstance(item, dict) or item.get("role") != "user":
            return False
        for b in _blocks(item):
            if not isinstance(b, dict) or b.get("type") == "tool_result":
                continue
            if b.get("type") == "text":
                text = b.get("text")
                if not isinstance(text, str) or not text.strip() or _rw().is_context_block(b):
                    continue
            return True
        return False

    def rounds(self, items: list[dict], start: int, end: int) -> list[Round]:
        """The tool rounds of ``items[start:end]`` in order, the newest included. A fold removes a
        round's assistant message, its tool-result message, any other user message and hook output; a
        carried system message stays where it is. A round is not foldable when it holds carried content
        next to its results (nested memory, D46), whose rules would leave the request, or content the
        user typed or pasted mid-turn (``user_authored``), which the model must keep seeing: it stays in
        place, whole, and the folds before and after it leave it there."""
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
            if (rw._is_tool_round(item) and rw._carried_at(item, False)) or self.user_authored(item):
                foldable = False
            current.append(i)
        if current is not None:
            rounds.append(Round(tuple(current), foldable))
        return rounds

    def note_facts(self, items: list[dict]) -> tuple[int, list[str], list[str], list[str]]:
        """(rounds, tool names, calls, call ids) of folded messages, in order: a call is its tool name and
        locator (``describe``), what the model needs to run it again."""
        rounds, tools, calls, ids = 0, [], [], []
        for item in items:
            if item.get("role") != "assistant":
                continue
            rounds += 1
            for b in _blocks(item):
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name, locator, _ = self.describe(b, NOTE_CALL_CHARS)
                tools.append(name)
                calls.append(f"{name} {locator}")
                if isinstance(b.get("id"), str) and b["id"]:
                    ids.append(_CONTROL.sub(" ", b["id"]).replace(" ", ""))
        return rounds, tools, calls, ids


MESSAGES = MessagesInTurn()


# Stubs and notes: pure functions of the CLI's blocks and the page id -----------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")


def _clip(text: str, limit: int = LOCATOR_CHARS) -> str:
    """``text`` on one line, so it cannot add a line a stub's format reads: each control character
    becomes a space, nothing else changes (two spaces in a path stay two). Longer than ``limit``, its
    middle is cut out and marked, so the start and the end (a file's name) stay."""
    text = _CONTROL.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    keep = limit // 2
    return f"{text[:keep]} [... {len(text) - 2 * keep:,} characters ...] {text[-keep:]}"


def _was(size: Size) -> str:
    was = f"was: {size.bytes:,} bytes, {size.lines:,} lines"
    if size.images:
        was += (f", {size.images} image{'s' if size.images != 1 else ''} (recall shows text only; run the call "
                "again to see an image)")
    if size.documents:
        pages = f", {size.pages:,} page{'s' if size.pages != 1 else ''}" if size.pages is not None else ""
        was += (f", {size.documents} document{'s' if size.documents != 1 else ''}{pages}, {size.document_bytes:,} "
                "bytes (the page keeps it byte for byte, but recall shows text only, not a document's contents; "
                "run the call again to see the document)")
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
    # Worded for any conversation: one without Contexto's tools (a subagent given only some tools, or a
    # head where tool search defers them) has the call above the stub to run again. Not a branch on the
    # tool list, so the stub stays a pure function of the CLI's blocks and the page id.
    return (f"[Contexto paged out this tool result to keep the request under its bound. The original is stored "
            f"in page {page_id}.\n"
            f"tool: {name}\n"
            f"{locator}\n"
            f"{_was(size)}\n"
            f"page: {page_id}\n"
            f'If you have the contexto_recall tool, call it with page_id "{page_id}" to read it (pass the next '
            "offset to continue a long page); contexto_search finds lines in it. Without it, run the tool call "
            "again.]")


_STUB_PAGE = re.compile(r"\npage: (pg_[0-9a-f]{16})\n")
_NOTE_PAGE = re.compile(r" of this turn into page (pg_[0-9a-f]{16}) to keep the request under its bound\.")


def stub_page(content: Any) -> str | None:
    """The page id a result stub names, or None when ``content`` is not one (``stub_text``'s form)."""
    if not isinstance(content, str) or not content.startswith("[Contexto paged out this tool result "):
        return None
    found = _STUB_PAGE.search(content)
    return found.group(1) if found else None


def is_own_stub(content: Any) -> bool:
    """A stub of one of Contexto's own search, recall or state results, which names no page."""
    return isinstance(content, str) and content.startswith("[Contexto dropped this result of its own tool ")


_NOTE_IDS = re.compile(r"\ncall ids: ([^\n]*)\n")


def note_calls(text: Any) -> tuple[str, ...]:
    """The call ids a fold note lists (every call it folded), or () when ``text`` is not one."""
    if note_page(text) is None:
        return ()
    found = _NOTE_IDS.search(text)
    if not found:
        return ()
    return tuple(i for i in found.group(1).split(";")[0].split(" ") if i and i != "none")


def note_page(text: Any) -> str | None:
    """The page id a fold note names, or None when ``text`` is not one (``fold_note``'s form)."""
    if not isinstance(text, str) or not text.startswith("[Contexto folded "):
        return None
    found = _NOTE_PAGE.search(text)
    return found.group(1) if found else None


def _counted(names: list[str]) -> str:
    counts: OrderedDict[str, int] = OrderedDict()
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    shown = [f"{name} x{count}" for name, count in list(counts.items())[:NOTE_NAMES]]
    if len(counts) > NOTE_NAMES:
        shown.append(f"and {len(counts) - NOTE_NAMES} more")
    return ", ".join(shown) or "none"


def _calls(calls: list[str]) -> str:
    """The calls a fold note lists, in order, up to ``NOTE_CALLS`` and ``NOTE_CALLS_BUDGET`` characters;
    the rest are counted."""
    shown: list[str] = []
    used = 0
    for call in calls[:NOTE_CALLS]:
        if shown and used + len(call) + 2 > NOTE_CALLS_BUDGET:
            break
        shown.append(call)
        used += len(call) + 2
    text = "; ".join(shown) or "none"
    if len(calls) > len(shown):
        text += f"; and {len(calls) - len(shown):,} more calls"
    return text


def _ids(ids: list[str]) -> str:
    shown = " ".join(ids[:NOTE_IDS]) or "none"
    return shown + (f"; {len(ids) - NOTE_IDS:,} more in the page" if len(ids) > NOTE_IDS else "")


_NOTE_TAIL = ('If you have the contexto_recall tool, call it with a page_id named here to read them (pass the '
              "next offset to continue; a page is long); contexto_search finds lines in them. Without it, run the "
              "calls again.]")
_NOTES: OrderedDict[tuple[str, int], str] = OrderedDict()
_NOTES_LOCK = threading.Lock()  # engines of several sessions can share a process


def fold_note(hooks: Any, removed: list[dict], page_id: str) -> str:
    """The note a fold leaves in the turn-start message: how many rounds, which tools, the calls with
    what each ran or read (enough to run them again where Contexto's tools are not available), their
    size, and the page. ``removed`` is what the folded messages were as sent (a stubbed result is its
    stub). Kept per page id and count, so the messages are measured once per process."""
    key = (page_id, len(removed))
    with _NOTES_LOCK:
        note = _NOTES.get(key)
        if note is not None:
            _NOTES.move_to_end(key)
            return note
    note = _fold_note_uncached(hooks, removed, page_id)
    with _NOTES_LOCK:
        _NOTES[key] = note
        while len(_NOTES) > _NOTE_CACHE_SIZE:
            _NOTES.popitem(last=False)
    return note


def _fold_note_uncached(hooks: Any, removed: list[dict], page_id: str) -> str:
    rounds, tools, calls, ids = hooks.note_facts(removed)
    size = sum(len(canonical_bytes(m)) for m in removed)
    note = (f"[Contexto folded {rounds:,} earlier tool round{'s' if rounds != 1 else ''} of this turn into page "
            f"{page_id} to keep the request under its bound. They are stored in page {page_id}.\n"
            f"tools: {_counted(tools)}\n"
            f"calls: {_calls(calls)}\n"
            f"call ids: {_ids(ids)}\n"
            f"was: {size:,} bytes\n"
            f"{_NOTE_TAIL}")
    return note


def _round_numbers(messages: list[dict], start: int, upto: int, dialect: Any) -> list[int]:
    """For each index in ``[start, upto)``, the number of the turn's tool round it is part of (1 for the
    first assistant message after the turn start)."""
    numbers, count = [], 0
    for i in range(start, upto):
        if dialect.is_assistant(messages[i]):
            count += 1
        numbers.append(count)
    return numbers


def turn_note(hooks: Any, messages: list[dict], sent: list[dict], start: int,
              group: list[tuple[tuple[tuple[int, int], ...], str]], listed: tuple[int, str] | None,
              dialect: Any) -> str:
    """The one fold note of the turn starting at ``start``: the newest fold in detail (``fold_note``),
    then the earlier ones as round numbers and page ids, the first ``listed[0]`` of them as one item
    naming the folds page ``listed[1]`` that lists them in detail. A pure function of the CLI's blocks and
    the page ids. ``sent`` is the view before the folded messages are replaced."""
    ranges, page_id = group[-1]
    note = fold_note(hooks, [sent[i] for a, b in ranges for i in range(a, b)], page_id)
    earlier = group[:-1]
    if not earlier:
        return note
    last = max(b for rs, _ in earlier for _, b in rs)
    numbers = _round_numbers(messages, start, last, dialect)

    def span(rs: tuple[tuple[int, int], ...]) -> str:
        first, end = numbers[rs[0][0] - start], numbers[rs[-1][1] - 1 - start]
        return f"round {first}" if first == end else f"rounds {first} to {end}"

    parts = []
    covered = listed[0] if listed is not None else 0
    if covered:
        whole = ((earlier[0][0][0][0], earlier[covered - 1][0][-1][1]),)
        parts.append(f"{span(whole)} in {covered} pages listed in page {listed[1]}")
    parts += [f"{span(rs)} in {pid}" for rs, pid in earlier[covered:]]
    head, tail = note[:-len(_NOTE_TAIL)], _NOTE_TAIL
    return f"{head}earlier folds of this turn: {'; '.join(parts)}\n{tail}"


def folds_page(hooks: Any, sent: list[dict], group: list[tuple[tuple[tuple[int, int], ...], str]]) -> list[dict]:
    """The messages of a folds page: the detailed note of each fold in ``group``, in order, so search
    finds them and a reader follows each to its rounds page. ``sent`` is the view with no fold applied."""
    return [{"role": "user", "content": [
        {"type": "text", "text": _fold_note_uncached(hooks, [sent[i] for a, b in rs for i in range(a, b)], pid)}
        for rs, pid in group]}]


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
    by_turn: dict[int, list[tuple[tuple[tuple[int, int], ...], str]]] = {}
    for start, ranges, page_id in edits.folds:
        if start < n and ranges[-1][1] <= n:
            by_turn.setdefault(start, []).append((ranges, page_id))
    listed = {start: (covered, page_id) for start, covered, page_id in edits.lists}
    for start, group in by_turn.items():
        # One note per turn, however many folds (Reviewer B, finding 1): rebuilt at each fold, bounded.
        out[start] = hooks.with_note(out[start], turn_note(hooks, messages, out, start, group, listed.get(start),
                                                           dialect))
        for ranges, _ in group:
            for a, b in ranges:
                for i in range(a, b):
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
        folds=tuple(f for f in edits.folds if f[0] >= k), lists=tuple(x for x in edits.lists if x[0] >= k),
        strips=_merged([(max(a, strip_upto), b) for a, b in edits.strips]))


def clipped(edits: InTurn, matched: int, messages: list[dict], dialect: Any) -> InTurn:
    """Rule 10's resync inside the window: the CLI changed its history from ``matched`` on (``messages``
    is its new history), so an edit that names a message there no longer has one. Edits before it stand.

    A fold keeps only whole rounds: it is cut back to the last round that ends before ``matched``, so
    that no call stays folded while its results are sent, nor the other way round, whichever role the
    message at ``matched`` has; a fold with no whole round left is dropped. Its note then lists what it
    still folds, and its page (which holds the rest too) is unchanged. A thinking strip keeps its old
    end (clipped to the new history's length): the thinking of a message after ``matched`` was produced
    under the edits before it, which stand."""
    if edits == EMPTY or extent(edits) <= matched:
        return edits
    n = len(messages)
    starts_round = matched >= n or dialect.is_assistant(messages[matched])
    folds = []
    for start, ranges, page_id in edits.folds:
        removed = [i for a, b in ranges for i in range(a, b)]
        if start >= matched or not removed:
            continue
        if removed[-1] >= matched:
            # The cut: ``matched`` itself when a round starts there, else the call message of the round
            # that ``matched`` is part of, which goes back into the request with the rest of its round.
            calls = [i for i in removed if i < matched and dialect.is_assistant(messages[i])]
            cut = matched if starts_round else (calls[-1] if calls else removed[0])
            removed = [i for i in removed if i < cut]
        if removed:
            folds.append((start, _spans(removed), page_id))
    # A folds page stands for the turn's first folds only while those are unchanged.
    def first(group: Any, start: int, covered: int) -> list:
        return [f for f in group if f[0] == start][:covered]

    lists = tuple((start, covered, pid) for start, covered, pid in edits.lists
                  if first(folds, start, covered) == first(edits.folds, start, covered))
    return edits._replace(stubs=tuple(s for s in edits.stubs if s[0] < matched), folds=tuple(folds), lists=lists,
                          strips=_merged([(a, min(b, n)) for a, b in edits.strips if a < n]))


# The epoch record (rule 16) -----------------------------------------------------------------------

def to_record(edits: InTurn) -> dict[str, Any]:
    return {"stubs": [list(s) for s in edits.stubs],
            "folds": [[start, [list(r) for r in ranges], page_id] for start, ranges, page_id in edits.folds],
            "strips": [list(r) for r in edits.strips], "batches": edits.batches, "rewrote": edits.rewrote,
            "lists": [list(x) for x in edits.lists], "room_low": edits.room_low}


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
            strips=tuple((int(a), int(b)) for a, b in record["strips"]), batches=int(record["batches"]),
            rewrote=int(record["rewrote"]),
            lists=tuple((int(s), int(c), str(p)) for s, c, p in record.get("lists", [])),
            room_low=int(record.get("room_low", 0)))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("epoch record field in_turn") from error
    if (extent(edits) > n or edits.rewrote > n or edits.strips != _merged(list(edits.strips))
            or list(edits.stubs) != sorted(edits.stubs)):
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


def page_line(config: Any) -> int:
    """The line a batch holds requests under: ``T`` less an allowance for the response's output, a
    quarter of ``min_turn_room``. The room published when the response arrives is ``bound - (input +
    output) - headroom_floor``, so a request at ``T`` would publish less than ``min_turn_room`` by the
    whole output; with the allowance an output of up to three quarters of ``min_turn_room`` (9,000
    tokens with the defaults) still leaves half of it."""
    return trigger(config) - config.min_turn_room // OUTPUT_ALLOWANCE_SHARE


def show_line(config: Any) -> int:
    """The line for a small bound (``_tightness``): the bound less the floor and the output allowance."""
    return config.bound - config.headroom_floor - config.min_turn_room // OUTPUT_ALLOWANCE_SHARE


def _tightness(request: dict, messages: list[dict], sent: list[dict], state: Any, config: Any, estimator: Any,
               dialect: Any) -> tuple[int, bool]:
    """(the head's tokens, whether the bound is tight): tight when it leaves less than two
    ``min_turn_room`` between the head plus the turn's prompt and ``T`` (Reviewer B, finding 3). Then a
    batch holds requests under ``show_line`` instead of ``page_line``, and a new result is shown once if
    it fits there; the room may fall under ``min_turn_room``, logged once per turn."""
    rw = _rw()
    head = rw.measured_head(request, state, estimator)
    if head is None:
        head = estimator.estimate(canonical_json(dialect.head_estimate_source(request)))
    start = rw.turn_starts(messages)[-1]
    prompt = rw.estimate_items(estimator, [sent[start]])
    return head, trigger(config) - (head + prompt) < HEADWAY_ROOMS * config.min_turn_room


class _Candidate(NamedTuple):
    i: int
    j: int
    own: bool  # one of Contexto's search, recall or state results: no page
    saving: int  # estimated tokens a stub frees, a fresh result counted with its margin
    plain: int  # the same at the session's ratio alone
    document: bool  # the result holds a document: stubbed once seen, unread only when over the bound alone
    call: dict | None
    result: dict


_DENSE = TokenEstimator()  # counts at FRESH_BYTES_PER_TOKEN, whatever the session has measured
_DENSE.observe(round(FRESH_BYTES_PER_TOKEN * 1_000_000), 1_000_000)


def fresh_margin(items: list[dict], estimator: Any, dialect: Any) -> int:
    """What the estimate of tool results that arrived with this request may be short by. The session's
    ratio is a blend learned mostly from the head's text, and tool output is denser (measured in the
    replay harness: a 60,000-token result estimated 17,195 low). Until a response's usage has measured
    it, a fresh result counts at ``FRESH_BYTES_PER_TOKEN`` for in-turn decisions; this is the
    difference, never negative."""
    hooks = dialect.in_turn
    extra = 0
    for item in items:
        if item is not FOLDED and hooks.results(item):
            extra += max(0, dialect.estimate_items(_DENSE, [item]) - dialect.estimate_items(estimator, [item]))
    return extra


def page(request: dict, messages: list[dict], previous_n: int, estimate: int, state: Any, store: Any,
         config: Any, estimator: Any, events: list[dict]) -> tuple[Any, int | None]:
    """Rule 5 for one request inside a tool loop: run a batch when one is due. ``previous_n`` is how many
    of the messages the previous request had. Returns the state and the estimate of what is now sent, or
    the state unchanged and None when no batch ran.

    Rule 14: a store failure keeps the state from before the attempt and enters degraded mode; the
    request is sent unpaged. Pages a failed batch already wrote are rewritten identically by the next.
    In degraded mode every later request of the loop whose estimate passes the line retries: the batch
    is attempted as if not degraded, and when it writes its pages the engine leaves degraded mode
    (``degraded_leave``) and sends the batch; a failure stays degraded. A loop has no turn start to
    retry at, and a subagent's has none at all, so waiting for one would leave it unbounded."""
    rw = _rw()
    dialect = rw._dialect()
    hooks = dialect.in_turn
    if hooks is None or not config.in_turn_paging or not callable(getattr(store, "page_parts", None)):
        return state, None
    margin = fresh_margin(view(messages, state, dialect)[previous_n:], estimator, dialect)
    _, tight = _tightness(request, messages, view(messages, state, dialect), state, config, estimator, dialect)
    if estimate + margin <= (show_line(config) if tight else page_line(config)):
        return state, None
    retry = state.degraded
    calls, attempt = rw.StoreCalls(store), []
    try:
        new, after = _batch(request, messages, previous_n, estimate, margin,
                            replace(state, degraded=False) if retry else state, calls, config, estimator, attempt,
                            hooks)
    except rw.STORE_FAILURES as error:
        return rw.enter_degraded(state, rw.store_failure(error, calls, state), events), None
    if retry:
        if after is None:  # no batch was due, so the store was not tried: still degraded
            events.extend(attempt)
            return state, None
        events.append({"event": "degraded_leave", "epoch": state.epoch})
        new = replace(new, conflict_page=None)
    events.extend(attempt)
    return new, after


def _batch(request: dict, messages: list[dict], previous_n: int, estimate: int, margin: int, state: Any,
           store: Any, config: Any, estimator: Any, events: list[dict], hooks: Any) -> tuple[Any, int | None]:
    """One batch. Every decision is made on the estimate plus the fresh results' margin; what is logged
    and returned is the estimate itself."""
    rw = _rw()
    dialect = rw._dialect()
    n = len(messages)
    head, tight = _tightness(request, messages, view(messages, state, dialect), state, config, estimator,
                             dialect)
    line = show_line(config) if tight else page_line(config)
    start = rw.turn_starts(messages)[-1]
    last_assistant = max(i for i in range(start, n) if rw.is_assistant(messages[i]))
    sent = view(messages, state, dialect)

    def tokens(items: list[dict]) -> int:
        return rw.estimate_items(estimator, [m for m in items if m is not FOLDED])

    def counted(i: int, item: dict) -> int:
        """A sent item's tokens for a decision: a fresh tool result with its margin."""
        return tokens([item]) + (fresh_margin([item], estimator, dialect) if i >= previous_n else 0)

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
            media = hooks.has_media(result)
            if not media and (size < STUB_MIN_BYTES or size < STUB_MIN_RATIO * len(utf8(text))):
                # A byte rule, so the choice never depends on the measured ratio. Images and documents are
                # priced by pixels and pages, not bytes (a 10-page PDF can be 1 KB): for them the test is
                # only that the stub frees something (``plain`` below), which is not a ratio either.
                continue
            stubbed_item = hooks.with_stubs(sent[i], {j: text})
            plain = tokens([sent[i]]) - tokens([stubbed_item])
            saving = counted(i, sent[i]) - counted(i, stubbed_item)
            if plain > 0:
                (seen if i < last_assistant else unseen).append(
                    _Candidate(i, j, is_own, saving, plain, hooks.has_document(result), call, result))

    # What the whole ladder could free, and so what cannot be paged.
    rounds = [r for r in hooks.rounds(sent, start, n)[:-1] if r.foldable and r.removed]
    in_rounds = {i for r in rounds for i in r.removed}
    # Rule 4 measures the head only at k == 0, from the first usage. A new epoch before that would leave
    # it unmeasured for the rest of the session, so step 2 waits for it; the other steps keep k.
    may_page_turns = start > state.k and not (state.head_tokens is None and state.k == 0)
    earlier = tokens(rw.window_items(sent[:start], state.k)) if start > state.k else 0
    freeable = ((earlier if may_page_turns else 0) + sum(counted(i, sent[i]) for r in rounds for i in r.removed)
                + sum(c.saving for c in seen if c.i not in in_rounds and (c.i >= start or not may_page_turns)))
    pressure = estimate + margin
    floor = pressure - freeable
    halfway = floor + math.ceil(BATCH_FRACTION * max(0, line - floor))
    # Down to halfway between what cannot be paged and the line, and far enough under the line that the
    # next rounds do not need another batch; never under what cannot be paged.
    headway = line - HEADWAY_ROOMS * config.min_turn_room
    target = max(floor, min(halfway, headway))
    # Stubbing that ends at or under this line is enough; over it, the next step runs.
    escalate = max(target, headway)
    over_bound = pressure > config.bound
    base = tokens(rw.transform_messages(messages, state))
    # Step 0: the model reads every result whole at least once unless it cannot fit. A result is stubbed
    # on arrival only when the head, what cannot be paged of the messages (the turn-start prompt, the
    # newest round) and the unread results could not be sent under T with everything older paged. The
    # messages are counted directly here, not from the anchored estimate, which carries the error of the
    # older results that paging would take out with them.
    pageable = (earlier + sum(tokens([sent[i] for i in r.removed]) for r in rounds)
                + sum(c.plain for c in seen if c.i >= start and c.i not in in_rounds))
    least = head + base - pageable + sum(c.saving - c.plain for c in unseen)
    # Reviewer B, finding 3: when the bound leaves less than two min_turn_room between the head and the
    # prompt and T, every new result would fail the test against T, though the request would be far under
    # the bound. Then a result is shown once if it fits under the bound less the floor and the output
    # allowance; the room on that request may fall under min_turn_room, logged once per turn.
    show = show_line(config) if tight else trigger(config)
    if tight and pressure > trigger(config) and state.inturn.room_low != start + 1:
        events.append({"event": "turn_room_low", "epoch": state.epoch, "reason": "in_turn_small_bound",
                       "room": max(0, config.bound - pressure - config.headroom_floor),
                       "min_turn_room": config.min_turn_room})
        state = replace(state, inturn=state.inturn._replace(room_low=start + 1))
    if tight:
        # Under a tight bound the floor, not the 1.8 count, keeps the request under the bound: a result
        # shown once is tested at the session's ratio, as the request is sent.
        least -= sum(c.saving - c.plain for c in unseen)
    huge: list[_Candidate] = []
    for c in sorted(unseen, key=lambda c: (-c.saving, c.i, c.j)):
        if least <= show:
            break
        if c.document and c.plain <= config.bound:
            continue  # a document is read whole unless it is over the bound on its own (Reviewer B, finding 8)
        huge.append(c)
        least -= c.plain if tight else c.saving
    # A batch that changes earlier messages rewrites the cached prefix. The request right after one starts
    # no other unless without it the request would publish less than min_turn_room (its estimate over
    # T, the room's own line) or be over the bound; and none starts for less than min_turn_room.
    after_rewrite = state.inturn.rewrote == previous_n and state.inturn.rewrote > 0
    may_rewrite = over_bound or (freeable >= config.min_turn_room
                                 and (not after_rewrite or estimate > show))
    if not (huge or may_rewrite):
        return state, None
    before = state

    def measured(new: Any) -> int:
        """The anchored estimate of the request under ``new``: the pre-send estimate less what the
        edits took out of the messages, both sides counted by the estimator at one ratio."""
        return estimate - (base - tokens(rw.transform_messages(messages, new)))

    def pressed(new: Any) -> int:
        """``measured`` plus the margin of the fresh results still in the request."""
        return measured(new) + fresh_margin(view(messages, new, dialect)[previous_n:], estimator, dialect)

    def stub(chosen: list[_Candidate], current: Any) -> Any:
        by_turn: dict[int, list[_Candidate]] = {}
        for c in chosen:
            if not c.own:
                by_turn.setdefault(_turn_of(messages, current, dialect, c.i), []).append(c)
        ids: dict[tuple[int, int], str] = {}
        for turn, group in by_turn.items():
            written = store.page_parts([hooks.result_page(c.call, c.result) for c in group], turn, RESULT_PAGE,
                                       [c.i for c in group])
            ids.update({(c.i, c.j): page_id for c, page_id in zip(group, written)})
        new = [(c.i, c.j, ids.get((c.i, c.j), "")) for c in chosen]
        edits = current.inturn
        return replace(current, inturn=edits._replace(
            stubs=tuple(sorted([*edits.stubs, *new])),
            strips=_merged([*edits.strips, (min(c.i for c in chosen), n)])))

    counts = {"results": 0, "own_results": 0, "unseen_results": 0, "rounds": 0, "turns": 0, "pages": 0}
    first_changed = n
    cur = pressure

    def stubbed(chosen: list[_Candidate], unseen_step: bool) -> None:
        nonlocal state, cur, first_changed
        state = stub(chosen, state)
        cur = pressed(state)
        first_changed = min(first_changed, min(c.i for c in chosen))
        counts["results"] += len(chosen)
        counts["own_results"] += sum(1 for c in chosen if c.own)
        counts["pages"] += sum(1 for c in chosen if not c.own)
        if unseen_step:
            counts["unseen_results"] += len(chosen)

    def page_turns() -> None:
        nonlocal state, cur, first_changed
        paged: list[dict] = []
        state = rw.new_epoch(view(messages, state, dialect), request, state, store, config, estimator, start, n,
                             paged, ROTATION_REASON)
        events.extend(paged)
        counts["turns"] += sum(e.get("turns", 0) for e in paged if e["event"] == "page_out")
        counts["pages"] += sum(e.get("pages", 0) for e in paged if e["event"] == "page_out")
        cur = pressed(state)
        first_changed = min(first_changed, start)

    if huge:
        stubbed(huge, True)
        unseen = [c for c in unseen if c not in huge]
    # Steps 1 to 3 change messages the previous request had: a rewrite of the cached prefix.
    rewriting = may_rewrite and cur > line
    # Step 1: older results, oldest first, down to the target.
    if rewriting and seen:
        chosen, left = [], cur
        for c in seen:
            if left <= target:
                break
            chosen.append(c)
            left -= c.saving
        if chosen:
            stubbed(chosen, False)
    # Step 2: the earlier turns of the window, through the rotation machinery.
    if rewriting and cur > escalate and may_page_turns:
        page_turns()
    # Step 3: the oldest complete rounds of the running turn, into one page.
    if rewriting and cur > escalate:
        sent = view(messages, state, dialect)
        rounds = [r for r in hooks.rounds(sent, start, n)[:-1] if r.foldable and r.removed]
        folded, left = [], cur
        for r in rounds:
            if left <= target:
                break
            folded.append(r)
            left -= sum(counted(i, rw.strip_thinking(sent[i])) for i in r.removed)
        if folded:
            removed = [i for r in folded for i in r.removed]
            # The page holds the rounds as sent, a stubbed result as its stub (its original is in its own
            # result page), except one of Contexto's own results, stubbed with no page: the page holds
            # that one whole, since once folded no stub in the request names it.
            kept = state.inturn._replace(stubs=tuple(s for s in state.inturn.stubs if s[2]))
            stored = view(messages, replace(state, inturn=kept), dialect)
            page_id = store.page_parts([[stored[i] for i in removed]], _turn_of(messages, state, dialect, start),
                                       ROUNDS_PAGE, [removed[0]])[0]
            edits = state.inturn
            state = replace(state, inturn=edits._replace(
                folds=(*edits.folds, (start, _spans(removed), page_id)),
                strips=_merged([*edits.strips, (start, n)])))
            state = _listed(messages, state, store, start, hooks, dialect)
            cur = pressed(state)
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
        if estimate > config.bound:
            events.append({"event": "in_turn_exhausted", "epoch": state.epoch, "estimate": estimate,
                           "bound": config.bound})
        return state, None
    rewrote = n if first_changed < previous_n else state.inturn.rewrote
    state = replace(state, inturn=state.inturn._replace(batches=state.inturn.batches + 1, rewrote=rewrote))
    after = measured(state)
    if first_changed <= state.k:  # the first retained message changed: everything sent is new
        resent = tokens(rw.transform_messages(messages, state))
    else:
        sent = view(messages, state, dialect)
        resent = tokens([rw.strip_thinking(sent[i]) for i in range(first_changed, n)])
    only_huge = bool(huge) and counts["results"] == len(huge) and not counts["rounds"] and not counts["turns"]
    events.append({"event": "in_turn_batch", "epoch": state.epoch,
                   "trigger": "result" if only_huge else "bound" if over_bound else "room",
                   "results_stubbed": counts["results"], "own_results_stubbed": counts["own_results"],
                   "unseen_results_stubbed": counts["unseen_results"],
                   "documents_over_bound": sum(1 for c in huge if c.document), "rounds_folded": counts["rounds"],
                   "turns_paged": counts["turns"], "pages": counts["pages"], "estimate_before": estimate,
                   "estimate_after": after, "margin": margin, "first_changed": first_changed,
                   "resent_tokens": resent, "messages": n})
    if after > config.bound:
        events.append({"event": "in_turn_exhausted", "epoch": state.epoch, "estimate": after,
                       "bound": config.bound})
    return state, after


def _listed(messages: list[dict], state: Any, store: Any, start: int, hooks: Any, dialect: Any) -> Any:
    """Keep the turn's note under NOTE_BYTES: when it would pass the cap, write a folds page that lists
    every fold but the newest in detail, and let the note name that page instead of each of them."""
    edits = state.inturn
    group = [(ranges, pid) for s, ranges, pid in edits.folds if s == start]
    if len(group) < 2:
        return state
    sent = view(messages, state, dialect)
    if len(utf8(_note_text(sent[start]))) <= NOTE_BYTES:
        return state
    unfolded = view(messages, replace(state, inturn=edits._replace(folds=(), lists=())), dialect)
    page_id = store.page_parts([folds_page(hooks, unfolded, group[:-1])], _turn_of(messages, state, dialect, start),
                               FOLDS_PAGE, [start])[0]
    lists = tuple(x for x in edits.lists if x[0] != start) + ((start, len(group) - 1, page_id),)
    return replace(state, inturn=edits._replace(lists=tuple(sorted(lists))))


def _note_text(item: dict) -> str:
    """The fold note a view put on a turn-start message: its last text block."""
    blocks = _blocks(item)
    last = blocks[-1] if blocks else None
    return last.get("text", "") if isinstance(last, dict) and note_page(last.get("text")) else ""


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
