"""Acceptance 3 credit for Codex sessions (packet A20): the answer is a Responses request.

Synthetic, content-free session logs shaped like the proxy's: ``request`` entries for
``POST /responses`` with ``stream_hold`` (held_events above 0 for a tool round, 0 for the answer), and
``mcp_call`` events with B15 ``returned`` ranges and B19 sources. The page is a Codex (Responses) page.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import codex_recall_credit, recall_credit, script  # noqa: E402,F401  (script: spikes/m0 path)
from fidelity.l2f import Probe  # noqa: E402

from contexto.store.render import render_page  # noqa: E402

PLANT = "cfg checkout-api.retention_days=juniper-2573"
PAGE = {"id": "pg_cccc000000000001", "turn": 0,
        "stub": {"user_text": "log", "tools": [], "paths": []},
        "messages": [{"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": "log line 0\n" * 20 + PLANT + "\nlog tail\n"}]},
                     {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}]}
PROBE = Probe("q_p_01", "a", "paged_only", "turn-1", "What value did checkout-api.retention_days have?", "juniper-2573",
              subjects=("checkout-api.retention_days",), statements=(PLANT,))
POS = render_page(PAGE).find(PLANT)
WHOLE = {"page_id": PAGE["id"], "start": POS - 5, "end": POS + len(PLANT) + 1}
ANSWERED = "q_p_01: juniper-2573"


def resp(held: int | None, status: int = 200, path: str = "/responses") -> dict:
    ev = {"event": "request", "method": "POST", "path": path, "status": status, "api": "responses"}
    if held is not None:
        ev["stream_hold"] = {"held_events": held, "triggered": True}
    return ev


def call(tool: str, returned: list[dict], **extra) -> dict:
    return {"event": "mcp_call", "tool": tool, "ok": True, "returned": returned, **extra}


def turn(*calls: dict) -> list[dict]:
    """A question turn: a tool-search round, a tool round per call, then the text answer."""
    out = [resp(3)]  # tool search
    for c in calls:
        out += [c, resp(4)]  # the call's result goes back in the next request, itself a tool round...
    out[-1] = resp(0)  # ...except the last, which answers in text
    return out


def test_page_is_a_responses_page_and_holds_the_plant():
    assert POS > 0


def test_credited_through_a_contexto_recall_range():
    log = [resp(0)] + turn(call("contexto_recall", [{"page_id": PAGE["id"], "start": 0, "end": POS + len(PLANT)}]))
    r = codex_recall_credit.credit([PROBE], ANSWERED, log, [PAGE])
    row = r["paged_only"][0]
    assert row["credited"] and row["via"]["tool"] == "contexto_recall" and r["acceptance_3"]


def test_the_messages_rule_finds_no_answer_in_a_codex_log():
    # recall_credit looks for /v1/messages, so on a Codex log it fails closed (B28) and credits nothing.
    log = turn(call("contexto_search", [WHOLE]))
    assert recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["credited"] == 0
    assert codex_recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["credited"] == 1


def test_credited_through_a_contexto_search_paged_hit():
    log = turn(call("contexto_search", [WHOLE]))
    row = codex_recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["paged_only"][0]
    assert row["credited"] and row["via"] == {"tool": "contexto_search", "page_id": PAGE["id"],
                                              "range": [WHOLE["start"], WHOLE["end"]], "calls": 1}


def test_a_recall_over_several_calls_is_merged():
    half = POS + 10
    log = turn(call("contexto_recall", [{"page_id": PAGE["id"], "start": 0, "end": half}]),
               call("contexto_recall", [{"page_id": PAGE["id"], "start": half, "end": half + 200}]))
    row = codex_recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["paged_only"][0]
    assert row["credited"] and row["via"]["calls"] == 2


def test_in_view_spans_never_credit():
    log = turn(call("contexto_search", [{**WHOLE, "source": "in_view"}]))
    r = codex_recall_credit.credit([PROBE], ANSWERED, log, [PAGE])
    assert r["credited"] == 0 and r["in_view_spans"] == 1 and r["correct_not_credited"] == ["q_p_01"]
    paged = turn(call("contexto_search", [{**WHOLE, "source": "paged"}]))
    assert codex_recall_credit.credit([PROBE], ANSWERED, paged, [PAGE])["credited"] == 1


def test_a_call_logged_after_the_answer_never_credits():
    log = turn(call("contexto_search", [])) + [call("contexto_recall", [WHOLE])]
    assert codex_recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["credited"] == 0


def test_the_answer_is_the_text_response_not_a_later_tool_round_or_a_failed_retry():
    log = turn(call("contexto_search", [WHOLE]))
    answer_at = len(log) - 1
    # After the answer: a retried attempt that failed and a round that held a tool call are no answers.
    late = log + [resp(None, status=502), call("contexto_recall", [WHOLE]), resp(5)]
    assert codex_recall_credit.answer_index(late) == answer_at
    r = codex_recall_credit.credit([PROBE], ANSWERED, late, [PAGE])
    assert r["credited"] == 1 and r["paged_only"][0]["via"]["calls"] == 1  # the late recall is not merged in


def test_v1_responses_path_is_recognized():
    log = [dict(e, path="/v1/responses") if e["event"] == "request" else e for e in turn(call("contexto_search", [WHOLE]))]
    assert codex_recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["credited"] == 1


def test_no_answer_among_responses_requests_credits_nothing():
    rounds_only = [resp(3), call("contexto_search", [WHOLE]), resp(4)]
    assert codex_recall_credit.answer_index(rounds_only) is None
    assert codex_recall_credit.credit([PROBE], ANSWERED, rounds_only, [PAGE])["credited"] == 0
    failed = [call("contexto_search", [WHOLE]), resp(0, status=500)]
    assert codex_recall_credit.credit([PROBE], ANSWERED, failed, [PAGE])["credited"] == 0


def test_without_stream_hold_the_last_successful_responses_request_answers():
    log = [resp(None), call("contexto_search", [WHOLE]), resp(None), resp(None, status=502)]
    assert codex_recall_credit.answer_index(log) == 2
    assert codex_recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["credited"] == 1


def test_a_wrong_answer_is_not_credited():
    log = turn(call("contexto_search", [WHOLE]))
    r = codex_recall_credit.credit([PROBE], "q_p_01: unknown", log, [PAGE])
    assert r["credited"] == 0 and r["paged_only"][0]["returned"] and not r["paged_only"][0]["answered_correctly"]


def test_messages_logs_give_exactly_recall_credit_results():
    def req():
        return {"event": "request", "path": "/v1/messages", "status": 200}
    logs = [
        [req(), call("contexto_search", [WHOLE]), req()],
        [req(), call("contexto_recall", [{"page_id": PAGE["id"], "start": 0, "end": POS + 10}]), req()],
        [req(), call("contexto_search", [{**WHOLE, "source": "in_view"}]), req()],
        [req(), req(), call("contexto_recall", [WHOLE])],
        [call("contexto_recall", [WHOLE])],
    ]
    for log in logs:
        for answers in (ANSWERED, "q_p_01: unknown"):
            assert codex_recall_credit.credit([PROBE], answers, log, [PAGE]) == recall_credit.credit(
                [PROBE], answers, log, [PAGE])


PLANT_2 = "cfg billing-api.retry_limit=cobalt-8841"
PAGE_2 = {"id": "pg_cccc000000000002", "turn": 1, "stub": {"user_text": "log", "tools": [], "paths": []},
          "messages": [{"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "log line 1\n" * 20 + PLANT_2 + "\nlog tail\n"}]},
                       {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}]}
PROBE_2 = Probe("q_p_02", "a", "paged_only", "turn-2", "What value did billing-api.retry_limit have?", "cobalt-8841",
                subjects=("billing-api.retry_limit",), statements=(PLANT_2,))
POS_2 = render_page(PAGE_2).find(PLANT_2)
WHOLE_2 = {"page_id": PAGE_2["id"], "start": POS_2 - 5, "end": POS_2 + len(PLANT_2) + 1}
BOTH = "q_p_01: juniper-2573\nq_p_02: cobalt-8841"
TURNS = (("q_p_01",), ("q_p_02",))


def test_turn_ends_are_the_successful_replies_without_a_tool_call():
    log = [resp(0), resp(0), resp(3), call("contexto_search", [WHOLE]), resp(None, status=502), resp(0)]
    assert codex_recall_credit.turn_ends(log) == [0, 1, 5]
    assert codex_recall_credit.turn_ends([resp(None), resp(None), resp(None, status=500)]) == [1]  # no hold info
    assert codex_recall_credit.turn_ends([call("contexto_search", [])]) == []


def test_a_2xx_cut_short_upstream_is_not_a_turn_end():
    """A 200 whose relay failed (``error: "relay: <class>"``) is retried by Codex, so it answers
    nothing, held or not; without hold info the last successful request is the last uncut one."""
    cut = {**resp(0), "error": "relay: IncompleteRead"}
    assert codex_recall_credit.turn_ends([resp(0), cut, resp(0)]) == [0, 2]
    assert codex_recall_credit.turn_ends([resp(None), {**resp(None), "error": "relay: OSError"}]) == [0]


def test_a_recall_in_a_later_probe_turn_does_not_credit_an_earlier_probe():
    # Probe turn 1 answers q_p_01 with no recall of its plant; probe turn 2 recalls both plants.
    log = ([resp(0), resp(0)]  # content turns
           + [resp(3), call("contexto_search", []), resp(0)]  # probe turn 1: q_p_01 answered unaided
           + [resp(3), call("contexto_recall", [WHOLE]), call("contexto_recall", [WHOLE_2]), resp(0)])
    r = codex_recall_credit.credit([PROBE, PROBE_2], BOTH, log, [PAGE, PAGE_2], TURNS)
    rows = {row["id"]: row for row in r["paged_only"]}
    assert not rows["q_p_01"]["credited"] and rows["q_p_02"]["credited"] and r["credited"] == 1
    # Session-wide (no probe_turn_ids) the later recall would have credited q_p_01 as well.
    assert codex_recall_credit.credit([PROBE, PROBE_2], BOTH, log, [PAGE, PAGE_2])["credited"] == 2


def test_a_tool_round_without_an_mcp_call_is_not_a_turn_end():
    # A tool-search or exec round calls no MCP tool. B29's Messages fallback ("no mcp_call before the
    # next request") would take it for a turn end and shift the probe turns; held_events does not.
    log = ([resp(0)]
           + [resp(2), resp(3), call("contexto_search", [WHOLE]), resp(0)]  # probe turn 1: tool search, search
           + [resp(2), resp(3), call("contexto_recall", [WHOLE_2]), resp(0)])  # probe turn 2
    assert codex_recall_credit.turn_ends(log) == [0, 4, 8]
    r = codex_recall_credit.credit([PROBE, PROBE_2], BOTH, log, [PAGE, PAGE_2], TURNS)
    assert r["credited"] == 2


def test_too_few_turn_ends_fail_closed_per_turn():
    log = [resp(None), call("contexto_recall", [WHOLE]), resp(None)]  # no hold info: one turn end only
    r = codex_recall_credit.credit([PROBE, PROBE_2], BOTH, log, [PAGE, PAGE_2], TURNS)
    assert r["credited"] == 0 and r["reason"] == "probe turn answers not found"


def test_messages_logs_with_probe_turns_give_exactly_recall_credit_results():
    def req(held=None):
        ev = {"event": "request", "path": "/v1/messages", "status": 200}
        return ev if held is None else {**ev, "stream_hold": {"held_events": held, "triggered": True}}
    log = [req(0), req(3), call("contexto_search", [WHOLE]), req(0), req(3), call("contexto_recall", [WHOLE_2]), req(0)]
    for turns in (None, TURNS):
        assert codex_recall_credit.credit([PROBE, PROBE_2], BOTH, log, [PAGE, PAGE_2], turns) == recall_credit.credit(
            [PROBE, PROBE_2], BOTH, log, [PAGE, PAGE_2], turns)


def test_a_stub_shaped_log_without_held_events_takes_the_last_answer():
    # An upstream that streams output_item.done but no output_item.added (codex_stub.py before #154's
    # fix) never starts the proxy's hold: every request logs held_events 0, tool rounds included. Each
    # probe turn then reads: the tool round's request, the exec call's search and recall, the answer
    # request. The rule falls back to the last successful request, the last probe turn's answer.
    probe_turn = [resp(0), call("contexto_search", [WHOLE]), call("contexto_recall", [WHOLE]), resp(0)]
    log = [resp(0), resp(0)] + probe_turn + probe_turn
    assert codex_recall_credit.answer_index(log) == len(log) - 1
    assert codex_recall_credit.credit([PROBE], ANSWERED, log, [PAGE])["credited"] == 1


def test_a_turn_cut_off_after_a_held_tool_call_has_no_answer():
    # Codex sends a tool call's output back in a new request, so a reply holding a tool call ends a
    # turn only when the turn is cut off (exit, error, or limit). That turn then has no answer, and the
    # cutoff stays at the last earlier answer: its calls never credit (under-credit, never over).
    answered = turn(call("contexto_search", []))
    cut_off = [resp(3), call("contexto_recall", [WHOLE])]
    assert codex_recall_credit.answer_index(answered + cut_off) == len(answered) - 1
    assert codex_recall_credit.credit([PROBE], ANSWERED, answered + cut_off, [PAGE])["credited"] == 0
    credited_before = turn(call("contexto_search", [WHOLE]))
    assert codex_recall_credit.credit([PROBE], ANSWERED, credited_before + cut_off, [PAGE])["credited"] == 1


def test_turn_ends_that_do_not_match_the_script_credit_nothing():
    log = ([resp(0)]
           + [resp(3), call("contexto_search", [WHOLE]), resp(0)]
           + [resp(3), call("contexto_recall", [WHOLE_2]), resp(0)])  # 3 turn ends
    exact = codex_recall_credit.credit([PROBE, PROBE_2], BOTH, log, [PAGE, PAGE_2], TURNS, expected_turns=3)
    assert exact["credited"] == 2
    for expected in (2, 4):
        r = codex_recall_credit.credit([PROBE, PROBE_2], BOTH, log, [PAGE, PAGE_2], TURNS, expected_turns=expected)
        assert r["credited"] == 0 and r["reason"] == f"turn answers do not match the script (3 found, {expected} expected)"


def test_a_request_logged_without_a_status_counts_as_answered():
    log = [resp(0), call("contexto_search", [WHOLE]), {k: v for k, v in resp(0).items() if k != "status"}]
    assert codex_recall_credit.turn_ends(log) == [0, 2]
