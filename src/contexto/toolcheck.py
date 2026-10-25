"""The missing-tools check: is the main conversation running with Contexto's MCP tools?

Neither Claude Code nor Codex reports an MCP server that fails to start: the session runs on without
``contexto_search`` and ``contexto_recall``, so paged-out turns can no longer be recalled and nothing
says why (until v0.2.8, ``contexto run codex`` registered a bare ``contexto`` command, and when that
was not on PATH Codex ran this way). The proxy sees every request, so it checks, on the main
conversation only (``RewriteEngine.publishes``): subagents and side requests may legitimately run
without the tools.

What shows the tools are there, per dialect (measured with zero spend against a local stub, Claude
Code 2.1.302 and 2.1.310 and codex-cli 0.163.0, temporary homes and fake keys):

- Claude Code (Messages API): the request names them. With tool search off, ``tools`` holds
  ``mcp__contexto__contexto_search`` and the other four by name. With tool search on (2.1.302 with
  ``ENABLE_TOOL_SEARCH``; 2.1.310 by default), ``tools`` holds ``ToolSearch`` and a ``defer_loading``
  placeholder, and Claude Code lists every deferred tool by name, one per line: in a role ``system``
  message on 5.x model ids, in a ``<system-reminder>`` block of ``messages[0]`` on 4.x ids, where its
  later notices are ``<system-reminder>`` blocks too. Without the server neither appears. When the server goes away
  later (a resumed session whose server cannot start, or a live one whose server died), Claude Code
  leaves the old listing in history and adds notices: "deferred tools are no longer available" with
  the names, and "MCP servers have disconnected" with ``contexto`` (with tool search off, the tool
  list keeps naming the tools). So every system text is read, in order, and later notices override
  earlier ones (``contexto_notices``). A request with no tool list at all (Claude Code's title,
  topic, and quota side requests) is not judged.
- Codex (Responses API): the request cannot show it. With code mode Codex defers MCP tools behind
  its ``exec`` tool and never names them; the request with the server running is the same, byte for
  byte apart from ids, as the request without it. So the MCP server records in the session store
  when it answers ``tools/list`` (``SessionStore.set_mcp_listed``), and the check reads that record.
  The launcher clears it before the engine starts (``start_proxy``), and a record older than the
  engine (beyond ``WALL_SLACK_S``) does not count either. Once written, it stays: a server that
  crashes after answering cannot be detected. A Contexto tool
  named in ``tools`` or ``additional_tools`` would count too (not seen on 0.163.0).

When: a main request without the tools is reported once the grace (``GRACE_S``, for a server still
starting) has passed, and again only after the tools were seen in between. A run shorter than the
grace is judged at exit (``verdict``) from its last main request, when startup races are over. The
events are content-free: ``mcp_tools_missing`` (dialect, where it was judged, whether the server
answered ``tools/list``, how many main requests lacked the tools, seconds since start) and
``mcp_tools_found`` when the tools appear after a report. At exit, a session whose last main request
still lacked the tools logs ``mcp_tools_missing`` again (``repeated``), since a warning printed mid-run
can be drawn over by the CLI's TUI. The launcher shows ``missing_tools_message`` on stderr once during
the run and once after the CLI exits.

Refused tools (D44): the tools can be offered and still refused. Claude Code 2.1.302 in ``-p`` mode
refuses an MCP tool that no permission rule allows, and the model gets the tool result "Claude requested
permissions to use mcp__contexto__contexto_search, but you haven't granted it yet." (``is_error``). The
launcher's ``--settings`` allows Contexto's tools, but a user's ``ask`` rule for one of them outranks that
allow (measured, zero spend; the same on 2.1.310). So the check also reads the trailing user messages
(those after the last assistant message) of every Messages API request, main or not, since a refusal loses
recall in a subagent too. A result counts only when its id names a Contexto tool call of an assistant
message in the request, so another tool's output that quotes the text does not. The first refusal of a
Contexto tool logs ``mcp_tools_denied`` with the tools' names (never other content); the launcher shows it
once. A user who answers "No" at an interactive prompt gets other text, which does not count: that was a
choice.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from typing import Any

TOOL_PREFIX = "mcp__contexto__"
SERVER = "contexto"  # the server's name in Claude Code's notices (mcp__<server>__ is the tool prefix)
# Claude Code's default MCP connect timeout is 30 s (MCP_TIMEOUT); Codex's startup_timeout_sec is 10 s.
GRACE_S = 30.0
# The MCP server's record is wall-clock time from another process: a clock stepped back (an NTP
# correction) between engine start and tools/list must not make this launch's record look older. A
# previous launch's server answers well over this long before the next engine starts.
WALL_SLACK_S = 2.0
DIALECTS = frozenset({"anthropic", "responses"})
_LISTED_TOOL = re.compile(r"^(mcp__contexto__contexto_[a-z_]+)[ \t]*$")
# Claude Code 2.1.302's refusal of a tool no rule allows (captured with zero spend against a local stub).
_DENIED = re.compile(r"requested permissions to use (mcp__contexto__contexto_[a-z_]+), but you haven't granted it")
_SERVER_LINE = re.compile(r"^contexto(?:[ \t(:]|$)")  # "contexto" or "contexto (ENOENT): ..." in a server list
# A user message's <system-reminder> block that is a deferred-tool listing or an MCP notice (4.x model ids,
# measured on 2.1.302 with tool search on and on 2.1.310): its first line inside the tag.
_NOTICE_BLOCK = re.compile(r"\s*<system-reminder>\s*(?:The following deferred tools |\d+ deferred tools are available "
                           r"again|The following MCP servers |# MCP Server Instructions[ \t]*(?:\r?\n|\Z))")
CONTEXT_TAG, CONTEXT_END = "<system-reminder>", "</system-reminder>"
INTERRUPT_MARKER = "[Request interrupted by user"  # contexto.rewrite.INTERRUPT_MARKER: Esc during a tool run
TOOL_RESULT_MISSING = "[Tool result missing due to internal error]"  # contexto.rewrite.TOOL_RESULT_MISSING
# D47: on 5.x ids a prompt typed during a tool run (queued) that is one whole <system-reminder> block arrives as
# a role "system" text that starts with the first of these sentences and holds the second after the prompt;
# other reminders may follow (2.1.302 and 2.1.310, reviewer A's pty captures). Any other queued prompt arrives
# as a user text block starting with the first sentence, which is never read. Claude Code has other mid-turn
# openers ("Another Claude session sent a message while you were working:", "A peer session sent a message
# while you were working:"); they were not measured and are not skipped.
QUEUED_OPEN = "The user sent a new message while you were working:"
QUEUED_CLOSE = ("This is how Claude Code surfaces messages the user sends mid-turn \u2014 within the running turn, "
                "often alongside the next tool result, rather than as a separate conversation turn. Address the "
                "message above as you continue this turn.")


def _without_queued(text: str) -> str:
    """A role ``system`` text without the queued prompt in it: when the text starts with ``QUEUED_OPEN``, from
    there to the end of the last ``QUEUED_CLOSE``. The prompt can quote the closing sentence, but Claude Code's
    own comes after it, so the last one ends the user's text; with no closing sentence, the rest is dropped.
    A text that only quotes the opening sentence later (a nested CLAUDE.md on 2.1.302, reviewer A) is kept
    whole, so the notices after it are still read."""
    if not text.startswith(QUEUED_OPEN):
        return text
    end = text.rfind(QUEUED_CLOSE)
    return text[end + len(QUEUED_CLOSE):] if end > 0 else ""


def _notice_block(text: str) -> bool:
    """One whole reminder whose first line is a notice: it opens with the tag and that heading, ends with the
    closing tag, and holds no second opening tag (every genuine notice block in the 2.1.310 step 2 matrix has
    that form; a typed prompt that starts with an imitation and goes on with text does not)."""
    return (_NOTICE_BLOCK.match(text) is not None and text.rstrip().endswith(CONTEXT_END)
            and text.count(CONTEXT_TAG) == 1)


def _result_missing(block: dict) -> bool:
    """Claude Code's synthetic result for a tool run it lost (contexto.rewrite._result_missing): ``is_error``
    true and the marker as its whole text."""
    if block.get("type") != "tool_result" or block.get("is_error") is not True:
        return False
    return any(text.strip() == TOOL_RESULT_MISSING for text in _texts(block.get("content")))


def _named(tool: Any) -> bool:
    return isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"].startswith(TOOL_PREFIX)


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return []


def _system_texts(request: dict[str, Any]) -> list[str]:
    """The system prompt, then, in message order, every role ``system`` message and every
    ``<system-reminder>`` text of a user message: Claude Code puts the deferred-tool listing and the MCP
    notices in a role ``system`` message on 5.x model ids and in ``<system-reminder>`` blocks of user
    messages on 4.x ids (2.1.302 with tool search on, and 2.1.310, where it is on by default). Of a user
    message, a text block is read only when it is one whole ``<system-reminder>`` whose first line is one
    of those notices (``_notice_block``), and never when it is the message's last text block and the
    message holds no ``tool_result``: that position is the typed prompt (a plain-string message is all
    prompt), and no genuine notice block sat there in the 2.1.310 matrix. Nor is any text block after
    a ``[Request interrupted by user`` block: a prompt typed after Esc lands there, in the tool-result
    message, after that marker (reviewer A's pty captures on 2.1.302 and 2.1.310), nor any text block
    of a message holding Claude Code's synthetic "[Tool result missing due to internal error]" result,
    where a prompt queued during a tool run, then Esc and Enter, landed with no marker (D47). A role
    ``system`` text is read without the queued prompt in it (``_without_queued``): on 5.x ids a prompt
    typed during a tool run that is one whole reminder arrives there, between Claude Code's two sentences
    (any other queued prompt is a user text block starting with the first, never read). So a prompt that
    imitates a notice, alone, followed by text, typed after Esc, or queued, is not read, nor is a
    CLAUDE.md quoted in a context block. What remains readable is a user's earlier text block of a
    message with several, typed as one exact copy of a notice; Claude Code sends a typed prompt as one
    block, so this was not seen."""
    texts = _texts(request.get("system"))
    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "system":
            texts += [_without_queued(text) for text in _texts(message.get("content"))]
        elif message.get("role") == "user" and isinstance(message.get("content"), list):
            blocks = [b for b in message["content"] if isinstance(b, dict)]
            text_at = [i for i, b in enumerate(blocks) if b.get("type") == "text" and isinstance(b.get("text"), str)]
            marker = next((n for n, i in enumerate(text_at) if blocks[i]["text"].startswith(INTERRUPT_MARKER)), None)
            if any(_result_missing(b) for b in blocks):
                text_at = []  # a prompt queued, then Esc, can land next to this result with no marker
            elif marker is not None:
                text_at = text_at[:marker]  # what follows the marker was typed after Esc
            elif text_at and not any(b.get("type") == "tool_result" for b in blocks):
                text_at = text_at[:-1]  # the typed prompt
            texts += [blocks[i]["text"] for i in text_at if _notice_block(blocks[i]["text"])]
    return texts


def contexto_notices(texts: list[str]) -> tuple[bool, bool]:
    """Read Claude Code's system texts in order (2.1.302's notices, captured with zero spend) and return
    (a Contexto tool is still offered by name, the contexto server is down). Later notices override
    earlier ones, because Claude Code adds notices and never edits history:

    - a tool name on its own line counts as offered, unless it falls under "The following deferred
      tools are no longer available", which removes it (tool search; a resumed session whose server
      could not start, or a live one whose server died);
    - "N deferred tools are available again ... mcp__contexto__* (N)" offers the names announced
      earlier again (the server came back);
    - ``contexto`` under "The following MCP servers have disconnected" or "... failed to connect" means
      the server is down (with tool search off, the tool list keeps naming the tools after a crash);
      the server's "## contexto" section under "# MCP Server Instructions", or its tools offered again,
      means it is up.

    A listing ends at a blank line; the instructions section ends at the next top-level heading."""
    offered: set[str] = set()
    announced: set[str] = set()
    down = False
    for text in texts:
        if SERVER not in text:  # says nothing about contexto
            continue
        section: str | None = None
        for raw in text.splitlines():
            line = raw.strip()
            low = line.lower()
            if not line:
                if section != "instructions":
                    section = None
            elif "deferred tools" in low and "no longer available" in low:
                section = "removed"
            elif "deferred tools" in low and "available again" in low:
                section = None
                if TOOL_PREFIX in line:
                    offered |= announced or {TOOL_PREFIX + "*"}
                    down = False
            elif "deferred tools" in low and "available" in low:
                section = "offered"
            elif low.startswith("the following mcp servers") and ("disconnected" in low or "failed to connect" in low):
                section = "down"
            elif line == "# MCP Server Instructions":
                section = "instructions"
            elif (named := _LISTED_TOOL.match(line)) is not None:
                if section == "removed":
                    offered.discard(named.group(1))
                else:
                    offered.add(named.group(1))
                    announced.add(named.group(1))
                    down = False
            elif section == "down" and _SERVER_LINE.match(line):
                down = True
            elif section == "instructions":
                if line == f"## {SERVER}":
                    down = False
                elif line.startswith("# "):
                    section = None
    return bool(offered), down


def anthropic_tools_offered(request: dict[str, Any]) -> bool | None:
    """True when a Contexto tool is in ``tools`` by name, or listed as a deferred tool (tool search) in
    the system prompt, a role ``system`` message, or a user message's ``<system-reminder>`` block, and
    Claude Code's later notices did not take it away (``contexto_notices``); False when not; None for a
    request with no tool list."""
    tools = request.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    offered, down = contexto_notices(_system_texts(request))
    return (offered or any(_named(t) for t in tools)) and not down


def _contexto_calls(messages: list[dict[str, Any]]) -> dict[str, str]:
    """The Contexto tool calls of the assistant messages, by ``tool_use`` id."""
    calls: dict[str, str] = {}
    for message in messages:
        content = message.get("content") if message.get("role") == "assistant" else None
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str) and _named(b):
                calls[b["id"]] = b["name"]
    return calls


def anthropic_denied_tools(request: dict[str, Any]) -> list[str]:
    """Contexto tools Claude Code refused for lack of a permission rule, in the request's trailing user
    messages (every user message after the last assistant message; role ``system`` messages among them are
    skipped). A ``tool_result`` counts only when it has ``is_error``, its ``tool_use_id`` is a string naming a
    Contexto tool call (``mcp__contexto__``) of an assistant message in the request, and its text is the
    refusal naming that same tool: another tool's result that quotes the refusal text (a failed Bash command
    printing it) does not. The results may be split across those user messages in any order. Only the
    trailing user messages are read, so a refusal counts on the request that first carries it."""
    messages = [m for m in request.get("messages") or [] if isinstance(m, dict) and m.get("role") != "system"]
    trailing: list[dict[str, Any]] = []
    for message in reversed(messages):
        if message.get("role") != "user":
            break
        trailing.insert(0, message)
    errors = [b for m in trailing for b in (m.get("content") if isinstance(m.get("content"), list) else [])
              if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")]
    if not errors:
        return []  # most requests: no failed result, so no scan of the history
    calls = _contexto_calls(messages[:len(messages) - len(trailing)])
    denied: list[str] = []
    for block in errors:
        tool_use_id = block.get("tool_use_id")
        name = calls.get(tool_use_id) if isinstance(tool_use_id, str) else None
        if name is None:
            continue
        if any(name in _DENIED.findall(text) for text in _texts(block.get("content"))) and name not in denied:
            denied.append(name)
    return denied


def _names_tree(tools: Any, depth: int = 0) -> bool:
    """A Contexto tool or namespace named anywhere in a Responses tool list (namespaces nest tools)."""
    if not isinstance(tools, list) or depth > 3:
        return False
    return any(_named(t) or (isinstance(t, dict) and _names_tree(t.get("tools"), depth + 1)) for t in tools)


def responses_tools_offered(request: dict[str, Any]) -> bool | None:
    """True when the request names a Contexto tool; None otherwise, since Codex's code mode defers MCP
    tools without naming them, so their absence from the request proves nothing."""
    if _names_tree(request.get("tools")):
        return True
    for item in request.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "additional_tools" and _names_tree(item.get("tools")):
            return True
    return None


class ToolCheck:
    """Per-engine state of the check (see the module docstring). ``observe`` runs on every request the
    engine transforms (never ``count_tokens``); ``verdict`` once, at exit."""

    def __init__(self, store: Any, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, grace: float = GRACE_S) -> None:
        self.store = store
        self.clock = clock
        self.grace = grace
        self.started = clock()
        self.started_wall = wall()  # the MCP server's record is wall-clock time from another process
        self.dialect: str | None = None
        self.present: bool | None = None  # at the last judged main request
        self.missing_requests = 0
        self.reported = False
        self._exit_judged = False
        self._listed = False
        self.denied_reported = False

    def listed(self) -> bool:
        """Has the MCP server answered tools/list since this engine started? Once true, stays true."""
        if not self._listed:
            reader = getattr(self.store, "mcp_listed_at", None)
            try:
                at = reader() if callable(reader) else None
            except Exception:  # an unreadable record is no record; never fail the request
                at = None
            self._listed = isinstance(at, (int, float)) and at >= self.started_wall - WALL_SLACK_S
        return self._listed

    def observe(self, request: dict[str, Any], dialect: str, *, main: bool) -> list[dict[str, Any]]:
        if dialect not in DIALECTS:
            return []
        denied = self._denied(request, dialect, main)
        return [*denied, *self._offered(request, dialect, main)]

    def _denied(self, request: dict[str, Any], dialect: str, main: bool) -> list[dict[str, Any]]:
        """The first refusal of a Contexto tool, on any Messages API request (D44)."""
        if self.denied_reported or dialect != "anthropic":
            return []
        names = anthropic_denied_tools(request)
        if not names:
            return []
        self.denied_reported = True
        return [{"event": "mcp_tools_denied", "dialect": dialect, "main": main,
                 "tools": [name.removeprefix(TOOL_PREFIX) for name in names],
                 "elapsed_s": int(self.clock() - self.started)}]

    def _offered(self, request: dict[str, Any], dialect: str, main: bool) -> list[dict[str, Any]]:
        if not main:
            return []
        if dialect == "anthropic":
            present = anthropic_tools_offered(request)
            if present is None:
                return []
        else:
            present = bool(responses_tools_offered(request)) or self.listed()
        self.dialect, self.present = dialect, present
        if present:
            self.missing_requests = 0
            if self.reported:
                self.reported = False
                return [{"event": "mcp_tools_found", "dialect": dialect, "at": "request"}]
            return []
        self.missing_requests += 1
        if self.reported or self.clock() - self.started < self.grace:
            return []
        self.reported = True
        return [self._event("request")]

    def verdict(self) -> dict[str, Any] | None:
        """At exit, once: the ``mcp_tools_missing`` event when the last judged main request lacked the
        tools, else None. ``repeated`` says it was already reported during the run: it is reported again
        because a warning printed mid-run can be drawn over by the CLI's TUI, and the exit is where the
        user sees it. A Codex record written after that request counts."""
        if self.present is not False or self._exit_judged:
            return None
        if self.dialect == "responses" and self.listed():
            return None
        self._exit_judged = True
        repeated, self.reported = self.reported, True
        return {**self._event("exit"), "repeated": repeated}

    def _event(self, at: str) -> dict[str, Any]:
        return {"event": "mcp_tools_missing", "dialect": self.dialect, "at": at, "server_listed": self.listed(),
                "requests": self.missing_requests, "elapsed_s": int(self.clock() - self.started)}


def denied_tools_message(tools: Any) -> str:
    """The launcher's stderr warning for an ``mcp_tools_denied`` event."""
    names = [t for t in tools if isinstance(t, str)] if isinstance(tools, list) else []
    named = ", ".join(names) or "its tools"
    return (f"Claude Code refused Contexto's {named} (\"you haven't granted it yet\"), so paged-out turns may not "
            "be recalled. contexto run allows its own tools in the --settings it passes; a permission 'ask' rule "
            "matching mcp__contexto__ tools outranks that allow, and organization-managed settings can too. "
            "See README, Troubleshooting.")


