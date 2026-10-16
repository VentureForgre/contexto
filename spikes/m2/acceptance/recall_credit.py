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


def plant_spans(probes: list[Any], pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Where each probe's first statement sits: page id and [start, end) in that page's rendered text."""
    rendered = [(p["id"], render_page(p)) for p in pages]
    spans = {}
    for probe in probes:
        statement = probe.statements[0] if probe.statements else ""
        for page_id, text in rendered:
            pos = text.find(statement) if statement else -1
            if pos >= 0:
                spans[probe.id] = {"page_id": page_id, "start": pos, "end": pos + len(statement)}
                break
    return spans


def credit(probes: list[Any], answers: str, session_log: list[dict[str, Any]],
           pages: list[dict[str, Any]]) -> dict[str, Any]:
    given = {}
    for line in answers.splitlines():
        qid, sep, value = line.partition(":")
        if sep:
            given[qid.strip()] = value.strip()
    ranges = returned_ranges(session_log)
    cutoff = answer_index(session_log)
    spans = plant_spans(probes, pages)
    rows = []
    for probe in probes:
        correct = bool(probe.answer) and given.get(probe.id, "").lower() == probe.answer.lower()
        span = spans.get(probe.id)
        via = None
        if span:
            # B16 pages a recall over several calls, so a line may span adjacent returned ranges:
            # merge the page's ranges (overlapping or touching) before checking containment.
            merged: list[dict[str, Any]] = []
            for r in sorted((r for r in ranges if (cutoff is None or r["at"] < cutoff)
                             and r["page_id"] == span["page_id"]), key=lambda r: r["start"]):
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
            "acceptance_3": any(r["credited"] for r in rows), "in_view_spans": in_view_spans(session_log)}
