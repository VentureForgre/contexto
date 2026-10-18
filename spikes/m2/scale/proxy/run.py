"""MEASUREMENT (zero spend): drive the real Contexto proxy with a synthetic Claude Code session that grows
to tens of millions of tokens of history, against a local fake upstream. See README.md.

    .venv/bin/python spikes/m2/scale/proxy/run.py --label first

Every model call goes to ``upstream.FakeUpstream`` on 127.0.0.1; the proxy is started with that URL only.
The session store lives in a fresh temp CONTEXTO_HOME under /tmp, removed at the end unless --keep-home.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import pstats
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scale.proxy import synth  # noqa: E402
from scale.proxy.upstream import FakeUpstream  # noqa: E402

BREAK_EVENTS = ("over_bound", "bound_too_small", "transform_error", "proxy_error", "fallback_failed",
                "degraded_enter", "store_error", "page_conflict", "observe_usage_error")
COUNTED_EVENTS = ("rotation", "rotation_noop", "page_out", "window_minimal", "bound_too_small", "over_bound",
                  "carried_over_budget", "carried_paged", "turn_room_low", "estimate_error", "usage_missing",
                  "usage_unattributed", "resync", "transform_error", "proxy_error", "degraded_enter", "store_error",
                  "fallback_failed", "retained_stale")
REQUEST_FIELDS = ("label", "checkpoint_m", "phase", "round", "turn", "kind", "req_in_turn", "history_tokens",
                  "body_mb", "upstream_bytes", "upstream_tokens", "upstream_messages", "status", "e2e_s", "ttfb_s",
                  "pre_forward_s", "read_body_s", "proxy_transform_s", "engine_s", "observe_s", "index_text_s",
                  "index_text_n", "pages_read_s", "page_out_s", "set_retained_s", "set_epoch_s", "rotated",
                  "rotation_reason", "events", "proxy_maxrss_mb", "load1")
CHECKPOINT_FIELDS = ("label", "checkpoint_m", "turn", "history_tokens", "reached_by", "ff_turns", "ff_seconds",
                     "pages", "pages_mb", "store_mb", "log_mb", "proxy_idle_rss_mb", "proxy_maxrss_mb",
                     "max_upstream_tokens", "ff_turn_peak_max", "ff_turns_over_bound", "uptime", *(f"n_{e}" for e in COUNTED_EVENTS))


def load_avg() -> float:
    return round(os.getloadavg()[0], 2)


def uptime() -> str:
    try:
        return subprocess.run(["uptime"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def ps_rss_mb(pid: int) -> float | None:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
        return round(int(out.strip()) / 1024, 1) if out.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def dir_mb(path: Path) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return round(total / 1e6, 2)


def log_events(log_path: Path) -> tuple[dict[str, int], int]:
    """Counts of engine and proxy events in the session log, and the largest upstream input logged."""
    counts = {e: 0 for e in COUNTED_EVENTS}
    max_tokens = 0
    if not log_path.exists():
        return counts, 0
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            name = entry.get("event")
            if name in counts:
                counts[name] += 1
            if name == "request":
                for inner in [*(entry.get("transform_events") or []), *(entry.get("usage_events") or [])]:
                    if isinstance(inner, dict) and inner.get("event") in counts:
                        counts[inner["event"]] += 1
                usage = entry.get("usage") or {}
                max_tokens = max(max_tokens, sum(usage.get(k) or 0 for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
    return counts, max_tokens


class Worker:
    """A ``worker.py serve`` process: the proxy, with a JSON-lines control channel."""

    def __init__(self, args: argparse.Namespace, home: Path, engine_pkl: Path, upstream_port: int, err) -> None:
        cmd = [sys.executable, str(HERE / "worker.py"), "serve", "--home", str(home), "--bound", str(args.bound),
               "--upstream", f"http://127.0.0.1:{upstream_port}"]
        if engine_pkl.exists():
            cmd += ["--engine-in", str(engine_pkl)]
        self.engine_pkl = engine_pkl
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True,
                                     env={**os.environ, "CONTEXTO_HOME": str(home)})
        ready = self._read()
        self.port, self.pid = ready["port"], ready["pid"]

    def _read(self) -> dict[str, Any]:
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"proxy worker exited (code {self.proc.poll()}); see the worker log")
        return json.loads(line)

    def call(self, cmd: dict[str, Any]) -> dict[str, Any]:
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()
        return self._read()

    def close(self) -> float:
        out = self.call({"cmd": "exit", "engine_out": str(self.engine_pkl)})
        self.proc.wait(timeout=120)
        return out.get("maxrss_mb", 0.0)


class Session:
    """The CLI side: the synthetic history, sent as full-history requests the way Claude Code sends them."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.shape = synth.Shape(head_tokens=args.head_tokens, turn_tokens=args.turn_tokens,
                                 reminders=args.reminders, seed=args.seed)
        self.head = synth.head(self.shape)
        self.wire = synth.WireBuilder(self.head)
        self.turn = 0
        self.pending: list[dict[str, Any]] | None = None  # the next turn's messages, generated once

    def next_messages(self) -> list[dict[str, Any]]:
        if self.pending is None:
            self.pending = synth.turn(self.shape, self.turn)
        return self.pending

    def turn_start_tokens(self) -> int:
        """History tokens of the next turn's turn-start request (what plain Claude Code would send)."""
        messages = self.next_messages()
        first = synth.request_points(self.shape, self.turn, messages)[0][0]
        tail = sum(len(synth.serialize(m)) + 1 for m in messages[:first])
        return synth.tokens_of(len(self.wire.prefix) + self.wire.history_bytes + tail + len(self.wire.suffix))

    def turn_reaching(self, target: int) -> int:
        """The first turn (from the current one) whose turn-start request carries at least ``target`` tokens."""
        if self.turn_start_tokens() >= target:
            return self.turn
        fixed = len(self.wire.prefix) + len(self.wire.suffix)
        history = self.wire.history_bytes + sum(len(synth.serialize(m)) + 1 for m in self.next_messages())
        t = self.turn + 1
        while True:
            messages = synth.turn(self.shape, t)
            first = synth.request_points(self.shape, t, messages)[0][0]
            tail = sum(len(synth.serialize(m)) + 1 for m in messages[:first])
            if synth.tokens_of(fixed + history + tail) >= target:
                return t
            history += sum(len(synth.serialize(m)) + 1 for m in messages)
            t += 1

    def finish_turn(self) -> None:
        self.wire.extend(self.next_messages())
        self.turn += 1
        self.pending = None


