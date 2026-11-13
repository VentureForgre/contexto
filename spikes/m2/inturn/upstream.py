"""The fake upstream: it receives exactly what the engine forwards, checks it the way an enforcing
account would, reports usage with its own tokenizer and a prompt-cache model, and answers with the
script's next step. It never raises on a bad request: each violation is counted by kind.

What it enforces, and where each rule is recorded:

Messages API (``docs/research/M0-interception.md``, "Constraints"; contract rule 1):

- ``tool_use_without_result``: a ``tool_use`` whose ``tool_result`` is not in the next message.
- ``tool_result_without_use``: a ``tool_result`` whose ``tool_use`` is not in the assistant message
  right before it.
- ``empty_content``: a user or assistant message with no content, or a text block with no text.
- ``system_before_first_user``: a role ``system`` message before the first user message (measured 400).
- ``thinking_binding``: a thinking block whose prefix changed. On 5.x models "nothing before the
  thinking block" may change: ``system``, ``tools`` and the earlier messages. Removing thinking blocks
  and appending are valid. A signature here is a keyed hash of the request the block answered
  (``system``, ``tools``, messages; ``cache_control`` and thinking blocks left out), so the same hash
  taken over what precedes the block's message in a later request must match.
- ``thinking_modified``: a thinking block whose text or signature is not what was emitted.

Responses API (same note, "Call pairing" and "Reasoning items"):

- ``call_without_output``, ``call_with_several_outputs``: a client ``*_call`` must have exactly one
  ``*_output`` with its ``call_id`` after it. Hosted calls (``web_search_call`` and the like) have none.
- ``output_without_call``: an ``*_output`` with no call of that ``call_id`` before it.
- ``reasoning_without_following``: a ``reasoning`` item not followed by another model item (documented).
- ``reasoning_modified``: a ``reasoning`` item whose content is not what was emitted (inferred).

Both: ``not_an_object``, a message or item that is not a JSON object.

Counted as information, not as violations (``info``): requests inside a tool loop where the in-flight
turn's thinking did not come back. The documented consequence on the Messages API is that thinking is
disabled for the rest of the turn, without an error (``docs/research/M0-paging-policy.md``).

- ``inflight_thinking_missing`` (Responses: ``inflight_reasoning_dropped``): at least one assistant
  message (call) of the in-flight turn arrived without the thinking block (reasoning item) it was
  emitted with.
- ``..._latest``: the newest one, whose results the request returns, is among them.
- ``inflight_assistant_absent`` (``inflight_calls_absent``): at least one did not arrive at all.

The prompt cache: ``cache_read`` is the longest prefix (the head, then item by item, on the JSON with
``cache_control`` ignored and a string ``content`` read as one text block) that an earlier request of
the same conversation began with. There is no minimum length, no expiry and no limit on breakpoints.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections import Counter
from itertools import accumulate
from typing import Any, NamedTuple

from inturn import cli
from inturn.scripts import Step, digest
from inturn.tokenizer import block_tokens, dumps, item_tokens, message_tokens, value_tokens

KEY = b"inturn fake upstream: not a secret"
THINKING_TYPES = ("thinking", "redacted_thinking")
HOSTED_CALL_TYPES = frozenset({"web_search_call", "file_search_call", "image_generation_call",
                               "code_interpreter_call", "mcp_call"})
MODEL_ROLES = ("assistant", "reasoning", "call", "hosted")
MESSAGES_KINDS = ("tool_use_without_result", "tool_result_without_use", "empty_content",
                  "system_before_first_user", "thinking_binding", "thinking_modified", "not_an_object")
RESPONSES_KINDS = ("call_without_output", "call_with_several_outputs", "output_without_call",
                   "reasoning_without_following", "reasoning_modified", "not_an_object")


def _sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _mac(binding: bytes, thinking: str) -> str:
    return hmac.new(KEY, binding + thinking.encode(), hashlib.sha256).hexdigest()


def sign(binding: bytes, thinking: str) -> str:
    """A thinking block's signature: the hash of the prefix it is bound to, then a keyed hash of that
    and the thinking text."""
    return binding.hex() + _mac(binding, thinking)


def without_cache_control(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: without_cache_control(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [without_cache_control(v) for v in value]
    return value


class _Item(NamedTuple):
    """What the upstream needs of one message or item, worked out once per object."""

    ref: Any  # the object, held so its id() stays its own
    role: str  # Messages: the role. Responses: user, assistant, reasoning, call, hosted, output or other
    digest: bytes  # of its JSON, cache_control ignored
    bare: bytes  # the same with thinking blocks removed
    tokens: int
    wire: int  # bytes as sent
    marked: bool  # carries a cache breakpoint
    uses: tuple[str, ...]  # tool call ids it makes
    results: tuple[str, ...]  # tool call ids it returns results for
    thinking: tuple[tuple[str, bool], ...]  # per thinking block: (the prefix hash it is bound to, intact)
    empty: bool
    reasoning: str  # a Responses reasoning item's id


class _Head(NamedTuple):
    refs: tuple
    digest: bytes
    tokens: int
    marked: bool


class _Sent(NamedTuple):
    """The previous request of a conversation, for the next one's chains and the re-sent count."""

    digests: list[bytes]
    chain: list[bytes]  # chain[i] covers the head and the first i items
    bares: list[bytes]  # the items' digests without their thinking
    bare_chain: list[bytes]  # the chain over those
    totals: list[int]  # totals[i]: tokens of the head and the first i items


