"""Project memory measured at zero spend (D69, the owner's "Measure at zero spend"): synthetic sessions of
one project through the real launcher store, the real engine and the real MCP server
(``tests/synthetic_project.py``), with a stub's usage and no model.

- Two sessions: which of the first session's rules and decisions are in the second's first request, which
  are absent and why, which the preamble holds after the second's first rotation, that no unpinned fact is
  in its window, how many facts one project-scope search reaches (and a session-scope one: none), and the
  sizes of the start text, the preamble and the first request with project memory on and off.
- Five sessions: the exact still-open set at the fifth session's start, in order and marked with its
  session, after closures and replacements made through the MCP server in later sessions; the facts of
  each earlier session one search reaches; and the omitted line when the carried set is over the budget.
- The bound: every request of the later sessions at or under it, by the engine's estimate and the stub's
  count, with the carried text present; every search charged by the one rule, and no room below zero.
- Fifty earlier sessions: the time project memory adds to a session's start and the latency of searches,
  cold and warm, in process (``test_fifty_earlier_sessions_time``, deselect it with ``-k "not fifty"``).

Every number is printed (``pytest -s``) and, when ``CONTEXTO_MEASURE_OUT`` names a file, written to it as
JSON. The content is synthetic and fixed, so every count is the same on every run; the times are not, and
only generous ceilings are asserted on them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from contexto import mcp
from contexto.rewrite import compose_preamble
from contexto.store import project as project_module
from contexto.store.project import MAIN_AUTHOR, read_records
from contexto.tokens import estimate_tokens
from synthetic_project import (
    CONFIG,
    MARK,
    Decision,
    Fact,
    Plan,
    Rule,
    Session,
    Turn,
    carried_lines,
    charge_for,
    facts,
    omitted,
    plan_turns,
    repo,
    session_id,
    show,
)

OFF_PATH = {"over_bound", "bound_too_small", "degraded_enter", "store_error", "fallback_failed", "transform_error",
            "start_text_unavailable"}


@pytest.fixture(scope="module")
def clean_env():
    """No CONTEXTO_* variable of the developer's in reach, and an empty project cache."""
    with pytest.MonkeyPatch.context() as mp:
        for name in [n for n in os.environ if n.startswith("CONTEXTO_")]:
            mp.delenv(name)
        project_module.forget_cache()
        yield mp
    project_module.forget_cache()


def every_event(session: Session) -> set[str]:
    return {e["event"] for row in session.rows for e in row.events + row.usage_events}


def estimate_errors(session: Session) -> list[int]:
    """The requests whose real input was more than 10% over the engine's estimate."""
    return [i for i, row in enumerate(session.rows) if any(e["event"] == "estimate_error" for e in row.usage_events)]


def check_bound(session: Session) -> dict:
    """Rule 19's bound in a session that starts with carried records: every request at or under the bound
    by the engine's estimate and by the stub's count, the carried section in every one (in the first
    turn start before the first rotation, in the preamble after), and nothing off the normal path. The
    stub counts 3.2 bytes a token against the estimator's seed of 3.5, so the first request's estimate,
    made before any usage was measured, is low by more than 10% (``estimate_error``); no later one is."""
    assert session.rotations(), "the session must rotate"
    assert max(row.real for row in session.rows) <= CONFIG.bound
    assert max(row.estimate for row in session.rows) <= CONFIG.bound
    assert all(MARK in session.text(i) for i in range(len(session.rows)))
    assert not OFF_PATH & every_event(session)
    assert estimate_errors(session) == [0]
    return {"requests": len(session.rows), "rotations": len(session.rotations()),
            "largest_real": max(row.real for row in session.rows),
            "largest_estimate": max(row.estimate for row in session.rows), "bound": CONFIG.bound,
            "estimate_error_requests": estimate_errors(session)}


def texts(upstream: dict) -> list[str]:
    """The text blocks of a request's messages."""
    return [block["text"] for message in upstream["messages"] if isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "text"]


