"""The missing-tools check (contexto.toolcheck): is the main conversation running with Contexto's MCP tools?

Request shapes are the ones captured with zero spend from Claude Code 2.1.302 and codex-cli 0.163.0
against a local stub (temporary HOME, CLAUDE_CONFIG_DIR and CODEX_HOME, fake keys), trimmed to the
parts the check reads; texts are placeholders.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto.launcher import NoticeLog
from contexto.rewrite import Config, RewriteEngine
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.toolcheck import (
    GRACE_S,
    ToolCheck,
    anthropic_denied_tools,
    anthropic_tools_offered,
    denied_tools_message,
    missing_tools_message,
    responses_tools_offered,
)

BUILTIN = ["Agent", "Bash", "CronCreate", "Edit", "Read", "Skill", "WebFetch", "Write"]
CONTEXTO = ["mcp__contexto__contexto_close", "mcp__contexto__contexto_pin", "mcp__contexto__contexto_recall",
            "mcp__contexto__contexto_search", "mcp__contexto__contexto_state"]


def tool(name: str, **extra) -> dict:
    return {"name": name, "description": "d", "input_schema": {"type": "object", "properties": {}}, **extra}


def claude_request(tool_names: list[str], *, deferred: list[str] | None = None, prompt: str = "say ok") -> dict:
    """Claude Code's main-conversation request. ``deferred``: tool search on (ENABLE_TOOL_SEARCH), where
    Claude Code 2.1.302 sends ToolSearch plus a defer_loading placeholder in ``tools`` and lists the
    deferred tools by name, one per line, in a role "system" message."""
    messages = [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>context</system-reminder>"},
                                             {"type": "text", "text": prompt}]}]
    tools = [tool(n) for n in tool_names]
    if deferred is not None:
        tools += [tool("ToolSearch"), tool("DeferredToolPlaceholder", defer_loading=True)]
        listing = ("# Environment\n...\nThe following deferred tools are now available via ToolSearch. "
                   "Their schemas are NOT loaded. Use ToolSearch with query \"select:<name>[,<name>...]\" "
                   "to load tool schemas before "
                   "calling them:\n" + "\n".join(deferred) + "\n\nAvailable agent types for the Agent tool:\n- claude")
        messages.append({"role": "system", "content": [{"type": "text", "text": listing}]})
    return {"model": "claude-opus-5-5", "max_tokens": 32000, "stream": True,
            "system": [{"type": "text", "text": "You are Claude Code."}], "messages": messages, "tools": tools}


def codex_request(prompt: str = "say ok") -> dict:
    """Codex's request with code mode (codex-cli 0.163.0): no top-level tools; the additional_tools item
    holds the functions, clock and collaboration namespaces. MCP tools are deferred behind ``exec`` and
    never named, so this request is the same whether or not the contexto MCP server started."""
    namespaces = [
        {"type": "namespace", "name": "functions", "description": "d",
         "tools": [{"type": "custom", "name": "exec", "description": "Some deferred nested tools may be omitted "
                    "from this description. They are still available on the global `tools` object and listed in "
                    "`ALL_TOOLS`. ... `await tools.mcp__ologs__get_profile(...)`"},
                   {"type": "function", "name": "wait"}, {"type": "function", "name": "request_user_input"}]},
        {"type": "namespace", "name": "clock", "description": "d", "tools": [{"type": "function", "name": "sleep"}]},
        {"type": "namespace", "name": "collaboration", "description": "d",
         "tools": [{"type": "function", "name": "spawn_agent"}, {"type": "function", "name": "wait_agent"}]},
    ]
    return {"model": "gpt-6-astra", "stream": True, "store": False, "tool_choice": "auto",
            "input": [{"type": "additional_tools", "id": "at_1", "role": "developer", "tools": namespaces},
                      {"type": "message", "id": "m1", "role": "developer",
                       "content": [{"type": "input_text", "text": "base instructions"}]},
                      {"type": "message", "id": "m2", "role": "user",
                       "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]},
                      {"type": "message", "id": "m3", "role": "user",
                       "content": [{"type": "input_text", "text": prompt}]}]}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Marker:
    """A store stand-in for the MCP server's tools/list record."""

    def __init__(self, at: float | None = None) -> None:
        self.at = at

    def mcp_listed_at(self) -> float | None:
        return self.at


# Request-side detection ------------------------------------------------------------------------------

def test_claude_tools_by_name_in_the_tool_list():
    assert anthropic_tools_offered(claude_request(BUILTIN + CONTEXTO)) is True
    assert anthropic_tools_offered(claude_request(BUILTIN)) is False


def test_claude_tool_search_lists_deferred_tools_in_a_system_message():
    assert anthropic_tools_offered(claude_request(["Bash", "Read"], deferred=["CronCreate", *CONTEXTO])) is True
    assert anthropic_tools_offered(claude_request(["Bash", "Read"], deferred=["CronCreate", "WebFetch"])) is False


# D45: on 4.x model ids (claude-sonnet-4-6) with tool search on, the listing and the notices are
# <system-reminder> blocks of user messages, not role "system" messages. Captured with zero spend from
# 2.1.310, where tool search is on by default (the same on 2.1.302 with ENABLE_TOOL_SEARCH): a fresh
# session, a resume whose server could not start, and a resume with it back. Trimmed where marked.
def reminder(text: str) -> dict:
    return {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}


LISTING_4X = ("The following deferred tools are now available via ToolSearch. Their schemas are NOT loaded — "
              "calling them directly will fail with InputValidationError. Use ToolSearch with query "
              "\"select:<name>[,<name>...]\" to load tool schemas before calling them:\nCronCreate\nWebFetch\n")
