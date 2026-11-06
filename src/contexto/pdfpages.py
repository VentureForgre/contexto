"""PDF page counts for the token estimate (D48): certain, or the API's maximum.

A PDF's pages are trusted only when the count is certain (round 8, the orchestrator's rule): every
object in the file, and every object in every object stream, was read by a PDF lexer (PDF whitespace
including NUL, comments, ``#xx`` escapes in names, any spacing, nested literal strings), every stream's
end was confirmed at its ``/Length``, and every object stream was inflated. The pages are then the object
numbers whose ``/Type`` (resolved when indirect) is ``/Page``, each once over every version, or the
``/Kids`` references that are not themselves page tree nodes, whichever is more; any ``/Count`` may raise
that, never stand in for a part that was not read. Anything else counts as ``PDF_MAX_PAGES``, the only
value that is always safe: an object stream not inflated (a bound, another filter, a predictor, damaged
data), a stream whose end is not where its ``/Length`` says, an object header inside a skipped stream body
(a ``/Length`` that lies long), a token the lexer cannot read, an indirect ``/Type`` or ``/Count`` whose
target was not read, a bound passed, or no pages in a file that has objects.

The work is linear and bounded: one pass over the file outside stream bodies, one over each inflated
object stream, at most ``PDF_MAX_TOKENS`` tokens, ``PDF_MAX_OBJECTS`` objects, ``PDF_MAX_DEPTH`` nesting and
``PDF_INFLATE_BUDGET`` inflated bytes per PDF.
"""

from __future__ import annotations

import bisect
import re
import zlib
from typing import Any, NamedTuple

PDF_MAX_PAGES = 600  # the API's limit, and the count of any PDF that is not certain
PDF_STREAM_OUTPUT = 4 << 20  # inflated bytes per object stream
PDF_INFLATE_BUDGET = 16 << 20  # inflated bytes per PDF: object streams hold dictionaries, not page content
PDF_MAX_TOKENS = 1_000_000  # tokens read per PDF, object streams included (a 600-page PDF with 12,000 links: 407,000)
PDF_MAX_OBJECTS = 200_000  # objects read per PDF, every version
PDF_MAX_DEPTH = 64  # arrays and dictionaries inside one another
PDF_LENGTH_CANDIDATES = 8  # definitions tried for an indirect /Length
PDF_MAX_KID_REFS = 50_000  # /Kids references kept per PDF, every version (a 600-page tree lists about 1,200)
_KEPT = frozenset({b"Type", b"Count", b"Kids", b"Length", b"N", b"First", b"Filter", b"DecodeParms"})

_WHITE = rb"\x00\t\n\x0c\r "  # PDF whitespace, as a regex class body
_WHITESPACE = frozenset(b"\x00\t\n\x0c\r ")
_DELIMITERS = rb"()<>\[\]{}/%"
_TOKEN = re.compile(  # whitespace and comments, then one token (or the end)
    rb"(?:[" + _WHITE + rb"]|%[^\r\n]*)*(?:"
    rb"(?P<name>/[^" + _WHITE + _DELIMITERS + rb"]*)"
    rb"|(?P<number>[+-]?(?:\d+\.?\d*|\.\d+))(?=[" + _WHITE + _DELIMITERS + rb"]|\Z)"
    rb"|(?P<open><<|\[|\{)"
    rb"|(?P<close>>>|\]|\})"
    rb"|(?P<hex><[0-9A-Fa-f" + _WHITE + rb"]*>)"
    rb"|(?P<string>\()"
    rb"|(?P<word>[^" + _WHITE + _DELIMITERS + rb"]+)"
    rb"|(?P<end>\Z))"
)
_STRING_STOP = re.compile(rb"[()\\]")
_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_HIDDEN_OBJECT = re.compile(rb"endobj[" + _WHITE + rb"]+\d+[" + _WHITE + rb"]+\d+[" + _WHITE + rb"]+obj(?![A-Za-z])")
_INTEGER_OBJECT = re.compile(rb"(?<![0-9])(\d{1,9})[" + _WHITE + rb"]+\d{1,5}[" + _WHITE + rb"]+obj[" + _WHITE
                             + rb"]*(\d{1,12})[" + _WHITE + rb"]*endobj")
