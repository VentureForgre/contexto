"""Codex usage with Contexto against plain Codex, from one recorded run's own logs (packet A27). Zero
spend: it reads the session log of a finished run and sends nothing. Numbers only.

    uv run python spikes/m2/acceptance/codex_usage_vs_plain.py <run dir> [--csv <file>] [--o200k]

It follows lane B's B25 method (docs/research/M2-usage-savings-run3.md) for the Responses API:

- **Contexto, measured.** Each Responses request's logged usage: input (cached and uncached) and output.
- **Plain at the same request points, modeled.** The proxy logs what Codex sent
  (``original_request_bytes``, its full history) and what Contexto sent (``request_bytes``). Plain
  tokens = Contexto's measured input + the difference converted to tokens, so only the paged-out part
  is modeled. Unlike B25, the difference is split in two, because tool JSON tokenizes more sparsely
  than text: the tool-loop bytes of the paged probe turns convert at the JSON rate, the rest (paged
  turn text, net of Contexto's preamble) at the text rate. Which turns are paged at each request comes
  from the logged rotation events; a turn still in the window stays measured.
- **Calibration, on the run itself.** The text rate is each added turn's bytes over its measured tokens
  on the epoch-0 requests, which Contexto passes through unchanged (asserted). The JSON rate is the
  same ratio on the append-only requests inside a probe turn (one tool call and its output appended,
  no rotation; asserted by equal byte growth on both sides).
- **Plain-run shape, modeled.** Content turns as above; each probe turn as one request with no tool
  loop: the same-points plain at the turn's first request, less every earlier probe turn's tool loop.
- **Compaction.** Codex compacts at ``CODEX_COMPACT_AT`` (244,800 for a 272,000 window): checked
  against the plain peak.
- **Cache for plain** (Responses usage has cached and uncached input only): best case, each request
  reads the previous request's whole prompt; the run's own measured prefix-hit fraction on its
  append-only requests; and no cache.
- **Cost, modeled**, at the M0 cost model's gpt-6-astra prices.

``--o200k`` adds a cross-check of the text rate against the o200k_base tokenizer (needs ``tiktoken``)
on the script's own generated content turns, never on run content.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance import codex_budget, codex_driver  # noqa: E402

SESSION = "codex-step1"
# gpt-6-astra, USD per million tokens (docs/research/M0-cost-model.md, prices table, vendor-documented
# 2026-10-15): input, output, cache read, cache write (OpenAI writes cost 1.25x input; every uncached
# input token is counted as written, an assumption). Above 272K input: input x2, output x1.5.
PRICES = {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5}
LONG_CONTEXT = 272_000


def requests(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The run's Responses requests in order: user turn (1-based), held events, both byte sizes,
    measured usage, whether a rotation happened on it, and the user turns paged when it was sent.

    Paging comes from the logged rotation events, carried forward to the requests that do not rotate.
    The engine counts Codex's opening context as its turn 0, so ``turns_paged`` = p pages user turns 1 to
    p - 1. A rotation need not page every completed turn: in a probe turn the previous turn stays in
    the window with its tool loop [measured: A22, retained_messages above 1 from probe turn 1 on]."""
    out, turn, paged = [], 1, 0
    for e in codex_driver.responses_requests(events):
        if "status" not in e:
            continue
        inp, cached, output = codex_driver.usage_tokens(e.get("usage"))
        held = (e.get("stream_hold") or {}).get("held_events")
        rotations = [t for t in e.get("transform_events") or [] if t.get("event") == "rotation"]
        if rotations:
            paged = max(0, rotations[-1]["turns_paged"] - 1)
        out.append({"n": len(out) + 1, "turn": turn, "held": held, "epoch": e.get("epoch"),
                    "orig": e["original_request_bytes"], "sent": e["request_bytes"], "input": inp,
                    "cached": cached, "output": output, "rotated": bool(rotations), "paged_turns": paged})
        if held == 0:
            turn += 1
    return out


