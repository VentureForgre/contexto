"""Which records the main conversation made (D69): the evidence project memory carries records on.

The engine reads the main conversation's own history. A record is the main conversation's when that
history holds Contexto's own ``contexto_pin`` or ``contexto_close`` call (a Messages API ``tool_use``
block in an assistant message, or a Responses API ``function_call`` item, named with the server's
prefix, ``mcp__contexto__``) and, with the same call id, its result (a ``tool_result`` block or a
``function_call_output`` item) whose text is exactly the server's success reply: ``Recorded <kind>
<record id>.`` (with the note an unpinned kind gets), ``Closed <obligation id> with <record id>.``, or
the server's structured content ``{"record_id": "<record id>"}`` alone, which is what a CLI that
prefers structured content passes on. A subagent's or a side request's call is in its own conversation,
never in main's, and a result that only quotes a success reply (a subagent's report returned through
an Agent call, a file, a page) belongs to another tool's call, so neither is ever taken.

The limits: this trusts the engine's choice of main conversation (D50). On Codex, where only the
turn-start rule picks main, whatever conversation is main is trusted. A pin whose result never reaches
a later request of the main conversation (the session ends right after it) is not confirmed, so it is
never carried. A code-mode call (Codex's ``exec`` running ``tools.mcp__contexto__contexto_pin(...)``)
is not recognised and is not confirmed either.

``MainRecords.scan(key, messages)`` reads only the messages it has not read before for that
conversation (a history shorter than before is read again from the start, as a new one), so each
request costs its new messages only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from contexto.mcp import UNPINNED_NOTE
from contexto.store.pinned import FACT_KINDS
from contexto.store.render import OWN_TOOL_PREFIX

PIN_TOOL = OWN_TOOL_PREFIX + "contexto_pin"
CLOSE_TOOL = OWN_TOOL_PREFIX + "contexto_close"
_ID = r"rec_[0-9a-f]{24}"
_PINNED = re.compile(rf"Recorded (obligation|decision|event|artifact|reference) ({_ID})\.")
_CLOSED = re.compile(rf"Closed {_ID} with ({_ID})\.")
_STRUCTURED = re.compile(rf'\{{\s*"record_id"\s*:\s*"({_ID})"\s*\}}')
_TEXT_TYPES = ("text", "input_text", "output_text")
KEYS_KEPT = 64  # conversations tracked at once; the oldest is forgotten past it


def confirmed_id(tool: str, text: str) -> str | None:
    """The record id the server's success reply ``text`` to ``tool`` names, None for any other text."""
    if tool == PIN_TOOL:
        head = _PINNED.match(text)
        if head and (text == head.group(0) or (head.group(1) in FACT_KINDS and
                                               text == head.group(0) + UNPINNED_NOTE.format(kind=head.group(1)))):
            return head.group(2)
    elif tool == CLOSE_TOOL:
        found = _CLOSED.fullmatch(text)
        if found is not None:
            return found.group(1)
    found = _STRUCTURED.fullmatch(text)
    if found is not None and json.loads(text) == {"record_id": found.group(1)}:
        return found.group(1)
    return None


def _result_text(value: Any) -> str | None:
    """The one text of a tool result's content: a string, or a list holding exactly one text part."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        part = value[0]
        if part.get("type") in _TEXT_TYPES and isinstance(part.get("text"), str):
            return part["text"]
    return None


@dataclass
class MainRecords:
    """Per conversation: how many of its messages were read, and its pin and close calls still waiting
    for their result (call id -> tool name)."""

    _read: dict[str, int] = field(default_factory=dict)
    _waiting: dict[str, dict[str, str]] = field(default_factory=dict)

    def scan(self, key: str, messages: Any) -> list[str]:
        """The record ids confirmed by the messages of ``key`` not read before, in order."""
        if not isinstance(messages, list):
            return []
        start = self._read.get(key, 0)
        if start > len(messages):  # not the history read before: read it as a new one
            start = 0
            self._waiting.pop(key, None)
        if key not in self._read and len(self._read) >= KEYS_KEPT:
            oldest = next(iter(self._read))
            self._read.pop(oldest)
            self._waiting.pop(oldest, None)
        waiting = self._waiting.setdefault(key, {})
        found: list[str] = []
        for message in messages[start:]:
            if isinstance(message, dict):
                self._message(message, waiting, found)
        self._read[key] = len(messages)
        return found

    @staticmethod
    def _message(message: dict, waiting: dict[str, str], found: list[str]) -> None:
        kind = message.get("type")
        if kind == "function_call":  # Responses API
            name, call_id = message.get("name"), message.get("call_id")
            if name in (PIN_TOOL, CLOSE_TOOL) and isinstance(call_id, str):
                waiting[call_id] = name
            return
        if kind == "function_call_output":
            call_id = message.get("call_id")
            tool = waiting.pop(call_id, None) if isinstance(call_id, str) else None
            text = _result_text(message.get("output")) if tool else None
            record_id = confirmed_id(tool, text) if tool and text is not None else None
            if record_id:
                found.append(record_id)
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        role = message.get("role")
        for block in content:
            if not isinstance(block, dict):
                continue
            if role == "assistant" and block.get("type") == "tool_use":
                name, call_id = block.get("name"), block.get("id")
                if name in (PIN_TOOL, CLOSE_TOOL) and isinstance(call_id, str):
                    waiting[call_id] = name
            elif role == "user" and block.get("type") == "tool_result" and block.get("is_error") is not True:
                call_id = block.get("tool_use_id")
                tool = waiting.pop(call_id, None) if isinstance(call_id, str) else None
                text = _result_text(block.get("content")) if tool else None
                record_id = confirmed_id(tool, text) if tool and text is not None else None
                if record_id:
                    found.append(record_id)
