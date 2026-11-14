"""The proxy side of the scale harness: one process per phase, over one engine and one session store.

``serve``: the proxy ``contexto run claude`` starts, started by ``launcher.start_proxy`` itself (so it has
the launcher's ``NoticeLog``, ``SessionRecorder`` and ``X-Session-Affinity`` key) on 127.0.0.1, with the
engine either new (``launcher.build_engine``) or unpickled from the previous phase. Timers are wrapped
around the handler's ``_read_body`` and ``_transform``, the engine call, its ``observe_usage``, and the
methods of the store and its page catalog named in ``INSTRUMENTED``; nothing in ``src/`` changes. A
JSON-lines control channel on stdin/stdout returns per-request timings, ``ru_maxrss``, toggles cProfile,
and on ``exit`` stops the proxy as the launcher does and pickles the engine for the next phase.

``ff`` (fast-forward): the same engine, called directly at each turn start only, with the request headers
the driver sends (``synth.CLI_HEADERS``; none before 2026-11-13), the usage the fake upstream would report
for the bytes the proxy would send (``ceil(len(serialize_request(upstream)) / 1.83)``), and a proxy-shaped
entry in the session log. See README.md "How each checkpoint is reached".

Run by run.py; not meant to be started by hand.
"""

from __future__ import annotations

import argparse
import cProfile
import csv
import gc
import io
import json
import os
import pickle
import resource
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from contexto import launcher  # noqa: E402
from contexto import proxy as proxy_mod  # noqa: E402
from contexto.rewrite import RewriteEngine  # noqa: E402
from contexto.store import SessionStore  # noqa: E402
from contexto.store.catalog import PageCatalog  # noqa: E402
from scale.proxy import synth  # noqa: E402
from scale.proxy.upstream import OUTPUT_TOKENS  # noqa: E402

# Every name the harness reaches for in the package, by the class that has it: the methods it wraps a
# timer around, and the engine attributes its timer forwards. tests/test_scale_proxy_drift.py fails when
# one is not an attribute of its class, so a rename or a move in the package is caught by the suite.
STORE_METHODS = ("index_text", "pinned_text", "page_out", "set_retained", "set_epoch", "set_turn_room",
                 "set_side_room", "set_window", "log")
# The store's page catalog (``SessionStore._catalog``; D34). Until the store was split, ``index_text``
# read every page file through ``SessionStore._pages``. Now ``PageCatalog.entries`` lists ``pages/`` and
# reads only the files this process has not seen, and ``_read`` is one page file read. ``entries`` keeps
# the old timer key, ``store_pages`` (the ``pages_read_s`` and ``store_pages_s`` columns).
CATALOG_METHODS = {"entries": "store_pages", "_read": "store_page_read"}
HANDLER_METHODS = ("_read_body", "_transform")
ENGINE_ATTRIBUTES = ("observe_usage", "prime", "preserves_input", "tools_verdict", "states", "store")
INSTRUMENTED: tuple[tuple[type, tuple[str, ...]], ...] = (
    (SessionStore, (*STORE_METHODS, "_catalog")),
    (PageCatalog, tuple(CATALOG_METHODS)),
    (proxy_mod._Handler, HANDLER_METHODS),
    (RewriteEngine, ENGINE_ATTRIBUTES),
)
EVENT_NAMES = ("rotation", "rotation_noop", "page_out", "window_minimal", "bound_too_small", "over_bound",
               "carried_over_budget", "carried_paged", "carried_text_growing", "turn_room_low", "estimate_error",
               "usage_missing", "usage_unattributed", "resync", "degraded_enter", "store_error", "page_conflict",
               "fallback_failed", "retained_stale", "epoch_record_stale", "head_changed",
               # Added 2026-11-12: the engine did not take the synthetic session for Claude Code's.
               "session_start_unrecognised", "unclassified_system_message", "cli_version_untested",
               "mcp_tools_missing", "toolcheck_error", "cli_version_error")