def missing_tools_message(dialect: Any, server_listed: Any) -> str:
    """The launcher's stderr warning for an ``mcp_tools_missing`` event: what is wrong, the likely cause,
    and what to try."""
    server = f"{sys.executable} -P -m contexto mcp --help"
    if dialect == "responses":
        return ("Codex is running without Contexto's tools (contexto_search, contexto_recall): the contexto MCP "
                "server did not answer Codex's tool list in time, and Codex reports no error for that, so "
                f"paged-out turns cannot be recalled. Check that '{server}' runs; contexto before v0.2.8 "
                "started the server as a bare 'contexto' command, which fails when it is not on PATH. "
                "See README, Troubleshooting.")
    if server_listed:
        return ("Claude Code started the contexto MCP server, but its tools are not available to the model, so "
                "paged-out turns cannot be recalled. The server may have stopped after starting (check '/mcp' in "
                "Claude Code), or a permission deny rule or --disallowedTools matching mcp__contexto__ tools "
                "removed them. See README, Troubleshooting.")
    return ("Claude Code is running without Contexto's tools (contexto_search, contexto_recall): the contexto MCP "
            "server did not start or did not answer, and Claude Code reports no error for that, so paged-out turns "
            f"cannot be recalled. Check '/mcp' in Claude Code and that '{server}' runs. See README, Troubleshooting.")
