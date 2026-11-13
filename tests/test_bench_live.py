"""The live benchmark end to end at zero spend: three arms against a stub upstream and a stub ``claude``.

One run (module fixture) with a planted fake key, a planted settings file whose ``apiKeyHelper`` is a
planted command, a custom upstream host name resolved to 127.0.0.1 inside this process, and a temp
home directory. The tests then read what the run wrote and printed.

Sizes: the script is ``script.build(3, "v2", bound=12_000, turn_bytes=3_000)`` (35 turns, about 41,000
tokens) and the run's bound is 45,000, because ``contexto run`` needs a bound above its 25,000-token
headroom floor and the session must be several times what the window then holds for turns to be
paged out and recalled.
"""

from __future__ import annotations

import json
import shlex
import socket
import sys
from pathlib import Path

import pytest

import bench_stubs
from contexto.bench import credentials as bench_credentials
from contexto.bench import meter as bench_meter
from contexto.bench import script as bench_script

SEED = 3
BOUND = 45_000
HOST = "bench-upstream.planted-host.test"
ERROR_RATES = {"contexto": 0.0, "full": 0.25, "plain": 0.1}


@pytest.fixture(scope="module")
def script():
    return bench_script.build(SEED, "v2", bound=12_000, turn_bytes=3_000)


