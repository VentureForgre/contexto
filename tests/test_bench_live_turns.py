"""The live benchmark against the stub upstream and the stub ``claude``: what a turn's answers are, the
runaway caps, a turn that bypasses the meter, managed settings, the output directory's lock and the
manifest's statements. No model, no spend: everything runs on 127.0.0.1 with a fake key.
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib

import pytest

import bench_stubs
from contexto.bench import plan as bench_plan
from contexto.bench import runner
from contexto.bench import script as bench_script
from contexto.bench.arms import cli as cli_arm
from contexto.bench.arms import full as full_arm
from contexto.bench.arms import managed

BOUND = 30_000


@pytest.fixture(scope="module")
def script():
    return bench_script.build(3, "v2", bound=4_000, turn_bytes=2_000)


@pytest.fixture
def env(monkeypatch, tmp_path):
    bench_stubs.bench_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(full_arm, "BACKOFF_S", 0.0)
    # This machine's own managed settings are not read.
    monkeypatch.setattr(managed, "sources", lambda platform=None: [])
    return monkeypatch


def stub_for(script, **options):
    return bench_stubs.StubModel([script], key=bench_stubs.FAKE_KEY, **options)


# -- what a turn's answers are ---------------------------------------------------------------------------


def test_answers_written_before_a_tool_call_are_scored_with_the_rest(env, tmp_path, script):
    """The stub answers half of each probe turn, calls a tool, then answers the rest and corrects one
    answer; it gives each decision before a tool call and ends with a message that has none. Every
    answer counts, the last one for an id wins, and every decision is read."""
    stub = stub_for(script, split=True, error_rates={"plain": 0.2})
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("contexto", "plain"), bound=BOUND, stub=stub)
    assert run.code == 0, run.printed
    for arm in ("contexto", "plain"):
        unit = run.unit(arm)
        assert unit["status"] == "complete" and unit["concluded_ok"] == {d.probe: True for d in script.decisions}
        lines = [p for p in run.lines("probes.jsonl") if p["arm"] == arm]
        assert len(lines) == len(script.probes)
        # Every probe's given answer is the stub's last answer for it, whichever message it was in.
        assert [p["given"] for p in lines] == [stub.answered[(arm, script.seed, p["id"])] for p in lines]
        assert not any(p["given"] == "unsure" for p in lines)  # the corrected answers won


def test_cli_session_text_is_every_top_level_assistant_text_of_the_turn():
    """The stream parser alone, with events the way Claude Code prints them: one assistant event per
    content block, a subagent's message (parent_tool_use_id set) left out, and the result holding only
    the last message's text."""
    events = [
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "text", "text": "q_a: 1"}]}},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/w/cwd/notes/a.md"}}]}},
        {"type": "assistant", "parent_tool_use_id": "t1", "message": {"content": [{"type": "text", "text": "q_a: 9"}]}},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Grep", "input": {"pattern": "x"}},
            {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "notes/a.md"}}]}},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "text", "text": "q_b: 2"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "q_b: 2"},
    ]

    class Meter:
        ledger = type("L", (), {"stopped": type("E", (), {"is_set": staticmethod(lambda: False)})()})()
        stopped = None

        @staticmethod
        def turn_requests(turn):
            return 1

    session = cli_arm.Session.__new__(cli_arm.Session)
    session.proc = type("P", (), {"stdin": open(os.devnull, "wb")})()  # noqa: SIM115 - closed below
    import queue

    session._events = queue.Queue()
    for event in events:
        session._events.put(event)
    # The events stand for what the child prints after the turn is sent; what was read before it is
    # taken first (Session.drain), so here nothing is.
    session.drain = lambda until_eof=None: None
    session.sent = session.results = 0
    session.compaction_pre_tokens = []
    unit = type("U", (), {"meter": Meter(), "turn_timeout": 5.0})()
    turn = session.send(0, "turn text", unit)
    session.proc.stdin.close()
    assert turn.text == "q_a: 1\nq_b: 2" and turn.tool_calls == 3
    assert turn.read_paths == ["/w/cwd/notes/a.md", "notes/a.md"]
    assert cli_arm.files_read(turn.read_paths, ["notes/a.md", "notes/b.md"], cli_arm.Path("/w/cwd")) == 1


def test_an_answer_over_200_characters_is_scored_whole_then_cut_and_the_cut_is_stated(env, tmp_path, script):
    stub = stub_for(script, long_answers=True)
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, stub=stub)
    lines = run.lines("probes.jsonl")
    long = [p for p in lines if p["given_truncated"]]
    assert len(long) > 40
    # Right when scored whole (the 250 backticks before it are trimmed); its first 200 characters are
    # only backticks, so scoring after the cut would have made every one of them wrong.
    assert all(p["correct"] and p["given"] == "`" * 200 for p in long)
    assert all(not p["given_truncated"] for p in lines if p not in long)


