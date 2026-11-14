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
  of three or more characters, without the function words and the words of the generator's fixed
  texts), in the order they appear, without its instruction words (``instruction_words``: the words
  of the fixed phrases the generator wraps every such question in, such as "earlier in this
  conversation" and "Answer with the value only", that ``words`` does not already drop). They are
  found from the generator's own tables (``ATTRIBUTES``, ``SCENARIOS``): what of the question is
  not the table's text is the wrapper. Contexto's search returns a page only when it holds every word
  of the query, so a question whose words are not all on the plant's page finds nothing, and that is
  what the rule reports. For ``concluded`` the statement to find is the assistant's ``DECISION``
  reply.

The rule is one mechanical query per fact, so its figure is a floor, not what a model can reach. Beside
it, never in place of it, one more mechanical figure is measured for those three classes: the best
query from the question's words (``question_queries``). Each single content word of the question and
each pair of adjacent content words, every pair tried with a space and with a hyphen (``relay mesh``,
``relay-mesh``), is searched; ``best_rank`` is the best rank a page holding the needed statement gets
over all of them, and ``best_query`` the query that gives it. A probe counts when that rank is within
the ``SHOWN_IN_DETAIL`` pages the search tool shows in detail. For the first five classes the rule's
query already comes from the question (the key it names), so there the column repeats the rule's
figure. Which query a model makes is for the live run.

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


def _table_part(question: str) -> str | None:
    """The part of a new-class question the generator took from its tables: an ``ATTRIBUTES`` clause
    (with the service filled in) or a ``SCENARIOS`` question. None when no table text is in it."""
    from contexto.bench import script as bench_script

    for _, clause in bench_script.ATTRIBUTES:
        found = re.search(re.escape(clause).replace(re.escape("{svc}"), ".+?"), question)
        if found:
            return found.group(0)
    return next((scenario[3] for scenario in bench_script.SCENARIOS if scenario[3] in question), None)


def instruction_words(probe: Probe) -> frozenset[str]:
    """The content words of the fixed phrases a new-class question is wrapped in: the words of what is
    left of the question once its table text (``_table_part``) is taken out, less any word the table
    text also has. Empty for the other classes, and for a question with no table text in it."""
    if probe.cls not in NEW_CLASSES:
        return frozenset()
    own = _table_part(probe.question)
    if own is None:
        return frozenset()
    return words(probe.question.replace(own, " ", 1)) - words(own)


def rule_words(probe: Probe) -> list[str]:
    """The rule's query for a new-class probe, as words: the question's content words without its
    instruction words."""
    drop = instruction_words(probe)
    return [w for w in content_words(probe.question) if w not in drop]


def question_queries(probe: Probe) -> list[str]:
    """Every query the best-query column tries, each once, in order: the question's single content
    words (``rule_words``), then each pair of words adjacent in the question, both of them such
    content words, with a space and with a hyphen (``relay mesh``, ``relay-mesh``)."""
    single = rule_words(probe)
    allowed = set(single)
    tokens = _TOKEN.findall(probe.question.casefold())
    pairs = []
    for a, b in zip(tokens, tokens[1:]):
        if a in allowed and b in allowed:
            pairs += [f"{a} {b}", f"{a}-{b}"]
    return list(dict.fromkeys([*single, *pairs]))


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
        query = " ".join(rule_words(probe))
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
        best_rank, best_query = None, None
        if probe.cls in NEW_CLASSES and pairs:
            per_statement = []
            for _, statement in pairs:
                on = holders(statement)
                tried = [(rank, query) for query in question_queries(probe)
                         if (rank := _rank(run(query), on)) is not None]
                per_statement.append(min(tried) if tried else None)
            if all(per_statement):
                best_rank, best_query = max(per_statement)
        elif reachable:
            best_rank, best_query = max(r for r in ranks if r is not None), queries[0]
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
            "returned": len(run(queries[0])) if queries else 0,  # pages the first query returned
            "best_rank": best_rank, "best_query": best_query,
        })
    return rows


def _best(row: dict[str, Any]) -> bool:
    return row["best_rank"] is not None and row["best_rank"] <= SHOWN_IN_DETAIL


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``reach`` block of ``results.json``: totals, and the same counts by class in script order.

    ``shown`` counts the reachable probes whose rank is within the ``SHOWN_IN_DETAIL`` pages the
    search tool shows with their matching lines. ``covered`` counts the probes whose every needed
    statement was either returned by search or still in the request, and ``neither`` the rest.
    ``best_query`` counts the probes whose ``best_rank`` (the best query from the question's words,
    which for the first five classes is the rule's query) is within ``SHOWN_IN_DETAIL``, and
    ``neither_by_any_query`` those that are neither covered nor found that way, and
    ``rule_returned_nothing`` the probes whose first query returned no page at all.
    """

    def count(part: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "probes": len(part),
            "paged": sum(r["paged"] for r in part),
            "reachable": sum(r["reachable"] for r in part),
            "shown": sum(1 for r in part if r["reachable"] and r["rank"] <= SHOWN_IN_DETAIL),
            "covered": sum(r["covered"] for r in part),
            "neither": sum(not r["covered"] for r in part),
            "best_query": sum(_best(r) for r in part),
            "rule_returned_nothing": sum(r["returned"] == 0 for r in part),
            "neither_by_any_query": sum(not r["covered"] and not _best(r) for r in part),
        }

    by_class: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_class.setdefault(row["cls"], []).append(row)
    return {**count(rows), "by_class": {cls: count(part) for cls, part in by_class.items()}}
