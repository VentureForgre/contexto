"""Old tool-output dumps that repeat a key against the newer turn that states its value (D41; zero spend).

Reviewer A's simulation on PR #216, kept so D41's figures can be rerun. Every text comes from this
repository: Reads of its files, ``git log --stat``, and real tool outputs made by
``scale_dumps_gen.sh`` (``grep -rn KEY``, ``grep -rn -C3 KEY``, and ``pytest -vv --tb=long`` after
changing the key's value or making it a string). Bash outputs are cut at 30,000 characters (Claude
Code's limit) and Reads at 100,000.

    git worktree add /tmp/cx-dumps-repo 4d30c34          # the tree the dumps and files are read from
    spikes/m2/scale/store/scale_dumps_gen.sh /tmp/cx-dumps-repo /tmp/cx-dumps .venv/bin/python
    .venv/bin/python spikes/m2/scale/store/scale_dumps.py --repo /tmp/cx-dumps-repo --dumps /tmp/cx-dumps \\
        --trials 500 --seed 7                             # "loud"
    .venv/bin/python spikes/m2/scale/store/scale_dumps.py --repo /tmp/cx-dumps-repo --dumps /tmp/cx-dumps \\
        --trials 500 --seed 11 --quiet                    # "quiet"
    ... --trials 300 --seed 21 --quiet --adversarial many_calls   # and seed 22 split_output, 23 edits

Each session has 40, 150, or 600 turns. 1, 2, or 4 old dumps of the key come before the turn that
states its new value, which is one of four kinds: a one-line statement, an Edit (the key 4 times),
an Edit then a pytest run, or a long tool output then a one-line statement (the soak shape). 0 to 3
short passing mentions follow it, and half the sessions also hold an older one-line statement of the
old value. Other turns Read files, run pytest, or show git log, none naming the key. "Loud": the dump
turn's prompt and reply also name the key; ``--quiet``: only its command and output do. Keys are 10
constants of this repository and 3 heavily used identifiers (``page_out``, ``search_key``,
``build_page``). Pages are built with ``pages.build_page`` and ``scale_length.entry_with_ends`` and
ranked by ``scale_length``'s rankers for the query ``KEY``.

Per variant it prints, over all sessions and by bucket: the statement among the 3 detailed pages; an
old dump ranked above the statement; stale only (the statement not in detail, an old dump or older
statement in detail); and the newest mention in detail. Numbers only.
"""

from __future__ import annotations

import argparse
import csv
import functools
import io
import json
import random
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[3] / "src"))

import scale_length  # noqa: E402

from contexto.store.pages import build_page  # noqa: E402
from contexto.store.search import SHOWN_IN_DETAIL, fold  # noqa: E402

BASH_CAP = 30_000  # Claude Code cuts Bash output at 30,000 characters
READ_CAP = 100_000  # a Read of a saved log (2,000 lines)
QUALITY_REF = "4d30c34"  # the JSON dumps quote rows of results/d41/length/quality.csv as of this commit
KEYS = {  # key: (old value, new value)
    "RECENCY_WEIGHT": ("0.5", "0.8"), "SHOWN_IN_DETAIL": ("3", "4"), "SEARCH_MORE_LIMIT": ("50", "40"),
    "MATCHES_PER_PAGE": ("3", "4"), "MATCH_CONTEXT": ("1", "2"), "MATCH_LINE_CHARS": ("200", "160"),
    "RECALL_LIMIT": ("32_000", "24_000"), "IN_VIEW_LIMIT": ("3", "5"), "STUB_TEXT_CHARS": ("200", "300"),
    "UPSTREAM_TIMEOUT_S": ("900", "600"),
    # heavily used identifiers: a cut grep repeats them hundreds of times
    "page_out": ("list", "tuple"), "search_key": ("bytes", "str"), "build_page": ("dict", "Page"),
}
VARIANTS = {"D34 (b 0.75)": ("page", 0.75), "D41 first (b 0, every occurrence)": ("page", 0.0),
            "b 0, count capped at 3": ("cap3", 0.0), "D41 at 0ff1bfe (once per text)": ("gridfull:1:none", 0.0),
            **{f"text cap {c}, page cap {p}": (f"grid:{c}:{p}", 0.0) for c in (1, 2, 3) for p in ("2", "3", "none")}}
# The same cells with the stub fields that repeat the turn's prompt or paths counted again, as at 0ff1bfe
VARIANTS_FULL = {f"text cap {c}, page cap {p}, summary kept": (f"gridfull:{c}:{p}", 0.0)
                 for c in (1, 2, 3) for p in ("2", "3", "none")}
