"""How ``contexto_search`` matches and ranks pages, and which lines of a page it shows (D34).

Matching. A query is split on whitespace into words; a query with no words (empty or whitespace
only) matches nothing. A page matches when every word occurs in its searchable text (the stub and
every text of its turn, as before), case-insensitively, in any order. A word is matched as a
substring, as the whole query was before, so an identifier, a path, or part of one (``E4127``,
``src/contexto/mcp.py``, ``TIMEOUT`` in ``HTTP_TIMEOUT_S``) finds the same pages it found before:
for any query with a word, every page the exact, case-sensitive rule matched still matches. Case is
folded one character at a time (``fold``), so a word folds the same in the query and on the page.

Ranking. Pages fall in three groups, in this order: turn pages that hold the whole query as typed
(case-insensitively; for one word, every match), turn pages that hold only its words, and pages that
hold only paged context blocks (``<system-reminder>`` text the proxy paged on its own, PR #203),
which repeat the same words at every turn and would otherwise outrank the turn that is about them;
they stay searchable and recallable. Within each group a page's relevance is BM25 over the query's
words, plus the whole query as one more term when it has more than one word: term frequency
saturates (``K1``), long pages are discounted (``B``; length in UTF-8 bytes), and a word that occurs
on fewer pages weighs more (inverse document frequency). Relevance is then multiplied by a recency
prior, ``1 + RECENCY_WEIGHT * r``, where ``r`` is the page's position among the matching pages from
0 (oldest) to 1 (newest). So a newer page wins unless an older one is clearly more about the query:
with ``RECENCY_WEIGHT`` 0.5 the oldest match needs one and a half times the relevance of the newest
to rank above it, and two matches next to each other in time are ordered by relevance almost alone.
Ties go to the newer page. Last, the newest matching turn page (the newest matching page when no turn
page matches) is moved up to the last of the ``SHOWN_IN_DETAIL`` places if it ranked below them: the
turn that states a new value is often a long one, which BM25 discounts.

``contexto_search`` shows the 3 best pages in detail and lists the next 50. In each part turn pages
come first, newest first, so among the turn pages it shows the newest statement comes first (D28)
whatever their scores; context pages follow under their own line, newest first, because a context
page's turn is when the block was paged, not when it was sent (``contexto.mcp._newest_first``).

Lines. Within a page (or an in-view message) the matching lines are those that hold the most of the
query: lines holding the whole query if there are any, else lines holding all its words, else the
lines holding the most of its words, a word found on fewer lines of the page counting more.
"""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

K1 = 1.2  # BM25 term-frequency saturation (the usual value)
B = 0.75  # BM25 length normalization (the usual value)
RECENCY_WEIGHT = 0.5  # the newest match's relevance is multiplied by 1.5, the oldest's by 1
SHOWN_IN_DETAIL = 3  # contexto_search shows this many of the best pages in detail (mcp.SEARCH_LIMIT)


def fold(text: str) -> str:
    """``text`` lowercased one character at a time, so offsets into the result are offsets into ``text``.

    Each character is lowered on its own, as if alone, so a word folds the same wherever it occurs.
    ``str.lower`` on a whole string lowers capital sigma by its place in a word (final ``ς``, else
    ``σ``; the only context rule it applies), so every ``ς`` is then written ``σ``: ``ΟΔΟΣ`` matches
    in ``ΟΔΟΣΑ`` and ``Σ`` in both. A few characters lower to two (``İ``); those are kept as they are.
    """
    low = text.lower()
    if len(low) != len(text):
        low = "".join(c if len(c.lower()) != 1 else c.lower() for c in text)
    return low.replace("ς", "σ")


def search_key(folded: str) -> bytes:
    """Folded text as search holds it: UTF-8, so a page costs its UTF-8 size in memory whatever its
    characters (a ``str`` holding one emoji takes 4 bytes per character). UTF-8 is self-synchronising,
    so a substring test on the bytes is a substring test on the text. Lone surrogates, which JSON
    allows, are kept (``surrogatepass``), the same way in the query and in the text."""
    return folded.encode("utf-8", "surrogatepass")


def _escaped(term: str) -> str:
    return json.dumps(term, ensure_ascii=False)[1:-1]


