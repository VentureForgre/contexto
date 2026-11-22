"""Google Gemini API dialect (``POST /v1beta/models/*:generateContent``, ``:streamGenerateContent``, ``:countTokens``).

Supports Gemini's ``contents`` + ``parts`` history model:
- Top-level ``systemInstruction`` / ``system_instruction``, ``tools``, and ``toolConfig`` form the head.
- ``role: "user"`` content items with authored parts start turns; items carrying ``functionResponse``
  continue a tool loop.
- ``role: "model"`` items hold assistant text, ``functionCall`` parts, and ``thought: true`` thinking parts.
- Thinking parts (``thought: true``) are stripped from retained turns created before the current epoch.
- Usage comes from ``usageMetadata.promptTokenCount`` (which includes ``cachedContentTokenCount``) and
  ``candidatesTokenCount`` + ``thoughtsTokenCount``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from contexto.canonical import canonical_bytes
from contexto.rewrite import CARRIED_TEXT, THINKING_OMITTED, UNCLASSIFIED, Dialect
from contexto.tokens import TokenEstimator

GEMINI_PATH_RE = re.compile(
    r"^/v1(?:beta|alpha)?/models/([^/:]+):(generateContent|streamGenerateContent|countTokens)$"
)


def gemini_model_from_path(path: str) -> str | None:
    """Return the model name embedded in a Gemini API path, or None."""
    match = GEMINI_PATH_RE.match(path)
    return match.group(1) if match else None


def is_gemini_generate_path(path: str) -> bool:
    """True for Gemini ``:generateContent`` and ``:streamGenerateContent`` paths."""
    match = GEMINI_PATH_RE.match(path)
    return bool(match and match.group(2) in ("generateContent", "streamGenerateContent"))


def is_gemini_count_path(path: str) -> bool:
    """True for Gemini ``:countTokens`` paths."""
    match = GEMINI_PATH_RE.match(path)
    return bool(match and match.group(2) == "countTokens")


def _function_call(part: dict[str, Any]) -> dict[str, Any] | None:
    fc = part.get("functionCall")
    if not isinstance(fc, dict):
        fc = part.get("function_call")
    return fc if isinstance(fc, dict) else None


def _function_response(part: dict[str, Any]) -> dict[str, Any] | None:
    fr = part.get("functionResponse")
    if not isinstance(fr, dict):
        fr = part.get("function_response")
    return fr if isinstance(fr, dict) else None


def _is_text_only(item: dict[str, Any]) -> bool:
    parts = item.get("parts")
    return isinstance(parts, list) and all(isinstance(p, dict) and isinstance(p.get("text"), str) for p in parts)


class GeminiDialect(Dialect):
    name = "gemini"
    api_name = "gemini"
    field = "contents"
    media_floor = False  # Gemini prices images at a fixed token cost (~258 tokens), below Anthropic's floor
    in_turn = None

    def matches_endpoint(self, endpoint: str) -> bool:
        return bool(GEMINI_PATH_RE.match(endpoint))

    def is_count_endpoint(self, endpoint: str) -> bool:
        return is_gemini_count_path(endpoint)

    def is_context_block(self, block: Any) -> bool:
        return False

    def is_turn_start(self, item: dict) -> bool:
        if item.get("role", "user") != "user":
            return False
        parts = item.get("parts")
        if not isinstance(parts, list) or not parts:
            return False
        if any(isinstance(p, dict) and _function_response(p) is not None for p in parts):
            return False
        return any(isinstance(p, dict) and bool(p) for p in parts)

    def is_assistant(self, item: dict) -> bool:
        return item.get("role") == "model"

    def is_system_like(self, item: dict) -> bool:
        return item.get("role") == "system"

    def result_ids(self, item: dict) -> list[str]:
        parts = item.get("parts")
        if not isinstance(parts, list):
            return []
        ids: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            fr = _function_response(part)
            if fr is None:
                continue
            call_id = fr.get("id") or fr.get("name")
            if isinstance(call_id, str) and call_id:
                ids.append(call_id)
        return ids

    def retained_blocks(self, item: dict) -> list[tuple[str, dict]]:
        raw_role = item.get("role", "user")
        if raw_role == "system":
            return []
        role = "assistant" if raw_role == "model" else raw_role
        parts = item.get("parts")
        if not isinstance(parts, list):
            return []
        out: list[tuple[str, dict]] = []
        for part in parts:
            if not isinstance(part, dict):
                out.append((role, {"type": "text", "text": None}))
                continue
            if part.get("thought") is True:
                continue
            if "text" in part:
                text = part.get("text")
                if isinstance(text, str):
                    if text:
                        out.append((role, {"type": "text", "text": text}))
                else:
                    out.append((role, {"type": "text", "text": None}))
            elif (fc := _function_call(part)) is not None:
                name = fc.get("name", "") if isinstance(fc.get("name"), str) else ""
                args = fc.get("args")
                tu: dict[str, Any] = {"type": "tool_use", "name": name, "input": {} if args is None else args}
                call_id = fc.get("id") or fc.get("name")
                if isinstance(call_id, str) and call_id:
                    tu["id"] = call_id
                out.append(("assistant", tu))
            elif (fr := _function_response(part)) is not None:
                resp = fr.get("response")
                if isinstance(resp, str):
                    content: Any = resp
                elif resp is None:
                    content = ""
                else:
                    try:
                        content = json.dumps(resp, ensure_ascii=False, sort_keys=True)
                    except (ValueError, TypeError, RecursionError):
                        content = str(resp)
                tr: dict[str, Any] = {"type": "tool_result", "content": content}
                call_id = fr.get("id") or fr.get("name")
                if isinstance(call_id, str) and call_id:
                    tr["tool_use_id"] = call_id
                out.append(("user", tr))
        return out

    def classify_system(self, items: list[dict], i: int) -> str:
        return CARRIED_TEXT if _is_text_only(items[i]) else UNCLASSIFIED

    def normalise(self, item: dict) -> dict:
        return item

    def head_fingerprint(self, request: dict) -> str:
        head = self.head_estimate_source(request)
        return hashlib.sha256(canonical_bytes(head)).hexdigest()

    def head_estimate_source(self, request: dict) -> dict:
        return {
            "systemInstruction": request.get("systemInstruction") or request.get("system_instruction"),
            "tools": request.get("tools"),
            "toolConfig": request.get("toolConfig") or request.get("tool_config"),
        }

    def strip_thinking(self, item: dict) -> dict | None:
        if item.get("role") != "model":
            return item
        parts = item.get("parts")
        if not isinstance(parts, list) or not any(isinstance(p, dict) and p.get("thought") is True for p in parts):
            return item
        kept = [p for p in parts if not (isinstance(p, dict) and p.get("thought") is True)]
        if not kept:
            kept = [{"text": THINKING_OMITTED}]
        return {**item, "parts": kept}

    def merge_first(self, first: dict, blocks: list, text: str) -> dict:
        head: list[dict[str, Any]] = []
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                head.append({"text": block["text"]})
            elif isinstance(block, dict):
                head.append(block)
        if text:
            head.append({"text": text})
        if not head:
            return dict(first)
        own = first.get("parts")
        own_parts = list(own) if isinstance(own, list) else []
        return {**first, "parts": head + own_parts}

    def countable(self, item: dict) -> bool:
        return True

    def estimate_items(self, estimator: TokenEstimator, items: list[dict]) -> int:
        return estimator.estimate_items(items) if items else 0

    def lead_count(self, items: list[dict]) -> int:
        return 0

    def usage_tokens(self, usage: dict) -> int:
        # promptTokenCount already includes cachedContentTokenCount in Gemini usageMetadata
        return usage.get("promptTokenCount") or usage.get("input_tokens") or 0

    def output_tokens(self, usage: dict) -> int:
        candidates = usage.get("candidatesTokenCount") or 0
        thoughts = usage.get("thoughtsTokenCount") or 0
        total = candidates + thoughts
        return total if total > 0 else (usage.get("output_tokens") or 0)

    def judges_session_start(self, request: dict) -> bool:
        return True

    def session_start_seen(self, items: list[dict]) -> bool | None:
        return None  # generic API: no CLI-specific session-start text signature

    @property
    def session_start_version(self) -> str:
        return "generic"


GEMINI = GeminiDialect()
