"""The report reads what the live run writes: retried sessions and responses whose usage could not be read (D66).

The live run (``contexto.bench.runner`` and ``contexto.bench.meter``) numbers each attempt of a
unit's requests from 0, keeps every attempt's records in ``requests.jsonl`` and names the last
attempt in the unit's manifest entry. A response whose usage the meter could not read in full is
recorded with ``usage_unreadable: true`` and ``reserved_tokens``, the reservation the spend guard
counted for it. The builders here write records in exactly those shapes (``live_unit`` is
``runner.unit_entry``'s, ``live_requests`` the meter's), so the two slices cannot drift apart
unnoticed. No arm is run and nothing is sent anywhere.
"""

from __future__ import annotations

import json

import pytest

from contexto.bench import prices, records, report
from test_bench_records import MODEL, SMALL_BOUND, probe_rows, request_rows, small_script, write_run

TABLE = prices.load()


def live_requests(s, arm: str, attempt: int, repeat: int = 0, count: int | None = None) -> list[dict]:
    """The records the meter writes for one attempt of a unit: ``n`` from 0, ``attempt`` on every
    record. ``count`` cuts the attempt short after that many requests (a stop)."""
    rows = request_rows(s, arm, repeat)[0]
    rows = rows if count is None else rows[:count]
    return [{**row, "n": n, "attempt": attempt} for n, row in enumerate(rows)]


# The keys of a record of contexto.bench.meter (origin/claude/bench-live), in the order it writes them.
METER_KEYS = ("arm", "seed", "repeat", "model", "n", "turn", "main", "history_tokens", "uncached", "cache_write",
              "cache_read", "output", "status", "compaction", "basis", "attempt")


def meter_requests(s, arm: str, attempt: int, unreachable: tuple[int, ...] = (), repeat: int = 0) -> list[dict]:
    """The records the meter writes for one attempt when the requests numbered ``unreachable`` got
    no response. The meter takes ``n`` for a request before forwarding it and writes a record only
    for one that got a response, so those numbers are missing; Claude Code sends the request again
    and its record takes the next number."""
    out, n = [], 0
    for row in request_rows(s, arm, repeat)[0]:
        while n in unreachable:
            n += 1
        record = {**row, "n": n, "attempt": attempt}
        assert set(record) == set(METER_KEYS)
        out.append({key: record[key] for key in METER_KEYS})
        n += 1
    return out


def unreadable(row: dict, reserved: int | None) -> dict:
    """A record whose usage the meter could not read: the counts it did read (none here), the flag,
    and the reservation, as ``meter._Handler._handle`` writes them."""
    out = {**row, "uncached": 0, "cache_write": 0, "cache_read": 0, "output": 0, "usage_unreadable": True}
    if reserved is not None:
        out["reserved_tokens"] = reserved
    return out


def _tokens(rows: list[dict]) -> int:
    return sum(r["uncached"] + r["cache_write"] + r["cache_read"] + r["output"] for r in rows)


def live_unit(arm: str, seed: int, attempts: list[tuple[str, str | None, list[dict]]], repeat: int = 0,
              reason: str | None = None) -> dict:
    """A manifest entry as ``runner.unit_entry`` writes it. ``attempts`` lists each attempt's
    status, stop and records; the last one is the unit's own."""
    status, stop, rows = attempts[-1]
    cli = arm != "full"
    sized = [r["uncached"] + r["cache_write"] + r["cache_read"] for r in rows if r["main"] or r["compaction"]]
    return {
        "arm": arm, "seed": seed, "repeat": repeat, "status": status, "reason": reason,
        "cli_exit": 0 if cli else None, "compactions": 0 if cli else None, "compaction_pre_tokens": [],
        "concluded_ok": {}, "toolloop_reads": 10 if cli else None, "probe_turns_completed": 8,
        "stop": stop, "attempt": len(attempts) - 1,
        "attempts": [{"attempt": i, "status": st, "stop": sp, "requests": len(rs), "tokens": _tokens(rs)}
                     for i, (st, sp, rs) in enumerate(attempts)],
        "turns_completed": len({r["turn"] for r in rows}), "requests": len(rows), "tokens": _tokens(rows),
        "max_input": max(sized, default=0), "over_bound": 0, "compaction_requests": 0, "compaction_tokens": 0,
        "compaction_max_input": 0, "usage_unreadable": sum(1 for r in rows if r.get("usage_unreadable")),
        "token_cap": 1_000_000, "toolloop_requests": None, "toolloop_max_input": None,
        "toolloop_tool_calls": None,
    }


