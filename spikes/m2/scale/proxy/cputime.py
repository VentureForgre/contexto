"""MEASUREMENT (zero spend): the proxy's CPU time per request, on one or more source trees.

    .venv/bin/python spikes/m2/scale/proxy/cputime.py                        # this checkout's src
    mkdir /tmp/r1018 && git archive 62b02a16 src | tar -x -C /tmp/r1018        # another commit's package
    .venv/bin/python spikes/m2/scale/proxy/cputime.py --src /tmp/r1018/src --src src

For each tree and each history size, a child process reaches a turn the way fast-forward does (turn starts
only, no HTTP), then handles every request of the next ``--measure`` turns. Each request goes through the
proxy handler's ``_transform`` (parse, engine, serialize) and the engine's ``observe_usage``, and the
process's CPU time for the two (``time.process_time``) is taken. ``observe_usage`` gets what the proxy
would give it after the fake upstream's reply: its output tokens (``upstream.OUTPUT_TOKENS``) and, when the
tree's ``observe_usage`` takes them, ``tool_calls`` and ``tool_call_ids``. The trees alternate,
``--reps`` times, and each request's smallest time over the repetitions is kept: a request is the same
bytes every time, so the smallest is the repetition least disturbed by other work on the machine.

CPU time is not independent of the load. On the 18-core M5 Pro the same tool loop at 0.55M tokens cost 7.4
CPU-ms at a 1-minute load of 10 and 16.3 to 19.7 at loads of 32 to 51 (2026-11-12 and 13): [inference]
the scheduler puts work on efficiency cores and lowers clock speeds when the machine is busy, which the
smallest of a few repetitions does not remove. Two trees are comparable only within one invocation, which
runs them alternately at the same load; a figure from one invocation is not comparable with another's.

Every tree gets this checkout's synthetic session. Not measured: the HTTP read and relay, the session log
write, and anything that waits. Nothing is written to results/.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
ENDPOINT = "/v1/messages"


def child(src: str, turns: int, measure: int) -> int:
    """One tree, one history size: print one JSON object per measured request, as a list."""
    for p in (ROOT / "spikes" / "m2", Path(src).resolve()):
        sys.path.insert(0, str(p))
    # Imported here, not at the top: the tree is chosen at run time.
    import inspect

    from contexto import launcher
    from contexto import proxy as proxy_mod
    from scale.proxy import synth
    from scale.proxy.upstream import OUTPUT_TOKENS

    shape = synth.Shape()
    head = synth.head(shape)
    home = Path(tempfile.mkdtemp(prefix="contexto-scale-cpu-"))
    try:
        store = launcher.open_session(home, "cpu", home)
        engine, observe, name = launcher.build_engine(store, {"bound": 150_000})
        if name != "rewrite":
            raise SystemExit("contexto.rewrite is not importable from " + src)
        proxy = proxy_mod.ContextoProxy("http://127.0.0.1:9", launcher.NoticeLog(store, io.StringIO()), engine,
                                        observe_usage=observe)  # never started: nothing is sent anywhere
        handler = types.SimpleNamespace(owner=proxy, _log_transform_error=lambda *args, **kwargs: None)
        headers = dict(synth.CLI_HEADERS)
        # The keywords this tree's observe_usage takes of the two the proxy passes since D40 (older trees: none).
        takes = sorted({"tool_calls", "tool_call_ids"} & set(inspect.signature(engine.observe_usage).parameters))
        history: list[dict[str, Any]] = []
        out = []
        for t in range(turns + measure):
            messages = synth.turn(shape, t)
            for j, (end, reply) in enumerate(synth.request_points(shape, t, messages)):
                if t < turns and j > 0:
                    continue  # reaching the size: turn starts only, as worker.py ff does
                body = synth.serialize(synth.request(head, history, messages[:end]))
                entry: dict[str, Any] = {"event": "request", "path": ENDPOINT}
                c0 = time.process_time()
                sent, _, original = proxy_mod._Handler._transform(handler, body, dict(headers), entry, ENDPOINT)
                c1 = time.process_time()
                if "transform_error" in entry or "transform_skipped" in entry or original is None:
                    raise SystemExit(f"the transform did not run at turn {t}: {entry}")
                tokens = synth.tokens_of(len(sent))
                usage = {"input_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": tokens - 2,
                         "output_tokens": OUTPUT_TOKENS[reply]}
                calls_tool = reply == "tool_use"
                extra = {"tool_calls": calls_tool, "tool_call_ids": (f"toolu_cpu{t}_{j}",) if calls_tool else ()}
                engine.observe_usage(ENDPOINT, usage, original, **{k: extra[k] for k in takes})
                c2 = time.process_time()
                if t >= turns:
                    events = entry.get("transform_events") or []
                    out.append({"t": t, "j": j, "history_tokens": synth.tokens_of(len(body)), "upstream_tokens": tokens,
                                "cpu_s": c2 - c0, "transform_s": c1 - c0, "reply": reply,
                                "output_tokens": usage["output_tokens"], "observe_keywords": takes,
                                "rotated": any(e.get("event") == "rotation" for e in events)})
            history.extend(messages)
        print(json.dumps(out))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return 0


def run_child(src: str, turns: int, measure: int) -> list[dict[str, Any]]:
    done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", src, str(turns), str(measure)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"{src} at turn {turns} failed (code {done.returncode}):\n{done.stderr[-2000:]}")
    return json.loads(done.stdout)


def smallest(runs: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Per request, the repetition with the smallest CPU time."""
    best: dict[tuple[int, int], dict[str, Any]] = {}
    for rows in runs:
        for row in rows:
            key = (row["t"], row["j"])
            if key not in best or row["cpu_s"] < best[key]["cpu_s"]:
                best[key] = row
    return [best[key] for key in sorted(best)]