INSTRUCTIONS_4X = ("# MCP Server Instructions\n\nThe following MCP servers have provided instructions for how to use "
                   "their tools and resources:\n\n## contexto\nContexto keeps this session's older turns in a local "
                   "store. [trimmed]")
REMOVED_4X = ("The following deferred tools are no longer available (their MCP server disconnected). Do not search "
              "for them — ToolSearch will return no match:\n" + "\n".join(CONTEXTO) + "\n\nThis is ambient context "
              "[trimmed]\n\nThe following MCP servers are configured but failed to connect — their tools (typically "
              "named mcp__<server>__*) are unavailable for this session:\ncontexto (ENOENT): \"Executable not found "
              "in $PATH: stdio\"\n\nTreat this as a connection failure [trimmed]")
DISCONNECTED_4X = ("The following MCP servers have disconnected. Their instructions above no longer apply:\n"
                   "contexto\n\nThis is ambient context [trimmed]")
AGAIN_4X = ("5 deferred tools are available again (MCP server reconnected — names announced earlier in this "
            "conversation): mcp__contexto__* (5). Load via ToolSearch as before.")


def claude_request_4x(deferred: list[str]) -> dict:
    request = claude_request(["Bash", "Read", "ToolSearch"])
    request["messages"] = [{"role": "user", "content": [
        reminder("# Environment\n[trimmed]"), reminder(LISTING_4X + "\n".join(deferred)), reminder(INSTRUCTIONS_4X),
        {"type": "text", "text": "turn 1"}]}]
    return request


