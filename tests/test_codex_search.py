"""Paged search and stubs for Responses (Codex) pages (M2 B23).

Search, the stub, the index line, and render_page all read a page through one mapping
(render.as_message, which uses lane A's ResponsesDialect.retained_blocks), so what search matches
is what a hit shows and recall returns. Anthropic pages are unchanged (also checked offline with a
digest over 62 real recorded pages).
"""

import io
import json
import re

import pytest

from contexto import mcp
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.pages import _texts, build_stub, page_matches
from contexto.store.render import render_page

PLANT = "cfg checkout-api.retention_days=juniper-2573"
# Builder-2's failing case (verified on main 1c203d5): a Codex message turn.
MESSAGE_TURN = [{"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": f"{PLANT}\nReply with exactly: OK"}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}]


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore(key=b"k" * 32)


def call(store, name, arguments):
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    result = server.handle_line(line)["result"]
    assert result["isError"] is False
    return result["content"][0]["text"]


def test_a_codex_fact_is_found_shown_and_recalled_at_its_offset(store):
    [pid] = store.page_out([MESSAGE_TURN], 0)
    assert store.search("retention_days") == [pid]  # was [] before B23
    reply = call(store, "contexto_search", {"query": "retention_days"})
    [hit] = [line for line in reply.split("\n") if line.startswith("> ")]
    assert hit.endswith(PLANT)  # shown in the hit
    offset = int(re.match(r"> (\d+): ", hit).group(1))
    assert call(store, "contexto_recall", {"page_id": pid, "offset": offset}).startswith(PLANT)  # and recalled


def test_a_codex_stub_and_index_line_carry_the_user_text_and_its_keys(store):
    [pid] = store.page_out([MESSAGE_TURN], 0)
    stub = store.get_page(pid)["stub"]
    assert stub["user_text"].startswith(PLANT)  # was '' before B23
    line = store.index_text(4000)
    assert line.startswith(f"{pid} turn 0: {PLANT}")


CALLS = {
    "function_call": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "check it"}]},
                      {"type": "function_call", "name": "read_file", "call_id": "c1",
                       "arguments": json.dumps({"path": "conf/app.toml", "note": "FN-ARG-MARKER"})}],
    "custom_tool_call": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "patch"}]},
                         {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2",
                          "input": "*** Begin Patch CUSTOM-INPUT-MARKER"}],
    "function_call_output": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run"}]},
                             {"type": "function_call_output", "call_id": "c3", "output": "FN-OUTPUT-MARKER done"}],
    "custom_tool_call_output": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]},
                                {"type": "custom_tool_call_output", "call_id": "c4",
                                 "output": [{"type": "input_text", "text": "CUSTOM-OUTPUT-MARKER ok"}]}],
}
MARKERS = {"function_call": "FN-ARG-MARKER", "custom_tool_call": "CUSTOM-INPUT-MARKER",
           "function_call_output": "FN-OUTPUT-MARKER", "custom_tool_call_output": "CUSTOM-OUTPUT-MARKER"}


@pytest.mark.parametrize("kind", sorted(CALLS))
def test_each_codex_call_and_output_item_is_searchable_and_shown(store, kind):
    [pid] = store.page_out([CALLS[kind]], 0)
    marker = MARKERS[kind]
    assert store.search(marker) == [pid]  # nothing was yielded for these items before B23
    reply = call(store, "contexto_search", {"query": marker})
    [hit] = [line for line in reply.split("\n") if line.startswith("> ")]
    assert marker in hit and "@stub" not in reply and "> stub:" not in reply


def test_a_codex_function_call_feeds_the_stub_tools_and_paths():
    stub = build_stub(CALLS["function_call"])
    assert stub["tools"] == ["read_file"] and "conf/app.toml" in stub["paths"] and stub["user_text"] == "check it"


def test_typeless_codex_messages_are_read_like_typed_ones():
    turn = [{"role": "user", "content": [{"type": "input_text", "text": PLANT}]}]  # no top-level type
    page = {"turn": 0, "messages": turn, "stub": build_stub(turn)}
    assert page_matches(page, "retention_days") and PLANT in render_page(page)
    assert build_stub(turn)["user_text"] == PLANT


def test_codex_context_sections_and_reasoning_are_neither_searched_nor_stubbed():
    turn = [{"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "<environment_context>cwd SECRET-CTX</environment_context>"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "REASONING-MARKER"}]},
            *MESSAGE_TURN]
    page = {"turn": 0, "messages": turn, "stub": build_stub(turn)}
    assert not page_matches(page, "SECRET-CTX") and not page_matches(page, "REASONING-MARKER")
    assert page["stub"]["user_text"].startswith(PLANT)  # the first searchable user text, not the context section


@pytest.mark.parametrize("turn", [MESSAGE_TURN, *CALLS.values(),
                                  [{"role": "user", "content": "anthropic plain text"},
                                   {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Bash",
                                                                      "input": {"command": "ls conf"}}]},
                                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                                                 "content": "app.toml"}]}]])
def test_render_and_matching_see_the_same_text_in_both_dialects(turn):
    """Every string search matches is in the rendered page (as typed, or JSON-escaped for tool input)."""
    page = {"turn": 0, "messages": turn}
    rendered = render_page(page)
    for text in _texts(turn):
        for piece in filter(None, text.split("\n")):
            assert piece in rendered or json.dumps(piece, ensure_ascii=False)[1:-1] in rendered
