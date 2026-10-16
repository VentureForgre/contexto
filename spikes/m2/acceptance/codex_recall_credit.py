"""Acceptance 3 for Codex: recall credit when the answers came through the Responses API (packet A20).

``recall_credit.credit`` finds each user turn's answer among ``/v1/messages`` requests. A Codex
session's model requests go through the proxy as ``POST /responses`` (or ``/v1/responses``), so it
finds none and, since B28, credits nothing. Everything else carries over unchanged, because render and
search read Responses pages (B20, B23): ranges come from the content-free ``mcp_call`` events, plants
from ``render_page``, only ``paged`` spans credit (B19), a page's ranges are merged before the
containment check (B16), and with ``probe_turn_ids`` each probe is cut off at its own probe turn's
answer (B29). This module supplies only the Codex turn ends, through ``credit(..., turn_ends=...)``
(agreed between lanes A and B, 2026-10-16 12:19Z), so the credit rule stays lane B's code.

Codex turn ends, in log order:

- A Codex turn is several requests. Tool rounds, including the tool-search rounds Contexto's tools sit
  behind and exec or shell rounds that call no MCP tool, end in a ``*_call`` output item; the last
  request answers in text. Codex sends every tool call's output back in a new request, so a reply that
  holds a tool call ends a turn only when the turn is cut off (exit, error, or limit), and such a turn
  has no answer.
- The proxy's stream hold (contract PR #74, A9) starts at the first ``response.output_item.added``
  whose item is a ``*_call``, so ``stream_hold.held_events`` is above 0 for a tool round and 0 for a
  reply with no tool call. A turn end is a 2xx Responses request with ``held_events == 0``. Failed
  attempts, which Codex retries, never end a turn. On the A16 stub dry run (#154 with its stub fix),
  the 6 tool rounds log ``held_events`` 3 and the 21 turn ends (15 content replies, 6 probe answers)
  log 0.
- A request without ``stream_hold`` (the hold was skipped: an encoded or unchunked stream, or a log
  from before A9) cannot be classified. If no Responses request carries it, the last successful one
  is the only turn end, which is enough for the session-wide cutoff; per-turn credit then fails closed
  (B29: too few turn ends). Otherwise unclassified requests are left out, which can only under-credit.
- A log with no Responses request at all is a Messages session: ``recall_credit.credit`` decides it
  unchanged.

Reading is content-free, as in ``recall_credit``: session-log events and stored pages only, never a
transcript or tool output.
"""

from __future__ import annotations

from typing import Any

from acceptance import recall_credit

RESPONSES_PATHS = ("/responses", "/v1/responses")  # the proxy's RESPONSES_PATHS


def _is_responses_request(event: dict[str, Any]) -> bool:
    return event.get("event") == "request" and str(event.get("path") or "").split("?")[0] in RESPONSES_PATHS


def _succeeded(event: dict[str, Any]) -> bool:
    status = event.get("status")
    return isinstance(status, int) and 200 <= status < 300


def _held_events(event: dict[str, Any]) -> int | None:
    hold = event.get("stream_hold")
    return hold["held_events"] if isinstance(hold, dict) and isinstance(hold.get("held_events"), int) else None


def turn_ends(session_log: list[dict[str, Any]]) -> list[int]:
    """Log indices of the Responses requests that answered a user turn, in order."""
    ok = [i for i, ev in enumerate(session_log) if _is_responses_request(ev) and _succeeded(ev)]
    if not any(_held_events(session_log[i]) is not None for i in ok):
        return ok[-1:]
    return [i for i in ok if _held_events(session_log[i]) == 0]


def answer_index(session_log: list[dict[str, Any]]) -> int | None:
    """The log index of the request that carried the session's final answer, or None."""
    ends = turn_ends(session_log)
    return ends[-1] if ends else None


def credit(probes: list[Any], answers: str, session_log: list[dict[str, Any]], pages: list[dict[str, Any]],
           probe_turn_ids: tuple[tuple[str, ...], ...] | None = None) -> dict[str, Any]:
    """``recall_credit.credit`` for a Codex session; same arguments and result shape."""
    if not any(_is_responses_request(ev) for ev in session_log):
        return recall_credit.credit(probes, answers, session_log, pages, probe_turn_ids)
    return recall_credit.credit(probes, answers, session_log, pages, probe_turn_ids,
                                turn_ends=turn_ends(session_log))
