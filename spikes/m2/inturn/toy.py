"""A toy in-turn pager, for the harness's own tests. It is not the D63 design.

The tests need two things the real engine cannot give before D63 exists: an engine that meets every
target at once, to show the targets do not contradict each other, and engines that are wrong in one
way each, to show the target written for that way fails them. ``ToyPager`` is the first; its
``defect`` argument, and batch lines other than ``HIGH`` and ``LOW``, make the others.

What it does, per conversation:

- A tool result is replaced by a stub, and its round is written to the store as a page
  (``SessionStore.page_out``), in two cases. On arrival, when head, prompt and the request's new
  results come to ``targets.show_ceiling`` or more. Or in a batch: when the request would pass
  ``HIGH`` tokens, the oldest results not yet stubbed are stubbed until it is under ``LOW``; a batch
  never touches the results the request is the first to carry.
- A batch whose first stubbed item is at index ``p`` of the CLI's history strips thinking (Codex:
  drops reasoning items) from ``p`` to the end of that request, on that request and every later one.
- The main conversation publishes the retained snapshot and a room before the send and a room after
  the response; a subagent publishes a side room after each response with a tool call. Its ``request``
  event gives ``estimate``, the request as it would be without the batch, and ``sent_estimate``.
- A ``count_tokens`` request gets the current transform and changes nothing.

Where it cheats, and what it lacks: it sizes requests with the fake upstream's tokenizer, so its
estimate is exact; it never removes a round, so the stubs themselves add up (it cannot hold ``small``
at 1,500 rounds under the bound); it keeps no state across a restart, has no degraded mode, and does
not page at a turn start.
"""

from __future__ import annotations

import re
from typing import Any

from contexto.rewrite import COUNT_TOKENS, Config, dialect_for, retained_entries, usage_tokens
from inturn import cli
from inturn.stubs import StubRef
from inturn.tokenizer import block_tokens, item_tokens
from inturn.upstream import THINKING_TYPES, FakeUpstream

HIGH = 100_000  # a request that would pass this many tokens is batched first
LOW = 38_000  # ... down to this: head, prompt and a result that has to be shown still fit under the room line
STUB = "[toy pager: this result is stored as page {page} for call {call}; read it with contexto_recall]"
STUB_FORM = re.compile(r"\[toy pager: this result is stored as page (\S+) for call (\S+); ")
REMOVED = "[result removed]"
LYING_ROOM = 100_000
DEFECTS = ("", "junk_page", "strip_all", "stub_on_arrival", "rewrite_always", "no_room", "room_one",
           "no_stub_text", "show_once", "lying_room", "in_place")
# What each defect does (the reviewers' wrong engines among them):
# - junk_page: every stub names one page that holds none of the results;
# - strip_all: all thinking is stripped on every request;
# - stub_on_arrival: every result is stubbed in the request that first carries it;
# - rewrite_always: every request is a batch;
# - no_room: no room is published; room_one: every room is 1 token; lying_room: every room is 100,000;
# - no_stub_text: after the first page, a stubbed result is replaced by text that names no page or call;
# - show_once: every 4th request of a conversation pages every result it is not the first to carry, and
#   the others page none (reviewer A's "show once, then page");
# - in_place: the CLI's own oldest whole result is cleared in place for the send and restored when the
#   usage arrives.

def find_stubs(dialect: str, request: dict) -> list[StubRef]:
    """``stubs.find_stubs`` for this pager's stub text."""
    found = []
    for item in request.get("messages" if dialect == "claude" else "input") or []:
        if not isinstance(item, dict):
            continue
        if dialect == "claude":
            texts = [b.get("content") for b in item.get("content") or []
                     if isinstance(b, dict) and b.get("type") == "tool_result"] \
                if isinstance(item.get("content"), list) else []
        else:
            texts = [item.get("output")] if str(item.get("type", "")).endswith("_output") else []
        for text in texts:
            match = STUB_FORM.match(text) if isinstance(text, str) else None
            if match:
                found.append(StubRef(match.group(1), (match.group(2),)))
    return found


class _State:
    def __init__(self) -> None:
        self.stubbed: dict[int, str] = {}  # index of a result item in the CLI's history -> its page
        self.strip: list[bool] = []  # per index: thinking is stripped there
        self.seen = 0  # items in the conversation's previous request
        self.pages = 0
        self.size = 0  # the last forwarded request, in tokens
        self.first_page = ""
        self.requests = 0

    def stripped(self, i: int) -> bool:
        return i < len(self.strip) and self.strip[i]

    def cover(self, start: int, end: int) -> None:
        self.strip += [False] * (end - len(self.strip))
        self.strip[start:end] = [True] * (end - start)


