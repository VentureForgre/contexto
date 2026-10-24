"""L2d: quality versus working-context size. Probe design, budget sheet, and scorer.

One growing session on an interactive CLI seat, with probe checkpoints at several context sizes
(default 30k, 100k, 250k). Each checkpoint probes its own disjoint group of planted items, so an
answer written at one checkpoint cannot hand a later checkpoint its answers. This module makes no
model calls: lane 1 drives the subject turn by turn and meters every request.

Usage, from spikes/m0:
    python3 -m fidelity.l2d generate --out ../../.contexto/spikes/l2d [--head N] [--seed N]
    python3 -m fidelity.l2d budget [--head N]
    python3 -m fidelity.l2d score --answers-dir DIR --truth ../../.contexto/spikes/l2d/truth.jsonl

Outputs of ``generate`` (gitignored, never committed):
    turns.jsonl         one object per turn: id, kind, text (exactly what is sent), files
    files/              the files to pre-create in the subject's scratch dir, at the paths in turns.jsonl
    probes.jsonl        probe id, checkpoint, category, segment, question; no answers
    truth.jsonl         expected answers, superseded values, positions; for the scorer only
    budget.md           per-request budget sheet
    contamination.json  result of the restatement scan (must be an empty list of violations)
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fidelity.scoring import normalize, parse_answers
from fidelity.session import _filler_text

DEFAULT_SEED = 20261017
DEFAULT_HEAD = 81_808  # measured by lane 1 on lab.subject's first call, 2026-10-15
CHECKPOINTS = (30_000, 250_000)  # P1 lands right after the opening; P2 as large as the gate allows
REPLAY_CHECKPOINT = 40_000  # the replay arm's own extra checkpoint
TOKEN_CAP = 2_000_000

CATEGORIES = ("flagged", "unflagged", "superseded", "obligation")
PER_CATEGORY = 24  # per checkpoint
DEEP_SHARE = 16  # of PER_CATEGORY, planted in the shared opening; the rest in the growth segment

# Measured in E1/E2 (docs/research/M0-fidelity-test.md): numeric log text costs about 2.2x the
# chars/4 estimate, about 0.55 tokens per character. Prose stays near chars/4.
LOG_TOKENS_PER_CHAR = 0.55
PROSE_TOKENS_PER_CHAR = 0.25
READ_PREFIX_CHARS = 8  # inferred: the Read tool prefixes each line with its number
FILE_TOKENS = 18_000  # per file; inferred to sit under the Read tool's per-read limit
FILE_LINE_CHARS = 56  # average synthetic log line length
OPENING_STATEMENTS_PER_TURN = 120  # few, large opening turns: each turn is a full-context request
REPLY_TOKENS = 10
ANSWER_LINE_TOKENS = 14
FILES_DIR = "l2d/files"

_SYSTEMS = (
    "billing", "search", "gateway", "ingest", "notifier", "scheduler", "ledger", "identity",
    "cache", "exporter", "importer", "metrics", "payroll", "catalog", "checkout", "inventory",
    "mailer", "router", "archive", "backup", "reporting", "pricing", "quota", "webhook",
)
_ATTRIBUTES = (
    "service port", "deploy region", "release codename", "audit bucket", "flag prefix", "rate tier",
    "timeout class", "build tag", "owner alias", "storage class", "queue name", "canary cohort",
)
_WORDS = (
    "amber", "basalt", "cobalt", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper",
    "kelvin", "lumen", "marble", "nickel", "onyx", "pewter", "quartz", "russet", "sable", "topaz",
)
_OBLIGATION_TASKS = (
    "rotate the {s} credentials", "write the {s} rollback runbook", "pin the {s} base image",
    "add a canary for the {s}", "backfill the {s} audit trail", "remove the {s} legacy job",
    "document the {s} retry budget", "upgrade the {s} TLS bundle",
)


@dataclass(frozen=True)
class Plant:
    key: str
    checkpoint: int  # 1-based
    category: str
    segment: str  # "deep" (shared opening) or "growth" (the segment just before its checkpoint)
    subject: str  # the phrase the question names; never restated outside its statements
    value: str  # expected answer
    older: tuple[str, ...]  # superseded values (stale answers)
    question: str
    statements: tuple[str, ...]  # every text that states this plant, in order


@dataclass
class Turn:
    id: str
    kind: str  # "rules", "opening", "growth", "probe"
    text: str
    files: list[dict[str, Any]] = field(default_factory=list)  # {"path", "content", "chars", "lines"}
    checkpoint: int | None = None


@dataclass(frozen=True)
class Plan:
    seed: int
    head: int
    turns: tuple[Turn, ...]
    plants: tuple[Plant, ...]
    markers: tuple[str, ...]  # per-checkpoint line marker required by the standing rules
    replay_turns: tuple[Turn, ...] = ()  # direct-API replay (strip-thinking arm); never sent to the seat
    replay_marker: str = ""


def answers_file(checkpoint: int) -> str:
    return f"l2d/answers_{checkpoint}.txt"


def probe_id(plant: Plant, index: int) -> str:
    return f"q_{plant.checkpoint}_{index:03d}"


class _Values:
    """Unique, unguessable values: no value is ever produced twice in one plan."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.used: set[str] = set()

    def __call__(self) -> str:
        while True:
            value = f"{self.rng.choice(_WORDS)}-{self.rng.randrange(1000, 10000)}"
            if value not in self.used:
                self.used.add(value)
                return value


