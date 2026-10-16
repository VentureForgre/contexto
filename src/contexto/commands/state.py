"""``contexto state``: print the session's full pinned state, without the budget cap."""

from __future__ import annotations

import argparse

from contexto.commands._common import EXIT_OK, add_session_option, fail, say_session
from contexto.paths import contexto_home
from contexto.store import SessionNotFoundError, SessionStore
from contexto.store.ledger import LedgerError
from contexto.store.sessions import select_session


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "state", help="print the full pinned state",
        description="Print the session's full pinned state without the budget cap (the same text as "
                    "the MCP contexto_state tool).")
    add_session_option(parser)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    home = contexto_home()
    try:
        session_id, how = select_session(home, args.session)
    except SessionNotFoundError as e:
        return fail(str(e))
    say_session(session_id, how)
    try:
        print(SessionStore.open_existing(home, session_id).state_text())
    except LedgerError as e:
        return fail(f"cannot read the ledger of session {session_id}: {e}")
    return EXIT_OK
