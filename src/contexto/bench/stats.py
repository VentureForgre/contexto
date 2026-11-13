"""The statistics the benchmark report uses. Pure functions: no I/O, and the same input always gives
the same output (the two resampling methods take a seed).

Probes inside one session are not independent, so the report gives two views: probes pooled across
seeds (``wilson``, ``paired``, ``mcnemar_exact``) and seeds as the unit (``seed_level``). Run-to-run
noise comes from repeats of one seed (``repeat_noise``).
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Any

Z95 = 1.959963984540054  # the 0.975 quantile of the standard normal distribution
SIGN_FLIP_EXACT_MAX = 20  # seeds up to which the sign-flip test enumerates every pattern


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """The Wilson score interval for a proportion of ``k`` successes in ``n`` trials, 95% by default.

    Wilson, E. B. (1927), "Probable inference, the law of succession, and statistical inference",
    Journal of the American Statistical Association 22: 209-212. With no trials it returns (0, 1).
    """
    if not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """The exact two-sided McNemar p value from the two discordant counts.

    Under the null hypothesis each discordant pair falls either way with probability 1/2, so the
    smaller count is Binomial(b + c, 1/2); the p value is twice its lower tail, capped at 1.
    McNemar, Q. (1947), Psychometrika 12: 153-157; the exact binomial form is in Agresti, A. (2013),
    Categorical Data Analysis, 3rd edition, section 11.1. With no discordant pairs it returns 1.
    """
    if b < 0 or c < 0:
        raise ValueError(f"need counts of 0 or more, got b={b}, c={c}")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2 * tail / (1 << n))


def _tango_z(b: int, c: int, n: int, delta: float) -> float:
    """Tango's score statistic for the hypothesis that the paired difference is ``delta``."""
    lead = 2 * n
    mid = -b - c + (2 * n - b + c) * delta
    last = -c * delta * (1 - delta)
    q21 = (math.sqrt(max(0.0, mid * mid - 4 * lead * last)) - mid) / (2 * lead)  # the restricted estimate
    variance = n * (2 * q21 + delta * (1 - delta))
    excess = b - c - n * delta
    if variance <= 0:
        return 0.0 if excess == 0 else math.copysign(math.inf, excess)
    return excess / math.sqrt(variance)


