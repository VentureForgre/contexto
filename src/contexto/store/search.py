"""How ``contexto_search`` matches and ranks pages, and which lines of a page it shows (D34).

Matching. A query is split on whitespace into words. A page matches when every word occurs in
its searchable text (the stub and every text of its turn, as before), case-insensitively, in any
order. A word is matched as a substring, as the whole query was before, so an identifier, a path,
or part of one (``E4127``, ``src/contexto/mcp.py``, ``TIMEOUT`` in ``HTTP_TIMEOUT_S``) finds the
same pages it found before: every page the exact, case-sensitive rule matched still matches.

Ranking. Pages that hold the whole query as typed (case-insensitively; for one word, every match)
rank above pages that hold only its words. Within each of the two groups a page's relevance is
BM25 over the query's words, plus the whole query as one more term when it has more than one word:
term frequency saturates (``K1``), long pages are discounted (``B``), and a word that occurs on
fewer pages weighs more (inverse document frequency). Relevance is then multiplied by a recency
prior, ``1 + RECENCY_WEIGHT * r``, where ``r`` is the page's position among the matching pages
from 0 (oldest) to 1 (newest). So a newer page wins unless an older one is clearly more about the
query: with ``RECENCY_WEIGHT`` 0.5 the oldest match needs one and a half times the relevance of the
newest to rank above it, and two matches next to each other in time are ordered by relevance
almost alone. Pages that state a value look alike, so the newest statement of a value that changes
ranks first among them, while a page that mentions the word in passing does not outrank the pages
that are about it. Ties go to the newer page.

``contexto_search`` shows the 3 best pages in detail and lists the next 50, each group newest
first, so among the pages it shows the newest statement comes first (D28) whatever their scores.

Lines. Within a page (or an in-view message) the matching lines are those that hold the most of the
query: lines holding the whole query if there are any, else lines holding all its words, else the
lines holding the most of its words, a word found on fewer lines of the page counting more.
"""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

K1 = 1.2  # BM25 term-frequency saturation (the usual value)
B = 0.75  # BM25 length normalization (the usual value)
RECENCY_WEIGHT = 0.5  # the newest match's relevance is multiplied by 1.5, the oldest's by 1


def fold(text: str) -> str:
    """``text`` lowercased, one character for one, so offsets into the result are offsets into ``text``.

    ``str.lower`` turns a few characters into two (``İ``); those are kept as they are.
    """
    low = text.lower()
    if len(low) == len(text):
        return low
    return "".join(c if len(c.lower()) != 1 else c.lower() for c in text)


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


def rank(entries: Sequence[tuple[str, int, str]], query: str) -> list[str]:
    """Ids of the matching entries, best first (see the module docstring).

    ``entries`` are ``(page_id, turn, searchable text folded)`` for every page of the store: all of
    them count for the inverse document frequency and the average length, not only the matches.
    """
    q = Query.parse(query)
    if q is None or not entries:
        return []
    texts = [text for _, _, text in entries]
    present = {w: [w in text for text in texts] for w in q.words}  # `in` stops at the first occurrence
    matched = [i for i in range(len(texts)) if all(present[w][i] for w in q.words)]
    if not matched:
        return []
    n = len(texts)
    df = {w: sum(present[w]) for w in q.words}
    phrase_on = {i: (q.phrase in texts[i]) if q.multiword else True for i in matched}
    if q.multiword:
        df[q.phrase] = sum(phrase_on.values())
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q.terms() if df[t]}
    avg = sum(map(len, texts)) / n or 1.0
    by_age = sorted(matched, key=lambda i: (entries[i][1], entries[i][0]))
    position = {i: (k / (len(by_age) - 1) if len(by_age) > 1 else 1.0) for k, i in enumerate(by_age)}
    scored = []
    for i in matched:
        text = texts[i]
        norm = K1 * (1 - B + B * len(text) / avg)
        relevance = 0.0
        for term, weight in idf.items():
            if term == q.phrase and q.multiword and not phrase_on[i]:
                continue
            tf = text.count(term)
            relevance += weight * tf * (K1 + 1) / (tf + norm)
        score = relevance * (1 + RECENCY_WEIGHT * position[i])
        scored.append((phrase_on[i], score, entries[i][1], entries[i][0]))
    scored.sort(reverse=True)
    return [page_id for _, _, _, page_id in scored]


# Lines


def _forms(term: str) -> tuple[str, ...]:
    """A folded term as typed, then JSON-escaped as ``render_page`` prints a tool input (B18)."""
    escaped = _escaped(term)
    return (term,) if escaped == term else (term, escaped)


def _line_index(starts: list[int], pos: int) -> int:
    return bisect.bisect_right(starts, pos) - 1


def matching_lines(text: str, query: Query) -> tuple[list[int], dict[int, int]]:
    """The lines of ``text`` that hold the most of ``query``, as line numbers in order, and where each
    line's match is: ``{line: offset in text}`` of the phrase if the line holds it, else of the
    line's rarest query word. Empty when no line holds any word of the query.
    """
    low = fold(text)
    starts = [0]
    pos = low.find("\n")
    while pos != -1:
        starts.append(pos + 1)
        pos = low.find("\n", pos + 1)
    found: dict[str, dict[int, int]] = {}  # term -> {line: first offset of the term on that line}
    for term in (query.phrase, *query.words) if query.multiword else query.words:
        if term in found:
            continue
        lines: dict[int, int] = {}
        for form in _forms(term):
            pos = low.find(form)
            while pos != -1:
                line = _line_index(starts, pos)
                lines.setdefault(line, pos)
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


def match_length(text: str, query: Query, pos: int) -> int:
    """How many characters of ``text`` from ``pos`` the match there covers (phrase or word, in any form)."""
    low = fold(text)
    for term in (query.phrase, *query.words):
        for form in _forms(term):
            if low.startswith(form, pos):
                return len(form)
    return 0


def searchable(fields: Iterable[str]) -> str:
    """The folded searchable text of a page from its stub fields and texts."""
    return fold("\n".join(fields))
