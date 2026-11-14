"""A synthetic session of a project, end to end with the real parts and no model (D69's measurement).

``tests/test_project_measure.py`` drives sessions of one project through this harness:

- the store is a real ``SessionStore`` opened as the launcher opens it (``launcher.open_session``, so
  the working directory and the project are recorded; ``launcher.record_project_memory`` for a session
  with project memory off), and the launcher's start line is built (``launcher.project_memory_notice``);
- the real ``RewriteEngine`` is fed a Claude Code-shaped conversation request by request, with the
  usage a stub upstream would report (the request's bytes at a fixed ratio, as ``test_project_start``
  does);
- the real MCP ``Server`` on the same store answers the model's tool calls at the point a model makes
  them: after the turn-start request's response, inside the main conversation's tool loop, so the room
  the engine published is the one a search is charged against and a pin gets its author from the real
  rule (``pin_author``). Each call is a ``tool_use`` in the next request and its reply the ``tool_result``.

The content is synthetic and deterministic: standing rules the user states and the model pins as
obligations, decisions it pins (one may supersede an earlier one), closures, and facts with unique
keys and values that are only said in the conversation and never pinned. A ``Plan`` gives one turn to
each item, in order (rules, facts and decisions interleaved, then closures and superseding decisions,
then searches), and pads with filler turns so the early ones are paged out.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from contexto import launcher, mcp
from contexto.proxy import serialize_request
from contexto.rewrite import MESSAGES, Config, EpochState, RewriteEngine
from test_rewrite import FILL, assistant, request, system, user

# What a session runs under: the bound and headroom of test_project_start_bound, the default 6,000-token
# pin budget. A turn of the script is about 1,900 tokens, so 40 turns rotate several times.
CONFIG = Config(bound=50_000, headroom_floor=6_000, min_turn_room=3_000)
RATIO = 3.2  # the stub's bytes per token: denser than the estimator's seed of 3.5, so estimates run low
MARK = "Carried from earlier sessions of this project"  # how the carried section starts
TOOL_PREFIX = "mcp__contexto__"
CARRIED_LINE = re.compile(r"^- (\S+) (obligation|decision)\b.*? \(session ([^,]+), (\d{4}-\d{2}-\d{2})\): (.*)$")
OMITTED = re.compile(r"^\((\d+) more pinned items omitted; call contexto_state for the full list\)$")
OUT = os.environ.get("CONTEXTO_MEASURE_OUT")  # where a run records its numbers as JSON, when set


def session_id(tag: str, n: int) -> str:
    """A fixed session id, so labels are the same on every run."""
    return hashlib.sha256(f"{tag}-{n}".encode()).hexdigest()[:32]


def repo(base: Path, name: str = "repo") -> Path:
    """A directory with a ``.git`` entry: a project root that does not depend on where the temp dir is."""
    root = base.resolve() / name
    (root / ".git").mkdir(parents=True)
    return root


def usage(upstream: dict, ratio: float = RATIO) -> dict:
    """What the stub upstream reports for a request: its bytes at ``ratio``."""
    return {"input_tokens": int(len(serialize_request(upstream)) / ratio), "output_tokens": 5}


def record(section: str, numbers: dict[str, Any]) -> None:
    """Merge ``numbers`` into the JSON file named by ``CONTEXTO_MEASURE_OUT`` under ``section``."""
    if not OUT:
        return
    path = Path(OUT)
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    data[section] = numbers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def show(section: str, numbers: dict[str, Any]) -> None:
    """Print a section's numbers (``pytest -s``) and record them."""
    print(f"\n[D69 measure: {section}]")
    for key, value in numbers.items():
        print(f"  {key}: {json.dumps(value, sort_keys=True)}")
    record(section, numbers)


# The script ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """A standing rule the user states and the model pins as an obligation."""
    name: str
    text: str

    @property
    def body(self) -> str:
        return f"{self.name}: {self.text}"


@dataclass(frozen=True)
class Decision:
    name: str
    text: str
    supersedes: str | None = None  # the name of the decision this one replaces

    @property
    def body(self) -> str:
        return f"{self.name}: {self.text}"


@dataclass(frozen=True)
class Fact:
    """A fact said once in the conversation and never pinned: in the prompt, a tool's output or an answer."""
    key: str
    value: str
    where: str

    @property
    def line(self) -> str:
        return f"For the record, {self.key} is {self.value}."


@dataclass(frozen=True)
class Plan:
    rules: tuple[Rule, ...] = ()
    decisions: tuple[Decision, ...] = ()  # those with ``supersedes`` are pinned with the closures
    facts: tuple[Fact, ...] = ()
    closes: tuple[str, ...] = ()  # names of rules, this session's or carried, to close
    searches: tuple[str, ...] = ()  # fact keys, each searched with scope "session" and then "project"
    turns: int = 40  # at least; filler turns follow the items
    tail: tuple[Fact, ...] = ()  # facts said in the last turns, which stay in the retained window
    pins_per_turn: int = 1  # rules pinned in one tool loop (parallel calls)


