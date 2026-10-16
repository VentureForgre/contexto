"""Contexto on an OpenRig seat, modeled (packet A32): a seat-shaped Claude Code session replayed through
the engine at several bounds. Zero spend: nothing is sent to a model.

    uv run python spikes/m2/acceptance/seat_bound.py [--turns N] [--restart-every N] [--json out] [--csv out]

**Session (synthetic, labeled so).** The M0 seat measurements (``docs/research/M0-l2d-seat-usage.csv``,
session ``93eadb5d`` on ``lab.subject``) have per-call usage only: no request bodies or byte sizes, so
they cannot drive the engine. The session here is built to match them:

- a fixed head (system and tools) of ``head`` tokens: 82,000 or 92,000, the measured range (81,808 and
  84,073 on the first call, 92,197 by O2) and decision O3's "near 92k";
- turns cycled from the measured small turns, each a user message, tool rounds (one tool use and its
  result each) and a reply, at the measured context growth per call (``TURNS``);
- optionally one large-read turn, a single tool round of 149,140 tokens (G2's measured growth: nine
  parallel file reads), which no window at these bounds can hold.

Every text is filler at ``RATE`` bytes per token, and every token count is the bytes over ``RATE``, so
the engine's own usage feedback sees exactly what it sends.

**Engine and cache.** The real ``RewriteEngine`` on a fresh store, fed modeled usage after each request.
Proxy restarts start a new engine on the same store: with rule 16 (contract PR #176, A30) the store's
epoch record lets it resume; ``--no-resume`` models the store without one (A28's ``AsRunStore``). The
cache is A28's checkpoint model (``cache_rotation.checkpoint_read``): a request reads the longest earlier
whole prompt that prefixes it, else the head when unchanged.

**Cost.** Opus 5.5 at the M0 cost model's prices (``docs/research/M0-cost-model.md``, prices table,
vendor-documented 2026-10-15): input 4, output 20, cache read 0.2, cache write (5 minute TTL) 5 USD per
million tokens, no long-context step. Every uncached input token is counted as a cache write.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance import cache_rotation as cr  # noqa: E402
from contexto.rewrite import Config, RewriteEngine  # noqa: E402
from contexto.store import SessionStore  # noqa: E402

MODEL = "claude-opus-5-5"
PRICES = {"input": 4.0, "output": 20.0, "read": 0.2, "write": 5.0}
RATE = 4  # bytes per token of every synthetic text
BOUNDS = (150_000, 160_000, 170_000)
HEADS = (82_000, 92_000)
IDLE_COMPACTION_MIN = 200_000  # Claude Code's idle compaction minimum (docs/DECISIONS.md, O3)
NATIVE_COMPACTION = 967_000  # Claude Code on 1M-window models (docs/research/M0-interception.md)
ENDPOINT = "/v1/messages"
# The measured small turns of session 93eadb5d, as (user message tokens, [tool round tokens], output
# tokens per call): the growth between calls in M0-l2d-seat-usage.csv, rounded.
TURNS = (
    ("S0", 1_700, [500], [469, 125]),     # a reply through rig send: one Bash round
    ("O1", 3_600, [], [463]),
    ("O2", 1_700, [], [170]),
    ("G1", 900, [1_800], [260, 5]),       # one read
    ("P1", 3_200, [1_000], [4_777, 2_297]),  # one write
    ("G2s", 2_900, [1_800], [795, 128]),  # G2's first call, with a one-file read instead of nine
    ("P2", 900, [200], [3_812, 2_688]),
)
BIG_READ = 149_140  # G2's measured growth in one tool round (nine parallel reads)


def filler(tokens: int, tag: str) -> str:
    """``tokens`` tokens of plain text at RATE bytes per token (no characters JSON escapes)."""
    unit = f"{tag} "
    text = (unit * (tokens * RATE // len(unit) + 1))[: tokens * RATE]
    return text


def session(head: int, turns: int, big_read_at: int | None = None) -> list[dict[str, Any]]:
    """The requests of a synthetic seat session: ``turns`` turns cycled from TURNS after a fixed head of
    ``head`` tokens; ``big_read_at`` (0-based turn) replaces that turn's tool round with BIG_READ."""
    tools = [{"name": "Read", "description": "x", "input_schema": {"type": "object"}}]
    system = filler(head - len(json.dumps(tools)) // RATE - 8, "h")
    messages: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    for t in range(turns):
        name, user, rounds, outputs = TURNS[t % len(TURNS)]
        if t == big_read_at:
            rounds = [BIG_READ]
        messages.append({"role": "user", "content": [{"type": "text", "text": filler(user, f"u{t}")}]})
        for r, size in enumerate(rounds):
            requests.append({"turn": t + 1, "kind": name, "start": r == 0, "output": outputs[min(r, len(outputs) - 1)],
                             "body": {"model": MODEL, "system": system, "tools": tools, "max_tokens": 32_000,
                                      "stream": True, "messages": copy.deepcopy(messages)}})
            call = f"t{t}r{r}"
            messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": call, "name": "Read",
                                                               "input": {"q": filler(outputs[min(r, len(outputs) - 1)], "o")}}]})
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call,
                                                          "content": filler(size, f"r{t}")}]})
        last = outputs[-1]
        requests.append({"turn": t + 1, "kind": name, "start": not rounds, "output": last,
                         "body": {"model": MODEL, "system": system, "tools": tools, "max_tokens": 32_000,
                                  "stream": True, "messages": copy.deepcopy(messages)}})
        messages.append({"role": "assistant", "content": [{"type": "text", "text": filler(last, "a")}]})
    return requests


