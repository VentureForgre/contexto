"""Acceptance detectors over the Contexto session log and Claude Code's session JSONL.

Session log (``$CONTEXTO_HOME/sessions/<id>/`` JSONL, lane A schema per builder-1, 2026-10-16):
``{"event": "request", "path", "status", "usage": {...} | null, "over_bound", "epoch",
"transform_events": [...]}`` per upstream response; ``{"event": "upstream_4xx", ...}`` per 4xx; and
lane B's ``{"event": "mcp_call", "tool", "ok", ...}`` per MCP tool call. Rotation and resync details
are engine events inside ``transform_events``; a rotation is an event named exactly ``rotation``
(a ``rotation_noop`` pages nothing and is not counted). Lane A's A2b (PR #48) adds standalone
``over_bound`` (``source: "real"``) and ``estimate_error`` events after each response.

Acceptance 1, under intake's strict rule for the acceptance run (2026-10-16), fails on any request
over the bound, any real-source over_bound event, any ``bound_too_small``, and any pre-send
over_bound record not marked as a tool loop. A pre-send record with ``source: "estimate"`` and
``in_tool_loop: true`` (rule 5, PR #48) is reported only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MESSAGES = "/v1/messages"
# The probe turn's cap on contexto_recall calls (script.MAX_RECALLS). The verdict counts every recall
# in the session: a build-turn recall adds unplanned tokens too, so it counts against the cap.
MAX_RECALLS = 2


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
    # Intake's strict rule for the acceptance run (2026-10-16): any request over the bound fails
    # acceptance 1. Indexes of every such request.
    over_bound_indexes: list[int] = field(default_factory=list)
    # The engine's pre-send over_bound records (PR #48). One with ``source: "estimate"`` and
    # ``in_tool_loop: true`` is rule 5's tool-loop growth: reported only. Any other pre-send record
    # fails acceptance 1.
    pre_send_tool_loop: list[int] = field(default_factory=list)
    pre_send_other: list[int] = field(default_factory=list)
    bound_too_small: int = 0  # requests whose new turn alone could not fit the window
    window_minimal: int = 0  # requests that kept only the new turn (contract PR #50, lane A's A2c)
    max_input: int = 0
    # /v1/messages requests with no usage (null or missing): their size cannot be checked against
    # the bound, so they must not count as passing acceptance 1.
    unverified_requests: list[int] = field(default_factory=list)
    # Lane A's A2b (PR #48) logs these from real usage, as standalone session-log events.
    # ``over_bound`` events with ``source: "real"``: each fails acceptance 1.
    over_bound_real: int = 0
    over_bound_real_max_tokens: int = 0
    # Informational only: until lane A fixes PR #48's B1, a rotation request's estimate is the
    # pre-rotation one, which skews these. No verdict uses them.
    estimate_errors: int = 0  # ``estimate_error`` events: real input over the estimate by more than 10%
    estimate_min_ratio: float | None = None  # lowest estimate / real among them
    estimate_mean_ratio: float | None = None  # the engine's running mean, from the last such event
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
            "no_request_over_bound": self.over_bound_requests == 0,
            "no_pre_send_over_bound_outside_tool_loop": not self.pre_send_other,
            "no_real_over_bound": self.over_bound_real == 0,
            "no_bound_too_small": self.bound_too_small == 0,
            "every_request_has_usage": not self.unverified_requests,
            "zero_4xx": self.upstream_4xx == 0 and not any(400 <= s < 500 for s in self.statuses),
            "recall_called": self.recall_ok > 0,
            "recalls_within_cap": self.recall_calls <= MAX_RECALLS,
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
    session_log = list(session_log)
    for ev in session_log:
        if ev.get("event") == "over_bound" and ev.get("source") == "real":
            r.over_bound_real += 1
            r.over_bound_real_max_tokens = max(r.over_bound_real_max_tokens, int(ev.get("real_tokens") or 0))
        elif ev.get("event") == "estimate_error":
            r.estimate_errors += 1
            ratio = ev.get("ratio")
            if isinstance(ratio, (int, float)):
                r.estimate_min_ratio = ratio if r.estimate_min_ratio is None else min(r.estimate_min_ratio, ratio)
            if isinstance(ev.get("mean_ratio"), (int, float)):
                r.estimate_mean_ratio = ev["mean_ratio"]
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
        names = set()
        pre_send: list[bool] = []  # per pre-send over_bound record: is it a tool loop
        for te in ev.get("transform_events") or []:
            if not isinstance(te, dict):
                continue
            name = _event_name(te)
            names.add(name)
            r.engine_events[name] = r.engine_events.get(name, 0) + 1
            if name == "rotation":
                r.rotations += 1
            if name == "over_bound" and te.get("source") != "real":
                pre_send.append(te.get("source") == "estimate" and te.get("in_tool_loop") is True)
        r.bound_too_small += int("bound_too_small" in names)
        r.window_minimal += int("window_minimal" in names)
        epoch = ev.get("epoch")
        r.requests_per_epoch[str(epoch)] = r.requests_per_epoch.get(str(epoch), 0) + 1
        inp = _upstream_input(ev.get("usage"))
        if inp is None:
            r.unverified_requests.append(index)
        else:
            r.max_input = max(r.max_input, inp)
            if inp > bound:
                r.over_bound_requests += 1
                r.over_bound_indexes.append(index)
        if pre_send:
            (r.pre_send_tool_loop if all(pre_send) else r.pre_send_other).append(index)
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
