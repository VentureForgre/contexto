"""Tests for the M2 acceptance harness: script, budget, detectors, driver guard, pass rule.

Fixtures are synthetic: no content from a real session is committed.
"""

from __future__ import annotations

import io
import json
import os
import math
import shutil
import subprocess
import sys
import time
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
    b = budget.simulate(list(s.turn_bytes[:-1]), s.turn_bytes[-1], budget.Params())
    for row in b.rows[1:]:
        if row.kind == "turn":
            assert row.retained_turns >= 1
    assert b.rotations >= 1 and b.bound_too_small == 0
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
    b = budget.simulate(list(big.turn_bytes[:-1]), big.turn_bytes[-1], budget.Params(bound=100_000, head=54_500))
    assert b.bound_too_small > 0 and b.planned_over_bound > 0 and b.rotations == 0
    assert b.window_budget_first == 100_000 - 25_000 - 54_500


def test_conservative_estimator_for_log_text():
    assert budget.turn_tokens(18_000) == 10_001  # 1.8 bytes per token, measured for numeric logs


# Detectors


def _req(epoch, inp, cache_read=0, over=False, status=200, events=()):
    usage = {"input_tokens": inp - cache_read, "cache_creation_input_tokens": 0, "cache_read_input_tokens": cache_read,
             "output_tokens": 10}
    pre_send = [{"event": "over_bound", "source": "estimate", "in_tool_loop": True, "epoch": epoch, "estimate": inp,
                 "bound": 0}] if over else []
    return {"event": "request", "path": "/v1/messages", "status": status, "usage": usage, "over_bound": over,
            "epoch": epoch, "transform_events": [*events, *pre_send]}


def test_detectors_on_a_clean_synthetic_session():
    log = [
        {"event": "proxy_start", "port": 1},
        _req(1, 60_000),
        _req(1, 70_000, cache_read=60_000),
        {"event": "request", "path": "/v1/messages/count_tokens", "status": 200, "usage": None},
        _req(2, 80_000, events=[{"event": "rotation", "epoch": 2, "k": 3}, {"event": "rotation_noop", "epoch": 2}]),
        _req(2, 85_000, cache_read=78_000),
        _req(2, 120_000, cache_read=80_000, over=True),
        {"event": "mcp_call", "tool": "contexto_recall", "ok": True, "page_id": "pg_x"},
    ]
    cc = [{"type": "user"}, {"type": "assistant"}, {"type": "system", "subtype": "api_error"}]
    r = detectors.analyze(log, cc, bound=100_000)
    assert r.requests == 5 and r.epochs == 2 and r.rotations == 1  # rotation_noop is not counted
    assert r.requests_per_epoch == {"1": 2, "2": 3}
    assert r.cache_eligible == 3 and r.cache_hits == 3 and r.cache_hit_rate == 1.0
    assert r.over_bound_requests == 1 and r.pre_send_tool_loop == [5] and r.over_bound_indexes == [5]
    assert r.recall_ok == 1 and r.compaction_events == 0
    v = r.verdicts()
    assert not v["no_request_over_bound"]  # strict rule: even a flagged tool loop fails acceptance 1
    assert all(ok for name, ok in v.items() if name != "no_request_over_bound")


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
    assert r.over_bound_indexes == [2] and r.pre_send_tool_loop == [] and not v["no_request_over_bound"]
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
    cmd = driver.launcher_command("sess1", 150_000, "http://127.0.0.1:9")
    assert cmd[:8] == ["contexto", "run", "--bound", "150000", "--session", "sess1", "--upstream", "http://127.0.0.1:9"]
    assert cmd[8] == "claude" and "--input-format" in cmd and cmd[-1] == "claude-haiku-5-5"
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError):
        driver.StreamJsonTransport(["true"], tmp_path, {})


def test_scratch_nested_inside_a_git_work_tree_is_refused():
    nested = Path(__file__).resolve().parent  # this repository's checkout
    assert driver.inside_git(nested)
    with pytest.raises(ValueError):
        driver.StreamJsonTransport(["true"], nested, {})


