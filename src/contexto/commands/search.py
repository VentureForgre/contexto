"""``contexto search WORDS``: find paged-out turns with the search the MCP ``contexto_search`` tool uses (D57).

Pages holding every word, best first (the same ranking as the tool, D34). Each hit is its index line
(page id, turn, opening user text) and up to three matching lines, each with the character offset that
``contexto recall PAGE_ID --offset N`` reads on from. Only paged-out turns are searched: the
conversation still in the CLI's view is not in the store. ``--json`` prints the same hits as one JSON
object (D64).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator

from contexto.commands._common import EXIT_OK, add_session_option, fail, say_session
from contexto.paths import contexto_home
from contexto.store import PageUnreadableError, SessionNotFoundError, SessionStore
from contexto.store.pages import current_stub, index_line
from contexto.store.render import MATCHES_PER_PAGE, match_lines, matching_line_starts, render_page
from contexto.store.search import Query, text_matches
from contexto.store.sessions import select_session

DEFAULT_LIMIT = 10


def _limit(text: str) -> int:
    if not (text.isascii() and text.isdecimal()) or int(text) == 0:
        raise argparse.ArgumentTypeError(f"invalid count {text!r}: use a whole number, 1 or more")
    return int(text)


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "search", help="find paged-out turns by their words",
        description="Find the paged-out turns that hold every word, best match first, with the search the "
                    "MCP contexto_search tool uses. Each hit shows its page id, turn, and opening text, then "
                    "its matching lines with their offsets: `contexto recall PAGE_ID --offset N` prints the "
                    "turn from there. Only paged-out turns are searched. With --json, one object: session, "
                    "query, total (every matching page) and hits (at most --limit), each with page_id, turn, "
                    "line (its index line), matches (the lines shown: offset, text, and match, false for a "
                    "line of context; for a long line text is an excerpt and offset is where the excerpt "
                    "starts), "
                    "matching_lines (how many lines match in the whole turn) and "
                    "first_match_offset (null when only the index line matches).")
    parser.add_argument("words", nargs="+", metavar="WORDS", help="words to find, in any order and case")
    add_session_option(parser)
    parser.add_argument("--limit", type=_limit, default=DEFAULT_LIMIT, metavar="N",
                        help=f"show at most N matching pages (default {DEFAULT_LIMIT})")
    parser.add_argument("--json", action="store_true",
                        help="print JSON; no match is an empty hits list, and an error is still one line on stderr")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    home = contexto_home()
    try:
        session_id, how = select_session(home, args.session)
    except SessionNotFoundError as e:
        return fail(str(e))
    say_session(session_id, how)
    query = " ".join(args.words)
    store = SessionStore.open_existing(home, session_id)
    try:
        found = store.search(query)
    except OSError as e:  # the pages directory itself cannot be read
        return fail(f"the pages of session {session_id} cannot be read: {e}")
    if args.json:  # the notes below are `total` and an empty `hits` here
        hits = [hit_json(page_id, page, query) for page_id, page in readable(store, found, args.limit)]
        print(json.dumps({"session": session_id, "query": query, "total": len(found), "hits": hits},
                         indent=2, sort_keys=True))
        return EXIT_OK
    shown = 0
    for page_id, page in readable(store, found, args.limit):
        if shown:
            print()
        print("\n".join(hit_lines(page_id, page, query, session_id)))
        shown += 1
    if not shown:
        print(f"no paged-out turn in session {session_id} contains every word of {query!r}", file=sys.stderr)
    elif len(found) > shown:
        rest = len(found) - shown
        print(f"[{rest} more matching page{'' if rest == 1 else 's'}: rerun with --limit {len(found)}]",
              file=sys.stderr)
    return EXIT_OK


def readable(store: SessionStore, found: list[str], limit: int) -> Iterator[tuple[str, dict]]:
    """The first ``limit`` pages of ``found`` that can still be read, in order."""
    shown = 0
    for page_id in found:
        if shown == limit:
            return
        try:
            page = store.get_page(page_id)
        except PageUnreadableError:  # logged by the store; search itself skips damaged pages
            page = None
        if page is None:  # removed or damaged since the search listed it
            continue
        shown += 1
        yield page_id, page


def hit_json(page_id: str, page: dict, query: str) -> dict:
    """One hit for ``--json``: what ``hit_lines`` prints, as fields. ``first_match_offset`` is where
    ``contexto recall --offset`` starts to read every matching line, shown or not."""
    text = render_page(page)
    q = Query.parse(query)
    rows, total = match_lines(text, q, last=True) if q is not None and text_matches(text, q) else ([], 0)
    return {"page_id": page_id, "turn": page.get("turn"), "line": index_line(current_stub(page)),
            "matches": [{"offset": start, "text": line, "match": is_match} for start, _, is_match, line in rows],
            "matching_lines": total, "first_match_offset": matching_line_starts(text, q)[0] if total else None}


def hit_lines(page_id: str, page: dict, query: str, session_id: str) -> list[str]:
    """The page's index line, then its matching lines (``>``) and their context, each with its offset."""
    lines = [index_line(current_stub(page))]
    text = render_page(page)
    q = Query.parse(query)
    if q is None or not text_matches(text, q):  # the words are in the page's stub only
        return lines
    rows, total = match_lines(text, q, last=True)  # a turn's latest statements, as the MCP tool shows them
    lines += [f"  {'>' if is_match else ' '} {start}: {line}" for start, _, is_match, line in rows]
    if total > MATCHES_PER_PAGE:
        first = matching_line_starts(text, q)[0]
        lines.append(f"  [{total - MATCHES_PER_PAGE} more matching lines: contexto recall {page_id} "
                     f"--session {session_id} --offset {first}]")
    return lines
