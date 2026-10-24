"""BM25's length term on the store-scale probes, on the spike's page sizes and on page sizes drawn from
real turns (D41; zero spend).

    .venv/bin/python spikes/m2/scale/store/scale_length.py --sizes 17,1000,3000,6000 --out results/d41/length

For each page-size model (``spike``: every turn 19,000 to 30,000 tokens; ``calibration``: drawn from
the real turn sizes in ``calibration.json``, ``scale_synth.calibrated_goal``) and store size it writes
one store with ``scale_bench.build_store``, then runs every probe query of ``scale_probes`` through
``contexto_search`` in-process once per ranking variant:

- ``page b=X``: D34's ``rank``, BM25 counting every occurrence, with the length of the whole page
  against the mean page length, read from ``--d34-ref`` with ``git show`` so the comparison does not
  depend on the working tree. ``page b=0.75`` is D34 as merged; ``page b=0`` is D41's first version
  (PR #216 at 4d30c34: no length term, every occurrence counted);
- ``d41``: the working tree's ``rank`` (D41: b 0, and a term counted once per text that holds it);
- ``cap3``: ``page b=0`` with each page's count of a term capped at 3 (reviewer A's option on PR #216);
- ``texts b=X``: the plain reading of D34's follow-up: BM25 with the length of the page's texts that
  hold a query word, against the mean of that length over the matching pages;
- ``fields b=X``: BM25F: each text's count discounted by that text's length against the mean text
  length of the store, summed over the page's texts, then saturated;
- ``floored b=X``: the same BM25F with each text's length counted as at least the mean, so a text is
  discounted for being long but never boosted for being short.

D41 rejected every variant but ``d41``: Contexto ranks only as ``d41`` does, and ``cap3``, ``texts``,
``fields``, and ``floored`` exist only in this script, for the record. A text is one stub field or one
text, tool input, or tool output of the turn (``pages._texts``). The prototypes read entries that also
carry where each text ends (``entry_with_ends``); the working tree's search finds texts by
``search.TEXT_SEP``, which ``entry_with_ends`` also joins them with. It also runs reviewer-2's 13-turn cases from PR #204
(``tests/test_search_rank.py``) under each variant. Outputs (numbers only) go to ``--out``:
``quality.csv`` (every probe query, with the variant and page-size model) and ``summary.md``.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import importlib.util
import io
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from array import array
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

import scale_bench  # noqa: E402

import contexto.store as store_module  # noqa: E402
import contexto.store.catalog as catalog_module  # noqa: E402
import contexto.store.memory as memory_module  # noqa: E402
from contexto import mcp  # noqa: E402
from contexto.store import search as search_now  # noqa: E402
from contexto.store.memory import MemoryStore  # noqa: E402
from contexto.store.pages import _search_fields, _texts, is_context_page  # noqa: E402

D34_REF = "4b87ec2"  # main after PR #204 (D34)
HOLD_ALL_MB = "1048576"  # a search text budget (D39) that holds every store here as it is
K1 = 1.2
VARIANTS = (("page", 0.75), ("page", 0.5), ("page", 0.3), ("page", 0.0), ("d41", 0.0), ("cap3", 0.0),
            ("texts", 0.75), ("fields", 0.75), ("fields", 0.3), ("floored", 0.75))
GRID = (("page", 0.75), ("gridfull:1:none", 0.0)) + tuple(
    (f"grid:{c}:{p}", 0.0) for c in (1, 2, 3) for p in ("2", "3", "none"))
GRID_FULL = tuple((f"gridfull:{c}:{p}", 0.0) for c in (1, 2, 3) for p in ("2", "3", "none"))


def load_d34(ref: str) -> ModuleType:
    """D34's ``contexto.store.search`` as a separate module, from git."""
    try:
        source = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:src/contexto/store/search.py"],
                                capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"cannot read D34's search.py at {ref}: {e}") from e
    spec = importlib.util.spec_from_loader("search_d34", loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while the class is built
    exec(compile(source, f"{ref}:src/contexto/store/search.py", "exec"), module.__dict__)
    return module


# Entries that also carry where each text ends (for the prototypes)


class Entry(NamedTuple):
    """``contexto.store.search.SearchEntry`` plus what the other variants read: ``full``, every stub field
    and text of the page joined by ``search.TEXT_SEP`` (what D34 searched: the stub fields are not dropped
    when the turn's texts hold them), and ``ends``, the offset in ``full`` where each of them ends."""

    id: str
    turn: int
    text: bytes
    context: bool
    ends: array
    full: bytes


def entry_with_ends(page: dict[str, Any]) -> Entry:
    """The page as this tree's search holds it (``text``: the same bytes as ``pages.search_entry``), with
    ``full`` and its ``ends`` for D34 and the prototypes."""
    stub = page["stub"]
    fields = [stub["user_text"], stub.get("system_text", ""), stub.get("reminder_text", ""), *stub["tools"],
              *stub["paths"], *_texts(page["messages"])]
    keys = [search_now.search_key(search_now.fold(f)) for f in fields]
    ends, end = array("q"), -1
    for key in keys:
        end += 1 + len(key)
        ends.append(end)
    return Entry(page["id"], page["turn"], search_now.search_text(_search_fields(page)), is_context_page(page), ends,
                 search_now.TEXT_SEP.join(keys))


def term_counts(text: bytes, ends: Sequence[int], term: bytes) -> Iterator[tuple[int, int, int]]:
    """(index, occurrences, length) of each text holding ``term``. The texts are joined by
    ``search.TEXT_SEP``, a byte no query word or phrase holds, so an occurrence never spans two texts."""
    if not ends:
        found = text.count(term)
        if found:
            yield 0, found, len(text)
        return
    pos = text.find(term)
    while pos != -1:
        k = bisect.bisect_left(ends, pos + len(term))
        start = ends[k - 1] + 1 if k else 0
        yield k, text.count(term, start, ends[k]), ends[k] - start
        pos = text.find(term, ends[k] + 1)


# Prototype rankings: D34's matching, groups, idf, recency prior, and newest-page place; only relevance differs


def _rank_with(entries: Sequence[Any], query: str,
               relevance: Callable[[int, dict[bytes, float], bytes, dict[int, bool]], float]) -> list[str]:
    S = search_now
    q = S.Query.parse(query)
    if q is None or not entries:
        return []
    texts = [e.text for e in entries]
    n = len(texts)
    words = [S.search_key(w) for w in q.words]
    matched = [i for i in range(n) if all(w in texts[i] for w in words)]
    if not matched:
        return []
    df = {w: sum(1 for t in texts if w in t) for w in words}
    phrase = S.search_key(q.phrase)
    phrase_on = {i: (phrase in texts[i]) if q.multiword else True for i in matched}
    terms = words + ([phrase] if q.multiword else [])
    if q.multiword:
        df[phrase] = sum(phrase_on.values())
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in terms if df[t]}
    if not q.multiword:
        phrase = b""  # no phrase term: every term is scored on every matching page
    by_age = sorted(matched, key=lambda i: (entries[i].turn, entries[i].id))
    position = {i: (k / (len(by_age) - 1) if len(by_age) > 1 else 1.0) for k, i in enumerate(by_age)}
    scored = []
    for i in matched:
        score = relevance(i, idf, phrase, phrase_on) * (1 + S.RECENCY_WEIGHT * position[i])
        scored.append((not entries[i].context, phrase_on[i], score, entries[i].turn, entries[i].id))
    scored.sort(reverse=True)
    ranked = [page_id for *_, page_id in scored]
    real = [s for s in scored if s[0]] or scored
    newest = max(real, key=lambda s: (s[3], s[4]))[4]
    if ranked.index(newest) >= S.SHOWN_IN_DETAIL:
        ranked.remove(newest)
        ranked.insert(S.SHOWN_IN_DETAIL - 1, newest)
    return ranked


