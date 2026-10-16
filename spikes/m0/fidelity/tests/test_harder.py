"""Tests for the L2f run-scale generator: layout, contamination, sizes, guard."""

from __future__ import annotations

from collections import Counter

import pytest

from fidelity import harder


@pytest.fixture(scope="module")
def plan():
    return harder.build()


def test_mix_and_disjoint_groups(plan):
    per = Counter((p.group, p.type) for p in plan.probes)
    for g in "abc":
        assert per[(g, "chain")] == 40 and per[(g, "multihop")] == 24 and per[(g, "distractor")] == 24
        assert per[(g, "obligations")] == 24 and per[(g, "reasoning")] == 8
    assert len({p.id for p in plan.probes}) == len(plan.probes) == 360


def test_no_restatement_and_every_statement_once_in_order(plan):
    assert harder.contamination(plan) == []
    assert harder._check_turn_counts(plan) == []


def test_groups_span_their_whole_context(plan):
    # Each group's earliest plant sits in the first build turn: depth scales with checkpoint size.
    first = plan.turns[0]
    for g in "abc":
        assert any(st in first for p in plan.probes if p.group == g for st in p.statements)
    # A later group's plants keep arriving after earlier checkpoints.
    later = "\n".join(plan.turns[1:])
    assert any(st in later for p in plan.probes if p.group == "c" for st in p.statements)
    assert not any(st in plan.turns[2] for p in plan.probes if p.group == "a" for st in p.statements)


def test_sizes_aim_below_targets_by_the_measured_undercount(plan):
    for est, target in zip(plan.estimates, harder.CHECKPOINTS, strict=True):
        aim = target / harder.UNDERCOUNT
        assert abs(est - aim) <= 0.05 * aim


def test_planned_run_fits_the_guard(plan):
    requests = []
    for est in plan.estimates:
        requests += [est - 200, est]
    expected = sum(requests) * harder.UNDERCOUNT + 3 * 4_000
    assert expected < harder.RUN_CAP


def test_obligation_wordings_are_unique():
    texts = [st for p in harder.build().probes if p.type == "obligations" for st in p.statements]
    singles = [p for p in harder.build().probes if p.type == "obligations" and p.kind == "value"]
    assert all(len(set(p.statements)) == len(p.statements) for p in singles)
    assert texts