def test_the_full_arms_requests_name_the_model_of_the_run(env, tmp_path, script):
    stub = stub_for(script)
    seen = []
    real = full_arm.request

    def spy(*args, **kwargs):
        body = real(*args, **kwargs)
        seen.append(body["model"])
        return body

    env.setattr(full_arm, "request", spy)
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, stub=stub,
                                model="claude-opus-5-5")
    assert run.code == 0 and seen and set(seen) == {"claude-opus-5-5"}
    assert {r["model"] for r in run.lines("requests.jsonl")} == {"claude-opus-5-5"}


def test_toolloop_reads_counts_distinct_files_and_the_tool_calls_are_reported(env, tmp_path, script):
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain",), bound=BOUND)
    unit = run.unit("plain")
    assert unit["toolloop_reads"] == len(script.reads) == 10 and unit["toolloop_tool_calls"] == 10


# -- runaway caps ----------------------------------------------------------------------------------------


def test_a_turn_that_calls_tools_without_end_is_stopped_and_the_run_goes_on(env, tmp_path, script):
    stub = stub_for(script, tool_forever={"plain"})
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND, stub=stub,
                                turn_request_cap=12)
    plain = run.unit("plain")
    assert (plain["status"], plain["stop"], plain["turns_completed"]) == ("stopped", "runaway", 0)
    assert "more than 12 requests" in plain["reason"]
    assert len(stub.messages_requests("plain")) == 12 and plain["requests"] == 12
    assert run.unit("full")["status"] == "complete" and run.code == 1
    caps = run.manifest["runaway_caps"]
    assert caps["turn_requests"] == 12 and "at most 6 requests in a probe turn" in caps["note"]


def test_a_unit_past_its_token_cap_is_stopped_and_the_run_goes_on(env, tmp_path, script):
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND,
                                unit_token_cap=230_000)
    plain = run.unit("plain")
    assert (plain["status"], plain["stop"], plain["token_cap"]) == ("stopped", "runaway", 230_000)
    assert "cap of 230,000 tokens" in plain["reason"] and plain["tokens"] <= 230_000
    assert run.unit("full")["status"] == "complete"
    assert run.manifest["runaway_caps"]["unit_tokens"] == 230_000


def test_the_default_token_cap_is_twice_the_units_planned_worst_case(env, tmp_path, script):
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND)
    worst = bench_plan.unit_estimate("full", script, BOUND).worst_total
    assert run.unit("full")["token_cap"] == 2 * worst and run.manifest["runaway_caps"]["unit_tokens"] is None


# -- the meter cannot be bypassed silently ---------------------------------------------------------------


def test_a_turn_with_no_request_through_the_meter_stops_the_unit(env, tmp_path, script):
    from contexto.bench.arms import cli

    env.setattr(cli, "UNMETERED_GRACE_S", 1.0)  # how long it waits for a sign that the result answered no turn
    stub = stub_for(script)
    url = stub.start()
    try:
        env.setenv("STUB_CLAUDE_DIRECT_URL", url)  # the stub claude goes straight to the upstream
        run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND, stub=stub,
                                    upstream=url)
    finally:
        stub.stop()
    plain = run.unit("plain")
    assert (plain["status"], plain["stop"], plain["requests"]) == ("stopped", "unmetered", 0)
    assert "turn 1 ended with no request through" in plain["reason"]
    assert stub.messages_requests("plain")  # it did reach the upstream, around the meter
    assert run.unit("full")["status"] == "complete"


def test_managed_settings_that_reroute_claude_code_refuse_the_run(env, tmp_path, script):
    settings = tmp_path / "managed-settings.json"
    settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://elsewhere.test", "OTHER": "1"},
                                    "apiKeyHelper": "echo planted", "permissions": {}}))
    plist = tmp_path / "managed.plist"
    plist.write_bytes(plistlib.dumps({"forceLoginMethod": "claudeai"}))
    harmless = tmp_path / "harmless.json"
    harmless.write_text(json.dumps({"env": {"DISABLE_TELEMETRY": "1"}, "permissions": {"deny": ["Bash"]}}))
    env.setattr(managed, "sources", lambda platform=None: [
        ("the managed settings file", settings), ("the device-level managed preferences", plist),
        ("the managed settings drop-in 10-team.json", harmless), ("a source that is not there", tmp_path / "none")])
    found = managed.problems()
    assert found == ["the managed settings file sets apiKeyHelper; env ANTHROPIC_BASE_URL",
                     "the device-level managed preferences sets forceLoginMethod"]
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND)
    assert run.code == 2 and "organization-managed Claude Code settings" in run.printed
    assert "apiKeyHelper; env ANTHROPIC_BASE_URL" in run.printed and "planted" not in run.printed
    assert "elsewhere" not in run.printed and not run.out.exists()
    full = bench_stubs.run_bench(env, tmp_path / "f", [script], arms=("full",), bound=BOUND)
    assert full.code == 0  # the full arm does not run Claude Code