def _tokens_prose(text: str) -> int:
    return math.ceil(len(text) * PROSE_TOKENS_PER_CHAR)


def _tokens_log(chars: int, lines: int = 0) -> int:
    return math.ceil((chars + lines * READ_PREFIX_CHARS) * LOG_TOKENS_PER_CHAR)


def turn_tokens(turn: Turn) -> tuple[int, int]:
    """Return (tokens of the user text, tokens of the file reads) this turn adds."""
    log_chars = sum(len(line) for line in turn.text.split("\n") if "latency_ms=" in line or line.startswith("cfg "))
    text = _tokens_prose(turn.text) - math.ceil(log_chars * PROSE_TOKENS_PER_CHAR) + _tokens_log(log_chars)
    files = sum(_tokens_log(f["chars"], f["lines"]) for f in turn.files)
    return text, files


def build(
    seed: int = DEFAULT_SEED,
    head: int = DEFAULT_HEAD,
    checkpoints: Sequence[int] = CHECKPOINTS,
    replay_checkpoint: int | None = REPLAY_CHECKPOINT,
) -> Plan:
    """Build the seat plan and, unless ``replay_checkpoint`` is None, the replay arm's turns.

    Group k (1-based) belongs to seat checkpoint k. Group 0 belongs only to the replay's own
    checkpoint (about ``replay_checkpoint`` tokens) and never appears in a seat turn.
    """
    rng = random.Random(seed)
    values = _Values(rng)
    subjects = [f"{s} {a}" for s in _SYSTEMS for a in _ATTRIBUTES]
    rng.shuffle(subjects)
    cfg_keys = [f"{s}.shard_{n:02d}.map_version" for s in _SYSTEMS for n in range(12)]
    rng.shuffle(cfg_keys)
    groups = list(range(1, len(checkpoints) + 1)) + ([0] if replay_checkpoint else [])
    ob_ids = rng.sample(range(100, 1000), len(groups) * PER_CATEGORY)
    all_markers = [f";{c}{i + 1}" for i, c in enumerate(rng.sample("kmqvxzr", len(groups)))]
    markers = tuple(all_markers[: len(checkpoints)])
    replay_marker = all_markers[-1] if replay_checkpoint else ""

    plants: list[Plant] = []
    deep_first: list[tuple[int, str, str]] = []  # (checkpoint, category, statement), for the opening
    deep_second: list[tuple[int, str, str]] = []  # later statements (supersessions, closures)
    growth_text: dict[int, list[str]] = {k: [] for k in groups}
    growth_cfg: dict[int, list[str]] = {k: [] for k in groups}

    for k in groups:
        for category in CATEGORIES:
            for n in range(PER_CATEGORY):
                deep = n < DEEP_SHARE
                segment = "deep" if deep else "growth"
                key = f"{category}:{k}:{n:02d}"
                if category == "flagged":
                    subject, value = subjects.pop(), values()
                    statement = f"Please remember: the {subject} is {value}."
                    if deep:
                        deep_first.append((k, category, statement))
                    else:
                        growth_text[k].append(statement)
                    plants.append(Plant(key, k, category, segment, subject, value, (),
                                        f"What is the {subject}? Answer with the value only.", (statement,)))
                elif category == "unflagged":
                    subject, value = cfg_keys.pop(), values()
                    statement = f"cfg {subject}={value}"
                    if deep:
                        deep_first.append((k, category, statement))
                    else:
                        growth_cfg[k].append(statement)
                    plants.append(Plant(key, k, category, segment, subject, value, (),
                                        f"What value did {subject} have in the logs earlier in this conversation? "
                                        "Answer with the value only.", (statement,)))
                elif category == "superseded":
                    subject, old, new = subjects.pop(), values(), values()
                    first = f"Decision: the {subject} is {old}."
                    second = f"Change of plan: the {subject} is now {new}."
                    deep_first.append((k, category, first))
                    if deep:
                        deep_second.append((k, category, second))
                    else:
                        growth_text[k].append(second)
                    plants.append(Plant(key, k, category, segment, subject, new, (old,),
                                        f"What is the current {subject}? Answer with the value only.", (first, second)))
                else:
                    oid = f"AI-{ob_ids.pop()}"
                    task = rng.choice(_OBLIGATION_TASKS).format(s=rng.choice(_SYSTEMS))
                    opened = f"Action item {oid}: {task}. Keep it open until it is done."
                    closed = f"Action item {oid} is done."
                    # Deep: 8 open, 8 opened and closed in the opening. Growth: 4 opened in the
                    # growth segment (open), 4 opened in the opening and closed in the growth segment.
                    if deep:
                        deep_first.append((k, category, opened))
                        if n % 2:
                            deep_second.append((k, category, closed))
                        statements = (opened, closed) if n % 2 else (opened,)
                        value = "closed" if n % 2 else "open"
                        older = ("open",) if n % 2 else ()
                    elif n % 2:
                        deep_first.append((k, category, opened))
                        growth_text[k].append(closed)
                        statements, value, older = (opened, closed), "closed", ("open",)
                    else:
                        growth_text[k].append(opened)
                        statements, value, older = (opened,), "open", ()
                    plants.append(Plant(key, k, category, segment, oid, value, older,
                                        f"Is action item {oid} open or closed? Answer open or closed.", statements))

    turns: list[Turn] = []
    rules = ["Standing rules for this whole session. Keep them; they will not be repeated."]
    for k, marker in enumerate(markers, start=1):
        rules.append(f"- Whenever you write a file named answers_{k}.txt, end every line of it with {marker}")
    rules.append("Reply with exactly: OK")
    turns.append(Turn("S0", "rules", "\n".join(rules)))

    # Interleave checkpoints and categories so every group's deep plants sit at matched positions.
    # Group 0 (replay only) gets its own opening in the replay.
    replay_opening = _interleave([d for d in deep_first if d[0] == 0]) + _interleave([d for d in deep_second if d[0] == 0])
    deep_first = [d for d in deep_first if d[0] != 0]
    deep_second = [d for d in deep_second if d[0] != 0]
    opening = _interleave(deep_first) + _interleave(deep_second)
    for t, start in enumerate(range(0, len(opening), OPENING_STATEMENTS_PER_TURN), start=1):
        chunk = opening[start : start + OPENING_STATEMENTS_PER_TURN]
        prose = [s for s in chunk if not s.startswith("cfg ")]
        cfg = [s for s in chunk if s.startswith("cfg ")]
        parts = [*prose]
        if cfg:
            log = _filler_text(rng, len(cfg) + 6, f"o{t}").split("\n")  # minimal filler: the opening must stay small
            for line in cfg:
                log.insert(rng.randrange(1, len(log)), line)
            parts += ["Here is a short log for reference:", *log]
        parts.append("Reply with exactly: OK")
        turns.append(Turn(f"O{t}", "opening", "\n".join(parts)))

    context = head + sum(sum(turn_tokens(t)) + REPLY_TOKENS for t in turns)
    for k, target in enumerate(checkpoints, start=1):
        group = [p for p in plants if p.checkpoint == k]
        if not group:
            continue
        probe = _probe_turn(k, group)
        text_lines = growth_text[k] + [""]
        probe_tokens = sum(turn_tokens(probe))
        need = max(target - context - _tokens_prose("\n".join(text_lines)) - 200 - probe_tokens, 2_000)
        n_files = max(1, math.ceil(need / FILE_TOKENS))
        per_file_tokens = need / n_files
        lines_per_file = max(40, int(per_file_tokens / LOG_TOKENS_PER_CHAR / (FILE_LINE_CHARS + READ_PREFIX_CHARS)))
        files = []
        cfg_lines = list(growth_cfg[k])
        for f in range(n_files):
            body = _filler_text(rng, lines_per_file, f"g{k}f{f}").split("\n")
            mine = cfg_lines[f::n_files]
            for line in mine:
                body.insert(rng.randrange(1, len(body)), line)
            content = "\n".join(body) + "\n"
            files.append({"path": f"{FILES_DIR}/g{k}_{f:02d}.log", "content": content, "chars": len(content),
                          "lines": content.count("\n")})
        paths = ", ".join(f["path"] for f in files)
        text = "\n".join([
            *growth_text[k],
            f"Use the Read tool to read these {len(files)} files in full, all in parallel in one message: {paths}.",
            "Do not summarize them. After reading, reply with exactly: DONE",
        ])
        growth = Turn(f"G{k}", "growth", text, files, k)
        turns += [growth, probe]
        context += sum(turn_tokens(growth)) + REPLY_TOKENS + sum(turn_tokens(probe)) + len(group) * ANSWER_LINE_TOKENS
    plan = Plan(seed, head, tuple(turns), tuple(plants), markers)
    if not replay_checkpoint:
        return plan
    replay = _replay_turns(rng, plan, replay_opening, growth_text[0], growth_cfg[0], replay_marker, replay_checkpoint)
    return Plan(seed, head, tuple(turns), tuple(plants), markers, replay, replay_marker)


