"""L2f run scale: the recommended harder mix, laid out at matched relative depths, plus pilot 2 and the run.

Each checkpoint k has its own disjoint probe group. A group's plant events (statements, or a block
such as a distractor's look-alike lines or a reasoning table) are merged in random order, with each
plant's own statements kept in order, and placed evenly across [0, its checkpoint). Every group thus
spans its whole context, so depth scales with size. Filler log text fills the rest. The session is
cut into one build turn per checkpoint, followed by a probe fork that never enters the history.

Usage, from the repository root:
    python3 spikes/m0/fidelity/harder.py plan  --out .contexto/spikes/l2f/run-plan
    python3 spikes/m0/fidelity/harder.py pilot --settings .claude/settings.local.json --out .contexto/spikes/l2f
    python3 spikes/m0/fidelity/harder.py run   --settings .claude/settings.local.json --out .contexto/spikes/l2f
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fidelity.l2d import _WORDS, wilson  # noqa: E402
from fidelity.l2f import Probe, score  # noqa: E402
from fidelity.session import _filler_text  # noqa: E402

DEFAULT_SEED = 20261019
CHECKPOINTS = (41_000, 92_000, 243_000)
PILOT_SIZE = 15_000
RUN_CAP = 852_610
PILOT_CAP = 45_000
UNDERCOUNT = 1.07  # measured contexts ran 5.7% to 8.5% above estimates in L2d; aim estimates this much low
PROSE = 0.25
NUMERIC = 0.55

_ROOTS = (
    "billing", "search", "gateway", "ingest", "notifier", "scheduler", "ledger", "identity", "cache",
    "exporter", "importer", "metrics", "payroll", "catalog", "checkout", "inventory", "mailer", "router",
    "archive", "backup", "reporting", "pricing", "quota", "webhook", "auditor", "planner", "tracker",
    "uploader", "resolver", "relay",
)
_SUFFIXES = ("core", "edge", "mesh", "sync", "hub", "api", "ops", "ctl", "svc", "lab")
_PEOPLE = tuple(f"{p}{n}" for p in ("ash", "bea", "cyd", "dov", "eli", "fay", "gus", "hal", "ivy", "jay")
                for n in range(1, 10))
_TEAMS = tuple(f"team-{a}-{b}" for a in ("orca", "lynx", "heron", "bison", "otter", "raven", "moose", "egret",
                                          "viper", "koala", "panda", "gecko", "yak", "ibis", "newt", "wren")
               for b in ("red", "blue", "gold", "jade", "grey"))
_SITES = tuple(f"site-{w}{n}" for w in ("alder", "birch", "cedar", "elm", "fir", "hazel") for n in range(1, 10))
_REGIONS = tuple(f"{a}-{b}{n}" for a in ("north", "south", "east", "west") for b in ("ra", "lo") for n in (1, 2))


@dataclass(frozen=True)
class Mix:
    """Probes per group, by type and tier."""

    multihop: tuple[tuple[int, int], ...] = ((3, 12), (4, 12))  # (hops, count)
    chain: tuple[tuple[int, int], ...] = ((6, 20), (8, 20))  # (depth, count); half current, half previous
    obligation_single: int = 12
    obligation_sets: int = 12
    set_size: int = 6
    distractor: tuple[tuple[int, int], ...] = ((8, 24),)  # (look-alikes besides the true line, count)
    reasoning: int = 8

    def scaled(self, factor: float) -> Mix:
        def s(pairs: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
            return tuple((a, max(2, round(n * factor))) for a, n in pairs)
        return Mix(s(self.multihop), s(self.chain), max(2, round(self.obligation_single * factor)),
                   max(2, round(self.obligation_sets * factor)), self.set_size, s(self.distractor),
                   max(2, round(self.reasoning * factor)))


@dataclass
class Event:
    text: str
    plant: str  # probe id this event belongs to ("" for shared obligation events)
    order: int  # position within its plant


@dataclass
class Plan:
    seed: int
    checkpoints: tuple[int, ...]
    turns: list[str]  # build turn texts, one per checkpoint
    probe_turns: list[str]
    probes: list[Probe]
    estimates: list[int]  # estimated context at each probe fork


def tokens(text: str) -> int:
    numeric = sum(len(line) for line in text.split("\n") if "latency_ms=" in line or line.startswith(("cfg ", "r")) and "|" in line)
    return math.ceil((len(text) - numeric) * PROSE + numeric * NUMERIC)


class _Pools:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.services = [f"{r}-{s}" for r in _ROOTS for s in _SUFFIXES]
        self.people = list(_PEOPLE)
        self.teams = list(_TEAMS)
        self.sites = list(_SITES)
        for pool in (self.services, self.people, self.teams, self.sites):
            rng.shuffle(pool)
        self.used_values: set[str] = set()
        self.used_ids: set[str] = set()

    def value(self) -> str:
        while True:
            v = f"{self.rng.choice(_WORDS)}-{self.rng.randrange(1000, 10000)}"
            if v not in self.used_values:
                self.used_values.add(v)
                return v

    def near_values(self, n: int) -> list[str]:
        """n values with one stem and consecutive numbers: they differ only in the last digit or two."""
        while True:
            stem, base = self.rng.choice(_WORDS), self.rng.randrange(1000, 9990 - n)
            vals = [f"{stem}-{base + i}" for i in range(n)]
            if not self.used_values.intersection(vals):
                self.used_values.update(vals)
                return vals

    def ob_id(self) -> str:
        while True:
            oid = f"AI-{self.rng.randrange(10000, 100000)}"
            if oid not in self.used_ids:
                self.used_ids.add(oid)
                return oid


def _group(pools: _Pools, mix: Mix, group: str, start: int) -> tuple[list[Probe], list[list[Event]]]:
    """Probes and their ordered event lists for one group."""
    rng = pools.rng
    probes: list[Probe] = []
    plants: list[list[Event]] = []
    n = start

    def pid() -> str:
        nonlocal n
        n += 1
        return f"q_{group}_{n:03d}"

    for hops, count in mix.multihop:
        for _ in range(count):
            i = pid()
            svc, owner = pools.services.pop(), pools.people.pop()
            chain = [f"Ownership note: the {svc} service is owned by {owner}."]
            subjects = [svc, owner]
            region = rng.choice(_REGIONS)
            if hops == 2:
                chain.append(f"Rota note: {owner} is on call in region {region}.")
                q = f"In which region is the owner of the {svc} service on call? Answer with the region only."
            else:
                team = pools.teams.pop()
                chain.append(f"Staffing note: {owner} belongs to {team}.")
                subjects.append(team)
                if hops == 3:
                    chain.append(f"Coverage note: {team} covers region {region}.")
                else:
                    site = pools.sites.pop()
                    subjects.append(site)
                    chain.append(f"Facilities note: {team} works from {site}.")
                    chain.append(f"Coverage note: {site} sits in region {region}.")
                q = (f"Follow the notes from the {svc} service to its owner, the owner's team"
                     f"{', that team’s site,' if hops == 4 else ''} and the region it covers. Which region is it? "
                     "Answer with the region only.").replace("’", "'")
            probes.append(Probe(i, group, "multihop", f"{hops}-hop", q, region, subjects=tuple(subjects), statements=tuple(chain)))
            plants.append([Event(t, i, k) for k, t in enumerate(chain)])
    for depth, count in mix.chain:
        for c in range(count):
            i = pid()
            subject = f"{pools.services.pop()} rollout cohort"
            vals = [pools.value() for _ in range(depth)]
            st = [f"Cohort update: the {subject} is set to {vals[0]}."] + [f"Cohort update: the {subject} is now {v}." for v in vals[1:]]
            previous = c % 2 == 1
            if previous:
                q = f"What was the {subject} immediately before its current value? Answer with the value only."
                answer, older = vals[-2], tuple(v for v in vals if v != vals[-2])
            else:
                q = f"What is the current {subject}? Answer with the value only."
                answer, older = vals[-1], tuple(vals[:-1])
            probes.append(Probe(i, group, "chain", f"depth{depth}-{'previous' if previous else 'current'}", q, answer,
                                older=older, subjects=(subject,), statements=tuple(st)))
            plants.append([Event(t, i, k) for k, t in enumerate(st)])
    # Obligations: two close/reopen cycles for some; singles and sets use disjoint ids.
    patterns = (("open",), ("open", "close"), ("open", "close", "reopen"), ("open", "close", "reopen", "close"),
                ("open", "close", "reopen", "close", "reopen"))

    def obligation() -> tuple[str, str, list[str]]:
        oid = pools.ob_id()
        events = rng.choice(patterns)
        task = f"{rng.choice(('rotate', 'audit', 'migrate', 'document'))} the {rng.choice(('key', 'job', 'config', 'alert'))} backlog"
        wording = {
            ("open", 0): f"Action item {oid}: {task}.",
            ("close", 0): f"Action item {oid} is done.",
            ("reopen", 0): f"Action item {oid} is reopened; it is not done after all.",
            ("close", 1): f"Action item {oid} is done again, after the rework.",
            ("reopen", 1): f"Action item {oid} is reopened a second time.",
        }
        seen: dict[str, int] = {}
        texts = []
        for e in events:
            texts.append(wording[(e, seen.get(e, 0))])
            seen[e] = seen.get(e, 0) + 1
        return oid, "open" if events[-1] in ("open", "reopen") else "closed", texts

    for _ in range(mix.obligation_single):
        i = pid()
        oid, state, st = obligation()
        probes.append(Probe(i, group, "obligations", "single", f"Is action item {oid} open or closed? Answer open or closed.",
                            state, older=("closed",) if state == "open" else ("open",), subjects=(oid,), statements=tuple(st)))
        plants.append([Event(t, i, k) for k, t in enumerate(st)])
    for _ in range(mix.obligation_sets):
        i = pid()
        members = [obligation() for _ in range(mix.set_size)]
        open_ids = sorted(o for o, s, _ in members if s == "open")
        names = sorted(o for o, _, _ in members)
        all_st = tuple(t for _, _, st in members for t in st)
        probes.append(Probe(i, group, "obligations", f"set{mix.set_size}",
                            f"Of action items {', '.join(names)}, which are still open? List their ids.",
                            ", ".join(open_ids) or "none", kind="set", statements=all_st))
        for _, _, st in members:
            plants.append([Event(t, i, k) for k, t in enumerate(st)])
    for dups, count in mix.distractor:
        for _ in range(count):
            i = pid()
            svc = pools.services.pop()
            shards = sorted(rng.sample(range(10, 99), dups + 1))
            vals = pools.near_values(dups + 1)
            rng.shuffle(vals)
            lines = [f"cfg {svc}.shard_{s}.map_version={v}" for s, v in zip(shards, vals, strict=True)]
            t = rng.randrange(len(lines))
            key, answer = lines[t].split("=")[0][4:], lines[t].split("=")[1]
            probes.append(Probe(i, group, "distractor", f"{dups}-dups",
                                f"What value did {key} have in the logs earlier in this conversation? Answer with the value only.",
                                answer, older=tuple(v for k, v in enumerate(vals) if k != t), subjects=(key,),
                                statements=("\n".join(lines),)))
            plants.append([Event("\n".join(lines), i, 0)])
    for r in range(mix.reasoning):
        i = pid()
        steps = 1 if r % 2 == 0 else 2
        rows = [(f"r{j:02d}", rng.randrange(10, 99), rng.choice(("ok", "fail"))) for j in range(12)]
        label = f"checksum {group.upper()}{r + 1}"
        fails = sum(v for _, v, s in rows if s == "fail")
        value = str(fails if steps == 1 else (fails * 7) % 1000)
        rule = ("the sum of value over rows with status fail" if steps == 1
                else "the sum of value over rows with status fail, multiplied by 7, modulo 1000")
        text = (f"Compute {label} now: {rule}, using this table. Keep the result in mind for later; do not write it down.\n"
                + "\n".join(["row | value | status", *[f"{a} | {b} | {c}" for a, b, c in rows]]))
        probes.append(Probe(i, group, "reasoning", f"{steps}-step",
                            f"What is {label}, which you computed earlier? Answer with the number only.", value,
                            subjects=(label,), statements=(text,)))
        plants.append([Event(text, i, 0)])
    return probes, plants


def _merge(rng: random.Random, plants: list[list[Event]]) -> list[Event]:
    """Random merge that keeps each plant's events in order."""
    queues = [list(p) for p in plants]
    out: list[Event] = []
    while any(queues):
        live = [q for q in queues if q]
        weights = [len(q) for q in live]
        q = rng.choices(live, weights=weights)[0]
        out.append(q.pop(0))
    return out


