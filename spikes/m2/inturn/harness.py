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

With ``check_wire`` (the target tests run with it) every forwarded request is also serialized as the
proxy writes it, to prove the byte count, and the CLI's own history and head are digested again after
the engine's call: an engine that edits them for the send and restores them before the next request
fails the run.
"""

from __future__ import annotations

import functools
import io
import json
import os
import re
import statistics
import tempfile
import unicodedata
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
TRUTH_SHARE = 0.9  # a request-time room may count the request at this share of the upstream's tokens
STUB_DEPTH = 8  # how many pages a chain of stubs is followed through
COUNT_EVERY = 16
SESSION = "inturn"
PAGE_LINE = re.compile(r"(\S+) \(turn \d+\)")
HIT_LINE = re.compile(r"> (\d+): ")
PAGE_ID = re.compile(r"pg_[0-9a-f]{16}")  # contexto.store.pages.page_id
OMITTED = re.compile(r"\((\d+) older pages omitted; use contexto_search")  # render_index's line past its budget
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
    unnamed: int = 0  # results delivered so far, not whole in this request and not accounted for in it (rule 3)
    estimate_before: int | None = None  # the engine's estimate of the request without this request's rewrite
    output_tokens: int = 0  # the response, by the upstream's tokenizer
    cache_read_any: int = 0  # the any-prefix read (upstream.py): on Messages an upper bound
    resent_any: int = 0  # a rewrite: re-sent with that read; on Messages a lower bound


class Stubs:
    """Checks ``StubRef``s against the store (see ``stubs.py`` for the rules).

    Reachability is defined once, here. A call is reachable from a page when the page holds the call's
    original result byte for byte, or holds a stub or fold reference (``find_stubs`` on the page's
    messages) whose page leads to it, through at most ``STUB_DEPTH`` pages. A reference with
    ``call_ids`` leads only to those calls; one with empty ``call_ids`` references its page as a whole
    and leads to every call reachable from it."""

    def __init__(self, store: SessionStore, upstream: FakeUpstream, dialect: str, find: StubFinder,
                 originals: dict[str, bytes], own: Callable[[], set[str]]) -> None:
        self.store, self.upstream, self.dialect, self.find = store, upstream, dialect, find
        self.originals, self.own = originals, own
        self.field = "messages" if dialect == "claude" else "input"
        self.seen: dict[StubRef, bool] = {}
        self.covers: dict[str, frozenset[str]] = {}  # page id -> the calls reachable from it

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

    def reach(self, page_id: str) -> frozenset[str]:
        """The delivered calls reachable from page ``page_id``; empty when there is no such page."""
        known = self.covers.get(page_id)
        if known is None:
            known = self.covers[page_id] = self._reach(page_id, 0, {})
        return known

    def _reach(self, page_id: Any, depth: int, memo: dict[tuple[str, int], frozenset[str]]) -> frozenset[str]:
        if not isinstance(page_id, str):
            return frozenset()
        if (page_id, depth) in memo:
            return memo[page_id, depth]
        memo[page_id, depth] = frozenset()  # a page that leads back to itself adds nothing
        try:
            page = self.store.get_page(page_id)
        except (PageUnreadableError, ValueError, OSError):
            page = None
        if page is None or not isinstance(page.get("messages"), list):
            return frozenset()
        found = {call_id for call_id, contents in self.page_results(page).items()
                 if self.originals.get(call_id) in contents}
        if depth < STUB_DEPTH:
            try:
                nested = self.find(self.dialect, {self.field: page["messages"]})
            except NotImplementedError:
                nested = []
            for inner in nested:
                if not isinstance(inner.call_ids, tuple) or inner.page_id is None:
                    continue
                wanted = {call_id for call_id in inner.call_ids if call_id not in found}
                if inner.call_ids and not wanted:
                    continue
                further = self._reach(inner.page_id, depth + 1, memo)
                found |= further & wanted if inner.call_ids else further
        memo[page_id, depth] = frozenset(found)
        return memo[page_id, depth]

    def names(self, found: list[StubRef]) -> set[str]:
        """The calls the stubs and fold notes in ``found`` name. A reference with ``call_ids`` names each
        of them whose own original is reachable from its page (a fold note naming ten calls whose page
        holds nine names those nine); one with empty ``call_ids`` names every call reachable from its
        page, so a page that leads nowhere names nothing."""
        named: set[str] = set()
        for ref in found:
            if not isinstance(ref.call_ids, tuple):
                continue
            if ref.page_id is None:
                own = self.own()
                named.update(call_id for call_id in ref.call_ids if isinstance(call_id, str) and call_id in own)
                continue
            reached = self.reach(ref.page_id)
            named.update(reached if not ref.call_ids else (c for c in ref.call_ids if c in reached))
        return named

    def reachable(self, ref: StubRef) -> bool:
        """Whether ``ref`` leads to its originals: every call it names is reachable from its page, or,
        for a reference to a page as a whole, at least one delivered call is. A reference with no page
        leads to its originals only when every call it names is one of Contexto's own."""
        if not isinstance(ref.call_ids, tuple):
            return False
        if ref.page_id is None:
            own = self.own()
            return bool(ref.call_ids) and all(call_id in own for call_id in ref.call_ids)
        reached = self.reach(ref.page_id)
        return bool(reached) if not ref.call_ids else all(call_id in reached for call_id in ref.call_ids)


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
    # Per conversation, the head its first request carried, by the upstream's count: system and tools (Codex:
    # instructions and tools, and the items before the first prompt), as the CLI sent them.
    heads: dict[str, int] = field(default_factory=dict)

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

    def loop_resent(self) -> dict[tuple[str, int], tuple[int, int]]:
        """Per loop: the tokens its in-loop rewrites re-sent (the headline figure), and its own tokens."""
        return {key: (sum(s.resent for s in sent if s.in_loop and s.rewrite), max(s.loop_tokens for s in sent))
                for key, sent in self.loops().items()}

    def truth(self, sent: Sent) -> tuple[int, int]:
        """The most a request-time room and a response-time room may say for ``sent``: what the
        upstream counted, less the headroom floor (``targets.room``)."""
        config = self.config
        return (max(0, int(config.bound - config.headroom_floor - TRUTH_SHARE * sent.tokens)),
                max(0, config.bound - (sent.tokens + sent.output_tokens) - config.headroom_floor))

    def rooms(self, conversation: str) -> dict[str, int | None]:
        sent = [s for s in self.sent if s.conversation == conversation]
        rooms = [s.room for s in sent if s.room is not None]
        late = [s for s in sent if s.loop_tokens > LOOP_START]
        low = self.config.min_turn_room
        truths = [self.truth(s) for s in sent]
        return {"published": len(rooms),
                "published_at_request": sum(1 for s in sent if s.room_at_request is not None),
                "min": min(rooms) if rooms else None,
                "median": statistics.median_low(rooms) if rooms else None,
                "zero": sum(1 for room in rooms if room == 0),
                "past_50k": len(late),
                "request_under_min_past_50k": sum(1 for s in late
                                                  if s.room_at_request is not None and s.room_at_request < low),
                "response_zero_past_50k": sum(1 for s in late if s.room == 0),
                "response_under_half_min_past_50k": sum(1 for s in late if s.room is not None and s.room < low // 2),
                "request_over_truth": sum(1 for s, (most, _) in zip(sent, truths)
                                          if s.room_at_request is not None and s.room_at_request > most),
                "response_over_truth": sum(1 for s, (_, most) in zip(sent, truths)
                                           if s.room is not None and s.room > most)}

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
        loops = self.loop_resent()
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
            "cache_read_any_share": round(sum(s.cache_read_any for s in self.sent) / total, 4),
            "rewrites": {"count": len(rewrites), "in_loop": sum(1 for s in rewrites if s.in_loop),
                         "invalidated": sum(s.invalidated for s in rewrites), "resent": resent,
                         "mean_resent": round(resent / len(rewrites)) if rewrites else 0,
                         "largest_resent": max((s.resent for s in rewrites), default=0),
                         "resent_share": round(resent / total, 4),
                         "resent_any": sum(s.resent_any for s in rewrites),
                         "resent_any_share": round(sum(s.resent_any for s in rewrites) / total, 4),
                         "loop_tokens": sum(own for _, own in loops.values()),
                         "loop_resent_over_ceiling": sum(1 for spent, own in loops.values()
                                                         if spent > max(own, self.scenario.bound))},
            "thinking": {"newest_missing": sum(s.latest_thinking_missing for s in self.sent)},
            "arrival": {"requests_with_a_stubbed_new_result": sum(1 for s in self.sent if s.stubbed_on_arrival),
                        "results_stubbed_on_arrival": sum(s.stubbed_on_arrival for s in self.sent)},
            "heads": self.heads,
            "history_tokens": self.history_tokens,
            "scripted_tokens": self.scripted_tokens,
            "originals": self.originals,
            "requests_with_a_lost_result": sum(1 for s in self.sent if s.lost),
            "requests_with_an_unnamed_result": sum(1 for s in self.sent if s.unnamed),
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
        self.prompts: list[str] = []  # each turn's prompt text, in order
        self.call_turn: dict[str, int] = {}  # call id -> the turn (index into prompts) that delivered its result
        self.answers: dict[int, list[str]] = {}  # turn -> the text of each of its assistant messages that has text
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


def _read_texts(item: Any) -> list[str]:
    """The text the model reads of a forwarded message or item as prose: a string content, its ``text``,
    ``input_text`` and ``output_text`` parts, and the text of a plain-text ``document`` block. Tool results
    and outputs are left out, so a page id inside some tool's output (or a request field the model never
    reads) names nothing."""
    if not isinstance(item, dict):
        return []
    content = item.get("content")
    if isinstance(content, str):
        return [content]
    found = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "input_text", "output_text") and isinstance(block.get("text"), str):
            found.append(block["text"])
        elif block.get("type") == "document" and isinstance(block.get("source"), dict) \
                and isinstance(block["source"].get("data"), str) and block["source"].get("type") == "text":
            found.append(block["source"]["data"])
    return found


