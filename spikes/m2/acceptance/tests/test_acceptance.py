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
    joined = "\n".join(s.content_turns)
    for p in s.probes:
        for st in p.statements:
            assert joined.count(st) >= 1
        if p.kind == "value" and p.type != "obligations":
            assert not any(p.answer in text for text in s.probe_texts)


def test_turns_reach_the_requested_size(s):
    for size in s.content_bytes:
        assert abs(size - 30_000) <= 0.03 * 30_000


# Budget


def test_rotation_always_retains_the_newest_complete_turn(s):
    b = budget.simulate(list(s.content_bytes), list(s.probe_bytes), budget.Params())
    for row in b.rows[1:]:
        if row.kind == "turn" and row.note == "rotation":  # rule 4's bound rotation; A12's falls back to minimal
            assert row.retained_turns >= 1
    assert b.rotations >= 1 and b.bound_too_small == 0
    assert b.planned_over_bound == 0
    turn_epoch = {r.turn: r.epoch for r in b.rows if r.kind == "probe"}
    round_rows = [r for r in b.rows if r.kind == "round"]
    assert round_rows and all(r.epoch == turn_epoch[r.turn] for r in round_rows)  # tool loop never rotates


def test_budget_lines_and_worst_case(s):
    p = budget.Params(bound=100_000)
    b = budget.simulate(list(s.content_bytes), list(s.probe_bytes), p)
    assert b.h1_worst > b.h1_expected > 0
    assert b.baseline_cli > b.baseline_replay > b.history_tokens
    assert b.h1_worst == sum(r.input_tokens + p.max_tokens_reserve for r in b.rows)  # the worst pass's rows


def test_bound_too_small_when_the_window_budget_cannot_hold_a_turn():
    # 44 KB turns at a 54.5k head and a 100k bound: W = 20,500 cannot hold a 22k-token turn (rule 4).
    # As the engine does (contract PR #50), the turn still rotates to the minimal window (the new turn
    # alone), which is smaller than sending unrotated, and logs bound_too_small as well.
    big = script.build(turns=12, turn_bytes=44_000)
    b = budget.simulate(list(big.content_bytes), list(big.probe_bytes), budget.Params(bound=100_000, head=54_500))
    assert b.bound_too_small > 0 and b.window_minimal > 0
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
    assert r.cache_misses == [2, 3] and "recall_called" not in v
    assert r.informational() == {"recall_called": False, "recall_calls": 0, "search_calls": 0}


def test_a_search_only_run_passes_every_verdict_and_reports_recall_as_information():
    # Live run #2 (c8f0b08): 82 contexto_search calls, no contexto_recall, acceptance 3 credited 4/4
    # from search hits. The contract's acceptance 3 counts either tool (intake 07:57Z).
    log = [_req(0, 50_000), {"event": "mcp_call", "tool": "contexto_search", "ok": True,
                             "returned": [{"page_id": "pg_x", "start": 0, "end": 10}]}]
    r = detectors.analyze(log, [], bound=150_000)
    assert all(r.verdicts().values()) and "recall_called" not in r.verdicts()
    assert r.informational() == {"recall_called": False, "recall_calls": 0, "search_calls": 1}


def _check(tmp_path, log, *extra):
    sl, cc = tmp_path / "log.jsonl", tmp_path / "cc.jsonl"
    sl.write_text("".join(json.dumps(e) + "\n" for e in log), encoding="utf-8")
    cc.write_text("", encoding="utf-8")
    return run.main(["check", "--session-log", str(sl), "--cc-jsonl", str(cc), "--bound", "150000", *extra])


def test_check_fails_closed_when_acceptance_3_is_not_evaluated(tmp_path, capsys):
    # Reviewer-2 on PR #124: with recall_called informational, a clean log without --session-dir and
    # --answers must not pass; acceptance 3 is reported as not evaluated.
    log = [_req(0, 50_000)]
    assert _check(tmp_path, log) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdicts"]["recall_credited"] is False and out["recall_credit"]["reason"].startswith("not evaluated")
    assert out["verdicts"]["paged_only_plants_paged"] is False and out["paged_only_plants_paged"].startswith("not evaluated")
    session = tmp_path / "session"
    (session / "pages").mkdir(parents=True)
    assert _check(tmp_path, log, "--session-dir", str(session)) == 1  # pages but no answers: still not evaluated
    out = json.loads(capsys.readouterr().out)
    assert out["verdicts"]["recall_credited"] is False and out["recall_credit"]["reason"].startswith("not evaluated")


