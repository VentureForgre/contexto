"""How ``contexto_search`` matches and ranks pages, which lines of a page it shows (D34, D41), and
what it falls back to when no page holds every word (D65).

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
words, plus the whole query as one more term when it has more than one word, with two changes (D41).
A term's frequency on a page is the number of the page's texts that hold it, up to 3 (``TEXT_CAP``
1, ``PAGE_CAP`` 3), not the number of times it occurs. A text is one stub field (the prompt's first
200 characters, each tool name, each path) or one text of the turn: each text block, each string
field of a tool input, each tool output (``search_text``, ``pages._search_fields``). So a grep or a
log that repeats a key hundreds of times counts once, as the one-line reply that states the key's new
value does, and a turn that runs many greps counts 3. And page length does not count (BM25's b is 0):
a turn's length is mostly its tool output, which says little about how much the turn is about the
query; with b 0.75 a long turn that states a value once ranked below short older pages that state an
old one. Frequency saturates (``K1``): a page with the term in 3 or more texts scores 1.57 times a
page with it in one, and a word that occurs on fewer pages weighs more (inverse document frequency).
Relevance is then multiplied by a recency prior, ``1 + RECENCY_WEIGHT * r``, where ``r`` is the
page's position among the matching pages from 0 (oldest) to 1 (newest). With ``RECENCY_WEIGHT`` 0.5
the oldest match needs one and a half times the relevance of the newest to rank above it, which only
a page with the term in 3 texts against the newest page's 1 has (1.57 against 1.5). A page with it
in 2 texts (1.375; a prompt that names it counts twice, since the stub repeats the prompt) outranks
a one-line page unless that page is about 0.75 or more of the way newer among the matches, so once a
passing mention follows a one-line reply, an older page with 2 texts usually outranks the reply.
Repetition inside one text does not count:
a reply or a file that discusses a key at length weighs as one passing mention, so a newer passing
mention outranks it (D41 records this loss against D34). Ties go to the newer page. Last, the newest
matching turn page (the newest matching page when no turn page matches) is moved up to the last of
the ``SHOWN_IN_DETAIL`` places if it ranked below them: older pages that say the word in more texts
can outrank the turn that states a new value once. That place covers a statement only while it is
the newest mention; a later passing mention takes it.

``contexto_search`` shows the 3 best pages in detail and lists the next 50. In each part turn pages
come first, newest first, so among the turn pages it shows the newest statement comes first (D28)
whatever their scores; context pages follow under their own line, newest first, because a context
page's turn is when the block was paged, not when it was sent (``contexto.mcp._newest_first``).

Lines. Within a page (or an in-view message) the matching lines are those that hold the most of the
query: lines holding the whole query if there are any, else lines holding all its words, else the
lines holding the most of its words, a word found on fewer lines of the page counting more.

Fallback (D65). Everything above is strict search, and the fallback changes none of it: ``rank`` and
its constants are as they were. When ``rank`` finds no page (and nothing in view matches), the caller
asks ``rank_approximate`` for pages that hold part of the query. It is deterministic, and it makes
no model call. Its steps, each looser than strict matching (``rank_approximate`` has the detail):

1. End punctuation and stop words are dropped from the query (``plan_words``, ``STOP_WORDS``), and
   at most ``APPROX_WORDS`` words are searched. ``what port is the staging database on?`` searches
   ``port``, ``staging`` and ``database``.
2. A word written as an identifier is compared without ``_``, ``-`` and ``.``, so
   ``refresh_token`` finds a page that says ``refreshToken``.
3. A page need not hold every word: ``staging database port zzqqxx`` finds the page that holds the
   first three. A floor keeps noise out (``APPROX_FLOOR``): a page must hold 30 percent of the
   query's weight, and a word that no page holds counts in that weight. When the query's two facts
   sit on two pages, the second fact's page is shown beside the first (``_second_page``).
4. One plain word that no page holds is tried with one letter changed: ``databse`` finds
   ``database``.

Each hit names the words it matched, how (``exact``, ``folded``, ``typo``), the form found on the
page, and the words it did not match (``ApproxHit``), so a reply can say what is approximate about it
(``hit_view``).
"""

from __future__ import annotations

import bisect
import json
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

