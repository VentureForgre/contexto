import csv
import json
import math

import pytest

import cost_model as cm

PRICES = cm.load_prices()
OPUS = PRICES["parsed"]["claude-opus-5-5"]
HAIKU = PRICES["parsed"]["claude-haiku-5-5"]
ASTRA = PRICES["parsed"]["gpt-6-astra"]
T5 = OPUS.cache_tier("5m")


def trace(turns, head=1000, gap=0.0, intra=1.0, **flags):
    """turns: list of request lists, each request (added, output, kept)."""
    return cm.Trace("t", head, intra, tuple(
        cm.Turn(gap if i else 0.0, tuple(cm.Req(*r) for r in reqs), **flags) for i, reqs in enumerate(turns)))


# Pricing


def test_price_explicit_mode_bills_misses_as_writes():
    b = cm.price(OPUS, T5, prompt=1_000_000, hit=600_000, output=1000)
    assert (b.uncached, b.write, b.read, b.output) == (0, 400_000, 600_000, 1000)
    assert b.usd == pytest.approx((400_000 * 5 + 600_000 * 0.20 + 1000 * 20) / 1e6)


def test_price_automatic_mode_bills_misses_as_input():
    m = cm.Model("x", "openai", "x", "small", 1_000_000, input=1.0, output=4.0, cache_read=0.1,
                 cache_mode="automatic", cache_tiers=(cm.CacheTier("t", 600, 1.0),), default_tier="t")
    b = cm.price(m, m.cache_tier(), prompt=10_000, hit=8000, output=100)
    assert (b.uncached, b.write, b.read) == (2000, 0, 8000)
    assert b.usd == pytest.approx((2000 * 1.0 + 8000 * 0.1 + 100 * 4.0) / 1e6)


def test_long_context_tier_multiplies_whole_request():
    t = HAIKU.cache_tier()
    under = cm.price(HAIKU, t, 100_000, 90_000, 100)
    over = cm.price(HAIKU, t, 100_001, 90_001, 100)
    assert under.usd == pytest.approx((10_000 * 0.125 + 90_000 * 0.01 + 100 * 0.5) / 1e6)
    assert over.usd == pytest.approx(5 * (10_000 * 0.125 + 90_001 * 0.01 + 100 * 0.5) / 1e6)


def test_price_file_values_carry_source_date_and_tag():
    raw = json.loads(cm.PRICES.read_text())

    def leaves(x):
        if isinstance(x, dict):
            if "value" in x:
                yield x
            else:
                for v in x.values():
                    yield from leaves(v)
        elif isinstance(x, list):
            for v in x:
                yield from leaves(v)

    found = list(leaves(raw["models"])) + list(leaves(raw["compaction"])) + list(leaves(raw["cli_ttl"]))
    assert len(found) > 40
    for leaf in found:
        assert leaf["source"].startswith("https://")
        assert leaf["retrieved"] == raw["retrieved"]
        assert leaf["tag"] in ("vendor-documented", "inferred") or leaf["tag"].startswith("measured")


# Cache engine and plain strategy


def test_plain_reads_previous_prompt_and_writes_only_the_tail():
    r = cm.run_plain(trace([[(100, 10, 10), (500, 20, 20)]]), OPUS, T5)
    # Request 1: head 1000 + 100 written. Request 2: reads 1100, writes kept 10 + tool 500.
    assert r.total.write == 1100 + 510
    assert r.total.read == 1100
    assert r.total.output == 30
    assert r.breaks.count == {}


def test_ttl_expiry_rewrites_whole_prefix_and_is_attributed():
    warm = cm.run_plain(trace([[(100, 10, 10)], [(100, 10, 10)]], gap=299), OPUS, T5)
    cold = cm.run_plain(trace([[(100, 10, 10)], [(100, 10, 10)]], gap=301), OPUS, T5)
    assert warm.total.read == 1100 and warm.total.write == 1100 + 110
    assert cold.total.read == 0 and cold.total.write == 1100 + 1210
    # Rewritten = head + first user message; the 10 output tokens were never cached.
    assert cold.breaks.count["ttl"] == 1 and cold.breaks.tokens["ttl"] == 1100


