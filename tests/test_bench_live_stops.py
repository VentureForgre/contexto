"""How a live benchmark run ends when something goes wrong, and how it resumes. Zero spend: a stub
upstream and a stub ``claude``.

The script is ``script.build(3, "v2", bound=4_000, turn_bytes=2_000)`` (20 turns, about 20,000 tokens).
The bound of each run is stated where it matters.
"""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

import bench_stubs
from contexto.bench import plan as bench_plan
from contexto.bench import script as bench_script
from contexto.bench.arms import full as full_arm
from contexto.bench.meter import record_tokens

BOUND = 30_000


@pytest.fixture(scope="module")
def script():
    return bench_script.build(3, "v2", bound=4_000, turn_bytes=2_000)


@pytest.fixture
def env(monkeypatch, tmp_path):
    bench_stubs.bench_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(full_arm, "BACKOFF_S", 0.0)
    return monkeypatch


def stub_for(script, **options):
    return bench_stubs.StubModel([script], key=bench_stubs.FAKE_KEY, **options)


def test_the_first_401_stops_the_whole_run_and_the_same_command_resumes_it(env, tmp_path, script):
    stub = stub_for(script)
    stub.fault(401, arm="plain", nth=2)
    first = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND, stub=stub)
    assert first.code == 1 and "refused credential" in first.printed
    manifest = first.manifest
    assert [u["arm"] for u in manifest["units"]] == ["plain"] and manifest["finished"] is None
    unit = manifest["units"][0]
    assert (unit["status"], unit["stop"]) == ("stopped", "auth")
    assert (unit["turns_completed"], unit["probe_turns_completed"]) == (2, 0)
    assert "401" in unit["reason"] and unit["requests"] == 3
    assert not (first.out / "probes.jsonl").exists()  # a stopped unit writes no probe lines
    assert [r["status"] for r in first.lines("requests.jsonl")] == [200, 200, 401]  # its requests stay
    assert not stub.messages_requests("full")  # no later unit ran
    assert all(child.poll() is not None for child in first.children)

    second = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND)
    assert second.code == 0 and "resuming: 0 units are complete, 2 to run" in second.printed
    units = {u["arm"]: u for u in second.manifest["units"]}
    assert units["plain"]["status"] == units["full"]["status"] == "complete"
    assert units["plain"]["attempt"] == 1 and units["full"]["attempt"] == 0
    requests = second.lines("requests.jsonl")
    assert [r["status"] for r in requests if r["arm"] == "plain" and r["attempt"] == 0] == [200, 200, 401]
    assert len([r for r in requests if r["arm"] == "plain" and r["attempt"] == 1]) == units["plain"]["requests"]
    assert {p["arm"] for p in second.lines("probes.jsonl")} == {"plain", "full"}
    assert second.manifest["finished"] is not None
    assert second.manifest["spent_tokens"] == sum(record_tokens(r) for r in requests)

    third = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND)
    assert third.code == 0 and "resuming: 2 units are complete, 0 to run" in third.printed
    assert third.lines("requests.jsonl") == requests and not third.stub.requests


def test_an_existing_output_directory_fixes_the_run(env, tmp_path, script):
    done = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND)
    assert done.code == 0
    for change, option in (({"model": "claude-opus-5-5"}, "--model"), ({"bound": BOUND + 1}, "--bound"),
                           ({"seed": 4}, "--seed"), ({"protocol": "d27"}, "--protocol"),
                           ({"turn_kbytes": 9}, "--turn-kbytes")):
        again = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), **{"bound": BOUND, **change})
        assert again.code == 2 and option in again.printed and "use another --out" in again.printed
        assert not again.stub.requests
    assert bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND).manifest["units"] \
        == done.manifest["units"]


