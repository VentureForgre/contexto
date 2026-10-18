"""MEASUREMENT (zero spend): does the proxy's memory grow per request? Fast-forward to a history size, then
keep the session going over HTTP for many requests in one proxy process, with ``gc.collect()`` and a ``ps``
RSS reading after each request.

    .venv/bin/python spikes/m2/scale/proxy/leak.py --at 30 --turns 25 [--trace]

Writes results/leak_<at>M.csv (one row per request; ``--label`` overrides the name). ``--trace`` adds a tracemalloc snapshot at the end
(slower; printed, not timed).
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from scale.proxy import run, synth  # noqa: E402
from scale.proxy.upstream import FakeUpstream  # noqa: E402


def main(argv: list[str]) -> int:
    at, turns, trace, label = 5.0, 15, False, None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--at":
            at, i = float(argv[i + 1]), i + 2
        elif argv[i] == "--turns":
            turns, i = int(argv[i + 1]), i + 2
        elif argv[i] == "--trace":
            trace, i = True, i + 1
        elif argv[i] == "--label":
            label, i = argv[i + 1], i + 2
        else:
            rest.append(argv[i])
            i += 1
    label = label or f"{at:g}M"
    args = run.build_parser().parse_args(["--label", f"leak-{label}", *rest])
    home = Path(tempfile.mkdtemp(prefix="contexto-scale-leak-", dir="/tmp"))
    upstream = FakeUpstream()
    upstream.upstream_port = upstream.start()
    session = run.Session(args)
    engine_pkl = home / "engine.pkl"
    out_rows = []
    try:
        with open(home / "worker.log", "w") as err:
            run.fast_forward(args, session, home, engine_pkl, session.turn_reaching(int(at * 1e6)), home / "ff.csv", err)
            worker = run.Worker(args, home, engine_pkl, upstream.upstream_port, err)
            try:
                if trace:
                    worker.call({"cmd": "trace_start"})
                base = worker.call({"cmd": "gc"})
                out_rows.append({"request": 0, "body_mb": 0, **{k: base[k] for k in ("rss_mb", "maxrss_mb", "objects",
                                                                                      "threads")}})
                n = 0
                for _ in range(turns):
                    messages = session.next_messages()
                    for j, (end, reply) in enumerate(synth.request_points(session.shape, session.turn, messages)):
                        body = session.wire.body(messages[:end])
                        result = run.send(worker.port, body, f"t{session.turn}r{j}", reply)
                        upstream.take(f"t{session.turn}r{j}")
                        n += 1
                        probe = worker.call({"cmd": "gc"})
                        out_rows.append({"request": n, "body_mb": round(len(body) / 1e6, 2), "status": result["status"],
                                         "e2e_s": round(result["e2e_s"], 3),
                                         **{k: probe[k] for k in ("rss_mb", "maxrss_mb", "objects", "threads")}})
                        print(json.dumps(out_rows[-1]), file=sys.stderr, flush=True)
                    session.finish_turn()
                worker.call({"cmd": "stats"})
                if trace:
                    print(json.dumps(worker.call({"cmd": "trace_top", "n": 10}), indent=1))
            finally:
                worker.close()
    finally:
        upstream.stop()
        shutil.rmtree(home, ignore_errors=True)
    path = HERE / "results" / f"leak_{label}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=("request", "body_mb", "status", "e2e_s", "rss_mb", "maxrss_mb",
                                               "objects", "threads"))
        writer.writeheader()
        writer.writerows(out_rows)
    first, last = out_rows[1], out_rows[-1]
    print(json.dumps({"requests": len(out_rows) - 1, "rss_after_first_mb": first["rss_mb"],
                      "rss_after_last_mb": last["rss_mb"], "objects_first": first["objects"],
                      "objects_last": last["objects"], "maxrss_last_mb": last["maxrss_mb"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
