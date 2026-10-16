"""Acceptance detectors over the Contexto session log and Claude Code's session JSONL.

Session log (``$CONTEXTO_HOME/sessions/<id>/`` JSONL, lane A schema per builder-1, 2026-10-16):
``{"event": "request", "path", "status", "usage": {...} | null, "over_bound", "epoch",
"transform_events": [...]}`` per upstream response; ``{"event": "upstream_4xx", ...}`` per 4xx; and
lane B's ``{"event": "mcp_call", "tool", "ok", ...}`` per MCP tool call. Rotation and resync details
are engine events inside ``transform_events``; a rotation is an event named exactly ``rotation``
(a ``rotation_noop`` pages nothing and is not counted).

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
    # Request indexes whose measured input passed the bound without the engine's flag. The engine
    # flags over-bound from its own estimate before sending (rewrite.py), so an entry here means
    # the estimate ran low for that request: it fails acceptance 1 and points at the estimator.
    unlogged_over_bound: list[int] = field(default_factory=list)
    max_input: int = 0
    # /v1/messages requests with no usage (null or missing): their size cannot be checked against
    # the bound, so they must not count as passing acceptance 1.
    unverified_requests: list[int] = field(default_factory=list)
    rotations: int = 0
    epochs: int = 0
    requests_per_epoch: dict[str, int] = field(default_factory=dict)
    engine_events: dict[str, int] = field(default_factory=dict)  # every transform event, by name
    stream_compactions: int = 0  # stream-json compact_boundary events seen by the driver
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
            "zero_compaction": self.compaction_events == 0 and self.stream_compactions == 0,
            "over_bound_only_when_logged": not self.unlogged_over_bound,
            "every_request_has_usage": not self.unverified_requests,
            "zero_4xx": self.upstream_4xx == 0 and not any(400 <= s < 500 for s in self.statuses),
            "recall_called": self.recall_ok > 0,
        }


def paged_statements(session_dir: Path, statements: Iterable[str]) -> dict[str, bool]:
    """Which plant statements appear in a stored page: proof that their turn was paged out."""
    texts = [p.read_text(encoding="utf-8") for p in sorted((session_dir / "pages").glob("*.json"))]
    out = {}
    for st in statements:
        needle = json.dumps(st)[1:-1]  # as the statement appears inside the page's JSON
        out[st] = any(needle in t for t in texts)
    return out


def analyze(session_log: Iterable[dict[str, Any]], cc_jsonl: Iterable[dict[str, Any]], bound: int,
            stream_compactions: int = 0) -> Report:
    r = Report(stream_compactions=stream_compactions)
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
            if not isinstance(te, dict):
                continue
            name = _event_name(te)
            r.engine_events[name] = r.engine_events.get(name, 0) + 1
            if name == "rotation":
                r.rotations += 1
        epoch = ev.get("epoch")
        r.requests_per_epoch[str(epoch)] = r.requests_per_epoch.get(str(epoch), 0) + 1
        inp = _upstream_input(ev.get("usage"))
        if inp is None:
            r.unverified_requests.append(index)
        else:
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