def test_managed_settings_are_read_where_claude_code_reads_them(tmp_path):
    darwin = managed.sources("darwin")
    assert darwin[0] == ("the managed settings file",
                         managed.Path("/Library/Application Support/ClaudeCode/managed-settings.json"))
    assert darwin[-1][1] == managed.Path("/Library/Managed Preferences/com.anthropic.claudecode.plist")
    assert all(os.path.basename(os.path.expanduser("~")) not in label for label, _ in darwin)
    assert managed.sources("linux")[0][1] == managed.Path("/etc/claude-code/managed-settings.json")


# -- the output directory --------------------------------------------------------------------------------


def test_a_second_run_into_the_same_output_directory_is_refused_at_once(env, tmp_path, script):
    out = tmp_path / "out"
    out.mkdir()
    holder = os.open(out / runner.LOCK_NAME, os.O_RDWR | os.O_CREAT)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another run holds it
    try:
        stub = stub_for(script)
        run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, stub=stub, out=out)
        assert run.code == 2 and "another contexto bench run is writing to this output directory" in run.printed
        assert not stub.requests and sorted(p.name for p in out.iterdir()) == [runner.LOCK_NAME]
    finally:
        os.close(holder)
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, out=out)
    assert run.code == 0
    # Released on the way out: another process can take it now.
    probe = os.open(out / runner.LOCK_NAME, os.O_RDWR)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)


def test_the_lock_is_released_when_a_run_is_refused_or_fails(env, tmp_path, script, monkeypatch):
    out = tmp_path / "out"
    first = bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, out=out, budget=10)
    assert first.code == 2  # the budget is below the plan: refused before the directory was made
    bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, out=out)
    monkeypatch.setattr(runner, "_run_units", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, out=out)
    probe = os.open(out / runner.LOCK_NAME, os.O_RDWR)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)


# -- what the manifest says ------------------------------------------------------------------------------


def test_the_manifest_states_the_label_the_full_arms_scripted_steps_and_how_compactions_count(env, tmp_path,
                                                                                               script):
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("full", "plain"), bound=BOUND)
    manifest = run.manifest
    assert manifest["upstream"] == "local" and manifest["label"] == runner.STUB_LABEL
    assert len(manifest["label"]) <= 200 and manifest["label"].isprintable() and "stub" in manifest["label"]
    assert manifest["full_arm"]["scripted"] == ["decision replies", "tool-loop file reads"]
    assert manifest["full_arm"]["not_applicable"] == ["concluded_ok", "toolloop_reads"]
    assert "compaction_requests" in manifest["compaction_counting"]
    assert any("x-cc-context-compacted" in line for line in manifest["arm_differences"])
    assert manifest["upstream_source"] == "flag" and "$ANTHROPIC_BASE_URL" in manifest["upstream_note"]
    plain = run.unit("plain")
    for name in ("compaction_requests", "compaction_tokens", "compaction_max_input", "usage_unreadable"):
        assert isinstance(plain[name], int)
    assert runner.label("https://api.anthropic.com") is None and runner.label("https://gw.example.test") is None


def test_the_plain_arms_compaction_requests_count_in_its_largest_input(env, tmp_path, script):
    stub = stub_for(script, usage_scale={"plain": 5.0})
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain",), bound=70_000, stub=stub)
    plain = run.unit("plain")
    records = [r for r in run.lines("requests.jsonl") if r["arm"] == "plain"]
    flagged = [r for r in records if r["compaction"]]
    assert plain["compactions"] >= 1 and plain["compaction_requests"] == len(flagged) >= plain["compactions"]
    inputs = [r["uncached"] + r["cache_write"] + r["cache_read"] for r in records if r["main"] or r["compaction"]]
    assert plain["max_input"] == max(inputs) and plain["over_bound"] == sum(1 for x in inputs if x > 70_000)
    assert plain["compaction_max_input"] == max(r["uncached"] + r["cache_write"] + r["cache_read"] for r in flagged)


def test_the_upstream_defaults_to_the_environment_and_the_manifest_says_where_it_came_from(monkeypatch):
    import argparse

    from contexto.bench.modes import _live_options

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    args = argparse.Namespace(upstream=None)
    assert (_live_options.upstream(args), _live_options.upstream_source(args)) == (
        "https://api.anthropic.com", "default")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8317")
    assert (_live_options.upstream(args), _live_options.upstream_source(args)) == ("http://127.0.0.1:8317",
                                                                                  "environment")
    args.upstream = "https://gw.example.test"
    assert _live_options.upstream_source(args) == "flag"
    note = runner.upstream_note("environment")
    assert "$ANTHROPIC_BASE_URL (no --upstream was given)" in note and "never its host" in note
