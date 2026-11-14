"""``contexto bench --replay``: the replay itself, through the real proxy (D66).

``pinned_run`` replays the shipped trace once, with ``--out``, and the tests read what it printed and
wrote. ``test_the_pinned_replay_equals_the_trace`` is the one test that compares the measurements with
the trace: every other test reads the numbers it checks from the trace file or from the run itself and
never repeats a measurement as a literal, so recording the trace again
(``contexto bench --replay --record-trace src/contexto/bench/data/trace-20261016.json --force``) is the
whole of an intended update, and a change to the proxy fails one test, with the message that says how
to record it, not every test. ``test_a_proxy_change_needs_only_the_trace_recorded_again`` checks that.
``short_run`` is a shorter session that is not the pinned one. Neither needs ``claude``, a key or a
network.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from contexto import __version__, cli, mcp
from contexto.bench import script as bench_script
from contexto.bench.model import NEW_CLASSES, Script
from contexto.bench.modes import replay as replay_mode
from contexto.bench.replay import client, lines, output, reach, scripted, session, trace
from contexto.store import SessionStore

SHORT = ["--protocol", "d27", "--replay-turns", "8"]  # 14 turns, about 3 seconds


def _run(argv):
    """``contexto bench <argv>`` with its output captured: (exit code, stdout, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(["bench", *argv])
    return code, stdout.getvalue(), stderr.getvalue()


def _watched(argv):
    """Run a replay and keep what it measured and every address a socket connected to or looked up."""
    patch = pytest.MonkeyPatch()
    kept: dict = {"connections": [], "lookups": []}
    connect, getaddrinfo, replay = socket.socket.connect, socket.getaddrinfo, session.replay

    def watch_connect(sock, address):
        kept["connections"].append(address)
        return connect(sock, address)

    def watch_lookup(host, *args, **kwargs):
        kept["lookups"].append(host)
        return getaddrinfo(host, *args, **kwargs)

    def keep_replay(script, home):
        kept["script"] = script
        kept["measured"] = replay(script, home)
        return kept["measured"]

    patch.setattr(socket.socket, "connect", watch_connect)
    patch.setattr(socket, "getaddrinfo", watch_lookup)
    patch.setattr(session, "replay", keep_replay)
    try:
        kept["code"], kept["stdout"], kept["stderr"] = _run(argv)
    finally:
        patch.undo()
    return kept


def _load(out: Path) -> dict:
    return {
        "manifest": json.loads((out / "manifest.json").read_text("utf-8")),
        "requests": [json.loads(line) for line in (out / "requests.jsonl").read_text("utf-8").splitlines()],
        "reach": [json.loads(line) for line in (out / "reach.jsonl").read_text("utf-8").splitlines()],
        "results": json.loads((out / "results.json").read_text("utf-8")),
        "markdown": (out / "results.md").read_text("utf-8"),
    }


def _pinned(out: Path) -> dict:
    """The pinned replay with ``--out``, and the expectations of the trace it was compared with. Nothing
    is asserted here: a difference fails the comparison test alone."""
    expected = trace.load_packaged()["expected"]
    run = _watched(["--replay", "--out", str(out)])
    return {**run, "out": out, "expected": expected, **(_load(out) if (out / "results.json").exists() else {})}


@pytest.fixture(scope="module")
def pinned_run(tmp_path_factory):
    return _pinned(tmp_path_factory.mktemp("replay-pinned") / "out")


@pytest.fixture(scope="module")
def short_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("replay-short") / "out"
    run = _watched(["--replay", *SHORT, "--out", str(out)])
    assert run["code"] == 0, run["stdout"] + run["stderr"]
    return {**run, "out": out, **_load(out)}


# The checks of a pinned run. Each takes the run; the pinned numbers come from run["expected"], the
# trace the run was compared with, so they hold for the shipped trace and for one recorded again.


def check_equals_the_trace(run):
    assert run["code"] == 0, run["stdout"] + run["stderr"]  # stderr holds the differences and how to record
    assert trace.expected(run["measured"]) == run["expected"]  # every pinned measurement, on this platform
    block = run["results"]["replay"]
    assert block["pinned"] is True and block["differences"] == [] and block["failures"] == [] and block["passed"]
    assert run["stderr"] == ""


def check_the_bound_held(run):
    """The promise, whatever the trace says: not a pinned number but a property of every pinned run."""
    block, script = run["results"]["replay"], run["script"]
    assert block["max_input_estimated"] == max(r["uncached"] for r in run["measured"].requests) < script.bound
    assert (block["over_bound"], block["over_bound_proxy"], block["compaction_markers"]) == (0, 0, 0)
    assert block["failure_events"] == {} and block["problems"] == [] and block["notices"] == []
    assert block["turns"] == len(script.turns) and block["requests"] == len(scripted.steps(script))
    sizes = bench_script.estimate(script)
    assert (block["content_history_tokens"], block["history_tokens"]) == (sizes["history_tokens"], sizes["tokens"])
    assert block["head_tokens"] == session.head_tokens(client.head(session.session_name(script)))
    assert block["fit"]["fits"] is True and block["cannot_show"] == output.CANNOT_SHOW


def check_the_engine_ran_as_in_a_launch(run):
    events, measured, script = run["results"]["replay"]["events"], run["measured"], run["script"]
    assert events["request"] == run["expected"]["requests"]
    assert events["rotation"] == events["page_out"] == run["expected"]["rotations"]
    assert events["mcp_call"] == measured.searches == len(script.probes)
    assert events["launch"] == events["proxy_start"] == events["proxy_stop"] == 1
    assert not set(events) & set(session.FAILURE_EVENTS)
    # The proxy's stream hold ran (the fake upstream streams chunked), and no request was forwarded untransformed.
    assert not {"hold_skipped", "proxy_error", "transform_error", "upstream_4xx", "over_bound",
                "bound_too_small"} & set(events)
    # Diagnostics that are true of a synthetic client and are not failures, said in the output.
    assert run["results"]["replay"]["synthetic_client"]["diagnostics"] == {"session_start_unrecognised": 1,
                                                                            "estimate_error": 1}
    assert measured.toolloop_reads == len(script.reads)
    assert measured.probe_turns_completed == len(script.turns) - script.n_content


def check_the_terminal_text(run):
    text, expected, script = run["stdout"], run["expected"], run["script"]
    block, totals, sizes = run["results"]["replay"], expected["reach"], bench_script.estimate(script)
    head = text.splitlines()[:16]
    assert head[0] == "contexto bench --replay: seed 20261016, protocol v2, bound 150,000 tokens (the pinned trace)"
    joined = "\n".join(head)
    for said in ("No model is called", "Sizes are estimates, not token counts: 1.8 bytes per token for pasted log text "
                 "and 3.0 for everything else", "What this replay shows:",
                 f"Bound: held. {expected['requests']} requests, the largest {expected['max_input_estimated']:,} "
                 "estimated tokens against the bound of 150,000",
                 "Compaction: a replay has no CLI, so it cannot show this. What it shows: no request was over the "
                 "bound of 150,000 by the estimate, and none carried a compaction summary or marker.",
                 f"Search: under one fixed query per fact, the store's search returns {totals['reachable']} of "
                 f"{totals['probes']} planted facts, {totals['shown']} of them in the top 3, and "
                 f"{totals['covered'] - totals['reachable']} more are still in view. This is a floor under one "
                 "mechanical rule, not what a model can reach. The best query made from each question's own words "
                 f"puts {totals['best_query']} in the top 3. The best-query figure counts a page for a query only "
                 "when every word of the query is on the page as a whole word, not only inside a longer one, as the "
                 "store's search may match it.",
                 "What it cannot show:", "Answer accuracy, cache reads, real token counts, what a real CLI or a real "
                 "model does, and what the proxy does with what only Claude Code sends."):
        assert said in joined, said
    full = [r for r in run["requests"] if r["arm"] == "full"]
    plain = [r for r in run["requests"] if r["arm"] == "plain"]
    for said in (f"{sizes['turns']} turns ({sizes['content_turns']} content, {sizes['probe_turns']} probe)",
                 f"{sizes['history_tokens']:,} estimated tokens of history, "
                 f"{sizes['history_tokens'] / script.bound:.2f} times the bound",
                 f"the synthetic head is {expected['head_tokens']:,} estimated tokens.",
                 f"{expected['rotations']} rotations, {expected['pages']} pages stored, failure events: none",
                 "Client: The proxy logged session_start_unrecognised (1), estimate_error (1) for the synthetic "
                 "client; a replay expects these and does not count them as failures. Not exercised: the client "
                 "sends no Claude Code User-Agent", "no role system message inside messages",
                 "Modelled from the script, not run (log text at 1.8 bytes per token plus 20 tokens per reply, "
                 "probe turns at 3.5 bytes per token):",
                 f"Full context: the last request would be {full[-1]['uncached']:,} estimated tokens.",
                 f"{lines.compactions(plain)} modelled compactions, largest request "
                 f"{max(r['uncached'] for r in plain if r['main']):,} estimated tokens.",
                 "Pinned expectations: every measurement equals the trace.",
                 "Result: pass (the bound held, no failure event, measurements equal the pinned trace)",
                 "manifest.json, requests.jsonl, reach.jsonl, results.json, results.md"):
        assert said in text, said
    unreached = [cls for cls in NEW_CLASSES if totals["by_class"][cls]["reachable"] == 0]
    if unreached:
        assert "The rule's query finds none of " in text and "Which query a model makes is for the live run." in text
    assert "NOT the pinned trace" not in text and "FAIL" not in text and block["seconds"] > 0


def check_results_md(run):
    text, expected = run["markdown"], run["expected"]
    shows, cannot = text.index("## What this replay shows"), text.index("## What it cannot show")
    assert shows < cannot < text.index("## How the proxy saw the synthetic client") < text.index("## Measurements")
    assert shows < 900
    for said in ("This is the pinned trace", "No model is called", f"**The bound held.** {expected['requests']} "
                 "requests", "- **Compaction:** a replay has no CLI, so it cannot show this.",
                 "- Answer accuracy: the answers in the conversation are scripted.", "- Cache reads:",
                 "- Real token counts:", "- What a real CLI or a real model does.",
                 "session_start_unrecognised (1), estimate_error (1)",
                 f"| Largest request, estimated tokens | {expected['max_input_estimated']:,} |",
                 f"| Rotations | {expected['rotations']} |", "| Failure events | none |",
                 "a floor under mechanical queries, not what a model can reach",
                 "every `concluded` question shares: \"Earlier you had to pick the ..., and you replied with your "
                 "pick. What did you pick?\")", output.WHOLE_WORDS,
                 "| class | probes | paged | by search | in the top 3 | by search or in view | best query from the "
                 "question's words |",
                 "## Modelled lines (not run)", "errs toward plain Claude Code", "They are not a run of Claude Code",
                 "M0-proxy-measurements.md", "## The store", "`affinity.salt`", "page.key", "Pass: the bound held"):
        assert said in text, said
    totals = expected["reach"]
    for cls, cell in [*totals["by_class"].items(), ("all", totals)]:
        row = [cls, *(str(cell[k]) for k in ("probes", "paged", "reachable", "shown", "covered", "best_query"))]
        assert "| " + " | ".join(row) + " |" in text, cls


def check_manifest(run):
    manifest = run["manifest"]
    assert run["results"]["manifest"] == manifest
    listed = {"format": 1, "kind": "replay", "contexto_version": __version__, "claude_code_version": None,
              "model": None, "bound": 150_000, "protocol": "v2", "turn_kbytes": 40, "seed": 20261016,
              "seeds": [20261016], "repeats": 1, "arms": ["contexto", "full", "plain"], "upstream": "local",
              "plain_compaction": None, "cache_ttl": None}
    assert {key: manifest[key] for key in listed} == listed
    for stamp in (manifest["started"], manifest["finished"]):
        assert len(stamp) == 20 and stamp.endswith("Z") and stamp[10] == "T"
    unit_fields = ["arm", "seed", "repeat", "status", "reason", "cli_exit", "compactions", "compaction_pre_tokens",
                   "concluded_ok", "toolloop_reads", "probe_turns_completed"]
    assert [list(unit) for unit in manifest["units"]] == [unit_fields] * 3
    replayed, full, plain = manifest["units"]
    assert replayed == {"arm": "contexto", "seed": 20261016, "repeat": 0, "status": "complete", "reason": None,
                        "cli_exit": None, "compactions": 0, "compaction_pre_tokens": [], "concluded_ok": {},
                        "toolloop_reads": len(run["script"].reads),
                        "probe_turns_completed": run["measured"].probe_turns_completed}
    for unit, arm in ((full, "full"), (plain, "plain")):
        assert (unit["arm"], unit["status"], unit["compactions"]) == (arm, "not_run", None)
        assert "modelled" in unit["reason"] and "not run" in unit["reason"]
    added = manifest["replay"]
    assert added["pinned"] is True and added["trace"] == "trace-20261016.json" and added["turns"] == 40
    assert added["script_sha256"] == run["script"].digest()
    assert added["rates"] == trace.RATES == {"log_bytes_per_token": 1.8, "other_bytes_per_token": 3.0,
                                             "log_text_min_bytes": 5_000, "tool_result_text_is_log_text": True}
    assert added["modelled"]["summary_tokens"] == 7_920 and added["modelled"]["trigger"] == 150_000
    assert set(added["environment"]["pinned"]) == {*session.ENVIRONMENT, "CONTEXTO_*"}
    assert set(manifest) == {*listed, "started", "finished", "units", "replay"}


REQUEST_FIELDS = {"arm": str, "seed": int, "repeat": int, "model": type(None), "n": int, "turn": int, "main": bool,
                  "history_tokens": int, "uncached": int, "cache_write": int, "cache_read": int, "output": int,
                  "status": int, "compaction": bool, "basis": str}


def check_requests_jsonl(run):
    rows, script, expected = run["requests"], run["script"], run["expected"]
    for row in rows:
        assert list(row)[:len(REQUEST_FIELDS)] == list(REQUEST_FIELDS)
        for key, kind in REQUEST_FIELDS.items():
            assert type(row[key]) is kind, (key, row)
        assert (row["seed"], row["repeat"], row["status"]) == (20261016, 0, 200)
        assert row["cache_write"] == row["cache_read"] == 0  # a replay cannot show cache reads
    by_arm = {arm: [r for r in rows if r["arm"] == arm] for arm in ("contexto", "full", "plain")}
    assert sum(map(len, by_arm.values())) == len(rows)
    assert {r["basis"] for r in by_arm["contexto"]} == {"estimated"}
    assert {r["basis"] for r in [*by_arm["full"], *by_arm["plain"]]} == {"modelled"}
    for part in by_arm.values():
        assert [r["n"] for r in part] == list(range(len(part)))
    # The replayed arm: one row per request the fake upstream received, in order.
    replayed = by_arm["contexto"]
    assert [r["uncached"] for r in replayed] == expected["request_tokens"]
    assert [(r["turn"], r["round"]) for r in replayed] == [(s.turn, s.round) for s in scripted.steps(script)]
    history = lines.history_tokens(script)
    assert all(r["history_tokens"] == history[r["turn"]] and r["main"] and not r["compaction"] for r in replayed)
    assert replayed[-1]["history_tokens"] == bench_script.estimate(script)["tokens"]
    assert sum(r["rotation"] for r in replayed) == expected["rotations"]
    assert all(r["round"] == 0 for r in replayed if r["rotation"])
    # The proxy's own estimate is beside the fake upstream's count; once usage is known the two are close.
    assert all(type(r["proxy_estimate"]) is int for r in replayed)
    assert all(abs(r["proxy_estimate"] - r["uncached"]) < 0.15 * r["uncached"] for r in replayed[1:])
    # The modelled arms: the same rows the model gives for the measured head.
    assert by_arm["full"] == lines.full_rows(script, expected["head_tokens"])
    assert by_arm["plain"] == lines.plain_rows(script, expected["head_tokens"], 150_000)
    assert by_arm["full"][-1]["uncached"] == expected["head_tokens"] + bench_script.estimate(script)["tokens"]
    compactions = [r for r in by_arm["plain"] if r["compaction"]]
    assert compactions and all(not r["main"] and r["output"] == lines.SUMMARY_TOKENS for r in compactions)
    assert max(r["uncached"] for r in by_arm["plain"] if r["main"]) <= 150_000


def check_reach_jsonl(run):
    rows, script = run["reach"], run["script"]
    assert [r["id"] for r in rows] == [p.id for p in script.probes]
    listed = {"seed": int, "id": str, "cls": str, "overlap": int, "plant_turns": list, "paged": bool, "query": str,
              "reachable": bool}
    for row, probe in zip(rows, script.probes):
        assert list(row)[:9] == [*listed, "rank"]
        for key, kind in listed.items():
            assert type(row[key]) is kind, (key, row)
        assert row["rank"] is None or (type(row["rank"]) is int and row["rank"] >= 1)
        assert (row["rank"] is not None) == row["reachable"]
        assert (row["seed"], row["cls"], row["overlap"]) == (20261016, probe.cls, probe.overlap)
        assert row["plant_turns"] == bench_script.plant_turns(script, probe)
        assert row["query"] == reach.first_query(probe) and row["queries"][0] == row["query"]
        assert not row["reachable"] or (row["paged"] or row["cls"] in ("chain", "obligations"))
        assert row["covered"] >= row["reachable"]
        if probe.cls in NEW_CLASSES:
            # The rule's query has no instruction word, and the best query is one the column tries.
            assert not set(row["query"].split()) & reach.instruction_words(probe)
            assert row["best_query"] is None or row["best_query"] in reach.question_queries(probe)
            assert (row["best_query"] is None) == (row["best_rank"] is None)
        else:
            # The rule's query already comes from the question; the column counts it under its whole-word check.
            assert row["best_rank"] is None or (row["reachable"] and row["best_rank"] >= row["rank"])
            assert row["best_query"] in (None, row["query"])
    totals = reach.totals(rows)
    assert totals == run["results"]["replay"]["reach"] == run["expected"]["reach"]
    assert totals["covered"] + totals["neither"] == totals["probes"] == len(script.probes)
    assert totals["neither_by_any_query"] <= totals["neither"]


def check_results_json(run):
    data, rows = run["results"], run["requests"]
    assert (data["format"], data["kind"], data["bound"]) == (1, "replay", 150_000)
    series = data["request_series"]
    assert list(series) == ["contexto", "full", "plain"]
    assert [series[arm]["basis"] for arm in series] == ["estimated", "modelled", "modelled"]
    assert "estimated" in series["contexto"]["label"]
    assert all("modelled" in series[arm]["label"] for arm in ("full", "plain"))
    for arm in series:
        main = [r for r in rows if r["arm"] == arm and r["main"]]
        assert series[arm]["points"] == [[r["history_tokens"], r["uncached"]] for r in main]
    assert [len(series[arm]["points"]) for arm in series] == [run["expected"]["requests"], len(run["script"].turns),
                                                              len(run["script"].turns)]
    notes = " ".join(data["notes"])
    for said in ("Synthetic", "No model was called", "estimates, not token counts", "modelled from the script, not run",
                 "is an assumption, and it errs toward plain Claude Code", "floor under mechanical queries",
                 output.WHOLE_WORDS,
                 "Which query a model makes is for the live run", "cannot show answer accuracy, cache reads, real "
                 "token counts"):
        assert said in notes, said
    assert all(isinstance(note, str) for note in data["notes"])
    full = [r for r in rows if r["arm"] == "full"]
    plain = [r for r in rows if r["arm"] == "plain"]
    assert data["replay"]["modelled"] == {"full_max": full[-1]["uncached"],
                                          "plain_max": max(r["uncached"] for r in plain if r["main"]),
                                          "plain_compactions": lines.compactions(plain), "rates": lines.RATES_TEXT}


def check_the_store_is_kept_without_its_private_files(run):
    out, script = run["out"], run["script"]
    assert Script.read(out / "scripts" / "seed-20261016.json") == script
    assert sorted(p.name for p in out.iterdir()) == ["manifest.json", "reach.jsonl", "requests.jsonl", "results.json",
                                                     "results.md", "scripts", "store"]
    assert run["results"]["replay"]["private_files_removed"] == ["affinity.salt",
                                                                 "sessions/bench-replay-20261016/page.key"]
    assert not list((out / "store").rglob("page.key")) and not list((out / "store").rglob("affinity.salt"))
    store = SessionStore.open_existing(out / "store", "bench-replay-20261016")
    assert len(store.page_ids()) >= run["expected"]["pages"]  # the pages at the first probe turn, and any after
    probe = next(p for p in script.probes if p.cls == "paged_only")
    (page_id,) = store.search(probe.subjects[0])  # still readable and searchable without its key
    assert probe.statements[0] in json.dumps(store.get_page(page_id)["messages"])
    assert "store/ (session bench-replay-20261016)" in run["stdout"]


def check_no_path_of_this_machine_is_in_any_output_file(run):
    out = run["out"]
    private = [str(Path.home()).encode(), str(out.parent).encode(), str(Path.cwd()).encode()]
    files = [p for p in out.rglob("*") if p.is_file()]
    assert len(files) > 50
    for path in files:
        data = path.read_bytes()
        for needle in private:
            assert needle not in data, (path.name, needle)
    meta = out / "store" / "sessions" / "bench-replay-20261016" / "meta.json"
    assert b"/contexto-bench-replay" in meta.read_bytes()  # the fixed name the store records as its directory


def check_output_helpers_agree_with_what_was_written(run):
    script, measured = run["script"], run["measured"]
    assert output.request_rows(script, measured) == run["requests"]
    assert output.failures(script, measured, []) == []
    assert output.markdown(script, run["results"]) == run["markdown"]
    assert "\n".join(output.summary(script, run["results"])) in run["stdout"]


PINNED_CHECKS = [check_equals_the_trace, check_the_bound_held, check_the_engine_ran_as_in_a_launch,
                 check_the_terminal_text, check_results_md, check_manifest, check_requests_jsonl, check_reach_jsonl,
                 check_results_json, check_the_store_is_kept_without_its_private_files,
                 check_no_path_of_this_machine_is_in_any_output_file, check_output_helpers_agree_with_what_was_written]


# The pinned replay


def test_the_pinned_replay_equals_the_trace(pinned_run):
    """The one comparison with the trace. On a difference the message (stderr, in the assertion) lists
    what differed and gives the command that records the trace again."""
    check_equals_the_trace(pinned_run)


def _needs_outputs(run):
    if "results" not in run:
        pytest.fail(f"the pinned replay wrote no output (exit {run['code']}): {run['stderr'][-2000:]}")


@pytest.mark.parametrize("check", PINNED_CHECKS[1:], ids=lambda check: check.__name__)
def test_a_pinned_run(pinned_run, check):
    _needs_outputs(pinned_run)
    if pinned_run["code"] != 0:
        pytest.skip("the pinned replay differs from the trace: test_the_pinned_replay_equals_the_trace says how")
    check(pinned_run)


def test_the_replay_connects_to_loopback_only(pinned_run, short_run):
    for run in (pinned_run, short_run):
        assert len(run["connections"]) >= 2 * len(run["measured"].requests)  # client to proxy, proxy to upstream
        assert {address[0] for address in run["connections"]} == {"127.0.0.1"}
        assert set(run["lookups"]) <= {"127.0.0.1"}  # no name is ever resolved


def test_a_proxy_change_needs_only_the_trace_recorded_again(monkeypatch, tmp_path):
    """Reviewer A's repro of finding 2: one word of an MCP tool description made longer grows every
    request. Against the shipped trace the replay fails, saying what differed, the command that records
    the trace again and what to check in its diff; recorded again into a file the tests are pointed at,
    every check of a pinned run passes."""
    tools = copy.deepcopy(mcp.TOOLS)
    tool = next(t for t in tools if "a short note on" in t["description"])
    tool["description"] = tool["description"].replace("a short note on", "a short note saying")
    monkeypatch.setattr(mcp, "TOOLS", tools)
    shipped = trace.load_packaged()
    code, text, err = _run(["--replay"])
    assert code == 1 and "Pinned expectations: " in text and "Result: FAIL" in text
    requests = shipped["expected"]["requests"]
    assert f"request_tokens: {requests} of {requests} differ; the first is number 1" in err
    assert f"All {requests} differ, by +" in err and "(the head, the tool definitions)" in err
    assert err.startswith("The measurements differ from the pinned trace in ")
    for said in ("contexto bench --replay --record-trace src/contexto/bench/data/trace-20261016.json --force",
                 "1. request sizes grew or shrank only where the change was meant to act",
                 "2. the bound still holds", "3. reach did not fall"):
        assert said in err, said
    # The maintainer's step, into a temporary file, and the pinned checks pointed at it.
    path = tmp_path / "trace-20261016.json"
    code, text, err = _run(["--replay", "--record-trace", str(path)])
    assert code == 0 and err == "", err
    recorded = trace.parse(path.read_text("utf-8"))
    assert recorded["expected"]["request_tokens"] != shipped["expected"]["request_tokens"]
    assert {k: v for k, v in recorded.items() if k not in ("expected", "recorded_with")} == {
        k: v for k, v in shipped.items() if k not in ("expected", "recorded_with")}  # only the measurements moved
    monkeypatch.setattr(trace, "load_packaged", lambda seed=trace.PINNED["seed"]: recorded)
    run = _pinned(tmp_path / "out")
    for check in PINNED_CHECKS:
        check(run)


# A session that is not the pinned one


def test_a_replay_with_other_options_runs_and_says_it_is_not_the_pinned_trace(short_run):
    text, block = short_run["stdout"], short_run["results"]["replay"]
    script = short_run["script"]
    assert text.splitlines()[0].endswith("protocol d27, bound 150,000 tokens (NOT the pinned trace)")
    said = "nothing was checked against recorded hashes or expected measurements"
    assert said in "\n".join(text.splitlines()[:8]) and "Pinned expectations" not in text
    assert "Result: pass (the bound held, no failure event)" in text
    assert block["pinned"] is False and block["differences"] == [] and block["passed"]
    assert (block["turns"], block["requests"], block["over_bound"], block["failure_events"]) == (
        len(script.turns), len(scripted.steps(script)), 0, {})
    manifest = short_run["manifest"]
    assert manifest["protocol"] == "d27" and manifest["replay"]["pinned"] is False
    assert manifest["replay"]["trace"] is None and manifest["replay"]["checked"].startswith("nothing")
    assert manifest["units"][0]["toolloop_reads"] is None  # a d27 session has no tool-loop turn
    assert "not the pinned trace" in short_run["results"]["notes"][-1]
    assert "**This is not the pinned trace" in short_run["markdown"]
    assert len(short_run["reach"]) == len(script.probes) == 54


def test_two_replays_of_one_session_measure_the_same(short_run, tmp_path):
    again = _watched(["--replay", *SHORT])  # no --out: a temporary store, nothing written
    assert again["code"] == 0 and "Written to" not in again["stdout"]
    assert trace.expected(again["measured"]) == trace.expected(short_run["measured"])
    assert again["measured"].requests == short_run["measured"].requests
    assert list(tmp_path.iterdir()) == []


def test_a_store_whose_search_finds_nothing_reaches_no_paged_fact(monkeypatch, short_run):
    monkeypatch.setattr(SessionStore, "search", lambda self, query: [])
    code, text, _ = _run(["--replay", *SHORT])
    assert code == 0  # not the pinned trace: nothing to differ from, and the bound still held
    assert "the store's search returns 0 of 54 planted facts" in text and "puts 0 in the top 3" in text
    assert short_run["results"]["replay"]["reach"]["reachable"] > 0


def test_a_bound_the_session_does_not_fit_fails_and_says_why():
    """Reviewer B: a bound too small for the head and the largest turn says so; the sizes say "estimated"."""
    run = _watched(["--replay", "--protocol", "d27", "--replay-turns", "4", "--bound", "30000"])
    code, text, err, measured = run["code"], run["stdout"], run["stderr"], run["measured"]
    assert code == 1 and err == ""
    assert measured.over_bound > 0 and measured.over_bound_proxy > 0
    assert (f"Bound: NOT held. {len(measured.requests)} requests, the largest {measured.max_input:,} estimated tokens "
            f"against the bound of 30,000; {measured.over_bound} over the bound by the fake upstream's count, "
            f"{measured.over_bound_proxy} by the proxy's own log.") in text
    largest = max(lines.turn_estimates(run["script"]))
    too_small = (f"the bound of 30,000 is too small for this session: the synthetic head is {measured.head_tokens:,} "
                 f"estimated tokens and the largest turn about {largest:,}")
    assert f"  Why: {too_small}" in text
    assert (f"Compaction: a replay has no CLI, so it cannot show this. What it shows: {measured.over_bound} requests "
            "over the bound by the estimate") in text
    assert "at the size at which a CLI compacts" not in text
    result = text[text.index("Result: FAIL"):]
    for said in (f"{measured.over_bound} requests were over the bound of 30,000 tokens by the fake upstream's count "
                 f"(largest {measured.max_input:,})",
                 f"the proxy's own log marks {measured.over_bound_proxy} requests over the bound",
                 "failure events in the session log: ", too_small):
        assert said in result, said
    assert "bound 30,000 is too small" in text  # the launcher's own warning, as `contexto run` prints it


def test_bound_too_small_from_the_engine_fails_an_unpinned_run(monkeypatch, short_run):
    """Reviewer A's nit: the acceptance harness fails on bound_too_small, and so does a replay that is not
    the pinned one."""
    measured = dataclasses.replace(copy.deepcopy(short_run["measured"]),
                                   events={**short_run["measured"].events, "bound_too_small": 2})
    monkeypatch.setattr(session, "replay", lambda script, home: measured)
    code, text, _ = _run(["--replay", *SHORT])
    assert code == 1 and "the engine logged bound_too_small 2 times: a new turn alone did not fit its window" in text


# The pinned trace, refused or differing


def _no_replay(monkeypatch):
    def refuse(script, home):
        raise AssertionError("nothing may be replayed")

    monkeypatch.setattr(session, "replay", refuse)


def test_an_altered_trace_is_refused_before_anything_runs(monkeypatch, capsys):
    altered = copy.deepcopy(trace.load_packaged())
    altered["turn_sha256"][11] = "f" * 64
    monkeypatch.setattr(trace, "load_packaged", lambda seed=0: altered)
    _no_replay(monkeypatch)
    assert cli.main(["bench", "--replay"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    turns = len(altered["turn_sha256"])
    assert captured.err.startswith(f"contexto bench: error: the generator and the pinned trace disagree: turn 12 of "
                                   f"{turns} ")
    assert "ffffffffffffffff in the trace" in captured.err and "Nothing was replayed." in captured.err
    assert "--record-trace" in captured.err


def test_a_generator_that_disagrees_with_the_trace_is_refused(monkeypatch, capsys):
    real = bench_script.build

    def drifted(*args, **kwargs):
        script = real(*args, **kwargs)
        turns = list(script.turns)
        last = max(i for i, kind in enumerate(script.kinds) if kind == "log")
        turns[last] = turns[last].replace("level=INFO", "level=NOTE", 1)  # one word of one log line
        assert turns[last] != script.turns[last]
        return dataclasses.replace(script, turns=tuple(turns))

    monkeypatch.setattr(bench_script, "build", drifted)
    _no_replay(monkeypatch)
    assert cli.main(["bench", "--replay"]) == 1
    captured = capsys.readouterr()
    pinned = real(*(trace.PINNED[k] for k in ("seed", "protocol", "bound", "turn_bytes", "turns")))
    last = max(i for i, kind in enumerate(pinned.kinds) if kind == "log")
    assert captured.out == ""
    assert f"the generator and the pinned trace disagree: turn {last + 1} of {len(pinned.turns)}" in captured.err
    assert "Nothing was replayed." in captured.err
    # The same generator with other options is not the pinned trace, so there is nothing to refuse it by.
    monkeypatch.undo()
    monkeypatch.setattr(bench_script, "build", drifted)
    code, text, err = _run(["--replay", *SHORT])
    assert code == 0 and err == "" and "NOT the pinned trace" in text


def test_a_trace_missing_from_the_build_is_refused(monkeypatch, capsys):
    def missing(seed=0):
        raise trace.TraceError("this build has no pinned trace trace-20261016.json (FileNotFoundError)")

    monkeypatch.setattr(trace, "load_packaged", missing)
    _no_replay(monkeypatch)
    assert cli.main(["bench", "--replay"]) == 1
    assert "this build has no pinned trace" in capsys.readouterr().err


def _doctored(monkeypatch, pinned_run, **changes):
    measured = dataclasses.replace(copy.deepcopy(pinned_run["measured"]), **changes)
    monkeypatch.setattr(session, "replay", lambda script, home: measured)
    return measured


def test_a_search_that_no_longer_finds_a_plant_differs_from_the_trace(monkeypatch, pinned_run):
    rows = copy.deepcopy(pinned_run["measured"].reach)
    lost = next(r for r in rows if r["cls"] == "chain" and r["reachable"])
    lost.update(reachable=False, rank=None, covered=False, best_rank=None, best_query=None)
    _doctored(monkeypatch, pinned_run, reach=rows)
    code, text, err = _run(["--replay"])
    assert code == 1
    totals = pinned_run["expected"]["reach"]
    differences = trace.differences(pinned_run["expected"],
                                    trace.expected(dataclasses.replace(pinned_run["measured"], reach=rows)))
    assert f"Pinned expectations: {len(differences)} differences." in text and len(differences) > 5
    result = text[text.index("Result: FAIL"):]
    for said in (f"differs from the pinned trace: reach.reachable: expected {totals['reachable']}, measured "
                 f"{totals['reachable'] - 1}",
                 f"differs from the pinned trace: reach.shown: expected {totals['shown']}, measured "
                 f"{totals['shown'] - 1}",
                 f"differs from the pinned trace in {len(differences) - 5} more places (results.json, "
                 "replay.differences)"):
        assert said in result, said
    assert err.startswith(f"The measurements differ from the pinned trace in {len(differences)} places:")
    assert f"  and {len(differences) - 5} more (all of them are in results.json" in err
    assert err.count("\n  reach") == 5 and "--record-trace" in err


def test_a_request_size_that_changed_differs_from_the_trace(monkeypatch, pinned_run):
    requests = copy.deepcopy(pinned_run["measured"].requests)
    requests[40]["uncached"] += 7
    _doctored(monkeypatch, pinned_run, requests=requests, rotations=pinned_run["expected"]["rotations"] + 1)
    code, text, err = _run(["--replay"])
    assert code == 1 and "Bound: held." in text and "Pinned expectations: 2 differences." in text
    count = pinned_run["expected"]["requests"]
    assert f"request_tokens: 1 of {count} differ; the first is number 41, expected " in text
    assert ("Requests 1 to 40 are equal and the difference starts at number 41: what the proxy sends changed from "
            "there") in text
    rotations = pinned_run["expected"]["rotations"]
    assert f"differs from the pinned trace: rotations: expected {rotations}, measured {rotations + 1}" in text
    assert err.startswith("The measurements differ from the pinned trace in 2 places:")


def test_a_failure_event_or_a_request_over_the_bound_fails_the_pinned_replay(monkeypatch, pinned_run):
    _doctored(monkeypatch, pinned_run, failure_events={"turn_room_low": 2})
    code, text, _ = _run(["--replay"])
    assert code == 1 and "failure events in the session log: turn_room_low (2)" in text
    assert "failure_events.turn_room_low: not in the trace, measured 2" in text
    assert "Pinned expectations: 1 difference." in text
    requests = copy.deepcopy(pinned_run["measured"].requests)
    requests[17]["uncached"] = 151_000
    _doctored(monkeypatch, pinned_run, requests=requests, over_bound=1)
    code, text, _ = _run(["--replay"])
    assert code == 1 and "Bound: NOT held." in text
    assert "1 requests were over the bound of 150,000 tokens by the fake upstream's count (largest 151,000)" in text
    _doctored(monkeypatch, pinned_run, compaction_markers=1, problems=["request 3 does not fit round 1 of turn 3"])
    code, text, _ = _run(["--replay"])
    assert code == 1 and "1 compaction markers in the requests" in text
    assert "the session did not go as scripted: request 3 does not fit round 1 of turn 3" in text


@pytest.mark.parametrize("variant", ["legacy", "bogus"])
def test_the_users_environment_does_not_change_the_pinned_replay(monkeypatch, tmp_path, variant):
    """Reviewer B's repro: CONTEXTO_RECALL_VARIANT=legacy made the pinned replay differ, and a mistyped
    value made it report that the bound did not hold. Every knob the package reads is held at its default."""
    settings = {"CONTEXTO_RECALL_VARIANT": variant, "CONTEXTO_SEARCH_CACHE_MB": "not-a-number",
                "OPENRIG_SESSION_NAME": "canary-session", "CONTEXTO_HOME": str(tmp_path / "canary-home"),
                "CONTEXTO_SESSION": "canary", "ANTHROPIC_BASE_URL": "http://canary.invalid",
                "CLAUDE_CONFIG_DIR": str(tmp_path / "canary-config"), "CONTEXTO_A_KNOB_ADDED_LATER": "1"}
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    out = tmp_path / "out"
    code, text, err = _run(["--replay", "--out", str(out)])
    assert code == 0 and err == "", text + err
    assert "Pinned expectations: every measurement equals the trace." in text
    environment = _load(out)["manifest"]["replay"]["environment"]
    assert set(session.ENVIRONMENT) | {"CONTEXTO_*"} == set(environment["pinned"])
    assert set(settings) <= set(environment["set_by_the_user_and_ignored"])
    for path in out.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"canary" not in data and b"not-a-number" not in data, path.name
    assert {name: os.environ.get(name) for name in settings} == settings  # put back after the replay


def test_a_proxy_that_breaks_the_tool_loop_stops_the_replay_with_the_rule_named(monkeypatch, capsys):
    """Reviewer A's repro: the engine drops every assistant message that holds a tool call. The real API
    refuses each request that then carries a tool result, and so does the fake upstream."""
    from contexto import rewrite

    real = rewrite.transform_messages

    def broken(messages, state):
        return [m for m in real(messages, state) if not (
            m.get("role") == "assistant" and isinstance(m.get("content"), list)
            and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m["content"]))]

    monkeypatch.setattr(rewrite, "transform_messages", broken)
    for argv in (SHORT, ["--seed", "20261017", "--protocol", "d27", "--replay-turns", "8"]):
        assert cli.main(["bench", "--replay", *argv]) == 1
        err = capsys.readouterr().err
        assert err.startswith("contexto bench: error: the replay stopped: the proxy answered 400 "
                              "(invalid_request_error: messages."), err
        assert "unexpected `tool_use_id` found in `tool_result` blocks" in err, err


def test_a_session_that_stops_is_an_error(monkeypatch, capsys):
    def stop(script, home):
        raise session.ReplayError("the proxy answered 502 (api_error: upstream unreachable)")

    monkeypatch.setattr(session, "replay", stop)
    assert cli.main(["bench", "--replay", *SHORT]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == ("contexto bench: error: the replay stopped: the proxy answered 502 (api_error: "
                                    "upstream unreachable)")


def test_ctrl_c_prints_one_line_and_exits_130(monkeypatch, capsys, tmp_path):
    """Reviewer B: a KeyboardInterrupt printed a traceback. The temporary store is removed on the way."""
    made = []

    def interrupted(script, home):
        made.append(home)
        raise KeyboardInterrupt

    monkeypatch.setattr(session, "replay", interrupted)
    assert cli.main(["bench", "--replay", *SHORT]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "contexto bench: the replay was stopped by Ctrl+C; its temporary store was removed.\n"
    assert made and not made[0].exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_a_signal_stops_the_replay_cleanly(tmp_path, signum):
    """A real signal to a real replay: one line, exit 128 + N, no traceback, and the temporary store and
    the ports are gone. Only the process this test starts is signalled."""
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {**os.environ, "TMPDIR": str(scratch), "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen([sys.executable, "-m", "contexto", "bench", "--replay"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 60
        while not list(scratch.glob("contexto-bench-replay-*/sessions/*/log.jsonl")):
            assert process.poll() is None and time.monotonic() < deadline, "the replay did not start"
            time.sleep(0.05)
        time.sleep(0.3)  # inside the session, with the proxy and the fake upstream serving
        process.send_signal(signum)
        out, err = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    name = "Ctrl+C" if signum == signal.SIGINT else signal.Signals(signum).name
    assert process.returncode == 128 + signum, (out, err)
    assert err == f"contexto bench: the replay was stopped by {name}; its temporary store was removed.\n"
    assert "Traceback" not in out + err
    assert list(scratch.iterdir()) == []


# Options


@pytest.mark.parametrize("argv, said", [
    (["--seeds", "2"], "--replay replays one session, so --seeds and --repeats do not apply"),
    (["--repeats", "3"], "--replay replays one session, so --seeds and --repeats do not apply"),
    (["--replay-turns", "6"], "the script is too short for protocol v2 at this bound"),
    (["--force"], "--force only applies to --record-trace"),
    (["--dump-trace", "d", "--record-trace", "f"], "give one of --dump-trace and --record-trace"),
])
def test_options_a_replay_cannot_use_are_usage_errors(monkeypatch, capsys, tmp_path, argv, said):
    _no_replay(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["bench", "--replay", *argv]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and said in captured.err
    assert list(tmp_path.iterdir()) == []  # nothing dumped, nothing recorded


LIVE_STUB = "FLAG = None\ndef add_arguments(parser):\n    pass\ndef run(args):\n    print('live run')\n    return 0\n"


@pytest.mark.parametrize("argv", [["--dump-trace", "DIR"], ["--record-trace", "trace.json"], ["--replay-turns", "8"],
                                  ["--force"]])
def test_a_replay_option_without_replay_is_refused(monkeypatch, capsys, tmp_path, argv):
    """B's low: an option only a replay takes never runs another mode, above all the live run, which would
    call the model with the user's key (the mode contract's OWN_OPTIONS)."""
    from contexto.bench import modes

    monkeypatch.chdir(tmp_path)
    said = f"contexto bench: error: {argv[0]} belongs to the replay mode; give --replay with it, or leave it out"
    assert argv[0] in replay_mode.OWN_OPTIONS
    assert cli.main(["bench", *argv]) == 2  # this build has no live run: refused either way
    capsys.readouterr()
    # With a live run installed, as the live slice adds one.
    (tmp_path / "t_live_stub.py").write_text(LIVE_STUB, encoding="utf-8")
    monkeypatch.setattr(modes, "__path__", [*modes.__path__, str(tmp_path)])
    monkeypatch.delitem(sys.modules, "contexto.bench.modes.t_live_stub", raising=False)
    assert cli.main(["bench", *argv]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip() == said
    assert cli.main(["bench", "--script-only", "--out", str(tmp_path / "scripts-out"), *argv]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip() == said
    assert not any((tmp_path / name).exists() for name in ("DIR", "trace.json", "scripts-out"))  # nothing written
    monkeypatch.delitem(sys.modules, "contexto.bench.modes.t_live_stub", raising=False)


@pytest.mark.parametrize("bound", ["20000", "25000", "170001", "200000"])
def test_a_bound_a_replay_cannot_take_names_the_range_it_takes(monkeypatch, capsys, bound):
    """Reviewer B: the refusal named `contexto run` options (--allow-large-bound, --headroom-floor) that
    `contexto bench` does not have."""
    opened = []
    monkeypatch.setattr(socket.socket, "bind", lambda sock, address: opened.append(address))
    assert cli.main(["bench", "--replay", "--bound", bound, *SHORT]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and opened == []  # refused before any server starts
    assert (f"--bound {int(bound):,} cannot be replayed: a replay takes a bound from 25,001 to 170,000 tokens"
            in captured.err)
    assert "--allow-large-bound" not in captured.err and "--headroom-floor" not in captured.err


@pytest.mark.parametrize("value", ["0", "3", "-1", "x", "4.5"])
def test_a_bad_turn_count_is_refused_by_the_parser(capsys, value):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["bench", "--replay", "--replay-turns", value])
    assert stopped.value.code == 2 and "invalid turn count" in capsys.readouterr().err


def test_the_help_names_the_mode_and_its_options(capsys):
    with pytest.raises(SystemExit):
        cli.main(["bench", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    for said in ("replay mode", "--replay ", "--replay-turns N", "--dump-trace DIR", "--record-trace FILE",
                 "--force", "no key, no claude, no network, no model call", "cannot show answer accuracy, cache reads",
                 "for maintainers"):
        assert said in text, said


@pytest.mark.parametrize("name", replay_mode.OUTPUT_NAMES)
def test_an_output_directory_that_holds_results_is_refused(monkeypatch, capsys, tmp_path, name):
    """Reviewer B: a replay into the --out of a paid live run replaced its results without a word."""
    _no_replay(monkeypatch)
    out = tmp_path / "results"
    out.mkdir()
    held = out / name
    if "." in name:
        held.write_text("from an earlier run\n", "utf-8")
    else:
        held.mkdir()
    assert cli.main(["bench", "--replay", *SHORT, "--out", str(out)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"{out} already holds {name}: a replay never overwrites results and starts from an empty store" in (
        captured.err)
    assert sorted(p.name for p in out.iterdir()) == [name]
    if "." in name:
        assert held.read_text("utf-8") == "from an earlier run\n"


def test_an_output_directory_with_other_files_is_used(tmp_path):
    out = tmp_path / "results"
    out.mkdir()
    (out / "README.txt").write_text("notes\n", "utf-8")
    code, _, err = _run(["--replay", *SHORT, "--out", str(out)])
    assert code == 0 and err == "" and (out / "results.json").exists()
    assert (out / "README.txt").read_text("utf-8") == "notes\n"


def test_dump_trace_writes_the_pinned_session_and_replays_nothing(monkeypatch, capsys, tmp_path):
    _no_replay(monkeypatch)
    assert cli.main(["bench", "--replay", "--dump-trace", str(tmp_path / "dump")]) == 0
    said = capsys.readouterr().out
    shipped = trace.load_packaged()
    turns, files = len(shipped["turn_sha256"]), len(shipped["files"])
    assert (f"{turns} turns, {files} files, {turns + files + 3} files in all; nothing was replayed (checked against "
            "the pinned trace's hashes)") in said
    dumped = sorted((tmp_path / "dump" / "turns").iterdir())
    assert [trace._sha(p.read_text("utf-8")) for p in dumped] == shipped["turn_sha256"]
    assert [len(p.read_bytes()) for p in dumped] == shipped["turn_bytes"]
    for name, pinned in shipped["files"].items():
        assert trace._sha((tmp_path / "dump" / "files" / name).read_text("utf-8")) == pinned["sha256"]
    assert Script.read(tmp_path / "dump" / "script.json").digest() == shipped["script_sha256"]
    steps = (tmp_path / "dump" / "model-steps.jsonl").read_text("utf-8").splitlines()
    assert len(steps) == shipped["model"]["steps"]
    # Not the pinned session: it is written all the same, and the line says so.
    assert cli.main(["bench", "--replay", *SHORT, "--dump-trace", str(tmp_path / "other")]) == 0
    assert "14 turns, 0 files, 17 files in all; nothing was replayed (not the pinned trace)" in capsys.readouterr().out


def test_record_trace_writes_a_trace_that_the_same_session_then_equals(monkeypatch, tmp_path, short_run):
    """The maintainer's path, on the short session: record, then replay against what was recorded."""
    path = tmp_path / "data" / "trace-20261016.json"
    code, text, err = _run(["--replay", *SHORT, "--record-trace", str(path)])
    assert code == 0 and err == ""
    assert text.splitlines()[0].endswith("(recording a trace)") and "This run records a trace" in text
    requests = len(short_run["measured"].requests)
    assert f"Trace written to {path}: {requests} requests, the largest " in text and "estimated tokens" in text
    recorded = trace.parse(path.read_text("utf-8"))
    assert path.read_text("utf-8") == trace.to_json(recorded)
    assert recorded["script"] == {"seed": 20261016, "protocol": "d27", "bound": 150_000, "turn_bytes": 40_000,
                                  "turns": 8}
    assert recorded["expected"] == trace.expected(short_run["measured"])  # the same as the earlier run of it
    assert recorded["recorded_with"]["contexto"] == __version__ and recorded["note"].endswith("Do not edit by hand.")
    assert set(recorded["recorded_with"]) == {"contexto", "commit", "python"}
    trace.check(recorded, short_run["script"], 8)
    # An existing file is replaced only with --force.
    before = path.read_bytes()
    code, _, err = _run(["--replay", *SHORT, "--record-trace", str(path)])
    assert code == 2 and "exists: --record-trace replaces a file only when --force is given" in err
    assert path.read_bytes() == before
    code, _, err = _run(["--replay", *SHORT, "--record-trace", str(path), "--force"])
    assert code == 0 and err == "" and trace.parse(path.read_text("utf-8"))["expected"] == recorded["expected"]
    # With that trace as the shipped one, the same options are the pinned replay, and it passes.
    monkeypatch.setattr(trace, "PINNED", recorded["script"])
    monkeypatch.setattr(trace, "load_packaged", lambda seed=0: recorded)
    code, text, err = _run(["--replay", *SHORT])
    assert code == 0 and err == "" and "(the pinned trace)" in text.splitlines()[0]
    assert "Pinned expectations: every measurement equals the trace." in text
    # And the default options are then not the pinned ones.
    monkeypatch.setattr(session, "replay", lambda script, home: short_run["measured"])
    code, text, _ = _run(["--replay"])
    assert "NOT the pinned trace" in text.splitlines()[0]


def test_record_trace_writes_nothing_for_a_run_that_failed(monkeypatch, tmp_path, short_run):
    """Reviewer A: a trace records a passing run, so a failed one is not pinned as expected."""
    measured = dataclasses.replace(copy.deepcopy(short_run["measured"]), failure_events={"turn_room_low": 1})
    monkeypatch.setattr(session, "replay", lambda script, home: measured)
    path = tmp_path / "trace.json"
    code, text, err = _run(["--replay", *SHORT, "--record-trace", str(path)])
    assert code == 1 and "Result: FAIL" in text and not path.exists()
    assert f"the run failed, so no trace was written to {path}: a trace records a passing run" in err
