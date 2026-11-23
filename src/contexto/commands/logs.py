"""``contexto logs [SESSION] [--follow]``: view or live-tail a session's ``log.jsonl`` (or ``serve.log``).

Reads only local files (never blocks on FIFOs or follows symlinks). Terminal control characters and
invisible/bidi characters are escaped with ``safe_text`` before printing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from contexto.commands._common import (
    EXIT_OK,
    EXIT_USAGE,
    SESSION_HELP,
    add_session_option,
    fail,
    safe_text,
    say_session,
)
from contexto.paths import contexto_home, session_dir
from contexto.private import open_regular
from contexto.stats import FAULT_EVENTS
from contexto.store import SessionNotFoundError
from contexto.store.sessions import select_session

DEFAULT_LINES = 50
POLL_INTERVAL_S = 0.25


def _nonneg_int(text: str) -> int:
    if not (text.isascii() and text.isdecimal()):
        raise argparse.ArgumentTypeError(f"invalid line count {text!r}: use a whole number, 0 or more")
    return int(text)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "logs", help="view or live-tail a session's log.jsonl (or serve.log)",
        description="Print events from a session's log.jsonl (or $CONTEXTO_HOME/serve.log with --serve). "
                    "Use --follow to stream new events as they arrive until Ctrl+C.")
    parser.add_argument("session_id", metavar="SESSION", nargs="?", default=None, help=SESSION_HELP)
    add_session_option(parser)
    parser.add_argument("-f", "--follow", action="store_true",
                        help="keep streaming new log lines as they are written (Ctrl+C to stop)")
    parser.add_argument("-n", "--lines", type=_nonneg_int, default=DEFAULT_LINES, metavar="N",
                        help=f"show the last N matching events (default {DEFAULT_LINES}; 0 shows all)")
    parser.add_argument("--event", action="append", default=[], metavar="TYPE",
                        help="show only events with this event name (may be given more than once)")
    parser.add_argument("--faults", action="store_true",
                        help="show only fault events (including requests that logged a nested fault)")
    parser.add_argument("--serve", action="store_true",
                        help="read $CONTEXTO_HOME/serve.log instead of a session log")
    parser.add_argument("--json", action="store_true", help="print each matching event as raw JSON")
    parser.set_defaults(func=run)


def _short_ts(ts: Any) -> str:
    if not isinstance(ts, str) or not ts:
        return "        "
    if "T" in ts:
        tail = ts.split("T", 1)[1]
        return safe_text(tail[:8])
    return safe_text(ts[:19])


def _has_nested_fault(event: dict[str, Any]) -> bool:
    for key in ("transform_events", "usage_events"):
        nested = event.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict) and item.get("event") in FAULT_EVENTS:
                    return True
    return False


def matches_filter(event: dict[str, Any], events: set[str], faults_only: bool) -> bool:
    name = event.get("event")
    if events and name not in events:
        return False
    if faults_only:
        return (isinstance(name, str) and name in FAULT_EVENTS) or _has_nested_fault(event)
    return True


def format_event(event: dict[str, Any]) -> str:
    """Format one parsed JSON log event into a single safe terminal line."""
    ts = _short_ts(event.get("ts"))
    name = event.get("event")
    ev = safe_text(name) if isinstance(name, str) else "unknown"
    is_fault = isinstance(name, str) and name in FAULT_EVENTS
    badge = "[FAULT] " if is_fault else ""

    parts: list[str] = []
    if name == "request":
        method = safe_text(str(event.get("method", "")))
        path = safe_text(str(event.get("path", "")))
        status = event.get("status")
        if method or path:
            parts.append(f"{method} {path}".strip())
        if status is not None:
            parts.append(safe_text(str(status)))
        model = event.get("model")
        if isinstance(model, str) and model:
            parts.append(f"model={safe_text(model)}")
        usage = event.get("usage")
        if isinstance(usage, dict):
            inp = usage.get("input_tokens")
            cr = usage.get("cache_read_input_tokens")
            if cr is None and isinstance(usage.get("input_tokens_details"), dict):
                cr = usage["input_tokens_details"].get("cached_tokens")
            cw = usage.get("cache_creation_input_tokens")
            out_tok = usage.get("output_tokens")
            if isinstance(inp, int) and not isinstance(inp, bool):
                total_in = inp if event.get("api") == "responses" else (
                    inp + (cr if isinstance(cr, int) and not isinstance(cr, bool) else 0)
                    + (cw if isinstance(cw, int) and not isinstance(cw, bool) else 0))
                parts.append(f"in={total_in:,}")
            if isinstance(out_tok, int) and not isinstance(out_tok, bool):
                parts.append(f"out={out_tok:,}")
            if isinstance(cr, int) and not isinstance(cr, bool) and cr > 0:
                parts.append(f"cache_read={cr:,}")
        orig, sent = event.get("original_request_bytes"), event.get("request_bytes")
        if type(orig) is int and type(sent) is int:
            parts.append(f"bytes={orig:,}->{sent:,}")
        if event.get("over_bound") is True:
            parts.append("[OVER_BOUND]")
        if _has_nested_fault(event):
            nested_names = sorted({
                str(i.get("event"))
                for k in ("transform_events", "usage_events")
                for i in (event.get(k) if isinstance(event.get(k), list) else [])
                if isinstance(i, dict) and i.get("event") in FAULT_EVENTS
            })
            parts.append(f"[FAULT:{','.join(safe_text(n) for n in nested_names)}]")
        elif any(isinstance(i, dict) and i.get("event") == "rotation"
                 for i in (event.get("transform_events") if isinstance(event.get("transform_events"), list) else [])):
            parts.append("[ROTATION]")
        if isinstance(event.get("error"), str):
            parts.append(f"error={safe_text(event['error'])}")
    elif name in ("launch", "serve_open"):
        if isinstance(event.get("cli"), str):
            parts.append(f"cli={safe_text(event['cli'])}")
        cfg = event.get("config")
        if isinstance(cfg, dict) and type(cfg.get("bound")) is int:
            parts.append(f"bound={cfg['bound']:,}")
        if isinstance(event.get("upstream_source"), str):
            parts.append(f"upstream={safe_text(event['upstream_source'])}")
        if isinstance(event.get("statusline"), str):
            parts.append(f"statusline={safe_text(event['statusline'])}")
    elif name == "mcp_call":
        if isinstance(event.get("tool"), str):
            parts.append(f"tool={safe_text(event['tool'])}")
        if "ok" in event:
            parts.append(f"ok={str(bool(event['ok'])).lower()}")
        if isinstance(event.get("charged_tokens"), int) and not isinstance(event.get("charged_tokens"), bool):
            parts.append(f"charged={event['charged_tokens']:,}")
    elif name == "exit":
        if "code" in event:
            parts.append(f"code={safe_text(str(event['code']))}")
    else:
        for k, v in event.items():
            if k in ("ts", "event"):
                continue
            if isinstance(v, (str, int, float, bool)):
                parts.append(f"{safe_text(str(k))}={safe_text(str(v))}")
            if len(parts) >= 6:
                break

    detail = " ".join(parts)
    return f"{ts} {badge}{ev}" + (f"  {detail}" if detail else "")


def _parse_line(raw: bytes) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


MAX_LINE_BYTES = 4 << 20  # 4 MiB per log line (matches stats.LOG_BLOCK_BYTES)


def _read_existing(path: Path, events: set[str], faults_only: bool, limit: int,
                   *, follow: bool = False) -> tuple[list[dict[str, Any]], int, tuple[int, int]]:
    """Stream existing lines from ``path`` in bounded chunks; returns ``(matching_events, byte_offset, (dev, ino))``."""
    fd = open_regular(path)
    matched: deque[dict[str, Any]] | list[dict[str, Any]] = deque(maxlen=limit) if limit > 0 else []
    offset = 0
    buf = b""
    drop_until_nl = False
    try:
        st = os.fstat(fd)
        ident = (st.st_dev, st.st_ino)
        while chunk := os.read(fd, 1 << 20):
            if drop_until_nl:
                nl = chunk.find(b"\n")
                if nl < 0:
                    offset += len(chunk)
                    continue
                offset += nl + 1
                chunk = chunk[nl + 1:]
                drop_until_nl = False
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = buf[:nl]
                offset += nl + 1
                buf = buf[nl + 1:]
                if line.strip():
                    parsed = _parse_line(line)
                    if parsed is not None and matches_filter(parsed, events, faults_only):
                        matched.append(parsed)
            if len(buf) > MAX_LINE_BYTES:
                offset += len(buf)
                buf = b""
                drop_until_nl = True
    finally:
        os.close(fd)
    if not follow and buf and not drop_until_nl:
        offset += len(buf)
        if buf.strip():
            parsed = _parse_line(buf)
            if parsed is not None and matches_filter(parsed, events, faults_only):
                matched.append(parsed)
    return list(matched), offset, ident


def _emit(event: dict[str, Any], as_json: bool, stdout) -> None:
    if as_json:
        print(json.dumps(event, sort_keys=True), file=stdout, flush=True)
    else:
        print(format_event(event), file=stdout, flush=True)


def follow_log(path: Path, offset: int, events: set[str], faults_only: bool, as_json: bool,
               stdout, *, ident: tuple[int, int] | None = None,
               poll_s: float = POLL_INTERVAL_S, max_polls: int | None = None) -> int:
    """Tail new lines appended to ``path`` starting at ``offset`` until KeyboardInterrupt or ``max_polls``."""
    buf = b""
    drop_until_nl = False
    polls = 0
    try:
        while max_polls is None or polls < max_polls:
            try:
                fd = open_regular(path)
            except OSError:
                time.sleep(poll_s)
                polls += 1
                continue
            try:
                st = os.fstat(fd)
                cur_ident = (st.st_dev, st.st_ino)
                size = st.st_size
                if (ident is not None and cur_ident != ident) or size < offset:  # rotated or truncated
                    offset = 0
                    buf = b""
                    drop_until_nl = False
                ident = cur_ident
                if size > offset:
                    os.lseek(fd, offset, os.SEEK_SET)
                    new_data = os.read(fd, min(1 << 20, size - offset))
                    offset += len(new_data)
                    if drop_until_nl:
                        nl = new_data.find(b"\n")
                        if nl < 0:
                            new_data = b""
                        else:
                            new_data = new_data[nl + 1:]
                            drop_until_nl = False
                    buf += new_data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        parsed = _parse_line(line)
                        if parsed is not None and matches_filter(parsed, events, faults_only):
                            _emit(parsed, as_json, stdout)
                    if len(buf) > MAX_LINE_BYTES:
                        buf = b""
                        drop_until_nl = True
            finally:
                os.close(fd)
            polls += 1
            if max_polls is None or polls < max_polls:
                time.sleep(poll_s)
    except KeyboardInterrupt:
        return EXIT_OK
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    if args.serve and (args.session_id is not None or args.session is not None):
        print("contexto logs: --serve cannot be combined with a session", file=sys.stderr)
        return EXIT_USAGE
    if args.session_id is not None and args.session is not None:
        print("contexto logs: name the session as SESSION or with --session, not both", file=sys.stderr)
        return EXIT_USAGE
    home = contexto_home()
    if args.serve:
        log_path = home / "serve.log"
        label = str(log_path)
    else:
        try:
            if args.session_id is not None:
                sid, how = select_session(home, args.session_id, "SESSION")
            else:
                sid, how = select_session(home, args.session)
        except SessionNotFoundError as exc:
            return fail(str(exc))
        if not args.json:
            say_session(sid, how)
        log_path = session_dir(home, sid) / "log.jsonl"
        label = f"log for session {sid}"

    event_filter = {e.strip() for raw in (args.event or []) for e in raw.split(",") if e.strip()}
    ident: tuple[int, int] | None = None
    try:
        initial, offset, ident = _read_existing(log_path, event_filter, bool(args.faults), args.lines,
                                                follow=bool(args.follow))
    except FileNotFoundError:
        if not args.follow:
            return fail(f"no {label} yet")
        initial, offset = [], 0
    except OSError as exc:
        return fail(f"cannot read {label}: {exc.strerror or type(exc).__name__}")

    try:
        for item in initial:
            _emit(item, bool(args.json), sys.stdout)
        if args.follow:
            return follow_log(log_path, offset, event_filter, bool(args.faults), bool(args.json), sys.stdout,
                              ident=ident)
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return EXIT_OK
    return EXIT_OK