def test_isolated_config_names_only_the_helper_shim(tmp_path):
    seat = tmp_path / "seat.json"
    seat.write_text(json.dumps({"apiKeyHelper": "echo SECRET-HELPER-COMMAND", "env": {"X": "1"}}))
    config = driver.isolated_config(tmp_path / "scratch", seat)
    written = (config / "settings.json").read_text()
    assert "keyhelper.py" in written and str(seat) in written
    assert "SECRET-HELPER-COMMAND" not in written and "env" not in json.loads(written)


def test_child_env_drops_credentials_and_sets_the_config_dir(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(var, "x")
    env = driver.child_env(tmp_path / "home", tmp_path / "config")
    assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"} & set(env)
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "config")


def test_auth_watcher_stops_the_process_on_the_first_401(tmp_path):
    log = tmp_path / "log.jsonl"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    watcher = driver.SessionWatcher(log, proc)
    log.write_text(json.dumps({"event": "request", "path": "/v1/messages", "status": 200}) + "\n"
                   + json.dumps({"event": "upstream_4xx", "status": 401, "path": "/v1/messages"}) + "\n")
    assert proc.wait(timeout=10) != 0
    assert watcher.status == 401


def test_auth_watcher_ignores_the_unauthenticated_startup_probe(tmp_path):
    # First live H1 attempt: Claude Code's HEAD /api/hello probe got 401 from the owner's proxy.
    log = tmp_path / "log.jsonl"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    watcher = driver.SessionWatcher(log, proc)
    try:
        log.write_text(json.dumps({"event": "request", "method": "HEAD", "path": "/api/hello", "status": 401,
                                   "usage": None}) + "\n"
                       + json.dumps({"event": "request", "method": "POST", "path": "/v1/messages", "status": 200}) + "\n")
        time.sleep(1.5)
        assert proc.poll() is None and watcher.status is None
        with log.open("a") as fh:  # a credential failure on the Messages API still stops the run
            fh.write(json.dumps({"event": "request", "method": "POST", "path": "/v1/messages", "status": 403}) + "\n")
        assert proc.wait(timeout=10) != 0 and watcher.status == 403
    finally:
        watcher.stop()
        if proc.poll() is None:
            proc.kill()


def test_auth_failure_is_only_a_messages_api_post():
    f = driver.is_auth_failure
    assert f({"event": "request", "method": "POST", "path": "/v1/messages", "status": 401})
    assert f({"event": "upstream_4xx", "path": "/v1/messages/count_tokens", "status": 403})
    assert f({"event": "request", "method": "POST", "path": "/v1/messages?beta=true", "status": 401})
    assert not f({"event": "request", "method": "HEAD", "path": "/api/hello", "status": 401})
    assert not f({"event": "request", "method": "GET", "path": "/v1/models", "status": 401})
    assert not f({"event": "request", "method": "POST", "path": "/v1/messages", "status": 429})
    assert f({"event": "request", "method": "POST", "path": "/responses", "status": 401})  # contract PR #61
    from contexto.proxy import TRANSFORMED_PATHS
    assert driver.AUTH_PATHS == detectors.MODEL_PATHS == TRANSFORMED_PATHS  # one list, the proxy's


def test_stream_compactions_fail_the_zero_compaction_verdict():
    r = detectors.analyze([], [], bound=150_000, stream_compactions=1)
    assert r.verdicts()["zero_compaction"] is False


def test_paged_statements_reads_the_session_pages(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "pg_1.json").write_text(json.dumps({"messages": [{"content": "cfg billing-core.retention_days=amber-1"}]}))
    found = detectors.paged_statements(tmp_path, ["cfg billing-core.retention_days=amber-1", "cfg other=x"])
    assert found == {"cfg billing-core.retention_days=amber-1": True, "cfg other=x": False}


