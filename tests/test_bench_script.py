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
from contexto.bench.model import GENERATOR_WORDS, NEW_CLASSES, Probe, Script, lexical_overlap, words

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
V2_SCRIPT_DIGEST = "469a5465187a97997a4dd61e6d3486ff9108e925960084e62ee4d206973dea77"
SPIKE_PROBE_FIELDS = ("id", "group", "type", "tier", "question", "answer", "kind", "older", "subjects", "statements")
SEEDS = (20261016, 20261017, 1, 999)
SOUND = {"restatements": [], "key_free": [], "service_alone": [], "placement": [], "unplanted": []}
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
    assert script.checks(base) == SOUND
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


def _decision(v2: Script, **change) -> dict:
    return {**dataclasses.asdict(v2.decisions[0]), **change}


def _damaged(v2: Script, field: str, edit) -> dict:
    data = v2.to_dict()
    data[field] = edit(data[field]) if callable(edit) else edit
    return data


def _replace_at(items: list, index: int, value) -> list:
    items = list(items)
    items[index] = value
    return items


DAMAGE = [
    # (field, edit, words the error must hold)
    ("kinds", ["log"], "kinds: 34 turns but 1 kinds"),
    ("turn_bytes", [1, 2], "turn_bytes: 34 turns but 2 sizes"),
    ("turn_bytes", lambda b: [b[0] + 1, *b[1:]], "turn_bytes: turn 1 is"),
    ("protocol", "v9", "protocol: unknown protocol 'v9'"),
    ("reads", ["bench-files/absent.txt"], "reads: 'bench-files/absent.txt' is not among the files"),
    ("probe_turn_ids", lambda ids: [*ids[:-1], ["q_zz_99"]], "probe_turn_ids: unknown probe 'q_zz_99'"),
    ("decisions", lambda d: [{**d[0], "turn": 0}, *d[1:]], "decisions: turn 1 is not a decision turn"),
    ("decisions", lambda d: [{**d[0], "turn": 99}, *d[1:]], "decisions: turn 100 is not a decision turn"),
    ("decisions", lambda d: d[1:], "decisions: decision turn"),  # a decision turn with no Decision
    ("decisions", lambda d: [d[0], *d], "decisions: two decisions for turn"),
    ("decisions", lambda d: [{**d[0], "probe": "q_zz_99"}, *d[1:]], "decisions: unknown probe 'q_zz_99'"),
    ("decisions", lambda d: [{**d[0], "value": "zz-0000"}, *d[1:]], "decisions: the value of"),
    ("decisions", lambda d: [{**d[0], "reply": "DECISION DX-1: x"}, *d[1:]], "decisions: the reply of"),
    ("files", lambda f: {"../outside.txt": "x", **f}, "files: '../outside.txt'"),
    ("files", lambda f: {"/abs/path.txt": "x", **f}, "files: '/abs/path.txt'"),
    ("files", lambda f: {"bench-files\\x.txt": "x", **f}, "files: 'bench-files\\\\x.txt'"),
    ("files", lambda f: {"": "x", **f}, "files: ''"),
    ("files", lambda f: {"bench-files//x.txt": "x", **f}, "files: 'bench-files//x.txt'"),
    ("files", lambda f: {"bench-files/./x.txt": "x", **f}, "files: 'bench-files/./x.txt'"),
    ("files", lambda f: {"elsewhere/x.txt": "x", **f}, "files: 'elsewhere/x.txt'"),
    ("reads", lambda r: ["../outside.txt", *r], "reads: '../outside.txt'"),
    ("reads", lambda r: [r[0], *r], "reads: 'bench-files/log-01.txt' is read twice"),
    ("probe_turn_ids", lambda ids: [*ids[:-1], [*ids[-1], ids[0][0]]], "is asked 2 times"),
    ("probe_turn_ids", lambda ids: [*ids[:-1], ids[-1][1:]], "is asked 0 times"),
    ("turns", lambda t: [*t[:-1], t[-1].replace("q_", "x_")], "turns: probe turn 8 does not ask"),
    ("kinds", lambda k: [*k[:-1], "log"], "kinds: turn 34 is a probe turn"),
    ("kinds", lambda k: _replace_at(k, 10, "probe"), "kinds: turn 11 is a content turn"),
    ("kinds", lambda k: _replace_at(k, 10, "toolloop"), "kinds: 2 toolloop turns"),
    ("added_turns", [999], "added_turns: 999"),
    ("added_turns", lambda a: [a[1], a[0], *a[2:]], "added_turns: not in order"),
    ("paged_only", ["q_zz_99"], "paged_only: unknown probe 'q_zz_99'"),
    ("probes", lambda p: [p[0], *p], "probes: 'q_p_01' twice"),
    ("bound", -5, "bound: must be a whole number, 1 or more"),
    ("bound", 1.9, "bound: must be a whole number"),
    ("seed", 1.5, "seed: must be a whole number"),
    ("seed", True, "seed: must be a whole number"),
    ("seed", -1, "seed: must be a whole number, 0 or more"),
    ("turn_size", 0, "turn_size: must be a whole number, 1 or more"),
    ("probes", lambda p: [{**p[0], "overlap": 1.5}, *p[1:]], "overlap"),
    ("probes", lambda p: [{**p[0], "kind": "list"}, *p[1:]], "probes: 'q_p_01' has kind 'list'"),
    ("probes", lambda p: [{**p[0], "cls": "riddle"}, *p[1:]], "probes: 'q_p_01' has class 'riddle'"),
    # Reviewer B's nit 2 at 74bdf083:
    ("files", lambda f: {"bench-files/a\x00b.txt": "x", **f}, "files: 'bench-files/a\\x00b.txt' holds a control"),
    ("files", lambda f: {"bench-files/a\nb.txt": "x", **f}, "files: 'bench-files/a\\nb.txt' holds a control"),
    ("files", lambda f: {"bench-files/a\tb.txt": "x", **f}, "files: 'bench-files/a\\tb.txt' holds a control"),
    ("files", lambda f: {"bench-files/a\x1fb.txt": "x", **f}, "files: 'bench-files/a\\x1fb.txt' holds a control"),
    ("reads", lambda r: ["bench-files/a\nb.txt", *r], "reads: 'bench-files/a\\nb.txt' holds a control"),
    ("files", lambda f: {"bench-files/log-01.txt/x": "x", **f},
     "files: 'bench-files/log-01.txt/x' is below the file 'bench-files/log-01.txt'"),
    ("decisions", lambda d: [{**d[0], "label": "DX-0000", "reply": f"DECISION DX-0000: {d[0]['value']}"}, *d[1:]],
     "decisions: the label of the decision for turn"),
    ("decisions", lambda d: [{**d[0], "label": "", "reply": f"DECISION : {d[0]['value']}"}, *d[1:]],
     "decisions: the label of the decision for turn"),
    ("probes", lambda p: [{**p[0], "answer": "`\"`."}, *p[1:]], "probes: 'q_p_01' has an answer that is empty"),
    ("probes", lambda p: [{**p[0], "answer": ""}, *p[1:]], "probes: 'q_p_01' has an answer that is empty"),
]


