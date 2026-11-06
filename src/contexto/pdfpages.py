"""PDF page counts for the token estimate (D60): certain, or the API's maximum.

A PDF is never estimated low (D60), so its pages are trusted only when the count is certain: a PDF lexer
read every object in the file (PDF whitespace including NUL, comments, ``#xx`` escapes in names, any
spacing, nested literal strings), every stream's end was confirmed at its ``/Length`` (direct, or an
integer object it names), every object stream was inflated within the inflate budget and its objects
read by its offset table, and no object header could be hidden in a byte the lexer passed over (a stream
body, a string, a comment). Every version of an object counts, and so does every version of the object
an indirect value names, since the lexer does not know which one the xref names. The pages are the larger
of the object numbers whose ``/Type`` is ``/Page`` in any version together with the page tree's leaves
(each number once), and the tree as a renderer walks it (every ``/Kids`` entry that reaches a page, each
time it is listed); any node's ``/Count`` may raise that, never stand in for a part that was not read.
Anything else counts as ``PDF_MAX_PAGES``, the only value that is always safe: a cycle in the tree, an object
stream not inflated (a bound, another filter, a predictor, damaged data), a stream whose end is not where
its ``/Length`` says, ``N G obj`` inside a stream body, string or comment (a ``/Length`` that lies long, or
an object a reader would find by its offset), a token the lexer cannot read, an indirect value whose
target was not read, a bound passed, or no pages in a file that has objects.

The lexer is a hand-written single pass over the bytes. Its regular expressions are each one character
class or one literal, matched at a position or searched forward, so none can backtrack: a run of
whitespace, regular characters or hex digits is passed in one C call, and a container whose value the
count does not need is skipped to its next delimiter the same way. Every token, comment, string
delimiter and skipped delimiter is a step, and a PDF that takes more than ``PDF_MAX_STEPS`` is not
certain, so the time is linear in the input and bounded per PDF (about 0.25 s at most), and the parse
pauses briefly every few thousand steps so other threads get the GIL. Skipped runs make no Python objects;
what is kept (a few keys of each dictionary, page-tree references, at most ``PDF_MAX_ARRAY`` items of
an array) takes at most a step each, so it is bounded by the steps too, and inflated data by
``PDF_INFLATE_BUDGET``, one object stream at a time.
"""

from __future__ import annotations

import bisect
import re
import time
import zlib
from typing import Any, NamedTuple

PDF_MAX_PAGES = 600  # the API's limit, and the count of any PDF that is not certain
PDF_STREAM_OUTPUT = 4 << 20  # inflated bytes per object stream
PDF_INFLATE_BUDGET = 16 << 20  # inflated bytes per PDF: object streams hold dictionaries, not page content
# Lexer steps per PDF, object streams included. A 600-page PDF with 25 links a page takes about 60,000;
# a step costs about 0.3 microseconds, so a PDF that uses them all is uncertain in about 0.1 s.
PDF_MAX_STEPS = 400_000
# Steps between pauses of PDF_YIELD_SECONDS (about every 2 ms of parsing), so a long parse outside the
# transform lock does not keep another session's request waiting a full GIL switch interval at each of its
# reads and writes. A real sleep, not sleep(0): that drops the GIL and takes it straight back, which restarts
# a waiting thread's switch timer, so the waiter starved for the whole parse (1.9 s behind 20 hostile PDFs).
PDF_YIELD_STEPS = 4_096
PDF_YIELD_SECONDS = 0.0001
PDF_MAX_ARRAY = 4_096  # items kept of one array; a longer one cannot be a page tree's /Kids here
PDF_MAX_DEPTH = 64  # arrays and dictionaries inside one another
PDF_LENGTH_CANDIDATES = 8  # integer objects tried for an indirect /Length
# Keys the count reads; a dictionary keeps only these, so memory follows the page tree, not the content.
_KEPT = frozenset({b"Type", b"Count", b"Kids", b"Pages", b"Length", b"N", b"First", b"Filter", b"DecodeParms",
                   b"Predictor"})
_TREE_KEYS = (b"Type", b"Kids", b"Count", b"Pages")  # an object with one of these is recorded for the count

