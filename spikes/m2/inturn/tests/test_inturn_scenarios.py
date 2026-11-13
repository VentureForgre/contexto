"""The scenarios and the harness: what is true at v0.5.24 and stays true after D63.

The sizes pinned here are the ones docs/research/M2-in-turn-paging-baseline.md gives. Nothing here
asserts a number in-turn paging will change (over_bound counts, the largest request, the rooms): those
are in the research note, and what they must become is in test_inturn_targets.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from inturn import cli, harness, replay, scripts, stubs, targets  # noqa: E402

from contexto.rewrite import MESSAGES  # noqa: E402
from contexto.store import SessionStore  # noqa: E402

CASES = [(name, dialect) for name in scripts.BUILDERS for dialect in scripts.build(name, quick=True).dialects]
REPLAY = Path(__file__).resolve().parents[1] / "replay.py"

# harness.script_size at the sizes the tests run: requests, tool rounds, and the tool results in the
# fake upstream's tokens (text at 2.2 bytes a token, images by pixels).
SIZES = {
    ("loop120", "claude"): {"requests": 121, "rounds": 120, "result_tokens": 772_440, "largest_result": 6_437, "images": 0},
    ("loop120", "codex"): {"requests": 121, "rounds": 120, "result_tokens": 772_561, "largest_result": 6_439, "images": 0},
    ("large", "claude"): {"requests": 13, "rounds": 12, "result_tokens": 387_644, "largest_result": 200_037, "images": 0},
    ("large", "codex"): {"requests": 13, "rounds": 12, "result_tokens": 387_656, "largest_result": 200_038, "images": 0},
    ("small", "claude"): {"requests": 261, "rounds": 260, "result_tokens": 113_620, "largest_result": 437, "images": 0},
    ("small", "codex"): {"requests": 261, "rounds": 260, "result_tokens": 113_880, "largest_result": 438, "images": 0},
    ("subagent", "claude"): {"requests": 210, "rounds": 208, "result_tokens": 826_216, "largest_result": 5_037, "images": 0},
    ("images", "claude"): {"requests": 101, "rounds": 100, "result_tokens": 360_787, "largest_result": 5_313, "images": 68},
    ("images", "codex"): {"requests": 101, "rounds": 100, "result_tokens": 361_090, "largest_result": 5_317, "images": 68},
    ("multiturn", "claude"): {"requests": 116, "rounds": 110, "result_tokens": 519_070, "largest_result": 5_037, "images": 0},
    ("multiturn", "codex"): {"requests": 116, "rounds": 110, "result_tokens": 519_186, "largest_result": 5_039, "images": 0},
}
FULL_SMALL = {
    "claude": {"requests": 1_501, "rounds": 1_500, "result_tokens": 655_500, "largest_result": 437, "images": 0},
    "codex": {"requests": 1_501, "rounds": 1_500, "result_tokens": 657_000, "largest_result": 438, "images": 0},
}
# Each CLI's whole history with its head, in the fake upstream's tokens: what the last request would
# be with nothing paged. It depends on the script alone, so in-turn paging does not change it.
HISTORY = {
    ("loop120", "claude"): {"main": 810_930},
    ("loop120", "codex"): {"main": 810_764},
    ("large", "claude"): {"main": 405_864},
    ("large", "codex"): {"main": 404_148},
    ("small", "claude"): {"main": 167_466},
    ("small", "codex"): {"main": 165_418},
    ("subagent", "claude"): {"main": 97_916, "subagent": 793_482},
    ("images", "claude"): {"main": 400_251},
    ("images", "codex"): {"main": 398_864},
    ("multiturn", "claude"): {"main": 558_219},
    ("multiturn", "codex"): {"main": 558_140},
}


# The scripts ------------------------------------------------------------------------------------------

def test_every_scenario_runs_on_the_dialects_the_record_names():
    assert CASES == list(SIZES) == list(HISTORY)
    assert [name for name in scripts.BUILDERS if scripts.build(name, quick=True).dialects == ("claude",)] == ["subagent"]


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


def test_the_large_scenario_has_three_results_larger_than_a_turns_room():
    sizes = scripts.build("large").size
    assert sizes["big"] == [60_000, 120_000, 200_000]
    room = scripts.BOUND - 25_000 - 15_000  # the bound less the headroom floor and a head
    assert sum(1 for tokens in sizes["big"] if tokens > room / 2) == 3 and sizes["big"][-1] > scripts.BOUND


def test_the_subagent_runs_beside_main_and_has_its_own_head():
    scenario = scripts.build("subagent")
    main, sub = scenario.conversations
    assert (main.agent, sub.agent) == (False, True) and sub.rounds == 150
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


def test_the_engine_reads_the_requests_as_claude_codes_and_codexs():
    """The shapes are the ones the engine recognises: the session start, the turn starts, the subagent."""
    from contexto.rewrite import ANTHROPIC, RewriteEngine, claude_code_session, dialect_for

    main, sub = scripts.build("subagent", quick=True).conversations
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


# The runs ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_the_clis_history_is_append_only_and_complete(name, dialect):
    """The CLI resends its whole history unmodified: nothing the engine forwards is fed back into it."""
    run = harness.cached(name, dialect)
    assert run.history_digests_stable
    assert run.history_tokens == HISTORY[name, dialect]
    assert len(run.sent) == SIZES[name, dialect]["requests"]
    for conversation in run.scenario.conversations:
        counts = [s.cli_items for s in run.sent if s.conversation == conversation.name]
        assert counts == sorted(set(counts)), "each request holds more items than the one before"
        assert len(counts) == conversation.steps


@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_the_upstream_accepts_every_request_and_no_result_is_lost(name, dialect):
    run = harness.cached(name, dialect)
    assert run.violations == {} and run.rejected == 0
    assert all(s.violations == {} for s in run.sent)
    originals = run.originals
    assert originals["lost"] == 0
    assert originals["total"] == originals["in_view"] + originals["stored"]
    assert originals["total"] == sum(1 for c in run.scenario.conversations for call in c.calls if call.head)


@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_the_target_check_runs_on_every_scenario(name, dialect):
    """A harness error would otherwise hide behind the xfail marks of test_inturn_targets.py."""
    run = harness.cached(name, dialect)
    problems = targets.unmet(run)
    assert isinstance(problems, list) and all(isinstance(p, str) for p in problems)
    json.dumps(run.metrics())  # the numbers are JSON-ready


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_the_turns_after_the_long_loop_start_under_the_bound(dialect):
    """multiturn: the turn start after the loop pages it, and every result is then in a page."""
    run = harness.cached("multiturn", dialect)
    after = [s for s in run.sent if s.turn >= 4]
    assert len(after) == 6 and all(s.tokens <= run.scenario.bound and not s.over_real for s in after)
    assert run.pages >= 4 and run.originals["stored"] >= 100
    before = [s for s in run.sent if s.turn < 3]
    assert all(s.forwarded_items == s.cli_items and s.resent is None for s in before)


def test_the_subagents_requests_publish_side_rooms_and_mains_the_turn_room():
    run = harness.cached("subagent", "claude")
    rooms = run.metrics()["room"]
    assert rooms["main"]["published"] == run.scenario.conversations[0].steps
    assert rooms["subagent"]["published"] == 150  # every response with a tool call; the last one closes it
    assert run.events["side_room_closed"] == 1
    assert all(s.room_at_request is None for s in run.sent if s.conversation == "subagent")


@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_two_runs_give_identical_numbers(name, dialect):
    """On a small build of each scenario, which also serializes every forwarded request to prove the
    byte count."""
    scenario = scripts.build(name, quick=True)
    first = harness.run(scenario, dialect, check_wire=True).metrics()
    assert first == harness.run(scenario, dialect).metrics()
    assert max(first["history_tokens"].values()) > first["bound"], "even the small build passes the bound"


def test_a_scenario_does_not_run_on_a_dialect_it_lacks():
    with pytest.raises(ValueError, match="does not run on codex"):
        harness.run(scripts.build("subagent", quick=True), "codex")


# The stub hook ----------------------------------------------------------------------------------------

def not_filled_in(dialect: str, request: dict) -> list[stubs.StubRef]:
    """stubs.find_stubs as it is at v0.5.24, so these tests hold after D63 fills the real one in."""
    raise NotImplementedError


def test_without_the_hook_the_stub_check_is_reported_as_not_run():
    run = harness.run(scripts.build("large"), "claude", find_stubs=not_filled_in)
    assert run.stubs is None
    assert run.check_stubs() == ["stubs.find_stubs is not filled in, so no stub was checked"]
    assert run.metrics() == {**harness.cached("large", "claude").metrics(), "stubs": None}


def test_a_hook_that_finds_nothing_does_not_pass():
    run = harness.run(scripts.build("large"), "codex", find_stubs=lambda dialect, request: [])
    assert run.stubs == {"found": 0, "unreachable": 0}
    assert run.check_stubs() == ["no stub was found in any forwarded request"]


def test_a_stub_is_reachable_only_when_its_page_holds_the_original(tmp_path):
    conversation = scripts.loop120(rounds=2, result_tokens=80).conversations[0]
    one, two = conversation.calls
    originals = {scripts.claude_call_id(call.key): call for call in (one, two)}
    store = SessionStore(tmp_path, "s1")
    turn = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [cli.claude_tool_use(one)]},
            {"role": "user", "content": [cli.claude_tool_result(one)]}]
    (page_id,) = store.page_out([turn], 1)
    first, second = (scripts.claude_call_id(call.key) for call in (one, two))
    assert harness._reachable(store, stubs.StubRef(page_id, (first,)), originals)
    assert harness._reachable(store, stubs.StubRef(page_id), originals)
    assert not harness._reachable(store, stubs.StubRef(page_id, (second,)), originals)  # another call's result
    assert not harness._reachable(store, stubs.StubRef(page_id, ("toolu_unknown",)), originals)
    assert not harness._reachable(store, stubs.StubRef("0" * len(page_id), (first,)), originals)


def test_a_stub_that_names_a_missing_page_fails_the_check():
    def missing(dialect, request):
        return [stubs.StubRef("f" * 64, ())]
    run = harness.run(scripts.build("multiturn", quick=True), "claude", find_stubs=missing)
    assert run.stubs == {"found": 1, "unreachable": 1}
    assert run.check_stubs() == ["1 stubs name a page that is missing or lacks the original"]


def test_the_targets_pass_on_a_run_that_meets_them():
    scenario = scripts.loop120(rounds=2, result_tokens=200)
    run = harness.run(scenario, "claude", find_stubs=not_filled_in)
    assert targets.unmet(run) == ["stubs.find_stubs is not filled in, so no stub was checked"]
    run.stubs = {"found": 1, "unreachable": 0}
    assert targets.unmet(run) == []
    run.sent[-1].room = 0
    run.sent[-1].loop_tokens = harness.LOOP_START + 1
    run.originals["lost"] = 2
    run.violations = {"thinking_binding": 1}
    run.rejected = 1
    assert len(targets.unmet(run)) == 3


# The command ------------------------------------------------------------------------------------------

def test_the_command_prints_the_tables_and_writes_the_same_numbers(tmp_path):
    out = tmp_path / "out.json"
    done = subprocess.run([sys.executable, str(REPLAY), "--scenario", "large", "--dialect", "codex", "--json", str(out)],
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    expected = json.loads(json.dumps(harness.cached("large", "codex").metrics()))
    assert json.loads(out.read_text()) == {"runs": [expected]}
    assert done.stdout.rstrip("\n") == replay.tables([expected])
    assert done.stdout.count("| large | codex |") == 3


def test_the_command_refuses_an_unknown_scenario(capsys):
    with pytest.raises(SystemExit):
        replay.main(["--scenario", "nope"])
    assert "unknown scenario: nope" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        replay.main(["--scenario", "subagent", "--dialect", "codex"])
