"""Acceptance 3: credit recall from Contexto's content-free MCP log events (contract PR #95, e454e35).

An answer counts as recall when it reached the model through Contexto's tools: a ``contexto_recall``
of the page holding it, or a ``contexto_search`` hit whose returned lines contain it (B15). Nothing
here reads a transcript or tool output. From the session log's ``mcp_call`` events it takes the
character ranges each call returned (page id, start, end), and from the stored page it takes where
the planted statement sits in that page's rendered text (``render_page``, the text both tools read).
A paged-only probe is credited when it was answered correctly and its statement lies wholly inside the
union of the ranges returned for that page (B16 pages a recall over several calls, so adjacent or
overlapping ranges are merged), from calls logged before the answer.

Ranges, per call:
- ``contexto_recall``: ``[offset, offset + chars)`` from its existing fields.
- either tool: each entry of ``returned``, ``{page_id, start, end}`` (B15; end exclusive).

B19 marks each returned span with ``source``: ``paged`` (a stored page) or ``in_view`` (lines of the
retained window the model already has). Only paged spans credit: acceptance 3 is about content that
was paged out. A span with no ``source`` (logs before B19) counts as paged; any other value never
credits. In-view spans are counted and reported, never credited.
"""

from __future__ import annotations

from typing import Any

from contexto.store.render import render_page

TOOLS = ("contexto_search", "contexto_recall")
PAGED_SOURCES = (None, "paged")  # None: a span logged before B19 marked sources


def in_view_spans(session_log: list[dict[str, Any]]) -> int:
    """Returned spans marked in_view (B19): reported beside the credit, never credited."""
    return sum(1 for ev in session_log if ev.get("event") == "mcp_call" and ev.get("tool") in TOOLS
               for e in ev.get("returned") or [] if isinstance(e, dict) and e.get("source") == "in_view")


