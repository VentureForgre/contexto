"""D65: recall, false-hit rate and latency of search on the probe set of ``fallback_probes`` (zero spend).

    uv run python spikes/m2/scale/store/fallback_bench.py --store-home /tmp/d65-store            # "before"
    uv run python spikes/m2/scale/store/fallback_bench.py --store-home /tmp/d65-store --label after \\
        --baseline-src ../contexto-main/src
    uv run python spikes/m2/scale/store/fallback_bench.py --compare results/d65/before results/d65/after
    uv run python spikes/m2/scale/store/fallback_bench.py --sizes 60 --out /tmp/d65-quick          # a quick check

It builds the 6,000-page store of this spike (``scale_bench.build_store``, seed 7, floor 0, with the six
identifier pages of ``fallback_probes.EXTRA_FACTS``) under a temporary ``CONTEXTO_HOME``, or under
``--store-home``, where a later run finds it again. ``~/.contexto`` and ``$CONTEXTO_HOME`` are never used.

- Quality, in process (all page text held in memory, which changes speed only). The ranked pages of a
  probe are ``store.search(query)``. When that is empty and the store has ``search_approximate`` (the
  fallback D65 adds; ``main`` has none), they are the ids of ``store.search_approximate(query)``, best
  first. A probe counts at k when every expected page is in the top k. For ``none`` probes any page
  returned is a false hit.
- The ceiling. Each probe that alters a word is also run through strict search spelled the page's way
  (``ceiling_query``). "Reachable at 3" counts the probes whose page strict search then ranks in the
  top 3: a fallback that ranks as strict search does cannot do better on the others.
- Latest first. For a fact whose value changes, all three plants hold the query's words, so the top 3
  can hold a stale value first. ``latest_first`` says whether the expected (last) plant ranks above
  every older plant of that fact.
- A cross-check through the tool: each probe through ``contexto.mcp.Server`` in process, after a
  retained window and a turn room are published as the engine does per request. Only the detailed and
  the listed page lines of the reply are read.
- Latency, through a real ``python -m contexto mcp --session S`` child over stdio: the first search of
  the process, then the median of ``--reps`` calls per query, in ``--passes`` passes, for the nine
  strict hits of ``scale_probes.LATENCY_QUERIES`` and three strict misses from each of five probe
  classes. With ``--baseline-src`` (the ``src`` directory of another checkout) a second child runs that
  code on the same store, and every query is timed against both in turn, so both see the same load.
  Each pass starts with a throwaway child's search, which fills the OS file cache, and the side whose
  first search goes first alternates by pass; the first search is reported by that position.

Outputs (numbers and queries only, no page text) go to ``--out``/``--label``: ``quality.csv`` (one row per
probe), ``latency.csv`` (every call), ``env.json`` (one record per part) and ``summary.md``. A run
writes to temporary names and renames at the end, so a run that fails leaves the folder as it was.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

import scale_bench  # noqa: E402
from fallback_probes import (  # noqa: E402
    CLASSES,
    EXTRA_FACTS,
    PROBES,
    Probe,
    expected_pages,
    fact_of,
    split_of,
)
from scale_probes import FACTS, LATENCY_QUERIES, planted_turn  # noqa: E402

from contexto.store import SessionStore  # noqa: E402

SEED, FLOOR = 7, 0.0
SIZES = (6000,)
MISS_CLASSES = ("identifier", "question", "typo", "extra_word", "none")  # strict misses timed, three each
FIRST_QUERY = LATENCY_QUERIES["rare"][0]  # the first search of each MCP child
FIRST = ("first (searched first)", "first (searched second)")  # latency kinds of the first search, by position
QUALITY_FIELDS = ["n", "id", "class", "sub_kind", "flags", "split", "query", "expected", "source", "strict_pages",
                  "returned", "ranks", "top3", "top10", "any3", "any10", "latest_first", "ceiling_query",
                  "ceiling_ranks", "reach3", "reach10", "altered", "altered_pages", "tool_detailed", "tool_listed",
                  "tool_pages"]
LATENCY_FIELDS = ["n", "pass", "load1", "side", "kind", "query", "rep", "seconds", "chars"]
PARTS = ("quality", "latency")
HALVES = (("tune", "tune"), ("held_out", "held-out"))


class CompareError(Exception):
    """Two result folders cannot be put side by side."""


# The store


def session_name(n: int) -> str:
    return f"d65-{n}"


def corpus_id() -> str:
    """Changes when a plant, the seed or the floor does, so a store of another corpus is not reused."""
    return hashlib.sha256(repr((FACTS, EXTRA_FACTS, SEED, FLOOR)).encode()).hexdigest()[:16]


def probe_set_id() -> str:
    """Changes when a probe is added, removed or reworded, or moves to the other half."""
    probes = [(p.id, p.cls, p.query, p.expected, split_of(p)) for p in PROBES]
    return hashlib.sha256(repr(probes).encode()).hexdigest()[:16]


def open_store(home: Path, n: int) -> list[str]:
    """The page ids (one per turn, oldest first) of the ``n``-page store under ``home``, building it
    when it is not there, is not whole, or was built from other plants."""
    session = session_name(n)
    manifest = home / f"{session}.json"
    wanted = {"n": n, "corpus": corpus_id()}
    try:
        found = json.loads(manifest.read_text(encoding="utf-8"))
        ids = found.pop("ids")
        if found == wanted and sorted(SessionStore(home, session).page_ids()) == sorted(ids):
            print(f"[{time.strftime('%H:%M:%S')}] N={n}: reusing the store under {home}", flush=True)
            return ids
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    print(f"[{time.strftime('%H:%M:%S')}] N={n}: building the store under {home}", flush=True)
    manifest.unlink(missing_ok=True)
    shutil.rmtree(home / "sessions" / session, ignore_errors=True)  # a build that was cut short
    built = scale_bench.build_store(home, session, n, dialect="messages", floor=FLOOR, seed=SEED, extra=EXTRA_FACTS)
    if not built["markers_ok"]:
        raise SystemExit(f"a planted marker is missing from its page at N={n}")
    manifest.write_text(json.dumps({**wanted, "ids": built["ids"]}) + "\n", encoding="utf-8")
    return built["ids"]


def refuse_live_home(store_home: Path) -> None:
    """The bench writes its own sessions under ``--store-home``: never a home Contexto itself uses."""
    target = store_home.resolve()
    if target == (Path.home() / ".contexto").resolve():
        raise SystemExit("--store-home must not be ~/.contexto: the bench writes its own sessions there")
    live = os.environ.get("CONTEXTO_HOME")
    if live and target == Path(live).resolve():
        raise SystemExit("--store-home must not be $CONTEXTO_HOME: the bench writes its own sessions there")


# Quality


def ranked(store: Any, query: str) -> tuple[list[str], str]:
    """The ranked page ids for ``query`` and where they came from: ``strict``, ``fallback`` or ``none``."""
    strict = store.search(query)
    if strict:
        return strict, "strict"
    approximate = getattr(store, "search_approximate", None)
    ids = [hit.id for hit in approximate(query)] if approximate is not None else []
    return ids, "fallback" if ids else "none"


def score(expected: list[str], ranked_ids: list[str]) -> dict[str, Any]:
    """The rank of each expected page (``-``: not returned) and whether all, or any, are in the top 3 and 10."""
    if not expected:
        return {"ranks": "", "top3": "", "top10": "", "any3": "", "any10": ""}
    ranks = [ranked_ids.index(page) + 1 if page in ranked_ids else None for page in expected]
    return {"ranks": ";".join(str(r) if r else "-" for r in ranks),
            "top3": all(r is not None and r <= 3 for r in ranks), "top10": all(r is not None and r <= 10 for r in ranks),
            "any3": any(r is not None and r <= 3 for r in ranks), "any10": any(r is not None and r <= 10 for r in ranks)}


def ceiling_query(probe: Probe) -> str:
    """The probe spelled the page's way, for strict search: the page's identifier in place of the query's,
    the query before its typo or without its added word, a question without its question mark, an exact
    probe as it is. Empty for the classes with no such spelling (paraphrase, two_page, none)."""
    if probe.cls == "identifier":
        return " ".join(probe.original if w == probe.altered else w for w in probe.query.split())
    if probe.cls in ("typo", "extra_word"):
        return probe.base
    if probe.cls == "question":
        return probe.query.rstrip("?")
    return probe.query if probe.cls == "exact" else ""


def latest_first(probe: Probe, ids: list[str], ranked_ids: list[str]) -> Any:
    """For a probe on a fact whose value changes: whether the expected (last) plant's page is returned and
    ranks above the page of every older plant of that fact (``scale_bench``'s ``latest_first``). Empty
    for a probe with no such fact; for two such facts, both must hold."""
    rank = {page: i for i, page in enumerate(ranked_ids)}
    verdicts = []
    for fact_id, plant in probe.expected:
        fact = fact_of(fact_id)
        if fact.kind != "changing":
            continue
        pages = [ids[planted_turn(p.at, len(ids))] for p in fact.plants]
        latest = rank.get(pages[plant])
        older = [rank[page] for page in pages[:plant] if page in rank and page != pages[plant]]
        verdicts.append(latest is not None and all(r > latest for r in older))
    return all(verdicts) if verdicts else ""


def tool_pages(text: str) -> tuple[list[str], list[str]]:
    """The page ids a ``contexto_search`` reply shows in detail and lists; every other line is ignored."""
    detailed, listed = [], []
    for line in text.split("\n"):
        if m := scale_bench.DETAILED.match(line):
            detailed.append(m.group(1))
        elif m := scale_bench.LISTED.match(line):
            listed.append(m.group(1))
    return detailed, listed


def off_page(probe: Probe, expected: list[str], pages_with: Callable[[str], list[str]]) -> tuple[str, Any]:
    """The words of the query that are not the page's, and the most pages any of them is on (strict
    search of the one word): the typo, the restyled identifier or the added word; for a paraphrase the
    words its expected page does not hold; for ``none`` its absent words (0 unless the probe is wrong)."""
    if probe.cls == "none":
        words = list(probe.absent)
    elif probe.cls == "paraphrase":
        words = [w for w in probe.query.split() if not set(expected) <= set(pages_with(w))]
    else:
        words = [probe.altered] if probe.altered else []
    return " ".join(words), max((len(pages_with(w)) for w in words), default="")


def quality(home: Path, n: int, ids: list[str], writer: csv.DictWriter) -> None:
    client = scale_bench.InProcess(home, session_name(n))
    store = client.store
    # Hold every page's text: no result depends on the budget (D39), and the default 64 MB reads three
    # quarters of a 6,000-page store again on every search. The latency children keep the default.
    store.set_search_cache(None)
    one_word: dict[str, list[str]] = {}

    def pages_with(word: str) -> list[str]:
        if word not in one_word:
            one_word[word] = store.search(word)
        return one_word[word]

    for probe in PROBES:
        expected = expected_pages(probe, ids)
        found, source = ranked(store, probe.query)
        strict = found if source == "strict" else []
        ceiling = ceiling_query(probe)
        reach = score(expected, strict if ceiling == probe.query else store.search(ceiling)) if ceiling else None
        scale_bench.publish_request(store, 0)
        _, reply = client.call("contexto_search", {"query": probe.query})
        detailed, listed = tool_pages(reply)
        altered, altered_pages = off_page(probe, expected, pages_with)
        writer.writerow({
            "n": n, "id": probe.id, "class": probe.cls, "sub_kind": probe.sub_kind, "flags": " ".join(probe.flags),
            "split": split_of(probe), "query": probe.query,
            "expected": ";".join(f"{fact}:{plant}" for fact, plant in probe.expected),
            "source": source, "strict_pages": len(strict), "returned": len(found), **score(expected, found),
            "latest_first": latest_first(probe, ids, found), "ceiling_query": ceiling,
            "ceiling_ranks": reach["ranks"] if reach else "", "reach3": reach["top3"] if reach else "",
            "reach10": reach["top10"] if reach else "", "altered": altered, "altered_pages": altered_pages,
            "tool_detailed": sum(page in detailed for page in expected),
            "tool_listed": sum(page in listed for page in expected), "tool_pages": len(detailed) + len(listed)})


# Latency


def latency_queries(store: Any) -> list[tuple[str, str]]:
    """(kind, query): the nine strict hits of ``LATENCY_QUERIES``, then for each class of ``MISS_CLASSES``
    its first three probes that strict search finds nothing for in this store."""
    out = [(kind, q) for kind, queries in LATENCY_QUERIES.items() for q in queries]
    for cls in MISS_CLASSES:
        misses = (p.query for p in PROBES if p.cls == cls and not store.search(p.query))
        out += [(f"miss_{cls}", q) for q, _ in zip(misses, range(3))]  # stops searching at the third
    return out


def latency_pass(home: Path, n: int, pass_no: int, reps: int, queries: list[tuple[str, str]], writer: csv.DictWriter,
                 sides: dict[str, Callable[[Path, str], Any]]) -> None:
    """One pass. A throwaway child searches once, so the OS file cache holds the store before anything is
    timed. Then a new child per side, each side's first search (its kind says which side went first),
    and every query ``reps`` times against each side in turn. The side that goes first alternates by
    call and by pass, so over an even number of passes each side's first search is timed in both places."""
    session = session_name(n)
    store = SessionStore(home, session)
    base = {"n": n, "pass": pass_no, "load1": round(os.getloadavg()[0], 2)}

    def row(side: str, kind: str, query: str, rep: int, seconds: Any, chars: int) -> None:
        writer.writerow({**base, "side": side, "kind": kind, "query": query, "rep": rep,
                         "seconds": f"{seconds:.6f}" if seconds != "" else "", "chars": chars})

    def timed(side: str, client: Any, kind: str, query: str, rep: int) -> None:
        scale_bench.publish_request(store, 1000 * pass_no + rep)
        seconds, text = client.call("contexto_search", {"query": query})
        row(side, kind, query, rep, seconds, len(text))

    names = list(sides) if pass_no % 2 else list(sides)[::-1]
    warm = sides[names[0]](home, session)
    try:
        timed("warm-up", warm, "warm", FIRST_QUERY, 0)
    finally:
        warm.close()
    clients: dict[str, Any] = {}
    try:
        for side in names:
            clients[side] = sides[side](home, session)
            row(side, "startup", "initialize", 0, clients[side].startup_s, 0)
        for side, kind in zip(names, FIRST):
            timed(side, clients[side], kind, FIRST_QUERY, 0)
        for kind, query in queries:
            for rep in range(reps):
                for side in names if rep % 2 == 0 else names[::-1]:
                    timed(side, clients[side], kind, query, rep)
    finally:
        for side, client in clients.items():
            row(side, "peak_rss", "", 0, "", client.close())


