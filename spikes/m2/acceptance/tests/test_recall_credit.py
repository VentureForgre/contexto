"""Acceptance 3 credit from content-free MCP ranges (contract PR #95; B15's 'returned' entries)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import recall_credit, script  # noqa: E402,F401  (script puts spikes/m0 on the path)
from contexto.store.render import render_page  # noqa: E402
from fidelity.l2f import Probe  # noqa: E402

PLANT = "cfg checkout-api.retention_days=juniper-2573"
PAGE = {"id": "pg_aaaa000000000001", "turn": 0, "stub": {"user_text": "log", "tools": [], "paths": []},
        "messages": [{"role": "user", "content": [{"type": "text", "text": "log line 0\n" * 20 + PLANT + "\nlog tail\n"}]}]}
PROBE = Probe("q_p_01", "a", "paged_only", "turn-1", "What value did checkout-api.retention_days have?", "juniper-2573",
              subjects=("checkout-api.retention_days",), statements=(PLANT,))
POS = render_page(PAGE).find(PLANT)
ANSWERED = "q_p_01: juniper-2573"


def _req():
    return {"event": "request", "path": "/v1/messages", "status": 200}


def _call(tool, returned, **extra):
    return {"event": "mcp_call", "tool": tool, "ok": True, "returned": returned, **extra}


def test_credited_through_a_search_hit_whose_lines_hold_the_plant():
    log = [_req(), _call("contexto_search", [{"page_id": PAGE["id"], "start": POS - 5, "end": POS + len(PLANT) + 1}]), _req()]
    r = recall_credit.credit([PROBE], ANSWERED, log, [PAGE])
    row = r["paged_only"][0]
    assert row["credited"] and row["via"]["tool"] == "contexto_search" and r["acceptance_3"] and row["via"]["calls"] == 1


def test_credited_through_a_recall_range():
    log = [_req(), _call("contexto_recall", [{"page_id": PAGE["id"], "start": 0, "end": POS + len(PLANT)}]), _req()]
    assert recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["paged_only"][0]["via"]["tool"] == "contexto_recall"
    # the pre-B15 recall fields (offset, chars) give the same range
    old = [_req(), {"event": "mcp_call", "tool": "contexto_recall", "ok": True, "page_id": PAGE["id"], "offset": 0,
                    "chars": POS + len(PLANT)}, _req()]
    assert recall_credit.credit([PROBE], ANSWERED, old, [PAGE])["credited"] == 1


def test_a_correct_answer_with_no_returned_range_is_not_credited():
    log = [_req(), _call("contexto_search", []), _req()]  # a stub-only hit returns no range
    r = recall_credit.credit([PROBE], ANSWERED, log, [PAGE])
    assert r["credited"] == 0 and r["correct_not_credited"] == ["q_p_01"] and not r["acceptance_3"]


def test_a_returned_range_that_misses_the_plant_is_not_credited():
    short = [_req(), _call("contexto_recall", [{"page_id": PAGE["id"], "start": 0, "end": POS + 10}]), _req()]  # cut mid-line
    assert recall_credit.credit([PROBE], ANSWERED, short, [PAGE])["credited"] == 0
    other = [_req(), _call("contexto_search", [{"page_id": "pg_bbbb000000000002", "start": 0, "end": 10**6}]), _req()]
    assert recall_credit.credit([PROBE], ANSWERED, other, [PAGE])["credited"] == 0  # wrong page


def test_a_call_after_the_answer_or_a_wrong_answer_is_not_credited():
    late = [_req(), _call("contexto_recall", [{"page_id": PAGE["id"], "start": 0, "end": 10**6}])]  # after the last request
    assert recall_credit.credit([PROBE], ANSWERED, late, [PAGE])["credited"] == 0
    hit = [_req(), _call("contexto_recall", [{"page_id": PAGE["id"], "start": 0, "end": 10**6}]), _req()]
    r = recall_credit.credit([PROBE], "q_p_01: unknown", hit, [PAGE])
    assert r["credited"] == 0 and r["paged_only"][0]["returned"] is True


def test_a_line_split_across_paged_recall_ranges_is_credited():
    # B16: one recall returns at most half the remaining room, so a page is read in several calls.
    mid = POS + 10
    log = [_req(), _call("contexto_recall", [{"page_id": PAGE["id"], "start": 0, "end": mid}]),
           _call("contexto_recall", [{"page_id": PAGE["id"], "start": mid, "end": mid + 200}]), _req()]
    row = recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["paged_only"][0]
    assert row["credited"] and row["via"]["calls"] == 2 and row["via"]["range"] == [0, mid + 200]
    gap = [_req(), _call("contexto_recall", [{"page_id": PAGE["id"], "start": 0, "end": mid}]),
           _call("contexto_recall", [{"page_id": PAGE["id"], "start": mid + 1, "end": mid + 200}]), _req()]
    assert recall_credit.credit([PROBE], ANSWERED, gap, [PAGE])["credited"] == 0  # one character never returned


def test_in_view_spans_never_credit_and_unmarked_spans_count_as_paged():
    # B19: every returned span carries source paged or in_view; only paged content credits acceptance 3.
    span = {"page_id": PAGE["id"], "start": POS - 5, "end": POS + len(PLANT) + 1}
    in_view = [_req(), _call("contexto_search", [{**span, "source": "in_view"}]), _req()]
    r = recall_credit.credit([PROBE], ANSWERED, in_view, [PAGE])
    assert r["credited"] == 0 and not r["acceptance_3"] and r["correct_not_credited"] == ["q_p_01"]
    assert r["in_view_spans"] == 1
    # B19's in_view shape has no page_id at all (turn, message_index, start, end)
    shaped = [_req(), _call("contexto_search", [{"source": "in_view", "turn": 17, "message_index": 3, "start": 0, "end": 40}]),
              _req()]
    assert recall_credit.credit([PROBE], ANSWERED, shaped, [PAGE])["credited"] == 0
    for marked in ({**span, "source": "paged"}, span):  # marked paged, and unmarked (logs before B19)
        log = [_req(), _call("contexto_search", [marked]), _req()]
        r = recall_credit.credit([PROBE], ANSWERED, log, [PAGE])
        assert r["credited"] == 1 and r["acceptance_3"] and r["in_view_spans"] == 0
    unknown = [_req(), _call("contexto_search", [{**span, "source": "other"}]), _req()]
    assert recall_credit.credit([PROBE], ANSWERED, unknown, [PAGE])["credited"] == 0  # fails closed
