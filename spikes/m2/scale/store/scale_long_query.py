"""Long-query cost of ``rank`` at 6,000 pages: before and after narrowing pages word by word (D34).

Zero spend, synthetic. Pages are 6,000 words each drawn from a Zipf vocabulary of 50,000 words
(about 35 KB folded), so a query sentence drawn from the same distribution holds common and rare
words, as a pasted sentence does. Each query of 1 to 120 words is timed with one absent word last,
with it first, and with every word as drawn. ``--before`` is the commit whose ``search.py`` is the
before case (37fc926, the head reviewer-1 timed); the after case is the working tree.

    .venv/bin/python spikes/m2/scale/store/scale_long_query.py > results/d34_after/long_query.txt
"""

from __future__ import annotations

import argparse
import importlib.util
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from contexto.store import search as after  # noqa: E402


def load_before(commit: str):
    source = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:src/contexto/store/search.py"],
                            check=True, capture_output=True, text=True).stdout
    path = Path(tempfile.mkdtemp()) / "search_before.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("search_before", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["search_before"] = module
    spec.loader.exec_module(module)
    return module


def timed(rank, entries, query: str) -> tuple[float, int]:
    start = time.perf_counter()
    hits = rank(entries, query)
    return time.perf_counter() - start, len(hits)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=6000)
    parser.add_argument("--before", default="37fc926")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    before = load_before(args.before)
    rng = random.Random(args.seed)
    vocab = [f"w{i}x" for i in range(50_000)]
    weights = [1 / (i + 1) for i in range(len(vocab))]
    texts = [after.fold(" ".join(rng.choices(vocab, weights, k=6_000))) for _ in range(args.pages)]
    old = [(f"pg_{i}", i, t) for i, t in enumerate(texts)]
    new = [after.SearchEntry(f"pg_{i}", i, after.search_key(t), False) for i, t in enumerate(texts)]
    print(f"{args.pages} pages, {sum(map(len, texts)) / 1e6:.0f} MB folded; before {args.before}, after the "
          "working tree; seconds per call (hits)")
    print("| words | absent word | before, s | after, s |")
    print("|---:|---|---:|---:|")
    for words in (1, 3, 10, 40, 120):
        sentence = rng.choices(vocab, weights, k=words)
        cases = {"last": sentence[:-1] + ["absentterm"], "first": ["absentterm"] + sentence[1:], "none": sentence}
        for where, query in cases.items():
            q = " ".join(query)
            t_old, h_old = timed(before.rank, old, q)
            t_new, h_new = timed(after.rank, new, q)
            assert h_old == h_new, (q, h_old, h_new)
            print(f"| {words} | {where} | {t_old:.2f} ({h_old}) | {t_new:.2f} ({h_new}) |", flush=True)


if __name__ == "__main__":
    main()
