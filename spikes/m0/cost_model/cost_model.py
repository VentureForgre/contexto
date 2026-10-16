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
    kept: int  # output tokens that stay in the conversation
    thinking: int = 0  # part of ``kept`` that is thinking, when thinking stays in context


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


def trace_for(profile: dict, model: Model, **kw) -> Trace:
    """The profile's trace for ``model``, keeping thinking in context where that vendor does."""
    return generate_trace(profile, keep_thinking=bool(_value(profile["thinking_in_context"][model.vendor])), **kw)


def load_profile(path: Path = PROFILE) -> dict:
    return json.loads(Path(path).read_text())


def generate_trace(profile: dict, turns: int | None = None, seed: int | None = None,
                   keep_thinking: bool = False) -> Trace:
    """Seeded request trace. By default thinking is billed as output but not kept in context;
    ``keep_thinking`` keeps it, as when a model preserves prior thinking blocks."""
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
            reqs.append(Req(added=added, output=visible + th, kept=visible + (th if keep_thinking else 0),
                            thinking=th if keep_thinking else 0))
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


RESET_CAUSES = ("compaction", "rollover", "cli_compaction")


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
        self.cold_start = 0  # this many first requests miss the cache entirely (observed on a rig seat)
        self.sends = 0
        self.route_miss_rate = 0.0  # chance a warm, byte-identical prefix still misses (route-side)
        self._miss_rng = random.Random(7)

    def warm(self) -> bool:
        return self.last_send is not None and self.now - self.last_send <= self.tier.ttl_s

    def send(self, output: int, cause: str | None = None, reuse_cache: bool = True) -> Buckets:
        p = self.prompt
        cause = cause or self.pending_cause
        self.pending_cause = None
        cold_start = self.sends < self.cold_start
        self.sends += 1
        if cold_start:
            cause = "cold_start"
        warm = self.warm() and reuse_cache and not cold_start
        if warm and self.route_miss_rate and self._miss_rng.random() < self.route_miss_rate:
            warm = False
            cause = "route_miss"
        hit = p.reusable() if warm else 0
        b = price(self.model, self.tier, p.total, hit, output)
        # Tokens re-sent at miss price although they are not new since the last send.
        rewritten = max(0, p.total - hit - p.new_since_send) if self.last_send is not None else 0
        if rewritten:
            if not self.warm() and cause not in RESET_CAUSES + ("cold_start", "route_miss"):
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
class IdleCompaction:
    """Claude Code's idleCompaction: on the 1 h TTL, compact an idle conversation shortly before expiry."""

    min_tokens: int = 200_000  # CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS default
    lead_s: float = 60.0  # how long before expiry it fires (inferred; docs say "shortly before")


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
    bound_tokens: int | None = None  # hard working-context bound, enforced at turn boundaries
    headroom_tokens: int = 0  # free space kept under the bound at each turn start, for the coming turn
    headroom_online: bool = False  # size headroom from turns seen so far instead of a fixed value
    headroom_margin: float = 0.25  # online: headroom = running max of completed turn growth x (1 + margin)
    headroom_floor_tokens: int = 25_000  # online: headroom before enough turns are seen, and its minimum
    bound_fallback: str = "none"  # "none": report requests over the bound; "clear": server-side tool-result clearing
    strip_output_overhead: float = 0.0  # extra output from stripping kept thinking (vendor: 4.6% to 67.1%)
    clear_keep: int = 3  # tool uses the server-side clearing fallback keeps (vendor default)
    clear_placeholder_tokens: int = 15
    stubs_max_tokens: int | None = None  # beyond this, older stubs merge into coarser index entries


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
              clear: ClearPolicy | None = None, keepalive: Keepalive | None = None,
              idle: IdleCompaction | None = None, cold_start: int = 0, route_miss_rate: float = 0.0) -> Result:
    """Strategy (i), or (ii) and H4 with ``reset``, or H5 with ``clear``.

    ``idle`` adds Claude Code's idle compaction to (ii): only on a TTL of an hour or more,
    when a gap would outlast the cache and the prompt holds at least ``idle.min_tokens``.
    """
    tier = tier or model.cache_tier()
    eng = Engine(model, tier)
    name = reset.kind if reset else ("clear" if clear else "plain")
    eng.cold_start = cold_start
    eng.route_miss_rate = route_miss_rate
    res = Result(name, model, tier, Buckets(), [], eng.breaks)
    p = eng.prompt
    p.append(trace.head_tokens)
    tools: list[int] = []  # block indices of tool results not yet cleared, oldest first

    def do_reset(b: Buckets) -> int:
        nonlocal tools
        p.append(reset.instruction_tokens)
        size = p.total
        b.add(eng.send(reset.output_tokens, reset.kind, reset.reuse_cache))
        p.truncate(1)
        p.append(reset.seed_tokens, new=False)
        eng.pending_cause = reset.kind
        tools = []
        res.resets += 1
        return size

    for t, turn in enumerate(trace.turns, 1):
        b = Buckets()
        gap = turn.gap_s
        if (idle and reset and tier.ttl_s >= 3600 and eng.last_send is not None
                and gap > tier.ttl_s - idle.lead_s and p.total >= idle.min_tokens):
            early = tier.ttl_s - idle.lead_s
            eng.now += early
            do_reset(b)
            res.events["idle_compaction"] += 1
            gap -= early
        b.add(eng.idle(gap, keepalive))
        peak = 0
        for j, req in enumerate(turn.reqs):
            if j:
                eng.now += trace.intra_gap_s
            if reset and p.total + req.added >= reset.threshold:
                peak = max(peak, do_reset(b))
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
            keepalive: Keepalive | None = None, cli_compaction: Reset | None = None,
            cli_idle: IdleCompaction | None = None, cold_start: int = 0,
            route_miss_rate: float = 0.0) -> Result:
    """Strategy (iii): [head][pinned][stubs][window turns ...], rebuilt only at epoch rebases.

    By default the CLI never compacts: its local history grows without bound while the
    proxy keeps the upstream prompt small. With ``cli_compaction`` the CLI compacts when
    its own history reaches the threshold (checked at turn start, a rough model): the
    summary request goes through the proxy as one bounded request, and the proxy then
    rebases with the CLI's summary as the only verbatim turn.
    """
    tier = tier or model.cache_tier()
    eng = Engine(model, tier)
    res = Result("cvm", model, tier, Buckets(), [], eng.breaks)
    eng.cold_start = cold_start
    eng.route_miss_rate = route_miss_rate
    p = eng.prompt
    tail = policy.pin_position == "tail"
    cap = policy.hard_cap_tokens
    if policy.respect_price_tier and model.long_context:
        lc_cap = int(model.long_context.threshold * 0.9)
        cap = lc_cap if cap is None else min(cap, lc_cap)

    def pinned(t: int) -> int:
        return int(min(policy.pinned_max, policy.pinned_base + policy.pinned_per_turn * t))

    window: list[int] = []  # tokens per verbatim turn, oldest first
    window_think: list[int] = []  # thinking tokens inside each window turn
    cur: list[int] = []  # blocks of the turn in progress, kept verbatim by any rebase
    stubs = 0
    since_rebase = 0

    def layout(t: int) -> None:
        p.truncate(1)
        p.append(0 if tail else pinned(t), new=False)
        p.append(stubs, new=False)
        for n in window + cur:
            p.append(n, new=False)
        if tail:
            p.append(pinned(t), new=False)

    def page_out(budget: int) -> None:
        nonlocal stubs, window, since_rebase
        nonlocal window_think
        # Kept turns lose their thinking blocks: an edited prefix invalidates them anyway.
        kept, acc = [], 0
        for n, th in zip(reversed(window), reversed(window_think)):
            if acc + n - th > budget:
                break
            kept.append(n - th)
            acc += n - th
            if th:
                res.events["thinking_stripped"] += th
        kept.reverse()
        window_think = [0] * len(kept)
        stubs += policy.stub_tokens_per_turn * (len(window) - len(kept))
        if policy.stubs_max_tokens is not None and stubs > policy.stubs_max_tokens:
            stubs = policy.stubs_max_tokens
            res.events["stubs_merged"] += 1
        window = kept
        since_rebase = 0
        res.rebases += 1

    p.append(trace.head_tokens)
    layout(0)
    local = trace.head_tokens  # the CLI's own history, which the proxy never shrinks
    seen_max = 0  # largest completed turn growth so far, for online headroom
    # With thinking kept in context, every strip makes the model re-derive what it dropped.
    has_thinking = any(r.thinking for tt in trace.turns for r in tt.reqs)
    out_mult = 1.0 + (policy.strip_output_overhead if has_thinking else 0.0)

    def headroom() -> int:
        if not policy.headroom_online:
            return policy.headroom_tokens
        return max(policy.headroom_floor_tokens, int(seen_max * (1 + policy.headroom_margin)))

    def cli_compact(t: int, b: Buckets, event: str) -> None:
        nonlocal stubs, window, window_think, since_rebase, local
        p.append(cli_compaction.instruction_tokens)
        b.add(eng.send(cli_compaction.output_tokens, "cli_compaction"))
        stubs += policy.stub_tokens_per_turn * len(window)
        if policy.stubs_max_tokens is not None:
            stubs = min(stubs, policy.stubs_max_tokens)
        window, window_think = [cli_compaction.seed_tokens], [0]
        since_rebase = 0
        layout(t)
        eng.pending_cause = "cli_compaction"
        res.events[event] += 1
        local = trace.head_tokens + cli_compaction.seed_tokens

    for t, turn in enumerate(trace.turns, 1):
        b = Buckets()
        gap = turn.gap_s
        if (cli_idle and cli_compaction and tier.ttl_s >= 3600 and eng.last_send is not None
                and gap > tier.ttl_s - cli_idle.lead_s and local >= cli_idle.min_tokens):
            early = tier.ttl_s - cli_idle.lead_s
            b.add(eng.idle(early, None))
            cli_compact(t, b, "cli_idle_compaction")
            gap -= early
        b.add(eng.idle(gap, keepalive))
        if cli_compaction and local + turn.reqs[0].added >= cli_compaction.threshold:
            cli_compact(t, b, "cli_compaction")
        elif t > 1:
            recent = window[-5:] or [0]
            projected = p.total + 2 * sum(recent) / len(recent)
            cause = None
            if since_rebase >= policy.epoch_turns:
                cause = "rebase"
            elif policy.opportunistic and not eng.warm() and since_rebase >= 1:
                cause = "rebase_on_expiry"  # the prefix is lost anyway, so the rebase is nearly free
            elif cap is not None and projected > cap:
                cause = "rebase_cap"
            elif policy.bound_tokens is not None and p.total + headroom() > policy.bound_tokens:
                cause = "rebase_headroom"
            if cause:
                budget = policy.window_tokens
                if policy.bound_tokens is not None:
                    fixed = p.total - sum(window)
                    budget = min(budget, max(0, policy.bound_tokens - headroom() - fixed))
                page_out(budget)
                layout(t)
                eng.pending_cause = "rebase"
                res.events[cause] += 1
            elif not tail and policy.pin_updates == "immediate" and trace.turns[t - 2].pin_change:
                # Rebuilding pinned state edits the prefix, so kept thinking after it is stripped.
                for i, th in enumerate(window_think):
                    if th:
                        window[i] -= th
                        res.events["thinking_stripped"] += th
                window_think = [0] * len(window)
                layout(t)
                eng.pending_cause = "pin"
                res.events["pin_update"] += 1
        reqs = list(turn.reqs)
        if turn.recall:
            reqs.insert(1, Req(added=policy.recall_tokens, output=policy.recall_call_tokens,
                               kept=policy.recall_call_tokens))
            res.events["recall"] += 1
        peak = 0
        cur = []  # [message or tool result, assistant output] per request so far this turn
        cur_think = []  # thinking inside each cur block (assistant outputs only)
        for j, req in enumerate(reqs):
            if j:
                eng.now += trace.intra_gap_s
            added = req.added
            if j and policy.page_tool_output_over and added > policy.page_tool_output_over:
                added = policy.tool_output_excerpt_tokens
                res.events["tool_output_paged"] += 1
            bound = policy.bound_tokens
            if bound is not None and p.total + added > bound:
                # No client-side edit inside an active tool-use turn: its thinking must be passed back
                # unchanged behind an unchanged prefix. Only the server-side fallback may act here.
                if j and policy.bound_fallback == "clear":
                    base = len(p.tok) - len(cur) - (1 if tail else 0)
                    results = list(range(2, len(cur), 2))  # earlier tool results of this turn
                    victims = results[: max(0, len(results) - max(0, policy.clear_keep - 1))]
                    freed = 0
                    for i in victims:
                        if cur[i] > policy.clear_placeholder_tokens:
                            freed += cur[i] - policy.clear_placeholder_tokens
                            cur[i] = policy.clear_placeholder_tokens
                            p.resize(base + i, cur[i])
                    if freed:
                        eng.pending_cause = "clear"
                        res.events["clear_fallback"] += 1
                if p.total + added > bound:
                    res.events["bound_exceeded"] += 1
            if tail:
                p.pop()
            p.append(added)
            if tail:
                p.append(pinned(t), new=False)
                eng.pending_cause = eng.pending_cause or "pin"
            peak = max(peak, p.total)
            b.add(eng.send(round(req.output * out_mult)))
            if tail:
                p.pop()
            p.append(req.kept)
            if tail:
                p.append(pinned(t), new=False)
            cur += [added, req.kept]
            cur_think += [0, req.thinking]
        local += sum(r.added + r.kept for r in turn.reqs)
        seen_max = max(seen_max, sum(cur))
        window.append(sum(cur))
        window_think.append(sum(cur_think))
        cur, cur_think = [], []
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


