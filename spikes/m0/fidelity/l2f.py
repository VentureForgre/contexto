"""L2f: harder probe types, a calibration pilot, and the run budget.

The L2d probes hit the ceiling (every condition 96/96), so they cannot locate where quality slips.
This module plants harder material and tags every probe with a type and a difficulty tier:

    multihop     the answer joins 2 or 3 facts planted far apart (tier: hops)
    chain        a value revised several times; probe the current or the previous value (tier: depth)
    obligations  items opened, closed, and reopened at different depths; probe one status or the
                 full open set among several (tier: single or set)
    distractor   near-duplicate keys and values planted together (tier: how many duplicates)
    reasoning    a computation over a pasted table whose result the model must keep only in its
                 thinking (it replies OK); probed later (tier: steps)

Scoring is exact match after normalization; set answers compare as sorted id lists. Groups are
disjoint per probe fork, and a contamination scan proves no plant is restated.

Usage, from spikes/m0:
    python3 -m fidelity.l2f plan --out ../../.contexto/spikes/l2f          # pilot plan, no calls
    python3 -m fidelity.l2f budget                                          # run budget table
From the repository root (live pilot, capped):
    python3 spikes/m0/fidelity/l2f.py pilot --settings .claude/settings.local.json --out .contexto/spikes/l2f
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fidelity.l2d import Turn, _Values, turn_tokens, wilson  # noqa: E402
from fidelity.scoring import normalize, parse_answers  # noqa: E402
from fidelity.session import _filler_text  # noqa: E402

DEFAULT_SEED = 20261018
PILOT_CAP = 60_000
TYPES = ("multihop", "chain", "obligations", "distractor", "reasoning")

_SERVICE_ROOTS = (
    "billing", "search", "gateway", "ingest", "notifier", "scheduler", "ledger", "identity", "cache",
    "exporter", "importer", "metrics", "payroll", "catalog", "checkout", "inventory", "mailer", "router",
    "archive", "backup", "reporting", "pricing", "quota", "webhook", "auditor", "planner", "tracker",
    "uploader", "resolver", "relay",
)
# Two variants per root give 60 distinct, non-overlapping service names (no name is a prefix of another).
_SERVICES = tuple(f"{root}-{suffix}" for root in _SERVICE_ROOTS for suffix in ("core", "edge"))
_TEAMS = tuple(f"team-{w}" for w in ("orca", "lynx", "heron", "bison", "otter", "raven", "moose", "egret",
                                      "viper", "koala", "panda", "gecko", "yak", "ibis", "newt", "wren"))
_REGIONS = tuple(f"{a}-{b}{n}" for a in ("north", "south", "east", "west") for b in ("ra", "lo") for n in (1, 2))
_PEOPLE = tuple(f"{p}{n}" for p in ("ash", "bea", "cyd", "dov", "eli", "fay", "gus", "hal", "ivy", "jay")
                for n in (1, 2, 3))


@dataclass(frozen=True)
class Probe:
    id: str
    group: str  # disjoint probe group (one fork per group)
    type: str
    tier: str
    question: str
    answer: str
    kind: str = "value"  # "value" or "set"
    older: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()  # strings that must not be restated outside the plant statements
    statements: tuple[str, ...] = ()  # the plant statements, in order


@dataclass
class Plan:
    seed: int
    turns: list[Turn]
    probes: list[Probe]
    groups: tuple[str, ...]
    reasoning_values: dict[str, str] = field(default_factory=dict)

    def probe_turn(self, group: str) -> Turn:
        mine = [p for p in self.probes if p.group == group]
        lines = [f"{p.id}: {p.question}" for p in mine]
        text = "\n".join([
            "Answer the questions below from this conversation only. Do not use any tools.",
            "Reply with one line per question, in the form",
            "q_id: answer",
            "with the answer only. For a list, give the ids separated by commas. If you do not know, write unknown.",
            "",
            *lines,
        ])
        return Turn(f"P_{group}", "probe", text)


def build(seed: int = DEFAULT_SEED, groups: Sequence[str] = ("a", "b"), filler_lines: Sequence[int] = (0, 60, 60)) -> Plan:
    """Plant one disjoint probe group per entry in ``groups`` across three build turns.

    Each group gets 8 probes per type, split evenly between an easier and a harder tier. Statements
    for every group are spread across the three turns, so a group's plants sit at matched depths.
    """
    rng = random.Random(seed)
    values = _Values(rng)
    services = list(_SERVICES)
    rng.shuffle(services)
    people = list(_PEOPLE)
    rng.shuffle(people)
    teams = list(_TEAMS)
    rng.shuffle(teams)
    stage: list[list[str]] = [[], [], []]  # statements per build turn
    probes: list[Probe] = []
    reasoning_values: dict[str, str] = {}
    tables: list[str] = []
    n = 0

    def pid() -> str:
        nonlocal n
        n += 1
        return f"q_{n:03d}"

    for g in groups:
        # Multi-hop: service -> owner -> team -> region. Links are planted in different turns.
        for i in range(8):
            hops = 2 if i < 4 else 3
            svc, owner, team = services.pop(), people.pop(), teams.pop() if hops == 3 else None
            region = rng.choice(_REGIONS)
            s1 = f"Ownership note: the {svc} service is owned by {owner}."
            if hops == 2:
                s2 = f"Rota note: {owner} is on call in region {region}."
                stage[0].append(s1)
                stage[2].append(s2)
                q = f"In which region is the owner of the {svc} service on call? Answer with the region only."
                subj = (svc, owner)
                stmts = (s1, s2)
            else:
                s2 = f"Staffing note: {owner} belongs to {team}."
                s3 = f"Coverage note: {team} covers region {region}."
                stage[0].append(s1)
                stage[1].append(s2)
                stage[2].append(s3)
                q = f"Which region does the team of the owner of the {svc} service cover? Answer with the region only."
                subj = (svc, owner, team)
                stmts = (s1, s2, s3)
            probes.append(Probe(pid(), g, "multihop", f"{hops}-hop", q, region, subjects=subj, statements=stmts))
        # Supersession chains: depth 3 or 6; probe the current value, or the one before it.
        for i in range(8):
            depth = 3 if i < 4 else 6
            svc = services.pop()
            subject = f"{svc} rollout cohort"
            chain = [values() for _ in range(depth)]
            stmts = [f"Cohort update: the {subject} is set to {chain[0]}."]
            stmts += [f"Cohort update: the {subject} is now {v}." for v in chain[1:]]
            for k, st in enumerate(stmts):
                stage[min(2, k * 3 // depth)].append(st)
            previous = i % 2 == 1
            if previous:
                q = f"What was the {subject} immediately before its current value? Answer with the value only."
                answer, older = chain[-2], tuple(v for v in chain if v != chain[-2])
            else:
                q = f"What is the current {subject}? Answer with the value only."
                answer, older = chain[-1], tuple(chain[:-1])
            tier = f"depth{depth}-{'previous' if previous else 'current'}"
            probes.append(Probe(pid(), g, "chain", tier, q, answer, older=older, subjects=(subject,), statements=tuple(stmts)))
        # Obligations: open, close, reopen at different depths.
        ids = [f"AI-{rng.randrange(1000, 10000)}" for _ in range(16)]
        while len(set(ids)) < len(ids):
            ids = [f"AI-{rng.randrange(1000, 10000)}" for _ in range(16)]
        states: dict[str, str] = {}
        stmts_by: dict[str, list[str]] = {}
        for k, oid in enumerate(ids):
            pattern = k % 4  # 0 open; 1 opened then closed; 2 closed then reopened; 3 closed late
            events = {0: ["open"], 1: ["open", "close"], 2: ["open", "close", "reopen"], 3: ["open", "close"]}[pattern]
            where = {0: [0], 1: [0, 1], 2: [0, 1, 2], 3: [0, 2]}[pattern]
            texts = {
                "open": f"Action item {oid}: {rng.choice(('rotate', 'audit', 'migrate', 'document'))} the "
                        f"{rng.choice(('key', 'job', 'config', 'alert'))} backlog.",
                "close": f"Action item {oid} is done.",
                "reopen": f"Action item {oid} is reopened; it is not done after all.",
            }
            stmts_by[oid] = [texts[e] for e in events]
            for e, w in zip(events, where, strict=True):
                stage[w].append(texts[e])
            states[oid] = "open" if events[-1] in ("open", "reopen") else "closed"
        for k in range(4):  # one of each pattern: open, closed, reopened, closed late
            oid = ids[k]
            probes.append(Probe(pid(), g, "obligations", "single", f"Is action item {oid} open or closed? Answer open or closed.",
                                states[oid], older=("closed",) if states[oid] == "open" else ("open",),
                                subjects=(oid,), statements=tuple(stmts_by[oid])))
        for k in range(4):  # sets of 3 drawn from ids the single probes never name
            subset = sorted(ids[4 + 3 * k : 7 + 3 * k])
            open_ids = sorted(o for o in subset if states[o] == "open")
            probes.append(Probe(pid(), g, "obligations", "set",
                                f"Of action items {', '.join(subset)}, which are still open? List their ids.",
                                ", ".join(open_ids) or "none", kind="set", subjects=(),
                                statements=tuple(s for o in subset for s in stmts_by[o])))
        # Near-duplicate distractors: one true key among 2 or 5 look-alikes in the same log.
        for i in range(8):
            dups = 2 if i < 4 else 5
            svc = services.pop()
            stem = rng.choice(("amber", "basalt", "cobalt", "delta", "ember"))
            base = rng.randrange(1000, 9000)
            shards = rng.sample(range(10, 99), dups + 1)
            lines = []
            for s, shard in enumerate(shards):
                digits = str(base + s * 9)
                lines.append(f"cfg {svc}.shard_{shard}.map_version={stem}-{digits}")
            target = rng.randrange(len(lines))
            key = lines[target].split("=")[0][4:]
            answer = lines[target].split("=")[1]
            for line in lines:
                stage[1 if i % 2 else 2].append(line)
            others = tuple(l.split("=")[1] for j, l in enumerate(lines) if j != target)
            probes.append(Probe(pid(), g, "distractor", f"{dups}-dups",
                                f"What value did {key} have in the logs earlier in this conversation? Answer with the value only.",
                                answer, older=others, subjects=(key,), statements=tuple(lines)))
        # Reasoning kept only in thinking: compute over a pasted table, reply OK, probe later.
        for i in range(2):
            steps = 1 if i == 0 else 2
            rows = [(f"r{j:02d}", rng.randrange(10, 99), rng.choice(("ok", "fail"))) for j in range(12)]
            label = f"checksum {g.upper()}{i + 1}"
            fails = sum(v for _, v, s in rows if s == "fail")
            value = str(fails if steps == 1 else (fails * 7) % 1000)
            rule = ("the sum of value over rows with status fail" if steps == 1
                    else "the sum of value over rows with status fail, multiplied by 7, modulo 1000")
            table = "\n".join(["row | value | status", *[f"{r} | {v} | {s}" for r, v, s in rows]])
            text = (f"Compute {label} now: {rule}, using this table. Keep the result in mind for later; do not write "
                    f"it down.\n{table}")
            tables.append(text)
            reasoning_values[label] = value
            probes.append(Probe(pid(), g, "reasoning", f"{steps}-step",
                                f"What is {label}, which you computed earlier? Answer with the number only.", value,
                                subjects=(label,), statements=(text,)))

    turns: list[Turn] = []
    for t in range(3):
        rng.shuffle(stage[t])
        prose = [x for x in stage[t] if not x.startswith("cfg ")]
        cfg = [x for x in stage[t] if x.startswith("cfg ")]
        parts = list(prose)
        if t == 1:
            parts += tables  # the reasoning tables arrive mid-session
        log = _filler_text(rng, filler_lines[t], f"p{t}").split("\n") if filler_lines[t] else []
        for line in cfg:  # config lines live inside the log, as the questions say
            log.insert(rng.randrange(0, len(log) + 1), line)
        if log:
            parts.append("Here is a log excerpt for reference:")
            parts += log
        parts.append("Reply with exactly: OK")
        turns.append(Turn(f"B{t + 1}", "build", "\n".join(parts)))
    return Plan(seed, turns, probes, tuple(groups), reasoning_values)


def contamination(plan: Plan) -> list[str]:
    """Plant subjects and answers may appear only in their own statements and their own question."""
    texts = [(t.id, t.text) for t in plan.turns] + [(f"P_{g}", plan.probe_turn(g).text) for g in plan.groups]
    violations = []
    for p in plan.probes:
        own_probe = f"P_{p.group}"
        for where, text in texts:
            residue = text
            for st in p.statements:
                residue = residue.replace(st, "")
            if where == own_probe:
                residue = residue.replace(p.question, "")
            for token in p.subjects:
                if re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w-])", residue):
                    violations.append(f"{p.id}: {token!r} in {where}")
            if (p.kind == "value" and p.type != "obligations" and where.startswith("P_")
                    and re.search(rf"(?<![\w-]){re.escape(p.answer)}(?![\w-])", text)):
                violations.append(f"{p.id}: answer appears in probe turn {where}")
    return violations


# Scoring


def _norm_set(text: str | None) -> str:
    items = sorted({normalize(x) for x in re.split(r"[,\s]+", text or "") if normalize(x)})
    return ", ".join(items) if items else "none"


def score(probes: Sequence[Probe], answers_text: str) -> dict[str, Any]:
    answers = parse_answers(answers_text)
    cells: dict[str, dict[str, int]] = {}
    total = correct = 0
    for p in probes:
        given = answers.get(p.id)
        ok = (_norm_set(given) == _norm_set(p.answer)) if p.kind == "set" else (normalize(given) == normalize(p.answer))
        stale = (not ok) and p.kind == "value" and normalize(given) in {normalize(v) for v in p.older}
        for key in (p.type, f"{p.type}/{p.tier}"):
            c = cells.setdefault(key, {"correct": 0, "total": 0, "stale": 0})
            c["total"] += 1
            c["correct"] += int(ok)
            c["stale"] += int(stale)
        total += 1
        correct += int(ok)
    return {"correct": correct, "total": total, "interval": wilson(correct, total), "cells": dict(sorted(cells.items()))}


# Budget


def run_budget(head: int = 1_500, checkpoints: Sequence[int] = (41_000, 92_000, 243_000), probes_per_checkpoint: int = 120,
               strip: bool = True, gate_factor: float = 1.5) -> dict[str, Any]:
    """Direct-API replay budget: one growth request per checkpoint, then probe forks.

    Each checkpoint costs one build request at its context plus one fork per arm (preserved, and
    stripped if ``strip``). Outputs: 200 per build request, about 20 tokens per probe answer plus
    1,500 of thinking per fork [inferred]. Returns per-request rows, the total, and the worst
    pre-turn gate value (cumulative + factor x the next turn's cost).
    """
    rows = []
    arms = ("preserved", "stripped") if strip else ("preserved",)
    probe_text = probes_per_checkpoint * 30
    for k, ctx in enumerate(checkpoints, start=1):
        rows.append({"turn": f"G{k}", "context": ctx, "output": 200, "note": "growth (build)"})
        for arm in arms:
            rows.append({"turn": f"P{k}", "context": ctx + probe_text, "output": probes_per_checkpoint * 20 + 1_500,
                         "note": f"{arm} fork"})
    turns: dict[str, int] = {}
    for r in rows:
        turns[r["turn"]] = turns.get(r["turn"], 0) + r["context"] + r["output"]
    worst, cumulative = 0, 0
    for t, cost in turns.items():
        worst = max(worst, cumulative + int(gate_factor * cost))
        cumulative += cost
    return {"rows": rows, "total": cumulative, "worst_gate": worst, "head": head}


# Pilot (live, capped)


def pilot(settings: Path, out: Path, cap: int = PILOT_CAP) -> dict[str, Any]:
    """Build the three turns, then fork once per probe group. Thinking is reported, never enforced:
    the binding header is sent with the mismatch field unset, so every response lists any dropped
    or mismatched thinking in input_transformations."""
    from fidelity import replay  # local import: the pilot reuses the replay runner's client

    plan = build()
    post = replay.http_post(settings)
    binding = {"anthropic-beta": "thinking-binding-controls-2026-08-01"}

    def post_with_header(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return post(body, binding)

    # Estimates: each request's context is the turns so far, plus the probe text for forks.
    estimates: dict[str, int] = {}
    running = replay_head = 500
    for t in plan.turns:
        running += sum(turn_tokens(t)) + 100
        estimates[t.id] = running
    for g in plan.groups:
        estimates[f"P_{g}"] = running + sum(turn_tokens(plan.probe_turn(g)))
    del replay_head
    runner = replay.Runner(post_with_header, cap, estimates, margin=1.2)
    history: list[dict[str, Any]] = []
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = out / f"pilot-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    status = "complete"
    try:
        for turn in plan.turns:
            history.append(replay.user(turn.text))
            payload = runner.call(turn.id, turn.id, list(history))
            history.append({"role": "assistant", "content": payload["content"]})
        for g in plan.groups:
            pt = plan.probe_turn(g)
            payload = runner.call(pt.id, pt.id, [*history, replay.user(pt.text)])
            text = replay.text_of(payload)
            (run_dir / f"answers_{g}.txt").write_text(text + "\n", encoding="utf-8")
            results[g] = score([p for p in plan.probes if p.group == g], text)
    except (replay.CapExceeded, replay.RequestFailed) as exc:
        status = f"stopped: {exc}"
    summary = {"status": status, "total_tokens": runner.spent, "records": runner.records, "scores": results,
               "plan": {"seed": plan.seed, "groups": list(plan.groups), "probes": len(plan.probes)}}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def write_plan(plan: Plan, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "turns.jsonl").open("w", encoding="utf-8") as fh:
        for t in [*plan.turns, *(plan.probe_turn(g) for g in plan.groups)]:
            fh.write(json.dumps(asdict(t)) + "\n")
    with (out / "truth.jsonl").open("w", encoding="utf-8") as fh:
        for p in plan.probes:
            fh.write(json.dumps({k: v for k, v in asdict(p).items() if k not in ("statements", "subjects")}) + "\n")
    (out / "contamination.json").write_text(json.dumps({"violations": contamination(plan)}, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L2f harder probes: plan, budget, pilot.")
    sub = parser.add_subparsers(dest="command", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--out", type=Path, required=True)
    sub.add_parser("budget")
    pi = sub.add_parser("pilot")
    pi.add_argument("--settings", type=Path, required=True)
    pi.add_argument("--out", type=Path, required=True)
    pi.add_argument("--cap", type=int, default=PILOT_CAP)
    args = parser.parse_args(argv)
    if args.command == "plan":
        plan = build()
        write_plan(plan, args.out)
        est = [sum(turn_tokens(t)) for t in plan.turns]
        print(json.dumps({"probes": len(plan.probes), "turn_tokens_est": est, "contamination": len(contamination(plan))}))
        return 0
    if args.command == "budget":
        for strip in (False, True):
            b = run_budget(strip=strip)
            print(f"strip={strip} total={b['total']:,} worst_gate={b['worst_gate']:,}")
        return 0
    summary = pilot(args.settings, args.out, args.cap)
    print(json.dumps({k: summary[k] for k in ("status", "total_tokens", "scores")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