def test_live_refuses_a_plan_below_two_and_a_half_times_the_bound(tmp_path, capsys):
    seat = tmp_path / "seat.json"
    seat.write_text("{}")
    code = run.main(["live", "--approved", "10000000", "--turns", "4", "--scratch", str(tmp_path / "s"),
                     "--home", str(tmp_path / "h"), "--session", "x", "--seat-settings", str(seat)])
    assert code == 2 and "below 2.5x the bound" in capsys.readouterr().err


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
    seat = tmp_path / "seat.json"
    seat.write_text("{}")
    code = run.main(["live", "--approved", "1000", "--scratch", str(tmp_path / "s"), "--home", str(tmp_path / "h"),
                     "--session", "x", "--seat-settings", str(seat)])
    assert code == 2 and "worst-case" in capsys.readouterr().err


def test_dry_run_prints_the_budget(capsys):
    assert run.main(["dry-run"]) == 0
    out = capsys.readouterr().out
    assert "H1 acceptance run through the proxy" in out and "Full-context baseline" in out


def test_auto_turn_count_reaches_the_acceptance_history_at_150k():
    p = budget.Params()
    assert p.bound == 150_000
    s = run.build_script(script.DEFAULT_SEED, 0, 40_000, p)
    b = budget.simulate(list(s.turn_bytes[:-1]), s.turn_bytes[-1], p)
    assert b.history_tokens >= 375_000 and b.planned_over_bound == 0 and b.bound_too_small == 0
    shorter = script.build(script.DEFAULT_SEED, len(s.turns) - 2, 40_000)
    assert sum(budget.turn_tokens(x) + p.reply_tokens for x in shorter.turn_bytes[:-1]) < 375_000
    assert b.h1_worst + b.baseline_replay_worst < 4_000_000


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_credential_check_with_a_fake_helper_uses_the_helper_not_oauth(tmp_path):
    # Zero spend: a fake helper key and a stub upstream that answers 401; the real key is never used.
    from acceptance import credcheck

    seat = tmp_path / "seat.json"
    seat.write_text(json.dumps({"apiKeyHelper": "echo test-key-not-real"}))  # the key's format does not matter
    result = credcheck.check(seat)
    assert result["request_seen"] and result["key_matches_seat_helper"] and not result["oauth_beta_header"]
    assert result["pass"]


def test_credcheck_script_output_survives_a_pipe(tmp_path):
    # Run as a script with stdout piped and no claude on PATH: the JSON result must still arrive.
    seat = tmp_path / "seat.json"
    seat.write_text(json.dumps({"apiKeyHelper": "echo test-key-not-real"}))
    script_path = Path(__file__).resolve().parents[1] / "credcheck.py"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, str(script_path), str(seat)], capture_output=True, text=True,
                          env=env, timeout=60)
    result = json.loads(proc.stdout)
    assert result == {"ran": False, "reason": "claude CLI not installed"}
    assert proc.returncode == 1  # not a pass


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
@pytest.mark.parametrize("content", [None, "{}", '{"apiKeyHelper": "exit 3"}'])
def test_credcheck_reports_a_broken_seat_settings_file_cleanly(tmp_path, content):
    from acceptance import credcheck

    seat = tmp_path / "seat.json"
    if content is not None:
        seat.write_text(content)
    result = credcheck.check(seat)
    assert result["ran"] is False and "pass" not in result and result["reason"]


def test_requests_without_usage_are_unverified_and_fail_acceptance_1():
    log = [_req(1, 50_000), {"event": "request", "path": "/v1/messages", "status": 200, "usage": None, "epoch": 1},
           {"event": "request", "path": "/v1/messages", "status": 200, "epoch": 1}]
    r = detectors.analyze(log, [], bound=150_000)
    assert r.unverified_requests == [2, 3]
    assert r.verdicts()["every_request_has_usage"] is False
    assert detectors.analyze([_req(1, 50_000)], [], bound=150_000).verdicts()["every_request_has_usage"] is True