ADVERSARIAL = ("many_calls", "split_output", "edits")
PASSING = ["{k} is read once at startup.", "{k} applies per request.", "grep shows {k} in two places.",
           "Left {k} alone in this change."]


class Corpus:
    """The repository text the sessions are made of."""

    def __init__(self, repo: Path, dumps: Path) -> None:
        self.repo = repo
        self.files = (sorted(repo.glob("src/**/*.py")) + sorted(repo.glob("tests/*.py"))
                      + sorted(repo.glob("docs/**/*.md")) + sorted(repo.glob("spikes/m2/scale/store/*.py")))
        if not self.files:
            raise SystemExit(f"no repository files under {repo}")
        self.file_text = {p: p.read_text(errors="replace") for p in self.files}
        self.logs = {p.name: p.read_text(errors="replace") for p in dumps.glob("*.pytest_*")}
        if not self.logs:
            raise SystemExit(f"no pytest dumps in {dumps}; run scale_dumps_gen.sh first")
        self.gitlog = self.sh("git log --stat -n 400")
        path = "spikes/m2/scale/store/results/d41/length/quality.csv"
        self.quality = list(csv.DictReader(io.StringIO(self.sh(f"git show {QUALITY_REF}:{path}"))))

    @functools.cache  # noqa: B019 (one corpus per run)
    def sh(self, cmd: str) -> str:
        try:
            return subprocess.run(cmd, shell=True, cwd=self.repo, capture_output=True, text=True, check=False).stdout
        except OSError as e:
            raise SystemExit(f"cannot run {cmd!r} in {self.repo}: {e}") from e


def cat_n(text: str, limit: int = 2000) -> str:
    return "\n".join(f"{i:6d}\t{line}" for i, line in enumerate(text.splitlines()[:limit], 1))


def turn(prompt: str, calls: list[tuple[str, dict, str]], reply: str) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    for name, inp, out in calls:
        tid = f"toolu_{random.getrandbits(48):012x}"
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": out}]})
    msgs.append({"role": "assistant", "content": [{"type": "text", "text": reply}]})
    return msgs


def has(text: str, key: str) -> bool:
    return fold(key) in fold(text)


def filler(c: Corpus, rng: random.Random, key: str) -> list[dict[str, Any]]:
    """A turn that does not name the key: Reads of repository files, pytest logs, git log, or a short exchange."""
    if rng.random() < 0.3:
        return turn("continue", [], rng.choice(["Done.", "The linter is clean.", "Committed.",
                                                "Looks good; moving on to the next item."]))
    calls = []
    for _ in range(rng.choice([1, 1, 2, 3])):
        r = rng.random()
        if r < 0.55:
            p = rng.choice(c.files)
            text = c.file_text[p]
            if has(text, key):
                continue
            calls.append(("Read", {"file_path": str(p)}, cat_n(text)[:READ_CAP]))
        elif r < 0.8:
            _, text = rng.choice(list(c.logs.items()))
            if has(text, key):
                continue
            calls.append(("Bash", {"command": "pytest -vv --tb=long"}, text[:BASH_CAP]))
        else:
            start = rng.randrange(0, max(1, len(c.gitlog) - 20_000))
            chunk = c.gitlog[start:start + rng.randrange(2_000, 20_000)]
            if has(chunk, key):
                continue
            calls.append(("Bash", {"command": "git log --stat"}, chunk))
    return turn("keep going", calls, "Read the files; next step is the edit.")


def json_dump(c: Corpus, key: str, old: str, n: int) -> str:
    rows = [{"variant": q["variant"], "query": q["query"], "outcome": q["outcome"],
             "config": {key.lower(): old, "k1": 1.2}} for q in c.quality[:n]]
    return json.dumps(rows, indent=2)


