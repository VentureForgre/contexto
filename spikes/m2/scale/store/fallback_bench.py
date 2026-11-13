"""D65: recall, false-hit rate and latency of search on the probe set of ``fallback_probes`` (zero spend).

    uv run python spikes/m2/scale/store/fallback_bench.py --store-home /tmp/d65-store            # "before"
    uv run python spikes/m2/scale/store/fallback_bench.py --store-home /tmp/d65-store --label after \\
        --baseline-src ../contexto-main/src
    uv run python spikes/m2/scale/store/fallback_bench.py --compare results/d65/before results/d65/after
    uv run python spikes/m2/scale/store/fallback_bench.py --sizes 60 --out /tmp/d65-quick          # a quick check

It builds the 6,000-page store of this spike (``scale_bench.build_store``, seed 7, floor 0, with the six
identifier pages of ``fallback_probes.EXTRA_FACTS``) under a temporary ``CONTEXTO_HOME``, or under
``--store-home``, where a later run finds it again. ``~/.contexto`` is never used.

- Quality, in process (all page text held in memory, which changes speed only). The ranked pages of a probe are ``store.search(query)``. When that is empty and
  the store has ``search_approximate`` (the fallback D65 adds; ``main`` has none), they are the ids of
  ``store.search_approximate(query)``, best first. A probe counts at k when every expected page is in
  the top k. For ``none`` probes any page returned is a false hit.
- A cross-check through the tool: each probe through ``contexto.mcp.Server`` in process, after a
  retained window and a turn room are published as the engine does per request. Only the detailed and
  the listed page lines of the reply are read.
- Latency, through a real ``python -m contexto mcp --session S`` child over stdio: the first search of
  the process, then the median of ``--reps`` calls per query, in ``--passes`` passes, for the nine
  strict hits of ``scale_probes.LATENCY_QUERIES`` and three strict misses from each of five probe
  classes. With ``--baseline-src`` (the ``src`` directory of another checkout) a second child runs that
  code on the same store, and every query is timed against both in turn, so both see the same load.

Outputs (numbers and queries only, no page text) go to ``--out``/``--label``: ``quality.csv`` (one row per
probe), ``latency.csv`` (every call), ``env.json`` and ``summary.md``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shutil
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
    split_of,
)
from scale_probes import FACTS, LATENCY_QUERIES  # noqa: E402

from contexto.store import SessionStore  # noqa: E402

SEED, FLOOR = 7, 0.0
SIZES = (6000,)
MISS_CLASSES = ("identifier", "question", "typo", "extra_word", "none")  # strict misses timed, three each
FIRST_QUERY = LATENCY_QUERIES["rare"][0]  # the first search of each MCP child
QUALITY_FIELDS = ["n", "id", "class", "sub_kind", "split", "query", "expected", "source", "strict_pages", "returned",
                  "ranks", "top3", "top10", "any3", "any10", "altered", "altered_pages", "tool_detailed",
                  "tool_listed", "tool_pages"]
LATENCY_FIELDS = ["n", "pass", "load1", "side", "kind", "query", "rep", "seconds", "chars"]
SPLITS = ("tune", "held_out")


# The store


def session_name(n: int) -> str:
    return f"d65-{n}"


def corpus_id() -> str:
    """Changes when a plant, the seed or the floor does, so a store of another corpus is not reused."""
    return hashlib.sha256(repr((FACTS, EXTRA_FACTS, SEED, FLOOR)).encode()).hexdigest()[:16]


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


def tool_pages(text: str) -> tuple[list[str], list[str]]:
    """The page ids a ``contexto_search`` reply shows in detail and lists; every other line is ignored."""
    detailed, listed = [], []
    for line in text.split("\n"):
        if m := scale_bench.DETAILED.match(line):
            detailed.append(m.group(1))
        elif m := scale_bench.LISTED.match(line):
            listed.append(m.group(1))
    return detailed, listed


def altered_pages(store: Any, probe: Probe) -> Any:
    """Pages that hold the altered word as the query writes it (typo, identifier, extra_word), or the
    most pages any absent word of a ``none`` probe is on (0 unless the probe is wrong); strict search."""
    if probe.cls == "none":
        return max(len(store.search(w)) for w in probe.absent)
    return len(store.search(probe.altered)) if probe.altered else ""


def quality(home: Path, n: int, ids: list[str], writer: csv.DictWriter) -> None:
    client = scale_bench.InProcess(home, session_name(n))
    store = client.store
    # Hold every page's text: no result depends on the budget (D39), and the default 64 MB reads three
    # quarters of a 6,000-page store again on every search. The latency children keep the default.
    store.set_search_cache(None)
    for probe in PROBES:
        expected = expected_pages(probe, ids)
        found, source = ranked(store, probe.query)
        scale_bench.publish_request(store, 0)
        _, reply = client.call("contexto_search", {"query": probe.query})
        detailed, listed = tool_pages(reply)
        writer.writerow({
            "n": n, "id": probe.id, "class": probe.cls, "sub_kind": probe.sub_kind, "split": split_of(probe),
            "query": probe.query, "expected": ";".join(f"{fact}:{plant}" for fact, plant in probe.expected),
            "source": source, "strict_pages": len(found) if source == "strict" else 0, "returned": len(found),
            **score(expected, found),
            "altered": probe.altered, "altered_pages": altered_pages(store, probe),
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
    """One pass: a new child per side, its first search, then every query ``reps`` times against each side
    in turn. The side that goes first alternates by call and by pass."""
    session = session_name(n)
    store = SessionStore(home, session)
    base = {"n": n, "pass": pass_no, "load1": round(os.getloadavg()[0], 2)}

    def row(side: str, kind: str, query: str, rep: int, seconds: Any, chars: int) -> None:
        writer.writerow({**base, "side": side, "kind": kind, "query": query, "rep": rep,
                         "seconds": f"{seconds:.6f}" if seconds != "" else "", "chars": chars})

    def timed(side: str, kind: str, query: str, rep: int) -> None:
        scale_bench.publish_request(store, 1000 * pass_no + rep)
        seconds, text = clients[side].call("contexto_search", {"query": query})
        row(side, kind, query, rep, seconds, len(text))

    names = list(sides) if pass_no % 2 else list(sides)[::-1]
    clients: dict[str, Any] = {}
    try:
        for side in names:
            clients[side] = sides[side](home, session)
            row(side, "startup", "initialize", 0, clients[side].startup_s, 0)
        for side in names:
            timed(side, "first", FIRST_QUERY, 0)
        for kind, query in queries:
            for rep in range(reps):
                for side in names if rep % 2 == 0 else names[::-1]:
                    timed(side, kind, query, rep)
    finally:
        for side, client in clients.items():
            row(side, "peak_rss", "", 0, "", client.close())


# Summary


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _yes(rows: list[dict[str, str]], field: str) -> int:
    return sum(r[field] == "True" for r in rows)


def _of(rows: list[dict[str, str]], field: str, percent: bool = False) -> str:
    hit, total = _yes(rows, field), len(rows)
    return f"{hit}/{total} ({100 * hit / total:.0f}%)" if percent and total else f"{hit}/{total}"


def _halves(rows: list[dict[str, str]], field: str) -> str:
    """``tune | held-out | all`` cells for one yes/no field."""
    tune, held = ([r for r in rows if r["split"] == s] for s in SPLITS)
    return f"{_of(tune, field)} | {_of(held, field)} | {_of(rows, field, percent=True)}"


def _false_hits(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """``none`` rows with a yes/no ``hit`` field: any page returned."""
    return [{**r, "hit": str(int(r["returned"]) > 0)} for r in rows]


def _cell(rows: list[dict[str, str]], side: str, kind: str) -> float | None:
    """Seconds: the median of the calls of each query in each pass, then the median of those."""
    calls: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        if r["side"] == side and r["kind"] == kind and r["seconds"]:
            calls.setdefault((r["pass"], r["query"]), []).append(float(r["seconds"]))
    return statistics.median(statistics.median(v) for v in calls.values()) if calls else None


def _kinds(rows: list[dict[str, str]]) -> list[str]:
    seen = dict.fromkeys(r["kind"] for r in rows if r["kind"] not in ("startup", "peak_rss"))
    return sorted(seen, key=lambda k: (k != "first", list(seen).index(k)))  # the first search first


def quality_tables(rows: list[dict[str, str]], n: str) -> list[str]:
    lines = [f"## Recall at {int(n):,} pages", "",
             "A probe counts when every expected page is in the top 3 (top 10) of the ranked pages.", "",
             "| class | probes | top 3: tune | held-out | all | top 10: tune | held-out | all | "
             "answered by strict / fallback / nothing |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for cls in CLASSES:
        rs = [r for r in rows if r["class"] == cls]
        if cls == "none" or not rs:
            continue
        by = " / ".join(str(sum(r["source"] == s for r in rs)) for s in ("strict", "fallback", "none"))
        lines.append(f"| {cls} | {len(rs)} | {_halves(rs, 'top3')} | {_halves(rs, 'top10')} | {by} |")
        if cls == "two_page":
            lines.append(f"| two_page, either page | {len(rs)} | {_halves(rs, 'any3')} | {_halves(rs, 'any10')} | |")
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

    def some(classes: tuple[str, ...]) -> list[dict[str, str]]:
        return [r for r in rows if r["class"] in classes]

    def strict_hit(r: dict[str, str]) -> bool:  # strict search returned an expected page
        return r["source"] == "strict" and any(c.isdigit() for c in r["ranks"])

    exact, altered = some(("exact",)), some(("typo", "identifier", "extra_word", "two_page"))
    reworded = some(("paraphrase", "question"))
    answered = [r for r in reworded if r["source"] == "strict"]
    lines += ["", f"## Probe checks at {int(n):,} pages", "",
              f"- exact probes whose page strict search returns: {sum(map(strict_hit, exact))} of {len(exact)}.",
              "- typo, identifier, extra_word and two_page probes for which strict search returns an expected page: "
              f"{sum(map(strict_hit, altered))} of {len(altered)} (0 is right).",
              f"- none probes with an absent word on some page: {sum(int(r['altered_pages']) > 0 for r in none)} of "
              f"{len(none)} (0 is right).",
              f"- paraphrase and question probes that strict search answers, so a fallback never runs: "
              f"{len(answered)} of {len(reworded)}; the expected page is among the pages returned for "
              f"{sum(map(strict_hit, answered))} of them."]
    return lines


def latency_tables(rows: list[dict[str, str]], n: str) -> list[str]:
    sides = list(dict.fromkeys(r["side"] for r in rows))
    both = "baseline" in sides and "tree" in sides
    head = "| kind | queries | " + " | ".join(f"{s}, s" for s in sides) + (" | tree / baseline |" if both else " |")
    lines = [f"## Latency at {int(n):,} pages", "",
             "Seconds through an MCP child over stdio: the median of the calls per query per pass, then the median "
             "over queries and passes. `first` is the first search of a process. `miss_*` are queries strict "
             "search finds nothing for.", "", head, "|---|---:|" + "---:|" * (len(sides) + both)]
    for kind in _kinds(rows):
        cells = [_cell(rows, side, kind) for side in sides]
        queries = len({r["query"] for r in rows if r["kind"] == kind})
        ratio = ""
        if both and all(cells) and cells[sides.index("baseline")]:
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
    return lines


def summarize(folder: Path) -> str:
    """The tables of one result folder, from whichever of ``quality.csv`` and ``latency.csv`` it holds."""
    quality_rows, latency_rows = read_rows(folder / "quality.csv"), read_rows(folder / "latency.csv")
    lines = [f"# D65 probes: {label_of(folder)}", ""]
    try:
        env = json.loads((folder / "env.json").read_text(encoding="utf-8"))
        approx = "has" if env["search_approximate"] else "has no"
        lines += [f"Commit {env['commit'][:8]}, Python {env['python']}, {env['os']}, {env['cpu']}. "
                  f"{env['probes']} probes; the store {approx} `search_approximate`.", ""]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for n in dict.fromkeys(r["n"] for r in quality_rows):
        lines += [*quality_tables([r for r in quality_rows if r["n"] == n], n), ""]
    for n in dict.fromkeys(r["n"] for r in latency_rows):
        lines += [*latency_tables([r for r in latency_rows if r["n"] == n], n), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def label_of(folder: Path) -> str:
    try:
        return str(json.loads((folder / "env.json").read_text(encoding="utf-8"))["label"])
    except (OSError, ValueError, KeyError, TypeError):
        return folder.name


def measures(folder: Path) -> dict[str, str]:
    """Every figure of one result folder as ``{measure: value}``, in table order (for ``--compare``)."""
    out: dict[str, str] = {}
    quality_rows, latency_rows = read_rows(folder / "quality.csv"), read_rows(folder / "latency.csv")
    sizes = list(dict.fromkeys(r["n"] for r in (*quality_rows, *latency_rows)))
    for n in sizes:
        at = f"{int(n):,} pages, " if len(sizes) > 1 else ""
        rows = [r for r in quality_rows if r["n"] == n]
        for cls in CLASSES:
            rs = [r for r in rows if r["class"] == cls]
            if cls == "none" or not rs:
                continue
            for name, part in (("tune", [r for r in rs if r["split"] == "tune"]),
                               ("held-out", [r for r in rs if r["split"] == "held_out"]), ("all", rs)):
                out[f"{at}{cls}, {name} ({len(part)}): top 3 / top 10"] = f"{_yes(part, 'top3')} / {_yes(part, 'top10')}"
            if cls == "two_page":
                out[f"{at}two_page, all ({len(rs)}), either page: top 3 / top 10"] = \
                    f"{_yes(rs, 'any3')} / {_yes(rs, 'any10')}"
        none = _false_hits([r for r in rows if r["class"] == "none"])
        for kind in ("absent", "mixed", "all"):
            rs = [r for r in none if kind in ("all", r["sub_kind"])]
            if rs:
                out[f"{at}none, {kind} ({len(rs)}): probes with a page"] = _of(rs, "hit", percent=True)
        for name, part in (("tune", [r for r in none if r["split"] == "tune"]),
                           ("held-out", [r for r in none if r["split"] == "held_out"])):
            if part:
                out[f"{at}none, {name} ({len(part)}): probes with a page"] = _of(part, "hit", percent=True)
        if none:
            out[f"{at}none: median pages returned"] = f"{statistics.median(int(r['returned']) for r in none):g}"
        if rows:
            out[f"{at}answered by strict / fallback / nothing ({len(rows)})"] = " / ".join(
                str(sum(r["source"] == s for r in rows)) for s in ("strict", "fallback", "none"))
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
    """Two result folders side by side, as one markdown table."""
    a, b = measures(first), measures(second)
    lines = [f"| measure | {label_of(first)} | {label_of(second)} |", "|---|---|---|"]
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


def environment(args: argparse.Namespace) -> dict[str, Any]:
    """What ran and on what: versions and numbers only (no host name, no path)."""
    diff = git(ROOT, "diff", "--quiet", "HEAD", "--", "src")
    mac = platform.mac_ver()[0]
    env = {"label": args.label, "commit": scale_bench.git_head(),
           "src_matches_commit": diff is not None and diff.returncode == 0,
           "python": platform.python_version(), "os": f"macOS {mac}" if mac else f"{platform.system()} {platform.release()}",
           "cpu": cpu_model(), "cpus": os.cpu_count(), "sizes": args.sizes, "passes": args.passes, "reps": args.reps,
           "probes": len(PROBES), "corpus": "default plants and fallback_probes.EXTRA_FACTS, seed 7, floor 0",
           "search_approximate": hasattr(SessionStore, "search_approximate"),
           "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "load_start": [round(x, 2) for x in os.getloadavg()]}
    if args.baseline_src is not None:
        head = git(args.baseline_src, "rev-parse", "HEAD")
        env["baseline_commit"] = head.stdout.strip() if head is not None and head.returncode == 0 else "unknown"
    return env


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
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"),
                        help="print two result folders side by side and exit")
    args = parser.parse_args(argv)
    if args.compare:
        print(compare(*args.compare), end="")
        return 0
    if args.quality_only and args.latency_only:
        parser.error("--quality-only and --latency-only exclude each other")
    if args.store_home is not None and args.store_home.resolve() == (Path.home() / ".contexto").resolve():
        raise SystemExit("--store-home must not be ~/.contexto: the bench writes its own sessions there")
    sides: dict[str, Callable[[Path, str], Any]] = {"tree": scale_bench.McpChild}
    if args.baseline_src is not None:
        src = args.baseline_src.resolve()
        if not (src / "contexto" / "__init__.py").is_file():
            raise SystemExit(f"--baseline-src {args.baseline_src} holds no contexto package")
        sides["baseline"] = lambda home, session: scale_bench.McpChild(home, session, src=src)
    folder = args.out / args.label
    folder.mkdir(parents=True, exist_ok=True)
    home = args.store_home or Path(tempfile.mkdtemp(prefix="contexto-d65-"))
    home.mkdir(parents=True, exist_ok=True)
    env = environment(args)
    files = {}
    try:
        if not args.latency_only:
            files["quality"] = (folder / "quality.csv").open("w", newline="", encoding="utf-8")
            quality_writer = csv.DictWriter(files["quality"], QUALITY_FIELDS)
            quality_writer.writeheader()
        if not args.quality_only:
            files["latency"] = (folder / "latency.csv").open("w", newline="", encoding="utf-8")
            latency_writer = csv.DictWriter(files["latency"], LATENCY_FIELDS)
            latency_writer.writeheader()
        for n in args.sizes:
            ids = open_store(home, n)
            if not args.latency_only:
                print(f"[{time.strftime('%H:%M:%S')}] N={n}: quality, {len(PROBES)} probes", flush=True)
                quality(home, n, ids, quality_writer)
            if not args.quality_only:
                chooser = SessionStore(home, session_name(n))
                chooser.set_search_cache(None)  # as in the quality pass: speed only
                queries = latency_queries(chooser)
                for p in range(1, args.passes + 1):
                    print(f"[{time.strftime('%H:%M:%S')}] N={n}: latency pass {p}", flush=True)
                    latency_pass(home, n, p, args.reps, queries, latency_writer, sides)
                    files["latency"].flush()
    finally:
        for f in files.values():
            f.close()
        if args.store_home is None:
            shutil.rmtree(home, ignore_errors=True)
    env["load_end"] = [round(x, 2) for x in os.getloadavg()]
    (folder / "env.json").write_text(json.dumps(env, indent=1) + "\n", encoding="utf-8")
    summary = summarize(folder)
    (folder / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
