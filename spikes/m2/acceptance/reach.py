"""Can contexto_search reach every probe's answer? Offline, zero spend (intake, 2026-10-16 04:04Z).

For each probe, the store's own search (``contexto.store.pages.search_pages``, exact case-sensitive
substring over page stubs and text) runs with the probe's key terms against a session's stored pages.
A probe is reachable when every statement it needs is either still in the context window (on no
page) or on a page that one of its terms returns. A miss in a live run on a reachable probe is then
the model's, not the index's or the search's.

Key terms, deterministically:
- the probe's ``subjects`` (the plant's own key: a config key, a chain subject, an action item id, or
  for a multi-hop probe every hop's name, which the model learns hop by hop);
- if a probe has none (an obligation set), the identifiers its question names (``AI-12345`` style
  and dotted or dashed names).

Statements a probe needs:
- multi-hop: every statement (each hop);
- an obligation set: for each id in the question, the last statement that names it (its state);
- otherwise: the statements that contain the answer, or the last statement if none does (a status).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from contexto.store.pages import search_pages

_IDENT = re.compile(r"(?<![\w.\-])[A-Za-z][A-Za-z0-9]*(?:[.\-_][A-Za-z0-9]+)+")
CATEGORIES = ("chain", "obligations", "multihop", "distractor", "paged_only")


def key_terms(probe: Any) -> list[str]:
    if probe.subjects:
        return list(probe.subjects)
    return list(dict.fromkeys(_IDENT.findall(probe.question)))


def needed_statements(probe: Any, terms: list[str]) -> list[str]:
    if probe.type == "multihop":
        return list(probe.statements)
    if probe.kind == "set":
        needed = []
        for term in terms:
            naming = [s for s in probe.statements if term in s]
            if naming:
                needed.append(naming[-1])
        return needed
    with_answer = [s for s in probe.statements if probe.answer and probe.answer in s]
    return with_answer or list(probe.statements[-1:])


def _page_text(page: dict[str, Any]) -> str:
    return json.dumps(page.get("messages"), ensure_ascii=False)


def reach(probes: list[Any], pages: list[dict[str, Any]]) -> dict[str, Any]:
    texts = {p["id"]: _page_text(p) for p in pages}
    rows = []
    for probe in probes:
        terms = key_terms(probe)
        hits = {t: search_pages(pages, t) for t in terms}
        needs = []
        for statement in needed_statements(probe, terms):
            # json.dumps escapes the statement the same way inside the page text
            quoted = json.dumps(statement, ensure_ascii=False)[1:-1]
            on = [pid for pid, text in texts.items() if quoted in text]
            if not on:
                needs.append({"page": None, "in_window": True, "hit": True})
                continue
            found = [(t, h.index(pid) + 1) for t in terms for pid in on for h in [hits[t]] if pid in h]
            best = min(found, key=lambda x: x[1]) if found else None
            needs.append({"page": on[0], "in_window": False, "hit": bool(found),
                          "term": best[0] if best else None, "rank": best[1] if best else None})
        rows.append({"id": probe.id, "type": probe.type, "terms": terms, "needs": needs,
                     "reachable": bool(needs) and all(n["hit"] for n in needs) and bool(terms)})
    by_type: dict[str, dict[str, int]] = {}
    for row in rows:
        cell = by_type.setdefault(row["type"], {"probes": 0, "reachable": 0, "needs_paged": 0})
        cell["probes"] += 1
        cell["reachable"] += int(row["reachable"])
        cell["needs_paged"] += int(any(not n["in_window"] for n in row["needs"]))
    return {"pages": len(pages), "probes": len(rows), "reachable": sum(r["reachable"] for r in rows),
            "by_type": by_type, "unreachable": [r for r in rows if not r["reachable"]], "rows": rows}


def load_pages(session_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((session_dir / "pages").glob("*.json"))]