OPTIONAL_FIELDS = ("cache_creation_input_tokens", "request_bytes")


def load_measured(path: Path, creation_unknown: bool = False) -> list[dict]:
    """Load lane 1 rows (one per upstream request) from JSONL or CSV.

    Every LANE1_FIELDS key is required except ``cache_creation_input_tokens``, which
    Codex rows leave empty because the Responses API does not report cache writes, and
    ``request_bytes``; an empty or missing value loads as None. ``creation_unknown``
    loads cache writes as None even when the file writes a placeholder 0. Extra fields
    are ignored except ``ts``.
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
                   if f not in OPTIONAL_FIELDS and (f not in r or r[f] in (None, ""))]
        if missing:
            raise ValueError(f"{path} row {n}: missing {', '.join(missing)}")
        try:
            row = {f: int(r[f]) for f in LANE1_FIELDS if f not in OPTIONAL_FIELDS}
            for f in OPTIONAL_FIELDS:
                v = r.get(f)
                row[f] = None if v in (None, "") else int(v)
            if creation_unknown:
                row["cache_creation_input_tokens"] = None
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


def tile_trace(trace: Trace, turns: int) -> Trace:
    """Repeat a measured trace's turns (after the first, whose growth includes the head) to ``turns``."""
    body = trace.turns[1:] or trace.turns
    out = list(trace.turns[:1])
    while len(out) < turns:
        out.append(body[(len(out) - 1) % len(body)])
    return replace(trace, turns=tuple(out[:turns]), source=f"{trace.source}, tiled to {turns} turns")


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