@pytest.mark.parametrize("field, edit, message", DAMAGE)
def test_from_json_refuses_a_damaged_script_and_names_the_field(v2, field, edit, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        Script.from_dict(_damaged(v2, field, edit))
    kinds = list(v2.kinds)
    kinds[0] = "chat"
    with pytest.raises(ValueError, match="kinds: unknown turn kind 'chat'"):
        Script.from_dict({**v2.to_dict(), "kinds": kinds})


@pytest.mark.parametrize("found", [True, 1.0, "1", None, 2])
def test_from_json_refuses_a_format_that_is_not_the_integer_1(v2, found):
    with pytest.raises(ValueError, match=re.escape(f"not a benchmark script of format 1 (found {found!r})")):
        Script.from_dict({**v2.to_dict(), "format": found})


def test_from_json_refuses_json_nested_too_deeply_as_damage():
    for text in ("[" * 200_000 + "]" * 200_000, '{"a":' * 200_000 + "1" + "}" * 200_000):
        with pytest.raises(ValueError, match="not JSON: nested too deeply"):
            Script.from_json(text)


def test_from_json_accepts_both_protocols_undamaged():
    for protocol in ("d27", "v2"):
        for seed in (1, 2, 3):
            s = script.build(seed, protocol, bound=4_000, turn_bytes=2_000)
            assert Script.from_json(s.to_json()) == s


def test_a_script_is_hashable(v2):
    again = Script.from_json(v2.to_json())
    assert hash(again) == hash(v2) and len({v2, again}) == 1
    assert len({v2, script.build(SEED, "d27")}) == 2


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
    assert script.checks(s) == SOUND
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
        # The asked line and two decoys are in the log turn, the third decoy in a file.
        assert sum(line.startswith(f"cfg {service}.") for line in lines) == 3
        assert sum(line.startswith(f"cfg {service}.") for t in v2.files.values() for line in t.split("\n")) == 1
    assert len(attributes) == 6


def test_no_attribute_clause_shares_a_word_with_its_identifier():
    names = [name for name, _ in script.ATTRIBUTES]
    assert len(set(names)) == len(names) >= 12
    for name, clause in script.ATTRIBUTES:
        assert "{svc}" in clause and clause.endswith("?")
        identifier_words = set(re.findall(r"[a-z0-9]+", name))
        clause_words = set(re.findall(r"[a-z0-9]+", clause.casefold()))
        assert not identifier_words & clause_words, name
        assert not script.leaks(name, clause), name  # nor contains one or is contained in one
        assert name not in d27_identifiers()


# Pairs of attributes whose clauses a reader could take for one another, judged by hand from the
# clauses (the first three pairs are the reviewers'). Each pair must share a family.
CONFUSABLE = (
    ("failover_target", "standby_pool"), ("alert_route", "oncall_alias"), ("signing_secret", "cert_serial"),
    ("standby_pool", "backup_bucket"), ("alert_route", "log_stream"), ("image_tag", "release_channel"),
    ("image_tag", "schema_revision"), ("release_channel", "feature_set"),
)


def test_attribute_families_cover_the_whole_synonym_table():
    assert set(script.FAMILIES) == {name for name, _ in script.ATTRIBUTES}
    for a, b in CONFUSABLE:
        assert script.FAMILIES[a] == script.FAMILIES[b], (a, b)
    assert len(set(script.FAMILIES.values())) >= script.DECOYS + 1 + 2  # room to spare after leaks
    # Across families, no identifier shares a word with another attribute's clause: a question can
    # point at a planted decoy neither by meaning (families) nor by a word.
    clauses = dict(script.ATTRIBUTES)
    for a in clauses:
        for b in clauses:
            if script.FAMILIES[a] != script.FAMILIES[b]:
                assert not script.leaks(b, clauses[a]), (a, b)


def test_leaks():
    assert script.leaks("cache_namespace", "the cache svc service")
    assert script.leaks("alert_route", "the router hub service")  # contained in a word
    assert script.leaks("log_stream", "the catalog api service")
    assert not script.leaks("alert_route", "the relay mesh service")
    assert not script.leaks("dns_zone", "an")  # tokens under 3 characters do not count


def _cfg_lines(s: Script, service: str) -> list[tuple[str, str]]:
    """Every cfg line of ``service`` with where it is: a turn index or a file path."""
    out = []
    for where, text in [*enumerate(s.content_turns), *s.files.items()]:
        out += [(str(where), line) for line in text.split("\n") if line.startswith(f"cfg {service}.")]
    return out


SWEEP = range(200)  # seeds at the small test size: a 4,000-token bound and 2,000-byte turns


@pytest.fixture(scope="module")
def sweep() -> list[Script]:
    return [script.build(seed, "v2", bound=4_000, turn_bytes=2_000) for seed in SWEEP]


def test_a_search_by_service_alone_finds_four_candidates(sweep):
    for s in sweep:
        for p in s.probes:
            if p.cls not in ("paraphrase", "toolloop"):
                continue
            service = p.subjects[1]
            found = script.service_lines(s, service)
            values = {line.rsplit("=", 1)[1] for line in found if line.startswith("cfg ")}
            assert len(found) >= 4 and len(values) >= 4, (s.seed, p.id)
            lines = [line for text in [*s.content_turns, *s.files.values()] for line in text.split("\n")]
            for query in (service, service.split("-")):  # the hyphenated name, then its separate words
                terms = [query] if isinstance(query, str) else query
                hits = {line.rsplit("=", 1)[-1] for line in lines if all(t in line for t in terms)}
                assert len(hits) >= 4, (s.seed, p.id, query)
            own = {line.rsplit("=", 1)[1] for _, line in _cfg_lines(s, service)}
            assert set(p.older) == own - {p.answer} and len(p.older) == script.DECOYS
        assert script.service_alone(s) == [], s.seed


def test_each_cfg_question_has_exactly_one_answer_line_and_lines_in_two_places(sweep):
    for s in sweep:
        for p in s.probes:
            if p.cls not in ("paraphrase", "toolloop"):
                continue
            key, service, attribute = p.subjects
            lines = _cfg_lines(s, service)
            assert len(lines) == 1 + script.DECOYS, (s.seed, p.id)
            assert [line for _, line in lines if line.startswith(f"cfg {key}=")] == [p.statements[0]]
            assert len({where for where, _ in lines}) >= 2, (s.seed, p.id)
            planted = [line.split(".", 1)[1].split("=", 1)[0] for _, line in lines]
            assert len(set(planted)) == len(planted) and planted.count(attribute) == 1
            # No two planted attributes of one service share a family, so no question fits two lines.
            assert len({script.FAMILIES[a] for a in planted}) == len(planted), (s.seed, p.id, planted)
            # No planted attribute's identifier shares a word with the service name or the question.
            for name in planted:
                assert not script.leaks(name, p.question), (s.seed, p.id, name)
                assert not script.leaks(name, service), (s.seed, p.id, name)


def test_a_service_never_carries_the_like_named_attribute():
    # The reviewer's repro: seed 7 at the small size asked cache-svc for cache_namespace.
    s = script.build(7, "v2", bound=4_000, turn_bytes=2_000)
    for p in s.probes:
        if p.cls in ("paraphrase", "toolloop"):
            assert not script.leaks(p.subjects[2], p.subjects[1]), p.subjects


def test_service_alone_catches_a_service_with_no_decoys(v2):
    p = _new(v2, "paraphrase")[0]
    service = p.subjects[1]

    def strip(text: str) -> str:
        return "\n".join(line for line in text.split("\n")
                         if not line.startswith(f"cfg {service}.") or line == p.statements[0])

    bare = dataclasses.replace(v2, turns=tuple(strip(t) for t in v2.turns),
                               files={k: strip(t) for k, t in v2.files.items()})
    assert [q.id for q in script.service_alone(bare)] == [p.id]
    assert script.checks(bare)["service_alone"] == [f"{p.id}: a search for {service!r} alone finds only the plant line"]
    # A plain-words mention of the service in a filler line is also a line a search finds.
    turns = list(bare.turns)
    turns[12] += f"\nnote: {' '.join(service.split('-'))} restarted"
    assert script.service_alone(dataclasses.replace(bare, turns=tuple(turns))) == []


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
        assert p.question.endswith("What did you pick? Answer with the name only.")
        assert set(p.older) == set(p.subjects[1:]) - {d.value} and len(p.older) == 2  # the two that lost
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


RIGHT_REPLIES = (
    "{reply}", "Thinking it over.\n**{reply}**\n", "- `DECISION {label}: {VALUE}`.",
    "DECISION {label}: {other}\nDECISION {label}: {value}",  # the last line counts
    # The forms Reviewer B found rejected at a975801:
    "**DECISION** {label}: {value}", "DECISION **{label}**: {value}", "My reply: DECISION {label}: {value}",
    "DECISION {label}: {value} (it has the lowest number, 110)", "DECISION {label}: {value} = 110",
    "DECISION {label}: <{value}>", "decision {label}: {value}", "DECISION {label_lower}: {value}",
    "1. DECISION {label}: {value}", "DECISION {label_nbh}: {value}",
    # Reviewer B's forms rejected at 74bdf083: the losers named in brackets, and other separators.
    "DECISION {label}: {value} (beats {other} and {other2})", "DECISION: {label}: {value}",
    "DECISION {label} - {value}", "DECISION {label} = {value}", "DECISION {label} \u2013 {value}",
    "DECISION {label}: {value} [{other} and {other2} lost]", "DECISION {label}: {value} ({other} (high) lost)",
)
WRONG_REPLIES = (
    "DECISION {label}: {other}", "DECISION {label}: {value} or {other}", "DECISION {label}: {other2}",
    "DECISION DX-0000: {value}", "DECISION {label}0: {value}", "OK", "", "{value}",
    "DECISION {label}: {value}\nDECISION {label}: {other}", "DECISION {label}: none of them",
    "DECISION {label}: {value_prefix}", "DECISION {label}: {value}9", "DECISION {label}:",
    # Reviewer B's forms accepted at 74bdf083: a negated candidate is not a decision for it.
    "DECISION {label}: not {value}", "DECISION {label}: anything except {value}",
    "DECISION {label}: {other} (not {value})", "DECISION {label}: ({value}) {other}",
    "DECISION {label}: no, {value} is wrong", "DECISION {label}: {other} = 110 [{value}]",
    "DECISION {label}-0: {value}",
)


@pytest.mark.parametrize("form", RIGHT_REPLIES)
def test_decision_reply_ok_accepts_a_right_decision_in_common_forms(v2, form):
    d = v2.decisions[0]
    assert script.decision_reply_ok(v2, d.turn, form.format(**_reply_fields(v2, d))), form


@pytest.mark.parametrize("form", WRONG_REPLIES)
def test_decision_reply_ok_rejects_every_wrong_decision(v2, form):
    d = v2.decisions[0]
    assert not script.decision_reply_ok(v2, d.turn, form.format(**_reply_fields(v2, d))), form


def _reply_fields(s: Script, d) -> dict[str, str]:
    others = [name for name in next(p for p in s.probes if p.id == d.probe).subjects[1:] if name != d.value]
    return {"reply": d.reply, "label": d.label, "value": d.value, "VALUE": d.value.upper(), "other": others[0],
            "other2": others[1], "label_lower": d.label.lower(), "label_nbh": d.label.replace("-", "\u2011"),
            "value_prefix": d.value[:-1]}


def test_decision_reply_ok_refuses_a_turn_that_is_not_a_decision(v2):
    with pytest.raises(ValueError, match="not a decision turn"):
        script.decision_reply_ok(v2, 0, v2.decisions[0].reply)


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
    assert len(set(holders)) == 6  # six files hold one asked fact each
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
    assert words("cfg billing-core.retention_days=amber-4417") == {"cfg", "billing", "core", "retention", "days",
                                                                   "amber", "4417"}
    assert words("The API hub is OK; e2e ran.") == {"api", "hub", "e2e", "ran"}  # 3 characters count, "is" not
    assert words("WHAT value did Billing have, with THIS?") == {"value", "did", "billing"}  # case folded
    assert words("Next part of the working log: level=INFO latency_ms=3 shard=1 ok") == frozenset()  # filler, headers
    assert words("the search-hub service") == {"search", "hub", "service"}  # a subject word stays
    assert words("") == frozenset()


def test_generator_words_are_the_filler_and_fixed_turn_words():
    texts = [d27.LOG_HEAD, d27.LOG_TAIL, d27.INSTRUCTIONS, *d27.PROBE_HEADER, script.TOOLLOOP_HEAD,
             script.DECISION_HEAD, *(script.DECISION_RULE.format(rule=r) for r in ("lowest", "highest")),
             script.DECISION_ASK[0], script.DECISION_ASK[1].format(label="DX-1"),  # a turn holds a label, not "{label}"
             d27.filler_text(random.Random(0), 200, "s00")]
    found = {w for t in texts for w in re.findall(r"[a-z]+", t.casefold()) if len(w) >= 3}
    names = {w for name in d27.Pools(random.Random(0)).services for w in name.split("-")} | set(d27.WORDS)
    assert GENERATOR_WORDS == found - names
    assert "search" in found & names  # the one collision: a service root and a word of the tool-loop head
    assert "label" not in GENERATOR_WORDS  # no turn holds the placeholder's word
    # Subject words other than service names stay: "shard" is on every filler line.
    assert {"shard", "log", "name"} <= GENERATOR_WORDS


def test_lexical_overlap_counts_distinct_shared_words():
    statement = "cfg billing-core.retention_days=amber-4417"
    assert lexical_overlap("What value did billing-core.retention_days have? billing billing", [statement]) == 4
    assert lexical_overlap("How long does the billing core service keep its data?", [statement]) == 2
    assert lexical_overlap("What does the relay hub service use?", ["cfg relay-hub.dns_zone=onyx-1234"]) == 2
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
    # A decoy, a cfg line for the same service with another attribute, is not a restatement.
    assert all(any(line.startswith(f"cfg {q.subjects[1]}.") and line != q.statements[0]
                   for t in v2.content_turns for line in t.split("\n")) for q in _new(v2, "paraphrase"))
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


# (line, what is recorded as given, what it normalizes to); None: not an answer line.
ANSWER_LINES = [
    # The forms the spike's parser reads, unchanged.
    ("q_a_001: nickel-1771", "nickel-1771", "nickel-1771"),
    ("q_a_001 = nickel-1771", "nickel-1771", "nickel-1771"),
    ("  q_a_001:nickel-1771  ", "nickel-1771", "nickel-1771"),
    ("- q_a_001: nickel-1771", "nickel-1771", "nickel-1771"),
    ("* q_a_001: nickel-1771", "nickel-1771", "nickel-1771"),
    ("-q_a_001: nickel-1771", "nickel-1771", "nickel-1771"),
    ("q_a_001: `nickel-1771`.", "`nickel-1771`.", "nickel-1771"),
    ("q_a_001:", "", ""),
    # Markdown a Claude Code arm writes (its system prompt asks for GitHub-flavoured markdown).
    ("**q_a_001**: nickel-1771", "nickel-1771", "nickel-1771"),
    ("**q_a_001:** nickel-1771", "** nickel-1771", "nickel-1771"),
    ("__q_a_001__: nickel-1771", "nickel-1771", "nickel-1771"),
    ("`q_a_001`: nickel-1771", "nickel-1771", "nickel-1771"),
    ("- `q_a_001: nickel-1771`", "nickel-1771`", "nickel-1771"),
    ("- **q_a_001**: nickel-1771", "nickel-1771", "nickel-1771"),
    ("+ q_a_001: nickel-1771", "nickel-1771", "nickel-1771"),
    ("1. q_a_001: nickel-1771", "nickel-1771", "nickel-1771"),
    ("12) **q_a_001**: nickel-1771", "nickel-1771", "nickel-1771"),
    ("q_a_001: **nickel-1771**", "**nickel-1771**", "nickel-1771"),
    ("q_a_001: _nickel-1771_", "_nickel-1771_", "nickel-1771"),
    # Not answer lines.
    ("The answer to q_a_001: nickel-1771", None, None),
    ("q_a_001 is nickel-1771", None, None),
    ("Q_A_001: nickel-1771", None, None),
    ("1.5 q_a_001: nickel-1771", None, None),
    # A blockquote marks text as someone else's (a quoted note or file), so a quoted line is no answer.
    ("> q_a_001: nickel-1771", None, None),
    ("> - q_a_001: nickel-1771", None, None),
    ("> > `q_a_001`: nickel-1771", None, None),
    ("- > q_a_001: nickel-1771", None, None),
]


@pytest.mark.parametrize(("line", "given", "normalized"), ANSWER_LINES)
def test_answer_lines_in_markdown_are_read(line, given, normalized):
    """The second deliberate difference from the spike's scoring (see ``script.score``): an answer line
    may carry a list marker, a number, and emphasis or code around the id or the answer; a quoted line
    is not read."""
    from contexto.bench import d27

    parsed = d27.parse_answers(f"preamble\n{line}\nclosing words")
    assert parsed == ({} if given is None else {"q_a_001": given})
    if given is not None:
        assert d27.normalize(given) == normalized


def test_markdown_answers_are_scored_and_the_last_line_for_an_id_wins(v2):
    probe = next(p for p in v2.probes if p.kind == "value")
    for line, given, _normalized in ANSWER_LINES:
        text = line.replace("q_a_001", probe.id).replace("nickel-1771", probe.answer)
        expected = 0 if not given else 1
        assert script.score([probe], text)["correct"] == expected, line
    assert script.score([probe], f"**{probe.id}**: {probe.answer}\n1. {probe.id}: wrong-0")["correct"] == 0
    assert script.score([probe], f"{probe.id}: wrong-0\n1. `{probe.id}`: {probe.answer}")["correct"] == 1
    # Quoted or fenced text after the answer does not override it.
    for after in (f"> {probe.id}: wrong-0", f"```\n{probe.id}: wrong-0\n```"):
        assert script.score([probe], f"{probe.id}: {probe.answer}\nThe file said:\n{after}")["correct"] == 1


FENCED = [
    # (text, what parse_answers reads). The answer before any fence is q_a_01: nickel.
    ("```\nq_a_01: POISON\n```", "nickel"),
    ("```text\nq_a_01: POISON\n```", "nickel"),
    ("~~~\nq_a_01: POISON\n~~~", "nickel"),
    ("   ```yaml\nq_a_01: POISON\n   ```", "nickel"),
    ("- item\n  ```\n  q_a_01: POISON\n  ```", "nickel"),  # a fence inside a list item
    ("- ```\n  q_a_01: POISON\n  ```", "nickel"),  # a fence that opens a list item
    ("````\n```\nq_a_01: POISON\n````", "nickel"),  # a shorter fence does not close a longer one
    ("~~~\n```\nq_a_01: POISON\n~~~", "nickel"),  # nor does one of the other character
    ("```\nq_a_01: POISON", "nickel"),  # a fence never closed runs to the end of the text
    ("```\nq_a_01: POISON\n```\nq_a_01: copper", "copper"),  # after the closing fence lines are read again
    ("```q_a_01: copper```", "copper```"),  # backticks in a backtick fence's info string: inline code, read
    ("``\nq_a_01: copper\n``", "copper"),  # two backticks are no fence
]


@pytest.mark.parametrize(("after", "given"), FENCED)
def test_an_answer_in_a_fenced_code_block_does_not_override_one_outside(after, given):
    """The third deliberate difference from the spike's scoring (``script.score``): a fenced code block
    may hold a quoted file, so an answer outside any fence wins over one inside it."""
    from contexto.bench import d27

    assert d27.parse_answers(f"q_a_01: nickel\n{after}\nclosing words") == {"q_a_01": given}


def test_answers_written_only_inside_a_code_fence_are_read():
    """A reply that formats its whole answer list as a code block is scored, as the spike scored it."""
    from contexto.bench import d27

    assert d27.parse_answers("```\nq_a_01: nickel\nq_a_02: copper\n```") == {"q_a_01": "nickel", "q_a_02": "copper"}
    assert d27.parse_answers("Answers:\n~~~text\n- q_a_01: nickel\n~~~") == {"q_a_01": "nickel"}
    assert d27.parse_answers("```\nq_a_01: nickel\n```\nq_a_01: copper") == {"q_a_01": "copper"}
    assert d27.parse_answers("q_a_01: copper\n```\nq_a_01: nickel\n```") == {"q_a_01": "copper"}
    assert d27.parse_answers("```\n> q_a_01: POISON\n```") == {}


def _restated(probes, layout: str) -> str:
    """A reply to a probe turn in one of the layouts the live review saw with the real ``claude``: the
    probe turn's own ``q_<id>: <question>`` lines restated next to the answers."""
    questions = "\n".join(f"{p.id}: {p.question}" for p in probes)
    answers = "\n".join(f"{p.id}: {p.answer}" for p in probes)
    return {
        "questions, then answers in a fence": f"The questions were:\n{questions}\n\n```\n{answers}\n```",
        "answers, then the questions": f"{answers}\n\nYou asked:\n{questions}",
        "answers in a fence, then the questions": f"```\n{answers}\n```\nThe questions were:\n{questions}",
        "answers in a fence, then the questions in a fence": f"```\n{answers}\n```\nYou asked:\n```\n{questions}\n```",
        "questions as a list, then answers in a fence":
            "\n".join(f"- **{p.id}**: {p.question}" for p in probes) + f"\n\n~~~text\n{answers}\n~~~",
        "questions, then answers": f"{questions}\n\n{answers}",
    }[layout]


RESTATED_LAYOUTS = ["questions, then answers in a fence", "answers, then the questions",
                    "answers in a fence, then the questions", "answers in a fence, then the questions in a fence",
                    "questions as a list, then answers in a fence", "questions, then answers"]


@pytest.mark.parametrize("layout", RESTATED_LAYOUTS)
def test_a_restated_question_is_never_an_answer(v2, layout):
    """The fourth deliberate difference from the spike's scoring (``script.score``): a line whose answer
    is that probe's own question text is the reply restating the question, not answering it, so the
    next line for that id is read. The probe turns are the only text a reply can quote that holds
    ``q_`` lines, so without this rule the layout, not recall, decided the score."""
    probes = [p for p in v2.probes if p.kind == "value"][:9]
    result = script.score(probes, _restated(probes, layout))
    assert result["correct"] == 9, [(r["id"], r["given"]) for r in result["rows"] if not r["correct"]]
    assert all(r["given"] == r["expected"] for r in result["rows"])


def test_a_restated_question_with_no_answer_is_no_answer(v2):
    probe = next(p for p in v2.probes if p.kind == "value")
    row = script.score([probe], f"The question was:\n{probe.id}: {probe.question}")["rows"][0]
    assert row["given"] is None and row["correct"] is False
    # Only the probe's own question is set aside: another probe's question is an answer, a wrong one.
    other = next(p for p in v2.probes if p.kind == "value" and p.id != probe.id)
    row = script.score([probe], f"{probe.id}: {probe.answer}\n{probe.id}: {other.question}")["rows"][0]
    assert row["given"] == other.question and row["correct"] is False


def test_a_later_fence_still_overrides_answers_written_in_a_fence(v2):
    """What the fence rule does not handle, stated in ``d27.parse_answers``: when the answers are in a
    fence, a later fence that quotes other answer-shaped lines wins over them (last line wins). No
    content turn holds a ``q_`` line, so in this benchmark only a restated question can be such a line,
    and that is set aside by the rule above."""
    probe = next(p for p in v2.probes if p.kind == "value")
    text = f"```\n{probe.id}: {probe.answer}\n```\nFor reference, notes.md held:\n```\n{probe.id}: POISON-1\n```"
    assert script.score([probe], text)["rows"][0]["given"] == "POISON-1"


def test_a_quoted_line_after_the_answer_does_not_override_it():
    from contexto.bench import d27

    assert d27.parse_answers("q_a_01: nickel\nThe file said:\n> q_a_01: POISON") == {"q_a_01": "nickel"}
    assert d27.parse_answers("> q_a_01: POISON") == {}


def test_score_never_counts_a_missing_answer_correct(spike):
    # The first deliberate difference from the spike's scoring: seed 11 at the small size has a set
    # probe whose expected answer is "none", which the spike scores correct when nothing is answered.
    s = script.build(11, "d27", bound=4_000, turn_bytes=2_000)
    p = next(p for p in s.probes if p.id == "q_a_044")
    assert p.kind == "set" and p.answer == "none"
    theirs = spike["run"].build_script(11, 0, 2_000, spike["budget"].Params(bound=4_000)).probes
    assert spike["l2f"].score(list(theirs), "")["correct"] == 1  # the spike: one point for silence
    ours = script.score(s.probes, "")
    row = next(r for r in ours["rows"] if r["id"] == p.id)
    assert ours["correct"] == 0 and row["given"] is None and row["correct"] is False
    assert script.score([p], f"{p.id}:")["correct"] == 0  # an empty answer line is no answer either
    assert script.score([p], f"{p.id}: none")["correct"] == 1  # saying "none" is still right
    value = next(q for q in s.probes if q.kind == "value")
    assert script.score([value], "")["correct"] == 0
