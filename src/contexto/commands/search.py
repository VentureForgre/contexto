"""``contexto search WORDS``: find paged-out turns with the search the MCP ``contexto_search`` tool uses (D57).

Pages holding every word, best first (the same ranking as the tool, D34). Each hit is its index line
(page id, turn, opening user text) and up to three matching lines, each with the character offset that
``contexto recall PAGE_ID --offset N`` reads on from. Only paged-out turns are searched: the
conversation still in the CLI's view is not in the store. ``--json`` prints the same hits as one JSON
object (D64).

When no page holds every word, the command prints approximate matches instead, as the tool does
(D65, ``contexto.store.search.rank_approximate``): a note on stderr says so, followed by the words that
were searched when some were dropped, and under each page's index line a line names the words the
page matched and the ones it did not. With ``--json`` the object then has ``approximate`` and
``searched``, and each hit ``matched`` and ``missing``; a strict result's object is as D64 made it.
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
from contexto.store.search import ApproxHit, Plan, Query, hit_view, hit_words, text_matches
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
                    "approximate matches are printed with a note, each with the words it matched. With --json, "
                    "one object: session, "
                    "query, total (every matching page) and hits (at most --limit), each with page_id, turn, "
                    "line (its index line), matches (the lines shown: offset, text, and match, false for a "
                    "line of context; for a long line text is an excerpt and offset is where the excerpt "
                    "starts), "
                    "matching_lines (how many lines match in the whole turn) and "
                    "first_match_offset (null when only the index line matches). Approximate matches add "
                    "approximate (true) and searched (the words searched for), and to each hit matched "
                    "(word as typed, how: exact, folded or typo, and form as the page writes it) and missing "
                    "(the words it does not hold).")
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
    if args.json:  # the notes below are `total` and an empty `hits` here
        hits = [hit_json(page_id, page, query, (approximate[page_id], plan) if approximate else None)
                for page_id, page in readable(store, found, args.limit)]
        data = {"session": session_id, "query": query, "total": total if approximate else len(found), "hits": hits}
        if approximate:
            data.update(approximate=True, searched=list(plan.typed) if plan is not None else [])
        print(json.dumps(data, indent=2, sort_keys=True))
        return EXIT_OK
    nothing = f"no paged-out turn in session {session_id} contains every word of {query!r}"
    shown = 0
    for page_id, page in readable(store, found, args.limit):
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


def _matching(text: str, query: str, approximate: tuple[ApproxHit, Plan | None] | None) -> Query | None:
    """The query that picks a page's matching lines: the words as typed for a strict hit, the forms the page
    holds for an approximate one (D65); None when the match is in the page's stub only."""
    if approximate is not None:
        return hit_words(approximate[0], text, approximate[1])[2]
    q = Query.parse(query)
    return q if q is not None and text_matches(text, q) else None


def hit_json(page_id: str, page: dict, query: str,
             approximate: tuple[ApproxHit, Plan | None] | None = None) -> dict:
    """One hit for ``--json``: what ``hit_lines`` prints, as fields. ``first_match_offset`` is where
    ``contexto recall --offset`` starts to read every matching line, shown or not. An approximate hit
    (D65) also has ``matched`` and ``missing``, the words of ``hit_lines``' extra line."""
    text = render_page(page)
    q = _matching(text, query, approximate)
    rows, total = match_lines(text, q, last=True) if q is not None else ([], 0)
    hit = {"page_id": page_id, "turn": page.get("turn"), "line": index_line(current_stub(page)),
           "matches": [{"offset": start, "text": line, "match": is_match} for start, _, is_match, line in rows],
           "matching_lines": total, "first_match_offset": matching_line_starts(text, q)[0] if total else None}
    if approximate is not None:
        matched, missing, _ = hit_words(approximate[0], text, approximate[1])
        hit.update(matched=[{"word": word, "how": how, "form": form} for word, how, form in matched],
                   missing=missing)
    return hit


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
