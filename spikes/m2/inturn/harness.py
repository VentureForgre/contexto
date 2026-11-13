"""Drive a scripted scenario through the real ``RewriteEngine`` and a real ``SessionStore``.

For each request the harness does what ``contexto.proxy`` does, in its order: ``engine.prime(request)``,
``engine(request, headers, endpoint)``, send what came back to the upstream, then
``engine.observe_usage(endpoint, usage, request, tool_calls=..., tool_call_ids=...)`` with the CLI's own
request, before the CLI sees the response. The CLI then appends the response and its tool results to
its own history; the forwarded request is never fed back.
"""

from __future__ import annotations

import functools
import statistics
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contexto.proxy import serialize_request
from contexto.rewrite import Config, RewriteEngine
from contexto.store import SessionStore
from inturn import cli, scripts, stubs
from inturn.scripts import Call, Conversation, Scenario, Step
from inturn.tokenizer import block_tokens, dumps
from inturn.upstream import FakeUpstream

LOOP_START = 50_000  # the room target applies once a turn's own items pass this many tokens
StubFinder = Callable[[str, dict], list[stubs.StubRef]]


class RecordingStore(SessionStore):
    """The real session store, which also keeps what the engine published and paged."""

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
    cli_items: int
    forwarded_items: int
    estimate: int | None  # the engine's pre-send estimate of what it forwarded
    tokens: int  # the forwarded request by the upstream's tokenizer
    wire_bytes: int
    cache_read: int
    resent: int | None
    over_estimate: bool
    over_real: bool
    room_at_request: int | None  # main: the room published before the send
    room: int | None  # the room published from the response's usage (main's turn room, else a side room)
    violations: dict[str, int]


@dataclass
class Run:
    scenario: Scenario
    dialect: str
    sent: list[Sent] = field(default_factory=list)
    events: Counter[str] = field(default_factory=Counter)
    violations: dict[str, int] = field(default_factory=dict)
    rejected: int = 0
    info: dict[str, int] = field(default_factory=dict)
    pages: int = 0
    history_tokens: dict[str, int] = field(default_factory=dict)  # each CLI's full last request, unpaged
    originals: dict[str, int] = field(default_factory=dict)
    stubs: dict[str, int] | None = None  # None: stubs.find_stubs is not filled in
    history_digests_stable: bool = True

    def check_stubs(self) -> list[str]:
        """What is wrong with the stubs, as sentences; empty when every stub's original is reachable."""
        if self.stubs is None:
            return ["stubs.find_stubs is not filled in, so no stub was checked"]
        problems = []
        if not self.stubs["found"]:
            problems.append("no stub was found in any forwarded request")
        if self.stubs["unreachable"]:
            problems.append(f"{self.stubs['unreachable']} stubs name a page that is missing or lacks the original")
        return problems

    def rooms(self, conversation: str) -> dict[str, int | None]:
        sent = [s for s in self.sent if s.conversation == conversation]
        rooms = [s.room for s in sent if s.room is not None]
        late = [s for s in sent if s.loop_tokens > LOOP_START]
        return {"published": len(rooms),
                "min": min(rooms) if rooms else None,
                "median": statistics.median_low(rooms) if rooms else None,
                "zero": sum(1 for room in rooms if room == 0),
                "zero_past_50k": sum(1 for s in late if s.room == 0 or s.room_at_request == 0)}

    def metrics(self) -> dict[str, Any]:
        """The numbers of the run, JSON-ready. Two runs of the same scenario give the same dict."""
        total = sum(s.tokens for s in self.sent)
        rewrites = [s.resent for s in self.sent if s.resent is not None]
        names = [c.name for c in self.scenario.conversations]
        estimates = [s.estimate for s in self.sent if s.estimate is not None]
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
            "rewrites": {"count": len(rewrites), "tokens": sum(rewrites),
                         "mean": round(sum(rewrites) / len(rewrites)) if rewrites else 0,
                         "share": round(sum(rewrites) / total, 4)},
            "history_tokens": self.history_tokens,
            "originals": self.originals,
            "stubs": self.stubs,
            "events": dict(sorted(self.events.items())),
        }


class _Actor:
    """One conversation: its script position and the CLI that holds its history."""

    def __init__(self, conversation: Conversation, dialect: str) -> None:
        self.conversation = conversation
        self.cli = cli.CLIS[dialect](conversation)
        self.turn = 0
        self.step = 0
        self.loop_tokens = 0
        self.delivered: list[Call] = []
        self.last_forwarded: dict | None = None
        self.digests = [_digest(item) for item in self.cli.items]  # of each history item when it was added

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


def _estimate(events: list[dict]) -> int | None:
    request = next((e for e in reversed(events) if e.get("event") == "request"), {})
    return request.get("sent_estimate", request.get("estimate"))


def run(scenario: Scenario, dialect: str, *, find_stubs: StubFinder = stubs.find_stubs,
        check_wire: bool = False) -> Run:
    """Play ``scenario`` on ``dialect`` (``claude`` or ``codex``) and measure it. ``check_wire`` also
    serializes every forwarded request, to prove the byte count (slow; a test uses it)."""
    if dialect not in scenario.dialects:
        raise ValueError(f"{scenario.name} does not run on {dialect}")
    with tempfile.TemporaryDirectory(prefix="inturn-") as home:
        store = RecordingStore(Path(home), "inturn")
        store.set_mcp_listed()  # as after the MCP server answered tools/list in a real session
        try:
            return _run(scenario, dialect, store, find_stubs, check_wire)
        finally:
            store.release()


