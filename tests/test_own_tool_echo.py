"""Contexto's own tool calls are kept verbatim but not indexed (D51).

Once a turn that called ``contexto_search`` pages out, its page holds copies of lines from older pages.
Indexed, that page outranked the source pages it quoted and was shown first, newest first, so a stale
value led the reply (QA verify-obligations finding 2). The calls and results of Contexto's own tools are
left out of search, the stub, the index line, and the in-view search, and kept in the page for recall.
"""

from __future__ import annotations

import io
import json
import random
import re

import pytest

from contexto import mcp
from contexto.codex import RESPONSES
from contexto.rewrite import ANTHROPIC, retained_entries
from contexto.store.memory import MemoryStore
from contexto.store.pages import build_stub, index_line, searchable_text
from contexto.store.render import own_call_ids, render_page

SEARCH = "mcp__contexto__contexto_search"


def blocks(store, name, arguments):
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return server.handle_line(line)["result"]["content"]


def call(store, name, arguments):
    return blocks(store, name, arguments)[0]["text"]  # a search reply's coverage note is a block apart


def chat(user, reply):
    return [{"role": "user", "content": user}, {"role": "assistant", "content": [{"type": "text", "text": reply}]}]


def probe(store, n, question, query, answer):
    """A turn in which the model searched: the tool_result is the real reply over the pages so far."""
    tid = f"toolu_{n:024d}"
    return [{"role": "user", "content": question},
            {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": SEARCH,
                                               "input": {"query": query}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                          "content": blocks(store, "contexto_search", {"query": query})}]},
            {"role": "assistant", "content": [{"type": "text", "text": answer}]}]


def echo_session(probes: int = 3) -> tuple[MemoryStore, dict[str, int]]:
    """The QA repro (echo_repro.py): a codename changed twice, with probe turns that searched for it."""
    rng = random.Random(5)

    def filler(n=3000):
        return "\n".join(f"2026-10-{rng.randint(10, 28)}T0{rng.randint(0, 9)}:00:00Z worker-{rng.randint(1, 30)} "
                         f"job {rng.randint(1000, 9999)} done in {rng.randint(1, 900)} ms" for _ in range(n // 50))

    store = MemoryStore()
    turns: dict[str, int] = {}
    turn = [0]

    def page(t, name=None):
        if name:
            turns[name] = turn[0]
        store.page_out([t], turn[0])
        turn[0] += 1

    page(chat("Project notes:\n" + filler() + "\nOur release codename for the next release is Juniper.\n"
              + filler(1500), "OK"), "juniper")
    page(chat("More log:\n" + filler(), "OK"))
    page(chat("Update: the release codename changed from Juniper to Tamarind.\n" + filler(), "OK"), "tamarind")
    page(chat("More log:\n" + filler(), "OK"))
    page(probe(store, 1, "What is the release codename?", "release codename", "The release codename is Tamarind."),
         "probe1")
    page(chat("More log:\n" + filler(), "OK"))
    page(chat("Update: the release codename changed again, from Tamarind to Obsidian.\n" + filler(), "OK"),
         "obsidian")
    for _ in range(3):
        page(chat("More log:\n" + filler(), "OK"))
    page(probe(store, 2, "Remind me of the earliest release codename we had?", "release codename Juniper",
               "The first codename was Juniper."), "probe2")
    for _ in range(2):
        page(chat("More log:\n" + filler(), "OK"))
    if probes == 3:
        page(probe(store, 3, "Was Tamarind ever the codename?", "Tamarind",
                   "Yes, Tamarind replaced Juniper before Obsidian."), "probe3")
    page(chat("More log:\n" + filler(), "OK"))
    return store, turns


def detailed_turns(reply: str) -> list[int]:
    best = reply.split(mcp.SEARCH_BEST_HEADER, 1)[1].split("[more matching pages", 1)[0]
    return [int(line.split("(turn ", 1)[1].rstrip(")")) for line in best.splitlines() if line.startswith("pg_")]


@pytest.mark.parametrize("probes", [2, 3])
def test_a_paged_search_reply_does_not_outrank_the_pages_it_quotes(probes):
    store, turns = echo_session(probes)
    reply = call(store, "contexto_search", {"query": "release codename"})
    shown = detailed_turns(reply)
    # The newest statement of the value is among the best pages and its value is the first one shown
    # (before D51, with 3 probes it fell to the offsets-only list, and with 2 the first value was Juniper).
    assert turns["obsidian"] in shown, reply
    first = next(v for line in reply.splitlines() for v in ("Obsidian", "Tamarind", "Juniper") if v in line)
    assert first == "Obsidian", reply
    # A probe turn is found by its own question and answer only: no line of an earlier search reply is
    # shown, nested offsets ("> 519: > 488: ...") never appear, and its trailer never reads as page text.
    paged = reply.split(mcp.SEARCH_BEST_HEADER, 1)[1]
    assert not re.search(r"(?m)^[> ] \d+: (>|\s*\d+:|pg_)", paged), reply
    assert mcp.SEARCH_COVERAGE[:40] not in paged and "tool_result" not in paged and SEARCH not in paged


def test_a_search_turn_is_still_found_by_its_own_words_and_recalled_verbatim():
    store, turns = echo_session(3)
    reply = call(store, "contexto_search", {"query": "earliest release codename"})
    assert detailed_turns(reply) == [turns["probe2"]]
    assert "Remind me of the earliest release codename we had?" in reply
    [page_id] = [p for p in store.search("earliest release codename")]
    page = store.get_page(page_id)
    recalled = call(store, "contexto_recall", {"page_id": page_id})
    assert "tool_result:" in recalled and "Juniper" in recalled  # the echoed reply is kept for recall
    assert recalled.startswith(render_page(page)[:200])


def test_the_echoed_reply_is_left_out_of_search_text_stub_and_index_line():
    store, turns = echo_session(3)
    page = next(p for p in store.pages.values() if p["turn"] == turns["probe2"])
    text = searchable_text(page)  # folded
    assert "remind me of the earliest release codename" in text
    assert "tamarind" not in text and "paged hits come only" not in text
    line = index_line(page, salient=True)
    assert "Obsidian" not in line and "pg_" not in line.split(":", 1)[1]
    assert page["stub"]["tools"] == [SEARCH]  # the tool used is still listed


def test_rendered_searchable_text_blanks_own_calls_at_the_same_offsets():
    store, turns = echo_session(3)
    page = next(p for p in store.pages.values() if p["turn"] == turns["probe1"])
    verbatim, masked = render_page(page), render_page(page, searchable=True)
    assert len(verbatim) == len(masked) and verbatim.count("\n") == masked.count("\n")
    assert "Juniper" in verbatim and "Juniper" not in masked
    assert "What is the release codename?" in masked and "The release codename is Tamarind." in masked


# Every shape of call: Claude Code by name, Codex by name or from its code-mode exec tool.

OUTPUT = "pg_aaaaaaaaaaaaaaaa (turn 3)\n> 10: ECHOED-MARKER src/app/config.py"
ASK = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "what was it"}]}]


