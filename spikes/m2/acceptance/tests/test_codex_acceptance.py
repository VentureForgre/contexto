"""M2 A16: the Codex pieces of the acceptance harness (driver, stub, estimate, dry run). Synthetic only;
the dry-run test runs the real codex binary against the 127.0.0.1 stub and is skipped without it."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import codex_budget, codex_driver, codex_dryrun, script  # noqa: E402
from acceptance.codex_stub import reply_items  # noqa: E402

PROBE = script.PROBE_HEADER[0] + "\nq_a_001: first?\nq_p_01: second?"


def test_turn_command_starts_then_resumes_by_thread_id(tmp_path):
    first = codex_driver.turn_command("contexto", "S", 64_000, "http://u", "/h", tmp_path / "l.txt")
    assert first[:4] == ["contexto", "run", "--bound", "64000"] and first[-1] == "-"
    assert "resume" not in first and "--json" in first and f"model={codex_driver.MODEL}" in first
    assert first[first.index("-o") + 1] == str(tmp_path / "l.txt")
    later = codex_driver.turn_command("contexto", "S", 64_000, "http://u", "/h", tmp_path / "l.txt", thread="T1",
                                      codex_upstream="http://u/v1")
    assert later[-3:] == ["resume", "T1", "-"]  # never 'resume --last -': '-' would be read as a session id
    assert later[later.index("--codex-upstream") + 1] == "http://u/v1"


def test_child_env_keeps_no_credential_or_seat_variables(tmp_path):
    base = {"PATH": "/bin", "TERM": "xterm", "OPENAI_API_KEY": "synthetic", "ANTHROPIC_BASE_URL": "x",
            "OPENRIG_SESSION": "x", "CLAUDE_CODE_ENTRYPOINT": "x", "CODEX_HOME": "/x", "CONTEXTO_HOME": "/y",
            "HOME": "/real/home"}
    env, store = codex_driver.child_env(tmp_path / "store", tmp_path / "home", base=base)
    assert env == {"PATH": "/bin", "TERM": "xterm", "HOME": str(tmp_path / "home")}  # before PR #148
    assert store == tmp_path / "home" / ".contexto"
    env, store = codex_driver.child_env(tmp_path / "store", tmp_path / "home", mcp_home_workaround=False, base=base)
    assert env["CONTEXTO_HOME"] == str(tmp_path / "store") and store == tmp_path / "store"
    assert env["HOME"] != "/real/home"


def _entry(path="/responses", inp=100, cached=0, out=5, status=200, rotation=None, usage_events=()):
    tevents = [{"event": "rotation", "reason": rotation}] if rotation else []
    return {"event": "request", "method": "POST", "path": path, "status": status, "transform_events": tevents,
            "usage": {"input_tokens": inp, "input_tokens_details": {"cached_tokens": cached}, "output_tokens": out},
            "usage_events": list(usage_events)}


def test_spend_counts_responses_input_including_cached_plus_output():
    events = [_entry(inp=1_000, cached=600, out=10), _entry(path="/v1/responses", inp=500, out=5),
              _entry(path="/v1/messages", inp=9_999), {"event": "request", "method": "GET", "path": "/models"}]
    assert codex_driver.spend(events) == 1_515  # cached is inside input_tokens; other paths do not count


def test_analyze_reads_the_responses_dialect_log():
    events = [_entry(inp=900), _entry(inp=950, cached=800), _entry(inp=600, rotation="bound"),
              _entry(inp=700, cached=0),
              _entry(inp=70_000, usage_events=[{"event": "over_bound", "source": "real"}]),
              _entry(status=401), {"event": "mcp_call", "tool": "contexto_search", "ok": True},
              {"event": "mcp_call", "tool": "contexto_recall", "ok": False}, {"event": "turn_room_low"}]
    a = codex_driver.analyze(events, 64_000)
    assert a["requests"] == 6 and a["model_4xx"] == 1 and a["over_bound_real"] == 1 and a["max_input"] == 70_000
    assert a["rotations"] == {"bound": 1} and a["failure_events"] == ["turn_room_low"]
    assert a["cache_hit_rate"] == round(1 / 3, 3)  # eligible: 2nd, 4th, 5th (not the first, the rotation, the 401)
    assert a["mcp_calls"] == {"contexto_recall": 1, "contexto_search": 1} and a["mcp_failed"] == 1
    assert a["acceptance_1"] is False


class FakeCodex:
    """Stands in for subprocess.run: records each turn and writes the last message."""

    def __init__(self, exit_at=None):
        self.calls, self.exit_at = [], exit_at

    def __call__(self, cmd, input, cwd, env, **kwargs):
        self.calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_text("q_a_001: x" if script.PROBE_HEADER[0] in input else "OK")
        code = 7 if self.exit_at == len(self.calls) else 0
        out = json.dumps({"type": "thread.started", "thread_id": "T1"}) if len(self.calls) == 1 else ""
        return SimpleNamespace(returncode=code, stdout=out)


def _turns(tmp_path, runner):
    return codex_driver.CodexTurns("contexto", "S", 64_000, "http://u", "/h", tmp_path, {}, runner=runner)


def test_run_session_resumes_the_thread_and_keeps_only_probe_answers(tmp_path):
    s = script.build(turns=4, turn_bytes=2_000)
    runner = FakeCodex()
    record = codex_driver.run_session(s, _turns(tmp_path, runner), tmp_path / "log.jsonl", 10**9, [1] * len(s.turns))
    assert record["turns_completed"] == len(s.turns) and record["stopped"] is None and record["thread"]
    assert "resume" not in runner.calls[0] and all(c[-3:] == ["resume", "T1", "-"] for c in runner.calls[1:])
    assert record["answers"].count("q_a_001: x") == len(s.probe_turn_ids)


def test_run_session_never_starts_a_turn_that_could_pass_the_cap(tmp_path):
    s = script.build(turns=4, turn_bytes=2_000)
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(_entry(inp=900, out=100)) + "\n")  # 1,000 spent already
    runner = FakeCodex()
    # 1,000 spent + 401 + 100 passes 1,500 (exactly 1,500 would be allowed).
    record = codex_driver.run_session(s, _turns(tmp_path, runner), log, 1_500, [401] * len(s.turns), reserve=100)
    assert record["turns_completed"] == 0 and "could pass the approved 1,500" in record["stopped"]
    assert runner.calls == []
    with pytest.raises(ValueError):
        codex_driver.run_session(s, _turns(tmp_path, runner), log, 10**9, [1])


def test_run_session_stops_when_codex_fails(tmp_path):
    s = script.build(turns=4, turn_bytes=2_000)
    record = codex_driver.run_session(s, _turns(tmp_path, FakeCodex(exit_at=2)), tmp_path / "log.jsonl", 10**9,
                                      [1] * len(s.turns))
    assert record["turns_completed"] == 2 and record["stopped"] == "turn 2: codex exited 7"


def test_stub_replies_ok_then_calls_the_tools_then_answers():
    lead = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "x"}]}]
    content = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "log\nReply with exactly: OK"}]}]
    assert reply_items(lead + content, {}, "q")[0] == "ok"
    index = "pg_0123456789abcdef turn 1: synthetic\nlog token pg_fedcba9876543210 is not an index line"
    probe = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": index},
                                                             {"type": "input_text", "text": PROBE}]}]
    kind, [call] = reply_items(lead + probe, {}, "retention_days")
    assert kind == "recall_call" and call["type"] == "custom_tool_call" and call["name"] == "exec"
    assert '"retention_days"' in call["input"] and '"pg_0123456789abcdef"' in call["input"]
    assert "fedcba9876543210" not in call["input"]  # only an index line's id, never a lookalike
    output = [call, {"type": "custom_tool_call_output", "call_id": call["call_id"], "output": "recall: 10 chars"}]
    kind, [message] = reply_items(lead + probe + output, {"q_a_001": "alpha"}, "q")
    assert kind == "answers" and message["content"][0]["text"] == "q_a_001: alpha\nq_p_01: unknown"


def test_estimate_is_the_stated_formula_and_checks_the_baseline_fits():
    s = codex_dryrun.build(script.DEFAULT_SEED, 64_000, 20_000)
    b = codex_budget.estimate(s, 64_000)
    per_turn = 1 + b.retries_per_turn
    assert b.h1_requests == s.n_content * per_turn + len(s.probe_turn_ids) * (1 + b.tool_rounds + b.retries_per_turn)
    assert b.h1_worst == b.h1_requests * (64_000 + codex_budget.OUTPUT_ALLOWANCE)
    assert b.total_worst == math.ceil((b.h1_worst + b.baseline_worst) * (1 + b.margin))
    assert b.baseline_fits and b.baseline_max_input <= codex_budget.CODEX_COMPACT_AT
    wide = codex_budget.estimate(codex_dryrun.build(script.DEFAULT_SEED, 80_000, 40_000), 80_000)
    assert not wide.baseline_fits  # 80,000 with 40 KB turns does not fit a full-context Codex baseline
    assert "x 1.10" in codex_budget.formula(b) and str(f"{b.total_worst:,}") in codex_budget.formula(b)


@pytest.mark.skipif(shutil.which("codex") is None, reason="needs the codex CLI (zero spend: local stub only)")
def test_dry_run_end_to_end_against_the_local_stub(tmp_path):
    contexto = shutil.which("contexto") or str(Path(sys.executable).parent / "contexto")
    if not Path(contexto).exists():
        pytest.skip("needs the contexto executable")
    assert codex_dryrun.main([contexto, str(tmp_path), "--bound", "64000", "--turn-kbytes", "20"]) == 0
    d = json.loads((tmp_path / "codex-dry-run.json").read_text())
    a = d["analysis"]
    assert d["ran"]["turns_completed"] == d["ran"]["turns"] and a["acceptance_1"] and a["rotations"]
    assert a["mcp_calls"] == {"contexto_recall": d["turns"]["probe"], "contexto_search": d["turns"]["probe"]}
    assert a["mcp_failed"] == 0 and d["score"]["correct"] == d["score"]["total"] == 54
    assert subprocess.run(["git", "-C", str(tmp_path), "rev-parse"], capture_output=True).returncode != 0