def tokens_of(body: dict[str, Any]) -> int:
    return max(1, len(cr.ser(body)) // RATE)


def replay(requests: list[dict[str, Any]], config: Config, restarts: set[int] = frozenset(),
           resume: bool = True) -> list[dict[str, Any]]:
    """Each request as the engine sends it: tokens, cache, and the engine's events, by kind. A new engine
    starts on the same store at each request index in ``restarts``; ``resume`` gives the store rule 16's
    epoch record (SessionStore), else none (A28's AsRunStore)."""
    store_cls = SessionStore if resume else cr.AsRunStore
    home = Path(tempfile.mkdtemp(prefix="contexto-a32-"))
    head = tokens_of({"tools": requests[0]["body"]["tools"], "system": requests[0]["body"]["system"]})
    rows: list[dict[str, Any]] = []
    sent: list[tuple[bytes, int, int]] = []
    try:
        engine = RewriteEngine(store_cls(home, "a32"), config=config)
        for i, req in enumerate(requests):
            if i in restarts:
                engine = RewriteEngine(store_cls(home, "a32"), config=config)
            original = copy.deepcopy(req["body"])
            upstream, _, events = engine(copy.deepcopy(req["body"]), {"anthropic-beta": ""}, ENDPOINT)
            tokens = tokens_of(upstream)
            view, head_bytes = cr.cache_view("messages", upstream)
            cached = cr.checkpoint_read(view, head_bytes, tokens, head, sent)
            engine.observe_usage(ENDPOINT, cr._usage("messages", tokens, cached, req["output"]), original)
            sent.append((view, head_bytes, tokens))
            kinds: dict[str, int] = {}
            for e in events:
                if isinstance(e, dict) and e.get("event"):
                    kinds[e["event"]] = kinds.get(e["event"], 0) + 1
            sizing = next((e for e in events if isinstance(e, dict) and e.get("event") == "rotation"), None)
            rows.append({"n": i + 1, "turn": req["turn"], "kind": req["kind"], "start": req["start"],
                         "original": tokens_of(original), "tokens": tokens, "cached": cached,
                         "uncached": tokens - cached, "output": req["output"],
                         "room": config.bound - tokens - config.headroom_floor, "events": kinds,
                         "rotation": None if sizing is None else
                         {k: sizing.get(k) for k in ("reason", "fixed_overhead", "window_budget")}})
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return rows


def usd(rows: list[dict[str, Any]]) -> float:
    return sum(r["cached"] * PRICES["read"] + r["uncached"] * PRICES["write"] + r["output"] * PRICES["output"]
               for r in rows) / 1e6


def summarize(rows: list[dict[str, Any]], config: Config) -> dict[str, Any]:
    events: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for r in rows:
        for k, v in r["events"].items():
            events[k] = events.get(k, 0) + v
        if r["rotation"]:
            reasons[r["rotation"]["reason"]] = reasons.get(r["rotation"]["reason"], 0) + 1
    sizing = [r["rotation"] for r in rows if r["rotation"]]
    rooms = [r["room"] for r in rows if r["start"]]
    tokens, cached = sum(r["tokens"] for r in rows), sum(r["cached"] for r in rows)
    growth = max((b["tokens"] - a["tokens"] for a, b in zip(rows, rows[1:]) if a["turn"] == b["turn"]), default=0)
    return {
        "requests": len(rows), "rotations": len(sizing), "reasons": reasons,
        "fixed_overhead": [min(s["fixed_overhead"] for s in sizing), max(s["fixed_overhead"] for s in sizing)] if sizing else None,
        "window_budget": [min(s["window_budget"] for s in sizing), max(s["window_budget"] for s in sizing)] if sizing else None,
        "window_target": [int(config.window_fraction * min(s["window_budget"] for s in sizing)),
                          int(config.window_fraction * max(s["window_budget"] for s in sizing))] if sizing else None,
        "turn_start_room": {"min": min(rooms), "median": statistics.median(rooms), "max": max(rooms),
                            "below_min_turn_room": sum(x < config.min_turn_room for x in rooms), "turns": len(rooms)},
        "window_minimal": events.get("window_minimal", 0), "turn_room_low": events.get("turn_room_low", 0),
        "bound_too_small": events.get("bound_too_small", 0), "over_bound": events.get("over_bound", 0),
        "input_tokens": tokens, "cached": cached, "uncached": tokens - cached,
        "cached_share": round(cached / tokens, 4), "output": sum(r["output"] for r in rows),
        "peak_input": max(r["tokens"] for r in rows), "largest_in_turn_growth": growth,
        "margin_to_idle_compaction": IDLE_COMPACTION_MIN - max(r["tokens"] for r in rows),
        # The largest single turn that stays under idle compaction: a turn's growth is added on top of
        # its turn-start input, which rotation keeps under the bound.
        "max_turn_start_input": max(r["tokens"] for r in rows if r["start"]),
        "one_turn_growth_under_idle_compaction": IDLE_COMPACTION_MIN - max(r["tokens"] for r in rows if r["start"]),
        "usd": round(usd(rows), 4),
    }


CSV_COLUMNS = ("scenario", "n", "turn", "kind", "start", "original", "tokens", "cached", "uncached", "output",
               "room", "rotation")


def scenarios(turns: int, restart_every: int, csv_rows: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Every modeled case: per head and bound, the base session; restarts every ``restart_every``
    turns with and without resume; and the base session with one large-read turn. Plain Claude Code
    (no Contexto) at the same requests is the reference. ``csv_rows`` collects the base sessions'
    requests, one line each."""
    out: dict[str, dict[str, Any]] = {}
    for head in HEADS:
        base = session(head, turns)
        big = session(head, turns, big_read_at=turns // 2)
        starts = [i for i, r in enumerate(base) if r["start"]]
        restarts = {starts[t] for t in range(restart_every, len(starts), restart_every)}
        plain = [{"tokens": tokens_of(r["body"]), "output": r["output"]} for r in base]
        prev = 0
        for p in plain:  # plain Claude Code: the previous whole prompt is cached (best case)
            p["cached"] = min(prev, p["tokens"])
            p["uncached"] = p["tokens"] - p["cached"]
            prev = p["tokens"]
        out[f"head {head:,} plain"] = {"input_tokens": sum(p["tokens"] for p in plain),
                                       "peak_input": max(p["tokens"] for p in plain), "usd": round(usd(plain), 4)}
        for bound in BOUNDS:
            config = Config(bound=bound)
            key = f"head {head:,} bound {bound:,}"
            rows = replay(base, config)
            out[key] = summarize(rows, config)
            if csv_rows is not None:
                csv_rows += [",".join(str(v) for v in (f"head{head}_bound{bound}", r["n"], r["turn"], r["kind"], int(r["start"]), r["original"],
                                                       r["tokens"], r["cached"], r["uncached"], r["output"], r["room"],
                                                       (r["rotation"] or {}).get("reason", "")))
                             for r in rows]
            out[key + " restarts resume"] = summarize(replay(base, config, restarts, True), config)
            out[key + " restarts no-resume"] = summarize(replay(base, config, restarts, False), config)
            out[key + " large read"] = summarize(replay(big, config), config)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--turns", type=int, default=56)
    ap.add_argument("--restart-every", type=int, default=14)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--csv", type=Path, help="per-request rows of the base sessions")
    a = ap.parse_args(argv)
    csv_rows: list[str] = []
    result = {"turns": a.turns, "restart_every": a.restart_every, "rate": RATE,
              "scenarios": scenarios(a.turns, a.restart_every, csv_rows)}
    if a.csv:
        a.csv.write_text(",".join(CSV_COLUMNS) + "\n" + "\n".join(csv_rows) + "\n", encoding="utf-8")
    text = json.dumps(result, indent=1)
    if a.json:
        a.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
