"""Time the approximate-match fallback against strict search on one store (D65). Zero spend.

For each query the strict search (``rank``, unchanged by D65) and the fallback (``rank_approximate``)
run in turn on the same entries, ``--reps`` times, alternating, and the least CPU seconds of each are
printed (CPU time and the least of several, because the figures are ratios and other work on the
machine only ever adds): what a strict miss costs today, what the fallback adds, and their sum over
the strict miss. With ``--old
FILE`` (``git show <commit>:src/contexto/store/search.py > FILE``) the ``rank`` of that file is timed
in the same alternation, to show that a strict hit costs what it did.

    python spikes/m2/scale/store/scale_bench.py --sizes 6000 --keep-store /tmp/scale   # builds the store
    PYTHONPATH=src python spikes/m2/scale/store/scale_fallback.py --home /tmp/scale/home --session scale6000
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

from contexto.store import SessionStore
from contexto.store import search as new_search
from contexto.store.catalog import text_budget_bytes

QUERIES = (
    ("strict hit, 1 word", "E4127"),
    ("strict hit, 3 words", "staging DB port"),
    ("strict hit, common word", "self"),
    ("miss, 1 absent word", "zzqqxx"),
    ("miss, identifier style", "refresh_token"),
    ("miss, 1 typo", "databse"),
    ("miss, extra word", "staging DB port zzqqxx"),
    ("miss, extra word first", "zzqqxx staging DB port"),
    ("miss, question", "what port is the staging databse on"),
    ("miss, 6 absent words", "zzqqxa zzqqxb zzqqxc zzqqxd zzqqxe zzqqxf"),
    ("miss, 2 absent long words", "kubernetes ingress"),
    ("miss, typo with a common half", "retrun"),
    ("miss, 120 words", " ".join(f"w{i}" for i in range(120))),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--cache-mb", default="64", help="the search text budget, as the MCP server's default")
    parser.add_argument("--old", type=Path, help="a search.py of an earlier commit, to time its rank alongside")
    args = parser.parse_args(argv)
    store = SessionStore.open_existing(args.home, args.session)
    store.set_search_cache(text_budget_bytes(args.cache_mb))
    old_rank = None
    if args.old is not None:
        spec = importlib.util.spec_from_file_location("search_old", args.old)
        old = importlib.util.module_from_spec(spec)
        sys.modules["search_old"] = old  # its dataclass looks its module up by name
        spec.loader.exec_module(old)
        old_rank = old.rank
    entries, fetch, _ = store._catalog.search_entries()
    new_search.rank(entries, "warm", fetch)  # the first search of a process reads every page
    entries, fetch, _ = store._catalog.search_entries()
    print(f"{len(entries)} pages, {sum(e.text is not None for e in entries)} held as they are")
    print(f"{'query':32} {'old rank':>9} {'rank':>8} {'fallback':>9} {'sum/rank':>9}  strict hits, approximate hits")
    for label, query in QUERIES:
        times: dict[str, list[float]] = {"old": [], "strict": [], "fallback": []}
        strict: list[str] = []
        approximate: list = []
        for _ in range(args.reps):
            if old_rank is not None:
                start = time.process_time()
                old_rank(entries, query, fetch)
                times["old"].append(time.process_time() - start)
            start = time.process_time()
            strict = new_search.rank(entries, query, fetch)
            times["strict"].append(time.process_time() - start)
            start = time.process_time()
            approximate = new_search.rank_approximate(entries, query, fetch)
            times["fallback"].append(time.process_time() - start)
        med = {k: min(v) if v else float("nan") for k, v in times.items()}
        ratio = (med["strict"] + med["fallback"]) / med["strict"]
        print(f"{label:32} {med['old']:9.3f} {med['strict']:8.3f} {med['fallback']:9.3f} {ratio:9.2f}  "
              f"{len(strict)}, {len(approximate)} of {approximate.total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
