"""The class-A experiment tool's parts (synthetic fixtures; no session content is committed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import classa, run, script  # noqa: E402


def _sse(events):
    return [f"data: {json.dumps(e)}\n".encode() for e in events]


def test_parse_message_keeps_every_block_for_the_continuation():
    lines = _sse([
        {"type": "message_start", "message": {"usage": {"input_tokens": 100, "cache_read_input_tokens": 50}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1",
                                                                      "name": "mcp__contexto__contexto_search", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"query": '}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"x"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
    ])
    m = classa.parse_message(lines)
    assert m["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    assert m["content"][1] == {"type": "tool_use", "id": "toolu_1", "name": "mcp__contexto__contexto_search",
                               "input": {"query": "x"}}
    assert m["stop_reason"] == "tool_use" and classa._usage_tokens(m["usage"]) == 157


class FakeServer:
    def __init__(self):
        self.calls = []

    def _tools_call(self, params):
        self.calls.append(params)
        return {"content": [{"type": "text", "text": "hit"}], "isError": False}


def test_contexto_calls_go_to_main_mcp_and_other_tools_are_refused():
    server = FakeServer()
    content = [{"type": "text", "text": "looking"},
               {"type": "tool_use", "id": "a", "name": "mcp__contexto__contexto_search", "input": {"query": "q"}},
               {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "ls"}}]
    results, calls = classa.answer_tools(server, content)
    assert server.calls == [{"name": "contexto_search", "arguments": {"query": "q"}}]
    assert [r["tool_use_id"] for r in results] == ["a", "b"] and results[1]["is_error"] and "is_error" not in results[0]
    assert calls == [{"tool": "contexto_search", "ok": True}, {"tool": "Bash", "ok": False, "unavailable": True}]
    assert classa.answer_tools(server, [{"type": "text", "text": "done"}]) == ([], [])


def _bodies(probe):
    old = {"name": "mcp__contexto__contexto_search", "description": "old", "input_schema": {"type": "object"}}
    other = {"name": "Bash", "description": "run", "input_schema": {"type": "object"}}
    first = {"messages": [{"role": "user", "content": "turn 1"}], "tools": [other, old], "_betas": []}
    last = {"messages": [{"role": "user", "content": "turn 1"}, {"role": "user", "content": [{"type": "text", "text": probe}]}],
            "tools": [other, old], "_betas": []}
    return [first, last]


def test_experiment_bodies_change_only_the_contexto_tools_and_the_probe_turn():
    new = {"name": "mcp__contexto__contexto_search", "description": "new format", "input_schema": {"type": "object"}}
    bodies = _bodies("all nine probes")
    exp, changes = classa.experiment_bodies(bodies, "all nine probes", "four targets", [new])
    assert bodies[1]["messages"][1]["content"][0]["text"] == "all nine probes"  # the input is not changed
    assert exp[1]["messages"][1]["content"][0]["text"] == "four targets" and exp[0]["messages"] == bodies[0]["messages"]
    assert all(b["tools"][1] == new and b["tools"][0]["name"] == "Bash" for b in exp)
    assert set(changes["tools_changed"]) == {"mcp__contexto__contexto_search"}
    with pytest.raises(classa.ExperimentError):
        classa.experiment_bodies(bodies, "not there", "x", [new])


def test_targets_text_asks_the_four_targets_under_the_same_header():
    s, _ = run.plan()
    text = classa.targets_text(s)
    lines = text.split("\n")
    assert lines[:len(script.PROBE_HEADER)] == list(script.PROBE_HEADER)
    asked = [ln.split(":")[0] for ln in lines[len(script.PROBE_HEADER):] if ln.startswith("q_")]
    assert asked == list(classa.TARGETS)


def test_score_targets_tells_the_asked_link_from_an_older_one():
    s, _ = run.plan()
    by_id = {p.id: p for p in s.probes}
    answer = "\n".join([f"q_a_016: {by_id['q_a_016'].answer}", f"q_a_012: {by_id['q_a_012'].older[0]}",
                        "q_a_035: something else"])
    out = classa.score_targets(s, answer)
    assert out["correct"] == 1 and out["targets"]["q_a_016"]["correct"]
    assert out["targets"]["q_a_012"]["older_link"] and not out["targets"]["q_a_012"]["correct"]
    assert not out["targets"]["q_a_024"]["answered"]


def test_main_refuses_over_the_ceiling_without_an_upstream_and_live_without_a14(tmp_path, capsys):
    base = ["--run-dir", str(tmp_path), "--session", "s", "--run-code", str(tmp_path), "--out", str(tmp_path / "o"),
            "--seat-settings", str(tmp_path / "seat.json")]
    assert classa.main([*base, "--stub", "--approved", "600001"]) == 2
    assert classa.main(base) == 2  # neither --stub nor --upstream
    from contexto.store import SessionStore
    if not hasattr(SessionStore, "set_retained"):  # until B19 (b) and A14 are on main
        assert classa.main([*base, "--upstream", "http://127.0.0.1:9"]) == 2
        assert "set_retained" in capsys.readouterr().err
