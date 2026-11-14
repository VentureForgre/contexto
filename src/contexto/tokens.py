"""The token estimator both lanes use (M2 contract, rewrite engine rule 3).

Before a session has real usage, tool_result content (and a Codex tool's output item, D61) counts 1.5
bytes per token and all other text 3.5 (intake decision 2026-10-16 01:27Z, contract PR #44; tool output
such as numeric logs tokenizes densely, and M0 measured about 1.8 bytes per token for it, so the seed
errs toward overcounting). Once the proxy
reports real usage (``TokenEstimator.observe``), the session's cumulative measured bytes-per-token
ratio replaces both seeds.

Images (D48) are priced by what the model sees, not by their base64 bytes, which overcounted a
screenshot 55 to 270 times: an image costs ``width * height / 750`` tokens after the API's downscaling
to a 2,576-pixel long edge, or its 28-pixel patches plus 3 when that is more (D61), at most 4,787
(Anthropic's high-resolution tier; dimensions read from the PNG, JPEG, GIF, or WebP header). An image
whose size cannot be read (a URL, a file id, an unknown format, a JPEG whose frame header is not within
256 markers) gets the largest cost it can have, 4,787 tokens, since the bound depends on it.

A PDF is never estimated low (D60): it costs ``PDF_PAGE_TOKENS`` a page, its pages counted only when
certain (``contexto.pdfpages``), else ``PDF_MAX_PAGES``, the API's maximum. That covers a document by base64,
URL or file id and Codex ``input_file``; one by URL or file id, or too large or damaged to decode, is
never certain. Its bytes priced as text undercounted a compact PDF (0.1 to 1.7 KB a page) 2 to 50 times.

Media bytes stay out of every byte count the ratio divides, and the engine takes the media's lowest
plausible cost (``MediaCost.floor``, 0 for a document) off the real usage when no ratio is measured yet,
so neither a screenshot nor a PDF skews the ratio later text is estimated at.
"""

from __future__ import annotations

import base64
import binascii
import itertools
import math
import re
import struct
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple

from contexto.canonical import canonical_bytes, utf8
from contexto.pdfpages import PDF_MAX_PAGES, UNCERTAIN, PdfCount, count_pages

TEXT_BYTES_PER_TOKEN = 3.5
TOOL_RESULT_BYTES_PER_TOKEN = 1.5

# Images (D48). Anthropic documents tokens = width * height / 750 after downscaling: to a 2,576-pixel
# long edge and at most 4,784 tokens on the high-resolution tier (Opus 4.7 and later, Sonnet 5 and
# later), to 1,568 pixels and tokens on the models before it. count_tokens on claude-haiku-5-5 measured
# 641, 1,719, 2,694 and 3,243 for 800x600, 1440x900, 1920x1080 and 2000x1250 (formula: 640 to 3,334).
# Those four are exactly ceil(w / 28) * ceil(h / 28) + 3, 28-pixel patches and 3 more tokens (PR #234
# review A), which the pixel formula undercounts for a thin image (100 for 2576x29, against 187) and for
# some larger sizes (6.6% to 27.7% of sizes above 500 pixels an edge, by up to 34 to 91 tokens, depending
# on the sample; PR #250 review A); an image is priced at the higher of the two (D61). The 4,784-token
# maximum is 92 x 52 patches at the 2,576-pixel edge, so 4,787 with the 3.
PIXELS_PER_TOKEN = 750
PATCH_PIXELS = 28
PATCH_EXTRA_TOKENS = 3
IMAGE_MAX_EDGE = 2_576
IMAGE_MAX_TOKENS = 4_784 + PATCH_EXTRA_TOKENS
IMAGE_MIN_TOKENS = 64  # a tiny image is never priced near 0, as a few patches would
IMAGE_FLOOR_MAX_EDGE = 1_568  # the older models' downscaling: the lowest cost an image can have
IMAGE_FLOOR_MAX_TOKENS = 1_568
IMAGE_FLOOR_SHARE = 0.95  # measured costs ran up to 3% under the formula (2,694 for 2,765)
# PDFs (D60): the API sends each page as its text plus an image of the page. Anthropic documents 1,500 to
# 3,000 text tokens a page; a 3-page PDF measured 4,748 tokens (1,583 a page). A page is allowed the top
# of that range plus an image at the older 1,568-token limit, so a dense page (small-font code or tables)
# fits too.
PDF_PAGE_TOKENS = 3_000 + IMAGE_FLOOR_MAX_TOKENS
PDF_TYPICAL_PAGE_TOKENS = 1_583  # the measured page, for telling the allowance from the bound (D60)
# What D60 counts a PDF at when its pages are not certain: the API's maximum, so it is never estimated low.
UNCERTAIN_DOCUMENT_TOKENS = PDF_MAX_PAGES * PDF_PAGE_TOKENS
PDF_MAX_BASE64 = 32 << 20  # base64 characters a PDF is decoded from: the API's request limit
# PDFs whose counts are kept, least recently used dropped first; an entry is a key and a 2-tuple (under
# 300 bytes), so this costs at most about 19 MB, and no request holds enough PDFs to cycle it.
PDF_CACHE_SIZE = 65_536
PDF_PARSE_MAX_WAIT = 1.0  # seconds a PDF waits for smaller ones before it is parsed next (_SmallestFirst)
IMAGE_TYPES = frozenset({"image", "input_image"})  # Messages, Responses
HEAD_CHARS = 64  # base64 characters that hold the PNG, GIF and WebP dimensions
JPEG_HEAD_CHARS = 262_144  # a JPEG's frame header follows its EXIF and ICC segments: read up to 192 KB
MEDIA_KEYS = ("content", "output", "source")  # where a block or item can hold media
MEDIA_DEPTH = 10  # messages, message, content, tool_result, content, document, source, content, image

