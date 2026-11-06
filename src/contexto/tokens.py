"""The token estimator both lanes use (M2 contract, rewrite engine rule 3).

Before a session has real usage, tool_result content counts 1.5 bytes per token and all other
text 3.5 (intake decision 2026-10-16 01:27Z, contract PR #44; tool output such as numeric logs tokenizes densely, and
M0 measured about 1.8 bytes per token for it, so the seed errs toward overcounting). Once the proxy
reports real usage (``TokenEstimator.observe``), the session's cumulative measured bytes-per-token
ratio replaces both seeds.

Images and documents (D48) are priced by what the model sees, not by their base64 bytes, which
overcounted a screenshot 55 to 270 times: an image costs ``width * height / 750`` tokens after the
API's downscaling to a 2,576-pixel long edge, at most 4,784 (Anthropic's high-resolution tier;
dimensions read from the PNG, JPEG, GIF, or WebP header), and a PDF ``PDF_PAGE_TOKENS`` per page,
its pages never counted low: the largest count the file offers (round 7). Media whose size cannot be
read (a URL, a file id, an unknown format) gets the largest cost one can have, and a PDF whose pages
cannot be counted a guess of one page per 3 KB: every fallback errs high, since the bound depends on
it. Parsing a PDF is bounded in time and memory and never raises.
Media bytes stay out of every byte count the ratio divides, and the engine takes the media's lowest
plausible cost (``MediaCost.floor``) off the real usage before it updates the ratio, so a screenshot
never skews the ratio later text is estimated at.
"""

from __future__ import annotations

import base64
import binascii
import bisect
import math
import re
import struct
import threading
import zlib
from collections import OrderedDict
from typing import Any, NamedTuple

from contexto.canonical import canonical_bytes, utf8

TEXT_BYTES_PER_TOKEN = 3.5
TOOL_RESULT_BYTES_PER_TOKEN = 1.5

# Images (D48). Anthropic documents tokens = width * height / 750 after downscaling: to a 2,576-pixel
# long edge and at most 4,784 tokens on the high-resolution tier (Opus 4.7 and later, Sonnet 5 and
# later), to 1,568 pixels and tokens on the models before it. count_tokens on claude-haiku-5-5 measured
# 641, 1,719, 2,694 and 3,243 for 800x600, 1440x900, 1920x1080 and 2000x1250 (formula: 640 to 3,334).
PIXELS_PER_TOKEN = 750
IMAGE_MAX_EDGE = 2_576
IMAGE_MAX_TOKENS = 4_784
IMAGE_MIN_TOKENS = 64  # a thin or tiny image: patch rounding is not modelled, so never price it near 0
IMAGE_FLOOR_MAX_EDGE = 1_568  # the older models' downscaling: the lowest cost an image can have
IMAGE_FLOOR_MAX_TOKENS = 1_568
IMAGE_FLOOR_SHARE = 0.95  # measured costs ran up to 3% under the formula (2,694 for 2,765)
# PDFs: each page is sent as its text plus an image of the page. A 3-page PDF measured 4,748 tokens
# (1,583 a page); Anthropic documents 1,500 to 3,000 text tokens a page, and the page image comes on
# top, so a page is priced at 3,000 plus an image at the older 1,568-token limit. Typical pages cost
# far less; a dense page (small-font code or tables) can cost more than 3,000 text tokens.
PDF_PAGE_TOKENS = 3_000 + IMAGE_FLOOR_MAX_TOKENS
PDF_FLOOR_PAGE_TOKENS = 1_000
PDF_GUESS_BYTES_PER_PAGE = 3_000  # a page count that cannot be read: the most compact text pages, so it errs high
PDF_MAX_PAGES = 600  # the API's limit; also the cap on every count and guess
DOCUMENT_UNKNOWN_PAGES = 10  # a document by URL or file id: its size is not in the request
# Bounds on the work for one PDF (the engine runs under the proxy's one transform lock, and a PDF can
# come from an untrusted file): beyond any of them the page count is a guess, which errs high.
PDF_MAX_BASE64 = 32 << 20  # characters decoded: the API's request limit
PDF_MAX_OBJECT_STREAMS = 128  # object streams inflated
PDF_STREAM_INPUT = 1 << 20  # compressed bytes read for one object stream
PDF_STREAM_OUTPUT = 2 << 20  # inflated bytes per object stream
PDF_INFLATE_BUDGET = 4 << 20  # inflated bytes per PDF: object streams hold dictionaries, not page content
PDF_HEADER_WINDOW = 2_048  # bytes searched around /ObjStm for its object start and stream keyword
PDF_MAX_PAGE_OBJECTS = 10_000  # /Type /Page matches read per PDF, object streams included
PDF_MAX_TREE_NODES = 1_000  # /Type /Pages matches read per PDF, object streams included
PDF_TREE_WINDOW = 8_192  # bytes searched on each side of /Type /Pages for its /Count (a 600-page /Kids)
PDF_OBJECT_WINDOW = 8_192  # bytes searched back from /Type /Page for its "N G obj" header
PDF_MAX_KID_REFS = 20_000  # /Kids references read per PDF for the floor; past it the floor counts fewer pages
IMAGE_TYPES = frozenset({"image", "input_image"})  # Messages, Responses
HEAD_CHARS = 64  # base64 characters that hold the PNG, GIF and WebP dimensions
JPEG_HEAD_CHARS = 262_144  # a JPEG's frame header follows its EXIF and ICC segments: read up to 192 KB
# PDFs whose counts are kept, least recently used dropped first. An entry is a key and three ints (288
# bytes measured), so this costs at most about 19 MB, and no request holds enough PDFs to cycle it.
PDF_CACHE_SIZE = 65_536
MEDIA_KEYS = ("content", "output", "source")  # where a block or item can hold media
MEDIA_DEPTH = 10  # messages, message, content, tool_result, content, document, source, content, image

