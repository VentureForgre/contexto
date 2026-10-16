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


def test_a_log_with_no_answer_request_credits_nothing_and_says_why():
    """B28 (lane A's A20 finding): without a /v1/messages request there is no answer to cut off at,
    so credit fails closed instead of crediting every call."""
    for path in (None, "/v1/responses", "/other"):
        log = [_call("contexto_search", [{"page_id": PAGE["id"], "start": POS - 5, "end": POS + len(PLANT) + 1}])]
        if path:
            log.append({"event": "request", "path": path, "status": 200})
        r = recall_credit.credit([PROBE], ANSWERED, log, [PAGE])
        assert r["credited"] == 0 and r["acceptance_3"] is False
        assert r["reason"] == "answer request not found"
        assert r["paged_only"][0]["answered_correctly"] and not r["paged_only"][0]["returned"]


def test_a_normal_log_has_no_reason_key():
    log = [_req(), _call("contexto_search", [{"page_id": PAGE["id"], "start": POS - 5, "end": POS + len(PLANT) + 1}]), _req()]
    assert "reason" not in recall_credit.credit([PROBE], ANSWERED, log, [PAGE])


# B29 (reviewer-1 on #161): the cutoff is the answer of the probe turn that asked the probe


TURNS = (("q_a_901",), ("q_p_01",), ("q_a_902",), ("q_a_903",))  # q_p_01 is asked in probe turn 2
HIT = [{"page_id": PAGE["id"], "start": POS - 5, "end": POS + len(PLANT) + 1}]


def _turn(*events):
    """A probe turn: its requests and calls, ending with the answer request (no call after it)."""
    return [*events, _req()]


def test_a_range_returned_in_a_later_probe_turn_does_not_credit_an_earlier_answer():
    log = [_req(),  # a content turn
           *_turn(), *_turn(), *_turn(_req(), _call("contexto_search", HIT)), *_turn()]  # the hit in turn 3
    r = recall_credit.credit([PROBE], ANSWERED, log, [PAGE], probe_turn_ids=TURNS)
    assert r["credited"] == 0 and not r["paged_only"][0]["returned"]
    assert recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["credited"] == 1  # the old session-wide rule did


def test_a_range_in_the_probes_own_turn_before_its_answer_credits_it():
    log = [_req(), *_turn(), *_turn(_req(), _call("contexto_search", HIT)), *_turn(), *_turn()]
    r = recall_credit.credit([PROBE], ANSWERED, log, [PAGE], probe_turn_ids=TURNS)
    assert r["credited"] == 1 and r["paged_only"][0]["via"]["tool"] == "contexto_search" and "reason" not in r


def test_turn_answers_are_the_messages_requests_with_no_call_after_them():
    log = [_req(), _req(), _call("contexto_search", HIT), _req(), {"event": "request", "path": "/v1/messages/count_tokens"}]
    assert recall_credit.turn_answer_indices(log) == [0, 3]


def test_too_few_turn_answers_fail_closed_and_say_why():
    log = [_req(), _call("contexto_search", HIT), _req()]  # one turn answer, four probe turns
    r = recall_credit.credit([PROBE], ANSWERED, log, [PAGE], probe_turn_ids=TURNS)
    assert r["credited"] == 0 and r["acceptance_3"] is False and r["reason"] == "probe turn answers not found"
    no_request = recall_credit.credit([PROBE], ANSWERED, [_call("contexto_search", HIT)], [PAGE], probe_turn_ids=TURNS)
    assert no_request["reason"] == "answer request not found" and no_request["credited"] == 0  # B28 still holds


def test_a_non_mcp_tool_round_is_not_a_turn_end_when_the_stream_hold_is_logged():
    """A Bash or Read round calls no MCP tool, but the proxy held its tool_use: not a turn end."""
    def req(held):
        return {**_req(), "stream_hold": {"triggered": bool(held), "held_events": held}}
    log = [req(0), req(5), req(3), _call("contexto_search", HIT), req(0)]  # a Bash round, a search round, the answer
    assert recall_credit.turn_answer_indices(log) == [0, 4]
    assert recall_credit.turn_answer_indices([_req(), _req()]) == [0, 1]  # no stream_hold: the mcp_call fallback


def test_caller_supplied_turn_ends_replace_the_messages_rules():
    """Lane A's Codex credit passes its own turn ends (2xx /responses with held_events 0)."""
    codex = [{"event": "request", "path": "/v1/responses", "status": 200}, _call("contexto_search", HIT),
             {"event": "request", "path": "/v1/responses", "status": 200}] * 4
    ends = [2, 5, 8, 11]  # turn 2's answer is index 5; the hit at index 4 precedes it
    r = recall_credit.credit([PROBE], ANSWERED, codex, [PAGE], probe_turn_ids=TURNS, turn_ends=ends)
    assert r["credited"] == 1 and "reason" not in r
    assert recall_credit.credit([PROBE], ANSWERED, codex, [PAGE])["reason"] == "answer request not found"
    assert recall_credit.credit([PROBE], ANSWERED, codex, [PAGE], turn_ends=[])["reason"] == "answer request not found"
    log = [_req(), *_turn(), *_turn(_req(), _call("contexto_search", HIT)), *_turn(), *_turn()]
    assert (recall_credit.credit([PROBE], ANSWERED, log, [PAGE], probe_turn_ids=TURNS)
            == recall_credit.credit([PROBE], ANSWERED, log, [PAGE], probe_turn_ids=TURNS,
                                    turn_ends=recall_credit.turn_answer_indices(log)))  # omitted = today's result


def test_a_failed_request_and_its_retry_are_one_turn_end():
    """A local 502 (no stream_hold) followed by its retried 200 is one turn end, under either rule."""
    failed = {"event": "request", "path": "/v1/messages", "status": 502}
    held = {**_req(), "stream_hold": {"triggered": False, "held_events": 0}}
    assert recall_credit.turn_answer_indices([_req(), failed, _req()]) == [0, 2]  # the mcp_call fallback
    assert recall_credit.turn_answer_indices([held, failed, held]) == [0, 2]  # the stream-hold rule
    assert recall_credit.turn_answer_indices([{**_req(), "status": None}]) == [0]  # no status: counted