def test_check_fails_a_run_with_no_retrieval_at_all(tmp_path, capsys):
    # The first H1 rerun's shape: 0 recalls and 0 searches. Every acceptance 1 verdict passes, and
    # check still exits 1, on recall_credited.
    log = [_req(0, 50_000), _req(0, 60_000)]
    session = tmp_path / "session"
    (session / "pages").mkdir(parents=True)
    answers = tmp_path / "answers.txt"
    answers.write_text("q_p_01: unknown\n", encoding="utf-8")
    assert _check(tmp_path, log, "--session-dir", str(session), "--answers", str(answers)) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdicts"]["recall_credited"] is False and out["recall_credit"]["credited"] == 0
    assert out["informational"] == {"recall_called": False, "recall_calls": 0, "search_calls": 0}
    assert all(v for k, v in out["verdicts"].items() if k not in ("recall_credited", "paged_only_plants_paged"))


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
    assert code == 2 and "combined worst case" in capsys.readouterr().err


def test_dry_run_prints_the_budget(capsys):
    assert run.main(["dry-run"]) == 0
    out = capsys.readouterr().out
    assert "H1 acceptance run through the proxy" in out and "Full-context baseline" in out


def test_auto_turn_count_reaches_the_acceptance_history_at_150k():
    p = budget.Params()
    assert p.bound == 150_000
    s = run.build_script(script.DEFAULT_SEED, 0, 40_000, p)
    b = budget.simulate(list(s.content_bytes), list(s.probe_bytes), p)
    assert b.history_tokens >= 375_000 and b.planned_over_bound == 0 and b.bound_too_small == 0
    shorter = script.build(script.DEFAULT_SEED, s.n_content - 1, 40_000)
    assert sum(budget.turn_tokens(x) + p.reply_tokens for x in shorter.content_bytes) < 375_000
    # D27 (6x9 plus a six-request baseline) is above the earlier 4,000,000 budget: a new approval, not a test.


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
    # The stub dry runs: at 70 KB turns W (about 60,000) cannot hold two turns of about 39,000 tokens,
    # so the engine keeps only the new turn (window_minimal, contract PR #50) and nothing goes over the
    # bound; the simulation now does the same (dry runs 1 and 2: window_minimal 4, over bound 0).
    p = budget.Params()
    s = run.build_script(script.DEFAULT_SEED, 0, 70_000, p)
    b = budget.simulate(list(s.content_bytes), list(s.probe_bytes), p)
    assert b.window_minimal > 0 and b.bound_too_small == 0 and b.planned_over_bound == 0