class Meter:
    """Per-request timers keyed by the request's x-scale-id (thread-local, set when its body is read)."""

    def __init__(self) -> None:
        self.local = threading.local()
        self.records: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def begin(self, request_id: str) -> None:
        self.local.id = request_id
        with self.lock:
            self.records[request_id] = {"id": request_id}

    def rec(self) -> dict[str, Any] | None:
        request_id = getattr(self.local, "id", None)
        return self.records.get(request_id) if request_id is not None else None

    def add(self, key: str, seconds: float) -> None:
        rec = self.rec()
        if rec is not None:
            rec[key] = rec.get(key, 0.0) + seconds
            rec[key + "_n"] = rec.get(key + "_n", 0) + 1

    def events(self, key: str, events: list[dict[str, Any]]) -> None:
        rec = self.rec()
        if rec is not None:
            rec.setdefault(key, []).extend(summarize_events(events))

    def take(self) -> list[dict[str, Any]]:
        with self.lock:
            out, self.records = list(self.records.values()), {}
        return out


def summarize_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Content-free event summaries: names and the numeric fields the README reports."""
    keep = ("reason", "k", "turns", "pages", "fixed_overhead", "window_budget", "carried_tokens", "carried_growth",
            "new_turn_tokens", "estimate", "sent_bytes", "real_tokens", "source", "in_tool_loop", "epoch",
            "upstream_messages", "messages", "turns_paged")
    out = []
    for e in events:
        if isinstance(e, dict) and isinstance(e.get("event"), str):
            out.append({"event": e["event"], **{k: e[k] for k in keep if k in e and not isinstance(e[k], (dict, list))}})
    return out


METER = Meter()
PROFILE: dict[str, cProfile.Profile | None] = {"p": None}


def timed(key: str, fn):
    def wrapper(*args, **kwargs):
        t = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            METER.add(key, time.perf_counter() - t)
    wrapper.__wrapped_original__ = fn
    return wrapper


def instrument_store(store: SessionStore) -> None:
    for name in STORE_METHODS:
        setattr(store, name, timed("store_" + name, getattr(store, name)))
    catalog = store._catalog
    for name, key in CATALOG_METHODS.items():
        setattr(catalog, name, timed(key, getattr(catalog, name)))


def uninstrument_store(store: SessionStore) -> None:
    """Take the timers off, and the page catalog with them: it holds a lock, which cannot be pickled, and
    the next process builds its own (reading each page file once, as a restarted proxy does)."""
    for name in STORE_METHODS:
        store.__dict__.pop(name, None)
    store.__dict__.pop("_page_catalog", None)


def instrument_handler() -> None:
    handler = proxy_mod._Handler
    read_body, transform = handler._read_body, handler._transform

    def _read_body(self, *args, **kwargs):
        METER.begin(self.headers.get("x-scale-id") or f"anon-{time.monotonic_ns()}")
        t, cpu = time.perf_counter(), time.thread_time()
        try:
            return read_body(self, *args, **kwargs)
        finally:
            METER.add("read_body", time.perf_counter() - t)
            METER.add("cpu", time.thread_time() - cpu)

    def _transform(self, *args, **kwargs):
        t, cpu = time.perf_counter(), time.thread_time()
        try:
            profiler = PROFILE["p"]
            if profiler is not None:
                return profiler.runcall(transform, self, *args, **kwargs)
            return transform(self, *args, **kwargs)
        finally:
            METER.add("proxy_transform", time.perf_counter() - t)
            METER.add("cpu", time.thread_time() - cpu)

    handler._read_body = _read_body
    handler._transform = _transform


class TimedEngine:
    """The engine with timers on its two entry points. Every other attribute is the engine's
    (``preserves_input``, ``prime``, ``tools_verdict``), so the proxy and the launcher take the same paths
    as with the bare engine. ``observe_usage`` passes its keywords through: the proxy gives a callback
    ``tool_calls`` and ``tool_call_ids`` only when its signature takes them (D40).

    The ``cpu`` key is the handler thread's CPU time (``time.thread_time``) in ``_read_body``,
    ``_transform`` and ``observe_usage`` together: the proxy's own work on a request, without the time it
    waits for a CPU, for the transform lock or for the upstream."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def __call__(self, request, headers, endpoint):
        t = time.perf_counter()
        out = self.engine(request, headers, endpoint)
        METER.add("engine", time.perf_counter() - t)
        METER.events("events", out[2])
        return out

    def observe_usage(self, endpoint, usage, request, **extra):
        t, cpu = time.perf_counter(), time.thread_time()
        profiler = PROFILE["p"]
        observe = self.engine.observe_usage
        events = (profiler.runcall(observe, endpoint, usage, request, **extra) if profiler is not None
                  else observe(endpoint, usage, request, **extra))
        METER.add("observe", time.perf_counter() - t)
        METER.add("cpu", time.thread_time() - cpu)
        METER.events("usage_events", events)
        return events

    def __getattr__(self, name: str) -> Any:
        engine = self.__dict__.get("engine")
        if engine is None:
            raise AttributeError(name)
        return getattr(engine, name)