def facts(tag: str, count: int) -> tuple[Fact, ...]:
    places = ("prompt", "tool", "answer")
    return tuple(Fact(f"FACTKEY{tag}{i:02d}Z", f"value-{hashlib.sha256(f'{tag}{i}'.encode()).hexdigest()[:10]}",
                      places[i % 3]) for i in range(count))


class Call(NamedTuple):
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    event: dict[str, Any] | None  # the mcp_call event the server logged
    room: tuple[str | None, int | None, int]  # the main room before the call
    room_after: tuple[str | None, int | None, int]
    ratio: float | None  # the measured bytes per token the server charged at
    turn: int

    @property
    def text(self) -> str:
        """The reply text, without search's uncharged coverage note."""
        notes = (mcp.SEARCH_COVERAGE, mcp.SEARCH_COVERAGE_UNCHECKED)
        return "".join(b["text"] for b in self.result.get("content", []) if b.get("text") not in notes)


class Row(NamedTuple):
    upstream: dict
    events: list[dict]
    usage_events: list[dict]
    real: int  # what the stub upstream counted
    estimate: int  # the engine's estimate of what it sent
    room: tuple[str | None, int | None, int]  # the main room after the request-time publish
    k: int
    turn: int
    state: EpochState  # the main conversation's state after the request


@dataclass
class Turn:
    prompt: str
    output: str
    answer: str
    calls: list[tuple[str, Callable[[], dict[str, Any]]]] = field(default_factory=list)


def plan_turns(plan: Plan, ids: dict[str, str]) -> list[Turn]:
    """The plan's turns. ``ids`` maps a record's name to its id, filled as pins return; a later call reads it."""
    def pin_rules(rules: list[Rule]) -> list[tuple[str, Callable[[], dict[str, Any]]]]:
        return [("contexto_pin", lambda r=r: {"kind": "obligation", "text": r.body}) for r in rules]

    rules = list(plan.rules)
    groups = [rules[i:i + plan.pins_per_turn] for i in range(0, len(rules), plan.pins_per_turn)]
    plain = [d for d in plan.decisions if d.supersedes is None]
    items: list[tuple[str, Any]] = []
    for i in range(max(len(groups), len(plain), len(plan.facts))):
        if i < len(groups):
            items.append(("rules", groups[i]))
        if i < len(plan.facts):
            items.append(("fact", plan.facts[i]))
        if i < len(plain):
            items.append(("decision", plain[i]))
    items += [("close", name) for name in plan.closes]
    items += [("decision", d) for d in plan.decisions if d.supersedes is not None]
    items += [("search", key) for key in plan.searches]
    count = max(plan.turns, len(items) + len(plan.tail))
    late = {count - len(plan.tail) + i: fact for i, fact in enumerate(plan.tail)}
    turns = []
    for t in range(count):
        prompt, output, answer = f"prompt {t}", f"output {t}", f"answer {t}"
        calls: list[tuple[str, Callable[[], dict[str, Any]]]] = []
        if t in late:
            items_t: tuple[str, Any] | None = ("fact", late[t])
        else:
            items_t = items[t] if t < len(items) else None
        if items_t is not None:
            kind, item = items_t
            if kind == "rules":
                prompt += "\n" + "\n".join(f"From now on, standing rule {r.body}" for r in item)
                calls += pin_rules(item)
            elif kind == "decision":
                prompt += f"\nWe decide {item.body}"
                args = {"kind": "decision", "text": item.body}
                if item.supersedes is not None:
                    calls.append(("contexto_pin", lambda a=args, s=item.supersedes: {**a, "supersedes": [ids[s]]}))
                else:
                    calls.append(("contexto_pin", lambda a=args: a))
            elif kind == "close":
                prompt += f"\nRule {item} no longer applies."
                calls.append(("contexto_close", lambda n=item: {"obligation_id": ids[n], "note": f"{n} lifted"}))
            elif kind == "search":
                prompt += f"\nWhat was {item} again?"
                calls += [("contexto_search", lambda k=item: {"query": k}),
                          ("contexto_search", lambda k=item: {"query": k, "scope": "project"})]
            else:
                fact = item
                if fact.where == "prompt":
                    prompt += "\n" + fact.line
                elif fact.where == "tool":
                    output += "\n" + fact.line
                else:
                    answer += "\n" + fact.line
        turns.append(Turn(f"{prompt}\n{FILL}", f"{output}\n{FILL}", f"{answer}\n{FILL}", calls))
    return turns


# A session -------------------------------------------------------------------------------------------