def build(seed: int = DEFAULT_SEED, checkpoints: Sequence[int] = CHECKPOINTS, mix: Mix = Mix()) -> Plan:
    rng = random.Random(seed)
    pools = _Pools(rng)
    head = 600  # system prompt and the per-turn instructions [inferred]
    targets = [int(c / UNDERCOUNT) - head for c in checkpoints]
    placed: list[tuple[float, str]] = []  # (estimated token position, text)
    probes: list[Probe] = []
    probe_turns: list[str] = []
    for k, target in enumerate(targets, start=1):
        group = chr(ord("a") + k - 1)
        group_probes, plants = _group(pools, mix, group, 0)
        probes += group_probes
        probe_turns.append(_probe_text(group_probes))
        events = _merge(rng, plants)
        span = target - tokens(probe_turns[-1]) - 400
        for j, ev in enumerate(events):
            placed.append(((j + rng.random()) / len(events) * span * 0.97, ev.text))
    placed.sort(key=lambda x: x[0])
    turns: list[str] = []
    estimates: list[int] = []
    context = head
    cursor = 0
    for k, target in enumerate(targets):
        parts: list[str] = []
        while cursor < len(placed) and placed[cursor][0] < target - context - 200 + (context - head):
            parts.append(placed[cursor][1])
            cursor += 1
        used = tokens("\n".join(parts))
        need = max(0, target - context - used - tokens(probe_turns[k]) - 200)
        sample = _filler_text(random.Random(0), 50, "kxcy")
        per_line = tokens(sample) / 50  # measured on generated filler, not assumed
        total_lines = int(need / per_line)
        chunks = max(1, len(parts))
        body: list[str] = []
        for t, part in enumerate(parts or [""]):
            if part:
                body.append(part)
            lines = total_lines // chunks + (1 if t < total_lines % chunks else 0)
            if lines:
                body.append(_filler_text(rng, lines, f"k{k}c{t}"))
        text = "\n".join(["Here is the next part of the working log. Keep it in mind.", *body, "Reply with exactly: OK"])
        turns.append(text)
        context += tokens(text) + 60
        estimates.append(context + tokens(probe_turns[k]))
    return Plan(seed, tuple(checkpoints), turns, probe_turns, probes, estimates)


