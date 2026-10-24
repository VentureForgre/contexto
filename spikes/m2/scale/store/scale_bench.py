"""Store-scale spike: search, recall, and index speed and search quality at 17 to 6,000 pages (zero spend).

    uv run python spikes/m2/scale/store/scale_bench.py            # or: .venv/bin/python ... (see README)

For each store size N it writes a fresh session store through ``SessionStore.page_out`` (the call
the engine makes when it pages turns) from synthetic Claude Code turns (``scale_synth``), then:

- size: bytes of the session directory, page count, page token estimates;
- speed: ``contexto_search`` (rare, common, and multi-word queries) and ``contexto_recall`` through
  a real ``contexto mcp --session S`` child over stdio, spawned like the launcher's ``--mcp-config``
  does, and in-process through ``contexto.mcp.Server``; ``index_text(4000)`` in-process (the engine
  calls it in the proxy's request path at a rotation); the MCP child's peak RSS (``wait4``). Each
  timing is the median of ``--reps`` calls, the pass is repeated ``--passes`` times, and the
  reported figure is the median of the pass medians;
- quality: every probe query of ``scale_probes`` through ``contexto_search``, recording whether the
  planted page is among the detailed hits, among the listed pages, or absent, whether the reply
  shows the planted value, and, for changing values, whether the latest value's page comes first;
- a cProfile of in-process search at ``--profile-n`` pages.

Before each request it publishes a turn room and a retained-window snapshot, as the engine does, so
search charges the room and checks the conversation in view, as in a live session. Outputs (numbers
only, no page text) go to ``--out``: ``stores.csv``, ``latency.csv`` (every call), ``quality.csv``
(every probe query), ``df.csv`` (query frequencies, real and synthetic), ``profile_<N>.txt``,
``env.json``, and ``summary.md`` (the README's tables). Stores are written under a temporary
``CONTEXTO_HOME`` and deleted at the end unless ``--keep``.
"""

from __future__ import annotations

import argparse
import cProfile
import csv
import io
import json
import os
import platform
import pstats
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

import scale_synth  # noqa: E402
from scale_probes import FACTS, LATENCY_QUERIES, all_queries, planted_turn  # noqa: E402

from contexto import mcp  # noqa: E402
from contexto.store import SessionStore  # noqa: E402
from contexto.store.pages import _texts, page_matches  # noqa: E402

SIZES = (17, 100, 500, 1000, 3000, 6000)
INDEX_BUDGET = 4_000  # rewrite.Config.index_budget
WINDOW, BYTES_PER_TOKEN, MODEL = 110_000, 2.1, "claude-sonnet-5"  # recall limit then binds at 32,000 chars
ROOM = 10**9  # a room that never runs out: the per-call limit binds, as in a turn with room left
SEARCH_CACHE_MB: str | None = None  # --search-cache-mb: the MCP server's search text budget (D39); None, its default
DETAILED = re.compile(r"^(pg_[0-9a-f]{16}) \(turn (\d+)\)$")
LISTED = re.compile(r"^(pg_[0-9a-f]{16}) \(turn (\d+)\) @")
NOT_SHOWN = re.compile(r"^\[(\d+) more matching pages not shown")
QUALITY_FIELDS = ["n", "dialect", "fact", "kind", "query", "outcome", "rank_shown", "matching_pages",
                  "query_on_planted_page", "value_shown", "casefold_on_page", "all_words_on_page", "latest_first",
                  "stale_only"]


# Store building


