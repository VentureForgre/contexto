"""The short scale run: 200 turns through the real proxy, with latency and memory budgets.

One run of ``short.measure`` (the ``short_run`` fixture in conftest.py; see ``short.py`` for what each
quantity is) serves every test here. No network beyond 127.0.0.1, no model calls, and no ``claude`` or
``codex``. The run's numbers are printed at the end of the pytest output, pass or fail.

The budgets (``short.BUDGETS``) next to what was measured on 2026-11-12, and the factor between them.
"CI" is the slowest of the six runners in this branch's first CI run (Ubuntu with 4 CPUs and macOS with 3,
Python 3.12 to 3.15, loads under 2.4), "fastest CI" the fastest; peak RSS on Linux was read wrongly in that
run, so that row also counts the second run's four Ubuntu jobs. "Laptop" is an 18-core Apple M5 Pro on
which other test suites were running, over 39 runs at 1-minute loads of 25 to 84.

    quantity                                budget     CI      factor  fastest CI  laptop, 39 runs
    added time per request, median          200 ms     50.9    3.9     17.1        31.7 to 79.2
    added time per request, p95             800 ms     109.8   7.3     28.2        50.3 to 388.2
    growth of added time, per M tokens      300 ms     36.5    8.2     14.7        20.4 to 164.6
    startup of the proxy, best of 3         3,000 ms   423     7.1     76.6        133 to 1,049
    peak RSS of the proxy                   300 MB     86.8    3.5     52.0        51.6 to 62.8
    RSS growth per MB the body grows        150 MB     17.9    8.4     3.5         6.4 to 13.9
    live objects gained per turn            40         8.91    4.5     7.27        7.11 to 7.59
    100 concurrent requests, floor          5 req/s    49.8    10      225.8       15.4 to 124.6

The latency budgets are loose because one macOS runner and the loaded laptop were several times slower than
the rest; a budget that fails on a slow runner would be worse than a loose one. What they catch was
measured by Reviewer B on 2026-11-12, by injecting slowdowns into the package of a scratch copy on the
laptop (load 7 to 10), and from the CI runs' values:

- A linear slowdown is caught only when it is large. On the laptop, 2, 4, 8, 12 and 16 times the proxy's
  CPU work per request all passed (16 times: median 170 ms against 200). From the Ubuntu runners' values
  the threshold there is about 8.5 to 12 times on the median and 9 to 13 times on growth; only the slow
  macOS runner (median 50.9 ms) is near 4 times.
- A quadratic pass over the 1,269 messages of the last request is caught when a step costs about 200 ns
  (growth 402 ms per million tokens against 300). A cheap one passes: a pair loop at 5 ns a step, a dict
  ``.get`` comparison at 26 ns (growth 65) and a list membership test all passed. The full run at 30M
  tokens, 28 times as many messages, is the check for those.
- A leak: a proxy that kept each request's parsed messages gained 5,203 objects per turn (budget 40); one
  that kept each request's bytes peaked at 622 MB (budget 300). A leak under about 350 KB per request that
  is not made of Python objects passes.
- A start of 3 seconds or more fails (3,119 ms); a 2.5-second sleep in ``start_proxy`` passed.

Not caught: a slowdown of less than about 10 times on a typical runner, a cheap quadratic pass, a small
leak outside Python objects, and anything the 200-turn session (0.9M tokens) is too short to show.
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


def test_the_driver_keeps_its_connections_open(short_run):
    """One connection to the proxy for the session, one each for the first-request sends and for the 104-byte
    sends with the proxy as shipped and with TCP_NODELAY on its sockets, and one per client of the burst. A connection per request left about 850 of this machine's local ports in TIME_WAIT
    per run, and several runs at once failed with EADDRNOTAVAIL (README.md, "Connections")."""
    assert short_run["driver_connections"] == short.CLIENTS + 4, short_run["connections"]
    # The proxy opens a new upstream connection per request (contexto.proxy, by design): every request
    # it forwarded, and the direct sends.
    assert short_run["upstream_connections"] == short_run["requests"] + short.CLIENTS * short.PER_CLIENT \
        + 5 * short.SMALL_SENDS, short_run["connections"]


def test_what_the_proxy_adds_to_a_request_with_no_history_is_measured(short_run):
    """The session's first request and a 104-byte one, each through the proxy and straight to the fake
    upstream: the audit of 2026-11-12 compared its "about +2 ms" with these (README.md)."""
    for key in ("first_request_over_direct_ms", "tiny_request_over_direct_ms", "tiny_request_headers_over_direct_ms",
                "tiny_request_nodelay_over_direct_ms"):
        assert short_run[key] is not None, short.report(short_run)
    assert short_run["tiny_request_bytes"] == len(short.TINY_BODY) < 200


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