def resumed_4x(request: dict, *blocks: str) -> dict:
    request["messages"] += [{"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
                            {"role": "user", "content": [*map(reminder, blocks), {"type": "text", "text": "next"}]}]
    return request


def test_claude_4x_tool_search_lists_deferred_tools_in_a_context_block():
    assert anthropic_tools_offered(claude_request_4x(CONTEXTO)) is True
    assert anthropic_tools_offered(claude_request_4x([])) is False
    down = resumed_4x(claude_request_4x(CONTEXTO), REMOVED_4X, DISCONNECTED_4X)
    assert anthropic_tools_offered(down) is False
    assert anthropic_tools_offered(resumed_4x(down, AGAIN_4X, INSTRUCTIONS_4X)) is True


def test_claude_context_blocks_that_are_not_tool_notices_are_not_read():
    # A CLAUDE.md that lists the tools, quoted in a context block, and the user's own text say nothing.
    claude_md = reminder("Codebase and user instructions are shown below.\nContents of CLAUDE.md:\n"
                         + "\n".join(CONTEXTO))
    request = claude_request_4x([])
    request["messages"][0]["content"].insert(1, claude_md)
    request["messages"][0]["content"].append({"type": "text", "text": "\n".join(CONTEXTO)})
    assert anthropic_tools_offered(request) is False
    # The user pasting a listing without the tag is the user's text.
    request["messages"] += [{"role": "assistant", "content": "OK"},
                            {"role": "user", "content": "please read this:\n" + LISTING_4X + "\n".join(CONTEXTO)}]
    assert anthropic_tools_offered(request) is False


def test_claude_request_without_tools_is_not_judged():
    # Claude Code's side requests (title, topic, quota) carry no tool list: they say nothing about MCP.
    request = claude_request([])
    assert anthropic_tools_offered(request) is None
    del request["tools"]
    assert anthropic_tools_offered(request) is None


def test_claude_user_text_naming_a_tool_does_not_count_and_does_not_crash():
    # The user's own words are not where Claude Code lists tools; only the tool list and system text count.
    request = claude_request(BUILTIN, prompt="call mcp__contexto__contexto_search\nmcp__contexto__contexto_recall")
    assert anthropic_tools_offered(request) is False
    odd = claude_request(BUILTIN, deferred=[])
    odd["system"] = "plain string system prompt"
    odd["messages"].append({"role": "system", "content": "mcp__contexto__contexto_state"})
    assert anthropic_tools_offered(odd) is True
    odd["tools"].append("not a dict")
    assert anthropic_tools_offered(odd) is True


# Claude Code 2.1.302's notices when the server goes away and comes back, captured with zero spend
# (local stub, temporary homes, fake key): a resumed session whose second launch could not start the
# server (``-c`` with its command off PATH), the third launch with it back, and an interactive session
# whose server died after its first turn. Trimmed only where marked.
REMOVED_ON_RESUME = (
    "The following deferred tools are no longer available (their MCP server disconnected). Do not search for "
    "them — ToolSearch will return no match:\n" + "\n".join(CONTEXTO) + "\n\nThis is ambient context — do "
    "not narrate it to the user unless they ask or it is directly relevant to their request.\n\nThe following MCP "
    "servers are configured but failed to connect — their tools (typically named mcp__<server>__*) are "
    "unavailable for this session:\ncontexto (ENOENT): \"Executable not found in $PATH: stdio\"\n\nTreat this as a "
    "connection failure, not a missing capability. [trimmed]\n\nThe following MCP servers have disconnected. Their "
    "instructions above no longer apply:\ncontexto\n\nThis is ambient context [trimmed]\n\n"
    "<total_tokens>15000000 tokens left</total_tokens>")
REMOVED_LIVE = REMOVED_ON_RESUME.replace('contexto (ENOENT): "Executable not found in $PATH: stdio"', "contexto")
AVAILABLE_AGAIN = (
    "5 deferred tools are available again (MCP server reconnected — names announced earlier in this "
    "conversation): mcp__contexto__* (5). Load via ToolSearch as before.\n\n# MCP Server Instructions\n\nThe "
    "following MCP servers have provided instructions for how to use their tools and resources:\n\n## contexto\n"
    "Contexto keeps this session's older turns in a local store. [trimmed]\n\n"
    "<total_tokens>15000000 tokens left</total_tokens>")
# Tool search off: the tool list keeps naming the tools after the server died (calling one fails with
# "No such tool available ... Its MCP server 'contexto' has disconnected"); only this notice tells.
DISCONNECTED = ("The following MCP servers have disconnected. Their instructions above no longer apply:\ncontexto\n\n"
                "This is ambient context — do not narrate it to the user unless they ask or it is directly "
                "relevant to their request.\n\n<total_tokens>15000000 tokens left</total_tokens>")
INSTRUCTIONS = ("# MCP Server Instructions\n\nThe following MCP servers have provided instructions for how to use "
                "their tools and resources:\n\n## contexto\nContexto keeps this session's older turns in a local "
                "store. [trimmed]\n\n<total_tokens>15000000 tokens left</total_tokens>")


def with_system(request: dict, *texts: str) -> dict:
    """Later turns: each notice arrives as a role "system" message after an assistant reply and a user turn."""
    for text in texts:
        request["messages"] += [{"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
                                {"role": "user", "content": [{"type": "text", "text": "next"}]},
                                {"role": "system", "content": [{"type": "text", "text": text}]}]
    return request


def test_claude_tool_search_tools_no_longer_available_are_missing():
    # Reviewer-1's repro: the removal notice alone lists the names, one per line.
    removal_only = with_system(claude_request(["Bash", "Read"], deferred=["CronCreate"]), REMOVED_LIVE)
    assert anthropic_tools_offered(removal_only) is False
    # Resumed with the server down: the first launch's listing is still in history, then the removal.
    resumed = with_system(claude_request(["Bash", "Read"], deferred=["CronCreate", *CONTEXTO]), REMOVED_ON_RESUME)
    assert anthropic_tools_offered(resumed) is False
    # The same notice in a live interactive session whose server died after the first turn.
    live = with_system(claude_request(["Bash", "Read"], deferred=["CronCreate", *CONTEXTO]), REMOVED_LIVE)
    assert anthropic_tools_offered(live) is False


def test_claude_tool_search_tools_back_after_a_removal_are_present():
    # Third launch with the server back: Claude Code names them by glob, "announced earlier".
    again = with_system(claude_request(["Bash"], deferred=["CronCreate", *CONTEXTO]), REMOVED_ON_RESUME,
                        AVAILABLE_AGAIN)
    assert anthropic_tools_offered(again) is True
    # A fresh listing by name after the removal counts too; a removal after that wins again.
    fresh = with_system(claude_request(["Bash"], deferred=["CronCreate", *CONTEXTO]), REMOVED_LIVE,
                        "The following deferred tools are now available via ToolSearch:\n" + "\n".join(CONTEXTO))
    assert anthropic_tools_offered(fresh) is True
    assert anthropic_tools_offered(with_system(fresh, REMOVED_LIVE)) is False


def test_claude_tools_named_but_their_server_disconnected_are_missing():
    # Tool search off, server died mid-session: the tool list still names them, the notice says otherwise.
    dead = with_system(claude_request(BUILTIN + CONTEXTO), DISCONNECTED)
    assert anthropic_tools_offered(dead) is False
    # Resumed with the server back: its instructions are announced again after the notice.
    assert anthropic_tools_offered(with_system(dead, INSTRUCTIONS)) is True
    # Another server's disconnect says nothing about contexto's.
    other = with_system(claude_request(BUILTIN + CONTEXTO), DISCONNECTED.replace(":\ncontexto\n", ":\nologs\n"))
    assert anthropic_tools_offered(other) is True


def test_claude_system_texts_are_read_in_order_top_level_first():
    request = claude_request(["Bash"], deferred=["CronCreate"])
    request["system"] = [{"type": "text", "text": "You are Claude Code."},
                         {"type": "text", "text": "The following deferred tools are now available via ToolSearch:\n"
                                                  + "\n".join(CONTEXTO)}]
    assert anthropic_tools_offered(request) is True
    assert anthropic_tools_offered(with_system(request, REMOVED_LIVE)) is False
    request["system"][1]["text"] = REMOVED_LIVE
    request["messages"][-1]["content"][0]["text"] += "\n\nThe following deferred tools are now available:\n" + \
        "\n".join(CONTEXTO)
    assert anthropic_tools_offered(request) is True


def test_codex_code_mode_request_cannot_show_the_tools():
    assert responses_tools_offered(codex_request()) is None


def test_codex_tools_named_in_a_request_count_when_present():
    request = codex_request()
    request["tools"] = [{"type": "function", "name": "mcp__contexto__contexto_search"}]
    assert responses_tools_offered(request) is True
    nested = codex_request()
    nested["input"][0]["tools"].append({"type": "namespace", "name": "mcp__contexto__",
                                        "tools": [{"type": "function", "name": "contexto_search"}]})
    assert responses_tools_offered(nested) is True


# The check's state machine --------------------------------------------------------------------------

def test_claude_missing_is_reported_once_after_the_grace():
    clock = Clock()
    check = ToolCheck(Marker(), clock=clock, wall=lambda: 0.0)
    assert check.observe(claude_request(BUILTIN), "anthropic", main=True) == []  # inside the grace
    clock.now += GRACE_S
    [event] = check.observe(claude_request(BUILTIN), "anthropic", main=True)
    assert event == {"event": "mcp_tools_missing", "dialect": "anthropic", "at": "request", "server_listed": False,
                     "requests": 2, "elapsed_s": int(GRACE_S)}
    assert check.observe(claude_request(BUILTIN), "anthropic", main=True) == []  # once
    # Still missing at exit: repeated, because a mid-run warning in a TUI may never be seen.
    repeat = check.verdict()
    assert repeat == {"event": "mcp_tools_missing", "dialect": "anthropic", "at": "exit", "server_listed": False,
                      "requests": 3, "elapsed_s": int(GRACE_S), "repeated": True}
    assert check.verdict() is None  # once at exit


def test_exit_does_not_repeat_once_the_tools_came_back():
    clock = Clock()
    check = ToolCheck(Marker(), clock=clock, wall=lambda: 0.0)
    clock.now += GRACE_S
    assert check.observe(claude_request(BUILTIN), "anthropic", main=True)[0]["event"] == "mcp_tools_missing"
    assert check.observe(claude_request(BUILTIN + CONTEXTO), "anthropic", main=True)[0]["event"] == "mcp_tools_found"
    assert check.verdict() is None


def test_claude_present_tools_never_report_and_a_change_reports_again():
    clock = Clock()
    check = ToolCheck(Marker(), clock=clock, wall=lambda: 0.0)
    clock.now += GRACE_S
    assert check.observe(claude_request(BUILTIN + CONTEXTO), "anthropic", main=True) == []
    assert check.verdict() is None
    [lost] = check.observe(claude_request(BUILTIN), "anthropic", main=True)
    assert lost["event"] == "mcp_tools_missing"
    [found] = check.observe(claude_request(BUILTIN + CONTEXTO), "anthropic", main=True)
    assert found == {"event": "mcp_tools_found", "dialect": "anthropic", "at": "request"}
    [again] = check.observe(claude_request(BUILTIN), "anthropic", main=True)
    assert again["event"] == "mcp_tools_missing"


def test_side_requests_and_untooled_requests_are_ignored():
    clock = Clock()
    clock.now += GRACE_S
    check = ToolCheck(Marker(), clock=clock, wall=lambda: 0.0)
    clock.now += GRACE_S
    assert check.observe(claude_request(BUILTIN), "anthropic", main=False) == []  # a subagent
    assert check.observe(claude_request([]), "anthropic", main=True) == []  # a side request with no tools
    assert check.verdict() is None


def test_short_claude_run_reports_at_exit():
    check = ToolCheck(Marker(), clock=Clock(), wall=lambda: 0.0)
    assert check.observe(claude_request(BUILTIN), "anthropic", main=True) == []
    event = check.verdict()
    assert event["event"] == "mcp_tools_missing" and event["at"] == "exit" and event["requests"] == 1
    assert event["repeated"] is False  # not reported during the run
    assert check.verdict() is None  # once


def test_slow_start_inside_the_grace_is_not_reported():
    clock = Clock()
    check = ToolCheck(Marker(), clock=clock, wall=lambda: 0.0)
    assert check.observe(claude_request(BUILTIN), "anthropic", main=True) == []
    clock.now += 2
    assert check.observe(claude_request(BUILTIN + CONTEXTO), "anthropic", main=True) == []
    assert check.verdict() is None


def test_claude_event_says_whether_the_server_listed_its_tools():
    check = ToolCheck(Marker(at=50.0), clock=Clock(), wall=lambda: 10.0)
    check.observe(claude_request(BUILTIN), "anthropic", main=True)
    assert check.verdict()["server_listed"] is True  # the server answered, Claude Code did not offer the tools


def test_codex_uses_the_mcp_servers_tools_list_record():
    marker = Marker()
    clock = Clock()
    check = ToolCheck(marker, clock=clock, wall=lambda: 100.0)
    clock.now += GRACE_S
    [event] = check.observe(codex_request(), "responses", main=True)
    assert event["event"] == "mcp_tools_missing" and event["dialect"] == "responses" and not event["server_listed"]
    marker.at = 150.0  # the server answered tools/list during this launch
    [found] = check.observe(codex_request(), "responses", main=True)
    assert found["event"] == "mcp_tools_found"
    assert check.verdict() is None


def test_codex_record_from_an_earlier_launch_does_not_count():
    check = ToolCheck(Marker(at=90.0), clock=Clock(), wall=lambda: 100.0)
    assert check.observe(codex_request(), "responses", main=True) == []
    assert check.verdict()["server_listed"] is False


def test_codex_record_survives_a_small_wall_clock_step_back():
    # An NTP correction between engine start and tools/list: this launch's record looks a little older.
    assert ToolCheck(Marker(at=98.5), clock=Clock(), wall=lambda: 100.0).listed() is True
    assert ToolCheck(Marker(at=97.0), clock=Clock(), wall=lambda: 100.0).listed() is False


def test_codex_exit_rechecks_the_record():
    marker = Marker()
    check = ToolCheck(marker, clock=Clock(), wall=lambda: 100.0)
    check.observe(codex_request(), "responses", main=True)
    marker.at = 101.0  # listed after the first request, before exit
    assert check.verdict() is None


def test_no_main_request_means_no_verdict_and_a_broken_store_is_not_listed():
    class Broken:
        def mcp_listed_at(self):
            raise OSError("unreadable")

    assert ToolCheck(Marker(), clock=Clock()).verdict() is None
    check = ToolCheck(Broken(), clock=Clock(), wall=lambda: 0.0)
    check.observe(codex_request(), "responses", main=True)
    assert check.verdict()["server_listed"] is False
    assert ToolCheck(object(), clock=Clock()).observe(codex_request(), "responses", main=True) == []


# Refused tools (D44) ---------------------------------------------------------------------------------

def refusal(name: str) -> str:
    """Claude Code 2.1.302's tool result for a tool no permission rule allows (zero-spend stub, -p mode)."""
    return f"Claude requested permissions to use {name}, but you haven't granted it yet."


def after_tool_calls(results: list[tuple[str, object, bool]], *, history: list[dict] | None = None) -> dict:
    """A main request carrying the results of the model's tool calls, then the role system message 2.1.302
    adds after them. ``results``: (tool name, result content, is_error)."""
    request = claude_request(BUILTIN + CONTEXTO)
    uses = [{"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": {}}
            for i, (name, _, _) in enumerate(results)]
    answers = [{"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": content, "is_error": error}
               for i, (_, content, error) in enumerate(results)]
    request["messages"] += [*(history or []), {"role": "assistant", "content": uses},
                            {"role": "user", "content": answers},
                            {"role": "system", "content": [{"type": "text", "text": "<total_tokens>1</total_tokens>"}]}]
    return request


SEARCH, RECALL = "mcp__contexto__contexto_search", "mcp__contexto__contexto_recall"


def test_refused_contexto_tools_are_named_from_the_newest_user_message():
    request = after_tool_calls([(SEARCH, refusal(SEARCH), True), (RECALL, refusal(RECALL), True),
                                (SEARCH, refusal(SEARCH), True)])
    assert anthropic_denied_tools(request) == [SEARCH, RECALL]
    listed = after_tool_calls([(RECALL, [{"type": "text", "text": refusal(RECALL)}], True)])
    assert anthropic_denied_tools(listed) == [RECALL]  # a block list, not only a string


@pytest.mark.parametrize("results", [
    [(SEARCH, "No paged-out turn contains every word of that query.", False)],  # ran
    [(RECALL, "Unknown page id: pg_0. Page ids appear in the paged-history index.", True)],  # ran, failed
    [(SEARCH, refusal(SEARCH), False)],  # not an error result
    [("mcp__contexto_x__contexto_state", refusal("mcp__contexto_x__contexto_state"), True)],  # another server
    [("Bash", refusal("Bash"), True)],
    # The user answered "No" at an interactive prompt: a choice, not a missing rule.
    [(SEARCH, "The user doesn't want to proceed with this tool use. The tool use was rejected.", True)],
])
def test_results_that_are_not_a_refusal_of_a_contexto_tool_do_not_count(results):
    assert anthropic_denied_tools(after_tool_calls(results)) == []


def test_a_refusal_counts_only_as_the_result_of_a_contexto_tool_call():
    # A failed Bash command that prints the refusal text (say, grepping a transcript) is not a refusal.
    quoted = f"Exit code 1\n{refusal(SEARCH)}"
    assert anthropic_denied_tools(after_tool_calls([("Bash", quoted, True)])) == []
    # The true positive next to it still counts, and only for the tool its call named.
    both = after_tool_calls([("Bash", quoted, True), (RECALL, refusal(RECALL), True)])
    assert anthropic_denied_tools(both) == [RECALL]
    assert anthropic_denied_tools(after_tool_calls([(RECALL, refusal(SEARCH), True)])) == []
    # A result whose call is in no assistant message of the request does not count either.
    orphan = after_tool_calls([(RECALL, refusal(RECALL), True)])
    orphan["messages"][-3]["content"] = [{"type": "text", "text": "no tool call"}]
    assert anthropic_denied_tools(orphan) == []


def test_results_split_across_trailing_user_messages_in_any_order_count():
    # Review of PR #224: the results of one round can arrive in consecutive user messages, in any order,
    # with role "system" messages between them; and a call can sit in an earlier assistant message.
    request = after_tool_calls([(SEARCH, refusal(SEARCH), True), ("Bash", "ok", False),
                                (RECALL, refusal(RECALL), True)])
    answers = request["messages"][-2]["content"]
    request["messages"][-2:] = [{"role": "user", "content": [answers[2]]},
                                {"role": "system", "content": "<total_tokens>1</total_tokens>"},
                                {"role": "user", "content": [answers[1]]},
                                {"role": "user", "content": [answers[0], {"type": "text", "text": "next"}]}]
    assert anthropic_denied_tools(request) == [RECALL, SEARCH]
    # The calls may sit in earlier assistant messages than the last one.
    uses = request["messages"][1]["content"]
    between = {"role": "user", "content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"}]}
    request["messages"][1:2] = [{"role": "assistant", "content": [uses[0]]}, between,
                                {"role": "assistant", "content": uses[1:]}]
    assert anthropic_denied_tools(request) == [RECALL, SEARCH]
    # A refusal before the last assistant message was reported on an earlier request: not read again.
    request["messages"][2]["content"].append(request["messages"][-1]["content"].pop(0))
    assert anthropic_denied_tools(request) == [RECALL]