def test_a_run_needs_a_budget_that_covers_the_plans_worst_case(env, tmp_path, script):
    worst = bench_plan.estimate({3: script}, bench_plan.units([3], 1, ("full", "plain")), BOUND)["total"].worst_total
    for budget, said in ((None, "needs --budget-tokens"), (worst - 1, f"worst case of {worst:,} tokens")):
        refused = bench_stubs.run_bench(env, tmp_path, [script], arms=("full", "plain"), bound=BOUND, budget=budget)
        assert refused.code == 2 and said in refused.printed, refused.printed
        assert not refused.out.exists() and not refused.stub.requests
    if worst:
        assert f"pass at least {bench_plan.budget_to_pass(worst):,}" in refused.printed
    allowed = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, budget=worst)
    assert allowed.code == 0


def test_the_guard_stops_before_the_budget_is_passed(env, tmp_path, script):
    """A plan that undercounts: the worst case is patched to nothing, so only the guard is left."""
    real = bench_plan.estimate

    def nothing(*args, **kwargs):
        plan = real(*args, **kwargs)
        plan["total"].worst = dict.fromkeys(plan["total"].worst, 0)
        return plan

    env.setattr(bench_plan, "estimate", nothing)
    budget = 150_000
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND, budget=budget)
    assert run.code == 1 and "stopped on the token budget" in run.printed
    (unit,) = run.manifest["units"]
    assert (unit["arm"], unit["status"], unit["stop"]) == ("plain", "stopped", "budget")
    assert "budget" in unit["reason"] and 0 < unit["turns_completed"] < len(script.turns)
    spent = sum(record_tokens(r) for r in run.lines("requests.jsonl"))
    assert 0 < spent <= budget and run.manifest["spent_tokens"] == spent
    # The request that could have passed the budget never reached the upstream.
    assert len(run.stub.messages_requests("plain")) == unit["requests"] and not run.stub.messages_requests("full")
    assert all(child.poll() is not None for child in run.children)


def test_the_full_arm_retries_429_529_and_a_dropped_stream_and_nothing_else(env, tmp_path, script):
    stub = stub_for(script)
    stub.fault(429, arm="full", nth=1)
    stub.fault(529, arm="full", nth=3)
    stub.fault("drop", arm="full", nth=5)
    stub.fault(503, arm="full", nth=0, path="/v1/messages/count_tokens")
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, stub=stub)
    assert run.code == 0 and run.unit("full")["status"] == "complete"
    statuses = [r["status"] for r in sorted(run.lines("requests.jsonl"), key=lambda r: r["n"])]
    assert statuses == [200, 429, 200, 200, 529, 200, 200, 200, 200, 200, 200]  # the cut stream answered 200
    assert len(run.lines("probes.jsonl")) == len(script.probes)

    again = stub_for(script)
    again.fault(400, arm="full", nth=2)
    stopped = bench_stubs.run_bench(env, tmp_path / "b", [script], arms=("full",), bound=BOUND, stub=again)
    unit = stopped.unit("full")
    assert stopped.code == 1
    assert (unit["status"], unit["stop"], unit["probe_turns_completed"]) == ("stopped", "api_error", 2)
    assert "400 (invalid_request_error)" in unit["reason"] and len(again.messages_requests("full")) == 3

    forever = stub_for(script)
    forever.fault(500, arm="full", nth=0, times=50)
    gave_up = bench_stubs.run_bench(env, tmp_path / "c", [script], arms=("full",), bound=BOUND, stub=forever)
    assert gave_up.unit("full")["stop"] == "api_error" and "5 times in a row" in gave_up.unit("full")["reason"]
    assert len(forever.messages_requests("full")) == full_arm.RETRIES + 1


def test_a_full_arm_that_does_not_fit_is_not_run_and_the_other_arms_still_run(env, tmp_path, script):
    stub = stub_for(script, count_tokens={"full": 2_000_000})
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full", "plain"), bound=BOUND, stub=stub)
    full, plain = run.unit("full"), run.unit("plain")
    assert (full["status"], full["stop"], full["requests"]) == ("not_run", None, 0)
    assert "does not fit the model's context window of 1,000,000 tokens" in full["reason"]
    assert not stub.messages_requests("full") and plain["status"] == "complete" and run.code == 0
    assert {p["arm"] for p in run.lines("probes.jsonl")} == {"plain"}

    refused = stub_for(script)
    refused.fault(400, arm="full", nth=0)
    run = bench_stubs.run_bench(env, tmp_path / "b", [script], arms=("full",), bound=BOUND, stub=refused)
    assert run.unit("full")["status"] == "not_run" and "400" in run.unit("full")["reason"]

    small = bench_stubs.run_bench(env, tmp_path / "c", [script], arms=("full",), bound=BOUND, context_window=10_000)
    assert small.unit("full")["status"] == "not_run" and "10,000 tokens" in small.unit("full")["reason"]
    assert small.manifest["full_arm"]["context_window"] == 10_000


