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
``concluded``
    Six short ``decision`` turns each give three candidates with a number each and a rule (take the
    lowest, or the highest) and ask for a reply of exactly ``DECISION <label>: <name>``. The fact
    exists in the conversation only as the assistant's reply. The question describes what was
    decided in other words (``SCENARIOS``) and contains neither the label nor a candidate.
    ``decision_reply_ok`` tells whether a reply made the right decision, so a reader can tell a
    wrong conclusion from a failed recall. Scoring is against the generator's value either way.
``toolloop``
    One turn asks the assistant to read ``TOOLLOOP_FILES`` (10) generated files, one at a time, with
    its file-read tool and then reply OK. The files hold log filler and sum to about twice the
    content turn size (10 files of about 8,000 bytes, 80,000 bytes in all, at the default 40,000
    byte turn). Six of them carry one ``cfg`` fact each, asked about as in ``paraphrase``. The
    script carries the files (``Script.files``) and the order the turn lists them in
    (``Script.reads``).

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
from contexto.bench.d27 import DEFAULT_SEED, REPLY_TOKENS, normalize, parse_answers, turn_tokens
from contexto.bench.model import (
    D27_CLASSES,
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
    "ATTRIBUTES", "D27_CLASSES", "DEFAULT_SEED", "NEW_CLASSES", "PROTOCOLS", "SCENARIOS", "Decision", "Probe",
    "Script", "build", "checks", "decision_reply_ok", "estimate", "key_free", "later_tokens", "lexical_overlap",
    "load_bytes", "overlap_by_class", "placement", "plant_turns", "restatements", "score", "unplanted", "words",
]

PER_CLASS = 6  # probes in each new class
NEW_PROBES_PER_TURN = 9
TOOLLOOP_FILES = 10
TOOLLOOP_TURNS_WORTH = 2  # the files sum to this many content turns' bytes
MAX_FILE_BYTES = 20_000  # more files, not larger ones, above this: a file-read tool caps what one read returns
FILE_DIR = "bench-files"