_PAIRS = {b"<<": b">>", b"[": b"]", b"{": b"}"}
_KEYWORDS = {b"true": True, b"false": False, b"null": None}


class PdfCount(NamedTuple):
    """A PDF's pages for the estimate, whether they are certain (else ``PDF_MAX_PAGES``), and a count that
    errs low for the ratio's floor (D48)."""

    pages: int
    counted: bool
    floor_pages: int = 1


UNCERTAIN = PdfCount(PDF_MAX_PAGES, False)


class _Uncertain(Exception):
    """The count cannot be certain."""


class _Name(bytes):
    """A PDF name, ``#xx`` escapes decoded, without the slash."""


class _Ref(NamedTuple):
    number: int
    generation: int


class _Word(bytes):
    """A keyword the lexer read (``obj``, ``endobj``, ``stream``, an operator)."""


_STRING = object()  # a literal or hex string: its content is not needed


class _Lexer:
    """PDF tokens from ``buffer[start:end]``, counted against the PDF's shared ``budget`` (a one-item list)."""

    def __init__(self, buffer: bytes, start: int, end: int, budget: list[int]) -> None:
        self.buffer, self.at, self.end, self.budget = buffer, start, end, budget

    def next(self) -> tuple[str, Any] | None:
        if self.at < self.end:
            match = _TOKEN.match(self.buffer, self.at, self.end)
            if match is None:
                raise _Uncertain  # a ")" or a lone ">" outside a string
            kind = match.lastgroup
            self.at = match.end()
            if kind == "end":
                return None
            self.budget[0] -= 1
            if self.budget[0] < 0:
                raise _Uncertain
            text = match.group(kind)
            if kind == "name":
                name = text[1:]
                if b"#" in name:
                    name = _NAME_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), name)
                return "value", _Name(name)
            if kind == "number":
                return "value", int(text) if text.lstrip(b"+-").isdigit() else float(text)
            if kind == "string":
                self.at = self._string_end(self.at)
                return "value", _STRING
            if kind == "hex":
                return "value", _STRING
            if kind in ("open", "close"):
                return kind, text
            return "word", _Word(text)
        return None

    def _string_end(self, at: int) -> int:
        depth = 1
        while depth:
            stop = _STRING_STOP.search(self.buffer, at, self.end)
            if stop is None:
                raise _Uncertain
            char = self.buffer[stop.start()]
            if char == 0x5C:  # a backslash escapes the next byte
                at = stop.start() + 2
                continue
            depth += 1 if char == 0x28 else -1
            at = stop.end()
        return at


def _read(lexer: _Lexer, stops: frozenset[bytes]) -> tuple[list, bytes | None]:
    """Values up to the first of ``stops`` read outside any array or dictionary (returned), or to the end
    (None). Two integers followed by ``R`` are a reference."""
    stack: list[tuple[bytes, list]] = [(b"", [])]
    while True:
        token = lexer.next()
        if token is None:
            if len(stack) > 1:
                raise _Uncertain
            return stack[0][1], None
        kind, value = token
        if kind == "open":
            if len(stack) > PDF_MAX_DEPTH:
                raise _Uncertain
            stack.append((value, []))
            continue
        if kind == "close":
            if len(stack) == 1 or _PAIRS[stack[-1][0]] != value:
                raise _Uncertain
            opener, items = stack.pop()
            value = _dictionary(items) if opener == b"<<" else items
        elif kind == "word":
            items = stack[-1][1]
            if value == b"R":
                if len(items) < 2 or type(items[-1]) is not int or type(items[-2]) is not int:
                    raise _Uncertain
                generation = items.pop()
                items.append(_Ref(items.pop(), generation))
                continue
            if len(stack) == 1 and value in stops:
                return items, bytes(value)
            value = _KEYWORDS.get(value, value)
        stack[-1][1].append(value)


