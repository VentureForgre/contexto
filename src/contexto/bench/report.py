"""Turn bench output directories into ``results.json`` and ``results.md``.

``build(out_dirs)`` reads one or more live output directories (``contexto.bench.records``) and
returns the results as a dict; ``render(results)`` is the plain-text report of that dict;
``write(out_dir)`` writes both, and the charts, into a directory. The live run calls
``write(out)`` when it finishes, and ``contexto bench --report DIR [DIR ...]`` calls it on
directories that already exist.

What is counted:

- The per-arm figures and the comparisons use repeat 0 of each seed only, so the seed that was
  repeated is not counted more than once. The repeats are used for the noise figures alone.
- Two arms are compared only on seeds where both arms' units are ``complete``; the probes left out
  are counted and the reason is given.
- Cost is the measured token classes priced per request (``contexto.bench.prices``) at every model
  in the price table, so the comparison does not depend on one model's prices.

The "What this run shows" text is produced by fixed rules (``_verdict``), not written by hand.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contexto.bench import prices as price_tables
from contexto.bench import records, stats
from contexto.bench.d27 import BYTES_PER_TOKEN_LOG, BYTES_PER_TOKEN_TEXT, REPLY_TOKENS

FORMAT = 1
ALPHA = 0.05
ARM_ORDER = ("contexto", "full", "plain")
PAIRS = (("contexto", "full"), ("contexto", "plain"), ("plain", "full"))
LABELS = {"contexto": "Contexto", "full": "Full context", "plain": "Plain Claude Code"}
INLINE = {"contexto": "Contexto", "full": "full context", "plain": "plain Claude Code"}
DEFAULT_CACHE_TTL = "5m"
MODEL_BLOCKS = ("arms", "pairs", "noise", "request_series", "plain_compaction", "all_requests")
LIMITS = (
    "The sessions are synthetic: generated log text with facts planted in it, not recorded work. A result here "
    "says how the arms handle this kind of session and may not carry over to real ones.",
    "Scoring is exact match after normalisation against the generator's answers, with no model judge. A right "
    "answer in other words is counted wrong, in every arm alike.",
    "One version of Claude Code and one version of Contexto were run. Another version can behave differently.",
    "A difference between arms that is smaller than the run-to-run noise is not a finding.",
)


class ReportError(ValueError):
    """Output directories that cannot be reported on: they disagree with each other or with
    themselves."""


Key = tuple[str, int, int]  # arm, seed, repeat


@dataclass
class _Group:
    """Everything recorded for one model, across the directories given."""

    model: str
    first_seed: int
    cache_ttl: str | None
    plain_set: dict[str, Any] | None
    seeds: list[int] = field(default_factory=list)
    arms: list[str] = field(default_factory=list)
    units: dict[Key, dict[str, Any]] = field(default_factory=dict)
    probes: dict[Key, dict[str, dict[str, Any]]] = field(default_factory=dict)
    requests: dict[Key, list[dict[str, Any]]] = field(default_factory=dict)
    ttl: dict[Key, str] = field(default_factory=dict)


def label(arm: str) -> str:
    """An arm's name as a row or series label."""
    return LABELS.get(arm, arm)


def _inline(arm: str) -> str:
    return INLINE.get(arm, arm)


def _unit_words(model: str, key: Key) -> str:
    return f"{model}, {key[0]}, seed {key[1]}, repeat {key[2]}"


def _input(request: Mapping[str, Any]) -> int:
    return request["uncached"] + request["cache_write"] + request["cache_read"]


# Reading and grouping


def _check_poolable(runs: Sequence[records.Run]) -> None:
    first = runs[0].manifest
    shown = {"bound": lambda m: f"{m['bound']:,} tokens", "protocol": lambda m: str(m["protocol"]),
             "turn_kbytes": lambda m: f"{m['turn_kbytes'] * 1_000:,} bytes"}
    names = {"bound": "bound", "protocol": "protocol", "turn_kbytes": "turn size"}
    problems = []
    for position, run in enumerate(runs[1:], start=2):
        for name, show in shown.items():
            if run.manifest[name] != first[name]:
                problems.append(f"{names[name]}: {show(first)} in directory 1 and {show(run.manifest)} in "
                                f"directory {position}")
    if problems:
        raise ReportError("cannot pool these directories, because they were not run on the same session: "
                          + "; ".join(problems) + ". Report them separately.")


def _group(runs: Sequence[records.Run]) -> dict[str, _Group]:
    groups: dict[str, _Group] = {}
    origin: dict[tuple[str, Key], int] = {}
    for position, run in enumerate(runs, start=1):
        manifest = run.manifest
        model = manifest["model"]
        if model is None:
            raise ReportError(f"{run.path / 'manifest.json'}: field 'model': a live run names its model, got null")
        group = groups.setdefault(model, _Group(model, manifest["seed"], manifest["cache_ttl"],
                                                manifest["plain_compaction"]))
        for seed in [*manifest["seeds"], *(u["seed"] for u in manifest["units"])]:
            if seed not in group.seeds:
                group.seeds.append(seed)
        for arm in [*manifest["arms"], *(u["arm"] for u in manifest["units"])]:
            if arm not in group.arms:
                group.arms.append(arm)
        for unit in manifest["units"]:
            key = records.unit_key(unit)
            if (model, key) in origin:
                raise ReportError(f"the unit {_unit_words(model, key)} is in directory {origin[(model, key)]} and "
                                  f"directory {position}: a unit may be recorded once")
            origin[(model, key)] = position
            group.units[key] = unit
            group.ttl[key] = manifest["cache_ttl"] or DEFAULT_CACHE_TTL
        for row in run.probes:
            group.probes.setdefault(records.unit_key(row), {})[row["id"]] = row
        for row in run.requests:
            group.requests.setdefault(records.unit_key(row), []).append(row)
    for group in groups.values():
        group.arms.sort(key=lambda arm: ARM_ORDER.index(arm) if arm in ARM_ORDER else len(ARM_ORDER))
        for rows in group.requests.values():
            rows.sort(key=lambda r: r["n"])
        _check_group(group)
    return groups