def check_charges(session: Session) -> dict:
    """Each search, of either scope, is charged by the one rule (its reply's characters at the smaller of
    the measured bytes per token and 1.5, at most the room left), against the main room the engine
    published, and no room goes below zero."""
    out = {"session": 0, "project": 0}
    control = 0
    for call in session.searches("session") + session.searches("project"):
        event = call.event
        assert event is not None and event["ok"], event
        left = call.room[1] - call.room[2]
        if event["turn_budget"] == "control":  # a fixed protocol reply (no page matches): never charged
            assert call.arguments.get("scope", "session") == "session" and call.text in mcp.CONTROL_REPLIES
            assert event["charged_tokens"] == 0 and event["room_left"] == left >= 0
            control += 1
            continue
        assert event["turn_budget"] == "charged", event
        assert event["charged_tokens"] == min(left, charge_for(call.text, call.ratio))
        assert event["room_left"] == left - event["charged_tokens"] >= 0
        assert call.room_after[1] - call.room_after[2] == event["room_left"]
        assert event["limited_by"] == "main"
        out[call.arguments.get("scope", "session")] += event["charged_tokens"]
    assert all(c.room_after[1] - c.room_after[2] >= 0 for c in session.calls if c.room_after[1] is not None)
    return {"charged_session_scope": out["session"], "charged_project_scope": out["project"],
            "searches": len(session.searches("session")) + len(session.searches("project")),
            "uncharged_control_replies": control}


def reached(session: Session, scope: str, wanted: tuple[Fact, ...]) -> list[str]:
    """The keys of ``wanted`` whose search (one call each, by key) returned the line that holds the value."""
    by_key = {c.arguments["query"]: c for c in session.searches(scope)}
    return [f.key for f in wanted if f.key in by_key and f.value in by_key[f.key].text]


# Two sessions ----------------------------------------------------------------------------------------

RULES = tuple(Rule(f"RULE{i:02d}Q", f"always run check number {i} before a commit") for i in range(6))
DECISIONS = (Decision("DECISION01Q", "the release codename is Obsidian"),
             Decision("DECISION02Q", "releases ship on Tuesdays"),
             Decision("DECISION03Q", "the release codename is Basalt", supersedes="DECISION01Q"))
CLOSED = ("RULE01Q", "RULE04Q")
FACTS = facts("A", 12)
TAIL = facts("T", 2)  # said in the first session's last turns, which are never paged out


@dataclass
class Two:
    first: Session
    second: Session
    off: Session
    root: Path


@pytest.fixture(scope="module")
def two(tmp_path_factory, clean_env):
    base = tmp_path_factory.mktemp("two")
    home, root = base / "contexto-home", repo(base)
    first = Session(home, session_id("two", 1), root).run(
        Plan(rules=RULES, decisions=DECISIONS, facts=FACTS, closes=CLOSED, turns=50, tail=TAIL))
    first.end()
    keys = tuple(f.key for f in FACTS + TAIL)
    second = Session(home, session_id("two", 2), root).run(Plan(searches=keys, turns=40))
    second.end()
    off = Session(home, session_id("two", 3), root, memory=False)  # the same first request, memory off
    off.turn(0, first_turn(Plan(searches=keys, turns=40)), {})
    off.end()
    return Two(first, second, off, root)


def first_turn(plan: Plan) -> Turn:
    """A plan's first turn without its tool calls: what the first request holds."""
    turn = plan_turns(plan, {})[0]
    return Turn(turn.prompt, turn.output, turn.answer)


def test_two_sessions_the_first_session_ran_as_a_session_does(two):
    first = two.first
    records = read_records(first.store.dir)
    pins = [r for r in records if r.kind in ("obligation", "decision")]
    assert len(pins) == len(RULES) + len(DECISIONS) and {r.author for r in pins} == {MAIN_AUTHOR}
    assert sum(r.kind == "closure" for r in records) == len(CLOSED)
    assert len(first.rotations()) >= 3 and not OFF_PATH & every_event(first)
    assert all(c.result["isError"] is False for c in first.calls)
    fact_turns = [t for t, turn in enumerate(first.turn_starts) if any(
        f.key in str(first.history[turn:first.turn_starts[t + 1] if t + 1 < len(first.turn_starts) else None])
        for f in FACTS)]
    assert max(fact_turns) < first.paged_turns()  # every fact the plan spreads is paged out