def send(port: int, body: bytes, request_id: str, reply: str) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01",
               "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14",
               "x-api-key": "sk-ant-scale-harness-not-a-key", "User-Agent": "contexto-scale-harness",
               "x-scale-id": request_id, "x-scale-reply": reply}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=900)
    t0 = time.monotonic()
    try:
        conn.request("POST", "/v1/messages", body=body, headers=headers)
        response = conn.getresponse()
        t_headers = time.monotonic()
        data = response.read()
        t_end = time.monotonic()
        status = response.status
    finally:
        conn.close()
    ok = status == 200 and b"message_stop" in data
    return {"t0": t0, "status": status if ok or status != 200 else -1, "ttfb_s": t_headers - t0, "e2e_s": t_end - t0}


def run_turn(session: Session, worker: Worker, upstream: FakeUpstream, rows: list[dict[str, Any]],
             base: dict[str, Any]) -> None:
    """Send every request of the next turn (turn start, then one per tool_result) and finish the turn."""
    messages = session.next_messages()
    for j, (end, reply) in enumerate(synth.request_points(session.shape, session.turn, messages)):
        body = session.wire.body(messages[:end])
        request_id = f"t{session.turn}r{j}"
        result = send(worker.port, body, request_id, reply)
        up = upstream.take(request_id) or {}
        rows.append({**base, "turn": session.turn, "kind": "turn_start" if j == 0 else "tool_loop", "req_in_turn": j,
                     "history_tokens": synth.tokens_of(len(body)), "body_mb": round(len(body) / 1e6, 3),
                     "upstream_bytes": up.get("bytes"), "upstream_tokens": up.get("tokens"),
                     "upstream_messages": up.get("messages"), "status": result["status"],
                     "e2e_s": round(result["e2e_s"], 4), "ttfb_s": round(result["ttfb_s"], 4),
                     "pre_forward_s": round(up["t_recv"] - result["t0"], 4) if up else None,
                     "load1": load_avg(), "_id": request_id})
    session.finish_turn()


