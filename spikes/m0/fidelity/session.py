"""Seeded synthetic long session with planted facts, decisions, supersessions, and obligations.

Messages use the Anthropic Messages API content-block shape (text, tool_use, tool_result) so the
paging policy is exercised against real protocol rules: roles alternate, and every assistant
tool_use is answered by a tool_result in the next user message.

Ground truth comes from the planting bookkeeping, never from scanning the generated text.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SEED = 20261015
DEFAULT_TURNS = 240

# Every planted claim uses this phrasing so a deterministic reader can find it:
#   "the <subject> is <value>"  or, for a later revision, "the <subject> is now <value>".
_FACT_SUBJECTS = (
    ("staging database port", "staging"),
    ("release codename", "release"),
    ("on-call rotation owner", "rotation"),
    ("primary deploy region", "region"),
    ("audit bucket name", "audit"),
    ("feature flag prefix", "flag"),
)
_DECISION_TOPICS = (
    ("chosen logging library", "logging"),
    ("chosen queue backend", "queue"),
    ("chosen retry policy", "retry"),
    ("chosen schema migration tool", "migration"),
)
_DETAIL_SERVICES = ("billing", "search", "gateway", "ingest", "notifier", "scheduler")
_OBLIGATIONS = (
    "rotate the staging API key",
    "write the rollback runbook",
    "pin the base image digest",
    "add a canary for the gateway",
    "backfill the audit bucket",
    "remove the legacy cron job",
    "document the retry budget",
    "upgrade the TLS bundle",
)
_FILLER_ASKS = (
    "Can you look at why the nightly job is slow?",
    "Summarize what changed in the last deploy.",
    "Run the unit tests for the parser module.",
    "Check the lint output for the api package.",
    "What does the error in the worker log mean?",
    "Grep for TODO markers in the client.",
)
_WORDS = ("amber", "basalt", "cobalt", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper")


@dataclass(frozen=True)
class Planted:
    """One planted item in the ground-truth ledger."""

    key: str  # stable id, e.g. "fact:staging", "decision:logging", "obligation:ob03"
    category: str  # "pinned_fact", "detail", "decision", "obligation"
    subject: str  # phrase used in text ("staging database port") or obligation id
    value: str  # current value, or "open" / "closed" for obligations
    turn: int  # turn index where the current value was stated
    hint: str  # keyword a reader may use to pick a paged stub to recall
    history: tuple[tuple[int, str], ...] = ()  # (turn, value) for every statement, oldest first
    text: str = ""  # obligation description


@dataclass(frozen=True)
class Probe:
    id: str
    category: str  # "early_fact", "detail", "current_decision", "obligation_status", "recent_fact"
    key: str  # Planted.key it checks
    question: str
    answer: str  # expected answer before normalization


@dataclass
class Session:
    seed: int
    messages: list[dict[str, Any]]
    turn_starts: list[int]  # message index where each turn begins (a user text message)
    planted: dict[str, Planted]
    probes: list[Probe]
    ledger: list[dict[str, Any]] = field(default_factory=list)  # pinned-state records, turn order

    @property
    def turns(self) -> int:
        return len(self.turn_starts)

    def turn_messages(self, turn: int) -> list[dict[str, Any]]:
        start = self.turn_starts[turn]
        end = self.turn_starts[turn + 1] if turn + 1 < self.turns else len(self.messages)
        return self.messages[start:end]


def _code(rng: random.Random) -> str:
    # Unguessable value: a model cannot score by prior knowledge.
    return f"{rng.choice(_WORDS)}-{rng.randrange(1000, 10000)}"


def _filler_text(rng: random.Random, lines: int, label: str) -> str:
    out = []
    for n in range(lines):
        out.append(
            f"{label} line {n:04d} level={rng.choice(('INFO', 'DEBUG', 'WARN'))} "
            f"latency_ms={rng.randrange(1, 900)} shard={rng.randrange(0, 64)} ok"
        )
    return "\n".join(out)


class _Builder:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.messages: list[dict[str, Any]] = []
        self.turn_starts: list[int] = []
        self.tool_seq = 0

    def user(self, text: str) -> None:
        self.turn_starts.append(len(self.messages))
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})

    def assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})

    def tool_call(self, name: str, tool_input: dict[str, Any], result: str, lead: str) -> None:
        self.tool_seq += 1
        use_id = f"toolu_{self.tool_seq:05d}"
        self.messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": lead},
                    {"type": "tool_use", "id": use_id, "name": name, "input": tool_input},
                ],
            }
        )
        self.messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": use_id, "content": result}],
            }
        )


def generate(seed: int = DEFAULT_SEED, turns: int = DEFAULT_TURNS) -> Session:
    """Build a session of ``turns`` turns and its probes. Same seed and turns, same session."""
    if turns < 60:
        raise ValueError("need at least 60 turns to place early, middle, and recent items")
    b = _Builder(seed)
    rng = b.rng
    planted: dict[str, Planted] = {}
    ledger: list[dict[str, Any]] = []

    early_end = max(10, turns // 10)
    recent_start = turns - max(6, turns // 20)
    schedule: dict[int, list[tuple[str, Any]]] = {}

    def at(turn: int, item: tuple[str, Any]) -> None:
        schedule.setdefault(turn, []).append(item)

    # Early pinned facts: stated once near the start and recorded in the ledger as verified.
    early_turns = rng.sample(range(1, early_end), len(_FACT_SUBJECTS) - 1)
    for (subject, hint), turn in zip(_FACT_SUBJECTS[:-1], early_turns, strict=True):
        at(turn, ("fact", (subject, hint)))
    # One recent fact as a control: it should survive every bounded window.
    at(recent_start + 1, ("fact", _FACT_SUBJECTS[-1]))

    # Details: buried inside large tool outputs and never recorded in the ledger.
    detail_turns = rng.sample(range(early_end, turns // 2), len(_DETAIL_SERVICES))
    for service, turn in zip(_DETAIL_SERVICES, detail_turns, strict=True):
        at(turn, ("detail", service))

    # Decisions: an early position, then one or two supersessions spread over the session.
    for subject, hint in _DECISION_TOPICS:
        first = rng.randrange(1, early_end)
        revisions = sorted(rng.sample(range(early_end, recent_start), rng.choice((1, 2))))
        at(first, ("decision", (subject, hint, False)))
        for turn in revisions:
            at(turn, ("decision", (subject, hint, True)))

    # Obligations: opened early or mid-session; half are closed later.
    for n, text in enumerate(_OBLIGATIONS):
        oid = f"ob{n:02d}"
        opened = rng.randrange(1, turns // 2)
        at(opened, ("open", (oid, text)))
        if n % 2 == 0:
            at(rng.randrange(opened + 5, recent_start), ("close", oid))

    for slot in range(turns):
        items = schedule.get(slot, [])
        if not items:
            _filler_turn(b, slot)
            continue
        for kind, payload in items:
            turn = len(b.turn_starts)  # actual turn index; a slot may hold several items
            if kind == "fact":
                subject, hint = payload
                value = _code(rng)
                b.user(f"Please remember this: the {subject} is {value}.")
                b.assistant(f"Recorded. The {subject} is {value}.")
                key = f"fact:{hint}"
                planted[key] = Planted(key, "pinned_fact", subject, value, turn, hint, ((turn, value),))
                ledger.append({"key": key, "kind": "fact", "subject": subject, "value": value, "turn": turn})
            elif kind == "detail":
                service = payload
                value = _code(rng)
                subject = f"{service} shard map version"
                path = f"services/{service}/config.toml"
                body = _filler_text(rng, rng.randrange(60, 140), service)
                lines = body.split("\n")
                lines.insert(rng.randrange(5, len(lines) - 5), f"# note: the {subject} is {value}")
                b.user(f"Show me the {service} service config.")
                b.tool_call("read_file", {"path": path}, "\n".join(lines), f"Reading {path}.")
                b.assistant(f"That is the {service} config; it looks consistent.")
                key = f"detail:{service}"
                planted[key] = Planted(key, "detail", subject, value, turn, service, ((turn, value),))
            elif kind == "decision":
                subject, hint, revision = payload
                value = _code(rng)
                key = f"decision:{hint}"
                if revision:
                    b.user(f"Change of plan: the {subject} is now {value}.")
                    b.assistant(f"Understood, the {subject} is now {value}; the earlier choice is superseded.")
                    old = planted[key]
                    history = old.history + ((turn, value),)
                else:
                    b.user(f"Decision: the {subject} is {value}.")
                    b.assistant(f"Noted. The {subject} is {value}.")
                    history = ((turn, value),)
                planted[key] = Planted(key, "decision", subject, value, turn, hint, history)
                ledger.append({"key": key, "kind": "decision", "subject": subject, "value": value, "turn": turn})
            elif kind == "open":
                oid, text = payload
                b.user(f"Action item {oid}: {text}. Keep it open until it is done.")
                b.assistant(f"Tracking action item {oid} as open.")
                key = f"obligation:{oid}"
                planted[key] = Planted(key, "obligation", oid, "open", turn, oid, ((turn, "open"),), text)
                ledger.append({"key": key, "kind": "obligation", "subject": oid, "value": "open", "turn": turn, "text": text})
            elif kind == "close":
                oid = payload
                key = f"obligation:{oid}"
                b.user(f"Action item {oid} is done.")
                b.assistant(f"Closing action item {oid}.")
                old = planted[key]
                planted[key] = Planted(
                    key, "obligation", oid, "closed", turn, oid, old.history + ((turn, "closed"),), old.text
                )
                ledger.append({"key": key, "kind": "closure", "subject": oid, "value": "closed", "turn": turn})

    session = Session(seed, b.messages, b.turn_starts, planted, [], ledger)
    session.probes = _probes(session)
    return session


def _filler_turn(b: _Builder, turn: int) -> None:
    rng = b.rng
    ask = rng.choice(_FILLER_ASKS)
    b.user(f"{ask} (turn {turn})")
    calls = rng.choice((0, 1, 1, 2))
    for c in range(calls):
        name = rng.choice(("run_command", "read_file", "grep"))
        tool_input = {"command": f"job-{turn}-{c}"} if name == "run_command" else {"path": f"logs/t{turn}_{c}.log"}
        b.tool_call(name, tool_input, _filler_text(rng, rng.randrange(20, 120), f"t{turn}"), "Checking.")
    b.assistant(f"Done with turn {turn}: nothing that changes the plan.")


def _probes(session: Session) -> list[Probe]:
    probes: list[Probe] = []
    for key in sorted(session.planted):
        p = session.planted[key]
        if p.category == "pinned_fact":
            category = "recent_fact" if p.hint == _FACT_SUBJECTS[-1][1] else "early_fact"
            q = f"What is the {p.subject}? Answer with the value only."
        elif p.category == "detail":
            category = "detail"
            q = f"What is the {p.subject} in the {p.hint} service config? Answer with the value only."
        elif p.category == "decision":
            category = "current_decision"
            q = f"What is the current {p.subject}? Answer with the value only."
        else:
            category = "obligation_status"
            q = f"Is action item {p.subject} open or closed? Answer open or closed."
        probes.append(Probe(f"q_{key.replace(':', '_')}", category, key, q, p.value))
    return probes
