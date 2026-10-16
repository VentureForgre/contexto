"""Headroom that forgets outliers, offline (packet A33). Zero spend: recorded or synthetic requests are
replayed through the engine under candidate headroom rules; nothing is sent.

    uv run python spikes/m2/acceptance/headroom_outliers.py codex <codex session dir> [--rate B] [--json out]
    uv run python spikes/m2/acceptance/headroom_outliers.py claude <run dir> <session id> [--rate B] [--json out]
    uv run python spikes/m2/acceptance/headroom_outliers.py seat [--json out]   (needs A32's seat_bound.py)

Today's rule (rewrite.py ``headroom``): headroom = max(headroom_floor, ceil(1.25 x max_turn_growth)), and
``max_turn_growth`` only grows, so one outlier turn keeps W = bound - headroom - F small or negative for
the rest of the session (A32). The candidates are prototypes, applied by patching the engine for the
duration of one replay, never by changing ``src``:

- ``cap``: headroom at most a share of the bound (``headroom()`` replaced).
- ``decay``: at each turn start, max_turn_growth = max(floor(previous x d), this turn's growth).
- ``window``: at each turn start, max_turn_growth = the largest growth of the last N turn starts.

Decay and window set ``max_turn_growth`` before the engine's rotation decision (a wrapper around
``guarded_rotation``, which runs only at a turn start), recomputing the turn's growth the way the engine
does (``estimate_items`` of the messages added since the previous turn start, per turn). The replays
reuse A28's ``cache_rotation`` (requests, usage feedback, checkpoint cache model, prices) and A32's
``seat_bound`` session. Output is numbers only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance import cache_rotation as cr  # noqa: E402
from contexto import rewrite  # noqa: E402
from contexto.rewrite import Config  # noqa: E402

# The engine's headroom before rule 17 (contract PR #186): no cap, no decay. Every candidate is replayed
# on top of it, so "today" is the rule A33 measured.
BEFORE_RULE_17 = {"headroom_cap": 1_000_000.0, "headroom_decay": 1.0}

CANDIDATES: dict[str, dict[str, Any]] = {
    "today": {},
    "cap_25": {"cap": 0.25},
    "cap_33": {"cap": 1 / 3},
    "decay_0.5": {"decay": 0.5},
    "decay_0.75": {"decay": 0.75},
    "window_3": {"window": 3},
    "window_5": {"window": 5},
    "decay_0.5+cap_33": {"decay": 0.5, "cap": 1 / 3},
    "decay_0.5+cap_25": {"decay": 0.5, "cap": 0.25},
}


@contextlib.contextmanager
def headroom_rule(cap: float | None = None, decay: float | None = None, window: int | None = None
                  ) -> Iterator[None]:
    """Patch the engine with one candidate rule for the duration of a replay."""
    patches = []
    if cap is not None:
        def capped(state: Any, config: Config) -> int:
            grown = math.ceil(state.max_turn_growth * config.headroom_factor)
            return max(config.headroom_floor, min(grown, int(cap * config.bound)))
        patches.append(mock.patch.object(rewrite, "headroom", capped))
    if decay is not None or window is not None:
        original = rewrite.guarded_rotation
        tracked: dict[str, dict[str, Any]] = {}

        def forgetting(request, messages, n, estimate, state, store, config, estimator, events):
            key = rewrite.conversation_key(request)
            t = tracked.setdefault(key, {"n": None, "count": 0, "value": 0, "growths": []})
            count = len(rewrite.turn_starts(messages))
            growth = None
            if t["n"] is not None and count > t["count"] and n > t["n"]:
                growth = rewrite.estimate_items(estimator, messages[t["n"]:]) // (count - t["count"])
            t["n"], t["count"] = n, count
            if growth is not None:
                t["growths"].append(growth)
                if window is not None:
                    value = max(t["growths"][-window:])
                else:
                    value = max(int(t["value"] * decay), growth)
                t["value"] = value
                state = rewrite.replace(state, max_turn_growth=value)
            return original(request, messages, n, estimate, state, store, config, estimator, events)
        patches.append(mock.patch.object(rewrite, "guarded_rotation", forgetting))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


@contextlib.contextmanager
def capture() -> Iterator[list[dict[str, Any]]]:
    """Every engine call's events and whether its request was at a turn start, in order."""
    calls: list[dict[str, Any]] = []
    original = rewrite.RewriteEngine.__call__

    def recording(self, request, headers, endpoint):
        upstream, out_headers, events = original(self, request, headers, endpoint)
        dialect = rewrite.dialect_for(endpoint)
        start = False
        if dialect is not None:
            token = rewrite._DIALECT.set(dialect)
            try:
                start = rewrite.at_turn_start(request.get(dialect.field) or [])
            finally:
                rewrite._DIALECT.reset(token)
        calls.append({"events": [e for e in events if isinstance(e, dict)], "start": start})
        return upstream, out_headers, events

    with mock.patch.object(rewrite.RewriteEngine, "__call__", recording):
        yield calls


