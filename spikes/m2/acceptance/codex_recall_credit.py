"""Acceptance 3 for Codex: recall credit when the answer came through the Responses API (packet A20).

``recall_credit.credit`` finds the answering request as the last ``/v1/messages`` request. A Codex
session's model requests go through the proxy as ``POST /responses`` (or ``/v1/responses``), so it
finds none and, since B28, credits nothing. Everything else carries over unchanged, because render and search read
Responses pages (B20, B23): ranges come from the content-free ``mcp_call`` events, plants from
``render_page``, only ``paged`` spans credit (B19), and a page's ranges are merged before the
containment check (B16). This module therefore differs only in how it finds the answer:

- the answer is the last Responses request that succeeded (2xx) and carried no tool call. A Codex
  turn is several requests: tool rounds, including the tool-search rounds Contexto's tools sit
  behind, each end in a ``*_call`` item, and the final request answers in text. The proxy's stream
  hold (contract PR #74) starts at the first ``*_call`` item, so its ``held_events`` is above 0 for a
  tool round and 0 for the answer. Failed attempts that Codex retries are not answers.
- a log whose Responses requests carry no ``stream_hold`` (the hold was skipped, or a log from before
  it) falls back to the last successful Responses request.
- a log with no Responses request at all is a Messages session: ``recall_credit.credit`` decides it
  unchanged.
- a Codex log with Responses requests but no answer among them (every one a tool round, or every one
  failed) credits nothing: no call can be shown to precede an answer.

Reading is content-free, as in ``recall_credit``: session-log events and stored pages only, never a
transcript or tool output. The answer is located by index and handed to ``recall_credit.credit``
through a view of the log in which that request is the only request event, so the credit rule itself
is lane B's code, not a copy.
"""

from __future__ import annotations

from typing import Any

from acceptance import recall_credit

RESPONSES_PATHS = ("/responses", "/v1/responses")  # the proxy's RESPONSES_PATHS


def _path(event: dict[str, Any]) -> str:
    return str(event.get("path") or "").split("?")[0]


def _is_responses_request(event: dict[str, Any]) -> bool:
    return event.get("event") == "request" and _path(event) in RESPONSES_PATHS


def _succeeded(event: dict[str, Any]) -> bool:
    status = event.get("status")
    return isinstance(status, int) and 200 <= status < 300


def _tool_round(event: dict[str, Any]) -> bool | None:
    """True for a response that held a tool call, False for one that did not, None when unknown."""
    hold = event.get("stream_hold")
    if not isinstance(hold, dict) or not isinstance(hold.get("held_events"), int):
        return None
    return hold["held_events"] > 0


def answer_index(session_log: list[dict[str, Any]]) -> int | None:
    """The log index of the Responses request that carried the final answer, or None."""
    ok = [i for i, ev in enumerate(session_log) if _is_responses_request(ev) and _succeeded(ev)]
    known = [i for i in ok if _tool_round(session_log[i]) is not None]
    if known:
        answers = [i for i in known if not _tool_round(session_log[i])]
        return answers[-1] if answers else None
    return ok[-1] if ok else None


def answer_view(session_log: list[dict[str, Any]], answer_at: int | None) -> list[dict[str, Any]]:
    """The log with every request event neutralized except ``answer_at``, which reads as the Messages
    request ``recall_credit.answer_index`` looks for. Indices, and every other event, are unchanged."""
    view = []
    for i, ev in enumerate(session_log):
        if i == answer_at:
            view.append({"event": "request", "path": "/v1/messages", "status": ev.get("status")})
        elif ev.get("event") == "request":
            view.append({"event": "request_not_answer"})
        else:
            view.append(ev)
    return view


def credit(probes: list[Any], answers: str, session_log: list[dict[str, Any]],
           pages: list[dict[str, Any]]) -> dict[str, Any]:
    """``recall_credit.credit`` for a Codex session; same arguments and result shape."""
    if not any(_is_responses_request(ev) for ev in session_log):
        return recall_credit.credit(probes, answers, session_log, pages)
    # With no answer the view has no request event, and recall_credit fails closed (B28).
    return recall_credit.credit(probes, answers, answer_view(session_log, answer_index(session_log)), pages)