def tango_interval(b: int, c: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Tango's score confidence interval for a difference of paired proportions, 95% by default.

    ``b`` pairs are right only on the first side, ``c`` only on the second, out of ``n`` pairs; the
    difference is ``(b - c) / n``. The limits are the differences at which the score statistic
    equals ``z`` and ``-z``, found by bisection. Unlike the Wald interval it stays inside [-1, 1]
    and has a sensible width when a discordant count is small or zero. Tango, T. (1998),
    "Equivalence test and confidence interval for the difference in proportions for the
    paired-sample design", Statistics in Medicine 17: 891-908.
    """
    if b < 0 or c < 0 or n <= 0 or b + c > n:
        raise ValueError(f"need b, c >= 0 and b + c <= n with n > 0, got b={b}, c={c}, n={n}")
    estimate = (b - c) / n

    def limit(target: float, low: float, high: float) -> float:
        # The statistic falls as delta rises: it is above ``target`` at ``low`` and below it at ``high``.
        for _ in range(200):
            middle = (low + high) / 2
            if _tango_z(b, c, n, middle) > target:
                low = middle
            else:
                high = middle
        return (low + high) / 2

    lower = -1.0 if _tango_z(b, c, n, -1.0) <= z else limit(z, -1.0, estimate)
    upper = 1.0 if _tango_z(b, c, n, 1.0) >= -z else limit(-z, estimate, 1.0)
    return (lower, upper)


def paired(a: Sequence[bool], b: Sequence[bool]) -> dict[str, Any]:
    """Compare two arms on the same probes: ``a[i]`` and ``b[i]`` say whether each arm got probe i right.

    Returns the 2x2 table (``both``, ``a_only``, ``b_only``, ``neither``), both accuracies, the
    difference ``accuracy_a - accuracy_b``, its 95% interval by Tango's score method
    (``tango_interval``) and the exact two-sided McNemar p value (``mcnemar_exact``).
    """
    if len(a) != len(b):
        raise ValueError(f"the two arms have {len(a)} and {len(b)} results: they must be aligned")
    if not a:
        raise ValueError("no pairs to compare")
    n = len(a)
    both = sum(1 for x, y in zip(a, b) if x and y)
    a_only = sum(1 for x, y in zip(a, b) if x and not y)
    b_only = sum(1 for x, y in zip(a, b) if y and not x)
    return {
        "n": n, "both": both, "a_only": a_only, "b_only": b_only, "neither": n - both - a_only - b_only,
        "accuracy_a": (both + a_only) / n, "accuracy_b": (both + b_only) / n, "difference": (a_only - b_only) / n,
        "interval": tango_interval(a_only, b_only, n), "interval_method": "Tango score (1998)",
        "mcnemar_p": mcnemar_exact(a_only, b_only),
    }


def seed_level(differences: Sequence[float], resamples: int = 10_000, seed: int = 0) -> dict[str, Any]:
    """Treat each seed as one observation: ``differences`` holds one paired difference per seed.

    Returns the mean difference, a two-sided sign-flip permutation p value and a percentile
    bootstrap 95% interval over seeds.

    Sign-flip test (Fisher's randomization test for paired data; Good, P. (2005), Permutation,
    Parametric and Bootstrap Tests of Hypotheses, 3rd edition, section 3.1): under the null
    hypothesis each seed's difference is as likely to have either sign, so the p value is the share
    of sign patterns whose mean is at least as far from zero as the observed mean. Every one of the
    ``2 ** n`` patterns is counted for up to ``SIGN_FLIP_EXACT_MAX`` seeds; above that ``resamples``
    random patterns are drawn and the p value is ``(hits + 1) / (resamples + 1)``. The smallest
    exact p value with n seeds is ``2 / 2 ** n``, so fewer than six seeds cannot reach 0.05.

    Bootstrap (Efron, B. and Tibshirani, R. (1993), An Introduction to the Bootstrap, section 13.3):
    ``resamples`` samples of n seeds drawn with replacement; the interval is the 2.5th and 97.5th
    percentiles of their means (the values at sorted positions ``floor(0.025 * resamples)`` and
    ``ceil(0.975 * resamples) - 1``). With fewer than two seeds there is no interval (None).
    """
    values = [float(d) for d in differences]
    n = len(values)
    if n == 0:
        raise ValueError("no seeds")
    if resamples < 1:
        raise ValueError("need at least one resample")
    total = sum(values)
    tolerance = 1e-9 * max(1.0, sum(abs(v) for v in values))
    threshold = abs(total) - tolerance
    if n <= SIGN_FLIP_EXACT_MAX:
        sums = [0.0]
        for v in values:
            sums = [s + v for s in sums] + [s - v for s in sums]
        p_value = sum(1 for s in sums if abs(s) >= threshold) / len(sums)
        method = "exact"
    else:
        flips = random.Random(seed + 1)
        hits = sum(1 for _ in range(resamples)
                   if abs(sum(v if flips.random() < 0.5 else -v for v in values)) >= threshold)
        p_value = (hits + 1) / (resamples + 1)
        method = "monte-carlo"
    interval = None
    if n >= 2:
        draw = random.Random(seed)
        means = sorted(sum(draw.choices(values, k=n)) / n for _ in range(resamples))
        interval = (means[math.floor(0.025 * resamples)], means[math.ceil(0.975 * resamples) - 1])
    return {"seeds": n, "differences": values, "mean": total / n, "sign_flip_p": p_value, "sign_flip_method": method,
            "bootstrap_interval": interval, "resamples": resamples}


def repeat_noise(runs: Sequence[Sequence[bool]]) -> dict[str, Any]:
    """Run-to-run noise from repeats of one seed and arm: ``runs[r][i]`` says whether repeat r got
    probe i right.

    ``flip_rate`` is the share of probes the repeats did not all agree on. ``pairwise_agreement`` is
    the mean, over every pair of repeats, of the share of probes the two agree on (the observed
    agreement of Fleiss, J. L. (1971), Psychological Bulletin 76: 378-382, without the chance
    correction). The accuracy figures are over repeats; ``accuracy_sd`` is the sample standard
    deviation (n - 1 in the denominator). With fewer than two repeats the flip rate, agreement and
    standard deviation are None: one run says nothing about noise.
    """
    if not runs:
        raise ValueError("no runs")
    n = len(runs[0])
    if n == 0 or any(len(r) != n for r in runs):
        raise ValueError("the repeats must be aligned, non-empty lists of the same length")
    accuracies = [sum(1 for x in r if x) / n for r in runs]
    out: dict[str, Any] = {
        "repeats": len(runs), "probes": n, "accuracies": accuracies, "accuracy_min": min(accuracies),
        "accuracy_max": max(accuracies), "accuracy_range": max(accuracies) - min(accuracies),
        "flips": None, "flip_rate": None, "pairwise_agreement": None, "accuracy_sd": None,
    }
    if len(runs) < 2:
        return out
    mean = sum(accuracies) / len(runs)
    flips = sum(1 for i in range(n) if len({bool(r[i]) for r in runs}) > 1)
    pairs = [(x, y) for i, x in enumerate(runs) for y in runs[i + 1:]]
    agreement = sum(sum(1 for u, v in zip(x, y) if bool(u) == bool(v)) / n for x, y in pairs) / len(pairs)
    out.update({"flips": flips, "flip_rate": flips / n, "pairwise_agreement": agreement,
                "accuracy_sd": math.sqrt(sum((x - mean) ** 2 for x in accuracies) / (len(runs) - 1))})
    return out
