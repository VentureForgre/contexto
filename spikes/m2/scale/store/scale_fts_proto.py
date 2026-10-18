"""Prototype the obvious search fixes on a real store (measurement only; nothing in src/ changes).

    uv run python spikes/m2/scale/store/scale_fts_proto.py --sizes 1000,6000

Builds the same store as ``scale_bench`` (through ``SessionStore.page_out``), then measures, against
the current ``SessionStore.search`` on that store:

- ``text cache``: each page's searchable text (the haystack ``page_matches`` reads) held in memory
  by the MCP process; pages are immutable, so the cache never goes stale. Load time, memory, query time.
- ``fts5 trigram``: an SQLite FTS5 table with the trigram tokenizer, ``case_sensitive 1`` (the same
  exact, case-sensitive substring semantics as today), one row per page inserted and committed as
  ``page_out`` would. Insert time per page, index size, query time; the result set is checked
  against the current search for every query.
- ``fts5 bm25``: an FTS5 word index (unicode61, case-insensitive, all query words in any order)
  ranked by BM25, for retrieval quality: where the planted page of each probe query lands.

Writes ``fts_proto.csv`` (timings) and ``fts_quality.csv`` (one row per probe query) to ``--out``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import scale_bench  # noqa: E402  (puts src/ on the path)
from contexto.store import SessionStore  # noqa: E402
from scale_probes import FACTS, LATENCY_QUERIES, planted_turn  # noqa: E402

WORD = re.compile(r"\w+")


def timed(fn, reps: int) -> tuple[float, object]:
    times, out = [], None
    for _ in range(reps):
        start = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - start)
    return statistics.median(times), out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", type=lambda s: [int(x) for x in s.split(",")], default=[1000, 6000])
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--out", type=Path, default=HERE / "results")
    parser.add_argument("--summarize-only", action="store_true",
                        help="rewrite fts_summary.md from the CSVs already in --out")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.summarize_only:
        timing = list(csv.DictReader((args.out / "fts_proto.csv").open()))
        qual = list(csv.DictReader((args.out / "fts_quality.csv").open()))
        for r in timing + qual:
            r["n"] = int(r["n"])
        (args.out / "fts_summary.md").write_text(summarize(timing, qual, args.out / "quality.csv"), encoding="utf-8")
        return 0
    queries = [(k, q) for k, qs in LATENCY_QUERIES.items() for q in qs]
    timing_rows, quality_rows = [], []
    for n in args.sizes:
        home = Path(tempfile.mkdtemp(prefix="contexto-fts-"))
        try:
            built = scale_bench.build_store(home, "s", n, dialect="messages", floor=0.0, seed=7)
            store = SessionStore(home, "s")
            ids = built["ids"]
            turn_of = {pid: t for t, pid in enumerate(ids)}

            def row(what: str, query: str, seconds: float, **extra) -> None:
                timing_rows.append({"n": n, "what": what, "query": query, "seconds": f"{seconds:.6f}", **extra})

            # Current search, for reference and for the result-set check.
            current = {}
            for kind, q in queries:
                s, current[q] = timed(lambda: store.search(q), args.reps)
                row(f"current search ({kind})", q, s, hits=len(current[q]))

            # Text cache: load once, then scan in memory.
            tracemalloc.start()
            start = time.perf_counter()
            cache = {}
            for path in store.pages_dir.glob("pg_*.json"):
                page = json.loads(path.read_text(encoding="utf-8"))
                cache[page["id"]] = (page["turn"], scale_bench.page_text(page))
            load_s = time.perf_counter() - start
            mem = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            row("text cache: load all pages", "", load_s, bytes=mem)
            ordered = sorted(cache.items(), key=lambda kv: (kv[1][0], kv[0]), reverse=True)  # kept with the cache
            for kind, q in queries:
                s, hits = timed(lambda: [pid for pid, (t, text) in ordered if q in text], args.reps)
                row(f"text cache search ({kind})", q, s, hits=len(hits), same=hits == current[q])

            # FTS5 trigram, case-sensitive: exact substring semantics, written at page time.
            db_path = home / "fts.sqlite"
            db = sqlite3.connect(db_path)
            db.execute("CREATE VIRTUAL TABLE pages USING fts5(page_id UNINDEXED, turn UNINDEXED, body, "
                       "tokenize='trigram case_sensitive 1')")
            inserts = []
            for pid in ids:
                turn, text = cache[pid]
                start = time.perf_counter()
                db.execute("INSERT INTO pages(page_id, turn, body) VALUES (?, ?, ?)", (pid, turn, text))
                db.commit()
                inserts.append(time.perf_counter() - start)
            db.close()
            row("fts5 trigram: insert+commit per page (median)", "", statistics.median(inserts),
                bytes=db_path.stat().st_size)
            db = sqlite3.connect(db_path)
            for kind, q in queries:
                phrase = '"' + q.replace('"', '""') + '"'
                s, hits = timed(lambda: [r[0] for r in db.execute(
                    "SELECT page_id FROM pages WHERE pages MATCH ? ORDER BY CAST(turn AS INTEGER) DESC",
                    (phrase,))], args.reps)
                row(f"fts5 trigram search ({kind})", q, s, hits=len(hits), same=hits == current[q])
            db.close()

            # FTS5 word index ranked by BM25: where would the planted pages land?
            wdb = sqlite3.connect(":memory:")
            wdb.execute("CREATE VIRTUAL TABLE w USING fts5(page_id UNINDEXED, turn UNINDEXED, body)")
            wdb.executemany("INSERT INTO w(page_id, turn, body) VALUES (?, ?, ?)",
                            [(pid, t, text) for pid, (t, text) in cache.items()])
            for fact in FACTS:
                plant_ids = [ids[planted_turn(p.at, n)] for p in fact.plants]
                target = plant_ids[-1]
                for q in fact.queries:
                    words = WORD.findall(q)
                    match = " AND ".join('"' + w + '"' for w in words)
                    ranked = [r[0] for r in wdb.execute(
                        "SELECT page_id FROM w WHERE w MATCH ? ORDER BY bm25(w)", (match,))] if words else []
                    newest = sorted(ranked, key=lambda pid: -turn_of[pid])
                    pos = ranked.index(target) + 1 if target in ranked else None
                    pos_new = newest.index(target) + 1 if target in newest else None
                    first_plant = next((pid for pid in ranked[:3] if pid in plant_ids), None)
                    quality_rows.append({
                        "n": n, "fact": fact.id, "kind": fact.kind, "query": q, "word_matches": len(ranked),
                        "bm25_rank": pos, "words_newest_first_rank": pos_new,
                        "bm25_latest_first": (first_plant == target) if fact.kind == "changing" else ""})
        finally:
            shutil.rmtree(home, ignore_errors=True)
        print(f"N={n} done", flush=True)
    for name, rows in (("fts_proto", timing_rows), ("fts_quality", quality_rows)):
        with (args.out / f"{name}.csv").open("w", newline="") as f:
            cols = list(dict.fromkeys(k for r in rows for k in r))
            w = csv.DictWriter(f, cols)
            w.writeheader()
            w.writerows(rows)
    summary = summarize(timing_rows, quality_rows, args.out / "quality.csv")
    (args.out / "fts_summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


def summarize(timing: list[dict], quality: list[dict], current_quality: Path) -> str:
    """Markdown tables: search time per approach and kind, and where planted pages land per approach."""
    sizes = sorted({r["n"] for r in timing})
    lines = ["## Prototype speed (seconds, in-process; median of 5 calls per query, median over the 3 queries)", "",
             "| N | approach | rare | common | multi-word | result sets equal to today's | one-time cost |",
             "|---:|---|---:|---:|---:|---|---|"]
    for n in sizes:
        rs = [r for r in timing if r["n"] == n]
        for approach in ("current search", "text cache search", "fts5 trigram search"):
            cells = []
            for kind in ("rare", "common", "multi"):
                vals = [float(r["seconds"]) for r in rs if r["what"] == f"{approach} ({kind})"]
                cells.append(f"{statistics.median(vals):.4f}")
            same = [r.get("same") for r in rs if r["what"].startswith(approach) and "(" in r["what"]]
            once = ""
            if approach == "text cache search":
                load = next(r for r in rs if r["what"] == "text cache: load all pages")
                once = f"load {float(load['seconds']):.2f} s, {int(load['bytes']) / 1e6:.0f} MB held"
            elif approach == "fts5 trigram search":
                ins = next(r for r in rs if r["what"].startswith("fts5 trigram: insert"))
                once = f"{float(ins['seconds']) * 1000:.2f} ms insert+commit per page, index {int(ins['bytes']) / 1e6:.0f} MB"
            agree = "n/a" if approach == "current search" else ("yes" if all(str(s) == "True" for s in same) else "NO")
            lines.append(f"| {n} | {approach} | {' | '.join(cells)} | {agree} | {once} |")
    cur = list(csv.DictReader(current_quality.open())) if current_quality.exists() else []
    lines += ["", "## Prototype quality: where the planted (latest) page lands, per probe query", "",
              "| N | ordering | in top 3 | in top 53 | changing: first value page in the top 3 is the latest (of 24) | "
              "facts with a query in the top 3 (of 30) |", "|---:|---|---:|---:|---:|---:|"]
    for n in sorted({r["n"] for r in quality}):
        rs = [r for r in quality if r["n"] == n]
        cs = [r for r in cur if int(r["n"]) == n and r["dialect"] == "messages"]
        if cs:
            facts = {r["fact"] for r in cs if r["outcome"] == "detailed"}
            lines.append(f"| {n} | today: exact case-sensitive substring, newest first | "
                         f"{sum(r['outcome'] == 'detailed' for r in cs)} | "
                         f"{sum(r['outcome'] in ('detailed', 'listed') for r in cs)} | "
                         f"{sum(r['latest_first'] == 'True' for r in cs)} | {len(facts)} |")
        for key, label in (("words_newest_first_rank", "all words, case-insensitive, newest first"),
                           ("bm25_rank", "all words, case-insensitive, BM25 (no recency)")):
            ranks = [(r, int(r[key])) for r in rs if r[key]]
            facts = {r["fact"] for r, k in ranks if k <= 3}
            latest = str(sum(str(r["bm25_latest_first"]) == "True" for r in rs)) if key == "bm25_rank" else "n/a"
            lines.append(f"| {n} | {label} | {sum(k <= 3 for _, k in ranks)} | {sum(k <= 53 for _, k in ranks)} | "
                         f"{latest} | {len(facts)} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