class Reply(NamedTuple):
    output: list[dict]  # Messages: the assistant message's content. Responses: the output items
    usage: dict  # in the API's own shape
    tool_calls: bool
    call_ids: tuple[str, ...]
    input_tokens: int
    cache_read: int
    wire_bytes: int
    resent: int | None  # tokens of the previous request from its first changed item on; None for an append
    violations: dict[str, int]  # this request's, by kind


class _Emitted(NamedTuple):
    call_ids: tuple[str, ...]
    thinking: str  # Messages: "1" when it was emitted with thinking. Responses: the reasoning item's id


class FakeUpstream:
    def __init__(self) -> None:
        self.violations: Counter[str] = Counter()  # by kind: the requests that showed it
        self.info: Counter[str] = Counter()
        self.requests = 0
        self.rejected = 0  # requests with at least one violation
        self._items: dict[int, _Item] = {}
        self._heads: dict[tuple, _Head] = {}
        self._sizes: dict[int, tuple[Any, int]] = {}
        self._cached: dict[str, set[bytes]] = {}  # per conversation: a hash of each prefix it has sent
        self._sent: dict[str, _Sent] = {}
        self._inflight: dict[str, list[_Emitted]] = {}
        self._reasoning: dict[str, bytes] = {}  # reasoning id -> digest of the item as emitted
        self._replies: Counter[str] = Counter()

    # What one item is ---------------------------------------------------------------------------

    def message(self, message: dict) -> _Item:
        known = self._items.get(id(message))
        if known is not None and known.ref is message:
            return known
        raw = dumps(message)
        marked = '"cache_control"' in raw
        plain = without_cache_control(message) if marked else message
        content = plain.get("content")
        if isinstance(content, str):  # the API reads a string as one text block
            plain = {**plain, "content": [{"type": "text", "text": content}]}
        blocks = [b for b in plain.get("content") or [] if isinstance(b, dict)]
        data = dumps(plain).encode()
        full = _sha(data)
        thinking = [b for b in blocks if b.get("type") in THINKING_TYPES]
        bare = full
        if thinking:
            kept = [b for b in plain["content"] if not (isinstance(b, dict) and b.get("type") in THINKING_TYPES)]
            bare = _sha(dumps({**plain, "content": kept}).encode())
        role = plain.get("role") if isinstance(plain.get("role"), str) else ""
        empty = role in ("user", "assistant") and (not plain.get("content") or any(
            b.get("type") == "text" and not str(b.get("text") or "").strip() for b in blocks))
        item = _Item(message, role, full, bare, message_tokens(plain), len(raw.encode()), marked,
                     tuple(b["id"] for b in blocks if b.get("type") == "tool_use" and isinstance(b.get("id"), str)),
                     tuple(b["tool_use_id"] for b in blocks
                           if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)),
                     tuple(self._signed(b) for b in thinking), empty, "")
        self._items[id(message)] = item
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

    def item(self, item: dict) -> _Item:
        known = self._items.get(id(item))
        if known is not None and known.ref is item:
            return known
        raw = dumps(item)
        marked = '"cache_control"' in raw
        plain = without_cache_control(item) if marked else item
        full = _sha(dumps(plain).encode() if marked else raw.encode())
        kind = plain.get("type", "message")
        kind = kind if isinstance(kind, str) else ""
        call_id = plain.get("call_id", plain.get("id"))
        call_id = call_id if isinstance(call_id, str) else ""
        uses: tuple[str, ...] = ()
        results: tuple[str, ...] = ()
        reasoning = ""
        if kind == "message":
            role = plain.get("role") if plain.get("role") in ("user", "assistant") else "other"
        elif kind == "reasoning":
            role, reasoning = "reasoning", str(plain.get("id") or "")
        elif kind.endswith("_output"):
            role, results = "output", (call_id,)
        elif kind in HOSTED_CALL_TYPES or (kind == "tool_search_call" and plain.get("execution") != "client"):
            role = "hosted"
        elif kind.endswith("_call"):
            role, uses = "call", (call_id,)
        else:
            role = "other"
        found = _Item(item, role, full, full, item_tokens(plain), len(raw.encode()), marked, uses, results, (),
                      False, reasoning)
        self._items[id(item)] = found
        return found

    def tokens(self, dialect: str, item: dict) -> int:
        """One of the CLI's own messages or items, by this tokenizer."""
        return (self.message(item) if dialect == "claude" else self.item(item)).tokens

    def request_tokens(self, endpoint: str, request: dict) -> int:
        """A whole request by this tokenizer: its head and every item. Nothing is checked or recorded."""
        claude = endpoint == cli.MESSAGES
        head = self._head(request, ("tools", "system") if claude else ("instructions", "tools"))
        items = request.get("messages" if claude else "input") or []
        return head.tokens + sum((self.message(i) if claude else self.item(i)).tokens for i in items)

    def _head(self, request: dict, fields: tuple[str, ...]) -> _Head:
        values = tuple(request.get(f) for f in fields)
        key = tuple(id(v) for v in values)
        known = self._heads.get(key)
        if known is not None and all(a is b for a, b in zip(known.refs, values)):
            return known
        plain = [without_cache_control(v) for v in values]
        head = _Head(values, _sha(dumps(dict(zip(fields, plain))).encode()),
                     sum(value_tokens(v) for v in plain), '"cache_control"' in dumps(values))
        self._heads[key] = head
        return head

    def _size(self, value: Any) -> int:
        known = self._sizes.get(id(value))
        if known is None or known[0] is not value:
            known = self._sizes[id(value)] = (value, len(dumps(value).encode()))
        return known[1]

    def _wire(self, request: dict, field: str, items: list[_Item]) -> int:
        """The request's size as the proxy would write it, without serializing it again."""
        shell: dict[str, Any] = {}
        total = 0
        for name, value in request.items():
            if name == field:
                shell[name] = []
            elif isinstance(value, (list, dict)) or (isinstance(value, str) and len(value) > 256):
                shell[name] = None
                total += self._size(value) - 4  # in place of "null"
            else:
                shell[name] = value
        return total + len(dumps(shell).encode()) + sum(i.wire for i in items) + max(0, len(items) - 1)

    # One request --------------------------------------------------------------------------------

    def respond(self, conversation: str, endpoint: str, request: dict, step: Step) -> Reply:
        """Check and price ``request``, then answer with ``step``. ``conversation`` names the
        conversation the harness is sending for; the upstream uses it only for its own bookkeeping
        (the previous request, and the turn in flight)."""
        claude = endpoint == cli.MESSAGES
        field = "messages" if claude else "input"
        sent = request.get(field) or []
        items = [self.message(i) if claude else self.item(i) for i in sent if isinstance(i, dict)]
        head = self._head(request, ("tools", "system") if claude else ("instructions", "tools"))
        previous = self._sent.get(conversation)
        digests = [i.digest for i in items]
        bares = [i.bare for i in items]
        chain = self._chain(head.digest, digests, previous and (previous.digests, previous.chain))
        bare = self._chain(head.digest, bares, previous and (previous.bares, previous.bare_chain))
        totals = list(accumulate((i.tokens for i in items), initial=head.tokens))

        found: Counter[str] = Counter()
        if len(items) != len(sent):
            found["not_an_object"] = len(sent) - len(items)
        inflight = self._inflight.get(conversation, [])
        if claude:
            self._check_messages(items, bare, found)
            self._note_messages(items, inflight)
        else:
            self._check_responses(items, found)
            self._note_responses(items, inflight)
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
        if previous is not None:
            if previous.chain[0] != chain[0]:
                resent = previous.totals[-1]
            else:
                shared = min(len(digests), len(previous.digests))
                first = next((i for i in range(shared) if digests[i] != previous.digests[i]), None)
                if first is not None:
                    resent = previous.totals[-1] - previous.totals[first]
        self._sent[conversation] = _Sent(digests, chain, bares, bare, totals)

        n = self._replies[conversation]
        self._replies[conversation] += 1
        if claude:
            output = self._messages_reply(step, bare[-1])
            emitted = _Emitted(tuple(b["id"] for b in output if b["type"] == "tool_use"), "1" if step.thinking else "")
            marks = [i for i, item in enumerate(items) if item.marked]
            written = totals[marks[-1] + 1] if marks else (totals[0] if head.marked else 0)
            creation = max(0, written - cache_read)
            usage = {"input_tokens": total - cache_read - creation, "cache_read_input_tokens": cache_read,
                     "cache_creation_input_tokens": creation, "output_tokens": sum(block_tokens(b) for b in output)}
        else:
            output = self._responses_reply(step, conversation, n)
            emitted = _Emitted(tuple(i["call_id"] for i in output if i["type"].endswith("_call")), output[0]["id"])
            out = sum(item_tokens(i) for i in output)
            usage = {"input_tokens": total, "input_tokens_details": {"cached_tokens": cache_read},
                     "output_tokens": out, "output_tokens_details": {"reasoning_tokens": item_tokens(output[0])},
                     "total_tokens": total + out}
        if emitted.call_ids:
            self._inflight.setdefault(conversation, []).append(emitted)
        else:
            self._inflight[conversation] = []  # the turn is over
        return Reply(output, usage, bool(emitted.call_ids), emitted.call_ids, total, cache_read,
                     self._wire(request, field, items), resent, dict(found))

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

    # The rules ----------------------------------------------------------------------------------

    @staticmethod
    def _check_messages(items: list[_Item], bare: list[bytes], found: Counter[str]) -> None:
        user_seen = False
        for i, item in enumerate(items):
            if item.role == "system" and not user_seen:
                found["system_before_first_user"] += 1
            user_seen = user_seen or item.role == "user"
            if item.empty:
                found["empty_content"] += 1
            if item.uses:
                after = items[i + 1] if i + 1 < len(items) else None
                returned = set(after.results) if after is not None and after.role == "user" else set()
                found["tool_use_without_result"] += sum(1 for use in item.uses if use not in returned)
            if item.results:
                before = items[i - 1] if i else None
                made = set(before.uses) if before is not None and before.role == "assistant" else set()
                found["tool_result_without_use"] += sum(1 for result in item.results if result not in made)
            for binding, intact in item.thinking:
                if not intact:
                    found["thinking_modified"] += 1
                elif binding != bare[i].hex():
                    found["thinking_binding"] += 1
        for kind in [k for k, count in found.items() if not count]:
            del found[kind]

    def _check_responses(self, items: list[_Item], found: Counter[str]) -> None:
        outputs: dict[str, int] = {}
        for i, item in enumerate(items):
            if item.role == "call":
                outputs.setdefault(item.uses[0], 0)
            elif item.role == "output":
                if item.results[0] in outputs:
                    outputs[item.results[0]] += 1
                else:
                    found["output_without_call"] += 1
            elif item.role == "reasoning":
                if i + 1 >= len(items) or items[i + 1].role not in MODEL_ROLES:
                    found["reasoning_without_following"] += 1
                if self._reasoning.get(item.reasoning) != item.digest:
                    found["reasoning_modified"] += 1
        missing = sum(1 for count in outputs.values() if count == 0)
        several = sum(1 for count in outputs.values() if count > 1)
        if missing:
            found["call_without_output"] += missing
        if several:
            found["call_with_several_outputs"] += several

    def _note_messages(self, items: list[_Item], inflight: list[_Emitted]) -> None:
        if not inflight:
            return
        by_call = {item.uses[0]: item for item in items if item.role == "assistant" and item.uses}
        missing = [e for e in inflight if e.thinking and e.call_ids[0] in by_call
                   and not by_call[e.call_ids[0]].thinking]
        self.info["inflight_thinking_missing"] += bool(missing)
        self.info["inflight_thinking_missing_latest"] += inflight[-1] in missing
        self.info["inflight_assistant_absent"] += any(e.call_ids[0] not in by_call for e in inflight)

    def _note_responses(self, items: list[_Item], inflight: list[_Emitted]) -> None:
        if not inflight:
            return
        calls = {item.uses[0] for item in items if item.role == "call"}
        reasoning = {item.reasoning for item in items if item.role == "reasoning"}
        dropped = [e for e in inflight if e.thinking not in reasoning and any(c in calls for c in e.call_ids)]
        self.info["inflight_reasoning_dropped"] += bool(dropped)
        self.info["inflight_reasoning_dropped_latest"] += inflight[-1] in dropped
        self.info["inflight_calls_absent"] += any(not any(c in calls for c in e.call_ids) for e in inflight)

    # The answer ---------------------------------------------------------------------------------

    @staticmethod
    def _messages_reply(step: Step, binding: bytes) -> list[dict]:
        content: list[dict] = []
        if step.thinking:
            content.append({"type": "thinking", "thinking": step.thinking,
                            "signature": sign(binding, step.thinking)})
        if step.text:
            content.append({"type": "text", "text": step.text})
        content.extend(cli.claude_tool_use(call) for call in step.calls)
        return content

    def _responses_reply(self, step: Step, conversation: str, n: int) -> list[dict]:
        opaque = hashlib.shake_256(f"{conversation}:{n}:{step.thinking}".encode()).digest(len(step.thinking))
        reasoning = {"type": "reasoning", "id": "rs_" + digest("reasoning", conversation, n)[:32], "summary": [],
                     "encrypted_content": base64.b64encode(opaque).decode()}
        self._reasoning[reasoning["id"]] = self.item(reasoning).digest
        output = [reasoning]
        if step.text:
            output.append({"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": step.text}]})
        output.extend(cli.codex_call(call) for call in step.calls)
        return output