# D61: markers a JPEG's walk reads before giving up on finding the frame header. Real files reach it within a
# few dozen; crafted ones of fill bytes, standalone markers or 2-byte segments walked up to 196,608 bytes one
# marker at a time and priced 30 times slower than a normal JPEG on every request.
JPEG_MAX_MARKERS = 256

_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_JPEG_FILL = re.compile(rb"\xff+")
_NOT_BASE64 = re.compile(r"[^A-Za-z0-9+/]")
_PDF_PAGES: OrderedDict[tuple[int, int], PdfCount] = OrderedDict()  # (length, hash) of base64 data -> count
_PDF_PAGES_LOCK = threading.Lock()  # the proxy fills the cache outside its transform lock (prime_media)


class _SmallestFirst:
    """A lock for parsing one PDF at a time, granted to the waiter with the smallest PDF first (in arrival
    order among equals), except that a waiter that has waited ``PDF_PARSE_MAX_WAIT`` goes next, the
    longest-waiting first, so a large PDF is never kept waiting while smaller ones keep arriving. Parsing
    holds the GIL, and twenty parses at once starved another session's request of it (a text request
    waited 2.05 s behind 20 hostile PDFs; one at a time, pausing as the lexer does, 0.005 to 0.12 s; main
    0.014 to 0.58 s, its time spent reading the same bodies). Smallest first, an ordinary PDF waits for the
    parse in progress, not for twenty large ones queued before it (4.5 s in arrival order). A waiter that
    is interrupted takes itself out of the queue, so no later parse waits for it."""

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._busy = False
        self._granted: int | None = None  # the waiter chosen to go next, chosen once by the one releasing
        self._waiting: dict[int, tuple[int, float]] = {}  # ticket -> (size, when it started waiting)
        self._tickets = itertools.count()

    def _choose(self) -> None:
        if not self._waiting:
            self._granted = None
            return
        now = time.monotonic()
        aged = [(since, ticket) for ticket, (_, since) in self._waiting.items() if now - since >= PDF_PARSE_MAX_WAIT]
        self._granted = (min(aged)[1] if aged else
                         min(self._waiting, key=lambda ticket: (self._waiting[ticket][0], ticket)))
        self._changed.notify_all()

    @contextmanager
    def hold(self, size: int) -> Iterator[None]:
        with self._changed:
            ticket = next(self._tickets)
            self._waiting[ticket] = (size, time.monotonic())
            if not self._busy and self._granted is None:
                self._choose()
            try:
                while self._granted != ticket:
                    self._changed.wait()
            except BaseException:  # interrupted while waiting: out of the queue, and another chosen if it was next
                del self._waiting[ticket]
                if self._granted == ticket or (not self._busy and self._granted is None):
                    self._choose()
                raise
            del self._waiting[ticket]
            self._granted, self._busy = None, True
        try:
            yield
        finally:
            with self._changed:
                self._busy = False
                self._choose()