def test_any_over_bound_fails_acceptance_1_under_the_strict_rule():
    real = {"event": "over_bound", "source": "real", "epoch": 1, "over_bound": True, "real_tokens": 151_000,
            "estimate": 140_000, "bound": 150_000}
    err = {"event": "estimate_error", "epoch": 1, "real_tokens": 151_000, "estimate": 120_000, "ratio": 0.795,
           "mean_ratio": 0.9}
    # The estimate missed: no pre-send record, and A2b logged it from real usage.
    r = detectors.analyze([_req(1, 50_000), _req(1, 151_000), real, err], [], bound=150_000)
    v = r.verdicts()
    assert r.over_bound_indexes == [2] and not v["no_request_over_bound"]
    assert r.over_bound_real == 1 and r.over_bound_real_max_tokens == 151_000 and not v["no_real_over_bound"]
    assert r.estimate_errors == 1 and r.estimate_min_ratio == 0.795 and r.estimate_mean_ratio == 0.9
    # A tool-loop pre-send record (source "estimate", in_tool_loop true) is reported only; the real
    # over-bound it led to still fails.
    loop = detectors.analyze([_req(1, 151_000, over=True), {**real, "estimate": 152_000}], [], bound=150_000)
    lv = loop.verdicts()
    assert loop.pre_send_tool_loop == [1] and lv["no_pre_send_over_bound_outside_tool_loop"]
    assert not lv["no_real_over_bound"] and not lv["no_request_over_bound"]
    # A pre-send record that is not marked as a tool loop fails, even if real usage stayed under.
    untagged = {"event": "over_bound", "source": "estimate", "epoch": 1, "estimate": 151_000, "bound": 150_000}
    u = detectors.analyze([_req(1, 140_000, events=[untagged])], [], bound=150_000)
    assert u.pre_send_other == [1] and not u.verdicts()["no_pre_send_over_bound_outside_tool_loop"]
    # bound_too_small is a configuration failure.
    bts = detectors.analyze([_req(1, 151_000, events=[{"event": "bound_too_small", "epoch": 1}])], [], bound=150_000)
    assert bts.bound_too_small == 1 and not bts.verdicts()["no_bound_too_small"]
    # window_minimal is counted.
    wm = detectors.analyze([_req(1, 120_000, events=[{"event": "window_minimal", "epoch": 1}])], [], bound=150_000)
    assert wm.window_minimal == 1 and wm.verdicts()["no_request_over_bound"]


def test_window_must_hold_the_newest_complete_turn_and_the_new_one():
    # The stub dry run through the launcher: at 70 KB turns the engine logged bound_too_small and went
    # over the bound, because W (about 60,000) cannot hold two turns of about 39,000 tokens.
    p = budget.Params()
    s = run.build_script(script.DEFAULT_SEED, 0, 70_000, p)
    b = budget.simulate(list(s.turn_bytes[:-1]), s.turn_bytes[-1], p)
    assert b.bound_too_small > 0 and b.planned_over_bound > 0 and b.rotations == 0