def test_compaction_resets_to_head_plus_seed():
    reset = cm.Reset("compaction", threshold=2600, instruction_tokens=50, output_tokens=300, seed_tokens=100)
    r = cm.run_plain(trace([[(500, 10, 10)]] * 5), OPUS, T5, reset)
    # Prompt with the next user message: 1500, 2010, 2520, then 3030 >= 2600 fires before turn 4.
    assert r.resets == 1
    fourth = r.turns[3]
    assert fourth.b.requests == 2 and fourth.b.output == 300 + 10
    assert fourth.prompt_tokens == 2530 + 50  # the summary request
    # The summary request reads the warm prefix; the next request reads only the head.
    assert fourth.b.read == 2520 + 1000  # last sent prompt, then the head
    assert r.breaks.count["compaction"] == 1 and r.breaks.tokens["compaction"] == 100
    # After the reset: head + seed + 510 for turn 4, then turn 5 appends 500.
    assert r.turns[4].prompt_tokens == 1000 + 100 + 510 + 500


def test_rollover_is_the_same_mechanism_with_its_own_cause():
    reset = cm.Reset("rollover", threshold=2600, output_tokens=100, seed_tokens=50)
    r = cm.run_plain(trace([[(500, 10, 10)]] * 4), OPUS, T5, reset)
    assert r.strategy == "rollover" and r.resets == 1 and r.breaks.count["rollover"] == 1


def test_plain_flags_turn_where_window_is_exceeded():
    small = cm.Model(**{**OPUS.__dict__, "context_window": 2500})
    r = cm.run_plain(trace([[(500, 10, 10)]] * 5), small, T5)
    assert r.over_window_turn == 3  # prompts 1500, 2010, 2520


def test_clearing_replaces_old_tool_results_and_breaks_cache():
    clear = cm.ClearPolicy(trigger=3000, keep=1, clear_at_least=0, placeholder_tokens=5)
    turns = [[(10, 1, 1), (1000, 1, 1)]] * 3
    r = cm.run_plain(trace(turns), OPUS, T5, clear=clear)
    # Turns 2 and 3 each push the prompt past 3000 with a new tool result, and keep=1 clears
    # the previous one: 3023 -> 2028 in turn 2, 3040 -> 2045 in turn 3.
    assert r.resets == 2
    assert r.breaks.count["clear"] == 2
    assert r.turns[1].prompt_tokens == 2028 and r.turns[2].prompt_tokens == 2045


def test_keepalive_keeps_cache_warm_through_idle_gap():
    tr = trace([[(100, 10, 10)], [(100, 10, 10)]], gap=1000)
    plain = cm.run_plain(tr, OPUS, T5)
    kept = cm.run_plain(tr, OPUS, T5, keepalive=cm.Keepalive(margin_s=30))
    assert kept.breaks.count["keepalive_ping"] == 3  # 1000 // 270
    assert "ttl" not in kept.breaks.count
    assert kept.total.read == 1100 + 3 * 1110
    assert kept.total.usd < plain.total.usd


def test_cold_start_forces_full_writes_on_first_requests():
    tr = trace([[(100, 10, 10), (100, 10, 10), (100, 10, 10)]])
    warm = cm.run_plain(tr, OPUS, T5)
    cold = cm.run_plain(tr, OPUS, T5, cold_start=2)
    assert warm.total.read == 1100 + 1210
    assert cold.total.read == 1210  # only the third request reads
    assert cold.breaks.count["cold_start"] == 1  # the second request re-writes a cacheable prefix


# Context virtual memory


def test_cvm_rebase_rewrites_everything_after_the_system_head():
    pol = cm.CvmPolicy(window_tokens=250, epoch_turns=3, opportunistic=False, respect_price_tier=False,
                       pinned_base=100, pinned_per_turn=0, pinned_max=100, stub_tokens_per_turn=7)
    r = cm.run_cvm(trace([[(100, 10, 10)]] * 5), OPUS, T5, pol)
    assert r.rebases == 1 and r.events["rebase"] == 1
    fourth = r.turns[3].b
    # Window keeps the last two 110-token turns (220 <= 250), one turn paged to a 7-token stub.
    assert fourth.read == 1000
    assert fourth.write == 100 + 7 + 220 + 100
    # Rewritten excludes turn 3's 10 output tokens, which were never cached.
    assert r.breaks.count["rebase"] == 1 and r.breaks.tokens["rebase"] == 100 + 7 + 220 - 10


