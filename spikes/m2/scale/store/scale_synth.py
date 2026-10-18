"""Synthetic Claude Code turns for the store-scale spike (deterministic, no model, no network).

``session_turn(t, n_pages, ...)`` returns turn ``t`` of an ``n_pages``-page session as the list of
Messages API messages the engine pages (``strip_carried`` output: the turn-start user message, then
assistant messages with thinking, text, and tool_use blocks, and user messages with tool_result
blocks). ``to_responses`` maps a turn to Codex Responses items holding the same text.

Background text comes from fixed pools of code blocks, prose sentences, and command output, with
per-turn unique values (shas, timings, line numbers). Every pool line that contains a probe query
or a planted marker is dropped, so how often a probe query appears is set only by the injected
distractors: each query ``q`` lands on a page with probability ``distractor_rate(q)``, the share
of real Claude Code turns containing ``q`` (``calibration.json``, from ``scale_calibrate.py``),
raised to ``floor``. The background of turn ``t`` and its distractor draws depend only on ``t``
and the seed, never on ``n_pages``; only plant positions scale with the session.
"""

from __future__ import annotations

import base64
import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

from contexto.tokens import estimate_message_tokens
from scale_probes import FACTS, LATENCY_QUERIES, RENAME_MENTION_RATE, Fact, Plant, planted_turn

HERE = Path(__file__).resolve().parent
TARGET_TOKENS = (19_000, 23_000)  # the goal; the last tool call overshoots it by 0 to ~6K

VERBS = ("load", "parse", "build", "apply", "merge", "split", "encode", "decode", "verify", "render", "collect",
         "resolve", "compact", "normalize", "replay", "append", "scan", "index", "hash", "emit", "drain",
         "attach", "detach", "compute", "check", "register", "flush", "open", "close", "read", "write", "seal")
NOUNS = ("record", "entry", "frame", "page", "ledger", "chunk", "segment", "cursor", "offset", "payload",
         "envelope", "manifest", "digest", "schema", "column", "event", "span", "stamp", "window", "epoch",
         "anchor", "header", "body", "stub", "budget", "session", "message", "block", "result", "summary",
         "journal", "snapshot", "marker", "channel", "lease", "token", "estimate", "boundary", "spool", "shard")
CLASSES = ("Ledger", "Frame", "Journal", "Spool", "Cursor", "Manifest", "Envelope", "Digest", "Segment",
           "Anchor", "Lease", "Window", "Epoch", "Snapshot", "Budget", "Router", "Writer", "Reader")
PKGS = ("ledger", "wire", "api", "billing", "ingest", "auth", "store", "sched", "export", "search", "notify",
        "gateway")
TYPES = ("int", "str", "bytes", "float", "bool", "dict[str, Any]", "list[str]", "Path", "Frame", "Record")
ERRORS = ("ValueError", "KeyError", "LookupError", "RuntimeError", "OSError", "LedgerError", "FrameError")
PROSE = (
    "The {n} path reads the {m} before it checks the {k}, so the order matters here.",
    "I traced {f} through {p}; it only touches the {n} when the {m} is already sealed.",
    "This keeps {f} pure: it returns a new {n} and never mutates the {m} in place.",
    "{f} is called from three places, and only one of them passes a {n} that can be empty.",
    "The test covers the happy path but not the case where the {m} arrives before the {n}.",
    "I'll update {p} so the {n} and the {m} are compared by value instead of identity.",
    "Looks like {f} was written before the {k} existed, which explains the extra copy.",
    "Next I'll run the focused tests for {p} and then the whole suite.",
    "The diff is small: one guard in {f} and a test that pins the {n} order.",
    "Both call sites already handle the {n} being None, so the guard is enough.",
    "The {m} is rebuilt on every call, which is fine at this size but worth noting.",
    "I checked the git history for {p}; the {k} logic has not changed since it was added.",
    "Can you make {f} handle an empty {n} without raising, and add a test for it?",
    "Please look at why {p} is slow when the {m} has many entries.",
    "Let's split {f} so the {n} parsing and the {m} validation are separate.",
    "Refactor {p} to drop the duplicate {k} lookups, keep the behaviour the same.",
    "Add type hints to {f} and fix whatever mypy reports in {p}.",
    "Why does {f} return a {n} here instead of the {m}?",
)
CODE = (
    "def {f}(self, {a}: {t}) -> {t2}:",
    "    \"\"\"Return the {n} for {a}, or None when the {m} is unknown.\"\"\"",
    "    {v} = self._{n}s.get({a})",
    "    if {v} is None:",
    "        raise {e}(f\"unknown {n}: {{{a}!r}}\")",
    "    return {v}",
    "    for {v} in self.{m}s:",
    "        if {v}.{k} == {a}:",
    "            yield {v}",
    "    {v} = [{w} for {w} in {a} if {w}.{k}]",
    "    self._{m} = {g}({v})",
    "    with self._lock:",
    "        self._{n}s[{a}] = {v}",
    "class {C}{C2}:",
    "    __slots__ = (\"_{n}\", \"_{m}\")",
    "    def __init__(self, {a}: {t}) -> None:",
    "        self._{n} = {a}",
    "@dataclass(frozen=True)",
    "    {k}: {t} = field(default_factory=dict)",
    "from {pk}.{n} import {f}, {C}{C2}",
    "import json",
    "    return {g}(self._{n}, {a}=self._{m})",
    "    # {v} is sorted by {k}, so the first match wins",
    "    assert {v}.{k} is not None, {v}",
    "    {v}.update({{\"{k}\": {a}}})",
    "    log.debug(\"{f}: %s\", {v})",
)
LOG = (
    "{p}::test_{f} PASSED",
    "{p}::test_{f}_{k} PASSED",
    "{p}::test_{f} FAILED",
    "E       AssertionError: assert {v} == {w}",
    "E       {e}: unknown {n}",
    "{p}:{line}: in {f}",
    "{p}:{line}:    {v} = self._{n}s.get({a})",
    "{sha} {f}: keep the {n} order stable",
    "{sha} fix({pk}): {f} handles an empty {m}",
    "{sha} test({pk}): cover the {k} edge case",
    " {p} | {d} +{i}-",
)


