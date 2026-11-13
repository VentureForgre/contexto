"""The short scale run: about 200 turns through the real proxy, with latency and memory budgets.

One run of ``short.measure`` (see ``short.py`` for what each quantity is) serves every test here. No
network beyond 127.0.0.1, no model calls, and no ``claude`` or ``codex``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scale.proxy import short  # noqa: E402

# Set to a file path to append each run's measurements to it, one JSON object per line (the flake check
# in README.md, "The short run", reads the spread from it).
OUT_ENV = "CONTEXTO_SCALE_SHORT_OUT"


@pytest.fixture(scope="module")
def result(tmp_path_factory, request):
    measured = short.measure(home=tmp_path_factory.mktemp("scale-short"))
    request.config.stash[short.STASH_KEY] = short.report(measured)
    out = os.environ.get(OUT_ENV)
    if out:
        with open(out, "a") as f:
            f.write(json.dumps({k: v for k, v in measured.items() if k != "rows"}) + "\n")
    return measured


def test_every_request_of_200_turns_stays_under_the_bound(result):
    assert result["turns"] == short.TURNS and result["requests"] >= 2 * short.TURNS
    assert result["unseen_upstream"] == 0, "a request did not reach the fake upstream"
    assert all(r["status"] == 200 for r in result["rows"])
    assert result["max_upstream_tokens"] <= result["bound"]
    for event in ("over_bound", "bound_too_small", "transform_error"):
        assert result["events"].get(event, 0) == 0, result["events"]
    assert result["broken"] == []
    # The session is ten times the bound, so it held only because turns were paged out.
    assert result["history_tokens"] > 10 * result["bound"]
    assert result["events"].get("rotation", 0) >= 10 and result["pages"] >= short.TURNS // 2


def test_added_time_per_request_and_its_growth_with_history(result):
    for key in ("added_median_ms", "added_p95_ms", "added_ms_per_mtok"):
        assert result[key] is not None and result[key] <= short.BUDGETS[key], short.report(result)


def test_startup_time(result):
    assert result["startup_ms"] <= short.BUDGETS["startup_ms"], short.report(result)


def test_memory_peak_and_growth(result):
    assert 0 < result["peak_rss_mb"] <= short.BUDGETS["peak_rss_mb"], short.report(result)
    assert result["objects_per_turn"] <= short.BUDGETS["objects_per_turn"], short.report(result)
    if result["rss_mb_per_body_mb"] is not None:  # None where ``ps`` cannot read the proxy's RSS
        assert result["rss_mb_per_body_mb"] <= short.BUDGETS["rss_mb_per_body_mb"], short.report(result)


def test_concurrent_requests_all_answer(result):
    burst = result["throughput"]
    assert burst["requests"] == short.CLIENTS * short.PER_CLIENT and burst["not_200"] == 0, burst
    assert burst["rps"] >= short.BUDGETS["throughput_floor_rps"], short.report(result)