# Every repetition is bounded, so each scan is linear in the PDF's size; numbers have at most 9 digits.
_PAGE_OBJECT = re.compile(rb"/Type\s{0,16}/Page(?![A-Za-z])")
_PAGE_TREE = re.compile(rb"/Type\s{0,16}/Pages\b")
_COUNT = re.compile(rb"/Count\s{0,16}(\d{1,9})(?!\d)")
_REFERENCE = re.compile(rb"(?<!\d)(\d{1,9})\s{1,16}\d{1,5}\s{1,16}R\b")
_OBJECT_HEADER = re.compile(rb"(?<!\d)(\d{1,9})\s{1,16}\d{1,5}\s{1,16}obj\Z")  # searched up to "obj"
_OBJECT_TABLE = re.compile(rb"(\d{1,9})\s{1,16}(\d{1,9})(?!\d)")
_FIRST = re.compile(rb"/First\s{1,16}(\d{1,9})(?!\d)")
_OBJECT_STREAM = re.compile(rb"/Type\s{0,16}/ObjStm\b")
_FLATE_ONLY = re.compile(rb"/Filter\s{0,16}(?:/FlateDecode\b|\[\s{0,16}/FlateDecode\s{0,16}\])")
# /Length 15 0 R is indirect: its value is elsewhere, so the stream is read up to PDF_STREAM_INPUT.
_DIRECT_LENGTH = re.compile(rb"/Length\s{1,16}(\d{1,9})(?!\d)(?!\s{1,16}\d{1,9}\s{1,16}R)")
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_NOT_BASE64 = re.compile(r"[^A-Za-z0-9+/]")
_PDF_PAGES: OrderedDict[tuple[int, int], PdfCount] = OrderedDict()  # (length, hash) of base64 data -> count
_PDF_PAGES_LOCK = threading.Lock()  # the proxy primes the cache outside its transform lock (prime_media)


class PdfCount(NamedTuple):
    """A PDF's pages for the estimate (errs high), whether they were counted rather than guessed, and
    a count that errs low for the ratio's floor (D48)."""

    pages: int
    counted: bool
    floor_pages: int = 1


class MediaCost(NamedTuple):
    """Image and document blocks in a value: their canonical JSON bytes, their estimated tokens
    (errs high, for the bound), and their lowest plausible tokens (errs low, for the ratio)."""

    bytes: int = 0
    tokens: int = 0
    floor: int = 0


def _size(data: str | bytes) -> int:
    return len(utf8(data)) if isinstance(data, str) else len(data)


def _tokens(n: int, ratio: float) -> int:
    return math.ceil(n / ratio) if n else 0


def estimate_tokens(data: str | bytes, *, tool_result: bool = False,
                    bytes_per_token: float | None = None) -> int:
    """Estimated tokens for ``data``, rounded up so non-empty input counts at least one token."""
    ratio = bytes_per_token or (TOOL_RESULT_BYTES_PER_TOKEN if tool_result else TEXT_BYTES_PER_TOKEN)
    return _tokens(_size(data), ratio)


