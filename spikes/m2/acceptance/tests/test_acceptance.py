"""Tests for the M2 acceptance harness: script, budget, detectors, driver guard, pass rule.

Fixtures are synthetic: no content from a real session is committed.
"""

from __future__ import annotations

import io
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import budget, detectors, driver, run, script  # noqa: E402


@pytest.fixture(scope="module")
def s():
    return script.build(turns=17, turn_bytes=30_000)


# Script


def test_script_is_deterministic_and_chain_heavy(s):
    again = script.build(turns=17, turn_bytes=30_000)
    assert again.turns == s.turns and again.probes == s.probes
    types = Counter(p.type for p in s.probes)
    assert types["chain"] == 30 and types["chain"] > sum(v for k, v in types.items() if k != "chain")
    assert types["paged_only"] == script.PAGED_ONLY


def test_no_plant_is_restated(s):
    assert script.restatements(s) == []


def test_paged_only_plants_sit_only_in_the_first_turn(s):
    for p in s.probes:
        if p.id in s.paged_only:
            assert p.statements[0] in s.turns[0]
            assert not any(p.statements[0] in t for t in s.turns[1:])


def test_every_plant_statement_appears_once_and_the_probe_turn_holds_no_answer(s):
    joined = "\n".join(s.turns[:-1])
    for p in s.probes:
        for st in p.statements:
            assert joined.count(st) >= 1
        if p.kind == "value" and p.type != "obligations":
            assert p.answer not in s.turns[-1]


def test_turns_reach_the_requested_size(s):
    for size in s.turn_bytes[:-1]:
        assert abs(size - 30_000) <= 0.03 * 30_000


# Budget


def test_rotation_always_retains_the_newest_complete_turn(s):
    b = budget.simulate(list(s.turn_bytes[:-1]), s.turn_bytes[-1], budget.Params(bound=100_000))
    for row in b.rows[1:]:
        if row.kind == "turn":
            assert row.retained_turns >= 1
    assert b.rotations >= 1 and b.history_tokens >= 2.5 * 100_000
    assert b.planned_over_bound == 0
    recall_rows = [r for r in b.rows if r.kind == "recall"]
    assert recall_rows and all(r.epoch == b.rows[-1].epoch for r in recall_rows)  # tool loop never rotates


def test_budget_lines_and_worst_case(s):
    p = budget.Params(bound=100_000)
    b = budget.simulate(list(s.turn_bytes[:-1]), s.turn_bytes[-1], p)
    assert b.h1_worst > b.h1_expected > 0
    assert b.baseline_cli > b.baseline_replay > b.history_tokens
    assert b.h1_worst - b.h1_expected == sum(p.max_tokens_reserve - r.output_expected for r in b.rows)


def test_bound_too_small_when_the_window_budget_cannot_hold_a_turn():
    # 44 KB turns at a 54.5k head and a 100k bound: W = 20,500 cannot hold a 22k-token turn (rule 4).
    big = script.build(turns=12, turn_bytes=44_000)
    b = budget.simulate(list(big.turn_bytes[:-1]), big.turn_bytes[-1], budget.Params(bound=100_000))
    assert b.bound_too_small > 0 and b.planned_over_bound > 0 and b.rotations == 0
    assert b.window_budget_first == 100_000 - 25_000 - 54_500


def test_conservative_estimator_for_log_text():
    assert budget.turn_tokens(20_000) == 10_001  # 2.0 bytes per token, not 3.5


# Detectors


def _req(epoch, inp, cache_read=0, over=False, status=200, events=()):
    usage = {"input_tokens": inp - cache_read, "cache_creation_input_tokens": 0, "cache_read_input_tokens": cache_read,
             "output_tokens": 10}
    return {"event": "request", "path": "/v1/messages", "status": status, "usage": usage, "over_bound": over,
            "epoch": epoch, "transform_events": list(events)}


def test_detectors_on_a_clean_synthetic_session():
    log = [
        {"event": "proxy_start", "port": 1},
        _req(1, 60_000),
        _req(1, 70_000, cache_read=60_000),
        {"event": "request", "path": "/v1/messages/count_tokens", "status": 200, "usage": None},
        _req(2, 80_000, events=[{"event": "rotation", "epoch": 2, "k": 3}]),
        _req(2, 85_000, cache_read=78_000),
        _req(2, 120_000, cache_read=80_000, over=True),
        {"event": "mcp_call", "tool": "contexto_recall", "ok": True, "page_id": "pg_x"},
    ]
    cc = [{"type": "user"}, {"type": "assistant"}, {"type": "system", "subtype": "api_error"}]
    r = detectors.analyze(log, cc, bound=100_000)
    assert r.requests == 5 and r.epochs == 2 and r.rotations == 1
    assert r.cache_eligible == 3 and r.cache_hits == 3 and r.cache_hit_rate == 1.0
    assert r.over_bound_requests == 1 and r.unlogged_over_bound == []
    assert r.recall_ok == 1 and r.compaction_events == 0
    assert all(r.verdicts().values())


