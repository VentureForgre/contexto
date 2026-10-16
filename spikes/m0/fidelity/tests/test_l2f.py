"""Tests for the L2f harder probe set: plan, contamination, scoring, budget."""

from __future__ import annotations

from collections import Counter

import pytest

from fidelity import l2f


@pytest.fixture(scope="module")
def plan():
    return l2f.build()


def test_plan_is_deterministic(plan):
    again = l2f.build()
    assert [t.text for t in again.turns] == [t.text for t in plan.turns] and again.probes == plan.probes


def test_every_group_has_every_type_and_tier(plan):
    per = Counter((p.group, p.type) for p in plan.probes)
    for g in plan.groups:
        for t in ("multihop", "chain", "obligations", "distractor"):
            assert per[(g, t)] == 8
        assert per[(g, "reasoning")] == 2
    tiers = Counter(p.tier for p in plan.probes)
    assert tiers["2-hop"] == tiers["3-hop"] and tiers["2-dups"] == tiers["5-dups"]
    singles = Counter(p.answer for p in plan.probes if p.type == "obligations" and p.kind == "value")
    assert singles["open"] == singles["closed"]


def test_no_plant_is_restated_and_the_scan_catches_one(plan):
    assert l2f.contamination(plan) == []
    victim = next(p for p in plan.probes if p.type == "chain")
    original = plan.turns[2].text
    plan.turns[2].text = original + f"\nReminder: {victim.subjects[0]}."
    try:
        assert any(victim.id in v for v in l2f.contamination(plan))
    finally:
        plan.turns[2].text = original


def test_statements_and_plants_sit_in_the_build_turns(plan):
    text = "\n".join(t.text for t in plan.turns)
    for p in plan.probes:
        for st in p.statements:
            assert st in text, (p.id, st)


def test_reasoning_answers_are_computed_from_the_table(plan):
    for p in plan.probes:
        if p.type != "reasoning":
            continue
        table = p.statements[0]
        rows = [line.split(" | ") for line in table.split("\n") if line.startswith("r")]
        fails = sum(int(v) for _, v, s in rows if s == "fail")
        expected = fails if p.tier == "1-step" else (fails * 7) % 1000
        assert p.answer == str(expected)
        assert "Reply with exactly: OK" in plan.turns[1].text


def test_scoring_handles_sets_stale_and_tiers(plan):
    group = [p for p in plan.probes if p.group == "a"]
    lines = []
    for p in group:
        if p.kind == "set":
            lines.append(f"{p.id}: {', '.join(reversed(p.answer.split(', ')))}")  # order must not matter
        elif p.type == "chain" and p.older:
            lines.append(f"{p.id}: {p.older[0]}")  # a superseded value scores stale
        else:
            lines.append(f"{p.id}: {p.answer}")
    result = l2f.score(group, "\n".join(lines))
    chain = result["cells"]["chain"]
    assert chain["correct"] == 0 and chain["stale"] == 8
    assert result["cells"]["obligations/set"]["correct"] == 4
    assert result["correct"] == len(group) - 8


def test_budget_gate_and_strip_arm():
    with_strip = l2f.run_budget(strip=True)
    without = l2f.run_budget(strip=False)
    assert with_strip["total"] > without["total"]
    assert with_strip["worst_gate"] >= with_strip["total"] - 1