def test_cvm_rebases_on_expiry_when_opportunistic():
    pol = cm.CvmPolicy(window_tokens=0, epoch_turns=100, opportunistic=True, respect_price_tier=False)
    tr = trace([[(100, 10, 10)]] * 3, gap=400)
    r = cm.run_cvm(tr, OPUS, T5, pol)
    assert r.events["rebase_on_expiry"] == 2
    assert r.breaks.count["ttl"] == 2


def test_cvm_prompt_stays_bounded_over_a_long_session():
    prof = cm.load_profile()
    tr = cm.generate_trace(prof, turns=600)
    pol = cm.CvmPolicy(**prof["cvm"]["policy"])
    r = cm.run_cvm(tr, OPUS, None, pol)
    plain = cm.run_plain(tr, OPUS)
    assert r.max_prompt < 250_000 < plain.max_prompt


def test_cvm_respects_long_context_price_threshold():
    prof = cm.load_profile()
    tr = cm.generate_trace(prof, turns=120)
    pol = cm.CvmPolicy(**{**prof["cvm"]["policy"], "window_tokens": 16_000})
    r = cm.run_cvm(tr, HAIKU, None, pol)
    over = [t for t in r.turns if t.prompt_tokens > HAIKU.long_context.threshold]
    assert len(over) < len(r.turns) * 0.1


def test_cvm_tail_pinning_rewrites_pinned_block_every_request():
    pol = cm.CvmPolicy(pin_position="tail", pinned_base=50, pinned_per_turn=0, pinned_max=50, epoch_turns=100,
                       opportunistic=False, respect_price_tier=False)
    r = cm.run_cvm(trace([[(100, 10, 10), (100, 10, 10)]] * 2), OPUS, T5, pol)
    assert r.breaks.count["pin"] == r.total.requests - 1
    assert r.breaks.tokens["pin"] == 50 * (r.total.requests - 1)


def test_recall_adds_one_round_trip():
    pol = cm.CvmPolicy(respect_price_tier=False)
    with_recall = cm.run_cvm(trace([[(100, 10, 10)]], recall=True), OPUS, T5, pol)
    assert with_recall.total.requests == 2
    assert with_recall.events["recall"] == 1


def test_cvm_bound_is_never_exceeded_even_mid_turn():
    pol = cm.CvmPolicy(window_tokens=5000, epoch_turns=100, opportunistic=False, respect_price_tier=False,
                       pinned_base=500, pinned_per_turn=0, pinned_max=500, bound_tokens=6000,
                       tool_output_excerpt_tokens=200)
    # Each turn: a user message, then two 2000-token tool results; the last turn has a 9000-token result.
    turns = [[(100, 10, 10), (2000, 10, 10), (2000, 10, 10)]] * 4 + [[(100, 10, 10), (9000, 10, 10)]]
    r = cm.run_cvm(trace(turns), OPUS, T5, pol)
    assert r.max_prompt <= 6000
    assert r.events["rebase_bound"] >= 1
    assert r.events["tool_output_paged"] == 1  # the 9000-token result alone cannot fit
    assert r.events["bound_exceeded"] == 0


def test_cvm_bound_stubs_active_turn_results_and_strips_its_thinking():
    pol = cm.CvmPolicy(window_tokens=5000, epoch_turns=100, opportunistic=False, respect_price_tier=False,
                       pinned_base=0, pinned_per_turn=0, pinned_max=0, bound_tokens=5000,
                       tool_output_excerpt_tokens=100)
    # One turn: four 1500-token tool results, each assistant step keeps 50 thinking of 60 tokens.
    reqs = (cm.Req(100, 60, 60, thinking=50),) + tuple(cm.Req(1500, 60, 60, thinking=50) for _ in range(4))
    tr = cm.Trace("t", 1000, 1.0, (cm.Turn(0.0, reqs),))
    r = cm.run_cvm(tr, OPUS, T5, pol)
    assert r.max_prompt <= 5000 and r.events["bound_exceeded"] == 0
    assert r.events["active_turn_result_stubbed"] >= 1
    assert r.events["thinking_stripped"] >= 50
    assert r.events["tool_output_paged"] == 0  # stubbing earlier results was enough


