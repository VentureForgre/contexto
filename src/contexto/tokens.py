"""The token estimator both lanes use (M2 contract, rewrite engine rule 3).

Before a session has real usage, tool_result content counts 1.5 bytes per token and all other
text 3.5 (intake decision 2026-10-16 01:27Z, contract PR #44; tool output such as numeric logs tokenizes densely, and
M0 measured about 1.8 bytes per token for it, so the seed errs toward overcounting). Once the proxy
reports real usage (``TokenEstimator.observe``), the session's cumulative measured bytes-per-token
ratio replaces both seeds.

Images (D48) are priced by what the model sees, not by their base64 bytes, which overcounted a
screenshot 55 to 270 times: an image costs ``width * height / 750`` tokens after the API's downscaling
to a 2,576-pixel long edge, at most 4,784 (Anthropic's high-resolution tier; dimensions read from the
PNG, JPEG, GIF, or WebP header). An image whose size cannot be read (a URL, a file id, an unknown
format) gets the largest cost it can have, 4,784 tokens, since the bound depends on it. Documents (a
PDF by base64, URL or file id; Codex ``input_file``) are still estimated by their bytes as text, as
before D48, which errs high; pricing them by page awaits a separate decision.
Media bytes stay out of every byte count the ratio divides, and the engine takes the media's lowest
plausible cost (``MediaCost.floor``, 0 for a document) off the real usage before it updates the ratio,
so neither a screenshot nor a PDF skews the ratio later text is estimated at.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import struct
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
IMAGE_TYPES = frozenset({"image", "input_image"})  # Messages, Responses
HEAD_CHARS = 64  # base64 characters that hold the PNG, GIF and WebP dimensions
JPEG_HEAD_CHARS = 262_144  # a JPEG's frame header follows its EXIF and ICC segments: read up to 192 KB
MEDIA_KEYS = ("content", "output", "source")  # where a block or item can hold media
MEDIA_DEPTH = 10  # messages, message, content, tool_result, content, document, source, content, image

_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_NOT_BASE64 = re.compile(r"[^A-Za-z0-9+/]")


class MediaCost(NamedTuple):
    """Image and document blocks in a value, for the measured ratio: their canonical JSON bytes, their
    estimated tokens (errs high), and their lowest plausible tokens (errs low). A document counts its
    bytes as tokens (it is estimated by its bytes, at most one token a byte) and has no floor."""

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


def split_media(node: Any, depth: int = 0, *, documents: bool = True) -> tuple[Any, list[dict]]:
    """``node`` (a message or item list, a message or item, or a block) with every image and document
    block replaced by None, and those blocks; with ``documents`` False, images only (the estimate prices
    a document by its bytes, D48). Media is looked for only where the APIs allow it: in
    ``content`` and ``output`` lists, and a document's ``source`` (a tool's input is text even when it
    holds an image-shaped object), at most ``MEDIA_DEPTH`` levels down. Only the containers on the way
    to a media block are copied; a value without media is returned as is."""
    if depth > MEDIA_DEPTH:
        return node, []
    if isinstance(node, dict):
        kind = _media_kind(node)
        if kind == "image" or (kind and documents):
            return None, [node]
        found: list[dict] = []
        copy: dict | None = None
        for key in MEDIA_KEYS:
            value = node.get(key)
            if isinstance(value, (dict, list)):
                stripped, media = split_media(value, depth + 1, documents=documents)
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
                stripped, media = split_media(value, depth + 1, documents=documents)
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


def media_keys(blocks: list[dict]) -> tuple:
    """A content key per media block (from ``split_media``), in order: two requests with equal keys carry
    the same media (D48). Base64 data is keyed by its length and hash, any other source by its JSON."""
    keys = []
    for block in blocks:
        data = _base64_payload(block)
        keys.append((len(data), hash(data)) if data is not None else hash(canonical_bytes(block)))
    return tuple(keys)


def media_cost(blocks: list[dict], *, measure_bytes: bool = False) -> MediaCost:
    """The cost of media blocks (from ``split_media``). An image's bytes are counted only when asked, since
    that serializes it; a document's always, since they are its estimate."""
    size = tokens = floor = 0
    for block in blocks:
        if _media_kind(block) == "image":
            high, low = _image_cost(block)
            tokens += high
            floor += low
            size += len(canonical_bytes(block)) if measure_bytes else 0
        else:
            length = len(canonical_bytes(block))
            size += length
            tokens += length
    return MediaCost(size, tokens, floor)


# Messages -------------------------------------------------------------------------------------------

class MessageSizes(NamedTuple):
    """A message list's text bytes, tool_result bytes (images left out of both), and image tokens."""

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
            stripped, found = split_media(block, documents=False)
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
    """Any JSON value (a Responses item list) as text, with its images priced as images."""
    stripped, found = split_media(value, documents=False)
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
        both without the request's images and documents (D48)."""
        if request_bytes > 0 and input_tokens > 0:
            self.total_bytes += request_bytes
            self.total_tokens += input_tokens

    def estimate(self, data: str | bytes, *, tool_result: bool = False) -> int:
        return estimate_tokens(data, tool_result=tool_result, bytes_per_token=self.measured_bytes_per_token)

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        return estimate_message_tokens(messages, self.measured_bytes_per_token)

    def estimate_value(self, value: Any) -> int:
        return estimate_value_tokens(value, self.measured_bytes_per_token)
