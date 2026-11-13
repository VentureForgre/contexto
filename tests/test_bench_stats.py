"""The benchmark's statistics (D66). Expected values are published worked examples or are worked out
by hand in the comments; none comes from running the code under test."""

from __future__ import annotations

import math

import pytest

from contexto.bench import stats

# Wilson


@pytest.mark.parametrize("k, n, low, high", [
    # Newcombe, R. G. (1998), "Two-sided confidence intervals for the single proportion", Statistics
    # in Medicine 17: 857-872, Table I, method 3 (Wilson score, no continuity correction).
    (81, 263, 0.2553, 0.3662),
    (15, 148, 0.0624, 0.1605),
    (0, 20, 0.0000, 0.1611),
    (1, 29, 0.0061, 0.1718),
])
def test_wilson_reproduces_newcombes_table(k, n, low, high):
    interval = stats.wilson(k, n)
    assert interval == pytest.approx((low, high), abs=5e-5)


def test_wilson_by_hand():
    # The limits are the roots p of (p_hat - p)^2 = z^2 p (1 - p) / n. For k = 5, n = 10, z^2 = 3.841459:
    # (1 + 0.3841459) p^2 - (1 + 0.3841459) p + 0.25 = 0, so p = 0.5 -/+ sqrt(0.25 - 0.25 / 1.3841459)
    # = 0.5 -/+ sqrt(0.0693833) = 0.5 -/+ 0.263407.
    assert stats.wilson(5, 10) == pytest.approx((0.236593, 0.763407), abs=1e-6)
    # k = n = 10: the upper limit is 1 and the lower is n / (n + z^2) = 10 / 13.841459 = 0.722467.
    assert stats.wilson(10, 10) == pytest.approx((0.722467, 1.0), abs=1e-6)
    assert stats.wilson(0, 0) == (0.0, 1.0)
    low, high = stats.wilson(52, 54)
    assert 0 <= low < 52 / 54 < high <= 1


def test_wilson_refuses_impossible_counts():
    for k, n in ((3, 2), (-1, 2)):
        with pytest.raises(ValueError):
            stats.wilson(k, n)


# McNemar


def test_mcnemar_exact_regression_fixture():
    # Live run #4: 52 against 47 of 54 with disjoint misses, so the discordant counts are 7 and 2.
    # P(X <= 2) for X ~ Binomial(9, 1/2) is (1 + 9 + 36) / 512 = 46 / 512; twice that is 92 / 512.
    assert stats.mcnemar_exact(7, 2) == 92 / 512 == 0.1796875
    assert round(stats.mcnemar_exact(7, 2), 4) == 0.1797
    assert stats.mcnemar_exact(2, 7) == 92 / 512  # symmetric


@pytest.mark.parametrize("b, c, p", [
    (0, 0, 1.0),  # no discordant pairs
    (1, 1, 1.0), (4, 4, 1.0), (13, 13, 1.0),  # equal counts
    (1, 0, 1.0),  # 2 * (1 / 2)
    (5, 0, 2 / 32),  # 2 * (1 / 32)
    (10, 1, 24 / 2048),  # 2 * (1 + 11) / 2048
    (9, 3, 598 / 4096),  # 2 * (1 + 12 + 66 + 220) / 4096
])
def test_mcnemar_exact_by_hand(b, c, p):
    assert stats.mcnemar_exact(b, c) == p


def test_mcnemar_exact_handles_large_counts_and_refuses_negative_ones():
    assert 0 < stats.mcnemar_exact(700, 400) < 1e-15
    assert stats.mcnemar_exact(2_000, 2_000) == 1.0
    with pytest.raises(ValueError):
        stats.mcnemar_exact(-1, 2)


# Paired differences


def _arms(both: int, a_only: int, b_only: int, neither: int) -> tuple[list[bool], list[bool]]:
    a = [True] * both + [True] * a_only + [False] * b_only + [False] * neither
    b = [True] * both + [False] * a_only + [True] * b_only + [False] * neither
    return a, b


def test_tango_interval_reproduces_tangos_own_example():
    # Tango (1998), Statistics in Medicine 17: 891-908, section 6.2 and Table VII: sleeping
    # difficulties in 32 matched pairs, cells 4, 9, 3, 16. "The proposed score-based confidence
    # interval is -0.027 to 0.390."
    assert stats.tango_interval(9, 3, 32) == pytest.approx((-0.027, 0.390), abs=5e-4)
    result = stats.paired(*_arms(4, 9, 3, 16))
    assert (result["n"], result["both"], result["a_only"], result["b_only"], result["neither"]) == (32, 4, 9, 3, 16)
    assert result["accuracy_a"] == 13 / 32 and result["accuracy_b"] == 7 / 32 and result["difference"] == 6 / 32
    assert result["interval"] == pytest.approx((-0.027, 0.390), abs=5e-4)
    assert result["interval_method"] == "Tango score (1998)"
    assert result["mcnemar_p"] == 598 / 4096


