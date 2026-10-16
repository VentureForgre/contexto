"""Offline search reachability of probe answers (synthetic pages, no model calls)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import reach, script  # noqa: E402
from fidelity.l2f import Probe  # noqa: E402


def _page(pid: str, turn: int, text: str) -> dict:
    return {"id": pid, "turn": turn, "stub": {"user_text": text[:40], "tools": [], "paths": []},
            "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}


PAGES = [
    _page("pg_a", 0, "log s0 ... cfg checkout-api.retention_days=juniper-2573 ... more"),
    _page("pg_b", 1, "Ownership note: the ledger-svc service is owned by ash9."),
    _page("pg_c", 2, "Action item AI-1: rotate keys.\nAction item AI-1 is done."),
]


def test_terms_are_subjects_or_question_identifiers():
    p = Probe("q1", "a", "paged_only", "t", "What value did checkout-api.retention_days have?", "juniper-2573",
              subjects=("checkout-api.retention_days",), statements=("cfg checkout-api.retention_days=juniper-2573",))
    assert reach.key_terms(p) == ["checkout-api.retention_days"]
    s = Probe("q2", "a", "obligations", "set", "Of action items AI-1, AI-2, which are still open?", "AI-2",
              kind="set", statements=("Action item AI-1: rotate keys.", "Action item AI-1 is done."))
    assert reach.key_terms(s) == ["AI-1", "AI-2"]
    assert reach.needed_statements(s, ["AI-1", "AI-2"]) == ["Action item AI-1 is done."]


def test_reachable_paged_and_window_statements():
    paged = Probe("q1", "a", "paged_only", "t", "?", "juniper-2573", subjects=("checkout-api.retention_days",),
                  statements=("cfg checkout-api.retention_days=juniper-2573",))
    hop = Probe("q2", "a", "multihop", "2-hop", "?", "team-x", subjects=("ledger-svc", "ash9"),
                statements=("Ownership note: the ledger-svc service is owned by ash9.", "Staffing note: ash9 belongs to team-x."))
    r = reach.reach([paged, hop], PAGES)
    assert r["reachable"] == 2 and r["unreachable"] == []
    rows = {row["id"]: row for row in r["rows"]}
    assert rows["q1"]["needs"] == [{"page": "pg_a", "in_window": False, "hit": True,
                                    "term": "checkout-api.retention_days", "rank": 1}]
    assert rows["q2"]["needs"][1]["in_window"] is True  # the second hop is not on any page
    assert r["by_type"]["multihop"] == {"probes": 1, "reachable": 1, "needs_paged": 1}


def test_a_page_the_terms_do_not_return_is_unreachable():
    # The answer sits on pg_a, but the probe's own term never appears there (not vacuous).
    p = Probe("q3", "a", "distractor", "t", "?", "juniper-2573", subjects=("billing.retention",),
              statements=("cfg checkout-api.retention_days=juniper-2573",))
    r = reach.reach([p], PAGES)
    assert r["reachable"] == 0 and r["unreachable"][0]["needs"][0] == {"page": "pg_a", "in_window": False,
                                                                       "hit": False, "term": None, "rank": None}


def test_the_acceptance_script_has_terms_for_every_probe():
    s = script.build(turns=4, turn_bytes=8_000)
    assert all(reach.key_terms(p) for p in s.probes)
    assert all(reach.needed_statements(p, reach.key_terms(p)) for p in s.probes)


def test_a_multihop_probe_with_one_unreachable_hop_is_unreachable():
    hop = Probe("q4", "a", "multihop", "3-hop", "?", "west", subjects=("ledger-svc", "ash9", "team-x"),
                statements=("Ownership note: the ledger-svc service is owned by ash9.",
                            "Action item AI-1: rotate keys.",  # on pg_c, which none of the hop names returns
                            "Coverage note: team-x covers region west."))
    r = reach.reach([hop], PAGES, turn_texts=["", "", "", "Coverage note: team-x covers region west."])
    needs = r["rows"][0]["needs"]
    assert [n["hit"] for n in needs] == [True, False, True] and r["reachable"] == 0
    assert needs[2]["in_window"] is True  # turn 3 is not paged and holds the last hop


def test_a_statement_on_no_page_and_in_no_window_turn_is_unreachable():
    p = Probe("q5", "a", "chain", "t", "?", "v9", subjects=("svc.key",), statements=("svc.key is now v9.",))
    # turn 0's opening is on pg_t (so turn 0 is paged), but the statement later in it is not
    opening = "Next part of the working log: s0 line 0000 level=INFO " * 4
    pages = [_page("pg_t", 0, opening)]
    r = reach.reach([p], pages, turn_texts=[opening + "\nsvc.key is now v9.", ""])
    assert r["rows"][0]["needs"][0] == {"page": None, "in_window": False, "hit": False} and r["reachable"] == 0


def test_ranks_beyond_the_search_limit_do_not_count(monkeypatch):
    monkeypatch.setattr(reach, "SEARCH_LIMIT", 1)  # contexto_search returns at most SEARCH_LIMIT ids
    pages = [_page("pg_old", 0, "svc.key is v1"), _page("pg_new", 5, "svc.key mentioned again")]
    p = Probe("q6", "a", "chain", "t", "?", "v1", subjects=("svc.key",), statements=("svc.key is v1",))
    assert reach.reach([p], pages)["reachable"] == 0  # pg_old ranks 2nd, past the limit


def test_pages_map_to_script_turns_by_content_and_drift_is_reported():
    # Reviewer-2 on PR #88: page turn numbers can drift (for example when carried text is paged).
    turns = ["Opening turn zero text " * 8, "Next part one " * 10, "Next part two " * 10]
    pages = [_page("pg_x", 0, turns[0]), _page("pg_y", 2, turns[1]),  # page says 2, content is turn 1
             _page("pg_z", 3, "carried system text only")]
    m = reach.map_pages(pages, turns)
    assert [r["script_turn"] for r in m["pages"]] == [0, 1, None]
    assert [r["id"] for r in m["drift"]] == ["pg_y"] and m["unmatched"] == ["pg_z"] and m["ambiguous"] == []
    # the window follows content: turn 2 is not paged, so a statement there is in the window
    p = Probe("q7", "a", "chain", "t", "?", "two", subjects=("part two",), statements=("Next part two",))
    r = reach.reach([p], pages, turn_texts=turns)
    assert r["rows"][0]["needs"][0]["in_window"] is True and r["turn_mapping"]["drift"][0]["id"] == "pg_y"


def test_reach_fails_closed_on_an_unmatched_or_ambiguous_page():
    # Reviewer-2 on PR #91: such a page may hold any turn, so nothing it could hold counts as in view.
    turns = ["Opening turn zero text " * 8, "svc.key is now v9. " + "Next part one " * 10]
    unmatched = [_page("pg_u", 1, "carried system text only")]  # its number points at turn 1
    p = Probe("q8", "a", "chain", "t", "?", "v9", subjects=("svc.key",), statements=("svc.key is now v9.",))
    r = reach.reach([p], unmatched, turn_texts=turns)
    assert r["mapping_ok"] is False and r["turn_mapping"]["unmatched"] == ["pg_u"]
    assert r["rows"][0]["needs"][0]["in_window"] is False and r["reachable"] == 0
    twin = "Same opening for both turns " * 6
    ambiguous = [_page("pg_a2", 0, twin)]
    r = reach.reach([p], ambiguous, turn_texts=[twin + "x", twin + "svc.key is now v9."])
    assert r["turn_mapping"]["ambiguous"] == ["pg_a2"] and r["mapping_ok"] is False and r["reachable"] == 0
