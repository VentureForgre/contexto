"""``contexto state``: the session's full pinned state, without the budget cap.

By default for a person (D57): the pinned records by group, or "No pinned records.", then how many
turns are paged out and the commands that list and search them. ``--raw`` prints the text the MCP
``contexto_state`` tool gives the model, whose opening lines are instructions to the model.
"""

from __future__ import annotations

import argparse
import os

from contexto.commands._common import EXIT_OK, add_session_option, fail, say_session
from contexto.paths import contexto_home
from contexto.store import SessionNotFoundError, SessionStore
from contexto.store.ledger import LedgerError
from contexto.store.pinned import render_state
from contexto.store.sessions import select_session


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "state", help="print the full pinned state",
        description="Print the decisions, open obligations, and verified facts the model pinned in a "
                    "session, with no budget cap, and how many turns are paged out.")
    add_session_option(parser)
    parser.add_argument("--raw", action="store_true",
                        help="print exactly what the MCP contexto_state tool returns to the model, including "
                             "its instructions to the model")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    home = contexto_home()
    try:
        session_id, how = select_session(home, args.session)
    except SessionNotFoundError as e:
        return fail(str(e))
    say_session(session_id, how)
    store = SessionStore.open_existing(home, session_id)
    try:
        if args.raw:
            print(store.state_text())
            return EXIT_OK
        root = store.workdir()
        pinned = render_state(store.state(root), staleness_checked=root is not None, opening=())
    except LedgerError as e:
        return fail(f"cannot read the ledger of session {session_id}: {e}")
    print(pinned or "No pinned records.")
    print()
    try:
        pages = page_count(store)
    except OSError as e:  # the pins stand; the page count is reported in one line
        return fail(f"the pages of session {session_id} cannot be read: {e}")
    if pages:
        print(f"{pages} turn{'' if pages == 1 else 's'} paged out: list them with "
              f"`contexto sessions show {session_id}`, search them with "
              f"`contexto search --session {session_id} WORDS`.")
    else:
        print("No turns paged out yet.")
    return EXIT_OK


def page_count(store: SessionStore) -> int:
    """Page files in the store (no page is read); OSError when the pages directory cannot be listed."""
    try:
        names = os.listdir(store.pages_dir)
    except FileNotFoundError:
        return 0
    return sum(1 for n in names if n.startswith("pg_") and n.endswith(".json") and (store.pages_dir / n).is_file())