_WHITESPACE = b"\x00\t\n\x0c\r "
_DIGITS = b"0123456789"
_REGULAR, _WHITE, _DELIMITER = 0, 1, 2
_KIND = bytes(_WHITE if c in _WHITESPACE else _DELIMITER if c in b"()<>[]{}/%" else _REGULAR for c in range(256))
# One character class or literal each: linear, and no backtracking state however long the run.
_WHITE_RUN = re.compile(rb"[\x00\t\n\x0c\r ]+")
_REGULAR_RUN = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]+")
_HEX_RUN = re.compile(rb"[0-9A-Fa-f\x00\t\n\x0c\r ]*")
_LINE_END = re.compile(rb"[\r\n]")
_STRING_STOP = re.compile(rb"[()\\]")
# What can change a skipped container's structure (a name holds no delimiter, so it cannot), and what can end
# a value that is skipped whole.
_SKIP_STOP = re.compile(rb"[()<>\[\]{}%]|obj")
_VALUE_STOP = re.compile(rb"[()<>\[\]{}/%]|obj")
# A cross-reference table's entries and subsection headers: digits, f, n and whitespace only, so it holds no
# object header and is passed in one match.
_XREF_RUN = re.compile(rb"[0-9fn\x00\t\n\x0c\r ]*")
_NOT_WHITE = re.compile(rb"[^\x00\t\n\x0c\r ]")

# Token kinds
_END, _NAME, _INT, _OTHER, _WORD, _OPEN, _CLOSE = range(7)
_CLOSERS = {b"<<": b">>", b"[": b"]", b"{": b"}"}
_OBJECT_KEYWORDS = frozenset({b"obj", b"endobj", b"stream", b"endstream"})
_KEYWORDS = {b"true": True, b"false": False, b"null": None}


class PdfCount(NamedTuple):
    """A PDF's pages for the estimate, and whether they are certain (else ``PDF_MAX_PAGES``)."""

    pages: int
    counted: bool


UNCERTAIN = PdfCount(PDF_MAX_PAGES, False)


class _Uncertain(Exception):
    """The count cannot be certain."""


class _Ref(int):
    """An indirect reference, by its object number (the generation is not needed)."""


_SKIPPED = object()  # a value read but not kept: a string, a real number, a container the count ignores
_LONG = object()  # an array longer than PDF_MAX_ARRAY
_BIG = object()  # an integer of more than 12 digits


def _unescape(name: bytes) -> bytes:
    parts = name.split(b"#")
    out = [parts[0]]
    for part in parts[1:]:
        try:
            out.append(bytes([int(part[:2], 16)]) + part[2:] if len(part) >= 2 else b"#" + part)
        except ValueError:
            out.append(b"#" + part)
    return b"".join(out)


def _back_over_space(buf: bytes, i: int, lo: int) -> int:
    """``i`` moved back over whitespace and comments (a comment runs from its line's first ``%``), or -1
    when that cannot be decided within a short window. Errs toward finding a header."""
    for _ in range(4):
        segment = buf[max(lo, i - 64):i]
        rest = segment.rstrip(_WHITESPACE)
        if not rest and i - 64 > lo:
            return -1
        j = i - (len(segment) - len(rest))
        space = segment[len(rest):]
        if b"\n" not in space and b"\r" not in space:
            return j  # no line end, so no comment ends here
        start = max(lo, j - 4_096)
        found = max(buf.rfind(b"\n", start, j), buf.rfind(b"\r", start, j))
        if found < 0 and start > lo:
            return -1
        percent = buf.find(b"%", found + 1 if found >= 0 else lo, j)
        if percent < 0:
            return j
        i = percent
    return -1


def _header_before(buf: bytes, p: int, lo: int) -> bool:
    """Whether ``N G`` (two integers, whitespace or comments between) could end just before ``obj`` at
    ``p``, within ``buf[lo:p]``. Errs toward yes."""
    i = p
    for _ in range(2):  # the generation, then the object number
        j = _back_over_space(buf, i, lo)
        if j < 0:
            return True
        if j == i:
            return False
        digits = len(buf[max(lo, j - 11):j]) - len(buf[max(lo, j - 11):j].rstrip(_DIGITS))
        if not 0 < digits <= 10:
            return False
        i = j - digits
    return i == lo or _KIND[buf[i - 1]] != _REGULAR