WINDOW = 40  # characters: a turn is still in view when any window this long of its prompt or answers is


def _bare(text: str) -> str:
    """``text`` without whitespace, Unicode format characters (zero-width spaces and joiners) and
    non-spacing or enclosing marks (a combining grapheme joiner), so none can hide a turn still in view."""
    return "".join(c for c in text if not c.isspace() and unicodedata.category(c) not in ("Cf", "Mn", "Me"))


def _windows(texts: tuple[str, ...]) -> set[str]:
    """Every ``WINDOW``-character run of ``texts`` (bare), one every half window and the last one; a text
    shorter than the window counts whole."""
    found: set[str] = set()
    step = max(1, WINDOW // 2)
    for text in texts:
        t = _bare(text)
        if len(t) <= WINDOW:
            found.update([t] if t else [])
        else:
            found.update(t[i:i + WINDOW] for i in range(0, len(t) - WINDOW + 1, step))
            found.add(t[-WINDOW:])
    return found


def _turn_left(turn: int, texts: list[tuple[str, ...]], haystack: str, parts: set[str]) -> bool:
    """Rule 3: whether turn ``turn`` (``texts[turn]`` is its prompt and answers) has left the request whose
    bare text is ``haystack`` and whose bare text parts are ``parts``. Only the turn's own runs count: a run
    found anywhere in another turn's text (a recurring short prompt, a template, a quote of an earlier
    prompt) says nothing about which turn is there. A run shorter than the window (a whole short text such
    as "ok" or "Done.") counts only when it is a whole text part of the request, not inside other text."""
    others = [_bare(x) for t, own in enumerate(texts) if t != turn for x in own]
    mine = [w for w in _windows(texts[turn]) if not any(w in other for other in others)]
    return not any((w in parts) if len(w) < WINDOW else (w in haystack) for w in mine)


def _estimate(events: list[dict]) -> tuple[int | None, int | None]:
    """The engine's pre-send estimates from its ``request`` event: of what it forwarded
    (``sent_estimate``, else ``estimate``), and of the request without this request's rewrite
    (``estimate``)."""
    request = next((e for e in reversed(events) if e.get("event") == "request"), {})
    return request.get("sent_estimate", request.get("estimate")), request.get("estimate")


def run(scenario: Scenario, dialect: str, *, find_stubs: StubFinder = stubs.find_stubs,
        engine: EngineFactory | None = None, check_wire: bool = False, index_budget: int | None = None) -> Run:
    """Play ``scenario`` on ``dialect`` (``claude`` or ``codex``) and measure it.

    ``engine(store, config)`` builds the engine under test; the default is the real ``RewriteEngine``.
    It is called like the proxy calls one: ``prime(request)``, ``engine(request, headers, endpoint)``
    returning ``(forwarded, headers, events)``, and ``observe_usage(...)`` returning events.
    ``check_wire`` also serializes every forwarded request, to prove the byte count. ``index_budget``
    sets the engine's ``Config.index_budget``, to run a session whose index lists only its newest pages."""
    if dialect not in scenario.dialects:
        raise ValueError(f"{scenario.name} does not run on {dialect}")
    with tempfile.TemporaryDirectory(prefix="inturn-") as home:
        store = RecordingStore(Path(home), SESSION)
        store.set_mcp_listed()  # as after the MCP server answered tools/list in a real session
        reader = SessionStore(Path(home), SESSION)  # the MCP server's own handle: another process, in real use
        try:
            return _Play(scenario, dialect, store, reader, find_stubs, engine, check_wire, index_budget).play()
        finally:
            reader.release()
            store.release()


class _Play:
    def __init__(self, scenario: Scenario, dialect: str, store: RecordingStore, reader: SessionStore,
                 find_stubs: StubFinder | None, engine: EngineFactory | None, check_wire: bool,
                 index_budget: int | None = None) -> None:
        self.scenario, self.dialect, self.store, self.check_wire = scenario, dialect, store, check_wire
        self.config = (Config(bound=scenario.bound) if index_budget is None
                       else Config(bound=scenario.bound, index_budget=index_budget))
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
                actor.call_turn[call_id] = len(actor.prompts) - 1
            if item.get("role") == "assistant":
                text = "".join(_read_texts(item))
                if text.strip():
                    actor.answers.setdefault(len(actor.prompts) - 1, []).append(text)

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

    def unnamed(self, actor: _Actor, forwarded: dict, items: list, missing: list[str]) -> int:
        """Rule 3: of ``missing`` (results left out of ``forwarded`` that no ``StubRef`` in it names),
        how many are not accounted for. Only a result of an earlier turn whose call is gone from the
        request as well can be, and only when the text the model reads names where it went: a page id
        in the request's own text (a text part of a message, not a tool result and not another request
        field) reaches it; or the whole turn left (no 40-character run of its prompt or of any of its
        answers that is found in no other turn's text, whitespace, format characters and combining marks
        removed, is anywhere in that text; a run shorter than 40 characters counts only as a whole text
        part), that text
        carries the index's "(N older pages omitted; use contexto_search" line with N at least 1 and at
        most the number of store pages it does not name, and the result is stored byte for byte in some
        page. An index past its budget lists only the newest pages, so a correct long session needs the
        second way. ``system`` is not read: the engine sends it byte for byte (contract rule 1)."""
        current = len(actor.prompts) - 1
        earlier = [call_id for call_id in missing if actor.call_turn.get(call_id, current) < current]
        if not earlier:
            return len(missing)
        calls = {call_id for item in items for call_id in item.uses}
        gone = [call_id for call_id in earlier if call_id not in calls]
        if not gone:
            return len(missing)
        texts = "\0".join(text for item in forwarded.get(self.field) or [] for text in _read_texts(item))
        reached: set[str] = set()
        for page_id in set(PAGE_ID.findall(texts)):
            reached |= self.stubs.reach(page_id)
        named = set(PAGE_ID.findall(texts))
        unlisted = sum(1 for name in self.page_names if name[:-5] not in named)
        omitted = any(1 <= int(n) <= unlisted for n in OMITTED.findall(texts))
        haystack = _bare(texts)
        parts = {_bare(x) for x in texts.split("\0")}
        turn_texts = [(actor.prompts[t], *actor.answers.get(t, ())) for t in range(len(actor.prompts))]

        def left(turn: int) -> bool:
            return _turn_left(turn, turn_texts, haystack, parts)

        accounted = sum(1 for call_id in gone if call_id in reached or (
            omitted and left(actor.call_turn[call_id]) and actor.results[call_id] in self.paged.get(call_id, ())))
        return len(missing) - accounted

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
            actor.prompts.append(actor.conversation.turns[turn].prompt)
            actor.loop_tokens = 0
            new = actor.cli.items[before:]
            actor.prompt_tokens = upstream.tokens(self.dialect, new[0])
            self.added(actor, new)
            if actor.requests == 0:  # the head: system and tools, and on Codex the items before the first prompt
                result.heads[name] = (upstream.head_tokens(self.endpoint, actor.cli.request())
                                      + sum(upstream.tokens(self.dialect, item) for item in actor.cli.items[:before]))
        step = actor.advance()
        if step.probe:
            step = self.resolve(actor, step)
        request = actor.cli.request()

        self.engine.prime(request)
        rooms, sides = len(store.turn_rooms), len(store.side_published)
        forwarded, _, events = self.engine(request, dict(actor.cli.headers), self.endpoint)
        room_at_request = store.turn_rooms[-1] if len(store.turn_rooms) > rooms else None
        if self.check_wire:
            self.unchanged(actor)
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

        # What became of every result delivered so far: whole in this request, or accounted for in it
        # (``unnamed``, rule 3); and whether it is in some page at all.
        self.index_pages()
        view: dict[str, set[bytes]] = {}
        for item in reply.items:
            for call_id, content in zip(item.results, item.contents):
                view.setdefault(call_id, set()).add(content)
        unstored: set[str] = set()  # the calls a stub in this request stands for without a stored copy
        named: set[str] = set()
        if self.find is not None:
            try:
                found = self.find(self.dialect, forwarded)
            except NotImplementedError:
                self.find = None
            else:
                for ref in found:
                    if self.stubs.check(ref) and ref.page_id is None:
                        unstored.update(ref.call_ids)
                named = self.stubs.names(found)
        missing = [call_id for call_id, content in actor.results.items() if content not in view.get(call_id, ())]
        unnamed = self.unnamed(actor, forwarded, reply.items, [c for c in missing if c not in named])
        lost = sum(1 for call_id in missing
                   if actor.results[call_id] not in self.paged.get(call_id, ()) and call_id not in unstored)
        stubbed = sum(1 for call_id in actor.fresh if actor.results[call_id] not in view.get(call_id, ()))
        unseen = (upstream.head_tokens(self.endpoint, request) + actor.prompt_tokens
                  + sum(actor.result_tokens[call_id] for call_id in actor.fresh))

        for event in (*events, *usage_events):
            result.events[event.get("event", "?")] += 1
        over = [e.get("source") for e in (*events, *usage_events) if e.get("event") == "over_bound"]
        estimate, estimate_before = _estimate(events)
        sent = Sent(name, turn, in_loop, actor.loop_tokens, actor.appended_tokens, len(request[self.field]),
                    len(forwarded.get(self.field) or []), estimate, reply.input_tokens, reply.wire_bytes,
                    reply.cache_read, reply.rewrite, reply.invalidated or 0, reply.resent or 0, reply.latest_missing,
                    "estimate" in over, "real" in over, room_at_request, room, reply.tool_calls, unseen, stubbed,
                    lost, reply.violations, unnamed=unnamed, estimate_before=estimate_before,
                    output_tokens=reply.output_tokens, cache_read_any=reply.cache_read_any,
                    resent_any=reply.resent_any or 0)
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

    def unchanged(self, actor: _Actor) -> None:
        """``check_wire``: the CLI's own history and head are as the CLI made them, right after the
        engine's call (``finish`` checks again at the end, for every conversation)."""
        if (actor.head_digest != _digest(actor.cli.head())
                or any(_digest(item) != known for item, known in zip(actor.cli.items, actor.digests))):
            raise RuntimeError(f"{actor.conversation.name}: the engine changed the CLI's own history in place "
                               f"(request {len(self.result.sent) + 1})")

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
    """One run per scenario and dialect for a whole test session, with ``check_wire``."""
    return run(scripts.build(name, full=full), dialect, check_wire=True)
