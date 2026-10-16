"""``contexto recall <page id>``: print a paged-out turn exactly as ``contexto_recall`` renders it."""

from __future__ import annotations

import argparse
import sys

from contexto.commands._common import EXIT_OK, add_session_option, fail, say_session
from contexto.paths import contexto_home
from contexto.store import SessionNotFoundError, SessionStore
from contexto.store.render import RECALL_LIMIT, recall_chunk
from contexto.store.sessions import select_session


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "recall", help="print a paged-out turn",
        description=f"Print a paged-out turn as plain text, thinking omitted, {RECALL_LIMIT:,} characters "
                    "at a time (the same rendering as the MCP contexto_recall tool).")
    parser.add_argument("page_id", metavar="PAGE_ID")
    add_session_option(parser)
    parser.add_argument("--offset", type=int, default=0, metavar="N",
                        help="character offset to continue from (default 0)")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    home = contexto_home()
    try:
        session_id, how = select_session(home, args.session)
    except SessionNotFoundError as e:
        return fail(str(e))
    say_session(session_id, how)
    page = SessionStore.open_existing(home, session_id).get_page(args.page_id)
    if page is None:
        return fail(f"no page {args.page_id!r} in session {session_id}")
    try:
        chunk = recall_chunk(page, args.offset)
    except ValueError as e:
        return fail(str(e))
    sys.stdout.write(chunk["text"])
    if chunk["text"] and not chunk["text"].endswith("\n"):
        sys.stdout.write("\n")
    if chunk["next_offset"] is not None:
        rest = chunk["total_chars"] - chunk["next_offset"]
        print(f"[{rest} more characters: rerun with --offset {chunk['next_offset']}]", file=sys.stderr)
    return EXIT_OK
