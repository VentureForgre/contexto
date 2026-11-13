"""The fake upstream: it receives exactly what the engine forwards, checks it the way an enforcing
account would, reports usage with its own tokenizer and a prompt-cache model, and answers with the
script's next step. It never raises on a bad request: each violation is counted by kind.

Every request is read from its JSON as it stands when it arrives, so an engine that edits one of its
own messages in place between requests is judged on what it sends, as over HTTP. The one exception is
the CLI's own history items and head, which the harness pins (``pin``, ``pin_head``): they are read
once, and the harness fails the run if any of them changed by its end (``harness._Play.finish``).

What it enforces, and where each rule is recorded:

Messages API (``docs/research/M0-interception.md``, "Constraints"; contract rule 1):

- ``tool_use_without_result``: a ``tool_use`` whose ``tool_result`` is not in the next message.
- ``tool_result_without_use``: a ``tool_result`` whose ``tool_use`` is not in the assistant message
  right before it.
- ``duplicate_tool_use``, ``duplicate_tool_result``: the same id twice.
- ``text_before_tool_result``: in a user message, a block of another kind ahead of a ``tool_result``.
  Text after the results is valid (Claude Code puts reminders there).
- ``first_message_not_user``: the first message that is not a role ``system`` one is not a user message.
- ``empty_content``: a user or assistant message with no content, or a text block with no text.
- ``system_before_first_user``: a role ``system`` message before the first user message (measured 400).
- ``thinking_binding``: a thinking block whose prefix changed. On 5.x models "nothing before the
  thinking block" may change: ``system``, ``tools``, the earlier messages, and the blocks ahead of it
  in its own message. A signature here is a keyed hash of the request the block answered (``system``,
  ``tools``, messages; ``cache_control`` and thinking blocks left out) and of the blocks ahead of it in
  its message, so the same hash taken over what precedes the block in a later request must match.
- ``thinking_modified``: a thinking block whose text or signature is not what was emitted.

Responses API (same note, "Call pairing" and "Reasoning items"):

- ``call_without_output``, ``call_with_several_outputs``: a client ``*_call`` must have exactly one
  ``*_output`` with its ``call_id`` after it. Hosted calls (``web_search_call`` and the like) have none.
- ``output_without_call``: an ``*_output`` with no call of that ``call_id`` before it.
- ``output_kind_mismatch``: an output whose type is not its call's type with ``_output`` (a
  ``function_call`` answered by a ``custom_tool_call_output``).
- ``duplicate_call``: the same call item twice.
- ``reasoning_without_following``: a ``reasoning`` item not followed by the item the model emitted
  after it (documented). A reasoning item left behind when its call was removed is caught here even
  when another reasoning item follows.
- ``reasoning_modified``: a ``reasoning`` item whose content is not what was emitted (inferred).

Both: ``not_an_object``, a message or item that is not a JSON object.

One more kind is this harness's rule, not an API error (the API accepts the request and, as
documented, may stop thinking for the rest of the turn):

- ``thinking_removed_outside_rewrite`` (Responses: ``reasoning_removed_outside_rewrite``): thinking the
  model emitted is missing and no rewrite covers it. A request rewrites content at ``p`` when its head
  differs from the conversation's previous request, or its item at ``p`` is the first that differs
  from that request's in more than its thinking (Responses: with reasoning items left out of both). Such a request covers every response emitted so far
  whose message is at ``p`` or later (or gone): that thinking may be missing on that request and on
  every later one. Thinking before ``p``, and thinking emitted after the rewrite, must come back, and
  removing thinking alone covers nothing. So stripping the newest response's thinking on a plain
  append, one old block by itself, or everything on every request, is counted.

Counted as information (``info``), per request:

- ``inflight_thinking_missing`` (Responses: ``inflight_reasoning_dropped``): at least one response of
  the turn in flight arrived without the thinking (reasoning item) it was emitted with.
- ``..._latest``: the newest one, whose results the request returns, is among them.
- ``inflight_assistant_absent`` (``inflight_calls_absent``): at least one did not arrive at all.
- ``thinking_kept_around_strip`` (``reasoning_kept_around_drop``): thinking is kept both before and
  after thinking that is missing. This is what a rewrite in the middle of a loop leaves behind. The
  research records it as valid (the strip was a removal from the end when it was made; the later
  blocks are appends); no live run has confirmed it inside a tool loop.

The prompt cache: ``cache_read`` is the longest prefix (the head, then item by item, on the JSON with
``cache_control`` ignored and a string ``content`` read as one text block) that an earlier request of
the same conversation began with. There is no minimum length, no expiry, no limit on breakpoints and
no 20-block lookback.

Two numbers describe a prefix rewrite. ``invalidated`` is the previous request's tokens from its first
changed item to its end: the cache the rewrite threw away. ``resent`` is what the rewrite cost: the
request's uncached tokens less the tokens of the items the CLI added since its previous request (an
added item counts as the engine forwarded it, when that is smaller).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections import Counter
from collections.abc import Sequence
from itertools import accumulate
from typing import Any, NamedTuple

from inturn import cli
from inturn.scripts import Step, digest
from inturn.tokenizer import block_tokens, canonical, dumps, item_tokens, message_tokens, value_tokens

KEY = b"inturn fake upstream: not a secret"
THINKING_TYPES = ("thinking", "redacted_thinking")
HOSTED_CALL_TYPES = frozenset({"web_search_call", "file_search_call", "image_generation_call",
                               "code_interpreter_call", "mcp_call"})
MODEL_ROLES = ("assistant", "call", "hosted")
MESSAGES_KINDS = ("tool_use_without_result", "tool_result_without_use", "duplicate_tool_use",
                  "duplicate_tool_result", "text_before_tool_result", "first_message_not_user", "empty_content",
                  "system_before_first_user", "thinking_binding", "thinking_modified",
                  "thinking_removed_outside_rewrite", "not_an_object")
RESPONSES_KINDS = ("call_without_output", "call_with_several_outputs", "output_without_call",
                   "output_kind_mismatch", "duplicate_call", "reasoning_without_following", "reasoning_modified",
                   "reasoning_removed_outside_rewrite", "not_an_object")
NO_BLOCKS = b"[]"


def _sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _mac(binding: bytes, thinking: str) -> str:
    return hmac.new(KEY, binding + thinking.encode(), hashlib.sha256).hexdigest()


def _binding(prefix: bytes, before: bytes) -> bytes:
    """What a thinking block is bound to: the request up to its message, and the blocks ahead of it
    in that message (their JSON, thinking blocks left out)."""
    return _sha(prefix + before)


def sign(binding: bytes, thinking: str) -> str:
    """A thinking block's signature: the hash it is bound to, then a keyed hash of that and the text."""
    return binding.hex() + _mac(binding, thinking)