def old_dump(c: Corpus, rng: random.Random, key: str, old: str, quiet: bool) -> list[dict[str, Any]]:
    """An old turn whose tool output repeats the key (real output from this repository)."""
    kind = rng.choice(["grep", "grepc", "pytest_value", "pytest_type", "pytest_type_read", "json"])
    if kind == "grep":
        cmd = f"grep -rn {key} src tests docs spikes"
        out = c.sh(cmd)[:BASH_CAP]
    elif kind == "grepc":
        cmd = f"grep -rn -C3 {key} src tests docs"
        out = c.sh(cmd)[:BASH_CAP]
    elif kind.startswith("pytest"):
        mode = "value" if kind == "pytest_value" else "type"
        out = c.logs.get(f"{key}.pytest_{mode}") or rng.choice([v for k, v in c.logs.items() if k.endswith(mode)])
        out = out[:READ_CAP if kind.endswith("read") else BASH_CAP]
        cmd = "pytest -vv --tb=long tests/"
    else:
        out = json_dump(c, key, old, rng.randrange(20, 300))[:BASH_CAP]
        cmd = "python -m json.tool results/runs.json"
    if not has(out, key):  # a log that never names the key: still a long turn, but not a match
        out += f"\n{key} = {old}\n"
    if quiet:  # the turn's prompt and reply do not name the key: only the command and output do
        return turn("check where it is used", [("Bash", {"command": cmd}, out)], "Here are the uses.")
    return turn(f"why is {key} {old}?", [("Bash", {"command": cmd}, out)],
                f"{key} is {old} for now; the logs above show where it is used.")


