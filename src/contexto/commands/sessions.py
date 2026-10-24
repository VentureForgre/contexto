"""``contexto sessions list`` and ``contexto sessions show <id>``."""

from __future__ import annotations

import argparse
import json

from contexto.commands._common import EXIT_OK, fail
from contexto.paths import contexto_home
from contexto.store import SessionNotFoundError
from contexto.store.sessions import list_sessions, session_summary


def register(subparsers) -> None:
    parser = subparsers.add_parser("sessions", help="list and inspect sessions",
                                   description="List and inspect sessions under $CONTEXTO_HOME.")
    sub = parser.add_subparsers(title="sessions commands", metavar="<list|show>", required=True)
    ls = sub.add_parser("list", help="list sessions, most recent activity first")
    ls.add_argument("--json", action="store_true", help="print JSON")
    ls.set_defaults(func=run_list)
    show = sub.add_parser("show", help="summarize one session")
    show.add_argument("session_id", metavar="ID")
    show.add_argument("--json", action="store_true", help="print JSON")
    show.set_defaults(func=run_show)


def run_list(args: argparse.Namespace) -> int:
    home = contexto_home()
    sessions = list_sessions(home)
    if args.json:
        print(json.dumps([vars(s) for s in sessions], indent=2, sort_keys=True))
        return EXIT_OK
    if not sessions:
        print(f"No sessions under {home / 'sessions'}.")
        return EXIT_OK
    rows = [("ID", "CREATED", "PAGES", "RECORDS", "LAST ACTIVITY")]
    rows += [(s.id, s.created, str(s.pages), "?" if s.records is None else str(s.records), s.last_activity)
             for s in sessions]
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return EXIT_OK


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
    last_epoch = summary["last_epoch"]
    print(f"Last epoch: {json.dumps(last_epoch, sort_keys=True) if last_epoch else 'none logged'}")
    problems = summary["ledger_problems"]
    print(f"Ledger: {'intact' if not problems else '; '.join(problems)}")
    return EXIT_OK