def test_stub_dry_run_models_log_text_and_the_rest_separately():
    from acceptance import stubrun

    log = "x" * 18_000
    body = {"system": "s", "messages": [{"role": "user", "content": [{"type": "text", "text": log}]},
                                         {"role": "user", "content": "short"}]}
    m = stubrun.model_tokens(body)
    assert m["log_bytes"] == 18_000 and m["tokens"] == 10_000 + -(-m["other_bytes"] // 3)
    assert body["messages"][0]["content"][0]["text"] == log  # the caller's body is not changed


def test_stub_budget_prices_six_probe_turns_and_six_baseline_requests():
    # D27: the stub budget block follows budget.simulate, one baseline request per probe turn and each
    # probe turn's recalls priced, not one of each (dry run #3 found it 6x low on the baseline).
    from acceptance import stubrun

    p = budget.Params()
    head = {"path": "/v1/messages", "system_bytes": 9_000, "tools_bytes": 60_000, "tools": 26, "tokens": 40_000,
            "max_tokens": 128_000}
    inputs = [40_000, 60_000, 80_000] + [50_000 + 500 * k for k in range(script.PROBE_TURNS)]
    log = [{"event": "request", "path": "/v1/messages", "epoch": 0,
            "usage": {"input_tokens": x, "output_tokens": 2}} for x in inputs]
    probes = [900] * script.PROBE_TURNS
    out = stubrun.summarize([head], log, 3, p, probes, [40_000] * 3)["budget"]
    assert len(out["baseline_requests"]) == script.PROBE_TURNS
    assert out["baseline_replay_worst"] == sum(out["baseline_requests"]) + script.PROBE_TURNS * p.max_tokens_reserve
    per_recall = budget.recall_tokens(p)
    priced = inputs[:3] + [x + k * per_recall for x in inputs[3:] for k in range(p.tool_rounds + 1)]
    assert out["h1_worst"] == sum(priced) + len(priced) * p.max_tokens_reserve
    assert out["total_worst"] == out["h1_worst"] + out["baseline_replay_worst"]


def test_probe_turn_is_tool_neutral_and_the_recall_budget_is_report_only():
    s4 = script.build(turns=4, turn_bytes=8_000)
    for probe in s4.probe_texts:
        assert "contexto" not in probe and "recall" not in probe  # intake: product-realistic wording
        assert "If the conversation does not contain it, say unknown." in probe
    assert detectors.MAX_RECALLS == script.MAX_RECALLS == 2  # a report-only recall cap; the budget prices rounds
    assert detectors.PROBE_TURNS == script.PROBE_TURNS  # D27: the budget prices 2 recalls per probe turn
    calls = [{"event": "mcp_call", "tool": "contexto_recall", "ok": True}] * (2 * script.PROBE_TURNS + 1)
    r = detectors.analyze(calls, [], bound=150_000)
    assert r.recalls_over_budget == 1 and "recalls_within_cap" not in r.verdicts()
    assert detectors.analyze(calls[:-1], [], bound=150_000).recalls_over_budget == 0


def test_the_doc_carries_the_budget_live_gates_on():
    import re

    doc = (Path(__file__).resolve().parents[4] / "docs" / "research" / "M2-acceptance-harness.md").read_text()
    block = re.search(r"<!-- approval-table:begin -->\n(.*?)\n<!-- approval-table:end -->", doc, re.S)
    _, b = run.plan()
    assert block and block.group(1) == budget.approval_table(b)
    assert f"{b.h1_worst + b.baseline_replay_worst:,}" in block.group(1)  # the figure run.py live refuses to go below


def test_live_refuses_a_missing_seat_settings_file_or_a_git_scratch(tmp_path, capsys):
    _, b = run.plan()
    common = ["live", "--approved", str(b.h1_worst + b.baseline_replay_worst), "--home", str(tmp_path / "h"),
              "--session", "x",
              "--upstream", "http://127.0.0.1:9"]
    code = run.main([*common, "--scratch", str(tmp_path / "s"), "--seat-settings", str(tmp_path / "missing.json")])
    assert code == 2 and "refusing: the seat settings file" in capsys.readouterr().err
    seat = tmp_path / "seat.json"
    seat.write_text("{}")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    code = run.main([*common, "--scratch", str(repo / "s"), "--seat-settings", str(seat)])
    assert code == 2 and "inside a git work tree" in capsys.readouterr().err


def test_tool_rounds_are_priced_per_round_and_the_probe_turn_fits():
    _, b = run.plan()
    assert b.recall_tokens == budget.recall_tokens(budget.Params()) == budget.Params().round_tokens == 8_200
    assert budget.b9_largest_chunk_tokens() == 17_778  # 32,000 chars at 1.8: one recall's largest chunk (info)
    assert budget.b9_recall_tokens(84_303) == math.ceil(int(84_303 / 4 * 1.5) / 1.8)
    probe_turns = [r for r in b.rows if r.kind in ("probe", "recall")]
    assert sum(r.kind == "probe" for r in probe_turns) == script.PROBE_TURNS  # D27
    assert max(r.input_tokens for r in probe_turns) < budget.Params().bound


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


# D27: six probe turns of nine (intake, 2026-10-16 05:37Z)

D27_ASSIGNMENT = [
    ["q_a_001", "q_a_020", "q_a_028", "q_p_03", "q_a_016", "q_a_038", "q_a_046", "q_a_002", "q_a_006"],
    ["q_a_050", "q_a_041", "q_a_012", "q_a_035", "q_a_021", "q_a_049", "q_a_004", "q_a_013", "q_p_02"],
    ["q_a_022", "q_a_044", "q_a_026", "q_a_031", "q_a_036", "q_a_009", "q_a_047", "q_a_045", "q_a_024"],
    ["q_a_017", "q_a_018", "q_a_010", "q_a_042", "q_a_019", "q_a_027", "q_p_01", "q_a_033", "q_a_007"],
    ["q_p_04", "q_a_014", "q_a_037", "q_a_011", "q_a_030", "q_a_034", "q_a_003", "q_a_008", "q_a_025"],
    ["q_a_015", "q_a_048", "q_a_023", "q_a_040", "q_a_039", "q_a_029", "q_a_032", "q_a_043", "q_a_005"],
]


def test_d27_assignment_is_the_one_fixed_before_any_result():
    s, _ = run.plan()
    assert [list(t) for t in s.probe_turn_ids] == D27_ASSIGNMENT  # recorded 05:39Z, never tuned
    assert [list(t) for t in run.plan()[0].probe_turn_ids] == D27_ASSIGNMENT  # deterministic


def test_every_probe_is_asked_exactly_once_over_six_turns_of_nine():
    s, _ = run.plan()
    ids = [i for t in s.probe_turn_ids for i in t]
    assert len(s.probe_turn_ids) == 6 and all(len(t) == 9 for t in s.probe_turn_ids)
    assert sorted(ids) == sorted(p.id for p in s.probes) and len(set(ids)) == 54
    for ids_k, text in zip(s.probe_turn_ids, s.probe_texts):
        asked = [line.split(":", 1)[0] for line in text.splitlines()
                 if line.startswith("q_") and not line.startswith("q_id")]
        assert asked == list(ids_k)


def test_the_scorer_maps_answers_from_all_six_turns_back_to_probe_ids():
    s, _ = run.plan()
    by_id = {p.id: p for p in s.probes}
    turn_answers = ["\n".join(f"{i}: {by_id[i].answer}" for i in ids) for ids in s.probe_turn_ids]
    collected = "\n".join(turn_answers)  # how live writes the answers file
    assert run.score(list(s.probes), collected)["correct"] == 54


def test_the_budget_gate_uses_the_six_turn_plan():
    s, b = run.plan()
    assert sum(r.kind == "probe" for r in b.rows) == 6
    assert len(b.probe_rooms) == 6 and all(room >= 0 for room in b.probe_rooms)
    single = budget.simulate(list(s.content_bytes), sum(s.probe_bytes), budget.Params())
    assert b.h1_worst != single.h1_worst  # the gate is the six-turn figure, not a one-turn approximation



def test_live_gates_on_the_combined_h1_and_baseline_worst_case(tmp_path, capsys):
    # D27 (intake 05:42Z): the approval covers H1 and the six-request baseline.
    _, b = run.plan()
    combined = b.h1_worst + b.baseline_replay_worst
    common = ["--home", str(tmp_path / "h"), "--session", "x", "--upstream", "http://127.0.0.1:9",
              "--scratch", str(tmp_path / "s"), "--seat-settings", str(tmp_path / "missing.json")]
    assert run.main(["live", "--approved", str(combined - 1), *common]) == 2
    assert "below the combined worst case" in capsys.readouterr().err
    assert run.main(["live", "--approved", str(combined), *common]) == 2  # passes the gate, stops at the seat file
    assert "seat settings file" in capsys.readouterr().err


def test_a12_rotates_when_the_new_turns_room_would_fall_under_min_turn_room():
    # Contract PR #115 (A12, 1ce122c): rotate also when bound - estimate - headroom floor < min_turn_room,
    # to 0.5 * (W - min_turn_room), falling back to the minimal window, and only if that restores the room.
    s, _ = run.plan()
    p = budget.Params()
    with_a12 = budget.simulate(list(s.content_bytes), list(s.probe_bytes), p)
    without = budget.simulate(list(s.content_bytes), list(s.probe_bytes), budget.Params(min_turn_room=0))
    assert with_a12.room_rotations > without.room_rotations and with_a12.rotations > without.rotations
    for r in with_a12.rows:
        if r.note == "rotation (A12 room)":  # the room it publishes is at least min_turn_room
            assert p.bound - r.input_tokens - budget.HEADROOM_FLOOR >= p.min_turn_room
    assert with_a12.planned_over_bound == 0 and with_a12.bound_too_small == 0 and with_a12.turn_room_low_epochs == []


def test_a12_logs_turn_room_low_once_per_epoch_instead_of_thrashing():
    # A small bound where paging cannot restore the room: no room rotation, turn_room_low once per epoch.
    b = budget.simulate([4_000] * 12, [1_200] * 2, budget.Params(bound=60_000, head=20_000, min_turn_room=12_000))
    lows = [r for r in b.rows if "turn_room_low" in r.note]
    assert b.turn_room_low_epochs and len(lows) == len(b.turn_room_low_epochs)
    assert len(set(b.turn_room_low_epochs)) == len(b.turn_room_low_epochs)  # once per epoch
    assert b.room_rotations < len([r for r in b.rows if r.kind in ("turn", "probe")]) // 2  # no thrash


def test_min_turn_room_is_the_engines_own_default():
    # Guard (reviewer-2, PR #117): the simulation reads min_turn_room from the engine's Config, with no
    # fallback, so it cannot drift from the engine on main.
    from contexto.rewrite import Config

    assert hasattr(Config(), "min_turn_room")
    assert budget.Params().min_turn_room == Config().min_turn_room


def test_turn_room_low_fails_acceptance_1():
    # Decided a failure event (contract PR #115 at 1eb8a4c).
    assert "turn_room_low" in detectors.FAILURE_EVENTS
    r = detectors.analyze([_req(1, 90_000, events=[{"event": "turn_room_low", "epoch": 1}])], [], bound=150_000)
    assert r.failure_events == {"turn_room_low": 1} and not r.verdicts()["no_failure_events"]


def test_retained_stale_fails_acceptance_1():
    # A failed snapshot write logs retained_stale, an acceptance 1 failure (contract PR #125 at 01a58a2),
    # whether it arrives as a request's transform event or as its own session-log line.
    assert "retained_stale" in detectors.FAILURE_EVENTS
    r = detectors.analyze([_req(1, 90_000, events=[{"event": "retained_stale", "epoch": 1}])], [], bound=150_000)
    assert r.failure_events == {"retained_stale": 1} and not r.verdicts()["no_failure_events"]
    r = detectors.analyze([_req(1, 90_000), {"event": "retained_stale", "epoch": 1}], [], bound=150_000)
    assert r.failure_events == {"retained_stale": 1} and not r.verdicts()["no_failure_events"]


def test_h1_spend_takes_the_proxy_logged_usage_after_a_mid_turn_stop():
    # Reviewer-2 on PR #112: the driver's spend grows only per finished turn; a stop inside the probe
    # turn's tool loop drops that turn's requests. The proxy-logged usage is what was billed.
    log = [_req(0, 100_000)] + [_req(1, 105_000) for _ in range(4)]  # 5 requests, 10 output tokens each
    logged = 100_000 + 4 * 105_000 + 5 * 10
    spend = run.h1_spend(100_010, log, abnormal=True, reserve=32_000)  # results saw only the first turn
    assert spend["spent_logged"] == logged and spend["spent_results"] == 100_010
    assert spend["in_flight_allowance"] == 105_000 + 32_000  # one more request of the last size may be unlogged
    assert spend["spent"] == logged + 137_000
    clean = run.h1_spend(logged, log, abnormal=False, reserve=32_000)
    assert clean["spent"] == logged and clean["in_flight_allowance"] == 0
    assert run.h1_spend(500, [], abnormal=False, reserve=32_000)["spent"] == 500  # no log: the driver's figure


def test_a_trailing_slash_model_request_counts_toward_spend_exactly_once(tmp_path):
    # Reviewer-2 on PR #66: the spend paths use the same path helper as the 4xx and auth checks.
    slash = {"event": "request", "method": "POST", "path": "/v1/messages/", "status": 200,
             "usage": {"input_tokens": 1_000, "output_tokens": 10}}
    count = {"event": "request", "method": "POST", "path": "/v1/messages/count_tokens/", "status": 200,
             "usage": {"input_tokens": 999}}  # token counts bill nothing
    responses = {"event": "request", "method": "POST", "path": "/responses/", "status": 200,
                 "usage": {"input_tokens": 500, "output_tokens": 5}}
    spend = run.h1_spend(0, [slash, count, responses], abnormal=False, reserve=0)
    assert spend["spent_logged"] == 1_010 + 505
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in (slash, count, responses)) + "\n")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    watcher = driver.SessionWatcher(log, proc, approved=10**9, reserve=0)
    try:
        time.sleep(0.6)
        assert watcher.spent == 1_010 + 505  # each counted once; count_tokens not at all
    finally:
        watcher.stop()
        proc.kill()
        proc.wait(timeout=10)
    assert detectors.BILLED_PATHS <= detectors.MODEL_PATHS and not any("count_tokens" in p for p in detectors.BILLED_PATHS)