K1 = 1.2  # BM25 term-frequency saturation (the usual value); BM25's b is 0, no length term (D41)
TEXT_SEP = b"\xff"  # joins a page's texts in its search text; UTF-8 never holds this byte (D41)
# A term's frequency on a page: each text counts it at most TEXT_CAP times, the page at most PAGE_CAP
# (None: no cap). Chosen on a grid of 1 to 3 and 2, 3, none (D41, spikes/m2/scale/store/results/d41/grid).
TEXT_CAP = 1
PAGE_CAP: int | None = 3
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
    """What ``rank`` reads of a page: its id, turn, folded searchable text (``search_text``: its texts
    joined by ``TEXT_SEP``), and whether it holds only paged context blocks
    (``contexto.store.pages.is_context_page``). ``text`` is None for a page whose text is not held in
    memory; ``rank`` then asks its ``fetch`` for the page (D39)."""

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

    ``entries`` are every page of the store: all of them count for the inverse document frequency, not
    only the matches. An entry whose ``text`` is None is read through ``fetch(i)``, ``i`` its index in
    ``entries``, which returns the whole entry, or None for a page that cannot be read now (left out, as
    an unreadable page is). Each page is handled in turn and only what ranking needs is kept of it
    (which words it holds, and for a match the number of its texts that hold each term), so pages read
    through ``fetch`` are not held for the length of the query (D39).

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
    n = 0
    matched = []  # (id, turn, context, holds the phrase, texts holding each term)
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
        j = 0
        while j < count and words[j] in text:  # `in` stops at the first occurrence
            df[j] += 1
            j += 1
        if j == count:
            on = (phrase in text) if q.multiword else True
            tf = tuple(term_frequency(text, t) if t != phrase or on else 0 for t in terms)
            matched.append((entry.id, entry.turn, entry.context, on, tf))
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
        freq[phrase] = sum(1 for m in matched if m[3])
    idf = {t: math.log(1 + (n - freq[t] + 0.5) / (freq[t] + 0.5)) for t in terms if freq[t]}
    weights = [idf.get(t) for t in terms]
    by_age = sorted(range(len(matched)), key=lambda m: (matched[m][1], matched[m][0]))
    position = {m: (k / (len(by_age) - 1) if len(by_age) > 1 else 1.0) for k, m in enumerate(by_age)}
    scored = []
    for m, (page_id, turn, context, on, tf) in enumerate(matched):
        relevance = 0.0
        for weight, held, term in zip(weights, tf, terms):
            if weight is None or (term == phrase and q.multiword and not on):
                continue
            relevance += weight * held * (K1 + 1) / (held + K1)  # BM25 with b 0: no length term (D41)
        score = relevance * (1 + RECENCY_WEIGHT * position[m])
        scored.append((not context, on, score, turn, page_id))
    scored.sort(reverse=True)
    ranked = [page_id for *_, page_id in scored]
    # The newest matching page (a turn page when one matches) always takes one of the detailed places:
    # older pages that say the word more often can outrank the turn that states the new value once
    # (D28; reviewer-2 on PR #204).
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


def search_text(fields: Iterable[str]) -> bytes:
    """A page's search text as ``rank`` reads it: each field folded and encoded (``search_key``), joined
    by ``TEXT_SEP`` so ``rank`` can count the texts that hold a term (D41). UTF-8 never holds that byte,
    so no query word or phrase can match across two texts, and a word matches exactly where it matches
    in ``searchable``'s text: a word holds no whitespace, so it never spanned the newline there either.
    A field is folded on its own, which folds as folding the joined text does (``fold`` lowers one
    character at a time)."""
    return TEXT_SEP.join(search_key(fold(f)) for f in fields)


def term_frequency(text: bytes, term: bytes) -> int:
    """A term's frequency on a page (D41): over the page's texts in ``text`` (``search_text``), the sum of
    each text's count of ``term`` up to ``TEXT_CAP``, the sum itself at most ``PAGE_CAP`` (None: no cap).
    After each text the scan jumps to the next text holding the term, and it stops once the page cap is
    reached, so a text or a page that repeats the term costs little."""
    text_cap, page_cap = TEXT_CAP, PAGE_CAP
    found, size, pos = 0, len(text), text.find(term)
    while pos != -1:
        end = text.find(TEXT_SEP, pos + len(term))
        if end == -1:
            end = size
        if text_cap == 1:
            found += 1
        else:
            start = text.rfind(TEXT_SEP, 0, pos) + 1
            found += min(text_cap, text.count(term, start, end))
        if page_cap is not None and found >= page_cap:
            return page_cap
        if end == size:
            break
        pos = text.find(term, end + 1)
    return found


# Approximate matches (D65): what search falls back to when no page holds every word