# Media ----------------------------------------------------------------------------------------------

def _media_kind(node: dict) -> str | None:
    """``image`` or ``document`` for a block the model sees as media, else None. A document whose
    source is plain text or content blocks is text, and is measured as such."""
    kind = node.get("type")
    if kind in IMAGE_TYPES:
        return "image"
    if kind == "document":
        source = node.get("source")
        return None if isinstance(source, dict) and source.get("type") in ("text", "content") else "document"
    if kind == "input_file":
        data = node.get("file_data")
        return None if isinstance(data, str) and data.startswith("data:text/") else "document"
    return None


def split_media(node: Any, depth: int = 0) -> tuple[Any, list[dict]]:
    """``node`` (a message or item list, a message or item, or a block) with every image and document
    block replaced by None, and those blocks. Media is looked for only where the APIs allow it: in
    ``content`` and ``output`` lists, and a document's ``source`` (a tool's input is text even when it
    holds an image-shaped object), at most ``MEDIA_DEPTH`` levels down. Only the containers on the way
    to a media block are copied; a value without media is returned as is."""
    if depth > MEDIA_DEPTH:
        return node, []
    if isinstance(node, dict):
        if _media_kind(node):
            return None, [node]
        found: list[dict] = []
        copy: dict | None = None
        for key in MEDIA_KEYS:
            value = node.get(key)
            if isinstance(value, (dict, list)):
                stripped, media = split_media(value, depth + 1)
                if media:
                    copy = copy if copy is not None else dict(node)
                    copy[key] = stripped
                    found.extend(media)
        return (node if copy is None else copy), found
    if isinstance(node, list):
        found = []
        items: list | None = None
        for i, value in enumerate(node):
            if isinstance(value, dict):
                stripped, media = split_media(value, depth + 1)
                if media:
                    items = items if items is not None else list(node)
                    items[i] = stripped
                    found.extend(media)
        return (node if items is None else items), found
    return node, []


def _base64_payload(block: dict) -> str | None:
    """The base64 data of an inline image or file: a Messages ``base64`` source or a Responses data URL."""
    source = block.get("source")
    if isinstance(source, dict):
        data = source.get("data") if source.get("type") == "base64" else None
    else:
        data = block.get("image_url") if block.get("type") == "input_image" else block.get("file_data")
        if isinstance(data, dict):  # the Chat Completions shape {"url": ...}
            data = data.get("url")
        if isinstance(data, str):
            data = data.partition(",")[2] if data.startswith("data:") and ";base64," in data[:100] else None
    return data if isinstance(data, str) and data else None


def _decode(data: str, chars: int) -> bytes:
    """The first ``chars`` characters of base64 data, decoded; line breaks and other stray characters
    are dropped first, so wrapped (MIME) base64 decodes too."""
    head = data[:chars]
    if not _BASE64.fullmatch(head):
        head = _NOT_BASE64.sub("", head)
    head = head[: len(head) - len(head) % 4]
    try:
        return base64.b64decode(head)
    except (binascii.Error, ValueError):
        return b""