def test_detectors_flag_compaction_unlogged_over_bound_4xx_and_misses():
    log = [
        _req(1, 50_000),
        _req(1, 110_000),  # over the bound but not flagged
        _req(1, 60_000),  # eligible full miss
        _req(1, 40_000, status=400),
        {"event": "upstream_4xx", "status": 400, "path": "/v1/messages"},
    ]
    cc = [{"type": "system", "subtype": "compact_boundary"}, {"type": "user", "isCompactSummary": True}]
    r = detectors.analyze(log, cc, bound=100_000)
    v = r.verdicts()
    assert r.compaction_events == 2 and not v["zero_compaction"]
    assert r.unlogged_over_bound == [2] and not v["over_bound_only_when_logged"]
    assert r.upstream_4xx == 1 and not v["zero_4xx"]
    assert r.cache_misses == [2, 3] and not v["recall_called"]


def test_read_jsonl_skips_a_torn_last_line(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(json.dumps(_req(1, 1)) + "\n" + '{"event": "requ')
    assert len(detectors.read_jsonl(path)) == 1


# Driver


class FakeTransport:
    def __init__(self, cost: int = 1_000):
        self.sent: list[str] = []
        self.cost = cost

    def send(self, text):
        self.sent.append(text)
        return {"type": "result", "result": "OK" if len(self.sent) < 3 else "q_p_01: x", "is_error": False,
                "usage": {"input_tokens": self.cost, "output_tokens": 5}}

    def close(self):
        return 0


def test_driver_sends_every_turn_and_returns_the_probe_answer():
    t = FakeTransport()
    r = driver.Run(t, approved=10**9, reserve=32_000, estimates=[1_000] * 3)
    assert driver.run_session(["a", "b", "c"], r) == "q_p_01: x"
    assert t.sent == ["a", "b", "c"] and r.spent == 3 * 1_005


def test_guard_reserves_full_output_and_stops_before_the_cap():
    t = FakeTransport()
    r = driver.Run(t, approved=37_500, reserve=32_000, estimates=[5_000, 5_000])
    with pytest.raises(driver.BudgetExceeded):
        driver.run_session(["a", "b"], r)
    assert t.sent == ["a"]  # turn 1 projects 37,000; turn 2 projects 1,005 + 5,000 + 32,000 = 38,005 > 37,500


def test_launcher_command_and_scratch_rules(tmp_path):
    cmd = driver.launcher_command("sess1", 100_000)
    assert cmd[:6] == ["contexto", "run", "--bound", "100000", "--session", "sess1"]
    assert cmd[6] == "claude" and "--input-format" in cmd and cmd[-1] == "claude-haiku-5-5"
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError):
        driver.StreamJsonTransport(["true"], tmp_path, {})


def test_child_env_drops_rig_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENRIG_SESSION_NAME", "x")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "y")
    env = driver.child_env(tmp_path)
    assert "OPENRIG_SESSION_NAME" not in env and "CLAUDE_CODE_SESSION_ID" not in env
    assert env["CONTEXTO_HOME"] == str(tmp_path)


# Pass rule


def test_pass_rule_within_two_overall_and_one_per_category():
    base = {"correct": 50, "cells": {"chain": {"correct": 28}, "multihop": {"correct": 8}}}
    assert run.compare({"correct": 48, "cells": {"chain": {"correct": 27}, "multihop": {"correct": 7}}}, base)["pass"]
    assert not run.compare({"correct": 47, "cells": {"chain": {"correct": 27}, "multihop": {"correct": 7}}}, base)["pass"]
    assert not run.compare({"correct": 48, "cells": {"chain": {"correct": 26}, "multihop": {"correct": 8}}}, base)["pass"]


def test_live_refuses_without_enough_approved_budget(tmp_path, capsys):
    code = run.main(["live", "--approved", "1000", "--scratch", str(tmp_path / "s"), "--home", str(tmp_path / "h"),
                     "--session", "x"])
    assert code == 2 and "refusing" in capsys.readouterr().err


def test_dry_run_prints_the_budget(capsys):
    assert run.main(["dry-run"]) == 0
    out = capsys.readouterr().out
    assert "H1 acceptance run through the proxy" in out and "Full-context baseline" in out


def test_auto_turn_count_reaches_the_acceptance_history_at_150k():
    p = budget.Params()
    assert p.bound == 150_000
    s = run.build_script(script.DEFAULT_SEED, 0, 70_000, p)
    b = budget.simulate(list(s.turn_bytes[:-1]), s.turn_bytes[-1], p)
    assert b.history_tokens >= 375_000 and b.planned_over_bound == 0 and b.bound_too_small == 0
    shorter = script.build(script.DEFAULT_SEED, len(s.turns) - 2, 70_000)
    assert sum(budget.turn_tokens(x) + p.reply_tokens for x in shorter.turn_bytes[:-1]) < 375_000
    assert b.h1_worst + b.baseline_replay_worst < 4_000_000