def _run(scenario: Scenario, dialect: str, store: RecordingStore, find_stubs: StubFinder | None,
         check_wire: bool) -> Run:
    engine = RewriteEngine(store, config=Config(bound=scenario.bound))
    upstream = FakeUpstream()
    actors = {c.name: _Actor(c, dialect) for c in scenario.conversations}
    result = Run(scenario, dialect)
    endpoint = cli.CLIS[dialect].endpoint
    field_name = "messages" if dialect == "claude" else "input"
    originals = {(cli.claude_call_id if dialect == "claude" else cli.codex_call_id)(call.key): call
                 for c in scenario.conversations for call in c.calls}
    stubs_seen: dict[stubs.StubRef, bool] = {}

    for name in scenario.schedule:
        actor = actors[name]
        turn = actor.turn
        in_loop = not actor.at_prompt()
        if not in_loop:
            before = len(actor.cli.items)
            actor.cli.prompt(actor.conversation.turns[turn])
            actor.loop_tokens = 0
            _added(actor, upstream, dialect, actor.cli.items[before:])
        step = actor.advance()
        request = actor.cli.request()

        engine.prime(request)
        rooms, sides = len(store.turn_rooms), len(store.side_published)
        forwarded, _, events = engine(request, dict(actor.cli.headers), endpoint)
        room_at_request = store.turn_rooms[-1] if len(store.turn_rooms) > rooms else None
        reply = upstream.respond(name, endpoint, forwarded, step)
        if check_wire:
            assert reply.wire_bytes == len(serialize_request(forwarded)), "the byte count is wrong"
        rooms = len(store.turn_rooms)
        usage_events = engine.observe_usage(endpoint, dict(reply.usage), request, tool_calls=reply.tool_calls,
                                            tool_call_ids=reply.call_ids)
        room = store.turn_rooms[-1] if len(store.turn_rooms) > rooms else None
        if room is None:
            room = next((r for r in reversed(store.side_published[sides:]) if r is not None), None)

        for event in (*events, *usage_events):
            result.events[event.get("event", "?")] += 1
        over = [e.get("source") for e in (*events, *usage_events) if e.get("event") == "over_bound"]
        result.sent.append(Sent(name, turn, in_loop, actor.loop_tokens, len(request[field_name]),
                                len(forwarded.get(field_name) or []), _estimate(events), reply.input_tokens,
                                reply.wire_bytes, reply.cache_read, reply.resent, "estimate" in over,
                                "real" in over, room_at_request, room, reply.violations))
        actor.last_forwarded = forwarded
        if find_stubs is not None:
            try:
                found = find_stubs(dialect, forwarded)
            except NotImplementedError:
                find_stubs = None
            else:
                for ref in found:
                    if ref not in stubs_seen:
                        stubs_seen[ref] = _reachable(store, ref, originals)
        _added(actor, upstream, dialect, actor.cli.take(reply.output, step))
        actor.delivered.extend(step.calls)

    unfinished = [name for name, actor in actors.items() if not actor.done()]
    if unfinished:
        raise RuntimeError(f"{scenario.name}: the schedule leaves {unfinished} unfinished")
    result.violations = dict(upstream.violations)
    result.rejected = upstream.rejected
    result.info = dict(upstream.info)
    result.pages = len(store.page_ids())
    if find_stubs is not None:
        result.stubs = {"found": len(stubs_seen), "unreachable": sum(1 for ok in stubs_seen.values() if not ok)}
    stored = "\n".join(dumps(store.get_page(page_id)) for page_id in store.page_ids())
    counts = Counter({"total": 0, "in_view": 0, "stored": 0, "lost": 0})
    for actor in actors.values():
        view = dumps(actor.last_forwarded[field_name])
        for call in actor.delivered:
            if not call.head:
                continue
            counts["total"] += 1
            if call.head in view and call.tail in view:
                counts["in_view"] += 1
            elif call.head in stored and call.tail in stored:
                counts["stored"] += 1
            else:
                counts["lost"] += 1
        result.history_tokens[actor.conversation.name] = upstream.request_tokens(endpoint, actor.cli.request())
        result.history_digests_stable &= actor.digests == [_digest(i) for i in actor.cli.items]
    result.originals = dict(counts)
    return result


def _digest(item: dict) -> str:
    return scripts.digest(dumps(item))


def _added(actor: _Actor, upstream: FakeUpstream, dialect: str, items: list[dict]) -> None:
    actor.loop_tokens += sum(upstream.tokens(dialect, item) for item in items)
    actor.digests.extend(_digest(item) for item in items)


def _reachable(store: SessionStore, ref: stubs.StubRef, originals: dict[str, Call]) -> bool:
    page = store.get_page(ref.page_id)
    if page is None:
        return False
    text = dumps(page)
    for call_id in ref.call_ids:
        call = originals.get(call_id)
        if call is None or not call.head or call.head not in text or call.tail not in text:
            return False
    return True


def script_size(scenario: Scenario, dialect: str) -> dict[str, int]:
    """What the script holds, before any engine sees it: requests, tool rounds, and its tool results
    in the upstream's tokens."""
    render = cli.claude_tool_result if dialect == "claude" else cli.codex_output
    results = [block_tokens(render(call)) for c in scenario.conversations for call in c.calls]
    return {"requests": len(scenario.schedule), "rounds": sum(c.rounds for c in scenario.conversations),
            "result_tokens": sum(results), "largest_result": max(results),
            "images": sum(len(call.images) for c in scenario.conversations for call in c.calls)
            + sum(len(t.images) for c in scenario.conversations for t in c.turns)}


@functools.cache
def cached(name: str, dialect: str, full: bool = False) -> Run:
    """One run per scenario and dialect for a whole test session."""
    return run(scripts.build(name, full=full), dialect)
