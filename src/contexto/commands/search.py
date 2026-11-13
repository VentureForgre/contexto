"""``contexto search WORDS``: find paged-out turns with the search the MCP ``contexto_search`` tool uses (D57).

Pages holding every word, best first (the same ranking as the tool, D34). Each hit is its index line
(page id, turn, opening user text) and up to three matching lines, each with the character offset that
``contexto recall PAGE_ID --offset N`` reads on from. Only paged-out turns are searched: the
conversation still in the CLI's view is not in the store.

When no page holds every word, the command prints approximate matches instead, as the tool does
(D65, ``contexto.store.search.rank_approximate``): a note on stderr says so, followed by the words that
were searched when some were dropped, and under each page's index line a line names the words the
page matched and the ones it did not.
"""

from __future__ import annotations

import argparse
import sys

from contexto.commands._common import EXIT_OK, add_session_option, fail, say_session
from contexto.paths import contexto_home
from contexto.store import PageUnreadableError, SessionNotFoundError, SessionStore
from contexto.store.pages import current_stub, index_line
from contexto.store.render import MATCHES_PER_PAGE, match_lines, matching_line_starts, render_page
from contexto.store.search import ApproxHit, Plan, Query, hit_view, text_matches
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
                    "turn from there. Only paged-out turns are searched. When no turn holds every word, "
                    "approximate matches are printed with a note, each with the words it matched.")
    parser.add_argument("words", nargs="+", metavar="WORDS", help="words to find, in any order and case")
    add_session_option(parser)
    parser.add_argument("--limit", type=_limit, default=DEFAULT_LIMIT, metavar="N",
                        help=f"show at most N matching pages (default {DEFAULT_LIMIT})")
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
    approximate: dict[str, ApproxHit] = {}
    plan = None
    try:
        found = store.search(query)
        if not found:  # no page holds every word: approximate matches, said to be (D65)
            result = store.search_approximate(query)
            approximate, plan = {hit.id: hit for hit in result}, result.plan
            found, total = list(approximate), result.total
    except OSError as e:  # the pages directory itself cannot be read
        return fail(f"the pages of session {session_id} cannot be read: {e}")
    nothing = f"no paged-out turn in session {session_id} contains every word of {query!r}"
    shown = 0
    for page_id in found:
        if shown == args.limit:
            break
        try:
            page = store.get_page(page_id)
        except PageUnreadableError:  # logged by the store; search itself skips damaged pages
            page = None
        if page is None:  # removed or damaged since the search listed it
            continue
        if shown:
            print()
        elif approximate:
            print(f"{nothing}; approximate matches, best first:", file=sys.stderr)
            if plan is not None and plan.searched():  # which words were searched, as the tool's reply says
                print(plan.searched(), file=sys.stderr)
        print("\n".join(hit_lines(page_id, page, query, session_id,
                                  (approximate[page_id], plan) if approximate else None)))
        shown += 1
    if not shown:
        print(nothing, file=sys.stderr)
    elif approximate:
        rest = total - shown
        if rest > 0 and total <= len(found):
            print(f"[{rest} more approximate match{'' if rest == 1 else 'es'}: rerun with --limit {total}]",
                  file=sys.stderr)
        elif rest > 0:  # more pages matched than search_approximate returns
            listable = f"rerun with --limit {len(found)} for {len(found) - shown} of them, and " \
                if len(found) > shown else ""
            print(f"[{rest} more approximate matches: {listable}use more specific words]", file=sys.stderr)
    elif len(found) > shown:
        rest = len(found) - shown
        print(f"[{rest} more matching page{'' if rest == 1 else 's'}: rerun with --limit {len(found)}]",
              file=sys.stderr)
    return EXIT_OK


def hit_lines(page_id: str, page: dict, query: str, session_id: str,
              approximate: tuple[ApproxHit, Plan | None] | None = None) -> list[str]:
    """The page's index line, then its matching lines (``>``) and their context, each with its offset.
    An approximate hit (D65) has a line under the index line naming the words the page matched and the
    ones it did not, and its matching lines are chosen by the forms found on the page."""
    lines = [index_line(current_stub(page))]
    text = render_page(page)
    if approximate is None:
        q = Query.parse(query)
        if q is None or not text_matches(text, q):  # the words are in the page's stub only
            return lines
    else:
        words, q = hit_view(approximate[0], text, approximate[1])
        lines.append("  " + words)
        if q is None:
            return lines
    rows, total = match_lines(text, q, last=True)  # a turn's latest statements, as the MCP tool shows them
    lines += [f"  {'>' if is_match else ' '} {start}: {line}" for start, _, is_match, line in rows]
    if total > MATCHES_PER_PAGE:
        first = matching_line_starts(text, q)[0]
        lines.append(f"  [{total - MATCHES_PER_PAGE} more matching lines: contexto recall {page_id} "
                     f"--session {session_id} --offset {first}]")
    return lines