def merge_stats(rows: list[dict[str, Any]], records: list[dict[str, Any]], maxrss: float) -> None:
    by_id = {r["id"]: r for r in records}
    for row in rows:
        rec = by_id.get(row.pop("_id", None) or "")
        if rec is None:
            continue
        events = [*rec.get("events", []), *rec.get("usage_events", [])]
        rotation = next((e for e in events if e["event"] == "rotation"), {})
        names = sorted({e["event"] for e in events if e["event"] in COUNTED_EVENTS})
        row.update({"read_body_s": round(rec.get("read_body", 0), 4),
                    "proxy_transform_s": round(rec.get("proxy_transform", 0), 4),
                    "engine_s": round(rec.get("engine", 0), 4), "observe_s": round(rec.get("observe", 0), 4),
                    "index_text_s": round(rec.get("store_index_text", 0), 4),
                    "index_text_n": rec.get("store_index_text_n", 0),
                    "pages_read_s": round(rec.get("store_pages", 0), 4),
                    "page_out_s": round(rec.get("store_page_out", 0), 4),
                    "set_retained_s": round(rec.get("store_set_retained", 0), 4),
                    "set_epoch_s": round(rec.get("store_set_epoch", 0), 4),
                    "rotated": int(bool(rotation)), "rotation_reason": rotation.get("reason", ""),
                    "events": " ".join(names), "proxy_maxrss_mb": round(maxrss, 1)})


def fast_forward(args: argparse.Namespace, session: Session, home: Path, engine_pkl: Path, to_turn: int,
                 ff_csv: Path, err) -> float:
    """Advance the engine through turn starts session.turn .. to_turn - 1 in a ``worker.py ff`` process."""
    shape = {"head_tokens": args.head_tokens, "turn_tokens": args.turn_tokens, "reminders": args.reminders,
             "seed": args.seed}
    cmd = [sys.executable, str(HERE / "worker.py"), "ff", "--home", str(home), "--bound", str(args.bound),
           "--shape", json.dumps(shape), "--from-turn", str(session.turn), "--to-turn", str(to_turn),
           "--engine-out", str(engine_pkl), "--csv-out", str(ff_csv)]
    if engine_pkl.exists():
        cmd += ["--engine-in", str(engine_pkl)]
    started = time.monotonic()
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                         env={**os.environ, "CONTEXTO_HOME": str(home)})
    if out.returncode != 0:
        raise RuntimeError(f"fast-forward worker failed (code {out.returncode}); see the worker log")
    while session.turn < to_turn:
        session.next_messages()
        session.finish_turn()
    return time.monotonic() - started


def write_profile(prof_path: Path, out_txt: Path, label: str) -> None:
    stream = open(out_txt, "w")
    stats = pstats.Stats(str(prof_path), stream=stream)
    stats.strip_dirs().sort_stats("cumulative")
    print(f"# cProfile of the proxy's _transform and observe_usage, run {label}; sorted by cumulative time",
          file=stream)
    stats.print_stats(40)
    stats.sort_stats("tottime")
    print("# sorted by own time", file=stream)
    stats.print_stats(25)
    stream.close()


