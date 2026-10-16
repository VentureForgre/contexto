"""Acceptance detectors over the Contexto session log and Claude Code's session JSONL.

Session log (``$CONTEXTO_HOME/sessions/<id>/`` JSONL, lane A schema per builder-1, 2026-10-16):
``{"event": "request", "path", "status", "usage": {...} | null, "over_bound", "epoch",
"transform_events": [...]}`` per upstream response; ``{"event": "upstream_4xx", ...}`` per 4xx; and
lane B's ``{"event": "mcp_call", "tool", "ok", ...}`` per MCP tool call. Rotation and resync details
are engine events inside ``transform_events``; a rotation is an event named exactly ``rotation``
(a ``rotation_noop`` pages nothing and is not counted). Lane A's A2b (PR #48) adds standalone
``over_bound`` (``source: "real"``) and ``estimate_error`` events after each response, as separate
lines; A2e (PR #76) moves them onto the request's entry as ``usage_events``. ``usage_events()``
reads both forms and counts each event once.

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

from contexto.proxy import TRANSFORMED_PATHS

MESSAGES = "/v1/messages"
# Model requests for acceptance 1's zero-4xx (contract PR #61): the paths the proxy transforms,
# /v1/messages, its count_tokens, and /responses. 4xx on any other path are reported apart.
MODEL_PATHS = TRANSFORMED_PATHS


def request_path(event: dict[str, Any]) -> str:
    """The event's path without its query string or a trailing slash ("/v1/messages/" is a model path)."""
    path = str(event.get("path") or "").split("?")[0]
    return path.rstrip("/") or path


# Model paths whose responses are billed: Messages and Responses, not their token counts.
BILLED_PATHS = frozenset(p for p in MODEL_PATHS if not p.endswith(("count_tokens", "input_tokens")))


# Intake (2026-10-16 03:39Z, contract PR at 147b182): any of these, wherever it appears (a request's
# transform events, its usage_events, or a separate session-log line), fails acceptance 1.
FAILURE_EVENTS = ("degraded_enter", "store_error", "page_conflict", "fallback_failed", "transform_error",
                  "turn_room_missing", "unclassified_system_message",
                  "turn_room_low")  # A12: decided a failure, contract PR #115 at 1eb8a4c
