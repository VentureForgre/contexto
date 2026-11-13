"""The live benchmark's two Claude Code arms with the real ``claude``, against the stub upstream.

Skipped when ``claude`` is not on PATH (CI has none). Zero spend: Claude Code gets an isolated
configuration directory, a temp HOME, ``DISABLE_AUTOUPDATER=1``, a fake key from a helper, and a base
URL on 127.0.0.1; nothing reaches a model API.

Sizes. The script is ``script.build(3, "v2", bound=4_000, turn_bytes=2_000)`` (20 turns). The run's
bound is 70,000 tokens: Claude Code 2.1.322's own head is about 25,000 tokens as the stub counts it,
Contexto keeps 25,000 tokens of headroom under the bound, and 67,000 is the smallest bound whose
compaction trigger the plain arm can set (window 100,000). For the plain arm the stub reports 1.6
times its modelled usage, which carries the session over 70,000 tokens during the probe turns, so
the test can see where Claude Code compacts. The Contexto arm's usage is reported as modelled.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

import bench_stubs
from contexto.bench import script as bench_script

BOUND = 70_000
CLAUDE = shutil.which("claude")
pytestmark = pytest.mark.skipif(CLAUDE is None, reason="claude is not on PATH")


@pytest.fixture(scope="module")
def script():
    return bench_script.build(3, "v2", bound=4_000, turn_bytes=2_000)


@pytest.fixture(scope="module")
def run(script, tmp_path_factory):
    version = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True, timeout=120).stdout
    if "Claude Code" not in version:
        pytest.skip("the claude on PATH is not Claude Code")
    tmp = tmp_path_factory.mktemp("bench-real")
    monkeypatch = pytest.MonkeyPatch()
    try:
        bench_stubs.bench_environment(monkeypatch, tmp, real_claude=True)
        stub = bench_stubs.StubModel([script], key=bench_stubs.FAKE_KEY, usage_scale={"plain": 1.6},
                                     read_paths="absolute")
        result = bench_stubs.run_bench(monkeypatch, tmp, [script], arms=("contexto", "plain"), bound=BOUND, stub=stub,
                                       upstream=stub.start(), turn_timeout=300.0)
        stub.stop()
        result.version = version.split()[0]
        yield result
    finally:
        monkeypatch.undo()


def test_both_arms_finish_the_session_with_the_real_claude(run, script):
    assert run.code == 0, run.printed
    assert run.manifest["claude_code_version"] == run.version
    assert run.manifest["request_classes"] == "header"  # Claude Code named each request's class
    for arm in ("contexto", "plain"):
        unit = run.unit(arm)
        assert (unit["status"], unit["cli_exit"], unit["probe_turns_completed"]) == ("complete", 0, 8), unit
        assert unit["concluded_ok"] == {d.probe: True for d in script.decisions}
    assert len(run.lines("probes.jsonl")) == 2 * len(script.probes)


def test_the_tool_loop_turns_files_are_read_without_a_permission_prompt(run, script):
    for arm in ("contexto", "plain"):
        assert run.unit(arm)["toolloop_reads"] == len(script.reads) == 10
        loop = [r for r in run.stub.messages_requests(arm) if r["turn"] == script.toolloop_turn]
        assert len(loop) == 11 and not any(r["tool_errors"] for r in loop)  # ten reads, each answered with the file


def test_the_key_reaches_the_upstream_in_the_header(run):
    assert run.stub.requests and all(r["key_ok"] for r in run.stub.requests)
    assert all(r["x_api_key"] and r["bearer"] for r in run.stub.requests)  # a helper's key: both headers
    assert all(r["user_agent"].startswith("claude-cli/") for r in run.stub.requests)


def test_the_plain_arm_compacts_at_the_bound_and_the_contexto_arm_does_not(run):
    """The first evidence for the 33,000-token reserve short of a live run: with the window set to
    the bound plus 33,000, Claude Code compacts at the first request after its reported usage
    reached the bound, and not before."""
    assert run.manifest["plain_compaction"]["value"] == BOUND + 33_000
    contexto, plain = run.unit("contexto"), run.unit("plain")
    assert contexto["compactions"] == 0 and contexto["compaction_pre_tokens"] == []
    assert plain["compactions"] == 1, plain
    (pre_tokens,) = plain["compaction_pre_tokens"]
    requests = sorted((r for r in run.lines("requests.jsonl") if r["arm"] == "plain"), key=lambda r: r["n"])
    summaries = [r for r in requests if r["compaction"]]
    assert summaries and not any(r["main"] for r in summaries)
    before = [r["uncached"] + r["cache_write"] + r["cache_read"] + r["output"]
              for r in requests[:requests.index(summaries[0])] if r["main"]]
    assert len(before) >= 2 and all(usage < BOUND for usage in before[:-1]), before[-3:]
    assert before[-1] >= BOUND  # the usage that crossed the trigger
    assert before[-1] <= pre_tokens < BOUND + 3_000, (before[-1], pre_tokens)
    after = [r for r in requests[requests.index(summaries[-1]) + 1:] if r["main"]]
    assert after and after[0]["uncached"] + after[0]["cache_write"] + after[0]["cache_read"] < before[-1]
