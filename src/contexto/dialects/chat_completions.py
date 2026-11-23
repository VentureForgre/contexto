"""OpenAI Chat Completions API dialect (``POST /v1/chat/completions`` and ``/chat/completions``).

Supports standard Chat Completions message histories:
- ``role: "system"`` and ``role: "developer"`` messages (leading ones kept as lead; later ones carried)
- ``role: "user"`` turn-start messages (string or multi-part content arrays)
- ``role: "assistant"`` messages with ``content``, ``tool_calls``, and ``reasoning_content`` / ``reasoning``
- ``role: "tool"`` (and legacy ``role: "function"``) tool-result messages paired by ``tool_call_id``
- Usage from ``usage.prompt_tokens`` / ``usage.completion_tokens`` (with automatic
  ``stream_options.include_usage = True`` when streaming unless explicitly set to False).
"""

from __future__ import annotations

import hashlib
from typing import Any

from contexto.canonical import canonical_bytes, loads_bounded
from contexto.rewrite import CARRIED_TEXT, SESSION_START, THINKING_OMITTED, UNCLASSIFIED, Dialect
from contexto.tokens import TokenEstimator

CHAT_COMPLETIONS_PATHS = frozenset({"/chat/completions", "/v1/chat/completions"})
_THINKING_BLOCK_TYPES = frozenset({"thinking", "reasoning", "redacted_thinking"})
_REASONING_KEYS = ("reasoning_content", "reasoning")


def _damaged_block() -> dict[str, Any]:
    return {"type": "text", "text": None}


def _is_text_only(item: dict[str, Any]) -> bool:
    content = item.get("content")
    if isinstance(content, str) or content is None:
        return True
    return isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "text" for p in content)