def test_stub_dry_run_models_log_text_and_the_rest_separately():
    from acceptance import stubrun

    log = "x" * 18_000
    body = {"system": "s", "messages": [{"role": "user", "content": [{"type": "text", "text": log}]},
                                         {"role": "user", "content": "short"}]}
    m = stubrun.model_tokens(body)
    assert m["log_bytes"] == 18_000 and m["tokens"] == 10_000 + -(-m["other_bytes"] // 3)
    assert body["messages"][0]["content"][0]["text"] == log  # the caller's body is not changed


def test_probe_turn_is_tool_neutral_and_the_recall_budget_is_report_only():
    s4 = script.build(turns=4, turn_bytes=8_000)
    probe = s4.turns[-1]
    assert "contexto" not in probe and "recall" not in probe  # intake: product-realistic wording
    assert "If the conversation does not contain it, say unknown." in probe
    assert detectors.MAX_RECALLS == script.MAX_RECALLS == budget.Params().recalls == 2
    calls = [{"event": "mcp_call", "tool": "contexto_recall", "ok": True}] * 3
    r = detectors.analyze(calls, [], bound=150_000)
    assert r.recalls_over_budget == 1 and "recalls_within_cap" not in r.verdicts()


def test_the_doc_carries_the_budget_live_gates_on():
    import re

    doc = (Path(__file__).resolve().parents[4] / "docs" / "research" / "M2-acceptance-harness.md").read_text()
    block = re.search(r"<!-- approval-table:begin -->\n(.*?)\n<!-- approval-table:end -->", doc, re.S)
    _, b = run.plan()
    assert block and block.group(1) == budget.approval_table(b)
    assert f"{b.h1_worst:,}" in block.group(1)  # the figure run.py live refuses to go below


def test_live_refuses_a_missing_seat_settings_file_or_a_git_scratch(tmp_path, capsys):
    _, b = run.plan()
    common = ["live", "--approved", str(b.h1_worst), "--home", str(tmp_path / "h"), "--session", "x",
              "--upstream", "http://127.0.0.1:9"]
    code = run.main([*common, "--scratch", str(tmp_path / "s"), "--seat-settings", str(tmp_path / "missing.json")])
    assert code == 2 and "refusing: the seat settings file" in capsys.readouterr().err
    seat = tmp_path / "seat.json"
    seat.write_text("{}")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    code = run.main([*common, "--scratch", str(repo / "s"), "--seat-settings", str(seat)])
    assert code == 2 and "inside a git work tree" in capsys.readouterr().err


def test_recalls_are_priced_at_the_largest_b9_chunk_and_the_probe_turn_fits():
    _, b = run.plan()
    assert b.recall_tokens == budget.recall_tokens(budget.Params()) == 17_778  # 32,000 chars at 1.8
    assert budget.b9_recall_tokens(84_303) <= b.recall_tokens  # B9 returns less at a smaller W
    assert budget.b9_recall_tokens(84_303) == math.ceil(int(84_303 / 4 * 1.5) / 1.8)
    probe_turn = [r for r in b.rows if r.kind in ("probe", "recall")]
    assert len(probe_turn) == 1 + script.MAX_RECALLS
    assert max(r.input_tokens for r in probe_turn) < budget.Params().bound


def test_non_model_4xx_are_counted_apart_from_acceptance_1():
    probe = {"event": "request", "method": "HEAD", "path": "/api/hello", "status": 401, "usage": None}
    probe_4xx = {"event": "upstream_4xx", "status": 401, "path": "/api/hello"}
    r = detectors.analyze([probe, probe_4xx, _req(0, 50_000)], [], bound=150_000)
    assert r.non_model_4xx == {"HEAD /api/hello": 1} and r.upstream_4xx == 0 and r.verdicts()["zero_4xx"]
    model_4xx = {"event": "upstream_4xx", "status": 400, "path": "/v1/messages/count_tokens"}
    assert not detectors.analyze([model_4xx], [], bound=150_000).verdicts()["zero_4xx"]


def test_local_model_path_4xx_and_encoded_body_rejections_fail_zero_4xx():
    # reviewer-2, PR #62: 4xx the proxy answers itself on a model path have no upstream_4xx event.
    framing = {"event": "request", "method": "POST", "path": "/v1/messages/count_tokens", "status": 400,
               "error": "malformed request framing"}
    r = detectors.analyze([framing], [], bound=150_000)
    assert r.model_request_4xx == 1 and r.non_model_4xx == {} and not r.verdicts()["zero_4xx"]
    rejected = {"event": "encoded_body_rejected", "encoding": "gzip", "endpoint": "/v1/messages", "reason": "x"}
    r = detectors.analyze([rejected], [], bound=150_000)
    assert r.encoded_body_rejected == 1 and not r.verdicts()["zero_4xx"]
    slash = {"event": "request", "method": "POST", "path": "/v1/messages/", "status": 400}
    assert detectors.analyze([slash], [], bound=150_000).model_request_4xx == 1
    assert driver.is_auth_failure({"event": "request", "method": "POST", "path": "/v1/messages/", "status": 401})


def _wait(proc, seconds=10):
    try:
        return proc.wait(timeout=seconds)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_spend_watcher_stops_inside_a_turn_before_real_spend_can_pass_the_approval(tmp_path):
    # PR #68 review: the probe turn's tool loop runs inside one Claude Code turn, so the pre-turn
    # projection cannot stop extra recalls; the watcher stops on real usage as it is logged.
    def req(inp, out=100):
        return json.dumps({"event": "request", "method": "POST", "path": "/v1/messages", "status": 200,
                           "usage": {"input_tokens": 2, "cache_read_input_tokens": inp - 2, "output_tokens": out}})
    log = tmp_path / "log.jsonl"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    watcher = driver.SessionWatcher(log, proc, approved=900_000, reserve=32_000)
    try:
        log.write_text(req(100_000) + "\n" + req(120_000) + "\n")  # 220,200 + 2 x 152,000 = 524,200: fine
        time.sleep(1.0)
        assert proc.poll() is None and watcher.spent == 220_200 and watcher.spend_stop is None
        with log.open("a") as fh:  # 360,300 + 2 x 172,000 = 704,300: fine; then 520,400 + 2 x 192,000 = 904,400
            fh.write(req(140_000) + "\n" + req(160_000) + "\n")
        assert _wait(proc) != 0 and "could pass the approved 900,000" in (watcher.spend_stop or "")
        assert watcher.spent == 520_400 and watcher.status is None
    finally:
        watcher.stop()


def test_spend_watcher_ignores_spend_without_an_approval_and_other_paths(tmp_path):
    log = tmp_path / "log.jsonl"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    watcher = driver.SessionWatcher(log, proc)
    try:
        log.write_text(json.dumps({"event": "request", "path": "/v1/messages", "status": 200,
                                   "usage": {"input_tokens": 10_000_000}}) + "\n")
        time.sleep(0.6)
        assert proc.poll() is None and watcher.spend_stop is None
    finally:
        watcher.stop()
        proc.kill()
        proc.wait(timeout=10)
    log = tmp_path / "log2.jsonl"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    watcher = driver.SessionWatcher(log, proc, approved=100)
    try:
        log.write_text(json.dumps({"event": "request", "path": "/v1/messages/count_tokens", "status": 200,
                                   "usage": {"input_tokens": 10_000}}) + "\n")
        time.sleep(0.6)
        assert proc.poll() is None and watcher.spent == 0
    finally:
        watcher.stop()
        proc.kill()
        proc.wait(timeout=10)


def test_transport_reports_an_in_turn_spend_stop_as_budget_exceeded(tmp_path):
    # A fake 'claude' that waits; the watcher kills it, and send() says why.
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps({"event": "request", "path": "/v1/messages", "status": 200,
                               "usage": {"input_tokens": 90_000, "output_tokens": 100}}) + "\n")
    t = driver.StreamJsonTransport([sys.executable, "-c", "import sys, time; sys.stdin.readline(); time.sleep(30)"],
                                   tmp_path, dict(os.environ), log, approved=100_000, reserve=1_000)
    try:
        with pytest.raises(driver.BudgetExceeded, match="stopped inside the turn"):
            t.send("hello")
    finally:
        t.close()  # reaps the stopped process and its pipes (no ResourceWarning or BrokenPipe noise)


