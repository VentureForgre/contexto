"""Which records the main conversation made (D69): the evidence project memory carries records on.

The engine reads the main conversation's own history. A record is the main conversation's when that
history holds Contexto's own ``contexto_pin`` or ``contexto_close`` call (a Messages API ``tool_use``
block in an assistant message, a Responses API ``function_call`` or ``custom_tool_call`` item named
with the server's prefix ``mcp__contexto__``, or a Codex code-mode ``exec`` call whose parsed JS
statements invoke ONLY literal ``tools.mcp__contexto__contexto_pin`` / ``contexto_close`` calls with
literal arguments) and, with the same call id, its result (a ``tool_result`` block or a
``function_call_output`` / ``custom_tool_call_output`` item) whose text is the server's success reply:
``Recorded <kind> <record id>.`` (with the note an unpinned kind gets), ``Closed <obligation id> with
<record id>.``, or the server's structured content ``{"record_id": "<record id>"}`` alone, which is
what a CLI that prefers structured content passes on. A subagent's or a side request's call is in its
own conversation, never in main's, and a result that only quotes a success reply (a subagent's report
returned through an Agent call, a file, a page, or an ``exec`` call mixing other tools or arbitrary
JS) belongs to another tool's call, so neither is ever taken.

Which main conversation is trusted: the engine (``RewriteEngine._confirm_main_records``) hands a
conversation's messages to ``scan`` only while it is main and trusted. Trusted are the engine's first
main conversation (main since its own first request, or the conversation a new engine resumed from the
epoch record, unless the record says ``untrusted`` and the conversation is not in the session's
``trusted_conversations``; after a restart a first main the record does not resume is trusted only when
the store is new (no readable record and no Claude Code session served before the engine started), the
conversation is in ``trusted_conversations``, the record names it unmarked, or it is a new Claude Code
session: ``RewriteEngine._first_trusted``; a store that served sessions and has no readable record is
not new) and one that took main by ``compacted``, by D50's other session rules, ``new_cli_session``
(``/clear``) and ``resumed_cli_session`` (``/resume``), or by ``after_side_request`` when
``_first_trusted`` holds for it (in a new store's engine it holds for every conversation), or by
``more_turn_starts`` when it is in ``trusted_conversations`` (``RewriteEngine._trusted_takeover``). Each
conversation an engine trusts is written to that list (``SessionStore.add_trusted_conversation``). One
that took main from a known main by ``more_turn_starts``, the turn-start rule, and is not in the list
is not trusted: a Claude Code subagent whose requests lack
``X-Claude-Code-Agent-Id`` (by which D50 tells subagents apart; with it a subagent never takes main)
once SendMessage has resumed it past main's turn starts, or a Codex child conversation given more
input. Its pins and closures are not confirmed while it is main; if it later takes main by one of the
trusted rules its history is read then. A trusted conversation that loses main and takes it back by
turn starts stays trusted. A request that carries ``X-Claude-Code-Agent-Id`` or a ``parent_session_id``
is never read, even with main's conversation key (a fork of main, which D40 treats as main for the
room), nor is one whose ``x-claude-code-request-class`` is ``subagent``, ``workflow`` or ``auxiliary``
(D70, sent only while Claude Code's gateway hint headers are on); the scan position stays where it was.

The limits: a pin whose result never reaches a later request of the main conversation (the session
ends right after it) is not confirmed, so it is never carried. A Codex code-mode ``exec`` call that
combines ``contexto_pin`` / ``contexto_close`` with any other tool call, control flow, comments,
template literals, string concatenation, or arbitrary expressions is not confirmed either, so untrusted
output from other tools cannot forge a confirmation.

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

from contexto.canonical import loads_bounded
from contexto.mcp import UNPINNED_NOTE
from contexto.store.pinned import FACT_KINDS
from contexto.store.render import CODE_MODE_TOOL, OWN_TOOL_PREFIX, _code

PIN_TOOL = OWN_TOOL_PREFIX + "contexto_pin"
CLOSE_TOOL = OWN_TOOL_PREFIX + "contexto_close"
_ID = r"rec_[0-9a-f]{24}"
_ID_RE = re.compile(_ID)
_PINNED = re.compile(rf"Recorded (obligation|decision|event|artifact|reference) ({_ID})\.")
_CLOSED = re.compile(rf"Closed {_ID} with ({_ID})\.")
_STRUCTURED = re.compile(rf'\{{\s*"record_id"\s*:\s*"({_ID})"\s*\}}')
_TEXT_TYPES = ("text", "input_text", "output_text")
KEYS_KEPT = 64  # conversations tracked at once; the oldest is forgotten past it
REPORTED_KEPT = 4096  # call ids already reported as unmatched; the oldest is forgotten past it
_MAX_EXEC_CODE_BYTES = 128 * 1024
_MAX_EXEC_DEPTH = 32
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_EXEC_ERROR_LINE = re.compile(
    r"^(?:Exit code:\s*[1-9]\d*|Process exited with code\s+[1-9]\d*|"
    r"(?:Uncaught\s+)?(?:Syntax|Type|Reference|Range|Eval|URI)?Error:)",
    re.MULTILINE,
)


class _ExecSpec(NamedTuple):
    """A Codex code-mode ``exec`` call whose parsed JS statements invoke only literal
    ``contexto_pin`` / ``contexto_close`` calls with literal arguments."""

    tools: frozenset[str]  # subset of {PIN_TOOL, CLOSE_TOOL}
    max_calls: int  # number of literal pin/close calls in the exec code
    code_ids: frozenset[str]  # any rec_... ids present in call arguments (cannot be newly minted)


class _ExecParseError(Exception):
    pass


class _StrictExecParser:
    """Strict allowlist recursive-descent parser for pure ``contexto_pin`` / ``contexto_close`` Codex
    code-mode ``exec`` scripts (D51/D69).

    Only permits a flat sequence of statements of two forms:
    1. ``[const|let|var <ident> = | return ] [await] tools.<tool>(<arg_obj>)``
       or ``tools["<tool>"](<arg_obj>)`` / ``tools['<tool>'](<arg_obj>)``
       where ``<tool>`` is literally ``mcp__contexto__contexto_pin`` or ``contexto_close``, and
       ``<arg_obj>`` is a literal JS object (plain keys, JSON/JS string/number/bool/null/array/object
       values, or ``<bound_var>.record_id`` referencing an earlier pin in the same script).
    2. ``console.log(<expr>[, ...])`` or ``return <expr>`` where each ``<expr>`` is ONLY a previously
       bound variable ``<bound_var>``, ``<bound_var>.record_id``, ``JSON.stringify(<bound_var>)``,
       or an array/object of those — never arbitrary string literals, operators, comments, control
       flow, template literals, Unicode identifier escapes, ``eval``, ``Function``, or other tools.
    """

    def __init__(self, code: str) -> None:
        self.s = code
        self.n = len(code)
        self.i = 0
        self.bound: set[str] = set()
        self.tools: list[str] = []
        self.code_ids: set[str] = set()

    def _ws(self) -> None:
        s, n = self.s, self.n
        while self.i < n and s[self.i] in " \t\r\n":
            self.i += 1

    def _peek(self, text: str) -> bool:
        return self.s.startswith(text, self.i)

    def _expect(self, text: str) -> None:
        self._ws()
        if not self.s.startswith(text, self.i):
            raise _ExecParseError(f"expected {text!r}")
        self.i += len(text)

    def _read_ident(self) -> str:
        self._ws()
        m = _IDENT_RE.match(self.s, self.i)
        if m is None:
            raise _ExecParseError("expected identifier")
        self.i = m.end()
        return m.group(0)

    def _try_keyword(self, kw: str) -> bool:
        self._ws()
        end = self.i + len(kw)
        if self.s.startswith(kw, self.i) and (end == self.n or not (self.s[end].isalnum() or self.s[end] in "_$")):
            self.i = end
            return True
        return False

    def _read_string(self) -> str:
        self._ws()
        if self.i >= self.n or self.s[self.i] not in ('"', "'"):
            raise _ExecParseError("expected string")
        quote = self.s[self.i]
        self.i += 1
        out: list[str] = []
        s, n = self.s, self.n
        while self.i < n:
            ch = s[self.i]
            self.i += 1
            if ch == quote:
                val = "".join(out)
                self.code_ids.update(_ID_RE.findall(val))
                return val
            if ch in ("\n", "\r"):
                raise _ExecParseError("unterminated string")
            if ch == "\\":
                if self.i >= n:
                    raise _ExecParseError("trailing escape")
                esc = s[self.i]
                self.i += 1
                simple = {'"': '"', "'": "'", "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
                          "t": "\t"}
                if esc in simple:
                    out.append(simple[esc])
                elif esc == "u" and self.i + 4 <= n:
                    hex4 = s[self.i:self.i + 4]
                    try:
                        out.append(chr(int(hex4, 16)))
                    except ValueError as exc:
                        raise _ExecParseError("bad unicode escape") from exc
                    self.i += 4
                else:
                    raise _ExecParseError("unsupported escape")
            else:
                out.append(ch)
        raise _ExecParseError("unterminated string")

    def _read_value(self, depth: int = 0) -> None:
        if depth > _MAX_EXEC_DEPTH:
            raise _ExecParseError("too deep")
        self._ws()
        if self.i >= self.n:
            raise _ExecParseError("unexpected eof")
        ch = self.s[self.i]
        if ch in ('"', "'"):
            self._read_string()
            return
        if ch == "{":
            self._read_object(depth + 1)
            return
        if ch == "[":
            self._read_array(depth + 1)
            return
        if ch == "-" or ch.isdigit():
            m = _NUMBER_RE.match(self.s, self.i)
            if m is None:
                raise _ExecParseError("bad number")
            self.i = m.end()
            return
        if self._try_keyword("true") or self._try_keyword("false") or self._try_keyword("null"):
            return
        # Allow referencing <bound_var>.record_id (e.g. closing an obligation pinned earlier in the same exec).
        ident = self._read_ident()
        if ident in self.bound and self._peek(".record_id"):
            self.i += len(".record_id")
            if self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] in "_$"):
                raise _ExecParseError("bad property")
            return
        raise _ExecParseError("disallowed value")

    def _read_array(self, depth: int) -> None:
        self._expect("[")
        self._ws()
        if self._peek("]"):
            self.i += 1
            return
        while True:
            self._read_value(depth)
            self._ws()
            if self._peek(","):
                self.i += 1
                self._ws()
                if self._peek("]"):
                    self.i += 1
                    return
                continue
            self._expect("]")
            return

    def _read_object(self, depth: int) -> None:
        self._expect("{")
        self._ws()
        if self._peek("}"):
            self.i += 1
            return
        while True:
            self._ws()
            if self.i < self.n and self.s[self.i] in ('"', "'"):
                self._read_string()
            else:
                self._read_ident()
            self._expect(":")
            self._read_value(depth)
            self._ws()
            if self._peek(","):
                self.i += 1
                self._ws()
                if self._peek("}"):
                    self.i += 1
                    return
                continue
            self._expect("}")
            return

    def _read_tool_call(self) -> None:
        self._try_keyword("await")
        if self._read_ident() != "tools":
            raise _ExecParseError("expected tools")
        self._ws()
        if self._peek("."):
            self.i += 1
            tool = self._read_ident()
        elif self._peek("["):
            self.i += 1
            tool = self._read_string()
            self._expect("]")
        else:
            raise _ExecParseError("expected property access on tools")
        if tool not in (PIN_TOOL, CLOSE_TOOL):
            raise _ExecParseError("disallowed tool")
        self._expect("(")
        self._ws()
        if not self._peek(")"):
            self._read_object(0)
        self._expect(")")
        self.tools.append(tool)

    def _read_log_expr(self, depth: int = 0) -> None:
        if depth > _MAX_EXEC_DEPTH:
            raise _ExecParseError("too deep")
        self._ws()
        if self._peek("JSON.stringify"):
            self.i += len("JSON.stringify")
            self._expect("(")
            self._read_log_expr(depth + 1)
            self._expect(")")
            return
        if self._peek("["):
            self.i += 1
            self._ws()
            if self._peek("]"):
                self.i += 1
                return
            while True:
                self._read_log_expr(depth + 1)
                self._ws()
                if self._peek(","):
                    self.i += 1
                    self._ws()
                    if self._peek("]"):
                        self.i += 1
                        return
                    continue
                self._expect("]")
                return
        if self._peek("{"):
            self.i += 1
            self._ws()
            if self._peek("}"):
                self.i += 1
                return
            while True:
                self._ws()
                if self.i < self.n and self.s[self.i] in ('"', "'"):
                    self._read_string()
                    self._expect(":")
                    self._read_log_expr(depth + 1)
                else:
                    key = self._read_ident()
                    self._ws()
                    if self._peek(":"):
                        self.i += 1
                        self._read_log_expr(depth + 1)
                    elif key not in self.bound:
                        raise _ExecParseError("unbound shorthand")
                self._ws()
                if self._peek(","):
                    self.i += 1
                    self._ws()
                    if self._peek("}"):
                        self.i += 1
                        return
                    continue
                self._expect("}")
                return
        ident = self._read_ident()
        if ident not in self.bound:
            raise _ExecParseError("unbound identifier")
        self._ws()
        if self._peek(".record_id"):
            self.i += len(".record_id")
            if self.i < self.n and (self.s[self.i].isalnum() or self.s[self.i] in "_$"):
                raise _ExecParseError("bad property")

    def parse(self) -> _ExecSpec:
        while True:
            self._ws()
            while self.i < self.n and self.s[self.i] == ";":
                self.i += 1
                self._ws()
            if self.i >= self.n:
                break
            if self._try_keyword("const") or self._try_keyword("let") or self._try_keyword("var"):
                var = self._read_ident()
                if var in ("tools", "console", "JSON", "eval", "Function", "globalThis"):
                    raise _ExecParseError("reserved identifier")
                self._expect("=")
                self._read_tool_call()
                self.bound.add(var)
            elif self._try_keyword("return"):
                saved = self.i
                try:
                    self._read_tool_call()
                except _ExecParseError:
                    self.i = saved
                    self._read_log_expr()
            elif self._peek("console.log"):
                self.i += len("console.log")
                self._expect("(")
                self._ws()
                if not self._peek(")"):
                    while True:
                        self._read_log_expr()
                        self._ws()
                        if self._peek(","):
                            self.i += 1
                            continue
                        break
                self._expect(")")
            else:
                self._read_tool_call()
            stmt_end = self.i
            self._ws()
            if self.i < self.n:
                if self.s[self.i] == ";":
                    self.i += 1
                elif "\n" not in self.s[stmt_end:self.i]:
                    raise _ExecParseError("missing statement separator")
        if not self.tools:
            raise _ExecParseError("no record calls")
        return _ExecSpec(tools=frozenset(self.tools), max_calls=len(self.tools), code_ids=frozenset(self.code_ids))


def _exec_record_spec(name: Any, raw_args: Any) -> _ExecSpec | None:
    """For a Codex code-mode ``exec`` call whose parsed JS statements invoke ONLY literal
    ``contexto_pin`` or ``contexto_close`` calls (at least one) with literal arguments and optional
    logging/returning of their bound results, return its ``_ExecSpec``; None for any other call."""
    if name != CODE_MODE_TOOL:
        return None
    code = _code(raw_args)
    if code is None or len(code) > _MAX_EXEC_CODE_BYTES:
        return None
    try:
        return _StrictExecParser(code).parse()
    except _ExecParseError:
        return None


def _exec_confirmed_ids(spec: _ExecSpec, text: str) -> list[str]:
    """Extract newly created record ids from the output of a pure pin/close code-mode ``exec`` call.

    Rejects outputs that report a non-zero exit code or JS runtime error, accepts exact server reply
    text, per-line replies (for example after Codex's ``Output:`` header or batched calls), or JSON
    objects holding ``record_id`` / MCP ``content`` / ``structuredContent``. Any record id that
    appeared in the ``exec`` call arguments is rejected, and at most ``spec.max_calls`` unique ids
    are returned.
    """
    if _EXEC_ERROR_LINE.search(text):
        return []
    candidates: list[str] = []

    def consider(candidate: str) -> None:
        if len(candidates) >= spec.max_calls:
            return
        for tool in spec.tools:
            rid = confirmed_id(tool, candidate)
            if rid and rid not in spec.code_ids and rid not in candidates:
                candidates.append(rid)
                return

    def walk_json(value: Any, depth: int = 0) -> None:
        if depth > 8 or len(candidates) >= spec.max_calls:
            return
        if isinstance(value, str):
            consider(value.strip())
            return
        if isinstance(value, list):
            for item in value:
                walk_json(item, depth + 1)
            return
        if isinstance(value, dict):
            if set(value) == {"record_id"} and isinstance(value["record_id"], str):
                consider(json.dumps(value))
                return
            structured = value.get("structuredContent")
            if isinstance(structured, dict):
                walk_json(structured, depth + 1)
            content = value.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in _TEXT_TYPES and isinstance(part.get("text"), str):
                        consider(part["text"].strip())

    stripped = text.strip()
    consider(stripped)
    if not candidates:
        try:
            walk_json(loads_bounded(stripped))
        except (ValueError, RecursionError):
            pass
    if not candidates:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            consider(line)
            if line.startswith(("{", "[")):
                try:
                    walk_json(loads_bounded(line))
                except (ValueError, RecursionError):
                    pass
    return candidates


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
    if found is not None:
        try:
            if loads_bounded(text) == {"record_id": found.group(1)}:
                return found.group(1)
        except (ValueError, RecursionError):
            return None
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
    _waiting: dict[str, dict[str, str | _ExecSpec]] = field(default_factory=dict)
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

    def _result(self, call_id: Any, value: Any, is_error: bool, waiting: dict[str, str | _ExecSpec],
                found: list[str], unmatched: list[dict[str, str]]) -> None:
        entry = waiting.pop(call_id, None) if isinstance(call_id, str) else None
        if entry is None:
            return
        text, reason = (None, "is_error") if is_error else _result_text(value)
        if isinstance(entry, _ExecSpec):
            ids = _exec_confirmed_ids(entry, text) if text is not None else []
            if ids:
                found.extend(ids)
                return
            tool_label = (next(iter(entry.tools)).removeprefix(OWN_TOOL_PREFIX)
                          if len(entry.tools) == 1 else "exec")
        else:
            record_id = confirmed_id(entry, text) if text is not None else None
            if record_id:
                found.append(record_id)
                return
            tool_label = entry.removeprefix(OWN_TOOL_PREFIX)
        if call_id not in self._reported:
            self._reported[call_id] = None
            if len(self._reported) > REPORTED_KEPT:
                self._reported.pop(next(iter(self._reported)))
            unmatched.append({"event": "main_record_unmatched", "tool": tool_label,
                              "reason": reason or "reply"})

    def _message(self, message: dict, waiting: dict[str, str | _ExecSpec], found: list[str],
                 unmatched: list[dict[str, str]]) -> None:
        kind = message.get("type")
        if kind in ("function_call", "custom_tool_call"):  # Responses API (direct MCP or code-mode exec)
            name, call_id = message.get("name"), message.get("call_id")
            if not isinstance(call_id, str):
                return
            if name in (PIN_TOOL, CLOSE_TOOL):
                waiting[call_id] = name
            else:
                spec = _exec_record_spec(name, message.get("arguments" if kind == "function_call" else "input"))
                if spec is not None:
                    waiting[call_id] = spec
            return
        if kind in ("function_call_output", "custom_tool_call_output"):
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
