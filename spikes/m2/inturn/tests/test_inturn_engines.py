"""The targets cannot be met by a wrong engine, and can be met by a right one.

``toy.ToyPager`` is a small in-turn pager written for these tests (it is not the D63 design). Run
through every scenario it meets every target, so the targets do not contradict each other. Each of its
defects makes it wrong in one way, and the target written for that way fails it:

- ``junk_page``: every stub names one page that holds none of the results;
- ``strip_all``: all thinking is stripped on every request;
- ``stub_on_arrival``: a result is stubbed in the request that first carries it, so the model never reads it;
- ``rewrite_always``: the prefix is rewritten on every request;
- ``no_room``: no recall room is published;
- ``room_one``: every room published is 1 token;
- ``lying_room``: every room published is 100,000 tokens (reviewer A's ``lying_room``);
- ``no_stub_text``: after the first page, a stubbed result is replaced by text that names nothing
  (reviewer A's ``no_stub_text``);
- ``show_once``: every 4th request pages every result it is not the first to carry (reviewer A);
- ``in_place``: the CLI's own oldest whole result is cleared in place for the send and restored after
  (reviewer A's ``InPlace``);
- ``inflate``: once in a run, a second rewrite right after a batch, with an estimate of 999,999 published
  for it (reviewer A's inflating engine);
- ``silent_older``: from each turn start on, every earlier turn's result is ``[cleared]``, its rounds paged,
  its calls and prompt kept (reviewer A's ``SilentOlder``);
- ``drop_older_stubs``: from each turn start on, an earlier turn's stub becomes ``[result removed]``
  (reviewer B);
- ``drop_older_rounds``: from each turn start on, the rounds of earlier turns still in view are removed
  and paged, and nothing names the page;
- ``trim_older_prompts``, ``split_older_prompts``: the same, with each earlier prompt trimmed by one
  character or split into two blocks (reviewer A's ``trim`` and ``split`` at 304e598e);
- ``drop_older_turns``: every earlier turn removed whole and paged, with no index (reviewer A's
  ``wholeturns``).

Two more are the toy with other batch lines: reviewer A's ``trickle`` (100,000 down to 75,000) and
reviewer B's (112,500 down to 87,500). Both page in batches of a sixth of the bound or so and re-send
more than the loop's own tokens.

Two are right and pass: ``noted_older_rounds`` (``drop_older_rounds`` with a line naming the page), and
the toy with every ``StubRef`` naming its page as a whole (empty ``call_ids``).
"""

from __future__ import annotations

import copy
import functools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from inturn import cli, harness, scripts, targets, toy  # noqa: E402
from inturn.stubs import StubRef  # noqa: E402
from inturn.upstream import FakeUpstream  # noqa: E402

from contexto import mcp  # noqa: E402
from contexto.store import SessionStore  # noqa: E402

CASES = [(name, dialect) for name in scripts.BUILDERS for dialect in scripts.build(name, quick=True).dialects]


@functools.cache
def toy_run(name: str, dialect: str, defect: str = "", lines: tuple[int, int] = (toy.HIGH, toy.LOW),
            quick: bool = True) -> harness.Run:
    high, low = lines
    return harness.run(scripts.build(name, quick=quick), dialect, find_stubs=toy.find_stubs, check_wire=True,
                       engine=functools.partial(toy.ToyPager, defect=defect, high=high, low=low))


# A right engine --------------------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "dialect"), CASES)
def test_a_pager_that_follows_the_rules_meets_every_target(name, dialect):
    run = toy_run(name, dialect)
    assert targets.unmet_by_rule(run) == {}
    m = run.metrics()
    assert m["largest"]["tokens"] <= m["bound"] < max(run.scripted_tokens.values())
    assert m["stubs"]["found"] >= m["pages"] > 0 and m["stubs"]["unreachable"] == 0
    assert m["originals"]["stored"] > 0 and m["originals"]["lost"] == 0
    searches = [p for p in run.probes if p["kind"] == "search"]
    assert searches and all(p["outcome"] in ("in_view", "paged") for p in searches)
    if name != "large":  # its three large results are handled on arrival: no batch, so no rewrite
        assert 1 <= m["rewrites"]["in_loop"] == m["thinking"]["newest_missing"] <= 3
        assert m["rewrites"]["resent_any"] <= m["rewrites"]["resent"] < m["rewrites"]["invalidated"] / 4
        if dialect == "codex":  # automatic prefix caching: one figure
            assert m["rewrites"]["resent_any"] == m["rewrites"]["resent"]
        around = m["info"].get("thinking_kept_around_strip", 0) + m["info"].get("reasoning_kept_around_drop", 0)
        assert around > 0  # thinking kept before and after a stripped range: valid by the research, not confirmed live
        assert {p["outcome"] for p in run.probes if p["kind"] == "recall"} == {"returned"}


def test_two_runs_of_the_toy_pager_give_identical_numbers():
    """Reviewer B: search breaks ties on page ids, which are keyed by the store's random key. Here the
    probes reach pages, so a tie broken differently in another run would change an outcome."""
    run = harness.run(scripts.build("loop120", quick=True), "claude", find_stubs=toy.find_stubs, engine=toy.ToyPager)
    assert {p["outcome"] for p in run.probes} == {"paged", "returned"}
    assert run.metrics() == toy_run("loop120", "claude").metrics()


def test_the_large_scenario_shows_the_result_that_fits_and_stubs_the_two_that_do_not():
    run = toy_run("large", "claude")
    carrying = {s.unseen_tokens: s.stubbed_on_arrival for s in run.sent if s.unseen_tokens > 50_000}
    assert carrying == {75_347: 0, 135_347: 1, 215_347: 1}  # the 60,000 result is whole in its first request
    assert run.metrics()["arrival"] == {"requests_with_a_stubbed_new_result": 2, "results_stubbed_on_arrival": 2}
    assert max(s.tokens for s in run.sent) < 90_400