@pytest.mark.parametrize("tool_use_id", [["toolu_0"], {"id": "toolu_0"}, 0, None])
def test_a_result_whose_id_is_not_a_string_does_not_match_and_does_not_raise(tool_use_id):
    request = after_tool_calls([(SEARCH, refusal(SEARCH), True)])
    request["messages"][-2]["content"][0]["tool_use_id"] = tool_use_id
    assert anthropic_denied_tools(request) == []
    check = ToolCheck(Marker(), clock=Clock(), wall=lambda: 0.0)
    assert check.observe(request, "anthropic", main=False) == []


def test_only_the_trailing_user_messages_are_read():
    old = after_tool_calls([(SEARCH, refusal(SEARCH), True)])["messages"][1:]
    request = after_tool_calls([(SEARCH, "matches", False)], history=old[:-1])  # the refusal is a turn back
    assert anthropic_denied_tools(request) == []
    assert anthropic_denied_tools(claude_request(BUILTIN + CONTEXTO, prompt=refusal(SEARCH))) == []  # user text
    assert anthropic_denied_tools({"messages": "not a list"}) == []
    assert anthropic_denied_tools({}) == []


def test_a_refusal_is_reported_once_on_any_conversation_and_does_not_touch_the_missing_check():
    clock = Clock()
    check = ToolCheck(Marker(), clock=clock, wall=lambda: 0.0)
    clock.now += 3
    request = after_tool_calls([(SEARCH, refusal(SEARCH), True), (RECALL, refusal(RECALL), True)])
    [event] = check.observe(request, "anthropic", main=False)  # a subagent loses recall too
    assert event == {"event": "mcp_tools_denied", "dialect": "anthropic", "main": False,
                     "tools": ["contexto_search", "contexto_recall"], "elapsed_s": 3}
    assert check.observe(request, "anthropic", main=True) == []  # once per session
    assert check.verdict() is None  # the tools were offered
    assert check.observe(after_tool_calls([(SEARCH, refusal(SEARCH), True)]), "responses", main=True) == []


