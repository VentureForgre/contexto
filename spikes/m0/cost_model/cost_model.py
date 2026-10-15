"""M0 cost model: long-session token and dollar cost under four context strategies.

Strategies, all driven by the same seeded request-level trace:

  plain       (i)   resend the full history every request, prompt caching on
  compaction  (ii)  plain, plus the CLI's built-in compaction at its threshold
  rollover    (H4)  plain, plus a supervisor that starts a fresh session seeded
                    with a handoff when the context fills
  cvm         (iii) context virtual memory: stable head, append-only verbatim
                    window, older turns paged to stubs, head rebuilt at epochs

H5 (provider-native tool-result clearing and server-side compaction) is costed as
a variant of (i) and (ii).

Stdlib only. Every number in docs/research/M0-cost-model.md comes from the
generated blocks this script writes (see ``--write-doc`` and ``--check-doc``).
No model calls are made.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRICES = HERE / "prices.json"
PROFILE = HERE / "profiles" / "synthetic_realistic.json"
REPO = HERE.parents[2]
DOC = REPO / "docs" / "research" / "M0-cost-model.md"
SPIKE_OUT = REPO / ".contexto" / "spikes" / "l2a"  # gitignored

MTOK = 1_000_000
LANE1_FIELDS = (
    "turn",
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "request_bytes",
    "message_count",
)


# Prices


@dataclass(frozen=True)
class CacheTier:
    name: str
    ttl_s: float
    write: float  # USD per MTok for tokens written to the cache at this tier


@dataclass(frozen=True)
class LongContext:
    threshold: int  # prompt tokens above which the multipliers apply to the whole request
    input_mult: float
    output_mult: float


@dataclass(frozen=True)
class Model:
    key: str
    vendor: str
    display: str
    tier: str  # "frontier" or "small"
    context_window: int
    input: float  # USD per MTok, uncached input
    output: float
    cache_read: float
    cache_mode: str  # "explicit": writes billed at tier price; "automatic": misses billed as input
    cache_tiers: tuple[CacheTier, ...]
    default_tier: str
    long_context: LongContext | None = None

    def cache_tier(self, name: str | None = None) -> CacheTier:
        name = name or self.default_tier
        for t in self.cache_tiers:
            if t.name == name:
                return t
        raise KeyError(f"{self.key} has no cache tier {name!r}")


def _value(x):
    return x["value"] if isinstance(x, dict) else x


def load_prices(path: Path = PRICES) -> dict:
    """Return the raw price file plus parsed models under key ``parsed``."""
    raw = json.loads(Path(path).read_text())
    models = {}
    for key, m in raw["models"].items():
        p = m["usd_per_mtok"]
        tiers = tuple(
            CacheTier(t["name"], float(_value(t["ttl_s"])), float(_value(t["write"])))
            for t in m["cache_tiers"]
        )
        lc = m.get("long_context")
        models[key] = Model(
            key=key,
            vendor=m["vendor"],
            display=m["display"],
            tier=m["tier"],
            context_window=int(_value(m["context_window"])),
            input=float(_value(p["input"])),
            output=float(_value(p["output"])),
            cache_read=float(_value(p["cache_read"])),
            cache_mode=m["cache_mode"],
            cache_tiers=tiers,
            default_tier=m["default_tier"],
            long_context=(
                LongContext(int(_value(lc["threshold"])), float(_value(lc["input_mult"])),
                            float(_value(lc["output_mult"])))
                if lc
                else None
            ),
        )
    raw["parsed"] = models
    return raw


# Session profile and trace


def sample(rng: random.Random, spec) -> float:
    """Draw one value from a distribution spec in a profile file."""
    if isinstance(spec, (int, float)):
        return float(spec)
    dist = spec["dist"]
    if dist == "fixed":
        x = float(spec["value"])
    elif dist == "lognormal":
        x = spec["median"] * math.exp(spec["sigma"] * rng.gauss(0.0, 1.0))
    elif dist == "poisson":
        limit, k, p = math.exp(-spec["mean"]), 0, 1.0
        while True:
            p *= rng.random()
            if p <= limit:
                break
            k += 1
        x = float(k)
    elif dist == "mixture":
        r, acc = rng.random(), 0.0
        comps = spec["components"]
        total = sum(c["weight"] for c in comps)
        for c in comps:
            acc += c["weight"] / total
            if r <= acc:
                return sample(rng, c)
        return sample(rng, comps[-1])
    else:
        raise ValueError(f"unknown dist {dist!r}")
    return min(max(x, spec.get("min", -math.inf)), spec.get("max", math.inf))


@dataclass(frozen=True)
class Req:
    added: int  # tokens appended to the prompt just before this request (user message or tool result)
    output: int  # billed output tokens, thinking included
    kept: int  # output tokens that stay in the conversation (thinking excluded)


@dataclass(frozen=True)
class Turn:
    gap_s: float  # think time before the turn's first request
    reqs: tuple[Req, ...]
    recall: bool = False  # cvm only: the model fetches one paged item this turn
    pin_change: bool = False  # pinned state changed during this turn


@dataclass(frozen=True)
class Trace:
    name: str
    head_tokens: int  # system prompt plus tool definitions
    intra_gap_s: float  # seconds between requests inside a turn
    turns: tuple[Turn, ...]
    source: str = "synthetic"


def load_profile(path: Path = PROFILE) -> dict:
    return json.loads(Path(path).read_text())


def generate_trace(profile: dict, turns: int | None = None, seed: int | None = None) -> Trace:
    rng = random.Random(profile["seed"] if seed is None else seed)
    # Independent stream so cvm-only events never shift the shared workload.
    side = random.Random((profile["seed"] if seed is None else seed) + 1)
    n = profile["turns"] if turns is None else turns
    think = profile["thinking_tokens_per_request"]
    out = []
    for t in range(n):
        gap = 0.0 if t == 0 else sample(rng, profile["think_time_s"])
        k = int(sample(rng, profile["tool_calls_per_turn"]))
        user = round(sample(rng, profile["user_message_tokens"]))
        reqs = []
        added = user
        for i in range(k + 1):
            final = i == k
            visible = round(sample(rng, profile["final_output_tokens" if final else "tool_call_output_tokens"]))
            th = round(sample(rng, think))
            reqs.append(Req(added=added, output=visible + th, kept=visible))
            if not final:
                added = round(sample(rng, profile["tool_output_tokens"]))
        out.append(
            Turn(
                gap_s=gap,
                reqs=tuple(reqs),
                recall=side.random() < profile["cvm"]["recall_rate"],
                pin_change=side.random() < profile["cvm"]["pin_change_rate"],
            )
        )
    return Trace(profile["name"], int(profile["system_and_tools_tokens"]), float(profile["intra_turn_gap_s"]),
                 tuple(out))


# Prompt and cache engine


class Prompt:
    """Prompt as a list of block sizes, with enough history to compute cache hits."""

    def __init__(self) -> None:
        self.tok: list[int] = []
        self.cum: list[int] = [0]
        self.dirty = 0  # lowest block index changed since last send, other than appends
        self.sent_blocks = 0  # blocks in the prompt as last sent
        self.new_since_send = 0  # genuinely new tokens appended since last send

    @property
    def total(self) -> int:
        return self.cum[-1]

    def append(self, n: int, new: bool = True) -> None:
        self.tok.append(n)
        self.cum.append(self.cum[-1] + n)
        if new:
            self.new_since_send += n

    def truncate(self, i: int) -> None:
        del self.tok[i:]
        del self.cum[i + 1 :]
        self.dirty = min(self.dirty, i)

    def pop(self) -> int:
        n = self.tok[-1]
        self.truncate(len(self.tok) - 1)
        return n

    def resize(self, i: int, n: int) -> None:
        """Change block ``i`` in place, for example a tool result replaced by a placeholder."""
        delta = n - self.tok[i]
        self.tok[i] = n
        for j in range(i + 1, len(self.cum)):
            self.cum[j] += delta
        self.dirty = min(self.dirty, i)

    def reusable(self) -> int:
        """Tokens of the leading prefix identical to the prompt as last sent."""
        return self.cum[min(self.sent_blocks, self.dirty, len(self.tok))]

    def mark_sent(self) -> None:
        self.sent_blocks = len(self.tok)
        self.dirty = len(self.tok)
        self.new_since_send = 0


@dataclass
class Buckets:
    uncached: int = 0
    write: int = 0
    read: int = 0
    output: int = 0
    usd: float = 0.0
    requests: int = 0

    def add(self, o: "Buckets") -> None:
        self.uncached += o.uncached
        self.write += o.write
        self.read += o.read
        self.output += o.output
        self.usd += o.usd
        self.requests += o.requests

    @property
    def input_total(self) -> int:
        return self.uncached + self.write + self.read


def price(model: Model, tier: CacheTier, prompt: int, hit: int, output: int) -> Buckets:
    """Price one request whose prompt has ``hit`` tokens served from a warm cache.

    Explicit mode (Anthropic, and OpenAI GPT-5.6 and later): every miss token is
    written to the cache at the tier's write price. Automatic mode (older OpenAI
    models): misses are billed as ordinary input and cached for free.
    """
    miss = prompt - hit
    b = Buckets(read=hit, output=output, requests=1)
    if model.cache_mode == "explicit":
        b.write = miss
        in_usd = miss * tier.write + hit * model.cache_read
    else:
        b.uncached = miss
        in_usd = miss * model.input + hit * model.cache_read
    out_usd = output * model.output
    lc = model.long_context
    if lc and prompt > lc.threshold:
        in_usd *= lc.input_mult
        out_usd *= lc.output_mult
    b.usd = (in_usd + out_usd) / MTOK
    return b


RESET_CAUSES = ("compaction", "rollover")


@dataclass
class Breaks:
    count: Counter = field(default_factory=Counter)
    tokens: Counter = field(default_factory=Counter)
    usd: Counter = field(default_factory=Counter)


class Engine:
    def __init__(self, model: Model, tier: CacheTier) -> None:
        self.model, self.tier = model, tier
        self.prompt = Prompt()
        self.now = 0.0
        self.last_send: float | None = None
        self.breaks = Breaks()
        self.max_prompt = 0
        self.pending_cause: str | None = None  # why the next send's prefix changed

    def warm(self) -> bool:
        return self.last_send is not None and self.now - self.last_send <= self.tier.ttl_s

    def send(self, output: int, cause: str | None = None, reuse_cache: bool = True) -> Buckets:
        p = self.prompt
        cause = cause or self.pending_cause
        self.pending_cause = None
        warm = self.warm() and reuse_cache
        hit = p.reusable() if warm else 0
        b = price(self.model, self.tier, p.total, hit, output)
        # Tokens re-sent at miss price although they are not new since the last send.
        rewritten = max(0, p.total - hit - p.new_since_send) if self.last_send is not None else 0
        if rewritten:
            if not self.warm() and cause not in RESET_CAUSES:
                why = "ttl"
            else:
                why = cause or "other"
            premium = self.tier.write if self.model.cache_mode == "explicit" else self.model.input
            self.breaks.count[why] += 1
            self.breaks.tokens[why] += rewritten
            self.breaks.usd[why] += rewritten * (premium - self.model.cache_read) / MTOK
        self.max_prompt = max(self.max_prompt, p.total)
        p.mark_sent()
        self.last_send = self.now
        return b

    def idle(self, gap_s: float, keepalive: "Keepalive | None") -> Buckets:
        """Advance through think time, sending keepalive pings if configured."""
        spent = Buckets()
        if keepalive and self.last_send is not None and self.tier.ttl_s < gap_s <= keepalive.max_idle_s:
            step = self.tier.ttl_s - keepalive.margin_s
            pings = int(gap_s // step)
            for _ in range(pings):
                self.now += step
                self.prompt.append(keepalive.ping_tokens)
                spent.add(self.send(keepalive.ping_output))
                self.prompt.pop()
                self.breaks.count["keepalive_ping"] += 1
            self.now += gap_s - pings * step
        else:
            self.now += gap_s
        return spent


# Strategies


@dataclass(frozen=True)
class Keepalive:
    """Proxy-sent requests that read the warm prefix so it survives an idle gap."""

    max_idle_s: float = 3600.0
    margin_s: float = 30.0
    ping_tokens: int = 10
    ping_output: int = 1


@dataclass(frozen=True)
class Reset:
    """Built-in compaction (ii) or supervisor rollover (H4): one extra request, then a short history.

    When the prompt would reach ``threshold``, the session sends the current prompt
    plus an instruction, gets ``output_tokens`` back (summary or handoff), and the next
    request starts from the head plus ``seed_tokens``.
    """

    kind: str  # "compaction" or "rollover"
    threshold: int
    instruction_tokens: int = 500
    output_tokens: int = 8000
    seed_tokens: int = 23_000
    reuse_cache: bool = True  # the reset request reads the warm conversation prefix


@dataclass(frozen=True)
class ClearPolicy:
    """H5 server-side tool-result clearing (Anthropic context editing)."""

    trigger: int = 100_000
    keep: int = 3
    clear_at_least: int = 0
    placeholder_tokens: int = 15


@dataclass(frozen=True)
class CvmPolicy:
    window_tokens: int = 32_000  # verbatim recent turns kept after a rebase
    epoch_turns: int = 8  # rebase at least this often
    opportunistic: bool = True  # also rebase when the cache has already expired
    hard_cap_tokens: int | None = None  # force a rebase above this prompt size
    respect_price_tier: bool = True  # rebase before crossing a long-context price threshold
    pinned_base: int = 3000
    pinned_per_turn: float = 15.0
    pinned_max: int = 12_000
    stub_tokens_per_turn: int = 60
    pin_position: str = "head"  # "head": rebuilt at epochs; "tail": refreshed every request
    pin_updates: str = "epoch"  # "epoch": batched; "immediate": rebuild head on change
    recall_tokens: int = 1500
    recall_call_tokens: int = 120
    page_tool_output_over: int | None = None  # page large tool results at once
    tool_output_excerpt_tokens: int = 400


@dataclass
class TurnRow:
    turn: int
    prompt_tokens: int  # largest prompt sent during the turn
    b: Buckets
    cum_usd: float


@dataclass
class Result:
    strategy: str
    model: Model
    tier: CacheTier
    total: Buckets
    turns: list[TurnRow]
    breaks: Breaks
    resets: int = 0
    rebases: int = 0
    max_prompt: int = 0
    over_window_turn: int | None = None  # first turn whose prompt exceeds the model window
    events: Counter = field(default_factory=Counter)


def _finish_turn(res: Result, t: int, b: Buckets, peak: int) -> None:
    res.total.add(b)
    res.turns.append(TurnRow(t, peak, b, res.total.usd))
    if res.over_window_turn is None and peak > res.model.context_window:
        res.over_window_turn = t


def run_plain(trace: Trace, model: Model, tier: CacheTier | None = None, reset: Reset | None = None,
              clear: ClearPolicy | None = None, keepalive: Keepalive | None = None) -> Result:
    """Strategy (i), or (ii) and H4 with ``reset``, or H5 with ``clear``."""
    tier = tier or model.cache_tier()
    eng = Engine(model, tier)
    name = reset.kind if reset else ("clear" if clear else "plain")
    res = Result(name, model, tier, Buckets(), [], eng.breaks)
    p = eng.prompt
    p.append(trace.head_tokens)
    tools: list[int] = []  # block indices of tool results not yet cleared, oldest first
    for t, turn in enumerate(trace.turns, 1):
        b = eng.idle(turn.gap_s, keepalive)
        peak = 0
        for j, req in enumerate(turn.reqs):
            if j:
                eng.now += trace.intra_gap_s
            if reset and p.total + req.added >= reset.threshold:
                p.append(reset.instruction_tokens)
                peak = max(peak, p.total)
                b.add(eng.send(reset.output_tokens, reset.kind, reset.reuse_cache))
                p.truncate(1)
                p.append(reset.seed_tokens, new=False)
                eng.pending_cause = reset.kind
                tools = []
                res.resets += 1
            p.append(req.added)
            if j:
                tools.append(len(p.tok) - 1)
            if clear and p.total > clear.trigger and len(tools) > clear.keep:
                victims = tools[: -clear.keep] if clear.keep else tools
                freed = sum(p.tok[i] - clear.placeholder_tokens for i in victims)
                if freed >= clear.clear_at_least:
                    for i in victims:
                        p.resize(i, min(p.tok[i], clear.placeholder_tokens))
                    tools = tools[len(victims):]
                    eng.pending_cause = "clear"
                    res.resets += 1
            peak = max(peak, p.total)
            b.add(eng.send(req.output))
            p.append(req.kept)
        _finish_turn(res, t, b, peak)
    res.max_prompt = eng.max_prompt
    return res


def run_cvm(trace: Trace, model: Model, tier: CacheTier | None = None, policy: CvmPolicy = CvmPolicy(),
            keepalive: Keepalive | None = None) -> Result:
    """Strategy (iii): [head][pinned][stubs][window turns ...], rebuilt only at epoch rebases."""
    tier = tier or model.cache_tier()
    eng = Engine(model, tier)
    res = Result("cvm", model, tier, Buckets(), [], eng.breaks)
    p = eng.prompt
    tail = policy.pin_position == "tail"
    cap = policy.hard_cap_tokens
    if policy.respect_price_tier and model.long_context:
        lc_cap = int(model.long_context.threshold * 0.9)
        cap = lc_cap if cap is None else min(cap, lc_cap)

    def pinned(t: int) -> int:
        return int(min(policy.pinned_max, policy.pinned_base + policy.pinned_per_turn * t))

    window: list[int] = []  # tokens per verbatim turn, oldest first
    stubs = 0
    since_rebase = 0

    def layout(t: int) -> None:
        p.truncate(1)
        p.append(0 if tail else pinned(t), new=False)
        p.append(stubs, new=False)
        for n in window:
            p.append(n, new=False)
        if tail:
            p.append(pinned(t), new=False)

    def page_out() -> None:
        nonlocal stubs, window, since_rebase
        kept, acc = [], 0
        for n in reversed(window):
            if acc + n > policy.window_tokens:
                break
            kept.append(n)
            acc += n
        kept.reverse()
        stubs += policy.stub_tokens_per_turn * (len(window) - len(kept))
        window = kept
        since_rebase = 0
        res.rebases += 1

    p.append(trace.head_tokens)
    layout(0)

    for t, turn in enumerate(trace.turns, 1):
        b = eng.idle(turn.gap_s, keepalive)
        if t > 1:
            recent = window[-5:] or [0]
            projected = p.total + 2 * sum(recent) / len(recent)
            cause = None
            if since_rebase >= policy.epoch_turns:
                cause = "rebase"
            elif policy.opportunistic and not eng.warm() and since_rebase >= 1:
                cause = "rebase_on_expiry"  # the prefix is lost anyway, so the rebase is nearly free
            elif cap is not None and projected > cap:
                cause = "rebase_cap"
            if cause:
                page_out()
                layout(t)
                eng.pending_cause = "rebase"
                res.events[cause] += 1
            elif not tail and policy.pin_updates == "immediate" and trace.turns[t - 2].pin_change:
                layout(t)
                eng.pending_cause = "pin"
                res.events["pin_update"] += 1
        reqs = list(turn.reqs)
        if turn.recall:
            reqs.insert(1, Req(added=policy.recall_tokens, output=policy.recall_call_tokens,
                               kept=policy.recall_call_tokens))
            res.events["recall"] += 1
        turn_tokens = 0
        peak = 0
        for j, req in enumerate(reqs):
            if j:
                eng.now += trace.intra_gap_s
            added = req.added
            if j and policy.page_tool_output_over and added > policy.page_tool_output_over:
                added = policy.tool_output_excerpt_tokens
                res.events["tool_output_paged"] += 1
            if tail:
                p.pop()
            p.append(added)
            if tail:
                p.append(pinned(t), new=False)
                eng.pending_cause = eng.pending_cause or "pin"
            peak = max(peak, p.total)
            b.add(eng.send(req.output))
            if tail:
                p.pop()
            p.append(req.kept)
            if tail:
                p.append(pinned(t), new=False)
            turn_tokens += added + req.kept
        window.append(turn_tokens)
        since_rebase += 1
        _finish_turn(res, t, b, peak)
    res.max_prompt = eng.max_prompt
    return res


# Lane 1 measured data


def _parse_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError as e:
        raise ValueError(f"bad ts {s!r}: {e}") from e


def load_measured(path: Path) -> list[dict]:
    """Load lane 1 rows (one per upstream request) from JSONL or CSV.

    Every LANE1_FIELDS key is required except ``cache_creation_input_tokens``, which
    Codex rows leave empty because the Responses API does not report cache writes; an
    empty or missing value loads as None. Extra fields are ignored except ``ts``.
    """
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    if path.suffix == ".csv":
        rows = list(csv.DictReader(text.splitlines()))
    else:
        rows = []
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"{path}:{n}: bad JSON: {e}") from e
    out = []
    for n, r in enumerate(rows, 1):
        missing = [f for f in LANE1_FIELDS
                   if f != "cache_creation_input_tokens" and (f not in r or r[f] in (None, ""))]
        if missing:
            raise ValueError(f"{path} row {n}: missing {', '.join(missing)}")
        try:
            row = {f: int(r[f]) for f in LANE1_FIELDS if f != "cache_creation_input_tokens"}
            cc = r.get("cache_creation_input_tokens")
            row["cache_creation_input_tokens"] = None if cc in (None, "") else int(cc)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{path} row {n}: non-integer field: {e}") from e
        if r.get("ts") not in (None, ""):
            row["ts"] = _parse_ts(str(r["ts"]))
        out.append(row)
    return out


def price_measured(rows: list[dict], model: Model, tier: CacheTier | None = None,
                   missing_creation: str = "write") -> Buckets:
    """Price observed usage buckets as reported.

    ``input_tokens`` is the uncached part (lane 1 subtracts cached tokens from Codex
    totals). When ``cache_creation_input_tokens`` is None, ``missing_creation`` says how
    to price ``input_tokens`` on a model that bills cache writes: "write" (upper bound,
    every uncached token was cached) or "input" (lower bound).
    """
    tier = tier or model.cache_tier()
    total = Buckets()
    for r in rows:
        cc = r["cache_creation_input_tokens"]
        inp = r["input_tokens"]
        if cc is None:
            cc, inp = (inp, 0) if missing_creation == "write" and model.cache_mode == "explicit" else (0, inp)
        prompt = inp + cc + r["cache_read_input_tokens"]
        in_usd = inp * model.input + cc * tier.write + r["cache_read_input_tokens"] * model.cache_read
        out_usd = r["output_tokens"] * model.output
        lc = model.long_context
        if lc and prompt > lc.threshold:
            in_usd *= lc.input_mult
            out_usd *= lc.output_mult
        total.add(Buckets(uncached=inp, write=cc, read=r["cache_read_input_tokens"], output=r["output_tokens"],
                          usd=(in_usd + out_usd) / MTOK, requests=1))
    return total


def trace_from_measured(rows: list[dict], name: str, intra_gap_s: float = 15.0) -> tuple[Trace, int]:
    """Turn measured rows into a replayable trace.

    The first request's whole prompt becomes the head. Each later request's growth is
    its prompt size minus the previous prompt size, so the previous visible output is
    folded into ``added`` and ``kept`` is zero. Rows sharing a ``turn`` value form one
    turn; with ``ts`` present the gap before a turn is the time since the previous row.
    Returns the trace and the number of shrinking prompts (a compaction or reset in the
    measured session), which contribute no growth.
    """
    if not rows:
        raise ValueError("no measured rows")
    sizes = [r["input_tokens"] + (r["cache_creation_input_tokens"] or 0) + r["cache_read_input_tokens"]
             for r in rows]
    shrinks = 0
    turns: list[Turn] = []
    cur: list[Req] = []
    cur_turn, cur_gap = rows[0]["turn"], 0.0
    for i, r in enumerate(rows):
        delta = 0 if i == 0 else sizes[i] - sizes[i - 1]
        if delta < 0:
            shrinks += 1
            delta = 0
        if r["turn"] != cur_turn:
            turns.append(Turn(cur_gap, tuple(cur)))
            gap = r["ts"] - rows[i - 1]["ts"] if "ts" in r and "ts" in rows[i - 1] else 0.0
            cur, cur_turn, cur_gap = [], r["turn"], max(0.0, gap)
        cur.append(Req(added=delta, output=r["output_tokens"], kept=0))
    turns.append(Turn(cur_gap, tuple(cur)))
    return Trace(name, sizes[0], intra_gap_s, tuple(turns), source="measured"), shrinks


# Scenario helpers


def compaction_for(prices: dict, model: Model, **overrides) -> Reset:
    c = prices["compaction"][model.vendor]
    if "threshold_tokens" in c:
        threshold = int(_value(c["threshold_tokens"]))
    else:
        threshold = int(model.context_window * _value(c["threshold_fraction"]))
    kw = dict(kind="compaction", threshold=min(threshold, model.context_window),
              output_tokens=int(_value(c["summary_tokens"])),
              seed_tokens=int(_value(c["summary_tokens"])) + int(_value(c["reattach_tokens"])),
              instruction_tokens=int(_value(c["instruction_tokens"])), reuse_cache=bool(_value(c["reuse_cache"])))
    kw.update(overrides)
    return Reset(**kw)


def rollover_for(prices: dict, profile: dict, model: Model, **overrides) -> Reset:
    r = profile["rollover"]
    kw = dict(kind="rollover",
              threshold=int(compaction_for(prices, model).threshold * r["threshold_fraction_of_compaction"]),
              output_tokens=r["handoff_output_tokens"], seed_tokens=r["seed_tokens"],
              instruction_tokens=r["instruction_tokens"])
    kw.update(overrides)
    return Reset(**kw)


STRATEGIES = ("plain", "compaction", "rollover", "cvm")
LABEL = {"plain": "(i) plain", "compaction": "(ii) compaction", "rollover": "H4 rollover", "cvm": "(iii) cvm"}


def run_all(trace: Trace, prices: dict, profile: dict, model: Model, policy: CvmPolicy | None = None,
            tier: str | None = None, keepalive: Keepalive | None = None) -> dict[str, Result]:
    t = model.cache_tier(tier)
    policy = policy or CvmPolicy(**profile["cvm"]["policy"])
    return {
        "plain": run_plain(trace, model, t, keepalive=keepalive),
        "compaction": run_plain(trace, model, t, compaction_for(prices, model), keepalive=keepalive),
        "rollover": run_plain(trace, model, t, rollover_for(prices, profile, model), keepalive=keepalive),
        "cvm": run_cvm(trace, model, t, policy, keepalive=keepalive),
    }


def optimal_epoch(rebuilt: float, growth: float, requests: float, write: float, read: float) -> float:
    """Epoch length (turns) minimizing per-turn cost of an append-only window rebased every E turns.

    A rebase rewrites ``rebuilt`` tokens (pinned state, stubs, kept window; the system
    head stays cached) at ``write`` instead of reading them. Between rebases each of
    ``requests`` requests per turn re-reads the growth appended since the rebase. Per
    turn that is (write - read) * rebuilt / E + read * requests * growth * E / 2, whose
    minimum is at E = sqrt(2 * (write - read) * rebuilt / (read * requests * growth)).
    """
    return math.sqrt(2 * (write - read) * rebuilt / (read * requests * growth))


# Report


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _k(n: float) -> str:
    return f"{n / 1000:,.1f}k" if abs(n) < 1_000_000 else f"{n / 1_000_000:,.2f}M"


def _table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def trace_stats(trace: Trace, ttls: tuple[float, ...] = (300, 1800, 3600)) -> dict:
    reqs = [r for t in trace.turns for r in t.reqs]
    growth = sum(r.added + r.kept for r in reqs)
    gaps = [t.gap_s for t in trace.turns[1:]]
    tool = sorted(r.added for t in trace.turns for r in t.reqs[1:])

    def pct(q: float) -> int:
        return tool[min(len(tool) - 1, int(q * len(tool)))] if tool else 0

    return {
        "turns": len(trace.turns),
        "requests": len(reqs),
        "requests_per_turn": len(reqs) / len(trace.turns),
        "growth_per_turn": growth / len(trace.turns),
        "total_growth": growth,
        "output_total": sum(r.output for r in reqs),
        "tool_results": len(tool),
        "tool_p50": pct(0.5),
        "tool_p90": pct(0.9),
        "tool_max": tool[-1] if tool else 0,
        "gap_over": {ttl: sum(g > ttl for g in gaps) / max(1, len(gaps)) for ttl in ttls},
        "recalls": sum(t.recall for t in trace.turns),
        "pin_changes": sum(t.pin_change for t in trace.turns),
    }


def _note(r: Result) -> str:
    if r.strategy == "plain":
        return f"over the {_k(r.model.context_window)} window from turn {r.over_window_turn}" \
            if r.over_window_turn else "fits the window"
    if r.strategy in RESET_CAUSES:
        return f"{r.resets} {r.strategy}s"
    if r.strategy == "clear":
        return f"{r.resets} clears"
    return f"{r.rebases} rebases"


def build_blocks(prices: dict, profile: dict) -> dict[str, str]:
    models: dict[str, Model] = prices["parsed"]
    rep = prices["report"]
    keys = rep["models"]
    focus = models[rep["focus_model"]]
    trace = generate_trace(profile)
    st = trace_stats(trace)
    policy = CvmPolicy(**profile["cvm"]["policy"])
    sweep_basis = f"Basis: modeled on the `{trace.name}` profile (inferred), seed {profile['seed']}."
    basis = f"Basis: modeled on the `{trace.name}` profile (inferred), {st['turns']} turns, seed {profile['seed']}."
    blocks: dict[str, str] = {}

    rows = []
    for k in keys:
        m = models[k]
        tiers = ", ".join(f"{t.name}: ${t.write:g}" for t in m.cache_tiers)
        lc = (f"x{m.long_context.input_mult:g} input, x{m.long_context.output_mult:g} output above "
              f"{_k(m.long_context.threshold)}") if m.long_context else "none"
        rows.append([m.display, f"${m.input:g}", f"${m.output:g}", f"${m.cache_read:g}", tiers, _k(m.context_window),
                     lc])
    blocks["prices"] = _table(["Model", "Input", "Output", "Cache read", "Cache write by TTL", "Window",
                               "Long-context pricing"], rows) + (
        f"\n\nUSD per million tokens, retrieved {prices['retrieved']}, vendor-documented. Source URL per value: "
        "`spikes/m0/cost_model/prices.json`.")

    blocks["profile"] = _table(["Quantity (inferred profile)", "Value"], [
        ["Turns", st["turns"]],
        ["API requests, one per tool round trip", f"{st['requests']} ({st['requests_per_turn']:.2f} per turn)"],
        ["Head: system prompt and tools", _k(trace.head_tokens)],
        ["Context growth per turn, mean", _k(st["growth_per_turn"])],
        ["Full history after the last turn", _k(st["total_growth"] + trace.head_tokens)],
        ["Tool results: p50 / p90 / max", f"{_k(st['tool_p50'])} / {_k(st['tool_p90'])} / {_k(st['tool_max'])}"],
        ["Output tokens, thinking included", _k(st["output_total"])],
        ["Think time over 5 min / 30 min / 1 h",
         f"{st['gap_over'][300]:.0%} / {st['gap_over'][1800]:.0%} / {st['gap_over'][3600]:.0%} of turns"],
        ["Turns with a recall, (iii) only", st["recalls"]],
        ["Turns that change pinned state", st["pin_changes"]],
    ])

    rows, ratio_rows, all_rs = [], [], {}
    for k in keys:
        m = models[k]
        rs = run_all(trace, prices, profile, m, policy)
        all_rs[k] = rs
        for s in STRATEGIES:
            r = rs[s]
            tot = r.total
            rows.append([m.display, LABEL[s], _k(tot.uncached), _k(tot.write), _k(tot.read), _k(tot.output),
                         _usd(tot.usd), _k(r.max_prompt), _note(r)])
        c = rs["compaction"].total.usd
        ratio_rows.append([m.display] + [_usd(rs[s].total.usd) for s in STRATEGIES]
                          + [f"{rs['cvm'].total.usd / c:.2f}", f"{rs['rollover'].total.usd / c:.2f}"])
    blocks["headline"] = _table(["Model", "Strategy", "Uncached in", "Cache write", "Cache read", "Output", "Cost",
                                 "Max prompt", "Note"], rows) + "\n\n" + basis
    blocks["ratios"] = _table(["Model"] + [LABEL[s] for s in STRATEGIES] + ["(iii) / (ii)", "H4 / (ii)"],
                              ratio_rows) + "\n\n" + basis

    rs = all_rs[focus.key]
    step = max(1, len(trace.turns) // 12)
    marks = sorted({1, *range(step, len(trace.turns) + 1, step), len(trace.turns)})
    rows = []
    for t in marks:
        row = [t]
        for s in STRATEGIES:
            tr = rs[s].turns[t - 1]
            row += [_k(tr.prompt_tokens), _usd(tr.cum_usd)]
        rows.append(row)
    head = ["Turn"]
    for s in STRATEGIES:
        head += [f"{LABEL[s]} prompt", f"{LABEL[s]} cumulative"]
    blocks["per_turn"] = _table(head, rows) + f"\n\n{focus.display}. Prompt is the largest request in the turn. " + basis

    rows = []
    for t in sorted({1, *range(50, len(trace.turns) + 1, 50)}):
        for s in STRATEGIES:
            b = rs[s].turns[t - 1].b
            rows.append([t, LABEL[s], b.requests, _k(b.uncached), _k(b.write), _k(b.read), _k(b.output),
                         f"${b.usd:.3f}"])
    blocks["per_turn_buckets"] = _table(["Turn", "Strategy", "Requests", "Uncached in", "Cache write", "Cache read",
                                         "Output", "Turn cost"], rows) + (
        f"\n\n{focus.display}. Full per-turn rows for every model: `cost_model.py --per-turn-csv` (written under "
        "`.contexto/spikes/l2a/`). " + basis)

    rows = []
    for s in STRATEGIES:
        r = rs[s]
        causes = sorted(c for c in r.breaks.count if c != "keepalive_ping") or ["none"]
        for c in causes:
            rows.append([LABEL[s], c, r.breaks.count.get(c, 0), _k(r.breaks.tokens.get(c, 0)),
                         _usd(r.breaks.usd.get(c, 0.0))])
    blocks["breaks"] = _table(["Strategy", "Cause", "Breaks", "Tokens rewritten", "Cost above a cache read"],
                              rows) + f"\n\n{focus.display}, 5 min TTL. " + basis

    variants = [
        (f"default: pinned head, rebase every {policy.epoch_turns} turns or on expiry", policy, None, None),
        ("no rebase on expiry", replace(policy, opportunistic=False), None, None),
        ("pinned state rebuilt at once on every change", replace(policy, pin_updates="immediate"), None, None),
        ("pinned state at the tail, re-sent every request", replace(policy, pin_position="tail"), None, None),
        ("tool results over 8k paged at once", replace(policy, page_tool_output_over=8000), None, None),
        ("1 h cache TTL", policy, "1h", None),
        ("keepalive pings through idle gaps up to 1 h", policy, None, Keepalive()),
    ]
    rows = []
    for label, pol, tier, ka in variants:
        r = run_cvm(trace, focus, focus.cache_tier(tier), pol, keepalive=ka)
        nb = sum(v for c, v in r.breaks.count.items() if c != "keepalive_ping")
        rows.append([label, _usd(r.total.usd), nb, _usd(sum(r.breaks.usd.values())), r.breaks.count["keepalive_ping"],
                     _k(r.max_prompt)])
    blocks["variants"] = _table(["(iii) variant", "Cost", "Cache breaks", "Break cost", "Pings", "Max prompt"],
                                rows) + f"\n\n{focus.display}. " + basis

    rows = []
    for tier, ka, label in [(None, None, "5 min TTL (Claude Code default with an API key)"),
                            ("1h", None, "1 h TTL (Claude Code main conversation on a subscription)"),
                            (None, Keepalive(), "5 min TTL plus keepalive pings up to 1 h idle")]:
        rr = run_all(trace, prices, profile, focus, policy, tier=tier, keepalive=ka)
        rows.append([label] + [_usd(rr[s].total.usd) for s in STRATEGIES])
    blocks["ttl"] = _table(["Cache setting"] + [LABEL[s] for s in STRATEGIES], rows) + \
        f"\n\n{focus.display}. Subscription use is billed against plan limits, not dollars; the dollar column is the API-price equivalent of the same tokens. " + basis

    windows, epochs = rep["sweep_windows"], rep["sweep_epochs"]
    compact_usd = rs["compaction"].total.usd
    rows, best = [], {}
    for w in windows:
        costs = [run_cvm(trace, focus, None, replace(policy, window_tokens=w, epoch_turns=e)).total.usd for e in epochs]
        lo = min(costs)
        best[w] = epochs[costs.index(lo)]
        rows.append([_k(w)] + [f"**{_usd(c)}**" if c == lo else _usd(c) for c in costs] + [f"{lo / compact_usd:.2f}"])
    blocks["sweep"] = _table(["Window"] + [f"E={e}" for e in epochs] + ["best / (ii)"], rows) + (
        f"\n\n(iii) cost for {focus.display}, rebase on expiry on. Bold is the cheapest epoch length for each window. "
        f"(ii) costs {_usd(compact_usd)}. " + basis)

    tier = focus.cache_tier()
    no_gap = replace(trace, turns=tuple(replace(t, gap_s=0.0) for t in trace.turns))
    rows = []
    mean_stubs = policy.stub_tokens_per_turn * st["turns"] / 2
    epoch_gaps = []
    for w in windows:
        rebuilt = policy.pinned_base + mean_stubs + w
        e_star = optimal_epoch(rebuilt, st["growth_per_turn"], st["requests_per_turn"], tier.write, focus.cache_read)
        costs = [run_cvm(no_gap, focus, None, replace(policy, window_tokens=w, epoch_turns=e, opportunistic=False))
                 .total.usd for e in epochs]
        sim = epochs[costs.index(min(costs))]
        epoch_gaps.append(abs(e_star - sim) / sim)
        rows.append([_k(w), f"{e_star:.1f}", sim, best[w]])
    blocks["epoch_rule"] = _table(["Window", "Analytic E*", "Simulated best E, no idle gaps",
                                   "Simulated best E, profile think time"], rows) + (
        f"\n\n{focus.display}: write ${tier.write:g}, read ${focus.cache_read:g} per MTok. " + basis)

    rows = []
    thresholds = rep["sweep_reset_thresholds"]
    cvm_usd = rs["cvm"].total.usd
    for th in thresholds:
        c = run_plain(trace, focus, None, compaction_for(prices, focus, threshold=th))
        h = run_plain(trace, focus, None, rollover_for(prices, profile, focus, threshold=th))
        rows.append([_k(th), _usd(c.total.usd), c.resets, _usd(h.total.usd), h.resets,
                     f"{cvm_usd / c.total.usd:.2f}"])
    blocks["reset_sweep"] = _table(["Reset threshold", "(ii) compaction", "Compactions", "H4 rollover", "Rollovers",
                                    "(iii) / (ii)"], rows) + (
        f"\n\n{focus.display}. Claude Code accepts an auto-compact window from 100K to 1M "
        f"(`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, vendor-documented). (iii) costs {_usd(cvm_usd)}. " + basis)

    rows = []
    plain_usd = rs["plain"].total.usd
    for label, cp in [("trigger 100k, keep 3, clear_at_least 0 (vendor defaults)", ClearPolicy()),
                      ("trigger 100k, keep 3, clear_at_least 20k", ClearPolicy(clear_at_least=20_000)),
                      ("trigger 100k, keep 3, clear_at_least 50k", ClearPolicy(clear_at_least=50_000)),
                      ("trigger 200k, keep 3, clear_at_least 50k", ClearPolicy(trigger=200_000, clear_at_least=50_000))]:
        r = run_plain(trace, focus, None, clear=cp)
        rows.append([label, _usd(r.total.usd), r.resets, _k(r.max_prompt), f"{r.total.usd / plain_usd:.2f}",
                     f"{cvm_usd / r.total.usd:.2f}"])
    oai = models.get("gpt-6-astra")
    if oai:
        o_plain = all_rs[oai.key]["plain"].total.usd if oai.key in all_rs else run_plain(trace, oai).total.usd
        o_cvm = all_rs[oai.key]["cvm"].total.usd if oai.key in all_rs else run_cvm(trace, oai, None, policy).total.usd
        r = run_plain(trace, oai, None, compaction_for(prices, oai, threshold=200_000))
        rows.append([f"{oai.display}: server-side compaction, compact_threshold 200k", _usd(r.total.usd), r.resets,
                     _k(r.max_prompt), f"{r.total.usd / o_plain:.2f}", f"{o_cvm / r.total.usd:.2f}"])
    blocks["h5"] = _table(["H5 provider-native setting", "Cost", "Clears or compactions", "Max prompt",
                           "vs (i) plain", "(iii) / this"], rows) + (
        f"\n\n{focus.display} unless named. Clearing replaces each cleared tool result with a 15-token placeholder "
        "(placeholder size inferred) and breaks the cache from the first cleared block. OpenAI's compacted item size "
        "is not documented; the row reuses the (ii) summary assumption. " + basis)

    rows = []
    for n in rep["sweep_turns"]:
        rr = run_all(generate_trace(profile, turns=n), prices, profile, focus, policy)
        c = rr["compaction"].total.usd
        rows.append([n] + [_usd(rr[s].total.usd) for s in STRATEGIES] + [f"{rr['cvm'].total.usd / c:.2f}",
                                                                       rr["compaction"].resets])
    blocks["by_length"] = _table(["Turns"] + [LABEL[s] for s in STRATEGIES] + ["(iii) / (ii)", "Compactions"],
                                 rows) + f"\n\n{focus.display}. " + sweep_basis

    rows = []
    for label, scale in rep["sweep_tool_scale"]:
        prof = json.loads(json.dumps(profile))
        prof["tool_output_tokens"]["median"] = profile["tool_output_tokens"]["median"] * scale
        tr = generate_trace(prof)
        rr = run_all(tr, prices, profile, focus, policy)
        c = rr["compaction"].total.usd
        rows.append([label, _k(trace_stats(tr)["growth_per_turn"])] + [_usd(rr[s].total.usd) for s in STRATEGIES]
                    + [f"{rr['cvm'].total.usd / c:.2f}"])
    blocks["by_tool_size"] = _table(["Tool output median", "Growth per turn"] + [LABEL[s] for s in STRATEGIES]
                                    + ["(iii) / (ii)"], rows) + f"\n\n{focus.display}. " + basis

    rows = []
    for label, think in rep["sweep_think"]:
        prof = json.loads(json.dumps(profile))
        if think is not None:
            prof["think_time_s"] = think
        tr = generate_trace(prof)
        rr = run_all(tr, prices, profile, focus, policy)
        c = rr["compaction"].total.usd
        rows.append([label, f"{trace_stats(tr)['gap_over'][300]:.0%}"] + [_usd(rr[s].total.usd) for s in STRATEGIES]
                    + [f"{rr['cvm'].total.usd / c:.2f}"])
    blocks["by_think"] = _table(["Think time", "Gaps over 5 min"] + [LABEL[s] for s in STRATEGIES]
                                + ["(iii) / (ii)"], rows) + f"\n\n{focus.display}, 5 min TTL. " + basis

    rows = []
    for th in (compaction_for(prices, focus).threshold, 200_000):
        for reuse in (True, False):
            r = run_plain(trace, focus, None, compaction_for(prices, focus, reuse_cache=reuse, threshold=th))
            rows.append([_k(th), "reads the warm prefix (vendor-documented)" if reuse else
                         "full miss (as after an idle gap)", r.resets, _usd(r.total.usd), f"{cvm_usd / r.total.usd:.2f}"])
    blocks["compaction_cache"] = _table(["Threshold", "(ii) summary request", "Compactions", "(ii) cost",
                                         "(iii) / (ii)"], rows) + f"\n\n{focus.display}. " + basis

    rows = []
    for rate, size in rep["sweep_recall"]:
        prof = json.loads(json.dumps(profile))
        prof["cvm"]["recall_rate"] = rate
        tr = generate_trace(prof)
        r = run_cvm(tr, focus, None, replace(policy, recall_tokens=size))
        rows.append([f"{rate:g}", _k(size), r.events["recall"], _usd(r.total.usd), f"{r.total.usd / compact_usd:.2f}"])
    blocks["by_recall"] = _table(["Recalls per turn", "Recall size", "Recalls", "(iii) cost", "(iii) / (ii)"], rows) + (
        f"\n\n{focus.display}. Each recall is one extra upstream round trip that the proxy answers itself. "
        f"(ii) costs {_usd(compact_usd)}. " + basis)

    # Findings: the sentences the doc's summary cites, computed from the runs above.
    ratios = {k: all_rs[k]["cvm"].total.usd / all_rs[k]["compaction"].total.usd for k in keys}
    plain = rs["plain"]
    ttl_share = plain.breaks.usd.get("ttl", 0.0) / plain.total.usd
    hour = run_all(trace, prices, profile, focus, policy, tier="1h")
    c100 = run_plain(trace, focus, None, compaction_for(prices, focus, threshold=100_000))
    cross = None
    for n in range(1, 61):
        rr = run_all(generate_trace(profile, turns=n), prices, profile, focus, policy)
        if rr["cvm"].total.usd < rr["compaction"].total.usd:
            cross = n
            break
    ka = run_cvm(trace, focus, None, policy, keepalive=Keepalive())
    lo_k, hi_k = min(ratios, key=ratios.get), max(ratios, key=ratios.get)
    blocks["findings"] = "\n".join([
        f"- Over {st['turns']} turns on {focus.display}, (iii) costs {_usd(rs['cvm'].total.usd)} against "
        f"{_usd(rs['compaction'].total.usd)} for (ii) built-in compaction, {_usd(rs['rollover'].total.usd)} for H4 "
        f"rollover, and {_usd(plain.total.usd)} for (i) plain, which would pass the "
        f"{_k(focus.context_window)} window at turn {plain.over_window_turn}. Across the five models (iii) costs "
        f"{ratios[lo_k]:.2f} to {ratios[hi_k]:.2f} of (ii) ({models[lo_k].display} to {models[hi_k].display}).",
        f"- Cache expiry, not prompt rewriting, is the largest single cost in (i): {ttl_share:.0%} of its cost is "
        f"the premium (write price minus read price) for re-writing prefixes after idle gaps over the 5 minute TTL. The 1 h TTL cuts (i) to "
        f"{_usd(hour['plain'].total.usd)} and (ii) to {_usd(hour['compaction'].total.usd)}; (iii) gains less "
        f"({_usd(hour['cvm'].total.usd)}) because its prefix is small. Keepalive pings bring (iii) to "
        f"{_usd(ka.total.usd)}.",
        f"- Bounding the prompt is most of the saving. Compaction configured at 100k costs "
        f"{_usd(c100.total.usd)} ({c100.resets} lossy compactions), within "
        f"{c100.total.usd / rs['cvm'].total.usd - 1:.0%} of (iii); the difference between them is fidelity, which "
        "this model does not score (lane 2 packet L2b).",
        f"- (iii) costs more than (ii) for very short sessions: it first beats (ii) at turn {cross}. Its fixed "
        "overhead is the pinned block, stubs, and recall round trips.",
        f"- Under the profile's think time, rebases mostly ride on cache expiry, so epoch length barely matters "
        f"and window size sets the cost. With no idle gaps the analytic rule E* = sqrt(2 (w - r) R / (r n g)) "
        f"lands within {max(epoch_gaps):.0%} of the simulated optimum on the tested grid.",
    ])

    sample_path = HERE / "testdata" / "lane1_sample.jsonl"
    mrows = load_measured(sample_path)
    mt, shrinks = trace_from_measured(mrows, "lane1 synthetic sample")
    oai_rows = load_measured(HERE / "testdata" / "lane1_codex_sample.csv")
    rr = run_all(mt, prices, profile, focus, policy)
    rows = [
        ["Files", "`testdata/lane1_sample.jsonl` (Claude Code shape), `testdata/lane1_codex_sample.csv` (Codex shape)"],
        ["Basis", "synthetic test input, not measured"],
        ["Claude rows / turns / shrinking prompts", f"{len(mrows)} / {len(mt.turns)} / {shrinks}"],
        [f"Claude rows priced as reported, {focus.display}", _usd(price_measured(mrows, focus).usd)],
        ["Claude rows replayed: " + " / ".join(LABEL[s] for s in STRATEGIES),
         " / ".join(_usd(rr[s].total.usd) for s in STRATEGIES)],
    ]
    if oai:
        hi = price_measured(oai_rows, oai, missing_creation="write").usd
        lo = price_measured(oai_rows, oai, missing_creation="input").usd
        rows.append([f"Codex rows priced on {oai.display}, empty cache writes as writes / as input",
                     f"{_usd(hi)} / {_usd(lo)}"])
    blocks["lane1"] = _table(["Quantity", "Value"], rows)
    return blocks


BEGIN = "<!-- BEGIN GENERATED: {} -->"
END = "<!-- END GENERATED: {} -->"


def render_doc(text: str, blocks: dict[str, str]) -> str:
    for name, body in blocks.items():
        b, e = BEGIN.format(name), END.format(name)
        if b not in text or e not in text:
            raise ValueError(f"doc has no generated block {name!r}")
        head, rest = text.split(b, 1)
        _, tail = rest.split(e, 1)
        text = f"{head}{b}\n{body}\n{e}{tail}"
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prices", type=Path, default=PRICES)
    ap.add_argument("--profile", type=Path, default=PROFILE)
    ap.add_argument("--doc", type=Path, default=DOC)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--write-doc", action="store_true", help="rewrite generated blocks in the doc")
    g.add_argument("--check-doc", action="store_true", help="exit 1 if the doc's generated blocks are stale")
    g.add_argument("--measured", type=Path, help="price and replay a lane 1 JSONL or CSV file")
    g.add_argument("--per-turn-csv", type=Path, nargs="?", const=SPIKE_OUT / "per_turn.csv",
                   help=f"write per-turn rows for every model and strategy (default {SPIKE_OUT}/per_turn.csv)")
    args = ap.parse_args(argv)

    try:
        prices = load_prices(args.prices)
        profile = load_profile(args.profile)
    except (OSError, json.JSONDecodeError, KeyError) as e:
        print(f"error: cannot load inputs: {e}", file=sys.stderr)
        return 2

    if args.measured:
        try:
            rows = load_measured(args.measured)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        tr, shrinks = trace_from_measured(rows, args.measured.name)
        for k in prices["report"]["models"]:
            m = prices["parsed"][k]
            rr = run_all(tr, prices, profile, m)
            print(f"{m.display}: observed {_usd(price_measured(rows, m).usd)} (measured); replay (modeled) "
                  + ", ".join(f"{LABEL[s]} {_usd(rr[s].total.usd)}" for s in STRATEGIES) + f"; shrinks {shrinks}")
        return 0

    if args.per_turn_csv:
        trace = generate_trace(profile)
        try:
            args.per_turn_csv.parent.mkdir(parents=True, exist_ok=True)
            with args.per_turn_csv.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["model", "strategy", "turn", "prompt_tokens", "uncached", "cache_write", "cache_read",
                            "output", "usd", "cum_usd"])
                for k in prices["report"]["models"]:
                    for s, r in run_all(trace, prices, profile, prices["parsed"][k]).items():
                        for tr in r.turns:
                            w.writerow([k, s, tr.turn, tr.prompt_tokens, tr.b.uncached, tr.b.write, tr.b.read,
                                        tr.b.output, f"{tr.b.usd:.6f}", f"{tr.cum_usd:.6f}"])
        except OSError as e:
            print(f"error: cannot write {args.per_turn_csv}: {e}", file=sys.stderr)
            return 2
        print(f"wrote {args.per_turn_csv}")
        return 0

    blocks = build_blocks(prices, profile)
    if args.write_doc or args.check_doc:
        try:
            current = args.doc.read_text()
        except OSError as e:
            print(f"error: cannot read doc: {e}", file=sys.stderr)
            return 2
        new = render_doc(current, blocks)
        if args.check_doc:
            if new != current:
                print("doc is stale: run cost_model.py --write-doc", file=sys.stderr)
                return 1
            print("doc matches script output")
            return 0
        args.doc.write_text(new)
        print(f"wrote {args.doc}")
        return 0
    for name, body in blocks.items():
        print(f"## {name}\n\n{body}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
