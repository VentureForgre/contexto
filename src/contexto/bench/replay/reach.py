"""Which planted facts can the store's search reach? The query rule and the measurement.

The rule is fixed here, per probe class, and uses only what a model would have when it reads the
question. It is not tuned to the store: a class the rule cannot reach is reported as unreachable.

Queries (``needs`` returns them with the statement each must find):

- ``paged_only``, ``chain``, ``distractor`` and a single ``obligations`` probe: the probe's key term,
  which its question names (a config key, the chain's subject, an action item id). This is the rule
  of ``spikes/m2/acceptance/reach.py::key_terms``. The statement to find is the one that holds the
  answer, or the last statement when none does (an obligation's state).
- an ``obligations`` set: one query per action item id in the question; each must find the last
  statement that names that id.
- ``multihop``: the question names only the first hop, so the hops are followed in order. Hop 1
  searches the service name and must find the ownership note, which names the owner; hop 2 searches
  the owner and must find the staffing note; and so on to the note that gives the region. A hop is
  searched only when the statement of the hop before it was found or is still in view, because that
  is where its name comes from.
- ``paraphrase``, ``toolloop`` and ``concluded``: the question does not contain the key, so the
  query is the question's content words (``contexto.bench.model.words``: runs of letters and digits
  of four or more characters, without the function words), in the order they appear. Contexto's
  search returns a page only when it holds every word of the query, so a question whose words are
  not all on the plant's page finds nothing, and that is what the rule reports. For ``concluded``
  the statement to find is the assistant's ``DECISION`` reply.

For those three classes one more figure is measured and reported beside the rule's, never in place
of it: ``word_rank``, the best rank a page holding the needed statement gets when one content word
that the question shares with that statement is searched alone, or null when they share no word or
no such search returns the page. It tells a question whose words are on the plant's line but lost
among the query's other words from a question that shares no word with its plant at all.

Results. ``reachable`` means the store's search (``SessionStore.search``, the function the
``contexto_search`` MCP tool calls) returned a page that holds the needed statement, for every
statement the probe needs; ``rank`` is the 1-based place of the first such page among the results,
and for a probe with several statements the worst of their ranks. ``paged`` means every statement of
the plant is in a stored page; ``in_view`` means every needed statement is in the request the proxy
sent upstream at that moment, so the model can read it without a search; ``covered`` means every
needed statement is one or the other (returned by search, or in view). A multi-hop probe goes on to
its next hop when the hop's statement is either.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from contexto.bench.model import NEW_CLASSES, Probe, words

SHOWN_IN_DETAIL = 3  # the pages contexto_search shows with their matching lines (contexto.mcp.SEARCH_LIMIT)
_IDENT = re.compile(r"(?<![\w.\-])[A-Za-z][A-Za-z0-9]*(?:[.\-_][A-Za-z0-9]+)+")
_TOKEN = re.compile(r"[a-z0-9]+")


def key_terms(probe: Probe) -> list[str]:
    """The probe's key terms: its subjects, or for a probe without any (an obligation set) the
    identifiers its question names."""
    if probe.subjects:
        return list(probe.subjects)
    return list(dict.fromkeys(_IDENT.findall(probe.question)))


def content_words(question: str) -> list[str]:
    """The question's content words (``words``), each once, in the order they first appear."""
    counted = words(question)
    return list(dict.fromkeys(w for w in _TOKEN.findall(question.casefold()) if w in counted))


def needed_statements(probe: Probe) -> list[str]:
    """The statements a correct answer needs, for a probe that is neither multi-hop nor a set: the
    assistant's reply for ``concluded``, else the statements that hold the answer, or the last one
    when none does."""
    if probe.cls == "concluded":
        return list(probe.statements[-1:])
    with_answer = [s for s in probe.statements if probe.answer and probe.answer in s]
    return with_answer or list(probe.statements[-1:])


def follows_hops(probe: Probe) -> bool:
    return probe.type == "multihop"


def needs(probe: Probe) -> list[tuple[str, str]]:
    """``(query, statement)`` pairs in the order they are searched: the statement must be on a page
    the query returns."""
    if probe.cls in NEW_CLASSES:
        query = " ".join(content_words(probe.question))
        return [(query, statement) for statement in needed_statements(probe)]
    terms = key_terms(probe)
    if follows_hops(probe):
        return list(zip(terms, probe.statements))
    if probe.kind == "set":
        out = []
        for term in terms:
            naming = [s for s in probe.statements if term in s]
            if naming:
                out.append((term, naming[-1]))
        return out
    return [(terms[0], statement) for statement in needed_statements(probe)] if terms else []