def counts(calls: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for call in calls:
        for e in call["events"]:
            out[e["event"]] = out.get(e["event"], 0) + 1
    return out


def summary(rows: list[dict[str, Any]], calls: list[dict[str, Any]], config: Config, model: str) -> dict[str, Any]:
    """Rotations and reasons, turn-start room, the rule's events, peak input, and modeled cost."""
    c = counts(calls)
    reasons: dict[str, int] = {}
    for call in calls:
        for e in call["events"]:
            if e["event"] == "rotation":
                reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
    rooms = [r["room"] for r, call in zip(rows, calls) if call["start"]]
    budgets = [e["window_budget"] for call in calls for e in call["events"] if e["event"] == "rotation"
               and isinstance(e.get("window_budget"), int)]
    over = sum(1 for call in calls for e in call["events"] if e.get("over_bound"))
    return {"requests": len(rows), "rotations": sum(reasons.values()), "reasons": reasons,
            "room_min": min(rooms) if rooms else None, "room_median": statistics.median(rooms) if rooms else None,
            "below_min_turn_room": sum(x < config.min_turn_room for x in rooms),
            "window_minimal": c.get("window_minimal", 0), "turn_room_low": c.get("turn_room_low", 0),
            "bound_too_small": c.get("bound_too_small", 0), "over_bound": over,
            "window_budget_min": min(budgets) if budgets else None,
            "peak_input": max(r["tokens"] for r in rows),
            "usd": round(cr.cost(rows, model), 4) if model in cr.PRICES else None}


def evaluate_run(run: dict[str, Any], bound: int, rate: float | None = None) -> dict[str, Any]:
    """A recorded run (A28's loader), each candidate, at the run's own config otherwise."""
    model = run["model"] if run["model"] in cr.PRICES else ("gpt-6-astra" if run["api"] == "responses"
                                                            else "claude-haiku-5-5")
    config = Config(bound=bound, **BEFORE_RULE_17)
    base_bytes = None
    out = {}
    for name, rule in CANDIDATES.items():
        with headroom_rule(**rule), capture() as calls:
            rows = cr.replay(run, config, baseline_bytes=base_bytes, rate=rate)
        if base_bytes is None:
            base_bytes = [r["bytes"] for r in rows]
        out[name] = summary(rows, calls, config, model)
    return out


def evaluate_seat(bounds: tuple[int, ...] = (150_000, 170_000), head: int = 92_000, turns: int = 56) -> dict[str, Any]:
    """A32's synthetic seat session with its large read, each candidate, at each bound."""
    from acceptance import seat_bound  # noqa: PLC0415  (A32, PR #183)

    big = seat_bound.session(head, turns, big_read_at=turns // 2)
    out: dict[str, Any] = {}
    for bound in bounds:
        config = Config(bound=bound, **BEFORE_RULE_17)
        for name, rule in CANDIDATES.items():
            with headroom_rule(**rule), capture() as calls:
                rows = seat_bound.replay(big, config)
            s = seat_bound.summarize(rows, config)
            big_turn = [r for r in rows if r["turn"] == turns // 2 + 1]
            out[f"{bound} {name}"] = {
                "rotations": s["rotations"], "reasons": s["reasons"],
                "room_min": s["turn_start_room"]["min"], "room_median": s["turn_start_room"]["median"],
                "below_min_turn_room": s["turn_start_room"]["below_min_turn_room"],
                "window_minimal": s["window_minimal"], "turn_room_low": s["turn_room_low"],
                "bound_too_small": s["bound_too_small"], "over_bound": s["over_bound"],
                "window_budget": s["window_budget"], "peak_input": s["peak_input"],
                "big_turn_peak": max(r["tokens"] for r in big_turn),
                "big_turn_start": big_turn[0]["tokens"],
                "margin_to_200k": s["margin_to_idle_compaction"], "usd": s["usd"],
                "calls": len(calls)}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="kind", required=True)
    c = sub.add_parser("codex")
    c.add_argument("session_dir", type=Path)
    c.add_argument("--bound", type=int, default=64_000)
    m = sub.add_parser("claude")
    m.add_argument("run_dir", type=Path)
    m.add_argument("session")
    m.add_argument("--bound", type=int, default=150_000)
    s = sub.add_parser("seat")
    for p in (c, m):
        p.add_argument("--rate", type=float, default=None)
    for p in (c, m, s):
        p.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.kind == "seat":
        result = evaluate_seat()
    else:
        run = cr.codex_run(args.session_dir) if args.kind == "codex" else cr.claude_run(args.run_dir, args.session)
        result = evaluate_run(run, args.bound, args.rate)
    text = json.dumps(result, indent=2)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