# Wrong engines ---------------------------------------------------------------------------------------

WRONG = [
    ("junk_page", {"storage", "recall"}),  # nothing can be read back, so the searches find nothing either
    ("strip_all", {"violations", "thinking"}),
    ("stub_on_arrival", {"seen_first"}),
    ("rewrite_always", {"cache"}),
    ("no_room", {"room"}),
    ("room_one", {"room", "recall"}),  # with 1 token of room every search is refused
    ("no_stub_text", {"storage"}),
    ("show_once", {"cache"}),
    ("lying_room", {"room"}),
]
TRICKLE = (100_000, 75_000)  # reviewer A
NARROW = (112_500, 87_500)  # reviewer B
HIGHER = (110_000, 70_000)  # a batch leaves the request over the pressure line, so a second one is held to the in-a-row rule
OLDER = [("silent_older", 51), ("drop_older_stubs", 31), ("drop_older_rounds", 51), ("trim_older_prompts", 51),
         ("split_older_prompts", 51), ("drop_older_turns", 51), ("fake_omission", 51), ("fake_omission_split", 51),
         ("fake_omission_trim", 51), ("fake_omission_trimall", 51), ("fake_omission_doc", 51), ("omit_turns_n0", 51),
         ("omit_turns_n99", 51)]  # requests failed, quick build


@pytest.mark.parametrize(("defect", "rules"), WRONG)
def test_each_wrong_engine_fails_the_rule_written_for_it_and_no_other(defect, rules):
    assert set(targets.unmet_by_rule(toy_run("loop120", "claude", defect))) == rules


@pytest.mark.parametrize(("defect", "rules"), [w for w in WRONG if w[0] in ("junk_page", "strip_all", "stub_on_arrival",
                                                                         "no_stub_text", "show_once")])
def test_the_wrong_engines_whose_shapes_differ_on_codex_fail_there_too(defect, rules):
    """Stored items, reasoning items and outputs are other shapes on the Responses API, and its cache is
    another model; the room rules read only numbers."""
    assert set(targets.unmet_by_rule(toy_run("loop120", "codex", defect))) == rules


@pytest.mark.parametrize(("lines", "quick", "dialect"), [(NARROW, True, "claude"), (NARROW, True, "codex"),
                                                        (TRICKLE, False, "claude")])
def test_a_pager_that_batches_a_sixth_of_the_bound_at_a_time_re_sends_more_than_the_loop(lines, quick, dialect):
    """Reviewer A's ``trickle`` passes the quick build (its loop is too short for the cost to show) and
    fails at the size the target tests run (``replay.py --engine toy`` and the note give both dialects);
    reviewer B's fails at either size."""
    run = toy_run("loop120", dialect, lines=lines, quick=quick)
    by_rule = targets.unmet_by_rule(run)
    assert set(by_rule) == {"cache"}
    assert by_rule["cache"][0].startswith("main, turn 1: its rewrites re-sent ")
    m = run.metrics()["rewrites"]
    assert m["resent"] > m["loop_tokens"] and m["loop_resent_over_ceiling"] == 1
    reference = toy_run("loop120", dialect, quick=quick).metrics()["rewrites"]
    assert m["resent"] > 4 * reference["resent"]


@pytest.mark.parametrize(("defect", "requests"), OLDER)
def test_an_engine_that_takes_earlier_turns_results_out_of_view_without_naming_them_fails(defect, requests):
    """Rule 3 for earlier turns. ``silent_older`` and ``drop_older_stubs`` keep the calls in view, so no
    exemption applies; the others remove the calls (and change, split or drop the prompts), and no page id
    in the text the model reads reaches them. Each pages what it takes out, so no result is lost."""
    run = toy_run("multiturn", "claude", defect)
    assert targets.unmet_by_rule(run) == {"storage": [
        f"{requests} requests left out a tool result that no stub, fold note or page id in that request accounts for "
        f"(up to {46 if defect != 'drop_older_stubs' else 32} results at once)"]}
    assert run.metrics()["requests_with_a_lost_result"] == 0 and run.originals["lost"] == 0


def test_rounds_of_an_earlier_turn_may_leave_when_a_page_id_in_the_request_reaches_them():
    """``noted_older_rounds``: the same rounds removed, with a line naming their page in the request."""
    for quick in (True, False):
        run = toy_run("multiturn", "claude", "noted_older_rounds", quick=quick)
        assert targets.unmet_by_rule(run) == {}
        assert run.originals["stored"] > run.originals["in_view"] > 0


def test_the_v0_5_24_turn_start_rotation_is_accounted_for_by_its_turn_pages():
    """At v0.5.24 the turn start after ``multiturn``'s loop pages every earlier turn whole, prompt
    included, and the index names the 4 turn pages by id, which reaches every result."""
    run = harness.cached("multiturn", "claude")
    assert run.metrics()["requests_with_an_unnamed_result"] == 0 and run.originals["lost"] == 0
    assert [s.turn for s in run.sent if s.lost] == []


def test_an_engine_that_inflates_its_estimate_to_rewrite_twice_in_a_row_fails():
    """Reviewer A's L1: the second rewrite publishes an estimate of 999,999; it counts as 1.02 x the
    upstream's count of the request, which leaves it under the line."""
    for quick in (True, False):
        assert targets.unmet_by_rule(toy_run("loop120", "claude", lines=HIGHER, quick=quick)) == {}
        run = toy_run("loop120", "claude", "inflate", lines=HIGHER, quick=quick)
        assert targets.unmet_by_rule(run) == {"cache": [
            "main: 1 requests rewrote the prefix right after a rewrite, though the request would have kept "
            "min_turn_room without it"]}
        (pair,) = [(a, b) for a, b in zip(run.sent, run.sent[1:]) if a.rewrite and b.rewrite]
        assert pair[1].estimate_before == toy.INFLATED
        assert targets.pressure_line(run) < pair[0].tokens + pair[1].appended_tokens < 100_000