def _check_group(group: _Group) -> None:
    """A complete unit has probe records, and the complete units of one seed answered the same probes."""
    reference: dict[int, tuple[Key, set[str]]] = {}
    for key, unit in group.units.items():
        if unit["status"] != "complete":
            continue
        ids = set(group.probes.get(key, {}))
        if not ids:
            raise ReportError(f"the unit {_unit_words(group.model, key)} is complete in manifest.json but "
                              "probes.jsonl has no records for it")
        other_key, other_ids = reference.setdefault(key[1], (key, ids))
        if ids != other_ids:
            raise ReportError(f"the units of seed {key[1]} did not answer the same probes: "
                              f"{_unit_words(group.model, other_key)} has {len(other_ids)} probes and "
                              f"{_unit_words(group.model, key)} has {len(ids)}. A probes.jsonl cut short gives this.")


# The blocks of one model


def _proportion(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n, k = len(rows), sum(1 for r in rows if r["correct"])
    return {"probes": n, "correct": k, "accuracy": k / n if n else None,
            "interval": list(stats.wilson(k, n)) if n else None}


def _by_class(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        out.setdefault(row["cls"], []).append(row)
    return out


def _tokens(requests: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out = {"requests": len(requests)}
    for name in ("uncached", "cache_write", "cache_read", "output"):
        out[name] = sum(r[name] for r in requests)
    out["input"] = out["uncached"] + out["cache_write"] + out["cache_read"]
    return out


def _price_order(model: str, table: Mapping[str, Any]) -> list[str]:
    """The models of the price table, the run's own model first when the table has it."""
    known = price_tables.models(table)
    try:
        own = price_tables.resolve(model, table)
    except price_tables.PriceError:
        return known
    return [own, *(m for m in known if m != own)]


def _costs(group: _Group, keys: Iterable[Key], table: Mapping[str, Any]) -> dict[str, float]:
    keys = list(keys)
    return {model: math.fsum(price_tables.cost(group.requests.get(key, []), model, group.ttl[key], table)
                             for key in keys)
            for model in _price_order(group.model, table)}


def _counted(group: _Group, arm: str) -> list[Key]:
    """The units the pooled figures use: the arm's complete units at repeat 0, in seed order."""
    return [(arm, seed, 0) for seed in group.seeds
            if group.units.get((arm, seed, 0), {}).get("status") == "complete"]


def _concluded(group: _Group, arm: str, keys: Sequence[Key]) -> dict[str, Any] | None:
    rows = [(key, row) for key in keys for row in group.probes[key].values() if row["cls"] == "concluded"]
    if not rows:
        return None
    scripted = arm == "full"  # the full arm's decision replies are the script's, so they are right by construction
    known = [(key, row) for key, row in rows if row["id"] in group.units[key]["concluded_ok"]]
    right = rows if scripted else [(key, row) for key, row in known if group.units[key]["concluded_ok"][row["id"]]]
    return {"decisions": len(rows), "decisions_right": len(right), "scripted": scripted,
            "decisions_unknown": 0 if scripted else len(rows) - len(known),
            "all": _proportion([row for _, row in rows]), "decision_right": _proportion([row for _, row in right])}


def _arm(group: _Group, arm: str, bound: int, table: Mapping[str, Any]) -> dict[str, Any]:
    keys = _counted(group, arm)
    rows = [row for key in keys for row in group.probes[key].values()]
    requests = [r for key in keys for r in group.requests.get(key, [])]
    sizes = [_input(r) for r in requests]
    compactions = [group.units[key]["compactions"] for key in keys]
    cost = _costs(group, keys, table)
    block = _proportion(rows)
    block = {"units": len(keys), "seeds": [key[1] for key in keys], **block}
    block.update({
        "stale": sum(1 for r in rows if r["stale"]),
        "by_class": {cls: _proportion(of_class) for cls, of_class in _by_class(rows).items()},
        "tokens": _tokens(requests),
        "max_input": max(sizes, default=0),
        "over_bound": sum(1 for size in sizes if size > bound),
        "http_errors": sum(1 for r in requests if not 200 <= r["status"] < 300),
        "other_model_requests": sum(1 for r in requests if r["model"] not in (None, group.model)),
        "compactions": None if any(c is None for c in compactions) or not keys else sum(compactions),
        "cost": cost,
        "cost_per_unit": {model: usd / len(keys) for model, usd in cost.items()} if keys else {},
        "concluded": _concluded(group, arm, keys),
        "not_complete": [{"seed": key[1], "repeat": key[2], "status": unit["status"], "reason": unit["reason"]}
                         for key, unit in group.units.items() if key[0] == arm and unit["status"] != "complete"],
    })
    return block


def _noise(group: _Group, arm: str) -> dict[str, Any] | None:
    """Run-to-run noise from the first seed that has two or more complete repeats of this arm."""
    for seed in group.seeds:
        keys = sorted(key for key, unit in group.units.items()
                      if key[:2] == (arm, seed) and unit["status"] == "complete")
        if len(keys) < 2:
            continue
        ids = list(group.probes[keys[0]])
        runs = [[bool(group.probes[key][pid]["correct"]) for pid in ids] for key in keys]
        return {"seed": seed, "repeat_ids": [key[2] for key in keys], **stats.repeat_noise(runs)}
    return None


def _p(value: float) -> str:
    return "< 0.0001" if value < 0.0001 else f"= {value:.4f}"


def _points(value: float, signed: bool = False) -> str:
    return f"{100 * value:+.1f}" if signed else f"{100 * value:.1f}"


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def _verdict(a: str, b: str, pooled: dict[str, Any] | None, seed_level: dict[str, Any] | None,
             noise_a: dict[str, Any] | None, noise_b: dict[str, Any] | None) -> dict[str, Any]:
    """What a comparison shows, by fixed rules.

    A direction is named only when the pooled 95% interval excludes 0, the seed-level sign-flip p
    value is below 0.05, and the difference is larger than the run-to-run noise where noise was
    measured. Otherwise the result is "no difference shown", with the reasons. The noise figure is
    the larger of the two arms' accuracy ranges across repeats of one seed. The smallest p value
    the sign-flip test can give with n seeds is 2 / 2**n, so the text says when the number of
    seeds makes 0.05 unreachable.
    """
    title = f"{label(a)} against {_inline(b)}"
    if pooled is None or seed_level is None:
        return {"result": "not_compared", "text": f"{title}: not compared, because no seed has a complete unit of "
                                                  "both arms."}
    low, high = pooled["interval"]
    difference = pooled["difference"]
    seeds = seed_level["seeds"]
    min_p = 2 / 2 ** seeds
    excludes = low > 0 or high < 0
    below = seed_level["sign_flip_p"] < ALPHA
    ranges = [n["accuracy_range"] for n in (noise_a, noise_b) if n is not None]
    noise_range = max(ranges) if ranges else None
    exceeds = None if noise_range is None else abs(difference) > noise_range + 1e-12
    if excludes and below and exceeds is not False:
        result = "a_higher" if difference > 0 else "b_higher"
        winner, loser = (a, b) if difference > 0 else (b, a)
        lead = f"{label(winner)} answered more probes correctly than {_inline(loser)}."
    else:
        result = "no_difference_shown"
        reasons = []
        if not excludes:
            reasons.append("the pooled interval contains 0")
        if not below:
            reasons.append(f"the seed-level p value is {ALPHA} or more")
        if exceeds is False:
            reasons.append(f"the difference is not larger than the run-to-run noise ({_points(noise_range)} points)")
        lead = f"no difference shown, because {' and '.join(reasons)}."
    sentences = [
        f"{title}: {lead}",
        f"The pooled difference is {_points(difference, True)} points (95% interval {_points(low, True)} to "
        f"{_points(high, True)} points, exact McNemar p {_p(pooled['mcnemar_p'])}) over "
        f"{_plural(pooled['n'], 'probe')} of {_plural(seeds, 'seed')}, and the seed-level sign-flip p value is "
        f"{seed_level['sign_flip_p']:.4f}.",
    ]
    if min_p >= ALPHA:
        sentences.append(
            f"With {_plural(seeds, 'seed')} the seed-level test cannot reach {ALPHA}: its smallest possible p value "
            f"is 2 / 2^{seeds} = {min_p:.4f}, because the test only asks whether each seed's difference could have "
            "had the other sign. This run cannot show a difference at that level, however large the difference is.")
    if noise_range is None:
        sentences.append("Run-to-run noise was not measured for these arms (no seed was repeated), so the "
                         "difference cannot be compared with it.")
    else:
        parts = [f"{_points(n['accuracy_range'])} points for {_inline(arm)}"
                 for arm, n in ((a, noise_a), (b, noise_b)) if n is not None]
        sentences.append(f"Run-to-run noise (the range of accuracy across repeats of one seed) is "
                         f"{' and '.join(parts)}; the difference between the arms is "
                         f"{_points(abs(difference))} points.")
    return {"result": result, "interval_excludes_zero": excludes, "seed_p_below_alpha": below,
            "seed_test_min_p": min_p, "seed_test_can_reach_alpha": min_p < ALPHA, "noise_range": noise_range,
            "difference_exceeds_noise": exceeds, "text": " ".join(sentences)}


def _pair(group: _Group, a: str, b: str, noise: Mapping[str, Any]) -> dict[str, Any]:
    seeds: list[int] = []
    dropped_units: list[dict[str, Any]] = []
    per_seed: dict[str, int] = {}  # probes dropped, by seed
    for seed in group.seeds:
        missing = []
        for arm in (a, b):
            unit = group.units.get((arm, seed, 0))
            if unit is None:
                missing.append({"arm": arm, "seed": seed, "repeat": 0, "status": "absent",
                                "reason": "the manifest lists no such unit"})
            elif unit["status"] != "complete":
                missing.append({"arm": arm, "seed": seed, "repeat": 0, "status": unit["status"],
                                "reason": unit["reason"]})
        if not missing:
            seeds.append(seed)
            continue
        dropped_units += missing
        per_seed[str(seed)] = max((len(rows) for key, rows in group.probes.items() if key[1] == seed), default=0)
    out: dict[str, Any] = {"a": a, "b": b, "seeds": seeds, "pooled": None, "by_class": {}, "seed_level": None,
                           "per_seed": [],
                           "dropped": {"probes": sum(per_seed.values()), "per_seed": per_seed, "units": dropped_units}}
    right_a: list[bool] = []
    right_b: list[bool] = []
    classes: dict[str, tuple[list[bool], list[bool]]] = {}
    for seed in seeds:
        rows_a, rows_b = group.probes[(a, seed, 0)], group.probes[(b, seed, 0)]
        seed_a = [bool(row["correct"]) for row in rows_a.values()]
        seed_b = [bool(rows_b[pid]["correct"]) for pid in rows_a]
        of_seed = stats.paired(seed_a, seed_b)
        out["per_seed"].append({"seed": seed, "n": of_seed["n"], "accuracy_a": of_seed["accuracy_a"],
                                "accuracy_b": of_seed["accuracy_b"], "difference": of_seed["difference"]})
        right_a += seed_a
        right_b += seed_b
        for row, x, y in zip(rows_a.values(), seed_a, seed_b):
            cell = classes.setdefault(row["cls"], ([], []))
            cell[0].append(x)
            cell[1].append(y)
    if seeds:
        out["pooled"] = stats.paired(right_a, right_b)
        out["by_class"] = {cls: stats.paired(x, y) for cls, (x, y) in classes.items()}
        out["seed_level"] = stats.seed_level([s["difference"] for s in out["per_seed"]])
    out["verdict"] = _verdict(a, b, out["pooled"], out["seed_level"], noise.get(a), noise.get(b))
    return out


def _series(group: _Group) -> dict[str, Any]:
    out = {}
    for arm in group.arms:
        rows = [r for r in group.requests.get((arm, group.first_seed, 0), [])
                if r["main"] and r["history_tokens"] is not None]
        if not rows:
            continue
        bases = sorted({r["basis"] for r in rows})
        if len(bases) > 1:
            raise ReportError(f"the main requests of {_unit_words(group.model, (arm, group.first_seed, 0))} mix the "
                              f"bases {' and '.join(bases)}: one series has one basis")
        out[arm] = {"basis": bases[0], "label": label(arm), "points": [[r["history_tokens"], _input(r)] for r in rows]}
    return out


def _plain_compaction(group: _Group, bound: int) -> dict[str, Any] | None:
    units = [(key, unit) for key, unit in group.units.items() if key[0] == "plain"]
    if not units:
        return None
    values = [v for _, unit in units for v in unit["compaction_pre_tokens"]]
    trigger = group.plain_set["intended_trigger"] if group.plain_set else bound
    return {
        "set": group.plain_set, "bound": bound,
        "units": [{"seed": key[1], "repeat": key[2], "status": unit["status"], "compactions": unit["compactions"],
                   "pre_tokens": list(unit["compaction_pre_tokens"])} for key, unit in units],
        "compactions": sum(unit["compactions"] or 0 for _, unit in units),
        "pre_tokens": {"count": len(values), "min": min(values), "max": max(values),
                       "mean": sum(values) / len(values),
                       "below_trigger": sum(1 for v in values if v < trigger)} if values else None,
    }


def _model_block(group: _Group, bound: int, table: Mapping[str, Any]) -> dict[str, Any]:
    arms = {arm: _arm(group, arm, bound, table) for arm in group.arms}
    noise = {arm: _noise(group, arm) for arm in group.arms}
    pairs = {f"{a}_vs_{b}": _pair(group, a, b, noise) for a, b in PAIRS if a in arms and b in arms}
    every = [r for rows in group.requests.values() for r in rows]
    return {"arms": arms, "pairs": pairs, "noise": noise, "request_series": _series(group),
            "plain_compaction": _plain_compaction(group, bound),
            "all_requests": {"units": len(group.requests), "tokens": _tokens(every),
                             "cost": _costs(group, group.requests, table)}}


def _manifest(runs: Sequence[records.Run]) -> dict[str, Any]:
    """The manifest of the results: the directory's own, or one that describes the directories merged."""
    first = runs[0].manifest
    if len(runs) == 1:
        return first
    models = list(dict.fromkeys(run.manifest["model"] for run in runs))
    merged = dict(first)
    merged["model"] = models[0] if len(models) == 1 else None
    merged["models"] = models
    merged["seeds"] = list(dict.fromkeys(seed for run in runs for seed in run.manifest["seeds"]))
    merged["arms"] = list(dict.fromkeys(arm for run in runs for arm in run.manifest["arms"]))
    merged["repeats"] = max(run.manifest["repeats"] for run in runs)
    merged["started"] = min(run.manifest["started"] for run in runs)
    finished = [run.manifest["finished"] for run in runs]
    merged["finished"] = None if any(f is None for f in finished) else max(finished)
    merged["units"] = [{**unit, "model": run.manifest["model"]} for run in runs for unit in run.manifest["units"]]
    differs = {}
    for name in ("contexto_version", "claude_code_version", "upstream", "cache_ttl", "plain_compaction"):
        values = [run.manifest[name] for run in runs]
        if any(value != values[0] for value in values):
            differs[name] = list({json.dumps(v, sort_keys=True): v for v in values}.values())
    merged["merged"] = {"directories": len(runs), "differs": differs}
    return merged


def build(out_dirs: Iterable[Path | str] | Path | str, prices: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The results of one or more live output directories, as the dict ``results.json`` holds.

    With several directories (one per model, or one run split across shells) the units are pooled
    per model. One model gives the top-level ``arms``, ``pairs`` and ``noise``; several give
    ``by_model`` and leave those three null. Directories whose bound, protocol or turn size differ
    are refused (``ReportError``), and so is a unit that two directories both hold. ``prices`` is a
    price table (``contexto.bench.prices.load``) to use in place of the shipped one.
    """
    dirs = [Path(out_dirs)] if isinstance(out_dirs, (str, Path)) else [Path(d) for d in out_dirs]
    if not dirs:
        raise ReportError("no output directory was given")
    runs = [records.read_run(d) for d in dirs]
    _check_poolable(runs)
    table = price_tables.load() if prices is None else price_tables.check(dict(prices), "the price table given")
    groups = _group(runs)
    bound = runs[0].manifest["bound"]
    blocks = {model: _model_block(group, bound, table) for model, group in groups.items()}
    first = next(iter(groups.values()))
    one = len(groups) == 1
    priced = True
    for model in groups:
        try:
            price_tables.resolve(model, table)
        except price_tables.PriceError:
            priced = False
    basis = {s["basis"] for block in blocks.values() for s in block["request_series"].values()}
    notes = []
    if basis <= {"measured"}:
        notes.append(f"Request size is the input the upstream reported (uncached, cache write and cache read "
                     f"tokens added) for each main request of seed {first.first_seed}, repeat 0.")
    notes.append(f"History length is estimated from the script, not counted by a tokenizer: {BYTES_PER_TOKEN_LOG} "
                 f"bytes per token for log turns plus {REPLY_TOKENS} tokens per reply, and {BYTES_PER_TOKEN_TEXT} "
                 "bytes per token for probe turns.")
    results: dict[str, Any] = {
        "format": FORMAT, "kind": "live", "manifest": _manifest(runs),
        "request_series": blocks[first.model]["request_series"], "bound": bound, "notes": notes,
        "prices": {"cache_ttl": first.cache_ttl or DEFAULT_CACHE_TTL, "cache_ttl_assumed": first.cache_ttl is None,
                   "replaced": prices is not None, "run_model_priced": priced, "table": table},
        "arms": None, "pairs": None, "noise": None, "plain_compaction": None, "all_requests": None,
        "by_model": None, "charts": [],
    }
    if one:
        results.update(blocks[first.model])
    else:
        results["by_model"] = blocks
    return json.loads(json.dumps(results))  # tuples become lists, as they are when the file is read back


# The report in words


def _percent(value: float | None) -> str:
    return "none" if value is None else f"{100 * value:.1f}%"


def _range(interval: Sequence[float] | None) -> str:
    return "none" if interval is None else f"{100 * interval[0]:.1f}% to {100 * interval[1]:.1f}%"


def _usd(value: float) -> str:
    return f"${value:,.2f}" if value >= 100 else f"${value:,.4f}"


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return [*lines, ""]


def _share(cell: Mapping[str, Any] | None) -> str:
    if cell is None or not cell["probes"]:
        return "not run"
    return f"{cell['correct']:,} of {cell['probes']:,} ({_percent(cell['accuracy'])}, {_range(cell['interval'])})"


def _status_words(status: str) -> str:
    return {"not_run": "was not run", "stopped": "stopped before it finished",
            "absent": "is not in the manifest"}.get(status, f"is {status}")


def _what_was_run(results: Mapping[str, Any]) -> list[str]:
    m = results["manifest"]
    upstream = {"anthropic": "the Anthropic API", "custom": "a custom upstream (its address is not recorded)",
                "local": "a local address (127.0.0.1): a stub standing in for a model"}[m["upstream"]]
    models = m.get("models") or [m["model"]]
    units = m["units"]
    counts = {status: sum(1 for u in units if u["status"] == status) for status in records.STATUSES}
    lines = [
        f"- A live run of `contexto bench`: protocol {m['protocol']}, bound {m['bound']:,} tokens, content turns of "
        f"{m['turn_kbytes'] * 1_000:,} bytes.",
        f"- Model: {', '.join(str(x) for x in models)}. Upstream: {upstream}.",
        f"- Versions: Contexto {m['contexto_version']}, Claude Code {m['claude_code_version'] or 'not recorded'}.",
        f"- Seeds: {', '.join(str(s) for s in m['seeds'])} ({_plural(len(m['seeds']), 'seed')}). "
        + (f"The first seed was run {m['repeats']} times to measure run-to-run noise."
           if m["repeats"] > 1 else "No seed was repeated."),
        f"- Arms: {', '.join(_inline(a) for a in m['arms'])}.",
        f"- Started {m['started']}, finished {m['finished'] or 'not recorded (the run did not finish)'}.",
        f"- Units (one arm on one seed, once): {len(units)} recorded, {counts['complete']} complete, "
        f"{counts['stopped']} stopped, {counts['not_run']} not run.",
        "- The tables below use repeat 0 of each seed, so the repeated seed is counted once. The other repeats are "
        "used only for run-to-run noise.",
    ]
    merged = m.get("merged")
    if merged:
        lines.append(f"- These results merge {merged['directories']} output directories.")
        for name, values in merged["differs"].items():
            lines.append(f"- The directories differ in `{name}`: {', '.join(json.dumps(v) for v in values)}.")
    lines.append("")
    incomplete = [u for u in units if u["status"] != "complete"]
    if incomplete:
        lines += ["Units that are not complete:", ""]
        lines += _table(["Arm", "Seed", "Repeat", "Status", "Reason"],
                        [[label(u["arm"]), u["seed"], u["repeat"], u["status"].replace("_", " "),
                          u["reason"] or "none given"] for u in incomplete])
    return lines


def _accuracy(block: Mapping[str, Any], h: str) -> list[str]:
    arms = block["arms"]
    lines = [f"{h} Accuracy by arm", "",
             "Probes answered correctly, with the Wilson 95% interval. A session is one arm on one seed.", ""]
    lines += _table(["Arm", "Sessions", "Probes", "Correct", "Accuracy", "95% interval", "Stale answers"],
                    [[label(arm), a["units"], f"{a['probes']:,}", f"{a['correct']:,}", _percent(a["accuracy"]),
                      _range(a["interval"]), a["stale"]] for arm, a in arms.items()])
    classes = list(dict.fromkeys(cls for a in arms.values() for cls in a["by_class"]))
    if classes:
        lines += [f"{h}# Accuracy by probe class", "",
                  "Each cell is correct of asked, with the accuracy and its Wilson 95% interval.", ""]
        lines += _table(["Class", *(label(arm) for arm in arms)],
                        [[cls, *(_share(a["by_class"].get(cls)) for a in arms.values())] for cls in classes])
    return lines


def _differences(block: Mapping[str, Any], h: str) -> list[str]:
    pairs = block["pairs"]
    if not pairs:
        return []
    lines = [f"{h} Differences between arms", "",
             "Each pair is compared on the same probes, pooled across seeds. The difference is the first arm's "
             "accuracy minus the second's, in percentage points, with a 95% interval by Tango's score method and "
             "the exact two-sided McNemar p value.", ""]
    rows = []
    for pair in pairs.values():
        title = f"{label(pair['a'])} against {_inline(pair['b'])}"
        p = pair["pooled"]
        if p is None:
            rows.append([title, 0, "not compared", "", "", "", "", "", ""])
            continue
        rows.append([title, f"{p['n']:,}", p["both"], p["a_only"], p["b_only"], p["neither"],
                     f"{_points(p['difference'], True)} points",
                     f"{_points(p['interval'][0], True)} to {_points(p['interval'][1], True)} points",
                     f"p {_p(p['mcnemar_p'])}"])
    lines += _table(["Pair", "Probes", "Both right", "Only first right", "Only second right", "Neither right",
                     "Difference", "95% interval", "Exact McNemar test"], rows)
    dropped = []
    for pair in pairs.values():
        title = f"{label(pair['a'])} against {_inline(pair['b'])}"
        counted: dict[int, list[Mapping[str, Any]]] = {}
        for unit in pair["dropped"]["units"]:
            counted.setdefault(unit["seed"], []).append(unit)
        for seed, units in counted.items():
            why = " and ".join(f"the {_inline(u['arm'])} unit {_status_words(u['status'])}"
                               + (f" ({u['reason']})" if u["reason"] else "") for u in units)
            per_seed = pair["dropped"]["per_seed"][str(seed)]
            dropped.append(f"- {title}: {_plural(per_seed, 'probe')} of seed {seed} were dropped because {why}.")
    if dropped:
        lines += ["A pair is compared only on seeds where both arms' units are complete.", "", *dropped, ""]
    else:
        lines += ["No probe was dropped from a comparison: every pair had complete units of both arms on every "
                  "seed.", ""]
    classes = list(dict.fromkeys(cls for pair in pairs.values() for cls in pair["by_class"]))
    if classes:
        lines += [f"{h}# Differences by probe class", "",
                  "Each cell is the difference in percentage points, then the two discordant counts (only first "
                  "right, only second right) and the exact McNemar p value.", ""]
        body = []
        for cls in classes:
            row = [cls]
            for pair in pairs.values():
                c = pair["by_class"].get(cls)
                row.append("not compared" if c is None else
                           f"{_points(c['difference'], True)} points ({c['a_only']}, {c['b_only']}; "
                           f"p {_p(c['mcnemar_p'])}), n = {c['n']}")
            body.append(row)
        lines += _table(["Class", *(f"{label(p['a'])} against {_inline(p['b'])}" for p in pairs.values())], body)
    lines += [f"{h}# Differences with seeds as the unit", "",
              "Probes inside one session are not independent, so each seed is also treated as one observation: its "
              "difference in accuracy between the two arms. The p value is an exact sign-flip permutation test and "
              "the interval a percentile bootstrap over seeds.", ""]
    rows = []
    for pair in pairs.values():
        title = f"{label(pair['a'])} against {_inline(pair['b'])}"
        s = pair["seed_level"]
        if s is None:
            rows.append([title, 0, "not compared", "", "", ""])
            continue
        interval = s["bootstrap_interval"]
        rows.append([title, s["seeds"], ", ".join(_points(d, True) for d in s["differences"]),
                     f"{_points(s['mean'], True)} points", f"p {_p(s['sign_flip_p'])}",
                     "none with one seed" if interval is None else
                     f"{_points(interval[0], True)} to {_points(interval[1], True)} points"])
    lines += _table(["Pair", "Seeds", "Difference per seed (points)", "Mean difference", "Sign-flip test",
                     "Bootstrap 95% interval"], rows)
    return lines


def _noise_section(block: Mapping[str, Any], h: str) -> list[str]:
    noise, pairs = block["noise"], block["pairs"]
    lines = [f"{h} Run-to-run noise", ""]
    measured = {arm: n for arm, n in noise.items() if n is not None}
    if not measured:
        return [*lines, "Run-to-run noise was not measured: no seed was run more than once (`--repeats 1`). "
                        "Without it there is no way to tell a difference between arms from the variation of one "
                        "arm between two runs of the same session.", ""]
    lines += ["The same session run more than once in the same arm. The range is the highest accuracy minus the "
              "lowest; a flipped probe is one the repeats did not all agree on.", ""]
    lines += _table(["Arm", "Seed", "Repeats", "Accuracy of each repeat", "Range", "Probes that flipped",
                     "Pairwise agreement"],
                    [[label(arm), n["seed"], n["repeats"], ", ".join(_percent(a) for a in n["accuracies"]),
                      f"{_points(n['accuracy_range'])} points", f"{n['flips']} of {n['probes']}",
                      _percent(n["pairwise_agreement"])] for arm, n in measured.items()])
    rows = []
    for pair in pairs.values():
        if pair["pooled"] is None:
            continue
        verdict = pair["verdict"]
        a, b = noise.get(pair["a"]), noise.get(pair["b"])
        against = {True: "larger than the noise", False: "not larger than the noise",
                   None: "noise not measured"}[verdict["difference_exceeds_noise"]]
        rows.append([f"{label(pair['a'])} against {_inline(pair['b'])}",
                     f"{_points(pair['pooled']['difference'], True)} points",
                     "not measured" if a is None else f"{_points(a['accuracy_range'])} points",
                     "not measured" if b is None else f"{_points(b['accuracy_range'])} points", against])
    if rows:
        lines += ["The difference between arms next to the noise of each arm:", ""]
        lines += _table(["Pair", "Difference between arms", "Noise of the first arm", "Noise of the second arm",
                         "Difference against noise"], rows)
    return lines


def _concluded_section(block: Mapping[str, Any], h: str) -> list[str]:
    rows = []
    for arm, a in block["arms"].items():
        c = a["concluded"]
        if c is None:
            continue
        decisions = "scripted" if c["scripted"] else f"{c['decisions_right']} of {c['decisions']}"
        rows.append([label(arm), decisions, _share(c["all"]),
                     _share(c["decision_right"]) if c["decision_right"]["probes"] else "none to ask about"])
    if not rows:
        return []
    return [f"{h} Facts concluded by the assistant", "",
            "A `concluded` fact exists in the session only as the assistant's own `DECISION` reply. The first "
            "column is how many of those replies were right in that arm. The probe is scored against the "
            "generator's value, so a wrong decision makes a wrong answer; the last column leaves those out, which "
            "separates a failed recall from a wrong conclusion. The full context arm's replies are scripted, so "
            "they are right by construction.", "",
            *_table(["Arm", "Decision replies right", "Concluded probes right, all",
                     "Concluded probes right, where the decision was right"], rows)]


def _tokens_section(block: Mapping[str, Any], results: Mapping[str, Any], h: str) -> list[str]:
    lines = [f"{h} Tokens and requests", "",
             f"Every request that reached the upstream in the sessions counted above, side requests included. "
             f"Input is uncached, cache write and cache read added. The bound is {results['bound']:,} tokens.", ""]
    lines += _table(["Arm", "Sessions", "Requests", "Uncached input", "Cache write", "Cache read", "Output",
                     "Largest request input", "Requests over the bound", "Compactions", "Requests not answered 2xx"],
                    [[label(arm), a["units"], f"{a['tokens']['requests']:,}", f"{a['tokens']['uncached']:,}",
                      f"{a['tokens']['cache_write']:,}", f"{a['tokens']['cache_read']:,}",
                      f"{a['tokens']['output']:,}", f"{a['max_input']:,}", a["over_bound"],
                      "not applicable" if a["compactions"] is None else a["compactions"], a["http_errors"]]
                     for arm, a in block["arms"].items()])
    return lines


def _cost_section(block: Mapping[str, Any], results: Mapping[str, Any], h: str) -> list[str]:
    arms = block["arms"]
    prices = results["prices"]
    table = prices["table"]
    models = list(dict.fromkeys(m for a in arms.values() for m in a["cost"]))
    lines = [f"{h} Cost at published prices", "",
             "The measured token classes of each arm priced at every model in the price table, request by request. "
             "Only the first row is what this run was billed at; the others answer what the same tokens would "
             "cost at another model's prices. Each cell is USD per session, then the total over the arm's "
             "sessions.", ""]
    lines += _table(["Prices of", *(label(arm) for arm in arms)],
                    [[model, *((f"{_usd(a['cost_per_unit'][model])} ({_usd(a['cost'][model])} over "
                                f"{_plural(a['units'], 'session')})") if a["units"] else "not run"
                               for a in arms.values())] for model in models])
    sessions = {a["units"] for a in arms.values() if a["units"]}
    if len(sessions) > 1:
        lines += ["The arms completed different numbers of sessions, so compare the cost per session, not the "
                  "totals.", ""]
    source = table.get("source") or next(iter(table["models"].values())).get("source")
    accessed = table.get("accessed") or next(iter(table["models"].values())).get("accessed")
    lines.append(f"- Prices: {source}, read {accessed}, USD per million tokens"
                 + (", from a table given with `--prices`." if prices["replaced"] else "."))
    lines.append(f"- Cache writes are priced at the {'1-hour' if prices['cache_ttl'] == '1h' else '5-minute'} rate "
                 "for every arm, because a request record does not carry its cache lifetime."
                 + (" The manifest does not record a cache lifetime, so 5 minutes is assumed."
                    if prices["cache_ttl_assumed"] else ""))
    for model in models:
        entry = table["models"][model]
        if len(entry["rows"]) > 1 and entry.get("note"):
            lines.append(f"- {model}: {entry['note']}")
    other = sum(a["other_model_requests"] for a in arms.values())
    if other:
        lines.append(f"- {_plural(other, 'request')} went to a model other than the run's own (a CLI sends some "
                     "side requests to a smaller model). Every row prices them at that row's model, so the first "
                     "row is close to the bill, not equal to it.")
    if not prices["run_model_priced"]:
        own = results["manifest"].get("models") or [results["manifest"]["model"]]
        for model in own:
            if model not in table["models"]:
                lines.append(f"- {model} has no row in the price table, so no row above is what this run was "
                             "billed at. `--prices FILE` takes a table that includes it.")
    total = block["all_requests"]
    if total["cost"]:
        first = next(iter(total["cost"]))
        lines.append(f"- Every request recorded, repeats and unfinished units included: "
                     f"{total['tokens']['requests']:,} requests, {total['tokens']['input']:,} input and "
                     f"{total['tokens']['output']:,} output tokens, {_usd(total['cost'][first])} at the prices of "
                     f"{first}.")
    return [*lines, ""]


def _compaction_section(block: Mapping[str, Any], h: str) -> list[str]:
    c = block["plain_compaction"]
    if c is None:
        return []
    lines = [f"{h} Compaction in the plain Claude Code arm", ""]
    setting = c["set"]
    if setting is None:
        lines += ["The manifest does not record what was set for compaction.", ""]
    else:
        lines.append(f"Set: `{setting['variable']}` = {setting['value']:,}, intended to start compaction at "
                     f"{setting['intended_trigger']:,} tokens; the bound of the Contexto arm is {c['bound']:,} "
                     f"tokens. The run recorded the threshold as "
                     f"{'forced' if setting['forced'] else 'not forced'}. {setting['note']}".rstrip())
        lines.append("")
    pre = c["pre_tokens"]
    if pre is None:
        lines.append(f"Compactions seen: {c['compactions']}. No `pre_tokens` figure was reported, so where "
                     "compaction started cannot be checked.")
    else:
        trigger = setting["intended_trigger"] if setting else c["bound"]
        lines.append(f"Compactions seen: {c['compactions']} across {_plural(len(c['units']), 'session')}. The "
                     f"`pre_tokens` the CLI reported at each ranged from {pre['min']:,} to {pre['max']:,} tokens "
                     f"(mean {pre['mean']:,.0f}), which is {pre['min'] / trigger:.2f} to {pre['max'] / trigger:.2f} "
                     f"times the intended start of {trigger:,} tokens; {pre['below_trigger']} of {pre['count']} "
                     "were below it.")
    lines.append("")
    lines += _table(["Seed", "Repeat", "Status", "Compactions", "pre_tokens of each"],
                    [[u["seed"], u["repeat"], u["status"].replace("_", " "),
                      "not recorded" if u["compactions"] is None else u["compactions"],
                      ", ".join(f"{v:,}" for v in u["pre_tokens"]) or "none"] for u in c["units"]])
    return lines


def _model_sections(block: Mapping[str, Any], results: Mapping[str, Any], h: str) -> list[str]:
    return [*_accuracy(block, h), *_differences(block, h), *_noise_section(block, h), *_concluded_section(block, h),
            *_tokens_section(block, results, h), *_cost_section(block, results, h), *_compaction_section(block, h)]


def render(results: Mapping[str, Any]) -> str:
    """The plain-text report (``results.md``) of a results dict. It reads nothing else."""
    by_model = results.get("by_model")
    blocks = by_model if by_model else {None: results}
    lines = ["# Contexto benchmark results", "",
             "Written by `contexto bench` from the records in the output directory: `manifest.json`, `probes.jsonl` "
             "and `requests.jsonl`. `contexto bench --report DIR` recomputes it.", "",
             "## What was run", "", *_what_was_run(results)]
    for model, block in blocks.items():
        if model is None:
            lines += _model_sections(block, results, "##")
        else:
            lines += [f"## Results for {model}", "", *_model_sections(block, results, "###")]
    if results.get("charts"):
        lines += ["## Charts", ""]
        for chart in results["charts"]:
            lines += [f"![{chart['title']}]({chart['path']})", ""]
    lines += ["## What this run shows", "",
              "Each line is produced by a fixed rule. A direction is named only when the pooled 95% interval "
              f"excludes 0, the seed-level p value is below {ALPHA}, and the difference is larger than the "
              "run-to-run noise where noise was measured. Otherwise the line says no difference was shown.", ""]
    for model, block in blocks.items():
        for pair in block["pairs"].values():
            lines.append(f"- {'' if model is None else model + ': '}{pair['verdict']['text']}")
        if not block["pairs"]:
            lines.append(f"- {'' if model is None else model + ': '}Fewer than two arms were run, so there is no "
                         "comparison.")
    lines += ["", "### Limits", "", *(f"- {limit}" for limit in LIMITS), ""]
    return "\n".join(lines)


def write(out_dir: Path | str, sources: Iterable[Path | str] | None = None,
          prices: Mapping[str, Any] | None = None, charts: bool = True) -> dict[str, Any]:
    """Write ``results.json``, ``results.md`` and the charts into ``out_dir`` and return the results.

    The records are read from ``sources``, or from ``out_dir`` itself when ``sources`` is None,
    which is what the live run does when it finishes. Nothing is written if the records cannot be
    read. ``charts=False`` leaves the SVG files out.
    """
    out_dir = Path(out_dir)
    results = build([out_dir] if sources is None else list(sources), prices)
    chart_module = None
    if charts:
        from contexto.bench import charts as chart_module
        results["charts"] = [{"path": f"charts/{name}", "title": title}
                             for name, title in chart_module.planned(results)]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    (out_dir / "results.md").write_text(render(results), encoding="utf-8")
    if chart_module is not None and results["charts"]:
        chart_module.write(results, out_dir / "charts")
    return results