def _inline_files(files: Sequence[Mapping[str, Any]]) -> list[str]:
    out = []
    for f in files:
        out += [f"=== file {f['path']} ===", f["content"].rstrip("\n"), "=== end of file ==="]
    return out


def _filler_files(rng: random.Random, tokens: int, label: str, cfg_lines: Sequence[str] = ()) -> list[dict[str, Any]]:
    n_files = max(1, math.ceil(tokens / FILE_TOKENS))
    lines_per_file = max(20, int(tokens / n_files / LOG_TOKENS_PER_CHAR / (FILE_LINE_CHARS - 4)))
    files = []
    for f in range(n_files):
        body = _filler_text(rng, lines_per_file, f"{label}f{f}").split("\n")
        for line in list(cfg_lines)[f::n_files]:
            body.insert(rng.randrange(1, len(body)), line)
        content = "\n".join(body) + "\n"
        files.append({"path": f"replay/{label}_{f:02d}.log", "content": content, "chars": len(content), "lines": 0})
    return files


def _strip_reply_lines(text: str) -> list[str]:
    drop = ("Reply with exactly:", "Use the Read tool", "Do not summarize them.")
    return [line for line in text.split("\n") if not line.startswith(drop)]


def _replay_turns(
    rng: random.Random,
    plan: Plan,
    opening: Sequence[str],
    growth_text: Sequence[str],
    growth_cfg: Sequence[str],
    marker: str,
    target: int,
) -> tuple[Turn, ...]:
    """The strip-thinking arm's replay, one request per turn, files inline.

    RS and RG plant group 0 and grow to about ``target``; RP0 probes group 0 there. RF then adds
    plant-free filler so that the seat's own rules, opening, and G1 (merged into RF, verbatim
    except for the reply and Read instructions) end where they end on the seat: the seat's head is
    about ``plan.head`` tokens of system prompt and tools, which the replay stands in for with
    filler. RP1 asks the seat's P1 questions at about the seat's P1 context.
    """
    group0 = [p for p in plan.plants if p.checkpoint == 0]
    prose = [s for s in opening if not s.startswith("cfg ")]
    cfg = [s for s in opening if s.startswith("cfg ")]
    log = _filler_text(rng, len(cfg) + 6, "ro").split("\n")
    for line in cfg:
        log.insert(rng.randrange(1, len(log)), line)
    rs = Turn("RS", "replay", "\n".join([
        f"Standing rule for this whole session: whenever you answer checkpoint R, end every line of the answer with {marker}",
        *prose, "Here is a short log for reference:", *log, "Reply with exactly: OK",
    ]))
    probe0 = _replay_probe("RP0", "R", group0, "answer with one line per question")
    context = REPLAY_HEAD + sum(turn_tokens(rs)) + REPLY_TOKENS
    head_text = "\n".join([*growth_text, "Here are the files for reference:"])
    need = max(target - context - sum(turn_tokens(Turn("head", "replay", head_text))) - sum(turn_tokens(probe0)) - 200, 2_000)
    files = _filler_files(rng, need, "rg", growth_cfg)
    rg = Turn("RG", "replay", "\n".join([head_text, *_inline_files(files), "Reply with exactly: DONE"]))
    context += sum(turn_tokens(rg)) + REPLY_TOKENS

    seat = {t.id: t for t in plan.turns}
    p1 = seat["P1"]
    merged = []
    for t in plan.turns:
        if t.id == "P1":
            break
        merged += _strip_reply_lines(t.text)
        merged += _inline_files(t.files)
    seat_text = "\n".join(merged)
    group1 = [p for p in plan.plants if p.checkpoint == 1]
    probe1 = _replay_probe("RP1", "1", group1, f"reply with exactly the lines you would write to {answers_file(1)}")
    seat_p1 = next(r.context for r in requests(plan) if r.turn == "P1")
    seat_tokens = sum(turn_tokens(Turn("seat", "replay", seat_text)))
    filler_need = max(seat_p1 - context - seat_tokens - sum(turn_tokens(probe1)) - 300, 2_000)
    filler = _filler_files(rng, filler_need, "rf")
    rf = Turn("RF", "replay", "\n".join([
        "Here are some unrelated files for reference:", *_inline_files(filler), seat_text, "Reply with exactly: OK",
    ]))
    del p1
    return (rs, rg, probe0, rf, probe1)