def _fill(template: str, rng: random.Random) -> str:
    n, m, k = rng.choice(NOUNS), rng.choice(NOUNS), rng.choice(NOUNS)
    return template.format(
        f=f"{rng.choice(VERBS)}_{rng.choice(NOUNS)}", g=f"{rng.choice(VERBS)}_{rng.choice(NOUNS)}",
        n=n, m=m, k=k, a=rng.choice(NOUNS), v=rng.choice(NOUNS), w=rng.choice(NOUNS)[:3],
        t=rng.choice(TYPES), t2=rng.choice(TYPES), e=rng.choice(ERRORS), C=rng.choice(CLASSES),
        C2=rng.choice(CLASSES), pk=rng.choice(PKGS), p=_path(rng), line=rng.randrange(10, 900),
        sha=f"{rng.getrandbits(28):07x}", d=rng.randrange(1, 60), i="+" * rng.randrange(1, 12))


def _path(rng: random.Random) -> str:
    pkg, name = rng.choice(PKGS), f"{rng.choice(NOUNS)}s"
    return f"tests/test_{name}.py" if rng.random() < 0.25 else f"src/{pkg}/{name}.py"


@lru_cache(maxsize=None)
def forbidden() -> tuple[str, ...]:
    """Strings background text must never contain: every probe query and planted marker, and the
    rename identifiers (their mentions are injected at a fixed rate). The common latency queries
    are allowed: they are meant to be on nearly every page."""
    common = set(LATENCY_QUERIES["common"])
    words = {q for f in FACTS for q in f.queries} | {p.marker for f in FACTS for p in f.plants}
    words |= {q for k, qs in LATENCY_QUERIES.items() if k != "common" for q in qs}
    words |= {name for f in FACTS if f.rename for name in f.rename}
    return tuple(sorted(words - common))


def _clean(line: str) -> bool:
    return not any(w in line for w in forbidden())


@lru_cache(maxsize=None)
def pools(seed: int = 7) -> dict[str, tuple[str, ...]]:
    """Fixed line pools, filtered of probe strings (built once per process)."""
    rng = random.Random(f"pools:{seed}")
    out = {}
    for name, templates, size in (("prose", PROSE, 4000), ("code", CODE, 12000), ("log", LOG, 4000)):
        lines = []
        while len(lines) < size:
            line = _fill(rng.choice(templates), rng)
            if _clean(line):
                lines.append(line)
        out[name] = tuple(lines)
    return out


NEAR_TEMPLATES = (
    "Checked the {q} handling again; nothing to change there.",
    "# {q}: see the note in the module docstring",
    "grep shows {q} in two places, both unrelated to this change.",
    "Leaving {q} as it is for now.",
    "The {q} part is covered by the existing tests.",
    "log: {q} unchanged",
)