# Function and question words of English, dropped from a query before the fallback ranks pages. A fixed
# list of plain letters: a word with a digit or a symbol is never one of them. Words that carry meaning
# in code or in a log stay out of it (not, no, all, set, get, new, self, return, import).
STOP_WORDS = frozenset("""
a an the
is are was were be been am do does did has have had
what which who whom whose where when why how
of on in at to for from by with about as into
and or but if then than that this these those
it its i we you he she they me us my our your their there here
can could should would will shall may might must please tell
what's where's who's how's it's that's there's don't doesn't didn't isn't aren't wasn't weren't can't
""".split())
STRIP_LEFT = "\"'`(["  # punctuation dropped from the ends of a query word before the fallback
STRIP_RIGHT = "\"'`)]?!.,;:"
# The fallback searches at most this many words: the specific ones first (``Plan.specific``), then the
# longest. EAGER_WORDS, for the same cost.
APPROX_WORDS = 6
FOLD_CHARS = "_-."  # deleted from a word and from the page, so identifier styles compare equal
FOLD_BYTES = FOLD_CHARS.encode()
FOLD_MIN_CHARS = 4  # a word shorter than this without its separators is matched as typed
SPECIFIC_MIN_CHARS = 3  # a shorter word (``5``, ``v2``) is too short to stand for a page on its own
# The floor: a page is returned when it holds at least this share of the query's weight. A word weighs
# its inverse document frequency; a word that no page holds counts in the total too, at the weight of
# a word on no page. One exception: a page that holds a specific word of the query (an identifier, a
# number, a path) is returned whatever its share, unless more than half the pages hold that word.
APPROX_FLOOR = 0.3
# The second-fact place (``_second_page``): the words the best page lacks that a page holds must weigh at
# least this share of a word on no page (a typo match at its full weight), unless one of them is specific
# or stands next to its neighbour from the query. One word passes on at most sqrt((n + 1) / 2) - 1/2 of
# n pages: 6 of 100 (6.6 percent), 54 of 6,000 (0.9 percent).
SECOND_BAR = 0.5
APPROX_LIMIT = 50  # hits returned, best first; ``Approximate.total`` counts every page returned
TYPO_MIN_CHARS = 6  # a shorter word is never matched with a letter changed
TYPO_MAX_CHARS = 40  # nor a longer one: the pattern of its variants grows with the square of its length
TYPO_WEIGHT = 0.5  # a word matched through a typo weighs half
TYPO_WORK = 500_000  # places checked for a typo per query, at most; past it the typo is not searched
EXACT, FOLDED, TYPO = "exact", "folded", "typo"
_TYPO_WORD = re.compile(r"[a-z]+")
_DIGITS_APART = re.compile(r"\d[_.\-]+\d")  # as in 1.2 or 2026-11-05: deleting the separator makes another number
_DIGIT_PAIR = re.compile(r"\d\d")
_ANY_LETTER = "\x00"  # stands for one letter in a typo variant
_WORD_BYTES = frozenset(b"0123456789abcdefghijklmnopqrstuvwxyz")  # a typo match is not next to one of these


def _mixed_case(typed: str) -> bool:
    return any(c.isupper() for c in typed[1:]) and any(c.islower() for c in typed)


def _specific(word: str, typed: str) -> bool:
    """Whether a query word is an identifier, a number, a code or a path rather than a plain word: it
    has ``SPECIFIC_MIN_CHARS`` characters or more, and one of them is not a letter (an apostrophe and a
    combining accent count as letters), or it is in camelCase."""
    return len(word) >= SPECIFIC_MIN_CHARS and (_mixed_case(typed) or any(
        not (c.isalpha() or c == "'" or unicodedata.combining(c)) for c in word))


class Plan(NamedTuple):
    """The words the fallback searches for: folded, in query order, at most ``APPROX_WORDS``; each as it
    was typed (the first spelling, or the first in camelCase when the query also writes the word so, so
    a word is an identifier when any of its spellings is one); the stop words dropped; and how many
    further words were left out by the cut."""

    words: tuple[str, ...]
    typed: tuple[str, ...]
    dropped: tuple[str, ...]
    cut: int
    pairs: tuple[tuple[int, int], ...] = ()  # words that follow each other in the query, as indexes

    def shaped(self, j: int) -> bool:
        """Whether word ``j`` is written as an identifier: it has ``_``, ``-`` or ``.`` inside (one at
        its start or its end does not count: ``--force``, ``__init__``), or a capital after its first
        letter beside small letters (``refreshToken``, ``HTTPTimeout``), and without its separators it
        still has a letter and ``FOLD_MIN_CHARS`` characters. A separator between two digits rules it
        out. Only such a word is compared without separators: a plain word is matched as typed, and
        so is a number, a version or a date (``12`` never finds ``1.2``, nor ``v0.5.24`` ``v0.52.4``)."""
        word = self.words[j]
        inside = word.strip(FOLD_CHARS)
        bare = squeeze(inside)
        return ((bare != inside or _mixed_case(self.typed[j])) and len(bare) >= FOLD_MIN_CHARS
                and any(c.isalpha() for c in word) and _DIGITS_APART.search(word) is None)

    def specific(self, j: int) -> bool:
        """Whether word ``j`` is an identifier, a number, a code or a path (``_specific``)."""
        return _specific(self.words[j], self.typed[j])

    def plain(self, j: int) -> bool:
        """Whether word ``j`` can have a typo looked for: small letters only as folded, not camelCase,
        ``TYPO_MIN_CHARS`` to ``TYPO_MAX_CHARS`` of them."""
        return (TYPO_MIN_CHARS <= len(self.words[j]) <= TYPO_MAX_CHARS
                and _TYPO_WORD.fullmatch(self.words[j]) is not None and not self.specific(j))

    def searched(self) -> str | None:
        """The line a reply shows when the fallback did not search every word of the query (stop words
        dropped, or more than ``APPROX_WORDS`` words): ``[searched for: port, staging, database]``."""
        return f"[searched for: {', '.join(self.typed)}]" if self.dropped or self.cut else None