def _replay_probe(turn_id: str, label: str, group: Sequence[Plant], how: str) -> Turn:
    lines = [f"{probe_id(p, i)}: {p.question}" for i, p in enumerate(group)]
    text = "\n".join([
        f"Checkpoint {label}. Answer the questions below from what was said earlier in this conversation only.",
        f"Do not use any tools: {how}, in the form",
        "q_id: answer",
        "with the value only. Follow any standing rule for these answers given earlier in this session.",
        "If you do not know an answer, write unknown as the answer.",
        "",
        *lines,
    ])
    return Turn(turn_id, "replay_probe", text, [], 0 if label == "R" else 1)


def _interleave(items: list[tuple[int, str, str]]) -> list[str]:
    """Round-robin over (checkpoint, category) buckets so positions are matched across groups."""
    buckets: dict[tuple[int, str], list[str]] = {}
    for k, category, statement in items:
        buckets.setdefault((k, category), []).append(statement)
    order = sorted(buckets)
    out: list[str] = []
    while any(buckets.values()):
        for key in order:
            if buckets[key]:
                out.append(buckets[key].pop(0))
    return out


def _probe_turn(k: int, group: Sequence[Plant]) -> Turn:
    lines = [f"{probe_id(p, i)}: {p.question}" for i, p in enumerate(group)]
    text = "\n".join([
        f"Checkpoint {k}. Answer the questions below from what was said earlier in this conversation only.",
        "Do not read, search, or list any files and do not run commands. Make exactly one tool call:",
        f"write the answers to {answers_file(k)}, one line per question, in the form",
        "q_id: answer",
        "with the value only. Follow any standing rule for that file given earlier in this session.",
        "If you do not know an answer, write unknown as the answer.",
        "After writing the file, reply with exactly: WRITTEN",
        "",
        *lines,
    ])
    return Turn(f"P{k}", "probe", text, [], k)