def test_tango_interval_reproduces_a_second_published_table():
    # The 2x2 table 1, 1, 7, 12 (21 pairs) in the paired-contrasts vignette of the R package ratesci,
    # which prints -0.5172 and -0.0260 for the Tango asymptotic score interval ("AS(Tango)").
    assert stats.tango_interval(1, 7, 21) == pytest.approx((-0.5172, -0.0260), abs=5e-5)
    assert stats.paired(*_arms(1, 1, 7, 12))["difference"] == pytest.approx(-0.2857, abs=5e-5)


def test_tango_interval_with_no_discordant_pairs_by_hand():
    # With b = c = 0 the restricted estimate is 0 on one side, the statistic is -sqrt(n d / (1 - d))
    # for a difference d > 0, and it equals -z at d = z^2 / (n + z^2). For n = 54:
    # 3.841459 / 57.841459 = 0.066414. The interval is symmetric.
    low, high = stats.tango_interval(0, 0, 54)
    assert (low, high) == pytest.approx((-0.066414, 0.066414), abs=1e-6)


def test_tango_interval_at_the_edges():
    assert stats.tango_interval(10, 0, 10)[1] == 1.0  # every pair favours the first side
    assert stats.tango_interval(0, 10, 10)[0] == -1.0
    low, high = stats.tango_interval(10, 0, 10)
    assert 0 < low < 1  # 10 of 10 discordant one way excludes zero
    mirrored = stats.tango_interval(0, 10, 10)
    assert mirrored == pytest.approx((-high, -low), abs=1e-9)
    for b, c, n in ((1, 0, 5), (3, 2, 40), (0, 1, 1), (25, 25, 50)):
        low, high = stats.tango_interval(b, c, n)
        assert -1 <= low <= (b - c) / n <= high <= 1
    with pytest.raises(ValueError):
        stats.tango_interval(3, 3, 5)
    with pytest.raises(ValueError):
        stats.tango_interval(0, 0, 0)


def test_paired_on_the_run_4_fixture():
    # 52 and 47 of 54 with disjoint misses: 45 right in both, 7 only in the first arm, 2 only in the second.
    result = stats.paired(*_arms(45, 7, 2, 0))
    assert result["accuracy_a"] == 52 / 54 and result["accuracy_b"] == 47 / 54
    assert result["difference"] == 5 / 54
    assert round(result["mcnemar_p"], 4) == 0.1797
    low, high = result["interval"]
    assert low < 0 < 5 / 54 < high  # the interval includes zero, as the p value says
    assert high - low < 0.25


def test_paired_refuses_unaligned_or_empty_arms():
    with pytest.raises(ValueError, match="aligned"):
        stats.paired([True], [True, False])
    with pytest.raises(ValueError, match="no pairs"):
        stats.paired([], [])


# Seeds as the unit


def test_seed_level_sign_flip_by_hand():
    # Differences 0.1, 0.2, 0.3: the eight sign patterns sum to +/-0.6, +/-0.4, +/-0.2 and 0 twice
    # (0.1 + 0.2 - 0.3 and its mirror). Only +/-0.6 is as far from zero as the observed 0.6: p = 2 / 8.
    result = stats.seed_level([0.1, 0.2, 0.3])
    assert result["mean"] == pytest.approx(0.2)
    assert result["sign_flip_p"] == 0.25 and result["sign_flip_method"] == "exact"
    assert result["seeds"] == 3
    # Five seeds that all favour one arm: only the all-plus and all-minus patterns reach the observed sum.
    assert stats.seed_level([0.05, 0.02, 0.04, 0.01, 0.03])["sign_flip_p"] == 2 / 32
    # 1, -1: every pattern sums to 0 or +/-2, all at least as far from zero as the observed 0.
    assert stats.seed_level([1.0, -1.0])["sign_flip_p"] == 1.0
    # 3, 1, -1: sums are +/-5, +/-3 (twice each way), +/-1; the observed 3 is reached by 6 of 8.
    assert stats.seed_level([3.0, 1.0, -1.0])["sign_flip_p"] == 6 / 8
    assert stats.seed_level([0.0, 0.0, 0.0])["sign_flip_p"] == 1.0


