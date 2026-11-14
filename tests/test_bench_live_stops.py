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


def test_a_403_in_the_full_arm_stops_the_whole_run_too(env, tmp_path, script):
    for path in ("/v1/messages/count_tokens", "/v1/messages"):
        stub = stub_for(script)
        stub.fault(403, arm="full", nth=1, path=path)
        run = bench_stubs.run_bench(env, tmp_path / path.rsplit("/", 1)[1], [script], arms=("full", "plain"),
                                    bound=BOUND, stub=stub)
        (unit,) = run.manifest["units"]
        assert run.code == 1 and (unit["arm"], unit["status"], unit["stop"]) == ("full", "stopped", "auth")
        assert "403" in unit["reason"] and unit["probe_turns_completed"] == 1
        assert not stub.messages_requests("plain") and not (run.out / "probes.jsonl").exists()


def test_without_claude_on_path_only_the_full_arm_can_run(env, tmp_path, script):
    env.setenv("PATH", "/usr/bin:/bin")
    refused = bench_stubs.run_bench(env, tmp_path / "a", [script], arms=("full", "plain"), bound=BOUND)
    assert refused.code == 127 and "'claude' was not found on PATH" in refused.printed
    assert not refused.out.exists() and not refused.stub.requests
    alone = bench_stubs.run_bench(env, tmp_path / "b", [script], arms=("full",), bound=BOUND)
    assert alone.code == 0 and alone.manifest["claude_code_version"] is None and alone.manifest["cache_ttl"] is None
    assert alone.manifest["plain_compaction"] is None and alone.manifest["request_classes"] is None


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


@pytest.mark.parametrize(("status", "times"), [(529, 4), (400, 1)])
def test_claude_code_giving_up_on_failed_responses_is_an_api_error(env, tmp_path, script, status, times):
    """Turn 2's requests all fail (the stub claude tries a 529 four times, a 400 once) and Claude Code
    prints an error result: the upstream failed, so the unit stops as api_error, not cli_error."""
    stub = stub_for(script)
    stub.fault(status, arm="plain", nth=1, times=times)
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND, stub=stub)
    plain = run.unit("plain")
    assert (plain["status"], plain["stop"], plain["turns_completed"]) == ("stopped", "api_error", 1), plain
    assert plain["reason"] == (f"the upstream answered none of turn 2's requests ({times} failed "
                               f"response{'s' if times > 1 else ''}: a 429, 529 or other error status, or none), "
                               "and Claude Code gave up on the turn")
    assert run.unit("full")["status"] == "complete"


def test_a_request_that_got_no_response_is_counted_per_attempt_and_its_reservation_stays_spent(env, tmp_path,
                                                                                               script):
    """The upstream reads one billed request and closes with no response; the full arm retries it. The
    unit and its attempt count it in the manifest as soon as it happens (a kill keeps the count), its
    reservation stays in spent_tokens, and a resumed run counts it as spent."""
    import json

    from contexto.bench import runner

    out = tmp_path / "out"
    stub = stub_for(script)
    stub.fault("close", arm="full", nth=2)
    during = []

    def look(arm, path):
        if path == "/v1/messages" and len(stub.messages_requests()) == 5:  # two after the unanswered one
            during.append(json.loads((out / "manifest.json").read_text())["units"][-1])

    stub.on_request = look
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, stub=stub, out=out)
    assert run.code == 0, run.printed
    unit = run.unit("full")
    records = run.lines("requests.jsonl")
    assert unit["status"] == "complete" and unit["requests"] == len(records)
    assert sorted(r["n"] for r in records) == [n for n in range(len(records) + 1) if n != 2]  # n 2 left a gap
    assert unit["requests_unanswered"] == 1 and unit["unanswered_tokens"] > 0
    (attempt,) = unit["attempts"]
    assert (attempt["requests_unanswered"], attempt["unanswered_tokens"]) == (1, unit["unanswered_tokens"])
    assert unit["tokens"] == sum(record_tokens(r) for r in records)  # the records' own tokens
    assert run.manifest["spent_tokens"] == unit["tokens"] + unit["unanswered_tokens"]
    assert any("requests_unanswered" in line and "unanswered_tokens" in line for line in run.manifest["metering"])
    (seen,) = during
    assert (seen["status"], seen["requests_unanswered"], seen["unanswered_tokens"]) == (
        "running", 1, unit["unanswered_tokens"])
    assert seen["attempts"][-1]["requests_unanswered"] == 1
    assert runner.recorded_spend(out, run.manifest) == run.manifest["spent_tokens"]
    # A unit a kill left running keeps the count its running entry had.
    killed = {"units": [{**seen}]}
    runner.settle_killed(killed, {})
    assert (killed["units"][0]["requests_unanswered"], killed["units"][0]["attempts"][-1]["unanswered_tokens"]) == (
        1, unit["unanswered_tokens"])
    assert runner.recorded_spend(tmp_path / "nowhere", killed) == unit["unanswered_tokens"]


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
    env.setenv("STUB_CLAUDE_ERROR_RESULT", "2")  # turn 2's request is answered, then Claude Code gives up
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND, stub=stub)
    env.delenv("STUB_CLAUDE_ERROR_RESULT")
    plain = run.unit("plain")
    assert (plain["status"], plain["stop"], plain["turns_completed"]) == ("stopped", "cli_error", 1)
    assert plain["reason"] == "Claude Code reported an error on turn 2"  # no "(success)"
    assert run.unit("full")["status"] == "complete"

    slow = stub_for(script, delay_s={"plain": 30.0})
    started = time.monotonic()
    run = bench_stubs.run_bench(env, tmp_path / "b", [script], arms=("plain",), bound=BOUND, stub=slow,
                                turn_timeout=0.5)
    plain = run.unit("plain")
    assert (plain["status"], plain["stop"]) == ("stopped", "timeout")
    assert "turn 1 had no result within" in plain["reason"]
    # The unit ended on the timeout, long before the upstream's answer, and left no process behind.
    assert time.monotonic() - started < 20 and all(child.poll() is not None for child in run.children)

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


