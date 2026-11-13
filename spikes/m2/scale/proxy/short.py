"""MEASUREMENT (zero spend): the short scale run, the version the test suite runs on every pull request.

    .venv/bin/python spikes/m2/scale/proxy/short.py              # one run: the numbers against the budgets
    .venv/bin/python spikes/m2/scale/proxy/short.py --repeat 5   # the spread over five runs

About 200 turns of the synthetic Claude Code-shaped session (``synth.py``) through the real proxy
(``worker.py serve``, one process for the whole run) and the local fake upstream (``upstream.py``), every
request over HTTP. Nothing leaves 127.0.0.1 and neither ``claude`` nor ``codex`` is needed.

What it measures, and what ``tests/test_scale_proxy_short.py`` holds to ``BUDGETS``:

- **The bound**: every upstream request's size, the events of ``run.BREAK_EVENTS``, every status.
- **Added time per request**: the driver's send-to-last-byte time minus the time the fake upstream spent on
  the request (from its headers to its last event). That is the client's write of the body to the proxy, the
  proxy's read, parse, transform and serialization, its connection to the upstream, and the relay back.
- **Growth with history**: the slope of added time against the size of the history the CLI sent, in
  milliseconds per million tokens, over the tool-loop requests (Theil-Sen: the median of the slopes between
  every pair of requests, so a few slow requests do not move it).
- **Startup**: from starting the proxy process to its ``ready`` line (the interpreter, the imports, opening
  the session, building the engine, binding the port), the smallest of ``STARTUPS`` starts.
- **Memory**: the proxy's peak RSS (``ru_maxrss`` of the process itself), how much its RSS grows per MB the
  request body grows, and how many live Python objects it gains per turn (after ``gc.collect()``).
- **Throughput**: ``CLIENTS`` threads each sending ``PER_CLIENT`` small requests at once, in requests a
  second. The clients, the fake upstream and the timer share one Python process, so this is a floor for the
  proxy, not its limit.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scale.proxy import run, synth  # noqa: E402
from scale.proxy.summarize import p95  # noqa: E402
from scale.proxy.upstream import FakeUpstream  # noqa: E402

TURNS = 200
HEAD_TOKENS = 20_000
TURN_TOKENS = 8_000
BOUND = 150_000
REMINDERS = "mixed"
STARTUPS = 3
CLIENTS = 100
PER_CLIENT = 3
SAMPLE_EVERY = 20  # turns between memory samples
SEND_TIMEOUT_S = 120

# What the test asserts. Provisional until the harness runs again; see README.md, "The short run".
BUDGETS: dict[str, float] = {
    "added_median_ms": 100.0,
    "added_p95_ms": 400.0,
    "added_ms_per_mtok": 250.0,
    "startup_ms": 5_000.0,
    "peak_rss_mb": 1_000.0,
    "rss_mb_per_body_mb": 200.0,
    "objects_per_turn": 1_000.0,
    "throughput_floor_rps": 10.0,
}


def theil_sen(points: list[tuple[float, float]]) -> tuple[float, float]:
    """The line through ``points`` as (slope, intercept): the median of the slopes between every pair, and
    the median of what is left. Up to 29 percent of the points can be outliers without moving it."""
    slopes = [(y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(points) for x2, y2 in points[i + 1:] if x2 != x1]
    if not slopes:
        return 0.0, statistics.median(y for _, y in points) if points else 0.0
    slope = statistics.median(slopes)
    return slope, statistics.median(y - slope * x for x, y in points)


def short_args(reminders: str = REMINDERS) -> argparse.Namespace:
    return run.build_parser().parse_args(["--label", "short", "--head-tokens", str(HEAD_TOKENS), "--turn-tokens",
                                          str(TURN_TOKENS), "--bound", str(BOUND), "--reminders", reminders])


def start(args: argparse.Namespace, home: Path, upstream: FakeUpstream, err) -> tuple[run.Worker, float]:
    """A proxy process on a new ``CONTEXTO_HOME``, and the seconds from starting it to its ``ready`` line."""
    home.mkdir(parents=True)
    t0 = time.monotonic()
    worker = run.Worker(args, home, home / "engine.pkl", upstream.upstream_port, err)
    return worker, time.monotonic() - t0


def burst(worker: run.Worker, upstream: FakeUpstream, body: bytes, clients: int, per_client: int) -> dict[str, Any]:
    """``clients`` threads, released together, each sending ``body`` ``per_client`` times."""
    statuses: list[list[int]] = [[] for _ in range(clients)]
    barrier = threading.Barrier(clients + 1)

    def client(i: int) -> None:
        barrier.wait()
        for j in range(per_client):
            request_id = f"c{i}r{j}"
            try:
                statuses[i].append(run.send(worker.port, body, request_id, "end_turn", SEND_TIMEOUT_S)["status"])
            except OSError:
                statuses[i].append(-2)
            upstream.take(request_id)

    threads = [threading.Thread(target=client, args=(i,), daemon=True) for i in range(clients)]
    for t in threads:
        t.start()
    barrier.wait()
    t0 = time.monotonic()
    for t in threads:
        t.join()
    seconds = time.monotonic() - t0
    flat = [s for per in statuses for s in per]
    worker.call({"cmd": "stats"})
    return {"clients": clients, "requests": len(flat), "not_200": sum(1 for s in flat if s != 200),
            "seconds": round(seconds, 3), "rps": round(len(flat) / seconds, 1) if seconds else 0.0}


def measure(turns: int = TURNS, *, reminders: str = REMINDERS, startups: int = STARTUPS, clients: int = CLIENTS,
            per_client: int = PER_CLIENT, home: Path | None = None) -> dict[str, Any]:
    """One short run. Returns the measurements (see the module docstring); ``rows`` holds one dict per
    request of the session, with the columns of ``run.REQUEST_FIELDS``."""
    args = short_args(reminders)
    made_home = home is None
    home = Path(tempfile.mkdtemp(prefix="contexto-scale-short-")) if home is None else home
    upstream = FakeUpstream()
    upstream.upstream_port = upstream.start()
    began = time.monotonic()
    rows: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    startup_s: list[float] = []
    try:
        with open(home / "worker.log", "w") as err:
            session = run.Session(args)
            first = session.next_messages()
            first_body = session.wire.body(first[:synth.request_points(session.shape, 0, first)[0][0]])
            probe = None
            for i in range(startups):
                probe, seconds = start(args, home / f"startup-{i}", upstream, err)
                startup_s.append(seconds)
                if i < startups - 1:
                    probe.close(save=False)
            try:
                concurrent = burst(probe, upstream, first_body, clients, per_client) if probe and clients else {}
            finally:
                if probe is not None:
                    probe.close(save=False)
            worker, _ = start(args, home / "run", upstream, err)
            peak = 0.0
            try:
                base = {"label": args.label, "checkpoint_m": 0, "phase": "drive", "round": 0}
                for t in range(turns):
                    turn_rows: list[dict[str, Any]] = []
                    run.run_turn(session, worker, upstream, turn_rows, base, SEND_TIMEOUT_S)
                    stats = worker.call({"cmd": "stats"})
                    run.merge_stats(turn_rows, stats["records"], stats["maxrss_mb"])
                    rows.extend(turn_rows)
                    if (t + 1) % SAMPLE_EVERY == 0 or t + 1 == turns:
                        seen = worker.call({"cmd": "gc"})
                        samples.append({"turn": t + 1, "body_mb": turn_rows[-1]["body_mb"], "rss_mb": seen["rss_mb"],
                                        "maxrss_mb": round(seen["maxrss_mb"], 1), "objects": seen["objects"]})
            finally:
                peak = worker.close(save=False)
        session_dir = home / "run" / "sessions" / "scale"
        counts, max_logged = run.log_events(session_dir / "log.jsonl")
        pages = len(list((session_dir / "pages").glob("pg_*.json")))
        store_mb = run.dir_mb(session_dir)
    finally:
        upstream.stop()
        if made_home:
            shutil.rmtree(home, ignore_errors=True)
    added = [r["added_s"] * 1e3 for r in rows if r.get("added_s") is not None]
    loops = [(r["history_tokens"] / 1e6, r["added_s"] * 1e3) for r in rows
             if r["kind"] == "tool_loop" and r.get("added_s") is not None]
    slope, intercept = theil_sen(loops)
    first_sample, last_sample = (samples[0], samples[-1]) if samples else ({}, {})
    body_growth = (last_sample.get("body_mb") or 0) - (first_sample.get("body_mb") or 0)
    rss_known = bool(samples) and all(s["rss_mb"] is not None for s in samples)
    turn_span = (last_sample.get("turn") or 0) - (first_sample.get("turn") or 0)
    return {
        "python": platform.python_version(), "platform": sys.platform, "turns": turns, "reminders": reminders,
        "requests": len(rows), "seconds": round(time.monotonic() - began, 1),
        "history_tokens": rows[-1]["history_tokens"] if rows else 0, "body_mb": rows[-1]["body_mb"] if rows else 0,
        "bound": args.bound, "max_upstream_tokens": max([max_logged, *(r.get("upstream_tokens") or 0 for r in rows)]),
        "unseen_upstream": sum(1 for r in rows if not r.get("upstream_tokens")),
        "broken": run.broken(rows, counts, args.bound), "events": {k: v for k, v in counts.items() if v},
        "pages": pages, "store_mb": store_mb,
        "added_median_ms": round(statistics.median(added), 3) if added else None,
        "added_p95_ms": round(p95(added), 3) if added else None,
        "added_max_ms": round(max(added), 3) if added else None,
        "added_ms_per_mtok": round(slope, 3), "added_intercept_ms": round(intercept, 3),
        "startup_ms": round(min(startup_s) * 1e3, 1) if startup_s else None,
        "startup_all_ms": [round(s * 1e3, 1) for s in startup_s],
        "peak_rss_mb": round(peak, 1),
        "rss_mb_per_body_mb": round((last_sample["rss_mb"] - first_sample["rss_mb"]) / body_growth, 2)
        if rss_known and body_growth > 0 else None,
        "objects_per_turn": round((last_sample["objects"] - first_sample["objects"]) / turn_span, 2)
        if turn_span > 0 else None,
        "samples": samples, "throughput": concurrent, "throughput_rps": concurrent.get("rps"),
        "rows": rows,
    }


def over_budget(result: dict[str, Any], budgets: dict[str, float] = BUDGETS) -> list[str]:
    """The budgets ``result`` does not meet, each as a sentence; empty when it meets them all. A quantity
    that could not be measured (no ``ps`` for the RSS samples) is not judged."""
    out = []
    for key, limit in budgets.items():
        if key == "throughput_floor_rps":
            value = result.get("throughput_rps")
            if value is not None and value < limit:
                out.append(f"throughput {value} requests a second is under the floor of {limit:g}")
            continue
        value = result.get(key)
        if value is not None and value > limit:
            out.append(f"{key} {value} is over the budget of {limit:g}")
    return out


def report(result: dict[str, Any]) -> str:
    """The run as lines of text: the bound, then each budgeted quantity with its budget and the factor."""
    lines = [f"short scale run: {result['turns']} turns, {result['requests']} requests, "
             f"{result['history_tokens']:,} tokens of history ({result['body_mb']} MB body) in {result['seconds']} s; "
             f"Python {result['python']} on {result['platform']}",
             f"  largest upstream request {result['max_upstream_tokens']:,} tokens (bound {result['bound']:,}); "
             f"{result['pages']} pages; broken: {result['broken'] or 'nothing'}; events {result['events']}"]
    for key, limit in BUDGETS.items():
        value = result.get("throughput_rps" if key == "throughput_floor_rps" else key)
        if value is None:
            lines.append(f"  {key}: not measured (budget {limit:g})")
        elif key == "throughput_floor_rps":
            lines.append(f"  throughput_rps: {value} (floor {limit:g}, {value / limit:.0f} times above it)")
        else:
            lines.append(f"  {key}: {value} (budget {limit:g}, {limit / value:.1f} times the measured value)"
                         if value > 0 else f"  {key}: {value} (budget {limit:g})")
    lines.append(f"  added time at no history (the fit's intercept): {result['added_intercept_ms']} ms; "
                 f"largest {result['added_max_ms']} ms; startups {result['startup_all_ms']} ms")
    lines.append(f"  memory samples: {json.dumps(result['samples'])}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--turns", type=int, default=TURNS)
    parser.add_argument("--reminders", choices=("first", "mixed"), default=REMINDERS)
    parser.add_argument("--repeat", type=int, default=1, help="runs; with more than one, the spread is printed")
    parser.add_argument("--clients", type=int, default=CLIENTS, help="concurrent clients of the throughput burst")
    parser.add_argument("--json", help="append each run's measurements (without the rows) to this file, one per line")
    args = parser.parse_args(argv)
    results = []
    failed = False
    for _ in range(args.repeat):
        result = measure(args.turns, reminders=args.reminders, clients=args.clients)
        results.append(result)
        print(report(result), flush=True)
        problems = [*result["broken"], *over_budget(result)]
        failed = failed or bool(problems)
        for problem in problems:
            print(f"  FAILED: {problem}", flush=True)
        if args.json:
            with open(args.json, "a") as f:
                f.write(json.dumps({k: v for k, v in result.items() if k != "rows"}) + "\n")
    if len(results) > 1:
        print(f"spread over {len(results)} runs (smallest, median, largest):")
        for key in ("added_median_ms", "added_p95_ms", "added_intercept_ms", "added_ms_per_mtok", "startup_ms",
                    "peak_rss_mb", "rss_mb_per_body_mb", "objects_per_turn", "throughput_rps", "seconds"):
            values = sorted(r[key] for r in results if r.get(key) is not None)
            if values:
                print(f"  {key}: {values[0]}, {statistics.median(values)}, {values[-1]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