def rank_texts(entries: Sequence[Entry], query: str, b: float) -> list[str]:
    """BM25 with the length of the page's texts that hold a query word, against its mean over the matches."""
    words = [search_now.search_key(w) for w in (search_now.Query.parse(query) or search_now.Query("", ())).words]
    lengths: dict[int, int] = {}
    mean = [1.0]

    def relevance(i: int, idf: dict[bytes, float], phrase: bytes, phrase_on: dict[int, bool]) -> float:
        if not lengths:  # first call: the length of every matching page, and their mean
            for j in phrase_on:
                held = {k: size for w in words for k, _, size in term_counts(entries[j].text, entries[j].ends, w)}
                lengths[j] = sum(held.values())
            mean[0] = sum(lengths.values()) / len(lengths) or 1.0
        norm = K1 * (1 - b + b * lengths[i] / mean[0])
        total = 0.0
        for term, weight in idf.items():
            if term == phrase and not phrase_on[i]:
                continue
            tf = entries[i].text.count(term)
            total += weight * tf * (K1 + 1) / (tf + norm)
        return total
    return _rank_with(entries, query, relevance)


def rank_fields(entries: Sequence[Entry], query: str, b: float, floored: bool) -> list[str]:
    """BM25F over the page's texts against the store's mean text length; ``floored``: length at least the mean."""
    count = sum(max(len(e.ends), 1) for e in entries)
    avg = (sum(len(e.text) for e in entries) - sum(max(len(e.ends) - 1, 0) for e in entries)) / count or 1.0

    def relevance(i: int, idf: dict[bytes, float], phrase: bytes, phrase_on: dict[int, bool]) -> float:
        total = 0.0
        for term, weight in idf.items():
            if term == phrase and not phrase_on[i]:
                continue
            tf = sum(c / (1 - b + b * (max(size, avg) if floored else size) / avg)
                     for _, c, size in term_counts(entries[i].text, entries[i].ends, term))
            total += weight * tf * (K1 + 1) / (tf + K1)
        return total
    return _rank_with(entries, query, relevance)