@pytest.mark.parametrize(("arm", "extra", "says"), [
    ("plain", "3", "answered no turn"),  # the extra run ends before the next turn goes out
    ("contexto", "3", "answered no turn"),
    ("plain", "3:late", "answered no turn"),  # it takes the next turn's place, so it is found later
    ("plain", "20", "after the script's last turn"),  # found when the session ends
])
def test_a_result_that_answers_no_turn_stops_the_unit_as_desync(env, tmp_path, script, arm, extra, says):
    """Claude Code prints one more run, with its own result, when a background task reports back. From
    then on every reply would be scored against the wrong turn, so the unit stops and is never complete."""
    env.setenv("STUB_CLAUDE_EXTRA_RESULT", extra)
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=(arm, "full"), bound=BOUND)
    unit = run.unit(arm)
    assert (unit["status"], unit["stop"]) == ("stopped", "desync"), unit
    assert says in unit["reason"] and "none of the unit's answers are scored" in unit["reason"]
    assert {p["arm"] for p in run.lines("probes.jsonl")} == {"full"}  # nothing of the unit is scored
    assert run.unit("full")["status"] == "complete" and run.code == 1
    assert all(child.poll() is not None for child in run.children)


def test_each_turn_has_one_result_without_a_background_task(env, tmp_path, script):
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain",), bound=BOUND)
    assert run.unit("plain")["status"] == "complete" and run.code == 0
    assert len(run.lines("probes.jsonl")) == len(script.probes)


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
    import json
    import sys
    import types

    # This build has the report code: the run ends with the results, read from what it wrote.
    real = bench_stubs.run_bench(env, tmp_path / "r", [script], arms=("full",), bound=BOUND)
    assert real.code == 0 and "--report" not in real.printed and real.printed.count("\n") == 1
    results = json.loads((real.out / "results.json").read_text(encoding="utf-8"))
    assert results["kind"] == "live" and (real.out / "results.md").exists()
    with monkeypatch.context() as patch:  # a build without it: importing it fails
        import contexto.bench

        patch.setitem(sys.modules, "contexto.bench.report", None)
        patch.delattr(contexto.bench, "report", raising=False)
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

    def broken(out):
        raise KeyError("a private detail")

    report.write = broken
    failing = bench_stubs.run_bench(env, tmp_path / "c", [script], arms=("full",), bound=BOUND)
    assert failing.code == 0 and "the report could not be written (KeyError)" in failing.printed
    assert "private detail" not in failing.printed and "contexto bench --report out" in failing.printed


def test_probe_lines_of_a_unit_the_manifest_does_not_record_are_dropped_on_resume(env, tmp_path, script):
    """A run killed between a unit's probe lines and its manifest entry: the unit runs again, once."""
    import json

    first = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND)
    manifest = first.manifest
    manifest["units"], manifest["finished"] = [], None
    (first.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    again = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND)
    # requests.jsonl holds attempt 0's records, so the rerun is attempt 1 though the manifest had lost it.
    unit = again.unit("full")
    assert again.code == 0 and unit["attempt"] == 1 and [a["attempt"] for a in unit["attempts"]] == [1]
    # The printed attempt is the manifest's number (0-based), as the report's messages give it.
    assert f"full seed {script.seed} repeat 0, attempt 1: complete" in again.printed
    assert ", attempt" not in first.printed  # the first attempt is not named
    probes = again.lines("probes.jsonl")
    assert len(probes) == len(script.probes) == len({p["id"] for p in probes})
    assert len(again.lines("requests.jsonl")) == 16  # both attempts' requests stay: they were billed


