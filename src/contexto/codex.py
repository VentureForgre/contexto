"""Codex adapter (M2 lane A, packet A5): the OpenAI Responses API dialect for the rewrite engine.

Shapes measured on codex-cli 0.154.0 with ``gpt-6-astra`` (zero-spend probes, spikes/m2/codex/, and
docs/research/M2-codex-adapter.md). A custom provider sends ``POST <base>/responses`` with the full
history in ``input`` on every request, ``store: false``, and no ``previous_response_id``. The first
items, before the user's first prompt, are the lead: an ``additional_tools`` developer item, the
base-instructions developer message, tagged developer context (permissions, collaboration mode, and
similar), and a user message carrying ``<environment_context>`` (and ``# AGENTS.md instructions``
when present). Later items are the conversation: user prompts, assistant messages, ``reasoning``
items with ``encrypted_content``, and tool calls with their outputs.

Mapping onto the contract's rules (``contexto.rewrite``):

- Turn start: a user ``message`` with authored content (a part that is not a context section).
- Lead: every item before the first turn start. It stays first in every upstream request and is
  never paged (the analogue of ``system`` and of the session-start message).
- System-like items: developer or system messages, the ``additional_tools`` item, and user messages
  made only of context sections. After the lead they are carried (other text, contract step 4),
  since no Codex hook-output signature is documented yet; non-text ones are unclassified.
- Thinking (rule 2): ``reasoning`` items of retained turns that existed when the epoch began are
  dropped. Dropping a reasoning item alone keeps call pairing valid; turns are paged whole, so a
  ``function_call`` and its output stay together.
- Preamble (rule 1): an ``input_text`` part prepended to the first retained turn start.
- Head (rule 6): top-level ``instructions`` and ``tools`` plus the lead items.
- Usage: ``response.usage.input_tokens`` already includes cached tokens, so it is the input total.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from contexto.canonical import canonical_bytes, canonical_json
from contexto.rewrite import CARRIED_TEXT, SESSION_START, UNCLASSIFIED, Dialect
from contexto.tokens import TokenEstimator

MODEL_OUTPUT_TYPES = frozenset({"reasoning", "function_call", "custom_tool_call", "local_shell_call",
                                "web_search_call", "image_generation_call", "tool_search_call"})
TEXT_PART_TYPES = frozenset({"input_text", "output_text", "text"})
# Context sections Codex injects as messages (measured tags, and AGENTS.md per the 0.154.0 source).
CONTEXT_SECTION = re.compile(r"\s*(<[A-Za-z_][\w -]*>|# AGENTS\.md instructions)")


def _parts(item: dict) -> list:
    content = item.get("content")
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    return content if isinstance(content, list) else []


def _is_message(item: dict) -> bool:
    return item.get("type", "message") == "message"


def is_context_part(part: Any) -> bool:
    return (isinstance(part, dict) and part.get("type") in TEXT_PART_TYPES
            and isinstance(part.get("text"), str) and bool(CONTEXT_SECTION.match(part["text"])))


class ResponsesDialect(Dialect):
    name = "responses"
    field = "input"

    def is_context_block(self, block: Any) -> bool:
        return False  # Codex sends context as separate messages, not inside the prompt message

    def is_turn_start(self, item: dict) -> bool:
        if not _is_message(item) or item.get("role") != "user":
            return False
        parts = _parts(item)
        return any(isinstance(p, dict) and not is_context_part(p) for p in parts)

    def is_assistant(self, item: dict) -> bool:
        return item.get("type") in MODEL_OUTPUT_TYPES or (_is_message(item) and item.get("role") == "assistant")

    def is_system_like(self, item: dict) -> bool:
        if item.get("type") == "additional_tools":
            return True
        if not _is_message(item):
            return False
        if item.get("role") in ("developer", "system"):
            return True
        parts = _parts(item)
        return item.get("role") == "user" and bool(parts) and all(is_context_part(p) for p in parts)

    def classify_system(self, items: list[dict], i: int) -> str:
        if i < self.lead_count(items):
            return SESSION_START
        if item_is_text_only(items[i]):
            return CARRIED_TEXT
        return UNCLASSIFIED

    def normalise(self, item: dict) -> dict:
        return item

    def head_fingerprint(self, request: dict) -> str:
        items = request.get("input") or []
        head = {"instructions": request.get("instructions"), "tools": request.get("tools"),
                "lead": items[: self.lead_count(items)]}
        return hashlib.sha256(canonical_bytes(head)).hexdigest()

    def head_estimate_source(self, request: dict) -> dict:
        return {"instructions": request.get("instructions"), "tools": request.get("tools")}

    def strip_thinking(self, item: dict) -> dict | None:
        return None if item.get("type") == "reasoning" else item

    def merge_first(self, first: dict, blocks: list, text: str) -> dict:
        own = first.get("content")
        own_parts = [{"type": "input_text", "text": own}] if isinstance(own, str) else list(own or [])
        head = [*blocks] + ([{"type": "input_text", "text": text}] if text else [])
        return {**first, "content": head + own_parts}

    def countable(self, item: dict) -> bool:
        return True

    def estimate_items(self, estimator: TokenEstimator, items: list[dict]) -> int:
        return estimator.estimate(canonical_json(items)) if items else 0

    def lead_count(self, items: list[dict]) -> int:
        for i, item in enumerate(items):
            if self.is_turn_start(item):
                return i
        return len(items)

    def usage_tokens(self, usage: dict) -> int:
        return usage.get("input_tokens") or 0  # cached_tokens is a subset of input_tokens


def item_is_text_only(item: dict) -> bool:
    if item.get("type") == "additional_tools" or not _is_message(item):
        return False
    return all(isinstance(p, dict) and p.get("type") in TEXT_PART_TYPES for p in _parts(item))


RESPONSES = ResponsesDialect()
