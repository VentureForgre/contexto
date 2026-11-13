"""The fake upstream's tokenizer. It is deliberately not the engine's estimator.

The engine counts text at 3.5 bytes a token and tool output at 1.5 until it has real usage, then
everything at one measured ratio (``contexto.tokens``). This tokenizer keeps two ratios for the whole
run, so the engine's one ratio is always a little wrong for one of the two kinds, as it is against the
real API:

- tool output (a Messages ``tool_result`` block, a Responses ``*_output`` item): 2.2 bytes a token;
- everything else (system, tools, prompts, thinking and its signature, tool calls): 3.1 bytes a token;
- an image: the contract's rule 3 price from its pixel size (``contexto.tokens.image_tokens``, D48 and
  D61), read from the image's own header, and 4,787 when the header cannot be read;
- 4 tokens for each message or item.

Bytes are those of the block's JSON as the proxy would write it (no spaces, non-ASCII kept), with
``cache_control`` and images left out. Each block is rounded up on its own.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from typing import Any

from contexto.tokens import IMAGE_MAX_TOKENS, image_size, image_tokens

TEXT_BYTES_PER_TOKEN = 3.1
TOOL_BYTES_PER_TOKEN = 2.2
ITEM_TOKENS = 4
IMAGE_TYPES = ("image", "input_image")  # Messages, Responses


def dumps(value: Any) -> str:
    """JSON as the proxy writes a request: insertion key order, no spaces, non-ASCII kept."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def text_tokens(byte_count: int) -> int:
    return math.ceil(byte_count / TEXT_BYTES_PER_TOKEN)


def tool_tokens(byte_count: int) -> int:
    return math.ceil(byte_count / TOOL_BYTES_PER_TOKEN)


def image_data(block: dict) -> str | None:
    """The base64 data of an inline image block (Messages ``source``, Responses ``image_url``)."""
    source = block.get("source")
    if isinstance(source, dict):
        data = source.get("data") if source.get("type") == "base64" else None
    else:
        data = block.get("image_url")
        if isinstance(data, str):
            data = data.partition(",")[2] if data.startswith("data:") else None
    return data if isinstance(data, str) and data else None


def image_cost(block: dict) -> int:
    data = image_data(block)
    if data is None:
        return IMAGE_MAX_TOKENS
    try:
        size = image_size(base64.b64decode(data[:64]))
    except (binascii.Error, ValueError):
        size = None
    if not size or min(size) <= 0:
        return IMAGE_MAX_TOKENS
    return image_tokens(*size)


def _is_image(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") in IMAGE_TYPES


def _without_images(parts: list) -> tuple[list, int]:
    """``parts`` with image blocks taken out, and their token cost."""
    cost = sum(image_cost(p) for p in parts if _is_image(p))
    return ([p for p in parts if not _is_image(p)] if cost else parts), cost


def _plain(block: dict) -> dict:
    return {k: v for k, v in block.items() if k != "cache_control"} if "cache_control" in block else block


def block_tokens(block: Any) -> int:
    """One Messages content block, or one Responses item or content part."""
    if _is_image(block):
        return image_cost(block)
    if not isinstance(block, dict):
        return text_tokens(len(dumps(block).encode()))
    block = _plain(block)
    kind = block.get("type")
    tool_output = kind == "tool_result" or (isinstance(kind, str) and kind.endswith("_output"))
    images = 0
    for key in ("content", "output"):  # where the two APIs allow images inside a block or item
        if isinstance(block.get(key), list):
            kept, cost = _without_images(block[key])
            if cost:
                block = {**block, key: kept}
                images += cost
    size = len(dumps(block).encode())
    return (tool_tokens(size) if tool_output else text_tokens(size)) + images


def message_tokens(message: dict) -> int:
    """One Messages API message: its blocks, plus the per-message tokens."""
    content = message.get("content")
    if isinstance(content, str):
        return ITEM_TOKENS + text_tokens(len(content.encode()))
    return ITEM_TOKENS + sum(block_tokens(b) for b in content or [])


def item_tokens(item: dict) -> int:
    """One Responses API input item."""
    return ITEM_TOKENS + block_tokens(item)


def value_tokens(value: Any) -> int:
    """A head field (``system``, ``tools``, ``instructions``) as text; 0 when it is absent."""
    if value is None:
        return 0
    if isinstance(value, str):
        return text_tokens(len(value.encode()))
    return text_tokens(len(dumps(value).encode()))