class WordMatch(NamedTuple):
    """A query word a page holds: ``how`` is ``exact``, ``folded`` (another identifier style) or ``typo``
    (one letter differs), and ``form`` is what the page holds, case-folded."""

    word: str
    how: str
    form: str


class ApproxHit(NamedTuple):
    id: str
    turn: int
    matched: tuple[WordMatch, ...]
    missing: tuple[str, ...]


class Approximate(list):
    """Approximate hits, best first, with how many pages matched (``total``: those over the floor and the
    second-fact page, which can be under it) and the ``Plan``."""

    total = 0
    plan: Plan | None = None


def plan_words(query: object) -> Plan | None:
    """The words of ``query`` the fallback searches for, or None when there are none: the query has no
    words, or only stop words (strict search has already looked for exactly those)."""
    if not isinstance(query, str):
        return None
    typed: dict[str, str] = {}  # each word as first typed, or as first typed in camelCase if it ever is
    order = []  # every word of the query, folded, in order, with repeats
    for token in query.split():
        token = token.lstrip(STRIP_LEFT).rstrip(STRIP_RIGHT)
        if token:
            order.append(fold(token))
            if order[-1] not in typed or (_mixed_case(token) and not _mixed_case(typed[order[-1]])):
                typed[order[-1]] = token  # so the order of two spellings does not decide (reviewer B on PR #267)
    kept = [w for w in typed if w not in STOP_WORDS]
    if not kept:
        return None
    cut = max(0, len(kept) - APPROX_WORDS)
    if cut:  # identifiers, numbers and paths first, then the longest words; ties go to the earlier word
        best = sorted(range(len(kept)), key=lambda i: (not _specific(kept[i], typed[kept[i]]), -len(kept[i]), i))
        kept = [kept[i] for i in sorted(best[:APPROX_WORDS])]
    at = {w: j for j, w in enumerate(kept)}
    pairs = dict.fromkeys((at[a], at[b]) for a, b in zip(order, order[1:]) if a in at and b in at and a != b)
    return Plan(tuple(kept), tuple(typed[w] for w in kept), tuple(w for w in typed if w in STOP_WORDS), cut,
                tuple(pairs))


def squeeze(word: str) -> str:
    """``word`` without ``_``, ``-`` and ``.``: ``refresh_token``, ``refresh-token``, ``refresh.token`` and
    the folded ``refreshToken`` are all ``refreshtoken``."""
    return word.translate(_SQUEEZE)


_SQUEEZE = str.maketrans("", "", FOLD_CHARS)


def _folded_form(squeezed: str) -> re.Pattern[bytes]:
    """``squeezed`` as a page may write it: with any run of separators between two of its characters,
    except between two digits, where a separator would make another number (``sha256`` is found as
    ``sha-256`` and ``sha_256``, never as ``sha-2.56``)."""
    separators = b"[" + re.escape(FOLD_BYTES) + b"]*"
    parts = []
    for a, b in zip(squeezed, squeezed[1:]):
        parts.append(re.escape(search_key(a)))
        if not (a.isdecimal() and b.isdecimal()):
            parts.append(separators)
    return re.compile(b"".join(parts) + re.escape(search_key(squeezed[-1:])))


def _form_frequency(text: bytes, form: re.Pattern[bytes]) -> int:
    """As ``term_frequency``, for a term given as a pattern: the number of the page's texts in which
    ``form`` occurs, at most ``PAGE_CAP``. (Each text counts once, as with ``TEXT_CAP`` 1.)"""
    found, match = 0, form.search(text)
    while match is not None:
        found += 1
        if PAGE_CAP is not None and found >= PAGE_CAP:
            return PAGE_CAP
        end = text.find(TEXT_SEP, match.end())
        if end == -1:
            break
        match = form.search(text, end + 1)
    return found