def _probe_text(group: Sequence[Probe]) -> str:
    return "\n".join([
        "Answer the questions below from this conversation only. Do not use any tools.",
        "Reply with one line per question, in the form",
        "q_id: answer",
        "with the answer only. For a list, give the ids separated by commas. If you do not know, write unknown.",
        "",
        *[f"{p.id}: {p.question}" for p in group],
    ])


def contamination(plan: Plan) -> list[str]:
    texts = [(f"B{k + 1}", t) for k, t in enumerate(plan.turns)] + [(f"P{k + 1}", t) for k, t in enumerate(plan.probe_turns)]
    out = []
    for p in plan.probes:
        own = f"P{ord(p.group) - ord('a') + 1}"
        for where, text in texts:
            residue = text
            for st in p.statements:
                residue = residue.replace(st, "")
            if where == own:
                residue = residue.replace(p.question, "")
            for token in p.subjects:
                if re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w-])", residue):
                    out.append(f"{p.id}: {token!r} in {where}")
    return out


def _check_turn_counts(plan: Plan) -> list[str]:
    """Every statement must appear exactly once across the build turns, in order."""
    joined = "\n".join(plan.turns)
    problems = []
    for p in plan.probes:
        last = -1
        for st in p.statements:
            if p.kind == "set":
                continue
            at = joined.find(st)
            if at < 0 or joined.count(st) != 1:
                problems.append(f"{p.id}: statement missing or repeated")
            elif at < last:
                problems.append(f"{p.id}: statements out of order")
            last = max(last, at)
    return problems


