"""Offline variant builder on a small synthetic recorded run (no model, no network)."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

import variants
from contexto.canonical import canonical_json

FILL = "synthetic log line " * 120


def write_run(root: Path, turns: int = 8) -> Path:
    """A recorded run in Claude Code's delta format: shape, blobs, transcript records, api-requests."""
    shape = {"system": [{"type": "text", "text": "synthetic head"}],
             "tools": [{"name": "Read", "input_schema": {"type": "object"}},
                       {"name": "mcp__contexto__contexto_search", "input_schema": {"type": "object"}},
                       {"name": "mcp__contexto__contexto_recall", "input_schema": {"type": "object"}}]}
    records = [{"type": "api-request-shape", "shape": shape, "shapeHash": "h"}]
    history, log = [], []
    for t in range(turns):
        user = {"role": "user", "content": [{"type": "text", "text": f"turn {t} {FILL}"}]}
        records.append({"type": "user", "message": user})
        tail = [variants.message_hash(user)]
        if history:  # the previous assistant reply arrives in this request's tail
            tail.insert(0, variants.message_hash(history[-1]))
        keep = len(history) - (1 if history else 0)
        records.append({"type": "api-request", "id": f"r{t}", "base": f"r{t - 1}" if t else None, "keep": keep,
                        "tail": tail, "params": {"model": "claude-haiku-5-5", "max_tokens": 64}})
        history = history + [user]
        reply = {"role": "assistant", "content": [{"type": "text", "text": f"OK {t}"}]}
        records.append({"type": "assistant", "message": reply})
        history = history + [reply]
        log.append({"event": "request", "path": "/v1/messages", "transform_events": [],
                    "usage": {"input_tokens": 1000 * (t + 1), "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0, "output_tokens": 5}})
    session = root / "claude-config" / "projects" / "p"
    session.mkdir(parents=True)
    (session / "s.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    sessions = root / "home" / "sessions" / "x"
    sessions.mkdir(parents=True)
    (sessions / "log.jsonl").write_text("\n".join(json.dumps(e) for e in log))
    return root


def test_rebuild_reconstructs_each_request_from_deltas(tmp_path):
    requests, approximate = variants.rebuild_requests(write_run(tmp_path / "run"))
    assert approximate == 0 and len(requests) == 8
    assert [len(r["messages"]) for r in requests] == [1, 3, 5, 7, 9, 11, 13, 15]
    assert requests[-1]["messages"][-1]["content"][0]["text"].startswith("turn 7")
    assert requests[0]["model"] == "claude-haiku-5-5" and requests[0]["stream"] is True


def test_variants_replay_and_write_probe_requests(tmp_path):
    run = write_run(tmp_path / "run")
    spec = tmp_path / "v.json"
    spec.write_text(json.dumps({"reminded": {"engine": {"recall_rule": "RULE", "turn_reminder": "REMIND"},
                                             "bound": 6_000},
                                "control": {"engine": {"turn_reminder": "REMIND"}, "bound": 6_000,
                                            "probe": "recorded"}}))
    probe = tmp_path / "probe.txt"
    probe.write_text("neutral probe text")
    out = tmp_path / "out"
    assert variants.main([str(run), "--variants", str(spec), "--out", str(out), "--probe-text", str(probe)]) == 0
    summary = json.loads((out / "summary.json").read_text())
    names = [v["variant"] for v in summary["variants"]]
    assert names == ["baseline", "reminded", "control"]
    assert [v["probe_text"] for v in summary["variants"]] == ["substituted", "substituted", "recorded"]
    control = json.loads((out / "control" / "probe_request.json").read_text())
    assert "neutral probe text" not in canonical_json(control) and "turn 7" in canonical_json(control)
    reminded = summary["variants"][1]
    assert reminded["rotations"] >= 1 and reminded["probe"]["contexto_tools_present"] == ["contexto_search", "contexto_recall"]
    assert reminded["probe"]["turn_reminders"] >= 1 and reminded["probe"]["preamble_block"] is not None
    sent = json.loads((out / "reminded" / "probe_request.json").read_text())
    assert "neutral probe text" in canonical_json(sent) and "turn 7" not in canonical_json(sent["messages"][-1])
    assert "synthetic log line" not in json.dumps(summary)  # numbers only


def test_unknown_engine_knob_is_refused(tmp_path):
    run = write_run(tmp_path / "run")
    spec = tmp_path / "v.json"
    spec.write_text(json.dumps({"bad": {"engine": {"bound": 1}}}))
    try:
        variants.main([str(run), "--variants", str(spec), "--out", str(tmp_path / "out")])
    except SystemExit as exc:
        assert "unknown engine knobs" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_strip_thinking_removes_every_thinking_block():
    upstream = {"messages": [
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                                          {"type": "text", "text": "a"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "sig2"}]},
    ]}
    out, count = variants.strip_all_thinking(upstream)
    assert count == 2 and "signature" not in json.dumps(out)
    assert out["messages"][1]["content"] == [{"type": "text", "text": "a"}]
    assert out["messages"][2]["content"] and out["messages"][2]["content"][0]["type"] == "text"  # placeholder


def test_outputs_are_deterministic_and_private(tmp_path):
    run = write_run(tmp_path / "run")
    spec = tmp_path / "v.json"
    spec.write_text(json.dumps({"v": {"engine": {"turn_reminder": "R"}, "bound": 6_000}}))
    outs = []
    for i in range(2):
        out = tmp_path / f"out{i}"
        variants.main([str(run), "--variants", str(spec), "--out", str(out)])
        outs.append((out / "v" / "probe_request.json").read_bytes())
        assert stat.S_IMODE((out / "v" / "probe_request.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((out / "summary.json").stat().st_mode) == 0o600
    assert outs[0] == outs[1]  # fixed page key: byte-identical requests


def test_out_inside_a_repo_must_be_gitignored(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(".contexto/\n")
    variants.check_out_is_ignored(repo / ".contexto" / "out")  # ignored: fine
    variants.check_out_is_ignored(tmp_path / "elsewhere")  # outside any repo: fine
    with pytest.raises(SystemExit, match="not ignored"):
        variants.check_out_is_ignored(repo / "results")


def test_probe_substitution_skips_a_trailing_context_block():
    request = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "old probe"},
        {"type": "text", "text": "<system-reminder>synthetic context</system-reminder>"}]}]}
    out = variants.substitute_probe_text(request, "new probe")
    assert out["messages"][0]["content"] == [{"type": "text", "text": "new probe"},
                                             {"type": "text", "text": "<system-reminder>synthetic context</system-reminder>"}]
