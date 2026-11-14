"""``contexto bench --replay``: the replay itself, through the real proxy (D66).

``pinned_run`` replays the shipped trace once, with ``--out``, and the tests read what it printed and
wrote. It is the test that the proxy holds the bound on the pinned session, and its measurements must
be the trace's on every platform and Python version CI runs. ``short_run`` is a shorter session that
is not the pinned one. Neither needs ``claude``, a key or a network.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import socket
from pathlib import Path

import pytest

from contexto import __version__, cli
from contexto.bench import script as bench_script
from contexto.bench.model import Script
from contexto.bench.replay import lines, output, reach, scripted, session, trace
from contexto.store import SessionStore

SHORT = ["--protocol", "d27", "--replay-turns", "8"]  # 14 turns, 68 requests, about 3 seconds


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


@pytest.fixture(scope="module")
def pinned_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("replay-pinned") / "out"
    run = _watched(["--replay", "--out", str(out)])
    assert run["code"] == 0, run["stdout"] + run["stderr"]
    return {**run, "out": out, **_load(out)}


@pytest.fixture(scope="module")
def short_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("replay-short") / "out"
    run = _watched(["--replay", *SHORT, "--out", str(out)])
    assert run["code"] == 0, run["stdout"] + run["stderr"]
    return {**run, "out": out, **_load(out)}


# The pinned replay


def test_the_pinned_replay_holds_the_bound_and_equals_the_trace(pinned_run):
    expected = trace.load_packaged()["expected"]
    block = pinned_run["results"]["replay"]
    assert trace.expected(pinned_run["measured"]) == expected  # every pinned measurement, on this platform
    assert block["pinned"] is True and block["differences"] == [] and block["failures"] == [] and block["passed"]
    assert (block["turns"], block["requests"], block["rotations"], block["pages"]) == (56, 138, 13, 47)
    assert block["max_input_estimated"] == 116_113 < 150_000
    assert (block["over_bound"], block["over_bound_proxy"], block["compaction_markers"]) == (0, 0, 0)
    assert block["failure_events"] == {} and block["problems"] == [] and block["notices"] == []
    assert block["content_history_tokens"] == 952_957 and block["history_tokens"] == 956_085
    assert block["head_tokens"] == 21_277
    assert block["cannot_show"] == ["answer accuracy", "cache reads", "real token counts", "what a real CLI does",
                                    "what a real model does"]
    assert block["seconds"] < 60
    assert pinned_run["stderr"] == ""


def test_the_replay_connects_to_loopback_only(pinned_run, short_run):
    for run in (pinned_run, short_run):
        assert len(run["connections"]) >= 2 * len(run["measured"].requests)  # client to proxy, proxy to upstream
        assert {address[0] for address in run["connections"]} == {"127.0.0.1"}
        assert set(run["lookups"]) <= {"127.0.0.1"}  # no name is ever resolved


def test_the_engine_ran_as_in_a_launch_and_logged_nothing_that_fails(pinned_run):
    events = pinned_run["results"]["replay"]["events"]
    assert events["request"] == 138 and events["rotation"] == events["page_out"] == 13
    assert events["mcp_call"] == 72 and events["launch"] == events["proxy_start"] == events["proxy_stop"] == 1
    assert not set(events) & set(session.FAILURE_EVENTS)
    # The proxy's stream hold ran (the fake upstream streams chunked), and no request was forwarded untransformed.
    assert not {"hold_skipped", "proxy_error", "transform_error", "upstream_4xx", "over_bound"} & set(events)
    # Diagnostics that are true of a synthetic client and are not failures: the first request is sized
    # before any usage is known, and the client is not a CLI whose session start the proxy knows.
    assert events["estimate_error"] == 1 and events["session_start_unrecognised"] == 1
    assert pinned_run["measured"].toolloop_reads == 10 and pinned_run["measured"].searches == 72
    assert pinned_run["measured"].probe_turns_completed == 8


def test_the_terminal_text_says_first_what_a_replay_shows_and_what_it_cannot(pinned_run):
    text = pinned_run["stdout"]
    head = text.splitlines()[:14]
    assert head[0] == "contexto bench --replay: seed 20261016, protocol v2, bound 150,000 tokens (the pinned trace)"
    joined = "\n".join(head)
    for said in ("No model is called", "Sizes are estimates, not token counts: 1.8 bytes per token for pasted log text "
                 "and 3.0 for everything else", "What this replay shows:", "Bound: held. 138 requests, the largest "
                 "116,113 estimated tokens against the bound of 150,000", "Compaction: none.", "There is no CLI in a "
                 "replay", "the store's search returns 48 of 72 planted facts", "What it cannot show:",
                 "Answer accuracy, cache reads, real token counts, and what a real CLI or a real model does."):
        assert said in joined, said
    for said in ("56 turns (48 content, 8 probe)", "952,957 estimated tokens of history, 6.35 times the bound",
                 "13 rotations, 47 pages stored, failure events: none", "Modelled from the script, not run:",
                 "Not reachable by the rule's query: paraphrase, concluded, toolloop.",
                 "Pinned expectations: every measurement equals the trace.",
                 "Result: pass (the bound held, no failure event, measurements equal the pinned trace)",
                 "manifest.json, requests.jsonl, reach.jsonl, results.json, results.md"):
        assert said in text, said
    assert "NOT the pinned trace" not in text and "FAIL" not in text


def test_results_md_says_the_same_near_its_top(pinned_run):
    text = pinned_run["markdown"]
    shows, cannot = text.index("## What this replay shows"), text.index("## What it cannot show")
    assert shows < cannot < text.index("## Measurements") and shows < 900
    for said in ("This is the pinned trace", "No model is called", "**The bound held.** 138 requests",
                 "- Answer accuracy: the answers in the conversation are scripted.", "- Cache reads:",
                 "- Real token counts:", "- What a real CLI or a real model does.",
                 "| Largest request, estimated tokens | 116,113 |", "| Rotations | 13 |", "| Failure events | none |",
                 "| chain | 30 | 27 | 27 | 27 | 30 | - |", "| concluded | 6 | 6 | 0 | 0 | 0 | 0 |",
                 "| all | 72 | 66 | 48 | 48 | 54 | - |", "## Modelled lines (not run)",
                 "They are not a run of Claude Code",
                 "M0-proxy-measurements.md", "Pass: the bound held"):
        assert said in text, said


# The output files, by the shared schema


def test_manifest_fields(pinned_run):
    manifest = pinned_run["manifest"]
    assert pinned_run["results"]["manifest"] == manifest
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
                        "toolloop_reads": 10, "probe_turns_completed": 8}
    for unit, arm in ((full, "full"), (plain, "plain")):
        assert (unit["arm"], unit["status"], unit["compactions"]) == (arm, "not_run", None)
        assert "modelled" in unit["reason"] and "not run" in unit["reason"]
    added = manifest["replay"]
    assert added["pinned"] is True and added["trace"] == "trace-20261016.json" and added["turns"] == 40
    assert added["script_sha256"] == trace.load_packaged()["script_sha256"]
    assert added["rates"] == {"log_bytes_per_token": 1.8, "other_bytes_per_token": 3.0, "log_text_min_bytes": 5_000}
    assert added["modelled"]["summary_tokens"] == 7_920 and added["modelled"]["trigger"] == 150_000
    assert set(manifest) == {*listed, "started", "finished", "units", "replay"}


REQUEST_FIELDS = {"arm": str, "seed": int, "repeat": int, "model": type(None), "n": int, "turn": int, "main": bool,
                  "history_tokens": int, "uncached": int, "cache_write": int, "cache_read": int, "output": int,
                  "status": int, "compaction": bool, "basis": str}


def test_requests_jsonl_has_the_replayed_arm_estimated_and_two_arms_modelled(pinned_run):
    rows, script = pinned_run["requests"], pinned_run["script"]
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
    assert [r["uncached"] for r in replayed] == trace.load_packaged()["expected"]["request_tokens"]
    assert [(r["turn"], r["round"]) for r in replayed] == [(s.turn, s.round) for s in scripted.steps(script)]
    history = lines.history_tokens(script)
    assert all(r["history_tokens"] == history[r["turn"]] and r["main"] and not r["compaction"] for r in replayed)
    assert replayed[-1]["history_tokens"] == bench_script.estimate(script)["tokens"]
    assert max(r["uncached"] for r in replayed) == 116_113 and all(r["uncached"] <= 150_000 for r in replayed)
    assert sum(r["rotation"] for r in replayed) == 13 and all(r["round"] == 0 for r in replayed if r["rotation"])
    # The proxy's own estimate is beside the fake upstream's count; once usage is known the two are close.
    assert all(type(r["proxy_estimate"]) is int for r in replayed)
    assert all(abs(r["proxy_estimate"] - r["uncached"]) < 0.15 * r["uncached"] for r in replayed[1:])
    # The modelled arms: the same rows the model gives for the measured head.
    assert by_arm["full"] == lines.full_rows(script, 21_277)
    assert by_arm["plain"] == lines.plain_rows(script, 21_277, 150_000)
    assert by_arm["full"][-1]["uncached"] == 21_277 + 956_085
    compactions = [r for r in by_arm["plain"] if r["compaction"]]
    assert len(compactions) == 8 and all(not r["main"] and r["output"] == 7_920 for r in compactions)
    assert max(r["uncached"] for r in by_arm["plain"] if r["main"]) <= 150_000


def test_reach_jsonl_rows(pinned_run):
    rows, script = pinned_run["reach"], pinned_run["script"]
    assert [r["id"] for r in rows] == [p.id for p in script.probes] and len(rows) == 72
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
    totals = reach.totals(rows)
    assert totals == pinned_run["results"]["replay"]["reach"] == trace.load_packaged()["expected"]["reach"]
    assert (totals["probes"], totals["paged"], totals["reachable"], totals["covered"]) == (72, 66, 48, 54)
    by_class = {cls: (c["probes"], c["reachable"], c["covered"]) for cls, c in totals["by_class"].items()}
    assert by_class == {"paged_only": (4, 4, 4), "multihop": (8, 7, 8), "chain": (30, 27, 30),
                        "obligations": (6, 4, 6), "distractor": (6, 6, 6), "paraphrase": (6, 0, 0),
                        "concluded": (6, 0, 0), "toolloop": (6, 0, 0)}
    # The classes whose questions do not name their subject: paged, and not found by the rule's query.
    for row in rows:
        if row["cls"] in ("paraphrase", "concluded", "toolloop"):
            assert row["paged"] and not row["reachable"] and not row["in_view"]
        if row["cls"] == "concluded":
            assert row["overlap"] == 0 and row["word_rank"] is None


def test_results_json_has_the_chart_series_the_bound_and_the_notes(pinned_run):
    data, rows = pinned_run["results"], pinned_run["requests"]
    assert (data["format"], data["kind"], data["bound"]) == (1, "replay", 150_000)
    series = data["request_series"]
    assert list(series) == ["contexto", "full", "plain"]
    assert [series[arm]["basis"] for arm in series] == ["estimated", "modelled", "modelled"]
    assert "estimated" in series["contexto"]["label"]
    assert all("modelled" in series[arm]["label"] for arm in ("full", "plain"))
    for arm in series:
        main = [r for r in rows if r["arm"] == arm and r["main"]]
        assert series[arm]["points"] == [[r["history_tokens"], r["uncached"]] for r in main]
    assert [len(series[arm]["points"]) for arm in series] == [138, 56, 56]
    notes = " ".join(data["notes"])
    for said in ("Synthetic", "No model was called", "estimates, not token counts", "modelled from the script, not run",
                 "is an assumption", "cannot show answer accuracy, cache reads, real token counts"):
        assert said in notes, said
    assert all(isinstance(note, str) for note in data["notes"])
    assert data["replay"]["modelled"] == {"full_max": 977_362, "plain_max": 140_412, "plain_compactions": 8}


def test_the_script_and_the_store_are_kept_with_out(pinned_run):
    out, script = pinned_run["out"], pinned_run["script"]
    assert Script.read(out / "scripts" / "seed-20261016.json") == script
    assert sorted(p.name for p in out.iterdir()) == ["manifest.json", "reach.jsonl", "requests.jsonl", "results.json",
                                                     "results.md", "scripts", "store"]
    store = SessionStore.open_existing(out / "store", "bench-replay-20261016")
    assert len(store.page_ids()) >= 47  # the pages at the first probe turn, and any paged after it
    probe = next(p for p in script.probes if p.cls == "paged_only")
    (page_id,) = store.search(probe.subjects[0])
    assert probe.statements[0] in json.dumps(store.get_page(page_id)["messages"])
    assert "store/ (session bench-replay-20261016)" in pinned_run["stdout"]


def test_no_path_of_this_machine_is_in_any_output_file(pinned_run):
    out = pinned_run["out"]
    private = [str(Path.home()).encode(), str(out.parent).encode(), str(Path.cwd()).encode()]
    files = [p for p in out.rglob("*") if p.is_file()]
    assert len(files) > 50
    for path in files:
        data = path.read_bytes()
        for needle in private:
            assert needle not in data, (path.name, needle)
    meta = out / "store" / "sessions" / "bench-replay-20261016" / "meta.json"
    assert b"/contexto-bench-replay" in meta.read_bytes()  # the fixed name the store records as its directory


# A session that is not the pinned one


def test_a_replay_with_other_options_runs_and_says_it_is_not_the_pinned_trace(short_run):
    text, block = short_run["stdout"], short_run["results"]["replay"]
    assert text.splitlines()[0].endswith("protocol d27, bound 150,000 tokens (NOT the pinned trace)")
    said = "nothing was checked against recorded hashes or expected measurements"
    assert said in "\n".join(text.splitlines()[:8]) and "Pinned expectations" not in text
    assert "Result: pass (the bound held, no failure event)" in text
    assert block["pinned"] is False and block["differences"] == [] and block["passed"]
    assert (block["turns"], block["requests"], block["over_bound"], block["failure_events"]) == (14, 68, 0, {})
    manifest = short_run["manifest"]
    assert manifest["protocol"] == "d27" and manifest["replay"]["pinned"] is False
    assert manifest["replay"]["trace"] is None and manifest["replay"]["checked"].startswith("nothing")
    assert manifest["units"][0]["toolloop_reads"] is None  # a d27 session has no tool-loop turn
    assert "not the pinned trace" in short_run["results"]["notes"][-1]
    assert "**This is not the pinned trace" in short_run["markdown"]
    assert len(short_run["reach"]) == 54 and block["reach"]["covered"] == 54


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
    assert "the store's search returns 0 of 54 planted facts by the fixed query rule" in text
    assert short_run["results"]["replay"]["reach"]["reachable"] > 0


def test_a_bound_the_session_does_not_fit_fails_and_says_why():
    code, text, err = _run(["--replay", "--protocol", "d27", "--replay-turns", "4", "--bound", "30000"])
    assert code == 1 and err == ""
    assert ("Bound: NOT held. 64 requests, the largest 45,009 estimated tokens against the bound of 30,000; 4 over "
            "the bound by the fake upstream's count, 4 by the proxy's own log.") in text
    assert "4 requests at the size at which a CLI compacts" in text and "Compaction: none" not in text
    result = text[text.index("Result: FAIL"):]
    for said in ("4 requests were over the bound of 30,000 tokens by the fake upstream's count (largest 45,009)",
                 "the proxy's own log marks 4 requests over the bound",
                 "failure events in the session log: turn_room_low (10)"):
        assert said in result, said
    assert "bound 30,000 is too small" in text  # the launcher's own warning, as `contexto run` prints it


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
    assert captured.err.startswith("contexto bench: error: the generator and the pinned trace disagree: turn 12 of 56 ")
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
    assert captured.out == "" and "the generator and the pinned trace disagree: turn 48 of 56" in captured.err
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
    lost.update(reachable=False, rank=None, covered=False)
    _doctored(monkeypatch, pinned_run, reach=rows)
    code, text, err = _run(["--replay"])
    assert code == 1
    assert "Pinned expectations: 9 differences." in text
    result = text[text.index("Result: FAIL"):]
    for said in ("differs from the pinned trace: reach.reachable: expected 48, measured 47",
                 "differs from the pinned trace: reach.neither: expected 18, measured 19",
                 "differs from the pinned trace: reach.by_class.chain.reachable: expected 27, measured 26",
                 "differs from the pinned trace: reach_sha256: expected "):
        assert said in result, said
    assert "The measurements differ from the pinned trace." in err and "--record-trace" in err


def test_a_request_size_that_changed_differs_from_the_trace(monkeypatch, pinned_run):
    requests = copy.deepcopy(pinned_run["measured"].requests)
    requests[40]["uncached"] += 7
    _doctored(monkeypatch, pinned_run, requests=requests, rotations=14)
    code, text, _ = _run(["--replay"])
    assert code == 1 and "Bound: held." in text
    assert "request_tokens: 1 of 138 differ; the first is number 41, expected " in text
    assert "differs from the pinned trace: rotations: expected 13, measured 14" in text


def test_a_failure_event_or_a_request_over_the_bound_fails_the_pinned_replay(monkeypatch, pinned_run):
    _doctored(monkeypatch, pinned_run, failure_events={"turn_room_low": 2})
    code, text, _ = _run(["--replay"])
    assert code == 1 and "failure events in the session log: turn_room_low (2)" in text
    assert "failure_events.turn_room_low: not in the trace, measured 2" in text
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
    import os
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
        assert "roles must alternate" in err or "unexpected `tool_use_id`" in err, err


def test_a_session_that_stops_is_an_error(monkeypatch, capsys):
    def stop(script, home):
        raise session.ReplayError("the proxy answered 502: upstream unreachable")

    monkeypatch.setattr(session, "replay", stop)
    assert cli.main(["bench", "--replay", *SHORT]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == ("contexto bench: error: the replay stopped: the proxy answered 502: upstream "
                                    "unreachable")


# Options


@pytest.mark.parametrize("argv, said", [
    (["--seeds", "2"], "--replay replays one session, so --seeds and --repeats do not apply"),
    (["--repeats", "3"], "--replay replays one session, so --seeds and --repeats do not apply"),
    (["--replay-turns", "6"], "the script is too short for protocol v2 at this bound"),
])
def test_options_a_replay_cannot_use_are_usage_errors(monkeypatch, capsys, argv, said):
    _no_replay(monkeypatch)
    assert cli.main(["bench", "--replay", *argv]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and said in captured.err


@pytest.mark.parametrize("bound, said", [
    ("20000", "the proxy does not start with this bound: --bound 20,000 is not above the headroom floor"),
    ("200000", "the proxy does not start with this bound: --bound 200000 is above 170,000"),
])
def test_a_bound_the_launcher_refuses_is_refused_in_its_words(monkeypatch, capsys, bound, said):
    """The replay starts the proxy through the launcher's own checks, so `contexto run`'s limits apply."""
    opened = []
    monkeypatch.setattr(socket.socket, "bind", lambda sock, address: opened.append(address))
    assert cli.main(["bench", "--replay", "--bound", bound, *SHORT]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and said in captured.err and opened == []  # refused before any server starts


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
                 "no key, no claude, no network, no model call", "cannot show answer accuracy, cache reads",
                 "for maintainers"):
        assert said in text, said


def test_an_output_directory_that_already_has_a_store_is_refused(monkeypatch, capsys, short_run):
    _no_replay(monkeypatch)
    before = (short_run["out"] / "results.json").read_bytes()
    assert cli.main(["bench", "--replay", *SHORT, "--out", str(short_run["out"])]) == 2
    assert "already exists: a replay starts from an empty store" in capsys.readouterr().err
    assert (short_run["out"] / "results.json").read_bytes() == before


def test_dump_trace_writes_the_pinned_session_and_replays_nothing(monkeypatch, capsys, tmp_path):
    _no_replay(monkeypatch)
    assert cli.main(["bench", "--replay", "--dump-trace", str(tmp_path / "dump")]) == 0
    said = capsys.readouterr().out
    assert ("56 turns, 10 files, 69 files in all; nothing was replayed (checked against the pinned trace's "
            "hashes)") in said
    shipped = trace.load_packaged()
    turns = sorted((tmp_path / "dump" / "turns").iterdir())
    assert [trace._sha(p.read_text("utf-8")) for p in turns] == shipped["turn_sha256"]
    assert [len(p.read_bytes()) for p in turns] == shipped["turn_bytes"]
    for name, pinned in shipped["files"].items():
        assert trace._sha((tmp_path / "dump" / "files" / name).read_text("utf-8")) == pinned["sha256"]
    assert Script.read(tmp_path / "dump" / "script.json").digest() == shipped["script_sha256"]
    assert len((tmp_path / "dump" / "model-steps.jsonl").read_text("utf-8").splitlines()) == 138
    # Not the pinned session: it is written all the same, and the line says so.
    assert cli.main(["bench", "--replay", *SHORT, "--dump-trace", str(tmp_path / "other")]) == 0
    assert "14 turns, 0 files, 17 files in all; nothing was replayed (not the pinned trace)" in capsys.readouterr().out


def test_record_trace_writes_a_trace_that_the_same_session_then_equals(monkeypatch, tmp_path, short_run):
    """The maintainer's path, on the short session: record, then replay against what was recorded."""
    path = tmp_path / "data" / "trace-20261016.json"
    code, text, err = _run(["--replay", *SHORT, "--record-trace", str(path)])
    assert code == 0 and err == ""
    assert text.splitlines()[0].endswith("(recording a trace)") and "This run records a trace" in text
    assert f"Trace written to {path}: 68 requests" in text
    recorded = trace.parse(path.read_text("utf-8"))
    assert path.read_text("utf-8") == trace.to_json(recorded)
    assert recorded["script"] == {"seed": 20261016, "protocol": "d27", "bound": 150_000, "turn_bytes": 40_000,
                                  "turns": 8}
    assert recorded["expected"] == trace.expected(short_run["measured"])  # the same as the earlier run of it
    assert recorded["recorded_with"]["contexto"] == __version__ and recorded["note"].endswith("Do not edit by hand.")
    trace.check(recorded, short_run["script"], 8)
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


def test_output_helpers_agree_with_what_was_written(pinned_run):
    script, measured = pinned_run["script"], pinned_run["measured"]
    assert output.request_rows(script, measured) == pinned_run["requests"]
    assert output.failures(script, measured, []) == []
    assert output.markdown(script, pinned_run["results"]) == pinned_run["markdown"]
    assert "\n".join(output.summary(script, pinned_run["results"])) in pinned_run["stdout"]