# The recalls the budget prices (script.MAX_RECALLS). A cost guard only (intake, 2026-10-16): the
# model is not told about it, and ``recalls_over_budget`` reports it without failing any verdict.
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
    upstream_4xx: int = 0  # model-path 4xx the upstream returned (``upstream_4xx`` events)
    # Request records with a 4xx status on any model path, whether the proxy answered it itself (for
    # example 400 "malformed request framing", which has no upstream_4xx event) or passed upstream's
    # 4xx through (that one is also counted in upstream_4xx). Both fail zero_4xx, so counting a
    # passed-through 4xx twice changes no verdict (reviewer-2, PR #66). Plus rule 15's
    # ``encoded_body_rejected`` below (a 415 with no request record).
    model_request_4xx: int = 0
    encoded_body_rejected: int = 0
    # 4xx on other paths, by "METHOD path": Claude Code's unauthenticated HEAD /api/hello probe gets 401
    # from the owner's proxy. Reported on their own; no verdict uses them (how acceptance 1 reads them
    # is pending intake).
    non_model_4xx: dict[str, int] = field(default_factory=dict)
    # Contract PR #98: a 5xx Contexto's own proxy generates on a model request fails acceptance 1; an
    # upstream 5xx passed through is reported only. See five_xx() for how each is told apart.
    local_5xx: list[dict[str, Any]] = field(default_factory=list)
    upstream_5xx: list[dict[str, Any]] = field(default_factory=list)
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
    # FAILURE_EVENTS seen, by name, each counted once; any fails acceptance 1.
    failure_events: dict[str, int] = field(default_factory=dict)
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
    def recalls_over_budget(self) -> int:
        """Recall calls beyond the budget's priced count: reported, never a verdict."""
        return max(0, self.recall_calls - MAX_RECALLS)

    @property
    def cache_hit_rate(self) -> float | None:
        return self.cache_hits / self.cache_eligible if self.cache_eligible else None

    def verdicts(self) -> dict[str, bool]:
        return {
            "zero_compaction": self.compaction_events == 0 and self.stream_compactions == 0,
            "no_request_over_bound": self.over_bound_requests == 0,
            "no_pre_send_over_bound_outside_tool_loop": not self.pre_send_other,
            "no_real_over_bound": self.over_bound_real == 0,
            "no_failure_events": not self.failure_events,
            "no_bound_too_small": self.bound_too_small == 0,
            "every_request_has_usage": not self.unverified_requests,
            "zero_4xx": (self.upstream_4xx == 0 and self.model_request_4xx == 0 and self.encoded_body_rejected == 0
                         and not any(400 <= s < 500 for s in self.statuses)),
            "no_local_5xx": not self.local_5xx,
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


def _is_usage_event(ev: dict[str, Any]) -> bool:
    return (ev.get("event") == "over_bound" and ev.get("source") == "real") or ev.get("event") == "estimate_error"


def _usage_key(ev: dict[str, Any]) -> tuple[Any, ...]:
    return (ev.get("event"), ev.get("epoch"), ev.get("real_tokens"), ev.get("estimate"))


def usage_events(session_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The real-usage events (``over_bound`` with ``source: "real"``, ``estimate_error``), each once.

    Two log forms carry them. Lane A's A2b wrote them as separate session-log lines (older runs,
    including the recorded H1 rerun). A2e (PR #76) moves them onto the request's entry as a
    ``usage_events`` list and ORs a real over_bound into the entry's ``over_bound`` flag. Both are
    read; a separate line that repeats an entry's event (same event, epoch, real_tokens, estimate)
    is counted once. The entry's ``over_bound`` flag is never read as a real over-bound: a pre-send
    tool-loop record sets it too (reviewer-2, PR #76).
    """
    events: list[dict[str, Any]] = []
    on_entries: dict[tuple[Any, ...], int] = {}
    for ev in session_log:
        if ev.get("event") != "request":
            continue
        listed = [u for u in ev.get("usage_events") or [] if isinstance(u, dict) and _is_usage_event(u)]
        for u in listed:
            u = {"epoch": ev.get("epoch"), **u}
            events.append(u)
            on_entries[_usage_key(u)] = on_entries.get(_usage_key(u), 0) + 1
    for ev in session_log:
        if ev.get("event") == "request" or not _is_usage_event(ev):
            continue
        key = _usage_key(ev)
        if on_entries.get(key):
            on_entries[key] -= 1  # the same event, already read from its request entry
            continue
        events.append(ev)
    return events


def _event_key(ev: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(sorted((k, v) for k, v in ev.items() if k != "ts" and isinstance(v, (str, int, float, bool, type(None)))))


def failure_events(session_log: list[dict[str, Any]]) -> dict[str, int]:
    """FAILURE_EVENTS by name, from request entries (transform events and usage_events) and separate
    session-log lines. A separate line that repeats an event already on an entry counts once."""
    counts: dict[str, int] = {}
    on_entries: dict[tuple[Any, ...], int] = {}
    for ev in session_log:
        if ev.get("event") != "request":
            continue
        for item in [*(ev.get("transform_events") or []), *(ev.get("usage_events") or [])]:
            if isinstance(item, dict) and _event_name(item) in FAILURE_EVENTS:
                name = _event_name(item)
                counts[name] = counts.get(name, 0) + 1
                key = _event_key({"epoch": ev.get("epoch"), **item})
                on_entries[key] = on_entries.get(key, 0) + 1
    for ev in session_log:
        name = _event_name(ev)
        if ev.get("event") == "request" or name not in FAILURE_EVENTS:
            continue
        key = _event_key(ev)
        if on_entries.get(key):
            on_entries[key] -= 1
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts


def five_xx(session_log: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Model-path 5xx split into LOCAL (the proxy answered it) and UPSTREAM (passed through).

    From the proxy at main (src/contexto/proxy.py, read-only; lane A owns it), every local 5xx on a
    model path leaves a mark in the session log:
    - ``_forward`` when upstream cannot be reached: the request entry gets status 502 and
      ``error: "upstream unreachable: <class>"``, and no ``response_bytes``;
    - ``_handle``'s last resort: no request entry, but ``{event: proxy_error, fallback: "local_502"}``
      with the path (a ``fallback: "forward_original"`` record forwards the CLI's bytes instead);
    - a WebSocket upgrade: ``{event: websocket_rejected, path}``, answered 501.
    A passed-through upstream 5xx is a request entry with ``response_bytes`` and no such error. A
    model-path 5xx entry with neither mark fails closed: it counts as LOCAL, flagged ``unmarked``.
    """
    local, upstream = [], []
    for i, ev in enumerate(session_log):
        name = ev.get("event")
        path = str(ev.get("path") or "").split("?")[0]
        path = path.rstrip("/") or path
        if path not in MODEL_PATHS:
            continue
        if name == "proxy_error" and ev.get("fallback") == "local_502":
            local.append({"line": i + 1, "path": path, "status": 502, "mark": "proxy_error local_502",
                          "error": ev.get("error")})
        elif name == "websocket_rejected":
            local.append({"line": i + 1, "path": path, "status": 501, "mark": "websocket_rejected"})
        elif name == "request" and int(ev.get("status") or 0) >= 500:
            status, error = int(ev["status"]), str(ev.get("error") or "")
            if error.startswith("upstream unreachable"):
                local.append({"line": i + 1, "path": path, "status": status, "mark": "upstream unreachable",
                              "error": error})
            elif "response_bytes" in ev:
                upstream.append({"line": i + 1, "path": path, "status": status})
            else:
                local.append({"line": i + 1, "path": path, "status": status, "mark": "unmarked", "error": error or None})
    return local, upstream


def analyze(session_log: Iterable[dict[str, Any]], cc_jsonl: Iterable[dict[str, Any]], bound: int,
            stream_compactions: int = 0) -> Report:
    r = Report(stream_compactions=stream_compactions)
    session_log = list(session_log)
    for name, count in failure_events(session_log).items():
        r.failure_events[name] = count
    r.local_5xx, r.upstream_5xx = five_xx(session_log)
    for ev in usage_events(session_log):
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
        path = request_path(ev)
        if name == "encoded_body_rejected":
            r.encoded_body_rejected += 1
            continue
        if name == "upstream_4xx":
            if path in MODEL_PATHS:
                r.upstream_4xx += 1
            continue  # other paths are counted once, from their request record, below
        if name == "mcp_call":
            tool = str(ev.get("tool"))
            r.mcp_calls[tool] = r.mcp_calls.get(tool, 0) + 1
            if tool == "contexto_recall":
                r.recall_calls += 1
                r.recall_ok += int(bool(ev.get("ok")))
            continue
        if name == "request" and 400 <= int(ev.get("status") or 0) < 500:
            if path in MODEL_PATHS:
                r.model_request_4xx += 1
            else:
                label = f"{ev.get('method') or '?'} {path}"
                r.non_model_4xx[label] = r.non_model_4xx.get(label, 0) + 1
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