# Live drivers


def _drive(plan: Plan, settings: Path, out: Path, cap: int, label: str, margin: float) -> dict[str, Any]:
    from fidelity import replay

    post = replay.http_post(settings)
    binding = {"anthropic-beta": "thinking-binding-controls-2026-08-01"}  # observe drops; field unset

    def post_with_header(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return post(body, binding)

    estimates = {}
    for k in range(len(plan.turns)):
        estimates[f"B{k + 1}"] = plan.estimates[k] - 200
        estimates[f"P{k + 1}"] = plan.estimates[k]
    runner = replay.Runner(post_with_header, cap, estimates, margin=margin)
    run_dir = out / f"{label}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = []
    scores: dict[str, Any] = {}
    status = "complete"
    try:
        for k, text in enumerate(plan.turns):
            history.append(replay.user(text))
            payload = runner.call(f"B{k + 1}", f"B{k + 1}", list(history))
            history.append({"role": "assistant", "content": payload["content"]})
            payload = runner.call(f"P{k + 1}", f"P{k + 1}", [*history, replay.user(plan.probe_turns[k])])
            answer = replay.text_of(payload)
            (run_dir / f"answers_{k + 1}.txt").write_text(answer + "\n", encoding="utf-8")
            group = [p for p in plan.probes if p.group == chr(ord("a") + k)]
            scores[f"P{k + 1}"] = score(group, answer)
    except (replay.CapExceeded, replay.RequestFailed) as exc:
        status = f"stopped: {exc}"
    contexts = {r["label"]: sum(v for kk, v in r["usage"].items() if kk.endswith("input_tokens")) for r in runner.records}
    summary = {"status": status, "total_tokens": runner.spent, "records": runner.records, "scores": scores,
               "measured_input": contexts, "planned": plan.estimates, "seed": plan.seed}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary["run_dir"] = str(run_dir)
    return summary


def pilot_plan() -> Plan:
    return build(seed=DEFAULT_SEED + 1, checkpoints=(PILOT_SIZE,), mix=Mix().scaled(0.5))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L2f run scale: plan, pilot 2, run.")
    sub = parser.add_subparsers(dest="command", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--out", type=Path, required=True)
    for name in ("pilot", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--settings", type=Path, required=True)
        sp.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        for plan, name in ((pilot_plan(), "pilot"), (build(), "run")):
            d = args.out / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "truth.jsonl").write_text("".join(json.dumps(asdict(p)) + "\n" for p in plan.probes), encoding="utf-8")
            report = {"probes": len(plan.probes), "estimates": plan.estimates,
                      "contamination": len(contamination(plan)), "order_problems": len(_check_turn_counts(plan))}
            (d / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(name, json.dumps(report))
        return 0
    if args.command == "pilot":
        summary = _drive(pilot_plan(), args.settings, args.out, PILOT_CAP, "pilot2", margin=1.15)
    else:
        summary = _drive(build(), args.settings, args.out, RUN_CAP, "run", margin=1.1)
    brief = {k: summary[k] for k in ("status", "total_tokens", "measured_input", "planned", "run_dir")}
    brief["scores"] = {k: {"correct": v["correct"], "total": v["total"], "interval": v["interval"]} for k, v in summary["scores"].items()}
    print(json.dumps(brief, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
