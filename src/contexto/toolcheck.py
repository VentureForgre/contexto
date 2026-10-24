"""The missing-tools check: is the main conversation running with Contexto's MCP tools?

Neither Claude Code nor Codex reports an MCP server that fails to start: the session runs on without
``contexto_search`` and ``contexto_recall``, so paged-out turns can no longer be recalled and nothing
says why (until v0.2.8, ``contexto run codex`` registered a bare ``contexto`` command, and when that
was not on PATH Codex ran this way). The proxy sees every request, so it checks, on the main
conversation only (``RewriteEngine.publishes``): subagents and side requests may legitimately run
without the tools.

What shows the tools are there, per dialect (measured with zero spend against a local stub, Claude
Code 2.1.302 and codex-cli 0.163.0, temporary homes and fake keys):

- Claude Code (Messages API): the request names them. Normally ``tools`` holds
  ``mcp__contexto__contexto_search`` and the other four by name; with tool search on, ``tools`` holds
  ``ToolSearch`` and a ``defer_loading`` placeholder, and a role ``system`` message lists every
  deferred tool by name, one per line. Without the server neither appears. When the server goes away
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
_SERVER_LINE = re.compile(r"^contexto(?:[ \t(:]|$)")  # "contexto" or "contexto (ENOENT): ..." in a server list


def _named(tool: Any) -> bool:
    return isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"].startswith(TOOL_PREFIX)


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return []


def _system_texts(request: dict[str, Any]) -> list[str]:
    """The system prompt, then every role ``system`` message, in order. The user's own messages are not
    read: naming a tool there says nothing."""
    texts = _texts(request.get("system"))
    for message in request.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "system":
            texts += _texts(message.get("content"))
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
    """True when a Contexto tool is in ``tools`` by name, or listed as a deferred tool in the system
    prompt or a role ``system`` message (tool search), and Claude Code's later notices did not take it
    away (``contexto_notices``); False when not; None for a request with no tool list."""
    tools = request.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    offered, down = contexto_notices(_system_texts(request))
    return (offered or any(_named(t) for t in tools)) and not down


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
        if not main or dialect not in DIALECTS:
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