def text_rates(reqs: list[dict[str, Any]]) -> list[float]:
    """Bytes per token of each turn added between consecutive epoch-0 (pass-through) requests."""
    e0 = [r for r in reqs if r["epoch"] == 0]
    if any(r["orig"] != r["sent"] for r in e0):
        raise ValueError("an epoch-0 request was rewritten; the text rate needs pass-through requests")
    return [(b["sent"] - a["sent"]) / (b["input"] - a["input"]) for a, b in zip(e0, e0[1:])]


def json_pairs(reqs: list[dict[str, Any]]) -> list[tuple[int, int, int]]:
    """(request number, bytes, tokens) of each tool call and output appended inside a probe turn (no
    rotation, the same growth on both sides)."""
    pairs = []
    for a, b in zip(reqs, reqs[1:]):
        if a["turn"] != b["turn"] or b["rotated"]:
            continue
        grown = b["sent"] - a["sent"]
        if grown != b["orig"] - a["orig"]:
            raise ValueError(f"request {b['n']} is not append-only")
        pairs.append((b["n"], grown, b["input"] - a["input"]))
    return pairs


def quartiles(values: list[float]) -> tuple[float, float, float]:
    """First quartile, median, third quartile (linear interpolation)."""
    v = sorted(values)

    def at(q: float) -> float:
        i = q * (len(v) - 1)
        lo = int(i)
        return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (i - lo)

    return at(0.25), at(0.5), at(0.75)


def tool_loop_bytes(reqs: list[dict[str, Any]]) -> dict[int, int]:
    """Each turn's tool-loop bytes: what Codex appended between the turn's first and last request."""
    turns: dict[int, list[dict[str, Any]]] = {}
    for r in reqs:
        turns.setdefault(r["turn"], []).append(r)
    return {t: rs[-1]["orig"] - rs[0]["orig"] for t, rs in turns.items()}


def plain_same_points(reqs: list[dict[str, Any]], r_text: float, r_json: float) -> list[float]:
    """Plain Codex's input at each request: measured input + paged text at r_text + paged tool JSON at
    r_json. The paged tool loops are those of the user turns the rotation state had paged; a retained
    turn's loop is already in the measured input."""
    loops = tool_loop_bytes(reqs)
    out = []
    for r in reqs:
        paged_json = sum(b for t, b in loops.items() if t <= r["paged_turns"])
        out.append(r["input"] + (r["orig"] - r["sent"] - paged_json) / r_text + paged_json / r_json)
    return out


def plain_run_shape(reqs: list[dict[str, Any]], same: list[float], r_json: float,
                    n_content: int) -> list[dict[str, Any]]:
    """One plain request per turn: content turns at the same points; each probe turn's first request
    less the earlier probe turns' tool loops; output as Contexto measured on the turn's answer."""
    loops = tool_loop_bytes(reqs)
    rows = []
    for t in sorted({r["turn"] for r in reqs}):
        first = next(i for i, r in enumerate(reqs) if r["turn"] == t)
        answer = max(i for i, r in enumerate(reqs) if r["turn"] == t)
        earlier = sum(b for u, b in loops.items() if n_content < u < t)
        rows.append({"turn": t, "input": same[first] - earlier / r_json, "output": reqs[answer]["output"]})
    return rows


def best_cache(inputs: list[float]) -> list[tuple[float, float]]:
    """(cached, uncached) when each request reads the previous request's whole prompt from cache."""
    out, prev = [], 0.0
    for x in inputs:
        cached = min(prev, x)
        out.append((cached, x - cached))
        prev = x
    return out


def measured_hit_fraction(reqs: list[dict[str, Any]]) -> float:
    """On the run's append-only requests, cached tokens over the previous request's input: how much of a
    reusable prefix the cache actually served (measured)."""
    pairs = [(a, b) for a, b in zip(reqs, reqs[1:]) if a["turn"] == b["turn"] and not b["rotated"]]
    return sum(b["cached"] for _, b in pairs) / sum(a["input"] for a, _ in pairs)


def usd(cached: float, uncached: float, output: float, prompt: float, write: bool = True) -> float:
    """One request's modeled cost (prices above; the long-context step per request). ``write=False``
    prices uncached input at the input rate instead of the cache-write rate."""
    step_in, step_out = (2.0, 1.5) if prompt > LONG_CONTEXT else (1.0, 1.0)
    uncached_rate = PRICES["cache_write"] if write else PRICES["input"]
    return (cached * PRICES["cache_read"] * step_in + uncached * uncached_rate * step_in
            + output * PRICES["output"] * step_out) / 1e6