def test_mid_turn_page_strips_active_turn_thinking_even_without_stubbing():
    pol = cm.CvmPolicy(window_tokens=5000, epoch_turns=100, opportunistic=False, respect_price_tier=False,
                       pinned_base=0, pinned_per_turn=0, pinned_max=0, bound_tokens=3000)
    think = 50
    turn1 = (cm.Req(100, 60, 60, thinking=think), cm.Req(1200, 60, 60, thinking=think))
    turn2 = (cm.Req(100, 60, 60, thinking=think), cm.Req(1000, 60, 60, thinking=think))
    tr = cm.Trace("t", 1000, 1.0, (cm.Turn(0.0, turn1), cm.Turn(0.0, turn2)))
    r = cm.run_cvm(tr, OPUS, T5, pol)
    # Turn 2's tool result would reach 3580: paging turn 1 is enough, so nothing is stubbed,
    # but the page changes the prefix in front of turn 2's first step, so its thinking goes.
    assert r.events["mid_turn_page"] == 1
    assert r.events["active_turn_result_stubbed"] == 0
    assert r.events["active_turn_thinking_stripped"] == think
    assert r.max_prompt <= 3000


def test_strip_overhead_charged_only_when_thinking_is_in_context():
    pol = cm.CvmPolicy(respect_price_tier=False, strip_output_overhead=0.5)
    plain_reqs = trace([[(100, 100, 100)]])
    think_reqs = cm.Trace("t", 1000, 1.0, (cm.Turn(0.0, (cm.Req(100, 100, 100, thinking=40),)),))
    assert cm.run_cvm(plain_reqs, OPUS, T5, pol).total.output == 100
    assert cm.run_cvm(think_reqs, OPUS, T5, pol).total.output == 150


def test_trace_for_keeps_thinking_per_vendor():
    prof = cm.load_profile()
    anth = cm.trace_for(prof, OPUS, turns=10)
    oai = cm.trace_for(prof, ASTRA, turns=10)
    assert any(r.thinking for t in anth.turns for r in t.reqs)
    assert not any(r.thinking for t in oai.turns for r in t.reqs)


def test_policy_for_bound_rejects_a_bound_the_head_fills():
    pol = cm.CvmPolicy()
    assert cm.policy_for_bound(pol, 30_000, 42_000) is None
    fitted = cm.policy_for_bound(pol, 100_000, 42_000)
    assert fitted.bound_tokens == 100_000
    assert fitted.window_tokens + fitted.pinned_max + fitted.stubs_max_tokens < 100_000 - 42_000


def test_idle_compaction_fires_before_a_1h_expiry_only():
    reset = cm.Reset("compaction", threshold=10_000_000, output_tokens=100, seed_tokens=200)
    idle = cm.IdleCompaction(min_tokens=1000, lead_s=60)
    tr = trace([[(500, 10, 10)], [(500, 10, 10)]], gap=4000)
    hour = cm.run_plain(tr, OPUS, OPUS.cache_tier("1h"), reset, idle=idle)
    assert hour.events["idle_compaction"] == 1 and hour.resets == 1
    # The summary request ran at 3540 s, while the 1 h cache was still warm.
    assert hour.turns[1].b.read >= 1500
    five = cm.run_plain(tr, OPUS, T5, reset, idle=idle)
    assert five.events["idle_compaction"] == 0