def _keyword_at(buf: bytes, p: int) -> bool:
    """Whether ``obj`` at ``p`` is the keyword ``obj`` or ``endobj``, not part of a longer word or a name."""
    if p + 3 < len(buf) and _KIND[buf[p + 3]] == _REGULAR:
        return False
    if p >= 3 and buf[p - 3:p] == b"end":
        p -= 3
    return p == 0 or (_KIND[buf[p - 1]] != _REGULAR and buf[p - 1] != 0x2F)


class _Frame:
    """An array or dictionary being read whose value is kept (the bottom frame: a run of values)."""

    __slots__ = ("closer", "items", "key", "pending", "long")

    def __init__(self, closer: bytes | None, dictionary: bool = False) -> None:
        self.closer = closer
        self.items: Any = {} if dictionary else []
        self.key: bytes | None = None  # a dictionary's key awaiting its value
        self.pending: list[int] = []  # a dictionary value's integers, which may still become a reference
        self.long = False


class _Reader:
    """Every object of one PDF, every version: the file's own in one pass, then each object stream's."""

    def __init__(self, pdf: bytes) -> None:
        self.pdf = pdf
        self.steps = PDF_MAX_STEPS
        self.pause = PDF_MAX_STEPS - PDF_YIELD_STEPS  # the step count at which to yield next
        self.room = PDF_INFLATE_BUDGET  # inflated bytes left for object streams
        self.defined: dict[int, list] = {}  # object number -> its value in every version read
        self.versions: list[tuple[int, dict]] = []  # every page-tree-shaped dictionary, every version
        self.streams: list[tuple[dict, int, int]] = []  # stream objects that may be object streams
        self.lengths: list[tuple[int, int]] = []  # indirect /Length objects used, and the length taken
        self.shapes: dict[tuple, dict] = {}  # one dictionary per distinct kept content ({/Type /Page})
        self.integers: dict[int, list[int]] | None = None
        # The lexer's position, buffer and whether skipped bytes are checked for object headers
        self.buf, self.at, self.end, self.check = pdf, 0, len(pdf), True

    # Lexing ---------------------------------------------------------------------------------------

    def step(self) -> None:
        self.steps -= 1
        if self.steps < self.pause:
            self.yield_or_stop()

    def yield_or_stop(self) -> None:
        if self.steps < 0:
            raise _Uncertain
        self.pause = max(0, self.pause - PDF_YIELD_STEPS)  # at 0, the next step past the budget stops
        time.sleep(PDF_YIELD_SECONDS)

    def token(self) -> tuple[int, Any]:
        buf, i, end = self.buf, self.at, self.end
        while i < end:
            c = buf[i]
            kind = _KIND[c]
            if kind == _WHITE:
                i = _WHITE_RUN.match(buf, i, end).end()
            elif c == 0x25:  # %: a comment to the end of the line
                i = self.comment(i)
            else:
                break
        else:
            self.at = i
            return _END, None
        self.steps -= 1
        if self.steps < self.pause:
            self.yield_or_stop()
        if kind == _REGULAR:
            j = _REGULAR_RUN.match(buf, i, end).end()
            self.at = j
            text = buf[i:j]
            if text.isdigit():
                return _INT, int(text) if j - i <= 12 else _BIG
            if text[0] in b"+-" and text[1:].isdigit():
                return _INT, int(text) if j - i <= 13 else _BIG
            if not text.strip(b"0123456789.+-"):
                return _OTHER, _SKIPPED  # a real number
            return _WORD, text
        if c == 0x2F:  # /: a name
            match = _REGULAR_RUN.match(buf, i + 1, end)
            j = match.end() if match else i + 1
            self.at = j
            name = buf[i + 1:j]
            return _NAME, _unescape(name) if b"#" in name else name
        if c == 0x28:  # (
            self.at = self.string(i + 1)
            return _OTHER, _SKIPPED
        if c == 0x3C:  # <
            if buf[i + 1:min(i + 2, end)] == b"<":
                self.at = i + 2
                return _OPEN, b"<<"
            j = _HEX_RUN.match(buf, i + 1, end).end()
            if j >= end or buf[j] != 0x3E:
                raise _Uncertain
            self.at = j + 1
            return _OTHER, _SKIPPED
        if c == 0x3E:  # >
            if buf[i + 1:min(i + 2, end)] != b">":
                raise _Uncertain
            self.at = i + 2
            return _CLOSE, b">>"
        self.at = i + 1
        if c in (0x5B, 0x7B):  # [ {
            return _OPEN, bytes([c])
        if c in (0x5D, 0x7D):  # ] }
            return _CLOSE, bytes([c])
        raise _Uncertain  # a ")" outside a string

    def comment(self, i: int) -> int:
        self.step()
        match = _LINE_END.search(self.buf, i, self.end)
        stop = match.start() if match else self.end
        self.hidden(i, stop)
        return stop

    def string(self, i: int) -> int:
        """The end of a literal string whose content starts at ``i``."""
        buf, end, start, depth = self.buf, self.end, i, 1
        search = _STRING_STOP.search
        while True:
            match = search(buf, i, end)
            if match is None:
                raise _Uncertain
            self.step()
            p = match.start()
            c = buf[p]
            if c == 0x5C:  # a backslash escapes the next byte
                i = p + 2
                continue
            depth += 1 if c == 0x28 else -1
            i = p + 1
            if not depth:
                self.hidden(start, p)
                return i

    def hidden(self, lo: int, hi: int) -> None:
        """A byte range the lexer passes over (a stream body, a string, a comment) must hold no object header
        a reader could find by its offset: a lying /Length, or an object written inside a string."""
        if not self.check:
            return
        buf = self.buf
        p = buf.find(b"obj", lo, hi)
        while p >= 0:
            self.step()
            standalone = p > lo and _KIND[buf[p - 1]] != _REGULAR and (
                p + 3 >= len(buf) or _KIND[buf[p + 3]] != _REGULAR)
            if standalone and _header_before(buf, p, lo):
                raise _Uncertain
            p = buf.find(b"obj", p + 3, hi)

    def skip(self, closer: bytes) -> None:
        """Pass over a container whose value is not kept, from just after its opener: strings, names, comments
        and nesting are read, and every other byte is passed in C to the next delimiter."""
        buf, end, i = self.buf, self.end, self.at
        closers = [closer]
        search = _SKIP_STOP.search
        while closers:
            match = search(buf, i, end)
            if match is None:
                raise _Uncertain
            self.step()
            p = match.start()
            c = buf[p]
            if c == 0x6F:  # "obj": an object keyword never sits inside a container
                if _keyword_at(buf, p):
                    raise _Uncertain
                i = p + 3
            elif c == 0x25:
                i = self.comment(p)
            elif c == 0x28:
                i = self.string(p + 1)
            elif c == 0x3C:
                if buf[p + 1:min(p + 2, end)] == b"<":
                    closers.append(b">>")
                    i = p + 2
                else:
                    i = _HEX_RUN.match(buf, p + 1, end).end()
                    if i >= end or buf[i] != 0x3E:
                        raise _Uncertain
                    i += 1
            elif c == 0x3E:
                if buf[p + 1:min(p + 2, end)] != b">" or closers[-1] != b">>":
                    raise _Uncertain
                closers.pop()
                i = p + 2
            elif c in (0x5B, 0x7B):
                closers.append(b"]" if c == 0x5B else b"}")
                i = p + 1
            elif c in (0x5D, 0x7D):
                if closers[-1] != bytes([c]):
                    raise _Uncertain
                closers.pop()
                i = p + 1
            else:
                raise _Uncertain  # ")"
            if len(closers) > PDF_MAX_DEPTH:
                raise _Uncertain
        self.at = i

    def skip_value(self) -> None:
        """Pass over the value of a dictionary key the count does not read: regular tokens (a number, a
        reference, a keyword) up to the next delimiter, or one name, string or container."""
        buf, end = self.buf, self.end
        start = i = self.at
        while True:
            match = _VALUE_STOP.search(buf, i, end)
            p = match.start() if match else end
            self.step()
            if match is not None and buf[p] == 0x6F:  # "obj"
                if _keyword_at(buf, p):
                    raise _Uncertain
                i = p + 3
                continue
            if _NOT_WHITE.search(buf, start, p):
                self.at = p  # regular tokens: the delimiter after them starts the next key
                return
            if match is None:
                raise _Uncertain
            c = buf[p]
            if c == 0x25:
                start = i = self.comment(p)
                continue
            if c == 0x2F:
                match = _REGULAR_RUN.match(buf, p + 1, end)
                self.at = match.end() if match else p + 1
            elif c == 0x28:
                self.at = self.string(p + 1)
            elif c == 0x3C and buf[p + 1:min(p + 2, end)] == b"<":
                self.at = p + 2
                self.skip(b">>")
            elif c == 0x3C:
                i = _HEX_RUN.match(buf, p + 1, end).end()
                if i >= end or buf[i] != 0x3E:
                    raise _Uncertain
                self.at = i + 1
            elif c in (0x5B, 0x7B):
                self.at = p + 1
                self.skip(b"]" if c == 0x5B else b"}")
            else:
                raise _Uncertain  # a key without a value, or a stray closer
            return

    # Values ---------------------------------------------------------------------------------------

    def read(self, stops: frozenset[bytes], *, between: bool = False) -> tuple[list, bytes | None]:
        """Values up to the first of ``stops`` outside any container (returned with them), or to the end (None).
        Two integers then ``R`` are a reference. ``between`` objects only the last two values are kept, and
        containers (a trailer) are skipped."""
        stack = [_Frame(None)]
        while True:
            kind, value = self.token()
            frame = stack[-1]
            if kind == _END:
                if len(stack) > 1:
                    raise _Uncertain
                return frame.items, None
            if kind == _OPEN:
                closer = _CLOSERS[value]
                if not self.keeps(frame, between):
                    self.skip(closer)
                    self.put(frame, _SKIPPED)
                    continue
                if len(stack) > PDF_MAX_DEPTH:
                    raise _Uncertain
                stack.append(_Frame(closer, value == b"<<"))
                continue
            if kind == _CLOSE:
                if frame.closer != value:
                    raise _Uncertain
                stack.pop()
                value = self.close(frame)
                frame = stack[-1]
            elif kind == _WORD:
                if value == b"R":
                    self.fold(frame)
                    continue
                if value in _OBJECT_KEYWORDS:
                    if len(stack) == 1 and value in stops:
                        return frame.items, value
                    raise _Uncertain  # inside a container, or out of place
                if value == b"xref" and between:
                    self.at = _XREF_RUN.match(self.buf, self.at, self.end).end()
                value = _KEYWORDS.get(value, _SKIPPED)
            self.put(frame, value)
            if between and len(frame.items) > 2:
                del frame.items[:-2]
            elif frame.key is not None and frame.key not in _KEPT:
                self.skip_value()  # a key the count does not read: its value is passed over whole
                frame.key = None

    @staticmethod
    def keeps(frame: _Frame, between: bool) -> bool:
        """Whether a container opening in ``frame`` is kept (else skipped)."""
        if frame.closer is None:
            return not between
        if isinstance(frame.items, dict):
            if frame.key is None or frame.pending:
                raise _Uncertain  # a container as a key
            return frame.key in _KEPT
        return True

    def put(self, frame: _Frame, value: Any) -> None:
        if not isinstance(frame.items, dict):
            items = frame.items
            if len(items) >= 2 and type(items[-2]) is int and frame.closer is not None:
                items[-2] = _SKIPPED  # past the reach of an R: only its object matters
            items.append(value)
            if frame.closer is not None and len(items) > PDF_MAX_ARRAY:
                frame.long = True
                del items[:-2]
            return
        if frame.key is None:
            if not isinstance(value, bytes):
                raise _Uncertain  # a dictionary key must be a name
            frame.key = value
            return
        pending = frame.pending
        if type(value) is int:
            if len(pending) == 2:
                raise _Uncertain
            pending.append(value)
            return
        if pending:
            if len(pending) == 2:
                raise _Uncertain  # "/K 1 2 /L": the 2 would be a key
            self.commit(frame, pending.pop())
            if not isinstance(value, bytes):
                raise _Uncertain
            frame.key = value
            return
        self.commit(frame, value)

    @staticmethod
    def commit(frame: _Frame, value: Any) -> None:
        if frame.key in _KEPT:
            frame.items[frame.key] = value
        frame.key = None

    def fold(self, frame: _Frame) -> None:
        """``R``: the two integers before it are a reference."""
        if isinstance(frame.items, dict):
            if len(frame.pending) != 2:
                raise _Uncertain
            number = frame.pending[0]
            frame.pending.clear()
            self.commit(frame, _Ref(number))
            return
        items = frame.items
        if len(items) < 2 or type(items[-1]) is not int or type(items[-2]) is not int:
            raise _Uncertain
        del items[-1]
        items[-1] = _Ref(items[-1])

    def close(self, frame: _Frame) -> Any:
        if isinstance(frame.items, dict):
            if len(frame.pending) == 2:
                raise _Uncertain
            if frame.pending:
                self.commit(frame, frame.pending.pop())
            if frame.key is not None:
                raise _Uncertain  # a key without a value
            return frame.items
        return _LONG if frame.long else frame.items

    # Objects --------------------------------------------------------------------------------------

    def read_file(self) -> None:
        while True:
            items, stop = self.read(frozenset({b"obj"}), between=True)
            if stop is None:
                break
            if len(items) < 2 or type(items[-1]) is not int or type(items[-2]) is not int:
                raise _Uncertain
            number = items[-2]
            body, stop = self.read(frozenset({b"endobj", b"stream"}))
            if stop is None or len(body) > 1:
                raise _Uncertain
            value = body[0] if body else None
            if stop == b"stream":
                if not isinstance(value, dict):
                    raise _Uncertain
                start, end = self.stream(value)
                if b"Type" in value or b"First" in value:
                    self.streams.append((value, start, end))
                rest, stop = self.read(frozenset({b"endobj"}))
                if stop is None or rest:
                    raise _Uncertain
            self.add(number, value)
        for dictionary, start, end in self.streams:
            if b"ObjStm" in self.resolve_all(dictionary.get(b"Type")) or b"First" in dictionary:
                self.object_stream(dictionary, start, end)

    def add(self, number: int, value: Any) -> None:
        if isinstance(value, dict):
            try:
                shape = tuple(value.items())
                value = self.shapes.setdefault(shape, value)
            except TypeError:  # a list inside: not shared
                pass
            if any(key in value for key in _TREE_KEYS):
                self.versions.append((number, value))
        elif not isinstance(value, (int, bytes, list)) or isinstance(value, bool):
            value = None
        self.defined.setdefault(number, []).append(value)

    def resolve_all(self, value: Any) -> list:
        """What ``value`` can be: a reference's target in every version read (the xref names one, and the
        lexer does not know which), anything else as it is. A target not read is uncertain."""
        if not isinstance(value, _Ref):
            return [value]
        if value not in self.defined:
            raise _Uncertain
        return self.defined[value]

    def resolve(self, value: Any) -> Any:
        """``value`` resolved where every version must agree (a stream's filter, an object stream's table):
        versions that differ are uncertain."""
        values = self.resolve_all(value)
        if any(other != values[0] for other in values[1:]):
            raise _Uncertain
        return values[0]

    def stream(self, dictionary: dict) -> tuple[int, int]:
        """Pass over a stream's body: it must end where its /Length says, followed by ``endstream``."""
        pdf, body = self.pdf, self.at
        if pdf.startswith(b"\r\n", body):
            body += 2
        elif pdf[body:body + 1] in (b"\n", b"\r"):
            body += 1
        length = dictionary.get(b"Length")
        if type(length) is int:
            sizes, number = [length], None
        elif isinstance(length, _Ref):
            number = int(length)
            sizes = [self.resolve(length)] if number in self.defined else self.length_candidates(number)
            if any(type(size) is not int for size in sizes):
                raise _Uncertain
        else:
            raise _Uncertain
        for size in sizes:
            if size < 0:
                continue
            end = body + size
            match = _WHITE_RUN.match(pdf, end, min(len(pdf), end + 32))
            after = match.end() if match else end
            if pdf.startswith(b"endstream", after):
                self.hidden(body, end)
                if number is not None:
                    self.lengths.append((number, size))  # checked against the object's final value
                self.at = after + len(b"endstream")
                return body, end
        raise _Uncertain

    def length_candidates(self, number: int) -> list[int]:
        """Values of ``N G obj <integer> endobj`` objects numbered ``number`` (qpdf and PDFlib write a stream's
        length as an object after it), read backward from each ``endobj``, at most a few per number."""
        if self.integers is None:
            self.integers = {}
            pdf, at = self.pdf, 0
            while (p := pdf.find(b"endobj", at)) >= 0:
                at = p + 6
                self.step()
                found = _integer_object(pdf, p)
                if found is not None:
                    values = self.integers.setdefault(found[0], [])
                    if len(values) < PDF_LENGTH_CANDIDATES:
                        values.append(found[1])
        return self.integers.get(number, [])

    def object_stream(self, dictionary: dict, start: int, end: int) -> None:
        """Inflate an object stream and read its objects by its offset table."""
        filters = self.resolve(dictionary.get(b"Filter"))
        if isinstance(filters, list):
            filters = [self.resolve(f) for f in filters]
            filters = filters[0] if len(filters) == 1 else filters
        parameters = self.resolve(dictionary.get(b"DecodeParms"))
        if isinstance(parameters, list):
            parameters = [self.resolve(p) for p in parameters]
            parameters = parameters[0] if len(parameters) == 1 else parameters
        if isinstance(parameters, dict) and self.resolve(parameters.get(b"Predictor", 1)) not in (1, None):
            raise _Uncertain
        if parameters is not None and not isinstance(parameters, dict):
            raise _Uncertain
        room = min(PDF_STREAM_OUTPUT, self.room)
        if room <= 0:
            raise _Uncertain  # zlib reads a max_length of 0 as no limit: the budget is spent
        if filters is None:
            if end - start > room:
                raise _Uncertain
            data = self.pdf[start:end]
        elif filters == b"FlateDecode":
            inflater = zlib.decompressobj()
            try:
                data = inflater.decompress(memoryview(self.pdf)[start:end], room)
            except zlib.error:
                raise _Uncertain from None
            if inflater.unconsumed_tail or not inflater.eof:
                raise _Uncertain
        else:
            raise _Uncertain
        self.room -= len(data)
        count, first = self.resolve(dictionary.get(b"N")), self.resolve(dictionary.get(b"First"))
        if type(count) is not int or type(first) is not int or not 0 <= first <= len(data) or count < 0:
            raise _Uncertain
        saved = self.buf, self.at, self.end, self.check
        try:
            # No file offset points into inflated data, so nothing there is checked for hidden headers.
            self.buf, self.at, self.end, self.check = data, 0, first, False
            table, _ = self.read(frozenset())
            if len(table) != 2 * count or not all(type(n) is int for n in table):
                raise _Uncertain
            offsets = [(first + table[i + 1], table[i]) for i in range(0, len(table), 2)]
            bounds = sorted({offset for offset, _ in offsets} | {len(data)})
            for offset, number in offsets:
                if not first <= offset < len(data):
                    raise _Uncertain
                self.at, self.end = offset, bounds[bisect.bisect_right(bounds, offset)]
                values, _ = self.read(frozenset())
                if len(values) != 1:
                    raise _Uncertain
                self.add(number, values[0])
        finally:
            self.buf, self.at, self.end, self.check = saved

    # The count ------------------------------------------------------------------------------------

    def counted(self) -> PdfCount:
        for number, size in self.lengths:
            if any(type(value) is not int or value != size for value in self.defined.get(number, [None])):
                raise _Uncertain  # the stream was read at a length a version of its object does not have
        # Every version of each object, and of each object an indirect value names, may be the one the xref
        # names, so a page is any version typed /Page, a node's /Kids are each version's, and a /Count the
        # largest: a dead redefinition can only add.
        typed, nodes, roots = set(), set(), set()
        kids_of: dict[int, list[list]] = {}
        for number, dictionary in self.versions:
            kinds = self.resolve_all(dictionary.get(b"Type"))
            if b"Page" in kinds:
                typed.add(number)
            if b"Pages" in kinds:
                nodes.add(number)
            if b"Kids" in dictionary:
                for kids in self.resolve_all(dictionary[b"Kids"]):
                    if not isinstance(kids, list) or not all(isinstance(kid, _Ref) for kid in kids):
                        raise _Uncertain  # a direct page, or a tree that was not read
                    kids_of.setdefault(number, []).append(kids)
            if b"Pages" in dictionary:  # a catalog names the tree's root
                root = dictionary[b"Pages"]
                if isinstance(root, dict):
                    raise _Uncertain
                if isinstance(root, _Ref):
                    roots.add(int(root))
        # A dictionary with /Kids is a node when the tree reaches it (a reader takes any such kid as a node,
        # typed or not), so a form field's or a name tree's /Kids is never counted as pages.
        nodes |= roots & kids_of.keys()
        listed: set[int] = set()
        queue = list(nodes)
        while queue:
            for kids in kids_of.get(queue.pop(), ()):
                for kid in kids:
                    if kid not in listed:
                        listed.add(kid)
                        if kid in kids_of and kid not in nodes:
                            nodes.add(kid)
                            queue.append(kid)
        # A renderer counts every /Kids entry that reaches a page, so a page listed twice is two pages.
        walked = _tree_pages(kids_of, nodes, (roots & nodes) | (nodes - listed))
        pages = max(len(typed | (listed - nodes)), walked)
        if not pages:
            raise _Uncertain  # a PDF whose pages the reader cannot see
        largest = 0
        for number, dictionary in self.versions:
            if number in nodes and b"Count" in dictionary:
                for count in self.resolve_all(dictionary[b"Count"]):
                    if count is _BIG:
                        largest = PDF_MAX_PAGES
                    elif type(count) is int:
                        largest = max(largest, count)
                    else:
                        raise _Uncertain
        return PdfCount(min(PDF_MAX_PAGES, max(pages, largest)), True)