def test_seed_level_bootstrap_by_hand():
    # Equal differences: every resample has the same mean.
    result = stats.seed_level([0.05] * 6)
    assert result["bootstrap_interval"] == pytest.approx((0.05, 0.05))
    # 0 and 1: a resample's mean is 0, 0.5 or 1 with probability 1/4, 1/2, 1/4, so the 2.5th
    # percentile is 0 and the 97.5th is 1.
    assert stats.seed_level([0.0, 1.0])["bootstrap_interval"] == (0.0, 1.0)
    # One seed: no interval, and the sign-flip test cannot reject.
    single = stats.seed_level([0.3])
    assert single["bootstrap_interval"] is None and single["sign_flip_p"] == 1.0 and single["mean"] == 0.3
    values = [0.02, -0.01, 0.05, 0.03, 0.00, 0.04, 0.01]
    low, high = stats.seed_level(values)["bootstrap_interval"]
    assert min(values) <= low < sum(values) / len(values) < high <= max(values)


def test_seed_level_is_deterministic_and_seeded():
    values = [0.02, -0.01, 0.05, 0.03, 0.00, 0.04, 0.01]
    assert stats.seed_level(values) == stats.seed_level(values)
    # The seed picks the resamples: with few of them, different seeds give different intervals.
    assert len({stats.seed_level(values, resamples=20, seed=k)["bootstrap_interval"] for k in range(8)}) > 1
    assert stats.seed_level(values, resamples=20, seed=3) == stats.seed_level(values, resamples=20, seed=3)
    assert stats.seed_level(values, seed=1)["sign_flip_p"] == stats.seed_level(values, seed=2)["sign_flip_p"]


def test_seed_level_monte_carlo_above_twenty_seeds():
    values = [0.01 * (k + 1) for k in range(21)]  # all positive: the exact p value would be 2 / 2**21
    result = stats.seed_level(values, resamples=2_000)
    assert result["sign_flip_method"] == "monte-carlo" and result["resamples"] == 2_000
    assert result["sign_flip_p"] == 1 / 2_001  # no random pattern reaches the observed sum; (0 + 1) / (2000 + 1)
    assert stats.seed_level(values, resamples=2_000) == result
    assert stats.seed_level(values[:20])["sign_flip_method"] == "exact"
    assert stats.seed_level(values[:20])["sign_flip_p"] == 2 / 2 ** 20
    balanced = stats.seed_level([0.01, -0.01] * 11, resamples=2_000)
    assert balanced["sign_flip_p"] == 1.0  # the observed sum is 0


def test_seed_level_refuses_no_seeds():
    with pytest.raises(ValueError):
        stats.seed_level([])
    with pytest.raises(ValueError):
        stats.seed_level([0.1], resamples=0)


# Repeats


def test_repeat_noise_by_hand():
    runs = [[True, True, False, False], [True, False, False, True], [True, True, False, True]]
    result = stats.repeat_noise(runs)
    assert result["repeats"] == 3 and result["probes"] == 4
    assert result["accuracies"] == [0.5, 0.5, 0.75]
    # Probes 2 and 4 are not unanimous: 2 of 4.
    assert result["flips"] == 2 and result["flip_rate"] == 0.5
    # Pairs agree on 2, 3 and 3 of 4 probes: (0.5 + 0.75 + 0.75) / 3.
    assert result["pairwise_agreement"] == pytest.approx(2 / 3)
    assert (result["accuracy_min"], result["accuracy_max"], result["accuracy_range"]) == (0.5, 0.75, 0.25)
    # Mean 0.583333; squared deviations 0.006944, 0.006944, 0.027778 sum to 0.041667; / 2 = 0.020833; sqrt.
    assert result["accuracy_sd"] == pytest.approx(0.144338, abs=1e-6)
    assert result["accuracy_sd"] == pytest.approx(math.sqrt(1 / 48))


def test_repeat_noise_with_identical_and_single_runs():
    same = stats.repeat_noise([[True, False, True]] * 4)
    assert same["flip_rate"] == 0 and same["pairwise_agreement"] == 1 and same["accuracy_sd"] == 0
    assert same["accuracy_range"] == 0
    one = stats.repeat_noise([[True, False]])
    assert one["repeats"] == 1 and one["accuracies"] == [0.5]
    assert one["flip_rate"] is None and one["pairwise_agreement"] is None and one["accuracy_sd"] is None
    opposite = stats.repeat_noise([[True, False], [False, True]])
    assert opposite["flip_rate"] == 1 and opposite["pairwise_agreement"] == 0 and opposite["accuracy_range"] == 0


def test_repeat_noise_refuses_unaligned_runs():
    for runs in ([], [[]], [[True], [True, False]]):
        with pytest.raises(ValueError):
            stats.repeat_noise(runs)