# Real-usage events in both log forms (lane A's A2e, PR #76: fixtures follow its code, read-only).

def _entry(epoch, inp, usage_events=None, over=False, events=()):
    e = _req(epoch, inp, events=events)
    e["over_bound"] = over
    if usage_events is not None:
        e["usage_events"] = usage_events
    return e


REAL = {"event": "over_bound", "source": "real", "over_bound": True, "real_tokens": 151_000, "estimate": 140_000,
        "bound": 150_000}
ERR = {"event": "estimate_error", "real_tokens": 151_000, "estimate": 120_000, "ratio": 0.795, "mean_ratio": 0.9}


def test_usage_events_on_request_entries_are_read():
    log = [_entry(1, 50_000), _entry(1, 151_000, usage_events=[REAL, ERR], over=True)]
    r = detectors.analyze(log, [], bound=150_000)
    assert r.over_bound_real == 1 and r.over_bound_real_max_tokens == 151_000 and not r.verdicts()["no_real_over_bound"]
    assert r.estimate_errors == 1 and r.estimate_min_ratio == 0.795 and r.estimate_mean_ratio == 0.9


def test_separate_usage_event_lines_are_still_read():
    log = [_entry(1, 50_000), _entry(1, 151_000), {**REAL, "epoch": 1}, {**ERR, "epoch": 1}]
    r = detectors.analyze(log, [], bound=150_000)
    assert r.over_bound_real == 1 and r.estimate_errors == 1 and not r.verdicts()["no_real_over_bound"]