def retried_run(out, seeds=(1, 2, 3)):
    """Seeds 1 to 3, arms contexto and full. The Contexto session of seed 1 was stopped by the
    budget after 3 requests (attempt 0) and completed when the command was run again (attempt 1)."""
    units, probes, requests = [], [], []
    for seed in seeds:
        s = small_script(seed)
        for arm in ("contexto", "full"):
            if (arm, seed) == ("contexto", 1):
                first = live_requests(s, arm, 0, count=3)
                second = live_requests(s, arm, 1)
                units.append(live_unit(arm, seed, [("stopped", "budget", first), ("complete", None, second)]))
                requests += first + second
            else:
                rows = live_requests(s, arm, 0)
                units.append(live_unit(arm, seed, [("complete", None, rows)]))
                requests += rows
            probes += probe_rows(s, arm, 0, set())
    return write_run(out, units, probes, requests, arms=["contexto", "full"])


# Retried sessions


def test_a_session_retried_after_a_budget_stop_is_read_and_reported(tmp_path):
    out = retried_run(tmp_path / "run")
    run = records.read_run(out)
    assert len([r for r in run.requests if r["arm"] == "contexto" and r["seed"] == 1]) == 3 + len(
        small_script(1).turns)
    report.write(out)  # the documented path after a stop: run the command again, then read the report
    assert (out / "results.md").exists()


def test_every_attempt_counts_in_tokens_and_cost_and_the_retries_are_reported(tmp_path):
    out = retried_run(tmp_path / "run")
    run = records.read_run(out)
    results = report.build([out])
    block = results["arms"]["contexto"]
    every = [r for r in run.requests if r["arm"] == "contexto"]
    stopped = [r for r in every if r["seed"] == 1 and r["attempt"] == 0]
    assert len(stopped) == 3
    assert block["tokens"]["requests"] == len(every)
    assert block["tokens"]["input"] == sum(r["uncached"] + r["cache_write"] + r["cache_read"] for r in every)
    assert block["cost"][MODEL] == pytest.approx(prices.cost(every, MODEL, "5m", TABLE))
    retries = {k: v for k, v in block["retries"].items() if k not in ("cost", "cost_upper")}
    assert retries == {"sessions": 1, "attempts": 1, "requests": 3, "tokens": _tokens(stopped), "unreadable": 0}
    for model in prices.models(TABLE):
        assert block["retries"]["cost"][model] == pytest.approx(prices.cost(stopped, model, "5m", TABLE))
        completed = [r for r in every if not (r["seed"] == 1 and r["attempt"] == 0)]
        assert block["cost_completed_per_unit"][model] == pytest.approx(
            prices.cost(completed, model, "5m", TABLE) / 3)
    assert block["retries"]["cost_upper"] == block["retries"]["cost"]
    full = results["arms"]["full"]
    assert {k: v for k, v in full["retries"].items() if k not in ("cost", "cost_upper")} == {
        "sessions": 0, "attempts": 0, "requests": 0, "tokens": 0, "unreadable": 0}
    assert set(full["retries"]["cost"].values()) == {0}
    assert full["cost_completed_per_unit"] == full["cost_per_unit"]
    # Accuracy is the completed attempt's probes, once per session.
    assert (block["units"], block["probes"]) == (3, 3 * 72)
    text = report.render(results)
    section = text.split("## Tokens and requests")[1].split("## Cost at published prices")[0]
    usd = report._usd(block["retries"]["cost"][MODEL])
    assert f"| Contexto | 3 | 1 | 1 | 3 | {_tokens(stopped):,} | {usd} | 0 | 0 |" in section
    assert f"| Full context | 3 | 0 | 0 | 0 | 0 | {report._usd(0)} | 0 | 0 |" in section
    assert f"Their cost at {MODEL} prices" in section
    assert "every attempt of a session is counted, because every attempt was billed" in section
    cost = text.split("## Cost at published prices")[1]
    assert (f"- Contexto: 1 session was retried; the cost includes the 1 attempt that did not complete (3 requests, "
            f"{usd} at the prices of {MODEL}), because they were billed. Without them the cost per session is "
            f"{report._usd(block['cost_completed_per_unit'][MODEL])} at those prices.") in cost
    assert "retried attempts included" in cost