def probe_index(plan: Plan) -> list[tuple[str, Plant]]:
    out = []
    for k in sorted({p.checkpoint for p in plan.plants}):
        group = [p for p in plan.plants if p.checkpoint == k]
        out += [(probe_id(p, i), p) for i, p in enumerate(group)]
    return out


# Contamination and position checks


def contamination(plan: Plan) -> list[str]:
    """Every place a plant's subject or value appears outside its own statements and question.

    Subjects may appear only in the plant's statements and in its own probe question. Values may
    appear only in the plant's statements, never in any question or file outside its own line.
    An empty list means no plant is restated anywhere before (or after) its probe.
    """
    texts: list[tuple[str, str]] = []
    for turn in (*plan.turns, *plan.replay_turns):
        texts.append((turn.id, turn.text))
        texts += [(f"{turn.id}:{f['path']}", f["content"]) for f in turn.files]
    violations = []
    # Group 0 belongs to the replay only; the replay's own opening and growth carry no seat plant.
    seat_text = "\n".join(t.text + "".join(f["content"] for f in t.files) for t in plan.turns)
    replay_own = "\n".join(t.text for t in plan.replay_turns if t.id in ("RS", "RG"))
    for plant in plan.plants:
        for stmt in plant.statements:
            if plant.checkpoint == 0 and stmt in seat_text:
                violations.append(f"{plant.key}: replay-only plant appears in a seat turn")
            if plant.checkpoint != 0 and stmt in replay_own:
                violations.append(f"{plant.key}: seat plant appears in the replay's own opening or growth")
    all_subjects = [p.subject for p in plan.plants]
    for i, a in enumerate(all_subjects):
        for b in all_subjects[i + 1 :]:
            if a in b or b in a:
                violations.append(f"subject overlap: {a!r} / {b!r}")
    for plant in plan.plants:
        allowed = list(plant.statements) + [plant.question]
        for where, text in texts:
            residue = text
            for stmt in allowed:
                residue = residue.replace(stmt, "")
            # Obligation answers are the words open/closed; only the item id identifies them.
            tokens = {plant.subject} if plant.category == "obligation" else {plant.subject, plant.value, *plant.older}
            for token in tokens:
                pattern = rf"(?<![\w.-]){re.escape(token)}(?![\w-])"
                if re.search(pattern, residue):
                    violations.append(f"{plant.key}: {token!r} restated in {where}")
    return violations


def plant_offsets(plan: Plan, replay: bool = False) -> dict[str, int]:
    """Estimated token offset (from the start of the conversation) of each plant's current value.

    With ``replay``, offsets are on the replay's turns; replay probe turns are forks and take no room.
    """
    offsets: dict[str, int] = {}
    position = 0
    for turn in plan.replay_turns if replay else plan.turns:
        if turn.kind == "replay_probe":
            offsets[f"__probe__{turn.id}"] = position
            continue
        text_tokens, _ = turn_tokens(turn)
        for plant in plan.plants:
            last = plant.statements[-1]
            at = turn.text.find(last)
            if at >= 0:
                offsets[plant.key] = position + math.ceil(text_tokens * at / max(len(turn.text), 1))
        position += text_tokens
        for f in turn.files:
            for plant in plan.plants:
                at = f["content"].find(plant.statements[-1])
                if at >= 0:
                    offsets[plant.key] = position + _tokens_log(at)
            position += _tokens_log(f["chars"], f["lines"])
        position += REPLY_TOKENS
    return offsets


# Budget


@dataclass
class Request:
    turn: str
    context: int
    output: int
    note: str = ""


def requests(plan: Plan, parallel: bool = True) -> list[Request]:
    """Model every request: its full input context and output. Each tool call is its own request.

    rules and opening turns: one request. Growth turns: the user message, then one request after
    the reads (parallel) or one per file read (sequential). Probe turns: the user message, which
    produces the Write call, then the request after the Write result.
    """
    out: list[Request] = []
    context = plan.head
    for turn in plan.turns:
        text, _ = turn_tokens(turn)
        context += text
        if turn.kind in ("rules", "opening"):
            out.append(Request(turn.id, context, REPLY_TOKENS))
            context += REPLY_TOKENS
        elif turn.kind == "growth":
            out.append(Request(turn.id, context, 60 * len(turn.files), "issues the Read calls"))
            if parallel:
                context += sum(_tokens_log(f["chars"], f["lines"]) for f in turn.files)
                out.append(Request(turn.id, context, REPLY_TOKENS, "after all reads"))
            else:
                for f in turn.files:
                    context += _tokens_log(f["chars"], f["lines"])
                    out.append(Request(turn.id, context, 60, f"after reading {Path(f['path']).name}"))
            context += REPLY_TOKENS
        else:
            group = sum(1 for p in plan.plants if p.checkpoint == turn.checkpoint)
            answers = group * ANSWER_LINE_TOKENS
            out.append(Request(turn.id, context, answers, f"checkpoint {turn.checkpoint} at {context:,} tokens"))
            context += answers
            out.append(Request(turn.id, context, REPLY_TOKENS, "after the Write"))
            context += REPLY_TOKENS
    return out