def _typo_pattern(word: str) -> re.Pattern[bytes]:
    """A whole word one letter away from ``word``: one letter changed, added or left out, or two
    neighbours swapped. ``word`` itself is not among them."""
    variants = set()
    for i in range(len(word) + 1):
        variants.add(word[:i] + _ANY_LETTER + word[i:])  # the page's word has one letter more
        if i < len(word):
            variants.add(word[:i] + _ANY_LETTER + word[i + 1:])  # one letter differs
            variants.add(word[:i] + word[i + 1:])  # the page's word has one letter fewer
        if i + 1 < len(word) and word[i] != word[i + 1]:
            variants.add(word[:i] + word[i + 1] + word[i] + word[i + 2:])  # two neighbours swapped
    variants.discard(word)
    wild = re.escape(search_key(_ANY_LETTER))
    body = b"|".join(re.escape(search_key(v)).replace(wild, b"[a-z]") for v in sorted(variants))
    return re.compile(b"(?:" + body + b")(?![0-9a-z])")


class _TypoFinder:
    """Finds, on one page after another, a whole word one letter away from ``word``.

    A word one edit away keeps a piece of ``word`` unchanged. For 7 letters or more the pieces are
    the letters before a middle letter and the letters after it: an edit, or a swap of two
    neighbours, touches only one of them. For 6 letters they are the two halves (3 letters each, where
    the other split would leave 2), and the word with its two middle letters swapped, which keeps
    neither half. So only the places where a piece occurs are looked at: two or three substring scans
    per page, and one anchored match per place that starts a word. ``spent`` turns true once more
    than ``TYPO_WORK`` places have been looked at over the pages; the typo is then not searched at
    all, so the cost stays bounded when a piece is as common as ``ret`` in a store of thousands of
    pages. ``work`` is the places already checked for an earlier word of the same query: the bound is
    for the query, not for each word tried."""

    def __init__(self, word: str, work: int = 0) -> None:
        size = len(word)
        if size >= 7:
            gap = (size - 1) // 2
            pieces = [(word[:gap], (0,)), (word[gap + 1:], (-gap - 2, -gap - 1, -gap))]
        else:
            half = size // 2
            pieces = [(word[:half], (0,)), (word[half:], (-half - 1, -half, -half + 1))]
            if word[half - 1] != word[half]:
                pieces.append((word[:half - 1] + word[half] + word[half - 1] + word[half + 1:], (0,)))
        self.pieces = [(search_key(piece), offsets) for piece, offsets in pieces]
        self.match = _typo_pattern(word).match
        self.work = work
        self.spent = work > TYPO_WORK

    def find(self, text: bytes) -> bytes | None:
        """A whole word of ``text`` one letter away, or None. The same word for the same text."""
        match, inside, limit, work, find = self.match, _WORD_BYTES, TYPO_WORK, self.work, text.find
        try:
            for piece, offsets in self.pieces:
                pos = find(piece)
                while pos != -1:
                    work += 1
                    if work > limit:
                        self.spent = True
                        return None
                    for offset in offsets:
                        start = pos + offset
                        if start > 0:
                            if text[start - 1] in inside:  # inside a longer word
                                continue
                        elif start < 0:
                            continue
                        found = match(text, start)
                        if found is not None:
                            return found.group()
                    pos = find(piece, pos + 1)
            return None
        finally:
            self.work = work


def _read(entries: Sequence[SearchEntry], fetch: Callable[[int], SearchEntry | None] | None,
          ) -> Iterable[tuple[int, SearchEntry, bytes]]:
    """Every readable page as ``(index, entry, text)``; a page not held is read through ``fetch``."""
    for i, entry in enumerate(entries):
        if entry.text is None:
            entry = fetch(i) if fetch is not None else None
            if entry is None or entry.text is None:
                continue
        yield i, entry, entry.text


def _beside(pairs: tuple[tuple[int, int], ...], word: int, form: bytes, raw: list[bytes], text: bytes) -> int:
    """The pairs of query words, as a bit set, in which ``word``, found on the page as ``form``, stands
    next to its neighbour as the query has them."""
    return sum(1 << b for b, (j, k) in enumerate(pairs)
               if (j == word and form + b" " + raw[k] in text) or (k == word and raw[j] + b" " + form in text))


def _saturated(frequency: int) -> float:
    return frequency * (K1 + 1) / (frequency + K1)  # BM25 with b 0, as ``rank`` scores a term (D41)


