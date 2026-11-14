"""The model is told to pin a user's standing rules at once, and contexto_pin says what it keeps (D51).

QA (verify-obligations): a "whenever X, do Y" rule placed after other content was lost once its turn paged
out, because the model pinned 1 of 16 such rules; the MCP instructions are all it sees before the first
rotation. contexto_pin's description named a kind "fact" the tool rejects, and promised the pinned state
for records that are never shown there.
"""

from __future__ import annotations

import io
import json
import re

import pytest

from contexto import mcp
from contexto.store.memory import MemoryStore
from contexto.store.recall import RULE_LINES

PIN = next(t for t in mcp.TOOLS if t["name"] == "contexto_pin")


@pytest.mark.parametrize("text", [mcp.INSTRUCTIONS, RULE_LINES[2]], ids=["instructions", "preamble"])
def test_both_texts_say_to_pin_a_standing_rule_at_once_even_for_a_one_word_reply(text):
    for needed in ("standing rule", "preference", "whenever", "from now on", "contexto_pin", "obligation",
                   "one word", "keep it open until the user withdraws it", "paged"):
        assert needed in text, needed


def test_the_instructions_give_the_pin_arguments_and_stay_short():
    # Claude Code defers the tool, so its schema is not loaded when the rule is stated (QA live-fidelity 5).
    assert '{"kind": "obligation", "text": ...}' in mcp.INSTRUCTIONS
    # Both texts are sent on every request: by the estimator the D51 sentence adds 90 tokens to the
    # instructions (122 to 212) and 79 to the pin line (40 to 119); D69's clause that only the user's own
    # words are pinned (review B, finding 8) adds 38 (212 to 250) and 28 (119 to 147).
    assert len(mcp.INSTRUCTIONS) <= 880 and len(RULE_LINES[2]) <= 520


def test_the_pin_description_names_only_kinds_the_tool_accepts():
    words = set(re.findall(r"[a-z]+", PIN["description"].lower()))
    assert "fact" not in words
    assert set(mcp.RECORD_KINDS) <= words
    assert PIN["inputSchema"]["properties"]["kind"]["enum"] == list(mcp.RECORD_KINDS)


def pin(store, **arguments):
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_pin", "arguments": arguments}})
    return server.handle_line(line)["result"]


@pytest.mark.parametrize("kind", ["event", "artifact", "reference"])
def test_the_pinned_state_shows_what_the_description_says(kind):
    store = MemoryStore()
    assert "only when verified (epistemic verified, with a provenance source)" in PIN["description"]
    assert "obligations and active decisions stay in the pinned state" in PIN["description"]
    assert pin(store, kind="obligation", text="whenever you give a date, use ISO 8601")["isError"] is False
    assert pin(store, kind="decision", text="the codename is Obsidian")["isError"] is False
    assert pin(store, kind=kind, text=f"reported {kind} note")["isError"] is False
    assert pin(store, kind=kind, text=f"verified {kind} note", epistemic="verified",
               provenance=[{"type": "url", "locator": "https://example.com/note"}])["isError"] is False
    shown = store.pinned_text(6000)
    assert "whenever you give a date, use ISO 8601" in shown and "the codename is Obsidian" in shown
    assert f"verified {kind} note" in shown and f"reported {kind} note" not in shown
    assert pin(store, kind="fact", text="x")["isError"] is True