def budget_sheet(plan: Plan) -> str:
    par = requests(plan, parallel=True)
    seq = requests(plan, parallel=False)
    total_par = sum(r.context + r.output for r in par)
    total_seq = sum(r.context + r.output for r in seq)
    lines = [
        "# L2d budget sheet",
        "",
        f"Seed {plan.seed}. Fixed head (system prompt, tools, onboarding) assumed {plan.head:,} tokens; "
        "rerun `python3 -m fidelity.l2d generate --head N` with the measured value.",
        "",
        "Size estimate: log and file text at 0.55 tokens per character (measured: chars/4 undercounts numeric "
        "text about 2.2x in E1/E2), plus about 8 characters per line for the Read tool's line numbers "
        "(inferred); prose at chars/4. The meter counts every category, cache reads included, so each "
        "request costs its full context. Side calls by the CLI are not modeled; the margin covers them.",
        "",
        "Compaction sanity bound: the largest context is at most about 250k, far below the measured block point of "
        "about 977k for a 1M window (lane 1), so no compaction is expected.",
        "",
        "| # | turn | context | output | cumulative | note |",
        "|---|---|---|---|---|---|",
    ]
    cumulative = 0
    for i, r in enumerate(par, start=1):
        cumulative += r.context + r.output
        lines.append(f"| {i} | {r.turn} | {r.context:,} | {r.output:,} | {cumulative:,} | {r.note} |")
    checkpoints = [r for r in par if r.note.startswith("checkpoint")]
    lines += [
        "",
        "Checkpoints (context of the probe request): " + ", ".join(f"P{i + 1} {r.context:,}" for i, r in enumerate(checkpoints)),
        "",
        f"Planned total, parallel reads: {total_par:,} tokens ({total_par / TOKEN_CAP:.0%} of {TOKEN_CAP:,}).",
        f"If the subject reads files one at a time: {total_seq:,} tokens ({total_seq / TOKEN_CAP:.0%}).",
        "",
    ]
    worst, where = gate(plan)
    lines += [
        f"Worst pre-turn gate value (cumulative before the turn + 1.5 x the turn's cost): {worst:,} before {where} "
        f"(limit 1,900,000).",
        "",
    ]
    if plan.replay_turns:
        rep = replay_requests(plan)
        lines += [
            "## Strip-thinking arm (direct-API replay; separate approval, not part of the lab run)",
            "",
            "| # | turn | context | output | note |",
            "|---|---|---|---|---|",
        ]
        for i, r in enumerate(rep, start=1):
            lines.append(f"| {i} | {r.turn} | {r.context:,} | {r.output:,} | {r.note} |")
        full = sum(r.context + r.output for r in rep)
        no_strip_p1 = sum(r.context + r.output for r in replay_requests(plan, stripped_p1=False))
        lines += [
            "",
            f"Replay total with the stripped arm at P1: {full:,} tokens; without it: {no_strip_p1:,}.",
            "",
        ]
    return "\n".join(lines)


# Strip-thinking arm (design only; a direct-API replay, never run on the CLI seat)

REPLAY_HEAD = 1_500  # inferred: a short system prompt, no CLI tools
REPLY_WITH_THINKING = 200  # inferred: "OK" plus a short preserved thinking block per turn
PROBE_THINKING = 1_500  # inferred


def replay_requests(plan: Plan, stripped_p1: bool = True) -> list[Request]:
    """Model the direct-API replay: one request per turn, files inline, probes as forks.

    RS, RG, and RF enter the shared history. RP0 forks into a preserved and a stripped request.
    RP1 forks into a preserved request and, if ``stripped_p1``, a stripped one. Probe forks never
    enter the shared history, so no answer can reach a later probe.
    """
    out: list[Request] = []
    context = REPLAY_HEAD
    for turn in plan.replay_turns:
        text, files = turn_tokens(turn)
        if turn.kind == "replay_probe":
            answers = sum(1 for p in plan.plants if p.checkpoint == turn.checkpoint) * ANSWER_LINE_TOKENS
            arms = ("preserved", "stripped") if (turn.id == "RP0" or stripped_p1) else ("preserved",)
            for arm in arms:
                out.append(Request(turn.id, context + text, answers + PROBE_THINKING, f"{arm} arm"))
            continue
        context += text + files
        out.append(Request(turn.id, context, REPLY_WITH_THINKING))
        context += REPLY_WITH_THINKING
    return out


def gate(plan: Plan, factor: float = 1.5) -> tuple[int, str]:
    """builder-1's pre-turn gate: cumulative before a turn plus factor x that turn's cost.

    Returns the worst value over the run and the turn where it occurs.
    """
    by_turn: dict[str, int] = {}
    order: list[str] = []
    for r in requests(plan):
        if r.turn not in by_turn:
            order.append(r.turn)
            by_turn[r.turn] = 0
        by_turn[r.turn] += r.context + r.output
    worst, where, cumulative = 0, "", 0
    for turn in order:
        value = cumulative + int(factor * by_turn[turn])
        if value > worst:
            worst, where = value, turn
        cumulative += by_turn[turn]
    return worst, where