def rank_capped(entries: Sequence[Entry], query: str, cap: int) -> list[str]:
    """BM25 with b 0 counting every occurrence, each page's count of a term capped at ``cap``."""
    def relevance(i: int, idf: dict[bytes, float], phrase: bytes, phrase_on: dict[int, bool]) -> float:
        total = 0.0
        for term, weight in idf.items():
            if term == phrase and not phrase_on[i]:
                continue
            tf = min(entries[i].text.count(term), cap)
            total += weight * tf * (K1 + 1) / (tf + K1)
        return total
    return _rank_with(entries, query, relevance)


def rank_grid(entries: Sequence[Any], query: str, text_cap: int, page_cap: int | None) -> list[str]:
    """This tree's ``rank`` with ``search.TEXT_CAP`` and ``search.PAGE_CAP`` set for the call (the D41 grid)."""
    saved = search_now.TEXT_CAP, search_now.PAGE_CAP
    search_now.TEXT_CAP, search_now.PAGE_CAP = text_cap, page_cap
    try:
        return search_now.rank(entries, query)
    finally:
        search_now.TEXT_CAP, search_now.PAGE_CAP = saved


def _rank_of(d34: ModuleType | None, kind: str, b: float) -> Callable[[Sequence[Any], str], list[str]]:
    if kind == "d41":
        return search_now.rank
    if kind.startswith(("grid:", "gridfull:")):  # grid:TEXT_CAP:PAGE_CAP; gridfull reads ``full``
        _, c, p = kind.split(":")
        return lambda entries, query: rank_grid(entries, query, int(c), None if p == "none" else int(p))
    if kind == "cap3":
        return lambda entries, query: rank_capped(entries, query, 3)
    if kind == "texts":
        return lambda entries, query: rank_texts(entries, query, b)
    if kind in ("fields", "floored"):
        return lambda entries, query: rank_fields(entries, query, b, kind == "floored")
    if d34 is None:
        raise SystemExit("page variants need D34's search.py (--d34-ref)")

    def rank(entries: Any, query: str) -> list[str]:
        saved = d34.B
        d34.B = b
        try:
            return d34.rank(entries, query)
        finally:
            d34.B = saved
    return rank


