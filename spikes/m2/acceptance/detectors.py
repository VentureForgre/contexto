"""Acceptance detectors over the Contexto session log and Claude Code's session JSONL.

Session log (``$CONTEXTO_HOME/sessions/<id>/`` JSONL, lane A schema per builder-1, 2026-10-16):
``{"event": "request", "path", "status", "usage": {...} | null, "over_bound", "epoch",
"transform_events": [...]}`` per upstream response; ``{"event": "upstream_4xx", ...}`` per 4xx; and
lane B's ``{"event": "mcp_call", "tool", "ok", ...}`` per MCP tool call. Rotation and resync details
are engine events inside ``transform_events``; a rotation is an event named exactly ``rotation``
(a ``rotation_noop`` pages nothing and is not counted). Lane A's A2b (PR #48) adds standalone
``over_bound`` (``source: "real"``) and ``estimate_error`` events after each response.

Acceptance 1 exempts only a tool loop the engine flagged over-bound before sending (D17, rule 5).
Any other request over the bound fails it, and so does any real-source over_bound event that such
a tool loop does not explain.

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
# Engine events that only happen when a request starts a turn and the rotation trigger fired. A
# request flagged over-bound before sending with none of them is inside a tool loop (rule 5).
TURN_START_EVENTS = frozenset({"rotation", "rotation_noop", "bound_too_small", "window_minimal"})


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
    over_bound_logged: int = 0  # requests the engine flagged over_bound before sending
    # Acceptance 1's only exemption (D17, rule 5): a tool loop that grew past the bound, which the
    # engine flagged before sending. These request indexes are over the bound and exempt.
    over_bound_tool_loop: list[int] = field(default_factory=list)
    # Every other request over the bound fails acceptance 1: an estimator miss (no pre-send flag),
    # or a flagged turn start (``bound_too_small``, a configuration failure).
    over_bound_failures: list[int] = field(default_factory=list)
    bound_too_small: int = 0  # requests whose new turn alone could not fit the window
    window_minimal: int = 0  # requests that kept only the new turn (contract PR #50, lane A's A2c)
    max_input: int = 0
    # /v1/messages requests with no usage (null or missing): their size cannot be checked against
    # the bound, so they must not count as passing acceptance 1.
    unverified_requests: list[int] = field(default_factory=list)
    # Lane A's A2b (PR #48) logs these from real usage, as standalone session-log events.
    over_bound_real: int = 0  # ``over_bound`` events with ``source: "real"``
    # Real-source over_bound events not explained by an exempt tool loop: the estimate missed.
    # Each fails acceptance 1; reported with the largest real input among them.
    estimator_misses: int = 0
    estimator_miss_max_tokens: int = 0
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
            "over_bound_only_in_tool_loop": not self.over_bound_failures,
            "no_estimator_misses": self.estimator_misses == 0,
            "no_bound_too_small": self.bound_too_small == 0,
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
    session_log = list(session_log)
    real_over: dict[int, int] = {}  # real_tokens -> count of over_bound events from real usage
    for ev in session_log:
        if ev.get("event") == "over_bound" and ev.get("source") == "real":
            r.over_bound_real += 1
            tokens = int(ev.get("real_tokens") or 0)
            real_over[tokens] = real_over.get(tokens, 0) + 1
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
        pre_send_over = bool(ev.get("over_bound"))
        for te in ev.get("transform_events") or []:
            if not isinstance(te, dict):
                continue
            name = _event_name(te)
            names.add(name)
            r.engine_events[name] = r.engine_events.get(name, 0) + 1
            if name == "rotation":
                r.rotations += 1
            if te.get("over_bound") is True and te.get("source") != "real":
                pre_send_over = True  # the engine's request event, or its record tagged source "estimate"
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
                if pre_send_over and not names & TURN_START_EVENTS:
                    r.over_bound_tool_loop.append(index)
                    if real_over.get(inp):  # the estimate did not miss this one
                        real_over[inp] -= 1
                else:
                    r.over_bound_failures.append(index)
        if pre_send_over:
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
    misses = [tokens for tokens, count in real_over.items() for _ in range(count)]
    r.estimator_misses = len(misses)
    r.estimator_miss_max_tokens = max(misses, default=0)
    for rec in cc_jsonl:
        if (rec.get("type") == "system" and rec.get("subtype") == "compact_boundary") or rec.get("isCompactSummary"):
            r.compaction_events += 1
    return r