def _dictionary(items: list) -> dict:
    if len(items) % 2 or not all(isinstance(key, _Name) for key in items[0::2]):
        raise _Uncertain
    return dict(zip(items[0::2], items[1::2], strict=True))


class _Object(NamedTuple):
    number: int
    value: Any  # a dictionary, an integer or a name; anything else as None
    stream: tuple[int, int] | None  # the body of a stream object, in the file


class _Reader:
    """Every object of one PDF, every version, in file order (object streams after the file's own)."""

    def __init__(self, pdf: bytes) -> None:
        self.pdf = pdf
        self.budget = [PDF_MAX_TOKENS]
        self.objects: list[_Object] = []
        self.latest: dict[int, _Object] = {}
        self.room = PDF_INFLATE_BUDGET  # inflated bytes left for object streams
        self.kid_refs = PDF_MAX_KID_REFS  # /Kids references left to keep
        self._integers: dict[int, list[int]] | None = None

    def read(self) -> None:
        lexer = _Lexer(self.pdf, 0, len(self.pdf), self.budget)
        while True:
            items, stop = _read(lexer, frozenset({b"obj"}))
            if stop is None:
                break
            if len(items) < 2 or type(items[-1]) is not int or type(items[-2]) is not int:
                raise _Uncertain
            number = items[-2]
            body, stop = _read(lexer, frozenset({b"endobj", b"stream"}))
            if stop is None or len(body) > 1:
                raise _Uncertain
            value = body[0] if body else None
            span = None
            if stop == b"stream":
                if not isinstance(value, dict):
                    raise _Uncertain
                span = self._stream(lexer, value)
                rest, stop = _read(lexer, frozenset({b"endobj"}))
                if stop is None or rest:
                    raise _Uncertain
            self._add(_Object(number, value, span))
        for stream in [o for o in self.objects if o.stream is not None]:
            if self.resolve(stream.value.get(b"Type")) == b"ObjStm":
                self._object_stream(stream)

    def _add(self, entry: _Object) -> None:
        if len(self.objects) >= PDF_MAX_OBJECTS:
            raise _Uncertain
        value = entry.value
        if isinstance(value, dict):  # only what the count reads is kept, so memory follows the tree
            value = {key: item for key, item in value.items() if key in _KEPT}
            kids = value.get(b"Kids")
            if isinstance(kids, list):
                self.kid_refs -= len(kids)
                if self.kid_refs < 0:
                    raise _Uncertain
        elif not isinstance(value, (int, _Name)) or isinstance(value, bool):
            value = None
        entry = entry._replace(value=value)
        self.objects.append(entry)
        self.latest[entry.number] = entry

    def resolve(self, value: Any) -> Any:
        """A reference's target, as read last; anything else as it is. A target not read is uncertain."""
        if not isinstance(value, _Ref):
            return value
        target = self.latest.get(value.number)
        if target is None:
            raise _Uncertain
        return target.value

    def _stream(self, lexer: _Lexer, dictionary: dict) -> tuple[int, int]:
        """Skip a stream's body: its end must be where its /Length says, then ``endstream``."""
        pdf, body = self.pdf, lexer.at
        if pdf[body:body + 2] == b"\r\n":
            body += 2
        elif pdf[body:body + 1] in (b"\n", b"\r"):
            body += 1
        length = dictionary.get(b"Length")
        lengths = [length] if type(length) is int else self._indirect_length(length)
        for size in lengths:
            if size < 0:
                continue
            end = body + size
            after = end
            while after < len(pdf) and pdf[after] in _WHITESPACE and after - end < 32:
                after += 1
            if pdf.startswith(b"endstream", after):
                if _HIDDEN_OBJECT.search(pdf, body, end):  # an object inside a body: a /Length that lies long
                    raise _Uncertain
                lexer.at = after + len(b"endstream")
                return body, end
        raise _Uncertain

    def _indirect_length(self, length: Any) -> list[int]:
        if not isinstance(length, _Ref):
            raise _Uncertain
        target = self.latest.get(length.number)
        if target is not None and type(target.value) is int:
            return [target.value]
        if self._integers is None:  # qpdf writes a stream's length as an object after it
            self._integers = {}
            for match in _INTEGER_OBJECT.finditer(self.pdf):
                found = self._integers.setdefault(int(match.group(1)), [])
                if len(found) < PDF_LENGTH_CANDIDATES:
                    found.append(int(match.group(2)))
        return self._integers.get(length.number, [])

    def _object_stream(self, stream: _Object) -> None:
        """Inflate an object stream and read its objects by its offset table."""
        dictionary = stream.value
        filters = self.resolve(dictionary.get(b"Filter"))
        if isinstance(filters, list):
            filters = [self.resolve(f) for f in filters]
            filters = filters[0] if len(filters) == 1 else filters
        parameters = self.resolve(dictionary.get(b"DecodeParms"))
        if isinstance(parameters, list):
            parameters = parameters[0] if len(parameters) == 1 else parameters
        if isinstance(parameters, dict) and self.resolve(parameters.get(b"Predictor", 1)) not in (1, None):
            raise _Uncertain
        start, end = stream.stream
        if filters is None:
            data = self.pdf[start:end]
        elif filters == b"FlateDecode":
            room = min(PDF_STREAM_OUTPUT, self.room)
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
        if self.room < 0:
            raise _Uncertain
        count, first = self.resolve(dictionary.get(b"N")), self.resolve(dictionary.get(b"First"))
        if type(count) is not int or type(first) is not int or not 0 <= first <= len(data) or count < 0:
            raise _Uncertain
        table, _ = _read(_Lexer(data, 0, first, self.budget), frozenset())
        if len(table) != 2 * count or not all(type(n) is int for n in table):
            raise _Uncertain
        offsets = [(first + table[i + 1], table[i]) for i in range(0, len(table), 2)]
        bounds = sorted({offset for offset, _ in offsets} | {len(data)})
        for offset, number in offsets:
            if not first <= offset < len(data):
                raise _Uncertain
            stop = bounds[bisect.bisect_right(bounds, offset)]
            values, _ = _read(_Lexer(data, offset, stop, self.budget), frozenset())
            if len(values) != 1:
                raise _Uncertain
            self._add(_Object(number, values[0], None))


