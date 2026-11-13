"""Protocol ``d27``: the 54-probe acceptance script of live runs #3 and #4, ported from the spikes.

This is a port, kept line for line where the random-number call order depends on it, of

- ``spikes/m2/acceptance/script.py`` (``build``, ``assign_probes``, the constants),
- ``spikes/m2/acceptance/run.py::build_script`` and ``spikes/m2/acceptance/budget.py::turn_tokens``,
- ``spikes/m0/fidelity/harder.py`` (``_Pools``, ``_group``, ``_merge``),
- ``spikes/m0/fidelity/session.py::_filler_text`` and ``spikes/m0/fidelity/l2d.py::_WORDS``,
- ``spikes/m0/fidelity/scoring.py`` (``normalize``, ``parse_answers``).

The spike's probe mix is fixed here (``CHAIN_HEAVY`` in the spike): its 2-hop and reasoning branches
are not ported because that mix never takes them. For any seed, bound and turn size the turns,
probes and probe-turn assignment equal the spike's; ``tests/test_bench_script.py`` compares them and
pins SHA-256 digests, so a change in Python's ``random`` fails a test instead of changing a script.

The wheel ships no spikes, so nothing here imports from them.
"""

from __future__ import annotations

import functools
import random
import re

from contexto.bench.model import Probe, Script, lexical_overlap

DEFAULT_SEED = 20261016  # the seed of live runs #3 and #4
PAGED_ONLY = 4  # probes whose only statement sits in turn 1
PROBE_TURNS = 6
PROBES_PER_TURN = 9
PROBE_HEADER = (
    "Answer the questions below about this conversation. Reply with one line per question, in the form",
    "q_id: answer",
    "with the answer only. For a list, give the ids separated by commas. If the conversation does not "
    "contain it, say unknown.",
)
INSTRUCTIONS = (
    "This is a long working session. Each message pastes the next part of a working log. Read it, keep "
    "what matters, and reply with exactly: OK"
)
LOG_HEAD = "Next part of the working log:"
LOG_TAIL = "Reply with exactly: OK"

# The spike's chain-heavy mix: (hops, count), (depth, count), (look-alikes besides the true line, count).
MULTIHOP = ((3, 4), (4, 4))
CHAIN = ((6, 15), (8, 15))
OBLIGATION_SINGLE = 4
OBLIGATION_SETS = 2
SET_SIZE = 6
DISTRACTOR = ((8, 6),)

BYTES_PER_TOKEN_LOG = 1.8  # M0's measured rate for numeric log text
BYTES_PER_TOKEN_TEXT = 3.5  # the rate the spike's budget uses for a probe turn's prose
REPLY_TOKENS = 20  # "OK" plus a little thinking per content turn (budget.Params.reply_tokens)
HISTORY_FACTOR = 2.5  # the content turns reach this many times the bound

WORDS = (
    "amber", "basalt", "cobalt", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper",
    "kelvin", "lumen", "marble", "nickel", "onyx", "pewter", "quartz", "russet", "sable", "topaz",
)
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


def turn_tokens(turn_bytes: int) -> int:
    """The token estimate of ``turn_bytes`` bytes of log text, at 1.8 bytes per token."""
    return int(turn_bytes / BYTES_PER_TOKEN_LOG) + 1


def filler_text(rng: random.Random, lines: int, label: str) -> str:
    out = []
    for n in range(lines):
        out.append(
            f"{label} line {n:04d} level={rng.choice(('INFO', 'DEBUG', 'WARN'))} "
            f"latency_ms={rng.randrange(1, 900)} shard={rng.randrange(0, 64)} ok"
        )
    return "\n".join(out)


@functools.cache
def filler_line_bytes() -> float:
    """The mean size of a filler line with its newline, measured on the generator, not assumed."""
    sample = filler_text(random.Random(0), 200, "s00")
    return (len(sample.encode("utf-8")) + 1) / 200


class Pools:
    """The shuffled name pools and the values and ids already used, so nothing is planted twice."""

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
            v = f"{self.rng.choice(WORDS)}-{self.rng.randrange(1000, 10000)}"
            if v not in self.used_values:
                self.used_values.add(v)
                return v

    def near_values(self, n: int) -> list[str]:
        """n values with one stem and consecutive numbers: they differ only in the last digit or two."""
        while True:
            stem, base = self.rng.choice(WORDS), self.rng.randrange(1000, 9990 - n)
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


def _probe(pid: str, type_: str, tier: str, question: str, answer: str, *, kind: str = "value",
           older: tuple[str, ...] = (), subjects: tuple[str, ...] = (), statements: tuple[str, ...] = ()) -> Probe:
    return Probe(pid, "a", type_, tier, question, answer, kind, older, subjects, statements, cls=type_,
                 overlap=lexical_overlap(question, statements))


