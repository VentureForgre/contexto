"""Summarize scale runs: one markdown table row per checkpoint, measured numbers only.

    .venv/bin/python spikes/m2/scale/proxy/summarize.py first-1 first-2 first-3

Each label is one run (results/requests_<label>.csv, results/checkpoints_<label>.csv). Timings are the
median over runs of each run's median (the README's "median of 3"); p95 pools every sample of every run.
Only ``phase == measure`` rows count (drive and profiled requests are excluded).
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def read(path: Path) -> list[dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def num(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))]


def median_of_medians(per_run: list[list[float]]) -> float | None:
    medians = [statistics.median(v) for v in per_run if v]
    return statistics.median(medians) if medians else None


def metric(runs: list[list[dict[str, str]]], checkpoint: str, field: str, kind: str | None = None,
           rotated: str | None = None) -> tuple[float | None, float | None, int]:
    per_run = []
    for rows in runs:
        values = [num(r[field]) for r in rows if r["checkpoint_m"] == checkpoint and r["phase"] == "measure"
                  and (kind is None or r["kind"] == kind) and (rotated is None or r["rotated"] == rotated)]
        per_run.append([v for v in values if v is not None])
    pooled = [v for run in per_run for v in run]
    return median_of_medians(per_run), p95(pooled), len(pooled)


def fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}" if digits else f"{value:,.0f}"


def summarize(labels: list[str]) -> str:
    runs = [read(RESULTS / f"requests_{label}.csv") for label in labels]
    cps = [read(RESULTS / f"checkpoints_{label}.csv") for label in labels]
    checkpoints = [row["checkpoint_m"] for row in cps[0]]
    lines = [
        f"Runs: {', '.join(labels)}. Seconds; median of per-run medians (p95 pooled, n samples).",
        "",
        "| History (M tokens) | Body MB | Turn start e2e | Tool loop e2e | Proxy transform, turn start | "
        "Proxy transform, tool loop | Engine, tool loop | Rotating turn start: e2e | index_text per call | "
        "Max upstream tokens | Proxy peak RSS MB | Pages | Store MB |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cp in checkpoints:
        hist = [num(r["history_tokens"]) for run in runs for r in run if r["checkpoint_m"] == cp
                and r["phase"] == "measure"]
        body = [num(r["body_mb"]) for run in runs for r in run if r["checkpoint_m"] == cp and r["phase"] == "measure"]
        if not hist:
            continue

        def cell(field: str, kind: str | None = None, rotated: str | None = None) -> str:
            med, p, n = metric(runs, cp, field, kind, rotated)
            return f"{fmt(med)} (p95 {fmt(p)}, n {n})" if n else "n/a"

        index_calls = []
        for run in runs:
            for r in run:
                if r["checkpoint_m"] == cp and r["phase"] == "measure" and num(r["index_text_n"]):
                    index_calls.append(num(r["index_text_s"]) / num(r["index_text_n"]))
        cp_rows = [row for rows in cps for row in rows if row["checkpoint_m"] == cp]
        max_up = max(num(row["max_upstream_tokens"]) or 0 for row in cp_rows)
        rss = statistics.median(num(row["proxy_maxrss_mb"]) for row in cp_rows)
        lines.append(
            f"| {min(hist) / 1e6:.2f} to {max(hist) / 1e6:.2f} | {min(body):.1f} to {max(body):.1f} | "
            f"{cell('e2e_s', 'turn_start')} | {cell('e2e_s', 'tool_loop')} | {cell('proxy_transform_s', 'turn_start')} | "
            f"{cell('proxy_transform_s', 'tool_loop')} | {cell('engine_s', 'tool_loop')} | "
            f"{cell('e2e_s', 'turn_start', '1')} | "
            f"{fmt(statistics.median(index_calls)) if index_calls else 'n/a'} | {fmt(max_up, 0)} | {fmt(rss, 0)} | "
            f"{cp_rows[0]['pages']} | {cp_rows[0]['store_mb']} |")
    lines += ["", "Events in the session log through each checkpoint (first run):", "",
              "| History (M) | Reached by | rotation | window_minimal | bound_too_small | over_bound | "
              "carried_over_budget | turn_room_low | estimate_error | transform_error | load (uptime) |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in cps[0]:
        lines.append(f"| {row['checkpoint_m']} | {row['reached_by']} | {row['n_rotation']} | {row['n_window_minimal']} | "
                     f"{row['n_bound_too_small']} | {row['n_over_bound']} | {row['n_carried_over_budget']} | "
                     f"{row['n_turn_room_low']} | {row['n_estimate_error']} | {row['n_transform_error']} | "
                     f"{row['uptime'].split('load averages:')[-1].strip()} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    print(summarize(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
