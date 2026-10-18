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
  deferred tool by name, one per line. Without the server neither appears. A request with no tool
  list at all (Claude Code's title, topic, and quota side requests) is not judged.
- Codex (Responses API): the request cannot show it. With code mode Codex defers MCP tools behind
  its ``exec`` tool and never names them; the request with the server running is the same, byte for
  byte apart from ids, as the request without it. So the MCP server records in the session store
  when it answers ``tools/list`` (``SessionStore.set_mcp_listed``), and the check reads that record:
  a record older than this engine is from an earlier launch and does not count. A Contexto tool
  named in ``tools`` or ``additional_tools`` would count too (not seen on 0.163.0).

When: a main request without the tools is reported once the grace (``GRACE_S``, for a server still
starting) has passed, and again only after the tools were seen in between. A run shorter than the
grace is judged at exit (``verdict``) from its last main request, when startup races are over. The
events are content-free: ``mcp_tools_missing`` (dialect, where it was judged, whether the server
answered ``tools/list``, how many main requests lacked the tools, seconds since start) and
``mcp_tools_found`` when the tools appear after a report. The launcher shows ``missing_tools_message``
once on stderr.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Any, Callable

TOOL_PREFIX = "mcp__contexto__"
# Claude Code's default MCP connect timeout is 30 s (MCP_TIMEOUT); Codex's startup_timeout_sec is 10 s.
GRACE_S = 30.0
DIALECTS = frozenset({"anthropic", "responses"})
_LISTED_TOOL = re.compile(r"^mcp__contexto__contexto_[a-z_]+[ \t]*$", re.MULTILINE)


def _named(tool: Any) -> bool:
    return isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"].startswith(TOOL_PREFIX)


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return []


def anthropic_tools_offered(request: dict[str, Any]) -> bool | None:
    """True when a Contexto tool is in ``tools`` by name, or listed as a deferred tool in the system
    prompt or a role ``system`` message (tool search); False when not; None for a request with no tool
    list. The user's own messages are not read: naming a tool there says nothing."""
    tools = request.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    if any(_named(t) for t in tools):
        return True
    texts = _texts(request.get("system"))
    for message in request.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "system":
            texts += _texts(message.get("content"))
    return any(_LISTED_TOOL.search(t) for t in texts)


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
        self._listed = False

    def listed(self) -> bool:
        """Has the MCP server answered tools/list since this engine started? Once true, stays true."""
        if not self._listed:
            reader = getattr(self.store, "mcp_listed_at", None)
            try:
                at = reader() if callable(reader) else None
            except Exception:  # an unreadable record is no record; never fail the request
                at = None
            self._listed = isinstance(at, (int, float)) and at >= self.started_wall
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
        """At exit: the ``mcp_tools_missing`` event when the last judged main request lacked the tools
        and that was not reported yet; else None. A Codex record written after that request counts."""
        if self.present is not False or self.reported:
            return None
        if self.dialect == "responses" and self.listed():
            return None
        self.reported = True
        return self._event("exit")

    def _event(self, at: str) -> dict[str, Any]:
        return {"event": "mcp_tools_missing", "dialect": self.dialect, "at": at, "server_listed": self.listed(),
                "requests": self.missing_requests, "elapsed_s": int(self.clock() - self.started)}


def missing_tools_message(dialect: Any, server_listed: Any) -> str:
    """The launcher's one stderr warning for an ``mcp_tools_missing`` event: what is wrong, the likely
    cause, and what to try."""
    server = f"{sys.executable} -P -m contexto mcp --help"
    if dialect == "responses":
        return ("Codex is running without Contexto's tools (contexto_search, contexto_recall): the contexto MCP "
                "server never answered Codex's tool list, and Codex reports no error for that, so paged-out turns "
                f"cannot be recalled. Check that '{server}' runs; contexto before v0.2.8 started the server as a "
                "bare 'contexto' command, which fails when it is not on PATH. See README, Troubleshooting.")
    if server_listed:
        return ("Claude Code started the contexto MCP server but did not offer its tools to the model, so "
                "paged-out turns cannot be recalled. A permission deny rule or --disallowedTools matching "
                "mcp__contexto__ tools would do this; '/mcp' in Claude Code shows the server. "
                "See README, Troubleshooting.")
    return ("Claude Code is running without Contexto's tools (contexto_search, contexto_recall): the contexto MCP "
            "server did not start or did not answer, and Claude Code reports no error for that, so paged-out turns "
            f"cannot be recalled. Check '/mcp' in Claude Code and that '{server}' runs. See README, Troubleshooting.")