def build_store(home: Path, session: str, n: int, *, dialect: str, floor: float, seed: int,
                sizes: str = "spike") -> dict[str, Any]:
    """Write an ``n``-page store the way the engine pages turns; return its manifest and size figures."""
    store = SessionStore(home, session)
    store.set_window(WINDOW, BYTES_PER_TOKEN, model=MODEL)
    queries = all_queries()
    df = dict.fromkeys(queries, 0)
    ids, tokens, write_s = [], [], 0.0
    markers_ok = True
    for t in range(n):
        turn = scale_synth.session_turn(t, n, seed=seed, floor=floor, sizes=sizes)
        if dialect == "responses":
            turn = scale_synth.to_responses(turn)
        text = "\n".join(_texts(turn))
        for q in queries:
            if q in text:
                df[q] += 1
        for _, _, plant in scale_synth.plants_for(t, n):
            markers_ok &= plant.marker in text
        start = time.perf_counter()
        (page_id,) = store.page_out([turn], t)  # one turn per call; the engine batches, each page is built alike
        write_s += time.perf_counter() - start
        ids.append(page_id)
    pages = list(store.pages_dir.glob("pg_*.json"))
    for path in pages:
        tokens.append(json.loads(path.read_text(encoding="utf-8"))["tokens"])
    disk = sum(p.stat().st_size for p in store.dir.rglob("*") if p.is_file())
    blocks = sum(p.stat().st_blocks * 512 for p in store.dir.rglob("*") if p.is_file())
    return {"ids": ids, "df": {q: c / n for q, c in df.items()}, "markers_ok": markers_ok,
            "row": {"n": n, "dialect": dialect, "pages": len(pages), "bytes": disk, "disk_bytes": blocks,
                    "page_bytes_median": statistics.median(p.stat().st_size for p in pages),
                    "page_tokens_min": min(tokens), "page_tokens_median": statistics.median(tokens),
                    "page_tokens_max": max(tokens), "page_tokens_total": sum(tokens),
                    "write_s_total": round(write_s, 3), "write_ms_per_page": round(1000 * write_s / n, 3),
                    "markers_on_planted_pages": markers_ok}}


def publish_request(store: SessionStore, seq: int) -> None:
    """What the engine does per request before a tool call can run: snapshot the window in view, publish the room."""
    entries = [{"turn": 10**6, "message_index": 0, "role": "user", "text": "Continue with the next step."},
               {"turn": 10**6, "message_index": 1, "role": "assistant",
                "text": "\n".join(scale_synth.pools()["prose"][:20])}]
    store.set_retained(seq, 0, entries)
    store.set_turn_room(f"turn-{seq}", ROOM)


# MCP over stdio, as Claude Code runs it


class McpChild:
    """``python -m contexto mcp --session S`` with ``CONTEXTO_HOME`` set, as ``launcher.mcp_payload`` spawns it."""

    def __init__(self, home: Path, session: str) -> None:
        env = {**os.environ, "CONTEXTO_HOME": str(home), "PYTHONPATH": str(ROOT / "src")}
        env.pop("CONTEXTO_SEARCH_CACHE_MB", None)
        if SEARCH_CACHE_MB is not None:
            env["CONTEXTO_SEARCH_CACHE_MB"] = SEARCH_CACHE_MB
        start = time.perf_counter()
        self.proc = subprocess.Popen([sys.executable, "-m", "contexto", "mcp", "--session", session],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", env=env, bufsize=1)
        self.next_id = 0
        self._request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                     "clientInfo": {"name": "scale-bench", "version": "0"}})
        self.startup_s = time.perf_counter() - start
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"contexto mcp exited during {method} (code {self.proc.poll()})")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(f"contexto mcp error on {method}: {reply['error']}")
        return reply["result"]

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[float, str]:
        start = time.perf_counter()
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        elapsed = time.perf_counter() - start
        return elapsed, "\n".join(b["text"] for b in result["content"])

    def close(self) -> int:
        """End input (the shutdown signal), reap the child, return its peak RSS in bytes."""
        self.proc.stdin.close()
        _, status, usage = os.wait4(self.proc.pid, 0)
        self.proc.returncode = os.waitstatus_to_exitcode(status)
        self.proc.stdout.close()
        rss = usage.ru_maxrss
        return rss if sys.platform == "darwin" else rss * 1024  # macOS reports bytes, Linux KiB


class InProcess:
    def __init__(self, home: Path, session: str) -> None:
        self.store = SessionStore(home, session)
        if SEARCH_CACHE_MB is not None:
            from contexto.store.catalog import text_budget_bytes

            self.store.set_search_cache(text_budget_bytes(SEARCH_CACHE_MB))
        self.server = mcp.Server(self.store, io.StringIO(), io.StringIO())
        self.next_id = 0

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[float, str]:
        self.next_id += 1
        line = json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
        start = time.perf_counter()
        reply = self.server.handle_line(line)
        elapsed = time.perf_counter() - start
        return elapsed, "\n".join(b["text"] for b in reply["result"]["content"])