def test_engine_logs_a_refusal_before_its_request_event():
    engine = RewriteEngine(MemoryStore(), config=Config(bound=150_000))
    request = after_tool_calls([(RECALL, refusal(RECALL), True)])
    _, _, events = engine(request, {}, "/v1/messages")
    names = [e["event"] for e in events]
    assert "mcp_tools_denied" in names and names.index("mcp_tools_denied") < len(names) - 1
    denied = events[names.index("mcp_tools_denied")]
    assert denied["tools"] == ["contexto_recall"] and "granted" not in json.dumps(denied)  # content-free


def test_denied_tools_message_and_notice_log_show_it_once():
    message = denied_tools_message(["contexto_search", "contexto_recall"])
    assert all(w in message for w in ["contexto_search, contexto_recall", "haven't granted it yet", "--settings",
                                      "'ask' rule", "README"]), message
    assert "its tools" in denied_tools_message(None)
    err = io.StringIO()
    log = NoticeLog(ListStore(), err)
    denied = {"event": "mcp_tools_denied", "dialect": "anthropic", "main": True, "tools": ["contexto_recall"]}
    log.log({"event": "request", "transform_events": [denied, {"event": "request", "epoch": 0}]})
    log.log({"event": "request", "transform_events": [denied]})
    [line] = err.getvalue().splitlines()
    assert line.startswith("contexto: warning: Claude Code refused Contexto's contexto_recall")
    assert len(log.store.events) == 2


