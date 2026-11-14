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

Which main conversation is trusted: the engine (``RewriteEngine._confirm_main_records``) hands a
conversation's messages to ``scan`` only while it is main and trusted. Trusted are the engine's first
main conversation (main since its own first request, or the conversation a new engine resumed from the
epoch record) and one that took main by one of D50's session
rules, ``new_cli_session`` (``/clear``), ``resumed_cli_session`` (``/resume``) or ``compacted``, or by
``after_side_request`` (``rewrite.TRUSTED_MAIN_REASONS``). One that took main from a known main by
``more_turn_starts``, the turn-start rule, is not trusted: a Claude Code subagent whose requests lack
``X-Claude-Code-Agent-Id`` (by which D50 tells subagents apart; with it a subagent never takes main)
once SendMessage has resumed it past main's turn starts, or a Codex child conversation given more
input. Its pins and closures are not confirmed while it is main; if it later takes main by one of the
trusted rules its history is read then. A trusted conversation that loses main and takes it back by
turn starts stays trusted. A request that carries ``X-Claude-Code-Agent-Id`` or a ``parent_session_id``
is never read, even with main's conversation key (a fork of main, which D40 treats as main for the
room), nor is one whose ``x-claude-code-request-class`` is ``subagent``, ``workflow`` or ``auxiliary``
(D70, sent only while Claude Code's gateway hint headers are on); the scan position stays where it was.

The limits: a pin whose result never reaches a later request of the main conversation (the session
ends right after it) is not confirmed, so it is never carried. A code-mode call (Codex's ``exec``
running ``tools.mcp__contexto__contexto_pin(...)``) is not recognised and is not confirmed either. On
codex-cli 0.163.0, as measured (``docs/research/M2-codex-acceptance-plan.md``), that is the only way the
model calls Contexto's tools, so a Codex session confirms nothing.

``MainRecords.scan(key, messages)`` reads only the messages it has not read before for that
conversation (a history shorter than before is read again from the start, as a new one), so each
request costs its new messages only. The engine uses ``read``, which marks the messages read only
once the store has kept the ids, so a failed write is tried again at the next request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple

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
REPORTED_KEPT = 4096  # call ids already reported as unmatched; the oldest is forgotten past it


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


def _result_text(value: Any) -> tuple[str | None, str | None]:
    """The one text of a tool result's content (a string, or a list holding exactly one text part) and
    None, or None and why there is none: ``parts`` (a list of another length), ``part_type`` (its one
    part is not text) or ``shape`` (neither a string nor a list)."""
    if isinstance(value, str):
        return value, None
    if not isinstance(value, list):
        return None, "shape"
    if len(value) != 1:
        return None, "parts"
    part = value[0]
    if isinstance(part, dict) and part.get("type") in _TEXT_TYPES and isinstance(part.get("text"), str):
        return part["text"], None
    return None, "part_type"


class Read(NamedTuple):
    """What ``MainRecords.read`` found in the messages not read before."""

    ids: list[str]  # the record ids confirmed, in order
    unmatched: list[dict[str, str]]  # content-free main_record_unmatched events, once per call id
    commit: Callable[[], None]  # marks the messages read


@dataclass
class MainRecords:
    """Per conversation: how many of its messages were read, and its pin and close calls still waiting
    for their result (call id -> tool name); and the call ids already reported as unmatched."""

    _read: dict[str, int] = field(default_factory=dict)
    _waiting: dict[str, dict[str, str]] = field(default_factory=dict)
    _reported: dict[str, None] = field(default_factory=dict)  # insertion-ordered, the newest REPORTED_KEPT

    def scan(self, key: str, messages: Any) -> list[str]:
        """The record ids confirmed by the messages of ``key`` not read before, in order; they count as
        read at once."""
        found = self.read(key, messages)
        found.commit()
        return found.ids

    def read(self, key: str, messages: Any) -> Read:
        """``scan`` in two steps: the ids (and the unmatched replies), and a call that marks the
        messages read. Until it is called nothing changes, so a caller whose write of the ids failed
        reads the same messages again at the next request and finds the same ids.

        A pin or close call whose result is not a success reply (an ``is_error`` result, content of
        another shape, several parts, a part that is not text, or a text that is not the server's
        reply) gives one ``main_record_unmatched`` event with the tool's name and the reason, never a
        text or an id, at most once per call id: if a CLI ever passes the reply in another form, every
        pin would go unconfirmed, and this is how the log shows it."""
        if not isinstance(messages, list):
            return Read([], [], lambda: None)
        start = self._read.get(key, 0)
        waiting = dict(self._waiting.get(key, {}))
        if start > len(messages):  # not the history read before: read it as a new one
            start, waiting = 0, {}
        found: list[str] = []
        unmatched: list[dict[str, str]] = []
        for message in messages[start:]:
            if isinstance(message, dict):
                self._message(message, waiting, found, unmatched)

        def commit() -> None:
            if key not in self._read and len(self._read) >= KEYS_KEPT:
                oldest = next(iter(self._read))
                self._read.pop(oldest)
                self._waiting.pop(oldest, None)
            self._read[key] = len(messages)
            self._waiting[key] = waiting

        return Read(found, unmatched, commit)

    def _result(self, call_id: Any, value: Any, is_error: bool, waiting: dict[str, str], found: list[str],
                unmatched: list[dict[str, str]]) -> None:
        tool = waiting.pop(call_id, None) if isinstance(call_id, str) else None
        if tool is None:
            return
        text, reason = (None, "is_error") if is_error else _result_text(value)
        record_id = confirmed_id(tool, text) if text is not None else None
        if record_id:
            found.append(record_id)
            return
        if call_id not in self._reported:
            self._reported[call_id] = None
            if len(self._reported) > REPORTED_KEPT:
                self._reported.pop(next(iter(self._reported)))
            unmatched.append({"event": "main_record_unmatched", "tool": tool.removeprefix(OWN_TOOL_PREFIX),
                              "reason": reason or "reply"})

    def _message(self, message: dict, waiting: dict[str, str], found: list[str],
                 unmatched: list[dict[str, str]]) -> None:
        kind = message.get("type")
        if kind == "function_call":  # Responses API
            name, call_id = message.get("name"), message.get("call_id")
            if name in (PIN_TOOL, CLOSE_TOOL) and isinstance(call_id, str):
                waiting[call_id] = name
            return
        if kind == "function_call_output":
            self._result(message.get("call_id"), message.get("output"), False, waiting, found, unmatched)
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
            elif role == "user" and block.get("type") == "tool_result":
                self._result(block.get("tool_use_id"), block.get("content"), block.get("is_error") is True,
                             waiting, found, unmatched)
