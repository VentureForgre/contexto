"""``contexto recall <page id>``: print a paged-out turn exactly as ``contexto_recall`` renders it.

``--json`` prints the same chunk as one JSON object, with where to read on from (D64).

``--from SESSION`` (D69) prints a page of an earlier session of the session's project, as the tool does
with ``session``: the same sessions, named the same way, read without changing them.
"""

from __future__ import annotations

import argparse
import json
import sys

from contexto.commands._common import EXIT_OK, add_session_option, fail, say_session
from contexto.paths import contexto_home
from contexto.store import PageUnreadableError, SessionNotFoundError, SessionStore, check_page_id
from contexto.store.project import NotEarlierSessionError
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
    parser.add_argument("--from", dest="origin", metavar="SESSION",
                        help="print the page from an earlier session of the session's project: its id, the "
                             "label `contexto search --project` shows, or any start of its id no other "
                             "earlier session shares. That session is only read.")
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
    earlier = None  # the earlier session the page is read from; the session's own id means the session
    if args.origin is not None and args.origin != session_id:
        try:
            earlier = store.earlier_session(args.origin)
        except NotEarlierSessionError as e:
            if store.project() is None:
                return fail(f"session {session_id} is not in a project, or project memory is off for it, so it "
                            "has no earlier session to read from")
            return fail(f"{args.origin[:40]!r} is {e}; `contexto search --project` shows their labels")
        print(f"from earlier session {earlier.id} ({earlier.label}, {earlier.date})", file=sys.stderr)
    where = f"earlier session {earlier.id}" if earlier else f"session {session_id}"
    try:
        page = store.earlier_page(earlier, args.page_id) if earlier else store.get_page(args.page_id)
    except PageUnreadableError as e:
        cause = "its stored file is damaged" if e.damaged else "reading its stored file failed"
        # A page of the session itself is logged by its store; nothing is written to an earlier session.
        return fail(f"{e} in {where}: {cause}" + ("" if earlier else " (logged page_unreadable)"))
    if page is None:
        return fail(f"no page {args.page_id!r} in {where}")
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
