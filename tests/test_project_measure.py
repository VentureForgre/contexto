"""Project memory measured at zero spend (D69, the owner's "Measure at zero spend"): synthetic sessions of
one project through the real launcher store, the real engine and the real MCP server
(``tests/synthetic_project.py``), with a stub's usage and no model.

- Two sessions: which of the first session's rules and decisions are in the second's first request, which
  are absent and why, which the preamble holds after the second's first rotation, that no unpinned fact is
  in its window, how many facts one project-scope search reaches (and a session-scope one: none), the
  facts said in the first session's last turns among them (on no page, found in its last window), and the
  sizes of the start text, the preamble and the first request with project memory on and off. The same
  two sessions on Codex's Responses API (``two_codex``) carry the same records and reach the same facts.
- Five sessions: the exact still-open set at the fifth session's start, in order and marked with its
  session, after closures and replacements made through the MCP server in later sessions; the facts of
  each earlier session one search reaches; and the omitted line when the carried set is over the budget.
- The bound: every request of the later sessions at or under it, by the engine's estimate and the stub's
  count, with the carried text present; every search charged by the one rule, and no room below zero.
- Fifty earlier sessions: the time project memory adds to a session's start and the latency of searches,
  cold and warm, in process, also with each earlier session's last window about 220 KB
  (``test_fifty_earlier_sessions_time``, deselect it with ``-k "not fifty"``).

Every number is printed (``pytest -s``) and, when ``CONTEXTO_MEASURE_OUT`` names a file, written to it as
JSON. The content is synthetic and fixed, so every count is the same on every run; the times are not, and
only generous ceilings are asserted on them.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from contexto import launcher, mcp
from contexto.canonical import canonical_bytes
from contexto.rewrite import MESSAGES, RewriteEngine, compose_preamble
from contexto.store import SessionStore
from contexto.store import project as project_module
from contexto.store.project import read_records
from contexto.tokens import estimate_tokens
from synthetic_project import (
    CONFIG,
    MARK,
    RESPONSES,
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
from test_rewrite import FILL, request, user

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
# The role a line in view names, by where the fact was said (review B on D69, finding 1).
ROLE_OF = {"prompt": "user", "tool": "tool result", "answer": "assistant"}


@dataclass
class Two:
    first: Session
    second: Session
    off: Session | None
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
    assert len(pins) == len(RULES) + len(DECISIONS)
    assert sum(r.kind == "closure" for r in records) == len(CLOSED)
    assert len(first.rotations()) >= 3 and not OFF_PATH & every_event(first)
    check_replies(first)
    assert max(fact_turns(first, FACTS)) < first.paged_turns()  # every fact the plan spreads is paged out
    assert min(fact_turns(first, TAIL)) >= first.paged_turns()  # and the tail's are in the retained window


def check_replies(session: Session) -> None:
    """Every pin and closure succeeded, and the next main request holds its call and the server's own
    reply: what a rule that confirms main-conversation pins from the conversation reads."""
    for call in session.calls:
        assert call.result["isError"] is False, call.result
        if call.tool == "contexto_pin":
            reply = f"Recorded {call.arguments['kind']} {call.result['structuredContent']['record_id']}."
        elif call.tool == "contexto_close":
            reply = f"Closed {call.arguments['obligation_id']} with {call.result['structuredContent']['record_id']}."
        else:
            continue
        assert call.result["content"][0]["text"].startswith(reply)
        loop = [row for row in session.rows if row.turn == call.turn][-1]  # the request after the tool call
        results = [m for m in loop.upstream["messages"] if m["role"] == "user" and isinstance(m["content"], list)
                   and any(b.get("type") == "tool_result" for b in m["content"])]
        assert any(b.get("type") == "tool_result" and b["content"] == call.result["content"]
                   for b in results[-1]["content"])


def fact_turns(session: Session, said: tuple[Fact, ...]) -> list[int]:
    """The turns of ``session`` that say one of the facts."""
    starts = [*session.turn_starts, len(session.history)]
    return [t for t in range(len(session.turn_starts))
            if any(f.key in str(session.history[starts[t]:starts[t + 1]]) for f in said)]


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
    show("two_sessions.search", {
        "facts": len(FACTS), "reached_project_scope": len(project), "reached_session_scope": len(session),
        "reached_by_place": by_place, "first_session_turns": len(two.first.turn_starts),
        "first_session_turns_paged": two.first.paged_turns(),
        "tail_facts_in_the_first_sessions_retained_window": len(TAIL),
        "tail_facts_reached_project_scope": len(reached(second, "project", TAIL))})


def test_two_sessions_a_fact_said_in_the_last_turns_of_the_first_session_is_reachable(two):
    """The first session's last turns were never paged out; its last retained-window snapshot holds them,
    and a project-scope search shows the line under the session, as in view at its last request."""
    first, second = two.first, two.second
    assert reached(second, "project", TAIL) == [f.key for f in TAIL]
    assert reached(second, "session", TAIL) == []
    turns = fact_turns(first, TAIL)
    by_key = {c.arguments["query"]: c for c in second.searches("project")}
    for fact, turn in zip(TAIL, turns):
        call = by_key[fact.key]
        lines = call.text.splitlines()
        line = next(x for x in lines if fact.value in x)
        assert line == f"> in view at its last request (turn {turn}, {ROLE_OF[fact.where]}): {fact.line}"
        assert lines[lines.index(line) - 1].startswith(f"[session {first.label}, ")
        assert mcp.EARLIER_IN_VIEW_NOTE in lines and mcp.EARLIER_READ_ON not in lines  # no page to read on
        assert call.event["earlier_in_view_hits"] == 1 and call.event["earlier_hits"] == 0
        spans = call.event["returned"]
        assert [s["source"] for s in spans] == ["earlier_in_view"] and "page_id" not in spans[0]
    for fact in FACTS:  # a paged fact is on a page, not in the last window
        assert "in view at its last request" not in by_key[fact.key].text


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


# Two sessions on Codex (the Responses API) ----------------------------------------------------------

@pytest.fixture(scope="module")
def two_codex(tmp_path_factory, clean_env):
    """The two-session script as Codex sends it: the same plans, pins, closures and searches."""
    base = tmp_path_factory.mktemp("two-codex")
    home, root = base / "contexto-home", repo(base)
    first = Session(home, session_id("codex", 1), root, dialect=RESPONSES).run(
        Plan(rules=RULES, decisions=DECISIONS, facts=FACTS, closes=CLOSED, turns=50, tail=TAIL))
    first.end()
    keys = tuple(f.key for f in FACTS + TAIL)
    second = Session(home, session_id("codex", 2), root, dialect=RESPONSES).run(Plan(searches=keys, turns=40))
    second.end()
    return Two(first, second, None, root)


def test_two_codex_sessions_carry_the_open_records_and_one_search_reaches_every_fact(two_codex):
    first, second = two_codex.first, two_codex.second
    assert all(call.result["isError"] is False for call in first.calls + second.calls)
    assert len(first.rotations()) >= 3 and first.paged_turns() < len(first.turn_starts)
    assert min(fact_turns(first, TAIL)) >= first.paged_turns() > max(fact_turns(first, FACTS))
    start = second.state.start_text
    window = second.text(0)
    names = [r.name for r in RULES] + [d.name for d in DECISIONS]
    present = [n for n in names if n in window]
    assert present == [r.name for r in RULES if r.name not in CLOSED] + ["DECISION02Q", "DECISION03Q"]
    assert [name for _, _, _, name in carried_lines(start)] == ["RULE05Q", "RULE03Q", "RULE02Q", "RULE00Q",
                                                                 "DECISION03Q", "DECISION02Q"]
    assert second.start_line == (f"contexto: project memory: 6 records carried from 1 of 1 earlier session in "
                                 f"{two_codex.root} (list: contexto project show; off: --no-project-memory)")
    assert [f.key for f in FACTS + TAIL if f.value in window] == []
    project, tail = reached(second, "project", FACTS), reached(second, "project", TAIL)
    assert project == [f.key for f in FACTS] and tail == [f.key for f in TAIL]
    assert reached(second, "session", FACTS + TAIL) == []
    assert second.rotations() and max(row.real for row in second.rows) <= CONFIG.bound
    assert max(row.estimate for row in second.rows) <= CONFIG.bound
    assert all(MARK in second.text(i) for i in range(len(second.rows))) and not OFF_PATH & every_event(second)
    charges = check_charges(second)
    show("two_sessions_codex", {
        "present_in_first_request": present, "facts": len(FACTS), "reached_project_scope": len(project),
        "tail_facts_reached_project_scope": len(tail), "first_session_turns": len(first.turn_starts),
        "first_session_turns_paged": first.paged_turns(), "start_text_tokens": estimate_tokens(start),
        "requests": len(second.rows), "rotations": len(second.rotations()),
        "largest_real": max(row.real for row in second.rows),
        "largest_estimate": max(row.estimate for row in second.rows), "bound": CONFIG.bound,
        "estimate_error_requests": estimate_errors(second), **charges})


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
        check_replies(session)


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


# Fifty earlier sessions -----------------------------------------------------------------------------

EARLIER, RECORDS, PAGES = 50, 20, 40  # earlier sessions of the project, pinned records and pages of each
OTHERS, OTHER_PROJECTS = 150, 15  # sessions of other projects in the same home, and how many projects
REPEATS = 5
# Generous ceilings (seconds) for a loaded CI runner; facts.md measured about 50 ms (start line, cold),
# 8 ms (session scope, cold) and 620 ms (project scope, cold) on a machine at load average 27 to 50.
CEILINGS = {"start_added_cold": 5.0, "start_added_warm": 2.0, "session_search_cold": 2.0,
            "session_search_warm": 2.0, "project_search_cold": 30.0, "project_search_warm": 15.0,
            "project_search_large_windows_cold": 30.0, "project_search_large_windows_warm": 15.0}
WINDOW_ENTRIES = 110  # of about 2 KB each: a last window of about 220 KB, as a long session leaves it
QUERY = "deploy key"  # on two pages of every session of the project, so every earlier session matches


def page_turn(text: str) -> list[dict]:
    return [{"role": "user", "content": [{"type": "text", "text": text}]},
            {"role": "assistant", "content": [{"type": "text", "text": f"noted\n{FILL}"}]}]


def earlier_session(home: Path, sid: str, root: Path, n: int, records: int, pages: int) -> None:
    """A session of ``root`` as a long one ends: ``records`` pins the model made through the MCP server in
    the main conversation's tool loop (two turns of the real engine, half of them in each, the calls and
    the server's replies in the next request), then ``pages`` paged turns of about 4 KB, two of which
    mention the deploy key and one a fact of its own."""
    session = Session(home, sid, root)
    store = session.store
    for t, part in enumerate((range(0, records // 2), range(records // 2, records))):
        calls = [("contexto_pin", lambda i=i: {"kind": "obligation" if i % 2 == 0 else "decision",
                                              "text": f"record {i} of session {n}: keep the migration for "
                                                      f"table {i} reversible"}) for i in part]
        session.turn(t, Turn(f"prompt {t}", f"output {t}", f"answer {t}", calls), {})
    assert all(c.result["isError"] is False for c in session.calls)
    texts = [f"turn {j} of session {n}\n{FILL}" for j in range(pages)]
    for j in (3 % pages, (pages // 2) % pages):
        texts[j] = f"turn {j} of session {n}: the deploy key was rotated to key-{n}-{j}\n{FILL}"
    texts[(n + 7) % pages] += f"\nFor the record, FIFTYKEY{n:03d}Z is fifty-value-{n:03d}."
    store.page_out([page_turn(t) for t in texts], 2)  # after the two turns the engine saw
    store.release()


def median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples) * 1000, 2)


def timed(action: Callable[[], object]) -> tuple[float, object]:
    started = time.perf_counter()
    result = action()
    return time.perf_counter() - started, result


class Reads:
    """Every path under ``sessions`` that the file functions are asked for while installed, outside ``own``."""

    NAMES = ("open", "scandir", "listdir", "stat", "lstat")

    def __init__(self, sessions: Path, own: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sessions, self.own, self.mp, self.outside = str(sessions), str(own), monkeypatch, []

    def _wrap(self, real: Callable) -> Callable:
        def spy(path=".", *args, **kwargs):
            if isinstance(path, (str, os.PathLike)):
                text = os.fspath(path)
                text = text if isinstance(text, str) else os.fsdecode(text)
                if text.startswith(self.sessions + os.sep) and not (text == self.own or
                                                                   text.startswith(self.own + os.sep)):
                    self.outside.append(text)
            return real(path, *args, **kwargs)
        return spy

    def __enter__(self) -> Reads:
        for name in self.NAMES:
            self.mp.setattr(os, name, self._wrap(getattr(os, name)))
        self.mp.setattr(builtins, "open", self._wrap(builtins.open))
        return self

    def __exit__(self, *exc) -> None:
        self.mp.undo()


def start(home: Path, root: Path, sid: str, memory: bool, seen: dict[str, str] | None = None) -> dict[str, float]:
    """A session start in process, as ``contexto run`` makes it: open the session, record the memory
    choice, the start line, and the engine's first request (which reads the carried text). The session
    is removed afterwards, so each start sees the same project. ``seen`` gets the line and the text."""
    total = time.perf_counter()
    store = launcher.open_session(home, sid, root)
    launcher.record_project_memory(store, memory)
    err = io.StringIO()
    notice, _ = timed(lambda: launcher.project_memory_notice(store, err))
    engine = RewriteEngine(store, config=CONFIG)
    first, (upstream, _, events) = timed(lambda: engine(request([user(f"prompt 0\n{FILL}", context=True)]), {},
                                                        MESSAGES))
    total = time.perf_counter() - total
    assert (MARK in json.dumps(upstream)) is memory
    assert any(e["event"] == "start_text" for e in events) is memory
    if seen is not None:
        seen.update(line=err.getvalue().strip(), text=next(iter(engine.states.values())).start_text)
    store.release()
    shutil.rmtree(store.dir)
    return {"total": total, "notice": notice, "first_request": first}


def searcher(home: Path, sid: str) -> tuple[SessionStore, mcp.Server]:
    """The current session reopened, as the MCP server opens it, with a fresh process cache."""
    project_module.forget_cache()
    store = SessionStore(home, sid)
    store.set_window(200_000, 3.2)
    return store, mcp.Server(store, io.StringIO(), io.StringIO())


def search(store: SessionStore, server: mcp.Server, scope: str, query: str = QUERY) -> tuple[float, dict, dict]:
    store.set_turn_room("1", 20_000)
    arguments = {"query": query} if scope == "session" else {"query": query, "scope": scope}
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_search", "arguments": arguments}})
    elapsed, response = timed(lambda: server.handle_line(line))
    result = response["result"]
    assert result["isError"] is False, result
    event = [json.loads(x) for x in (store.dir / "log.jsonl").read_text().splitlines()][-1]
    assert event["event"] == "mcp_call" and event["turn_budget"] == "charged" and event["room_left"] >= 0
    return elapsed, result, event


def test_fifty_earlier_sessions_time(tmp_path, clean_env, monkeypatch):
    """The 50-session timing: what project memory adds to a session's start, and search latency, cold
    (the process's project cache emptied; the OS file cache stays warm) and warm, in process."""
    load_before = os.getloadavg()
    home, root = tmp_path / "contexto-home", repo(tmp_path)
    built, _ = timed(lambda: [earlier_session(home, session_id("fifty", n), root, n, RECORDS, PAGES)
                              for n in range(EARLIER)])
    for n in range(OTHERS):
        other = repo(tmp_path / "others", f"project-{n % OTHER_PROJECTS}") if n < OTHER_PROJECTS else \
            tmp_path.resolve() / "others" / f"project-{n % OTHER_PROJECTS}"
        earlier_session(home, session_id("other", n), other, 1_000 + n, 5, 10)
    project = project_module.Project(home, root)  # the view of a session started now
    assert len(project.earlier) == EARLIER and len(project.carried()) == EARLIER * RECORDS

    times: dict[str, list[float]] = {}
    seen: dict[str, str] = {}

    def add(name: str, value: float) -> None:
        times.setdefault(name, []).append(value)

    # The session start, project memory on and off, cold and warm.
    for i in range(REPEATS):
        for memory in (True, False):
            tag = "on" if memory else "off"
            project_module.forget_cache()
            cold = start(home, root, session_id(f"start-{tag}-cold", i), memory, seen if memory else None)
            warm = start(home, root, session_id(f"start-{tag}-warm", i), memory)
            for name, value in cold.items():
                add(f"start_{tag}_cold_{name}", value)
            for name, value in warm.items():
                add(f"start_{tag}_warm_{name}", value)
    for temp in ("cold", "warm"):
        times[f"start_added_{temp}"] = [on - off for on, off in zip(times[f"start_on_{temp}_total"],
                                                                     times[f"start_off_{temp}_total"])]

    # A session-scope search reads nothing outside its own session, here and in a home with no other session.
    current = session_id("fifty", "current")  # the session that searches: 40 pages of its own
    earlier_session(home, current, root, 999, 0, PAGES)
    solo = tmp_path / "solo-home"
    (solo / "sessions").mkdir(parents=True, mode=0o700)
    shutil.copytree(home / "sessions" / current, solo / "sessions" / current)
    for name, where in (("session_search", home), ("session_search_alone", solo)):
        for _ in range(REPEATS):
            store, server = searcher(where, current)
            with Reads(where / "sessions", store.dir, monkeypatch) as reads:
                cold, result, event = search(store, server, "session")
                warm, _, _ = search(store, server, "session")
            assert reads.outside == [] and "earlier_searched" not in event
            assert "key-999-3" in result["content"][0]["text"]
            add(f"{name}_cold", cold)
            add(f"{name}_warm", warm)
            store.release()

    # A project-scope search, cold and warm; the same spy sees it read the earlier sessions.
    for _ in range(REPEATS):
        store, server = searcher(home, current)
        with Reads(home / "sessions", store.dir, monkeypatch) as reads:
            cold, result, event = search(store, server, "project")
        # It lists the home's sessions to find the project's, so it reads other projects' meta.json, and
        # nothing else of theirs: no ledger, no page.
        assert any(f"{os.sep}{session_id('fifty', 0)}{os.sep}pages" in p for p in reads.outside)
        others = {session_id("other", n) for n in range(OTHERS)}
        touched = {Path(p).relative_to(home / "sessions").parts for p in reads.outside}
        assert {parts[1:] for parts in touched if parts[0] in others} <= {(), ("meta.json",)}
        assert event["earlier_searched"] == EARLIER and event["earlier_matched"] == EARLIER
        assert event["earlier_not_searched"] == 0 and event["earlier_skipped"] == 0
        add("project_search_cold", cold)
        for _ in range(2):
            warm, _, _ = search(store, server, "project")
            add("project_search_warm", warm)
        one, result, event = search(store, server, "project", "FIFTYKEY007Z")
        assert "fifty-value-007" in result["content"][0]["text"] and event["earlier_matched"] == 1
        add("project_search_one_key_warm", one)
        store.release()

    # The same searches once each earlier session's last window is large: its retained.json, which the
    # engine wrote from two short turns above, replaced by one of WINDOW_ENTRIES entries, one of which
    # holds the query, so every earlier session has a line in view at its last request as well as its pages.
    small_windows = [(home / "sessions" / session_id("fifty", n) / "retained.json").stat().st_size
                     for n in range(EARLIER)]
    for n in range(EARLIER):
        entries = [{"turn": 3 + i, "message_index": 10 + i, "role": "assistant",
                    "text": f"window line {i} of session {n}\n{FILL}"} for i in range(WINDOW_ENTRIES)]
        entries[-1]["text"] += f"\nthe deploy key in view was key-{n}-window"
        (home / "sessions" / session_id("fifty", n) / "retained.json").write_bytes(
            canonical_bytes({"stamp": 9, "epoch": 1, "k": 4, "entries": entries}))
    large_windows = (home / "sessions" / session_id("fifty", 0) / "retained.json").stat().st_size
    for _ in range(REPEATS):
        store, server = searcher(home, current)
        cold, result, event = search(store, server, "project")
        assert event["earlier_in_view_hits"] > 0 and "key-49-window" in result["content"][0]["text"]
        add("project_search_large_windows_cold", cold)
        for _ in range(2):
            warm, _, _ = search(store, server, "project")
            add("project_search_large_windows_warm", warm)
        store.release()

    load_after = os.getloadavg()
    medians = {name: median_ms(values) for name, values in sorted(times.items())}
    margins = {}
    for name, ceiling in CEILINGS.items():
        measured = statistics.median(times[name])
        assert measured <= ceiling, f"{name}: median {measured:.3f} s over the ceiling of {ceiling} s"
        margins[name] = f"median {measured * 1000:.1f} ms against a ceiling of {ceiling:g} s " \
                        f"({ceiling / max(measured, 1e-6):.0f}x headroom)"
    sizes = sum(p.stat().st_size for p in (home / "sessions").rglob("*") if p.is_file())
    project_pages = sum(p.stat().st_size for n in range(EARLIER)
                        for p in (home / "sessions" / session_id("fifty", n) / "pages").iterdir())
    shown, cut = len(carried_lines(seen["text"])), omitted(seen["text"])
    assert shown + cut == len(project.carried()) and estimate_tokens(seen["text"]) <= CONFIG.pin_budget
    # The start line counts what the pin budget lets the start text show, and what it leaves out.
    assert seen["line"].startswith(f"contexto: project memory: {shown} of {len(project.carried())} records carried "
                                   f"({cut} did not fit the pin budget; contexto state lists them all) from "
                                   f"{EARLIER} of {EARLIER} earlier sessions in ")
    show("fifty_sessions", {
        "earlier_sessions": EARLIER, "records_each": RECORDS, "pages_each": PAGES,
        "other_project_sessions": OTHERS, "home_bytes": sizes, "project_page_bytes": project_pages,
        "build_seconds_for_the_50": round(built, 2), "carried": len(project.carried()),
        "start_line": seen["line"].replace(str(root), "<root>"), "start_text_lines_shown": shown,
        "start_text_omitted": cut, "start_text_tokens": estimate_tokens(seen["text"]),
        "start_text_shown_by_kind": {kind: sum(k == kind for _, k, _, _ in carried_lines(seen["text"]))
                                     for kind in ("obligation", "decision")},
        "retained_bytes_each_as_built": [min(small_windows), max(small_windows)],
        "retained_bytes_each_large": large_windows,
        "repeats": REPEATS, "medians_ms": medians, "ceilings": margins,
        "samples_ms": {name: [round(v * 1000, 2) for v in values] for name, values in sorted(times.items())},
        "load_average_before": [round(x, 2) for x in load_before],
        "load_average_after": [round(x, 2) for x in load_after],
        "cpu_count": os.cpu_count()})
