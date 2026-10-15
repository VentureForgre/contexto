"""Tests for the L2d quality-versus-context probe: plan, contamination, budget, scorer."""

from __future__ import annotations

import json
import statistics

import pytest

from fidelity import l2d


@pytest.fixture(scope="module")
def plan():
    return l2d.build()


def test_plan_is_deterministic(plan):
    again = l2d.build()
    assert [t.text for t in again.turns] == [t.text for t in plan.turns]
    assert again.plants == plan.plants
    assert [t.text for t in l2d.build(seed=1).turns] != [t.text for t in plan.turns]


def test_each_checkpoint_has_its_own_balanced_group(plan):
    ids = [pid for pid, _ in l2d.probe_index(plan)]
    assert len(ids) == len(set(ids)) == 3 * 4 * l2d.PER_CATEGORY
    for k in (1, 2, 3):
        group = [p for p in plan.plants if p.checkpoint == k]
        for category in l2d.CATEGORIES:
            cells = [p for p in group if p.category == category]
            assert len(cells) == l2d.PER_CATEGORY
            assert sum(p.segment == "deep" for p in cells) == l2d.DEEP_SHARE
        statuses = {p.value for p in group if p.category == "obligation"}
        assert statuses == {"open", "closed"}
        assert all(p.older for p in group if p.category == "superseded")
    values = [p.value for p in plan.plants if p.category != "obligation"]
    assert len(values) == len(set(values))


def test_no_plant_is_restated_anywhere(plan):
    assert l2d.contamination(plan) == []


def test_contamination_scan_catches_a_restatement(plan):
    victim = next(p for p in plan.plants if p.checkpoint == 3 and p.category == "unflagged")
    growth = next(t for t in plan.turns if t.id == "G2")
    original = growth.files[0]["content"]
    growth.files[0]["content"] = original + f"echo {victim.value}\n"
    try:
        hits = l2d.contamination(plan)
    finally:
        growth.files[0]["content"] = original
    assert any(victim.key in h and "G2" in h for h in hits)


def test_probe_turn_only_names_its_own_group(plan):
    for turn in plan.turns:
        if turn.kind != "probe":
            continue
        for p in plan.plants:
            named = p.subject in turn.text
            assert named == (p.checkpoint == turn.checkpoint), (turn.id, p.key)
            assert p.value not in turn.text or p.category == "obligation"


def test_statements_precede_their_probe_in_order(plan):
    order = {t.id: i for i, t in enumerate(plan.turns)}
    for p in plan.plants:
        where = []
        for stmt in p.statements:
            hits = [t.id for t in plan.turns if stmt in t.text or any(stmt in f["content"] for f in t.files)]
            assert len(hits) == 1, (p.key, stmt)
            where.append(order[hits[0]])
        assert where == sorted(where)
        assert where[-1] < order[f"P{p.checkpoint}"]
        expected = "deep" if plan.turns[where[-1]].kind == "opening" else "growth"
        assert p.segment == expected, p.key


def test_deep_plants_sit_at_matched_positions_across_checkpoints(plan):
    offsets = l2d.plant_offsets(plan)
    medians = [
        statistics.median(offsets[p.key] for p in plan.plants if p.checkpoint == k and p.segment == "deep")
        for k in (1, 2, 3)
    ]
    assert max(medians) - min(medians) <= 0.05 * max(medians)


def test_standing_rule_markers_appear_only_in_the_rules_turn(plan):
    assert len(set(plan.markers)) == 3
    for turn in plan.turns[1:]:
        for marker in plan.markers:
            assert marker not in turn.text
            assert all(marker not in f["content"] for f in turn.files)
    assert all(m in plan.turns[0].text for m in plan.markers)


@pytest.mark.parametrize("head", [25_000, 40_000])
def test_budget_fits_cap_and_reaches_checkpoints(head):
    plan = l2d.build(head=head)
    reqs = l2d.requests(plan)
    contexts = [r.context for r in reqs if r.note.startswith("checkpoint")]
    assert contexts[0] >= l2d.CHECKPOINTS[0]
    for got, target in zip(contexts[1:], l2d.CHECKPOINTS[1:], strict=True):
        assert abs(got - target) <= 0.1 * target
    assert sum(r.context + r.output for r in reqs) < 0.85 * l2d.TOKEN_CAP
    assert sum(r.context + r.output for r in l2d.requests(plan, parallel=False)) > l2d.TOKEN_CAP


def test_replay_arms_share_content(plan):
    reqs = l2d.replay_requests(plan)
    arms = [r for r in reqs if r.note.endswith("arm")]
    assert len(arms) == 6
    for a, b in zip(arms[::2], arms[1::2], strict=True):
        assert a.context == b.context and a.note == "preserved arm" and b.note == "stripped arm"


def _answers(plan, k, transform=lambda p, v: v, marker=None):
    lines = []
    for pid, p in l2d.probe_index(plan):
        if p.checkpoint == k:
            lines.append(f"{pid}: {transform(p, p.value)}{' ' + marker if marker else ''}")
    return "\n".join(lines)


def _truth(plan, k):
    return [
        {"id": pid, "checkpoint": p.checkpoint, "category": p.category, "segment": p.segment,
         "answer": p.value, "older": list(p.older), "distance": 1_000 * (i + 1)}
        for i, (pid, p) in enumerate(l2d.probe_index(plan)) if p.checkpoint == k
    ]


def test_scorer_perfect_answers_with_marker(plan):
    result = l2d.score_checkpoint(_truth(plan, 1), _answers(plan, 1, marker=plan.markers[0]), plan.markers[0])
    assert result["correct"] == result["total"] == 96
    assert result["instruction"]["compliant"] == result["instruction"]["lines"] == 96
    assert result["interval"][0] > 0.96


def test_scorer_counts_stale_unknown_and_missing_marker(plan):
    def degrade(p, v):
        if p.category == "superseded":
            return p.older[0]
        if p.category == "flagged" and p.segment == "deep":
            return "unknown"
        return v

    result = l2d.score_checkpoint(_truth(plan, 2), _answers(plan, 2, degrade), plan.markers[1])
    cats = result["tables"]["category"]
    assert cats["superseded"] == {"correct": 0, "total": 24, "stale": 24, "unknown": 0}
    assert cats["flagged"]["unknown"] == 16 and cats["flagged"]["correct"] == 8
    assert result["instruction"]["compliant"] == 0
    assert result["correct"] == 96 - 24 - 16


def test_wilson_interval():
    lo, hi = l2d.wilson(24, 24)
    assert 0.85 < lo < 0.87 and hi == 1.0
    lo, hi = l2d.wilson(48, 96)
    assert lo < 0.5 < hi


def test_generate_writes_files_and_no_answers_in_probes(tmp_path, plan):
    paths = l2d.write_outputs(plan, tmp_path)
    probes = [json.loads(line) for line in paths["probes.jsonl"].read_text().splitlines()]
    truth = [json.loads(line) for line in paths["truth.jsonl"].read_text().splitlines()]
    assert len(probes) == len(truth) == 288
    assert all("answer" not in p for p in probes)
    assert json.loads(paths["contamination.json"].read_text())["violations"] == []
    assert all(row["distance"] > 0 for row in truth)
    files = sorted((tmp_path / "files").iterdir())
    assert len(files) == sum(len(t.files) for t in plan.turns)