@pytest.fixture(scope="module")
def run(script, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("bench-live")
    monkeypatch = pytest.MonkeyPatch()
    try:
        home = bench_stubs.bench_environment(monkeypatch, tmp)
        argv_log = tmp / "stub-claude-argv.jsonl"
        monkeypatch.setenv("STUB_CLAUDE_ARGV_LOG", str(argv_log))
        helper = home / "secrets" / "planted-key-helper.sh"
        helper.parent.mkdir()
        helper.write_text(f"#!/bin/sh\nprintf %s {bench_stubs.FAKE_KEY}\n", encoding="utf-8")
        helper_command = f"/bin/sh {shlex.quote(str(helper))} --planted-helper-flag"
        settings = home / ".claude-planted" / "settings.local.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"apiKeyHelper": helper_command}), encoding="utf-8")
        stub = bench_stubs.StubModel([script], key=bench_stubs.FAKE_KEY, error_rates=ERROR_RATES)
        port = int(stub.start().rsplit(":", 1)[1])
        real_getaddrinfo = socket.getaddrinfo

        def getaddrinfo(host, *args, **kwargs):  # the planted host is this machine; nothing else changes
            return real_getaddrinfo("127.0.0.1" if host == HOST else host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        result = bench_stubs.run_bench(
            monkeypatch, tmp, [script], arms=("contexto", "full", "plain"), bound=BOUND, stub=stub,
            upstream=f"http://{HOST}:{port}", credentials=bench_credentials.resolve(None, settings))
        stub.stop()
        result.planted = {"key": bench_stubs.FAKE_KEY, "helper command": helper_command, "helper path": str(helper),
                          "settings path": str(settings), "host": HOST, "home": str(home),
                          "user": bench_stubs.HOME_NAME}
        result.argv_log = [json.loads(x) for x in argv_log.read_text(encoding="utf-8").splitlines()]
        yield result
    finally:
        monkeypatch.undo()


def test_every_unit_completes_and_the_manifest_has_the_shared_fields(run, script):
    assert run.code == 0, run.printed
    manifest = run.manifest
    assert {k: manifest[k] for k in ("format", "kind", "model", "bound", "protocol", "seed", "seeds", "repeats",
                                     "arms", "upstream", "cache_ttl")} == {
        "format": 1, "kind": "live", "model": "claude-haiku-5-5", "bound": BOUND, "protocol": "v2", "seed": SEED,
        "seeds": [SEED], "repeats": 1, "arms": ["contexto", "full", "plain"], "upstream": "custom", "cache_ttl": "5m"}
    assert manifest["claude_code_version"] == "2.1.322" and isinstance(manifest["contexto_version"], str)
    assert manifest["started"].endswith("Z") and manifest["finished"].endswith("Z")
    assert manifest["turn_kbytes"] == 3 and manifest["credential_source"] == "settings"
    assert manifest["request_classes"] == "header"
    assert manifest["cli_environment"] == {
        "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1", "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # the caller's own opt-out, kept for both arms and recorded
        "settings": {"permissions": {"allow": ["Read"]}}}
    assert len(manifest["arm_differences"]) == 5 and len(manifest["metering"]) == 4
    # A bound under 67,000 cannot be matched: the smallest window is set, and the manifest says so.
    assert manifest["plain_compaction"]["variable"] == "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
    assert manifest["plain_compaction"]["value"] == 100_000 and manifest["plain_compaction"]["forced"] is False
    assert manifest["plain_compaction"]["intended_trigger"] == 67_000
    assert [u["arm"] for u in manifest["units"]] == ["contexto", "full", "plain"]
    for unit in manifest["units"]:
        assert unit["status"] == "complete" and unit["reason"] is None and unit["stop"] is None, unit
        assert unit["seed"] == SEED and unit["repeat"] == 0 and unit["attempt"] == 0
        assert unit["probe_turns_completed"] == 8 and isinstance(unit["compaction_pre_tokens"], list)
    full, contexto, plain = run.unit("full"), run.unit("contexto"), run.unit("plain")
    assert full["cli_exit"] is None and full["compactions"] is None and full["concluded_ok"] == {}
    assert full["toolloop_reads"] is None and full["requests"] == 8
    decisions = {d.probe: True for d in script.decisions}
    for unit in (contexto, plain):
        assert unit["cli_exit"] == 0 and unit["compactions"] == 0 and unit["compaction_pre_tokens"] == []
        assert unit["concluded_ok"] == decisions and len(decisions) == 6
        assert unit["toolloop_reads"] == len(script.reads) == 10
        assert unit["toolloop_requests"] == 11 and unit["over_bound"] == 0
    assert (run.out / "scripts" / f"seed-{SEED}.json").read_text(encoding="utf-8") == script.to_json()
    assert sorted(p.name for p in run.out.iterdir()) == ["manifest.json", "probes.jsonl", "requests.jsonl", "scripts"]


def test_probes_are_the_parsed_answers_scored_against_the_script(run, script):
    probes = run.lines("probes.jsonl")
    assert len(probes) == 3 * len(script.probes) == 216
    by_id = {p.id: p for p in script.probes}
    correct = {}
    for line in probes:
        assert set(line) == {"arm", "seed", "repeat", "model", "id", "cls", "type", "tier", "kind", "turn", "overlap",
                             "expected", "given", "correct", "stale"}
        probe = by_id[line["id"]]
        assert (line["cls"], line["type"], line["tier"], line["kind"], line["overlap"], line["expected"]) == (
            probe.cls, probe.type, probe.tier, probe.kind, probe.overlap, probe.answer)
        assert script.kinds[line["turn"]] == "probe" and line["model"] == "claude-haiku-5-5"
        assert isinstance(line["correct"], bool) and isinstance(line["stale"], bool)
        # What the stub answered is what was parsed and stored, never more than 200 characters of it.
        given = run.stub.answered[(line["arm"], SEED, line["id"])]
        assert line["given"] == given and len(line["given"]) <= 200
        assert line["correct"] == (given == probe.answer)
        correct[line["arm"]] = correct.get(line["arm"], 0) + line["correct"]
    # The stub misses a seeded share of the full and plain arms' probes, so the scores are known and differ.
    for arm in ("full", "plain"):
        missed = sum(bench_stubs.wrong(arm, SEED, p.id, ERROR_RATES[arm]) for p in script.probes)
        assert correct[arm] == len(script.probes) - missed and missed > 0
    assert len(set(correct.values())) == 3, correct
    assert correct["contexto"] >= 54  # found through Contexto's search, not from the request's own text


def test_the_contexto_arm_recalls_through_the_real_mcp_server(run, script):
    assert run.stub.searches == len(script.probes)
    log = _session_log(run)
    calls = [e for e in log if e.get("event") == "mcp_call"]
    assert len(calls) == len(script.probes) and all(e["tool"] == "contexto_search" and e["ok"] for e in calls)
    assert sum(1 for e in calls if e.get("hits")) >= 54
    rotations = [t for e in log for t in e.get("transform_events") or [] if t.get("event") == "rotation"]
    assert rotations, "the session must be long enough for Contexto to page turns out"


def _session_log(run) -> list[dict]:
    (path,) = (run.work / f"contexto-{SEED}-0-0" / "contexto-home" / "sessions").glob("*/log.jsonl")
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_requests_have_the_shared_fields_and_one_record_per_billed_request(run, script):
    records = run.lines("requests.jsonl")
    history = bench_meter.history_tokens(script)
    assert history[-1] == bench_script.estimate(script)["tokens"]
    for record in records:
        assert set(record) == {"arm", "seed", "repeat", "model", "n", "turn", "main", "history_tokens", "uncached",
                               "cache_write", "cache_read", "output", "status", "compaction", "basis", "attempt"}
        assert record["basis"] == "measured" and record["status"] == 200 and record["compaction"] is False
        assert record["main"] is True and record["history_tokens"] == history[record["turn"]]
        assert all(isinstance(record[k], int) for k in ("uncached", "cache_write", "cache_read", "output", "n"))
    for arm in ("contexto", "full", "plain"):
        mine = [r for r in records if r["arm"] == arm]
        assert sorted(r["n"] for r in mine) == list(range(len(mine)))
        # The stub upstream saw exactly these requests, with exactly this usage.
        seen = run.stub.messages_requests(arm)
        assert len(mine) == len(seen) == run.unit(arm)["requests"]
        by_n = sorted(mine, key=lambda r: r["n"])
        assert [(r["uncached"], r["cache_write"], r["cache_read"]) for r in by_n] == [
            tuple(s["usage"][k] for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            for s in seen]
        assert [r["turn"] for r in by_n] == [s["turn"] for s in seen]
        assert sum(r["cache_read"] for r in mine) > 0
    full_turns = [r["turn"] for r in records if r["arm"] == "full"]
    assert full_turns == [i for i, k in enumerate(script.kinds) if k == "probe"]
    assert run.manifest["spent_tokens"] == sum(bench_meter.record_tokens(r) for r in records)


def test_the_meter_agrees_with_contextos_own_session_log(run):
    logged = [e["usage"] for e in _session_log(run)
              if e.get("event") == "request" and e.get("path") == "/v1/messages" and e.get("usage")]
    metered = sorted((r for r in run.lines("requests.jsonl") if r["arm"] == "contexto"), key=lambda r: r["n"])
    assert len(logged) == len(metered) > 40
    assert [(r["uncached"], r["cache_write"], r["cache_read"], r["output"]) for r in metered] == [
        (u["input_tokens"], u["cache_creation_input_tokens"], u["cache_read_input_tokens"], u["output_tokens"])
        for u in logged]


def test_nothing_private_is_written_printed_or_on_a_command_line(run):
    files = [p for p in run.out.rglob("*") if p.is_file()]
    assert len(files) == 4
    haystacks = {f"file {p.relative_to(run.out)}": p.read_text(encoding="utf-8") for p in files}
    haystacks["printed"] = run.printed
    # The one process whose command line is the helper command is the helper itself (/bin/sh -c COMMAND).
    commands = [c for c in run.commands if c[:2] != ["/bin/sh", "-c"]]
    assert len(commands) < len(run.commands), "the helper was run for the full arm's requests"
    haystacks["child command lines"] = json.dumps(commands)
    haystacks["claude command lines"] = json.dumps(run.argv_log)
    assert len(run.argv_log) == 2  # one session per Claude Code arm (the stub does not log --version)
    for where, text in haystacks.items():
        for what, needle in run.planted.items():
            assert needle not in text, f"the planted {what} is in {where}"
    assert "contexto" in haystacks["child command lines"] and sys.executable in haystacks["child command lines"]


def test_the_key_reaches_the_upstream_in_the_header_of_every_arm(run):
    assert run.stub.requests and all(r["key_ok"] for r in run.stub.requests)
    # A helper's key goes out as both headers, as Claude Code sends it.
    assert all(r["x_api_key"] and r["bearer"] for r in run.stub.requests)
    full = run.stub.messages_requests("full")
    assert all(r["user_agent"].startswith("contexto-bench/") for r in full)
    assert all(r["user_agent"].startswith("claude-cli/") for r in run.stub.messages_requests("plain"))
    assert {r["path"] for r in run.stub.requests if r["arm"] == "full"} == {"/v1/messages", "/v1/messages/count_tokens"}


def test_what_the_upstream_received_is_what_an_unmetered_run_would_send(run):
    for arm in ("contexto", "plain"):
        seen = run.stub.messages_requests(arm)
        # The request-class headers the benchmark asks Claude Code for stop at the meter.
        assert all(r["request_class"] is None and r["compaction"] is None for r in seen)
        assert all(r["accept_encoding"] == "identity" for r in seen)
    # Contexto adds its affinity header for an upstream that is not a vendor's, and the planted host is not.
    assert all(r["affinity"] for r in run.stub.messages_requests("contexto"))
    assert not any(r["affinity"] for r in run.stub.messages_requests("plain"))
    assert "mcp__contexto__contexto_search" in run.stub.messages_requests("contexto")[0]["tools"]
    assert run.stub.messages_requests("plain")[0]["tools"] == ["Read"]


def test_the_full_arm_sends_the_script_alone(run, script):
    seen = run.stub.messages_requests("full")
    assert [r["tool_choice"] for r in seen] == ["none"] * 8 and all(r["tools"] == ["read_file"] for r in seen)
    assert all(r["cache_breakpoints"] == 1 and r["max_tokens"] == 8_192 and r["stream"] for r in seen)
    assert [r["bytes"] for r in seen] == sorted(r["bytes"] for r in seen)  # each carries the earlier probe turns


def test_the_working_directories_are_outside_the_output_directory(run):
    assert run.work not in run.out.parents and run.out not in run.work.parents
    cwd = run.work / f"plain-{SEED}-0-0" / "cwd"
    assert sorted(p.name for p in (cwd / "bench-files").iterdir()) == [f"log-{n:02d}.txt" for n in range(1, 11)]
    settings = json.loads((run.work / f"plain-{SEED}-0-0" / "claude-config" / "settings.json").read_text())
    assert settings["permissions"] == {"allow": ["Read"]}
    assert Path(shlex.split(settings["apiKeyHelper"])[0]) == Path(sys.executable)
