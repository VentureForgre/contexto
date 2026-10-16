"""Acceptance detectors over the Contexto session log and Claude Code's session JSONL.

Session log (``$CONTEXTO_HOME/sessions/<id>/`` JSONL, lane A schema per builder-1, 2026-10-16):
``{"event": "request", "path", "status", "usage": {...} | null, "over_bound", "epoch",
"transform_events": [...]}`` per upstream response; ``{"event": "upstream_4xx", ...}`` per 4xx; and
lane B's ``{"event": "mcp_call", "tool", "ok", ...}`` per MCP tool call. Rotation and resync details
are engine events inside ``transform_events``; their names are builder-2's, so this module counts
any transform event whose name mentions rotation (an assumption to confirm when the engine lands).

Claude Code's JSONL marks a compaction with a ``system`` record whose subtype is
``compact_boundary`` and a ``user`` record with ``isCompactSummary`` [inferred from the 2.1.301
binary by lane 1, PR #9; not vendor-documented].
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MESSAGES = "/v1/messages"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line from a crash is not an event
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _upstream_input(usage: dict[str, Any] | None) -> int | None:
    if not usage:
        return None
    return sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def _event_name(ev: dict[str, Any]) -> str:
    return str(ev.get("event") or ev.get("type") or ev.get("kind") or "")


@dataclass
class Report:
    requests: int = 0
    statuses: dict[int, int] = field(default_factory=dict)
    upstream_4xx: int = 0
    over_bound_requests: int = 0  # usage above the bound
    over_bound_logged: int = 0  # requests flagged over_bound by the engine
    unlogged_over_bound: list[int] = field(default_factory=list)  # request indexes over the bound but not flagged
    max_input: int = 0
    rotations: int = 0
    epochs: int = 0
    cache_eligible: int = 0  # requests after the first in their epoch
    cache_hits: int = 0  # eligible requests that read cache
    cache_misses: list[int] = field(default_factory=list)  # request indexes of eligible full misses
    compaction_events: int = 0
    recall_calls: int = 0
    recall_ok: int = 0
    mcp_calls: dict[str, int] = field(default_factory=dict)

    @property
    def cache_hit_rate(self) -> float | None:
        return self.cache_hits / self.cache_eligible if self.cache_eligible else None

    def verdicts(self) -> dict[str, bool]:
        return {
            "zero_compaction": self.compaction_events == 0,
            "over_bound_only_when_logged": not self.unlogged_over_bound,
            "zero_4xx": self.upstream_4xx == 0 and not any(400 <= s < 500 for s in self.statuses),
            "recall_called": self.recall_ok > 0,
        }


def analyze(session_log: Iterable[dict[str, Any]], cc_jsonl: Iterable[dict[str, Any]], bound: int) -> Report:
    r = Report()
    seen_epochs: set[Any] = set()
    first_in_epoch: set[Any] = set()
    index = 0
    for ev in session_log:
        name = ev.get("event")
        if name == "upstream_4xx":
            r.upstream_4xx += 1
            continue
        if name == "mcp_call":
            tool = str(ev.get("tool"))
            r.mcp_calls[tool] = r.mcp_calls.get(tool, 0) + 1
            if tool == "contexto_recall":
                r.recall_calls += 1
                r.recall_ok += int(bool(ev.get("ok")))
            continue
        if name != "request" or ev.get("path") != MESSAGES:
            continue  # count_tokens and other paths have no turn effect
        index += 1
        r.requests += 1
        status = int(ev.get("status") or 0)
        r.statuses[status] = r.statuses.get(status, 0) + 1
        for te in ev.get("transform_events") or []:
            if isinstance(te, dict) and "rotat" in _event_name(te).lower():
                r.rotations += 1
        epoch = ev.get("epoch")
        inp = _upstream_input(ev.get("usage"))
        if inp is not None:
            r.max_input = max(r.max_input, inp)
            if inp > bound:
                r.over_bound_requests += 1
                if not ev.get("over_bound"):
                    r.unlogged_over_bound.append(index)
        if ev.get("over_bound"):
            r.over_bound_logged += 1
        if epoch not in seen_epochs:
            seen_epochs.add(epoch)
            first_in_epoch.add(index)
        elif inp is not None and status == 200:
            r.cache_eligible += 1
            if int((ev.get("usage") or {}).get("cache_read_input_tokens") or 0) > 0:
                r.cache_hits += 1
            else:
                r.cache_misses.append(index)
    r.epochs = len(seen_epochs)
    for rec in cc_jsonl:
        if (rec.get("type") == "system" and rec.get("subtype") == "compact_boundary") or rec.get("isCompactSummary"):
            r.compaction_events += 1
    return r
