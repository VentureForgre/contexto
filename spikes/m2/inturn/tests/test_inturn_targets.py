"""What D63 (in-turn paging) must deliver, one test per scenario and dialect.

Each test fails at v0.5.24, where the engine pages only at a turn start. The engine pull request fills
in ``inturn/stubs.py`` and removes the xfail marks. ``targets.unmet`` lists every target a run misses;
``targets.py`` states each rule, and ``test_inturn_engines.py`` shows, for each rule, an engine that
breaks it and fails.

The marks are strict, so a scenario that starts to pass shows up as a failure until its mark is
removed, and they expect an AssertionError, so a crash in the harness is an error and not an expected
failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from inturn import harness, scripts, targets  # noqa: E402

CASES = [(name, dialect) for name in scripts.BUILDERS for dialect in scripts.build(name, tiny=True).dialects]
NOT_BUILT = pytest.mark.xfail(strict=True, raises=AssertionError, reason="D63: in-turn paging not built")
BOTH = pytest.mark.parametrize("dialect", ["claude", "codex"])


def check(name: str, dialect: str) -> None:
    problems = targets.unmet(harness.cached(name, dialect))
    assert not problems, f"{name} on {dialect}:\n" + "\n".join(problems)


@NOT_BUILT
@BOTH
def test_loop120_stays_under_the_bound(dialect):
    check("loop120", dialect)


@NOT_BUILT
@BOTH
def test_large_results_stay_under_the_bound(dialect):
    check("large", dialect)


@NOT_BUILT
@BOTH
def test_many_small_results_stay_under_the_bound(dialect):
    check("small", dialect)


@NOT_BUILT
def test_a_subagents_loop_stays_under_the_bound():
    check("subagent", "claude")


@NOT_BUILT
@BOTH
def test_results_with_images_stay_under_the_bound(dialect):
    check("images", dialect)


@NOT_BUILT
@BOTH
def test_a_long_loop_between_short_turns_stays_under_the_bound(dialect):
    check("multiturn", dialect)


@NOT_BUILT
@BOTH
def test_parallel_calls_reminders_and_custom_tool_calls_stay_under_the_bound(dialect):
    check("mixed", dialect)


def test_there_is_one_target_test_for_each_scenario_and_dialect():
    assert len(CASES) == 13