# Scoring


ANSWER_MARKER = re.compile(r"\s*(;[a-z]\d)\s*$")


def wilson(correct: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 1.0)
    p = correct / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def depth_bucket(distance: int) -> str:
    for limit, name in ((10_000, "<10k"), (50_000, "10k-50k"), (150_000, "50k-150k")):
        if distance < limit:
            return name
    return ">=150k"


def score_checkpoint(truth: Sequence[Mapping[str, Any]], answers_text: str, marker: str) -> dict[str, Any]:
    """Score one checkpoint's answers file against its truth rows."""
    raw = parse_answers(answers_text)
    lines = [ln for ln in answers_text.splitlines() if ln.strip().lstrip("-* ").startswith("q_")]
    compliant = sum(1 for ln in lines if ln.rstrip().endswith(marker))
    answers = {k: ANSWER_MARKER.sub("", v) for k, v in raw.items()}

    def cell() -> dict[str, int]:
        return {"correct": 0, "total": 0, "stale": 0, "unknown": 0}

    tables: dict[str, dict[str, dict[str, int]]] = {"category": {}, "segment": {}, "depth": {}}
    wrong = []
    for row in truth:
        given = normalize(answers.get(row["id"]))
        keys = {"category": row["category"], "segment": row["segment"], "depth": depth_bucket(row["distance"])}
        cells = [tables[t].setdefault(keys[t], cell()) for t in tables]
        for c in cells:
            c["total"] += 1
        if given and given == normalize(row["answer"]):
            for c in cells:
                c["correct"] += 1
            continue
        wrong.append(row["id"])
        kind = "stale" if given in {normalize(v) for v in row.get("older", ())} else (
            "unknown" if not given or given == "unknown" else None
        )
        if kind:
            for c in cells:
                c[kind] += 1
    correct = sum(c["correct"] for c in tables["category"].values())
    total = len(truth)
    return {
        "correct": correct,
        "total": total,
        "interval": wilson(correct, total),
        "instruction": {"compliant": compliant, "lines": len(lines), "interval": wilson(compliant, len(lines))},
        "tables": {t: dict(sorted(v.items())) for t, v in tables.items()},
        "wrong": wrong,
        "missing": [row["id"] for row in truth if row["id"] not in raw],
    }


REPLAY_ANSWER_FILES = ("replay_RP0_preserved.txt", "replay_RP0_stripped.txt",
                       "replay_RP1_preserved.txt", "replay_RP1_stripped.txt")


def score_dir(answers_dir: Path, truth_rows: Sequence[Mapping[str, Any]], meta: Mapping[str, Any]) -> dict[str, Any]:
    """Score seat answers files (answers_k.txt) and, when present, the replay arm's answer files.

    Replay files hold the model's reply text for each fork: RP0 is scored against group 0 with the
    replay marker, RP1 against group 1 with the seat's first marker.
    """
    results: dict[str, Any] = {}
    for name in REPLAY_ANSWER_FILES:
        path = answers_dir / name
        if not path.is_file():
            continue
        key = name.removeprefix("replay_").removesuffix(".txt")
        group = 0 if key.startswith("RP0") else 1
        marker = meta["replay_marker"] if group == 0 else meta["markers"][0]
        rows = [dict(r, distance=r.get("replay_distance", r["distance"])) for r in truth_rows if r["checkpoint"] == group]
        results[key] = score_checkpoint(rows, path.read_text(encoding="utf-8"), marker)
        results[key]["context"] = meta["replay_contexts"][key.split("_")[0]]
    for k, marker in enumerate(meta["markers"], start=1):
        path = answers_dir / Path(answers_file(k)).name
        if not path.is_file() and results:
            continue  # a replay-only scoring directory
        rows = [r for r in truth_rows if r["checkpoint"] == k]
        if path.is_file():
            results[str(k)] = score_checkpoint(rows, path.read_text(encoding="utf-8"), marker)
            results[str(k)]["context"] = meta["checkpoint_contexts"][k - 1]
        else:
            results[str(k)] = {"missing_file": path.name}
    return results