# The engine runs the check on the main conversation ---------------------------------------------------

def test_engine_puts_the_event_before_its_request_event():
    engine = RewriteEngine(MemoryStore(), config=Config(bound=150_000))
    clock = Clock()
    engine.tools = ToolCheck(engine.store, clock=clock)
    clock.now += GRACE_S
    _, _, events = engine(claude_request(BUILTIN), {}, "/v1/messages")
    names = [e["event"] for e in events]
    assert "mcp_tools_missing" in names and names.index("mcp_tools_missing") < len(names) - 1
    assert events[-1].get("main") is True  # the engine's request event stays last
    _, _, events = engine(claude_request(BUILTIN), {}, "/v1/messages/count_tokens")
    assert "mcp_tools_missing" not in [e["event"] for e in events]


def test_engine_normal_claude_and_codex_requests_raise_nothing(tmp_path: Path):
    store = SessionStore(tmp_path, "s")
    engine = RewriteEngine(store, config=Config(bound=150_000))
    clock = Clock()
    engine.tools = ToolCheck(store, clock=clock)
    clock.now += GRACE_S
    _, _, events = engine(claude_request(BUILTIN + CONTEXTO), {}, "/v1/messages")
    assert not [e for e in events if e["event"].startswith("mcp_tools")]
    store.set_mcp_listed()  # what the MCP server records when it answers tools/list
    codex = RewriteEngine(store, config=Config(bound=150_000))
    codex.tools = ToolCheck(store, clock=clock, wall=lambda: 0.0)
    _, _, events = codex(codex_request(), {}, "/responses")
    assert not [e for e in events if e["event"].startswith("mcp_tools")] and codex.tools_verdict() is None


def test_engine_check_does_not_alter_the_request():
    engine = RewriteEngine(MemoryStore(), config=Config(bound=150_000))
    request = claude_request(BUILTIN, deferred=CONTEXTO)
    before = copy.deepcopy(request)
    upstream, _, _ = engine(request, {}, "/v1/messages")
    assert request == before and upstream == before


def test_engine_check_that_raises_keeps_the_rewrite_and_logs_only_its_type():
    # A diagnostic must never cost a successful rewrite (the proxy's catch-all would forward the original).
    request = claude_request(BUILTIN)
    expected, _, _ = RewriteEngine(MemoryStore(), config=Config(bound=150_000))(copy.deepcopy(request), {},
                                                                                "/v1/messages")

    class Raising:
        def observe(self, *args, **kwargs):
            raise ValueError("secret request text")

    engine = RewriteEngine(MemoryStore(), config=Config(bound=150_000))
    engine.tools = Raising()
    upstream, _, events = engine(request, {}, "/v1/messages")
    assert upstream == expected
    assert {"event": "toolcheck_error", "error": "ValueError"} in events
    assert "secret" not in json.dumps(events)
    assert events[-1].get("main") is True  # the engine's request event stays last


# The launcher's warning -----------------------------------------------------------------------------

class ListStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log(self, event: dict) -> None:
        self.events.append(event)