def test_two_sessions_the_window_holds_the_open_rules_and_decisions_and_nothing_else(two):
    first, second = two.first, two.second
    window = second.text(0)
    start = second.state.start_text
    assert start and start in texts(second.rows[0].upstream) and window.count(MARK) == 1
    names = [r.name for r in RULES] + [d.name for d in DECISIONS]
    present = [n for n in names if n in window]
    expected_open = [r.name for r in RULES if r.name not in CLOSED]
    expected_active = ["DECISION02Q", "DECISION03Q"]
    assert present == expected_open + expected_active
    absent = {n: ("closed" if n in CLOSED else "superseded by DECISION03Q") for n in names if n not in present}
    assert absent == {"RULE01Q": "closed", "RULE04Q": "closed", "DECISION01Q": "superseded by DECISION03Q"}
    lines = carried_lines(start)
    assert [name for _, _, _, name in lines] == ["RULE05Q", "RULE03Q", "RULE02Q", "RULE00Q",
                                                  "DECISION03Q", "DECISION02Q"]  # obligations, newest first
    assert {label for _, _, label, _ in lines} == {first.label}
    assert all(first.ids[name] == rid for rid, _, _, name in lines)
    # The values are nowhere in the window (the first prompt asks for a key: it names it, as a user would).
    leaked = [f.key for f in FACTS + TAIL if f.value in window or f.key in start]
    assert leaked == []
    rotation = second.rotations()[0]
    state = second.rows[rotation].state
    after = [n for n in names if n in second.text(rotation)]
    assert state.k > 0 and after == present and start in state.pinned_text
    pre = compose_preamble(CONFIG, state.pinned_text, state.index_text)
    assert [f.value for f in FACTS + TAIL if f.value in pre] == []
    assert second.start_line == (f"contexto: project memory: 6 records carried from 1 of 1 earlier session in "
                                 f"{two.root} (list: contexto project show; off: --no-project-memory)")
    show("two_sessions.window", {
        "present_in_first_request": present, "absent": absent, "present_after_first_rotation": after,
        "first_rotation_request": rotation, "facts_in_window": leaked, "start_line": second.start_line.replace(
            str(two.root), "<root>")})


def test_two_sessions_one_project_search_reaches_every_paged_fact_and_a_session_search_none(two):
    second = two.second
    project, session = reached(second, "project", FACTS), reached(second, "session", FACTS)
    assert project == [f.key for f in FACTS]
    assert session == []
    for call in second.searches("project"):
        assert call.event["earlier_searched"] == 1 and call.event["earlier_not_searched"] == 0
    by_place = {where: sum(1 for f in FACTS if f.where == where and f.key in project)
                for where in ("prompt", "tool", "answer")}
    show("two_sessions.search", {"facts": len(FACTS), "reached_project_scope": len(project),
                                 "reached_session_scope": len(session), "reached_by_place": by_place,
                                 "first_session_turns": len(two.first.turn_starts),
                                 "first_session_turns_paged": two.first.paged_turns()})


@pytest.mark.xfail(strict=True, reason="D69 finding: a project-scope search reads only an earlier session's pages, "
                   "so what it said in its last retained window (never paged out when it ended) cannot be reached")
def test_two_sessions_a_fact_said_in_the_last_turns_of_the_first_session_is_reachable(two):
    tail = reached(two.second, "project", TAIL)
    show("two_sessions.tail", {"tail_facts": len(TAIL), "reached_project_scope": len(tail),
                               "first_session_turns_not_paged": len(two.first.turn_starts) - two.first.paged_turns()})
    assert tail == [f.key for f in TAIL]


def test_two_sessions_sizes(two):
    second, off = two.second, two.off
    start = second.state.start_text
    rotation = second.rotations()[0]
    state = second.rows[rotation].state
    pre = compose_preamble(CONFIG, state.pinned_text, state.index_text)
    numbers = {
        "start_text_tokens": estimate_tokens(start), "start_text_bytes": len(start.encode()),
        "pin_budget": CONFIG.pin_budget,
        "preamble_tokens_after_first_rotation": estimate_tokens(pre),
        "pinned_text_tokens_after_first_rotation": estimate_tokens(state.pinned_text),
        "index_text_tokens_after_first_rotation": estimate_tokens(state.index_text),
        "carried_section_in_pinned_text": start in state.pinned_text,
        "first_request_bytes_on": len(second.text(0).encode()), "first_request_bytes_off": len(off.text(0).encode()),
        "first_request_estimate_on": second.rows[0].estimate, "first_request_estimate_off": off.rows[0].estimate,
        "first_room_on": second.rows[0].room[1], "first_room_off": off.rows[0].room[1],
    }
    assert estimate_tokens(start) <= CONFIG.pin_budget and estimate_tokens(state.pinned_text) <= CONFIG.pin_budget
    assert MARK not in off.text(0) and off.state.start_text == "" and off.start_line == ""
    assert numbers["first_request_bytes_on"] - numbers["first_request_bytes_off"] >= len(start.encode())
    added = numbers["first_request_estimate_on"] - numbers["first_request_estimate_off"]
    assert 0.9 * estimate_tokens(start) <= added <= 1.1 * estimate_tokens(start) + 5
    assert numbers["first_room_off"] - numbers["first_room_on"] == added
    show("two_sessions.sizes", numbers)