def ranker(d34: ModuleType | None, kind: str, b: float) -> Callable[..., list[str]]:
    """A ``rank(entries, query, fetch=None)`` as ``SessionStore.search`` calls it (D39). An entry whose text
    is not held is read through ``fetch`` first, so every variant sees every page; ``main`` holds all text
    (``HOLD_ALL_MB``), so this happens only if a page cannot be held. The variants that are not this
    tree's ``rank`` read each page's ``full`` text, as D34 searched it."""
    inner = _rank_of(d34, kind, b)
    own = kind == "d41" or kind.startswith("grid:")  # gridfull reads ``full``, as the tree at 0ff1bfe did

    def rank(entries: Sequence[Any], query: str, fetch: Callable[[int], Any] | None = None) -> list[str]:
        if any(e.text is None for e in entries):
            got = [e if e.text is not None else (fetch(i) if fetch is not None else None) for i, e in enumerate(entries)]
            entries = [e for e in got if e is not None and e.text is not None]
        if not own:
            entries = [e._replace(text=e.full) if isinstance(e, Entry) else e for e in entries]
        return inner(entries, query)
    return rank


def use(rank: Callable[..., list[str]]) -> None:
    """Search through ``rank``, over entries that carry text ends."""
    store_module.rank = memory_module.rank = rank
    catalog_module.search_entry = memory_module.search_entry = entry_with_ends


# Reviewer-2's 13-turn cases (PR #204), as in tests/test_search_rank.py

SOAK = "\n".join(f"{i:04d} INFO worker {i % 7} processed batch {i} in {i % 13} ms" for i in range(900))
PASSING = ["note: max_retries is read once at startup", "max_retries applies per request",
           "the max_retries default stays in config.toml", "grep shows max_retries in two places"]
ROWS = ((5, 6, 13), (6, 13), (6, 8, 12, 13), (13,))
DETAILED = re.compile(r"^pg_[0-9a-f]{16} \(turn (\d+)\)$", re.M)


