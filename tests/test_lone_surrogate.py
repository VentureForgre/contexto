"""A lone UTF-16 surrogate (the JSON escape \\ud83d with no low half) must not switch Contexto off.

Real Claude Code (2.1.302 to 2.1.318) replaces lone surrogates with U+FFFD before it sends a request, and the
Messages API answers 400 to a body that holds one, so neither path is reachable from a well-behaved CLI. These
tests pin the robustness contract anyway: the engine and the MCP server must treat such a string as data.
"""

from __future__ import annotations

import io
import json

import pytest

from contexto import mcp
from contexto.rewrite import Config, RewriteEngine
from contexto.store.memory import MemoryStore

LONE = "\ud83d"
FILLER = "word " * 4_000  # about 20 KB per turn


def conversation(turns: int, poisoned_turn: int | None) -> dict:
    messages = []
    for i in range(turns):
        text = f"turn {i} " + FILLER
        if i == poisoned_turn:
            text += f" cut emoji {LONE}"
        messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"reply {i}"}]})
    messages.append({"role": "user", "content": [{"type": "text", "text": "next"}]})
    return {"model": "claude-opus-5-5", "max_tokens": 100, "stream": True, "messages": messages}


def wire_size(request: dict) -> int:
    return len(json.dumps(request, ensure_ascii=False).encode("utf-8", "backslashreplace"))


@pytest.mark.parametrize("poisoned_turn", [None, 0, 3, 39])
def test_a_lone_surrogate_in_the_history_does_not_stop_the_bound(poisoned_turn):
    request = conversation(40, poisoned_turn)
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config(bound=100_000))
    out, _headers, _events = engine(json.loads(json.dumps(request)), {}, "/v1/messages")  # raises today
    assert wire_size(out) < wire_size(request) // 2  # old turns were paged, so the request is bounded


@pytest.mark.parametrize("line", [
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
     "params": {"name": "contexto_recall", "arguments": {"page_id": "pg_" + LONE}}},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope" + LONE, "arguments": {}}},
    {"jsonrpc": "2.0", "id": 1, "method": "x" + LONE},
    {"jsonrpc": "2.0", "id": "\udc00", "method": "ping"},
])
def test_a_reply_echoing_a_lone_surrogate_does_not_kill_the_server(line):
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)  # strict, like the real sys.stdout
    server = mcp.Server(MemoryStore("s1"), stdout, io.StringIO())
    ping = '{"jsonrpc":"2.0","id":2,"method":"ping"}'
    assert server.serve([json.dumps(line) + "\n", ping + "\n"]) == 0  # raises UnicodeEncodeError today
    replies = [json.loads(r) for r in raw.getvalue().decode("utf-8").splitlines()]
    assert len(replies) == 2
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
