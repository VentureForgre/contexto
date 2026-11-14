"""The benchmark's session script: generator, self-checks and scoring (D66).

``build(seed, protocol, bound, turn_bytes, turns)`` returns a ``Script``: the user turns to send, in
order, and the probes with their right answers. It is deterministic for its arguments, and nothing
in it comes from a real session.

Protocol ``d27`` is the 54-probe script of live runs #3 and #4, unchanged (``contexto.bench.d27``).
Every one of its questions names its subject's key, which suits a word search.

Protocol ``v2`` keeps every ``d27`` turn text and probe and adds three probe classes of six probes
each (18 new probes, 72 in all), whose questions contain none of their plant's subject strings:

``paraphrase``
    Six facts are planted as ``cfg <service>.<attribute>=<value>`` lines in one more pasted-log turn.
    The question names the service in plain words (``billing-core`` becomes "the billing core
    service") and describes the attribute in other words, from the fixed table ``ATTRIBUTES``.
    The service is named, so the attribute is what has to be matched: every service a
    ``paraphrase`` or ``toolloop`` probe asks about has ``DECOYS`` (3) more ``cfg`` lines for other
    attributes, each with its own value, so a search for the service finds at least four lines and
    only the paraphrase picks the right one. Exactly one line answers each question. The decoy
    values are the probe's ``older`` values, so an answer that picked a decoy is counted stale.
    Decoys come from other attribute ``FAMILIES`` than the asked attribute and than each other, so
    no question fits two attributes planted for its service; and no planted attribute's identifier
    shares a word with its service's name or with the question (``leaks``).
``concluded``
    Six short ``decision`` turns each give three candidates with a number each and a rule (take the
    lowest, or the highest) and ask for a reply of exactly ``DECISION <label>: <name>``. The pick is
    stated only in the assistant's reply; the user turn holds the candidates and the rule, so the
    pick can also be worked out again from it. The question describes what was decided in other
    words (``SCENARIOS``), asks for the name, and contains neither the label nor a candidate. The
    two candidates that lost are the probe's ``older`` values. ``decision_reply_ok`` tells whether
    a reply made the right decision, so a reader can tell a wrong conclusion from a failed recall.
    Scoring is against the generator's value either way.
``toolloop``
    One turn asks the assistant to read ``TOOLLOOP_FILES`` (10) generated files, one at a time, with
    its file-read tool and then reply OK. The files hold log filler and sum to about twice the
    content turn size (10 files of about 8,000 bytes, 80,000 bytes in all, at the default 40,000
    byte turn). Six of them carry one asked ``cfg`` fact each, asked about as in ``paraphrase``.
    The script carries the files (``Script.files``) and the order the turn lists them in
    (``Script.reads``). Naming the service is not what makes this class hard: the fact arrives as a
    tool result inside one long turn of tool calls.

Where the ``cfg`` lines go. A ``paraphrase`` service's asked line and two decoys are in the log
turn and its third decoy in a file. A ``toolloop`` service's asked line is in its own file, two
decoys in two other files and one decoy in the log turn. So each service's lines sit in at least two
places, and within the source its question names (the logs, or the files) there are still at
least three lines for the service. The self-check ``service_alone`` fails when a search for a
service's name, hyphenated or as separate words, finds only the plant line.

Placement. The eight new content turns go in one block straight after the first ``d27`` turn, in an
order shuffled by the new plants' random stream, so every ``d27`` content turn but the first comes
after them. The generator refuses a script in which fewer than ``bound`` tokens of later content
turns (``later_tokens``) separate a new plant from the first probe turn.

Probe turns. The six ``d27`` probe turns come first, unchanged. The 18 new probes follow in two more
turns of nine: the new probes in script order (paraphrase, concluded, toolloop, each 1 to 6) are
shuffled with ``random.Random(f"contexto-bench/v2/{seed}/probe-order").shuffle``, then the seventh
probe turn asks positions 0 to 8 and the eighth positions 9 to 17.

Random streams. ``d27`` draws from ``random.Random(seed)`` exactly as the spike does. Everything
``v2`` adds draws from ``random.Random(f"contexto-bench/v2/{seed}/plants")``, so the ``d27`` stream
is untouched; new services come from the ones ``d27`` left unused and new values avoid its values.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Sequence
from typing import Any

from contexto.bench import d27
from contexto.bench.d27 import REPLY_TOKENS, normalize, parse_answers, turn_tokens
from contexto.bench.model import (
    D27_CLASSES,
    DEFAULT_SEED,
    FILE_DIR,
    NEW_CLASSES,
    PROTOCOLS,
    Decision,
    Probe,
    Script,
    lexical_overlap,
    words,
)
from contexto.bench.stats import wilson

__all__ = [
    "ATTRIBUTES", "D27_CLASSES", "DEFAULT_SEED", "FAMILIES", "NEW_CLASSES", "PROTOCOLS", "SCENARIOS", "Decision",
    "Probe", "Script", "build", "checks", "decision_reply_ok", "estimate", "key_free", "later_tokens", "leaks",
    "lexical_overlap", "load_bytes", "overlap_by_class", "placement", "plant_turns", "restatements", "score",
    "service_alone", "service_lines", "unplanted", "words",
]

PER_CLASS = 6  # probes in each new class
NEW_PROBES_PER_TURN = 9
TOOLLOOP_FILES = 10
TOOLLOOP_TURNS_WORTH = 2  # the files sum to this many content turns' bytes
MAX_FILE_BYTES = 20_000  # more files, not larger ones, above this: a file-read tool caps what one read returns
DECOYS = 3  # more cfg lines for each paraphrase and toolloop service, one per other attribute

# The synonym table: an attribute identifier and a clause that asks for it without any of its words.
# ``{svc}`` is the service in plain words. tests/test_bench_script.py checks that no clause shares a
# word with its identifier.
ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("failover_target", "where does the {svc} service send its traffic when its primary goes down?"),
    ("image_tag", "which container build label is the {svc} service running?"),
    ("oncall_alias", "what is the paging handle of whoever is on duty for the {svc} service?"),
    ("backup_bucket", "where are the nightly copies of the {svc} service's data stored?"),
    ("signing_secret", "what does the {svc} service use to prove that its requests are authentic?"),
    ("queue_name", "from which work line does the {svc} service pull its jobs?"),
    ("cert_serial", "what number identifies the TLS credential that the {svc} service presents?"),
    ("dns_zone", "under which domain are the hosts of the {svc} service registered?"),
    ("cache_namespace", "what prefix does the {svc} service put in front of its stored lookups?"),
    ("log_stream", "where does the {svc} service write its diagnostic output?"),
    ("release_channel", "which update track is the {svc} service subscribed to?"),
    ("schema_revision", "which version of the database layout is the {svc} service on?"),
    ("throttle_tier", "which rate limiting class is the {svc} service assigned?"),
    ("feature_set", "which bundle of optional behaviours is switched on for the {svc} service?"),
    ("standby_pool", "which group of spare machines backs up the {svc} service?"),
    ("alert_route", "where are warnings about the {svc} service delivered?"),
)

# Attributes whose clauses could be read as asking for one another share a family. Two attributes of
# one family are never planted for the same service, so no question fits two of its service's lines.
# The grouping errs towards wide: backups and spare machines, warnings and diagnostic output, and the
# four kinds of version or switch are each one family.
FAMILIES: dict[str, str] = {
    "failover_target": "redundancy", "standby_pool": "redundancy", "backup_bucket": "redundancy",
    "oncall_alias": "signals", "alert_route": "signals", "log_stream": "signals",
    "signing_secret": "credentials", "cert_serial": "credentials",
    "image_tag": "versions", "release_channel": "versions", "schema_revision": "versions", "feature_set": "versions",
    "queue_name": "queues", "throttle_tier": "limits", "dns_zone": "naming", "cache_namespace": "caching",
}

# The decision scenarios: an id, the rule ("min" or "max"), the sentence that introduces the
# candidates, and the question that describes the decision in other words.
SCENARIOS: tuple[tuple[str, str, str, str], ...] = (
    ("quickest-host", "min",
     "Three hosts were load tested. Each line gives a host and its median latency in milliseconds.",
     "Earlier you had to pick the quickest of several machines by how long each took to respond, and you replied "
     "with your pick. What did you pick?"),
    ("soundest-build", "min",
     "Three release candidates went through the test suite. Each line gives a candidate and its count of failed "
     "checks.",
     "Earlier you had to pick the version that broke the fewest things when it was verified, and you replied with "
     "your pick. What did you pick?"),
    ("roomiest-store", "max",
     "Three storage tiers were measured. Each line gives a tier and its free capacity in terabytes.",
     "Earlier you had to pick the place with the most room left to keep data, and you replied with your pick. "
     "What did you pick?"),
    ("fullest-queue", "max",
     "Three queues were sampled. Each line gives a queue and its backlog in thousands of messages.",
     "Earlier you had to pick the waiting list with the most items in it, to be emptied first, and you replied "
     "with your pick. What did you pick?"),
    ("soonest-expiry", "min",
     "Three certificates were checked. Each line gives a certificate and the days left before it expires.",
     "Earlier you had to pick the credential that runs out soonest, to be renewed first, and you replied with "
     "your pick. What did you pick?"),
    ("furthest-behind", "max",
     "Three replicas were compared. Each line gives a replica and its replication lag in seconds.",
     "Earlier you had to pick the database copy that had fallen furthest behind, to be rebuilt, and you replied "
     "with your pick. What did you pick?"),
)
_RULE_WORD = {"min": "lowest", "max": "highest"}
# The fixed texts of the turns protocol v2 adds. model.GENERATOR_WORDS holds their words.
TOOLLOOP_HEAD = (
    "Next, {n} files instead of a pasted log. Read each file below with your file-read tool, one file at a time, "
    "in the order listed, each one to its end. Do not search them and do not skip any. The paths are relative to "
    "the current working directory. When you have read all {n}, reply with exactly: OK")
DECISION_HEAD = "Decision point: this message has no log."
DECISION_RULE = "Rule: take the one with the {rule} number."
DECISION_ASK = ("Reply with exactly one line and nothing else, in this form:", "DECISION {label}: <name>")
_TOKEN = re.compile(r"[a-z0-9]+")
_CFG_LINE = re.compile(r"^cfg ([^=\s]+)=\S*$")
CFG_CLASSES = ("paraphrase", "toolloop")  # the new classes planted as cfg lines
_HYPHENS = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2212\ufe63\uff0d"})


def build(seed: int = DEFAULT_SEED, protocol: str = "v2", bound: int = 150_000, turn_bytes: int = 40_000,
          turns: int = 0) -> Script:
    """The script for ``seed``.

    ``turns`` is the number of ``d27`` content turns of about ``turn_bytes`` bytes; 0 means the
    fewest whose sizes reach 2.5 times ``bound``, as live runs #3 and #4 chose them. Protocol ``v2``
    adds eight content turns and two probe turns to that. Raises ``ValueError`` for an unknown
    protocol, fewer than four ``d27`` content turns, or a ``v2`` script too short to put ``bound``
    tokens of history after its new plants.
    """
    if protocol not in PROTOCOLS:
        raise ValueError(f"unknown protocol {protocol!r}: use one of {', '.join(PROTOCOLS)}")
    base, pools = d27.build_script(seed, turns, turn_bytes, bound)
    if protocol == "d27":
        return base
    script = _extend(base, pools, random.Random(f"contexto-bench/v2/{seed}/plants"))
    problems = placement(script)
    if problems:
        raise ValueError("the script is too short for protocol v2 at this bound, so its new plants would not be "
                         f"followed by {bound:,} tokens of history: {problems[0]}")
    return script


def _plain(service: str) -> str:
    return service.replace("-", " ")


def leaks(identifier: str, text: str) -> bool:
    """Whether an attribute identifier shares a word with ``text``: a token of the identifier equals a
    token of ``text``, or one of the two (3 or more characters) contains the other. Tokens are runs of
    ASCII letters and digits after case folding. ``router`` leaks ``alert_route`` and ``catalog``
    leaks ``log_stream``; a search matches substrings, so containment counts."""
    mine = set(_TOKEN.findall(identifier.casefold()))
    theirs = {w for w in _TOKEN.findall(text.casefold()) if len(w) >= 3}
    return any(a == b or (len(a) >= 3 and (a in b or b in a)) for a in mine for b in theirs)


def _question(service: str, clause: str, source: str) -> str:
    return (f"From the {source} earlier in this conversation: {clause.format(svc=_plain(service))} "
            "Answer with the value only.")


def _extend(base: Script, pools: d27.Pools, rng: random.Random) -> Script:
    """``base`` (a d27 script) with the v2 turns, probes, decisions and files added."""
    size = base.turn_size

    def value() -> str:
        while True:
            v = f"{rng.choice(d27.WORDS)}-{rng.randrange(1000, 10000)}"
            if v not in pools.used_values:
                pools.used_values.add(v)
                return v

    def probe(pid: str, cls: str, tier: str, question: str, answer: str, subjects: tuple[str, ...],
              statements: tuple[str, ...], older: tuple[str, ...]) -> Probe:
        return Probe(pid, "b", cls, tier, question, answer, "value", older, subjects, statements, cls=cls,
                     overlap=lexical_overlap(question, statements))

    # The twelve services d27 left unused: six for paraphrase, six for toolloop. Each gets one asked
    # attribute (twelve different ones) and DECOYS more, all from different families, none sharing a
    # word with the question.
    services = rng.sample(sorted(pools.services), 2 * PER_CLASS)
    sources = ["logs"] * PER_CLASS + ["files you read"] * PER_CLASS
    clauses = dict(ATTRIBUTES)
    asked: list[str] = []
    planted: list[list[str]] = []  # per service: the asked attribute, then its decoys
    for service, source in zip(services, sources):
        options = [name for name, clause in ATTRIBUTES if name not in asked
                   and not leaks(name, _question(service, clause, source))]
        name = rng.choice(options)
        asked.append(name)
        question = _question(service, clauses[name], source)
        families = {FAMILIES[name]}
        chosen = [name]
        for other in rng.sample([n for n, _ in ATTRIBUTES], len(ATTRIBUTES)):
            if len(chosen) > DECOYS:
                break
            if FAMILIES[other] not in families and not leaks(other, question):
                families.add(FAMILIES[other])
                chosen.append(other)
        if len(chosen) <= DECOYS:  # cannot happen with the table above; tests/test_bench_script.py checks seeds
            raise RuntimeError(f"too few attribute families left for {service}")
        planted.append(chosen)

    facts: list[Probe] = []
    lines: list[list[str]] = []  # per service: the asked line, then the decoy lines
    for k, (service, source, names) in enumerate(zip(services, sources, planted)):
        cls, tier, pid = (("paraphrase", "cfg-line", f"q_r_{k + 1:02d}") if k < PER_CLASS
                          else ("toolloop", "file-line", f"q_t_{k - PER_CLASS + 1:02d}"))
        values = [value() for _ in names]
        mine = [f"cfg {service}.{n}={v}" for n, v in zip(names, values)]
        lines.append(mine)
        key = f"{service}.{names[0]}"
        facts.append(probe(pid, cls, tier,
                           _question(service, clauses[names[0]], source), values[0], (key, service, names[0]),
                           (mine[0],), tuple(values[1:])))
    paraphrase, toolloop = facts[:PER_CLASS], facts[PER_CLASS:]

    # Where each line goes: the log turn, or one of the files.
    n_files = max(TOOLLOOP_FILES, math.ceil(TOOLLOOP_TURNS_WORTH * size / MAX_FILE_BYTES))
    holders = rng.sample(range(n_files), PER_CLASS)
    in_log: list[str] = []
    in_file: list[list[str]] = [[] for _ in range(n_files)]
    for mine in lines[:PER_CLASS]:
        in_log += mine[:3]
        in_file[rng.randrange(n_files)].append(mine[3])
    for holder, mine in zip(holders, lines[PER_CLASS:]):
        in_file[holder].append(mine[0])
        for k, line in zip(rng.sample([k for k in range(n_files) if k != holder], 2), mine[1:3]):
            in_file[k].append(line)
        in_log.append(mine[3])
    rng.shuffle(in_log)

    # paraphrase: one more pasted-log turn.
    new_turns: list[tuple[str, str, Any]] = [("log", d27.log_turn(rng, in_log, size, "p0"), None)]

    # toolloop: files of log filler with the cfg lines spread through them.
    files: dict[str, str] = {}
    for k in range(n_files):
        used = sum(len(x.encode("utf-8")) + 1 for x in in_file[k])
        count = max(1, int((TOOLLOOP_TURNS_WORTH * size / n_files - used) / d27.filler_line_bytes()))
        log = d27.filler_text(rng, count, f"f{k + 1:02d}").split("\n")
        for item in in_file[k]:
            log.insert(rng.randrange(0, len(log) + 1), item)
        files[f"{FILE_DIR}/log-{k + 1:02d}.txt"] = "\n".join(log) + "\n"
    reads = tuple(files)
    new_turns.append(("toolloop", "\n".join([TOOLLOOP_HEAD.format(n=n_files), *reads]), None))

    # concluded: six decision turns, one per scenario.
    concluded: list[Probe] = []
    labels: set[str] = set()
    for i, (_scenario, rule, intro, question) in enumerate(rng.sample(SCENARIOS, PER_CLASS)):
        label = f"DX-{rng.randrange(1000, 10000)}"
        while label in labels:
            label = f"DX-{rng.randrange(1000, 10000)}"
        labels.add(label)
        names = [value() for _ in range(3)]
        numbers = rng.sample(range(110, 990, 40), 3)  # distinct, at least 40 apart
        pick = names[numbers.index(min(numbers) if rule == "min" else max(numbers))]
        text = "\n".join([
            DECISION_HEAD, intro, *[f"{name} = {number}" for name, number in zip(names, numbers)],
            DECISION_RULE.format(rule=_RULE_WORD[rule]), DECISION_ASK[0], DECISION_ASK[1].format(label=label),
        ])
        reply = f"DECISION {label}: {pick}"
        pid = f"q_c_{i + 1:02d}"
        concluded.append(probe(pid, "concluded", rule, f"{question} Answer with the name only.", pick,
                               (label, *names), (text, reply), tuple(n for n in names if n != pick)))
        new_turns.append(("decision", text, (pid, label, pick, reply)))

    rng.shuffle(new_turns)
    n = base.n_content
    first = 1  # the block goes straight after d27's first turn
    content = [*base.turns[:first], *[text for _, text, _ in new_turns], *base.turns[first:n]]
    kinds = [*base.kinds[:first], *[kind for kind, _, _ in new_turns], *base.kinds[first:n]]
    decisions = tuple(Decision(first + k, *extra) for k, (kind, _, extra) in enumerate(new_turns)
                      if kind == "decision")

    new_probes = [*paraphrase, *concluded, *toolloop]
    order = list(new_probes)
    random.Random(f"contexto-bench/v2/{base.seed}/probe-order").shuffle(order)
    blocks = [order[k: k + NEW_PROBES_PER_TURN] for k in range(0, len(order), NEW_PROBES_PER_TURN)]
    probe_turns = [*base.probe_texts, *[d27.probe_turn_text(block) for block in blocks]]
    texts = [*content, *probe_turns]
    added = [*range(first, first + len(new_turns)), *range(len(texts) - len(blocks), len(texts))]
    return Script(
        seed=base.seed, turns=tuple(texts), probes=(*base.probes, *new_probes), paged_only=base.paged_only,
        turn_bytes=tuple(len(t.encode("utf-8")) for t in texts),
        probe_turn_ids=(*base.probe_turn_ids, *[tuple(p.id for p in block) for block in blocks]),
        protocol="v2", bound=base.bound, turn_size=size, kinds=(*kinds, *("probe",) * len(probe_turns)),
        added_turns=tuple(added), decisions=decisions, files=files, reads=reads,
    )


def decision_reply_ok(script: Script, turn_index: int, reply_text: str) -> bool:
    """Whether ``reply_text``, the assistant's reply to the ``decision`` turn at ``turn_index``, made
    the right decision.

    The reply's last line that holds ``DECISION <label>`` for the turn's label, followed by ``:``,
    ``=`` or ``-``, counts, anywhere in the line and in any case, after markup (``*``, ``_``,
    backticks) is dropped and any Unicode hyphen or dash is read as ``-``; ``DECISION: <label>:`` is
    read the same way. In the text after the separator, anything inside round, square or curly
    brackets is ignored. The first candidate named in the rest (each candidate matched as a whole
    name, case folded) is the decision. It is right when it is the candidate the rule selects, no
    other candidate is named outside brackets, and no negation (``not``, ``no``, ``except``, ...)
    comes before it. So ``1. **DECISION** DX-1: nickel-1771 (lowest, 110)`` and ``DECISION DX-1 =
    nickel-1771 (beats onyx-2203 and basalt-1180)`` are right, and ``DECISION DX-1: nickel-1771 or
    onyx-2203``, ``DECISION DX-1: not nickel-1771`` and ``DECISION DX-1: (nickel-1771)`` are not. A
    reply with no such line for this label is not. Raises ``ValueError`` if that turn is not a
    decision turn.
    """
    decision = next((d for d in script.decisions if d.turn == turn_index), None)
    if decision is None:
        raise ValueError(f"turn {turn_index} is not a decision turn")
    probe = next((p for p in script.probes if p.id == decision.probe), None)
    candidates = list(probe.subjects[1:]) if probe is not None else [decision.value]
    label = re.escape(decision.label.casefold())
    line_rule = re.compile(r"(?<![a-z0-9])decision(?:\s*:\s*|\s+)" + label + r"(?![a-z0-9-])\s*[:=-](.*)$")
    given = None
    for line in reply_text.translate(_HYPHENS).splitlines():
        match = line_rule.search(re.sub(r"[*_`]", "", line).casefold())
        if match:
            given = match.group(1)
    if given is None:
        return False
    outside = given
    while True:  # innermost brackets first, so nested ones go too
        dropped = _BRACKETED.sub(" ", outside)
        if dropped == outside:
            break
        outside = dropped
    named = []
    for candidate in candidates:
        found = re.search(r"(?<![a-z0-9-])" + re.escape(candidate.casefold()) + r"(?![a-z0-9-])", outside)
        if found:
            named.append((found.start(), candidate))
    if len(named) != 1:
        return False
    start, first = named[0]
    before = re.findall(r"[a-z']+", outside[:start])
    if any(w in _NEGATIONS or w.endswith("n't") for w in before):
        return False
    return first == decision.value or normalize(first) == normalize(decision.value)


_BRACKETED = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}")
_NEGATIONS = frozenset(("not", "no", "never", "nor", "neither", "except", "excluding", "without", "other"))


# Sizes


def load_bytes(script: Script, turn_index: int) -> int:
    """The bytes turn ``turn_index`` adds to the conversation: its user text, plus the files a
    ``toolloop`` turn reads. Replies and tool-call framing are not counted."""
    extra = 0
    if script.kinds[turn_index] == "toolloop":
        extra = sum(len(script.files[path].encode("utf-8")) for path in script.reads)
    return script.turn_bytes[turn_index] + extra


def later_tokens(script: Script, turn_index: int) -> int:
    """The token estimate of the content turns after ``turn_index`` and before the first probe turn:
    the sum of ``turn_tokens(load_bytes)`` over them."""
    return sum(turn_tokens(load_bytes(script, i)) for i in range(turn_index + 1, script.n_content))


def estimate(script: Script) -> dict[str, int]:
    """Sizes of the script. An estimate, not a count: there is no tokenizer without a model.

    ``history_tokens`` is the content turns at 1.8 bytes per token (the rate M0 measured for this log
    text) plus ``REPLY_TOKENS`` per turn, which for a d27 script is the acceptance budget's
    ``history_tokens``. ``probe_tokens`` is the probe turns' text at 3.5 bytes per token. ``bytes``
    is every turn's text plus the files.
    """
    history = sum(turn_tokens(load_bytes(script, i)) + REPLY_TOKENS for i in range(script.n_content))
    probe = sum(int(b / d27.BYTES_PER_TOKEN_TEXT) + 1 for b in script.probe_bytes)
    file_bytes = sum(len(text.encode("utf-8")) for text in script.files.values())
    return {"turns": len(script.turns), "content_turns": script.n_content,
            "probe_turns": len(script.probe_turn_ids), "bytes": sum(script.turn_bytes) + file_bytes,
            "file_bytes": file_bytes, "history_tokens": history, "probe_tokens": probe, "tokens": history + probe}


def overlap_by_class(script: Script) -> dict[str, dict[str, float]]:
    """Per probe class, in script order: the number of probes and their mean lexical overlap."""
    out: dict[str, dict[str, float]] = {}
    for p in script.probes:
        cell = out.setdefault(p.cls, {"probes": 0, "overlap_sum": 0})
        cell["probes"] += 1
        cell["overlap_sum"] += p.overlap
    return {cls: {"probes": c["probes"], "mean_overlap": c["overlap_sum"] / c["probes"]} for cls, c in out.items()}


# Self-checks. Each returns the problems it found; an empty list passes.


def plant_turns(script: Script, probe: Probe) -> list[int]:
    """For each of the probe's statements, the content turn that plants it: the turn whose text holds
    it, the ``toolloop`` turn when a file holds it, or the ``decision`` turn when it is that turn's
    scripted reply. A statement planted nowhere is left out."""
    found: list[int] = []
    replies = {d.reply: d.turn for d in script.decisions}
    for statement in probe.statements:
        turn = next((i for i in range(script.n_content) if statement in script.turns[i]), None)
        if turn is None and any(statement in script.files[path] for path in script.reads):
            turn = script.toolloop_turn
        if turn is None:
            turn = replies.get(statement)
        if turn is not None:
            found.append(turn)
    return found


def unplanted(script: Script) -> list[str]:
    """Probes with a statement that no content turn, file or scripted reply holds."""
    return [f"{p.id}: {len(p.statements) - len(plant_turns(script, p))} of {len(p.statements)} statements are "
            "planted nowhere" for p in script.probes if len(plant_turns(script, p)) != len(p.statements)]


def placement(script: Script) -> list[str]:
    """New-class plants followed by fewer than ``script.bound`` tokens of content turns
    (``later_tokens``). A d27 script has no new-class probes, so it always passes."""
    problems = []
    for p in script.probes:
        if p.cls not in NEW_CLASSES:
            continue
        turns = plant_turns(script, p)
        if not turns:
            problems.append(f"{p.id}: planted nowhere")
            continue
        later = later_tokens(script, max(turns))
        if later < script.bound:
            problems.append(f"{p.id}: planted in turn {max(turns) + 1} with {later:,} tokens of content turns "
                            f"after it, under the bound {script.bound:,}")
    return problems


def _cfg_key(line: str) -> str | None:
    match = _CFG_LINE.match(line)
    return match.group(1) if match else None


def restatements(script: Script) -> list[str]:
    """Plant subjects that appear outside their own statements, in any turn or file.

    A d27 probe's own question may name its subject, as in the spike. A new-class probe's question
    may not: it is not excused here, so a subject in it is reported. For a ``paraphrase`` or
    ``toolloop`` probe a ``cfg`` line with another key is not a restatement: its service's decoys
    share the service, and another service's line may use the same attribute. Its own key may
    appear in no line but its plant, so exactly one line answers its question.
    """
    texts = [(f"turn {i + 1}", text, i >= script.n_content) for i, text in enumerate(script.turns)]
    texts += [(f"file {path}", text, False) for path, text in script.files.items()]
    problems = []
    for p in script.probes:
        for where, text, is_probe_turn in texts:
            if not any(subject in text for subject in p.subjects):
                continue
            residue = text
            for statement in p.statements:
                residue = residue.replace(statement, "")
            if p.cls in CFG_CLASSES:
                residue = "\n".join(line for line in residue.split("\n")
                                     if _cfg_key(line) in (None, p.subjects[0]))
            if is_probe_turn and p.cls not in NEW_CLASSES:
                residue = residue.replace(p.question, "")
            for subject in p.subjects:
                if subject in residue:
                    problems.append(f"{p.id}: {subject!r} restated in {where}")
    return problems


def key_free(script: Script) -> list[Probe]:
    """The new-class probes whose question contains one of their subject strings (for a ``cfg``
    probe: the key, the hyphenated service or the attribute identifier). A ``paraphrase`` or
    ``toolloop`` question names its service in plain words by design; ``service_alone`` checks
    that this does not single out the plant."""
    return [p for p in script.probes if p.cls in NEW_CLASSES and any(s in p.question for s in p.subjects)]


def service_lines(script: Script, service: str) -> list[str]:
    """The lines of the content turns and files that a search for ``service`` alone finds: lines
    that hold it hyphenated, or hold each of its separate words (as substrings, case folded, the way
    Contexto's search matches a query's words)."""
    parts = service.casefold().split("-")
    found = []
    for text in [*script.content_turns, *script.files.values()]:
        for line in text.split("\n"):
            folded = line.casefold()
            if service.casefold() in folded or all(part in folded for part in parts):
                found.append(line)
    return found


def service_alone(script: Script) -> list[Probe]:
    """The ``paraphrase`` and ``toolloop`` probes whose service name, hyphenated or as its separate
    words, finds no line but the probe's own plant (``service_lines``): for them the question is
    answered by the service name and the paraphrase decides nothing."""
    return [p for p in script.probes if p.cls in CFG_CLASSES
            and set(service_lines(script, p.subjects[1])) <= set(p.statements)]


def checks(script: Script) -> dict[str, list[str]]:
    """Every self-check by name. The script is sound when every list is empty."""
    return {
        "restatements": restatements(script),
        "key_free": [f"{p.id}: the question contains a subject string" for p in key_free(script)],
        "service_alone": [f"{p.id}: a search for {p.subjects[1]!r} alone finds only the plant line"
                          for p in service_alone(script)],
        "placement": placement(script),
        "unplanted": unplanted(script),
    }


# Scoring


def _norm_set(text: str | None) -> str:
    items = sorted({normalize(x) for x in re.split(r"[,\s]+", text or "") if normalize(x)})
    return ", ".join(items) if items else "none"


def score(probes: Sequence[Probe], answers_text: str) -> dict[str, Any]:
    """Score an answers text (``q_id: answer`` lines) against the probes. No model judge.

    The rules are the acceptance runs' (``spikes/m0/fidelity/l2f.py::score``) with three deliberate
    differences. First, a probe with no answer line, or an
    empty answer, is never correct. The spike reads a missing answer to a ``set`` probe as the empty
    set, so a ``set`` probe whose expected answer is ``none`` scored correct when the arm said nothing
    at all. Second, an answer line may be written in markdown (``d27.parse_answers``): a list or
    numbered item, with ``**``, ``__`` or backticks around the id, the separator or the answer, as in
    ``**q_a_01**: x``, ``1. q_a_01: x`` or ``- `q_a_01: x```; the spike dropped those lines, so an
    answer Claude Code wrote in markdown (its system prompt asks for it) counted as no answer. Third,
    a line in a blockquote (``> q_a_01: x``) is never an answer, and a line in a fenced code block
    yields to an answer for the same id outside any fence, so a note or file the reply quotes after
    its answers cannot override them (the spike read a line in a code fence like any other, and
    never a ``>`` line). A reply that puts all its answers in a code fence is still scored. Every
    other line the spike read is read the same, and ``normalize`` also trims ``*`` and ``_``, which no generated answer contains.
    Table rows (``| q_a_01 | x |``) and headings are not read. Otherwise unchanged: a ``value``
    answer is right when it equals the expected answer after normalization, a ``set`` answer when
    its ids are the expected ids in any order, and a wrong ``value`` answer that equals one of the
    probe's older values is also counted stale. Returns the totals, the Wilson 95% interval, the
    cells by type and by type and tier, and one row per probe in the order given.
    """
    answers = parse_answers(answers_text)
    cells: dict[str, dict[str, int]] = {}
    rows: list[dict[str, Any]] = []
    correct = 0
    for p in probes:
        given = answers.get(p.id)
        if not normalize(given):
            ok = False  # no answer is never right, also where the expected set is "none"
        elif p.kind == "set":
            ok = _norm_set(given) == _norm_set(p.answer)
        else:
            ok = normalize(given) == normalize(p.answer)
        stale = (not ok) and p.kind == "value" and normalize(given) in {normalize(v) for v in p.older}
        for key in (p.type, f"{p.type}/{p.tier}"):
            c = cells.setdefault(key, {"correct": 0, "total": 0, "stale": 0})
            c["total"] += 1
            c["correct"] += int(ok)
            c["stale"] += int(stale)
        correct += int(ok)
        rows.append({"id": p.id, "cls": p.cls, "type": p.type, "tier": p.tier, "kind": p.kind,
                     "expected": p.answer, "given": given, "correct": ok, "stale": stale})
    return {"correct": correct, "total": len(rows), "interval": wilson(correct, len(rows)),
            "cells": dict(sorted(cells.items())), "rows": rows}