def test_two_sessions_the_bound_and_the_charges(two):
    numbers = {**check_bound(two.second), **check_charges(two.second)}
    show("two_sessions.bound", numbers)


# Five sessions ---------------------------------------------------------------------------------------

def five_plans() -> list[Plan]:
    """Sessions 1 to 4. A later session closes or replaces records of an earlier one through the carried id."""
    def rules(s: int, *letters: str) -> tuple[Rule, ...]:
        return tuple(Rule(f"RULE{s}{x}Q", f"standing rule {x} of session {s}: keep the changelog current")
                     for x in letters)

    def decision(s: int, x: str, supersedes: str | None = None) -> Decision:
        return Decision(f"DECISION{s}{x}Q", f"decision {x} of session {s}: the build uses tool {s}{x}", supersedes)

    return [
        Plan(rules=rules(1, "A", "B", "C"), decisions=(decision(1, "A"), decision(1, "B")), facts=facts("B1", 4)),
        Plan(rules=rules(2, "A", "B"), decisions=(decision(2, "A"), decision(2, "S", "DECISION1AQ")),
             facts=facts("B2", 4), closes=("RULE1AQ",)),
        Plan(rules=rules(3, "A"), decisions=(decision(3, "A"), decision(3, "S", "DECISION2AQ")),
             facts=facts("B3", 4), closes=("RULE2AQ", "RULE1CQ")),
        Plan(rules=rules(4, "A", "B"), decisions=(decision(4, "A"), decision(4, "S", "DECISION1BQ")),
             facts=facts("B4", 4), closes=("RULE4BQ", "RULE3AQ")),
    ]


def expected_open(plans: list[Plan], labels: list[str]) -> list[tuple[str, str]]:
    """(name, session label) of the still-open set, as rule 19 orders it: open obligations, then active
    decisions, newest first in each. A model of the plans, independent of the store."""
    pinned: list[tuple[str, str, str]] = []  # (kind, name, label) in the order pinned
    gone: set[str] = set()
    for plan, label in zip(plans, labels):
        groups = [plan.rules[i:i + plan.pins_per_turn] for i in range(0, len(plan.rules), plan.pins_per_turn)]
        plain = [d for d in plan.decisions if d.supersedes is None]
        for i in range(max(len(groups), len(plain), len(plan.facts))):
            pinned += [("obligation", r.name, label) for r in (groups[i] if i < len(groups) else ())]
            if i < len(plain):
                pinned.append(("decision", plain[i].name, label))
        gone |= set(plan.closes)
        for d in plan.decisions:
            if d.supersedes is not None:
                pinned.append(("decision", d.name, label))
                gone.add(d.supersedes)
    alive = [p for p in reversed(pinned) if p[1] not in gone]
    return [(n, s) for k, n, s in alive if k == "obligation"] + [(n, s) for k, n, s in alive if k == "decision"]


@dataclass
class Five:
    sessions: list[Session]
    plans: list[Plan]


@pytest.fixture(scope="module")
def five(tmp_path_factory, clean_env):
    base = tmp_path_factory.mktemp("five")
    home, root = base / "contexto-home", repo(base)
    plans, ids, sessions = five_plans(), {}, []
    for n, plan in enumerate(plans, 1):
        session = Session(home, session_id("five", n), root).run(plan, ids)
        session.end()
        sessions.append(session)
    keys = tuple(f.key for plan in plans for f in plan.facts)
    last = Session(home, session_id("five", 5), root).run(Plan(searches=keys, turns=40), ids)
    last.end()
    return Five([*sessions, last], plans)


def test_five_sessions_each_later_session_closed_and_replaced_through_the_carried_id(five):
    for session, plan in zip(five.sessions[1:4], five.plans[1:]):
        own = {r.name for r in plan.rules} | {d.name for d in plan.decisions}
        replaced = {d.supersedes for d in plan.decisions if d.supersedes}
        for name in {n for n in plan.closes if n not in own} | replaced:
            assert session.ids[name] in session.text(0)  # the model read the id in its start text
        assert all(c.result["isError"] is False for c in session.calls), [c.result for c in session.calls]
        records = read_records(session.store.dir)
        assert {r.author for r in records if r.kind in ("obligation", "decision")} == {MAIN_AUTHOR}