@dataclass(frozen=True)
class Query:
    """A search query: its folded words (distinct, in order) and the folded query as a phrase."""

    phrase: str
    words: tuple[str, ...]

    @classmethod
    def parse(cls, query: str) -> Query | None:
        """None for a query with no words (empty or whitespace only)."""
        if not isinstance(query, str):
            return None
        folded = fold(query)
        words = tuple(dict.fromkeys(folded.split()))
        return cls(folded.strip(), words) if words else None

    @property
    def multiword(self) -> bool:
        return len(self.words) > 1 or self.phrase != self.words[0]

    def terms(self) -> tuple[str, ...]:
        """What BM25 scores: the words, and the phrase as one more term when it differs from the only word."""
        return self.words + ((self.phrase,) if self.multiword else ())


class SearchEntry(NamedTuple):
    """What ``rank`` reads of a page: its id, turn, folded searchable text (``search_key``), and whether
    it holds only paged context blocks (``contexto.store.pages.is_context_page``). ``text`` is None for
    a page whose text is not held in memory; ``rank`` then asks its ``fetch`` for the page (D39)."""

    id: str
    turn: int
    text: bytes | None
    context: bool = False


# A page read from the store costs about as much as 6 substring tests that fail on a 32 KB page (D39,
# measured: 92 us against 15 us at 6,000 synthetic pages). So on a page just read, a query of up to
# this many words tests every word at once, rather than reading the page again for its frequencies.
EAGER_WORDS = 6


def rank(entries: Sequence[SearchEntry], query: str,
         fetch: Callable[[int], SearchEntry | None] | None = None) -> list[str]:
    """Ids of the matching entries, best first (see the module docstring).

    ``entries`` are every page of the store: all of them count for the inverse document frequency and
    the average length, not only the matches. An entry whose ``text`` is None is read through
    ``fetch(i)``, ``i`` its index in ``entries``, which returns the whole entry, or None for a page
    that cannot be read now (left out, as an unreadable page is). Each page is handled in turn and only
    what ranking needs is kept of it (which words it holds, its length, and for a match its term
    counts), so pages read through ``fetch`` are not held for the length of the query (D39).

    Pages are narrowed word by word, so a page is not read further once it lacks a word, and a query
    reads no page twice when no page holds every word. The frequency of each word over the pages it
    was not read on is counted only when some page matches, in a second pass over those pages (a page
    just read through ``fetch`` is tested for every word at once when the query has at most
    ``EAGER_WORDS`` words or a match is already known, so it is rarely read twice).
    """
    q = Query.parse(query)
    if q is None or not entries:
        return []
    words = [search_key(w) for w in q.words]
    count = len(words)
    phrase = search_key(q.phrase)
    terms = words + ([phrase] if q.multiword else [])
    df = [0] * count
    n = total = 0
    matched = []  # (id, turn, context, length, holds the phrase, count of each term)
    pending = []  # (index, the first word not read on the page)
    for i, entry in enumerate(entries):
        text = entry.text
        fetched = text is None
        if fetched:
            entry = fetch(i) if fetch is not None else None
            if entry is None or entry.text is None:
                continue
            text = entry.text
        n += 1
        total += len(text)
        j = 0
        while j < count and words[j] in text:  # `in` stops at the first occurrence
            df[j] += 1
            j += 1
        if j == count:
            on = (phrase in text) if q.multiword else True
            tf = tuple(text.count(t) if t != phrase or on else 0 for t in terms)
            matched.append((entry.id, entry.turn, entry.context, len(text), on, tf))
        elif j + 1 < count:
            if fetched and (count <= EAGER_WORDS or matched):
                for k in range(j + 1, count):
                    df[k] += words[k] in text
            else:
                pending.append((i, j + 1))
    if not matched:
        return []
    for i, first in pending:
        text = entries[i].text
        if text is None:
            entry = fetch(i) if fetch is not None else None
            if entry is None or entry.text is None:
                continue  # gone since the first pass: counted as holding none of the remaining words
            text = entry.text
        for k in range(first, count):
            df[k] += words[k] in text
    freq = dict(zip(words, df))
    if q.multiword:
        freq[phrase] = sum(1 for m in matched if m[4])
    idf = {t: math.log(1 + (n - freq[t] + 0.5) / (freq[t] + 0.5)) for t in terms if freq[t]}
    weights = [idf.get(t) for t in terms]
    avg = total / n or 1.0
    by_age = sorted(range(len(matched)), key=lambda m: (matched[m][1], matched[m][0]))
    position = {m: (k / (len(by_age) - 1) if len(by_age) > 1 else 1.0) for k, m in enumerate(by_age)}
    scored = []
    for m, (page_id, turn, context, length, on, tf) in enumerate(matched):
        norm = K1 * (1 - B + B * length / avg)
        relevance = 0.0
        for weight, freq_in_page, term in zip(weights, tf, terms):
            if weight is None or (term == phrase and q.multiword and not on):
                continue
            relevance += weight * freq_in_page * (K1 + 1) / (freq_in_page + norm)
        score = relevance * (1 + RECENCY_WEIGHT * position[m])
        scored.append((not context, on, score, turn, page_id))
    scored.sort(reverse=True)
    ranked = [page_id for *_, page_id in scored]
    # The newest matching page (a turn page when one matches) always takes one of the detailed places:
    # BM25 discounts long pages, and the turn that states a new value is often the one with the long
    # tool output (D28; reviewer-2 on PR #204).
    real = [s for s in scored if s[0]] or scored
    newest = max(real, key=lambda s: (s[3], s[4]))[4]
    if ranked.index(newest) >= SHOWN_IN_DETAIL:
        ranked.remove(newest)
        ranked.insert(SHOWN_IN_DETAIL - 1, newest)
    return ranked


