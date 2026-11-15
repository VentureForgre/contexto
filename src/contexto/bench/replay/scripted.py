"""The scripted "model" of a replay: what the fake upstream replies, round by round.

There is no model in a replay. Every reply is derived from the script alone (``steps``), so it is the
same on every run, and its SHA-256 (``digest``) is pinned in the trace. One step is one model round,
which is one request:

- a ``log`` turn: the text ``OK``;
- a ``decision`` turn: the right ``DECISION <label>: <name>`` line (``Script.scripted_reply``);
- the ``toolloop`` turn: one call of the file-read tool per round, for the script's files in the
  order the turn lists them, and after the last file the text ``OK``;
- a ``probe`` turn: one call of the ``contexto_search`` tool per probe, in the order the turn asks
  them, with the first query of the probe's rule (``contexto.bench.replay.reach.first_query``), then
  one ``q_id: answer`` line per probe.

The answer lines are the generator's right answers. They are scripted, so they say nothing about
whether a model would answer correctly; they are there so that the conversation has the shape of a
real one. A replay never reports accuracy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from contexto.bench.model import Script
from contexto.bench.replay import reach

READ_TOOL = "read_file"  # the synthetic client's file-read tool
SEARCH_TOOL = "mcp__contexto__contexto_search"  # Contexto's own tool, as Claude Code names an MCP tool

RULES = {
    "log": "one round: the text OK",
    "decision": "one round: the DECISION line the turn's rule selects",
    "toolloop": f"one {READ_TOOL} call per round for each file in the order listed, then the text OK",
    "probe": "one contexto_search call per round for each probe in the order asked, with the first query of the "
             "probe's rule, then one 'q_id: answer' line per probe with the generator's answer",
}


@dataclass(frozen=True)
class Step:
    """One model round. ``kind`` is ``text`` (the reply is ``text`` and the turn ends) or ``tool_use``
    (the reply is one call of ``tool`` with ``argument``: a path for the file-read tool, a query for
    the search tool)."""

    turn: int
    round: int
    kind: str
    text: str = ""
    tool: str = ""
    argument: str = ""

    @property
    def tool_id(self) -> str:
        return f"toolu_bench_{self.turn:03d}_{self.round:02d}"

    @property
    def tool_input(self) -> dict[str, str]:
        return {"file_path": self.argument} if self.tool == READ_TOOL else {"query": self.argument}

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "text":
            return {"turn": self.turn, "round": self.round, "kind": "text", "text": self.text}
        return {"turn": self.turn, "round": self.round, "kind": "tool_use", "id": self.tool_id, "tool": self.tool,
                "input": self.tool_input}


def steps(script: Script) -> list[Step]:
    """Every model round of the session, in order."""
    by_id = {p.id: p for p in script.probes}
    out: list[Step] = []
    for turn, kind in enumerate(script.kinds):
        if kind == "toolloop":
            out += [Step(turn, n, "tool_use", tool=READ_TOOL, argument=path) for n, path in enumerate(script.reads)]
            out.append(Step(turn, len(script.reads), "text", text="OK"))
        elif kind == "probe":
            asked = [by_id[i] for i in script.probe_turn_ids[turn - script.n_content]]
            out += [Step(turn, n, "tool_use", tool=SEARCH_TOOL, argument=reach.first_query(p))
                    for n, p in enumerate(asked)]
            out.append(Step(turn, len(asked), "text", text="\n".join(f"{p.id}: {p.answer}" for p in asked)))
        else:
            out.append(Step(turn, 0, "text", text=script.scripted_reply(turn) or "OK"))
    return out


def rounds(script: Script) -> list[int]:
    """The number of model rounds, and so of requests, each turn takes."""
    counts = [0] * len(script.turns)
    for step in steps(script):
        counts[step.turn] += 1
    return counts


def digest(planned: list[Step]) -> str:
    """SHA-256 of the steps as JSON lines: one value that pins the whole scripted behaviour."""
    text = "".join(json.dumps(step.to_dict(), sort_keys=True) + "\n" for step in planned)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