def test_an_engine_that_edits_the_clis_objects_for_the_send_and_restores_them_fails():
    """Reviewer A's ``InPlace``: the model saw a cleared result, and every check after the usage reads
    the object restored. ``check_wire``, which the target tests run with, digests the CLI's history
    right after the engine's call."""
    engine = functools.partial(toy.ToyPager, defect="in_place")
    with pytest.raises(RuntimeError, match=r"main: the engine changed the CLI's own history in place \(request 4\)"):
        harness.run(scripts.build("loop120", quick=True), "claude", find_stubs=toy.find_stubs, engine=engine,
                    check_wire=True)
    run = harness.run(scripts.build("loop120", quick=True), "claude", find_stubs=toy.find_stubs, engine=engine)
    assert targets.unmet_by_rule(run) == {}  # without the check it passes: what reviewer A saw


@pytest.mark.parametrize(("defect", "rules"), [("no_room", {"room"}), ("room_one", {"room", "recall"})])
def test_a_subagent_without_a_usable_side_room_fails(defect, rules):
    by_rule = targets.unmet_by_rule(toy_run("subagent", "claude", defect))
    assert set(by_rule) == rules
    assert any(problem.startswith("subagent: ") for problem in by_rule["room"])


def test_what_each_wrong_engine_is_told():
    said = {defect: targets.unmet_by_rule(toy_run("loop120", "claude", defect)) for defect, _ in WRONG}
    assert said["junk_page"]["storage"][0].endswith("stubs do not lead to their originals, byte for byte")
    assert "that no stub, fold note or page id in that request accounts for" in said["junk_page"]["storage"][1]
    assert "neither whole in the request nor byte for byte in a store page" in said["junk_page"]["storage"][2]
    assert said["no_stub_text"]["storage"] == ["22 requests left out a tool result that no stub, fold note or page id "
                                               "in that request accounts for (up to 22 results at once)"]
    assert said["show_once"]["cache"] == ["main: 8 loop requests rewrote the prefix though without the rewrite they "
                                          "came to at most 52,251 tokens, not over 67,800"]
    assert said["lying_room"]["room"] == [
        "main: the request-time room was more than bound - headroom_floor - 0.9 x the request's tokens on 33 requests",
        "main: the response-time room was more than bound - (input + output) - headroom_floor on 33 requests"]
    assert "thinking_removed_outside_rewrite" in said["strip_all"]["violations"][0]
    assert "the newest response's thinking was missing on 34 requests" in said["strip_all"]["thinking"][0]
    assert "were stubbed in the request that first carried them" in said["stub_on_arrival"]["seen_first"][0]
    assert said["rewrite_always"]["cache"][0].startswith("main, turn 1: 33 prefix rewrites")
    assert "rewrote the prefix though without the rewrite they came to at most" in said["rewrite_always"]["cache"][1]
    assert "rewrote the prefix right after a rewrite" in said["rewrite_always"]["cache"][2]
    assert said["no_room"]["room"] == ["main: no room was published before 34 loop requests and after 34"]
    assert "the request-time room was under 12,000" in said["room_one"]["room"][0]
    codex = targets.unmet_by_rule(toy_run("loop120", "codex", "strip_all"))
    assert "reasoning_removed_outside_rewrite" in codex["violations"][0]


def test_stubbing_on_arrival_is_caught_even_where_some_results_may_be():
    by_rule = targets.unmet_by_rule(toy_run("large", "claude", "stub_on_arrival"))
    assert list(by_rule) == ["seen_first"]
    # The two largest results may be stubbed on arrival. The other ten had to be shown, and so had the
    # search's reply and the result of the read the model made after it.
    assert by_rule["seen_first"][0].startswith("12 results in 12 requests")


def nameless(dialect, request):
    """``toy.find_stubs`` with every reference naming its page as a whole, as a fold note does."""
    return [StubRef(ref.page_id, ()) for ref in toy.find_stubs(dialect, request)]


def test_a_reference_to_a_page_as_a_whole_covers_what_the_page_reaches_and_nothing_more():
    """A ``StubRef`` with empty ``call_ids``: reviewer A's first wrong engine, a stub that names a page
    and no call, now passes when its page holds the result, and fails when it does not."""
    run = harness.run(scripts.build("loop120", quick=True), "claude", find_stubs=nameless, engine=toy.ToyPager)
    assert targets.unmet_by_rule(run) == {} and run.stubs["unreachable"] == 0
    assert run.metrics()["stubs"] == toy_run("loop120", "claude").metrics()["stubs"]
    junk = harness.run(scripts.build("loop120", quick=True), "claude", find_stubs=nameless,
                       engine=functools.partial(toy.ToyPager, defect="junk_page"))
    by_rule = targets.unmet_by_rule(junk)
    assert set(by_rule) == {"storage", "recall"} and junk.stubs["found"] == junk.stubs["unreachable"] == 1
    assert "that no stub, fold note or page id in that request accounts for" in by_rule["storage"][1]


# A stub's original, byte for byte ----------------------------------------------------------------------

