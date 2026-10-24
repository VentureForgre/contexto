"""``contexto sessions list``, ``contexto sessions show <id>``, and ``contexto sessions prune`` (D37)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime

from contexto.commands._common import EXIT_ERROR, EXIT_OK, fail
from contexto.paths import contexto_home
from contexto.store import SessionNotFoundError
from contexto.store.prune import (ACTIVE_GRACE_S, SessionLiveError, check_not_live, parse_duration,
                                  remove_session, safe_session_path, select)
from contexto.store.sessions import list_sessions, session_summary


def human_size(n: int) -> str:
    """``n`` bytes in binary units: ``512 B``, ``4.0 KiB``, ``1.2 MiB``."""
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024
        if size < 1024 or unit == "GiB":
            break
    return f"{size:.1f} {unit}"


def _duration(text: str) -> int:
    try:
        return parse_duration(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _count(text: str) -> int:
    if not (text.isascii() and text.isdecimal()):
        raise argparse.ArgumentTypeError(f"invalid count {text!r}: use a whole number, 0 or more")
    return int(text)


def register(subparsers) -> None:
    parser = subparsers.add_parser("sessions", help="list and inspect sessions",
                                   description="List and inspect sessions under $CONTEXTO_HOME.")
    sub = parser.add_subparsers(title="sessions commands", metavar="<list|show|prune>", required=True)
    ls = sub.add_parser("list", help="list sessions, most recent activity first")
    ls.add_argument("--json", action="store_true", help="print JSON")
    ls.set_defaults(func=run_list)
    show = sub.add_parser("show", help="summarize one session")
    show.add_argument("session_id", metavar="ID")
    show.add_argument("--json", action="store_true", help="print JSON")
    show.set_defaults(func=run_show)
    prune = sub.add_parser(
        "prune", help="delete old sessions (dry run unless --yes)",
        description="Delete whole session directories. Without --yes, only print what would be removed. "
                    "Every given condition must hold. Sessions in use by a running proxy or MCP server, "
                    f"or active in the last {ACTIVE_GRACE_S // 60} minutes, are never removed.")
    prune.add_argument("--older-than", type=_duration, metavar="DURATION",
                       help="select sessions with no activity for DURATION (for example 30d, 12h, 90m, 2w)")
    prune.add_argument("--keep", type=_count, metavar="N",
                       help="keep the N sessions with the most recent activity; select the rest")
    prune.add_argument("--session", metavar="ID", help="select this one session")
    prune.add_argument("--yes", action="store_true", help="delete; without it nothing is deleted")
    prune.set_defaults(func=run_prune, prune_parser=prune)


def run_list(args: argparse.Namespace) -> int:
    home = contexto_home()
    sessions = list_sessions(home)
    if args.json:
        print(json.dumps([vars(s) for s in sessions], indent=2, sort_keys=True))
        return EXIT_OK
    if not sessions:
        print(f"No sessions under {home / 'sessions'}.")
        return EXIT_OK
    rows = [("ID", "CREATED", "PAGES", "RECORDS", "LAST ACTIVITY", "SIZE")]
    rows += [(s.id, s.created, str(s.pages), "?" if s.records is None else str(s.records), s.last_activity,
              human_size(s.size_bytes)) for s in sessions]
    _table(rows)
    print(f"Total: {human_size(sum(s.size_bytes for s in sessions))} in {_plural(len(sessions))}")
    return EXIT_OK


def _table(rows: list[tuple[str, ...]], indent: str = "") -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print(indent + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plural(n: int) -> str:
    return f"{n} session{'' if n == 1 else 's'}"


def run_prune(args: argparse.Namespace) -> int:
    if args.older_than is None and args.keep is None and args.session is None:
        args.prune_parser.error("say which sessions: --older-than DURATION, --keep N, or --session ID")
    home = contexto_home()
    now = time.time()
    try:
        chosen = select(home, older_than=args.older_than, keep=args.keep, session_id=args.session, now=now)
    except (SessionNotFoundError, ValueError) as e:  # UnsafeSessionPathError is a ValueError
        return fail(str(e))
    removable, skipped = [], []
    for c in chosen:
        try:
            check_not_live(safe_session_path(home, c.id), now=now)
        except (SessionLiveError, SessionNotFoundError, ValueError, OSError) as e:
            skipped.append((c, str(e)))
            continue
        removable.append(c)
    for c, why in skipped:
        print(f"skip {c.id}: {why}")
    # Refusing the one session the user named is a failure; skipping part of a broad selection is not.
    code = EXIT_ERROR if args.session is not None and skipped else EXIT_OK
    if not removable:
        print("No sessions to remove.")
        return code
    total = sum(c.size_bytes for c in removable)
    if not args.yes:
        print(f"Would remove {_plural(len(removable))}, {human_size(total)}:")
        rows = [("ID", "LAST ACTIVITY", "SIZE")]
        rows += [(c.id, _iso(c.last_activity), human_size(c.size_bytes)) for c in removable]
        _table(rows, indent="  ")
        print("Dry run: nothing was deleted. Rerun with --yes to delete.")
        return code
    freed, removed, failed = 0, 0, 0
    for c in removable:
        try:
            size = remove_session(home, c.id, now=time.time())
        except SessionLiveError as e:  # opened since the check above; nothing was deleted
            print(f"skip {c.id}: {e}")
            if args.session is not None:
                code = EXIT_ERROR
            continue
        except (SessionNotFoundError, ValueError, OSError) as e:
            failed += 1
            print(f"contexto: error: could not remove session {c.id}: {e}", file=sys.stderr)
            continue
        removed += 1
        freed += size
        print(f"removed {c.id} ({human_size(size)})")
    print(f"Removed {_plural(removed)}, {human_size(freed)} freed" + (f"; {failed} failed." if failed else "."))
    return EXIT_ERROR if failed else code


def run_show(args: argparse.Namespace) -> int:
    try:
        summary = session_summary(contexto_home(), args.session_id)
    except (SessionNotFoundError, ValueError) as e:
        return fail(str(e))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return EXIT_OK
    labels = [("id", "Session"), ("created", "Created"), ("last_activity", "Last activity"),
              ("pages", "Pages"), ("records", "Ledger records"), ("active_records", "Active records"),
              ("open_obligations", "Open obligations"), ("log_events", "Log events"),
              ("over_bound", "Over-bound turns"), ("upstream_4xx", "Upstream 4xx")]
    for key, label in labels:
        value = summary[key]
        print(f"{label}: {'?' if value is None else value}")
    print(f"Last epoch: {json.dumps(summary['last_epoch'], sort_keys=True) if summary['last_epoch'] else 'none logged'}")
    problems = summary["ledger_problems"]
    print(f"Ledger: {'intact' if not problems else '; '.join(problems)}")
    return EXIT_OK