def _group(pools: Pools) -> tuple[list[Probe], list[list[str]]]:
    """The 50 planted probes and, per plant, its statements in order."""
    rng = pools.rng
    probes: list[Probe] = []
    plants: list[list[str]] = []
    n = 0

    def pid() -> str:
        nonlocal n
        n += 1
        return f"q_a_{n:03d}"

    for hops, count in MULTIHOP:
        for _ in range(count):
            i = pid()
            svc, owner = pools.services.pop(), pools.people.pop()
            chain = [f"Ownership note: the {svc} service is owned by {owner}."]
            subjects = [svc, owner]
            region = rng.choice(_REGIONS)
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
            via_site = ", that team's site," if hops == 4 else ""
            q = (f"Follow the notes from the {svc} service to its owner, the owner's team{via_site} and the "
                 "region it covers. Which region is it? Answer with the region only.")
            probes.append(_probe(i, "multihop", f"{hops}-hop", q, region, subjects=tuple(subjects),
                                 statements=tuple(chain)))
            plants.append(chain)
    for depth, count in CHAIN:
        for c in range(count):
            i = pid()
            subject = f"{pools.services.pop()} rollout cohort"
            vals = [pools.value() for _ in range(depth)]
            st = ([f"Cohort update: the {subject} is set to {vals[0]}."]
                  + [f"Cohort update: the {subject} is now {v}." for v in vals[1:]])
            previous = c % 2 == 1
            if previous:
                q = f"What was the {subject} immediately before its current value? Answer with the value only."
                answer, older = vals[-2], tuple(v for v in vals if v != vals[-2])
            else:
                q = f"What is the current {subject}? Answer with the value only."
                answer, older = vals[-1], tuple(vals[:-1])
            probes.append(_probe(i, "chain", f"depth{depth}-{'previous' if previous else 'current'}", q, answer,
                                 older=older, subjects=(subject,), statements=tuple(st)))
            plants.append(st)
    # Obligations: two close/reopen cycles for some; singles and sets use disjoint ids.
    patterns = (("open",), ("open", "close"), ("open", "close", "reopen"), ("open", "close", "reopen", "close"),
                ("open", "close", "reopen", "close", "reopen"))

    def obligation() -> tuple[str, str, list[str]]:
        oid = pools.ob_id()
        events = rng.choice(patterns)
        task = (f"{rng.choice(('rotate', 'audit', 'migrate', 'document'))} the "
                f"{rng.choice(('key', 'job', 'config', 'alert'))} backlog")
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

    for _ in range(OBLIGATION_SINGLE):
        i = pid()
        oid, state, st = obligation()
        probes.append(_probe(i, "obligations", "single", f"Is action item {oid} open or closed? Answer open or closed.",
                             state, older=("closed",) if state == "open" else ("open",), subjects=(oid,),
                             statements=tuple(st)))
        plants.append(st)
    for _ in range(OBLIGATION_SETS):
        i = pid()
        members = [obligation() for _ in range(SET_SIZE)]
        open_ids = sorted(o for o, s, _ in members if s == "open")
        names = sorted(o for o, _, _ in members)
        all_st = tuple(t for _, _, st in members for t in st)
        probes.append(_probe(i, "obligations", f"set{SET_SIZE}",
                             f"Of action items {', '.join(names)}, which are still open? List their ids.",
                             ", ".join(open_ids) or "none", kind="set", statements=all_st))
        for _, _, st in members:
            plants.append(st)
    for dups, count in DISTRACTOR:
        for _ in range(count):
            i = pid()
            svc = pools.services.pop()
            shards = sorted(rng.sample(range(10, 99), dups + 1))
            vals = pools.near_values(dups + 1)
            rng.shuffle(vals)
            lines = [f"cfg {svc}.shard_{s}.map_version={v}" for s, v in zip(shards, vals, strict=True)]
            t = rng.randrange(len(lines))
            key, answer = lines[t].split("=")[0][4:], lines[t].split("=")[1]
            probes.append(_probe(i, "distractor", f"{dups}-dups",
                                 f"What value did {key} have in the logs earlier in this conversation? "
                                 "Answer with the value only.",
                                 answer, older=tuple(v for k, v in enumerate(vals) if k != t), subjects=(key,),
                                 statements=("\n".join(lines),)))
            plants.append(["\n".join(lines)])
    return probes, plants


def _merge(rng: random.Random, plants: list[list[str]]) -> list[str]:
    """Random merge that keeps each plant's statements in order."""
    queues = [list(p) for p in plants]
    out: list[str] = []
    while any(queues):
        live = [q for q in queues if q]
        weights = [len(q) for q in live]
        q = rng.choices(live, weights=weights)[0]
        out.append(q.pop(0))
    return out


def assign_probes(probes: list[Probe], seed: int, turns: int = PROBE_TURNS,
                  per_turn: int = PROBES_PER_TURN) -> list[list[Probe]]:
    """D27's fixed assignment, written down before any result (2026-10-16): shuffle a copy of the
    probes in script order with ``random.Random(seed).shuffle``, then turn k asks positions
    ``[per_turn * (k - 1), per_turn * k)``."""
    if len(probes) != turns * per_turn:
        raise ValueError(f"{len(probes)} probes do not fill {turns} turns of {per_turn}")
    order = list(probes)
    random.Random(seed).shuffle(order)
    return [order[per_turn * k: per_turn * (k + 1)] for k in range(turns)]