def test_a_mixed_log_counts_each_real_over_bound_once():
    log = [
        _entry(1, 151_000, usage_events=[REAL], over=True), {**REAL, "epoch": 1},  # both forms: one event
        _entry(2, 152_000), {**REAL, "epoch": 2, "real_tokens": 152_000},  # old form only
        _entry(3, 153_000, usage_events=[{**REAL, "real_tokens": 153_000}], over=True),  # new form only
    ]
    r = detectors.analyze(log, [], bound=150_000)
    assert r.over_bound_real == 3 and r.over_bound_real_max_tokens == 153_000


def test_the_entry_over_bound_flag_is_not_a_real_over_bound_signal():
    # Reviewer-2 on PR #76: the pre-send tool-loop exemption sets the entry's over_bound flag too, so
    # only {event: over_bound, source: real} in usage_events (or an old separate line) counts.
    pre_send = {"event": "over_bound", "source": "estimate", "in_tool_loop": True, "epoch": 1, "estimate": 151_000}
    loop = _entry(1, 149_000, usage_events=[ERR], over=True, events=[pre_send])
    r = detectors.analyze([loop], [], bound=150_000)
    assert r.over_bound_real == 0 and r.pre_send_tool_loop == [1] and r.verdicts()["no_real_over_bound"]
    flag_only = _entry(1, 149_000, over=True)  # no usage_events: the flag alone counts for nothing
    assert detectors.analyze([flag_only], [], bound=150_000).over_bound_real == 0
    # ...and a real event inside usage_events is never missed (no vacuous pass after #76)
    real = _entry(1, 151_000, usage_events=[ERR, REAL], over=True, events=[pre_send])
    assert not detectors.analyze([real], [], bound=150_000).verdicts()["no_real_over_bound"]


def test_failure_events_fail_acceptance_1_wherever_they_appear():
    # Intake 03:39Z (contract PR at 147b182). Shapes follow lane A's PR #76 (read-only).
    store_error = {"event": "store_error", "epoch": 2, "operation": "set_window", "error": "OSError"}
    degraded = {"event": "degraded_enter", "epoch": 2, "reason": "store_error"}
    log = [
        _entry(1, 90_000, events=[{"event": "unclassified_system_message", "epoch": 1}]),  # transform event
        _entry(2, 95_000, usage_events=[store_error, degraded]),  # on the request entry
        {**store_error, "ts": "x"}, degraded,  # the same two as separate lines too: counted once
        {"event": "fallback_failed", "epoch": 2},  # a separate store.log line only
        _entry(3, 96_000, events=[{"event": "page_conflict", "epoch": 3, "page_id": "pg_1"}]),
    ]
    r = detectors.analyze(log, [], bound=150_000)
    assert r.failure_events == {"unclassified_system_message": 1, "store_error": 1, "degraded_enter": 1,
                                "fallback_failed": 1, "page_conflict": 1}
    assert not r.verdicts()["no_failure_events"]
    for name in detectors.FAILURE_EVENTS:  # each one alone fails
        assert not detectors.analyze([{"event": name, "epoch": 0}], [], bound=150_000).verdicts()["no_failure_events"]
    assert detectors.analyze([_entry(1, 90_000)], [], bound=150_000).verdicts()["no_failure_events"]


