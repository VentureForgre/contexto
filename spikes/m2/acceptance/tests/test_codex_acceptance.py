"""M2 A16: the Codex pieces of the acceptance harness (driver, stub, estimate, dry run). Synthetic only;
the dry-run test runs the real codex binary against the 127.0.0.1 stub and is skipped without it."""

from __future__ import annotations

import json
import math
import shutil
import signal
import time
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
    assert env == {"PATH": "/bin", "TERM": "xterm", "HOME": str(tmp_path / "home"),
                   "CONTEXTO_HOME": str(tmp_path / "store")} and store == tmp_path / "store"
    env, store = codex_driver.child_env(tmp_path / "store", tmp_path / "home", mcp_home_workaround=True, base=base)
    assert env == {"PATH": "/bin", "TERM": "xterm", "HOME": str(tmp_path / "home")}  # a build before PR #148
    assert store == tmp_path / "home" / ".contexto"


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


class FakeProc:
    """A finished turn: writes the last message and, on the first turn, the thread.started event."""

    def __init__(self, fake, cmd, stdin, stdout, **kwargs):
        fake.calls.append(cmd)
        self.text, self.cmd, self.stdout, self.fake = [], cmd, stdout, fake
        self.stdin = SimpleNamespace(write=self.text.append, close=self._finish)
        self.returncode = 7 if fake.exit_at == len(fake.calls) else 0
        self.pid = -1  # never signalled: these turns finish on their own

    def _finish(self):
        prompt = "".join(self.text)
        Path(self.cmd[self.cmd.index("-o") + 1]).write_text("q_a_001: x" if script.PROBE_HEADER[0] in prompt else "OK")
        if len(self.fake.calls) == 1:
            self.stdout.write(json.dumps({"type": "thread.started", "thread_id": "T1"}) + "\n")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class FakeCodex:
    """Stands in for subprocess.Popen: each turn finishes at once (FakeProc)."""

    def __init__(self, exit_at=None):
        self.calls, self.exit_at = [], exit_at

    def __call__(self, cmd, **kwargs):
        return FakeProc(self, cmd, **kwargs)


def _turns(tmp_path, popen):
    return codex_driver.CodexTurns("contexto", "S", 64_000, "http://u", "/h", tmp_path, {}, popen=popen, poll=0.05)


def sleeper(cmd, **kwargs):
    """A real turn that would run for 60 seconds, in its own process group (as CodexTurns starts it)."""
    return subprocess.Popen([sys.executable, "-c", "import sys, time; sys.stdin.read(); time.sleep(60)"], **kwargs)


def test_run_session_resumes_the_thread_and_keeps_only_probe_answers(tmp_path):
    s = script.build(turns=4, turn_bytes=2_000)
    runner = FakeCodex()
    record = codex_driver.run_session(s, _turns(tmp_path, runner), tmp_path / "log.jsonl", 10**9, [1] * len(s.turns))
    assert record["turns_completed"] == len(s.turns) and record["stopped"] is None and record["thread"]
    assert "resume" not in runner.calls[0] and all(c[-3:] == ["resume", "T1", "-"] for c in runner.calls[1:])
    assert all("model_providers.contexto.request_max_retries=0" in c for c in runner.calls)  # retries pinned
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


def test_baseline_breakdown_adds_up_to_the_session_baseline():
    s = codex_budget.history_turns(64_000, 20_000)
    parts = codex_budget.baseline_breakdown(s)
    assert parts["requests"] == s.n_content + len(s.probe_turn_ids) == 21
    assert parts["head_total"] == 21 * codex_budget.CODEX_HEAD
    assert parts["input_total"] == parts["head_total"] + parts["history_total"]
    assert parts["once"] == parts["input_total"] + parts["output_total"]
    assert parts["retries"] == 0 and parts["total"] == parts["once"] == codex_budget.estimate(s, 64_000).baseline_worst
    assert codex_budget.baseline_breakdown(s, retries_per_turn=2)["total"] == 3 * parts["once"]


def test_staged_steps_and_the_default_bound_baseline_that_cannot_fit():
    small = codex_budget.staged(codex_budget.history_turns(64_000, 20_000), 64_000)
    assert small["replay_fits"] and small["total"] == small["step1_h1"] + small["step2_baseline_replay"]
    assert small["step2_baseline_replay"] < small["session_baseline"]  # the replay is the cheaper design
    default = codex_budget.staged(codex_budget.history_turns(150_000, 40_000), 150_000)
    assert default["content_turns"] == 18 and not default["replay_fits"]
    assert default["replay_largest_input"] + codex_budget.OUTPUT_ALLOWANCE > codex_budget.CODEX_WINDOW


def test_spend_watcher_stops_a_running_turn_and_the_session(tmp_path):
    """The in-turn stop (reviewer-1 on PR #154, a precondition for any live step): while a turn runs,
    once spent plus twice one request's worst case would pass the approved line, the turn's whole
    process group is terminated and the session stops."""
    s = script.build(turns=4, turn_bytes=2_000)
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(_entry(inp=500, out=0)) + "\n")
    turns = _turns(tmp_path, sleeper)
    started = time.monotonic()
    # Before the turn: 500 + 100 <= 2,000 passes. During it: 500 + 2 x 800 > 2,000 stops it.
    record = codex_driver.run_session(s, turns, log, 2_000, [100] * len(s.turns), inflight=800)
    assert time.monotonic() - started < 10  # stopped, not left to sleep 60 seconds
    assert record["stopped"] == "turn 1: stopped by the spend watcher (approved 2,000)"
    assert record["turns_completed"] == 0 and record["stopped_by_watcher"] == [0]
    assert turns.exits == [-signal.SIGTERM]


def test_spend_watcher_leaves_a_turn_alone_under_the_line(tmp_path):
    turns = _turns(tmp_path, FakeCodex())
    turns.watch = lambda: False
    assert turns.send(0, "log\nReply with exactly: OK") == "OK" and turns.stopped_by_watcher == []
