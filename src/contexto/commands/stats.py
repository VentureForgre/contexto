"""``contexto stats [SESSION]``: what a session did, as numbers (D64).

The peak prompt against the bound, the turns paged out, how often the model read them back, the share
of input read from cache, the bytes the CLI sent against the bytes forwarded, whether Claude Code
compacted, and anything that went wrong. ``--json`` prints the same numbers in the stable shape
docs/stats.md defines. Nothing is written, and only this machine's files are read.
"""

from __future__ import annotations

import argparse
import json
import sys

from contexto.commands._common import EXIT_OK, EXIT_USAGE, SESSION_HELP, add_session_option, fail, say_session
from contexto.paths import contexto_home
from contexto.stats import duration_text, render, session_stats
from contexto.store import SessionNotFoundError
from contexto.store.sessions import select_session


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "stats", help="show what a session did: peak prompt, turns paged, recalls, cache, faults",
        description="Show one session's numbers, read from its log and its store: the largest prompt against "
                    "the bound, requests over the bound, turns paged out and tokens kept on disk, recall and "
                    "search calls, the share of input read from cache, bytes sent against bytes forwarded, "
                    "whether Claude Code compacted, and faults with what to do about them. A line is shown "
                    "only when the log holds its data. docs/stats.md defines each number.")
    parser.add_argument("session_id", metavar="SESSION", nargs="?", default=None, help=SESSION_HELP)
    add_session_option(parser)
    parser.add_argument("--last-run", action="store_true",
                        help="count only the most recent `contexto run` of the session (what its exit summary showed)")
    parser.add_argument("--json", action="store_true", help="print JSON in the shape docs/stats.md documents")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.session_id is not None and args.session is not None:
        print("contexto stats: name the session as SESSION or with --session, not both", file=sys.stderr)
        return EXIT_USAGE
    home = contexto_home()
    try:
        session_id, how = select_session(home, args.session_id if args.session_id is not None else args.session)
        out = session_stats(home, session_id, last_run=args.last_run)
    except SessionNotFoundError as e:
        return fail(str(e))
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return EXIT_OK
    say_session(session_id, "SESSION" if args.session_id is not None and how == "--session" else how)
    runs = f"{out['runs']:,} run{'' if out['runs'] == 1 else 's'}"
    scope = f"the latest of {runs}" if args.last_run else f"the whole session ({runs}"
    span, in_runs = out["span_seconds"], out["run_seconds"]
    if span is None:
        span = "no log"
    elif args.last_run or in_runs is None or in_runs == span:
        span = f"{duration_text(span)} of log"
    else:  # runs days apart: the time in runs, and the span of the log, which is not time spent working
        span = f"{duration_text(in_runs)} in runs, {duration_text(span)} from the first log line to the last"
    print(f"Contexto stats for session {session_id}: {scope}{', ' if not args.last_run else ' ('}{span})")
    for line in render(out):
        print(line)
    return EXIT_OK