def start_proxy(store: SessionStore, engine, upstream: str, bound: int) -> proxy_mod.ContextoProxy:
    """``launcher.start_proxy`` as ``contexto run claude`` calls it, with the timed engine in place of the
    one it would build: the launcher's own wiring, whatever it becomes. Its stderr notices are dropped."""
    timed_engine = TimedEngine(engine)
    build = launcher.build_engine
    launcher.build_engine = lambda store, config: (timed_engine, timed_engine.observe_usage, "rewrite")
    try:
        proxy, _ = launcher.start_proxy(store, upstream, "scale-harness", {"bound": bound}, False,
                                        stderr=io.StringIO(), cli="claude")
    finally:
        launcher.build_engine = build
    return proxy


def stop_proxy(proxy: proxy_mod.ContextoProxy) -> None:
    """What the launcher does when the CLI exits: stop, judge the tools once more, record the CLI sessions."""
    proxy.stop()
    launcher.report_missing_tools(proxy)
    flush = getattr(proxy.transform, "flush", None)
    if callable(flush):
        flush()


def open_engine(home: Path, session: str, engine_in: str | None, bound: int):
    if engine_in:
        with open(engine_in, "rb") as f:
            engine = pickle.load(f)
        return engine.store, engine
    store = launcher.open_session(home, session, Path.cwd())
    transform, _, name = launcher.build_engine(store, {"bound": bound})
    if name != "rewrite":
        raise SystemExit("contexto.rewrite is not importable; the harness needs the real engine")
    return store, transform