def test_the_request_series_is_drawn_from_the_attempt_that_completed(tmp_path):
    out = retried_run(tmp_path / "run")
    run = records.read_run(out)
    series = report.build([out])["request_series"]["contexto"]
    final = [r for r in run.requests if (r["arm"], r["seed"], r["attempt"]) == ("contexto", 1, 1) and r["main"]]
    assert series["points"] == [[r["history_tokens"], r["uncached"] + r["cache_write"] + r["cache_read"]]
                                for r in final]


def test_a_request_number_repeated_within_one_attempt_is_still_an_error(tmp_path):
    out = retried_run(tmp_path / "run")
    path = out / "requests.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    index = next(i for i, r in enumerate(rows) if (r["arm"], r["seed"], r["attempt"], r["n"]) == ("contexto", 1, 1, 2))
    rows[index]["n"] = 1
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    with pytest.raises(records.RecordError, match="the request 1 appears twice for the unit contexto, seed 1, "
                                                  "repeat 0, attempt 1"):
        records.read_run(out)


def test_a_record_lost_from_an_earlier_attempt_shows_in_that_attempts_count(tmp_path):
    out = retried_run(tmp_path / "run")
    path = out / "requests.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if (r["arm"], r["seed"], r["attempt"], r["n"]) != ("contexto", 1, 0, 1)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    with pytest.raises(records.RecordError, match="contexto, seed 1, repeat 0, attempt 0 has 2 request records and "
                                                  "manifest.json says 3"):
        records.read_run(out)


def gap_run(out, counts: bool = True, lose: int | None = None):
    """Seeds 1 to 3, arms contexto and full, in the meter's shape. The full-context session of seed
    3 had request 1 go unanswered (the upstream reset the connection), so its numbers are 0, 2, 3,
    ...; attempt 0 of Contexto's seed-1 session (stopped by the budget) lost request 2 the same way.
    ``counts=False`` leaves out every count the runner writes; ``lose`` drops the record numbered so
    from the full-context session after its count was taken."""
    units, probes, requests = [], [], []
    for seed in (1, 2, 3):
        s = small_script(seed)
        for arm in ("contexto", "full"):
            if (arm, seed) == ("contexto", 1):
                first = meter_requests(s, arm, 0, unreachable=(2,))[:3]
                second = meter_requests(s, arm, 1)
                entry = live_unit(arm, seed, [("stopped", "budget", first), ("complete", None, second)])
                rows = first + second
            else:
                rows = meter_requests(s, arm, 0, unreachable=(1,) if (arm, seed) == ("full", 3) else ())
                entry = live_unit(arm, seed, [("complete", None, rows)])
                if lose is not None and (arm, seed) == ("full", 3):
                    rows = [r for r in rows if r["n"] != lose]
            if not counts:
                del entry["requests"]
                for a in entry["attempts"]:
                    del a["requests"]
            units.append(entry)
            requests += rows
            probes += probe_rows(s, arm, 0, set())
    return write_run(out, units, probes, requests, arms=["contexto", "full"])