class Stored:
    """A store with one scripted round, and the checker the harness uses."""

    def __init__(self, tmp_path, dialect: str = "claude", find=toy.find_stubs) -> None:
        self.dialect, self.store, self.upstream = dialect, SessionStore(tmp_path, "s1"), FakeUpstream()
        self.calls = scripts.loop120(rounds=3, result_tokens=120).conversations[0].calls
        self.ids = [cli.CLIS[dialect].call_id(call.key) for call in self.calls]
        self.originals = {call_id: self.upstream.results(dialect, self.round(call)[-1])[0][1]
                          for call_id, call in zip(self.ids, self.calls)}
        self.own: set[str] = set()
        self.stubs = harness.Stubs(self.store, self.upstream, dialect, find, self.originals, lambda: self.own)
        self.turns = 0

    def round(self, call, content=None) -> list[dict]:
        """The call and its result as the CLI holds them; ``content`` replaces the result's content."""
        if self.dialect == "claude":
            result = cli.claude_tool_result(call)
            if content is not None:
                result = {**result, "content": content}
            return [{"role": "assistant", "content": [cli.claude_tool_use(call)]}, {"role": "user", "content": [result]}]
        result = cli.codex_output(call)
        return [cli.codex_call(call), result if content is None else {**result, "output": content}]

    def page(self, messages: list[dict]) -> str:
        (page_id,) = self.store.page_out([messages], self.turns)
        self.turns += 1
        return page_id


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_a_stub_is_reachable_only_when_its_page_holds_the_whole_original(tmp_path, dialect):
    s = Stored(tmp_path, dialect)
    one, two = s.calls[:2]
    page = s.page(s.round(one))
    assert s.stubs.reachable(StubRef(page, (s.ids[0],)))
    assert s.stubs.reachable(StubRef(page, ()))  # the page as a whole: it reaches a delivered result
    assert not s.stubs.reachable(StubRef(s.page([{"role": "user", "content": "not a result"}]), ()))
    assert not s.stubs.reachable(StubRef(page, (s.ids[1],)))  # another call's result
    assert not s.stubs.reachable(StubRef(page, (s.ids[0], s.ids[1])))  # one of two
    assert not s.stubs.reachable(StubRef(page, ("toolu_unknown",)))
    assert not s.stubs.reachable(StubRef("0" * len(page), (s.ids[0],)))  # no such page
    assert not s.stubs.reachable(StubRef("../meta", (s.ids[0],)))
    both = s.page([*s.round(one), *s.round(two)])
    assert s.stubs.reachable(StubRef(both, (s.ids[0], s.ids[1])))


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_a_page_with_the_results_first_and_last_line_is_not_the_original(tmp_path, dialect):
    """Reviewer A's second wrong engine and reviewer B's check: the page keeps the two marker lines
    around a placeholder. Nor does a result that differs by one character count."""
    s = Stored(tmp_path, dialect)
    one = s.calls[0]
    cut = s.page(s.round(one, f"{one.head}\n[the middle was not stored]\n{one.tail}"))
    assert not s.stubs.reachable(StubRef(cut, (s.ids[0],)))
    almost = s.page(s.round(one, one.result[:-1] + "X"))
    assert not s.stubs.reachable(StubRef(almost, (s.ids[0],)))
    wrapped = s.page(s.round(one, [{"type": "text" if dialect == "claude" else "input_text", "text": one.result}]))
    assert not s.stubs.reachable(StubRef(wrapped, (s.ids[0],)))  # the same text in another shape: not byte for byte
    assert s.stubs.reachable(StubRef(s.page(s.round(one)), (s.ids[0],)))


def test_a_stub_inside_a_page_is_followed_to_the_page_that_holds_the_original(tmp_path):
    """A page of several rounds keeps a result that was already stubbed as its stub."""
    s = Stored(tmp_path)
    one, two = s.calls[:2]
    original = s.page(s.round(one))
    rounds = s.page([*s.round(one, toy.STUB.format(page=original, call=s.ids[0])), *s.round(two)])
    assert s.stubs.reachable(StubRef(rounds, (s.ids[0], s.ids[1])))
    junk = s.page([{"role": "user", "content": "not the result"}])
    astray = s.page(s.round(one, toy.STUB.format(page=junk, call=s.ids[0])) + [{"role": "user", "content": "x"}])
    assert not s.stubs.reachable(StubRef(astray, (s.ids[0],)))
    other = s.page(s.round(one, toy.STUB.format(page=original, call=s.ids[1])) + [{"role": "user", "content": "y"}])
    assert not s.stubs.reachable(StubRef(other, (s.ids[0],)))  # the inner stub stands for another call


FOLD = "[folded rounds: page "


def folds(dialect, request):
    """``toy.find_stubs``, and a fold note ``[folded rounds: page <id>]`` as a reference to its page as a whole."""
    found = toy.find_stubs(dialect, request)
    for message in request.get("messages") or []:
        for block in message.get("content") if isinstance(message.get("content"), list) else []:
            text = block.get("content") if block.get("type") == "tool_result" else block.get("text")
            if isinstance(text, str) and text.startswith(FOLD):
                found.append(StubRef(text[len(FOLD):].rstrip("]"), ()))
    return found


def test_a_fold_note_inside_a_page_leads_to_every_call_its_page_reaches(tmp_path):
    """A rounds page whose results were folded before keeps the fold note; following it reaches them."""
    s = Stored(tmp_path, find=folds)
    one, two, three = s.calls
    rounds = s.page([*s.round(one), *s.round(two)])
    outer = s.page([*s.round(one, FOLD + rounds + "]"), *s.round(three)])
    assert s.stubs.reach(outer) == {s.ids[0], s.ids[1], s.ids[2]}  # the note's page reaches two as well
    assert s.stubs.reachable(StubRef(outer, (s.ids[0], s.ids[2])))
    junk = s.page([{"role": "user", "content": "nothing"}])
    astray = s.page([*s.round(one, FOLD + junk + "]"), {"role": "user", "content": "z"}])
    assert s.stubs.reach(astray) == set()


def test_a_chain_of_stubs_is_followed_only_so_far(tmp_path):
    s = Stored(tmp_path)
    one = s.calls[0]

    def chain(length: int) -> str:
        page = s.page([*s.round(one), {"role": "user", "content": f"chain of {length}"}])
        for n in range(length):
            page = s.page([*s.round(one, toy.STUB.format(page=page, call=s.ids[0])),
                           {"role": "user", "content": f"link {n} of {length}"}])
        return page

    assert s.stubs.reachable(StubRef(chain(harness.STUB_DEPTH), (s.ids[0],)))
    assert not s.stubs.reachable(StubRef(chain(harness.STUB_DEPTH + 1), (s.ids[0],)))