def save_engine(engine, path: str) -> None:
    uninstrument_store(engine.store)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(engine, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def maxrss_mb() -> float:
    """This process's peak RSS in MB. On Linux it is ``VmHWM`` from ``/proc/self/status``, not
    ``ru_maxrss``: the kernel carries ``ru_maxrss`` across exec, so a new process starts at the peak of
    the one that started it. Under pytest that was the test process's own 1,600 MB (the first CI run of
    the short test, 2026-11-12), whatever the proxy used."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1e3  # KiB
    except (OSError, ValueError, IndexError):
        pass
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 1e6 if sys.platform == "darwin" else rss / 1e3  # bytes on macOS, KiB on Linux


def ps_rss_mb(pid: int) -> float | None:
    """Current RSS from ``ps`` (psutil is not available)."""
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
        return round(int(out.strip()) / 1024, 1) if out.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def reply(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    store, engine = open_engine(Path(args.home), args.session, args.engine_in, args.bound)
    instrument_store(store)
    instrument_handler()
    proxy = start_proxy(store, engine, args.upstream, args.bound)
    # ``start_s``: opening the session, building the engine and starting the proxy, without the interpreter
    # and the imports before them (the driver times the whole start, to this line).
    reply({"ready": True, "port": proxy.port, "pid": os.getpid(), "maxrss_mb": maxrss_mb(),
           "start_s": round(time.perf_counter() - started, 4)})
    for line in sys.stdin:
        cmd = json.loads(line)
        name = cmd.get("cmd")
        if name == "stats":
            reply({"records": METER.take(), "maxrss_mb": maxrss_mb()})
        elif name == "profile_on":
            PROFILE["p"] = cProfile.Profile()
            reply({"ok": True})
        elif name == "profile_off":
            profiler, PROFILE["p"] = PROFILE["p"], None
            if profiler is not None:
                profiler.dump_stats(cmd["path"])
            reply({"ok": True})
        elif name == "gc":  # leak probe: collect, then report live objects and current RSS (ps)
            collected = gc.collect()
            reply({"collected": collected, "objects": len(gc.get_objects()), "rss_mb": ps_rss_mb(os.getpid()),
                   "maxrss_mb": maxrss_mb(), "threads": threading.active_count()})
        elif name == "trace_start":
            tracemalloc.start(int(cmd.get("frames", 8)))
            reply({"ok": True})
        elif name == "trace_top":
            snapshot = tracemalloc.take_snapshot()
            top = snapshot.statistics("traceback")[: int(cmd.get("n", 8))]
            reply({"top": [{"mb": round(s.size / 1e6, 2), "count": s.count,
                            "trace": [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}" for f in s.traceback]}
                           for s in top]})
        elif name == "exit":  # without ``engine_out`` nothing is kept for a next phase
            stop_proxy(proxy)
            if cmd.get("engine_out"):
                save_engine(engine, cmd["engine_out"])
            reply({"ok": True, "maxrss_mb": maxrss_mb()})
            return 0
        else:
            reply({"error": f"unknown command {name!r}"})
    return 1


FF_FIELDS = ("turn", "history_tokens", "upstream_tokens", "turn_peak_tokens", "upstream_messages", "engine_s", "observe_s",
             "store_index_text_s", "store_index_text_n", "store_pages_s", "store_page_out_s", "rotated",
             "rotation_reason", "k", "epoch", "turns_paged", "fixed_overhead", "window_budget", "events",
             "store_page_reads_n")


def fast_forward(args: argparse.Namespace) -> int:
    """Turn starts ``from_turn`` to ``to_turn - 1`` through the engine directly (see the module docstring)."""
    store, engine = open_engine(Path(args.home), args.session, args.engine_in, args.bound)
    instrument_store(store)
    shape = synth.Shape(**json.loads(args.shape))
    head = synth.head(shape)
    history: list[dict[str, Any]] = []
    for t in range(args.from_turn):
        history.extend(synth.turn(shape, t))
    log = launcher.NoticeLog(store, io.StringIO())
    new_file = not Path(args.csv_out).exists()
    started = time.monotonic()
    with open(args.csv_out, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FF_FIELDS)
        if new_file:
            writer.writeheader()
        for t in range(args.from_turn, args.to_turn):
            messages = synth.turn(shape, t)
            first = synth.request_points(shape, t, messages)[0][0]
            request = synth.request(head, history, messages[:first])
            history_bytes = len(synth.serialize(request))
            history_tokens = synth.tokens_of(history_bytes)
            METER.begin(f"ff{t}")
            t0 = time.perf_counter()
            upstream, _, events = engine(request, dict(synth.CLI_HEADERS), "/v1/messages")  # the driver's headers
            METER.add("engine", time.perf_counter() - t0)
            sent = len(proxy_mod.serialize_request(upstream))
            tokens = synth.tokens_of(sent)
            # The turn's last request (its final tool_result), which fast-forward does not send: rotation happens
            # only at turn starts, so it is this request's upstream plus the turn's later messages as the CLI
            # sent them (they postdate the epoch, so they keep their thinking). Exact in bytes.
            points = synth.request_points(shape, t, messages)
            later = sum(len(synth.serialize(m)) + 1 for m in messages[first:points[-1][0]])
            peak = synth.tokens_of(sent + later)
            # What the fake upstream would answer the turn start with: its usage, and whether the reply
            # holds a tool call, which the proxy tells ``observe_usage`` (D40).
            calls_tool = points[0][1] == "tool_use"
            usage = {"input_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": tokens - 2,
                     "output_tokens": OUTPUT_TOKENS[points[0][1]]}
            t0 = time.perf_counter()
            usage_events = engine.observe_usage("/v1/messages", usage, request, tool_calls=calls_tool,
                                                tool_call_ids=(f"toolu_ff{t}",) if calls_tool else ())
            METER.add("observe", time.perf_counter() - t0)
            entry = {"event": "request", "method": "POST", "path": "/v1/messages", "fast_forward": True,
                     "api": "messages", "original_message_count": len(request["messages"]),
                     "original_request_bytes": history_bytes, "request_bytes": sent, "status": 200,
                     "usage": usage, "message_count": len(upstream["messages"]),
                     "transform_events": [proxy_mod._scalar_summary(e) for e in events],
                     "over_bound": any(bool(e.get("over_bound")) for e in [*events, *usage_events])}
            if usage_events:
                entry["usage_events"] = [proxy_mod._scalar_summary(e) for e in usage_events]
            log.log({"ts": proxy_mod.utc_now(), **entry})
            rec = METER.take()[0]
            rotation = next((e for e in events if e.get("event") == "rotation"), {})
            state = engine.states[next(iter(engine.states))] if len(engine.states) == 1 else None
            names = [e["event"] for e in [*events, *usage_events] if e.get("event") in EVENT_NAMES]
            writer.writerow({"turn": t, "history_tokens": history_tokens, "upstream_tokens": tokens,
                             "turn_peak_tokens": peak,
                             "upstream_messages": len(upstream["messages"]), "engine_s": round(rec.get("engine", 0), 6),
                             "observe_s": round(rec.get("observe", 0), 6),
                             "store_index_text_s": round(rec.get("store_index_text", 0), 6),
                             "store_index_text_n": rec.get("store_index_text_n", 0),
                             "store_pages_s": round(rec.get("store_pages", 0), 6),
                             "store_page_out_s": round(rec.get("store_page_out", 0), 6),
                             "rotated": int(bool(rotation)), "rotation_reason": rotation.get("reason", ""),
                             "k": state.k if state else "", "epoch": state.epoch if state else "",
                             "turns_paged": state.turns_paged if state else "",
                             "fixed_overhead": rotation.get("fixed_overhead", ""),
                             "window_budget": rotation.get("window_budget", ""), "events": " ".join(names),
                             "store_page_reads_n": rec.get("store_page_read_n", 0)})
            history.extend(messages)
            if (t + 1) % 50 == 0:
                f.flush()
                print(f"ff turn {t + 1} history {history_tokens:,} tokens, {time.monotonic() - started:.0f}s",
                      file=sys.stderr, flush=True)
    save_engine(engine, args.engine_out)
    reply({"ok": True, "seconds": round(time.monotonic() - started, 2), "maxrss_mb": maxrss_mb()})
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("serve", "ff"))
    parser.add_argument("--home", required=True)
    parser.add_argument("--session", default="scale")
    parser.add_argument("--bound", type=int, default=150_000)
    parser.add_argument("--engine-in")
    parser.add_argument("--engine-out")
    parser.add_argument("--upstream")
    parser.add_argument("--shape", default="{}")
    parser.add_argument("--from-turn", type=int, default=0)
    parser.add_argument("--to-turn", type=int, default=0)
    parser.add_argument("--csv-out")
    args = parser.parse_args(argv)
    if args.mode == "serve":
        if not args.upstream or not args.upstream.startswith("http://127.0.0.1:"):
            raise SystemExit("serve needs --upstream http://127.0.0.1:<port> (the local fake upstream only)")
        return serve(args)
    if not (args.engine_out and args.csv_out):
        raise SystemExit("ff needs --engine-out and --csv-out")
    return fast_forward(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
