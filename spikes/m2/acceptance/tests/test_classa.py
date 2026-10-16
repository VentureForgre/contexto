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


def test_main_refuses_over_the_ceiling_and_without_an_upstream(tmp_path):
    base = ["--run-dir", str(tmp_path), "--session", "s", "--run-code", str(tmp_path), "--out", str(tmp_path / "o"),
            "--seat-settings", str(tmp_path / "seat.json")]
    assert classa.main([*base, "--stub", "--approved", "600001"]) == 2  # the default ceiling
    assert classa.main([*base, "--stub", "--ceiling", "740000", "--approved", "740001"]) == 2
    assert classa.main([*base, "--stub", "--ceiling", "7400000", "--approved", "1000"]) == 2  # over 1,000,000
    assert classa.CEILING_MAX == 1_000_000
    assert classa.main(base) == 2  # neither --stub nor --upstream


def test_identical_reminders_are_marked_only_where_the_record_says():
    # The per-turn reminder decodes to one shared message object; a marker on the newest copy must not
    # mark the earlier ones (that bug added 62 bytes per request to the rebuilt bodies).
    from acceptance import replay

    reminder = {"role": "system", "content": [{"type": "text", "text": "reminder"}]}
    u1, a1 = {"role": "user", "content": [{"type": "text", "text": "turn 1"}]}, {"role": "assistant", "content": [{"type": "text", "text": "OK"}]}
    u2 = {"role": "user", "content": [{"type": "text", "text": "PROBE"}]}
    blobs = [{"type": "api-request-blob", "hash": replay.message_hash(m), "message": m} for m in (reminder, u1, a1, u2)]
    h = [replay.message_hash(m) for m in (u1, reminder, a1, u2, reminder)]
    params = {"model": "m", "max_tokens": 10, "betas": []}
    cc = [{"type": "api-request-shape", "shapeHash": "s", "shape": {"system": [], "tools": []}}, *blobs,
          {"type": "api-request", "id": "r1", "shapeHash": "s", "params": params, "keep": 0, "tail": h[:2],
           "cacheControl": [{"path": ["messages", 1, "content", 0], "value": {"type": "ephemeral"}}]},
          {"type": "api-request", "id": "r2", "shapeHash": "s", "params": params, "base": "r1", "keep": 2, "tail": h[2:],
           "cacheControl": [{"path": ["messages", 4, "content", 0], "value": {"type": "ephemeral"}}]}]
    bodies = classa.original_bodies(cc, "PROBE")
    msgs = bodies[1]["messages"]
    assert msgs[1]["content"] == "reminder"  # unmarked system message: the wire's string form
    assert msgs[4]["content"] == [{"type": "text", "text": "reminder", "cache_control": {"type": "ephemeral"}}]


def test_tool_results_logged_one_per_record_decode_as_the_one_message_sent():
    # Claude Code writes each tool result as its own user record but sends them as one user message;
    # the probe turns behind a tool loop decode only with the merged message (live run #2, request 20).
    from acceptance import replay

    u1 = {"role": "user", "content": [{"type": "text", "text": "PROBE 1"}]}
    a1 = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}},
                                           {"type": "tool_use", "id": "t2", "name": "x", "input": {}}]}
    r1 = {"type": "tool_result", "tool_use_id": "t1", "content": "one"}
    r2 = {"type": "tool_result", "tool_use_id": "t2", "content": "two"}
    merged = {"role": "user", "content": [r1, r2]}
    params = {"model": "m", "max_tokens": 10, "betas": []}
    cc = [{"type": "api-request-shape", "shapeHash": "s", "shape": {"system": [], "tools": []}},
          {"type": "api-request-blob", "hash": replay.message_hash(u1), "message": u1},
          {"type": "api-request-blob", "hash": replay.message_hash(a1), "message": a1},
          {"type": "user", "message": {"role": "user", "content": [r1]}},
          {"type": "user", "message": {"role": "user", "content": [r2]}},
          {"type": "api-request", "id": "q1", "shapeHash": "s", "params": params, "keep": 0, "tail": [replay.message_hash(u1)]},
          {"type": "api-request", "id": "q2", "shapeHash": "s", "params": params, "base": "q1", "keep": 1,
           "tail": [replay.message_hash(a1), replay.message_hash(merged)]}]
    with pytest.raises(replay.ReplayError):
        replay.recorded_messages(cc)  # one record per result: the merged hash is unknown
    decoded = classa.decode_requests(cc, stop=lambda msgs: False)
    assert decoded["q2"][2] == merged


def test_out_inside_a_git_work_tree_is_refused(tmp_path):
    # Reviewer-2 on #133: experiment output never lands in a repo, even a not-yet-created subdirectory.
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    outside = tmp_path / "outside"
    assert classa.out_in_git_tree(repo / "new" / "out") and classa.out_in_git_tree(repo)
    assert not classa.out_in_git_tree(outside / "new") and not classa.out_in_git_tree(tmp_path)
    base = ["--run-dir", str(tmp_path), "--session", "s", "--run-code", str(tmp_path), "--seat-settings",
            str(tmp_path / "seat.json"), "--stub"]
    assert classa.main([*base, "--out", str(repo / "o")]) == 2


def test_out_guard_fails_closed(tmp_path, monkeypatch, capsys):
    # Reviewer-2 on #137: the guard must not fail open on git errors or a misleading environment.
    import subprocess

    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    # GIT_DIR / GIT_WORK_TREE from the caller are ignored: a bogus GIT_DIR neither hides a repo nor
    # makes an outside path look inside.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outside))
    assert classa.out_in_git_tree(repo / "o") and not classa.out_in_git_tree(outside / "o")
    # A ceiling at the repo root (or discovery limits) would make a path inside the repo read as outside:
    # git, started in repo/sub, stops before the ceiling and reports "not a git repository".
    (repo / "sub").mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(repo))
    monkeypatch.setenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", "0")
    assert classa.out_in_git_tree(repo / "sub") and classa.out_in_git_tree(repo / "sub" / "o")
    # The check's own environment: no GIT_* variable at all, and LC_ALL=C (so the message match does
    # not depend on the caller's locale), whatever the caller set.
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    seen = {}
    real_run = subprocess.run

    def spy(cmd, **kw):
        seen.update(kw["env"])
        return real_run(cmd, **kw)

    monkeypatch.setattr(classa.subprocess, "run", spy)
    assert not classa.out_in_git_tree(outside / "o")
    assert seen["LC_ALL"] == "C" and not any(k.startswith("GIT_") for k in seen)
    assert classa.out_in_git_tree(repo / ".git")  # inside the git directory itself
    def dubious(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: detected dubious ownership in repository\n")

    monkeypatch.setattr(classa.subprocess, "run", dubious)
    assert classa.out_in_git_tree(outside / "o")  # any other git error counts as inside

    def no_git(cmd, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr(classa.subprocess, "run", no_git)
    with pytest.raises(classa.ExperimentError):
        classa.out_in_git_tree(outside / "o")
    base = ["--run-dir", str(tmp_path), "--session", "s", "--run-code", str(tmp_path), "--seat-settings",
            str(tmp_path / "seat.json"), "--stub", "--out", str(outside / "o")]
    assert classa.main(base) == 2 and "git is not installed" in capsys.readouterr().err
