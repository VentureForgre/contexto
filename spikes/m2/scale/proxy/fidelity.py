"""MEASUREMENT (zero spend): is fast-forwarding faithful? Reach the same turn twice, once with every
request over HTTP through the proxy and once with run.py's fast-forward (turn starts only, engine called
directly), then send the same next turn over HTTP in both and compare.

    .venv/bin/python spikes/m2/scale/proxy/fidelity.py --turns 80 [--reminders mixed]

Compared: the rotation sequence (event, epoch, k, reason, turns paged; F and W are reported as the
largest difference, since the estimator's ratio sees fewer samples when fast-forwarding), the set of page ids and their bytes,
the epoch record's decision fields, and the bytes of every upstream request of the next turn.
Prints one JSON object; exit code 0 when everything matches.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[3] / "src"))

from contexto.canonical import canonical_bytes  # noqa: E402
from scale.proxy import run  # noqa: E402
from scale.proxy.upstream import FakeUpstream  # noqa: E402

DECISION_FIELDS = ("epoch", "k", "strip_upto", "turns_paged", "max_turn_growth", "last_turn_count",
                   "window_minimal_count", "head_tokens", "initial_carried", "window_budget")


def rotations(log_path: Path) -> list[tuple]:
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        for e in entry.get("transform_events") or []:
            if isinstance(e, dict) and e.get("event") in ("rotation", "window_minimal", "bound_too_small"):
                out.append((e["event"], e.get("epoch"), e.get("k"), e.get("reason"), e.get("turns_paged"),
                            e.get("fixed_overhead"), e.get("window_budget")))
    return out


def turn_start_estimates(log_path: Path) -> dict[int, int]:
    """The engine's pre-send estimate for each request, keyed by its message count (fast-forward logs
    turn starts only, so the shared keys are the turn starts)."""
    out = {}
    for line in log_path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        for e in entry.get("transform_events") or []:
            if isinstance(e, dict) and e.get("event") == "request" and "estimate" in e:
                out[e["messages"]] = e["estimate"]
    return out


def page_contents(pages_dir: Path) -> list[str]:
    """Each page's turn and messages, hashed: page ids are keyed per session, so they differ between homes."""
    out = []
    for path in pages_dir.glob("pg_*.json"):
        page = json.loads(path.read_text(encoding="utf-8"))
        out.append(hashlib.sha256(canonical_bytes({"turn": page["turn"], "messages": page["messages"]})).hexdigest())
    return sorted(out)


def reach(mode: str, turns: int, argv: list[str]) -> tuple[Path, list[dict[str, Any]], FakeUpstream]:
    args = run.build_parser().parse_args(["--label", f"fidelity-{mode}", "--rounds", "1", "--turns-per-round", "1",
                                          *argv])
    home = Path(tempfile.mkdtemp(prefix=f"contexto-scale-fid-{mode}-", dir="/tmp"))
    upstream = FakeUpstream()
    upstream.upstream_port = upstream.start()
    session = run.Session(args)
    engine_pkl = home / "engine.pkl"
    rows: list[dict[str, Any]] = []
    with open(home / "worker.log", "w") as err:
        if mode == "http":
            run.drive_http(args, session, upstream, home, engine_pkl, turns, 0, rows, err)
        else:
            run.fast_forward(args, session, home, engine_pkl, turns, home / "ff.csv", err)
        measured: list[dict[str, Any]] = []
        run.checkpoint_measure(args, session, upstream, home, engine_pkl, 0, measured, err, home)
    upstream.stop()
    return home, measured, upstream


def main(argv: list[str]) -> int:
    turns = 80
    if "--turns" in argv:
        i = argv.index("--turns")
        turns = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    homes = {}
    result: dict[str, Any] = {"turns": turns, "args": argv}
    try:
        measured = {}
        for mode in ("http", "ff"):
            homes[mode], measured[mode], _ = reach(mode, turns, argv)
        sessions = {m: h / "sessions" / "scale" for m, h in homes.items()}
        rot = {m: rotations(s / "log.jsonl") for m, s in sessions.items()}
        est = {m: turn_start_estimates(s / "log.jsonl") for m, s in sessions.items()}
        shared = sorted(set(est["http"]) & set(est["ff"]))
        diffs = [(n, est["http"][n], est["ff"][n]) for n in shared if est["http"][n] != est["ff"][n]]
        result["turn_start_estimates"] = {
            "compared": len(shared), "different": len(diffs),
            "max_abs_diff": max((abs(a - b) for _, a, b in diffs), default=0),
            "max_rel_diff": round(max((abs(a - b) / a for _, a, b in diffs), default=0.0), 5),
            "largest": sorted(diffs, key=lambda d: -abs(d[1] - d[2]))[:3]}
        pages = {m: page_contents(s / "pages") for m, s in sessions.items()}
        epoch = {m: json.loads((s / "epoch.json").read_text()) for m, s in sessions.items()}
        sent = {m: [(r["kind"], r["upstream_bytes"], r["upstream_messages"]) for r in rows] for m, rows in measured.items()}
        result.update({
            "rotations": {m: len(v) for m, v in rot.items()},
            "rotations_equal": [r[:5] for r in rot["http"]] == [r[:5] for r in rot["ff"]],
            "max_f_w_diff_tokens": max([abs((a[i] or 0) - (b[i] or 0)) for a, b in zip(rot["http"], rot["ff"])
                                        for i in (5, 6)], default=0),
            "rotation_diffs": [(a, b) for a, b in zip(rot["http"], rot["ff"]) if a[:5] != b[:5]][:4],
            "pages": {m: len(v) for m, v in pages.items()},
            "pages_equal": pages["http"] == pages["ff"],
            "epoch_fields": {f: (epoch["http"].get(f), epoch["ff"].get(f)) for f in DECISION_FIELDS
                             if epoch["http"].get(f) != epoch["ff"].get(f)},
            "next_turn_requests_equal": sent["http"] == sent["ff"],
            "next_turn_requests": sent,
        })
    finally:
        for h in homes.values():
            shutil.rmtree(h, ignore_errors=True)
    print(json.dumps(result, indent=1))
    profile = argv[argv.index("--reminders") + 1] if "--reminders" in argv else "first"
    (HERE / "results" / f"fidelity_{profile}_{turns}.json").write_text(json.dumps(result, indent=1) + "\n")
    ok = result.get("rotations_equal") and result.get("pages_equal") and result.get("next_turn_requests_equal")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
