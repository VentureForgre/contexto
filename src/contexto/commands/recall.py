"""``contexto recall <page id>``: print a paged-out turn exactly as ``contexto_recall`` renders it.

``--json`` prints the same chunk as one JSON object, with where to read on from (D64)."""

from __future__ import annotations

import argparse
import json
import sys

from contexto.commands._common import EXIT_OK, add_session_option, fail, say_session
from contexto.paths import contexto_home
from contexto.store import PageUnreadableError, SessionNotFoundError, SessionStore, check_page_id
from contexto.store.render import RECALL_LIMIT, recall_chunk, recall_limit
from contexto.store.sessions import select_session

PAGE_ID_FORM = ("page ids are pg_ and 16 hex digits, as `contexto sessions show` and `contexto search` "
                "print them")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "recall", help="print a paged-out turn",
        description=f"Print a paged-out turn as plain text, thinking omitted, at most {RECALL_LIMIT:,} "
                    "characters at a time and about a quarter of the session's window budget (the same "
                    "rendering and limit as the MCP contexto_recall tool). `contexto sessions show` lists "
                    "the page ids; `contexto search` finds them with the offsets of their matching lines. "
                    "With --json, one object: session, page_id, offset, text, total_chars (the whole turn's "
                    "length) and next_offset (the --offset that reads on, null at the end).")
    parser.add_argument("page_id", metavar="PAGE_ID",
                        help="the page to print, for example pg_0123456789abcdef")
    add_session_option(parser)
    parser.add_argument("--offset", type=int, default=0, metavar="N",
                        help="character offset to continue from, or a matching line's offset from "
                             "`contexto search` (default 0)")
    parser.add_argument("--json", action="store_true", help="print JSON; an error is still one line on stderr")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    home = contexto_home()
    try:
        session_id, how = select_session(home, args.session)
    except SessionNotFoundError as e:
        return fail(str(e))
    say_session(session_id, how)
    try:
        check_page_id(args.page_id)
    except ValueError:
        return fail(f"no page {args.page_id[:40]!r} in session {session_id}: {PAGE_ID_FORM}")
    store = SessionStore.open_existing(home, session_id)
    try:
        page = store.get_page(args.page_id)
    except PageUnreadableError as e:
        cause = "its stored file is damaged" if e.damaged else "reading its stored file failed"
        return fail(f"{e} in session {session_id}: {cause} (logged page_unreadable)")
    if page is None:
        return fail(f"no page {args.page_id!r} in session {session_id}")
    limit, note = recall_limit(*store.window())
    try:
        chunk = recall_chunk(page, args.offset, limit)
    except ValueError as e:
        return fail(str(e))
    if args.json:  # next_offset says what the note below says
        print(json.dumps({"session": session_id, "page_id": args.page_id, **chunk}, indent=2, sort_keys=True))
        return EXIT_OK
    sys.stdout.write(chunk["text"])
    if chunk["text"] and not chunk["text"].endswith("\n"):
        sys.stdout.write("\n")
    if chunk["next_offset"] is not None:
        rest = chunk["total_chars"] - chunk["next_offset"]
        print(f"[{rest} more characters: rerun with --offset {chunk['next_offset']}; {note}]", file=sys.stderr)
    return EXIT_OK