def test_cli_compaction_under_cvm_counts_local_history():
    reset = cm.Reset("compaction", threshold=2600, instruction_tokens=50, output_tokens=100, seed_tokens=300)
    pol = cm.CvmPolicy(window_tokens=10_000, epoch_turns=100, opportunistic=False, respect_price_tier=False,
                       pinned_base=0, pinned_per_turn=0, pinned_max=0)
    # Local history: 1000 head + 510 per turn; turn 4 starts at 2530 + 500 >= 2600. Afterwards
    # local is 1300 + 510, and turn 5 starts at 1810 + 500 < 2600, so only one compaction.
    r = cm.run_cvm(trace([[(500, 10, 10)]] * 5), OPUS, T5, pol, cli_compaction=reset)
    assert r.events["cli_compaction"] == 1
    assert r.turns[3].b.output == 100 + 10
    assert r.breaks.count["cli_compaction"] == 1


def test_cvm_strips_thinking_from_kept_window_at_rebase():
    pol = cm.CvmPolicy(window_tokens=10_000, epoch_turns=2, opportunistic=False, respect_price_tier=False,
                       pinned_base=0, pinned_per_turn=0, pinned_max=0)
    turns = (tuple(cm.Req(100, 60, 60, thinking=50) for _ in range(1)),) * 3
    tr = cm.Trace("t", 1000, 1.0, tuple(cm.Turn(0.0, reqs) for reqs in turns))
    r = cm.run_cvm(tr, OPUS, T5, pol)
    assert r.events["thinking_stripped"] == 100  # two kept turns, 50 each
    # Turn 3 prompt: head + two stripped turns of 110 + its own 100.
    assert r.turns[2].prompt_tokens == 1000 + 2 * 110 + 100


def test_keep_thinking_trace_keeps_thinking_in_context():
    prof = cm.load_profile()
    a = cm.generate_trace(prof, turns=20)
    b = cm.generate_trace(prof, turns=20, keep_thinking=True)
    ra, rb = (r for t in a.turns for r in t.reqs), (r for t in b.turns for r in t.reqs)
    for x, y in zip(ra, rb):
        assert y.kept == x.kept + y.thinking and x.output == y.output


# Epoch rule


def test_optimal_epoch_minimizes_its_cost_function():
    rebuilt, g, n, w, r = 50_000, 5000, 4, 5.0, 0.2
    e_star = cm.optimal_epoch(rebuilt, g, n, w, r)

    def cost(e):
        return (w - r) * rebuilt / e + r * n * g * e / 2

    assert cost(e_star) <= min(cost(e_star * 0.9), cost(e_star * 1.1))
    assert e_star == pytest.approx(math.sqrt(2 * 4.8 * 50_000 / (0.2 * 4 * 5000)))


# Trace generation


def test_trace_is_deterministic_and_seed_sensitive():
    prof = cm.load_profile()
    assert cm.generate_trace(prof, turns=50) == cm.generate_trace(prof, turns=50)
    assert cm.generate_trace(prof, turns=50) != cm.generate_trace(prof, turns=50, seed=1)


def test_cvm_side_events_do_not_shift_shared_workload():
    prof = cm.load_profile()
    other = json.loads(json.dumps(prof))
    other["cvm"]["recall_rate"] = 0.9
    a, b = cm.generate_trace(prof, turns=40), cm.generate_trace(other, turns=40)
    assert [t.reqs for t in a.turns] == [t.reqs for t in b.turns]


# Lane 1 loader


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


ROW = dict(turn=1, input_tokens=3, cache_creation_input_tokens=1000, cache_read_input_tokens=0, output_tokens=50,
           request_bytes=4000, message_count=1, cli="claude-code", model="m", endpoint="/v1/messages",
           ts="2026-10-15T23:00:00.000000Z")


def test_load_measured_jsonl_and_csv_with_empty_cache_creation(tmp_path):
    rows = cm.load_measured(cm.HERE / "testdata" / "lane1_sample.jsonl")
    assert len(rows) == 6 and rows[0]["cache_creation_input_tokens"] == 42_384
    codex = cm.load_measured(cm.HERE / "testdata" / "lane1_codex_sample.csv")
    assert [r["cache_creation_input_tokens"] for r in codex] == [None, None, None]
    missing = {k: v for k, v in ROW.items() if k != "cache_creation_input_tokens"}
    assert cm.load_measured(write_jsonl(tmp_path / "m.jsonl", [missing]))[0]["cache_creation_input_tokens"] is None