def test_a_real_over_bound_without_a_pre_send_estimate_is_still_read():
    no_estimate = {**REAL, "estimate": None}  # PR #76: estimate is null after a fallback_failed forward
    r = detectors.analyze([_entry(1, 151_000, usage_events=[no_estimate], over=True)], [], bound=150_000)
    assert r.over_bound_real == 1 and r.over_bound_real_max_tokens == 151_000


# Local versus upstream 5xx on model requests (contract PR #98).

def test_a_local_upstream_unreachable_502_on_a_model_request_fails_acceptance_1():
    local = {"event": "request", "method": "POST", "path": "/v1/messages", "status": 502,
             "error": "upstream unreachable: TimeoutError", "duration_ms": 18187}
    r = detectors.analyze([_req(1, 50_000), local], [], bound=150_000)
    assert [x["mark"] for x in r.local_5xx] == ["upstream unreachable"] and not r.verdicts()["no_local_5xx"]


def test_the_same_502_on_a_non_model_path_does_not_fail():
    probe = {"event": "request", "method": "HEAD", "path": "/api/hello", "status": 502,
             "error": "upstream unreachable: TimeoutError"}
    r = detectors.analyze([probe], [], bound=150_000)
    assert r.local_5xx == [] and r.verdicts()["no_local_5xx"]


def test_an_upstream_5xx_passed_through_is_reported_and_does_not_fail():
    for status in (500, 529):
        passed = {"event": "request", "method": "POST", "path": "/v1/messages", "status": status,
                  "response_bytes": 120, "usage": None}
        r = detectors.analyze([passed], [], bound=150_000)
        assert r.local_5xx == [] and r.upstream_5xx[0]["status"] == status and r.verdicts()["no_local_5xx"]


def test_an_unmarked_model_path_5xx_fails_closed():
    unmarked = {"event": "request", "method": "POST", "path": "/v1/messages/count_tokens", "status": 503}
    r = detectors.analyze([unmarked], [], bound=150_000)
    assert r.local_5xx[0]["mark"] == "unmarked" and not r.verdicts()["no_local_5xx"]


def test_last_resort_and_websocket_local_errors_fail():
    last = {"event": "proxy_error", "method": "POST", "path": "/v1/messages", "error": "KeyError", "fallback": "local_502"}
    forwarded = {**last, "fallback": "forward_original"}  # forwarded the CLI's bytes: not a local error
    ws = {"event": "websocket_rejected", "path": "/responses"}
    r = detectors.analyze([last, forwarded, ws], [], bound=150_000)
    assert [(x["status"], x["mark"]) for x in r.local_5xx] == [(502, "proxy_error local_502"), (501, "websocket_rejected")]


def test_a_failed_forward_original_fallback_counts_as_one_local_502():
    # Reviewer-2 on PR #101: _handle's last resort first tries to forward the CLI's own bytes
    # (fallback forward_original); if that also fails it logs proxy_error local_502 and answers 502.
    first = {"event": "proxy_error", "method": "POST", "path": "/v1/messages", "error": "KeyError",
             "fallback": "forward_original"}
    again = {"event": "proxy_error", "path": "/v1/messages", "error": "OSError", "fallback": "local_502"}
    r = detectors.analyze([first, again], [], bound=150_000)
    assert [(x["line"], x["status"], x["mark"]) for x in r.local_5xx] == [(2, 502, "proxy_error local_502")]
    assert not r.verdicts()["no_local_5xx"]
    # the fallback that forwards and is answered by upstream is no local error
    forwarded = {"event": "request", "method": "POST", "path": "/v1/messages", "fallback": True, "status": 200,
                 "response_bytes": 900, "usage": {"input_tokens": 10, "output_tokens": 1}}
    ok = detectors.analyze([first, forwarded], [], bound=150_000)
    assert ok.local_5xx == [] and ok.verdicts()["no_local_5xx"]
