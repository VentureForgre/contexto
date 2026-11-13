"""The short scale run: 200 turns through the real proxy, with latency and memory budgets.

One run of ``short.measure`` (the ``short_run`` fixture in conftest.py; see ``short.py`` for what each
quantity is) serves every test here. No network beyond 127.0.0.1, no model calls, and no ``claude`` or
``codex``. The run's numbers are printed at the end of the pytest output, pass or fail.

The budgets (``short.BUDGETS``) next to what was measured on 2026-11-12, and the factor between them.
"CI" is the slowest of the six runners in this branch's first CI run (Ubuntu with 4 CPUs and macOS with 3,
Python 3.12 to 3.15, loads under 2.4), "fastest CI" the fastest. "Laptop" is an 18-core Apple M5 Pro on
which other test suites were running, over 39 runs at 1-minute loads of 25 to 84.

    quantity                                budget     CI      factor  fastest CI  laptop, 39 runs
    added time per request, median          200 ms     50.9    3.9     17.1        31.7 to 79.2
    added time per request, p95             800 ms     109.8   7.3     28.2        50.3 to 388.2
    growth of added time, per M tokens      300 ms     36.5    8.2     14.7        20.4 to 164.6
    startup of the proxy, best of 3         3,000 ms   423     7.1     76.6        133 to 1,049
    peak RSS of the proxy                   300 MB     66.0    4.5     55.3        51.6 to 62.8
    RSS growth per MB the body grows        150 MB     17.9    8.4     3.5         6.4 to 13.9
    live objects gained per turn            40         8.91    4.5     7.27        7.11 to 7.59
    100 concurrent requests, floor          5 req/s    49.8    10      225.8       15.4 to 124.6

The latency budgets are loose because one macOS runner and the loaded laptop were several times slower than
the rest; a budget that fails on a slow runner would be worse than a loose one. What they still catch: the
session ends at 1,269 messages, so a pass over every pair of messages is 1.6 million steps per request by
then. At 1 microsecond a step that is 1.6 s per request, eight times the median budget; at 20 nanoseconds
a step (a membership test in C) it is 32 ms and passes. The memory budgets depend little on the machine:
a proxy that kept each request's parsed messages would gain thousands of objects per turn, and one that
kept each request's bytes would add about 0.9 MB of RSS per request, more than 500 MB by the end.
Throughput is only a floor: it spread from 15 to 226 requests a second across these runs, and the 100
clients share one Python process with the fake upstream, so it says little about the proxy alone.
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
