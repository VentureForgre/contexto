"""``contexto search WORDS``: find paged-out turns with the search the MCP ``contexto_search`` tool uses (D57).

Pages holding every word, best first (the same ranking as the tool, D34). Each hit is its index line
(page id, turn, opening user text) and up to three matching lines, each with the character offset that
``contexto recall PAGE_ID --offset N`` reads on from. Only paged-out turns are searched: the
conversation still in the CLI's view is not in the store. ``--json`` prints the same hits as one JSON
object (D64).

``--project`` (D69) also searches the earlier sessions of the session's project, as the tool does with
``scope: "project"``: the same sessions, under the same labels, read without changing them.
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
from contexto.store.project import EARLIER_HITS, EARLIER_SESSIONS, EarlierSearch, InViewLine
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
                    "first_match_offset (null when only the index line matches). With --project too, "
                    "earlier: null when the session has no project view, else searched, skipped, "
                    "not_searched, in_view_skipped and sessions, each with session (its id), label (what "
                    "--from takes), date, total (its matches), in_view (lines in view when it ended: turn "
                    "and text; on no page, so not recallable) and hits (its best pages, as above).")
    parser.add_argument("words", nargs="+", metavar="WORDS", help="words to find, in any order and case")
    add_session_option(parser)
    parser.add_argument("--limit", type=_limit, default=DEFAULT_LIMIT, metavar="N",
                        help=f"show at most N matching pages (default {DEFAULT_LIMIT})")
    parser.add_argument("--json", action="store_true",
                        help="print JSON; no match is an empty hits list, and an error is still one line on stderr")
    parser.add_argument("--project", action="store_true",
                        help=f"also search the earlier sessions of the session's project (the newest "
                             f"{EARLIER_SESSIONS}), read-only: the best {EARLIER_HITS} matches of each, under a "
                             "line with the session's label and date, lines still in view when it ended first; "
                             "`contexto recall PAGE_ID --from LABEL` prints a page")
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
        out = {"session": session_id, "query": query, "total": len(found), "hits": hits}
        if args.project:
            out["earlier"] = earlier_json(store.search_earlier(query), query)
        print(json.dumps(out, indent=2, sort_keys=True))
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
    if args.project:
        search_earlier(store, query, bool(shown))
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


def search_earlier(store: SessionStore, query: str, after_hits: bool) -> None:
    """Print what the earlier sessions of the session's project hold of ``query`` (D69): for each with a
    match, newest first, a line with its label and date as the MCP tool shows them, then its lines still
    in view when it ended and its best pages. Those sessions are only read: nothing is locked, logged or
    created in them."""
    session_id = store.session_id
    found = store.search_earlier(query)
    if found is None:
        print(f"session {session_id} is not in a project, or project memory is off for it: only this session "
              "was searched", file=sys.stderr)
        return
    for hits in found.hits:
        if after_hits:
            print()
        count, total = len(hits.pages) + len(hits.in_view), hits.found + hits.in_view_found
        best = "" if total <= count else (f", the best {count} of {total} matches" if hits.in_view_found
                                          else f", the best {count} of {total} matching pages")
        print(f"[session {hits.label}, {hits.date}{best}:]")
        for line in hits.in_view:
            print(f"> in view when it ended (turn {shown_turn(line)}): {line.line}")
        if hits.pages:
            if hits.in_view:
                print()
            print("\n\n".join("\n".join(hit_lines(page_id, page, query, session_id, hits.label))
                              for page_id, page in hits.pages))
        after_hits = True
    if not found.hits:
        print(f"no earlier session of this project contains every word of {query!r}", file=sys.stderr)
    if any(hits.in_view for hits in found.hits):
        print("[a line in view when its session ended is on no page: contexto recall cannot print it]",
              file=sys.stderr)
    if any(hits.pages for hits in found.hits):
        print(f"[to print a page of an earlier session: contexto recall PAGE_ID --session {session_id} "
              "--from LABEL --offset N]", file=sys.stderr)
    if found.not_searched:
        print(f"[{found.not_searched} older session{'' if found.not_searched == 1 else 's'} of this project not "
              "searched]", file=sys.stderr)
    if found.skipped:
        print(f"[{found.skipped} earlier session{'' if found.skipped == 1 else 's'} could not be read]",
              file=sys.stderr)


def shown_turn(line: InViewLine) -> int:
    """The turn of a line in view as search shows it: the page numbering (from 0), as the MCP tool does."""
    return max(0, line.turn - 1)


def earlier_json(found: EarlierSearch | None, query: str) -> dict | None:
    """The earlier sessions' part for ``--json`` (D69): what ``search_earlier`` prints, as fields; None
    when the session has no project view. ``label`` is the session's label itself (what ``--from``
    takes), not its escaped form the text shows."""
    if found is None:
        return None
    return {"searched": found.searched, "skipped": found.skipped, "not_searched": found.not_searched,
            "in_view_skipped": found.in_view_skipped,
            "sessions": [{"session": hits.session, "label": json.loads(f'"{hits.label}"'), "date": hits.date,
                          "total": hits.found + hits.in_view_found,
                          "in_view": [{"turn": shown_turn(line), "text": line.line} for line in hits.in_view],
                          "hits": [hit_json(page_id, page, query) for page_id, page in hits.pages]}
                         for hits in found.hits]}


def hit_lines(page_id: str, page: dict, query: str, session_id: str, earlier: str | None = None) -> list[str]:
    """The page's index line, then its matching lines (``>``) and their context, each with its offset.
    ``earlier`` is the label of the earlier session the page is in, when it is not ``session_id``'s."""
    lines = [index_line(current_stub(page))]
    text = render_page(page)
    q = Query.parse(query)
    if q is None or not text_matches(text, q):  # the words are in the page's stub only
        return lines
    rows, total = match_lines(text, q, last=True)  # a turn's latest statements, as the MCP tool shows them
    lines += [f"  {'>' if is_match else ' '} {start}: {line}" for start, _, is_match, line in rows]
    if total > MATCHES_PER_PAGE:
        first = matching_line_starts(text, q)[0]
        origin = f" --from {earlier}" if earlier else ""
        lines.append(f"  [{total - MATCHES_PER_PAGE} more matching lines: contexto recall {page_id} "
                     f"--session {session_id}{origin} --offset {first}]")
    return lines
