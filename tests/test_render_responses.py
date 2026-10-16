"""M2 B20: ``render_page`` for Codex (Responses API) pages, aligned with the engine's retained-window
snapshot (A14). Item shapes follow codex-cli 0.154.0 as recorded in spikes/m2/codex/ (structure only);
all text is synthetic."""

from contexto.codex import RESPONSES
from contexto.rewrite import ANTHROPIC, retained_entries
from contexto.store.memory import MemoryStore
from contexto.store.render import match_lines, recall_chunk, render_page


def prompt(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def answer(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def codex_turn(planted="synthetic tool output"):
    """One Codex turn as the engine pages it: prompt, reasoning, a call and its output, the answer."""
    return [
        prompt("synthetic prompt about node 42"),
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-synthetic-1"},
        {"type": "function_call", "call_id": "call_1", "name": "exec", "arguments": '{"cmd": "cat log", "n": 3}'},
        {"type": "function_call_output", "call_id": "call_1", "output": f"line one\n{planted}\nline three"},
        answer("synthetic answer: node 42 links to node 7"),
    ]


ANTHROPIC_PAGE = {"turn": 4, "messages": [
    {"role": "user", "content": [{"type": "text", "text": "<system-reminder>synthetic context</system-reminder>"},
                                 {"type": "text", "text": "synthetic prompt"},
                                 {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "c3ludA=="}}]},
    {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                                      {"type": "text", "text": "synthetic answer"},
                                      {"type": "tool_use", "id": "t1", "name": "Read", "input": {"b": 2, "a": "é"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "plain result"},
                                 {"type": "tool_result", "tool_use_id": "t2", "content": [
                                     {"type": "text", "text": "listed result"}, {"type": "image"}, "junk"]}]},
    {"role": "system", "content": "synthetic carried note", "output_config": {"effort": "medium"}},
    {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "x"}, {"type": "server_tool_use"}]},
    {"role": "user", "content": "plain string prompt"},
    "not a message", {"role": "user", "content": 7}, {"role": "user", "content": None}]}


def test_anthropic_pages_render_byte_identically():
    """B20 must not move any existing page offset: this is main's render_page output (5243b42) for a page
    covering every Messages API branch, compared byte for byte."""
    expected = ("[turn 4] user:\n<system-reminder>synthetic context</system-reminder>\nsynthetic prompt\n"
                "[image omitted]\n[turn 4] assistant:\nsynthetic answer\ntool_use Read {\"a\": \"é\", \"b\": 2}\n"
                "[turn 4] user:\ntool_result:\nplain result\ntool_result:\nlisted result\n[image omitted]\n"
                "[malformed block omitted]\n[turn 4] system:\nsynthetic carried note\n[turn 4] assistant:\n"
                "[server_tool_use omitted]\n[turn 4] user:\nplain string prompt\n[malformed message omitted]\n"
                "[turn 4] user:\n[malformed content omitted]\n[turn 4] user:\n")
    assert render_page(ANTHROPIC_PAGE).encode() == expected.encode()


def test_each_responses_item_type_renders_its_text():
    context = {"type": "input_text", "text": "<environment_context>synthetic</environment_context>"}
    items = [
        {"type": "message", "role": "user", "content": [context, {"type": "input_text", "text": "asked"},
                                                         {"type": "input_image", "image_url": "data:x"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "said"},
                                                             {"type": "text", "text": "also said"}]},
        {"type": "message", "role": "user", "content": "plain string prompt"},
        {"type": "reasoning", "id": "rs", "encrypted_content": "opaque-synthetic"},
        {"type": "function_call", "name": "exec", "arguments": '{"z": 1, "a": "x"}'},
        {"type": "function_call", "name": "exec", "arguments": "not json"},
        {"type": "custom_tool_call", "name": "apply_patch", "input": "*** synthetic patch"},
        {"type": "function_call_output", "output": "out text"},
        {"type": "custom_tool_call_output", "output": [{"type": "input_text", "text": "listed out"},
                                                       {"type": "input_image", "image_url": "data:x"}]},
        {"type": "local_shell_call", "action": {"command": ["ls"]}},
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev only"}]},
        {"type": "message", "role": "user", "content": [context]},
        {"type": "additional_tools", "role": "developer", "tools": []},
    ]
    assert render_page({"turn": 2, "messages": items}).split("\n") == [
        "[turn 2] user:", "asked",
        "[turn 2] assistant:", "said", "also said",
        "[turn 2] user:", "plain string prompt",
        "[turn 2] assistant:", 'tool_use exec {"a": "x", "z": 1}',
        "[turn 2] assistant:", 'tool_use exec "not json"',
        "[turn 2] assistant:", 'tool_use apply_patch "*** synthetic patch"',
        "[turn 2] user:", "tool_result:", "out text",
        "[turn 2] user:", "tool_result:", "listed out",
    ]


def body_lines(text):
    """A rendered page's lines without its "[turn N] role:" headers."""
    return [line for line in text.split("\n") if not (line.startswith("[turn ") and line.endswith(":"))]


def test_page_lines_and_in_view_lines_are_identical():
    """The cross-check B20 exists for: a block found in view (A14 snapshot) and the same block found in
    a page read the same, so search shows one line either way."""
    items = codex_turn()
    in_view = "\n".join(e["text"] for e in retained_entries(items, 0, RESPONSES)).split("\n")
    assert body_lines(render_page({"turn": 1, "messages": items})) == in_view
    anthropic = [m for m in ANTHROPIC_PAGE["messages"][1:3]]  # text, tool_use, and tool_results, no images
    anthropic[1] = {"role": "user", "content": anthropic[1]["content"][:1]}
    in_view = "\n".join(e["text"] for e in retained_entries(anthropic, 0, ANTHROPIC)).split("\n")
    assert body_lines(render_page({"turn": 1, "messages": anthropic})) == in_view


def test_recall_and_search_find_a_planted_line_in_a_codex_page():
    store = MemoryStore(key=b"k" * 32)
    planted = "synthetic planted marker 6173"
    [page_id] = store.page_out([codex_turn(planted)], 0)
    page = store.get_page(page_id)
    text = render_page(page)
    assert "opaque-synthetic" not in text and "[input_text omitted]" not in text
    shown, total = match_lines(text, planted)
    assert total == 1
    start, end, is_match, line = next(s for s in shown if s[2])
    assert line == planted and text[start:end] == planted
    chunk = recall_chunk(page, offset=start, limit=len(planted))
    assert chunk["text"] == planted and chunk["offset"] == start
