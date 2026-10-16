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