class LockedProxy:
    """What report_missing_tools needs from ContextoProxy: the transform, its lock, and the log."""

    def __init__(self, event: dict | None) -> None:
        self.transform_lock = threading.Lock()
        self.logged: list[dict] = []
        self.locked_during_verdict: bool | None = None
        proxy = self

        class Transform:
            def tools_verdict(self):
                proxy.locked_during_verdict = proxy.transform_lock.locked()
                return event

        self.transform = Transform()

    def log(self, event: dict) -> None:
        self.logged.append(event)


def test_report_missing_tools_runs_under_the_transform_lock(monkeypatch):
    # A request still in a handler after proxy_stop_incomplete cannot run the check while the exit does.
    from contexto import launcher

    event = {"event": "mcp_tools_missing", "dialect": "anthropic", "at": "exit", "repeated": False}
    proxy = LockedProxy(event)
    launcher.report_missing_tools(proxy)
    assert proxy.locked_during_verdict is True and proxy.logged == [event]
    assert not proxy.transform_lock.locked()
    # A handler stuck holding the lock: the exit waits a moment, then skips the report rather than hang.
    monkeypatch.setattr(launcher, "REPORT_LOCK_TIMEOUT_S", 0.05)
    stuck = LockedProxy(event)
    stuck.transform_lock.acquire()
    launcher.report_missing_tools(stuck)
    assert stuck.locked_during_verdict is None and stuck.logged == []


@pytest.mark.parametrize("dialect, listed, words", [
    ("anthropic", False, ["Claude Code", "MCP server", "did not start", "contexto_search"]),
    ("anthropic", True, ["Claude Code", "stopped after starting", "'/mcp'", "mcp__contexto__"]),
    ("responses", False, ["Codex", "MCP server", "did not answer Codex's tool list in time", "v0.2.8"]),
])
def test_missing_tools_message_names_the_cause_and_the_fix(dialect, listed, words):
    message = missing_tools_message(dialect, listed)
    assert all(w in message for w in words), message
    assert "README" in message


def test_notice_log_prints_the_warning_once_from_a_request_or_at_exit():
    err = io.StringIO()
    log = NoticeLog(ListStore(), err)
    missing = {"event": "mcp_tools_missing", "dialect": "anthropic", "at": "request", "server_listed": False}
    log.log({"event": "request", "transform_events": [missing, {"event": "request", "epoch": 0}]})
    log.log({"event": "request", "transform_events": [missing]})
    lines = err.getvalue().splitlines()
    assert len(lines) == 1 and lines[0].startswith("contexto: warning:") and "Claude Code" in lines[0]
    assert len(log.store.events) == 2  # everything is still logged
    short = NoticeLog(ListStore(), io.StringIO())
    short.log({**missing, "at": "exit", "repeated": False})  # a run shorter than the grace
    [line] = short.stderr.getvalue().splitlines()
    assert line.startswith("contexto: warning:")


def test_notice_log_repeats_the_warning_once_after_the_cli_exits():
    # Interactive sessions report mid-run, where the TUI can draw over it: the exit event shows it again.
    err = io.StringIO()
    log = NoticeLog(ListStore(), err)
    missing = {"event": "mcp_tools_missing", "dialect": "responses", "at": "request", "server_listed": False}
    log.log({"event": "request", "transform_events": [missing]})
    log.log({**missing, "at": "exit", "repeated": True})
    log.log({**missing, "at": "exit", "repeated": True})
    first, again = err.getvalue().splitlines()
    assert first.startswith("contexto: warning: Codex")
    assert again.startswith("contexto: warning (repeated from the session): Codex")


def test_notice_log_is_quiet_for_normal_events():
    err = io.StringIO()
    log = NoticeLog(ListStore(), err)
    log.log({"event": "request", "transform_events": [{"event": "mcp_tools_found", "dialect": "anthropic"}]})
    log.log({"event": "proxy_stop"})
    assert err.getvalue() == ""


# The MCP server's record ----------------------------------------------------------------------------

def test_store_records_when_the_mcp_server_listed_its_tools(tmp_path: Path):
    store = SessionStore(tmp_path, "s")
    assert store.mcp_listed_at() is None
    store.set_window(40_000, 3.0)
    store.set_mcp_listed()
    first = store.mcp_listed_at()
    assert isinstance(first, float) and store.window() == (40_000, 3.0)  # other metadata kept
    memory = MemoryStore()
    assert memory.mcp_listed_at() is None
    memory.set_mcp_listed()
    assert isinstance(memory.mcp_listed_at(), float)


def test_mcp_server_records_its_first_tools_list(tmp_path: Path):
    from contexto.mcp import Server

    class Recording(MemoryStore):
        calls = 0

        def set_mcp_listed(self) -> None:
            Recording.calls += 1
            super().set_mcp_listed()

    store = Recording()
    out = io.StringIO()
    server = Server(store, out, io.StringIO())
    init = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
            '"capabilities":{},"clientInfo":{"name":"t","version":"1"}}}')
    listing = '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
    server.serve([init, listing, listing.replace('"id":2', '"id":3')])
    assert store.mcp_listed_at() is not None and Recording.calls == 1
    assert len(out.getvalue().splitlines()) == 3  # every request answered, protocol output only