def test_a_stub_without_a_page_is_allowed_only_for_contextos_own_search_recall_and_state(tmp_path):
    s = Stored(tmp_path)
    search = scripts.Call("a" * 64, "contexto_search", "0123456789abcdef", "")
    own = cli.claude_call_id(search.key)
    s.originals[own] = b"anything"
    s.own.add(own)
    assert s.stubs.reachable(StubRef(None, (own,)))
    assert not s.stubs.reachable(StubRef(None, (s.ids[0],)))  # a scripted Read: its result has to be stored
    assert not s.stubs.reachable(StubRef(None, (own, s.ids[0])))
    assert not s.stubs.reachable(StubRef(None, ()))


def test_a_fold_note_names_each_call_its_page_leads_to_and_no_other(tmp_path):
    """A fold note is a ``StubRef`` for several calls at once, with the rounds page. A call counts as
    named when its own original is reachable from that page."""
    s = Stored(tmp_path)
    one, two, three = s.calls
    rounds = s.page([*s.round(one), *s.round(two)])
    assert s.stubs.names([StubRef(rounds, (s.ids[0], s.ids[1]))]) == {s.ids[0], s.ids[1]}
    assert s.stubs.names([StubRef(rounds, (s.ids[0], s.ids[1], s.ids[2]))]) == {s.ids[0], s.ids[1]}
    assert s.stubs.names([StubRef(rounds, ())]) == {s.ids[0], s.ids[1]}  # the page as a whole
    assert s.stubs.names([StubRef(s.page([{"role": "user", "content": "nothing"}]), ())]) == set()
    assert s.stubs.names([StubRef(s.page(s.round(three)), (s.ids[2],)), StubRef(rounds, (s.ids[1],))]) == {
        s.ids[1], s.ids[2]}


def test_a_result_left_out_without_a_stub_in_that_request_fails_though_a_page_holds_it():
    """Reviewer A's ``no_stub_text``: the pages are written and search and recall still work, but the
    model sees no stub and no page id, so nothing tells it the result is there to get."""
    run = toy_run("loop120", "claude", "no_stub_text")
    assert run.metrics()["requests_with_a_lost_result"] == 0
    assert run.metrics()["requests_with_an_unnamed_result"] == 22
    assert run.originals["lost"] == 0 and run.originals["stored"] > 0


def test_the_harness_takes_its_list_of_own_calls_from_the_engines_rule():
    """The search and recall the harness makes late in a loop are Contexto's own calls; a stub for one of
    them may name no page."""
    run = toy_run("loop120", "claude")
    assert {p["outcome"] for p in run.probes} == {"paged", "returned"}
    play = harness._Play(scripts.build("loop120", tiny=True), "claude", SessionStore.__new__(SessionStore), None,
                         None, lambda store, config: None, False)
    search = scripts.Call("a" * 64, "contexto_search", "0123456789abcdef", "")
    scripted = play.actors["main"].conversation.calls[0]
    play.actors["main"].cli.history.append({"role": "assistant", "content": [cli.claude_tool_use(search),
                                                                             cli.claude_tool_use(scripted)]})
    assert play.own() == {cli.claude_call_id(search.key)}


# Each rule's line --------------------------------------------------------------------------------------

@functools.cache
def _base() -> harness.Run:
    run = harness.run(scripts.build("loop120", tiny=True), "claude", find_stubs=lambda dialect, request: [])
    run.stubs = {"found": 1, "unreachable": 0}  # as if one stub had been found and checked
    # v0.5.24's first estimate, before any usage, is 11% under the upstream's count, so its first
    # request-time room is 202 tokens over the truth line (the research note): as if it were not.
    first = run.sent[0]
    assert run.truth(first)[0] < first.room_at_request <= run.truth(first)[0] + 300
    first.room_at_request = run.truth(first)[0]
    return run


def base() -> harness.Run:
    """A short run of the real engine, under the bound, that meets every target once a stub is found."""
    return copy.deepcopy(_base())


def loop(run: harness.Run) -> list[harness.Sent]:
    return [s for s in run.sent if s.in_loop]


def test_a_short_run_under_the_bound_meets_every_target_but_the_stub_check():
    assert targets.unmet_by_rule(base()) == {}
    run = base()
    run.stubs = {"found": 0, "unreachable": 0}
    assert targets.unmet_by_rule(run) == {"storage": ["no stub was found in any forwarded request"]}
    run.stubs = None
    assert targets.unmet(run) == ["stubs.find_stubs is not filled in, so no stub was checked"]


def test_the_bound_rule():
    run = base()
    run.sent[5].over_estimate = True
    assert targets.bound(run) == ["over_bound: 1 from the estimate, 0 from real usage"]
    run = base()
    run.sent[5].tokens = 150_001
    assert len(targets.bound(run)) == 1 and "150,001 tokens" in targets.bound(run)[0]
    run.sent[5].tokens = 150_000
    assert targets.bound(run) == []


def test_the_storage_rule_counts_every_request_that_left_a_result_nowhere():
    run = base()
    run.sent[6].lost = run.sent[7].lost = 2
    assert targets.storage(run) == ["2 requests left a tool result neither whole in the request nor byte for byte "
                                    "in a store page (up to 2 results at once)"]


def test_the_storage_rule_counts_every_request_that_left_out_a_result_no_stub_in_it_names():
    run = base()
    run.sent[6].unnamed, run.sent[9].unnamed = 1, 3
    assert targets.storage(run) == ["2 requests left out a tool result that no stub, fold note or page id in that "
                                    "request accounts for (up to 3 results at once)"]


def test_the_thinking_rule_allows_one_missing_newest_block_for_each_rewrite_in_the_loop():
    run = base()
    loop(run)[3].latest_thinking_missing = True
    assert targets.thinking(run) == ["main, turn 1: the newest response's thinking was missing on 1 requests, and "
                                     "the loop has 0 prefix rewrites"]
    loop(run)[3].rewrite = True
    assert targets.thinking(run) == []
    loop(run)[9].latest_thinking_missing = True
    assert len(targets.thinking(run)) == 1
    run.sent[0].rewrite = True  # a rewrite at the turn's first request is not one of the loop's
    assert len(targets.thinking(run)) == 1