def test_the_plain_arms_compactions_are_counted(env, tmp_path, script):
    """Bound 70,000, so the window is 103,000. The stub reports five times its modelled usage, which
    carries the session over 70,000 tokens once, late in the content turns."""
    stub = stub_for(script, usage_scale={"plain": 5.0})
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain",), bound=70_000, stub=stub)
    unit = run.unit("plain")
    assert run.manifest["plain_compaction"] == {
        "variable": "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "value": 103_000, "intended_trigger": 70_000, "forced": True,
        "note": run.manifest["plain_compaction"]["note"]}
    assert unit["status"] == "complete" and unit["compactions"] == len(unit["compaction_pre_tokens"]) >= 1
    assert all(pre >= 70_000 for pre in unit["compaction_pre_tokens"])
    requests = sorted(run.lines("requests.jsonl"), key=lambda r: r["n"])
    summaries = [r for r in requests if r["compaction"]]
    assert len(summaries) == unit["compactions"] and not any(r["main"] for r in summaries)
    assert [r["compaction"] for r in stub.messages_requests("plain")] == [None] * len(requests)  # stripped
    first = requests.index(summaries[0])
    assert requests[first - 1]["uncached"] + requests[first - 1]["cache_write"] + requests[first - 1]["cache_read"] \
        + requests[first - 1]["output"] == unit["compaction_pre_tokens"][0]
    assert unit["over_bound"] >= 1  # a main request above the bound is counted for every arm
    # What was compacted away cannot be answered: the plain arm now misses probes it got right before.
    assert sum(p["correct"] for p in run.lines("probes.jsonl")) < len(script.probes) - 10


def test_thrashing_is_its_own_stop_and_the_run_goes_on(env, tmp_path, script):
    stub = stub_for(script, usage_scale={"plain": 200.0})
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=70_000, stub=stub)
    plain, full = run.unit("plain"), run.unit("full")
    assert (plain["status"], plain["stop"]) == ("stopped", "thrashing") and "thrashing" in plain["reason"]
    assert plain["compactions"] == 2 and plain["cli_exit"] in (1, -15)  # it exited, or was stopped first
    assert full["status"] == "complete" and run.code == 1 and run.manifest["finished"] is not None
    assert {p["arm"] for p in run.lines("probes.jsonl")} == {"full"}


def test_a_cli_error_a_cli_exit_and_a_silent_turn_each_stop_one_unit(env, tmp_path, script):
    stub = stub_for(script)
    stub.fault(400, arm="plain", nth=1)
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND, stub=stub)
    plain = run.unit("plain")
    assert (plain["status"], plain["stop"], plain["turns_completed"]) == ("stopped", "cli_error", 1)
    assert "turn 2" in plain["reason"] and run.unit("full")["status"] == "complete"

    slow = stub_for(script, delay_s={"plain": 5.0})
    started = time.monotonic()
    run = bench_stubs.run_bench(env, tmp_path / "b", [script], arms=("plain",), bound=BOUND, stub=slow,
                                turn_timeout=0.5)
    plain = run.unit("plain")
    assert (plain["status"], plain["stop"]) == ("stopped", "timeout")
    assert "turn 1 had no result within" in plain["reason"]
    assert time.monotonic() - started < 4.5 and all(child.poll() is not None for child in run.children)

    exiting = tmp_path / "exiting"
    exiting.mkdir()
    (exiting / "claude").write_text('#!/bin/sh\n[ "$1" = --version ] && echo "9.9.9 (Claude Code)" && exit 0\nexit 3\n')
    (exiting / "claude").chmod(0o755)
    env.setenv("PATH", f"{exiting}{os.pathsep}{os.environ['PATH']}")
    run = bench_stubs.run_bench(env, tmp_path / "c", [script], arms=("plain",), bound=BOUND)
    plain = run.unit("plain")
    assert (plain["status"], plain["stop"], plain["cli_exit"]) == ("stopped", "cli_exit", 3)
    assert "exited before turn 1 finished (exit code 3)" in plain["reason"]
    assert run.manifest["claude_code_version"] == "9.9.9"