def test_a_request_that_got_no_response_leaves_a_gap_in_n_and_the_run_is_still_reported(tmp_path):
    out = gap_run(tmp_path / "run")
    run = records.read_run(out)
    numbers = [r["n"] for r in run.requests if (r["arm"], r["seed"]) == ("full", 3)]
    assert numbers[:4] == [0, 2, 3, 4] and len(numbers) == len(small_script(3).turns)
    assert [r["n"] for r in run.requests if (r["arm"], r["seed"], r["attempt"]) == ("contexto", 1, 0)] == [0, 1, 3]
    assert run.unchecked == ()  # every attempt carries its count
    results = report.write(out)
    assert results["unchecked"] == []
    every = [r for r in run.requests if r["arm"] == "full"]
    assert results["arms"]["full"]["tokens"]["requests"] == len(every)
    assert "cannot be detected" not in (out / "results.md").read_text(encoding="utf-8")


def test_with_a_count_a_record_lost_beside_a_gap_is_still_an_error(tmp_path):
    turns = len(small_script(3).turns)
    out = gap_run(tmp_path / "run", lose=turns)  # the last record of the session
    with pytest.raises(records.RecordError, match=f"the unit full, seed 3, repeat 0 has {turns - 1} request records "
                                                  f"and manifest.json says {turns}: the file was cut short"):
        records.read_run(out)


