"""The benchmark's script generator (D66): protocol d27 is the spike's script byte for byte, protocol
v2 adds three probe classes without touching it, and the self-checks and scoring hold.

Everything here is generated from a seed; no content from a real session is used.
"""

from __future__ import annotations

import dataclasses
import hashlib
import random
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from contexto.bench import d27, script
from contexto.bench.model import NEW_CLASSES, Probe, Script, lexical_overlap, words

SEED = 20261016
# SHA-256 of each turn of the d27 script for seed 20261016 at the default bound (150,000) and turn
# size (40,000 bytes): 18 content turns, then 6 probe turns. Python's random module documents that
# only random() is reproducible across versions; these pins make a change in shuffle, sample, choice,
# choices or randrange on any Python version CI runs fail here instead of changing the benchmark.
D27_TURN_DIGESTS = (
    "826c6394768eb879193fdf964e5d2b912b821dab5edb8b68352b6f42fd47fd81",
    "36494950595c52daf773f953133faa6dfbaa745c78d864310a1ab2789f0b9fbf",
    "262a5f32d6285baeabcd35f7b6f35f9f3044823c31b19119d2de3b287cf46f24",
    "903f9da916b05f247705fe6509ae709aa315c2dbb23121b2c9b9c21405bec3c5",
    "c2f7861f69e5ef5c4488fbcb19639b50f876d64401d83db6cb001e3ae15eb2f4",
    "1cd63cb5cb8a0f2ad30f7d3a5b5d570e638d13164c12de1b6cbb55b0ecdbd937",
    "ec6f31218906d72292b3bb0df7de6e16135f2fee7de7c26f0a28a0c53e26bb76",
    "8e943f477a252cb37b5b4e575dcbb9814afe4ae954ccb984e913aba4950beda0",
    "52c139c5bccdf51ae750a22e4babdb4f1b6e00c7b7e97699df4a12e5dcc4e57a",
    "a86cc59e1ab838ba3dfd4afb23e0beb90eb8f13606c3eae9f12c63abb4bd3329",
    "f4e2094f5d419a793d05bdd191084794efdc379c3654b7b5bf6f86a9dde3a3a2",
    "1c94dcaa11d296b3494afdd50e5edb4c749fe0af9d38ee87b91e6b0176bd73e1",
    "45644dcaa06b334a221172a9fff8933e5b45ab0dd55bf9f1c46fa76f36547810",
    "8d173885aa4f421363a074d8126d3af1d178419e0df6a011f4af809b55ab8131",
    "aea6f5ee69d9688b142e4031627a8f8d0760dea4a8e6258227137102414a2682",
    "fe58bba1bf1753378a809ed72c8e1b2a9047c37e9c3d4f1604edd3c74a7017db",
    "ddc26244955f9abc8dbcf2e5f49967e7b9c30137c82f90df42fa2ec8410b0162",
    "03006f30554b550e0b369bcc1bd0b9b11fb4329e8569c5e87bb0176ef7aed1e0",
    "6d0051b68a37a1c57e505c11bf964636890ee7ede2a3f7d2bac0a3d3ef52c19c",
    "1a25616cf7762f8b3885f23839ebb09d3184ecf9b2681c7ed50972aa78cd1ba5",
    "9af4112458d9b8cb37a9bf2ef984ef2cbd794c4b0e10be692c5be18a44a28fb0",
    "865811bd8377dd44b1466d424dacb8351ae47113856d78f0c962b9a60700b9cf",
    "d6a8698a2c27c10c8290ead8223513041ec308654ea6810c91ed2e84738bd97c",
    "35ba176a6ae52def07325b3c86cdaec57bc03bad93c20d0baf3e90c261ad92da",
)
# SHA-256 of the whole v2 script's JSON for the same seed and defaults: it pins the new plants'
# random stream (seeded with a string) and every word of the new turns and questions. A deliberate
# change to the v2 protocol changes this value, and with it any recorded trace that pins the script.
V2_SCRIPT_DIGEST = "eb77c4f824c07ff720b7420cb7593d5e42aed03a81ba80481ebb3fd6687e2248"
SPIKE_PROBE_FIELDS = ("id", "group", "type", "tier", "question", "answer", "kind", "older", "subjects", "statements")
SEEDS = (20261016, 20261017, 1, 999)
BOUNDS = (150_000, 100_000)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def spike():
    """The acceptance spike's modules, imported the way the spike's own tests import them."""
    spikes = Path(__file__).resolve().parents[1] / "spikes"
    for path in (spikes / "m2", spikes / "m0"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from acceptance import budget, run
    from acceptance import script as acceptance_script
    from fidelity import l2f

    return {"budget": budget, "run": run, "script": acceptance_script, "l2f": l2f}


@pytest.fixture(scope="module")
def v2() -> Script:
    return script.build(SEED)


@pytest.fixture(scope="module")
def base() -> Script:
    return script.build(SEED, "d27")


def _new(s: Script, cls: str) -> list[Probe]:
    return [p for p in s.probes if p.cls == cls]


# Protocol d27: the port


def _assert_same_as_spike(ours: Script, theirs) -> None:
    assert ours.turns == theirs.turns
    assert ours.turn_bytes == theirs.turn_bytes
    assert ours.paged_only == theirs.paged_only
    assert ours.probe_turn_ids == theirs.probe_turn_ids
    assert len(ours.probes) == len(theirs.probes) == 54
    for mine, spike_probe in zip(ours.probes, theirs.probes):
        for name in SPIKE_PROBE_FIELDS:
            assert getattr(mine, name) == getattr(spike_probe, name), (mine.id, name)


@pytest.mark.parametrize("bound", BOUNDS)
@pytest.mark.parametrize("seed", SEEDS)
def test_d27_is_byte_identical_to_the_spike(spike, seed, bound):
    theirs = spike["run"].build_script(seed, 0, 40_000, spike["budget"].Params(bound=bound))
    _assert_same_as_spike(script.build(seed, "d27", bound=bound, turn_bytes=40_000), theirs)


@pytest.mark.parametrize("seed, turns, turn_bytes", [(SEED, 17, 30_000), (7, 0, 70_000), (999, 4, 2_000)])
def test_d27_is_byte_identical_to_the_spike_at_other_sizes(spike, seed, turns, turn_bytes):
    theirs = spike["run"].build_script(seed, turns, turn_bytes, spike["budget"].Params(bound=150_000))
    ours = script.build(seed, "d27", bound=150_000, turn_bytes=turn_bytes, turns=turns)
    _assert_same_as_spike(ours, theirs)
    if turns:
        assert ours.n_content == turns


def test_d27_turn_digests_are_pinned(spike, base):
    assert tuple(_sha(t) for t in base.turns) == D27_TURN_DIGESTS
    theirs = spike["run"].build_script(SEED, 0, 40_000, spike["budget"].Params(bound=150_000))
    assert tuple(_sha(t) for t in theirs.turns) == D27_TURN_DIGESTS  # the spike itself, on this Python


def test_d27_token_estimate_is_the_spike_budgets_history(spike, base):
    budget = spike["budget"]
    simulated = budget.simulate(list(base.content_bytes), list(base.probe_bytes), budget.Params(bound=150_000))
    sizes = script.estimate(base)
    assert sizes["history_tokens"] == simulated.history_tokens
    assert sizes["history_tokens"] >= 2.5 * 150_000
    assert d27.turn_tokens(40_000) == budget.turn_tokens(40_000) == 22_223
    assert sizes == {"turns": 24, "content_turns": 18, "probe_turns": 6, "bytes": sum(base.turn_bytes),
                     "file_bytes": 0, "history_tokens": sizes["history_tokens"],
                     "probe_tokens": sizes["tokens"] - sizes["history_tokens"], "tokens": sizes["tokens"]}


def test_d27_script_shape(base):
    assert base.protocol == "d27" and base.bound == 150_000 and base.turn_size == 40_000
    assert base.kinds == ("log",) * 18 + ("probe",) * 6
    assert base.added_turns == () and base.decisions == () and base.files == {} and base.reads == ()
    assert base.toolloop_turn is None
    assert all(p.cls == p.type for p in base.probes)
    assert script.checks(base) == {"restatements": [], "key_free": [], "placement": [], "unplanted": []}
    assert [base.scripted_reply(i) for i in (0, 17, 18)] == ["OK", "OK", None]


def test_the_package_does_not_import_the_spikes():
    package = Path(script.__file__).resolve().parent
    for path in [*package.rglob("*.py"), package.parent / "commands" / "bench.py"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+(spikes|acceptance|fidelity)\b", text, re.M), path
        assert "sys.path" not in text, path


def test_build_refuses_an_unknown_protocol_and_too_few_turns():
    with pytest.raises(ValueError, match="unknown protocol"):
        script.build(SEED, "v3")
    with pytest.raises(ValueError, match="at least 4"):
        script.build(SEED, "d27", turns=3)


# JSON


@pytest.mark.parametrize("protocol", ["d27", "v2"])
def test_script_round_trips_through_json(protocol, tmp_path):
    s = script.build(SEED, protocol, bound=20_000, turn_bytes=4_000)
    text = s.to_json()
    assert text == s.to_json() and text.endswith("\n")
    again = Script.from_json(text)
    assert again == s and again.to_json() == text and again.digest() == s.digest()
    path = tmp_path / "scripts" / "seed-1.json"
    s.write(path)
    assert Script.read(path) == s
    for p in s.probes:
        assert Probe.from_json(p.to_json()) == p


def test_script_json_shape(v2):
    data = v2.to_dict()
    assert list(data) == ["format", "protocol", "seed", "bound", "turn_size", "turns", "kinds", "turn_bytes",
                          "added_turns", "probes", "paged_only", "probe_turn_ids", "decisions", "files", "reads"]
    assert data["format"] == 1 and data["protocol"] == "v2" and data["seed"] == SEED
    assert list(data["probes"][0]) == ["id", "group", "type", "tier", "question", "answer", "kind", "older",
                                       "subjects", "statements", "cls", "overlap"]
    assert list(data["decisions"][0]) == ["turn", "probe", "label", "value", "reply"]
    assert len(data["turns"]) == len(data["kinds"]) == len(data["turn_bytes"]) == 34


@pytest.mark.parametrize("text, message", [
    ("{", "not JSON"),
    ("[]", "format 1"),
    ('{"format": 2}', "format 1"),
    ('{"format": 1, "seed": 1}', "not a benchmark script"),
])
def test_from_json_refuses_what_is_not_a_script(text, message):
    with pytest.raises(ValueError, match=message):
        Script.from_json(text)


def test_from_json_refuses_a_malformed_probe(v2):
    data = v2.to_dict()
    data["probes"][0] = {**data["probes"][0], "statements": "one string, not a list"}
    with pytest.raises(ValueError, match="not a benchmark probe"):
        Script.from_dict(data)


# Protocol v2


def test_v2_keeps_every_d27_turn_and_probe(v2, base):
    kept = tuple(t for i, t in enumerate(v2.turns) if i not in v2.added_turns)
    assert kept == base.turns
    assert v2.probes[:54] == base.probes
    assert v2.probe_turn_ids[:6] == base.probe_turn_ids
    assert v2.probe_texts[:6] == base.probe_texts
    assert v2.paged_only == base.paged_only
    assert v2.turns[0] == base.turns[0]  # the paged-only plants still open the session


def test_v2_adds_six_probes_of_each_new_class(v2):
    assert len(v2.probes) == 72
    counts = Counter(p.cls for p in v2.probes)
    assert {cls: counts[cls] for cls in NEW_CLASSES} == {"paraphrase": 6, "concluded": 6, "toolloop": 6}
    new = v2.probes[54:]
    assert [p.id for p in new] == ([f"q_r_{i:02d}" for i in range(1, 7)] + [f"q_c_{i:02d}" for i in range(1, 7)]
                                   + [f"q_t_{i:02d}" for i in range(1, 7)])
    assert all(p.cls == p.type and p.group == "b" and p.kind == "value" for p in new)
    assert len({p.id for p in v2.probes}) == 72
    assert len({p.answer for p in new}) == 18


def test_v2_probe_turns_follow_the_written_assignment(v2):
    assert len(v2.probe_turn_ids) == 8 and [len(ids) for ids in v2.probe_turn_ids] == [9] * 8
    order = [p.id for p in v2.probes[54:]]
    random.Random(f"contexto-bench/v2/{SEED}/probe-order").shuffle(order)
    assert v2.probe_turn_ids[6:] == (tuple(order[:9]), tuple(order[9:]))
    by_id = {p.id: p for p in v2.probes}
    for ids, text in zip(v2.probe_turn_ids, v2.probe_texts):
        assert text == "\n".join([*d27.PROBE_HEADER, "", *[f"{i}: {by_id[i].question}" for i in ids]])
    assert sorted(i for ids in v2.probe_turn_ids for i in ids) == sorted(by_id)


def test_v2_new_turns_form_one_block_after_the_first_turn(v2):
    assert v2.added_turns == (1, 2, 3, 4, 5, 6, 7, 8, 32, 33)
    assert Counter(v2.kinds[1:9]) == {"log": 1, "toolloop": 1, "decision": 6}
    assert v2.kinds[9:26] == ("log",) * 17 and v2.kinds[26:] == ("probe",) * 8
    assert v2.n_content == 26
    assert v2.turn_bytes == tuple(len(t.encode("utf-8")) for t in v2.turns)


def test_v2_script_digest_is_pinned(v2):
    assert v2.digest() == V2_SCRIPT_DIGEST
    assert script.build(SEED).digest() == V2_SCRIPT_DIGEST  # deterministic


@pytest.mark.parametrize("bound", BOUNDS)
@pytest.mark.parametrize("seed", SEEDS)
def test_v2_self_checks_pass(seed, bound):
    s = script.build(seed, "v2", bound=bound)
    assert script.checks(s) == {"restatements": [], "key_free": [], "placement": [], "unplanted": []}
    assert script.key_free(s) == [] and script.restatements(s) == []
    for p in s.probes[54:]:
        assert script.later_tokens(s, max(script.plant_turns(s, p))) >= bound


def test_v2_small_script_for_stub_runs_is_sound():
    """The sizes the other bench tests use against stub CLIs: a 4,000-token bound and 2,000-byte turns."""
    s = script.build(3, "v2", bound=4_000, turn_bytes=2_000)
    assert not any(script.checks(s).values())
    assert len(s.probes) == 72 and len(s.files) == 10
    assert script.estimate(s)["bytes"] < 200_000


def test_v2_refuses_a_script_too_short_to_bury_its_new_plants():
    with pytest.raises(ValueError, match="too short for protocol v2"):
        script.build(SEED, "v2", bound=150_000, turn_bytes=40_000, turns=4)
    assert script.build(SEED, "d27", bound=150_000, turn_bytes=40_000, turns=4).n_content == 4


def test_new_plants_do_not_collide_with_d27(v2, base):
    d27_text = "\n".join(base.turns)
    d27_values = {v for p in base.probes for v in (p.answer, *p.older)}
    for p in v2.probes[54:]:
        assert p.answer not in d27_values and p.answer not in d27_text
        for subject in p.subjects:
            assert subject not in d27_text, (p.id, subject)
    # The d27 stream is untouched: building v2 does not change what d27 draws.
    assert script.build(SEED, "d27").turns == base.turns


def test_new_class_questions_contain_no_subject_string(v2):
    for p in v2.probes[54:]:
        assert p.subjects, p.id
        for subject in p.subjects:
            assert subject not in p.question, (p.id, subject)
        assert p.answer not in p.question


# paraphrase


def test_paraphrase_plants_are_log_lines_asked_about_in_other_words(v2):
    probes = _new(v2, "paraphrase")
    log_turn = next(i for i in v2.added_turns if v2.kinds[i] == "log")
    lines = v2.turns[log_turn].split("\n")
    assert lines[0] == d27.LOG_HEAD and lines[-1] == d27.LOG_TAIL
    assert 39_000 <= v2.turn_bytes[log_turn] <= 40_000  # one content turn, sized as d27 sizes its own
    attributes = set()
    for p in probes:
        key, service, attribute = p.subjects
        assert key == f"{service}.{attribute}" and p.statements == (f"cfg {key}={p.answer}",)
        assert lines.count(p.statements[0]) == 1
        assert service.replace("-", " ") in p.question  # the service, in plain words
        assert script.plant_turns(v2, p) == [log_turn]
        attributes.add(attribute)
    assert len(attributes) >= 4
    positions = [lines.index(p.statements[0]) for p in probes]
    assert positions == sorted(positions)  # spread through the log in plant order


def test_no_attribute_clause_shares_a_word_with_its_identifier():
    names = [name for name, _ in script.ATTRIBUTES]
    assert len(set(names)) == len(names) >= 12
    for name, clause in script.ATTRIBUTES:
        assert "{svc}" in clause and clause.endswith("?")
        identifier_words = set(re.findall(r"[a-z0-9]+", name))
        clause_words = set(re.findall(r"[a-z0-9]+", clause.casefold()))
        assert not identifier_words & clause_words, name
        assert name not in d27_identifiers()


def d27_identifiers() -> set[str]:
    """The attribute identifiers d27's config lines use."""
    return {"retention_days", "map_version"}


# concluded


def _decide(turn_text: str) -> str:
    """Work a decision turn out from its text alone, as a reader would."""
    candidates = dict(re.findall(r"^([a-z]+-\d{4}) = (\d+)$", turn_text, re.M))
    assert len(candidates) == 3 and len(set(candidates.values())) == 3
    rule = re.search(r"^Rule: take the one with the (lowest|highest) number\.$", turn_text, re.M).group(1)
    pick = min if rule == "lowest" else max
    return pick(candidates, key=lambda name: int(candidates[name]))


def test_concluded_answers_follow_from_their_turn_alone(v2):
    probes = {p.id: p for p in _new(v2, "concluded")}
    assert len(v2.decisions) == 6 and {d.probe for d in v2.decisions} == set(probes)
    assert len({d.label for d in v2.decisions}) == 6
    for d in v2.decisions:
        p = probes[d.probe]
        text = v2.turns[d.turn]
        assert v2.kinds[d.turn] == "decision" and len(text) < 600
        assert _decide(text) == d.value == p.answer
        assert d.reply == f"DECISION {d.label}: {d.value}" and text.endswith(f"DECISION {d.label}: <name>")
        assert p.statements == (text, d.reply) and p.subjects[0] == d.label and d.value in p.subjects[1:]
        assert d.reply not in "\n".join(v2.turns)  # the fact is only ever the assistant's reply
        assert v2.scripted_reply(d.turn) == d.reply
        assert script.plant_turns(v2, p) == [d.turn, d.turn]
    assert Counter(p.tier for p in probes.values()) == {"min": 3, "max": 3}
    assert len({p.question for p in probes.values()}) == 6


def test_scenarios_describe_the_decision_in_other_words():
    assert len(script.SCENARIOS) == 6 and len({s[0] for s in script.SCENARIOS}) == 6
    for _, rule, intro, question in script.SCENARIOS:
        assert rule in ("min", "max")
        assert not words(question) & words(intro), question


def test_decision_reply_ok(v2):
    d = v2.decisions[0]
    others = [name for name in next(p for p in v2.probes if p.id == d.probe).subjects[1:] if name != d.value]
    assert script.decision_reply_ok(v2, d.turn, d.reply)
    assert script.decision_reply_ok(v2, d.turn, f"Thinking it over.\n**{d.reply}**\n")
    assert script.decision_reply_ok(v2, d.turn, f"- `DECISION {d.label}: {d.value.upper()}`.")
    assert script.decision_reply_ok(v2, d.turn, f"DECISION {d.label}: {others[0]}\nDECISION {d.label}: {d.value}")
    assert not script.decision_reply_ok(v2, d.turn, f"DECISION {d.label}: {others[0]}")
    assert not script.decision_reply_ok(v2, d.turn, f"DECISION {d.label}: {d.value} or {others[0]}")
    assert not script.decision_reply_ok(v2, d.turn, f"DECISION DX-0000: {d.value}")  # another label
    assert not script.decision_reply_ok(v2, d.turn, "OK")
    assert not script.decision_reply_ok(v2, d.turn, "")
    with pytest.raises(ValueError, match="not a decision turn"):
        script.decision_reply_ok(v2, 0, d.reply)


# toolloop


def test_toolloop_turn_reads_ten_files_holding_six_facts(v2):
    turn = v2.toolloop_turn
    assert turn in v2.added_turns and v2.kinds[turn] == "toolloop" and v2.scripted_reply(turn) == "OK"
    lines = v2.turns[turn].split("\n")
    assert tuple(lines[1:]) == v2.reads == tuple(v2.files)  # the order the turn lists them is the read order
    assert len(v2.reads) == 10 >= 8 and "reply with exactly: OK" in lines[0]
    assert all(re.fullmatch(r"bench-files/log-\d\d\.txt", path) for path in v2.reads)
    sizes = [len(text.encode("utf-8")) for text in v2.files.values()]
    assert all(7_800 <= n <= 8_200 for n in sizes)  # about 8,000 bytes each
    assert abs(sum(sizes) - 2 * 40_000) < 800  # two content turns' worth
    assert script.load_bytes(v2, turn) == v2.turn_bytes[turn] + sum(sizes)
    holders = []
    for p in _new(v2, "toolloop"):
        key, service, attribute = p.subjects
        assert p.statements == (f"cfg {key}={p.answer}",)
        holding = [path for path, text in v2.files.items() if p.statements[0] in text.split("\n")]
        assert len(holding) == 1
        holders.append(holding[0])
        assert p.statements[0] not in "\n".join(v2.turns)  # only in a file
        assert script.plant_turns(v2, p) == [turn]
    assert len(set(holders)) == 6  # six files hold one fact each; the other four are filler only
    used = {p.subjects[2] for p in _new(v2, "toolloop")} | {p.subjects[2] for p in _new(v2, "paraphrase")}
    assert len(used) == 12  # no attribute is planted twice


def test_toolloop_uses_more_files_not_larger_ones_for_a_large_turn():
    s = script.build(5, "v2", bound=150_000, turn_bytes=120_000)
    assert len(s.files) == 12 and max(len(t.encode("utf-8")) for t in s.files.values()) < 20_300
    assert not any(script.checks(s).values())


def test_estimate_counts_the_files(v2, base):
    ours, theirs = script.estimate(v2), script.estimate(base)
    assert ours["file_bytes"] == sum(len(t.encode("utf-8")) for t in v2.files.values())
    assert ours["bytes"] == sum(v2.turn_bytes) + ours["file_bytes"]
    assert (ours["turns"], ours["content_turns"], ours["probe_turns"]) == (34, 26, 8)
    added = ours["history_tokens"] - theirs["history_tokens"]
    assert added == sum(d27.turn_tokens(script.load_bytes(v2, i)) + d27.REPLY_TOKENS for i in v2.added_turns[:8])
    assert 60_000 < added < 75_000  # one log turn, two turns' worth of files, six short decision turns


# Lexical overlap


def test_words_tokenisation():
    assert words("cfg billing-core.retention_days=amber-4417") == {"billing", "core", "retention", "days", "amber",
                                                                   "4417"}
    assert words("The API hub is OK; e2e ran.") == frozenset()  # every token is under four characters
    assert words("WHAT value did Billing have, with THIS?") == {"value", "billing"}  # case folded, stop words out
    assert words("") == frozenset()


def test_lexical_overlap_counts_distinct_shared_words():
    statement = "cfg billing-core.retention_days=amber-4417"
    assert lexical_overlap("What value did billing-core.retention_days have? billing billing", [statement]) == 4
    assert lexical_overlap("How long does the billing core service keep its data?", [statement]) == 2
    assert lexical_overlap("How long is data kept?", [statement]) == 0
    assert lexical_overlap("alpha beta", ["alpha one", "beta two"]) == 2  # across all the statements


def test_every_probe_carries_its_overlap_and_new_classes_are_lexically_harder(v2):
    for p in v2.probes:
        assert p.overlap == lexical_overlap(p.question, p.statements)
    by_class = script.overlap_by_class(v2)
    assert list(by_class) == ["paged_only", "multihop", "chain", "obligations", "distractor", *NEW_CLASSES]
    assert sum(c["probes"] for c in by_class.values()) == 72
    easiest_new = max(by_class[cls]["mean_overlap"] for cls in NEW_CLASSES)
    hardest_d27 = min(c["mean_overlap"] for cls, c in by_class.items() if cls not in NEW_CLASSES)
    assert easiest_new < hardest_d27
    assert by_class["concluded"]["mean_overlap"] == 0


# Self-checks catch what they are for


def _with_probe(s: Script, index: int, **changes) -> Script:
    probes = list(s.probes)
    probes[index] = dataclasses.replace(probes[index], **changes)
    return dataclasses.replace(s, probes=tuple(probes))


def test_key_free_and_restatements_catch_a_subject_in_a_new_class_question(v2):
    p = v2.probes[54]
    bad = _with_probe(v2, 54, question=f"What value did {p.subjects[0]} have?")
    assert [q.id for q in script.key_free(bad)] == [p.id]
    assert script.checks(bad)["key_free"] == [f"{p.id}: the question contains a subject string"]
    # The probe turn that asks it now restates the subject, and a new-class question is not excused.
    turns = list(v2.turns)
    asked = next(i for i, ids in enumerate(v2.probe_turn_ids) if p.id in ids) + v2.n_content
    turns[asked] = turns[asked].replace(p.question, bad.probes[54].question)
    restated = script.restatements(dataclasses.replace(bad, turns=tuple(turns)))
    assert restated and all(r.startswith(f"{p.id}: ") and f"turn {asked + 1}" in r for r in restated)


def test_restatements_catch_a_subject_in_another_turn_or_file(v2):
    p = _new(v2, "toolloop")[0]
    turns = list(v2.turns)
    turns[12] += f"\nnote: see {p.subjects[1]} again"
    assert script.restatements(dataclasses.replace(v2, turns=tuple(turns))) == [
        f"{p.id}: {p.subjects[1]!r} restated in turn 13"]
    q = _new(v2, "paraphrase")[0]
    path = v2.reads[0]
    files = {**v2.files, path: v2.files[path] + f"cfg {q.subjects[0]}=zzz-0000\n"}
    problems = script.restatements(dataclasses.replace(v2, files=files))
    assert problems and all(r.startswith(f"{q.id}: ") and r.endswith(f"restated in file {path}") for r in problems)
    # A d27 probe's own question may name its subject; anyone else's text may not.
    d = v2.probes[0]
    assert d.subjects[0] in d.question and script.restatements(v2) == []


def test_placement_and_unplanted_catch_a_shallow_or_missing_plant(v2):
    deeper = dataclasses.replace(v2, bound=450_000)
    problems = script.placement(deeper)
    assert len(problems) == 18 and "under the bound 450,000" in problems[0]
    assert script.placement(dataclasses.replace(v2, bound=370_000)) == []
    gone = _with_probe(v2, 54, statements=("cfg nowhere.at_all=none-0000",))
    assert script.unplanted(gone) == [f"{v2.probes[54].id}: 1 of 1 statements are planted nowhere"]
    assert script.placement(gone) == [f"{v2.probes[54].id}: planted nowhere"]


# Scoring


def _answers(probes, wrong=(), stale=(), missing=()) -> str:
    lines = []
    for p in probes:
        if p.id in missing:
            continue
        if p.id in stale:
            lines.append(f"{p.id}: {p.older[0]}")
        elif p.id in wrong:
            lines.append(f"{p.id}: certainly-not-0000")
        else:
            lines.append(f"- {p.id}: `{p.answer.upper()}`.")
    return "preamble the parser ignores\n" + "\n".join(lines)


def test_score_matches_the_spike_on_the_d27_probes(spike, base):
    chain = [p for p in base.probes if p.type == "chain"]
    text = _answers(base.probes, wrong={base.probes[0].id, chain[3].id}, stale={chain[0].id, chain[1].id},
                    missing={base.probes[10].id})
    theirs = spike["l2f"].score(list(spike["run"].build_script(SEED, 0, 40_000, spike["budget"].Params()).probes), text)
    ours = script.score(base.probes, text)
    assert (ours["correct"], ours["total"]) == (theirs["correct"], theirs["total"]) == (49, 54)
    assert ours["cells"] == theirs["cells"]
    assert ours["interval"] == pytest.approx(theirs["interval"], abs=1e-4)  # the spike rounds z to 1.96
    assert ours["cells"]["chain"] == {"correct": 27, "total": 30, "stale": 2}


def test_score_returns_one_row_per_probe(v2):
    paraphrase, concluded = _new(v2, "paraphrase")[0], _new(v2, "concluded")[0]
    set_probe = next(p for p in v2.probes if p.kind == "set")
    chain = next(p for p in v2.probes if p.type == "chain")
    ids = set_probe.answer.split(", ")
    text = "\n".join([
        f"{paraphrase.id}: {paraphrase.answer}",
        f"{concluded.id}: the answer is {concluded.answer}",  # a value inside a sentence is wrong
        f"{set_probe.id}: {' , '.join(reversed(ids))}",  # a set in any order is right
        f"{chain.id}: {chain.older[0]}",
    ])
    result = script.score(v2.probes, text)
    rows = {r["id"]: r for r in result["rows"]}
    assert [r["id"] for r in result["rows"]] == [p.id for p in v2.probes] and result["total"] == 72
    assert list(rows[paraphrase.id]) == ["id", "cls", "type", "tier", "kind", "expected", "given", "correct", "stale"]
    assert rows[paraphrase.id] == {"id": paraphrase.id, "cls": "paraphrase", "type": "paraphrase", "tier": "cfg-line",
                                   "kind": "value", "expected": paraphrase.answer, "given": paraphrase.answer,
                                   "correct": True, "stale": False}
    assert rows[concluded.id]["correct"] is False and rows[concluded.id]["stale"] is False
    assert rows[set_probe.id]["correct"] is True
    assert rows[chain.id]["correct"] is False and rows[chain.id]["stale"] is True
    unanswered = next(r for r in result["rows"] if r["id"] not in (paraphrase.id, concluded.id, set_probe.id, chain.id))
    assert unanswered["given"] is None and unanswered["correct"] is False
    assert result["correct"] == sum(r["correct"] for r in result["rows"]) == 2
    assert result["cells"]["concluded"] == {"correct": 0, "total": 6, "stale": 0}
    assert result["cells"]["paraphrase/cfg-line"] == {"correct": 1, "total": 6, "stale": 0}
    assert script.score(v2.probes, _answers(v2.probes))["correct"] == 72