def probe_turn_text(block: list[Probe]) -> str:
    return "\n".join([*PROBE_HEADER, "", *[f"{p.id}: {p.question}" for p in block]])


def log_turn(rng: random.Random, items: list[str], turn_bytes: int, label: str, head: str = LOG_HEAD) -> str:
    """One pasted-log turn of about ``turn_bytes`` bytes: filler lines with ``items`` spread through
    them in order (sorted slots, each shifted by the items already placed)."""
    used = sum(len(x.encode("utf-8")) + 1 for x in items)
    filler_lines = max(0, int((turn_bytes - used - 80) / filler_line_bytes()))
    log = filler_text(rng, filler_lines, label).split("\n") if filler_lines else []
    slots = sorted(rng.randrange(0, len(log) + 1) for _ in items)
    for k, (slot, item) in enumerate(zip(slots, items)):
        log.insert(slot + k, item)
    return "\n".join([head, *log, LOG_TAIL])


def build(seed: int, turns: int, turn_bytes: int, bound: int = 150_000) -> tuple[Script, Pools]:
    """``turns`` content turns of about ``turn_bytes`` bytes each, then the six probe turns.

    Plants are merged in random order (each plant's statements stay in order) and spread across
    turns 2 to ``turns``; turn 1 carries only the paged-only plants and filler. Also returns the
    pools, which say which services, values and ids the script used.
    """
    if turns < 4:
        raise ValueError("need at least 4 content turns")
    rng = random.Random(seed)
    pools = Pools(rng)
    probes, plants = _group(pools)
    events = _merge(rng, plants)

    paged: list[Probe] = []
    first_lines: list[str] = []
    for i in range(PAGED_ONLY):
        svc = pools.services.pop()
        key = f"{svc}.retention_days"
        value = pools.value()
        line = f"cfg {key}={value}"
        first_lines.append(line)
        paged.append(_probe(f"q_p_{i + 1:02d}", "paged_only", "turn-1", f"What value did {key} have in the logs "
                            "earlier in this conversation? Answer with the value only.", value, subjects=(key,),
                            statements=(line,)))

    per_turn: list[list[str]] = [[] for _ in range(turns)]
    per_turn[0] = first_lines
    for j, ev in enumerate(events):
        per_turn[1 + j * (turns - 1) // len(events)].append(ev)

    texts = [log_turn(rng, items, turn_bytes, f"s{t}", INSTRUCTIONS if t == 0 else LOG_HEAD)
             for t, items in enumerate(per_turn)]
    all_probes = tuple(paged) + tuple(probes)
    blocks = assign_probes(list(all_probes), seed)
    texts += [probe_turn_text(block) for block in blocks]
    script = Script(
        seed=seed, turns=tuple(texts), probes=all_probes, paged_only=tuple(p.id for p in paged),
        turn_bytes=tuple(len(t.encode("utf-8")) for t in texts),
        probe_turn_ids=tuple(tuple(p.id for p in block) for block in blocks),
        protocol="d27", bound=bound, turn_size=turn_bytes,
        kinds=("log",) * turns + ("probe",) * len(blocks),
    )
    return script, pools


def build_script(seed: int, turns: int, turn_bytes: int, bound: int) -> tuple[Script, Pools]:
    """With ``turns`` 0, the fewest content turns whose sizes reach 2.5 times the bound, counting each
    turn at ``turn_tokens`` of its bytes plus ``REPLY_TOKENS``."""
    if turns:
        return build(seed, turns, turn_bytes, bound)
    n = 4
    while True:
        s, pools = build(seed, n, turn_bytes, bound)
        tokens = sum(turn_tokens(x) + REPLY_TOKENS for x in s.content_bytes)
        if tokens >= HISTORY_FACTOR * bound:
            return s, pools
        n += 1


# Scoring: exact match after normalization, as spikes/m0/fidelity/scoring.py.

_TRIM = " \t\r\n\"'`.,;:!"
_ANSWER_LINE = re.compile(r"^\s*(q_[a-z0-9_]+)\s*[:=]\s*(.*?)\s*$")


def normalize(text: str | None) -> str:
    """Case folded, trimmed of whitespace, quotes, backticks and trailing punctuation, with inner
    whitespace collapsed. Nothing else is forgiven."""
    if text is None:
        return ""
    return " ".join(text.strip(_TRIM).casefold().split()).strip(_TRIM)


def parse_answers(text: str) -> dict[str, str]:
    """Parse ``q_<id>: <answer>`` lines. The last line for an id wins; other lines are ignored."""
    answers: dict[str, str] = {}
    for line in text.splitlines():
        match = _ANSWER_LINE.match(line.strip().lstrip("-* "))
        if match:
            answers[match.group(1)] = match.group(2)
    return answers
