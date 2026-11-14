"""Drive a scripted scenario through the real ``RewriteEngine`` and a real ``SessionStore``.

For each request the harness does what ``contexto.proxy`` does, in its order: ``engine.prime(request)``,
``engine(request, headers, endpoint)``, send what came back to the upstream, then
``engine.observe_usage(endpoint, usage, request, tool_calls=..., tool_call_ids=...)`` with the CLI's own
request, before the CLI sees the response. The CLI then appends the response and its tool results to
its own history; the forwarded request is never fed back.

Three things are not scripted:

- At a probe step the model calls ``contexto_search`` and then ``contexto_recall``. The harness runs
  the real handlers (``contexto.mcp.Server``) on a second handle of the session store, as the MCP
  server process has, after the response's usage was observed. The replies go into the CLI's history
  as tool results.
- After some loop requests of a Claude Code conversation the harness sends the same history to
  ``/v1/messages/count_tokens`` (contract rule 11) and compares what the engine returns with what it
  forwarded: after every prefix rewrite, and after one request in ``COUNT_EVERY``.
- ``engine`` can be replaced, so a test can run a scenario through an engine of its own.
"""

from __future__ import annotations

import functools
import io
import json
import os
import re
import statistics
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from contexto import mcp
from contexto.proxy import serialize_request
from contexto.rewrite import COUNT_TOKENS, Config, RewriteEngine
from contexto.store import PageUnreadableError, SessionStore
from contexto.store.render import own_call_ids
from inturn import cli, scripts, stubs
from inturn.scripts import Call, Conversation, Probe, Scenario, Step
from inturn.stubs import StubRef
from inturn.tokenizer import block_tokens, dumps, item_tokens
from inturn.upstream import FakeUpstream

LOOP_START = 50_000  # the room rules apply once a turn's own items pass this many tokens
STUB_DEPTH = 8  # how many pages a chain of stubs is followed through
COUNT_EVERY = 16
SESSION = "inturn"
PAGE_LINE = re.compile(r"(\S+) \(turn \d+\)")
HIT_LINE = re.compile(r"> (\d+): ")
NEARLY_SPENT = mcp.RECALL_NEARLY_SPENT.split("{")[0]
StubFinder = Callable[[str, dict], list[StubRef]]
EngineFactory = Callable[[Any, Config], Any]


class RecordingStore(SessionStore):
    """The real session store, which also keeps the rooms the engine published."""

    def __init__(self, home: Path, session_id: str) -> None:
        super().__init__(home, session_id)
        self.turn_rooms: list[int] = []
        self.side_published: list[int | None] = []

    def set_turn_room(self, turn_id: str, room_tokens: int) -> None:
        self.turn_rooms.append(room_tokens)
        super().set_turn_room(turn_id, room_tokens)

    def set_side_room(self, room_id: str, room_tokens: int | None) -> None:
        self.side_published.append(room_tokens)
        super().set_side_room(room_id, room_tokens)


@dataclass
class Sent:
    """One request, as measured."""

    conversation: str
    turn: int
    in_loop: bool  # not the turn's first request: it returns tool results
    loop_tokens: int  # the CLI's own items since the turn's prompt, by the upstream's tokenizer
    appended_tokens: int  # the CLI's own items added since the conversation's previous request
    cli_items: int
    forwarded_items: int
    estimate: int | None  # the engine's pre-send estimate of what it forwarded
    tokens: int  # the forwarded request by the upstream's tokenizer
    wire_bytes: int
    cache_read: int
    rewrite: bool  # a prefix rewrite: the head or an item differs from the previous request
    invalidated: int  # a rewrite: the previous request's tokens from the first changed item on
    resent: int  # a rewrite: uncached tokens beyond the items new since the previous request
    latest_thinking_missing: bool  # the newest response of the turn in flight came back without its thinking
    over_estimate: bool
    over_real: bool
    room_at_request: int | None  # main: the room published before the send
    room: int | None  # the room published from the response's usage (main's turn room, else a side room)
    tool_calls: bool  # the response holds a tool call
    unseen_tokens: int  # head, the turn's prompt and the results this request is the first to carry
    stubbed_on_arrival: int  # of those results, how many are not whole in the forwarded request
    lost: int  # results delivered so far that are neither whole in this request nor byte for byte in a page
    violations: dict[str, int]