def rank_approximate(entries: Sequence[SearchEntry], query: str,
                     fetch: Callable[[int], SearchEntry | None] | None = None) -> Approximate:
    """Pages that hold part of ``query``, best first: what search falls back to when ``rank`` finds no
    page (D65). ``entries`` and ``fetch`` are as for ``rank``. Deterministic for a store and a query.

    The steps, each looser than strict matching:

    1. Punctuation at the ends of a word and stop words are dropped (``plan_words``); at most
       ``APPROX_WORDS`` words are searched, identifiers, numbers and paths first, then the longest.
    2. A word written as an identifier is compared without ``_``, ``-`` and ``.``, in the query and on
       the page, so identifier styles match each other (``Plan.shaped``). A separator between two
       digits is never dropped, on either side.
    3. A page need not hold every word. Each word weighs its inverse document frequency over the whole
       store, and a word that no page holds weighs as much as a word can. A page is returned when it
       holds at least ``APPROX_FLOOR`` of the weight of the query, or when it holds a specific word
       of the query (an identifier, a number or a path, ``Plan.specific``) that no more than half
       the pages hold.
    4. One plain word that no page holds, of ``TYPO_MIN_CHARS`` to ``TYPO_MAX_CHARS`` letters, is
       looked for with one letter changed, as a whole word (``_TypoFinder``); it weighs
       ``TYPO_WEIGHT``. Only the longest such word of the query is tried.

    Pages that hold every word come first, then turn pages before paged context blocks, then pages
    holding more of the weight, then pages that hold more pairs of words as the query has them, one
    after the other (as ``rank`` puts pages holding the whole query first). Pages equal in all that
    rank as in ``rank``: a word counts once per text, at most ``PAGE_CAP``, saturated by ``K1``, times
    the recency prior. Two pages are then given a place among the first ``SHOWN_IN_DETAIL``, the
    last of them, if they rank lower. When some page holds every word, it is the newest turn page
    that does, as in ``rank``. When none does, the query may hold two facts that sit on two pages,
    and the first page then crowds out the second with pages that share its words; so it is the best
    page for the words the first page lacks (``_second_page``). At most ``APPROX_LIMIT`` hits are
    returned, each with the words it matched, how, and the form found on the page.

    Cost: one pass over the store. Each word is looked for once on each page (strict search stops at
    a page's first missing word unless the page was just read from its file), and each pair of
    neighbouring words once on a page that holds both. The separators are deleted from each page only
    when a word is written as an identifier. A typo adds two or three substring scans per page. The
    word tried is the longest plain word no page holds, which is known only at the end: the longest
    not found so far is looked for, and when a page turns out to hold it, the next one is looked for
    from that page on and the pages before it are read once more for that word alone."""
    out = Approximate()
    plan = out.plan = plan_words(query)
    if plan is None or not entries:
        return out
    count = len(plan.words)
    raw = [search_key(w) for w in plan.words]
    squeezed = [search_key(squeeze(w)) for w in plan.words]
    shaped = [plan.shaped(j) for j in range(count)]
    specific = [plan.specific(j) for j in range(count)]
    # A folded match of a word with two digits side by side is confirmed on the page as it is: the
    # page without its separators would also show ``port8.0`` for ``port-80``.
    confirm = [_folded_form(squeeze(w)) if shaped[j] and _DIGIT_PAIR.search(squeeze(w)) else None
               for j, w in enumerate(plan.words)]
    fold_pages = any(shaped)
    df = [0] * count
    n = 0
    rows: dict[int, tuple[SearchEntry, list[tuple[str, int] | None]]] = {}
    together: dict[int, int] = {}  # per page, the pairs of words it holds one after the other, as a bit set
    can_typo = sorted((j for j in range(count) if plan.plain(j)), key=lambda j: (-len(plan.words[j]), j))
    typo = can_typo[0] if can_typo else None  # the longest plain word no page has held so far
    finder = _TypoFinder(plan.words[typo]) if typo is not None else None
    since = 0  # the index of the first page ``finder`` was run on
    forms: dict[int, tuple[SearchEntry, bytes, int]] = {}  # per page: its entry, the word found, its pairs
    for i, entry, text in _read(entries, fetch):
        n += 1
        without = text.translate(None, FOLD_BYTES) if fold_pages else text
        row = None
        for j in range(count):
            if shaped[j]:
                # a word found as typed is also found once the separators are gone, so one scan finds both
                frequency = term_frequency(without, squeezed[j])
                if not frequency:
                    continue
                as_typed = term_frequency(text, raw[j])
                if as_typed:
                    held = (EXACT, as_typed)
                else:
                    if confirm[j] is not None:
                        frequency = _form_frequency(text, confirm[j])
                        if not frequency:
                            continue
                    held = (FOLDED, frequency)
            else:
                frequency = term_frequency(text, raw[j])  # 0 when the page does not hold the word
                if not frequency:
                    continue
                held = (EXACT, frequency)
            df[j] += 1
            if row is None:
                row = [None] * count
                rows[i] = (entry._replace(text=None), row)
            row[j] = held
        if row is not None and plan.pairs:
            bits = sum(1 << b for b, (j, k) in enumerate(plan.pairs)
                       if row[j] is not None and row[k] is not None and raw[j] + b" " + raw[k] in text)
            if bits:
                together[i] = bits
        if typo is not None and df[typo]:  # this page holds the word: look for the next one from here on
            typo = next((j for j in can_typo if not df[j]), None)
            finder = _TypoFinder(plan.words[typo], finder.work) if typo is not None else None
            since, forms = i, {}
        if finder is not None and not finder.spent:
            form = finder.find(text)
            if form is not None:
                forms[i] = (entry._replace(text=None), form, _beside(plan.pairs, typo, form, raw, text))
    if finder is not None and since:  # the pages before the one the word was first looked for on
        for i, entry, text in _read(entries[:since], fetch):
            if finder.spent:
                break
            form = finder.find(text)
            if form is not None:
                forms[i] = (entry._replace(text=None), form, _beside(plan.pairs, typo, form, raw, text))
    if finder is None or finder.spent or not forms:
        typo, forms = None, {}  # no word is matched through a typo
    for i, (entry, _, bits) in forms.items():
        if i not in rows:
            rows[i] = (entry, [None] * count)
        rows[i][1][typo] = (TYPO, 1)
        df[typo] += 1
        if bits:
            together[i] = together.get(i, 0) | bits
    if not any(df):
        return out
    # A word no page holds weighs what a word with no page weighs, so the pages are measured against
    # the whole query, not only against the part of it that the store knows.
    weight = [math.log(1 + (n - df[j] + 0.5) / (df[j] + 0.5)) * (TYPO_WEIGHT if j == typo else 1.0)
              for j in range(count)]
    whole = sum(weight)
    telling = [specific[j] and 0 < df[j] and 2 * df[j] <= n for j in range(count)]  # at most half the pages

    def enough(held: list[int], of: float) -> bool:
        """The floor: the words ``held`` weigh ``APPROX_FLOOR`` of ``of``, or one of them is specific."""
        return sum(weight[j] for j in held) >= APPROX_FLOOR * of or any(telling[j] for j in held)

    passed = []  # (holds every word, a turn page, weight held, pairs together, relevance, turn, id, index)
    for i, (entry, row) in rows.items():
        on = [j for j in range(count) if row[j] is not None]
        if not enough(on, whole):
            continue
        relevance = sum(weight[j] * _saturated(row[j][1]) for j in on)
        passed.append((len(on) == count, not entry.context, sum(weight[j] for j in on),
                       together.get(i, 0).bit_count(), relevance, entry.turn, entry.id, i))
    scored = []
    for every in (True, False):  # the recency prior is a page's place among the pages of its own group
        group = sorted((p for p in passed if p[0] is every), key=lambda p: (p[5], p[6]))
        for place, (_, real, share, beside, relevance, turn, page_id, i) in enumerate(group):
            newer = place / (len(group) - 1) if len(group) > 1 else 1.0
            scored.append((every, real, share, beside, relevance * (1 + RECENCY_WEIGHT * newer), turn, page_id, i))
    scored.sort(reverse=True)
    if scored and scored[0][0]:  # as in ``rank``: the newest turn page holding every word is shown in detail (D28)
        complete = [s for s in scored if s[0]]
        placed = max([s for s in complete if s[1]] or complete, key=lambda s: (s[5], s[6]))
    elif scored:
        placed = _second_page(scored[0][7], rows, together, weight, math.log(1 + (n + 0.5) / 0.5), telling,
                              plan.pairs, typo)
    else:
        placed = None
    if placed is not None and placed[7] not in [s[7] for s in scored[:SHOWN_IN_DETAIL]]:
        scored = [s for s in scored if s[7] != placed[7]]
        scored.insert(SHOWN_IN_DETAIL - 1, placed)
    out.total = len(scored)
    labels: dict[int, re.Pattern[bytes]] = {}
    for *_, turn, page_id, i in scored[:APPROX_LIMIT]:
        row = rows[i][1]
        text = None
        matched = []
        for j in range(count):
            if row[j] is None:
                continue
            form = plan.words[j]
            if row[j][0] == TYPO:
                form = forms[i][1].decode("utf-8", "surrogatepass")
            elif row[j][0] == FOLDED:
                if text is None:
                    again = entries[i] if entries[i].text is not None else (fetch(i) if fetch is not None else None)
                    text = again.text if again is not None and again.text is not None else b""
                if j not in labels:
                    labels[j] = confirm[j] or _folded_form(squeeze(plan.words[j]))
                found = labels[j].search(text)
                form = found.group().decode("utf-8", "surrogatepass") if found else squeeze(plan.words[j])
            matched.append(WordMatch(plan.words[j], row[j][0], form))
        out.append(ApproxHit(page_id, turn, tuple(matched),
                             tuple(plan.words[j] for j in range(count) if row[j] is None)))
    return out


