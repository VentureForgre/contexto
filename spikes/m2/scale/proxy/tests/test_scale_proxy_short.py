"""The short scale run: about 200 turns through the real proxy, with latency and memory budgets.

One run of ``short.measure`` (the ``short_run`` fixture in conftest.py; see ``short.py`` for what each
quantity is) serves every test here. No network beyond 127.0.0.1, no model calls, and no ``claude`` or
``codex``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scale.proxy import short  # noqa: E402


def test_every_request_of_200_turns_stays_under_the_bound(short_run):
    assert short_run["turns"] == short.TURNS and short_run["requests"] >= 2 * short.TURNS
    assert short_run["unseen_upstream"] == 0, "a request did not reach the fake upstream"
    assert all(r["status"] == 200 for r in short_run["rows"])
    assert short_run["max_upstream_tokens"] <= short_run["bound"]
    for event in ("over_bound", "bound_too_small", "transform_error"):
        assert short_run["events"].get(event, 0) == 0, short_run["events"]
    assert short_run["broken"] == []
    # The session is several times the bound, so it held only because turns were paged out.
    assert short_run["history_tokens"] > 4 * short_run["bound"]
    assert short_run["events"].get("rotation", 0) >= 10 and short_run["pages"] >= short.TURNS // 2


def test_added_time_per_request_and_its_growth_with_history(short_run):
    for key in ("added_median_ms", "added_p95_ms", "added_ms_per_mtok"):
        assert short_run[key] is not None and short_run[key] <= short.BUDGETS[key], short.report(short_run)


def test_startup_time(short_run):
    assert short_run["startup_ms"] <= short.BUDGETS["startup_ms"], short.report(short_run)


def test_memory_peak_and_growth(short_run):
    assert 0 < short_run["peak_rss_mb"] <= short.BUDGETS["peak_rss_mb"], short.report(short_run)
    assert short_run["objects_per_turn"] <= short.BUDGETS["objects_per_turn"], short.report(short_run)
    if short_run["rss_mb_per_body_mb"] is not None:  # None where ``ps`` cannot read the proxy's RSS
        assert short_run["rss_mb_per_body_mb"] <= short.BUDGETS["rss_mb_per_body_mb"], short.report(short_run)


def test_concurrent_requests_all_answer(short_run):
    burst = short_run["throughput"]
    assert burst["requests"] == short.CLIENTS * short.PER_CLIENT and burst["not_200"] == 0, burst
    assert burst["rps"] >= short.BUDGETS["throughput_floor_rps"], short.report(short_run)