def test_a_dry_run_on_an_existing_run_prints_what_it_holds_and_changes_nothing(env, tmp_path, script, capsys):
    from contexto.cli import main

    stub = stub_for(script)
    stub.fault(400, arm="plain", nth=1)  # the plain unit stops; the full unit completes
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND, stub=stub)
    assert [u["status"] for u in run.manifest["units"]] == ["stopped", "complete"]
    before = {p.relative_to(run.out): p.read_bytes() for p in sorted(run.out.rglob("*")) if p.is_file()}
    spent = run.manifest["spent_tokens"]
    common = ["bench", "--dry-run", "--seed", "3", "--bound", str(BOUND), "--turn-kbytes", "2", "--arms",
              "plain,full", "--out", str(run.out)]
    capsys.readouterr()
    for model in (["--model", "claude-haiku-5-5"], []):
        assert main([*common, *model]) == 0
        out = capsys.readouterr().out
        assert "1 units to run" in out
        assert (f"The output directory already holds a run: 1 of 2 units complete, 1 stopped, {spent:,} tokens "
                "recorded; the plan below covers the 1 still to run.") in out
        assert "| plain | 1 |" in out and "| full |" not in out
    assert main([*common[:5], "40000", *common[6:], "--model", "claude-haiku-5-5"]) == 2
    assert "different --bound (30000, not 40000)" in capsys.readouterr().err
    after = {p.relative_to(run.out): p.read_bytes() for p in sorted(run.out.rglob("*")) if p.is_file()}
    assert after == before  # the dry run read the directory and wrote nothing


def test_a_torn_last_line_is_moved_aside_or_completed_before_the_run_appends(env, tmp_path, script):
    """A last line with no newline (a kill mid-write) never has the next record appended to it. A
    fragment that is not a whole JSON object goes to <file>.torn-<n>; a whole record that only lost
    its newline gets it back and is counted. Each is recorded in the manifest's torn_lines."""
    import json

    out = tmp_path / "out"
    first = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, out=out)
    assert first.code == 0
    requests, probes = out / "requests.jsonl", out / "probes.jsonl"
    records = requests.read_text().splitlines()
    whole = json.loads(records[-1])
    # The last record lost only its newline; a probe line was cut halfway.
    requests.write_text("\n".join(records[:-1]) + "\n" + records[-1], encoding="utf-8")
    cut = '{"arm": "plain", "seed": 3, "repe'
    probes.write_text(probes.read_text() + cut, encoding="utf-8")
    (out / "probes.jsonl.torn-1").write_text("an earlier fragment", encoding="utf-8")
    spent = first.manifest["spent_tokens"]
    second = bench_stubs.run_bench(env, tmp_path, [script], arms=("full", "plain"), bound=BOUND, out=out)
    assert second.code == 0, second.printed
    assert (out / "probes.jsonl.torn-2").read_text() == cut  # the next free name
    assert (out / "probes.jsonl.torn-1").read_text() == "an earlier fragment"
    for path in (requests, probes):
        text = path.read_text()
        assert text.endswith("\n") and all(json.loads(line) for line in text.splitlines())
    assert json.loads(requests.read_text().splitlines()[len(records) - 1]) == whole  # kept and completed
    torn = second.manifest["torn_lines"]
    assert [(t["file"], t["action"], t.get("moved_to")) for t in torn] == [
        ("requests.jsonl", "newline_added", None), ("probes.jsonl", "moved", "probes.jsonl.torn-2")]
    assert all(t["found"].endswith("Z") and t["bytes"] > 0 for t in torn)
    assert second.manifest["spent_tokens"] >= spent  # the completed record is still counted
    # Nothing torn: nothing moved, and what was recorded stays.
    third = bench_stubs.run_bench(env, tmp_path, [script], arms=("full", "plain"), bound=BOUND, out=out)
    assert third.code == 0 and third.manifest["torn_lines"] == torn
    assert sorted(p.name for p in out.glob("*.torn-*")) == ["probes.jsonl.torn-1", "probes.jsonl.torn-2"]


def test_torn_lines_are_left_alone_in_a_directory_that_is_not_a_live_run(env, tmp_path, script):
    out = tmp_path / "out"
    out.mkdir()
    (out / "manifest.json").write_text('{"kind": "replay", "units": []}', encoding="utf-8")
    (out / "requests.jsonl").write_text('{"half', encoding="utf-8")
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, out=out)
    assert run.code == 2 and (out / "requests.jsonl").read_text() == '{"half'
    assert not list(out.glob("*.torn-*"))