def _tree_pages(kids_of: dict[int, list[list]], nodes: set[int], roots: set[int]) -> int:
    """The most pages a renderer can reach from any of ``roots``: every /Kids entry that is not a node is a
    page, with multiplicity, and a node counts the pages under it in whichever of its versions holds the
    most (at most ``PDF_MAX_PAGES``). A cycle is uncertain. Iterative, each node's versions read once."""
    pages: dict[int, int] = {}
    for root in roots:
        stack, path = [root], set()
        while stack:
            node = stack[-1]
            if node in pages:
                stack.pop()
                continue
            below = [kid for kids in kids_of.get(node, ()) for kid in kids if kid in nodes and kid not in pages]
            if node not in path:
                path.add(node)
                if any(kid in path for kid in below):
                    raise _Uncertain  # a node lists itself, directly or further down
                stack.extend(below)
                continue
            pages[node] = min(PDF_MAX_PAGES, max(
                (sum(pages[kid] if kid in nodes else 1 for kid in kids) for kids in kids_of.get(node, ())),
                default=0))
            path.discard(node)
            stack.pop()
    return max((pages[root] for root in roots), default=0)


def _integer_object(pdf: bytes, p: int) -> tuple[int, int] | None:
    """``(N, value)`` when ``N G obj <value> endobj`` ends at the ``endobj`` at ``p``, read backward over
    single whitespace runs (no comments), else None."""

    def back(i: int, chars: bytes, most: int) -> int:
        segment = pdf[max(0, i - most - 1):i]
        return i - (len(segment) - len(segment.rstrip(chars)))

    i = back(p, _WHITESPACE, 64)
    j = back(i, _DIGITS, 12)
    if j == i or i - j > 12:
        return None
    value = int(pdf[j:i])
    i = back(j, _WHITESPACE, 64)
    if pdf[i - 3:i] != b"obj" or i - 3 < 0:
        return None
    k = back(i - 3, _WHITESPACE, 64)
    if k == i - 3:
        return None
    g = back(k, _DIGITS, 5)
    if g == k or k - g > 5:
        return None
    k = back(g, _WHITESPACE, 64)
    if k == g:
        return None
    n = back(k, _DIGITS, 10)
    if n == k or k - n > 10 or (n > 0 and _KIND[pdf[n - 1]] == _REGULAR):
        return None
    return int(pdf[n:k]), value


def count_pages(pdf: bytes) -> PdfCount:
    """The certain page count of a decoded PDF, or ``UNCERTAIN``. Never raises."""
    reader = _Reader(pdf)
    try:
        reader.read_file()
        return reader.counted()
    except (_Uncertain, RecursionError, ValueError, OverflowError, MemoryError, TypeError):
        return UNCERTAIN