def _page(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": "continue"}, {"role": "assistant", "content": [{"type": "text", "text": text}]}]


def residual(mentions: tuple[int, ...]) -> list[int]:
    """The detailed turns for ``max_retries``: turn 4 sets 3, the long turn 10 sets 7, ``mentions`` mention it."""
    store = MemoryStore("s1")
    for t in range(1, 14):
        if t == 4:
            turn = _page("Set max_retries to 3 in the client config.")
        elif t == 10:
            turn = [{"role": "user", "content": "run the soak test and raise the retry limit"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "t10", "name": "Bash",
                                                       "input": {"command": "./soak.sh"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t10", "content": SOAK}]},
                    {"role": "assistant", "content": [{"type": "text", "text": "Soak test passed. max_retries is now 7"}]}]
        elif t in mentions:
            turn = _page(PASSING[mentions.index(t) % len(PASSING)])
        else:
            turn = _page("ran the linter, no findings")
        store.page_out([turn], t)
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_search", "arguments": {"query": "max_retries"}}})
    reply = server.handle_line(line)["result"]["content"][0]["text"]
    return [int(m.group(1)) for m in DETAILED.finditer(reply)]


def _search_turns(store: MemoryStore, query: str) -> list[int]:
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_search", "arguments": {"query": query}}})
    reply = server.handle_line(line)["result"]["content"][0]["text"]
    return [int(m.group(1)) for m in DETAILED.finditer(reply)]


FILLER = "\n".join(f"step {i}: ran the build and checked the output, nothing unusual" for i in range(40))


def scenario_about() -> bool:
    """D34's ``test_a_page_about_the_query_outranks_newer_pages_that_mention_it_once`` as on main: turn 1's
    reply says "rate limit" in 4 lines of one text, 30 newer pages once. True when turn 1 is shown."""
    store = MemoryStore("s1")
    about = "\n".join(["The rate limit is 40 requests per minute per key.", "Raised the rate limit warning threshold.",
                       "rate limit headers: X-RateLimit-Remaining", "The rate limit resets every 60 seconds."])
    store.page_out([_page(FILLER + "\n" + about)], 1)
    store.page_out([_page(FILLER + "\nwe may hit a rate limit here") for _ in range(30)], 2)
    return 1 in _search_turns(store, "rate limit")


def scenario_equal() -> bool:
    """D34's ``test_the_newest_of_equally_relevant_statements_is_shown_first`` as on main: turns 8, 30, 45 set
    the timeout in one reply that says it twice; every 4th turn mentions it in passing. True when, for
    each of its 3 queries, the first value page shown is turn 45."""
    store = MemoryStore("s1")
    for t in range(1, 61):
        if t in (8, 30, 45):
            store.page_out([_page(FILLER + f"\nSet the HTTP client timeout to {t} seconds.\nHTTP_TIMEOUT_S = {t}")], t)
        else:
            store.page_out([_page(FILLER + ("\na client timeout surfaces as a 504" if t % 4 == 0 else ""))], t)
    for query in ("timeout", "client timeout", "HTTP_TIMEOUT_S"):
        values = [t for t in _search_turns(store, query) if t in (8, 30, 45)]
        if values[:1] != [45]:
            return False
    return True


def residual_ok(shown: list[int]) -> bool:
    """The current value (turn 10) is shown in detail, before the old one (turn 4)."""
    return [t for t in shown if t in (4, 10)][:1] == [10]


# Summary


def variants_named() -> list[tuple[str, tuple[str, float]]]:
    def name(kind: str, b: float) -> str:
        if kind.startswith(("grid:", "gridfull:")):
            _, c, p = kind.split(":")
            if kind.startswith("gridfull:"):
                return f"text cap {c}, page cap {p}" + (" (0ff1bfe)" if (c, p) == ("1", "none") else ", summary kept")
            return f"text cap {c}, page cap {p}"
        return kind if kind in ("d41", "cap3") else f"{kind} b={b:g}"
    return [(name(kind, b), (kind, b)) for kind, b in VARIANTS]


def summarize(rows: list[dict[str, Any]], residuals: dict[str, list[list[int]]],
              stores: list[tuple[str, int, dict[str, Any]]], scenarios: dict[str, tuple[bool, bool]]) -> str:
    out = ["| page sizes | N | page tokens min / median / max | total page tokens |", "|---|---:|---|---:|"]
    for sizes, n, row in stores:
        out.append(f"| {sizes} | {n:,} | {row['page_tokens_min']:,} / {row['page_tokens_median']:,} / "
                   f"{row['page_tokens_max']:,} | {row['page_tokens_total']:,} |")
    out += ["", "| page sizes | N | variant | top 3 (of 86) | latest first (of 24) | stale only (of 24) "
            "| facts (of 30) | value shown |", "|---|---:|---|---:|---:|---:|---:|---:|"]
    names = [name for name, _ in variants_named()]
    keys = sorted({(r["page_sizes"], int(r["n"]), r["variant"]) for r in rows},
                  key=lambda k: (k[0] != "spike", k[1], names.index(k[2]) if k[2] in names else len(names)))
    for sizes, n, variant in keys:
        rs = [r for r in rows if (r["page_sizes"], int(r["n"]), r["variant"]) == (sizes, n, variant)]
        top = sum(r["outcome"] == "detailed" for r in rs)
        latest = sum(r["latest_first"] is True for r in rs)
        stale = sum(r["stale_only"] is True for r in rs)
        facts = len({r["fact"] for r in rs if r["outcome"] == "detailed"})
        shown = sum(r["value_shown"] is True for r in rs)
        out.append(f"| {sizes} | {n:,} | {variant} | {top} | {latest} | {stale} | {facts} | {shown} |")
    out += ["", "Reviewer-2's 13-turn cases (detailed turns for `max_retries`; ok: turn 10, the current value, is "
            "shown and comes before turn 4):", "",
            "| variant | " + " | ".join(f"mentions {', '.join(map(str, r))}" for r in ROWS) + " | ok (of 4) |",
            "|---|" + "---|" * len(ROWS) + "---:|"]
    for variant, shown in residuals.items():
        cells = [f"{', '.join(map(str, s))}{'' if residual_ok(s) else ' (no)'}" for s in shown]
        out.append(f"| {variant} | " + " | ".join(cells) + f" | {sum(map(residual_ok, shown))} |")
    out += ["", "D34's two scenarios as main's tests state them (`scenario_about`, `scenario_equal`):", "",
            "| variant | a page about the query is shown | the newest of equal statements comes first |",
            "|---|---|---|"]
    for variant, (about, equal) in scenarios.items():
        out.append(f"| {variant} | {'yes' if about else 'no'} | {'yes' if equal else 'no'} |")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", type=lambda s: [int(x) for x in s.split(",")], default=[17, 1000, 3000, 6000])
    parser.add_argument("--page-sizes", type=lambda s: s.split(","), default=["spike", "calibration"])
    parser.add_argument("--floor", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--d34-ref", default=D34_REF)
    parser.add_argument("--out", type=Path, default=HERE / "results" / "d41" / "length")
    parser.add_argument("--grid", action="store_true",
                        help="run D34, D41 at 0ff1bfe, and every TEXT_CAP 1-3 and PAGE_CAP 2, 3, none instead")
    parser.add_argument("--grid-full", action="store_true",
                        help="run the grid cells that keep the stub fields repeating the prompt or paths")
    args = parser.parse_args(argv)
    global VARIANTS
    if args.grid:
        VARIANTS = GRID
    elif args.grid_full:
        VARIANTS = GRID_FULL
    args.out.mkdir(parents=True, exist_ok=True)
    d34 = load_d34(args.d34_ref)
    scale_bench.SEARCH_CACHE_MB = HOLD_ALL_MB  # the prototypes read each page's text ends from the held entry
    original = (store_module.rank, memory_module.rank, catalog_module.search_entry, memory_module.search_entry)
    home = Path(tempfile.mkdtemp(prefix="contexto-length-"))
    rows: list[dict[str, Any]] = []
    residuals: dict[str, list[list[int]]] = {}
    scenarios: dict[str, tuple[bool, bool]] = {}
    stores: list[tuple[str, int, dict[str, Any]]] = []
    try:
        for name, (kind, b) in variants_named():
            use(ranker(d34, kind, b))
            residuals[name] = [residual(r) for r in ROWS]
            scenarios[name] = (scenario_about(), scenario_equal())
        for sizes in args.page_sizes:
            for n in args.sizes:
                session = f"length-{sizes}-{n}"
                print(f"[{time.strftime('%H:%M:%S')}] {sizes} N={n}: building", flush=True)
                built = scale_bench.build_store(home, session, n, dialect="messages", floor=args.floor,
                                                seed=args.seed, sizes=sizes)
                if not built["markers_ok"]:
                    raise SystemExit(f"a planted marker is missing from its page ({sizes}, N={n})")
                stores.append((sizes, n, built["row"]))
                for name, (kind, b) in variants_named():
                    use(ranker(d34, kind, b))
                    sink = csv.DictWriter(io.StringIO(), scale_bench.QUALITY_FIELDS)
                    for r in scale_bench.quality(home, session, n, "messages", built["ids"], sink):
                        rows.append({**r, "page_sizes": sizes, "variant": name})
                    print(f"[{time.strftime('%H:%M:%S')}] {sizes} N={n}: {name} done", flush=True)
                shutil.rmtree(home / "sessions" / session, ignore_errors=True)
    finally:
        store_module.rank, memory_module.rank, catalog_module.search_entry, memory_module.search_entry = original
        shutil.rmtree(home, ignore_errors=True)
    with (args.out / "quality.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, ["page_sizes", "variant", *scale_bench.QUALITY_FIELDS])
        w.writeheader()
        w.writerows(rows)
    summary = summarize(rows, residuals, stores, scenarios)
    (args.out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