def report(results: Mapping[str, Any]) -> str:
    out = ["| checkpoint | context (est) | score | 95% interval | instruction | flagged | unflagged | superseded | obligation | stale |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for k, r in results.items():
        if "missing_file" in r:
            out.append(f"| {k} | - | no answers file | | | | | | | |")
            continue
        lo, hi = r["interval"]
        ins = r["instruction"]
        cats = r["tables"]["category"]
        cell = lambda c: f"{cats[c]['correct']}/{cats[c]['total']}" if c in cats else "-"  # noqa: E731, B023
        stale = sum(c["stale"] for c in cats.values())
        out.append(
            f"| {k} | {r['context']:,} | {r['correct']}/{r['total']} | {lo:.2f}-{hi:.2f} | "
            f"{ins['compliant']}/{ins['lines']} | {cell('flagged')} | {cell('unflagged')} | {cell('superseded')} | "
            f"{cell('obligation')} | {stale} |"
        )
    out += ["", "| checkpoint | segment or depth | correct/total |", "|---|---|---|"]
    for k, r in results.items():
        if "missing_file" in r:
            continue
        for table in ("segment", "depth"):
            for name, c in r["tables"][table].items():
                out.append(f"| {k} | {table} {name} | {c['correct']}/{c['total']} |")
    return "\n".join(out)


# Output


def write_outputs(plan: Plan, out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    names = ("turns.jsonl", "replay_turns.jsonl", "probes.jsonl", "truth.jsonl", "budget.md", "contamination.json", "meta.json")
    paths = {name: out / name for name in names}
    for name, turns in (("turns.jsonl", plan.turns), ("replay_turns.jsonl", plan.replay_turns)):
        with paths[name].open("w", encoding="utf-8") as handle:
            for turn in turns:
                handle.write(json.dumps(asdict(turn), ensure_ascii=False) + "\n")
    for turn in plan.turns:
        for f in turn.files:
            target = out / "files" / Path(f["path"]).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f["content"], encoding="utf-8")
    offsets = plant_offsets(plan)
    replay_offsets = plant_offsets(plan, replay=True) if plan.replay_turns else {}
    reqs = requests(plan)
    probe_contexts = [r.context for r in reqs if r.note.startswith("checkpoint")]
    probe_offsets = {}
    position = 0
    for turn in plan.turns:
        text, files = turn_tokens(turn)
        if turn.kind == "probe":
            probe_offsets[turn.checkpoint] = position
        position += text + files + REPLY_TOKENS
    with paths["probes.jsonl"].open("w", encoding="utf-8") as probes, paths["truth.jsonl"].open("w", encoding="utf-8") as truth:
        for pid, p in probe_index(plan):
            probes.write(json.dumps({"id": pid, "checkpoint": p.checkpoint, "category": p.category,
                                     "segment": p.segment, "question": p.question}) + "\n")
            row = {"id": pid, "checkpoint": p.checkpoint, "category": p.category, "segment": p.segment,
                   "answer": p.value, "older": list(p.older)}
            if p.checkpoint == 0:
                row["offset"] = replay_offsets[p.key]
                row["distance"] = replay_offsets["__probe__RP0"] - replay_offsets[p.key]
            else:
                row["offset"] = offsets[p.key]
                row["distance"] = probe_offsets[p.checkpoint] - offsets[p.key]
                if p.checkpoint == 1 and p.key in replay_offsets:
                    row["replay_distance"] = replay_offsets["__probe__RP1"] - replay_offsets[p.key]
            truth.write(json.dumps(row) + "\n")
    paths["budget.md"].write_text(budget_sheet(plan) + "\n", encoding="utf-8")
    violations = contamination(plan)
    paths["contamination.json"].write_text(json.dumps({"violations": violations}, indent=2) + "\n", encoding="utf-8")
    replay_contexts = {}
    for r in replay_requests(plan):
        if r.note.endswith("arm"):
            replay_contexts[r.turn] = r.context
    paths["meta.json"].write_text(json.dumps({
        "seed": plan.seed, "head": plan.head, "markers": list(plan.markers), "checkpoint_contexts": probe_contexts,
        "replay_marker": plan.replay_marker, "replay_contexts": replay_contexts,
    }, indent=2) + "\n", encoding="utf-8")
    return paths


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L2d quality-versus-context probe (no model calls).")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--seed", type=int, default=DEFAULT_SEED)
    gen.add_argument("--head", type=int, default=DEFAULT_HEAD)
    gen.add_argument("--checkpoints", type=lambda v: tuple(int(x) for x in v.split(",")), default=CHECKPOINTS,
                     help="comma-separated target contexts, e.g. 50000,100000,250000")
    bud = sub.add_parser("budget")
    bud.add_argument("--seed", type=int, default=DEFAULT_SEED)
    bud.add_argument("--head", type=int, default=DEFAULT_HEAD)
    bud.add_argument("--checkpoints", type=lambda v: tuple(int(x) for x in v.split(",")), default=CHECKPOINTS,
                     help="comma-separated target contexts, e.g. 50000,100000,250000")
    sc = sub.add_parser("score")
    sc.add_argument("--answers-dir", type=Path, required=True)
    sc.add_argument("--truth", type=Path, required=True)
    sc.add_argument("--json", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "generate":
        plan = build(args.seed, args.head, args.checkpoints)
        paths = write_outputs(plan, args.out)
        violations = json.loads(paths["contamination.json"].read_text())["violations"]
        for name, path in paths.items():
            print(f"{name}: {path.resolve()}")
        print(f"contamination violations: {len(violations)}")
        return 1 if violations else 0
    if args.command == "budget":
        print(budget_sheet(build(args.seed, args.head, args.checkpoints)))
        return 0
    truth = [json.loads(line) for line in args.truth.read_text(encoding="utf-8").splitlines() if line.strip()]
    meta = json.loads((args.truth.parent / "meta.json").read_text(encoding="utf-8"))
    results = score_dir(args.answers_dir, truth, meta)
    print(report(results))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
