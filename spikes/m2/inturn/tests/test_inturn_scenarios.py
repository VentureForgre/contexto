"""The scenarios and the harness: what is true at v0.5.24 and stays true after D63.

The sizes pinned here are the ones docs/research/M2-in-turn-paging-baseline.md gives. Nothing here
asserts a number in-turn paging will change (over_bound counts, the largest request, the rooms, what
the searches return): those are in the research note, and what they must become is in
test_inturn_targets.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from inturn import cli, harness, replay, scripts, stubs, targets  # noqa: E402

from contexto.rewrite import ANTHROPIC, MESSAGES, RewriteEngine, claude_code_session, dialect_for  # noqa: E402
from contexto.store.render import own_call_ids  # noqa: E402

CASES = [(name, dialect) for name in scripts.BUILDERS for dialect in scripts.build(name, tiny=True).dialects]
REPLAY = Path(__file__).resolve().parents[1] / "replay.py"

# harness.script_size at the sizes the tests run: requests, scripted tool rounds, probes (a search and
# a recall each), tool calls, and the scripted tool results in the fake upstream's tokens.
SIZES = {
    ("loop120", "claude"): {"requests": 125, "rounds": 120, "probes": 2, "calls": 120, "result_tokens": 772_440, "largest_result": 6_437, "images": 0},
    ("loop120", "codex"): {"requests": 125, "rounds": 120, "probes": 2, "calls": 120, "result_tokens": 772_561, "largest_result": 6_439, "images": 0},
    ("large", "claude"): {"requests": 15, "rounds": 12, "probes": 1, "calls": 12, "result_tokens": 387_644, "largest_result": 200_037, "images": 0},
    ("large", "codex"): {"requests": 15, "rounds": 12, "probes": 1, "calls": 12, "result_tokens": 387_656, "largest_result": 200_038, "images": 0},
    ("small", "claude"): {"requests": 265, "rounds": 260, "probes": 2, "calls": 260, "result_tokens": 113_620, "largest_result": 437, "images": 0},
    ("small", "codex"): {"requests": 265, "rounds": 260, "probes": 2, "calls": 260, "result_tokens": 113_880, "largest_result": 438, "images": 0},
    ("subagent", "claude"): {"requests": 214, "rounds": 208, "probes": 2, "calls": 208, "result_tokens": 826_216, "largest_result": 5_037, "images": 0},
    ("images", "claude"): {"requests": 105, "rounds": 100, "probes": 2, "calls": 100, "result_tokens": 360_787, "largest_result": 5_313, "images": 68},
    ("images", "codex"): {"requests": 105, "rounds": 100, "probes": 2, "calls": 100, "result_tokens": 361_090, "largest_result": 5_317, "images": 68},
    ("multiturn", "claude"): {"requests": 120, "rounds": 110, "probes": 2, "calls": 110, "result_tokens": 519_070, "largest_result": 5_037, "images": 0},
    ("multiturn", "codex"): {"requests": 120, "rounds": 110, "probes": 2, "calls": 110, "result_tokens": 519_186, "largest_result": 5_039, "images": 0},
    ("mixed", "claude"): {"requests": 50, "rounds": 45, "probes": 2, "calls": 75, "result_tokens": 230_775, "largest_result": 4_037, "images": 0},
    ("mixed", "codex"): {"requests": 50, "rounds": 45, "probes": 2, "calls": 75, "result_tokens": 230_863, "largest_result": 4_040, "images": 0},
}
FULL_SMALL = {
    "claude": {"requests": 1_505, "rounds": 1_500, "probes": 2, "calls": 1_500, "result_tokens": 655_500, "largest_result": 437, "images": 0},
    "codex": {"requests": 1_505, "rounds": 1_500, "probes": 2, "calls": 1_500, "result_tokens": 657_000, "largest_result": 438, "images": 0},
}
# Each CLI's whole scripted history with its head, in the fake upstream's tokens: what the last request
# would be with nothing paged and without the probe rounds, whose replies are the engine's. It depends
# on the script alone, so in-turn paging does not change it.
SCRIPTED = {
    ("loop120", "claude"): {"main": 810_930},
    ("loop120", "codex"): {"main": 810_764},
    ("large", "claude"): {"main": 405_864},
    ("large", "codex"): {"main": 404_148},
    ("small", "claude"): {"main": 167_466},
    ("small", "codex"): {"main": 165_418},
    ("subagent", "claude"): {"main": 97_916, "subagent": 795_349},
    ("images", "claude"): {"main": 400_251},
    ("images", "codex"): {"main": 398_864},
    ("multiturn", "claude"): {"main": 558_219},
    ("multiturn", "codex"): {"main": 558_140},
    ("mixed", "claude"): {"main": 257_772},
    ("mixed", "codex"): {"main": 256_043},
}


# The scripts ------------------------------------------------------------------------------------------

def test_every_scenario_runs_on_the_dialects_the_record_names():
    assert CASES == list(SIZES) == list(SCRIPTED)
    assert [name for name in scripts.BUILDERS if scripts.build(name, tiny=True).dialects == ("claude",)] == ["subagent"]


@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_each_scenarios_size_is_what_the_record_says(name, dialect):
    assert harness.script_size(scripts.build(name), dialect) == SIZES[name, dialect]


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_small_at_full_size_is_1500_rounds(dialect):
    """The tests run 260 rounds; ``replay.py --full`` runs these 1,500."""
    assert harness.script_size(scripts.build("small", full=True), dialect) == FULL_SMALL[dialect]


def test_a_script_is_the_same_every_time_it_is_built():
    for name in scripts.BUILDERS:
        assert scripts.build(name, quick=True) == scripts.build(name, quick=True)
    assert scripts.loop120(rounds=4) != scripts.loop120(rounds=5)


def test_every_tool_result_carries_its_own_first_and_last_line():
    for name in scripts.BUILDERS:
        calls = [call for c in scripts.build(name).conversations for call in c.calls if call.head]
        marks = [mark for call in calls for mark in (call.head, call.tail)]
        assert len(set(marks)) == len(marks)
        assert all(call.result.startswith(call.head + "\n") and call.result.endswith("\n" + call.tail) for call in calls)
        assert len({call.key for call in calls}) == len(calls)


def test_a_probe_looks_late_in_a_loop_for_a_line_only_one_early_result_holds():
    for name in scripts.BUILDERS:
        for conversation in scripts.build(name).conversations:
            for turn in conversation.turns:
                rounds = [i for i, step in enumerate(turn.steps) if step.calls]
                searches = [(i, step.probe) for i, step in enumerate(turn.steps)
                            if step.probe and step.probe.kind == "search"]
                unprobed = len(rounds) < scripts.PROBES_FROM or (name, conversation.name) == ("subagent", "main")
                assert len(searches) == (0 if unprobed else 1 if name == "large" else 2)
                text = "\n".join(call.result for step in turn.steps for call in step.calls)
                for at, probe in searches:
                    target = next(call for step in turn.steps for call in step.calls if call.key == probe.target)
                    assert text.count(probe.token) == 1 and probe.token in probe.line
                    assert "\n".join((probe.line, *probe.after)) in target.result
                    assert target.key in {call.key for step in turn.steps[:3] for call in step.calls}  # an early round
                    assert at > len(turn.steps) // 2  # late in the loop
                    recall = turn.steps[at + 1].probe
                    assert (recall.kind, recall.target, recall.fallback.tool) == ("recall", probe.target, "read")


def test_the_large_scenario_has_one_result_that_fits_and_two_that_do_not():
    """Target 3's line, 0.8 x (bound - headroom floor - min_turn_room) = 90,400: head, prompt and the
    60,000 result are under it; with the 120,000 or the 200,000 result they are over."""
    run = harness.cached("large", "claude")
    assert scripts.build("large").size["big"] == [60_000, 120_000, 200_000]
    assert targets.show_ceiling(run) == 90_400
    carrying = sorted(s.unseen_tokens for s in run.sent if s.unseen_tokens > 50_000)
    assert carrying == [75_347, 135_347, 215_347]  # head 15,101, prompt 209, and the result


def test_the_subagent_runs_beside_main_and_has_its_own_head():
    scenario = scripts.build("subagent")
    main, sub = scenario.conversations
    assert (main.agent, sub.agent) == (False, True) and sub.rounds == 150 and (main.probes, sub.probes) == (0, 2)
    order = scenario.schedule
    assert order.count("main") == main.steps and order.count("subagent") == sub.steps
    first, last = order.index("subagent"), len(order) - 1 - order[::-1].index("subagent")
    assert order[:first] == ("main",) * 5  # the prompt and four rounds, then the launch
    assert order[first:last].count("main") == 50 and order[last + 1:] == ("main",) * 4
    parent, child = cli.ClaudeCode(main), cli.ClaudeCode(sub)
    assert child.headers["X-Claude-Code-Agent-Id"] and "X-Claude-Code-Agent-Id" not in parent.headers
    assert child.headers["X-Claude-Code-Session-Id"] == parent.headers["X-Claude-Code-Session-Id"]
    assert child.system != parent.system and child.tools != parent.tools
    child.prompt(sub.turns[0])
    launch = next(call for call in main.calls if call.tool == "agent")
    assert child.history[0]["content"][1]["text"] == launch.arg  # the Agent call's prompt, unchanged
    assert scripts.claude_call_id(launch.key) in main.turns[0].steps[55].notice


def test_the_subagent_has_contextos_tools_and_cannot_launch_an_agent():
    main, sub = scripts.build("subagent", tiny=True).conversations
    names = {tool["name"] for tool in cli.ClaudeCode(sub).tools}
    assert {cli.CONTEXTO_PREFIX + name for name in cli.CONTEXTO_TOOLS} <= names and "Agent" not in names
    assert "Agent" in {tool["name"] for tool in cli.ClaudeCode(main).tools}


def test_the_mixed_scenario_has_the_round_shapes_the_other_loops_lack():
    scenario = scripts.build("mixed")
    assert scenario.size == {"rounds": 45, "single_tokens": 4_000, "parallel_tokens": 2_400, "parallel_rounds": 15,
                             "reminders": 11, "exec_rounds": 6}
    (turn,) = scenario.conversations[0].turns
    parallel = next(step for step in turn.steps if len(step.calls) == 3 and step.reminder)
    custom = next(step for step in turn.steps if step.calls and step.calls[0].tool == "exec")
    claude, codex = cli.ClaudeCode(scenario.conversations[0]), cli.Codex(scenario.conversations[0])
    _, results = claude.take([cli.claude_tool_use(call) for call in parallel.calls], parallel)
    assert [block["type"] for block in results["content"]] == ["tool_result"] * 3 + ["text"]  # one message
    assert results["content"][-1]["text"].startswith("<system-reminder>")
    added = codex.take([cli.codex_call(call) for call in parallel.calls], parallel)
    assert [item["type"] for item in added] == ["function_call"] * 3 + ["function_call_output"] * 3
    added = codex.take([cli.codex_call(call) for call in custom.calls], custom)
    assert [item["type"] for item in added] == ["custom_tool_call", "custom_tool_call_output"]
    assert added[0]["name"] == "exec" and own_call_ids(added) == set()  # an exec that is not one of Contexto's


def test_the_engine_reads_the_requests_as_claude_codes_and_codexs():
    """The shapes are the ones the engine recognises: the session start, the turn starts, the subagent."""
    main, sub = scripts.build("subagent", tiny=True).conversations
    parent, child = cli.ClaudeCode(main), cli.ClaudeCode(sub)
    parent.prompt(main.turns[0])
    child.prompt(sub.turns[0])
    for client, agent in ((parent, False), (child, True)):
        request = client.request()
        assert ANTHROPIC.session_start_seen(request["messages"]) is True
        assert claude_code_session(request, client.headers).agent is agent
    codex = cli.Codex(main)
    codex.prompt(main.turns[0])
    responses = dialect_for(cli.RESPONSES)
    items = codex.request()["input"]
    assert responses.lead_count(items) == 4 and responses.session_start_seen(items) is True
    assert RewriteEngine.preserves_input is True and cli.MESSAGES == MESSAGES


def test_a_contexto_call_is_one_the_engine_recognises_as_its_own_in_both_dialects():
    search = scripts.Call("a" * 64, "contexto_search", "0123456789abcdef", "", texts=("hit", "coverage"))
    recall = scripts.Call("b" * 64, "contexto_recall", json.dumps({"page_id": "p1", "offset": 40}), "")
    other = scripts.Call("c" * 64, "exec", "git log", "out")
    message = {"role": "assistant", "content": [cli.claude_tool_use(c) for c in (search, recall, other)]}
    assert own_call_ids([message]) == {scripts.claude_call_id(c.key) for c in (search, recall)}
    assert own_call_ids([cli.codex_call(c) for c in (search, recall, other)]) == {
        scripts.codex_call_id(c.key) for c in (search, recall)}
    assert cli.claude_tool_use(recall)["input"] == {"page_id": "p1", "offset": 40}
    assert cli.claude_tool_result(search)["content"] == [{"type": "text", "text": "hit"},
                                                         {"type": "text", "text": "coverage"}]
    assert cli.codex_output(search) == {"type": "custom_tool_call_output", "output": "hit\ncoverage",
                                        "call_id": scripts.codex_call_id(search.key)}


# The runs ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_the_clis_history_is_append_only_and_complete(name, dialect):
    """The CLI resends its whole history unmodified: nothing the engine forwards is fed back into it.
    (A run whose engine changed that history in place does not finish: see below.)"""
    run = harness.cached(name, dialect)
    assert run.scripted_tokens == SCRIPTED[name, dialect]
    assert len(run.sent) == SIZES[name, dialect]["requests"]
    for conversation in run.scenario.conversations:
        counts = [s.cli_items for s in run.sent if s.conversation == conversation.name]
        assert counts == sorted(set(counts)), "each request holds more items than the one before"
        assert len(counts) == conversation.steps
        assert run.history_tokens[conversation.name] >= run.scripted_tokens[conversation.name]


@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_the_upstream_accepts_every_request_and_no_result_is_lost(name, dialect):
    run = harness.cached(name, dialect)
    assert run.violations == {} and run.rejected == 0
    assert all(s.violations == {} and s.lost == 0 for s in run.sent)
    originals = run.originals
    assert originals["lost"] == 0
    assert originals["total"] == originals["in_view"] + originals["stored"]
    assert originals["total"] == sum(1 for c in run.scenario.conversations for call in c.calls if call.head)


@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_the_target_check_runs_on_every_scenario(name, dialect):
    """A harness error would otherwise hide behind the xfail marks of test_inturn_targets.py."""
    run = harness.cached(name, dialect)
    by_rule = targets.unmet_by_rule(run)
    assert set(by_rule) <= set(targets.RULES)
    assert targets.unmet(run) == [problem for problems in by_rule.values() for problem in problems]
    assert all(isinstance(problem, str) for problem in targets.unmet(run))
    json.dumps(run.metrics())  # the numbers are JSON-ready


@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_every_probe_is_made_and_count_tokens_gets_the_forwarded_transform(name, dialect):
    run = harness.cached(name, dialect)
    searches = [p for p in run.probes if p["kind"] == "search"]
    assert len(searches) == SIZES[name, dialect]["probes"] == len(run.probes) / 2
    assert all(p["request"] > len(run.sent) // 3 for p in searches)
    counted = run.count_tokens
    assert counted["different"] == 0 and counted["published"] == 0  # contract rule 11
    assert (counted["sent"] > 0) == (dialect == "claude")  # count_tokens is a Messages endpoint


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_the_turns_after_the_long_loop_start_under_the_bound(dialect):
    """multiturn: the turn start after the loop pages it, and every result is then in a page, byte for byte."""
    run = harness.cached("multiturn", dialect)
    after = [s for s in run.sent if s.turn >= 4]
    assert len(after) == 6 and all(s.tokens <= run.scenario.bound and not s.over_real for s in after)
    assert run.pages >= 4 and run.originals["stored"] >= 100
    if dialect == "codex":  # no in-turn paging (D63 built it for Messages only): the turn start pages the loop
        assert after[0].rewrite and not after[0].in_loop and 0 < after[0].resent < 2_000 < after[0].invalidated
    else:  # D63: the loop paged itself in turn, so the turn start after it is under its trigger
        assert any(s.rewrite and s.in_loop for s in run.sent if s.turn == 3) and not after[0].over_estimate
    before = [s for s in run.sent if s.turn < 3]
    assert all(s.forwarded_items == s.cli_items and not s.rewrite for s in before)


def test_the_subagents_requests_publish_side_rooms_and_mains_the_turn_room():
    run = harness.cached("subagent", "claude")
    rooms = run.metrics()["room"]
    assert rooms["main"]["published"] == rooms["main"]["published_at_request"] == 59
    assert rooms["subagent"]["published"] == 154  # every response with a tool call; the last one closes it
    assert rooms["subagent"]["published_at_request"] == 0 and run.events["side_room_closed"] == 1


@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_two_runs_give_identical_numbers(name, dialect):
    """On a tiny build of each scenario, which also serializes every forwarded request to prove the
    byte count."""
    scenario = scripts.build(name, tiny=True)
    first = harness.run(scenario, dialect, check_wire=True).metrics()
    assert first == harness.run(scenario, dialect).metrics()
    assert first["requests"] == len(scenario.schedule) and first["violations"] == {}


def test_a_scenario_does_not_run_on_a_dialect_it_lacks():
    with pytest.raises(ValueError, match="does not run on codex"):
        harness.run(scripts.build("subagent", tiny=True), "codex")


def test_an_engine_that_edits_the_clis_history_in_place_fails_the_run():
    """The proxy reads the CLI's own request again for the usage, and the upstream here read each of
    its items once: an engine may not change them."""
    class Scribbler(RewriteEngine):
        def __call__(self, request, headers, endpoint):
            if len(request["messages"]) > 8:
                request["messages"][3]["content"][0]["content"] = "[cleared in place]"
            return super().__call__(request, headers, endpoint)

    with pytest.raises(RuntimeError, match="changed the CLI's own history in place"):
        harness.run(scripts.build("loop120", tiny=True), "claude", engine=lambda store, config: Scribbler(store, config))


def test_the_hook_is_not_filled_in_at_this_commit_and_the_run_says_so():
    def not_filled_in(dialect, request):
        raise NotImplementedError

    run = harness.run(scripts.build("large", tiny=True), "claude", find_stubs=not_filled_in)
    assert run.stubs is None
    assert run.check_stubs() == ["stubs.find_stubs is not filled in, so no stub was checked"]
    assert stubs.StubRef("page", ("call",)) == ("page", ("call",))


# The command ------------------------------------------------------------------------------------------

def test_the_command_prints_the_tables_and_writes_the_same_numbers(tmp_path):
    out = tmp_path / "out.json"
    done = subprocess.run([sys.executable, str(REPLAY), "--scenario", "large", "--dialect", "codex", "--json", str(out)],
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    expected = json.loads(json.dumps(harness.cached("large", "codex").metrics()))
    assert json.loads(out.read_text()) == {"runs": [expected]}
    assert done.stdout.rstrip("\n") == replay.tables([expected])
    assert done.stdout.count("| large | codex |") == 4


def test_the_command_refuses_an_unknown_scenario(capsys):
    with pytest.raises(SystemExit):
        replay.main(["--scenario", "nope"])
    assert "unknown scenario: nope" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        replay.main(["--scenario", "subagent", "--dialect", "codex"])