def without_cache_control(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: without_cache_control(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [without_cache_control(v) for v in value]
    return value


class Item(NamedTuple):
    """What the upstream reads from one message or item."""

    role: str  # Messages: the role. Responses: user, assistant, reasoning, call, hosted, output or other
    kind: str  # Responses: the item's type. Messages: ""
    digest: bytes  # of its JSON, cache_control ignored
    bare: bytes  # the same with thinking blocks removed
    tokens: int
    wire: int  # bytes as sent
    marked: bool  # carries a cache breakpoint
    uses: tuple[str, ...]  # tool call ids it makes
    results: tuple[str, ...]  # tool call ids it returns results for, in order, repeats kept
    contents: tuple[bytes, ...]  # for each of those, a digest of the result's content (canonical JSON)
    thinking: tuple[tuple[str, bool, bytes], ...]  # per thinking block: bound-to hash, intact, blocks ahead of it
    empty: bool
    misplaced: bool  # Messages: a block of another kind ahead of a tool_result
    reasoning: str  # a Responses reasoning item's id


class _Head(NamedTuple):
    digest: bytes
    tokens: int
    marked: bool
    wire: dict[str, int]  # per head field, its bytes as sent


class _Sent(NamedTuple):
    """The previous request of a conversation, for the next one's chains and the rewrite numbers."""

    digests: list[bytes]
    chain: list[bytes]  # chain[i] covers the head and the first i items
    bares: list[bytes]  # the items' digests without their thinking
    bare_chain: list[bytes]  # the chain over those
    totals: list[int]  # totals[i]: tokens of the head and the first i items
    plain: list[bytes]  # the items' digests without their thinking, reasoning items left out


class Reply(NamedTuple):
    output: list[dict]  # Messages: the assistant message's content. Responses: the output items
    usage: dict  # in the API's own shape
    tool_calls: bool
    call_ids: tuple[str, ...]
    input_tokens: int
    cache_read: int
    wire_bytes: int
    first_changed: int | None  # a prefix rewrite: the index of the first item that differs from the previous request
    invalidated: int | None  # a prefix rewrite: the previous request's tokens from that item to its end
    resent: int | None  # a prefix rewrite: uncached tokens beyond the items new since the previous request
    latest_missing: bool  # the newest response of the turn in flight arrived without its thinking
    violations: dict[str, int]  # this request's, by kind
    items: list[Item]  # the request's items as read

    @property
    def rewrite(self) -> bool:
        return self.first_changed is not None


class _Emitted(NamedTuple):
    call_ids: tuple[str, ...]
    thinking: str  # Messages: "1" when it was emitted with thinking. Responses: the reasoning item's id
    bare: bytes  # Messages: the assistant message without its thinking. Responses: the message item, if any


class FakeUpstream:
    def __init__(self) -> None:
        self.violations: Counter[str] = Counter()  # by kind: the requests that showed it
        self.info: Counter[str] = Counter()
        self.requests = 0
        self.rejected = 0  # requests with at least one violation
        self._items: dict[tuple[bool, str], Item] = {}  # by dialect and the item's JSON: content, never identity
        self._pinned: dict[int, tuple[dict, Item]] = {}  # the CLI's own items, by identity (see pin)
        self._pinned_heads: dict[tuple, tuple[list, _Head]] = {}  # and its head values (see pin_head)
        self._sent: dict[str, _Sent] = {}
        self._cached: dict[str, set[bytes]] = {}  # per conversation: a hash of each prefix it has sent
        self._emitted: dict[str, list[_Emitted]] = {}  # per conversation: every response, in order
        self._loop_start: Counter[str] = Counter()  # per conversation: the first response of the turn in flight
        self._covered: dict[str, set[int]] = {}  # per conversation: the responses a prefix rewrite covers
        self._reasoning: dict[str, tuple[bytes, tuple[str, Any] | None]] = {}  # reasoning id -> as emitted, follower

    # What one item is ---------------------------------------------------------------------------

    def message(self, message: dict) -> Item:
        raw = dumps(message)
        known = self._items.get((True, raw))
        if known is not None:
            return known
        marked = '"cache_control"' in raw
        plain = without_cache_control(message) if marked else message
        content = plain.get("content")
        if isinstance(content, str):  # the API reads a string as one text block
            plain = {**plain, "content": [{"type": "text", "text": content}]}
        every = plain.get("content") if isinstance(plain.get("content"), list) else []
        blocks = [b for b in every if isinstance(b, dict)]
        full = _sha(dumps(plain).encode())
        thinking: list[tuple[str, bool, bytes]] = []
        ahead: list = []  # the blocks that are not thinking, in order
        for block in every:
            if isinstance(block, dict) and block.get("type") in THINKING_TYPES:
                thinking.append((*self._signed(block), dumps(ahead).encode()))
            else:
                ahead.append(block)
        bare = _sha(dumps({**plain, "content": ahead}).encode()) if thinking else full
        role = plain.get("role") if isinstance(plain.get("role"), str) else ""
        empty = role in ("user", "assistant") and (not plain.get("content") or any(
            b.get("type") == "text" and not str(b.get("text") or "").strip() for b in blocks))
        returned = [b for b in blocks if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)]
        kinds = [b.get("type") for b in blocks]
        last = max((i for i, kind in enumerate(kinds) if kind == "tool_result"), default=-1)
        item = Item(role, "", full, bare, message_tokens(plain), len(raw.encode()), marked,
                    tuple(b["id"] for b in blocks if b.get("type") == "tool_use" and isinstance(b.get("id"), str)),
                    tuple(b["tool_use_id"] for b in returned),
                    tuple(_sha(canonical(b.get("content")).encode()) for b in returned),
                    tuple(thinking), empty, any(kind != "tool_result" for kind in kinds[:last + 1]), "")
        self._items[True, raw] = item
        return item

    @staticmethod
    def _signed(block: dict) -> tuple[str, bool]:
        signature, thinking = block.get("signature"), block.get("thinking")
        if not (isinstance(signature, str) and len(signature) == 128 and isinstance(thinking, str)):
            return "", False
        binding = signature[:64]
        try:
            intact = hmac.compare_digest(signature[64:], _mac(bytes.fromhex(binding), thinking))
        except ValueError:
            intact = False
        return binding, intact

    def item(self, item: dict) -> Item:
        raw = dumps(item)
        known = self._items.get((False, raw))
        if known is not None:
            return known
        marked = '"cache_control"' in raw
        plain = without_cache_control(item) if marked else item
        full = _sha(dumps(plain).encode() if marked else raw.encode())
        kind = plain.get("type", "message")
        kind = kind if isinstance(kind, str) else ""
        call_id = plain.get("call_id", plain.get("id"))
        call_id = call_id if isinstance(call_id, str) else ""
        uses: tuple[str, ...] = ()
        results: tuple[str, ...] = ()
        contents: tuple[bytes, ...] = ()
        reasoning = ""
        if kind == "message":
            role = plain.get("role") if plain.get("role") in ("user", "assistant") else "other"
        elif kind == "reasoning":
            role, reasoning = "reasoning", str(plain.get("id") or "")
        elif kind.endswith("_output"):
            role, results, contents = "output", (call_id,), (_sha(canonical(plain.get("output")).encode()),)
        elif kind in HOSTED_CALL_TYPES or (kind == "tool_search_call" and plain.get("execution") != "client"):
            role = "hosted"
        elif kind.endswith("_call"):
            role, uses = "call", (call_id,)
        else:
            role = "other"
        found = Item(role, kind, full, full, item_tokens(plain), len(raw.encode()), marked, uses, results,
                     contents, (), False, False, reasoning)
        self._items[False, raw] = found
        return found

    def read(self, claude: bool, item: dict) -> Item:
        pinned = self._pinned.get(id(item))
        if pinned is not None and pinned[0] is item:
            return pinned[1]
        return self.message(item) if claude else self.item(item)

    def pin(self, dialect: str, item: dict) -> Item:
        """Read one of the CLI's own history items once and for all. Only for objects the caller checks
        for changes itself: a pinned object edited in place would be judged as it was."""
        found = self.read(dialect == "claude", item)
        self._pinned[id(item)] = (item, found)
        return found

    def tokens(self, dialect: str, item: dict) -> int:
        """One of the CLI's own messages or items, by this tokenizer."""
        return self.read(dialect == "claude", item).tokens

    def results(self, dialect: str, item: Any) -> list[tuple[str, bytes]]:
        """The tool results ``item`` holds: (call id, digest of the result's content). Two results are
        the same byte for byte exactly when their digests are equal."""
        if not isinstance(item, dict):
            return []
        found = self.read(dialect == "claude", item)
        return list(zip(found.results, found.contents))

    def head_tokens(self, endpoint: str, request: dict) -> int:
        return self._head(request, endpoint == cli.MESSAGES).tokens

    def request_tokens(self, endpoint: str, request: dict) -> int:
        """A whole request by this tokenizer: its head and every item. Nothing is checked or recorded."""
        claude = endpoint == cli.MESSAGES
        items = request.get("messages" if claude else "input") or []
        return self._head(request, claude).tokens + sum(self.read(claude, i).tokens for i in items)

    def _head(self, request: dict, claude: bool, pin: bool = False) -> _Head:
        fields = ("tools", "system") if claude else ("instructions", "tools")
        values = [request.get(f) for f in fields]
        key = tuple(id(v) for v in values)
        pinned = self._pinned_heads.get(key)
        if pinned is not None and all(a is b for a, b in zip(pinned[0], values)):
            return pinned[1]
        raw = {f: dumps(v) for f, v in zip(fields, values)}
        plain = [without_cache_control(v) for v in values]
        head = _Head(_sha(dumps(dict(zip(fields, plain))).encode()), sum(value_tokens(v) for v in plain),
                     any('"cache_control"' in text for text in raw.values()),
                     {f: len(text.encode()) for f, text in raw.items() if f in request})
        if pin:
            self._pinned_heads[key] = (values, head)
        return head

    def pin_head(self, endpoint: str, request: dict) -> None:
        """Read the CLI's own head values once, on the same condition as ``pin``."""
        self._head(request, endpoint == cli.MESSAGES, pin=True)

    @staticmethod
    def _wire(request: dict, field: str, head: _Head, items: list[Item]) -> int:
        """The request's size as the proxy would write it: its parts are already serialized."""
        shell = {name: [] if name == field else None if name in head.wire else value
                 for name, value in request.items()}
        return (len(dumps(shell).encode()) + sum(size - 4 for size in head.wire.values())  # in place of "null"
                + sum(i.wire for i in items) + max(0, len(items) - 1))

    # One request --------------------------------------------------------------------------------

    def respond(self, conversation: str, endpoint: str, request: dict, step: Step,
                appended: Sequence[dict] = ()) -> Reply:
        """Check and price ``request``, then answer with ``step``. ``conversation`` names the
        conversation the harness is sending for; the upstream uses it only for its own bookkeeping
        (the previous request, and the turn in flight). ``appended`` are the items the CLI added to
        its own history since that conversation's previous request, for the re-sent count."""
        claude = endpoint == cli.MESSAGES
        field = "messages" if claude else "input"
        sent = request.get(field) or []
        items = [self.read(claude, i) for i in sent if isinstance(i, dict)]
        head = self._head(request, claude)
        previous = self._sent.get(conversation)
        digests = [i.digest for i in items]
        bares = [i.bare for i in items]
        chain = self._chain(head.digest, digests, previous and (previous.digests, previous.chain))
        bare = self._chain(head.digest, bares, previous and (previous.bares, previous.bare_chain))
        totals = list(accumulate((i.tokens for i in items), initial=head.tokens))

        # A prefix rewrite: the head or an item differs from the previous request where both have one.
        first = invalidated = None
        if previous is not None:
            if previous.chain[0] != chain[0]:
                first, invalidated = 0, previous.totals[-1]  # the head changed: nothing of it can be read
            else:
                shared = min(len(digests), len(previous.digests))
                first = next((i for i in range(shared) if digests[i] != previous.digests[i]), None)
                if first is None and len(digests) < len(previous.digests):
                    first = len(digests)  # the previous request's tail was removed
            if first is not None and invalidated is None:
                invalidated = previous.totals[-1] - previous.totals[first]

        found: Counter[str] = Counter()
        if len(items) != len(sent):
            found["not_an_object"] = len(sent) - len(items)
        emitted = self._emitted.setdefault(conversation, [])
        where = self._where(claude, items, emitted)
        covered = self._covered.setdefault(conversation, set())
        # Where the request rewrites content. Thinking removed from a message, or a reasoning item
        # dropped, is not content: the two requests are compared without them.
        plain = [(i, item.bare) for i, item in enumerate(items) if item.role != "reasoning"]
        content = None
        if first == 0 and previous.chain[0] != chain[0]:
            content = 0
        elif first is not None:
            shared = min(len(plain), len(previous.plain))
            content = next((plain[j][0] for j in range(shared) if plain[j][1] != previous.plain[j]),
                           len(items) if len(plain) < len(previous.plain) else None)
        if content is not None:
            covered.update(e for e, (index, _) in enumerate(where) if index is None or index >= content)
        outside = sum(1 for e, (_, kept) in enumerate(where) if emitted[e].thinking and not kept and e not in covered)
        if claude:
            self._check_messages(items, bare, found)
            found["thinking_removed_outside_rewrite"] = outside
        else:
            self._check_responses(items, found)
            found["reasoning_removed_outside_rewrite"] = outside
        for kind in [k for k, count in found.items() if not count]:
            del found[kind]
        latest_missing = self._note(claude, conversation, emitted, where)
        self.requests += 1
        self.rejected += bool(found)
        for kind in found:
            self.violations[kind] += 1

        # The cache: every prefix an earlier request of the conversation began with can be read.
        cached = self._cached.setdefault(conversation, set())
        low, high = -1, len(chain) - 1  # the largest i with chain[i] cached; the set is prefix-closed
        while low < high:
            middle = (low + high + 1) // 2
            if chain[middle] in cached:
                low = middle
            else:
                high = middle - 1
        cache_read = totals[low] if low >= 0 else 0
        cached.update(chain[low + 1:])
        total = totals[-1]
        resent = None
        if first is not None:
            resent = max(0, total - cache_read - self._new_tokens(claude, items, appended))
        self._sent[conversation] = _Sent(digests, chain, bares, bare, totals, [b for _, b in plain])

        n = len(emitted)
        if claude:
            output = self._messages_reply(step, bare[-1])
            answer = _Emitted(tuple(b["id"] for b in output if b["type"] == "tool_use"), "1" if step.thinking else "",
                              self.message({"role": "assistant", "content": output}).bare)
            marks = [i for i, item in enumerate(items) if item.marked]
            written = totals[marks[-1] + 1] if marks else (totals[0] if head.marked else 0)
            creation = max(0, written - cache_read)
            usage = {"input_tokens": total - cache_read - creation, "cache_read_input_tokens": cache_read,
                     "cache_creation_input_tokens": creation, "output_tokens": sum(block_tokens(b) for b in output)}
        else:
            output = self._responses_reply(step, conversation, n)
            text = next((self.item(i).digest for i in output if i["type"] == "message"), b"")
            answer = _Emitted(tuple(i["call_id"] for i in output if i["type"].endswith("_call")), output[0]["id"], text)
            out = sum(item_tokens(i) for i in output)
            usage = {"input_tokens": total, "input_tokens_details": {"cached_tokens": cache_read},
                     "output_tokens": out, "output_tokens_details": {"reasoning_tokens": item_tokens(output[0])},
                     "total_tokens": total + out}
        emitted.append(answer)
        if not answer.call_ids:
            self._loop_start[conversation] = len(emitted)  # the turn is over
        return Reply(output, usage, bool(answer.call_ids), answer.call_ids, total, cache_read,
                     self._wire(request, field, head, items), first, invalidated, resent, latest_missing,
                     dict(found), items)

    @staticmethod
    def _chain(seed: bytes, digests: list[bytes], previous: tuple[list[bytes], list[bytes]] | None) -> list[bytes]:
        """chain[i] is a hash of the head and the first i items. The hashes of the part shared with
        the conversation's previous request (its digests and chain) are reused."""
        chain = [_sha(seed)]
        if previous is not None and previous[1][0] == chain[0]:
            old, old_chain = previous
            i, limit = 0, min(len(digests), len(old))
            while i < limit and digests[i] == old[i]:
                i += 1
            chain = old_chain[: i + 1]
        for item in digests[len(chain) - 1:]:
            chain.append(_sha(chain[-1] + item))
        return chain

    def _new_tokens(self, claude: bool, items: list[Item], appended: Sequence[dict]) -> int:
        """Tokens of the items the CLI added since its previous request, each as the engine forwarded it
        when that is smaller. An added item is found again by its content, else by the tool call it
        makes or answers; one that cannot be found counts as 0."""
        by_digest = {item.digest: item for item in items}
        by_bare = {item.bare: item for item in items}
        by_use = {use: item for item in items for use in item.uses}
        by_result = {result: item for item in items for result in item.results}
        total = 0
        for raw in appended:
            own = self.read(claude, raw)
            match = (by_digest.get(own.digest) or by_bare.get(own.bare)
                     or (by_use.get(own.uses[0]) if own.uses else None)
                     or (by_result.get(own.results[0]) if own.results else None))
            if match is not None:
                total += min(own.tokens, match.tokens)
        return total

    # Where the model's responses are in a request -------------------------------------------------

    @staticmethod
    def _where(claude: bool, items: list[Item], emitted: list[_Emitted]) -> list[tuple[int | None, bool]]:
        """For each response the model has emitted in this conversation, in order: the index of the
        item that carries it in this request (None when it is gone), and whether its thinking is there."""
        by_use: dict[str, int] = {}
        by_content: dict[bytes, int] = {}
        by_reasoning: dict[str, int] = {}
        for i, item in enumerate(items):
            if item.role in ("assistant", "call"):
                for use in item.uses:
                    by_use.setdefault(use, i)
            if item.role == "assistant":
                by_content.setdefault(item.bare, i)
            if item.reasoning:
                by_reasoning.setdefault(item.reasoning, i)
        out: list[tuple[int | None, bool]] = []
        for response in emitted:
            index = by_use.get(response.call_ids[0]) if response.call_ids else by_content.get(response.bare)
            if claude:
                out.append((index, index is not None and bool(items[index].thinking)))
            else:
                reasoning = by_reasoning.get(response.thinking)
                out.append((index if reasoning is None else reasoning, reasoning is not None))
        return out

    def _note(self, claude: bool, conversation: str, emitted: list[_Emitted],
              where: list[tuple[int | None, bool]]) -> bool:
        """Count the information kinds for one request. Returns whether the newest response of the turn
        in flight arrived without its thinking."""
        names = (("inflight_thinking_missing", "inflight_assistant_absent", "thinking_kept_around_strip") if claude
                 else ("inflight_reasoning_dropped", "inflight_calls_absent", "reasoning_kept_around_drop"))
        thought = [kept for response, (_, kept) in zip(emitted, where) if response.thinking]
        kept = [i for i, there in enumerate(thought) if there]
        self.info[names[2]] += bool(kept) and not all(thought[kept[0]:kept[-1] + 1])
        start = self._loop_start[conversation]
        inflight = list(zip(emitted, where))[start:]
        missing = [response for response, (index, there) in inflight
                   if response.thinking and index is not None and not there]
        latest = bool(inflight) and inflight[-1][0] in missing
        if inflight:
            self.info[names[0]] += bool(missing)
            self.info[names[0] + "_latest"] += latest
            self.info[names[1]] += any(index is None for _, (index, _) in inflight)
        return latest

    # The rules ----------------------------------------------------------------------------------

    @staticmethod
    def _check_messages(items: list[Item], bare: list[bytes], found: Counter[str]) -> None:
        user_seen = False
        used: set[str] = set()
        answered: set[str] = set()
        opening = next((item for item in items if item.role != "system"), None)
        if opening is not None and opening.role != "user":
            found["first_message_not_user"] += 1
        for i, item in enumerate(items):
            if item.role == "system" and not user_seen:
                found["system_before_first_user"] += 1
            user_seen = user_seen or item.role == "user"
            if item.empty:
                found["empty_content"] += 1
            if item.role == "user" and item.misplaced:
                found["text_before_tool_result"] += 1
            if item.uses:
                after = items[i + 1] if i + 1 < len(items) else None
                returned = set(after.results) if after is not None and after.role == "user" else set()
                found["tool_use_without_result"] += sum(1 for use in item.uses if use not in returned)
                found["duplicate_tool_use"] += sum(1 for n, use in enumerate(item.uses)
                                                   if use in used or use in item.uses[:n])
                used.update(item.uses)
            if item.results:
                before = items[i - 1] if i else None
                made = set(before.uses) if before is not None and before.role == "assistant" else set()
                found["tool_result_without_use"] += sum(1 for result in item.results if result not in made)
                found["duplicate_tool_result"] += sum(1 for n, result in enumerate(item.results)
                                                      if result in answered or result in item.results[:n])
                answered.update(item.results)
            for binding, intact, ahead in item.thinking:
                if not intact:
                    found["thinking_modified"] += 1
                elif binding != _binding(bare[i], ahead).hex():
                    found["thinking_binding"] += 1

    def _check_responses(self, items: list[Item], found: Counter[str]) -> None:
        outputs: dict[str, int] = {}
        kinds: dict[str, str] = {}
        for i, item in enumerate(items):
            if item.role == "call":
                if item.uses[0] in outputs:
                    found["duplicate_call"] += 1
                else:
                    outputs[item.uses[0]] = 0
                    kinds[item.uses[0]] = item.kind
            elif item.role == "output":
                if item.results[0] in outputs:
                    outputs[item.results[0]] += 1
                    if item.kind != kinds[item.results[0]] + "_output":
                        found["output_kind_mismatch"] += 1
                else:
                    found["output_without_call"] += 1
            elif item.role == "reasoning":
                after = items[i + 1] if i + 1 < len(items) else None
                digest_, follower = self._reasoning.get(item.reasoning, (None, None))
                if digest_ != item.digest:
                    found["reasoning_modified"] += 1
                    follower = None
                if follower is None:  # not one of ours as emitted: any model item but a reasoning item will do
                    followed = after is not None and after.role in MODEL_ROLES
                elif follower[0] == "call":
                    followed = after is not None and after.role == "call" and after.uses[0] == follower[1]
                else:
                    followed = after is not None and after.digest == follower[1]
                if not followed:
                    found["reasoning_without_following"] += 1
        found["call_without_output"] += sum(1 for count in outputs.values() if count == 0)
        found["call_with_several_outputs"] += sum(1 for count in outputs.values() if count > 1)

    # The answer ---------------------------------------------------------------------------------

    @staticmethod
    def _messages_reply(step: Step, prefix: bytes) -> list[dict]:
        content: list[dict] = []
        if step.thinking:  # first in the message: no block is ahead of it
            content.append({"type": "thinking", "thinking": step.thinking,
                            "signature": sign(_binding(prefix, NO_BLOCKS), step.thinking)})
        if step.text:
            content.append({"type": "text", "text": step.text})
        content.extend(cli.claude_tool_use(call) for call in step.calls)
        return content

    def _responses_reply(self, step: Step, conversation: str, n: int) -> list[dict]:
        opaque = hashlib.shake_256(f"{conversation}:{n}:{step.thinking}".encode()).digest(len(step.thinking))
        reasoning = {"type": "reasoning", "id": "rs_" + digest("reasoning", conversation, n)[:32], "summary": [],
                     "encrypted_content": base64.b64encode(opaque).decode()}
        output = [reasoning]
        if step.text:
            output.append({"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": step.text}]})
        output.extend(cli.codex_call(call) for call in step.calls)
        follower = None
        if len(output) > 1:
            after = output[1]
            follower = (("call", after["call_id"]) if after["type"].endswith("_call")
                        else ("message", self.item(after).digest))
        self._reasoning[reasoning["id"]] = (self.item(reasoning).digest, follower)
        return output