@lru_cache(maxsize=None)
def calibration(path: str = str(HERE / "calibration.json")) -> dict[str, float]:
    try:
        return dict(json.loads(Path(path).read_text(encoding="utf-8"))["df"])
    except (OSError, ValueError, KeyError) as e:
        raise SystemExit(f"cannot read calibration {path}: {e}; run scale_calibrate.py first") from e


def distractor_rate(query: str, floor: float = 0.0) -> float:
    return max(calibration().get(query, 0.0), floor)


def near_sentences(t: int, floor: float, seed: int) -> list[str]:
    """Distractor lines for page ``t``: for each probe query, with probability ``distractor_rate``, one
    line containing the query but no planted marker. Independent of the session length."""
    out = []
    for fact in FACTS:
        for q in fact.queries:
            rng = random.Random(f"near:{seed}:{t}:{q}")
            if rng.random() >= distractor_rate(q, floor):
                continue
            # A fact's own distractor counts only if it adds no longer query of that fact: "Every feature flag
            # ..." drawn for "flag" would also raise "feature flag" above its calibrated rate.
            longer = [q2 for q2 in fact.queries if q2 != q and q2 not in q]
            own = [s for s in fact.near if q in s and not any(q2 in s for q2 in longer)]
            out.append(rng.choice(own) if own and rng.random() < 0.5 else rng.choice(NEAR_TEMPLATES).format(q=q))
    return out


def plants_for(t: int, n_pages: int) -> list[tuple[Fact, int, Plant]]:
    """The plants on page ``t`` of an ``n_pages`` session: (fact, index of the plant, plant)."""
    return [(f, i, p) for f in FACTS for i, p in enumerate(f.plants) if planted_turn(p.at, n_pages) == t]


def rename_mentions(t: int, n_pages: int, seed: int) -> list[str]:
    """Code lines naming a renamed identifier: the old name before the rename page, the new one after."""
    out = []
    for fact in FACTS:
        if not fact.rename:
            continue
        at = planted_turn(fact.plants[0].at, n_pages)
        rng = random.Random(f"rename:{seed}:{t}:{fact.id}")
        if t != at and rng.random() < RENAME_MENTION_RATE:
            name = fact.rename[0] if t < at else fact.rename[1]
            out.append(f"    result = {name}(payload)  # {rng.choice(NOUNS)}")
    return out


def _tool_id(rng: random.Random) -> str:
    return "toolu_01" + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz0123456789")
                                for _ in range(22))


def _prose(rng: random.Random, lo: int, hi: int) -> str:
    return " ".join(rng.choice(pools()["prose"]) for _ in range(rng.randrange(lo, hi)))


def _numbered(lines: list[str], start: int = 1) -> str:
    return "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(lines, start))


def _step(rng: random.Random, inserts: list[str]) -> tuple[dict[str, Any], str]:
    """One tool call: (tool_use input with name, tool_result text). ``inserts`` lines go in the result."""
    p = pools()
    kind = rng.choices(("Read", "Grep", "pytest", "git", "Edit", "Bash"), (35, 20, 15, 10, 15, 5))[0]
    if kind == "Read":
        path = _path(rng)
        lines = [rng.choice(p["code"]) for _ in range(rng.randrange(60, 200))]
        for line in inserts:
            lines.insert(rng.randrange(len(lines) + 1), line)
        return {"name": "Read", "input": {"file_path": f"/Users/dev/work/ctx/{path}"}}, _numbered(lines)
    if kind == "Grep":
        pattern = f"{rng.choice(VERBS)}_{rng.choice(NOUNS)}"
        while not _clean(pattern):
            pattern = f"{rng.choice(VERBS)}_{rng.choice(NOUNS)}"
        lines = [f"{_path(rng)}:{rng.randrange(1, 800)}:{rng.choice(p['code']).strip()}"
                 for _ in range(rng.randrange(10, 60))] + inserts
        return {"name": "Grep", "input": {"pattern": pattern, "path": "src", "output_mode": "content"}}, "\n".join(lines)
    if kind in ("pytest", "Bash"):
        cmd = f"python -m pytest -q {_path(rng)}" if kind == "pytest" else f"python -m {rng.choice(PKGS)}.tool --check"
        lines = [rng.choice(p["log"]) for _ in range(rng.randrange(20, 90))] + inserts
        failed = rng.random() < 0.15
        lines.append(f"{rng.randrange(1, 400)} passed{', 1 failed' if failed else ''} in {rng.random() * 9:.2f}s")
        if failed or rng.random() < 0.02:
            lines.append("Command failed with exit code 1")
        return {"name": "Bash", "input": {"command": cmd, "description": "Run the focused tests"}}, "\n".join(lines)
    if kind == "git":
        lines = [rng.choice(p["log"]) for _ in range(rng.randrange(15, 50))] + inserts
        return {"name": "Bash", "input": {"command": "git log --oneline -40 && git diff --stat",
                                          "description": "Show recent history"}}, "\n".join(lines)
    path = _path(rng)
    old = "\n".join(rng.choice(p["code"]) for _ in range(rng.randrange(3, 12)))
    new = "\n".join(rng.choice(p["code"]) for _ in range(rng.randrange(3, 14)))
    snippet = [rng.choice(p["code"]) for _ in range(rng.randrange(8, 20))] + inserts
    return ({"name": "Edit", "input": {"file_path": f"/Users/dev/work/ctx/{path}", "old_string": old, "new_string": new}},
            f"The file /Users/dev/work/ctx/{path} has been updated. Here's the result of running `cat -n` on a "
            f"snippet of the edited file:\n{_numbered(snippet, rng.randrange(1, 400))}")