def ms(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "none"
    values = sorted(r["cpu_s"] * 1e3 for r in rows)
    return f"{statistics.median(values):.1f} ({values[0]:.1f} to {values[-1]:.1f}, n {len(values)})"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", action="append", help="a src directory holding contexto/ (repeat to compare)")
    parser.add_argument("--turns", default="17,76,188", help="turns to reach: 17, 76, 188 are 0.5M, 2M, 5M tokens")
    parser.add_argument("--measure", type=int, default=3, help="turns measured at each size")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--child", nargs=3, metavar=("SRC", "TURNS", "MEASURE"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return child(args.child[0], int(args.child[1]), int(args.child[2]))
    trees = args.src or [str(ROOT / "src")]
    sizes = [int(t) for t in args.turns.split(",")]
    print("CPU ms per request, _transform plus observe_usage: median (smallest to largest, n) of each request's "
          f"smallest time over {args.reps} repetitions")
    print("| History (M tokens) | Tree | Tool loop | Turn start, no rotation | Rotating turn start |")
    print("|---|---|---|---|---|")
    loops: dict[str, list[tuple[float, float]]] = {tree: [] for tree in trees}
    for turns in sizes:
        runs: dict[str, list[list[dict[str, Any]]]] = {tree: [] for tree in trees}
        for _ in range(args.reps):
            for tree in trees:  # alternating, so the trees share whatever else the machine is doing
                runs[tree].append(run_child(tree, turns, args.measure))
        for tree in trees:
            rows = smallest(runs[tree])
            if turns == sizes[0]:
                given = rows[0]["observe_keywords"] if rows else []
                print(f"{tree}: observe_usage given {', '.join(given) or 'neither tool_calls nor tool_call_ids'}",
                      file=sys.stderr)
            tool = [r for r in rows if r["j"] > 0]
            history = statistics.median(r["history_tokens"] for r in rows) / 1e6
            if tool:
                loops[tree].append((history, statistics.median(r["cpu_s"] for r in tool) * 1e3))
            print(f"| {history:.2f} | {tree} | {ms(tool)} | {ms([r for r in rows if r['j'] == 0 and not r['rotated']])} | "
                  f"{ms([r for r in rows if r['rotated']])} |", flush=True)
    for tree, points in loops.items():
        if len(points) > 1 and points[-1][0] != points[0][0]:
            slope = (points[-1][1] - points[0][1]) / (points[-1][0] - points[0][0])
            print(f"{tree}: a tool loop costs {slope:.1f} ms more per million tokens of history "
                  f"({points[0][0]:.2f}M to {points[-1][0]:.2f}M)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