class Stubs:
    """Checks ``StubRef``s against the store (see ``stubs.py`` for the rules)."""

    def __init__(self, store: SessionStore, upstream: FakeUpstream, dialect: str, find: StubFinder,
                 originals: dict[str, bytes], own: Callable[[], set[str]]) -> None:
        self.store, self.upstream, self.dialect, self.find = store, upstream, dialect, find
        self.originals, self.own = originals, own
        self.field = "messages" if dialect == "claude" else "input"
        self.seen: dict[StubRef, bool] = {}

    def page_results(self, page: dict) -> dict[str, set[bytes]]:
        held: dict[str, set[bytes]] = {}
        for message in page["messages"]:
            for call_id, content in self.upstream.results(self.dialect, message):
                held.setdefault(call_id, set()).add(content)
        return held

    def check(self, ref: StubRef) -> bool:
        known = self.seen.get(ref)
        if known is None:
            known = self.seen[ref] = self.reachable(ref)
        return known

    def reachable(self, ref: StubRef, depth: int = 0) -> bool:
        if not isinstance(ref.call_ids, tuple) or not ref.call_ids:
            return False
        if ref.page_id is None:
            own = self.own()
            return all(call_id in own for call_id in ref.call_ids)
        try:
            page = self.store.get_page(ref.page_id)
        except PageUnreadableError:
            return False
        if page is None or not isinstance(page.get("messages"), list):
            return False
        held = self.page_results(page)
        nested: list[StubRef] | None = None
        for call_id in ref.call_ids:
            original = self.originals.get(call_id)
            if original is None:
                return False
            if original in held.get(call_id, ()):
                continue
            if depth >= STUB_DEPTH:
                return False
            if nested is None:
                nested = self.find(self.dialect, {self.field: page["messages"]})
            if not any(call_id in inner.call_ids and self.reachable(StubRef(inner.page_id, (call_id,)), depth + 1)
                       for inner in nested):
                return False
        return True