# Summary


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_env(folder: Path) -> dict[str, Any]:
    """``env.json`` of a result folder: its label and one record per part it holds; empty when unreadable."""
    try:
        env = json.loads((folder / "env.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(env, dict):
        return {}
    return {key: value for key, value in env.items() if key == "label" or (key in PARTS and isinstance(value, dict))}


def label_of(folder: Path) -> str:
    return str(read_env(folder).get("label", folder.name))


def identity(part: dict[str, Any]) -> tuple[Any, Any, Any]:
    """What two runs must share to be compared: the store sizes, the corpus and the probe set. Page ids
    are keyed per store, so they cannot serve."""
    return part.get("sizes"), part.get("corpus"), part.get("probe_set")


def _pages(part: dict[str, Any]) -> str:
    return ", ".join(f"{n:,}" for n in part.get("sizes") or [])


def _yes(rows: list[dict[str, str]], field: str) -> int:
    return sum(r[field] == "True" for r in rows)


def _of(rows: list[dict[str, str]], field: str, percent: bool = False) -> str:
    hit, total = _yes(rows, field), len(rows)
    return f"{hit}/{total} ({100 * hit / total:.0f}%)" if percent and total else f"{hit}/{total}"


def _halves(rows: list[dict[str, str]], field: str) -> str:
    """``tune | held-out | all`` cells for one yes/no field."""
    tune, held = ([r for r in rows if r["split"] == s] for s, _ in HALVES)
    return f"{_of(tune, field)} | {_of(held, field)} | {_of(rows, field, percent=True)}"


def _false_hits(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """``none`` rows with a yes/no ``hit`` field: any page returned."""
    return [{**r, "hit": str(int(r["returned"]) > 0)} for r in rows]


def _either(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """two_page rows counted when either page is in the top k."""
    return [{**r, "top3": r["any3"], "top10": r["any10"], "latest_first": ""} for r in rows]


def _sources(rows: list[dict[str, str]]) -> str:
    return " / ".join(str(sum(r["source"] == s for r in rows)) for s in ("strict", "fallback", "none"))


def _recall(rows: list[dict[str, str]], percent: bool) -> str:
    """``probes | top 3 | reachable at 3 | top 10 | reachable at 10 | latest first | sources`` cells."""
    total = len(rows)

    def count(field: str) -> str:
        hit = _yes(rows, field)
        return f"{hit} ({100 * hit / total:.0f}%)" if percent and total else str(hit)

    ceiling = any(r["ceiling_query"] for r in rows)
    changing = [r for r in rows if r["latest_first"] != ""]
    latest = f"{_yes(changing, 'latest_first')} of {len(changing)}" if changing else "-"
    return (f"{total} | {count('top3')} | {_yes(rows, 'reach3') if ceiling else '-'} | {count('top10')} | "
            f"{_yes(rows, 'reach10') if ceiling else '-'} | {latest} | {_sources(rows)}")


def groups(rows: list[dict[str, str]]) -> list[tuple[str, list[dict[str, str]]]]:
    """(label, rows) of the groups a class is also reported by; empty groups are left out."""
    out = []

    def add(label: str, cls: str, keep: Callable[[dict[str, str]], bool]) -> None:
        found = [r for r in rows if r["class"] == cls and keep(r)]
        if found:
            out.append((label, found))

    add("identifier, the identifier alone", "identifier", lambda r: "page_words" not in r["flags"].split())
    add("identifier, with other page words", "identifier", lambda r: "page_words" in r["flags"].split())
    for edit in ("substitution", "deletion", "insertion", "swap"):
        add(f"typo, {edit}", "typo", lambda r, edit=edit: r["sub_kind"] == edit)
    add("typo, the other words are the page's own", "typo", lambda r: "carries_query" not in r["flags"].split())
    add("typo, the misspelled word carries the query", "typo", lambda r: "carries_query" in r["flags"].split())
    add("typo, typed word under 6 characters", "typo", lambda r: "short_typo" in r["flags"].split())
    for cls in ("typo", "extra_word", "paraphrase"):
        add(f"{cls}, off-page word on no page", cls, lambda r: r["altered_pages"] == "0")
        add(f"{cls}, off-page word on some page", cls, lambda r: r["altered_pages"] not in ("", "0"))
    return out


def _strict_hit(r: dict[str, str]) -> bool:
    """Strict search returned an expected page."""
    return r["source"] == "strict" and any(c.isdigit() for c in r["ranks"])


def quality_tables(rows: list[dict[str, str]], n: str) -> list[str]:
    head = "probes | top 3 | reachable at 3 | top 10 | reachable at 10 | latest first | strict / fallback / nothing |"
    lines = [f"## Recall at {int(n):,} pages", "",
             "A probe counts when every expected page is in the top 3 (top 10) of the ranked pages. Reachable: "
             "strict search ranks the page in the top 3 (top 10) when the query is spelled the page's way (for a "
             "question, without its question mark); `-` for a class with no such spelling. Latest first: of the "
             "probes on a fact whose value changes, those whose expected page ranks above every older value's page.",
             "", f"| class | half | {head}", "|---|---|---:|---:|---:|---:|---:|---|---|"]
    for cls in CLASSES:
        rs = [r for r in rows if r["class"] == cls]
        if cls == "none" or not rs:
            continue
        for label, part in ((cls, rs), ("two_page, either page", _either(rs)))[:2 if cls == "two_page" else 1]:
            for split, name in HALVES:
                lines.append(f"| {label} | {name} | {_recall([r for r in part if r['split'] == split], False)} |")
            lines.append(f"| {label} | all | {_recall(part, True)} |")
    lines += ["", f"## Groups at {int(n):,} pages", "",
              "The same counts for parts of a class, both halves together. The off-page word is the typo, the "
              "added word, or for a paraphrase the word its page does not hold (the most common one when there are "
              "several): on no page of the store, or on some.", "",
              f"| group | {head}", "|---|---:|---:|---:|---:|---:|---|---|"]
    lines += [f"| {label} | {_recall(part, True)} |" for label, part in groups(rows)]
    wrong = [r for r in rows if r["class"] not in ("exact", "none") and r["source"] == "strict" and not _strict_hit(r)]
    if wrong:
        lines += ["", f"## Probes strict search answers without the expected page at {int(n):,} pages", "",
                  "Some page holds every word of the query, so a fallback that runs only on an empty result never "
                  "runs for these.", "", "| probe | query | pages strict search returns |", "|---|---|---:|"]
        lines += [f"| {r['id']} | {r['query']} | {r['strict_pages']} |" for r in wrong]
    none = _false_hits([r for r in rows if r["class"] == "none"])
    if none:
        lines += ["", f"## Nothing to find (`none`) at {int(n):,} pages", "",
                  "A false hit is a probe for which any page is returned.", "",
                  "| sub-kind | probes | false hits: tune | held-out | all | median pages returned | "
                  "through the tool: probes with a page |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for kind in ("absent", "mixed", "all"):
            rs = [r for r in none if kind in ("all", r["sub_kind"])]
            if rs:
                pages = statistics.median(int(r["returned"]) for r in rs)
                lines.append(f"| {kind} | {len(rs)} | {_halves(rs, 'hit')} | {pages:g} | "
                             f"{sum(int(r['tool_pages']) > 0 for r in rs)} |")
    lines += ["", f"## Through the tool (`contexto_search`) at {int(n):,} pages", "",
              "Whether the reply shows every expected page in detail (the tool details 3 pages and lists 50).", "",
              "| class | probes | in detail | in detail or listed | not in the reply | differs from top 3 above |",
              "|---|---:|---:|---:|---:|---:|"]
    for cls in CLASSES:
        rs = [r for r in rows if r["class"] == cls]
        if cls == "none" or not rs:
            continue
        want = [len(r["expected"].split(";")) for r in rs]
        detail = [int(r["tool_detailed"]) == w for r, w in zip(rs, want)]
        shown = [int(r["tool_detailed"]) + int(r["tool_listed"]) == w for r, w in zip(rs, want)]
        differs = sum(d != (r["top3"] == "True") for r, d in zip(rs, detail))
        lines.append(f"| {cls} | {len(rs)} | {sum(detail)} | {sum(shown)} | {len(rs) - sum(shown)} | {differs} |")

    def some(*classes: str) -> list[dict[str, str]]:
        return [r for r in rows if r["class"] in classes]

    exact, altered = some("exact"), some("typo", "identifier", "extra_word", "two_page")
    reworded, questions, paraphrases = some("paraphrase", "question"), some("question"), some("paraphrase")
    answered = [r for r in reworded if r["source"] == "strict"]
    lines += ["", f"## Probe checks at {int(n):,} pages", "",
              f"- exact probes whose page strict search returns: {sum(map(_strict_hit, exact))} of {len(exact)}.",
              "- typo, identifier, extra_word and two_page probes for which strict search returns an expected page: "
              f"{sum(map(_strict_hit, altered))} of {len(altered)} (0 is right).",
              f"- none probes with an absent word on some page: {sum(int(r['altered_pages']) > 0 for r in none)} of "
              f"{len(none)} (0 is right).",
              f"- paraphrase probes whose expected page holds every word of the query: "
              f"{sum(r['altered'] == '' for r in paraphrases)} of {len(paraphrases)} (0 is right).",
              f"- paraphrase and question probes that strict search answers, so a fallback never runs: "
              f"{len(answered)} of {len(reworded)}; the expected page is among the pages returned for "
              f"{sum(map(_strict_hit, answered))} of them.",
              f"- question probes strict search finds once the question mark is removed: "
              f"{sum(r['ceiling_ranks'] == '1' for r in questions)} at rank 1, {_yes(questions, 'reach3')} in the top "
              f"3, {_yes(questions, 'reach10')} in the top 10, of {len(questions)}."]
    return lines


def _cell(rows: list[dict[str, str]], side: str, kind: str) -> float | None:
    """Seconds: the median of the calls of each query in each pass, then the median of those."""
    calls: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        if r["side"] == side and r["kind"] == kind and r["seconds"]:
            calls.setdefault((r["pass"], r["query"]), []).append(float(r["seconds"]))
    return statistics.median(statistics.median(v) for v in calls.values()) if calls else None


def _kinds(rows: list[dict[str, str]]) -> list[str]:
    seen = dict.fromkeys(r["kind"] for r in rows if r["kind"] not in ("startup", "peak_rss", "warm"))
    return sorted(seen, key=lambda k: (k not in FIRST, list(seen).index(k)))  # the first searches first


def _sides(rows: list[dict[str, str]]) -> list[str]:
    seen = dict.fromkeys(r["side"] for r in rows if r["kind"] != "warm")
    return sorted(seen, key=lambda s: (s != "tree", list(seen).index(s)))


def latency_tables(rows: list[dict[str, str]], n: str) -> list[str]:
    sides = _sides(rows)
    both = "baseline" in sides and "tree" in sides
    head = "| kind | queries | " + " | ".join(f"{s}, s" for s in sides) + (" | tree / baseline |" if both else " |")
    lines = [f"## Latency at {int(n):,} pages", "",
             "Seconds through an MCP child over stdio: the median of the calls per query per pass, then the median "
             "over queries and passes. `first` is the first search of a process, after a throwaway child's search "
             "has filled the OS file cache, by whether that side searched first or second in its pass. `miss_*` are "
             "queries strict search finds nothing for.", "", head, "|---|---:|" + "---:|" * (len(sides) + both)]
    for kind in _kinds(rows):
        cells = [_cell(rows, side, kind) for side in sides]
        queries = len({r["query"] for r in rows if r["kind"] == kind})
        ratio = ""
        if both and all(cells):
            ratio = f" {cells[sides.index('tree')] / cells[sides.index('baseline')]:.2f} |"
        elif both:
            ratio = " |"
        lines.append(f"| {kind} | {queries} | " + " | ".join("" if c is None else f"{c:.3f}" for c in cells)
                     + " |" + ratio)
    loads = {r["pass"]: r["load1"] for r in rows}
    rss = {side: [int(r["chars"]) for r in rows if r["side"] == side and r["kind"] == "peak_rss"] for side in sides}
    lines += ["", "1-minute load average at the start of each pass: "
              + ", ".join(loads[p] for p in sorted(loads, key=int)) + ".",
              "Peak RSS of the MCP child, MB: "
              + ", ".join(f"{side} {statistics.median(v) / 1e6:.0f}" for side, v in rss.items() if v) + "."]
    went_first = {side: sum(r["side"] == side and r["kind"] == FIRST[0] for r in rows) for side in sides}
    if len(set(went_first.values())) > 1:
        lines.append("The sides did not search first equally often ("
                     + ", ".join(f"{side} {count}" for side, count in went_first.items())
                     + "), so the first-search rows compare different numbers of passes; use an even number of passes.")
    return lines


def _part_line(name: str, part: dict[str, Any]) -> str:
    src = "" if part.get("src_matches_commit", True) else " with uncommitted changes under src/"
    extra = ""
    if name == "latency":
        extra = f", {part.get('passes')} passes of {part.get('reps')} calls per query"
        if part.get("baseline_commit"):
            extra += f", baseline commit {str(part['baseline_commit'])[:8]}"
    approx = "has" if part.get("search_approximate") else "has no"
    return (f"- {name.capitalize()}: commit {str(part.get('commit'))[:8]}{src}, {_pages(part)} pages, "
            f"{part.get('probes')} probes (set {part.get('probe_set')}){extra}; Python {part.get('python')}, "
            f"{part.get('os')}, {part.get('cpu')}; the store {approx} `search_approximate`.")


def summarize(folder: Path) -> str:
    """The tables of one result folder, from whichever of ``quality.csv`` and ``latency.csv`` it holds."""
    quality_rows, latency_rows = read_rows(folder / "quality.csv"), read_rows(folder / "latency.csv")
    env = read_env(folder)
    parts = {name: env[name] for name in PARTS if name in env}
    lines = [f"# D65 probes: {label_of(folder)}", ""]
    lines += [_part_line(name, part) for name, part in parts.items()]
    if len(parts) == 2 and (identity(parts["quality"]) != identity(parts["latency"])
                            or parts["quality"].get("commit") != parts["latency"].get("commit")):
        lines.append("- The two parts were not measured on the same commit, store size, corpus or probe set; read "
                     "them separately.")
    if parts:
        lines.append("")
    for n in dict.fromkeys(r["n"] for r in quality_rows):
        lines += [*quality_tables([r for r in quality_rows if r["n"] == n], n), ""]
    for n in dict.fromkeys(r["n"] for r in latency_rows):
        lines += [*latency_tables([r for r in latency_rows if r["n"] == n], n), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def measures(folder: Path) -> dict[str, str]:
    """Every figure of one result folder as ``{measure: value}``, in table order (for ``--compare``)."""
    out: dict[str, str] = {}
    quality_rows, latency_rows = read_rows(folder / "quality.csv"), read_rows(folder / "latency.csv")
    sizes = list(dict.fromkeys(r["n"] for r in (*quality_rows, *latency_rows)))

    def recall(label: str, part: list[dict[str, str]]) -> None:
        reach = f" (reachable {_yes(part, 'reach3')} / {_yes(part, 'reach10')})" \
            if any(r["ceiling_query"] for r in part) else ""
        out[f"{label} ({len(part)}): top 3 / top 10"] = f"{_yes(part, 'top3')} / {_yes(part, 'top10')}{reach}"
        changing = [r for r in part if r["latest_first"] != ""]
        if changing:
            out[f"{label} ({len(part)}): latest first, of {len(changing)} on a changing fact"] = \
                str(_yes(changing, "latest_first"))

    for n in sizes:
        at = f"{int(n):,} pages, " if len(sizes) > 1 else ""
        rows = [r for r in quality_rows if r["n"] == n]
        for cls in CLASSES:
            rs = [r for r in rows if r["class"] == cls]
            if cls == "none" or not rs:
                continue
            for split, name in HALVES:
                recall(f"{at}{cls}, {name}", [r for r in rs if r["split"] == split])
            recall(f"{at}{cls}, all", rs)
            if cls == "two_page":
                recall(f"{at}two_page, either page, all", _either(rs))
        for label, part in groups(rows):
            recall(f"{at}{label}", part)
        none = _false_hits([r for r in rows if r["class"] == "none"])
        for kind in ("absent", "mixed", "all"):
            rs = [r for r in none if kind in ("all", r["sub_kind"])]
            if rs:
                out[f"{at}none, {kind} ({len(rs)}): probes with a page"] = _of(rs, "hit", percent=True)
        for split, name in HALVES:
            part = [r for r in none if r["split"] == split]
            if part:
                out[f"{at}none, {name} ({len(part)}): probes with a page"] = _of(part, "hit", percent=True)
        if none:
            out[f"{at}none: median pages returned"] = f"{statistics.median(int(r['returned']) for r in none):g}"
        if rows:
            out[f"{at}answered by strict / fallback / nothing ({len(rows)})"] = _sources(rows)
        timed = [r for r in latency_rows if r["n"] == n]
        for kind in _kinds(timed):
            tree, baseline = _cell(timed, "tree", kind), _cell(timed, "baseline", kind)
            if tree is not None:
                same_pass = f" (baseline in the same passes {baseline:.3f})" if baseline is not None else ""
                out[f"{at}latency, {kind}, s"] = f"{tree:.3f}{same_pass}"
        if timed:
            loads = {r["pass"]: r["load1"] for r in timed}
            out[f"{at}load average at each pass"] = ", ".join(loads[p] for p in sorted(loads, key=int))
    return out


def compare(first: Path, second: Path) -> str:
    """Two result folders side by side, as one markdown table. ``CompareError`` when a folder holds no
    results or the two were not measured on the same store sizes, corpus and probe set."""
    envs = []
    for folder in (first, second):
        env = read_env(folder)
        if not any(name in env for name in PARTS):
            raise CompareError(f"{folder}: no results here (no env.json with a quality or a latency part)")
        envs.append(env)
    seen = [(str(folder), name, identity(env[name]))
            for folder, env in zip((first, second), envs) for name in PARTS if name in env]
    for folder, env in zip((first, second), envs):  # and each csv holds the sizes its record names
        for name in PARTS:
            sizes = sorted({int(r["n"]) for r in read_rows(folder / f"{name}.csv")})
            if (name in env or sizes) and sizes != sorted(env.get(name, {}).get("sizes") or []):
                raise CompareError(f"not comparable: {folder}/{name}.csv holds pages {sizes}, which is not what "
                                   "its env.json records")
    if len({json.dumps(value, sort_keys=True) for *_, value in seen}) > 1:
        detail = "; ".join(f"{folder} ({name}): pages {sizes}, corpus {corpus}, probe set {probes}"
                           for folder, name, (sizes, corpus, probes) in seen)
        raise CompareError(f"not comparable: the runs differ in store size, corpus or probe set. {detail}")

    def fact(env: dict[str, Any], get: Callable[[dict[str, Any]], str], part: str | None = None) -> str:
        found = [env[name] for name in ((part,) if part else PARTS) if name in env]
        return get(found[0]) if found else "-"

    facts = (("pages", lambda p: _pages(p), None),
             ("commit, quality part", lambda p: str(p.get("commit"))[:8], "quality"),
             ("commit, latency part", lambda p: str(p.get("commit"))[:8], "latency"),
             ("probes (probe set)", lambda p: f"{p.get('probes')} ({p.get('probe_set')})", None),
             ("`search_approximate` found", lambda p: "yes" if p.get("search_approximate") else "no", None))
    a, b = measures(first), measures(second)
    lines = [f"| measure | {label_of(first)} | {label_of(second)} |", "|---|---|---|"]
    lines += [f"| {name} | {fact(envs[0], get, part)} | {fact(envs[1], get, part)} |" for name, get, part in facts]
    lines += [f"| {key} | {a.get(key, '-')} | {b.get(key, '-')} |" for key in dict.fromkeys((*a, *b))]
    return "\n".join(lines) + "\n"


# Main


def cpu_model() -> str:
    try:
        if sys.platform == "darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                  check=True).stdout.strip()
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    return platform.processor() or platform.machine()


def git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    except OSError:
        return None


def part_record(args: argparse.Namespace, part: str) -> dict[str, Any]:
    """What one part of a run ran on: versions and numbers only (no host name, no path)."""
    diff = git(ROOT, "diff", "--quiet", "HEAD", "--", "src")
    mac = platform.mac_ver()[0]
    record = {"commit": scale_bench.git_head(), "src_matches_commit": diff is not None and diff.returncode == 0,
              "python": platform.python_version(),
              "os": f"macOS {mac}" if mac else f"{platform.system()} {platform.release()}",
              "cpu": cpu_model(), "cpus": os.cpu_count(), "sizes": args.sizes, "probes": len(PROBES),
              "probe_set": probe_set_id(),
              "corpus": {"id": corpus_id(), "seed": SEED, "floor": FLOOR, "extra_plants": [f.id for f in EXTRA_FACTS]},
              "search_approximate": hasattr(SessionStore, "search_approximate"),
              "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "load_start": [round(x, 2) for x in os.getloadavg()]}
    if part == "latency":
        record |= {"passes": args.passes, "reps": args.reps}
        if args.baseline_src is not None:
            head = git(args.baseline_src, "rev-parse", "HEAD")
            record["baseline_commit"] = head.stdout.strip() if head is not None and head.returncode == 0 else "unknown"
    return record


def _replace(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temporary name, so the file is whole or the old one."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _terminated(signum: int, frame: Any) -> None:
    raise SystemExit(128 + signum)  # so SIGTERM runs the clean-up SIGINT already ran


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", type=lambda s: [int(x) for x in s.split(",")], default=list(SIZES),
                        help="store sizes in pages (default 6000)")
    parser.add_argument("--label", default="before", help="the result folder under --out (default: before)")
    parser.add_argument("--out", type=Path, default=HERE / "results" / "d65")
    parser.add_argument("--store-home", type=Path, default=None,
                        help="keep the store here and reuse it on the next run (default: a temporary directory)")
    parser.add_argument("--baseline-src", type=Path, default=None,
                        help="the src directory of another checkout, timed against this one in the same passes")
    parser.add_argument("--quality-only", action="store_true")
    parser.add_argument("--latency-only", action="store_true")
    parser.add_argument("--passes", type=int, default=4,
                        help="latency passes (default 4; an even number times each side's first search in both orders)")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"),
                        help="print two result folders side by side and exit")
    args = parser.parse_args(argv)
    if args.compare:
        try:
            print(compare(*args.compare), end="")
        except CompareError as e:
            print(f"fallback_bench: {e}", file=sys.stderr)
            return 2
        return 0
    if args.quality_only and args.latency_only:
        parser.error("--quality-only and --latency-only exclude each other")
    if args.store_home is not None:
        refuse_live_home(args.store_home)
    sides: dict[str, Callable[[Path, str], Any]] = {"tree": lambda home, session: scale_bench.McpChild(home, session)}
    if args.baseline_src is not None:
        src = args.baseline_src.resolve()
        if not (src / "contexto" / "__init__.py").is_file():
            raise SystemExit(f"--baseline-src {args.baseline_src} holds no contexto package")
        sides["baseline"] = lambda home, session: scale_bench.McpChild(home, session, src=src)
    parts = [name for name, skip in (("quality", args.latency_only), ("latency", args.quality_only)) if not skip]
    folder = args.out / args.label
    folder.mkdir(parents=True, exist_ok=True)
    temporary = {name: folder / f"{name}.csv.tmp" for name in parts}
    try:
        previous = signal.signal(signal.SIGTERM, _terminated)
    except ValueError:  # not the main thread: the caller owns signals
        previous = None
    home = args.store_home or Path(tempfile.mkdtemp(prefix="contexto-d65-"))
    files, done = {}, False
    try:
        home.mkdir(parents=True, exist_ok=True)
        records = {name: part_record(args, name) for name in parts}
        files = {name: path.open("w", newline="", encoding="utf-8") for name, path in temporary.items()}
        writers = {name: csv.DictWriter(f, QUALITY_FIELDS if name == "quality" else LATENCY_FIELDS)
                   for name, f in files.items()}
        for writer in writers.values():
            writer.writeheader()
        for n in args.sizes:
            ids = open_store(home, n)
            if "quality" in parts:
                print(f"[{time.strftime('%H:%M:%S')}] N={n}: quality, {len(PROBES)} probes", flush=True)
                quality(home, n, ids, writers["quality"])
            if "latency" in parts:
                chooser = SessionStore(home, session_name(n))
                chooser.set_search_cache(None)  # as in the quality pass: speed only
                queries = latency_queries(chooser)
                for p in range(1, args.passes + 1):
                    print(f"[{time.strftime('%H:%M:%S')}] N={n}: latency pass {p}", flush=True)
                    latency_pass(home, n, p, args.reps, queries, writers["latency"], sides)
                    files["latency"].flush()
        done = True
    finally:
        for f in files.values():
            f.close()
        if not done:  # the folder keeps what it held before this run
            for path in temporary.values():
                path.unlink(missing_ok=True)
        if args.store_home is None:
            shutil.rmtree(home, ignore_errors=True)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    for name in parts:
        records[name] |= {"finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "load_end": [round(x, 2) for x in os.getloadavg()]}
        os.replace(temporary[name], folder / f"{name}.csv")
    merged = {**read_env(folder), **records}  # a part this run did not measure keeps its own record
    env = {"label": args.label, **{name: merged[name] for name in PARTS if name in merged}}
    _replace(folder / "env.json", json.dumps(env, indent=1) + "\n")
    summary = summarize(folder)
    _replace(folder / "summary.md", summary)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