def cost(rows: list[tuple[float, float, float]], write: bool = True) -> float:
    return sum(usd(c, u, o, c + u, write) for c, u, o in rows)


def analyze(events: list[dict[str, Any]], n_content: int,
            text_band: tuple[float, float] | None = None) -> dict[str, Any]:
    """Every figure above. ``text_band`` (low and high bytes per token) bounds the text rate when the run
    has too few pass-through requests to give its own spread; the JSON band is the pairs' quartiles."""
    reqs = requests(events)
    t_rates, pairs = text_rates(reqs), json_pairs(reqs)
    j_rates = [b / t for _, b, t in pairs]
    r_text = sum(t_rates) / len(t_rates)
    r_json = sum(b for _, b, _ in pairs) / sum(t for _, _, t in pairs)  # pooled fit: all bytes over all tokens
    q1, median, q3 = quartiles(j_rates)
    t_lo, t_hi = text_band or (min(t_rates), max(t_rates))
    same = plain_same_points(reqs, r_text, r_json)
    shape = plain_run_shape(reqs, same, r_json, n_content)
    hit = measured_hit_fraction(reqs)

    def scenario(inputs: list[float], outputs: list[int]) -> dict[str, Any]:
        best = best_cache(inputs)
        part = [(c * hit, c * (1 - hit) + u) for c, u in best]
        return {"input": sum(inputs), "output": sum(outputs), "peak": max(inputs), "requests": len(inputs),
                "compaction_fires": max(inputs) >= codex_budget.CODEX_COMPACT_AT,
                "cache_best": {"cached": sum(c for c, _ in best), "uncached": sum(u for _, u in best)},
                "usd_best_cache": cost([(c, u, o) for (c, u), o in zip(best, outputs)]),
                "usd_best_cache_input_rate": cost([(c, u, o) for (c, u), o in zip(best, outputs)], write=False),
                "usd_measured_hit": cost([(c, u, o) for (c, u), o in zip(part, outputs)]),
                "usd_no_cache": cost([(0.0, x, o) for x, o in zip(inputs, outputs)])}

    outputs = [r["output"] for r in reqs]
    contexto = {"input": sum(r["input"] for r in reqs), "cached": sum(r["cached"] for r in reqs),
                "uncached": sum(r["input"] - r["cached"] for r in reqs), "output": sum(outputs),
                "peak": max(r["input"] for r in reqs), "requests": len(reqs),
                "usd": cost([(r["cached"], r["input"] - r["cached"], r["output"]) for r in reqs]),
                "usd_input_rate": cost([(r["cached"], r["input"] - r["cached"], r["output"]) for r in reqs],
                                       write=False)}
    bands = {}
    for name, (rt, rj) in {"low": (t_hi, q3), "high": (t_lo, q1)}.items():  # more bytes per token, fewer tokens
        s2 = plain_same_points(reqs, rt, rj)
        bands[name] = {"same_points": sum(s2),
                       "run_shape": sum(x["input"] for x in plain_run_shape(reqs, s2, rj, n_content))}
    residuals = [(n, b / r_json - t) for n, b, t in pairs]  # modeled minus measured tokens, per pair
    shape_best = best_cache([x["input"] for x in shape])
    same_best = best_cache(same)
    per_turn = []
    for t, (c, u), row in zip(sorted({r["turn"] for r in reqs}), shape_best, shape):
        idx = [i for i, r in enumerate(reqs) if r["turn"] == t]
        per_turn.append({
            "turn": t, "kind": "content" if t <= n_content else "probe", "requests": len(idx),
            "contexto_input": sum(reqs[i]["input"] for i in idx),
            "contexto_uncached": sum(reqs[i]["input"] - reqs[i]["cached"] for i in idx),
            "contexto_usd": cost([(reqs[i]["cached"], reqs[i]["input"] - reqs[i]["cached"], reqs[i]["output"])
                                  for i in idx]),
            "plain_same_points": round(sum(same[i] for i in idx)),
            "plain_same_points_usd": cost([(*same_best[i], reqs[i]["output"]) for i in idx]),
            "plain_run_shape": round(row["input"]), "plain_run_shape_uncached": round(u),
            "plain_run_shape_usd": usd(c, u, row["output"], c + u)})
    return {"per_turn": per_turn, "calibration": {"text_rates": t_rates, "r_text": r_text, "text_band": [t_lo, t_hi],
                            "json_pairs": len(pairs), "r_json": r_json, "json_quartiles": [q1, median, q3],
                            "json_rate_min": min(j_rates), "json_rate_max": max(j_rates),
                            "json_outlier": max(pairs, key=lambda x: x[1] / x[2])[0],
                            "json_residual_tokens": residuals},
            "measured_hit_fraction": hit, "contexto": contexto,
            "plain_same_points": scenario(same, outputs),
            "plain_run_shape": scenario([x["input"] for x in shape], [x["output"] for x in shape]),
            "rate_band": bands, "compact_at": codex_budget.CODEX_COMPACT_AT,
            "per_request": [{**r, "plain": round(p)} for r, p in zip(reqs, same)],
            "run_shape_rows": [{**x, "input": round(x["input"])} for x in shape]}