def test_the_seen_first_rule_draws_its_line_at_0_8_of_what_a_request_can_hold():
    run = base()
    assert targets.show_ceiling(run) == int(0.8 * (150_000 - 25_000 - 12_000)) == 90_400
    sent = loop(run)[2]
    sent.stubbed_on_arrival, sent.unseen_tokens = 1, 90_400
    assert targets.seen_first(run) == []
    sent.unseen_tokens = 90_399
    assert len(targets.seen_first(run)) == 1


def pressed(run: harness.Run, *requests: harness.Sent) -> None:
    """Make each of ``requests`` a rewrite of a request past the pressure line."""
    for sent in requests:
        sent.rewrite = True
        run.sent[run.sent.index(sent) - 1].tokens = 70_000


def test_the_cache_rule_allows_two_rewrites_and_one_more_for_each_sixth_of_the_bound():
    run = base()
    tokens = run.sent[-1].loop_tokens
    assert 25_000 > tokens > 0  # so this loop may rewrite twice
    requests = loop(run)
    pressed(run, requests[2], requests[6])
    assert targets.cache(run) == []
    pressed(run, requests[10])
    assert targets.cache(run) == [f"main, turn 1: 3 prefix rewrites in a loop of {tokens:,} tokens; at most 2"]
    run.sent[-1].loop_tokens = 25_000
    assert targets.cache(run) == []


def test_the_cache_rule_allows_a_loop_rewrite_only_over_the_pressure_line():
    """Reviewer A's ``show_once``: paging when the request is well under the bound passes the count."""
    run = base()
    assert targets.pressure_line(run) == int(0.6 * 113_000) == 67_800
    before, sent = loop(run)[3:5]
    sent.rewrite = True
    before.tokens, sent.appended_tokens = 66_800, 1_000
    assert targets.cache(run) == ["main: 1 loop requests rewrote the prefix though without the rewrite they came to at "
                                  "most 67,800 tokens, not over 67,800"]
    sent.appended_tokens = 1_001
    assert targets.cache(run) == []
    run.sent[0].rewrite = True  # the turn's first request is not in the loop: a turn-start rotation is not held to it
    assert targets.cache(run) == []


def test_the_cache_rule_caps_what_a_loops_rewrites_re_send_at_one_extra_send_of_the_loop():
    run = base()
    own = run.sent[-1].loop_tokens
    assert own < run.config.bound  # so the bound is the ceiling here
    one, two = loop(run)[4], loop(run)[9]
    pressed(run, one, two)
    one.resent, two.resent = 100_000, 50_001
    assert targets.cache(run) == [f"main, turn 1: its rewrites re-sent 150,001 tokens, more than the loop's own "
                                  f"{own:,} tokens or the bound"]
    two.resent = 50_000  # one bound exactly
    assert targets.cache(run) == []
    run.sent[-1].loop_tokens = 160_000  # a longer loop: its own tokens are the ceiling
    two.resent = 60_000
    assert targets.cache(run) == []
    two.resent = 60_001
    assert len(targets.cache(run)) == 1


def test_the_rewrite_allowance_scales_with_the_head():
    """d = min(bound / 6, max(bound / 30, 0.5 x (room line - head))): 25,000 at every scripted head."""
    run = base()
    assert run.heads == {"main": 15_101}
    assert targets.rewrite_batch(run, "main") == 25_000
    for head, batch in [(13_538, 25_000), (63_000, 25_000), (73_000, 20_000), (92_000, 10_500), (103_000, 5_000),
                        (140_000, 5_000)]:
        run.heads["main"] = head
        assert targets.rewrite_batch(run, "main") == batch, head
    run.heads["main"] = 92_000  # PR #274's head: a loop of 21,000 tokens may rewrite 2 + 2 times
    requests = loop(run)
    pressed(run, *requests[1:12:3])
    run.sent[-1].loop_tokens = 21_000
    assert targets.cache(run) == []
    run.sent[-1].loop_tokens = 20_999
    assert targets.cache(run) == ["main, turn 1: 4 prefix rewrites in a loop of 20,999 tokens; at most 3"]


def test_the_cache_rule_forbids_a_rewrite_right_after_a_rewrite_unless_the_room_forces_it():
    """The exception counts as the room rule does, with the engine's own pre-send estimate, and allows
    2% of the bound for it: over 113,000 - 3,000. The estimate counts at most 1.02 x the upstream's count
    of the same request."""
    run = base()
    first, second = loop(run)[4:6]
    pressed(run, first, second)
    second.estimate_before = None
    assert targets.cache(run) == ["main: 1 requests rewrote the prefix right after a rewrite, though the request "
                                  "would have kept min_turn_room without it"]
    first.tokens, second.appended_tokens = 109_000, 1_001  # no estimate published: the upstream's count
    assert targets.cache(run) == []
    second.appended_tokens = 1_000
    assert len(targets.cache(run)) == 1
    second.estimate_before = 110_001  # the engine's estimate, published: it is the count
    assert targets.cache(run) == []
    second.estimate_before = 110_000
    assert len(targets.cache(run)) == 1
    first.tokens, second.estimate_before = 120_000, 109_000  # the engine's count rules, though the upstream's is over
    assert len(targets.cache(run)) == 1
    first.tokens, second.appended_tokens, second.estimate_before = 70_000, 1_000, 999_999  # reviewer A's L1
    assert len(targets.cache(run)) == 1  # it counts as 72,420
    first.tokens = 106_844  # 107,844 counted: 1.02 x that is over 110,000
    assert targets.cache(run) == []
    first.tokens = 106_843
    assert len(targets.cache(run)) == 1


def test_the_room_rule_needs_a_room_before_and_after_every_loop_request():
    run = base()
    loop(run)[3].room = None
    loop(run)[4].room_at_request = loop(run)[5].room_at_request = None
    assert targets.room(run) == ["main: no room was published before 2 loop requests and after 1"]