@pytest.mark.parametrize("bad", [
    {k: v for k, v in ROW.items() if k != "output_tokens"},
    {**ROW, "input_tokens": ""},
    {**ROW, "message_count": "many"},
    {**ROW, "ts": "yesterday"},
])
def test_load_measured_rejects_bad_rows(tmp_path, bad):
    with pytest.raises(ValueError):
        cm.load_measured(write_jsonl(tmp_path / "bad.jsonl", [bad]))


def test_load_measured_rejects_bad_json_and_missing_file(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("{not json\n")
    with pytest.raises(ValueError):
        cm.load_measured(p)
    with pytest.raises(ValueError):
        cm.load_measured(tmp_path / "absent.jsonl")


def test_measured_codex_file_reported_writes_and_empty_bytes():
    path = cm.HERE / "data" / "lane1_pr10_ae6e91a_codex_requests.csv"
    rows = cm.load_measured(path)
    assert len(rows) == 11
    assert all(r["cache_creation_input_tokens"] == 0 and r["request_bytes"] is None for r in rows)
    unknown = cm.load_measured(path, creation_unknown=True)
    assert all(r["cache_creation_input_tokens"] is None for r in unknown)
    assert cm.price_measured(rows, ASTRA).usd < cm.price_measured(unknown, ASTRA, missing_creation="write").usd
    tr, _ = cm.trace_from_measured(rows, "codex")
    assert len(tr.turns) == 6 and tr.head_tokens == 19693 + 6784


def test_tile_trace_repeats_turns_after_the_first():
    tr = trace([[(100, 1, 1)], [(200, 1, 1)], [(300, 1, 1)]])
    tiled = cm.tile_trace(tr, 6)
    assert [t.reqs[0].added for t in tiled.turns] == [100, 200, 300, 200, 300, 200]


def test_price_measured_uses_reported_buckets():
    rows = [dict(ROW, input_tokens=10, cache_creation_input_tokens=1000, cache_read_input_tokens=5000,
                 output_tokens=100)]
    b = cm.price_measured(rows, OPUS)
    assert b.usd == pytest.approx((10 * 4 + 1000 * 5 + 5000 * 0.2 + 100 * 20) / 1e6)


def test_price_measured_bounds_for_missing_cache_writes():
    rows = [dict(ROW, input_tokens=1000, cache_creation_input_tokens=None, cache_read_input_tokens=9000,
                 output_tokens=0)]
    hi = cm.price_measured(rows, ASTRA, missing_creation="write")
    lo = cm.price_measured(rows, ASTRA, missing_creation="input")
    assert hi.usd == pytest.approx((1000 * 12.5 + 9000 * 1.0) / 1e6)
    assert lo.usd == pytest.approx((1000 * 10 + 9000 * 1.0) / 1e6)


def test_trace_from_measured_growth_gaps_and_shrinks():
    rows = cm.load_measured(cm.HERE / "testdata" / "lane1_sample.jsonl")
    tr, shrinks = cm.trace_from_measured(rows, "s")
    assert tr.head_tokens == 42_387
    assert [len(t.reqs) for t in tr.turns] == [2, 2, 1, 1]
    assert tr.turns[0].reqs[1].added == 1450
    assert tr.turns[1].gap_s == pytest.approx(78.0)
    assert shrinks == 1 and tr.turns[3].reqs[0].added == 0


# Script and doc


def test_doc_reproduces_from_script():
    assert cm.main(["--check-doc"]) == 0


def test_per_turn_csv(tmp_path):
    out = tmp_path / "pt.csv"
    assert cm.main(["--per-turn-csv", str(out)]) == 0
    rows = list(csv.DictReader(out.read_text().splitlines()))
    models, turns = len(PRICES["report"]["models"]), cm.load_profile()["turns"]
    assert len(rows) == models * len(cm.STRATEGIES) * turns


def test_measured_cli(capsys):
    assert cm.main(["--measured", str(cm.HERE / "testdata" / "lane1_codex_sample.csv")]) == 0
    assert "gpt-6-astra: observed" in capsys.readouterr().out