class ChatCompletionsDialect(Dialect):
    name = "chat_completions"
    api_name = "chat_completions"
    field = "messages"
    media_floor = False  # OpenAI tile pricing can be below Anthropic's pixel floor
    in_turn = None

    def matches_endpoint(self, endpoint: str) -> bool:
        return endpoint in CHAT_COMPLETIONS_PATHS

    def is_count_endpoint(self, endpoint: str) -> bool:
        return False

    def is_context_block(self, block: Any) -> bool:
        return False

    def is_turn_start(self, item: dict) -> bool:
        if item.get("role") != "user":
            return False
        content = item.get("content")
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return any(isinstance(part, dict) for part in content)
        return False

    def is_assistant(self, item: dict) -> bool:
        return item.get("role") == "assistant"

    def is_system_like(self, item: dict) -> bool:
        return item.get("role") in ("system", "developer")

    def result_ids(self, item: dict) -> list[str]:
        if item.get("role") != "tool":
            return []
        call_id = item.get("tool_call_id")
        return [call_id] if isinstance(call_id, str) and call_id else []

    def retained_blocks(self, item: dict) -> list[tuple[str, dict]]:
        role = item.get("role", "unknown")
        if role in ("system", "developer"):
            return []
        if role == "tool":
            content = item.get("content")
            if isinstance(content, str):
                inner: Any = content
            elif isinstance(content, list):
                inner = [{"type": "text", "text": p["text"]} if isinstance(p, dict) and isinstance(p.get("text"), str)
                         else _damaged_block()
                         for p in content if not isinstance(p, dict) or p.get("type") == "text"]
            else:
                inner = "" if content is None else [_damaged_block()]
            block: dict[str, Any] = {"type": "tool_result", "content": inner}
            if isinstance(item.get("tool_call_id"), str) and item["tool_call_id"]:
                block["tool_use_id"] = item["tool_call_id"]
            return [("user", block)]
        if role == "function":
            content = item.get("content")
            inner = content if isinstance(content, str) else ("" if content is None else [_damaged_block()])
            return [("user", {"type": "tool_result", "content": inner})]

        out: list[tuple[str, dict]] = []
        content = item.get("content")
        if isinstance(content, str) and content:
            out.append((role, {"type": "text", "text": content}))
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    out.append((role, _damaged_block()))
                elif part.get("type") == "text":
                    text = part.get("text")
                    out.append((role, {"type": "text", "text": text} if isinstance(text, str) else _damaged_block()))
        if role == "assistant" and isinstance(item.get("tool_calls"), list):
            for call in item["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = fn.get("name", "") if isinstance(fn.get("name"), str) else ""
                raw_args = fn.get("arguments")
                try:
                    args = loads_bounded(raw_args) if isinstance(raw_args, str) else raw_args
                except ValueError:
                    args = raw_args
                tu: dict[str, Any] = {"type": "tool_use", "name": name, "input": {} if args is None else args}
                if isinstance(call.get("id"), str) and call["id"]:
                    tu["id"] = call["id"]
                out.append(("assistant", tu))
        return out

    def classify_system(self, items: list[dict], i: int) -> str:
        if i < self.lead_count(items):
            return SESSION_START
        return CARRIED_TEXT if _is_text_only(items[i]) else UNCLASSIFIED

    def normalise(self, item: dict) -> dict:
        return item

    def head_fingerprint(self, request: dict) -> str:
        items = request.get(self.field) or []
        head = {
            "tools": request.get("tools"),
            "functions": request.get("functions"),
            "response_format": request.get("response_format"),
            "lead": items[: self.lead_count(items)],
        }
        return hashlib.sha256(canonical_bytes(head)).hexdigest()

    def head_estimate_source(self, request: dict) -> dict:
        return {
            "tools": request.get("tools"),
            "functions": request.get("functions"),
            "response_format": request.get("response_format"),
        }

    def strip_thinking(self, item: dict) -> dict | None:
        if item.get("role") != "assistant":
            return item
        has_reasoning_key = any(k in item for k in _REASONING_KEYS)
        content = item.get("content")
        has_thinking_block = (isinstance(content, list)
                              and any(isinstance(b, dict) and b.get("type") in _THINKING_BLOCK_TYPES for b in content))
        if not has_reasoning_key and not has_thinking_block:
            return item
        cleaned = {k: v for k, v in item.items() if k not in _REASONING_KEYS}
        if has_thinking_block and isinstance(content, list):
            kept = [b for b in content if not (isinstance(b, dict) and b.get("type") in _THINKING_BLOCK_TYPES)]
            if not kept and not cleaned.get("tool_calls"):
                kept = [{"type": "text", "text": THINKING_OMITTED}]
            cleaned["content"] = kept
        elif has_reasoning_key and not cleaned.get("content") and not cleaned.get("tool_calls"):
            cleaned["content"] = THINKING_OMITTED
        return cleaned

    def merge_first(self, first: dict, blocks: list, text: str) -> dict:
        head = [*blocks] + ([{"type": "text", "text": text}] if text else [])
        if not head:
            return dict(first)
        own = first.get("content")
        if isinstance(own, str):
            own_parts = [{"type": "text", "text": own}] if own else []
        elif isinstance(own, list):
            own_parts = list(own)
        else:
            own_parts = []
        return {**first, "content": head + own_parts}

    def countable(self, item: dict) -> bool:
        return True

    def estimate_items(self, estimator: TokenEstimator, items: list[dict]) -> int:
        return estimator.estimate_items(items) if items else 0

    def lead_count(self, items: list[dict]) -> int:
        for i, item in enumerate(items):
            if self.is_turn_start(item):
                return i
        return len(items)

    def usage_tokens(self, usage: dict) -> int:
        # prompt_tokens already includes prompt_tokens_details.cached_tokens in Chat Completions
        return usage.get("prompt_tokens") or usage.get("input_tokens") or 0

    def output_tokens(self, usage: dict) -> int:
        return usage.get("completion_tokens") or usage.get("output_tokens") or 0

    def prepare_upstream(self, request: dict) -> dict:
        """Ensure streaming Chat Completions requests ask for usage unless explicitly disabled."""
        if request.get("stream") is not True:
            return request
        options = request.get("stream_options")
        if options is None:
            return {**request, "stream_options": {"include_usage": True}}
        if isinstance(options, dict) and "include_usage" not in options:
            return {**request, "stream_options": {**options, "include_usage": True}}
        return request

    def judges_session_start(self, request: dict) -> bool:
        return True

    def session_start_seen(self, items: list[dict]) -> bool | None:
        return None  # generic API: no CLI-specific session-start text signature

    @property
    def session_start_version(self) -> str:
        return "generic"


CHAT_COMPLETIONS = ChatCompletionsDialect()
