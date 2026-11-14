"""The targets cannot be met by a wrong engine, and can be met by a right one.

``toy.ToyPager`` is a small in-turn pager written for these tests (it is not the D63 design). Run
through every scenario it meets every target, so the targets do not contradict each other. Each of its
defects makes it wrong in one way, and the target written for that way fails it:

- ``junk_page``: every stub names one page that holds none of the results;
- ``strip_all``: all thinking is stripped on every request;
- ``stub_on_arrival``: a result is stubbed in the request that first carries it, so the model never reads it;
- ``rewrite_always``: the prefix is rewritten on every request;
- ``no_room``: no recall room is published;
- ``room_one``: every room published is 1 token.
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
def toy_run(name: str, dialect: str, defect: str = "") -> harness.Run:
    return harness.run(scripts.build(name, quick=True), dialect, find_stubs=toy.find_stubs, check_wire=not defect,
                       engine=functools.partial(toy.ToyPager, defect=defect))


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
        assert m["rewrites"]["resent"] < m["rewrites"]["invalidated"] / 4
        around = m["info"].get("thinking_kept_around_strip", 0) + m["info"].get("reasoning_kept_around_drop", 0)
        assert around > 0  # thinking kept before and after a stripped range: valid by the research, not confirmed live
        assert {p["outcome"] for p in run.probes if p["kind"] == "recall"} == {"returned"}


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
]


@pytest.mark.parametrize(("defect", "rules"), WRONG)
def test_each_wrong_engine_fails_the_rule_written_for_it_and_no_other(defect, rules):
    assert set(targets.unmet_by_rule(toy_run("loop120", "claude", defect))) == rules


@pytest.mark.parametrize(("defect", "rules"), WRONG[:3])
def test_the_wrong_engines_whose_shapes_differ_on_codex_fail_there_too(defect, rules):
    """Stored items, reasoning items and outputs are other shapes on the Responses API; the cache and
    room rules read only numbers."""
    assert set(targets.unmet_by_rule(toy_run("loop120", "codex", defect))) == rules


@pytest.mark.parametrize(("defect", "rules"), [("no_room", {"room"}), ("room_one", {"room", "recall"})])
def test_a_subagent_without_a_usable_side_room_fails(defect, rules):
    by_rule = targets.unmet_by_rule(toy_run("subagent", "claude", defect))
    assert set(by_rule) == rules
    assert any(problem.startswith("subagent: ") for problem in by_rule["room"])


def test_what_each_wrong_engine_is_told():
    said = {defect: targets.unmet_by_rule(toy_run("loop120", "claude", defect)) for defect, _ in WRONG}
    assert said["junk_page"]["storage"][0].endswith("stubs do not lead to their originals, byte for byte")
    assert "neither whole in the request nor byte for byte in a store page" in said["junk_page"]["storage"][1]
    assert "thinking_removed_outside_rewrite" in said["strip_all"]["violations"][0]
    assert "the newest response's thinking was missing on 34 requests" in said["strip_all"]["thinking"][0]
    assert "were stubbed in the request that first carried them" in said["stub_on_arrival"]["seen_first"][0]
    assert said["rewrite_always"]["cache"][0].startswith("main, turn 1: 33 prefix rewrites")
    assert "rewrote the prefix right after a rewrite" in said["rewrite_always"]["cache"][1]
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


def test_a_stub_that_names_no_call_fails_though_its_page_exists():
    """Reviewer A's first wrong engine: the stub quotes the result's first and last line and its
    ``StubRef`` names a page and no call."""
    def nameless(dialect, request):
        return [StubRef(ref.page_id, ()) for ref in toy.find_stubs(dialect, request)]
    run = harness.run(scripts.build("loop120", quick=True), "claude", find_stubs=nameless, engine=toy.ToyPager)
    assert run.stubs["found"] == run.stubs["unreachable"] > 0
    assert list(targets.unmet_by_rule(run)) == ["storage"]


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
    assert not s.stubs.reachable(StubRef(page, ()))  # names no call
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


def test_the_cache_rule_allows_two_rewrites_and_one_more_for_each_sixth_of_the_bound():
    run = base()
    tokens = run.sent[-1].loop_tokens
    assert 25_000 > tokens > 0  # so this loop may rewrite twice
    requests = loop(run)
    requests[2].rewrite = requests[6].rewrite = True
    assert targets.cache(run) == []
    requests[10].rewrite = True
    assert targets.cache(run) == [f"main, turn 1: 3 prefix rewrites in a loop of {tokens:,} tokens; at most 2"]
    run.sent[-1].loop_tokens = 25_000
    assert targets.cache(run) == []


def test_the_cache_rule_forbids_a_rewrite_right_after_a_rewrite_unless_the_room_forces_it():
    run = base()
    first, second = loop(run)[4:6]
    first.rewrite = second.rewrite = True
    assert targets.cache(run) == ["main: 1 requests rewrote the prefix right after a rewrite, though the request "
                                  "would have kept min_turn_room without it"]
    # bound 150,000 less headroom_floor 25,000 less min_turn_room 12,000: one more result would have left less
    first.tokens, second.appended_tokens = 112_000, 1_001
    assert targets.cache(run) == []
    second.appended_tokens = 1_000
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
