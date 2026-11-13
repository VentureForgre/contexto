"""What D63 (in-turn paging) must deliver, one test per scenario and dialect.

Each test fails at v0.5.24, where the engine pages only at a turn start. The engine pull request fills
in ``inturn/stubs.py`` and removes the xfail marks. ``targets.unmet`` lists every target a run misses:

- no ``over_bound`` event, from the pre-send estimate or from real usage;
- the largest forwarded request at or under the bound, by the estimate and by the upstream's count;
- no violation of any kind at the upstream;
- every stub's stored original reachable (``stubs.find_stubs``), and no tool result lost;
- the published recall room never 0 once the turn's own items have passed 50,000 tokens.

The marks are strict, so a scenario that starts to pass shows up as a failure until its mark is removed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from inturn import harness, scripts, targets  # noqa: E402

CASES = [(name, dialect) for name in scripts.BUILDERS for dialect in scripts.build(name, quick=True).dialects]


def check(name: str, dialect: str) -> None:
    problems = targets.unmet(harness.cached(name, dialect))
    assert not problems, f"{name} on {dialect}:\n" + "\n".join(problems)


@pytest.mark.xfail(strict=True, reason="D63: in-turn paging not built")
@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_loop120_stays_under_the_bound(dialect):
    check("loop120", dialect)


@pytest.mark.xfail(strict=True, reason="D63: in-turn paging not built")
@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_large_results_stay_under_the_bound(dialect):
    check("large", dialect)


@pytest.mark.xfail(strict=True, reason="D63: in-turn paging not built")
@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_many_small_results_stay_under_the_bound(dialect):
    check("small", dialect)


@pytest.mark.xfail(strict=True, reason="D63: in-turn paging not built")
def test_a_subagents_loop_stays_under_the_bound():
    check("subagent", "claude")


@pytest.mark.xfail(strict=True, reason="D63: in-turn paging not built")
@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_results_with_images_stay_under_the_bound(dialect):
    check("images", dialect)


@pytest.mark.xfail(strict=True, reason="D63: in-turn paging not built")
@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_a_long_loop_between_short_turns_stays_under_the_bound(dialect):
    check("multiturn", dialect)


def test_there_is_one_target_test_for_each_scenario_and_dialect():
    assert len(CASES) == 11
