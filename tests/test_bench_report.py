"""``contexto.bench.report``: results.json and results.md from output directories, and ``--report`` (D66).

The output directories are fixtures built by ``test_bench_records.make_run`` and its helpers: real
generated scripts, invented answers and request records. No arm is run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from contexto import cli
from contexto.bench import charts, prices, records, report, stats
from test_bench_records import (
    ARMS,
    MODEL,
    SMALL_BOUND,
    make_run,
    probe_rows,
    request_rows,
    small_script,
    unit,
    write_jsonl,
    write_run,
)

FIXTURES = Path(__file__).with_name("fixtures")
LIMITS = (
    "The sessions are synthetic",
    "Scoring is exact match",
    "One version of Claude Code",
    "A difference between arms that is smaller than the run-to-run noise is not a finding",
)


def crafted(out, wrong: dict, seeds=(1,), repeats=1, protocol="v2", arms=ARMS, **fields):
    """A directory where the wrong answers are chosen by hand: ``wrong[(arm, seed, repeat)]`` is a
    set of probe ids, a number n for the first n probes of the script, or a range of positions.
    ``wrong[arm]`` is the same for every unit of the arm that has no entry of its own."""
    units, probes, requests = [], [], []
    for seed in seeds:
        s = small_script(seed, protocol)
        for repeat in range(repeats if seed == seeds[0] else 1):
            for arm in arms:
                miss = wrong.get((arm, seed, repeat), wrong.get(arm, set()))
                if isinstance(miss, int):
                    miss = range(miss)
                if isinstance(miss, range):
                    miss = {s.probes[i].id for i in miss}
                rows, pre_tokens = request_rows(s, arm, repeat)
                units.append(unit(arm, seed, repeat, compactions=None if arm == "full" else len(pre_tokens),
                                  compaction_pre_tokens=pre_tokens))
                probes += probe_rows(s, arm, repeat, miss)
                requests += rows
    return write_run(out, units, probes, requests, protocol=protocol, **fields)


def rows_of(run: records.Run, arm: str, repeat: int = 0, seed: int | None = None) -> list[dict]:
    return [r for r in run.probes if r["arm"] == arm and r["repeat"] == repeat and seed in (None, r["seed"])]


def requests_of(run: records.Run, arm: str, repeat: int = 0) -> list[dict]:
    return [r for r in run.requests if r["arm"] == arm and r["repeat"] == repeat]


def requests_for(s, *arms: str) -> list[dict]:
    """The request rows of repeat 0 of each arm: a complete unit has request records."""
    return [row for arm in arms for row in request_rows(s, arm, 0)[0]]


# Per arm


def test_results_have_the_fields_of_the_schema_document(tmp_path):
    out = make_run(tmp_path / "run")
    results = report.build([out])
    assert (results["format"], results["kind"], results["bound"]) == (1, "live", SMALL_BOUND)
    assert results["manifest"] == records.read_manifest(out / "manifest.json")
    assert list(results["arms"]) == list(ARMS)
    assert list(results["pairs"]) == ["contexto_vs_full", "contexto_vs_plain", "plain_vs_full"]
    assert list(results["noise"]) == list(ARMS)
    assert results["by_model"] is None
    assert all(isinstance(note, str) and note for note in results["notes"])
    for arm in ARMS:
        block = results["arms"][arm]
        for field in ("units", "probes", "correct", "accuracy", "interval", "by_class", "tokens", "max_input",
                      "over_bound", "compactions", "cost"):
            assert field in block, field
        assert set(block["tokens"]) == {"requests", "uncached", "cache_write", "cache_read", "output", "input"}
    json.dumps(results)  # everything in it is JSON


def test_accuracy_per_arm_uses_repeat_0_of_each_seed_with_wilson_intervals(tmp_path):
    run = records.read_run(make_run(tmp_path / "run"))
    results = report.build([run.path])
    for arm in ARMS:
        rows = rows_of(run, arm)
        block = results["arms"][arm]
        right = sum(r["correct"] for r in rows)
        assert (block["units"], block["probes"], block["correct"]) == (3, 216, right)
        assert block["seeds"] == [11, 12, 13]
        assert block["accuracy"] == right / 216
        assert block["interval"] == list(stats.wilson(right, 216))
        assert list(block["by_class"]) == ["paged_only", "multihop", "chain", "obligations", "distractor",
                                           "paraphrase", "concluded", "toolloop"]
        assert sum(c["probes"] for c in block["by_class"].values()) == 216
        for cls, cell in block["by_class"].items():
            of_class = [r for r in rows if r["cls"] == cls]
            k = sum(r["correct"] for r in of_class)
            assert (cell["probes"], cell["correct"]) == (len(of_class), k)
            assert cell["accuracy"] == k / len(of_class) and cell["interval"] == list(stats.wilson(k, len(of_class)))


def test_the_repeated_seed_is_counted_once(tmp_path):
    # Repeats 1 and 2 of seed 1 get everything wrong; the pooled figures must not move.
    wrong = {("contexto", 1, 1): 72, ("contexto", 1, 2): 72, ("contexto", 1, 0): 3, ("contexto", 2, 0): 5}
    out = crafted(tmp_path / "run", wrong, seeds=(1, 2), repeats=3)
    block = report.build([out])["arms"]["contexto"]
    assert (block["units"], block["probes"], block["correct"]) == (2, 144, 144 - 8)


def test_tokens_sizes_and_cost_per_arm(tmp_path):
    run = records.read_run(make_run(tmp_path / "run"))
    results = report.build([run.path])
    table = prices.load()
    for arm in ARMS:
        requests = requests_of(run, arm)
        block = results["arms"][arm]
        tokens = block["tokens"]
        assert tokens["requests"] == len(requests)
        for name in ("uncached", "cache_write", "cache_read", "output"):
            assert tokens[name] == sum(r[name] for r in requests)
        assert tokens["input"] == tokens["uncached"] + tokens["cache_write"] + tokens["cache_read"]
        sizes = [r["uncached"] + r["cache_write"] + r["cache_read"] for r in requests]
        assert block["max_input"] == max(sizes)
        assert block["over_bound"] == sum(1 for size in sizes if size > SMALL_BOUND)
        # The cost at every model of the table, the run's own model first.
        assert list(block["cost"]) == [MODEL, "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"]
        for model, usd in block["cost"].items():
            assert usd == pytest.approx(prices.cost(requests, model, "5m", table))
            assert block["cost_per_unit"][model] == pytest.approx(usd / 3)
    assert results["arms"]["contexto"]["over_bound"] == 0
    assert results["arms"]["full"]["over_bound"] > 0
    assert results["arms"]["plain"]["compactions"] == sum(
        u["compactions"] for u in run.manifest["units"] if u["arm"] == "plain" and u["repeat"] == 0)
    assert results["arms"]["contexto"]["compactions"] == 0 and results["arms"]["full"]["compactions"] is None


def test_the_runs_own_model_is_listed_first_and_the_cache_lifetime_selects_the_write_price(tmp_path):
    out = make_run(tmp_path / "run", model="claude-opus-5-5", cache_ttl="1h", repeats=1, seeds=(11,))
    run = records.read_run(out)
    results = report.build([out])
    cost = results["arms"]["plain"]["cost"]
    assert list(cost)[0] == "claude-opus-5-5"
    assert cost["claude-opus-5-5"] == pytest.approx(prices.cost(requests_of(run, "plain"), "claude-opus-5-5", "1h"))
    assert results["prices"]["cache_ttl"] == "1h" and results["prices"]["cache_ttl_assumed"] is False
    assert results["prices"]["table"] == prices.load()


def test_a_missing_cache_lifetime_is_priced_at_five_minutes_and_said(tmp_path):
    out = make_run(tmp_path / "run", repeats=1, seeds=(11,), cache_ttl=None)
    results = report.build([out])
    assert results["prices"]["cache_ttl"] == "5m" and results["prices"]["cache_ttl_assumed"] is True
    assert "does not record a cache lifetime" in report.render(results)


def test_a_model_without_a_price_still_gets_the_cost_at_the_models_that_have_one(tmp_path):
    out = make_run(tmp_path / "run", model="house-model-7", repeats=1, seeds=(11,))
    results = report.build([out])
    assert list(results["arms"]["full"]["cost"]) == prices.models(prices.load())
    assert results["prices"]["run_model_priced"] is False
    assert "house-model-7 has no row in the price table" in report.render(results)


def test_a_replacement_price_table_is_used(tmp_path):
    out = make_run(tmp_path / "run", repeats=1, seeds=(11,))
    table = {"format": 1, "source": "https://example.test/prices", "accessed": "2027-01-02", "models": {
        MODEL: {"rows": [{"over_input_tokens": 0, "input": 1, "cache_write_5m": 1, "cache_write_1h": 1,
                          "cache_read": 1, "output": 1}]}}}
    results = report.build([out], prices=table)
    tokens = results["arms"]["full"]["tokens"]
    assert results["arms"]["full"]["cost"] == {MODEL: pytest.approx((tokens["input"] + tokens["output"]) / 1e6)}
    text = report.render(results)
    assert "https://example.test/prices" in text and "2027-01-02" in text


def test_every_recorded_request_is_totalled_separately_from_the_per_arm_blocks(tmp_path):
    run = records.read_run(make_run(tmp_path / "run"))
    results = report.build([run.path])
    total = results["all_requests"]
    assert total["tokens"]["requests"] == len(run.requests)
    assert total["cost"][MODEL] == pytest.approx(prices.cost(run.requests, MODEL))
    assert total["tokens"]["requests"] > sum(results["arms"][a]["tokens"]["requests"] for a in ARMS)  # the repeats


# Pairs


def test_the_published_run_4_counts_give_the_published_p_value(tmp_path):
    # 52 and 47 of 54, with disjoint misses of 2 and 7: exact McNemar p = 92/512 = 0.1797.
    s = small_script(20261016, "d27")
    ids = [p.id for p in s.probes]
    wrong = {"contexto": set(ids[:2]), "full": set(ids[10:17])}
    out = crafted(tmp_path / "run", wrong, seeds=(20261016,), protocol="d27", arms=("contexto", "full"))
    results = report.build([out])
    assert (results["arms"]["contexto"]["correct"], results["arms"]["full"]["correct"]) == (52, 47)
    assert list(results["pairs"]) == ["contexto_vs_full"]
    pair = results["pairs"]["contexto_vs_full"]
    pooled = pair["pooled"]
    assert (pooled["n"], pooled["both"], pooled["a_only"], pooled["b_only"], pooled["neither"]) == (54, 45, 7, 2, 0)
    assert pooled["mcnemar_p"] == 92 / 512
    assert pooled["difference"] == pytest.approx(5 / 54)
    assert pooled["interval"][0] < 0 < pooled["interval"][1]
    assert pair["verdict"]["result"] == "no_difference_shown"
    assert "0.1797" in report.render(results)


def test_pairs_are_the_paired_statistics_pooled_by_class_and_by_seed(tmp_path):
    run = records.read_run(make_run(tmp_path / "run"))
    results = report.build([run.path])
    for name, (a, b) in (("contexto_vs_full", ("contexto", "full")), ("contexto_vs_plain", ("contexto", "plain")),
                         ("plain_vs_full", ("plain", "full"))):
        pair = results["pairs"][name]
        assert (pair["a"], pair["b"], pair["seeds"]) == (a, b, [11, 12, 13])
        right_a = {(r["seed"], r["id"]): r["correct"] for r in rows_of(run, a)}
        right_b = {(r["seed"], r["id"]): r["correct"] for r in rows_of(run, b)}
        keys = list(right_a)
        expected = json.loads(json.dumps(stats.paired([right_a[k] for k in keys], [right_b[k] for k in keys])))
        assert pair["pooled"] == expected
        differences = []
        for seed in (11, 12, 13):
            of_seed = [k for k in keys if k[0] == seed]
            differences.append(stats.paired([right_a[k] for k in of_seed], [right_b[k] for k in of_seed])["difference"])
        assert pair["seed_level"] == json.loads(json.dumps(stats.seed_level(differences)))
        assert [s["difference"] for s in pair["per_seed"]] == differences
        chain = [r["id"] for r in rows_of(run, a, seed=11) if r["cls"] == "chain"]
        of_chain = [k for k in keys if k[1] in chain]
        assert pair["by_class"]["chain"] == json.loads(json.dumps(
            stats.paired([right_a[k] for k in of_chain], [right_b[k] for k in of_chain])))
        assert pair["dropped"] == {"probes": 0, "per_seed": {}, "units": []}


def test_a_pair_is_compared_only_where_both_units_are_complete_and_says_what_was_dropped(tmp_path):
    out = make_run(tmp_path / "run", repeats=1, not_run=[("full", 12)])
    results = report.build([out])
    pair = results["pairs"]["contexto_vs_full"]
    assert pair["seeds"] == [11, 13] and pair["pooled"]["n"] == 144
    assert pair["dropped"] == {"probes": 72, "per_seed": {"12": 72}, "units": [
        {"arm": "full", "seed": 12, "repeat": 0, "status": "not_run",
         "reason": "the history does not fit the model's context window"}]}
    assert results["pairs"]["contexto_vs_plain"]["pooled"]["n"] == 216
    assert results["arms"]["full"]["units"] == 2 and results["arms"]["contexto"]["units"] == 3
    assert results["arms"]["full"]["not_complete"] == [
        {"seed": 12, "repeat": 0, "status": "not_run", "reason": "the history does not fit the model's context window"}]
    text = report.render(results)
    assert "72 probes of seed 12 were dropped" in text
    assert "the history does not fit the model's context window" in text


def test_an_arm_that_never_ran_is_reported_as_not_run_and_its_pairs_as_not_compared(tmp_path):
    out = make_run(tmp_path / "run", repeats=1, seeds=(11,), not_run=[("full", 11)])
    results = report.build([out])
    full = results["arms"]["full"]
    assert (full["units"], full["probes"], full["accuracy"], full["interval"]) == (0, 0, None, None)
    pair = results["pairs"]["contexto_vs_full"]
    assert pair["pooled"] is None and pair["seed_level"] is None and pair["verdict"]["result"] == "not_compared"
    text = report.render(results)
    assert "Contexto against full context: not compared" in text


# Noise


def test_noise_comes_from_the_repeats_of_the_first_seed(tmp_path):
    run = records.read_run(make_run(tmp_path / "run", repeats=3))
    results = report.build([run.path])
    for arm in ARMS:
        runs = [[r["correct"] for r in rows_of(run, arm, repeat, seed=11)] for repeat in range(3)]
        expected = json.loads(json.dumps(stats.repeat_noise(runs)))
        noise = results["noise"][arm]
        assert noise["seed"] == 11
        assert {k: noise[k] for k in expected} == expected


def test_noise_is_null_with_one_repeat(tmp_path):
    results = report.build([make_run(tmp_path / "run", repeats=1)])
    assert results["noise"] == {"contexto": None, "full": None, "plain": None}
    assert "was not measured" in report.render(results)


# What this run shows


def test_with_few_seeds_the_seed_level_test_cannot_reach_005_and_the_report_says_why(tmp_path):
    # Five seeds: the smallest sign-flip p value is 2 / 2**5 = 0.0625, however large the difference.
    out = crafted(tmp_path / "run", {"plain": 30}, seeds=(1, 2, 3, 4, 5))
    results = report.build([out])
    pair = results["pairs"]["contexto_vs_plain"]
    assert pair["pooled"]["interval"][0] > 0 and pair["seed_level"]["sign_flip_p"] == 0.0625
    verdict = pair["verdict"]
    assert verdict["result"] == "no_difference_shown"
    assert (verdict["interval_excludes_zero"], verdict["seed_p_below_alpha"], verdict["seed_test_can_reach_alpha"],
            verdict["seed_test_min_p"]) == (True, False, False, 0.0625)
    text = report.render(results)
    assert "With 5 seeds the seed-level test cannot reach 0.05" in text
    assert "2 / 2^5 = 0.0625" in text


def test_a_direction_is_named_only_when_the_pooled_interval_and_the_seed_level_test_both_show_it(tmp_path):
    # On each of six seeds Contexto misses 2 probes, full context 3 others and plain the first 30.
    wrong = {"contexto": range(40, 42), "full": range(3), "plain": 30}
    out = crafted(tmp_path / "run", wrong, seeds=(1, 2, 3, 4, 5, 6))
    results = report.build([out])
    shown = results["pairs"]["contexto_vs_plain"]["verdict"]
    assert shown["result"] == "a_higher" and shown["seed_test_can_reach_alpha"] is True
    assert results["pairs"]["contexto_vs_plain"]["seed_level"]["sign_flip_p"] == 2 / 64
    assert results["pairs"]["plain_vs_full"]["verdict"]["result"] == "b_higher"
    # Contexto against full: one probe in 72 on every seed, always the same way. The seed-level test
    # is below 0.05, but the pooled interval (18 against 12 discordant probes of 432) contains 0.
    small = results["pairs"]["contexto_vs_full"]
    assert (small["pooled"]["a_only"], small["pooled"]["b_only"]) == (18, 12)
    assert small["seed_level"]["sign_flip_p"] == 2 / 64 and small["pooled"]["interval"][0] < 0
    assert small["verdict"]["result"] == "no_difference_shown"
    text = report.render(results)
    assert "Contexto answered more probes correctly than plain Claude Code" in text
    assert "Full context answered more probes correctly than plain Claude Code" in text
    assert "Contexto against full context: no difference shown, because the pooled interval contains 0" in text


def test_a_difference_smaller_than_the_run_to_run_noise_is_not_named(tmp_path):
    # Plain misses 20 of 72 on every seed (27.8 points below Contexto), and a second run of the first
    # seed misses 50: the accuracy range across repeats (41.7 points) is larger than that difference.
    wrong = {"plain": 20, ("plain", 1, 1): 50}
    out = crafted(tmp_path / "run", wrong, seeds=(1, 2, 3, 4, 5, 6), repeats=2)
    results = report.build([out])
    pair = results["pairs"]["contexto_vs_plain"]
    assert results["noise"]["plain"]["accuracy_range"] == pytest.approx(30 / 72)
    verdict = pair["verdict"]
    assert (verdict["interval_excludes_zero"], verdict["seed_p_below_alpha"]) == (True, True)
    assert verdict["noise_range"] == pytest.approx(30 / 72) and verdict["difference_exceeds_noise"] is False
    assert verdict["result"] == "no_difference_shown"
    text = report.render(results)
    assert "is not larger than the run-to-run noise" in text


def test_the_noise_is_printed_next_to_the_difference_between_arms(tmp_path):
    out = crafted(tmp_path / "run", {"plain": 30, ("plain", 1, 1): 27}, seeds=(1, 2), repeats=2)
    text = report.render(report.build([out]))
    line = next(x for x in text.splitlines() if x.startswith("| Contexto against plain Claude Code | +41.7 points"))
    assert line == ("| Contexto against plain Claude Code | +41.7 points | 0.0 points | 4.2 points | "
                    "larger than the noise |")


def test_the_report_always_states_its_limits(tmp_path):
    for name, kwargs in (("a", {}), ("b", {"repeats": 1, "seeds": (11,)})):
        text = report.render(report.build([make_run(tmp_path / name, **kwargs)]))
        assert "## What this run shows" in text
        for limit in LIMITS:
            assert limit in text, limit


# Concluded facts and the plain arm's compaction


def test_concluded_accuracy_is_given_over_all_probes_and_over_those_whose_decision_was_right(tmp_path):
    s = small_script(1)
    concluded = [p.id for p in s.probes if p.cls == "concluded"]
    assert len(concluded) == 6
    decisions = {pid: i >= 2 for i, pid in enumerate(concluded)}  # the first two decision replies were wrong
    units, probes = [], []
    for arm in ("contexto", "full"):
        units.append(unit(arm, 1, concluded_ok=decisions if arm == "contexto" else {}))
        probes += probe_rows(s, arm, 0, set(concluded[:3]))  # three wrong: two with a wrong decision, one without
    out = write_run(tmp_path / "run", units, probes, requests_for(s, "contexto", "full"))
    results = report.build([out])
    block = results["arms"]["contexto"]["concluded"]
    assert (block["decisions"], block["decisions_right"], block["scripted"]) == (6, 4, False)
    assert (block["all"]["probes"], block["all"]["correct"]) == (6, 3)
    assert (block["decision_right"]["probes"], block["decision_right"]["correct"]) == (4, 3)
    assert block["decision_right"]["interval"] == list(stats.wilson(3, 4))
    full = results["arms"]["full"]["concluded"]
    assert (full["decisions"], full["decisions_right"], full["scripted"]) == (6, 6, True)
    assert full["decision_right"] == full["all"]
    text = report.render(results)
    assert "| Contexto | 4 of 6 | 3 of 6 (50.0%" in text and "3 of 4 (75.0%" in text
    assert "scripted" in text


def test_a_d27_run_has_no_concluded_block(tmp_path):
    out = crafted(tmp_path / "run", {}, protocol="d27")
    results = report.build([out])
    assert results["arms"]["contexto"]["concluded"] is None
    assert "## Facts concluded by the assistant" not in report.render(results)


def test_the_plain_arms_compaction_is_reported_against_the_bound(tmp_path):
    run = records.read_run(make_run(tmp_path / "run"))
    results = report.build([run.path])
    block = results["plain_compaction"]
    assert block["set"] == run.manifest["plain_compaction"] and block["bound"] == SMALL_BOUND
    plain = [u for u in run.manifest["units"] if u["arm"] == "plain"]
    values = [v for u in plain for v in u["compaction_pre_tokens"]]
    assert block["compactions"] == len(values) > 0
    assert block["pre_tokens"] == {"count": len(values), "min": min(values), "max": max(values),
                                   "mean": sum(values) / len(values),
                                   "below_trigger": sum(1 for v in values if v < SMALL_BOUND)}
    assert [(u["seed"], u["repeat"], u["compactions"]) for u in block["units"]] == [
        (u["seed"], u["repeat"], u["compactions"]) for u in plain]
    text = report.render(results)
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in text and f"{min(values):,}" in text and f"{max(values):,}" in text


def test_without_a_plain_arm_there_is_no_compaction_block(tmp_path):
    out = crafted(tmp_path / "run", {}, arms=("contexto", "full"), plain_compaction=None)
    assert report.build([out])["plain_compaction"] is None


# The request series


def test_the_request_series_is_the_main_requests_of_the_first_seed_repeat_0(tmp_path):
    run = records.read_run(make_run(tmp_path / "run"))
    results = report.build([run.path])
    assert list(results["request_series"]) == list(ARMS)
    labels = {"contexto": "Contexto", "full": "Full context", "plain": "Plain Claude Code"}
    for arm in ARMS:
        series = results["request_series"][arm]
        main = [r for r in requests_of(run, arm) if r["seed"] == 11 and r["main"]]
        assert (series["basis"], series["label"]) == ("measured", labels[arm])
        assert series["points"] == [[r["history_tokens"], r["uncached"] + r["cache_write"] + r["cache_read"]]
                                    for r in main]
    assert len(results["request_series"]["plain"]["points"]) == len(small_script(11).turns)  # no side requests


# Bad directories


def test_a_complete_unit_without_probe_records_is_an_error(tmp_path):
    s = small_script(1)
    out = write_run(tmp_path / "run", [unit("contexto", 1), unit("full", 1)], probe_rows(s, "contexto", 0, set()),
                    requests_for(s, "contexto", "full"))
    with pytest.raises(report.ReportError, match="full, seed 1, repeat 0 is complete .* has no records for it"):
        report.build([out])


def test_units_of_one_seed_that_answered_different_probes_are_an_error(tmp_path):
    s = small_script(1)
    rows = probe_rows(s, "contexto", 0, set()) + probe_rows(s, "full", 0, set())[:-5]
    out = write_run(tmp_path / "run", [unit("contexto", 1), unit("full", 1)], rows,
                    requests_for(s, "contexto", "full"))
    with pytest.raises(report.ReportError, match="72 probes .* 67"):
        report.build([out])


def test_a_bad_record_is_reported_with_its_file_and_line(tmp_path):
    out = make_run(tmp_path / "run", repeats=1, seeds=(11,))
    lines = (out / "probes.jsonl").read_text(encoding="utf-8").splitlines()
    lines[6] = lines[6].replace('"correct": true', '"correct": "yes"').replace('"correct": false', '"correct": "no"')
    (out / "probes.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(records.RecordError, match=r"probes\.jsonl: line 7: field 'correct'"):
        report.build([out])


# Several directories


def test_one_run_split_across_directories_gives_the_results_of_the_whole(tmp_path):
    whole = make_run(tmp_path / "whole", seeds=(11, 12, 13), repeats=2)
    run = records.read_run(whole)
    parts = []
    for name, seeds in (("first", (11,)), ("second", (12, 13))):
        units = [u for u in run.manifest["units"] if u["seed"] in seeds]
        part = write_run(tmp_path / name, units, [r for r in run.probes if r["seed"] in seeds],
                         [r for r in run.requests if r["seed"] in seeds])
        parts.append(part)
    merged, single = report.build(parts), report.build([whole])
    for block in ("arms", "pairs", "noise", "request_series", "plain_compaction", "all_requests", "bound"):
        assert merged[block] == single[block], block
    # Several directories always give by_model; with one model it repeats the top-level blocks.
    assert list(merged["by_model"]) == [MODEL] and single["by_model"] is None
    assert merged["by_model"][MODEL]["arms"] == merged["arms"]
    assert report.render(merged).count("## Accuracy by arm") == 1
    assert "## Results for" not in report.render(merged)
    assert merged["manifest"]["seeds"] == [11, 12, 13] and len(merged["manifest"]["units"]) == 12
    assert merged["manifest"]["merged"]["directories"] == 2


def test_directories_of_different_models_are_reported_per_model(tmp_path):
    haiku = make_run(tmp_path / "haiku", repeats=1, seeds=(11, 12))
    opus = make_run(tmp_path / "opus", repeats=1, seeds=(11, 12), model="claude-opus-5-5", rng_seed=9)
    results = report.build([haiku, opus])
    assert list(results["by_model"]) == [MODEL, "claude-opus-5-5"]
    assert results["arms"] is None and results["pairs"] is None and results["noise"] is None
    for model, directory in ((MODEL, haiku), ("claude-opus-5-5", opus)):
        alone = report.build([directory])
        block = results["by_model"][model]
        for name in ("arms", "pairs", "noise", "request_series", "plain_compaction", "all_requests"):
            assert block[name] == alone[name], name
        assert list(block["arms"]["contexto"]["cost"])[0] == model
    assert results["manifest"]["model"] is None
    assert results["manifest"]["models"] == [MODEL, "claude-opus-5-5"]
    assert {u["model"] for u in results["manifest"]["units"]} == {MODEL, "claude-opus-5-5"}
    text = report.render(results)
    assert "## Results for claude-haiku-5-5" in text and "## Results for claude-opus-5-5" in text
    for limit in LIMITS:
        assert limit in text


@pytest.mark.parametrize("field, value, words", [
    ("bound", 5_000, "bound: 4,000 tokens in directory 1 and 5,000 tokens in directory 2"),
    ("protocol", "d27", "protocol: v2 in directory 1 and d27 in directory 2"),
    ("turn_kbytes", 3, "turn size: 2,000 bytes in directory 1 and 3,000 bytes in directory 2"),
])
def test_directories_that_differ_in_bound_protocol_or_turn_size_are_not_pooled(tmp_path, field, value, words):
    first = make_run(tmp_path / "first", repeats=1, seeds=(11,))
    second = make_run(tmp_path / "second", repeats=1, seeds=(12,))
    data = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    data[field] = value
    (second / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(report.ReportError) as exc:
        report.build([first, second])
    assert "cannot pool" in str(exc.value) and words in str(exc.value)


def test_the_same_unit_in_two_directories_is_an_error(tmp_path):
    first = make_run(tmp_path / "first", repeats=1, seeds=(11, 12))
    second = make_run(tmp_path / "second", repeats=1, seeds=(12, 13))
    with pytest.raises(report.ReportError) as exc:
        report.build([first, second])
    text = str(exc.value)
    assert "claude-haiku-5-5, contexto, seed 12, repeat 0" in text and "directory 1 and directory 2" in text


def test_differences_between_merged_directories_that_do_not_stop_pooling_are_recorded(tmp_path):
    first = make_run(tmp_path / "first", repeats=1, seeds=(11,))
    second = make_run(tmp_path / "second", repeats=1, seeds=(12,), claude_code_version="2.1.323")
    results = report.build([first, second])
    assert results["manifest"]["merged"]["differs"] == {"claude_code_version": ["2.1.322", "2.1.323"]}
    assert "claude_code_version" in report.render(results) and "2.1.323" in report.render(results)


# write


def test_write_makes_the_results_the_report_and_the_charts(tmp_path):
    out = make_run(tmp_path / "run")
    results = report.write(out)
    assert json.loads((out / "results.json").read_text(encoding="utf-8")) == json.loads(json.dumps(results))
    text = (out / "results.md").read_text(encoding="utf-8")
    assert text == report.render(results)
    assert text.startswith("# Contexto benchmark results\n")
    for heading in ("## What was run", "## Accuracy by arm", "### Accuracy by probe class",
                    "## Differences between arms", "### Differences by probe class",
                    "### Differences with seeds as the unit",
                    "## Run-to-run noise", "## Facts concluded by the assistant", "## Tokens and requests",
                    "## Cost at published prices", "## Compaction in the plain Claude Code arm", "## Charts",
                    "## What this run shows", "### Limits"):
        assert f"\n{heading}\n" in text, heading
    assert "![Accuracy by arm](charts/accuracy.svg)" in text
    assert "![Request size against history length](charts/request-size.svg)" in text
    assert (out / "charts" / "accuracy.svg").is_file() and (out / "charts" / "request-size.svg").is_file()
    assert "https://platform.claude.com/docs/en/about-claude/pricing" in text and "2026-11-12" in text


def test_write_is_reproducible_byte_for_byte(tmp_path):
    a, b = make_run(tmp_path / "a"), make_run(tmp_path / "b")
    report.write(a)
    report.write(b)
    report.write(a)  # overwrites its own output
    for name in ("results.json", "results.md", "charts/accuracy.svg", "charts/request-size.svg"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_write_can_read_other_directories_and_skip_the_charts(tmp_path):
    first = make_run(tmp_path / "first", repeats=1, seeds=(11,))
    second = make_run(tmp_path / "second", repeats=1, seeds=(12,))
    out = tmp_path / "merged" / "deeper"
    results = report.write(out, sources=[first, second], charts=False)
    assert results["arms"]["contexto"]["units"] == 2
    assert [name for name, _ in charts.planned(results)] == ["accuracy.svg", "request-size.svg"]  # one model
    assert sorted(p.name for p in out.iterdir()) == ["results.json", "results.md"]
    assert "## Charts" not in (out / "results.md").read_text(encoding="utf-8")
    assert not (first / "results.json").exists()


def test_nothing_about_where_the_files_are_goes_into_the_outputs(tmp_path):
    secret_dir = tmp_path / "home-of-zebediah" / "private-runs"
    out = make_run(secret_dir / "run", repeats=1, seeds=(11,))
    price_file = secret_dir / "my-prices.json"
    price_file.write_text(json.dumps(prices.load()), encoding="utf-8")
    assert cli.main(["bench", "--report", str(out), "--prices", str(price_file)]) == 0
    written = [p for p in out.rglob("*") if p.is_file() and p.name not in ("manifest.json", "probes.jsonl",
                                                                           "requests.jsonl")]
    assert sorted(p.name for p in written) == ["accuracy.svg", "request-size.svg", "results.json", "results.md"]
    for path in written:
        text = path.read_text(encoding="utf-8")
        for private in ("zebediah", "private-runs", "my-prices", str(tmp_path)):
            assert private not in text, (path.name, private)


# contexto bench --report


def test_report_mode_recomputes_one_directory_in_place(tmp_path, capsys):
    out = make_run(tmp_path / "run")
    assert cli.main(["bench", "--report", str(out)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"written: {out / 'results.json'}" in captured.out and f"written: {out / 'results.md'}" in captured.out
    assert "What this run shows" in captured.out
    assert json.loads((out / "results.json").read_text(encoding="utf-8"))["arms"]["contexto"]["units"] == 3
    assert (out / "charts" / "request-size.svg").is_file()


def test_report_mode_writes_to_out_when_given(tmp_path, capsys):
    run = make_run(tmp_path / "run", repeats=1, seeds=(11,))
    out = tmp_path / "elsewhere"
    assert cli.main(["bench", "--report", str(run), "--out", str(out)]) == 0
    assert (out / "results.json").is_file() and not (run / "results.json").exists()


def test_report_mode_merges_several_directories_into_out(tmp_path, capsys):
    haiku = make_run(tmp_path / "haiku", repeats=1, seeds=(11,))
    opus = make_run(tmp_path / "opus", repeats=1, seeds=(11,), model="claude-opus-5-5")
    assert cli.main(["bench", "--report", str(haiku), str(opus)]) == 2
    assert "--report with more than one directory needs --out DIR" in capsys.readouterr().err
    out = tmp_path / "both"
    assert cli.main(["bench", "--report", str(haiku), str(opus), "--out", str(out)]) == 0
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert list(results["by_model"]) == [MODEL, "claude-opus-5-5"]
    assert sorted(p.name for p in (out / "charts").iterdir()) == [
        "accuracy-claude-haiku-5-5.svg", "accuracy-claude-opus-5-5.svg", "request-size-claude-haiku-5-5.svg",
        "request-size-claude-opus-5-5.svg"]


def test_report_mode_uses_a_replacement_price_table(tmp_path, capsys):
    run = make_run(tmp_path / "run", repeats=1, seeds=(11,))
    table = prices.load()
    table["models"] = {"claude-haiku-5-5": table["models"]["claude-haiku-5-5"]}
    table["models"]["claude-haiku-5-5"]["rows"] = [dict(table["models"]["claude-haiku-5-5"]["rows"][0])]
    del table["models"]["claude-haiku-5-5"]["size_rule"]
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    assert cli.main(["bench", "--report", str(run), "--prices", str(path)]) == 0
    results = json.loads((run / "results.json").read_text(encoding="utf-8"))
    assert list(results["arms"]["full"]["cost"]) == [MODEL]
    assert results["prices"]["replaced"] is True


@pytest.mark.parametrize("damage, words", [
    (lambda out: (out / "manifest.json").unlink(), "manifest.json: cannot read the file"),
    (lambda out: (out / "requests.jsonl").write_text('{"arm": "contexto"}\n', encoding="utf-8"),
     "requests.jsonl: line 1: field 'seed': missing"),
])
def test_report_mode_exits_1_with_the_reason_for_a_bad_directory(tmp_path, capsys, damage, words):
    run = make_run(tmp_path / "run", repeats=1, seeds=(11,))
    damage(run)
    assert cli.main(["bench", "--report", str(run)]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("contexto bench: error: ") and words in captured.err
    assert not (run / "results.json").exists()


def test_report_mode_exits_1_for_a_bad_price_table(tmp_path, capsys):
    run = make_run(tmp_path / "run", repeats=1, seeds=(11,))
    assert cli.main(["bench", "--report", str(run), "--prices", str(tmp_path / "absent.json")]) == 1
    assert "absent.json: cannot read the file" in capsys.readouterr().err


def test_report_and_chart_are_modes_of_the_command(capsys):
    with pytest.raises(SystemExit):
        cli.main(["bench", "--help"])
    text = capsys.readouterr().out
    for option in ("--report DIR [DIR ...]", "--chart RESULTS", "--prices FILE", "report mode", "chart mode"):
        assert option in text, option
    assert cli.main(["bench", "--report", "a", "--chart", "b"]) == 2
    assert "give one mode, not --chart and --report" in capsys.readouterr().err


def test_fixture_helpers_write_what_the_readers_accept(tmp_path):
    s = small_script(1)
    write_jsonl(tmp_path / "requests.jsonl", request_rows(s, "plain", 0)[0])
    assert records.read_requests(tmp_path / "requests.jsonl")


def test_requests_sent_to_another_model_are_counted_and_the_cost_note_says_so(tmp_path):
    s = small_script(1)
    requests = request_rows(s, "contexto", 0)[0]
    requests[0] = {**requests[0], "model": "claude-haiku-5-5-small", "main": False}
    requests[1] = {**requests[1], "model": None}
    out = write_run(tmp_path / "run", [unit("contexto", 1)], probe_rows(s, "contexto", 0, set()), requests)
    results = report.build([out])
    assert results["arms"]["contexto"]["other_model_requests"] == 1
    assert "1 request went to a model other than the run's own" in report.render(results)
    assert results["pairs"] == {} and "Fewer than two arms were run" in report.render(results)


# The committed sample


def test_the_committed_sample_report_and_charts_regenerate_byte_for_byte(tmp_path):
    """tests/fixtures/bench_report/ is what the fixture run reports. If this fails after a deliberate
    change to the report or a chart, run tests/fixtures/make_bench_report.py and commit the result."""
    sys.path.insert(0, str(FIXTURES))
    try:
        import make_bench_report
    finally:
        sys.path.remove(str(FIXTURES))
    run = make_bench_report.build(tmp_path)
    for name in make_bench_report.FILES:
        committed = (FIXTURES / "bench_report" / name).read_bytes()
        assert (run / name).read_bytes() == committed, f"{name} differs from tests/fixtures/bench_report/{name}"