def turn_growth_p99(trace: Trace, page_over: int | None = None, excerpt: int = 400) -> int:
    """99th percentile of per-turn growth (every request's addition and kept output in a turn).

    With ``page_over``, tool results above it count as their ingest-time excerpt.
    """
    def size(j: int, r: Req) -> int:
        added = excerpt if j and page_over and r.added > page_over else r.added
        return added + r.kept

    g = sorted(sum(size(j, r) for j, r in enumerate(t.reqs)) for t in trace.turns[1:] or trace.turns)
    return g[min(len(g) - 1, int(0.99 * len(g)))]


INGEST_PAGE_OVER = 8000  # tool results above this are paged at ingest, before their first send (inferred)


def bounded_variants(policy: CvmPolicy, bound: int, trace: Trace,
                     margin: float = 0.25) -> list[tuple[str, CvmPolicy | None, int]]:
    """(label, policy, headroom) for (iii) under a bound.

    The oracle row sizes headroom from the whole trace's p99 turn growth, which a live proxy
    cannot know, so it is an upper bound on what one can do. The online rows size headroom
    from the largest completed turn so far plus ``margin``, starting from the floor.
    """
    out = []
    h = turn_growth_p99(trace)
    out.append(("oracle p99 headroom (upper bound for a live proxy)", policy_for_bound(policy, bound, trace.head_tokens, h), h))
    floor = policy.headroom_floor_tokens
    op = policy_for_bound(policy, bound, trace.head_tokens, floor)
    online = replace(op, headroom_online=True, headroom_margin=margin) if op else None
    out.append((f"online headroom: running max + {margin:.0%}", online, floor))
    paged = replace(online, page_tool_output_over=INGEST_PAGE_OVER) if online else None
    out.append((f"online headroom plus ingest paging over {_k(INGEST_PAGE_OVER)}", paged, floor))
    out.append(("online, ingest paging, and clear_tool_uses fallback (lossy)",
                replace(paged, bound_fallback="clear") if paged else None, floor))
    return out