def _second_page(first: int, rows: dict[int, tuple[SearchEntry, list[tuple[str, int] | None]]],
                 together: dict[int, int], weight: list[float], nowhere: float, telling: list[bool],
                 pairs: tuple[tuple[int, int], ...], typo: int | None) -> tuple | None:
    """The best page for the words that page ``first``, the best page of the query, does not hold: the
    second fact of a query whose two facts sit on two pages (D65). None when no page qualifies.

    A page qualifies when the lacked words it holds weigh at least ``APPROX_FLOOR`` of all the lacked
    words, so it is about them (a specific word whose own weight reaches ``SECOND_BAR`` of ``nowhere``
    needs no such share), and one of three things holds, so that what it adds is not a leftover
    common word: one of those words is specific (``telling``: on at most half the pages); or they weigh
    at least ``SECOND_BAR`` of ``nowhere``, the weight of a word on no page, with the word matched
    through a typo (``typo``) counted at its full weight; or one of them stands next to its neighbour
    as the query has them (a pair of ``pairs`` that the page holds, in ``together``). The best such
    page is, in order: a turn page before a context block; the larger weight of the lacked words; the
    larger weight of the whole query, so a page that also holds the query's other words beats one that
    holds only the lacked word; more pairs of words side by side; the newer page, so the latest value
    wins a tie (D28). It is returned as a row of the main order."""
    lacks = [j for j in range(len(weight)) if rows[first][1][j] is None]
    rest = sum(weight[j] for j in lacks)
    best = None
    for i, (entry, row) in rows.items():
        held = [j for j in lacks if row[j] is not None]
        if i == first or not held:
            continue
        share = sum(weight[j] for j in held)
        # A specific word that clears the bar by itself is shown whatever share of the lacked weight it
        # holds: words that no page holds weigh the most and would otherwise hide the rarest word asked for.
        if share < APPROX_FLOOR * rest and not any(telling[j] and weight[j] >= SECOND_BAR * nowhere for j in held):
            continue
        full = sum(weight[j] / (TYPO_WEIGHT if j == typo else 1.0) for j in held)  # the typo word unhalved
        bits = together.get(i, 0)
        beside = any(bits >> b & 1 and (j in held or k in held) for b, (j, k) in enumerate(pairs))
        if not (any(telling[j] for j in held) or full >= SECOND_BAR * nowhere or beside):
            continue
        on = [j for j in range(len(weight)) if row[j] is not None]
        whole = sum(weight[j] for j in on)
        key = (not entry.context, share, whole, bits.bit_count(), entry.turn, entry.id)
        if best is None or key > best[0]:
            relevance = sum(weight[j] * _saturated(row[j][1]) for j in on)
            best = (key, (False, not entry.context, whole, bits.bit_count(), relevance, entry.turn, entry.id, i))
    return best[1] if best is not None else None


def hit_view(hit: ApproxHit, text: str, plan: Plan | None = None) -> tuple[str, Query | None]:
    """How a reply shows an approximate hit on rendered page ``text``: the line naming the words the page
    matched and the ones it did not (``[matched: port, refresh_token as refreshToken; not matched:
    zzqqxx]``, each word as typed and each form as the page writes it), and the query that picks the
    page's matching lines: the forms found on the page that ``text`` holds (None when it holds none, so
    the match is in the stub only)."""
    typed = dict(zip(plan.words, plan.typed)) if plan is not None else {}
    low = fold(text)
    parts, forms = [], []
    for m in hit.matched:
        at, size = next(((low.find(f), len(f)) for f in _forms(m.form) if f in low), (-1, 0))
        if at != -1:
            forms.append(m.form)
        name = typed.get(m.word, m.word)
        parts.append(name if m.how == EXACT else f"{name} as {text[at:at + size] if at != -1 else m.form}")
    label = "matched: " + ", ".join(parts)
    if hit.missing:
        label += "; not matched: " + ", ".join(typed.get(w, w) for w in hit.missing)
    forms = list(dict.fromkeys(forms))
    return f"[{label}]", (Query(" ".join(forms), tuple(forms)) if forms else None)
