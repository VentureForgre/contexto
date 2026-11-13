"""Claude Code's gateway hint headers in the rewrite engine (D70): one test per row of the decision
table in ``RewriteEngine._classify``.

The header rows use the names and values Claude Code 2.1.322 sent a stub with
``CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`` (docs/research/D70-serve-probes.md). The fallback rows send no
hint header and replay the 2.1.314 and 2.1.318 captures in ``tests/fixtures``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from contexto.hints import (
    COMPACTION_HEADER,
    CONTEXT_COMPACTED_HEADER,
    GATED_HINT_HEADERS,
    REQUEST_CLASS_HEADER,
    GatewayHints,
    gateway_hints,
    without_hint_headers,
)
from contexto.rewrite import CLAUDE_CODE_COMPACTION_SUMMARY, MESSAGES, Config, RewriteEngine, compaction_summary
from contexto.store.memory import MemoryStore
from test_clear_main import OLD, SUBAGENT_TOOLS, changes, metadata, side_request
from test_codex import request as codex_request
from test_rewrite import CONFIG, FILL, assistant, conversation, history, request, tool_result, tool_use, user

FIXTURES = Path(__file__).parent / "fixtures"
COMPACTIONS = json.loads((FIXTURES / "compaction_shapes_2_1_31x.json").read_text())
SUBAGENTS = json.loads((FIXTURES / "subagent_shapes_2_1_31x.json").read_text())
CAPTURES = [(version, scenario) for version in sorted(COMPACTIONS) for scenario in sorted(COMPACTIONS[version])]
AGENT_ID = "a800b6f5b41866e3f"
PROMPT_ID = "c484ea9c-0000-4000-8000-000000000001"
DRIFTED = "This conversation continues an earlier one whose context ran out. A summary of it follows.\n"


def sent(session: str | None = OLD, request_class: str | None = None, *, entrypoint: str = "cli",
         agent: bool = False, **hints: str) -> dict[str, str]:
    """The headers of a Claude Code request: the ungated ones, plus the hint headers asked for
    (``context_compacted="manual"`` is ``x-claude-code-context-compacted: manual``)."""
    found = {"User-Agent": f"claude-cli/2.1.322 (external, {entrypoint})"}
    if session:
        found["X-Claude-Code-Session-Id"] = session
    if agent:
        found["x-claude-code-agent-id"] = AGENT_ID
    if request_class is not None:
        found[REQUEST_CLASS_HEADER] = request_class
    for name, value in hints.items():
        found["x-claude-code-" + name.replace("_", "-")] = value
    return found


def new_engine(config: Config = CONFIG) -> RewriteEngine:
    return RewriteEngine(MemoryStore(key=b"k" * 32), config=config)


def send(engine: RewriteEngine, req: dict, headers: dict[str, str], *, tool_calls: bool | None = False) -> dict:
    """One request and its answer; returns the engine's request event with the other events' names."""
    _, _, events = engine(req, headers, MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 1_200}, req, tool_calls=tool_calls)
    return {**events[-1], "changes": changes(events),
            "mismatches": [(e["header"], e["value"], e["signature"]) for e in events if e["event"] == "hint_mismatch"],
            "names": [e["event"] for e in events]}


def main_request(messages: list[dict], session: str = OLD, **extra) -> dict:
    return request(messages, metadata=metadata(session), **extra)


def toolless(messages: list[dict], session: str = OLD) -> dict:
    req = main_request(messages, session)
    del req["tools"]
    return req


# Reading the headers ------------------------------------------------------------------------------------


def test_the_hints_are_read_by_name_in_any_case_and_only_known_values_are_kept():
    hints = gateway_hints({"X-Claude-Code-Request-Class": "main", "X-CLAUDE-CODE-CONTEXT-COMPACTED": "auto",
                           "x-claude-code-compaction": "reactive", "x-claude-code-prompt-id": PROMPT_ID})
    assert hints == GatewayHints("main", "auto", "reactive")
    assert gateway_hints({}) == GatewayHints(None, None, None)
    # A value outside the documented set is never kept: an unknown class reads as no class, and an
    # unknown trigger as "other" (the header is there, its value is not one to log).
    odd = gateway_hints({REQUEST_CLASS_HEADER: "Main ", CONTEXT_COMPACTED_HEADER: "sideways", COMPACTION_HEADER: ""})
    assert odd == GatewayHints(None, "other", None)
    for known in ("main", "subagent", "workflow", "compaction", "auxiliary"):
        assert gateway_hints({REQUEST_CLASS_HEADER: known}).request_class == known


def test_the_gated_set_is_the_six_headers_the_variable_turns_on():
    assert GATED_HINT_HEADERS == {
        "x-claude-code-request-class", "x-claude-code-context-compacted", "x-claude-code-compaction",
        "x-claude-code-prompt-id", "x-claude-code-agent-type", "x-claude-code-prev-tool-durations"}
    headers = {"X-Claude-Code-Session-Id": OLD, "x-claude-code-agent-id": AGENT_ID,
               "x-claude-code-parent-agent-id": "a1", "X-Claude-Code-Request-Class": "subagent",
               "x-claude-code-prompt-id": PROMPT_ID, "x-claude-code-agent-type": "general-purpose",
               "x-claude-code-prev-tool-durations": "Bash=30", "x-claude-code-compaction": "manual",
               "x-claude-code-context-compacted": "manual", "x-api-key": "k"}
    assert without_hint_headers(headers) == {"X-Claude-Code-Session-Id": OLD, "x-claude-code-agent-id": AGENT_ID,
                                             "x-claude-code-parent-agent-id": "a1", "x-api-key": "k"}
    assert len(headers) == 10  # the argument is not changed


# Row: no hint header ------------------------------------------------------------------------------------


def test_without_a_hint_header_the_request_event_names_no_class():
    engine = new_engine()
    event = send(engine, main_request(history(1)), sent())
    assert event["main"] is True and "hint_mismatch" not in event["names"]
    assert not {"request_class", "compaction", "context_compacted"} & set(event)


# Row: request-class main --------------------------------------------------------------------------------


def test_class_main_is_judged_without_a_tool_list():
    """The session's first request is Claude Code's title request, which is main until the first prompt
    (D50). A main request with no tool list is, by the signature, one more side request and ties it at
    one turn start; its class says it is the main conversation."""
    engine = new_engine()
    title = send(engine, side_request("title this", OLD), sent(request_class="auxiliary"))
    assert title["main"] is True and title["request_class"] == "auxiliary"
    first_prompt = toolless(conversation("the first prompt", 1))
    prompt = send(engine, first_prompt, sent(request_class="main", prompt_id=PROMPT_ID))
    assert prompt["main"] is True and prompt["changes"] == ["after_side_request"]
    assert prompt["request_class"] == "main"
    # The header and the tool-list signature disagreed, which is logged once, with fixed words only.
    assert prompt["mismatches"] == [("request_class", "main", "no_tool_list")]
    assert PROMPT_ID not in json.dumps(prompt)


def test_fallback_a_request_without_a_tool_list_is_a_side_request():
    engine = new_engine()
    assert send(engine, side_request("title this", OLD), sent())["main"] is True
    prompt = send(engine, toolless(conversation("the first prompt", 1)), sent())
    assert prompt["main"] is False and prompt["changes"] == []
    # With a tool list it is judged, as before this change.
    again = new_engine()
    send(again, side_request("title this", OLD), sent())
    assert send(again, main_request(conversation("the first prompt", 1)), sent())["changes"] == ["after_side_request"]


def test_class_main_is_the_request_whose_session_start_is_judged():
    """D38 judges the session start on the first request the dialect judges. A subagent's request has a
    tool list and Claude Code's injected context but no session start, so by the signature alone it
    would be reported; its class keeps it out, and the main request is the one judged."""
    engine = new_engine()
    sub = request(conversation("SUB-TASK do it", 1), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    first = send(engine, sub, sent(request_class="subagent", agent=True))
    assert "session_start_unrecognised" not in first["names"] and engine.clients.session_start_judged is False
    second = send(engine, toolless(conversation("the prompt", 1)), sent(request_class="main"))
    assert "session_start_unrecognised" in second["names"] and engine.clients.session_start_judged is True
    # Fallback: the first request with a tool list is the one judged.
    plain = new_engine()
    assert "session_start_unrecognised" in send(plain, sub, sent(agent=True))["names"]


# Row: request-class compaction --------------------------------------------------------------------------


def test_class_compaction_is_bounded_and_handled_as_mains_own_request():
    """The summarization request is main's history with the instructions appended (measured: same
    first message, tools and system as a main turn), so it has main's key. It goes through main's
    epoch like any main request: the same bytes are sent with the header as without it."""
    turns = [*history(8), user("one more prompt")]
    summarize = [*turns, assistant("answer"), user("CRITICAL: Respond with TEXT ONLY. Summarize the conversation.")]

    def run(hinted: bool) -> tuple[dict, dict, RewriteEngine]:
        engine = new_engine()
        engine(main_request(turns), sent(request_class="main") if hinted else sent(), MESSAGES)
        headers = sent(request_class="compaction", compaction="manual") if hinted else sent()
        upstream, _, events = engine(main_request(summarize), headers, MESSAGES)
        return upstream, events[-1], engine

    hinted, event, engine = run(True)
    plain, plain_event, _ = run(False)
    assert event["main"] is True and event["request_class"] == "compaction" and event["compaction"] == "manual"
    assert len(engine.states) == 1  # main's own conversation, not a new one
    assert hinted == plain and len(hinted["messages"]) < len(summarize)  # paged, as main's turns are
    assert not event.get("over_bound") and event["estimate"] == plain_event["estimate"] <= CONFIG.bound
    assert "compaction" not in plain_event and "request_class" not in plain_event


def test_class_compaction_does_not_mark_a_conversation_compacted():
    engine = new_engine()
    send(engine, main_request(history(2)), sent(request_class="main"))
    other = main_request(conversation("another first message", 2))
    event = send(engine, other, sent(request_class="compaction", compaction="auto"))
    assert event["main"] is False and event["changes"] == []  # two turn starts each: a tie keeps main


# Rows: request-class subagent, workflow, auxiliary ------------------------------------------------------


@pytest.mark.parametrize("side_class", ["subagent", "workflow", "auxiliary"])
def test_a_side_class_never_takes_over_main(side_class):
    """A conversation with more turn starts than main takes over by the turn-start rule, which is the
    only thing that kept a subagent out when ``X-Claude-Code-Agent-Id`` was missing. The class keeps
    it out by every rule."""
    engine = new_engine()
    assert send(engine, main_request(history(1)), sent(request_class="main"))["main"] is True
    grown = request(conversation("SUB-TASK do it", 4), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    event = send(engine, grown, sent(request_class=side_class))
    assert event["main"] is False and event["changes"] == [] and event["request_class"] == side_class
    # Fallback: no class and no agent id, so only the turn-start rule is left, and it hands main over.
    plain = new_engine()
    send(plain, main_request(history(1)), sent())
    assert send(plain, grown, sent())["changes"] == ["more_turn_starts"]


@pytest.mark.parametrize("side_class", ["subagent", "workflow", "auxiliary"])
def test_a_side_class_in_a_new_session_never_takes_over_main(side_class):
    """D50's ``new_cli_session`` hands main to a request with a tool list and a session id no
    conversation was main in. The class keeps a side request out of that rule too."""
    new = "916e1695-b1be-43de-abe8-eb4ffa7e0931"
    engine = new_engine()
    send(engine, main_request(history(2)), sent(request_class="main"))
    event = send(engine, main_request(conversation("first prompt after clear", 1), new), sent(new, side_class))
    assert event["main"] is False and event["changes"] == []
    plain = new_engine()
    send(plain, main_request(history(2)), sent())
    assert send(plain, main_request(conversation("first prompt after clear", 1), new),
                sent(new))["changes"] == ["new_cli_session"]


@pytest.mark.parametrize("version", sorted(SUBAGENTS))
@pytest.mark.parametrize("scenario", ["fg", "resume"])
def test_fallback_a_captured_subagent_is_kept_out_by_its_agent_id(version, scenario):
    """The 2.1.314 and 2.1.318 captures: main's request up to its Agent call, then the subagent's
    request, opened by that call's prompt and carrying ``X-Claude-Code-Agent-Id``. With no hint header
    the agent id keeps it out however many turn starts it has; with its class it needs no agent id."""
    captured = SUBAGENTS[version][scenario]
    call = next(i for i, m in enumerate(captured) if m["role"] == "assistant")
    prompt = captured[call]["content"][-1]["input"]["prompt"]
    assert prompt == f"SUB-{scenario.upper()} task"
    resumed = request(conversation(prompt, 5), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    for headers in (sent(agent=True), sent(request_class="subagent", agent=True), sent(request_class="subagent")):
        engine = new_engine(Config(bound=1_000_000))
        first = sent(request_class="main") if REQUEST_CLASS_HEADER in headers else sent()
        assert send(engine, main_request(captured[:call]), first)["main"] is True
        event = send(engine, resumed, headers)
        assert event["main"] is False and event["changes"] == []
        after = send(engine, main_request(captured), first)
        assert after["main"] is True and after["changes"] == []


def test_class_auxiliary_with_a_tool_list_is_not_judged():
    """A side request that carries a tool list is, by the signature, a judged request: when it arrives
    first, the user's first prompt ties it at one turn start and stays a side conversation. Its class
    says it is a side request, so the prompt takes over as after any other side request."""
    side = request([user("classify this")], metadata=metadata(OLD), max_tokens=512)
    engine = new_engine()
    first = send(engine, side, sent(request_class="auxiliary"))
    assert first["main"] is True and first["mismatches"] == [("request_class", "auxiliary", "tool_list")]
    first_prompt = main_request(conversation("the first prompt", 1))
    prompt = send(engine, first_prompt, sent(request_class="main"))
    assert prompt["main"] is True and prompt["changes"] == ["after_side_request"] and prompt["mismatches"] == []
    plain = new_engine()
    send(plain, side, sent())
    assert send(plain, first_prompt, sent())["main"] is False


def test_a_side_class_that_arrives_first_is_not_checked_for_contextos_tools():
    """The first conversation seen is main until another takes over. The tool check judges main's
    tool list, and a subagent may run without Contexto's tools (contexto.toolcheck)."""
    sub = request(conversation("SUB-TASK do it", 1), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    engine = new_engine()
    assert send(engine, sub, sent(request_class="subagent", agent=True))["main"] is True
    assert engine.tools.present is None
    plain = new_engine()
    send(plain, sub, sent(agent=True))
    assert plain.tools.present is False  # the signature alone judges it, as before


# Row: an unknown class ----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["banana", "", "MAIN", "main, subagent", "x" * 5000])
def test_an_unknown_class_is_ignored_and_never_logged(value):
    engine = new_engine()
    send(engine, side_request("title this", OLD), sent())
    event = send(engine, main_request(conversation("the first prompt", 1)), sent(request_class=value))
    assert event["changes"] == ["after_side_request"] and event["mismatches"] == []  # the signatures decide
    assert "request_class" not in event
    assert not value or value not in json.dumps(event)
    grown = request(conversation("SUB-TASK do it", 4), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    assert send(engine, grown, sent(request_class=value))["changes"] == ["more_turn_starts"]


# Row: context-compacted ---------------------------------------------------------------------------------


def drift(messages: list[dict]) -> list[dict]:
    """The captured compacted conversation with a summary sentence Contexto does not know."""
    drifted = copy.deepcopy(messages)
    first = drifted[0]
    if isinstance(first["content"], str):
        first["content"] = first["content"].replace(CLAUDE_CODE_COMPACTION_SUMMARY, DRIFTED)
    else:
        for block in first["content"]:
            if block.get("type") == "text":
                block["text"] = block["text"].replace(CLAUDE_CODE_COMPACTION_SUMMARY, DRIFTED)
    assert not compaction_summary(drifted)
    return drifted


def replay(captured: list[dict], *, hints: bool, drifted: bool, trigger: str = "manual") -> list[dict]:
    """A captured compaction through one engine. The last request is the compacted conversation's
    first; with ``hints`` every request carries its class and that one ``context-compacted``."""
    engine = new_engine(Config(bound=1_000_000))
    out = []
    for i, captured_request in enumerate(captured):
        last = i == len(captured) - 1
        messages = captured_request["messages"]
        req = {"model": "claude-sonnet-5-5", "max_tokens": 32_000, "metadata": metadata(OLD),
               "messages": drift(messages) if last and drifted else messages}
        if captured_request["tools"]:
            req["tools"] = SUBAGENT_TOOLS
        headers = {"User-Agent": captured_request["user_agent"], "X-Claude-Code-Session-Id": OLD}
        if hints:
            headers[REQUEST_CLASS_HEADER] = "main" if captured_request["tools"] else "auxiliary"
            if last:
                headers[CONTEXT_COMPACTED_HEADER] = trigger
        out.append(send(engine, req, headers))
    return out


@pytest.mark.parametrize("version, scenario", CAPTURES)
def test_context_compacted_marks_the_compaction_when_the_summary_text_has_drifted(version, scenario):
    rows = replay(COMPACTIONS[version][scenario], hints=True, drifted=True)
    assert rows[-1]["main"] is True and rows[-1]["changes"] == ["compacted"]
    assert rows[-1]["context_compacted"] == "manual" and rows[-1]["request_class"] == "main"
    # The early warning that the text signature drifted.
    assert rows[-1]["mismatches"] == [("context_compacted", "manual", "compaction_summary")]
    assert all("context_compacted" not in row for row in rows[:-1])


@pytest.mark.parametrize("version, scenario", CAPTURES)
def test_fallback_the_summary_text_marks_the_compaction_without_the_header(version, scenario):
    captured = COMPACTIONS[version][scenario]
    rows = replay(captured, hints=False, drifted=False)
    assert rows[-1]["main"] is True and rows[-1]["changes"] == ["compacted"]
    assert all(not row["mismatches"] and "request_class" not in row for row in rows)
    # With the text drifted and no header nothing marks it: the compacted conversation is a side one.
    lost = replay(captured, hints=False, drifted=True)
    assert lost[-1]["main"] is False and lost[-1]["changes"] == []


@pytest.mark.parametrize("version, scenario", CAPTURES)
def test_the_header_and_the_summary_text_agree_on_the_captures(version, scenario):
    rows = replay(COMPACTIONS[version][scenario], hints=True, drifted=False, trigger="auto")
    assert rows[-1]["changes"] == ["compacted"] and rows[-1]["context_compacted"] == "auto"
    assert all(not row["mismatches"] for row in rows)


def test_context_compacted_is_sent_once_and_the_conversation_stays_main():
    """Only the first main request after a compaction carries the header. The requests after it are
    main's own by then, and the text signature still counts for a request that never had the header."""
    engine = new_engine()
    old = history(2)
    send(engine, main_request(old), sent(request_class="main"))
    compacted = [user(DRIFTED + "\nSummary:\nthe user asked for a thing"), assistant("kept"), user("next prompt")]
    first = send(engine, main_request(compacted), sent(request_class="main", context_compacted="auto"))
    assert first["changes"] == ["compacted"] and first["context_compacted"] == "auto"
    later = [*compacted, assistant("answer"), user("one more")]
    event = send(engine, main_request(later), sent(request_class="main"))
    assert event["main"] is True and event["changes"] == [] and "context_compacted" not in event
    assert event["mismatches"] == []


def test_context_compacted_hands_over_inside_a_tool_loop_once_main_is_idle():
    """D59: a compaction that keeps a pending tool call sends it under the new key with no turn start.
    The header marks that request as the text signature does, under the same condition: main's
    summary request was answered with no client call."""
    compacted = [user(DRIFTED + "\nSummary:\nx"), tool_use(4), tool_result(4)]
    for answered, expected in ((False, ["compacted"]), (True, [])):
        engine = new_engine()
        send(engine, main_request(history(2)), sent(request_class="main"), tool_calls=answered)
        event = send(engine, main_request(compacted), sent(request_class="main", context_compacted="auto"))
        assert event["changes"] == expected and event["main"] is bool(expected)


def test_context_compacted_hands_over_inside_a_tool_loop_when_the_key_was_seen_before():
    """Reviewer A's last nit: a side request under the compacted conversation's key arrives first, so
    the request that keeps the pending call is not that key's first. The header is on that very
    request, so it hands over there; a request of an older compaction never carries it."""
    summary = user(DRIFTED + "\nSummary:\nx")
    compacted = [summary, tool_use(4), tool_result(4)]
    for answered, expected in ((False, ["compacted"]), (True, [])):
        engine = new_engine()
        send(engine, main_request(history(2)), sent(request_class="main"), tool_calls=answered)
        early = send(engine, toolless([summary]), sent(request_class="auxiliary"))
        assert early["main"] is False and early["changes"] == []
        event = send(engine, main_request(compacted), sent(request_class="main", context_compacted="auto"))
        assert event["changes"] == expected and event["main"] is bool(expected)
        later = send(engine, main_request(compacted), sent(request_class="main"))  # the header is sent once
        assert later["changes"] == [] and later["main"] is bool(expected)


def test_fallback_a_seen_key_waits_for_its_next_turn_start_without_the_header():
    """Without the header nothing changes: the text signature hands over a pending-call compaction only
    at its key's first request (D59), and otherwise at the conversation's next turn start."""
    summary = user(CLAUDE_CODE_COMPACTION_SUMMARY + "\nSummary:\nx")
    assert compaction_summary([summary])
    compacted = [summary, tool_use(4), tool_result(4)]
    engine = new_engine()
    send(engine, main_request(history(2)), sent())
    send(engine, toolless([summary]), sent())
    event = send(engine, main_request(compacted), sent())
    assert event["changes"] == [] and event["main"] is False
    turn = send(engine, main_request([*compacted, assistant("done"), user("next prompt")]), sent())
    assert turn["changes"] == ["compacted"] and turn["main"] is True


def test_context_compacted_with_an_unknown_trigger_still_marks_and_logs_no_value():
    engine = new_engine()
    send(engine, main_request(history(2)), sent(request_class="main"))
    compacted = [user(DRIFTED + "\nSummary:\nx"), assistant("kept"), user("next prompt")]
    event = send(engine, main_request(compacted), sent(request_class="main", context_compacted="sideways-9"))
    assert event["changes"] == ["compacted"] and event["context_compacted"] == "other"
    assert event["mismatches"] == [("context_compacted", "other", "compaction_summary")]
    assert "sideways" not in json.dumps(event)


def test_context_compacted_does_not_hand_main_to_another_session_or_a_subagent():
    new = "916e1695-b1be-43de-abe8-eb4ffa7e0931"
    compacted = [user(DRIFTED + "\nSummary:\nx"), tool_use(4), tool_result(4)]
    for headers, session in ((sent(new, "main", entrypoint="sdk-cli", context_compacted="auto"), new),
                             (sent(request_class="subagent", agent=True, context_compacted="auto"), OLD),
                             (sent(request_class="main", agent=True, context_compacted="auto"), OLD)):
        engine = new_engine()
        send(engine, main_request(history(2)), sent(request_class="main"))
        event = send(engine, main_request(compacted, session), headers)
        assert event["main"] is False and event["changes"] == []


# Rows: a header and a signature disagree ----------------------------------------------------------------


def test_class_main_with_an_agent_id_never_takes_over_and_is_logged():
    """``X-Claude-Code-Agent-Id`` says a subagent sent it and the class says main. The answer that
    leaves main where it is wins, and the pair is named once."""
    engine = new_engine()
    send(engine, main_request(history(1)), sent(request_class="main"))
    grown = request(conversation("SUB-TASK do it", 4), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    event = send(engine, grown, sent(request_class="main", agent=True))
    assert event["main"] is False and event["changes"] == []
    assert event["mismatches"] == [("request_class", "main", "agent_id")]
    assert AGENT_ID not in json.dumps(event)


@pytest.mark.parametrize("side_class", ["subagent", "workflow"])
def test_a_subagent_class_without_an_agent_id_is_logged(side_class):
    engine = new_engine()
    send(engine, main_request(history(1)), sent(request_class="main"))
    sub = request(conversation("SUB-TASK do it", 1), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    assert send(engine, sub, sent(request_class=side_class))["mismatches"] == [
        ("request_class", side_class, "no_agent_id")]
    agreed = new_engine()
    assert send(agreed, sub, sent(request_class=side_class, agent=True))["mismatches"] == []


def test_each_disagreeing_pair_is_logged_once_per_engine():
    engine = new_engine()
    send(engine, main_request(history(1)), sent(request_class="main"))
    sub = request(conversation("SUB-TASK do it", 1), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    assert len(send(engine, sub, sent(request_class="subagent"))["mismatches"]) == 1
    assert send(engine, sub, sent(request_class="subagent"))["mismatches"] == []
    # Another pair is still reported.
    assert send(engine, sub, sent(request_class="main", agent=True))["mismatches"] == [
        ("request_class", "main", "agent_id")]


def test_the_measured_requests_agree_with_their_signatures():
    """The 2.1.322 shapes: a title request (class auxiliary, an empty tool list), main turns with a
    tool list, a subagent's turn with its agent id, and the summarization request."""
    engine = new_engine(Config(bound=1_000_000))
    title = side_request("title this", OLD)
    title["tools"] = []
    turns = conversation("the first prompt", 2)
    sub = request(conversation("SUB-TASK do it", 1), tools=SUBAGENT_TOOLS, metadata=metadata(OLD))
    events = [send(engine, title, sent(request_class="auxiliary")),
              send(engine, main_request(turns), sent(request_class="main", prompt_id=PROMPT_ID)),
              send(engine, sub, sent(request_class="subagent", agent=True, agent_type="general-purpose",
                                     prompt_id=PROMPT_ID)),
              send(engine, main_request([*turns, tool_use(1), tool_result(1)]),
                   sent(request_class="main", prev_tool_durations="Agent=4")),
              send(engine, main_request([*turns, tool_use(1), tool_result(1), assistant("ok"), user("TEXT ONLY")]),
                   sent(request_class="compaction", compaction="manual"))]
    assert [e["request_class"] for e in events] == ["auxiliary", "main", "subagent", "main", "compaction"]
    assert [e["main"] for e in events] == [True, True, False, True, True]
    assert all(e["mismatches"] == [] for e in events)
    logged = json.dumps(events)
    assert PROMPT_ID not in logged and "general-purpose" not in logged and "Agent=4" not in logged


# Other clients ------------------------------------------------------------------------------------------


def test_codex_requests_are_not_classified_by_the_headers():
    engine = new_engine()
    codex = codex_request([{"type": "message", "role": "user", "content": [{"type": "input_text", "text": FILL}]}])
    _, _, events = engine(codex, {REQUEST_CLASS_HEADER: "subagent", CONTEXT_COMPACTED_HEADER: "auto"},
                          "/v1/responses")
    assert "request_class" not in events[-1] and "context_compacted" not in events[-1]
    assert not [e for e in events if e["event"] == "hint_mismatch"]