def policy_for_bound(policy: CvmPolicy, bound: int, head: int, headroom: int = 0,
                     fallback: str = "none") -> CvmPolicy | None:
    """A (iii) policy sized to a hard working-context bound, or None when head plus headroom leave no room.

    Turn-boundary rebases keep ``headroom`` free for the coming turn. The split of the rest
    (pinned state at most 20%, stub index at most 15% or 8k, a third of the remainder for
    the kept window) is inferred; L2b owns the real policy.
    """
    free = bound - head - headroom
    if free < 6000:
        return None
    pinned_max = min(policy.pinned_max, int(0.2 * free))
    stubs_max = int(min(8000, 0.15 * free))
    window = int((free - pinned_max - stubs_max) / 3)
    return replace(policy, bound_tokens=bound, headroom_tokens=headroom, bound_fallback=fallback,
                   window_tokens=window, stubs_max_tokens=stubs_max, pinned_max=pinned_max,
                   pinned_base=min(policy.pinned_base, pinned_max))


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
    trace = trace_for(profile, focus)
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
        rs = run_all(trace_for(profile, m), prices, profile, m, policy)
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
        ("pinned state rebuilt at once on every change (strips kept thinking at each rebuild)",
         replace(policy, pin_updates="immediate"), None, None),
        ("pinned state at the tail, re-sent every request (invalid with kept thinking: edits a block in front "
         "of the active turn's thinking)", replace(policy, pin_position="tail"), None, None),
        ("tool results over 8k paged at once", replace(policy, page_tool_output_over=8000), None, None),
        ("1 h cache TTL", policy, "1h", None),
        ("keepalive pings through idle gaps up to 1 h", policy, None, Keepalive()),
        ("CLI still compacts its local history at the CLI threshold (brief Q2)", policy, None, "cli"),
    ]
    rows = []
    for label, pol, tier, ka in variants:
        if ka == "cli":
            r = run_cvm(trace, focus, focus.cache_tier(tier), pol, cli_compaction=compaction_for(prices, focus))
        else:
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
    idle_r = run_plain(trace, focus, focus.cache_tier("1h"), compaction_for(prices, focus), idle=IdleCompaction())
    rows.insert(2, ["1 h TTL with Claude Code idleCompaction ((ii) only)", "", _usd(idle_r.total.usd), "", ""])
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
        f"\n\n{focus.display}. Claude Code's `/autocompact` and `--autocompact` accept 100K to 1M; "
        f"`CLAUDE_CODE_AUTO_COMPACT_WINDOW` takes a token count capped at the model's window (vendor-documented). "
        f"Every compaction is a failure under the owner's bar; the table shows cost only. (iii) costs {_usd(cvm_usd)}. "
        + basis)

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
        o_plain = all_rs[oai.key]["plain"].total.usd if oai.key in all_rs else run_plain(trace_for(profile, oai), oai).total.usd
        o_cvm = all_rs[oai.key]["cvm"].total.usd if oai.key in all_rs else run_cvm(trace_for(profile, oai), oai, None, policy).total.usd
        r = run_plain(trace_for(profile, oai), oai, None, compaction_for(prices, oai, threshold=200_000))
        rows.append([f"{oai.display}: server-side compaction, compact_threshold 200k", _usd(r.total.usd), r.resets,
                     _k(r.max_prompt), f"{r.total.usd / o_plain:.2f}", f"{o_cvm / r.total.usd:.2f}"])
    blocks["h5"] = _table(["H5 provider-native setting", "Cost", "Clears or compactions", "Max prompt",
                           "vs (i) plain", "(iii) / this"], rows) + (
        f"\n\n{focus.display} unless named. Clearing replaces each cleared tool result with a 15-token placeholder "
        "(placeholder size inferred) and breaks the cache from the first cleared block. OpenAI's compacted item size "
        "is not documented; the row reuses the (ii) summary assumption. " + basis)

    rows = []
    for n in rep["sweep_turns"]:
        rr = run_all(trace_for(profile, focus, turns=n), prices, profile, focus, policy)
        c = rr["compaction"].total.usd
        rows.append([n] + [_usd(rr[s].total.usd) for s in STRATEGIES] + [f"{rr['cvm'].total.usd / c:.2f}",
                                                                       rr["compaction"].resets])
    blocks["by_length"] = _table(["Turns"] + [LABEL[s] for s in STRATEGIES] + ["(iii) / (ii)", "Compactions"],
                                 rows) + f"\n\n{focus.display}. " + sweep_basis

    rows = []
    for label, scale in rep["sweep_tool_scale"]:
        prof = json.loads(json.dumps(profile))
        prof["tool_output_tokens"]["median"] = profile["tool_output_tokens"]["median"] * scale
        tr = trace_for(prof, focus)
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
        tr = trace_for(prof, focus)
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
        tr = trace_for(prof, focus)
        r = run_cvm(tr, focus, None, replace(policy, recall_tokens=size))
        rows.append([f"{rate:g}", _k(size), r.events["recall"], _usd(r.total.usd), f"{r.total.usd / compact_usd:.2f}"])
    blocks["by_recall"] = _table(["Recalls per turn", "Recall size", "Recalls", "(iii) cost", "(iii) / (ii)"], rows) + (
        f"\n\n{focus.display}. Each recall is one extra upstream round trip that the proxy answers itself. "
        f"(ii) costs {_usd(compact_usd)}. " + basis)

    # Working-context bound (owner principle): a hard constraint first, cost second.
    comp = compaction_for(prices, focus)
    roll = rollover_for(prices, profile, focus)
    rank_blocks = []
    bounded_results = {}
    for bound in rep["bounds"]:
        cands = [("(i) plain", run_plain(trace, focus))]
        cands.append((f"(ii) compaction at the CLI default ({_k(comp.threshold)})", rs["compaction"]))
        # Reset 25k early (the profile's largest tool result) so the reset strategies stay inside the bound.
        at = bound - 25_000
        if at > trace.head_tokens + comp.seed_tokens + 5_000:
            cands.append((f"(ii) compaction configured at {_k(at)}",
                          run_plain(trace, focus, None, replace(comp, threshold=at))))
            cands.append((f"H4 rollover at {_k(at)}", run_plain(trace, focus, None, replace(roll, threshold=at))))
        cands.append(("H5 tool-result clearing, vendor defaults", run_plain(trace, focus, None, clear=ClearPolicy())))
        for vlabel, vp, h in bounded_variants(policy, bound, trace)[:3]:
            if vp:
                r = run_cvm(trace, focus, None, vp)
                if vp.headroom_online and not vp.page_tool_output_over:
                    bounded_results[bound] = (vp, r)
                cands.append((f"(iii) cvm, {vlabel}, window {_k(vp.window_tokens)}", r))
        bp = bool(bounded_results.get(bound))
        rows = []
        for label, r in cands:
            over = r.max_prompt > bound
            lossy = r.resets + r.events["clear_fallback"]
            kind = {"compaction": "compactions", "rollover": "lossy handoffs", "clear": "lossy clears"}.get(
                r.strategy, "lossy clears")
            exceeded = r.events["bound_exceeded"]
            if over and exceeded:
                verdict = f"fails: {exceeded} requests over the bound"
            elif over:
                verdict = "fails: over the bound"
            elif lossy:
                verdict = f"fails: {lossy} {kind}"
            else:
                verdict = "meets the bound with no lossy reset; fidelity depends on recall (L2b)"
            key = (over, lossy > 0, r.max_prompt, r.total.usd)
            rows.append((key, [label, _k(r.max_prompt), "no" if over else "yes", exceeded, lossy,
                               _usd(r.total.usd), verdict]))
        rows.sort(key=lambda x: x[0])
        passing = any(not k[0] and not k[1] for k, _ in rows)
        table = _table(["Rank", "Strategy", "Max working context", "Within bound", "Requests over bound",
                        "Lossy resets", "Cost", "Verdict"],
                       [[i + 1 if passing else "none"] + row for i, (_, row) in enumerate(rows)])
        note = "" if bp else (f" No strategy fits: the {_k(trace.head_tokens)} head alone exceeds or nearly fills the "
                              "bound. Rows are listed for reference, not ranked.")
        rank_blocks.append(f"Bound {_k(bound)}.{note}\n\n{table}")
    blocks["ranking"] = "\n\n".join(rank_blocks) + (
        f"\n\nSingle seed ({profile['seed']}); the seed sweep below gives over-bound counts across seeds. "
        f"Ranked by: within the bound, no lossy reset of any kind, smaller working context, then cost. Any "
        "lossy reset fails the owner's bar, which also requires accuracy equal to the full-context baseline. "
        f"{focus.display}, 5 min TTL. Lossy resets count compactions, rollover handoffs, and tool-result clears. "
        "Quality is not scored here; the bound values are placeholders until the quality-vs-size probe (L2d) "
        "measures the real one. " + basis)

    drop_tr = generate_trace(profile, keep_thinking=False)
    drop_rr = run_all(drop_tr, prices, profile, focus, policy)
    oh = policy.strip_output_overhead

    def rig_route(r: Result) -> float:
        # Thinking dropped on every request: every strategy re-thinks, so every one pays the overhead.
        return r.total.usd + r.total.output * oh * r.model.output / MTOK

    rows = [[LABEL[s], _usd(rs[s].total.usd), _k(rs[s].max_prompt), _usd(drop_rr[s].total.usd),
             _usd(rig_route(drop_rr[s]))] for s in STRATEGIES]
    for label, o in rep["sweep_strip_overhead"]:
        r = run_cvm(trace, focus, None, replace(policy, strip_output_overhead=o))
        rows.append([f"(iii), strip overhead {label}", _usd(r.total.usd), _k(r.max_prompt), "", ""])
    blocks["thinking"] = _table(["Strategy", "Thinking preserved and billed (default, direct connection)",
                                 "Max prompt", "Thinking dropped by the CLI (inferred alternative)",
                                 f"Rig proxy route: dropped every request, +{oh:.1%} re-thinking on all"], rows) + (
        f"\n\n{focus.display}. (i), (ii), and H4 carry every prior thinking block. (iii) keeps thinking until the next "
        f"rebase, which strips all of it ({_k(rs['cvm'].events['thinking_stripped'])} tokens stripped over "
        f"{st['turns']} turns), and charges {policy.strip_output_overhead:.1%} extra output for re-thinking. "
        "The vendor measured +4.6% (low effort) to +67.1% (max) output when thinking is dropped every turn after the "
        "first; the effort level is inferred. On the rig proxy route builder-4 observed replayed thinking dropped on "
        "every request, even on pure appends (`thinking_dropped`, `end_user_binding_mismatch`, cause unknown; "
        "measured, pending review). There the last column is the closer one: billed input falls because no thinking "
        "is carried, stripping at rebases saves nothing extra, and the re-thinking overhead applies to every "
        "strategy, not only (iii). " + basis)

    rows = []
    for label, calls, scale in rep["sweep_bound_stress"]:
        prof = json.loads(json.dumps(profile))
        prof["tool_calls_per_turn"]["mean"] = calls
        prof["tool_output_tokens"]["median"] = profile["tool_output_tokens"]["median"] * scale
        tr = trace_for(prof, focus)
        for bound in (100_000, 250_000):
            small = policy_for_bound(policy, bound, tr.head_tokens, 3000)
            variants = [("headroom 3k fixed (too small for these turns)", small, 3000)] + bounded_variants(policy, bound, tr)
            for vlabel, vp, h in variants:
                if vp is None:
                    rows.append([label, _k(bound), vlabel, _k(h), "no room", "", "", ""])
                    continue
                r = run_cvm(tr, focus, None, vp)
                rows.append([label, _k(bound), vlabel, _k(h), _k(r.max_prompt), r.events["bound_exceeded"],
                             r.events["clear_fallback"], _usd(r.total.usd)])
    blocks["bound_stress"] = _table(["Workload", "Bound", "(iii) variant", "Headroom H", "Max working context",
                                     "Requests over bound", "Lossy clears", "(iii) cost"], rows) + (
        f"\n\n{focus.display}. Rebases happen only at turn boundaries and leave H free, where H is the workload's "
        "p99 turn growth (after ingest paging, where used), read from the whole trace in advance; a real policy must "
        "set H from measured turn sizes. A fixed 3k H shows what an undersized headroom does. \"no room\" means head "
        "plus H leave no space under the bound, so no valid (iii) policy meets it for that workload. Nothing inside an active tool-use turn is edited client-side. "
        "Ingest paging replaces a tool result above the threshold with an excerpt and a recall id before its first send, "
        "which appends new content and edits no earlier message. The clear_tool_uses fallback is server-side, lossy, "
        "and fails the owner's bar. A request still over the bound is counted. " + basis) + "\n\n"
    seeds = range(1, rep["bound_seeds"] + 1)
    seed_stats = {}
    rows = []
    for bound in (100_000, 250_000):
        configs = [(lbl, None, vp_fn) for lbl, vp_fn in [
            ("oracle p99 headroom (upper bound for a live proxy)",
             lambda tr, b=bound: policy_for_bound(policy, b, tr.head_tokens, turn_growth_p99(tr))),
        ] + [(f"online headroom: running max + {m:.0%}",
              lambda tr, b=bound, m=m: (lambda bp: replace(bp, headroom_online=True, headroom_margin=m) if bp else None)(
                  policy_for_bound(policy, b, tr.head_tokens, policy.headroom_floor_tokens)))
             for m in rep["headroom_margins"]] + [
            (f"online + 25%, ingest paging over {_k(INGEST_PAGE_OVER)} (fidelity of paged results unscored)",
             lambda tr, b=bound: (lambda bp: replace(bp, headroom_online=True, headroom_margin=0.25,
                                                     page_tool_output_over=INGEST_PAGE_OVER) if bp else None)(
                 policy_for_bound(policy, b, tr.head_tokens, policy.headroom_floor_tokens)))]]
        for lbl, _, fn in configs:
            overs, costs = [], []
            for sd in seeds:
                tr = trace_for(profile, focus, seed=sd)
                vp = fn(tr)
                r = run_cvm(tr, focus, None, vp)
                overs.append(r.events["bound_exceeded"])
                costs.append(r.total.usd)
            hit = sum(1 for o in overs if o)
            seed_stats[(bound, lbl)] = (hit, sum(overs), max(overs))
            rows.append([_k(bound), lbl, f"{hit} of {len(overs)}", sum(overs), f"{sum(overs) / len(overs):.2f}",
                         max(overs), _usd(sum(costs) / len(costs))])
    blocks["seed_sweep"] = _table(["Bound", "(iii) headroom rule", "Seeds with any request over",
                                   "Requests over, total", "Mean per seed", "Worst seed", "Mean cost"], rows) + (
        f"\n\n{focus.display}, the inferred profile, seeds 1 to {len(seeds)}, {st['turns']} turns each. Online headroom "
        f"starts at {_k(policy.headroom_floor_tokens)} (inferred floor) and becomes the largest completed turn so far "
        "times (1 + margin). The oracle row reads each seed's p99 in advance, which a live proxy cannot do. "
        "Sweep basis: modeled, inferred profile.")

    rows = []
    for h in rep["sweep_heads"]:
        tr = replace(trace, head_tokens=h)
        c = run_plain(tr, focus, None, comp).total.usd
        v = run_cvm(tr, focus, None, policy).total.usd
        rows.append([_k(h), _usd(c), _usd(v), f"{v / c:.2f}", f"{v / rs['compaction'].total.usd:.2f}"])
    blocks["head_sweep"] = _table(["Head", "(ii) at this head", "(iii) at this head", "(iii) / (ii), same head",
                                   f"(iii) / (ii) at {_k(trace.head_tokens)}"], rows) + (
        f"\n\n{focus.display}. Behind a custom `ANTHROPIC_BASE_URL`, Claude Code can load tool definitions upfront, "
        "so (iii) may carry a larger head than a direct (ii) session. " + basis)

    seat = rep["seat_measured"]
    seat_tr = replace(trace, head_tokens=seat["head_tokens"])
    rows = []
    for label, tr, cold in [(f"default throwaway head {_k(trace.head_tokens)} (inferred profile)", trace, 0),
                            (f"OpenRig seat head {_k(seat['head_tokens'])}", seat_tr, 0),
                            (f"OpenRig seat head {_k(seat['head_tokens'])}, first {seat['cold_start_requests']} "
                             "calls full cache writes", seat_tr, seat["cold_start_requests"])]:
        c = run_plain(tr, focus, None, comp, cold_start=cold)
        v = run_cvm(tr, focus, None, policy, cold_start=cold)
        rows.append([label, _usd(c.total.usd), _usd(v.total.usd), f"{v.total.usd / c.total.usd:.2f}",
                     _usd(v.breaks.usd.get("cold_start", 0.0))])
    blocks["seat_head"] = _table(["Head", "(ii) compaction", "(iii) cvm", "(iii) / (ii)",
                                  "(iii) cold-start premium"], rows) + (
        f"\n\n{focus.display}, 300 turns. The 82k seat head and the three uncached first calls are measured on one "
        "OpenRig seat by lane 1 (pending lane 1 review; cause of the misses unknown) and replace only the head and "
        "the first requests' cache state; the rest of the workload is the inferred profile. Lane 1 reports about 46k "
        "for a default throwaway session, close to the 42k inferred head. " + basis)

    rows = []
    for label, rate in rep["sweep_route_miss"]:
        c = run_plain(trace, focus, None, comp, route_miss_rate=rate)
        v = run_cvm(trace, focus, None, policy, route_miss_rate=rate)
        rows.append([label, _usd(c.total.usd), _usd(v.total.usd), f"{v.total.usd / c.total.usd:.2f}",
                     v.breaks.count.get("route_miss", 0)])
    blocks["route_miss"] = _table(["Route-side full-miss rate", "(ii) compaction", "(iii) cvm", "(iii) / (ii)",
                                   "(iii) route misses"], rows) + (
        f"\n\n{focus.display}. A warm, byte-identical prefix still misses with this probability per request. "
        "Lane 1 measured intermittent full misses on the rig proxy route with identical per-block hashes: 3 of 13 "
        "cache-eligible calls on throwaway sessions and 3 of 3 on one lab seat, while cache reads worked on a fresh "
        "lab seat (small sample, pending lane 1 review). The 100% row is a worst case, not a measured steady state. "
        + basis)

    cc = run_cvm(trace, focus, None, policy, cli_compaction=comp)
    cci = run_cvm(trace, focus, focus.cache_tier("1h"), policy, cli_compaction=comp, cli_idle=IdleCompaction())
    hour_tier = focus.cache_tier("1h")
    idle_ii = run_plain(trace, focus, hour_tier, comp, idle=IdleCompaction())
    hour_ii = run_plain(trace, focus, hour_tier, comp)
    calm = json.loads(json.dumps(profile))
    calm["think_time_s"] = rep["sweep_think"][0][1]
    calm_rr = run_all(trace_for(calm, focus), prices, profile, focus, policy)
    hour_all = run_all(trace, prices, profile, focus, policy, tier="1h")
    blocks["assumptions"] = "\n".join([
        f"- **Billing mode.** The headline uses the 5 minute TTL, Claude Code's default with an API key or usage "
        f"credits. On a subscription within plan usage the main conversation gets the 1 hour TTL "
        f"(vendor-documented); there (iii) costs {_usd(hour_all['cvm'].total.usd)} against "
        f"{_usd(hour_all['compaction'].total.usd)} for (ii), a ratio of "
        f"{hour_all['cvm'].total.usd / hour_all['compaction'].total.usd:.2f} instead of "
        f"{rs['cvm'].total.usd / rs['compaction'].total.usd:.2f}. Which mode the owner's sessions use is not known.",
        f"- **The CLI never compacts on its own under (iii).** The proxy bounds the upstream prompt, but the CLI's "
        f"local history still grows past {_k(comp.threshold)}. Whether the CLI then compacts depends on how it "
        f"counts context, which is brief question 2 (lane 1). Lane 1 PR #9 reports that compaction-forcing settings "
        f"did not force a pre-send compaction in a `-p` resume (lane 1's measurement, not repeated here). If the CLI "
        f"does compact at {_k(comp.threshold)} of local history, (iii) costs {_usd(cc.total.usd)} instead of "
        f"{_usd(rs['cvm'].total.usd)} ({cc.events['cli_compaction']} CLI compactions in {st['turns']} turns), and "
        "each one is a lossy reset the proxy cannot undo. On the 1 hour TTL the CLI's idle compaction "
        f"(`idleCompaction`, default on, 200k minimum via `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`) also applies, and the "
        f"CLI's local history passes 200k early: a subscription session under (iii) takes "
        f"{cci.events['cli_idle_compaction']} idle CLI compactions in {st['turns']} turns ({_usd(cci.total.usd)}), "
        "which the proxy cannot prevent. Setting `idleCompaction: false` avoids them.",
        f"- **Same head and caching as a direct connection.** (iii) runs behind a custom `ANTHROPIC_BASE_URL`. "
        f"There Claude Code may load tool definitions upfront instead of through tool search, caching works only if "
        f"the proxy forwards `cache_control` markers unchanged, and the 1 hour TTL needs the `anthropic-beta` header "
        f"forwarded (vendor-documented, https://code.claude.com/docs/en/prompt-caching). The model gives (iii) the "
        f"same {_k(trace.head_tokens)} head and working cache as (ii); the head-size table shows the effect of a "
        f"larger head. Lane 1's rows are captured behind a custom base URL, so measured heads include this effect.",
        f"- **Idle compaction on the 1 hour TTL.** Claude Code can compact an idle conversation shortly before a "
        f"1 hour cache expires (`idleCompaction`, vendor-documented). Modeled for (ii) with the documented 200k "
        f"minimum: {_usd(idle_ii.total.usd)} instead of {_usd(hour_ii.total.usd)} "
        f"({idle_ii.events['idle_compaction']} idle compactions, each a lossy reset). Compaction requests themselves "
        "get the 5 minute TTL (vendor-documented), which changes nothing here because the next request rewrites the "
        "short history anyway.",
        f"- **Thinking.** Every Anthropic row keeps prior thinking in context and bills it as input, the "
        f"vendor-documented default for Opus 5.5, Haiku 5.5, and Fable 5.1, and the headline for a direct "
        f"connection; (iii) strips it at rebases and pays {policy.strip_output_overhead:.1%} extra output. Through the "
        f"rig proxy route, builder-4 observed replayed thinking dropped on every request (cause unknown; measured, "
        f"pending review). There every strategy re-thinks: (i) costs about {_usd(rig_route(drop_rr['plain']))}, (ii) "
        f"{_usd(rig_route(drop_rr['compaction']))}, and (iii) {_usd(rig_route(drop_rr['cvm']))} (thinking table, last "
        "column).",
        f"- **Think time.** The TTL findings depend on the inferred think-time mix, in which "
        f"{st['gap_over'][300]:.0%} of gaps exceed 5 minutes. With every gap under a minute, (iii) / (ii) is "
        f"{calm_rr['cvm'].total.usd / calm_rr['compaction'].total.usd:.2f} instead of "
        f"{rs['cvm'].total.usd / rs['compaction'].total.usd:.2f}.",
    ])

    # Findings: the sentences the doc's summary cites, computed from the runs above.
    ratios = {k: all_rs[k]["cvm"].total.usd / all_rs[k]["compaction"].total.usd for k in keys}
    plain = rs["plain"]
    ttl_share = plain.breaks.usd.get("ttl", 0.0) / plain.total.usd
    hour = run_all(trace, prices, profile, focus, policy, tier="1h")
    c100 = run_plain(trace, focus, None, compaction_for(prices, focus, threshold=100_000))
    cross = None
    for n in range(1, 61):
        rr = run_all(trace_for(profile, focus, turns=n), prices, profile, focus, policy)
        if rr["cvm"].total.usd < rr["compaction"].total.usd:
            cross = n
            break
    ka = run_cvm(trace, focus, None, policy, keepalive=Keepalive())
    lo_k, hi_k = min(ratios, key=ratios.get), max(ratios, key=ratios.get)
    b100 = bounded_results.get(100_000)
    o100 = seed_stats.get((100_000, "online headroom: running max + 25%"), (0, 0, 0))
    miss_c = run_plain(trace, focus, None, comp, route_miss_rate=rep["sweep_route_miss"][1][1])
    miss_v = run_cvm(trace, focus, None, policy, route_miss_rate=rep["sweep_route_miss"][1][1])
    blocks["findings"] = "\n".join([
        f"- Under the owner's bar (no compaction, a hard working-context bound), (iii) with online headroom (running "
        f"max of completed turns + 25%) is the only strategy that stays inside a bound without a lossy reset, and only "
        f"on some workloads: it meets 250k on all {rep['bound_seeds']} seeds and 100k on "
        f"{rep['bound_seeds'] - o100[0]} of {rep['bound_seeds']} (the other {o100[0]} send {o100[1]} requests over in "
        f"all, worst seed {o100[2]}). (i) and (ii) at the CLI default exceed the bound, (ii) configured to reset inside "
        "it compacts, H4 hands off lossily, and tool-result clearing still grows past it. No strategy meets 30k with a "
        f"{_k(trace.head_tokens)} head, and heavier tool use needs more headroom than a 100k bound leaves (bound "
        "stress table)."
        + (f" On seed {profile['seed']} at 100k, (iii) costs {_usd(b100[1].total.usd)}." if b100 else ""),
        f"- Cost, second: over {st['turns']} turns on {focus.display} with API-key billing (5 min TTL), (iii) costs {_usd(rs['cvm'].total.usd)} against "
        f"{_usd(rs['compaction'].total.usd)} for (ii) built-in compaction, {_usd(rs['rollover'].total.usd)} for H4 "
        f"rollover, and {_usd(plain.total.usd)} for (i) plain, which would pass the "
        f"{_k(focus.context_window)} window at turn {plain.over_window_turn}. Across the five models (iii) costs "
        f"{ratios[lo_k]:.2f} to {ratios[hi_k]:.2f} of (ii) ({models[lo_k].display} to {models[hi_k].display}).",
        f"- On the inferred think-time mix ({st['gap_over'][300]:.0%} of gaps over 5 min), cache expiry is the "
        f"largest single cost in (i): {ttl_share:.0%} of its cost is "
        f"the premium (write price minus read price) for re-writing prefixes after idle gaps over the 5 minute TTL. The 1 h TTL cuts (i) to "
        f"{_usd(hour['plain'].total.usd)} and (ii) to {_usd(hour['compaction'].total.usd)}; (iii) gains less "
        f"({_usd(hour['cvm'].total.usd)}) because its prefix is small. Keepalive pings bring (iii) to "
        f"{_usd(ka.total.usd)}.",
        f"- Bounding the prompt is most of the cost saving: compaction configured at 100k costs "
        f"{_usd(c100.total.usd)}, within {c100.total.usd / rs['cvm'].total.usd - 1:.0%} of (iii), but only by "
        f"compacting {c100.resets} times, which the owner's bar rules out.",
        f"- Intermittent route-side cache misses can outweigh every design choice: at the {rep['sweep_route_miss'][1][0]} full-miss "
        f"rate lane 1 saw on the rig proxy route (small sample, pending review), (ii) costs {_usd(miss_c.total.usd)} "
        f"and (iii) {_usd(miss_v.total.usd)}, about {miss_c.total.usd / rs['compaction'].total.usd:.1f}x their "
        "no-miss cost.",
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

    cx_path = HERE / "data" / "lane1_pr10_ae6e91a_codex_requests.csv"
    cx = load_measured(cx_path)  # cache writes reported by the API as 0
    cxt, cx_shrinks = trace_from_measured(cx, "lane1 PR #10 Codex")
    if oai:
        lo = price_measured(cx, oai)
        hi = price_measured(load_measured(cx_path, creation_unknown=True), oai, missing_creation="write")
        reads = sum(r["cache_read_input_tokens"] for r in cx)
        total_in = sum(r["input_tokens"] + r["cache_read_input_tokens"] for r in cx)
        growth = (sum(r.added for t in cxt.turns[1:] for r in t.reqs) / max(1, len(cxt.turns) - 1))
        tiled = tile_trace(cxt, st["turns"])
        tr_rr = run_all(tiled, prices, profile, oai, policy)
        rows = [
            ["Source", "lane 1 PR #10 at ae6e91a (in re-review), `data/lane1_pr10_ae6e91a_codex_requests.csv`"],
            ["Basis", "measured (pending lane 1 review)"],
            ["Requests / user turns / shrinking prompts", f"{len(cx)} / {len(cxt.turns)} / {cx_shrinks}"],
            ["Head: first request prompt", _k(cxt.head_tokens)],
            ["Growth per later turn, mean", _k(growth)],
            ["Cache reads as a share of input", f"{reads / total_in:.0%}"],
            [f"Observed buckets priced on {oai.display}, cache writes as reported (0)", _usd(lo.usd)],
            ["Upper bound if every uncached token were a cache write", _usd(hi.usd)],
            [f"Modeled: measured turns tiled to {st['turns']} turns on {oai.display}: "
             + " / ".join(LABEL[s] for s in STRATEGIES),
             " / ".join(_usd(tr_rr[s].total.usd) for s in STRATEGIES)],
            ["Modeled: max working context, (ii) / (iii)",
             f"{_k(tr_rr['compaction'].max_prompt)} / {_k(tr_rr['cvm'].max_prompt)}"],
        ]
        blocks["lane1_codex"] = _table(["Codex, measured", "Value"], rows) + (
            "\n\n`input_tokens` is uncached input (Codex input minus cached minus cache writes). "
            "`cache_creation_input_tokens` is 0 as reported by the Responses API, which reports cache writes "
            "explicitly (lane 1, pending review); lane 1 notes that an absent field would also read 0, so the "
            "write-priced figure is kept as an upper bound. `request_bytes` is empty. Lane 1 reports a 32.3k head, the context after the first turn; the 26.5k above is "
            "the first request's prompt. Tiling repeats the measured turns after the first; it keeps the measured "
            "head, growth shape, and gaps but is a model, not a measurement.")
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
        try:
            args.per_turn_csv.parent.mkdir(parents=True, exist_ok=True)
            with args.per_turn_csv.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["model", "strategy", "turn", "prompt_tokens", "uncached", "cache_write", "cache_read",
                            "output", "usd", "cum_usd"])
                for k in prices["report"]["models"]:
                    m = prices["parsed"][k]
                    for s, r in run_all(trace_for(profile, m), prices, profile, m).items():
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