def checkpoint_measure(args, session, upstream, home, engine_pkl, checkpoint, rows, err, prof_dir) -> dict[str, Any]:
    worker = Worker(args, home, engine_pkl, upstream.upstream_port, err)
    idle = ps_rss_mb(worker.pid)
    try:
        for r in range(args.rounds):
            round_rows: list[dict[str, Any]] = []
            base = {"label": args.label, "checkpoint_m": checkpoint, "phase": "measure", "round": r + 1}
            for _ in range(args.turns_per_round):
                run_turn(session, worker, upstream, round_rows, base)
            stats = worker.call({"cmd": "stats"})
            merge_stats(round_rows, stats["records"], stats["maxrss_mb"])
            rows.extend(round_rows)
            print(f"  round {r + 1}: {len(round_rows)} requests, last e2e {round_rows[-1]['e2e_s']}s, "
                  f"maxrss {stats['maxrss_mb']:.0f} MB", file=sys.stderr, flush=True)
        if args.profile_at and abs(checkpoint - args.profile_at) < 1e-9:
            prof = prof_dir / f"profile_{args.label}_{checkpoint:g}M.prof"
            worker.call({"cmd": "profile_on"})
            prof_rows: list[dict[str, Any]] = []
            base = {"label": args.label, "checkpoint_m": checkpoint, "phase": "profile", "round": 0}
            for _ in range(args.turns_per_round):
                run_turn(session, worker, upstream, prof_rows, base)
            worker.call({"cmd": "profile_off", "path": str(prof)})
            stats = worker.call({"cmd": "stats"})
            merge_stats(prof_rows, stats["records"], stats["maxrss_mb"])
            rows.extend(prof_rows)
            write_profile(prof, HERE / "results" / f"profile_{args.label}_{checkpoint:g}M.txt", args.label)
            prof.unlink(missing_ok=True)
    finally:
        maxrss = worker.close()
    return {"proxy_idle_rss_mb": idle, "proxy_maxrss_mb": round(maxrss, 1)}


def drive_http(args, session, upstream, home, engine_pkl, to_turn, checkpoint, rows, err) -> None:
    """Reach ``to_turn`` with every request over HTTP (the faithful path; used for small checkpoints)."""
    worker = Worker(args, home, engine_pkl, upstream.upstream_port, err)
    try:
        while session.turn < to_turn:
            drive_rows: list[dict[str, Any]] = []
            run_turn(session, worker, upstream, drive_rows,
                     {"label": args.label, "checkpoint_m": checkpoint, "phase": "drive", "round": 0})
            stats = worker.call({"cmd": "stats"})
            merge_stats(drive_rows, stats["records"], stats["maxrss_mb"])
            rows.extend(drive_rows)
    finally:
        worker.close()


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def ff_peaks(ff_csv: Path, first_turn: int, to_turn: int, bound: int) -> tuple[int, int]:
    """Fast-forwarded turns first_turn .. to_turn - 1: the largest projected last request of a turn
    (``turn_peak_tokens``) and how many turns have it over the bound."""
    if not ff_csv.exists():
        return 0, 0
    with open(ff_csv, newline="") as f:
        peaks = [int(r["turn_peak_tokens"]) for r in csv.DictReader(f) if first_turn <= int(r["turn"]) < to_turn]
    return max(peaks, default=0), sum(1 for p in peaks if p > bound)