# The synonym table: an attribute identifier and a clause that asks for it without any of its words.
# ``{svc}`` is the service in plain words. tests/test_bench_script.py checks that no clause shares a
# word with its identifier.
ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("failover_target", "where does the {svc} service send its traffic when its primary goes down?"),
    ("image_tag", "which container build label is the {svc} service running?"),
    ("oncall_alias", "what is the paging name of whoever is on duty for the {svc} service?"),
    ("backup_bucket", "where are the nightly copies of the {svc} service's data stored?"),
    ("signing_secret", "what does the {svc} service use to prove that its requests are authentic?"),
    ("queue_name", "from which work line does the {svc} service pull its jobs?"),
    ("cert_serial", "what number identifies the TLS certificate of the {svc} service?"),
    ("dns_zone", "under which domain are the host names of the {svc} service registered?"),
    ("cache_namespace", "what prefix does the {svc} service put in front of its stored lookups?"),
    ("log_stream", "where does the {svc} service write its diagnostic output?"),
    ("release_channel", "which update track is the {svc} service subscribed to?"),
    ("schema_revision", "which version of the database layout is the {svc} service on?"),
    ("throttle_tier", "which rate limiting class is the {svc} service assigned?"),
    ("feature_set", "which bundle of optional behaviours is switched on for the {svc} service?"),
    ("standby_pool", "which group of spare machines backs up the {svc} service?"),
    ("alert_route", "where are warnings about the {svc} service delivered?"),
)

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
_DECISION_LINE = re.compile(r"^\W*DECISION\s+([A-Za-z0-9-]+)\s*:\s*(.*?)\s*$")


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
              statements: tuple[str, ...]) -> Probe:
        return Probe(pid, "b", cls, tier, question, answer, "value", (), subjects, statements, cls=cls,
                     overlap=lexical_overlap(question, statements))

    def fact(pid: str, cls: str, tier: str, service: str, attribute: tuple[str, str], source: str) -> Probe:
        name, clause = attribute
        key, answer = f"{service}.{name}", value()
        question = f"From the {source} earlier in this conversation: {clause.format(svc=_plain(service))} " \
                   "Answer with the value only."
        return probe(pid, cls, tier, question, answer, (key, service, name), (f"cfg {key}={answer}",))

    services = rng.sample(sorted(pools.services), 2 * PER_CLASS)  # the services d27 left unused
    attributes = rng.sample(ATTRIBUTES, 2 * PER_CLASS)

    # paraphrase: one more pasted-log turn.
    paraphrase = [fact(f"q_r_{i + 1:02d}", "paraphrase", "cfg-line", services[i], attributes[i], "logs")
                  for i in range(PER_CLASS)]
    new_turns: list[tuple[str, str, Any]] = [
        ("log", d27.log_turn(rng, [p.statements[0] for p in paraphrase], size, "p0"), None)]

    # toolloop: files of log filler, six of them with one fact each.
    toolloop = [fact(f"q_t_{i + 1:02d}", "toolloop", "file-line", services[PER_CLASS + i],
                     attributes[PER_CLASS + i], "files you read")
                for i in range(PER_CLASS)]
    n_files = max(TOOLLOOP_FILES, math.ceil(TOOLLOOP_TURNS_WORTH * size / MAX_FILE_BYTES))
    holders = rng.sample(range(n_files), PER_CLASS)
    files: dict[str, str] = {}
    for k in range(n_files):
        planted = [toolloop[i].statements[0] for i in range(PER_CLASS) if holders[i] == k]
        used = sum(len(x.encode("utf-8")) + 1 for x in planted)
        lines = max(1, int((TOOLLOOP_TURNS_WORTH * size / n_files - used) / d27.filler_line_bytes()))
        log = d27.filler_text(rng, lines, f"f{k + 1:02d}").split("\n")
        for item in planted:
            log.insert(rng.randrange(0, len(log) + 1), item)
        files[f"{FILE_DIR}/log-{k + 1:02d}.txt"] = "\n".join(log) + "\n"
    reads = tuple(files)
    new_turns.append(("toolloop", "\n".join([
        f"Next, {n_files} files instead of a pasted log. Read each file below with your file-read tool, one file "
        "at a time, in the order listed, each one to its end. Do not search them and do not skip any. The paths "
        f"are relative to the current working directory. When you have read all {n_files}, reply with exactly: OK",
        *reads]), None))

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
            "Decision point: this message has no log.", intro,
            *[f"{name} = {number}" for name, number in zip(names, numbers)],
            f"Rule: take the one with the {_RULE_WORD[rule]} number.",
            "Reply with exactly one line and nothing else, in this form:",
            f"DECISION {label}: <name>",
        ])
        reply = f"DECISION {label}: {pick}"
        pid = f"q_c_{i + 1:02d}"
        concluded.append(probe(pid, "concluded", rule, f"{question} Answer with the value only.", pick,
                               (label, *names), (text, reply)))
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

    True when the reply's last ``DECISION <label>: <name>`` line for the turn's label gives the name
    the rule selects, compared after the scoring's normalization; markup around the line (``**``,
    backticks, a list dash) is ignored. Raises ``ValueError`` if that turn is not a decision turn.
    """
    decision = next((d for d in script.decisions if d.turn == turn_index), None)
    if decision is None:
        raise ValueError(f"turn {turn_index} is not a decision turn")
    given = None
    for line in reply_text.splitlines():
        match = _DECISION_LINE.match(line)
        if match and match.group(1) == decision.label:
            given = match.group(2)
    return given is not None and normalize(given.strip("*_ ")) == normalize(decision.value)


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


def restatements(script: Script) -> list[str]:
    """Plant subjects that appear outside their own statements, in any turn or file.

    A d27 probe's own question may name its subject, as in the spike. A new-class probe's question
    may not: it is not excused here, so a subject in it is reported.
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
            if is_probe_turn and p.cls not in NEW_CLASSES:
                residue = residue.replace(p.question, "")
            for subject in p.subjects:
                if subject in residue:
                    problems.append(f"{p.id}: {subject!r} restated in {where}")
    return problems


def key_free(script: Script) -> list[Probe]:
    """The new-class probes whose question contains one of their subject strings."""
    return [p for p in script.probes if p.cls in NEW_CLASSES and any(s in p.question for s in p.subjects)]


def checks(script: Script) -> dict[str, list[str]]:
    """Every self-check by name. The script is sound when every list is empty."""
    return {
        "restatements": restatements(script),
        "key_free": [f"{p.id}: the question contains a subject string" for p in key_free(script)],
        "placement": placement(script),
        "unplanted": unplanted(script),
    }


# Scoring


def _norm_set(text: str | None) -> str:
    items = sorted({normalize(x) for x in re.split(r"[,\s]+", text or "") if normalize(x)})
    return ", ".join(items) if items else "none"


def score(probes: Sequence[Probe], answers_text: str) -> dict[str, Any]:
    """Score an answers text (``q_id: answer`` lines) against the probes. No model judge.

    The rules are the acceptance runs' (``spikes/m0/fidelity/l2f.py::score``), unchanged: a ``value``
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
        ok = (_norm_set(given) == _norm_set(p.answer)) if p.kind == "set" else (normalize(given) == normalize(p.answer))
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
