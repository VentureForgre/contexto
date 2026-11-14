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
    assert "more than 12 billed requests" in plain["reason"]
    assert len(stub.messages_requests("plain")) == 12 and plain["requests"] == 12
    assert run.unit("full")["status"] == "complete" and run.code == 1
    caps = run.manifest["runaway_caps"]
    assert caps["turn_requests"] == 12 and "at most 6 requests in a probe turn" in caps["note"]
    assert caps["turn_failures"] == runner.TURN_FAILURE_CAP and "Failed responses" in caps["note"]
    line = next(x for x in run.manifest["arm_differences"] if "runaway caps" in x)
    assert line.startswith("Every arm alike") and "runaway_caps" in line


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
    harmless.write_text(json.dumps({"env": {"DISABLE_TELEMETRY": "1"}, "cleanupPeriodDays": 30, "permissions": {}}))
    env.setattr(managed, "sources", lambda platform=None: [
        ("the managed settings file", settings), ("the device-level managed preferences", plist),
        ("a managed settings drop-in", harmless), ("a source that is not there", tmp_path / "none")])
    found = managed.problems()
    assert found == ["the managed settings file sets apiKeyHelper; env ANTHROPIC_BASE_URL",
                     "the device-level managed preferences sets forceLoginMethod"]
    run = bench_stubs.run_bench(env, tmp_path, [script], arms=("plain", "full"), bound=BOUND)
    assert run.code == 2 and "organization-managed Claude Code settings" in run.printed
    assert "apiKeyHelper; env ANTHROPIC_BASE_URL" in run.printed and "planted" not in run.printed
    assert "elsewhere" not in run.printed and not run.out.exists()
    full = bench_stubs.run_bench(env, tmp_path / "f", [script], arms=("full",), bound=BOUND)
    assert full.code == 0  # the full arm does not run Claude Code


REFUSED_CASES = [(key, False if key == "autoCompactEnabled" else "planted-value")
                 for _what, keys in managed.REFUSED for key in keys]


@pytest.mark.parametrize(("key", "value"), REFUSED_CASES + [
    ("modelSettings", {"claude-haiku-5-5": {"autoCompactWindow": 500_000}}),
    ("permissions", {"deny": ["Read"]}), ("hooks", {"Stop": []}), ("autoCompactWindow", 400_000)])
def test_every_managed_setting_that_changes_an_arm_is_refused(tmp_path, key, value):
    """Each key of the table, set to a value that is not empty: compaction, permissions, the model, MCP
    servers, hooks, the system prompt, and where requests go."""
    path = tmp_path / "managed-settings.json"
    path.write_text(json.dumps({key: value, "cleanupPeriodDays": 30}))
    found = managed.problems([("the managed settings file", path)])
    assert found == [f"the managed settings file sets {key}"]
    assert "planted" not in found[0] and "500" not in found[0]  # the value is never named


def test_the_table_of_refused_managed_settings_covers_what_the_arms_depend_on():
    refused = {key for _what, keys in managed.REFUSED for key in keys}
    assert {"autoCompactEnabled", "autoCompactWindow", "modelSettings", "permissions", "model", "availableModels",
            "allowManagedMcpServersOnly", "deniedMcpServers", "managedMcpServers", "hooks", "disableAllHooks",
            "apiKeyHelper", "forceLoginMethod"} <= refused
    assert [what for what, _keys in managed.REFUSED][:6] == [
        "where requests go and with what credential", "compaction", "permissions", "the model", "MCP servers", "hooks"]


def test_a_managed_drop_in_or_mcp_file_is_named_by_kind_never_by_its_file_name(tmp_path, monkeypatch):
    drop_ins = tmp_path / managed.DROP_IN_DIR
    drop_ins.mkdir()
    (drop_ins / "10-acme-corp-route.json").write_text(json.dumps({"autoCompactEnabled": False}))
    (tmp_path / managed.MCP_FILE_NAME).write_text(json.dumps({"mcpServers": {"corp": {"command": "x"}}}))
    monkeypatch.setattr(managed, "managed_directory", lambda platform=None: tmp_path)
    found = managed.problems(managed.sources("linux"))
    assert found == ["a managed settings drop-in sets autoCompactEnabled",
                     "the managed MCP configuration sets mcpServers"]
    assert "acme" not in managed.refusal(found) and str(tmp_path) not in managed.refusal(found)


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
    assert first.code == 2 and not out.exists()  # below the plan: refused, and the directory it made is gone
    bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, out=out)
    monkeypatch.setattr(runner, "_run_units", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        bench_stubs.run_bench(env, tmp_path, [script], arms=("full",), bound=BOUND, out=out)
    probe = os.open(out / runner.LOCK_NAME, os.O_RDWR)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)