def broken(rows: list[dict[str, Any]], counts: dict[str, int], bound: int, ff_over: int = 0) -> list[str]:
    reasons = [f"{e}: {counts[e]}" for e in BREAK_EVENTS if counts.get(e)]
    if ff_over:
        reasons.append(f"fast-forwarded turns whose last request is over the bound: {ff_over}")
    bad = [r for r in rows if r.get("status") != 200]
    if bad:
        reasons.append(f"non-200 responses: {len(bad)}")
    over = [r for r in rows if (r.get("upstream_tokens") or 0) > bound]
    if over:
        reasons.append(f"upstream requests over the bound: {len(over)} (max {max(r['upstream_tokens'] for r in over):,})")
    return reasons


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", default="first", help="names the output files")
    parser.add_argument("--checkpoints", default="0.5,2,5,10,30", help="history sizes, millions of tokens")
    parser.add_argument("--rounds", type=int, default=3, help="measurement rounds per checkpoint")
    parser.add_argument("--turns-per-round", type=int, default=2)
    parser.add_argument("--profile-at", type=float, default=10.0, help="checkpoint to profile (0: none)")
    parser.add_argument("--http-until", type=float, default=0.5,
                        help="reach checkpoints up to this size with every request over HTTP")
    parser.add_argument("--reminders", choices=("first", "mixed"), default="first")
    parser.add_argument("--head-tokens", type=int, default=50_000)
    parser.add_argument("--turn-tokens", type=int, default=26_000)
    parser.add_argument("--bound", type=int, default=150_000)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--keep-going", action="store_true", help="continue past a failure")
    parser.add_argument("--keep-home", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    checkpoints = [float(c) for c in args.checkpoints.split(",")]
    results = HERE / "results"
    results.mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="contexto-scale-", dir="/tmp"))
    engine_pkl = home / "engine.pkl"
    ff_csv = results / f"ff_{args.label}.csv"
    ff_csv.unlink(missing_ok=True)
    err = open(home / "worker.log", "w")
    upstream = FakeUpstream()
    upstream.upstream_port = upstream.start()
    session = Session(args)
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    started = time.monotonic()
    print(f"scale run {args.label}: home {home}; {uptime()}", file=sys.stderr, flush=True)
    stop_reasons: list[str] = []
    ff_over_total = 0
    try:
        for checkpoint in checkpoints:
            target = int(checkpoint * 1e6)
            first_turn = session.turn
            to_turn = session.turn_reaching(target)
            load_at = uptime()
            print(f"checkpoint {checkpoint:g}M: turn {to_turn}; {load_at}", file=sys.stderr, flush=True)
            ff_seconds = 0.0
            if checkpoint <= args.http_until:
                drive_http(args, session, upstream, home, engine_pkl, to_turn, checkpoint, rows, err)
                reached = "http"
            else:
                ff_seconds = fast_forward(args, session, home, engine_pkl, to_turn, ff_csv, err)
                reached = "fast_forward"
            history = session.turn_start_tokens()
            peak_max, ff_over = ff_peaks(ff_csv, first_turn, to_turn, args.bound) if reached == "fast_forward" else (0, 0)
            ff_over_total += ff_over
            measured = checkpoint_measure(args, session, upstream, home, engine_pkl, checkpoint, rows, err, home)
            session_dir = home / "sessions" / "scale"
            counts, max_logged = log_events(session_dir / "log.jsonl")
            pages = list((session_dir / "pages").glob("pg_*.json"))
            cp_rows = [r for r in rows if r["checkpoint_m"] == checkpoint]
            summaries.append({"label": args.label, "checkpoint_m": checkpoint, "turn": to_turn,
                              "history_tokens": history, "reached_by": reached, "ff_turns": to_turn - first_turn,
                              "ff_seconds": round(ff_seconds, 1), "pages": len(pages),
                              "pages_mb": round(sum(p.stat().st_size for p in pages) / 1e6, 2),
                              "store_mb": dir_mb(session_dir), "log_mb": round(
                                  (session_dir / "log.jsonl").stat().st_size / 1e6, 2),
                              **measured, "max_upstream_tokens": max(
                                  [max_logged, *(r.get("upstream_tokens") or 0 for r in cp_rows)]),
                              "ff_turn_peak_max": peak_max, "ff_turns_over_bound": ff_over,
                              "uptime": load_at, **{f"n_{e}": counts[e] for e in COUNTED_EVENTS}})
            write_csv(results / f"requests_{args.label}.csv", REQUEST_FIELDS, rows)
            write_csv(results / f"checkpoints_{args.label}.csv", CHECKPOINT_FIELDS, summaries)
            print(f"checkpoint {checkpoint:g}M done at {time.monotonic() - started:.0f}s: "
                  f"{json.dumps(summaries[-1])}", file=sys.stderr, flush=True)
            stop_reasons = broken(rows, counts, args.bound, ff_over_total)
            if stop_reasons and not args.keep_going:
                print(f"STOPPED after checkpoint {checkpoint:g}M: {'; '.join(stop_reasons)}", file=sys.stderr)
                break
    finally:
        upstream.stop()
        err.close()
        if args.keep_home:
            print(f"kept {home}", file=sys.stderr)
        else:
            shutil.copy(home / "worker.log", results / f"worker_{args.label}.log")
            shutil.rmtree(home, ignore_errors=True)
    print(f"total {time.monotonic() - started:.0f}s; {uptime()}", file=sys.stderr)
    print(json.dumps({"label": args.label, "stopped": stop_reasons, "checkpoints": summaries}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