class Session:
    """One session of a project: the store as the launcher opens it, the engine, and the MCP server."""

    def __init__(self, home: Path, sid: str, workdir: Path, *, memory: bool = True, config: Config = CONFIG,
                 ratio: float = RATIO) -> None:
        self.store = launcher.open_session(home, sid, workdir)
        launcher.record_project_memory(self.store, memory)
        err = io.StringIO()
        launcher.project_memory_notice(self.store, err)
        self.start_line = err.getvalue().strip()
        self.server = mcp.Server(self.store, io.StringIO(), io.StringIO())
        self.server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}))
        self.engine = RewriteEngine(self.store, config=config)
        self.config, self.ratio = config, ratio
        self.history: list[dict] = []
        self.turn_starts: list[int] = []  # the message index of each turn's start
        self.rows: list[Row] = []
        self.calls: list[Call] = []
        self.ids: dict[str, str] = {}

    @property
    def label(self) -> str:
        return self.store.session_id[:8]

    def send(self, turn: int) -> Row:
        sent = request(list(self.history))
        upstream, _, events = self.engine(sent, {}, MESSAGES)
        room = self.store.turn_room()
        real = usage(upstream, self.ratio)
        usage_events = self.engine.observe_usage(MESSAGES, real, sent)
        event = events[-1]
        row = Row(upstream, events, usage_events, real["input_tokens"], event.get("sent_estimate", event["estimate"]),
                  room, event["k"], turn, self.state)
        self.rows.append(row)
        return row

    def call(self, tool: str, arguments: dict[str, Any], turn: int) -> Call:
        before = self.store.turn_room()
        ratio = self.store.window()[1]
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments}})
        result = self.server.handle_line(line)["result"]
        calls = [e for e in self.log() if e.get("event") == "mcp_call"]
        made = Call(tool, arguments, result, calls[-1] if calls else None, before, self.store.turn_room(), ratio,
                    turn)
        self.calls.append(made)
        return made

    def log(self) -> list[dict]:
        path = self.store.dir / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def turn(self, t: int, turn: Turn, ids: dict[str, str]) -> None:
        self.turn_starts.append(len(self.history))
        self.history.append(user(turn.prompt, context=t == 0))
        self.send(t)
        if t == 0:
            self.history.append(system())
        uses = [{"type": "tool_use", "id": f"toolu_{t}_r", "name": "Read", "input": {"file_path": f"f{t}.txt"}}]
        results = [{"type": "tool_result", "tool_use_id": f"toolu_{t}_r", "content": turn.output}]
        for j, (tool, arguments) in enumerate(turn.calls):
            args = arguments()
            made = self.call(tool, args, t)
            if tool == "contexto_pin" and not made.result["isError"]:
                ids[args["text"].split(":", 1)[0]] = made.result["structuredContent"]["record_id"]
            uses.append({"type": "tool_use", "id": f"toolu_{t}_{j}", "name": TOOL_PREFIX + tool, "input": args})
            results.append({"type": "tool_result", "tool_use_id": f"toolu_{t}_{j}",
                            "content": "".join(b["text"] for b in made.result["content"])})
        self.history += [{"role": "assistant", "content": uses}, {"role": "user", "content": results}]
        self.send(t)
        self.history.append(assistant(turn.answer))

    def run(self, plan: Plan, ids: dict[str, str] | None = None) -> Session:
        """Run ``plan``; ``ids`` (shared across the sessions of a project) gets each pinned record's id."""
        self.ids = ids if ids is not None else {}
        for t, turn in enumerate(plan_turns(plan, self.ids)):
            self.turn(t, turn, self.ids)
        return self

    def end(self) -> None:
        self.store.release()

    # What the run left -----------------------------------------------------------------------------

    @property
    def state(self) -> EpochState:
        return next(iter(self.engine.states.values()))

    def paged_turns(self) -> int:
        """How many of this session's turns are wholly paged out (before the retained window)."""
        k = self.state.k
        return sum(1 for t in range(len(self.turn_starts) - 1) if self.turn_starts[t + 1] <= k)

    def rotations(self) -> list[int]:
        """The indexes of the requests that rotated."""
        return [i for i, row in enumerate(self.rows) if any(e["event"] == "rotation" for e in row.events)]

    def text(self, i: int) -> str:
        return serialize_request(self.rows[i].upstream).decode()

    def searches(self, scope: str) -> list[Call]:
        return [c for c in self.calls if c.tool == "contexto_search" and c.arguments.get("scope", "session") == scope]


def carried_lines(text: str) -> list[tuple[str, str, str, str]]:
    """(record id, kind, session label, name) of each carried line of a carried section; the name is the
    body up to its first colon."""
    out = []
    for line in text.splitlines():
        found = CARRIED_LINE.match(line)
        if found:
            out.append((found.group(1), found.group(2), found.group(3), found.group(5).split(":", 1)[0]))
    return out


def omitted(text: str) -> int:
    """The count the carried section's omitted line gives, 0 without one."""
    for line in text.splitlines():
        found = OMITTED.match(line)
        if found:
            return int(found.group(1))
    return 0


def charge_for(text: str, ratio: float | None) -> int:
    """What the server charges for a reply of ``text``: its characters at the smaller of the measured
    bytes per token and 1.5 (``mcp._within_room``), the same for every tool and scope."""
    per = min(ratio, mcp.MAX_CHARS_PER_TOKEN) if ratio else mcp.UNMEASURED_CHARS_PER_TOKEN
    return math.ceil(len(text) / per)
