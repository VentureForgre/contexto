"""``contexto sessions list``, ``contexto sessions show <id>``, and ``contexto sessions prune`` (D37)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime

from contexto.commands._common import EXIT_ERROR, EXIT_OK, EXIT_USAGE, SESSION_HELP, fail
from contexto.paths import contexto_home, session_dir
from contexto.store import SessionNotFoundError, SessionStore
from contexto.store.prune import (
    ACTIVE_GRACE_S,
    SessionLiveError,
    check_not_live,
    parse_duration,
    remove_session,
    select,
)
from contexto.store.sessions import (
    check_home,
    list_sessions,
    no_sessions_yet,
    resolve_session,
    session_summary,
    starting_with,
)
from contexto.store.sessions import named as name_sessions

PAGES_SHOWN = 20  # pages `sessions show` lists without --all


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
                                   description="List and inspect sessions under $CONTEXTO_HOME. With no command, "
                                               "lists the sessions, as `contexto sessions list` does.")
    # --json is accepted before the command too (`contexto sessions --json`). Each command's own --json
    # leaves the value alone when it is not given (SUPPRESS): argparse would otherwise copy the command's
    # default over one given here and drop it without a word.
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.set_defaults(func=run_list)
    sub = parser.add_subparsers(title="sessions commands", metavar="<list|show|prune>")
    ls = sub.add_parser("list", help="list sessions, most recent activity first (the default)")
    ls.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    ls.set_defaults(func=run_list)
    show = sub.add_parser("show", help="summarize one session and list its pages",
                          description="Summarize one session, then list its paged-out turns newest first: "
                                      "page id, turn number, and opening text. `contexto recall PAGE_ID` "
                                      "prints one.")
    show.add_argument("session_id", metavar="ID", help=SESSION_HELP)
    show.add_argument("--all", action="store_true", help=f"list every page, not only the newest {PAGES_SHOWN}")
    show.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
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
    prune.add_argument("--session", metavar="ID",
                       help="select this one session (its full id; a prefix is refused, since prune deletes)")
    prune.add_argument("--yes", action="store_true", help="delete; without it nothing is deleted")
    prune.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                       help="print JSON: dry_run, total_bytes, and each selected session with its status")
    prune.set_defaults(func=run_prune, prune_parser=prune)


def run_list(args: argparse.Namespace) -> int:
    home = contexto_home()
    try:
        check_home(home)
    except SessionNotFoundError as e:
        return fail(str(e))
    sessions = list_sessions(home)
    if args.json:
        print(json.dumps([vars(s) for s in sessions], indent=2, sort_keys=True))
        return EXIT_OK
    if not sessions:
        message = no_sessions_yet(home)  # not an error: an empty list is an answer (and `[]` with --json)
        print(f"{message[0].upper()}{message[1:]}.")
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
        check_home(home)
        if args.session is not None:
            session_dir(home, args.session)  # an invalid id (empty, a path, over 255 bytes) is refused first
        if args.session is not None and starting_with(home, args.session) and not _is_session(home, args.session):
            # Prune is irreversible: a mistyped or already deleted id must not delete another session (D57).
            message = (f"--session {args.session!r} is not a session but the start of "
                       f"{name_sessions(starting_with(home, args.session))}; prune needs the full id")
            if args.json:
                print(json.dumps({"error": message}, indent=2, sort_keys=True))
            fail(message)
            return EXIT_USAGE
        chosen = select(home, older_than=args.older_than, keep=args.keep, session_id=args.session, now=now)
    except (SessionNotFoundError, ValueError, OSError) as e:  # UnsafeSessionPathError is a ValueError
        if args.json:  # stdout stays parseable: a script reads {"error": ...} and the exit code
            print(json.dumps({"error": str(e)}, indent=2, sort_keys=True))
        return fail(str(e))
    named = args.session is not None
    results: list[dict] = []
    failed = refused = 0

    def record(c, status: str, reason: str | None = None, size: int | None = None) -> None:
        results.append({"id": c.id, "last_activity": _iso(c.last_activity), "status": status, "reason": reason,
                        "size_bytes": c.size_bytes if size is None else size})

    def skip(c, why: str) -> None:
        nonlocal refused
        record(c, "skipped", why)
        if named:  # refusing the one session the user named is a failure
            refused += 1
            print(f"contexto: error: session {c.id}: {why}", file=sys.stderr)
        elif not args.json:  # skipping part of a broad selection is not
            print(f"skip {c.id}: {why}")

    removable = []
    for c in chosen:
        try:
            check_not_live(home, c.id, now=now, expect=c.ident)
        except (SessionLiveError, SessionNotFoundError, ValueError, OSError) as e:
            skip(c, str(e))
            continue
        removable.append(c)
    if not args.yes:
        for c in removable:
            record(c, "would_remove")
        total = sum(c.size_bytes for c in removable)
        if args.json:
            pass
        elif removable:
            print(f"Would remove {_plural(len(removable))}, {human_size(total)}:")
            rows = [("ID", "LAST ACTIVITY", "SIZE")]
            rows += [(c.id, _iso(c.last_activity), human_size(c.size_bytes)) for c in removable]
            _table(rows, indent="  ")
            print("Dry run: nothing was deleted. Rerun with --yes to delete.")
        elif not refused:
            print("No sessions to remove.")
    else:
        total = removed = 0
        for c in removable:
            try:
                size = remove_session(home, c.id, now=time.time(), expect=c.ident)
            except SessionLiveError as e:  # opened since the check above
                skip(c, str(e))
                continue
            except (SessionNotFoundError, ValueError, OSError) as e:
                failed += 1
                record(c, "failed", str(e))
                print(f"contexto: error: could not remove session {c.id}: {e}", file=sys.stderr)
                continue
            removed += 1
            total += size
            record(c, "removed", size=size)
            if not args.json:
                print(f"removed {c.id} ({human_size(size)})")
        if args.json:
            pass
        elif removable:
            print(f"Removed {_plural(removed)}, {human_size(total)} freed" + (f"; {failed} failed." if failed else "."))
        elif not refused:
            print("No sessions to remove.")
    if args.json:
        print(json.dumps({"dry_run": not args.yes, "total_bytes": total, "sessions": results},
                         indent=2, sort_keys=True))
    return EXIT_ERROR if failed or refused else EXIT_OK


def _is_session(home, session_id: str) -> bool:
    try:
        return session_dir(home, session_id).is_dir()
    except ValueError:
        return False


def run_show(args: argparse.Namespace) -> int:
    home = contexto_home()
    try:
        session_id = resolve_session(home, args.session_id)
        summary = session_summary(home, session_id)
    except (SessionNotFoundError, ValueError) as e:
        return fail(str(e))
    try:
        pages = SessionStore.open_existing(home, session_id).page_list()
    except OSError as e:  # the summary still stands; the page list is reported in one line
        pages, unreadable = [], f"the pages of session {session_id} cannot be read: {e}"
    else:
        unreadable = None
    listed = pages if args.all else pages[:PAGES_SHOWN]
    if args.json:
        summary["page_list"] = None if unreadable else listed
        summary["page_list_truncated"] = len(listed) < len(pages)  # --all lists every page
        print(json.dumps(summary, indent=2, sort_keys=True))
        return fail(unreadable) if unreadable else EXIT_OK
    labels = [("id", "Session"), ("created", "Created"), ("last_activity", "Last activity"),
              ("pages", "Pages"), ("records", "Ledger records"), ("active_records", "Active records"),
              ("open_obligations", "Open obligations"), ("log_events", "Log events"),
              ("over_bound", "Over-bound turns")]
    for key, label in labels:
        value = summary[key]
        print(f"{label}: {'?' if value is None else value}")
    print(f"Upstream 4xx: {summary['upstream_4xx']} ({summary['upstream_4xx_model']} on model requests)")
    last_epoch = summary["last_epoch"]
    print(f"Last epoch: {json.dumps(last_epoch, sort_keys=True) if last_epoch else 'none logged'}")
    problems = summary["ledger_problems"]
    print(f"Ledger: {'intact' if not problems else '; '.join(problems)}")
    if unreadable:
        return fail(unreadable)
    if not pages:
        print("Pages, newest first: none (no turns paged out yet)")
        return EXIT_OK
    print("Pages, newest first:")
    for page in listed:
        print(f"  {page['line']}")
    if len(pages) > len(listed):
        print(f"  ({len(pages) - len(listed)} older pages not listed: add --all, or find one with contexto search)")
    return EXIT_OK