class ToyPager:
    preserves_input = True

    def __init__(self, store: Any, config: Config, defect: str = "", high: int = HIGH, low: int = LOW) -> None:
        if defect not in DEFECTS:
            raise ValueError(f"unknown defect {defect!r}")
        self.store, self.config, self.defect, self.high, self.low = store, config, defect, high, low
        self.cleared: list[tuple[dict, str, Any]] = []  # in_place: (object, key, original value)
        self.count = FakeUpstream()  # used as a tokenizer only
        self.states: dict[str, _State] = {}
        self.forms: dict[tuple[int, str, bool], tuple[dict, dict | None, int]] = {}
        self.junk: str | None = None
        self.sent: tuple[dict, bool] | None = None  # the request in flight, and whether it is main's
        self.ceiling = int(0.8 * (config.bound - config.headroom_floor - config.min_turn_room))

    def prime(self, request: dict) -> None:
        pass

    # Shapes ---------------------------------------------------------------------------------------

    @staticmethod
    def results(claude: bool, item: dict) -> list[dict]:
        """The result blocks of a Messages user message, or the Responses output item itself."""
        if claude:
            content = item.get("content")
            return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] \
                if item.get("role") == "user" and isinstance(content, list) else []
        return [item] if str(item.get("type", "")).endswith("_output") else []

    @staticmethod
    def is_prompt(claude: bool, item: dict) -> bool:
        if claude:
            return item.get("role") == "user" and not ToyPager.results(True, item)
        return item.get("type", "message") == "message" and item.get("role") == "user"

    def form(self, claude: bool, state: _State, i: int, item: dict) -> tuple[dict | None, int]:
        """Item ``i`` as it is forwarded, and its tokens. The same object every time, so the prefix
        stays byte-identical between batches."""
        page, strip = state.stubbed.get(i, ""), self.defect == "strip_all" or state.stripped(i)
        hit = self.forms.get((id(item), page, strip))
        if hit is not None and hit[0] is item:
            return hit[1], hit[2]
        out: dict | None = item
        removed = self.defect == "no_stub_text" and page and page != state.first_page
        if page and claude:
            out = {**item, "content": [
                {**b, "content": REMOVED if removed else STUB.format(page=page, call=b["tool_use_id"])}
                if b.get("type") == "tool_result" else b for b in item["content"]]}
        elif page:
            out = {**item, "output": REMOVED if removed else STUB.format(page=page, call=item["call_id"])}
        if strip and claude and item.get("role") == "assistant" and isinstance(item.get("content"), list):
            kept = [b for b in item["content"] if b.get("type") not in THINKING_TYPES]
            out = item if len(kept) == len(item["content"]) else {**item, "content": kept}
        elif strip and not claude and item.get("type") == "reasoning":
            out = None
        tokens = 0 if out is None else self.count.read(claude, out).tokens
        self.forms[id(item), page, strip] = (item, out, tokens)
        return out, tokens

    def transform(self, claude: bool, state: _State, items: list[dict]) -> tuple[list[dict], int]:
        out, total = [], 0
        for i, item in enumerate(items):
            form, tokens = self.form(claude, state, i, item)
            if form is not None:
                out.append(form)
                total += tokens
        return out, total

    def page(self, claude: bool, state: _State, items: list[dict], i: int) -> None:
        """Write result item ``i`` and the call that made it as a page, and stub it from now on."""
        if self.defect == "junk_page":
            if self.junk is None:
                (self.junk,) = self.store.page_out([[{"role": "user", "content": "not the result"}]], 0)
            state.stubbed[i] = self.junk
            return
        if claude:
            turn = [items[i - 1], items[i]]
        else:
            call = next(j for j in range(i - 1, -1, -1) if items[j].get("call_id") == items[i]["call_id"])
            turn = [items[call], items[i]]
        (state.stubbed[i],) = self.store.page_out([turn], state.pages)
        state.first_page = state.first_page or state.stubbed[i]
        state.pages += 1

    # The engine's two calls -------------------------------------------------------------------------

    def __call__(self, request: dict, headers: dict[str, str], endpoint: str) -> tuple[dict, dict[str, str], list[dict]]:
        counting = endpoint == COUNT_TOKENS
        claude = counting or endpoint == cli.MESSAGES
        field = "messages" if claude else "input"
        main = "X-Claude-Code-Agent-Id" not in headers
        state = self.states.setdefault("main" if main else "sub", _State())
        items = request[field]
        if counting:  # rule 11: the current transform, nothing changed
            return {**request, field: self.transform(claude, state, items)[0]}, headers, [{"event": "count_tokens"}]
        self.restore()
        state.requests += 1
        high, low = self.high, self.low
        if self.defect == "show_once":
            high = low = 0 if state.requests % 4 == 0 else 10**9
        n = len(items)
        head = self.count.head_tokens(endpoint, request)
        fresh = [i for i in range(state.seen, n) if self.results(claude, items[i])]
        prompt = next((items[i] for i in range(n - 1, -1, -1) if self.is_prompt(claude, items[i])), None)
        arriving = sum((block_tokens if claude else item_tokens)(b) for i in fresh for b in self.results(claude, items[i]))
        too_large = head + (self.count.read(claude, prompt).tokens if prompt else 0) + arriving >= self.ceiling
        if fresh and (too_large or self.defect == "stub_on_arrival"):
            for i in fresh:
                self.page(claude, state, items, i)
        forwarded, size = self.transform(claude, state, items)
        size += head
        before = size
        if size > high or self.defect == "rewrite_always":
            first = None
            for i in range(state.seen):  # the oldest first; never a result this request is the first to carry
                if size <= low and self.defect != "rewrite_always":
                    break
                if i not in state.stubbed and self.results(claude, items[i]):
                    before = self.form(claude, state, i, items[i])[1]
                    self.page(claude, state, items, i)
                    size -= before - self.form(claude, state, i, items[i])[1]
                    first = i if first is None else first
            if first is not None:
                state.cover(first, n)
                forwarded, size = self.transform(claude, state, items)
                size += head
        state.seen, state.size = n, size
        self.sent = (request, main)
        if self.defect == "in_place":
            self.clear(claude, state, items)
        events: list[dict] = []
        if size > self.config.bound:
            events.append({"event": "over_bound", "source": "estimate"})
        events.append({"event": "request", "estimate": before, "sent_estimate": size})
        if main:
            dialect = dialect_for(endpoint)
            self.store.set_retained(0, 0, retained_entries(forwarded, 0, dialect))
            self.publish(True, items, claude, size)
        return {**request, field: forwarded}, headers, events

    def clear(self, claude: bool, state: _State, items: list[dict]) -> None:
        """in_place: clear the CLI's own oldest whole result, which goes out as it is."""
        for i in range(max(0, state.seen - 4)):
            if i not in state.stubbed and self.results(claude, items[i]):
                target = self.results(claude, items[i])[0]
                key = "content" if claude else "output"
                self.cleared.append((target, key, target[key]))
                target[key] = "x"
                return

    def restore(self) -> None:
        for target, key, value in reversed(self.cleared):
            target[key] = value
        self.cleared = []

    def observe_usage(self, endpoint: str, usage: dict, request: dict, tool_calls: bool | None = None,
                      tool_call_ids: tuple[str, ...] = ()) -> list[dict]:
        self.restore()
        claude = endpoint == cli.MESSAGES
        real = usage_tokens(usage, dialect_for(endpoint))
        used = real + (usage.get("output_tokens") or 0)
        if self.sent is not None and self.sent[0] is request and self.sent[1]:
            self.publish(True, request["messages" if claude else "input"], claude, used)
        elif self.defect != "no_room":
            room = self.lie(max(0, self.config.bound - used - self.config.headroom_floor))
            self.store.set_side_room("toy-subagent", room if tool_calls else None)
        return [{"event": "over_bound", "source": "real"}] if real > self.config.bound else []

    def publish(self, main: bool, items: list[dict], claude: bool, used: int) -> None:
        if self.defect == "no_room":
            return
        room = self.lie(max(0, self.config.bound - used - self.config.headroom_floor))
        turn = sum(1 for item in items if self.is_prompt(claude, item) and not self._context(claude, item))
        self.store.set_turn_room(str(max(1, turn)), room)

    def lie(self, room: int) -> int:
        return 1 if self.defect == "room_one" else LYING_ROOM if self.defect == "lying_room" else room

    @staticmethod
    def _context(claude: bool, item: dict) -> bool:
        """Codex's environment message, which is a user message but not a prompt."""
        if claude:
            return False
        parts = item.get("content") or []
        return bool(parts) and all(str(p.get("text", "")).startswith("<environment_context>") for p in parts)