def _thinking(rng: random.Random) -> dict[str, Any]:
    sig = base64.b64encode(rng.getrandbits(8 * 300).to_bytes(300, "big")).decode("ascii")
    return {"type": "thinking", "thinking": _prose(rng, 3, 9), "signature": sig}


def session_turn(t: int, n_pages: int, *, seed: int = 7, floor: float = 0.0,
                 target: tuple[int, int] = TARGET_TOKENS) -> list[dict[str, Any]]:
    """Turn ``t`` (0-based) of an ``n_pages`` session, as the engine pages it (Messages API)."""
    rng = random.Random(f"turn:{seed}:{t}")
    goal = rng.randrange(*target)
    plants = plants_for(t, n_pages)
    near = near_sentences(t, floor, seed)
    mentions = rename_mentions(t, n_pages, seed)
    user = _prose(rng, 1, 4)
    for _, _, plant in plants:
        if plant.user:
            user = f"{plant.user} {user}"
    texts = [p.assistant for _, _, p in plants if p.assistant]
    results = [p.tool_result for _, _, p in plants if p.tool_result] + mentions
    edits = [p.edit for _, _, p in plants if p.edit]
    for line in near:  # half in assistant text, half as a line of tool output
        (texts if rng.random() < 0.5 else results).append(line)

    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": user}]}]
    tokens = estimate_message_tokens(messages)
    # Every plant and distractor gets a step even when the turn is already at its size.
    while tokens < goal or results or edits or texts:
        if edits:
            old, new = edits.pop()
            path = "src/wire/frames.py"
            use = {"name": "Edit", "input": {"file_path": f"/Users/dev/work/ctx/{path}", "old_string": old,
                                             "new_string": new}}
            result = f"The file /Users/dev/work/ctx/{path} has been updated."
        else:
            use, result = _step(rng, [results.pop()] if results else [])
        say = texts.pop() if texts else (rng.choice(pools()["prose"]) if rng.random() < 0.6 else "")
        rid = _tool_id(rng)
        blocks: list[dict[str, Any]] = [_thinking(rng)] if rng.random() < 0.5 else []
        if say:
            blocks.append({"type": "text", "text": say})
        blocks.append({"type": "tool_use", "id": rid, "name": use["name"], "input": use["input"]})
        content: Any = result if rng.random() < 0.5 else [{"type": "text", "text": result}]
        step = [{"role": "assistant", "content": blocks},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": rid, "content": content}]}]
        messages += step
        tokens += estimate_message_tokens(step)
    messages.append({"role": "assistant", "content": [{"type": "text", "text": _prose(rng, 3, 10)}]})
    return messages


def to_responses(turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same turn as Codex Responses API items (message, reasoning, function_call, function_call_output)."""
    items: list[dict[str, Any]] = []
    for message in turn:
        role = message["role"]
        for block in message["content"]:
            kind = block["type"]
            if kind == "text":
                part = "input_text" if role == "user" else "output_text"
                items.append({"type": "message", "role": role, "content": [{"type": part, "text": block["text"]}]})
            elif kind == "thinking":
                items.append({"type": "reasoning", "summary": [], "encrypted_content": block["signature"]})
            elif kind == "tool_use":
                items.append({"type": "function_call", "name": block["name"], "call_id": "call_" + block["id"][8:],
                              "arguments": json.dumps(block["input"])})
            elif kind == "tool_result":
                inner = block["content"]
                text = inner if isinstance(inner, str) else "\n".join(p["text"] for p in inner)
                items.append({"type": "function_call_output", "call_id": "call_" + block["tool_use_id"][8:],
                              "output": text})
    return items