@dataclass
class Run:
    scenario: Scenario
    dialect: str
    config: Config
    sent: list[Sent] = field(default_factory=list)
    events: Counter[str] = field(default_factory=Counter)
    violations: dict[str, int] = field(default_factory=dict)
    rejected: int = 0
    info: dict[str, int] = field(default_factory=dict)
    pages: int = 0
    history_tokens: dict[str, int] = field(default_factory=dict)  # each CLI's full last request, unpaged
    scripted_tokens: dict[str, int] = field(default_factory=dict)  # the same without the probe rounds
    originals: dict[str, int] = field(default_factory=dict)
    stubs: dict[str, int] | None = None  # None: stubs.find_stubs is not filled in
    probes: list[dict[str, Any]] = field(default_factory=list)
    count_tokens: dict[str, int] = field(default_factory=lambda: {"sent": 0, "different": 0, "published": 0})

    def check_stubs(self) -> list[str]:
        """What is wrong with the stubs, as sentences; empty when every stub's original is reachable."""
        if self.stubs is None:
            return ["stubs.find_stubs is not filled in, so no stub was checked"]
        problems = []
        if not self.stubs["found"]:
            problems.append("no stub was found in any forwarded request")
        if self.stubs["unreachable"]:
            problems.append(f"{self.stubs['unreachable']} stubs do not lead to their originals, byte for byte")
        return problems

    def loops(self) -> dict[tuple[str, int], list[Sent]]:
        """The requests of each conversation's turns, in order."""
        out: dict[tuple[str, int], list[Sent]] = {}
        for sent in self.sent:
            out.setdefault((sent.conversation, sent.turn), []).append(sent)
        return out

    def rooms(self, conversation: str) -> dict[str, int | None]:
        sent = [s for s in self.sent if s.conversation == conversation]
        rooms = [s.room for s in sent if s.room is not None]
        late = [s for s in sent if s.loop_tokens > LOOP_START]
        low = self.config.min_turn_room
        return {"published": len(rooms),
                "published_at_request": sum(1 for s in sent if s.room_at_request is not None),
                "min": min(rooms) if rooms else None,
                "median": statistics.median_low(rooms) if rooms else None,
                "zero": sum(1 for room in rooms if room == 0),
                "past_50k": len(late),
                "request_under_min_past_50k": sum(1 for s in late
                                                  if s.room_at_request is not None and s.room_at_request < low),
                "response_zero_past_50k": sum(1 for s in late if s.room == 0),
                "response_under_half_min_past_50k": sum(1 for s in late if s.room is not None and s.room < low // 2)}

    def probe_counts(self, conversation: str) -> dict[str, dict[str, int]]:
        mine = [p for p in self.probes if p["conversation"] == conversation]
        return {kind: dict(sorted(Counter(p["outcome"] for p in mine if p["kind"] == kind).items()))
                for kind in ("search", "recall")}

    def metrics(self) -> dict[str, Any]:
        """The numbers of the run, JSON-ready. Two runs of the same scenario give the same dict."""
        total = sum(s.tokens for s in self.sent)
        rewrites = [s for s in self.sent if s.rewrite]
        names = [c.name for c in self.scenario.conversations]
        estimates = [s.estimate for s in self.sent if s.estimate is not None]
        resent = sum(s.resent for s in rewrites)
        return {
            "scenario": self.scenario.name, "dialect": self.dialect, "bound": self.scenario.bound,
            "size": self.scenario.size,
            "requests": len(self.sent),
            "requests_by_conversation": {n: sum(1 for s in self.sent if s.conversation == n) for n in names},
            "over_bound": {"estimate": sum(s.over_estimate for s in self.sent),
                           "real": sum(s.over_real for s in self.sent)},
            "largest": {"estimate": max(estimates, default=None), "tokens": max(s.tokens for s in self.sent),
                        "bytes": max(s.wire_bytes for s in self.sent)},
            "pages": self.pages, "rotations": self.events["rotation"],
            "violations": dict(sorted(self.violations.items())), "rejected_requests": self.rejected,
            "info": dict(sorted(self.info.items())),
            "room": {n: self.rooms(n) for n in names},
            "input_tokens": total,
            "cache_read_share": round(sum(s.cache_read for s in self.sent) / total, 4),
            "rewrites": {"count": len(rewrites), "in_loop": sum(1 for s in rewrites if s.in_loop),
                         "invalidated": sum(s.invalidated for s in rewrites), "resent": resent,
                         "mean_resent": round(resent / len(rewrites)) if rewrites else 0,
                         "resent_share": round(resent / total, 4)},
            "thinking": {"newest_missing": sum(s.latest_thinking_missing for s in self.sent)},
            "arrival": {"requests_with_a_stubbed_new_result": sum(1 for s in self.sent if s.stubbed_on_arrival),
                        "results_stubbed_on_arrival": sum(s.stubbed_on_arrival for s in self.sent)},
            "history_tokens": self.history_tokens,
            "scripted_tokens": self.scripted_tokens,
            "originals": self.originals,
            "requests_with_a_lost_result": sum(1 for s in self.sent if s.lost),
            "stubs": self.stubs,
            "probes": {n: self.probe_counts(n) for n in names if any(p["conversation"] == n for p in self.probes)},
            "count_tokens": self.count_tokens,
            "events": dict(sorted(self.events.items())),
        }


class _Actor:
    """One conversation: its script position and the CLI that holds its history."""

    def __init__(self, conversation: Conversation, dialect: str) -> None:
        self.conversation = conversation
        self.cli = cli.CLIS[dialect](conversation)
        self.turn = 0
        self.step = 0
        self.requests = 0
        self.loop_tokens = 0
        self.prompt_tokens = 0
        self.probe_tokens = 0
        self.appended: list[dict] = []  # the items added since the previous request
        self.appended_tokens = 0
        self.fresh: list[str] = []  # the calls whose results were added since the previous request
        self.results: dict[str, bytes] = {}  # every result delivered: call id -> digest of its content
        self.result_tokens: dict[str, int] = {}
        self.scripted: dict[str, str] = {}  # the scripted ones: call id -> call key
        self.hit: tuple[str, int] | None = None  # the page and offset the last search named
        self.previous: Sent | None = None
        self.last_forwarded: dict | None = None
        self.digests: list[str] = []  # of each history item when it was added
        self.head_digest = ""

    def at_prompt(self) -> bool:
        return self.step == 0

    def advance(self) -> Step:
        turn = self.conversation.turns[self.turn]
        step = turn.steps[self.step]
        self.step += 1
        if self.step == len(turn.steps):
            self.turn, self.step = self.turn + 1, 0
        return step

    def done(self) -> bool:
        return self.turn == len(self.conversation.turns)


def _digest(item: dict) -> str:
    return scripts.digest(dumps(item))


def _estimate(events: list[dict]) -> int | None:
    request = next((e for e in reversed(events) if e.get("event") == "request"), {})
    return request.get("sent_estimate", request.get("estimate"))


def run(scenario: Scenario, dialect: str, *, find_stubs: StubFinder = stubs.find_stubs,
        engine: EngineFactory | None = None, check_wire: bool = False) -> Run:
    """Play ``scenario`` on ``dialect`` (``claude`` or ``codex``) and measure it.

    ``engine(store, config)`` builds the engine under test; the default is the real ``RewriteEngine``.
    It is called like the proxy calls one: ``prime(request)``, ``engine(request, headers, endpoint)``
    returning ``(forwarded, headers, events)``, and ``observe_usage(...)`` returning events.
    ``check_wire`` also serializes every forwarded request, to prove the byte count."""
    if dialect not in scenario.dialects:
        raise ValueError(f"{scenario.name} does not run on {dialect}")
    with tempfile.TemporaryDirectory(prefix="inturn-") as home:
        store = RecordingStore(Path(home), SESSION)
        store.set_mcp_listed()  # as after the MCP server answered tools/list in a real session
        reader = SessionStore(Path(home), SESSION)  # the MCP server's own handle: another process, in real use
        try:
            return _Play(scenario, dialect, store, reader, find_stubs, engine, check_wire).play()
        finally:
            reader.release()
            store.release()


class _Play:
    def __init__(self, scenario: Scenario, dialect: str, store: RecordingStore, reader: SessionStore,
                 find_stubs: StubFinder | None, engine: EngineFactory | None, check_wire: bool) -> None:
        self.scenario, self.dialect, self.store, self.check_wire = scenario, dialect, store, check_wire
        self.config = Config(bound=scenario.bound)
        self.engine = engine(store, self.config) if engine else RewriteEngine(store, config=self.config)
        self.upstream = FakeUpstream()
        self.server = mcp.Server(reader, io.StringIO(), io.StringIO())
        self.actors = {c.name: _Actor(c, dialect) for c in scenario.conversations}
        self.endpoint = cli.CLIS[dialect].endpoint
        self.field = "messages" if dialect == "claude" else "input"
        self.result = Run(scenario, dialect, self.config)
        self.originals: dict[str, bytes] = {}  # every delivered result, all conversations
        self.find = find_stubs
        self.stubs = Stubs(store, self.upstream, dialect, find_stubs or (lambda dialect, request: []),
                           self.originals, self.own)
        self.paged: dict[str, set[bytes]] = {}  # call id -> the result contents the store's pages hold for it
        self.page_names: set[str] = set()
        self._own: tuple[int, set[str]] = (-1, set())
        for actor in self.actors.values():  # Codex's history starts with the items before the first prompt
            self.added(actor, list(actor.cli.items))
            actor.appended, actor.appended_tokens, actor.loop_tokens = [], 0, 0
            self.upstream.pin_head(self.endpoint, actor.cli.head())
            actor.head_digest = _digest(actor.cli.head())

    def own(self) -> set[str]:
        """The ids of the calls of Contexto's own search, recall and state tools, by the engine's rule."""
        size = sum(len(actor.cli.items) for actor in self.actors.values())
        if self._own[0] != size:
            self._own = (size, set().union(*(own_call_ids(actor.cli.items) for actor in self.actors.values())))
        return self._own[1]

    # The CLI's side -----------------------------------------------------------------------------

    def added(self, actor: _Actor, items: list[dict], probe: bool = False) -> None:
        """Account for items the CLI appended to its history."""
        tokens = sum(self.upstream.pin(self.dialect, item).tokens for item in items)
        actor.loop_tokens += tokens
        actor.appended_tokens += tokens
        actor.probe_tokens += tokens if probe else 0
        actor.appended.extend(items)
        actor.digests.extend(_digest(item) for item in items)
        for item in items:
            blocks = ([b for b in item.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_result"]
                      if self.dialect == "claude" and isinstance(item.get("content"), list) else [])
            for n, (call_id, content) in enumerate(self.upstream.results(self.dialect, item)):
                actor.results[call_id] = self.originals[call_id] = content
                actor.result_tokens[call_id] = block_tokens(blocks[n]) if blocks else item_tokens(item)
                actor.fresh.append(call_id)

    def resolve(self, actor: _Actor, step: Step) -> Step:
        """A probe step as the model would answer it: a search, then a recall of the page it named."""
        probe = step.probe
        if probe.kind == "search":
            call = Call(probe.key, "contexto_search", probe.token, "")
        elif actor.hit is not None:
            page_id, offset = actor.hit
            call = Call(probe.key, "contexto_recall", json.dumps({"page_id": page_id, "offset": offset}), "")
        else:
            call = probe.fallback  # the search named no page: the model goes on with its work
        return replace(step, calls=(call,))

    def call_tool(self, actor: _Actor, probe: Probe, call: Call, in_view: bool) -> Call:
        """Run one of Contexto's tools through the real MCP handler and record how it went."""
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": call.tool, "arguments": cli.contexto_arguments(call)}})
        reply = self.server.handle_line(line)["result"]
        texts = tuple(block["text"] for block in reply["content"])
        if probe.kind == "search":
            outcome, actor.hit = _search_outcome(texts, probe)
        else:
            outcome, actor.hit = _recall_outcome(texts, probe, bool(reply.get("isError"))), None
        self.result.probes.append({"conversation": actor.conversation.name, "kind": probe.kind,
                                   "request": len(self.result.sent), "target_in_view": in_view, "outcome": outcome})
        return replace(call, texts=texts, result="\n".join(texts))

    # The store's pages ----------------------------------------------------------------------------

    def index_pages(self) -> None:
        """Read the pages written since the last look: which results they hold, byte for byte."""
        try:
            names = {name for name in os.listdir(self.store.pages_dir) if name.endswith(".json")}
        except OSError:
            return
        for name in sorted(names - self.page_names):
            try:
                page = self.store.get_page(name[:-5])
            except PageUnreadableError:
                page = None
            if page is not None and isinstance(page.get("messages"), list):
                for call_id, contents in self.stubs.page_results(page).items():
                    self.paged.setdefault(call_id, set()).update(contents)
        self.page_names = names

    # One request ----------------------------------------------------------------------------------

    def play(self) -> Run:
        for name in self.scenario.schedule:
            self.request(self.actors[name])
        unfinished = [name for name, actor in self.actors.items() if not actor.done()]
        if unfinished:
            raise RuntimeError(f"{self.scenario.name}: the schedule leaves {unfinished} unfinished")
        return self.finish()

    def request(self, actor: _Actor) -> None:
        result, store, upstream, name = self.result, self.store, self.upstream, actor.conversation.name
        turn = actor.turn
        in_loop = not actor.at_prompt()
        if not in_loop:
            before = len(actor.cli.items)
            actor.cli.prompt(actor.conversation.turns[turn])
            actor.loop_tokens = 0
            new = actor.cli.items[before:]
            actor.prompt_tokens = upstream.tokens(self.dialect, new[0])
            self.added(actor, new)
        step = actor.advance()
        if step.probe:
            step = self.resolve(actor, step)
        request = actor.cli.request()

        self.engine.prime(request)
        rooms, sides = len(store.turn_rooms), len(store.side_published)
        forwarded, _, events = self.engine(request, dict(actor.cli.headers), self.endpoint)
        room_at_request = store.turn_rooms[-1] if len(store.turn_rooms) > rooms else None
        reply = upstream.respond(name, self.endpoint, forwarded, step, appended=actor.appended)
        if self.check_wire:
            assert reply.wire_bytes == len(serialize_request(forwarded)), "the byte count is wrong"
        if self.dialect == "claude" and in_loop and (reply.rewrite or actor.requests % COUNT_EVERY == COUNT_EVERY // 2):
            self.count_tokens(actor, request, forwarded)
        rooms = len(store.turn_rooms)
        usage_events = self.engine.observe_usage(self.endpoint, dict(reply.usage), request,
                                                 tool_calls=reply.tool_calls, tool_call_ids=reply.call_ids)
        room = store.turn_rooms[-1] if len(store.turn_rooms) > rooms else None
        if room is None:
            room = next((r for r in reversed(store.side_published[sides:]) if r is not None), None)

        # What became of every result delivered so far: whole in this request, or in a page byte for byte.
        self.index_pages()
        view: dict[str, set[bytes]] = {}
        for item in reply.items:
            for call_id, content in zip(item.results, item.contents):
                view.setdefault(call_id, set()).add(content)
        unstored: set[str] = set()  # the calls a stub in this request stands for without a stored copy
        if self.find is not None:
            try:
                found = self.find(self.dialect, forwarded)
            except NotImplementedError:
                self.find = None
            else:
                for ref in found:
                    if self.stubs.check(ref) and ref.page_id is None:
                        unstored.update(ref.call_ids)
        lost = sum(1 for call_id, content in actor.results.items()
                   if content not in view.get(call_id, ()) and content not in self.paged.get(call_id, ())
                   and call_id not in unstored)
        stubbed = sum(1 for call_id in actor.fresh if actor.results[call_id] not in view.get(call_id, ()))
        unseen = (upstream.head_tokens(self.endpoint, request) + actor.prompt_tokens
                  + sum(actor.result_tokens[call_id] for call_id in actor.fresh))

        for event in (*events, *usage_events):
            result.events[event.get("event", "?")] += 1
        over = [e.get("source") for e in (*events, *usage_events) if e.get("event") == "over_bound"]
        sent = Sent(name, turn, in_loop, actor.loop_tokens, actor.appended_tokens, len(request[self.field]),
                    len(forwarded.get(self.field) or []), _estimate(events), reply.input_tokens, reply.wire_bytes,
                    reply.cache_read, reply.rewrite, reply.invalidated or 0, reply.resent or 0, reply.latest_missing,
                    "estimate" in over, "real" in over, room_at_request, room, reply.tool_calls, unseen, stubbed,
                    lost, reply.violations)
        result.sent.append(sent)
        actor.previous, actor.last_forwarded = sent, forwarded
        actor.requests += 1
        actor.appended, actor.appended_tokens, actor.fresh = [], 0, []

        if step.probe and step.calls[0].tool.startswith("contexto_"):
            target = cli.CLIS[self.dialect].call_id(step.probe.target)
            in_view = actor.results[target] in view.get(target, ())
            step = replace(step, calls=(self.call_tool(actor, step.probe, step.calls[0], in_view),))
        elif step.probe and step.probe.kind == "recall":
            result.probes.append({"conversation": name, "kind": "recall", "request": len(result.sent),
                                  "target_in_view": None, "outcome": "not_called"})
        self.added(actor, actor.cli.take(reply.output, step), probe=bool(step.probe))
        if not step.probe:
            for call in step.calls:
                if call.head:
                    actor.scripted[cli.CLIS[self.dialect].call_id(call.key)] = call.key

    def count_tokens(self, actor: _Actor, request: dict, forwarded: dict) -> None:
        """Rule 11: a count_tokens request with the same history gets the transform just forwarded, and
        publishes nothing."""
        body = {k: v for k, v in request.items() if k not in ("max_tokens", "stream", "metadata")}
        rooms, sides = len(self.store.turn_rooms), len(self.store.side_published)
        view, _, _ = self.engine(body, dict(actor.cli.headers), COUNT_TOKENS)
        counted = self.result.count_tokens
        counted["sent"] += 1
        counted["different"] += any(view.get(key) != forwarded.get(key) for key in ("system", "tools", "messages"))
        counted["published"] += (len(self.store.turn_rooms), len(self.store.side_published)) != (rooms, sides)

    # The end ----------------------------------------------------------------------------------------

    def finish(self) -> Run:
        result, upstream = self.result, self.upstream
        result.violations = dict(upstream.violations)
        result.rejected = upstream.rejected
        result.info = dict(upstream.info)
        self.index_pages()
        result.pages = len(self.page_names)
        if self.find is not None:
            seen = self.stubs.seen
            result.stubs = {"found": len(seen), "unreachable": sum(1 for ok in seen.values() if not ok)}
        counts = Counter({"total": 0, "in_view": 0, "stored": 0, "lost": 0})
        for actor in self.actors.values():
            view: dict[str, set[bytes]] = {}
            for item in actor.last_forwarded[self.field]:
                for call_id, content in upstream.results(self.dialect, item):
                    view.setdefault(call_id, set()).add(content)
            for call_id in actor.scripted:  # the scripted results; Contexto's own replies are not counted here
                counts["total"] += 1
                content = actor.results[call_id]
                counts["in_view" if content in view.get(call_id, ()) else
                       "stored" if content in self.paged.get(call_id, ()) else "lost"] += 1
            name = actor.conversation.name
            result.history_tokens[name] = upstream.request_tokens(self.endpoint, actor.cli.request())
            result.scripted_tokens[name] = result.history_tokens[name] - actor.probe_tokens
            if (actor.digests != [_digest(i) for i in actor.cli.items]
                    or actor.head_digest != _digest(actor.cli.head())):
                # The proxy hands the engine the CLI's own request and reads it again for the usage; the
                # upstream here read each of its items once. Neither holds if the engine edits them.
                raise RuntimeError(f"{name}: the engine changed the CLI's own history in place")
        result.originals = dict(counts)
        return result


def _search_outcome(texts: tuple[str, ...], probe: Probe) -> tuple[str, tuple[str, int] | None]:
    """How a search went, and the page and offset a model would recall: ``refused`` (the room is used
    up), ``nothing``, ``too_long``, ``in_view`` (the line came back from the request in view),
    ``paged`` (it came back from a page), or ``other`` (a reply without the line)."""
    text = texts[0] if texts else ""
    if text == mcp.BUDGET_SPENT:
        return "refused", None
    if text == mcp.NO_HIT:
        return "nothing", None
    if text == mcp.SEARCH_TOO_LONG:
        return "too_long", None
    page = hit = None
    in_view = False
    for row in text.split("\n"):
        header = PAGE_LINE.fullmatch(row)
        if header:
            page = header.group(1)
        elif probe.line.strip() not in row:
            continue
        elif row.startswith("> in view"):
            in_view = True
        elif page and hit is None and (found := HIT_LINE.match(row)):
            hit = (page, int(found.group(1)))
    return ("paged" if hit else "in_view" if in_view else "other"), hit


def _recall_outcome(texts: tuple[str, ...], probe: Probe, error: bool) -> str:
    """``returned``: the reply holds the searched line and the two scripted lines after it, as the
    original has them. Else ``refused`` (the room is used up), ``error`` or ``partial``."""
    text = "\n".join(texts)
    if "\n".join((probe.line, *probe.after)) in text:
        return "returned"
    if texts and (texts[0] == mcp.BUDGET_SPENT or texts[0].startswith(NEARLY_SPENT)):
        return "refused"
    return "error" if error else "partial"


def script_size(scenario: Scenario, dialect: str) -> dict[str, int]:
    """What the script holds, before any engine sees it: requests, scripted tool rounds, probes, and its
    scripted tool results in the upstream's tokens."""
    render = cli.claude_tool_result if dialect == "claude" else cli.codex_output
    results = [block_tokens(render(call)) for c in scenario.conversations for call in c.calls]
    return {"requests": len(scenario.schedule), "rounds": sum(c.rounds for c in scenario.conversations),
            "probes": sum(c.probes for c in scenario.conversations),
            "calls": len(results), "result_tokens": sum(results), "largest_result": max(results),
            "images": sum(len(call.images) for c in scenario.conversations for call in c.calls)
            + sum(len(t.images) for c in scenario.conversations for t in c.turns)}


@functools.cache
def cached(name: str, dialect: str, full: bool = False) -> Run:
    """One run per scenario and dialect for a whole test session."""
    return run(scripts.build(name, full=full), dialect)