def test_a12_both_triggers_use_the_room_target_when_it_restores_the_room():
    # Engine order (rewrite.rotate_at_turn_start) and contract PR #120 (9eb191b): when the size trigger
    # and the room condition both fire, the room-sized target is used if it restores the room.
    # One round of 17,778 at probe turn 1 is what the old room-limited recall pricing gave here, and it
    # sets up the both-trigger case at turn 16.
    p = budget.Params(bound=80_000, head=20_000, min_turn_room=12_000, tool_rounds=1, round_tokens=17_778)
    b = budget.simulate([24_000] * 14, [1_200] * 2, p)
    both = [r for r in b.rows if r.note.startswith("rotation (bound + A12 room)")]
    assert both
    for r in both:
        assert p.bound - r.input_tokens - budget.HEADROOM_FLOOR >= p.min_turn_room


def test_a12_logs_turn_room_low_after_a_rotation_that_leaves_the_room_low():
    # Contract PR #120 (71b6cf8): turn_room_low only when the room is still low after the decision,
    # whatever the decision was (here, a rotation), once per epoch.
    p = budget.Params(bound=60_000, head=20_000, min_turn_room=12_000)
    b = budget.simulate([6_000] * 14, [1_200] * 2, p)
    after = [r for r in b.rows if r.note.startswith("rotation") and "turn_room_low" in r.note]
    assert after
    for r in after:
        assert p.bound - r.input_tokens - budget.HEADROOM_FLOOR < p.min_turn_room  # still low after it