# Measurements


def latency_pass(home: Path, session: str, n: int, dialect: str, ids: list[str], pass_no: int, reps: int,
                 writer: csv.DictWriter) -> dict[str, Any]:
    store = SessionStore(home, session)
    recall_id = ids[len(ids) // 2]
    load = os.getloadavg()
    base = {"n": n, "dialect": dialect, "pass": pass_no, "load1": round(load[0], 2)}

    def row(mode: str, kind: str, query: str, rep: int, seconds: float, chars: int) -> None:
        writer.writerow({**base, "mode": mode, "kind": kind, "query": query, "rep": rep,
                         "seconds": f"{seconds:.6f}", "chars": chars})

    def run(mode: str, client: Any) -> None:
        for kind, queries in LATENCY_QUERIES.items():
            for q in queries:
                for rep in range(reps):
                    publish_request(store, 1000 * pass_no + rep)
                    seconds, text = client.call("contexto_search", {"query": q})
                    row(mode, kind, q, rep, seconds, len(text))
        for rep in range(reps):
            publish_request(store, 1000 * pass_no + rep)
            seconds, text = client.call("contexto_recall", {"page_id": recall_id})
            row(mode, "recall", recall_id, rep, seconds, len(text))

    child = McpChild(home, session)
    row("stdio", "startup", "initialize", 0, child.startup_s, 0)
    run("stdio", child)
    rss = child.close()
    writer.writerow({**base, "mode": "stdio", "kind": "peak_rss", "query": "", "rep": 0, "seconds": "", "chars": rss})
    run("inproc", InProcess(home, session))
    for rep in range(reps):
        start = time.perf_counter()
        text = store.index_text(INDEX_BUDGET)
        row("inproc", "index_text", str(INDEX_BUDGET), rep, time.perf_counter() - start, len(text))
    return {"rss": rss, "load": load}


def parse_reply(text: str) -> dict[str, Any]:
    detailed, listed, not_shown = [], [], 0
    for line in text.split("\n"):
        if m := DETAILED.match(line):
            detailed.append(m.group(1))
        elif m := LISTED.match(line):
            listed.append(m.group(1))
        elif m := NOT_SHOWN.match(line):
            not_shown = int(m.group(1))
    return {"detailed": detailed, "listed": listed, "total": len(detailed) + len(listed) + not_shown}


def detailed_section(text: str, page_id: str) -> str:
    """The lines a reply shows for one detailed hit (its header up to the next hit or the listed section)."""
    out, inside = [], False
    for line in text.split("\n"):
        if DETAILED.match(line) or line.startswith("[more matching pages"):
            inside = line.startswith(page_id)
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def quality(home: Path, session: str, n: int, dialect: str, ids: list[str], writer: csv.DictWriter) -> list[dict]:
    client = InProcess(home, session)
    rows = []
    for fact in FACTS:
        plant_ids = [ids[planted_turn(p.at, n)] for p in fact.plants]
        target, target_plant = plant_ids[-1], fact.plants[-1]
        for q in fact.queries:
            publish_request(client.store, 0)
            _, text = client.call("contexto_search", {"query": q})
            reply = parse_reply(text)
            page = client.store.get_page(target) or {}
            on_page = bool(page) and page_matches(page, q)  # the test search applies to each page
            folded = page_text(page).casefold() if page else ""
            casefold = q.casefold() in folded  # would a case-insensitive substring match find it
            all_words = all(w in folded for w in q.casefold().split())  # would an any-order word match
            if target in reply["detailed"]:
                outcome = "detailed"
            elif target in reply["listed"]:
                outcome = "listed"
            else:
                outcome = "absent_buried" if on_page else "absent_nomatch"
            rank = None
            order = reply["detailed"] + reply["listed"]
            if target in order:
                rank = order.index(target) + 1
            value_shown = outcome == "detailed" and target_plant.marker in detailed_section(text, target)
            first_plant = next((pid for pid in reply["detailed"] if pid in plant_ids), None)
            # an older value in detail while the latest value's page is not (D41)
            stale_only = first_plant is not None and target not in reply["detailed"]
            r = {"n": n, "dialect": dialect, "fact": fact.id, "kind": fact.kind, "query": q, "outcome": outcome,
                 "rank_shown": rank, "matching_pages": reply["total"], "query_on_planted_page": on_page,
                 "value_shown": value_shown, "casefold_on_page": casefold, "all_words_on_page": all_words,
                 "latest_first": (first_plant == target) if fact.kind == "changing" else "",
                 "stale_only": stale_only if fact.kind == "changing" else ""}
            writer.writerow(r)
            rows.append(r)
    return rows


def page_text(page: dict[str, Any]) -> str:
    """Everything search reads on a page: the stub fields and every text of the turn."""
    stub = page["stub"]
    return "\n".join([stub["user_text"], stub.get("system_text", ""), *stub["tools"], *stub["paths"],
                      *_texts(page["messages"])])


def profile(home: Path, session: str, n: int, out: Path, calls: int = 5) -> None:
    """cProfile of ``calls`` in-process searches per query (one rare, one common), aggregated."""
    client = InProcess(home, session)
    buf = io.StringIO()
    for q in ("E4127", "self"):
        prof = cProfile.Profile()
        seconds = []
        for i in range(calls):
            publish_request(client.store, i)
            prof.enable()
            elapsed, _ = client.call("contexto_search", {"query": q})
            prof.disable()
            seconds.append(elapsed)
        buf.write(f"=== contexto_search {q!r} at {n} pages, in-process, {calls} calls aggregated, "
                  f"median {statistics.median(seconds):.3f} s per call under the profiler ===\n")
        for key in ("tottime", "cumulative"):
            buf.write(f"--- top 25 by {key} ---\n")
            pstats.Stats(prof, stream=buf).strip_dirs().sort_stats(key).print_stats(25)
    out.write_text(re.sub(r"/[^\s]*/", "", buf.getvalue()), encoding="utf-8")


# Summary


def med(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def summarize(out: Path) -> str:
    lat = list(csv.DictReader((out / "latency.csv").open()))
    qual = list(csv.DictReader((out / "quality.csv").open()))
    stores = list(csv.DictReader((out / "stores.csv").open()))
    lines = ["## Store size", "",
             "| N | dialect | pages | MB on disk | page tokens min / median / max | total page tokens | write ms/page |",
             "|---:|---|---:|---:|---|---:|---:|"]
    for s in stores:
        lines.append(f"| {s['n']} | {s['dialect']} | {s['pages']} | {int(s['disk_bytes']) / 1e6:.1f} | "
                     f"{int(float(s['page_tokens_min'])):,} / {int(float(s['page_tokens_median'])):,} / "
                     f"{int(float(s['page_tokens_max'])):,} | {int(s['page_tokens_total']):,} | {s['write_ms_per_page']} |")

    def cell(n: str, d: str, mode: str, kind: str) -> float:
        by_pass: dict[str, list[float]] = {}
        for r in lat:
            if r["n"] == n and r["dialect"] == d and r["mode"] == mode and r["kind"] == kind and r["seconds"]:
                by_pass.setdefault(r["pass"], []).append(float(r["seconds"]))
        # median per query per pass, then the median over queries and passes of those
        per = []
        for p in by_pass:
            qs: dict[str, list[float]] = {}
            for r in lat:
                if (r["n"], r["dialect"], r["mode"], r["kind"], r["pass"]) == (n, d, mode, kind, p) and r["seconds"]:
                    qs.setdefault(r["query"], []).append(float(r["seconds"]))
            per += [med(v) for v in qs.values()]
        return med(per)

    keys = sorted({(r["n"], r["dialect"]) for r in lat}, key=lambda k: (k[1], int(k[0])))
    lines += ["", "## Latency (seconds; median of 5 calls per query, median over queries and 3 passes)", "",
              "| N | dialect | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall (stdio) | "
              "search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |",
              "|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|"]
    for n, d in keys:
        rss = [int(r["chars"]) for r in lat if r["n"] == n and r["dialect"] == d and r["kind"] == "peak_rss"]
        loads = sorted({r["load1"] for r in lat if r["n"] == n and r["dialect"] == d}, key=float)
        lines.append(
            f"| {n} | {d} | {cell(n, d, 'stdio', 'rare'):.3f} | {cell(n, d, 'stdio', 'common'):.3f} | "
            f"{cell(n, d, 'stdio', 'multi'):.3f} | {cell(n, d, 'stdio', 'recall'):.4f} | "
            f"{cell(n, d, 'inproc', 'rare'):.3f} / {cell(n, d, 'inproc', 'common'):.3f} / {cell(n, d, 'inproc', 'multi'):.3f} | "
            f"{cell(n, d, 'inproc', 'index_text'):.3f} | {cell(n, d, 'stdio', 'startup'):.3f} | "
            f"{med(rss) / 1e6:.0f} | {', '.join(loads)} |")

    qkeys = sorted({(r["n"], r["dialect"]) for r in qual}, key=lambda k: (k[1], int(k[0])))
    n_queries = sum(len(f.queries) for f in FACTS)
    n_changing = sum(len(f.queries) for f in FACTS if f.kind == "changing")
    lines += ["", f"## Search quality: per query ({n_queries} probe queries over {len(FACTS)} facts)", "",
              "| N | dialect | detailed (top 3) | listed (4-53) | absent: buried | absent: query not on page "
              "(of which case-insensitive would match / all words present) | value shown in reply | "
              f"changing: latest page first (of {n_changing}) | median matching pages |",
              "|---:|---|---:|---:|---:|---|---:|---:|---:|"]
    for n, d in qkeys:
        rs = [r for r in qual if r["n"] == n and r["dialect"] == d]
        c = {o: sum(r["outcome"] == o for r in rs) for o in ("detailed", "listed", "absent_buried", "absent_nomatch")}
        ch = [r for r in rs if r["kind"] == "changing"]
        miss = [r for r in rs if r["outcome"] == "absent_nomatch"]
        fold = sum(r["casefold_on_page"] == "True" for r in miss)
        words = sum(r["all_words_on_page"] == "True" for r in miss)
        lines.append(f"| {n} | {d} | {c['detailed']} | {c['listed']} | {c['absent_buried']} | "
                     f"{c['absent_nomatch']} ({fold} / {words}) | "
                     f"{sum(r['value_shown'] == 'True' for r in rs)} | {sum(r['latest_first'] == 'True' for r in ch)} | "
                     f"{med([int(r['matching_pages']) for r in rs]):.0f} |")
    lines += ["", "## Search quality: per fact, best of its 2-3 queries (30 facts)", "",
              "| N | dialect | detailed | listed only | absent | static (18) detailed | rename (4) detailed | "
              "changing (8) detailed | changing (8) latest first, any query |",
              "|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
    rank = {"detailed": 0, "listed": 1, "absent_buried": 2, "absent_nomatch": 2}
    for n, d in qkeys:
        rs = [r for r in qual if r["n"] == n and r["dialect"] == d]
        best: dict[str, int] = {}
        latest: dict[str, bool] = {}
        kinds = {}
        for r in rs:
            best[r["fact"]] = min(best.get(r["fact"], 2), rank[r["outcome"]])
            kinds[r["fact"]] = r["kind"]
            if r["kind"] == "changing":
                latest[r["fact"]] = latest.get(r["fact"], False) or r["latest_first"] == "True"
        det = [f for f, b in best.items() if b == 0]
        lines.append(f"| {n} | {d} | {len(det)} | {sum(b == 1 for b in best.values())} | "
                     f"{sum(b == 2 for b in best.values())} | {sum(kinds[f] == 'static' for f in det)} | "
                     f"{sum(kinds[f] == 'rename' for f in det)} | {sum(kinds[f] == 'changing' for f in det)} | "
                     f"{sum(latest.values())} |")
    return "\n".join(lines) + "\n"


# Main


def git_head() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", type=lambda s: [int(x) for x in s.split(",")], default=list(SIZES))
    parser.add_argument("--responses-sizes", type=lambda s: [int(x) for x in s.split(",") if x], default=[1000],
                        help="sizes also built as Codex (Responses API) stores; empty for none")
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--profile-n", type=int, default=3000)
    parser.add_argument("--floor", type=float, default=0.0, help="minimum per-page distractor rate per query")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--page-sizes", choices=("spike", "calibration"), default="spike",
                        help="spike: every turn 19,000 to 30,000 tokens; calibration: drawn from the real turn sizes (D41)")
    parser.add_argument("--out", type=Path, default=HERE / "results")
    parser.add_argument("--keep", action="store_true", help="keep the stores (printed path)")
    parser.add_argument("--search-cache-mb", default=None,
                        help="the MCP server's search text budget in MB (D39; default: the server's own)")
    args = parser.parse_args(argv)
    global SEARCH_CACHE_MB
    SEARCH_CACHE_MB = args.search_cache_mb
    args.out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="contexto-scale-"))
    env = {"commit": git_head(), "python": platform.python_version(), "machine": platform.machine(),
           "platform": platform.platform(), "cpus": os.cpu_count(), "floor": args.floor, "seed": args.seed,
           "page_sizes": args.page_sizes, "passes": args.passes, "reps": args.reps,
           "search_cache_mb": args.search_cache_mb, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "uptime_start": subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()}
    plan = [(n, "messages") for n in args.sizes] + [(n, "responses") for n in args.responses_sizes]
    real = scale_synth.calibration()
    df_rows: dict[str, dict[str, Any]] = {q: {"query": q, "real_df": real.get(q, 0.0)} for q in all_queries()}
    files = {name: (args.out / f"{name}.csv").open("w", newline="") for name in ("stores", "latency", "quality")}
    try:
        writers = {
            "stores": csv.DictWriter(files["stores"], ["n", "dialect", "pages", "bytes", "disk_bytes", "page_bytes_median",
                                                       "page_tokens_min", "page_tokens_median", "page_tokens_max",
                                                       "page_tokens_total", "write_s_total", "write_ms_per_page",
                                                       "markers_on_planted_pages"]),
            "latency": csv.DictWriter(files["latency"], ["n", "dialect", "pass", "load1", "mode", "kind", "query", "rep",
                                                         "seconds", "chars"]),
            "quality": csv.DictWriter(files["quality"], QUALITY_FIELDS),
        }
        for w in writers.values():
            w.writeheader()
        for n, dialect in plan:
            session = f"scale-{dialect}-{n}"
            print(f"[{time.strftime('%H:%M:%S')}] N={n} {dialect}: building", flush=True)
            built = build_store(home, session, n, dialect=dialect, floor=args.floor, seed=args.seed,
                                sizes=args.page_sizes)
            if not built["markers_ok"]:
                raise SystemExit(f"a planted marker is missing from its page at N={n} ({dialect})")
            writers["stores"].writerow(built["row"])
            if dialect == "messages":
                for q, v in built["df"].items():
                    df_rows[q][f"synthetic_df_{n}"] = round(v, 5)
            print(f"[{time.strftime('%H:%M:%S')}] N={n} {dialect}: quality", flush=True)
            quality(home, session, n, dialect, built["ids"], writers["quality"])
            for p in range(args.passes):
                print(f"[{time.strftime('%H:%M:%S')}] N={n} {dialect}: latency pass {p + 1}", flush=True)
                latency_pass(home, session, n, dialect, built["ids"], p + 1, args.reps, writers["latency"])
                for f in files.values():
                    f.flush()
            if n == args.profile_n and dialect == "messages":
                profile(home, session, n, args.out / f"profile_{n}.txt")
            if not args.keep:
                shutil.rmtree(home / "sessions" / session, ignore_errors=True)
    finally:
        for f in files.values():
            f.close()
    with (args.out / "df.csv").open("w", newline="") as f:
        cols = ["query", "real_df"] + [f"synthetic_df_{n}" for n in args.sizes]
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(df_rows.values())
    env["uptime_end"] = subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()
    (args.out / "env.json").write_text(json.dumps(env, indent=1) + "\n", encoding="utf-8")
    summary = summarize(args.out)
    (args.out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    if args.keep:
        print(f"stores kept under {home}")
    else:
        shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