# Lines


def _forms(term: str) -> tuple[str, ...]:
    """A folded term as typed, then JSON-escaped as ``render_page`` prints a tool input (B18)."""
    escaped = _escaped(term)
    return (term,) if escaped == term else (term, escaped)


def _line_index(starts: list[int], pos: int) -> int:
    return bisect.bisect_right(starts, pos) - 1


def matching_lines(text: str, query: Query) -> tuple[list[int], dict[int, tuple[int, int]]]:
    """The lines of ``text`` that hold the most of ``query``, as line numbers in order, and where each
    line's match is: ``{line: (offset in text, length)}`` of the phrase if the line holds it, else of
    the line's rarest query word. Empty when no line holds any word of the query. Linear in the size
    of ``text`` plus the number of lines that hold a query word.
    """
    low = fold(text)
    starts = [0]
    pos = low.find("\n")
    while pos != -1:
        starts.append(pos + 1)
        pos = low.find("\n", pos + 1)
    found: dict[str, dict[int, tuple[int, int]]] = {}  # term -> {line: (first offset on the line, length)}
    for term in (query.phrase, *query.words) if query.multiword else query.words:
        if term in found:
            continue
        lines: dict[int, tuple[int, int]] = {}
        for form in _forms(term):
            pos = low.find(form)
            while pos != -1:
                line = _line_index(starts, pos)
                lines.setdefault(line, (pos, len(form)))
                # the next line: one occurrence per line is enough
                nxt = starts[line + 1] if line + 1 < len(starts) else len(low)
                pos = low.find(form, max(pos + 1, nxt))
        found[term] = lines
    word_lines = {w: found[w] for w in query.words}
    weight = {w: 1.0 / len(lines) for w, lines in word_lines.items() if lines}  # rarer in the page counts more
    phrase_lines = found[query.phrase] if query.multiword else {}
    keys: dict[int, tuple[bool, float]] = {}
    for w in query.words:  # summed in query order, so equal word sets give equal weights
        for line in word_lines[w]:
            has, total = keys.get(line, (line in phrase_lines, 0.0))
            keys[line] = (has, total + weight[w])
    if not keys:
        return [], {}
    best = max(keys.values())
    chosen = sorted(line for line, key in keys.items() if key == best)
    where = {}
    for line in chosen:
        if line in phrase_lines:
            where[line] = phrase_lines[line]
        else:
            rarest = max((w for w in query.words if line in word_lines[w]), key=lambda w: weight[w])
            where[line] = word_lines[rarest][line]
    return chosen, where


def text_matches(text: str, query: Query) -> bool:
    """Every word of ``query`` occurs in ``text`` (case-insensitively, in any form ``render_page`` prints)."""
    low = fold(text)
    return all(any(form in low for form in _forms(w)) for w in query.words)


def searchable(fields: Iterable[str]) -> str:
    """The folded searchable text of a page from its stub fields and texts."""
    return fold("\n".join(fields))