def codex_turn(kind, name, arguments):
    if kind == "function_call":
        called = {"type": "function_call", "name": name, "call_id": "c1", "arguments": json.dumps(arguments)}
        out = {"type": "function_call_output", "call_id": "c1", "output": OUTPUT}
    else:
        called = {"type": "custom_tool_call", "name": name, "call_id": "c1", "input": arguments}
        out = {"type": "custom_tool_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": OUTPUT}]}
    return [*ASK, called, out]


OWN = {
    "claude": [{"role": "user", "content": "what was it"},
               {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": SEARCH,
                                                  "input": {"query": "ECHOED-QUERY"}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": OUTPUT}]}],
    "codex_function": codex_turn("function_call", SEARCH, {"query": "ECHOED-QUERY"}),
    "codex_exec": codex_turn("custom_tool_call", "exec",
                             'const r = await tools["mcp__contexto__contexto_search"]({query: "ECHOED-QUERY"});\n'
                             "return r;"),
    "codex_exec_dot": codex_turn("custom_tool_call", "exec",
                                 'return await tools.mcp__contexto__contexto_recall({page_id: "ECHOED-QUERY"});'),
}
OTHER = {
    "claude_other_tool": [{"role": "user", "content": "what was it"},
                          {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                             "input": {"command": "ECHOED-QUERY"}}]},
                          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                        "content": OUTPUT}]}],
    "another_server": codex_turn("function_call", "mcp__contexto_x__contexto_search", {"query": "ECHOED-QUERY"}),
    "exec_mixed": codex_turn("custom_tool_call", "exec",
                             'await tools["mcp__contexto__contexto_search"]({query: "x"});\n'
                             'return await tools.exec_command({cmd: "ECHOED-QUERY"});'),
    "exec_no_tool": codex_turn("custom_tool_call", "exec", 'return "ECHOED-QUERY";'),
}


@pytest.mark.parametrize("shape", sorted(OWN))
def test_own_calls_are_not_indexed_in_either_dialect(shape):
    store = MemoryStore()
    [page_id] = store.page_out([OWN[shape]], 0)
    page = store.get_page(page_id)
    assert own_call_ids(OWN[shape])
    assert store.search("ECHOED-MARKER") == [] and store.search("ECHOED-QUERY") == []
    assert store.search("what was it") == [page_id]
    assert "src/app/config.py" not in build_stub(OWN[shape])["paths"]
    assert "ECHOED-MARKER" in render_page(page) and "ECHOED-MARKER" not in render_page(page, searchable=True)


@pytest.mark.parametrize("shape", sorted(OTHER))
def test_other_tools_and_mixed_exec_calls_stay_indexed(shape):
    store = MemoryStore()
    [page_id] = store.page_out([OTHER[shape]], 0)
    assert own_call_ids(OTHER[shape]) == set()
    assert store.search("ECHOED-MARKER") == [page_id] and store.search("ECHOED-QUERY") == [page_id]


def test_in_view_search_skips_own_calls():
    for dialect, turn in ((ANTHROPIC, OWN["claude"]), (RESPONSES, OWN["codex_exec"])):
        texts = [e["text"] for e in retained_entries(turn, 0, dialect)]
        assert any("what was it" in t for t in texts)
        assert not any("ECHOED" in t for t in texts), (dialect.name, texts)
    kept = [e["text"] for e in retained_entries(OTHER["claude_other_tool"], 0, ANTHROPIC)]
    assert any("ECHOED-MARKER" in t for t in kept)