def test_two_runs_into_a_new_output_directory_never_both_run(env, tmp_path, script, monkeypatch):
    """Both runs find no --out and reach the point where it is made before either has made it, and the
    run that takes the lock first holds it until the other run has returned: the other is refused, and
    nothing in the directory is written twice. Each run waits at the mkdir only on its first call
    (``Path.mkdir(parents=True)`` calls itself again after making the parent), and any wait that runs
    out fails the test, so a trial in which the two runs did not overlap cannot pass or fail."""
    import io
    import threading

    from contexto.bench import credentials as bench_credentials

    stub = stub_for(script)
    url = stub.start()
    out = tmp_path / "new" / "out"
    barrier = threading.Barrier(2, timeout=30)
    gate = threading.Condition()
    real_mkdir, real_prepare = runner.Path.mkdir, runner.prepare
    codes, printed, waited, inside, problems = {}, {}, set(), [], []

    def config(name):
        return runner.Config(model="claude-haiku-5-5", bound=BOUND, protocol="v2", turn_kbytes=2, seed=3, seeds=[3],
                             repeats=1, arms=("full",), budget=2_000_000_000, out=out, upstream=url,
                             credentials=bench_credentials.resolve(f"printf %s {bench_stubs.FAKE_KEY}", None),
                             work=tmp_path / f"work-{name}")

    def mkdir(path, *args, **kwargs):
        if path == out and threading.get_ident() not in waited:
            waited.add(threading.get_ident())
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                problems.append("the runs never both reached the mkdir")
        return real_mkdir(path, *args, **kwargs)

    def prepare(*args, **kwargs):
        # Called with the lock held. The first run in waits here until the other run has returned
        # (refused) or has come in too (the lock let both through), so the two always overlap.
        with gate:
            inside.append(threading.get_ident())
            gate.notify_all()
            if not gate.wait_for(lambda: len(inside) == 2 or len(codes) == 1, timeout=30):
                problems.append("the other run neither returned nor came in")
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(runner.Path, "mkdir", mkdir)
    monkeypatch.setattr(runner, "prepare", prepare)

    def one(name):
        stream, code = io.StringIO(), None
        try:
            code = runner.run(config(name), {3: script}, stdout=stream, stderr=stream)
        finally:
            with gate:
                codes[name] = code
                printed[name] = stream.getvalue()
                gate.notify_all()

    try:
        threads = [threading.Thread(target=one, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
    finally:
        stub.stop()
    assert not problems, (problems, printed)
    assert len(inside) == 1, printed  # only the run that held the lock read the manifest
    assert sorted(codes.values()) == [0, 2], printed
    loser = next(name for name, code in codes.items() if code == 2)
    assert "another contexto bench run is writing to this output directory" in printed[loser]
    lines = [json.loads(x) for x in (out / "probes.jsonl").read_text().splitlines()]
    assert len(lines) == len(script.probes) == len({p["id"] for p in lines})
    assert len(json.loads((out / "manifest.json").read_text())["units"]) == 1


def test_two_processes_started_together_into_a_new_output_directory_never_both_run(tmp_path, script):
    """``contexto bench --arms full`` twice, 20 times. Each process imports Contexto, says it is ready and
    waits; both are released together, so they reach --out at the same moment."""
    import subprocess
    import sys

    stub = stub_for(script)
    url = stub.start()
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CONTEXTO_"))}
    env.update({"HOME": str(home), "ANTHROPIC_API_KEY": bench_stubs.FAKE_KEY})
    launcher = ("import sys\nfrom contexto.cli import main\nimport contexto.bench.runner\n"
                "print('ready', flush=True)\nsys.stdin.readline()\nsys.exit(main(sys.argv[1:]))")
    collided = 0
    try:
        for trial in range(20):
            out = tmp_path / f"out-{trial}"
            procs = [subprocess.Popen(
                [sys.executable, "-c", launcher, "bench", "--model", "claude-haiku-5-5", "--arms", "full",
                 "--seed", "3", "--bound", "4000", "--turn-kbytes", "2", "--budget-tokens", "2000000000",
                 "--upstream", url, "--out", str(out), "--work", str(tmp_path / f"work-{trial}-{n}")],
                env=env, cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True) for n in range(2)]
            for p in procs:
                assert p.stdout.readline() == "ready\n"
            for p in procs:
                p.stdin.write("go\n")
                p.stdin.flush()
            results = [(p.wait(timeout=120), p.stdout.read()) for p in procs]
            for p in procs:
                p.stdin.close()
                p.stdout.close()
            codes = sorted(code for code, _ in results)
            said = "\n".join(text for _, text in results)
            if codes == [0, 2]:
                collided += 1
                assert "another contexto bench run is writing to this output directory" in said
            else:  # one ended before the other looked: the second found the unit complete
                assert codes == [0, 0] and "resuming: 1 units are complete, 0 to run" in said, said
            lines = [json.loads(x) for x in (out / "probes.jsonl").read_text().splitlines()]
            assert len(lines) == len(script.probes) == len({p["id"] for p in lines}), trial
            recorded = [json.loads(x) for x in (out / "requests.jsonl").read_text().splitlines()]
            assert {r["attempt"] for r in recorded} == {0} and len({r["n"] for r in recorded}) == len(recorded)
    finally:
        stub.stop()
    assert collided >= 10  # the runs did overlap


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