def test_head_follows_contexto_tool_bytes():
    # The head is derived (intake 2026-10-16 09:42Z): the measured 47,600 plus Contexto's own tool
    # definitions' byte change at 3.0 bytes per token. If the definitions change, update
    # budget.CONTEXTO_TOOL_BYTES (and the head with it) before any live approval.
    from contexto.mcp import TOOLS

    as_sent = [{"name": "mcp__contexto__" + t["name"], "description": t["description"], "input_schema": t["inputSchema"]}
               for t in TOOLS]
    measured = sum(len(json.dumps(t, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) for t in as_sent)
    assert measured == budget.CONTEXTO_TOOL_BYTES, f"Contexto's tool definitions are {measured} bytes; update budget.HEAD"
    assert budget.HEAD == budget.Params().head == 47_628
    assert budget.HEAD == budget.HEAD_MEASURED + math.ceil(
        (budget.CONTEXTO_TOOL_BYTES - budget.CONTEXTO_TOOL_BYTES_AT_MEASURE) / budget.OTHER_BYTES_PER_TOKEN)


def test_tool_rounds_are_calibrated_on_live_run_3():
    # Intake 10:06Z: price the worst case at run #3's most rounds and largest per-round growth, the
    # expected case at its mean rounds and rounds-weighted mean growth (each rounded up to 100).
    rounds, growth = budget.TOOL_ROUNDS_RUN3, budget.ROUND_GROWTH_RUN3
    p = budget.Params()
    assert sum(rounds) == 20 and p.tool_rounds == max(rounds) == 5
    assert p.tool_rounds_expected == sum(rounds) / len(rounds)
    assert p.round_tokens == math.ceil(max(growth) / 100) * 100 == 8_200
    weighted = sum(r * g for r, g in zip(rounds, growth)) / sum(rounds)
    assert p.round_tokens_expected == math.ceil(weighted / 100) * 100 == 2_100
    s, b = run.plan()
    assert budget.expected_rounds(p, len(s.probe_texts)) == [4, 4, 3, 3, 3, 3] and b.expected_tool_rounds == 20
    assert abs(b.h1_expected - budget.H1_SPENT_RUN3) / budget.H1_SPENT_RUN3 < 0.05  # tracks run #3's real H1
    assert b.h1_worst > budget.H1_SPENT_RUN3 and b.planned_over_bound == 0
    assert sum(r.kind == "round" for r in b.rows) == p.tool_rounds * len(s.probe_texts)  # worst: every turn
