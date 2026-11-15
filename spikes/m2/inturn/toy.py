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
CLEARED = "[cleared]"
LYING_ROOM = 100_000
INFLATED = 999_999
OLDER = ("silent_older", "drop_older_stubs", "drop_older_rounds", "noted_older_rounds", "trim_older_prompts",
         "split_older_prompts", "drop_older_turns", "fake_omission", "fake_omission_split",
         "fake_omission_trim", "fake_omission_trimall", "fake_omission_doc", "omit_turns", "omit_turns_n0",
         "omit_turns_n99")  # Claude Code only
ROUNDS_GONE = ("drop_older_rounds", "noted_older_rounds", "trim_older_prompts", "split_older_prompts", "fake_omission",
               "fake_omission_split", "fake_omission_trim", "fake_omission_trimall", "fake_omission_doc")
FAKE_OMISSION = ("fake_omission", "fake_omission_split", "fake_omission_trim", "fake_omission_trimall",
                 "fake_omission_doc")
OMIT_TURNS = {"omit_turns": 1, "omit_turns_n0": 0, "omit_turns_n99": 99}  # the N its omission line claims
DEFECTS = ("", "junk_page", "strip_all", "stub_on_arrival", "rewrite_always", "no_room", "room_one",
           "no_stub_text", "show_once", "lying_room", "in_place", "inflate", *OLDER)
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
#   usage arrives;
# - inflate: once in a run, the request right after a batch is a batch of one more result, and its request
#   event publishes an estimate of 999,999 (reviewer A's inflating engine);
# - silent_older: from each turn start on, every earlier turn's result is [cleared], with its rounds written
#   to a page and its calls and prompt kept (reviewer A's SilentOlder);
# - drop_older_stubs: from each turn start on, an earlier turn's stub becomes [result removed] (reviewer B);
# - drop_older_rounds: from each turn start on, the rounds of earlier turns are removed, calls and results,
#   and written to a page; their prompts and final answers stay, and nothing names the page;
# - noted_older_rounds: the same, but the last earlier answer kept ends with a line naming that page. This
#   one is right: rule 3 accounts for the rounds through the page id in the request's text;
# - trim_older_prompts, split_older_prompts: drop_older_rounds, with each earlier prompt losing its last
#   character, or split into two text blocks with every byte kept (reviewer A's trim and split at
#   304e598e: a changed prompt is not evidence that its turn was paged);
# - drop_older_turns: every earlier turn is removed whole and paged, with no index naming the page
#   (reviewer A's wholeturns);
# - fake_omission: drop_older_rounds, with the index's "(1 older pages omitted; use contexto_search ...)"
#   line added where the page id would go, though the turns' prompts are still in view (reviewer A at
#   9fe691ef); fake_omission_split, fake_omission_trim: the same with each earlier prompt split into two
#   blocks or trimmed by one character (reviewer B at 18d2e783: a changed prompt is not a turn that left);
#   fake_omission_trimall: prompt and every earlier answer trimmed by one character; fake_omission_doc: the
#   prompt moved into a plain-text document block (reviewer A at 3097dbe9);
# - omit_turns, omit_turns_n0, omit_turns_n99: drop_older_turns with an omission line claiming 1, 0 or 99
#   older pages. With 1 it is the open case the note states (the whole turn left, the count borne out by
#   the store, the results stored), and it passes; 0 and 99 fail.
# The last four strip thinking where they rewrite: in each earlier turn, from the first item they change to
# the next prompt.

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
        self.batched = False  # the previous request was a batch

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
        self.older_forms: dict[tuple[int, int], tuple[dict, dict]] = {}
        self.older_pages: dict[int, str] = {}  # OLDER: the turn's prompt index -> the page of what precedes it
        self.inflated = False
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
        if self.defect in OLDER and not claude:
            raise ValueError(f"{self.defect} is written for Claude Code's messages")
        if counting:  # rule 11: the current transform, nothing changed
            forwarded = self.transform(claude, state, items)[0]
            if self.defect in OLDER:
                forwarded = self.older(items, forwarded, False)
            return {**request, field: forwarded}, headers, [{"event": "count_tokens"}]
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
        forwarded, size = self.view(claude, state, items, head)
        before = size
        # The defects that take earlier turns out of view leave the batch the running turn's results only.
        start = self.prompt_index(items) if self.defect in ("silent_older", "drop_older_turns", *ROUNDS_GONE, *OMIT_TURNS) else 0
        one = self.defect == "inflate" and state.batched and not self.inflated and size <= high
        if size > high or self.defect == "rewrite_always" or one:
            first = None
            for i in range(start, state.seen):  # the oldest first; never a result this request is the first to carry
                if (size <= low and self.defect != "rewrite_always" and not one) or (one and first is not None):
                    break
                if i not in state.stubbed and self.results(claude, items[i]):
                    whole = self.form(claude, state, i, items[i])[1]
                    self.page(claude, state, items, i)
                    size -= whole - self.form(claude, state, i, items[i])[1]
                    first = i if first is None else first
            if first is not None:
                state.cover(first, n)
                forwarded, size = self.view(claude, state, items, head)
            state.batched = first is not None and not one
            if one and first is not None:
                self.inflated, before = True, INFLATED
        else:
            state.batched = False
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

    def view(self, claude: bool, state: _State, items: list[dict], head: int) -> tuple[list[dict], int]:
        """What is forwarded, and its tokens with the head."""
        forwarded, size = self.transform(claude, state, items)
        if self.defect in OLDER:
            forwarded = self.older(items, forwarded, True)
            size = sum(self.count.read(True, form).tokens for form in forwarded)
        return forwarded, head + size

    def prompt_index(self, items: list[dict]) -> int:
        return max((i for i, item in enumerate(items) if self.is_prompt(True, item)), default=0)

    def older(self, items: list[dict], forwarded: list[dict], write: bool) -> list[dict]:
        """The ``OLDER`` defects: what they do to the messages before the turn's prompt (Messages only;
        the toy forwards one message per message of the CLI's). ``write``: page what precedes the
        prompt the first time a request carries it."""
        p = self.prompt_index(items)
        if p and write and p not in self.older_pages:
            (self.older_pages[p],) = self.store.page_out([items[:p]], len(self.older_pages))
        if self.defect == "drop_older_turns":
            return forwarded[p:]
        if self.defect in OMIT_TURNS:
            line = {"type": "text", "text": f"({OMIT_TURNS[self.defect]} older pages omitted; use contexto_search to find them)"}
            head = forwarded[p]
            content = head["content"] if isinstance(head.get("content"), list) else [{"type": "text", "text": head["content"]}]
            return [self.older_form(head, [*content, line]), *forwarded[p + 1:]]
        out: list[dict] = []
        first = None
        for i, (item, form) in enumerate(zip(items, forwarded)):
            if i >= p:
                out.append(form)
                continue
            if self.is_prompt(True, item):  # each turn was rewritten at its own turn start: its responses after that
                first = None
                if self.defect in ("trim_older_prompts", "split_older_prompts", "fake_omission_split", "fake_omission_trim",
                                   "fake_omission_trimall", "fake_omission_doc"):
                    form = self.changed_prompt(form)
            blocks = item.get("content") if isinstance(item.get("content"), list) else []
            if self.defect in ROUNDS_GONE:
                if self.results(True, item) or any(b.get("type") == "tool_use" for b in blocks):
                    first = i if first is None else first
                    continue
            elif self.results(True, form):
                text = CLEARED if self.defect == "silent_older" else REMOVED
                changed = [{**b, "content": text} if b.get("type") == "tool_result" and (
                    self.defect == "silent_older" or (isinstance(b.get("content"), str) and STUB_FORM.match(b["content"])))
                    else b for b in form["content"]]
                if changed != form["content"]:
                    form = self.older_form(form, changed)
                    first = i if first is None else first
            if self.defect == "fake_omission_trimall" and form.get("role") == "assistant" \
                    and isinstance(form.get("content"), list):
                form = self.older_form(form, [{**b, "text": b["text"][:-1]} if b.get("type") == "text" and b.get("text")
                                              else b for b in form["content"]])
            if first is not None and form.get("role") == "assistant" and isinstance(form.get("content"), list):
                kept = [b for b in form["content"] if b.get("type") not in THINKING_TYPES]
                if len(kept) < len(form["content"]):
                    form = self.older_form(form, kept)
            out.append(form)
        earlier = len(out) - (len(items) - p)
        last = max((n for n in range(earlier) if out[n].get("role") == "assistant"), default=None)
        if self.defect in FAKE_OMISSION and last is not None:
            note = {"type": "text", "text": "(1 older pages omitted; use contexto_search to find them)"}
            out[last] = {**out[last], "content": [*out[last]["content"], note]}
        if self.defect == "noted_older_rounds" and last is not None and p in self.older_pages:
            note = {"type": "text", "text": f"[the tool rounds before this point are stored as page {self.older_pages[p]}]"}
            out[last] = {**out[last], "content": [*out[last]["content"], note]}
        return out

    def changed_prompt(self, form: dict) -> dict:
        """trim_older_prompts: the prompt's text less its last character; split_older_prompts: its text as two
        text blocks, every byte kept."""
        content = form.get("content")
        text = content if isinstance(content, str) else next(
            (b["text"] for b in content or [] if isinstance(b, dict) and b.get("type") == "text"), "")
        half = len(text) // 2
        if self.defect == "fake_omission_doc":
            return self.older_form(form, [{"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                                                           "data": text}}])
        blocks = ([{"type": "text", "text": text[:-1]}]
                  if self.defect in ("trim_older_prompts", "fake_omission_trim", "fake_omission_trimall")
                  else [{"type": "text", "text": text[:half]}, {"type": "text", "text": text[half:]}])
        return self.older_form(form, blocks)

    def older_form(self, form: dict, content: list[dict]) -> dict:
        """``form`` with ``content``, the same object for the same form every time."""
        key = (id(form), len(content))
        hit = self.older_forms.get(key)
        if hit is None or hit[0] is not form:
            hit = self.older_forms[key] = (form, {**form, "content": content})
        return hit[1]

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