def test_without_a_count_the_report_says_a_cut_file_cannot_be_detected(tmp_path):
    out = gap_run(tmp_path / "run", counts=False, lose=len(small_script(3).turns))
    run = records.read_run(out)  # nothing shows the lost record
    assert len(run.unchecked) == 6
    assert run.unchecked[0] == (("contexto", 1, 0), (0, 1))
    results = report.write(out)
    assert results["unchecked"][0] == {"model": MODEL, "arm": "contexto", "seed": 1, "repeat": 0, "attempts": [0, 1]}
    text = (out / "results.md").read_text(encoding="utf-8")
    assert ("- The manifest gives no count of request records for any session, so a `requests.jsonl` cut short at "
            "the end of a session cannot be detected.") in text
    # One session without a count is named.
    out = gap_run(tmp_path / "one")
    data = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    entry = next(u for u in data["units"] if (u["arm"], u["seed"]) == ("full", 3))
    del entry["requests"], entry["attempts"][0]["requests"]
    (out / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    text = report.render(report.build([out]))
    assert ("- The manifest gives no count of request records for 1 session (full context, seed 3, repeat 0), so a "
            "`requests.jsonl` cut short at the end of it cannot be detected. Every other session's count was "
            "checked.") in text


def test_the_units_counts_are_checked_against_the_attempt_they_belong_to(tmp_path):
    out = retried_run(tmp_path / "run")
    data = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    entry = next(u for u in data["units"] if (u["arm"], u["seed"]) == ("contexto", 1))
    entry["attempts"][0]["requests"] = 4
    (out / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(records.RecordError, match="attempt 0 has 3 request records and manifest.json says 4"):
        records.read_run(out)


def test_a_record_of_an_attempt_the_manifest_does_not_reach_is_an_error(tmp_path):
    out = retried_run(tmp_path / "run")
    data = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    entry = next(u for u in data["units"] if (u["arm"], u["seed"]) == ("contexto", 2))
    s = small_script(2)
    extra = live_requests(s, "contexto", 1, count=1)
    with (out / "requests.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(extra[0]) + "\n")
    assert entry["attempt"] == 0
    with pytest.raises(records.RecordError, match="attempt 1, but manifest.json names attempt 0 as the last"):
        records.read_run(out)


def test_a_unit_left_running_says_the_run_was_interrupted_and_how_to_resume(tmp_path):
    out = retried_run(tmp_path / "run")
    data = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    entry = next(u for u in data["units"] if (u["arm"], u["seed"]) == ("full", 3))
    entry["status"] = "running"
    (out / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    probes = [json.loads(line) for line in (out / "probes.jsonl").read_text(encoding="utf-8").splitlines()]
    (out / "probes.jsonl").write_text("".join(json.dumps(p) + "\n" for p in probes
                                              if (p["arm"], p["seed"]) != ("full", 3)), encoding="utf-8")
    with pytest.raises(records.RecordError) as caught:
        records.read_run(out)
    assert "the session full, seed 3, repeat 0, attempt 0 is 'running'" in str(caught.value)
    assert caught.value.field == f"units[{data['units'].index(entry)}].status"
    assert "was interrupted while this session ran" in str(caught.value)
    assert "the same command with the same --out resumes it" in str(caught.value)


def test_records_without_an_attempt_are_attempt_0(tmp_path):
    s = small_script(1)
    rows = [{k: v for k, v in r.items() if k != "attempt"} for r in live_requests(s, "contexto", 0)]
    entry = live_unit("contexto", 1, [("complete", None, rows)])
    del entry["attempt"], entry["attempts"]
    out = write_run(tmp_path / "run", [entry], probe_rows(s, "contexto", 0, set()), rows, arms=["contexto"])
    assert report.build([out])["arms"]["contexto"]["retries"]["sessions"] == 0


# Usage that could not be read


def unreadable_run(out, reserved: int | None = 160_000):
    """Seeds 1 to 3, arms contexto and full; one Contexto request of seed 1 has unreadable usage."""
    units, probes, requests = [], [], []
    for seed in (1, 2, 3):
        s = small_script(seed)
        for arm in ("contexto", "full"):
            rows = live_requests(s, arm, 0)
            if (arm, seed) == ("contexto", 1):
                rows[4] = unreadable(rows[4], reserved)
            units.append(live_unit(arm, seed, [("complete", None, rows)]))
            requests += rows
            probes += probe_rows(s, arm, 0, set())
    return write_run(out, units, probes, requests, arms=["contexto", "full"])


def highest_rate(model: str) -> float:
    entry = TABLE["models"][model]
    return max(row[rate] for row in entry["rows"] for rate in prices.RATES)


def test_a_request_with_unreadable_usage_is_counted_apart_and_bounds_the_cost(tmp_path):
    out = unreadable_run(tmp_path / "run")
    run = records.read_run(out)
    results = report.build([out])
    block = results["arms"]["contexto"]
    every = [r for r in run.requests if r["arm"] == "contexto"]
    read = [r for r in every if not r.get("usage_unreadable")]
    assert len(read) == len(every) - 1
    assert block["unreadable"] == {"requests": 1, "reserved_tokens": 160_000, "without_reservation": 0}
    assert block["tokens"]["requests"] == len(read)  # the measured totals leave it out
    assert block["tokens"]["input"] == sum(r["uncached"] + r["cache_write"] + r["cache_read"] for r in read)
    for model in prices.models(TABLE):
        low = prices.cost(read, model, "5m", TABLE)
        assert block["cost"][model] == pytest.approx(low)
        assert block["cost_upper"][model] == pytest.approx(low + 160_000 * highest_rate(model) / 1e6)
        assert prices.reservation_cost(160_000, model, TABLE) == pytest.approx(160_000 * highest_rate(model) / 1e6)
    assert results["arms"]["full"]["unreadable"]["requests"] == 0
    assert results["arms"]["full"]["cost_upper"] == results["arms"]["full"]["cost"]
    assert results["all_requests"]["unreadable"]["requests"] == 1
    text = report.render(results)
    tokens = text.split("## Tokens and requests")[1].split("## Cost at published prices")[0]
    assert "The counts are measured: the upstream reported them." not in tokens
    assert "1 request whose usage could not be read" in tokens
    assert f"| Contexto | 3 | 0 | 0 | 0 | 0 | {report._usd(0)} | 1 | 160,000 |" in tokens
    cost = text.split("## Cost at published prices")[1]
    low, high = block["cost_per_unit"][MODEL], block["cost_upper_per_unit"][MODEL]
    assert f"{report._usd(low)} to {report._usd(high)}" in cost
    assert "upper bound" in cost


def test_an_unreadable_records_partial_read_counts_toward_its_reservation_and_nothing_else(tmp_path):
    # The partial read is larger than any read request of the arm, over the bound and larger than the
    # reservation, and its basis differs from the others'. Each figure must leave it out, except the
    # reservation, which is the larger of the two (as the live run's guard counts it).
    units, probes, requests = [], [], []
    for seed in (1, 2, 3):
        s = small_script(seed)
        rows = live_requests(s, "contexto", 0)
        if seed == 2:
            rows[3] = {**rows[3], "uncached": SMALL_BOUND + 1_000, "cache_write": 0, "cache_read": 0, "output": 100,
                       "basis": "estimated", "usage_unreadable": True, "reserved_tokens": 1_000}
        units.append(live_unit("contexto", seed, [("complete", None, rows)]))
        requests += rows
        probes += probe_rows(s, "contexto", 0, set())
    out = write_run(tmp_path / "run", units, probes, requests, arms=["contexto"])
    block = report.build([out])["arms"]["contexto"]
    read = [r for r in requests if not r.get("usage_unreadable")]
    sizes = [r["uncached"] + r["cache_write"] + r["cache_read"] for r in read]
    assert block["max_input"] == max(sizes) < SMALL_BOUND
    assert block["over_bound"] == 0
    assert block["unreadable"] == {"requests": 1, "reserved_tokens": SMALL_BOUND + 1_100, "without_reservation": 0}
    assert block["bases"] == ["measured"]


def test_the_retry_and_unreadable_table_is_printed_only_when_it_has_something_to_show(tmp_path):
    from test_bench_records import make_run

    plain = report.render(report.build([make_run(tmp_path / "stub", repeats=1, seeds=(11,))]))
    tokens = plain.split("## Tokens and requests")[1].split("## Cost at published prices")[0]
    assert "Sessions run more than once" not in tokens and "Sessions retried" not in tokens
    assert "reserved tokens" not in plain and "retried attempts" not in plain
    assert ("No session was run more than once, and the usage of every request was read." in tokens)
    for out in (retried_run(tmp_path / "retried"), unreadable_run(tmp_path / "unreadable")):
        tokens = report.render(report.build([out])).split("## Tokens and requests")[1]
        assert "Sessions run more than once, and requests whose usage could not be read." in tokens
        assert "No session was run more than once" not in tokens


def test_without_the_reservation_the_cost_is_called_a_lower_bound(tmp_path):
    out = unreadable_run(tmp_path / "run", reserved=None)
    results = report.build([out])
    block = results["arms"]["contexto"]
    assert block["unreadable"] == {"requests": 1, "reserved_tokens": 0, "without_reservation": 1}
    assert block["cost_upper"] is None and block["cost_upper_per_unit"] is None
    cost = report.render(results).split("## Cost at published prices")[1]
    assert f"{report._usd(block['cost_per_unit'][MODEL])} or more" in cost
    assert "lower bound" in cost


def test_an_unreadable_request_is_left_out_of_the_request_series(tmp_path):
    out = unreadable_run(tmp_path / "run")
    run = records.read_run(out)
    results = report.build([out])
    series = results["request_series"]["contexto"]
    kept = [r for r in run.requests if (r["arm"], r["seed"]) == ("contexto", 1) and r["main"]
            and not r.get("usage_unreadable")]
    assert series["points"] == [[r["history_tokens"], r["uncached"] + r["cache_write"] + r["cache_read"]]
                                for r in kept]
    assert any("could not be read" in note for note in results["notes"])


@pytest.mark.parametrize("change, field", [
    ({"usage_unreadable": "yes"}, "usage_unreadable"),
    ({"usage_unreadable": True, "reserved_tokens": -1}, "reserved_tokens"),
    ({"attempt": -1}, "attempt"),
])
def test_the_live_fields_of_a_request_record_are_checked(tmp_path, change, field):
    s = small_script(1)
    rows = live_requests(s, "contexto", 0)
    rows[0].update(change)
    out = write_run(tmp_path / "run", [live_unit("contexto", 1, [("complete", None, rows)])],
                    probe_rows(s, "contexto", 0, set()), rows, arms=["contexto"])
    with pytest.raises(records.RecordError) as caught:
        records.read_run(out)
    assert caught.value.field == field


# Rounding


def test_a_mean_difference_of_exactly_zero_prints_without_a_sign_of_minus(tmp_path):
    # Seed differences of -5, -5, +4, +3, -3 and +6 probes of 72 sum to exactly 0, and their float
    # mean is -2.3e-18.
    from test_bench_report import crafted

    wrong = {}
    for seed, d in zip((1, 2, 3, 4, 5, 6), (-5, -5, 4, 3, -3, 6)):
        wrong[("contexto", seed, 0)] = range(-d) if d < 0 else range(0)
        wrong[("plain", seed, 0)] = range(d) if d > 0 else range(0)
    results = report.build([crafted(tmp_path / "run", wrong, seeds=(1, 2, 3, 4, 5, 6), arms=("contexto", "plain"))])
    pair = results["pairs"]["contexto_vs_plain"]
    assert pair["seed_level"]["mean"] == 0.0
    assert "opposite signs" not in pair["verdict"]["text"]
    text = report.render(results)
    assert "-0.0" not in text
    assert "+0.0 points" in text


# Requests with no record: no response from the upstream, or a record a killed run tore


def no_record_run(out, counted: bool = True):
    """``gap_run`` with the counts the live run writes for its gaps: request 1 of full context, seed 3,
    got no response after it was sent (166,692 tokens reserved, as in the live review's run), and so
    did request 2 of attempt 0 of Contexto's seed-1 session (50,000). A killed run tore one record of
    full context, seed 1 (its fragment held the unit and the reservation, 20,836), and one whose
    fragment named no unit (7,000, the largest reservation in the file). A torn probe line counts
    nothing. ``counted=False`` gives the second request fragment no ``counted_tokens``, as a run from
    before the live runner counted them wrote it."""
    out = gap_run(out)
    data = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    for unit in data["units"]:
        for entry in [unit, *unit["attempts"]]:
            entry.update({"requests_unanswered": 0, "unanswered_tokens": 0})
        if (unit["arm"], unit["seed"]) == ("full", 3):
            for entry in (unit, unit["attempts"][0]):
                entry.update({"requests_unanswered": 1, "unanswered_tokens": 166_692})
        if (unit["arm"], unit["seed"]) == ("contexto", 1):
            unit["attempts"][0].update({"requests_unanswered": 1, "unanswered_tokens": 50_000})
    torn = {"file": "requests.jsonl", "bytes": 134, "found": "2026-11-14T00:00:00Z", "action": "moved"}
    data["torn_lines"] = [
        {**torn, "moved_to": "requests.jsonl.torn-1", "counted_tokens": 20_836, "counted_from": "reservation",
         "arm": "full", "seed": 1, "repeat": 0, "attempt": 0},
        {**torn, "moved_to": "requests.jsonl.torn-2",
         **({"counted_tokens": 7_000, "counted_from": "largest_in_file"} if counted else {})},
        {**torn, "file": "probes.jsonl", "moved_to": "probes.jsonl.torn-1"},
        {**torn, "action": "newline_added"},
    ]
    (out / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return out


def test_requests_with_no_record_are_in_the_upper_cost_bound_and_named(tmp_path):
    out = no_record_run(tmp_path / "run")
    results = report.build([out])
    full, contexto, total = results["arms"]["full"], results["arms"]["contexto"], results["all_requests"]
    assert full["no_record"] == {"unanswered": 1, "torn": 1, "reserved_tokens": 166_692 + 20_836,
                                 "without_reservation": 0}
    assert contexto["no_record"] == {"unanswered": 1, "torn": 0, "reserved_tokens": 50_000, "without_reservation": 0}
    assert total["no_record"] == {"unanswered": 2, "torn": 2, "reserved_tokens": 166_692 + 20_836 + 50_000 + 7_000,
                                  "without_reservation": 0}
    for model in prices.models(TABLE):
        def kept(tokens: int, model: str = model) -> float:
            return prices.reservation_cost(tokens, model, TABLE)

        assert full["cost_upper"][model] == pytest.approx(full["cost"][model] + kept(187_528))
        assert contexto["cost_upper"][model] == pytest.approx(contexto["cost"][model] + kept(50_000))
        # Contexto's gap was in the attempt that did not complete: in its retries, not in its completed cost.
        assert contexto["retries"]["cost_upper"][model] == pytest.approx(contexto["retries"]["cost"][model]
                                                                         + kept(50_000))
        assert contexto["cost_completed_upper_per_unit"][model] == pytest.approx(
            contexto["cost_completed_per_unit"][model])
        assert total["cost_upper"][model] == pytest.approx(total["cost"][model] + kept(244_528))
    text = report.render(results)
    tokens = text.split("## Tokens and requests")[1].split("## Cost at published prices")[0]
    assert ("2 requests got no response from the upstream (the connection failed or closed before a status line), "
            "and a killed run cut short 2 request records, so they have no record in the table above. The live run "
            "kept their reservations as spent") in tokens
    cost = text.split("## Cost at published prices")[1]
    low, high = full["cost_per_unit"][MODEL], full["cost_upper_per_unit"][MODEL]
    assert f"{report._usd(low)} to {report._usd(high)}" in cost
    assert ("- Full context: 1 request got no response from the upstream and 1 request record was cut short by a "
            "killed run, so they have no record. The live run counted their reservations, 187,528 tokens, as spent, "
            "because the provider may have billed them; the higher figure adds those tokens at the model's highest "
            "price per token.") in cost
    assert (f"2 more requests got no response and 2 more request records were cut short; their reservations bring "
            f"the cost to at most {report._usd(total['cost_upper'][MODEL])}.") in cost


def test_a_torn_record_counted_by_no_run_leaves_only_a_lower_bound(tmp_path):
    results = report.build([no_record_run(tmp_path / "run", counted=False)])
    total = results["all_requests"]
    assert total["no_record"]["without_reservation"] == 1 and total["cost_upper"] is None
    assert results["arms"]["full"]["cost_upper"] is not None  # the arm's own were counted
    cost = report.render(results).split("## Cost at published prices")[1]
    assert "nothing bounds the cost of 1 of them" in cost


@pytest.mark.parametrize(("where", "value"), [("unit", -1), ("attempt", "1"), ("torn", {"counted_tokens": -5}),
                                              ("torn", {"file": 3})])
def test_the_counts_of_requests_with_no_record_are_checked(tmp_path, where, value):
    out = no_record_run(tmp_path / "run")
    data = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    if where == "unit":
        data["units"][0]["unanswered_tokens"] = value
    elif where == "attempt":
        data["units"][0]["attempts"][0]["requests_unanswered"] = value
    else:
        data["torn_lines"][0].update(value)
    (out / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(records.RecordError):
        records.read_run(out)


def test_an_unreadable_request_and_requests_with_no_record_share_one_upper_bound(tmp_path):
    out = no_record_run(tmp_path / "run")
    path = out / "requests.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0] = unreadable(rows[0], 9_000)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    results = report.build([out])
    total = results["all_requests"]
    for model in prices.models(TABLE):
        assert total["cost_upper"][model] == pytest.approx(
            total["cost"][model] + prices.reservation_cost(9_000 + 244_528, model, TABLE))
    cost = report.render(results).split("## Cost at published prices")[1]
    assert ("1 more request could not have its usage read and 2 more requests got no response and 2 more request "
            f"records were cut short; their reservations bring the cost to at most "
            f"{report._usd(total['cost_upper'][MODEL])}.") in cost