def image_size(head: bytes) -> tuple[int, int] | None:
    """(width, height) from the first bytes of a PNG, GIF, WebP, or JPEG file, or None."""
    try:
        if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", head[6:10])
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            chunk = head[12:16]
            if chunk == b"VP8 ":  # lossy: 14-bit sizes after the frame tag and start code
                width, height = struct.unpack("<HH", head[26:30])
                return width & 0x3FFF, height & 0x3FFF
            if chunk == b"VP8L":  # lossless: 14-bit sizes minus one, after the signature byte
                bits = int.from_bytes(head[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":  # extended: 24-bit canvas sizes minus one
                return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
            return None
        if head[:2] == b"\xff\xd8":
            return _jpeg_size(head)
    except struct.error:
        return None
    return None


def _jpeg_size(head: bytes) -> tuple[int, int] | None:
    """The frame size from the first start-of-frame segment (SOF0 to SOF15 but DHT, JPG, and DAC)."""
    i = 2
    while i + 9 <= len(head):
        if head[i] != 0xFF:
            return None
        marker = head[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # segments without a length
            i += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", head[i + 5:i + 9])
            return width, height
        if marker == 0xDA:  # start of scan: no frame header came first
            return None
        i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    return None


def image_tokens(width: int, height: int, *, max_edge: int = IMAGE_MAX_EDGE, max_tokens: int = IMAGE_MAX_TOKENS,
                 min_tokens: int = IMAGE_MIN_TOKENS) -> int:
    """``width * height / 750`` after scaling the long edge down to ``max_edge``, between ``min_tokens``
    and ``max_tokens``."""
    scale = min(1.0, max_edge / max(width, height))
    return min(max_tokens, max(min_tokens, math.ceil(width * scale * height * scale / PIXELS_PER_TOKEN)))


def _image_cost(block: dict) -> tuple[int, int]:
    data = _base64_payload(block)
    size = None
    if data is not None:
        try:
            head = _decode(data, HEAD_CHARS)
            if head[:2] == b"\xff\xd8":
                head = _decode(data, JPEG_HEAD_CHARS)
            size = image_size(head)
        except Exception:  # a damaged header must not break the transform: priced as unknown
            size = None
    if not size or min(size) <= 0:
        return IMAGE_MAX_TOKENS, 0  # a URL, a file id, or a header that cannot be read
    width, height = size
    floor = image_tokens(width, height, max_edge=IMAGE_FLOOR_MAX_EDGE, max_tokens=IMAGE_FLOOR_MAX_TOKENS, min_tokens=0)
    return image_tokens(width, height), math.floor(floor * IMAGE_FLOOR_SHARE)


def pdf_pages(data: str) -> int:
    return pdf_page_count(data).pages


def pdf_page_count(data: str) -> PdfCount:
    """Pages of a base64 PDF, between 1 and ``PDF_MAX_PAGES``. Never raises: any failure is a guess.
    Cached by content (least recently used dropped), since the engine estimates a history many times
    per request and the same PDF on every later request."""
    key = (len(data), hash(data))
    with _PDF_PAGES_LOCK:
        result = _PDF_PAGES.get(key)
        if result is not None:
            _PDF_PAGES.move_to_end(key)
            return result
    try:
        result = _count_pdf_pages(data)
    except Exception:  # a hostile or damaged PDF must not break the transform (D48)
        result = PdfCount(_guess_pages(len(data) * 3 // 4), False)
    with _PDF_PAGES_LOCK:
        _PDF_PAGES[key] = result
        while len(_PDF_PAGES) > PDF_CACHE_SIZE:
            _PDF_PAGES.popitem(last=False)
    return result


def _guess_pages(size: int) -> int:
    return min(PDF_MAX_PAGES, max(1, math.ceil(size / PDF_GUESS_BYTES_PER_PAGE)))


def _count_pdf_pages(data: str) -> PdfCount:
    """A PDF is never counted low (D48, round 7): its pages are the larger of its page objects, wherever
    they are (content streams and object streams too, every version, each object number once) and the
    largest ``/Count`` of any page tree node, at most ``PDF_MAX_PAGES``. Pages an incremental save deleted,
    or a node no catalog reaches, count too. When an object stream was not read (a bound, another filter,
    encryption), pages may hide in it: the page tree's count stands then, else the API's maximum. A PDF
    with neither pages nor a tree, or that cannot be decoded, is a guess of one page per 3 KB. The floor
    is the numbered page objects a ``/Kids`` array lists, at most the largest count, at least one."""
    if len(data) > PDF_MAX_BASE64:
        return PdfCount(PDF_MAX_PAGES, False)
    pdf = _decode(data, len(data))
    if not pdf:
        return PdfCount(_guess_pages(len(data) * 3 // 4), False)
    scan = _PageScan()
    streams = _object_stream_pages(pdf, scan)
    # The file is scanned whole, stream bodies included, but for object streams stored without a filter,
    # whose objects the pass above counted by number.
    complete = not scan.capped and scan.add(pdf, skip=scan.stored) and streams
    found, tree = len(scan.pages), scan.tree
    if scan.capped:  # more page objects or tree nodes than a document has: crafted, so trust neither
        return PdfCount(PDF_MAX_PAGES, False)
    floor = scan.floor_pages()
    if not complete:
        return PdfCount(min(PDF_MAX_PAGES, max(found, tree)), True, floor) if tree else PdfCount(PDF_MAX_PAGES, False)
    if found or tree:
        return PdfCount(min(PDF_MAX_PAGES, max(found, tree)), True, floor)
    return PdfCount(_guess_pages(len(pdf)), False)


class _PageScan:
    """Page objects (by object number), the page tree's largest count, and the object numbers its
    ``/Kids`` arrays reference, over one PDF's buffers. The caps hold for the whole PDF, so object
    streams cannot multiply them."""

    def __init__(self) -> None:
        self.pages: set = set()
        self.kids: set[int] = set()
        self.tree = 0
        self.capped = False
        self.page_matches = 0
        self.tree_matches = 0
        self.kid_refs = 0  # /Kids references read, at most PDF_MAX_KID_REFS
        self.stored: list[tuple[int, int]] = []  # spans of object streams stored without a filter, in order

    def add(self, buffer: bytes, numbers: tuple[list[int], list[int]] | None = None,
            skip: list[tuple[int, int]] = ()) -> bool:
        """Scan one buffer: the PDF, or an object stream's content with its offset table (``numbers``:
        offsets, object numbers). Objects inside a ``skip`` span were counted with their stream. False,
        and ``capped``, once the PDF passes ``PDF_MAX_PAGE_OBJECTS`` or ``PDF_MAX_TREE_NODES``, which no
        real document reaches."""
        # Only the gaps between skipped spans are searched, so skipped bytes cost nothing.
        gaps, at = [], 0
        for start, end in skip:
            if start > at:
                gaps.append((at, start))
            at = max(at, end)
        gaps.append((at, len(buffer)))

        def matches(pattern: re.Pattern) -> Any:
            for low, high in gaps:
                yield from pattern.finditer(buffer, low, high)

        for match in matches(_PAGE_OBJECT):
            self.page_matches += 1
            if self.page_matches > PDF_MAX_PAGE_OBJECTS:
                self.capped = True
                return False
            self.pages.add(self._number(buffer, match.start(), numbers))
        for match in matches(_PAGE_TREE):
            self.tree_matches += 1
            if self.tree_matches > PDF_MAX_TREE_NODES:
                self.capped = True
                return False
            count, kids = _node(buffer, match.start(), match.end(), kids=self.kid_refs <= PDF_MAX_KID_REFS)
            self.tree = max(self.tree, count)
            self.kid_refs += len(kids)
            if self.kid_refs <= PDF_MAX_KID_REFS:  # past it, the floor counts fewer pages, which is safe
                self.kids.update(kids)
        return True

    def floor_pages(self) -> int:
        """Pages the floor may count: numbered page objects that a ``/Kids`` array references, at most the
        tree's count when there is one, and at least 1 (D48). This can still run high (deleted or orphaned
        pages); the ratio rules in ``contexto.rewrite.text_usage`` do not rely on it."""
        drawn = sum(1 for page in self.pages if isinstance(page, int) and page in self.kids)
        return max(1, min(drawn, self.tree, PDF_MAX_PAGES) if self.tree else min(drawn, PDF_MAX_PAGES))

    @staticmethod
    def _number(buffer: bytes, at: int, numbers: tuple[list[int], list[int]] | None) -> Any:
        """The object number of the page whose ``/Type /Page`` is at ``at``: from the stream's offset
        table, or the ``N G obj`` header before it; else its position, so it still counts once."""
        if numbers is not None:
            index = bisect.bisect_right(numbers[0], at) - 1
            return numbers[1][index] if index >= 0 else (id(buffer), at)
        begin = buffer.rfind(b"obj", max(0, at - PDF_OBJECT_WINDOW), at)
        header = _OBJECT_HEADER.search(buffer, max(0, begin - 48), begin + 3) if begin >= 0 else None
        return int(header.group(1)) if header is not None else (id(buffer), at)


def _node(buffer: bytes, start: int, end: int, *, kids: bool = True) -> tuple[int, list[int]]:
    """The ``/Count`` and (unless ``kids`` is False) the ``/Kids`` references of the ``/Type /Pages``
    dictionary at ``start``: each the nearest after it, else before it, without crossing a ``>`` (a page
    tree node holds no nested dictionary between them), within ``PDF_TREE_WINDOW``, so the work per node
    is bounded."""
    stop = buffer.find(b">", end, end + PDF_TREE_WINDOW)
    after = (end, stop if stop >= 0 else min(len(buffer), end + PDF_TREE_WINDOW))
    gt = buffer.rfind(b">", max(0, start - PDF_TREE_WINDOW), start)
    before = (gt + 1 if gt >= 0 else max(0, start - PDF_TREE_WINDOW), start)
    count = 0
    for low, high in (after, before):
        found = _COUNT.search(buffer, low, high)
        if found is not None:
            count = int(found.group(1))
            break
    references: list[int] = []
    for low, high in (after, before) if kids else ():
        at = buffer.find(b"/Kids", low, high)
        opening = buffer.find(b"[", at, min(high, at + 32)) if at >= 0 else -1
        closing = buffer.find(b"]", opening, high) if opening >= 0 else -1
        if closing >= 0:
            references = [int(m.group(1)) for m in _REFERENCE.finditer(buffer, opening, closing)]
            break
    return count, references


def _object_stream_pages(pdf: bytes, scan: _PageScan) -> bool:
    """Scan a PDF's object streams, where PDF 1.5 and later can keep their page objects and page tree.
    Each stream is read (Flate inflated, or as stored when it has no filter, as PyMuPDF writes them),
    scanned with its offset table, and dropped in turn, within the ``PDF_*`` bounds; a stored stream's span
    is recorded in ``scan.stored`` so the scan of the file itself does not count its page objects twice.
    False
    when one was not read: another filter, a predictor, encryption, or past a bound."""
    view = memoryview(pdf)
    budget = PDF_INFLATE_BUDGET
    for n, match in enumerate(_OBJECT_STREAM.finditer(pdf)):
        if n >= PDF_MAX_OBJECT_STREAMS or budget <= 0:
            return False
        keyword = pdf.find(b"stream", match.end(), match.end() + PDF_HEADER_WINDOW)
        begin = pdf.rfind(b"obj", max(0, match.start() - PDF_HEADER_WINDOW), match.start())
        if keyword < 0 or begin < 0:
            return False
        header = pdf[begin:keyword]
        body = keyword + 6
        body += 2 if pdf[body:body + 2] == b"\r\n" else 1 if pdf[body:body + 1] in (b"\n", b"\r") else 0
        length = _DIRECT_LENGTH.search(header)
        if b"/Filter" not in header:
            end = body + int(length.group(1)) if length else pdf.find(b"endstream", body, body + PDF_STREAM_INPUT)
            if end < body or end - body > min(PDF_STREAM_OUTPUT, budget):
                return False
            out = pdf[body:end]
            scan.stored.append((body, end))
        else:
            if not _FLATE_ONLY.search(header) or b"/Predictor" in header:
                return False
            end = body + min(int(length.group(1)) if length else PDF_STREAM_INPUT, PDF_STREAM_INPUT)
            inflater = zlib.decompressobj()
            try:
                out = inflater.decompress(view[body:end], min(PDF_STREAM_OUTPUT, budget))
            except zlib.error:
                return False
            if inflater.unconsumed_tail or not inflater.eof:  # an output bound, or a stream longer than read
                return False
        budget -= len(out)
        if not scan.add(out, _object_table(header, out)):
            return False
    return True


def _object_table(header: bytes, out: bytes) -> tuple[list[int], list[int]] | None:
    """An object stream's (offsets, object numbers), offsets into the inflated data, or None."""
    first = _FIRST.search(header)
    if first is None or int(first.group(1)) > len(out):
        return None
    start = int(first.group(1))
    pairs = [(start + int(m.group(2)), int(m.group(1))) for m in _OBJECT_TABLE.finditer(out, 0, start)]
    pairs.sort()
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _document_cost(block: dict) -> tuple[int, int]:
    data = _base64_payload(block)
    if data is None:  # a URL or file id
        return DOCUMENT_UNKNOWN_PAGES * PDF_PAGE_TOKENS, PDF_FLOOR_PAGE_TOKENS
    count = pdf_page_count(data)
    return count.pages * PDF_PAGE_TOKENS, count.floor_pages * PDF_FLOOR_PAGE_TOKENS


def prime_media(value: Any) -> None:
    """Count the pages of every base64 PDF in ``value`` (a request's message or item list) into the
    cache, so the engine finds them there. The proxy calls this, through ``RewriteEngine.prime``, before
    it takes its transform lock (D48), so a large or crafted PDF is parsed without holding up other
    sessions. Never raises."""
    try:
        for block in split_media(value)[1]:
            if _media_kind(block) == "document" and (data := _base64_payload(block)) is not None:
                pdf_page_count(data)
    except Exception:  # priming is an optimisation; the engine counts again under the lock if it failed
        return


def media_keys(blocks: list[dict]) -> tuple:
    """A content key per media block (from ``split_media``), in order: two requests with equal keys carry
    the same media (D48). Base64 data is keyed by its length and hash, any other source by its JSON."""
    keys = []
    for block in blocks:
        data = _base64_payload(block)
        keys.append((len(data), hash(data)) if data is not None else hash(canonical_bytes(block)))
    return tuple(keys)


class MediaPart(NamedTuple):
    """One media block of a request: its content key, whether it is a document, its estimate and floor."""

    key: Any
    document: bool
    tokens: int
    floor: int


def media_parts(blocks: list[dict]) -> tuple[MediaPart, ...]:
    """Each media block's key, kind, estimate and floor."""
    parts = []
    for block, key in zip(blocks, media_keys(blocks), strict=True):
        document = _media_kind(block) != "image"
        high, low = _document_cost(block) if document else _image_cost(block)
        parts.append(MediaPart(key, document, high, low))
    return tuple(parts)


def media_cost(blocks: list[dict], *, measure_bytes: bool = False) -> MediaCost:
    """The cost of media blocks (from ``split_media``); their bytes only when asked, since that
    serializes them."""
    parts = media_parts(blocks)
    size = sum(len(canonical_bytes(block)) for block in blocks) if measure_bytes else 0
    return MediaCost(size, sum(p.tokens for p in parts), sum(p.floor for p in parts))


# Messages -------------------------------------------------------------------------------------------

class MessageSizes(NamedTuple):
    """A message list's text bytes, tool_result bytes (media left out of both), and media tokens."""

    text: int
    tool: int
    media_tokens: int


def measure_messages(messages: list[dict[str, Any]]) -> MessageSizes:
    text = tool = media = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text += _size(content)
            continue
        for block in content or []:
            stripped, found = split_media(block)
            if found:
                media += media_cost(found).tokens
                size = len(canonical_bytes(stripped)) if stripped is not None else 0
            else:
                size = len(canonical_bytes(block))
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool += size
            else:
                text += size
    return MessageSizes(text, tool, media)


def message_bytes(messages: list[dict[str, Any]]) -> tuple[int, int]:
    """(text bytes, tool_result bytes) of a message list, measured on canonical JSON per block."""
    text = tool = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text += _size(content)
            continue
        for block in content or []:
            size = len(canonical_bytes(block))
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool += size
            else:
                text += size
    return text, tool


def estimate_message_tokens(messages: list[dict[str, Any]], bytes_per_token: float | None = None) -> int:
    text, tool, media = measure_messages(messages)
    if bytes_per_token:
        return _tokens(text + tool, bytes_per_token) + media
    return _tokens(text, TEXT_BYTES_PER_TOKEN) + _tokens(tool, TOOL_RESULT_BYTES_PER_TOKEN) + media


def estimate_value_tokens(value: Any, bytes_per_token: float | None = None) -> int:
    """Any JSON value (a Responses item list) as text, with its media priced as media."""
    stripped, found = split_media(value)
    return (estimate_tokens(canonical_bytes(stripped), bytes_per_token=bytes_per_token)
            + (media_cost(found).tokens if found else 0))


class TokenEstimator:
    """Per-session estimator: the defaults until real usage arrives, then the measured ratio."""

    def __init__(self) -> None:
        self.total_bytes = 0
        self.total_tokens = 0

    @property
    def measured_bytes_per_token(self) -> float | None:
        """Cumulative ratio over every observed request, or None before the first."""
        return self.total_bytes / self.total_tokens if self.total_tokens else None

    def observe(self, request_bytes: int, input_tokens: int) -> None:
        """Add a request's byte size and its real input tokens (uncached, cache read, and cache write),
        both without the request's media (D48)."""
        if request_bytes > 0 and input_tokens > 0:
            self.total_bytes += request_bytes
            self.total_tokens += input_tokens

    def estimate(self, data: str | bytes, *, tool_result: bool = False) -> int:
        return estimate_tokens(data, tool_result=tool_result, bytes_per_token=self.measured_bytes_per_token)

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        return estimate_message_tokens(messages, self.measured_bytes_per_token)

    def estimate_value(self, value: Any) -> int:
        return estimate_value_tokens(value, self.measured_bytes_per_token)