def adversarial_dump(c: Corpus, rng: random.Random, key: str, old: str, kind: str) -> list[dict[str, Any]]:
    """Reviewer A's dumps built to hold the key in many texts (second review of PR #216). The turn's prompt
    and reply do not name the key."""
    files = [p for p in c.files if has(c.file_text[p], key)] or [c.files[0]]
    if kind == "many_calls":  # the agent greps file by file: each call's command and output hold the key
        k = rng.choice([5, 10, 20])
        calls = []
        for j in range(k):
            p = files[j % len(files)]
            lines = [f"{i}:{line}" for i, line in enumerate(c.file_text[p].splitlines(), 1) if has(line, key)]
            calls.append(("Bash", {"command": f"grep -n {key} {p.relative_to(c.repo)}"},
                          "\n".join(lines) or f"{key} = {old}"))
        return turn("find every use", calls, "Here are the uses.")
    if kind == "split_output":  # one tool result as many text blocks (an MCP tool's chunks)
        out = c.sh(f"grep -rn -C3 {key} src tests docs")[:BASH_CAP] or f"{key} = {old}"
        lines = out.splitlines()
        size = rng.choice([5, 20, 50])
        blocks = [{"type": "text", "text": "\n".join(lines[i:i + size])} for i in range(0, len(lines), size)]
        tid = f"toolu_{rng.getrandbits(48):012x}"
        return [{"role": "user", "content": "find every use"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "mcp__search",
                                                   "input": {"pattern": key}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": blocks}]},
                {"role": "assistant", "content": [{"type": "text", "text": "Here are the uses."}]}]
    if kind == "edits":  # a refactor turn: one Edit per use, each old and new string naming the key
        k = rng.choice([3, 6, 12])
        calls = [("Edit", {"file_path": str(files[j % len(files)]), "old_string": f"{key} * x",
                           "new_string": f"{key} * y"},
                  f"The file has been updated. Here's the result of running `cat -n` on a snippet:\n  10\t{key} * y")
                 for j in range(k)]
        return turn("tidy the call sites", calls, "Done.")
    raise SystemExit(f"unknown adversarial dump {kind!r}")


def statement(c: Corpus, rng: random.Random, key: str, old: str, new: str) -> tuple[str, list[dict[str, Any]]]:
    """The turn that states the current value, and its kind."""
    kind = rng.choice(["short", "edit", "edit_then_tests", "long_output"])
    if kind == "short":
        return kind, turn("what is it now?", [], f"{key} is now {new}.")
    edit = ("Edit", {"file_path": "src/contexto/x.py", "old_string": f"{key} = {old}", "new_string": f"{key} = {new}"},
            f"The file src/contexto/x.py has been updated. Here's the result of running `cat -n` on a snippet:\n"
            f"    51\t\n    52\t{key} = {new}\n    53\t")
    if kind == "edit":
        return kind, turn(f"set {key} to {new}", [edit], f"Changed {key} from {old} to {new}.")
    _, log = rng.choice([(k, v) for k, v in c.logs.items() if not has(v, key)] or [("x", "")])
    tests = ("Bash", {"command": "pytest -q"}, log[:BASH_CAP])
    if kind == "edit_then_tests":
        return kind, turn(f"set {key} to {new} and run the tests", [edit, tests], f"{key} is now {new}; the suite passes.")
    p = rng.choice([p for p in c.files if not has(c.file_text[p], key)])
    return kind, turn("run the soak and raise it", [tests, ("Read", {"file_path": str(p)}, cat_n(c.file_text[p])[:READ_CAP])],
                      f"Soak test passed. {key} is now {new}")


def session(c: Corpus, rng: random.Random, key: str, quiet: bool, adv: str = ""):
    old, new = KEYS[key]
    turns_n = rng.choice([40, 150, 600])
    s_new = rng.randrange(int(turns_n * 0.5), int(turns_n * 0.95))
    m = rng.choice([0, 1, 2, 3])
    d = rng.choice([1, 2, 4])
    dump_at = sorted(rng.sample(range(1, s_new), d))
    after = sorted(rng.sample(range(s_new + 1, turns_n + 1), m))
    old_stmt = rng.random() < 0.5 and rng.randrange(1, s_new)
    turns, roles, kinds = [], {}, {}
    for t in range(1, turns_n + 1):
        if t in dump_at:
            tr = adversarial_dump(c, rng, key, old, adv) if adv else old_dump(c, rng, key, old, quiet)
            roles[t] = "dump"
        elif t == s_new:
            kinds[t], tr = statement(c, rng, key, old, new)
            roles[t] = "new"
        elif t in after:
            tr = turn("continue", [], rng.choice(PASSING).format(k=key))
            roles[t] = "passing"
        elif old_stmt and t == old_stmt:
            tr = turn(f"set {key} to {old}", [], f"Set {key} to {old}.")
            roles[t] = "old"
        else:
            tr = filler(c, rng, key)
        turns.append(tr)
    return turns, roles, kinds, {"T": turns_n, "m": m, "d": d}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True, help="a git checkout of this repository (see above)")
    parser.add_argument("--dumps", type=Path, required=True, help="scale_dumps_gen.sh's output directory")
    parser.add_argument("--trials", type=int, default=500)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--quiet", action="store_true", help="only the dump's command and output name the key")
    parser.add_argument("--adversarial", choices=ADVERSARIAL, default="",
                        help="reviewer A's dumps that hold the key in many texts (their turn is always quiet)")
    parser.add_argument("--d34-ref", default=scale_length.D34_REF)
    parser.add_argument("--variants-full", action="store_true",
                        help="rank only the grid cells that keep the stub fields repeating the prompt or paths")
    args = parser.parse_args(argv)
    corpus = Corpus(args.repo, args.dumps)
    d34 = scale_length.load_d34(args.d34_ref)
    chosen = VARIANTS_FULL if args.variants_full else VARIANTS
    rankers = {name: scale_length.ranker(d34, kind, b) for name, (kind, b) in chosen.items()}
    rng = random.Random(args.seed)
    agg: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for _ in range(args.trials):
        key = rng.choice(list(KEYS))
        turns, roles, kinds, meta = session(corpus, rng, key, args.quiet, args.adversarial)
        pages = [build_page(b"k" * 32, tr, t) for t, tr in enumerate(turns, 1)]
        entries = [scale_length.entry_with_ends(p) for p in pages]
        turn_of = {p["id"]: p["turn"] for p in pages}
        new_turn = next(t for t, r in roles.items() if r == "new")
        dumps = {t for t, r in roles.items() if r == "dump"}
        stale = dumps | {t for t, r in roles.items() if r == "old"}
        newest = max(t for t, r in roles.items() if r in ("new", "passing"))
        for name, rank in rankers.items():
            ranked = [turn_of[i] for i in rank(entries, key)]
            top = ranked[:SHOWN_IN_DETAIL]
            pos_new = ranked.index(new_turn)
            for bucket in ("all", f"passing mentions after {meta['m']}", f"statement {kinds[new_turn]}"):
                a = agg[(name, bucket)]
                a["n"] += 1
                a["shown"] += new_turn in top
                a["dump_above"] += any(ranked.index(t) < pos_new for t in dumps)
                a["stale_only"] += (new_turn not in top) and bool(stale & set(top))
                a["newest"] += newest in top
    buckets = ["all"] + [f"passing mentions after {m}" for m in range(4)] + \
        [f"statement {k}" for k in ("short", "edit", "edit_then_tests", "long_output")]
    shape = args.adversarial or ("quiet" if args.quiet else "loud")
    print(f"{args.trials} sessions, seed {args.seed}, {shape}")
    print("| variant | sessions | n | statement in detail | old dump above it | stale only | newest mention in detail |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for bucket in buckets:
        for name in rankers:
            a = agg[(name, bucket)]
            if a["n"]:
                print(f"| {name} | {bucket} | {a['n']} | {a['shown']} | {a['dump_above']} | {a['stale_only']} | {a['newest']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