def test_the_room_rule_past_50_000_tokens():
    def late(rooms_at_request, rooms):
        run = base()
        for sent, before, after in zip(loop(run), rooms_at_request, rooms):
            sent.loop_tokens, sent.room_at_request, sent.room = 50_001, before, after
        return targets.room(run)

    n = len(loop(_base()))
    assert n == 16
    assert late([12_000] * n, [6_000] * n) == []
    assert late([11_999] + [12_000] * 15, [6_000] * n) == [
        "main: the request-time room was under 12,000 on 1 requests after the turn passed 50,000 tokens"]
    assert late([12_000] * n, [0] + [6_000] * 15) == [
        "main: the response-time room was 0 on 1 requests after the turn passed 50,000 tokens",
        "main: the response-time room was under 6,000 on 1 of 16 requests after the turn passed 50,000 tokens"]
    assert late([12_000] * n, [1] * n) == [  # reviewers: a room of 1 token passed before
        "main: the response-time room was under 6,000 on 16 of 16 requests after the turn passed 50,000 tokens"]
    run = base()
    for sent in loop(run):  # at 50,000 itself the rule does not apply yet
        sent.loop_tokens, sent.room = 50_000, 0
    assert targets.room(run) == []


def test_the_room_rule_holds_every_room_to_what_the_upstream_counted():
    """Reviewer A's ``lying_room``. A response-time room is at most ``bound - (input + output) -
    headroom_floor``; a request-time room at most ``bound - headroom_floor - 0.9 x the request``."""
    run = base()
    sent = loop(run)[6]
    sent.tokens, sent.output_tokens = 40_000, 300
    sent.room, sent.room_at_request = 150_000 - 40_300 - 25_000, 150_000 - 25_000 - 36_000
    assert run.truth(sent) == (89_000, 84_700)
    assert targets.room(run) == []
    sent.room += 1
    assert targets.room(run) == ["main: the response-time room was more than bound - (input + output) - "
                                 "headroom_floor on 1 requests"]
    sent.room -= 1
    sent.room_at_request += 1
    assert targets.room(run) == ["main: the request-time room was more than bound - headroom_floor - 0.9 x the "
                                 "request's tokens on 1 requests"]
    sent.tokens, sent.room, sent.room_at_request = 160_000, 0, 0  # over the bound: 0 is the truth
    assert targets.room(run) == []


def test_the_room_rule_lets_one_request_in_twenty_fall_under_half_the_minimum():
    run = harness.Run(_base().scenario, "claude", _base().config)
    template = loop(_base())[0]
    run.sent = [copy.copy(template) for _ in range(40)]
    for sent in run.sent:
        sent.loop_tokens, sent.room_at_request, sent.room = 60_000, 20_000, 9_000
    run.sent[0].room = run.sent[1].room = 5_999
    assert targets.room(run) == []  # 2 of 40
    run.sent[2].room = 5_999
    assert targets.room(run) == [
        "main: the response-time room was under 6,000 on 3 of 40 requests after the turn passed 50,000 tokens"]


def probes(search: str, in_view: bool, recall: str) -> list[dict]:
    return [{"conversation": "main", "kind": "search", "request": 9, "target_in_view": in_view, "outcome": search},
            {"conversation": "main", "kind": "recall", "request": 10, "target_in_view": None, "outcome": recall}]


def test_the_recall_rule():
    run = base()
    for search, in_view, recall, problem in [
        ("in_view", True, "not_called", None),
        ("paged", False, "returned", None),
        ("paged", True, "returned", None),  # found in a page though it is still in view
        ("refused", False, "not_called", "the search did not return the line (refused)"),
        ("nothing", True, "not_called", "the search did not return the line (nothing)"),
        ("other", False, "not_called", "the search did not return the line (other)"),
        ("in_view", False, "not_called", "the result is not whole in the request, and the search named no page"),
        ("paged", False, "refused", "recalling the page the search named did not return the original (refused)"),
        ("paged", False, "partial", "recalling the page the search named did not return the original (partial)"),
    ]:
        run.probes = probes(search, in_view, recall)
        assert targets.recall(run) == ([f"main, request 9: {problem}"] if problem else []), (search, recall)
    run.probes = []
    assert targets.recall(run) == ["the scenario made no search"]


def test_the_recall_rule_lets_a_subagent_be_told_nothing_while_its_result_is_still_in_its_request():
    """Search reads the main conversation's view only, so a subagent's search cannot return a line that
    is in the subagent's own request and in no page. Once the result is stubbed, it has to be found."""
    run = toy_run("subagent", "claude")
    assert targets.recall(run) == []
    run = copy.copy(run)
    run.probes = [{**p, "conversation": "subagent"} for p in probes("nothing", True, "not_called")]
    assert targets.recall(run) == []
    run.probes = [{**p, "conversation": "subagent"} for p in probes("nothing", False, "not_called")]
    assert targets.recall(run) == ["subagent, request 9: the search did not return the line (nothing)"]
    run.probes = [{**p, "conversation": "subagent"} for p in probes("refused", True, "not_called")]
    assert targets.recall(run) == ["subagent, request 9: the search did not return the line (refused)"]


def test_the_count_tokens_rule():
    run = base()
    assert run.count_tokens == {"sent": 1, "different": 0, "published": 0}
    run.count_tokens = {"sent": 4, "different": 1, "published": 2}
    assert targets.count_tokens(run) == [
        "1 of 4 count_tokens requests got another transform than the request just forwarded",
        "2 count_tokens requests published a room"]


def test_an_engine_that_answers_count_tokens_with_the_cli_request_fails_once_it_has_paged():
    class Uncounted(toy.ToyPager):
        def __call__(self, request, headers, endpoint):
            if endpoint.endswith("count_tokens"):
                return request, headers, []
            return super().__call__(request, headers, endpoint)

    run = harness.run(scripts.build("loop120", quick=True), "claude", find_stubs=toy.find_stubs, engine=Uncounted)
    assert list(targets.unmet_by_rule(run)) == ["count_tokens"] and run.count_tokens["different"] >= 2