def first_query(probe: Probe) -> str:
    """The one search the scripted model makes for the probe in its probe turn."""
    pairs = needs(probe)
    return pairs[0][0] if pairs else ""


def _quoted(statement: str) -> str:
    """The statement as it appears inside JSON text."""
    return json.dumps(statement, ensure_ascii=False)[1:-1]


def page_text(page: dict[str, Any]) -> str:
    """A page's messages as JSON text, which ``_quoted`` statements are looked up in."""
    return json.dumps(page.get("messages"), ensure_ascii=False)


def _rank(found: list[str], holders: Iterable[str]) -> int | None:
    places = [found.index(page_id) + 1 for page_id in holders if page_id in found]
    return min(places) if places else None


def measure(probes: Iterable[Probe], pages: dict[str, str], search: Callable[[str], list[str]], view: str,
            plant_turns: Callable[[Probe], list[int]], seed: int) -> list[dict[str, Any]]:
    """One ``reach.jsonl`` row per probe.

    ``pages`` maps a page id to its text (``page_text``), ``search`` is the store's search, and
    ``view`` is the JSON text of the messages the proxy sent upstream at the moment of measuring.
    """
    cache: dict[str, list[str]] = {}

    def run(query: str) -> list[str]:
        if query not in cache:
            cache[query] = list(search(query)) if query else []
        return cache[query]

    def holders(statement: str) -> list[str]:
        quoted = _quoted(statement)
        return [page_id for page_id, text in pages.items() if quoted in text]

    rows = []
    for probe in probes:
        pairs = needs(probe)
        ranks: list[int | None] = []
        seen: list[bool] = []
        queries: list[str] = []
        for query, statement in pairs:
            queries.append(query)
            ranks.append(_rank(run(query), holders(statement)))
            seen.append(_quoted(statement) in view)
            if ranks[-1] is None and not seen[-1] and follows_hops(probe):
                break  # the next hop's name comes from this statement, which the model cannot get
        complete = bool(pairs) and len(ranks) == len(pairs)
        reachable = complete and all(r is not None for r in ranks)
        word_rank = None
        if probe.cls in NEW_CLASSES and pairs:
            best = []
            for _, statement in pairs:
                on = holders(statement)
                shared = [w for w in content_words(probe.question) if w in words(statement)]
                places = [p for p in (_rank(run(w), on) for w in shared) if p is not None]
                best.append(min(places) if places else None)
            word_rank = max(best) if all(b is not None for b in best) else None
        rows.append({
            "seed": seed, "id": probe.id, "cls": probe.cls, "overlap": probe.overlap,
            "plant_turns": plant_turns(probe),
            "paged": bool(probe.statements) and all(holders(s) for s in probe.statements),
            "query": queries[0] if queries else "",
            "reachable": reachable,
            "rank": max(r for r in ranks if r is not None) if reachable else None,
            # Fields this builder added to the schema:
            "queries": list(dict.fromkeys(queries)),
            "needed": len(pairs),
            "in_view": bool(pairs) and all(_quoted(statement) in view for _, statement in pairs),
            "covered": complete and all(r is not None or s for r, s in zip(ranks, seen)),
            "word_rank": word_rank,
        })
    return rows


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``reach`` block of ``results.json``: totals, and the same counts by class in script order.

    ``shown`` counts the reachable probes whose rank is within the ``SHOWN_IN_DETAIL`` pages the
    search tool shows with their matching lines. ``covered`` counts the probes whose every needed
    statement was either returned by search or still in the request, and ``neither`` the rest.
    ``one_word`` (the three new classes only) counts the probes whose ``word_rank`` is within
    ``SHOWN_IN_DETAIL``.
    """

    def count(part: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "probes": len(part),
            "paged": sum(r["paged"] for r in part),
            "reachable": sum(r["reachable"] for r in part),
            "shown": sum(1 for r in part if r["reachable"] and r["rank"] <= SHOWN_IN_DETAIL),
            "covered": sum(r["covered"] for r in part),
            "neither": sum(not r["covered"] for r in part),
            "one_word": sum(1 for r in part if r["word_rank"] is not None and r["word_rank"] <= SHOWN_IN_DETAIL),
        }

    by_class: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_class.setdefault(row["cls"], []).append(row)
    return {**count(rows), "by_class": {cls: count(part) for cls, part in by_class.items()}}