def test_a_wrong_decision_reply_is_recorded_per_probe(env, tmp_path, script):
    turn = script.decisions[1].turn
    stub = stub_for(script, wrong_decisions={turn})
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain",), bound=BOUND, stub=stub)
    expected = {d.probe: d.turn != turn for d in script.decisions}
    assert run.unit("plain")["concluded_ok"] == expected and list(expected.values()).count(False) == 1
    wrong = next(p for p in run.lines("probes.jsonl") if p["id"] == script.decisions[1].probe)
    assert wrong["correct"] is False  # scored against the generator's value, not the assistant's reply


def test_ctrl_c_stops_the_children_records_the_unit_and_exits_130(env, tmp_path, script):
    stub = stub_for(script, delay_s={"plain": 0.05})

    def interrupt():
        deadline = time.monotonic() + 60
        while len(stub.messages_requests("plain")) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND, stub=stub)
    assert run.code == 130 and "interrupted" in run.printed
    (unit,) = run.manifest["units"]
    assert (unit["arm"], unit["status"], unit["stop"], unit["reason"]) == ("plain", "stopped", "interrupted",
                                                                          "the run was interrupted")
    assert run.manifest["finished"] is None and not (run.out / "probes.jsonl").exists()
    assert 2 <= len(run.lines("requests.jsonl")) < len(script.turns)
    assert all(child.poll() is not None for child in run.children)
    assert not stub.messages_requests("full")


def test_an_environment_key_goes_out_as_x_api_key_only_in_every_arm(env, tmp_path, script):
    from contexto.bench import credentials as bench_credentials

    credentials = bench_credentials.resolve(None, None, {"ANTHROPIC_API_KEY": bench_stubs.FAKE_KEY})
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full", "plain"), bound=BOUND, credentials=credentials)
    assert run.code == 0 and run.manifest["credential_source"] == "env"
    assert {r["arm"] for r in run.stub.requests} == {"full", "plain"}
    assert all(r["key_ok"] and r["x_api_key"] and not r["bearer"] for r in run.stub.requests)
    for name in ("manifest.json", "requests.jsonl", "probes.jsonl"):
        assert bench_stubs.FAKE_KEY not in (run.out / name).read_text(encoding="utf-8")
    assert bench_stubs.FAKE_KEY not in run.printed and bench_stubs.FAKE_KEY not in str(run.commands)
    settings = (run.work / "plain-3-0-0" / "claude-config" / "settings.json").read_text(encoding="utf-8")
    assert "apiKeyHelper" not in settings and bench_stubs.FAKE_KEY not in settings


def test_the_report_is_written_at_the_end_when_this_build_has_the_report_code(env, tmp_path, script, monkeypatch):
    import sys
    import types

    without = bench_stubs.run_bench(env, tmp_path / "a", [script], arms=("full",), bound=BOUND)
    assert "run 'contexto bench --report out'" in without.printed and without.printed.count("\n") == 2
    called = []
    report = types.ModuleType("contexto.bench.report")
    report.write = called.append
    monkeypatch.setitem(sys.modules, "contexto.bench.report", report)
    import contexto.bench

    monkeypatch.setattr(contexto.bench, "report", report, raising=False)
    having = bench_stubs.run_bench(env, tmp_path / "b", [script], arms=("full",), bound=BOUND)
    assert called == [having.out] and "--report" not in having.printed