def test_five_sessions_the_fifth_starts_with_exactly_the_still_open_set_in_order(five):
    last = five.sessions[4]
    labels = [s.label for s in five.sessions[:4]]
    want = expected_open(five.plans, labels)
    lines = carried_lines(last.state.start_text)
    got = [(name, label) for _, _, label, name in lines]
    assert got == want
    kinds = [kind for _, kind, _, _ in lines]
    assert kinds == sorted(kinds, key=lambda k: k != "obligation")  # obligations first
    assert all(last.ids[name] == rid for rid, _, _, name in lines)
    window = last.text(0)
    all_names = {r.name for p in five.plans for r in p.rules} | {d.name for p in five.plans for d in p.decisions}
    absent = sorted(all_names - {n for n, _ in want})
    assert [n for n in absent if n in window] == []
    two_back = set(five.plans[2].closes) | {d.supersedes for d in five.plans[2].decisions if d.supersedes}
    assert two_back == {"RULE2AQ", "RULE1CQ", "DECISION2AQ"} and not two_back & {n for n, _ in got}
    assert omitted(last.state.start_text) == 0
    rotation = last.rotations()[0]
    assert [n for n, _ in want if n not in last.text(rotation)] == []  # the preamble holds them after
    assert last.start_line.startswith(f"contexto: project memory: {len(want)} records carried from "
                                      f"{len({s for _, s in want})} of 4 earlier sessions in ")
    show("five_sessions.window", {
        "carried_in_first_request": [f"{n} (session {s})" for n, s in got], "absent": absent,
        "closed_or_superseded_in_session_3": sorted(two_back),
        "start_text_tokens": estimate_tokens(last.state.start_text), "pin_budget": CONFIG.pin_budget,
        "first_request_estimate": last.rows[0].estimate})


def test_five_sessions_one_project_search_reaches_the_facts_of_each_earlier_session(five):
    last = five.sessions[4]
    per_session = {}
    for n, plan in enumerate(five.plans, 1):
        got = reached(last, "project", plan.facts)
        assert got == [f.key for f in plan.facts], n
        assert reached(last, "session", plan.facts) == []
        per_session[f"session_{n}"] = f"{len(got)} of {len(plan.facts)}"
    for call in last.searches("project"):
        assert call.event["earlier_searched"] == 4 and call.event["earlier_not_searched"] == 0
    show("five_sessions.search", per_session)


def test_five_sessions_the_bound_and_the_charges(five):
    numbers = {}
    for n in (2, 3, 4, 5):
        session = five.sessions[n - 1]
        numbers[f"session_{n}"] = check_bound(session)
    numbers["session_5_charges"] = check_charges(five.sessions[4])
    show("five_sessions.bound", numbers)


def test_a_carried_set_over_the_budget_is_cut_inside_it_and_says_how_many_were_omitted(tmp_path, clean_env):
    home, root = tmp_path / "contexto-home", repo(tmp_path)
    long = "keep every migration reversible and name it in the changelog " * 10  # 620 characters
    sessions = []
    for n in range(1, 5):
        plan = Plan(rules=tuple(Rule(f"LONG{n}{i:02d}Q", long) for i in range(12)), pins_per_turn=6, turns=2)
        session = Session(home, session_id("long", n), root).run(plan)
        session.end()
        sessions.append(session)
    last = Session(home, session_id("long", 5), root)
    last.turn(0, first_turn(Plan(turns=1)), {})
    start = last.state.start_text
    shown, cut = carried_lines(start), omitted(start)
    total = 4 * 12
    assert estimate_tokens(start) <= CONFIG.pin_budget and len(shown) + cut == total and cut > 0
    names = [name for _, _, _, name in shown]
    newest_first = [f"LONG{n}{i:02d}Q" for n in range(4, 0, -1) for i in range(11, -1, -1)]
    assert names == newest_first[:len(names)]  # the oldest are the ones left out
    assert "contexto_state shows it whole" in start
    assert last.rows[0].real <= CONFIG.bound and last.rows[0].estimate <= CONFIG.bound
    line = start.splitlines()[-1]
    assert line == f"({cut} more pinned items omitted; call contexto_state for the full list)"
    show("five_sessions.over_budget", {
        "records": total, "shown": len(shown), "omitted": cut, "omitted_line": line,
        "start_text_tokens": estimate_tokens(start), "pin_budget": CONFIG.pin_budget,
        "full_section_tokens": estimate_tokens(last.store.state_text())})