def test_mcp_server_tools_list_survives_a_store_that_cannot_record():
    from contexto.mcp import Server

    class Failing(MemoryStore):
        def set_mcp_listed(self) -> None:
            raise OSError("read-only")

    out = io.StringIO()
    server = Server(Failing(), out, io.StringIO())
    reply = server.handle_line('{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
    assert reply is not None and reply["result"]["tools"]


def test_mcp_server_tools_list_survives_a_broken_stderr_too():
    from contexto.mcp import Server

    class Failing(MemoryStore):
        def set_mcp_listed(self) -> None:
            raise OSError("read-only")

    class BrokenPipe(io.StringIO):
        def write(self, text):
            raise BrokenPipeError("stderr closed")

    server = Server(Failing(), io.StringIO(), BrokenPipe())
    reply = server.handle_line('{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
    assert reply is not None and reply["result"]["tools"]


# contexto run codex, end to end with a fake codex ------------------------------------------------------

FAKE_CODEX = r'''
import json, os, subprocess, sys, tomllib, urllib.request

# Starts the MCP server from the config contexto wrote, as Codex does, and lists its tools; a server
# that cannot be started is skipped without a word, as codex-cli 0.163.0 does. Then one Responses request.
config = tomllib.load(open(os.path.join(os.environ["CODEX_HOME"], "config.toml"), "rb"))
server = config["mcp_servers"]["contexto"]
record = {"command": server["command"], "listed": False}
try:
    proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True,
                            env={"PATH": os.environ["PATH"], **server.get("env", {})})
except OSError:
    proc = None
if proc is not None:
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake-codex", "version": "0"}}
    for i, (method, params) in enumerate((("initialize", init), ("tools/list", {}))):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i + 1, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        reply = json.loads(proc.stdout.readline())
    record["listed"] = bool(reply.get("result", {}).get("tools"))
body = os.environ["FAKE_BODY"].encode()
base = config["model_providers"]["contexto"]["base_url"]
request = urllib.request.Request(base + "/responses", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer fake"})
with urllib.request.urlopen(request, timeout=10) as response:
    record["status"] = response.status
    response.read()
if proc is not None:
    proc.stdin.close()
    proc.wait(timeout=10)
json.dump(record, open(os.environ["FAKE_RECORD"], "w"))
'''


class ResponsesUpstream:
    """A loopback upstream that answers every POST with a small Responses JSON body."""

    def __enter__(self) -> str:
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.dumps({"id": "resp_1", "status": "completed", "output": [],
                                   "usage": {"input_tokens": 10, "output_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


def run_fake_codex(tmp_path: Path, monkeypatch, capsys, session: str) -> tuple[int, str, dict, list[dict]]:
    from contexto import cli

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "codex"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CODEX}")
    fake.chmod(0o755)
    home = tmp_path / "ctx"
    # PATH as for a user without contexto on it: only the fake codex and the system directories.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    monkeypatch.setenv("FAKE_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("FAKE_BODY", json.dumps(codex_request()))
    monkeypatch.chdir(tmp_path)
    with ResponsesUpstream() as upstream:
        code = cli.main(["run", "--session", session, "--upstream", upstream, "--codex-upstream", upstream,
                         "--codex-key-helper", "echo fake", "codex", "exec", "hi"])
    err = capsys.readouterr().err
    record = json.loads((tmp_path / "record.json").read_text())
    log = home / "sessions" / session / "log.jsonl"
    return code, err, record, [json.loads(line) for line in log.read_text().splitlines()]


def test_codex_normal_run_gets_no_warning(tmp_path, monkeypatch, capsys):
    code, err, record, events = run_fake_codex(tmp_path, monkeypatch, capsys, "c1")
    assert code == 0 and record["status"] == 200
    assert record["listed"] and record["command"] == sys.executable  # v0.2.8: started by absolute path
    assert "warning" not in err and not [e for e in events if str(e.get("event")).startswith("mcp_tools")]


def test_codex_with_v027_config_and_contexto_off_path_gets_the_warning(tmp_path, monkeypatch, capsys):
    """The original failure: v0.2.7 registered the server as a bare 'contexto'. Off PATH, Codex cannot
    start it, runs the turn without the tools, and says nothing; Contexto now does."""
    from contexto import codex

    monkeypatch.setattr(codex, "_mcp_server", lambda session_id: ("contexto", ["mcp", "--session", session_id]))
    code, err, record, events = run_fake_codex(tmp_path, monkeypatch, capsys, "c2")
    assert code == 0 and record["status"] == 200 and record["command"] == "contexto" and not record["listed"]
    warnings = [line for line in err.splitlines() if "warning" in line]
    assert len(warnings) == 1 and "Codex is running without Contexto's tools" in warnings[0]
    assert "did not answer Codex's tool list in time" in warnings[0] and "v0.2.8" in warnings[0]
    [missing] = [e for e in events if e["event"] == "mcp_tools_missing"]
    assert missing["dialect"] == "responses" and missing["at"] == "exit" and missing["server_listed"] is False


def test_codex_back_to_back_launch_does_not_count_the_previous_servers_record(tmp_path, monkeypatch, capsys):
    """A second launch on the same session can start well within the clock slack of the first launch's
    tools/list (0.7 s in a zero-spend Claude Code run against a fast stub). The launcher clears the
    record before the engine starts, so only this launch's server can count."""
    from contexto import codex

    code, err, record, _ = run_fake_codex(tmp_path, monkeypatch, capsys, "c3")
    assert code == 0 and record["listed"] and "warning" not in err
    monkeypatch.setattr(codex, "_mcp_server", lambda session_id: ("contexto", ["mcp", "--session", session_id]))
    code, err, record, events = run_fake_codex(tmp_path, monkeypatch, capsys, "c3")
    assert code == 0 and not record["listed"]
    assert len([line for line in err.splitlines() if "warning" in line]) == 1
    [missing] = [e for e in events if e.get("event") == "mcp_tools_missing"]
    assert missing["server_listed"] is False


def test_store_record_can_be_cleared(tmp_path: Path):
    for store in (SessionStore(tmp_path, "s"), MemoryStore()):
        store.set_mcp_listed()
        store.clear_mcp_listed()
        assert store.mcp_listed_at() is None