_PDF_PARSING = _SmallestFirst()


class MediaCost(NamedTuple):
    """Image and document blocks in a value: their canonical JSON bytes, their estimated tokens (errs
    high, for the bound), and their lowest plausible tokens (errs low, for the ratio). A document has no
    floor: a PDF's page can cost far under its allowance (a blank or image-only page)."""

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
    """The frame size from the first start-of-frame segment (SOF0 to SOF15 but DHT, JPG, and DAC), within
    ``JPEG_MAX_MARKERS`` markers; a run of fill bytes is skipped in one step (D61)."""
    i = 2
    for _ in range(JPEG_MAX_MARKERS):
        if i + 9 > len(head) or head[i] != 0xFF:
            return None
        i = _JPEG_FILL.match(head, i).end() - 1  # the last 0xFF before the marker
        if i + 9 > len(head):
            return None
        marker = head[i + 1]
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # segments without a length
            i += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", head[i + 5:i + 9])
            return width, height
        if marker == 0xDA:  # start of scan: no frame header came first
            return None
        i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    return None  # past the cap: a crafted file (fill runs, standalone markers, tiny segments), priced as unknown


def image_tokens(width: int, height: int, *, max_edge: int = IMAGE_MAX_EDGE, max_tokens: int = IMAGE_MAX_TOKENS,
                 min_tokens: int = IMAGE_MIN_TOKENS, patches: bool = True) -> int:
    """After scaling the long edge down to ``max_edge``, the higher of ``width * height / 750`` and, when
    ``patches``, ``ceil(width / 28) * ceil(height / 28) + 3`` (D61), between ``min_tokens`` and ``max_tokens``."""
    long = max(width, height)
    scale = min(1.0, max_edge / long)
    tokens = math.ceil(width * scale * height * scale / PIXELS_PER_TOKEN)
    if patches:  # each edge scaled in integers, rounded up, so the long one is max_edge exactly
        edges = [edge if long <= max_edge else -(-edge * max_edge // long) for edge in (width, height)]
        tokens = max(tokens, math.prod(-(-edge // PATCH_PIXELS) for edge in edges) + PATCH_EXTRA_TOKENS)
    return min(max_tokens, max(min_tokens, tokens))


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
    floor = image_tokens(width, height, max_edge=IMAGE_FLOOR_MAX_EDGE, max_tokens=IMAGE_FLOOR_MAX_TOKENS, min_tokens=0,
                         patches=False)
    return image_tokens(width, height), math.floor(floor * IMAGE_FLOOR_SHARE)


def pdf_page_count(data: str) -> PdfCount:
    """Pages of a base64 PDF (``contexto.pdfpages``): certain, or ``PDF_MAX_PAGES``. Never raises. Cached
    by content (least recently used dropped), since the engine estimates a history many times per request
    and the same PDF on every later request."""
    key = (len(data), hash(data))
    with _PDF_PAGES_LOCK:
        result = _PDF_PAGES.get(key)
        if result is not None:
            _PDF_PAGES.move_to_end(key)
            return result
    if len(data) > PDF_MAX_BASE64:  # never parsed, so it does not wait for the parse lock
        _cache_count(key, UNCERTAIN)
        return UNCERTAIN
    with _PDF_PARSING.hold(len(data)):  # cached before release, so a PDF is parsed once however many wait
        with _PDF_PAGES_LOCK:
            result = _PDF_PAGES.get(key)  # counted while this thread waited
        if result is None:
            try:
                pdf = _decode(data, len(data))
                result = count_pages(pdf) if pdf else UNCERTAIN
            except Exception:  # a hostile or damaged PDF must not break the transform (D60)
                result = UNCERTAIN
        _cache_count(key, result)
    return result


def _cache_count(key: tuple[int, int], result: PdfCount) -> None:
    with _PDF_PAGES_LOCK:
        _PDF_PAGES[key] = result
        _PDF_PAGES.move_to_end(key)
        while len(_PDF_PAGES) > PDF_CACHE_SIZE:
            _PDF_PAGES.popitem(last=False)


def _document_count(block: dict) -> PdfCount:
    data = _base64_payload(block)
    return pdf_page_count(data) if data is not None else UNCERTAIN  # a URL or file id: not in the request


def uncertain_documents(value: Any) -> int:
    """How many documents in ``value`` (a message or item list) count as ``PDF_MAX_PAGES`` because their
    pages are not certain (a PDF the reader could not read fully, or one by URL or file id)."""
    return sum(1 for block in split_media(value)[1]
               if _media_kind(block) == "document" and not _document_count(block).counted)


def uncertain_document_keys(value: Any) -> tuple:
    """The content key (``media_keys``) of every document in ``value`` that counts as ``PDF_MAX_PAGES``,
    in order, a PDF sent twice keyed twice."""
    return media_keys([block for block in split_media(value)[1]
                       if _media_kind(block) == "document" and not _document_count(block).counted])


def measured_documents(value: Any, costs: Any) -> tuple[int, int]:
    """For the uncertain PDFs in ``value``, given ``costs`` (content key to the tokens usage measured for
    that PDF, D63): how many tokens the estimate counts over their measured costs, and how many have no
    measured cost yet. The estimate less the first is the estimate with each measured PDF at its cost."""
    over = unmeasured = 0
    for key in uncertain_document_keys(value):
        cost = costs.get(key) if costs else None
        if cost is None:
            unmeasured += 1
        else:
            over += UNCERTAIN_DOCUMENT_TOKENS - cost
    return over, unmeasured


def pdf_allowance(value: Any) -> tuple[int, int]:
    """For the PDFs in ``value`` (a message or item list): the tokens by which the certain ones are estimated
    over a typical page cost (their allowance less 1,583 a page), and how many are not certain. A turn
    that fits its window with that taken off, and holds no uncertain PDF, falls short only by the PDFs'
    allowance (D60); an uncertain one's real size is unknown."""
    allowance = uncertain = 0
    for block in split_media(value)[1]:
        if _media_kind(block) == "document":
            count = _document_count(block)
            if count.counted:
                allowance += count.pages * (PDF_PAGE_TOKENS - PDF_TYPICAL_PAGE_TOKENS)
            else:
                uncertain += 1
    return allowance, uncertain


def prime_media(value: Any) -> None:
    """Count the pages of every base64 PDF in ``value`` (a request's message or item list) into the
    cache, so the engine finds them there. The proxy calls this, through ``RewriteEngine.prime``, before
    it takes its transform lock (D60), so a large or crafted PDF is parsed without holding up other
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


def media_cost(blocks: list[dict], *, measure_bytes: bool = False) -> MediaCost:
    """The cost of media blocks (from ``split_media``); their bytes only when asked, since that serializes
    them. A document costs ``PDF_PAGE_TOKENS`` a page and has floor 0 (D60)."""
    size = tokens = floor = 0
    for block in blocks:
        if _media_kind(block) == "image":
            high, low = _image_cost(block)
            tokens += high
            floor += low
        else:
            tokens += _document_count(block).pages * PDF_PAGE_TOKENS
        size += len(canonical_bytes(block)) if measure_bytes else 0
    return MediaCost(size, tokens, floor)


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
            if block is None:  # an image or document taken out by split_media
                continue
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


def is_tool_output(item: Any) -> bool:
    """A Responses item a tool returned (``function_call_output``, ``custom_tool_call_output``, ...)."""
    return isinstance(item, dict) and isinstance(item.get("type"), str) and item["type"].endswith("_output")


def estimate_item_tokens(items: list[Any], bytes_per_token: float | None = None) -> int:
    """A Responses item list as ``estimate_value_tokens`` prices it, except that before a ratio is measured
    a tool's output item counts at the tool-output seed, as a ``tool_result`` block does (D61)."""
    outputs = [item for item in items if is_tool_output(item)] if not bytes_per_token else []
    if not outputs:
        return estimate_value_tokens(items, bytes_per_token)
    rest = [item for item in items if not is_tool_output(item)]
    return ((estimate_value_tokens(rest) if rest else 0)
            + estimate_value_tokens(outputs, TOOL_RESULT_BYTES_PER_TOKEN))


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

    def estimate_items(self, items: list[Any]) -> int:
        return estimate_item_tokens(items, self.measured_bytes_per_token)
