"""Helpers shared by the store-reading subcommands (not a subcommand: the leading underscore skips discovery)."""

from __future__ import annotations

import sys

EXIT_OK = 0
EXIT_ERROR = 1  # unknown session or page, bad offset, unreadable store
# Exit code 2 is argparse's usage error.

SESSION_HELP = "session id, or a unique prefix of one"


def fail(message: str) -> int:
    print(f"contexto: error: {message}", file=sys.stderr)
    return EXIT_ERROR


def add_session_option(parser) -> None:
    parser.add_argument("--session", metavar="ID",
                        help=f"{SESSION_HELP} (default: $CONTEXTO_SESSION, else the most recently active "
                             "session with pages or pins)")


def say_session(session_id: str, how: str) -> None:
    print(f"session {session_id} ({how})", file=sys.stderr)