def count_pages(pdf: bytes) -> PdfCount:
    """The certain page count of a decoded PDF, or ``UNCERTAIN``."""
    reader = _Reader(pdf)
    try:
        reader.read()
        return _counted(reader)
    except (_Uncertain, RecursionError, ValueError, OverflowError):
        return UNCERTAIN


def _counted(reader: _Reader) -> PdfCount:
    types: dict[int, set] = {}
    for entry in reader.objects:
        if isinstance(entry.value, dict) and b"Type" in entry.value:
            types.setdefault(entry.number, set()).add(reader.resolve(entry.value[b"Type"]))
    pages = {number for number, kinds in types.items() if b"Page" in kinds}
    nodes = {number for number, kinds in types.items() if b"Pages" in kinds}
    largest, kids = 0, set()
    for entry in reader.objects:
        if entry.number not in nodes or not isinstance(entry.value, dict):
            continue
        count = reader.resolve(entry.value.get(b"Count", 0))
        if type(count) is not int:
            raise _Uncertain
        largest = max(largest, count)
        listed = reader.resolve(entry.value.get(b"Kids", []))
        if not isinstance(listed, list):
            raise _Uncertain
        kids.update(kid.number for kid in listed if isinstance(kid, _Ref) and kid.number not in nodes)
    found = max(len(pages), len(kids))
    if not found:
        raise _Uncertain  # a PDF whose pages the reader cannot see
    drawn = len(pages & kids)  # typed pages a tree lists; none listed (or none typed) floors at one page
    floor = min(drawn, largest) if largest else drawn
    return PdfCount(min(PDF_MAX_PAGES, max(found, largest)), True, max(1, min(floor, PDF_MAX_PAGES)))