def returned_ranges(session_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every returned range, with the log index of its call, in log order."""
    out = []
    for i, ev in enumerate(session_log):
        if ev.get("event") != "mcp_call" or ev.get("tool") not in TOOLS or not ev.get("ok"):
            continue
        entries = [e for e in ev.get("returned") or [] if isinstance(e, dict)]
        if not entries and ev.get("tool") == "contexto_recall" and ev.get("page_id") and isinstance(ev.get("chars"), int):
            start = int(ev.get("offset") or 0)
            entries = [{"page_id": ev["page_id"], "start": start, "end": start + int(ev["chars"])}]
        for e in entries:
            if e.get("source") not in PAGED_SOURCES:
                continue  # in_view (or an unknown marker) never credits acceptance 3
            if isinstance(e.get("start"), int) and isinstance(e.get("end"), int) and e.get("page_id"):
                out.append({"at": i, "tool": ev["tool"], "page_id": e["page_id"], "start": e["start"], "end": e["end"]})
    return out


def answer_index(session_log: list[dict[str, Any]]) -> int | None:
    """The log index of the last Messages request: the response that carried the final answer."""
    last = None
    for i, ev in enumerate(session_log):
        if ev.get("event") == "request" and str(ev.get("path") or "").split("?")[0] == "/v1/messages":
            last = i
    return last


def turn_answer_indices(session_log: list[dict[str, Any]]) -> list[int]:
    """Log indices of the Messages requests that answered a user turn, in order (B29).

    A request whose response called no tool ends its turn: the content turns' replies, then each
    probe turn's final answer. The proxy's stream hold (A9) holds a response from its first
    ``tool_use`` block, any tool, so ``stream_hold.held_events == 0`` marks a response with no tool
    call. A log without ``stream_hold`` falls back to "no ``mcp_call`` before the next request",
    which misses non-MCP tools (Bash, Read). Only answered requests count (``answered``), so a failed
    request the CLI retried (a local 502, or a 2xx cut short upstream) is not a turn end, and side
    requests (``side_request``) are skipped. Codex logs need their own rule: pass ``turn_ends``.
    """
    requests = [i for i, ev in enumerate(session_log)
                if ev.get("event") == "request" and str(ev.get("path") or "").split("?")[0] == "/v1/messages"
                and answered(ev) and not side_request(ev)]
    ends = []
    for n, i in enumerate(requests):
        hold = session_log[i].get("stream_hold")
        if isinstance(hold, dict) and isinstance(hold.get("held_events"), int):
            if hold["held_events"] == 0:
                ends.append(i)
            continue
        nxt = requests[n + 1] if n + 1 < len(requests) else len(session_log)
        if not any(session_log[j].get("event") == "mcp_call" for j in range(i + 1, nxt)):
            ends.append(i)
    return ends


def side_request(event: dict[str, Any]) -> bool:
    """A Claude Code side request, which answers no user turn: a request whose original body held one
    message (the proxy's ``original_message_count``).

    Claude Code 2.1.302 sends a title request as its first Messages request: one user message, no
    tool list, answered without a tool call, so the stream-hold rule took it for a turn end (the Opus
    run, 2026-10-25: 25 turn ends against 24, so nothing was credited). It has no ``api-request``
    record in Claude Code's own transcript. Main-conversation requests held at least two messages in
    every log measured (the first request of zero-spend stub runs on 2.1.301 and 2.1.302, and every
    request of the Opus run). 2.1.301 also sent one-message side requests in a tool round, not
    streamed; they are skipped too. The proxy logs no tool-list flag, and the engine's ``main`` is no marker:
    the first conversation seen is main, so the title request is logged with ``main: true`` (D38
    rejects judging by main for the same reason). A log without ``original_message_count`` (older
    logs, synthetic fixtures) has no side requests."""
    return event.get("original_message_count") == 1


def answered(event: dict[str, Any]) -> bool:
    """A request whose response reached the CLI whole: 2xx (``_ok``) and no ``relay: <class>`` error.
    The proxy logs a response that started but whose relay failed (upstream cut it short or reset it;
    contract rule 8) with its 2xx status and that error, and the client saw no terminator, so the CLI
    retries the request: whatever it held, it answered nothing."""
    return _ok(event.get("status")) and not str(event.get("error") or "").startswith("relay:")


def _ok(status: Any) -> bool:
    """A 2xx response; a request logged without a status (older logs) counts as answered."""
    return status is None or (isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300)


def _turn_order(page: dict[str, Any]) -> tuple[int, str]:
    turn = page.get("turn")
    return (turn if isinstance(turn, int) and not isinstance(turn, bool) else 1 << 62, str(page.get("id", "")))


def plant_spans(probes: list[Any], pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Where each probe's first statement sits: page id and [start, end) in that page's rendered text.

    The statement can appear on later pages too (a paged probe turn whose search results quoted it),
    so the page with the EARLIEST turn (ties by page id) is the plant, whatever order the caller
    passes the pages in (B30). ``pages_holding`` counts every page that holds it, for diagnostics.
    """
    rendered = [(p["id"], render_page(p)) for p in sorted(pages, key=_turn_order)]
    spans = {}
    for probe in probes:
        statement = probe.statements[0] if probe.statements else ""
        holding = [(page_id, text.find(statement)) for page_id, text in rendered if statement and statement in text]
        if holding:
            page_id, pos = holding[0]
            spans[probe.id] = {"page_id": page_id, "start": pos, "end": pos + len(statement),
                               "pages_holding": len(holding)}
    return spans


def credit(probes: list[Any], answers: str, session_log: list[dict[str, Any]],
           pages: list[dict[str, Any]], probe_turn_ids: tuple[tuple[str, ...], ...] | None = None,
           turn_ends: list[int] | None = None, expected_turns: int | None = None) -> dict[str, Any]:
    """Credit each paged-only probe answered correctly from a range returned before its answer.

    With ``probe_turn_ids`` (D27's assignment), a probe's cutoff is the answer of the probe turn that
    asked it, so a range returned in a later probe turn never credits an earlier answer (B29);
    without it, the session's last answer request. ``turn_ends`` (log indices of the requests that
    answered each user turn, in order) lets a caller with its own dialect supply them: it replaces
    ``turn_answer_indices`` and its last entry replaces ``answer_index``; omitted, the Messages rules
    apply. ``expected_turns`` (content turns plus probe turns) makes the turn mapping exact: unless
    the turn ends, detected or supplied, number exactly that many, nothing is credited, since a
    spurious or missing end would shift every probe's cutoff silently. Either way it fails closed
    (B28): with no answer request to cut off at, nothing is credited and the result says why.
    """
    given = {}
    for line in answers.splitlines():
        qid, sep, value = line.partition(":")
        if sep:
            given[qid.strip()] = value.strip()
    cutoff = answer_index(session_log) if turn_ends is None else (turn_ends[-1] if turn_ends else None)
    cutoffs: dict[str, int | None] = {probe.id: cutoff for probe in probes}
    reason = None if cutoff is not None else "answer request not found"
    if probe_turn_ids is not None and cutoff is not None:
        ends = turn_answer_indices(session_log) if turn_ends is None else list(turn_ends)
        if expected_turns is not None and len(ends) != expected_turns:
            cutoffs = {probe.id: None for probe in probes}
            reason = f"turn answers do not match the script ({len(ends)} found, {expected_turns} expected)"
        elif len(ends) < len(probe_turn_ids):
            cutoffs, reason = {probe.id: None for probe in probes}, "probe turn answers not found"
        else:
            turn_end = ends[len(ends) - len(probe_turn_ids):]
            of_turn = {pid: turn_end[k] for k, ids in enumerate(probe_turn_ids) for pid in ids}
            cutoffs = {probe.id: of_turn.get(probe.id) for probe in probes}
    # Fail closed (B28): with no answer request to cut off at, nothing can be shown to precede the
    # answer, so nothing is credited, with the reason in the result.
    ranges = returned_ranges(session_log) if reason is None else []
    spans = plant_spans(probes, pages)
    rows = []
    for probe in probes:
        correct = bool(probe.answer) and given.get(probe.id, "").lower() == probe.answer.lower()
        span = spans.get(probe.id)
        via = None
        cut = cutoffs.get(probe.id)
        if span and cut is not None:
            # B16 pages a recall over several calls, so a line may span adjacent returned ranges:
            # merge the page's ranges (overlapping or touching) before checking containment.
            merged: list[dict[str, Any]] = []
            for r in sorted((r for r in ranges if r["at"] < cut and r["page_id"] == span["page_id"]),
                            key=lambda r: r["start"]):
                if merged and r["start"] <= merged[-1]["end"]:
                    merged[-1]["end"] = max(merged[-1]["end"], r["end"])
                    merged[-1]["tools"].add(r["tool"])
                    merged[-1]["calls"] += 1
                else:
                    merged.append({"start": r["start"], "end": r["end"], "tools": {r["tool"]}, "calls": 1})
            for m in merged:
                if m["start"] <= span["start"] and span["end"] <= m["end"]:
                    via = {"tool": "+".join(sorted(m["tools"])), "page_id": span["page_id"],
                           "range": [m["start"], m["end"]], "calls": m["calls"]}
                    break
        rows.append({"id": probe.id, "answered_correctly": correct, "plant": span, "returned": via is not None,
                     "credited": correct and via is not None, **({"via": via} if via else {})})
    return {"paged_only": rows, "credited": sum(r["credited"] for r in rows),
            "correct_not_credited": [r["id"] for r in rows if r["answered_correctly"] and not r["credited"]],
            "acceptance_3": any(r["credited"] for r in rows), "in_view_spans": in_view_spans(session_log),
            **({"reason": reason} if reason else {})}