def o200k_check(n_content: int) -> dict[str, Any]:
    """The o200k_base tokenizer on the script's own generated turns (plan content, not run content):
    each content turn's JSON-escaped bytes per token, and every turn's token count."""
    import tiktoken  # optional: only for this cross-check

    enc = tiktoken.get_encoding("o200k_base")
    s = codex_budget.history_turns(64_000, 20_000)
    tokens = [len(enc.encode(t)) for t in s.turns]
    rows = [(len(json.dumps(t).encode()), n) for t, n in zip(s.turns[:n_content], tokens)]
    return {"turn_tokens": tokens, "rates": [b / t for b, t in rows],
            "mean_rate": sum(b for b, _ in rows) / sum(t for _, t in rows)}


def o200k_validation(reqs: list[dict[str, Any]], same: list[float], turn_tokens: list[int],
                     n_content: int) -> dict[str, Any]:
    """Plain input at the first probe request, counted another way: request 1's measured input, plus each
    later turn's o200k tokens, plus the per-turn overhead the pass-through pair measured beyond o200k
    (the reply and the two items' wrappers)."""
    overhead = (reqs[1]["input"] - reqs[0]["input"]) - turn_tokens[1]
    first_probe = next(i for i, r in enumerate(reqs) if r["turn"] == n_content + 1)
    counted = reqs[0]["input"] + sum(turn_tokens[1:n_content + 1]) + overhead * n_content
    return {"request": reqs[first_probe]["n"], "overhead_per_turn": overhead, "o200k_count": counted,
            "modeled": same[first_probe], "modeled_vs_count": same[first_probe] / counted - 1}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--csv", type=Path)
    ap.add_argument("--o200k", action="store_true")
    a = ap.parse_args(argv)
    try:
        events = codex_driver.read_log(a.run_dir / "store" / "sessions" / SESSION / "log.jsonl")
        n_content = codex_budget.history_turns(64_000, 20_000).n_content
        out = analyze(events, n_content)
    except (OSError, ValueError, KeyError, ZeroDivisionError) as exc:
        print(f"codex_usage_vs_plain: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if a.o200k:
        check = o200k_check(n_content)
        out = {**analyze(events, n_content, (min(check["rates"]), max(check["rates"]))), "o200k": check}
        same = [r["plain"] for r in out["per_request"]]
        out["o200k"]["validation"] = o200k_validation(requests(events), same, check["turn_tokens"], n_content)
    if a.csv:
        cols = ["n", "turn", "held", "paged_turns", "orig", "sent", "input", "cached", "output", "plain"]
        a.csv.write_text(",".join(cols) + "\n" + "".join(
            ",".join(str(r[c]) for c in cols) + "\n" for r in out["per_request"]))
    print(json.dumps({k: v for k, v in out.items() if k != "per_request"}, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