# What a reply is read as ---------------------------------------------------------------------------------

PROBE = scripts.Probe("search", "k", "t", "0123456789ab", "   120  word_0123 = load_ab(0123456789ab)",
                      ("   121  next line", "   122  the line after"))


def test_a_search_reply_is_read_as_the_model_would_read_it():
    line = PROBE.line.strip()
    paged = "\n".join([mcp.SEARCH_BEST_HEADER, "a1b2c3d4 (turn 3)", "  100: before", f"> 144: {line}", "  190: after"])
    assert harness._search_outcome((paged, mcp.SEARCH_COVERAGE), PROBE) == ("paged", ("a1b2c3d4", 144))
    in_view = "\n".join(["[in view, newest first:]", f"> in view (turn 0): {line}"])
    assert harness._search_outcome((in_view, mcp.SEARCH_COVERAGE), PROBE) == ("in_view", None)
    assert harness._search_outcome((in_view + "\n" + paged, mcp.SEARCH_COVERAGE), PROBE) == ("paged", ("a1b2c3d4", 144))
    elsewhere = paged.replace(line, "another line with 0123456789ab in it")
    assert harness._search_outcome((elsewhere, mcp.SEARCH_COVERAGE), PROBE) == ("other", None)
    assert harness._search_outcome((mcp.BUDGET_SPENT, mcp.SEARCH_COVERAGE), PROBE) == ("refused", None)
    assert harness._search_outcome((mcp.NO_HIT, mcp.SEARCH_COVERAGE), PROBE) == ("nothing", None)
    assert harness._search_outcome((mcp.SEARCH_TOO_LONG, mcp.SEARCH_COVERAGE), PROBE) == ("too_long", None)


def test_a_recall_reply_counts_only_with_the_line_and_the_two_after_it():
    whole = "\n".join((PROBE.line, *PROBE.after))
    assert harness._recall_outcome((f"tool_result:\n{whole}\nmore", "[90 more characters: ...]"), PROBE, False) == "returned"
    assert harness._recall_outcome((PROBE.line + "\n" + PROBE.after[0],), PROBE, False) == "partial"
    assert harness._recall_outcome((mcp.BUDGET_SPENT,), PROBE, False) == "refused"
    assert harness._recall_outcome((mcp.RECALL_NEARLY_SPENT.format(tokens=40),), PROBE, False) == "refused"
    assert harness._recall_outcome(("Unknown page id: zz. Page ids appear in the paged-history index.",), PROBE, True) == "error"


def test_only_the_text_the_model_reads_names_a_page():
    """Reviewer A's M1 at 304e598e: a page id inside a tool result, or in a request field the model does not
    read, names nothing for rule 3; one in a message's text part does."""
    from inturn.harness import _read_texts
    assert _read_texts({"role": "user", "content": "see pg_0123456789abcdef"}) == ["see pg_0123456789abcdef"]
    blocks = [{"type": "tool_result", "tool_use_id": "t", "content": "pg_0123456789abcdef"},
              {"type": "text", "text": "the index"}, {"type": "input_text", "text": "codex"}]
    assert _read_texts({"role": "user", "content": blocks}) == ["the index", "codex"]
    assert _read_texts({"type": "function_call_output", "call_id": "c", "output": "pg_0123456789abcdef"}) == []


def test_an_index_past_its_budget_accounts_for_the_whole_turns_it_omits():
    """Reviewer A's M at 9fe691ef. v0.5.24 with an index of 40 tokens lists only its newest page and writes
    "(N older pages omitted; ...)"; the whole earlier turns it paged are accounted for by that line."""
    for dialect in ("claude", "codex"):
        _past_budget(dialect)


def _past_budget(dialect: str) -> None:
    run = harness.run(scripts.build("multiturn", quick=True), dialect, index_budget=40)
    assert run.sent and run.metrics()["requests_with_an_unnamed_result"] == 0
    # (storage itself reports only that v0.5.24 has no stubs hook: the baseline's hook is empty by design)
    assert targets.unmet_by_rule(run)["storage"] == ["stubs.find_stubs is not filled in, so no stub was checked"]


def test_the_omission_line_needs_a_count_the_store_bears_out_and_the_whole_turn_gone():
    from inturn.harness import OMITTED
    assert OMITTED.findall("(0 older pages omitted; use contexto_search to find them)") == ["0"]
    assert OMITTED.findall("see 3 older pages omitted; use contexto_search") == []  # not the index's form


def test_the_open_case_an_omission_line_the_store_bears_out_after_a_whole_turn_left_passes():
    """Stated as open in the note: an engine that drops whole earlier turns and writes an omission line
    with a count the store bears out passes rule 3 for them, as a turn-start rotation past the index budget
    does; the results stay stored. The same engine claiming 0 or 99 older pages fails (above)."""
    run = toy_run("multiturn", "claude", "omit_turns")
    assert targets.unmet_by_rule(run) == {}
    assert run.metrics()["requests_with_a_lost_result"] == 0


def test_text_two_turns_share_says_nothing_about_which_turn_is_in_view():
    """Reviewer B's L1 at 10c8ae79: a run of a turn's text that another turn shares (every prompt
    "continue", a common template) is not evidence that this turn is still in view."""
    from inturn.harness import _windows
    shared = "Use the same checklist as before and report the result in one table."
    one, two = _windows((shared, "the first answer names the file src/a.py")), _windows((shared, "the second answer names src/b.py"))
    assert one & two and one - two and two - one


def test_format_characters_cannot_hide_a_turn_in_view():
    """Reviewer B's L2 at 10c8ae79: a zero-width space between characters is removed before matching."""
    from inturn.harness import _bare
    assert _bare("con\u200btin ue\u2060d") == "continued"
