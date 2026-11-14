"""Fast checks of the D65 probe set and its bench (small stores only, no network; one child that is killed at once)."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections import Counter
from pathlib import Path

import fallback_bench
import fallback_probes
import pytest
import scale_bench
import scale_synth
from fallback_probes import CLASSES, EXCLUDED, EXTRA_FACTS, HELD_OUT_FACTS, MINIMUM, PROBES, Probe
from scale_probes import FACTS, planted_turn

from contexto.store import SessionStore
from contexto.store.search import fold

SMALL = 40  # pages of the one store these tests build through the bench
# sha256 over the JSON of every turn of the default corpus (seed 7, floor 0), recorded on main at
# b0e2dbc before scale_synth took ``extra``. Python 3.12 to 3.15 give the same value.
DEFAULT_CORPUS_60 = "2647552be3c73245eb87d787a97ff86490883a1599c3e1141389abbb8ec56771"  # all 60 turns
DEFAULT_SAMPLE_6000 = "b466d2f86770ad3f9843ad091cca08d19a5bb48d60f260eea32b241317eb5d1a"  # turns 0, 1499, 2220, 5999
# sha256 over "id<TAB>query" of the 154 probes of the first set, sorted, as pushed at 2413af5.
FIRST_SET = "587ecca4babed28011397d929bce077db05ce2682aa71690f20ada849884011e"
# sha256 over "id<TAB>class<TAB>fact:plant;..." of the same 154, sorted, the same at 2413af5.
FIRST_SET_EXPECTED = "638ede21d8b29f614bdef9c4e9d299ed2e4808af483111a2c77b7b272e56f33e"
BEFORE = Path(__file__).resolve().parent / "results" / "d65" / "before"  # the checked-in run on main


def of(cls: str) -> list[Probe]:
    return [p for p in PROBES if p.cls == cls]


def planted(probe: Probe, n: int = 6000) -> str:
    """Everything planted on the probe's expected pages at ``n`` pages, folded as search folds a page."""
    return fold(fallback_probes.planted_text(probe, n))


def words(query: str) -> list[str]:
    return fold(query).split()


def edit_kind(a: str, b: str) -> str | None:
    """The single-character edit that turns ``a`` into ``b``, or None when they are not one edit apart."""
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return "substitution"
        if len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]:
            return "swap"
        return None
    if len(a) == len(b) + 1:
        return "deletion" if any(a[:i] + a[i + 1:] == b for i in range(len(a))) else None
    if len(a) + 1 == len(b):
        return "insertion" if any(b[:i] + b[i + 1:] == a for i in range(len(b))) else None
    return None


def turns_digest(n: int, turns: list[int] | None = None, **kwargs) -> str:
    h = hashlib.sha256()
    for t in turns if turns is not None else range(n):
        turn = scale_synth.session_turn(t, n, seed=7, floor=0.0, **kwargs)
        h.update(json.dumps(turn, sort_keys=True, ensure_ascii=True).encode())
    return h.hexdigest()


def background() -> str:
    """Every text a page of the D65 corpus (floor 0) can be built from at any size, folded: the line
    pools, every plant, the renamed identifiers, and for every query with a distractor rate above 0 each
    line ``scale_synth.near_sentences`` can draw for it. The fixed wording of tool calls is not here;
    ``small`` covers that."""
    lines = [line for pool in scale_synth.pools().values() for line in pool]
    for fact in (*FACTS, *EXTRA_FACTS):
        lines += [fallback_probes.plant_text(fact.id, i) for i in range(len(fact.plants))]
        lines += fact.rename or ()
        for q in fact.queries:
            if scale_synth.distractor_rate(q, 0.0) > 0:
                longer = [q2 for q2 in fact.queries if q2 != q and q2 not in q]
                lines += [s for s in fact.near if q in s and not any(q2 in s for q2 in longer)]
                lines += [template.format(q=q) for template in scale_synth.NEAR_TEMPLATES]
    return fold("\n".join(lines))


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> tuple[Path, Path]:
    """One quality run of the bench at ``SMALL`` pages: (the result folder, the store home it built)."""
    root = tmp_path_factory.mktemp("d65-bench")
    home, out = root / "home", root / "results"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("HOME", str(root / "fake-home"))  # nothing here may reach the real home
        patch.delenv("CONTEXTO_HOME", raising=False)
        assert fallback_bench.main(["--sizes", str(SMALL), "--quality-only", "--label", "before", "--out", str(out),
                                    "--store-home", str(home)]) == 0
    assert not (root / "fake-home").exists()
    return out / "before", home


@pytest.fixture(scope="module")
def small(run) -> tuple[Path, SessionStore, list[str]]:
    """The store of that run (the extended corpus) and its page ids, one per turn."""
    _, home = run
    ids = json.loads((home / f"d65-{SMALL}.json").read_text(encoding="utf-8"))["ids"]
    return home, SessionStore(home, f"d65-{SMALL}"), ids


@pytest.fixture
def safe_home(tmp_path: Path, monkeypatch) -> Path:
    """A test that calls ``main`` gets a home of its own, so a broken guard cannot touch the real one."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.delenv("CONTEXTO_HOME", raising=False)
    return tmp_path / "fake-home"


# The probe set


def test_edit_kind_names_one_edit_and_rejects_two():
    assert edit_kind("policy", "polciy") == "swap" and edit_kind("secret", "seccret") == "insertion"
    assert edit_kind("staging", "stagng") == "deletion" and edit_kind("region", "regiom") == "substitution"
    assert edit_kind("policy", "policy") is None and edit_kind("policy", "pilocy") is None
    assert edit_kind("staging", "stagn") is None and edit_kind("region", "regoim") is None


def test_every_class_has_its_minimum_and_the_set_at_least_115():
    counts = Counter(p.cls for p in PROBES)
    assert set(counts) == set(CLASSES) == set(MINIMUM)
    assert MINIMUM == {"exact": 15, "identifier": 15, "paraphrase": 15, "question": 15, "typo": 15,
                       "extra_word": 15, "two_page": 10, "none": 15}
    assert all(counts[c] >= MINIMUM[c] for c in CLASSES), counts
    assert len(PROBES) >= 115


def test_ids_and_queries_are_unique_and_queries_have_words():
    everything = [*PROBES, *(p for p, _ in EXCLUDED)]
    assert len({p.id for p in everything}) == len(everything)
    assert len({p.query for p in PROBES}) == len(PROBES)
    assert len({fold(p.query) for p in PROBES}) == len(PROBES)  # no two differ by case alone
    assert all(p.query == p.query.strip() and p.query.split() for p in PROBES)
    assert all(set(p.flags) <= set(fallback_probes.FLAGS) for p in PROBES)


def test_no_query_of_the_first_set_was_changed_and_the_excluded_probe_is_kept():
    first = [p for p in PROBES if "after_review" not in p.flags] + [p for p, _ in EXCLUDED]
    assert len(first) == 154
    text = "\n".join(f"{p.id}\t{p.query}" for p in sorted(first, key=lambda p: p.id))
    assert hashlib.sha256(text.encode()).hexdigest() == FIRST_SET
    text = "\n".join(f"{p.id}\t{p.cls}\t" + ";".join(f"{fact}:{plant}" for fact, plant in p.expected)
                     for p in sorted(first, key=lambda p: p.id))
    assert hashlib.sha256(text.encode()).hexdigest() == FIRST_SET_EXPECTED  # and no class or expected page
    assert [(p.id, p.query, p.cls) for p, _ in EXCLUDED] == [("none-16", "OAuth session token", "none")]
    assert all(reason for _, reason in EXCLUDED)
    assert not {p.id for p, _ in EXCLUDED} & {p.id for p in PROBES}


def test_probes_added_after_review_ask_what_the_first_set_did_not():
    added = [p for p in PROBES if "after_review" in p.flags]
    by_class = Counter(p.cls for p in added)
    assert by_class == {"typo": 15, "extra_word": 11, "paraphrase": 12, "none": 1}
    assert sum("carries_query" in p.flags for p in of("typo")) >= 10
    assert sum(p.sub_kind == "some_pages" for p in added if p.cls == "extra_word") >= 8
    assert sum(p.sub_kind == "common_word" for p in of("paraphrase")) >= 8


def test_every_expected_plant_exists_and_a_changing_fact_expects_its_last_plant():
    facts = {f.id: f for f in (*FACTS, *EXTRA_FACTS)}
    assert len(facts) == len(FACTS) + len(EXTRA_FACTS)
    for p in PROBES:
        want = {"none": 0, "two_page": 2}.get(p.cls, 1)
        assert len(p.expected) == want, p.id
        for fact_id, plant in p.expected:
            fact = facts[fact_id]
            assert 0 <= plant < len(fact.plants), p.id
            assert plant == len(fact.plants) - 1, p.id  # the last plant holds the value that is right now
        assert len({fact_id for fact_id, _ in p.expected}) == len(p.expected), p.id


def test_expected_pages_resolve_through_the_build_manifest():
    ids = [f"pg_{t:016x}" for t in range(6000)]
    probe = next(p for p in PROBES if p.expected == (("c01", 2),))
    assert fallback_probes.expected_turns(probe, 6000) == [planted_turn(0.85, 6000)]
    assert fallback_probes.expected_pages(probe, ids) == [ids[planted_turn(0.85, 6000)]]
    for p in of("two_page"):
        a, b = fallback_probes.expected_turns(p, 6000)
        assert a != b, p.id  # two facts on two pages
    assert fallback_probes.expected_pages(of("none")[0], ids) == []


def test_no_fact_has_probes_in_both_halves():
    half_of: dict[str, set[str]] = {}
    for p in PROBES:
        for fact_id, _ in p.expected:
            half_of.setdefault(fact_id, set()).add(fallback_probes.split_of(p))
    assert set(half_of) == {f.id for f in (*FACTS, *EXTRA_FACTS)}  # every fact is asked about
    assert all(len(halves) == 1 for halves in half_of.values()), half_of
    assert {f for f, halves in half_of.items() if halves == {"held_out"}} == set(HELD_OUT_FACTS)
    for p in of("two_page"):  # a two-page probe follows its facts, which share a half
        assert len({fact_id in HELD_OUT_FACTS for fact_id, _ in p.expected}) == 1, p.id
    pages = {half: {t for p in PROBES if fallback_probes.split_of(p) == half
                    for t in fallback_probes.expected_turns(p, 6000)} for half in ("tune", "held_out")}
    assert not pages["tune"] & pages["held_out"]  # so no page either


def test_the_halves_are_balanced_per_class_and_none_probes_alternate():
    for cls in CLASSES:
        splits = [fallback_probes.split_of(p) for p in of(cls)]
        assert abs(splits.count("tune") - splits.count("held_out")) <= 1, cls
    none = [fallback_probes.split_of(p) for p in of("none")]
    assert none == ["tune" if i % 2 == 0 else "held_out" for i in range(len(none))]
    for half in ("tune", "held_out"):
        here = [p for p in PROBES if fallback_probes.split_of(p) == half]
        assert {p.sub_kind for p in here if p.cls == "typo"} == {"substitution", "deletion", "insertion", "swap"}
        added = Counter(p.cls for p in here if "after_review" in p.flags)
        assert added["typo"] >= 7 and added["extra_word"] >= 5 and added["paraphrase"] >= 6, (half, added)
        assert sum(p.expected[0][0].startswith("x") for p in here if p.cls == "identifier") >= 2, half


def test_exact_probes_use_only_words_of_their_page():
    for p in of("exact"):
        text = planted(p)
        assert all(w in text for w in words(p.query)), p.id
    assert len({p.expected for p in of("exact")}) == len(of("exact"))  # one fact each


def test_typo_probes_are_words_of_the_page_with_one_edit_in_a_word_of_six_or_more():
    exact = {p.query: p for p in of("exact")}
    kinds = Counter()
    for p in of("typo"):
        text = planted(p)
        assert all(w in text for w in words(p.base)), p.id  # spelled right, strict search finds the page
        before, after = p.base.split(), p.query.split()
        assert len(before) == len(after), p.id
        changed = [(a, b) for a, b in zip(before, after) if a != b]
        assert changed == [(p.original, p.altered)], p.id
        assert len(p.original) >= 6, p.id
        assert edit_kind(p.original, p.altered) == p.sub_kind, p.id
        assert fold(p.altered) not in text, p.id  # the typo is not on the page, so strict search misses
        assert ("short_typo" in p.flags) == (len(p.altered) < 6), p.id
        if "carries_query" in p.flags:
            assert "after_review" in p.flags and len(before) <= 2, p.id
        else:
            assert p.expected == exact[p.base].expected, p.id  # exact probe i with a typo
        kinds[p.sub_kind] += 1
    assert set(kinds) == {"substitution", "deletion", "insertion", "swap"} and min(kinds.values()) >= 5, kinds
    assert [p.id for p in of("typo") if "short_typo" in p.flags] == ["typo-14"]
    carried = [p for p in of("typo") if "carries_query" in p.flags]
    assert sum(len(p.query.split()) == 1 for p in carried) >= 5 and sum(len(p.query.split()) == 2 for p in carried) >= 5


def test_identifier_typos_are_the_carried_typos_written_as_an_identifier():
    flagged = [p for p in of("typo") if "identifier_typo" in p.flags]
    assert [p.id for p in flagged] == ["typo-16", "typo-17", "typo-18", "typo-19", "typo-20", "typo-23"]
    for p in of("typo"):
        identifier = "carries_query" in p.flags and bool(re.search(r"[_\-]", p.altered))
        assert ("identifier_typo" in p.flags) == identifier, p.id
    clean = [p.id for p in of("typo") if "carries_query" in p.flags and "identifier_typo" not in p.flags]
    assert clean == ["typo-21", "typo-22", *(f"typo-{i}" for i in range(24, 31))]


def test_style_of_names_the_four_identifier_styles():
    style = fallback_probes.style_of
    assert style("retry_backoff_ms") == style("MAX_RETRIES") == "snake"
    assert style("refreshToken") == style("UserCache") == "camel"
    assert style("maxmemory-policy") == "kebab" and style("fetch.max.wait.ms") == "dotted"
    with pytest.raises(ValueError):
        style("server.max_connections")  # two styles in one identifier: not used


def test_identifier_probes_differ_from_the_pages_identifier_only_in_style():
    def bare(identifier: str) -> str:
        return re.sub(r"[_\-.]", "", identifier).casefold()

    page_styles, query_styles = Counter(), Counter()
    for p in of("identifier"):
        raw = fallback_probes.planted_text(p, 6000)
        assert p.original in raw, p.id  # the identifier as the page writes it
        assert p.altered in p.query.split(), p.id
        assert bare(p.original) == bare(p.altered), p.id  # the same words
        assert fold(p.original) != fold(p.altered), p.id  # case alone does not count
        assert fold(p.altered) not in planted(p), p.id  # the query's style is not on the page
        here, there = fallback_probes.style_of(p.original), fallback_probes.style_of(p.altered)
        assert here != there and p.sub_kind == f"{here}>{there}", p.id
        assert all(w in planted(p) for w in words(p.query) if w != fold(p.altered)), p.id  # the other words are
        assert ("page_words" in p.flags) == (len(p.query.split()) > 1), p.id
        page_styles[here] += 1
        query_styles[there] += 1
    four = {"snake", "camel", "kebab", "dotted"}
    assert set(page_styles) == four and set(query_styles) == four, (page_styles, query_styles)
    assert min(page_styles.values()) >= 2 and min(query_styles.values()) >= 3, (page_styles, query_styles)
    assert [p.id for p in of("identifier") if "page_words" in p.flags] == [
        "identifier-04", "identifier-13", "identifier-23"]


def test_paraphrase_probes_share_a_word_with_the_page_and_hold_one_it_lacks():
    for p in of("paraphrase"):
        text = planted(p)
        on = [w in text for w in words(p.query)]
        assert any(on) and not all(on), p.id
        if p.sub_kind == "common_word":  # only the substituted word is off the page
            assert [w for w in words(p.query) if w not in text] == [fold(p.altered)], p.id
            assert "after_review" in p.flags, p.id
        else:
            assert p.altered == "" and p.flags == (), p.id
    first = [p for p in of("paraphrase") if not p.sub_kind]
    assert len({p.expected for p in first}) == len(first) == len(FACTS)  # one per fact


def test_question_probes_are_questions_and_the_marked_ones_are_the_facts_own():
    facts = {f.id: f for f in FACTS}
    own = [p for p in of("question") if p.sub_kind == "fact_question"]
    new = [p for p in of("question") if p.sub_kind == "new"]
    assert len(own) + len(new) == len(of("question")) and own and new
    for p in of("question"):
        assert p.query.endswith("?") and len(p.query.split()) >= 4, p.id
    for p in own:
        assert p.query == facts[p.expected[0][0]].question, p.id
    assert not {p.query for p in new} & {f.question for f in FACTS}


def test_extra_word_probes_are_an_exact_query_plus_one_word_the_page_lacks():
    exact = {p.query: p for p in of("exact")}
    for p in of("extra_word"):
        base = exact[p.base]
        assert p.expected == base.expected, p.id
        assert Counter(p.query.split()) == Counter(p.base.split()) + Counter([p.altered]), p.id
        assert p.original == "" and fold(p.altered) not in planted(p), p.id
        assert p.sub_kind in ("no_page", "some_pages"), p.id
        if "after_review" in p.flags:
            assert p.sub_kind == "some_pages", p.id


def test_two_page_probes_name_words_of_both_pages_and_no_page_holds_them_all():
    for p in of("two_page"):
        first, second = (fold(fallback_probes.plant_text(fact_id, plant)) for fact_id, plant in p.expected)
        ws = [w for w in words(p.query) if w != "and"]
        assert any(w in first and w not in second for w in ws), p.id
        assert any(w in second and w not in first for w in ws), p.id
        assert all(w in first or w in second for w in ws), p.id


def test_none_probes_are_half_absent_words_and_half_an_absent_topic_with_a_common_word():
    kinds = Counter(p.sub_kind for p in of("none"))
    assert set(kinds) == {"absent", "mixed"} and abs(kinds["absent"] - kinds["mixed"]) <= 1, kinds
    for half in ("tune", "held_out"):
        here = Counter(p.sub_kind for p in of("none") if fallback_probes.split_of(p) == half)
        assert abs(here["absent"] - here["mixed"]) <= 1, (half, here)
    for p in of("none"):
        ws = words(p.query)
        assert p.absent and all(fold(w) in ws for w in p.absent), p.id
        common = [w for w in ws if w not in {fold(a) for a in p.absent}]
        assert bool(common) == (p.sub_kind == "mixed"), p.id


def test_words_said_to_be_on_no_page_are_in_no_text_a_page_is_built_from():
    text = background()
    for p in PROBES:
        if p.cls == "none":
            absent = list(p.absent)
        elif p.cls == "typo" or (p.cls == "extra_word" and p.sub_kind == "no_page"):
            absent = [p.altered]
        else:
            continue
        assert not [w for w in absent if fold(w) in text], p.id  # at any store size
    for p in PROBES:  # and the words said to be on many pages come from a distractor or a plant
        if p.sub_kind in ("some_pages", "common_word"):
            assert fold(p.altered) in text, p.id
        for w in fallback_probes.common_words(p) if p.cls == "none" else ():
            assert fold(w) in text, (p.id, w)


def test_none_probes_find_nothing_and_no_page_is_about_them(small):
    _, store, _ = small
    for p in of("none"):
        assert store.search(p.query) == [], p.id
        for w in p.absent:
            assert store.search(w) == [], (p.id, w)  # on no page, as a word or inside one
        for w in fallback_probes.common_words(p):
            assert store.search(w), (p.id, w)  # the common word is on some page


def test_exact_probes_are_found_by_strict_search_and_altered_ones_are_not(small):
    _, store, ids = small
    for p in PROBES:
        if p.cls == "exact":
            assert set(fallback_probes.expected_pages(p, ids)) <= set(store.search(p.query)), p.id
        elif p.cls in ("typo", "identifier", "two_page") or p.sub_kind == "no_page":
            # (a word that many pages hold may be on the expected page of a 40-page store by chance)
            assert not set(fallback_probes.expected_pages(p, ids)) & set(store.search(p.query)), p.id
            if p.cls == "typo":
                assert store.search(p.altered) == [], p.id  # the typo is on no page


# The corpus


def test_default_corpus_is_the_one_main_wrote_before_extra_plants():
    assert turns_digest(60) == DEFAULT_CORPUS_60
    assert turns_digest(60, extra=()) == DEFAULT_CORPUS_60
    assert turns_digest(60, extra=EXTRA_FACTS) != DEFAULT_CORPUS_60  # the digest does see a planted page
    assert turns_digest(6000, [0, 1499, 2220, 5999]) == DEFAULT_SAMPLE_6000


def test_default_store_has_the_same_page_ids_with_and_without_the_extra_argument(tmp_path: Path):
    n = 30
    built = scale_bench.build_store(tmp_path, "s", n, dialect="messages", floor=0.0, seed=7)
    store = SessionStore(tmp_path, "s")
    # the same turns written again, as main generated them: page_out returns the id each already has
    again = [store.page_out([scale_synth.session_turn(t, n, seed=7, floor=0.0)], t)[0] for t in range(n)]
    assert again == built["ids"] and len(store.page_ids()) == n
    # with the extra plants only the pages they land on get another id
    changed = {planted_turn(p.at, n) for f in EXTRA_FACTS for p in f.plants}
    extended = [store.page_out([scale_synth.session_turn(t, n, seed=7, floor=0.0, extra=EXTRA_FACTS)], t)[0]
                for t in range(n)]
    assert {t for t in range(n) if extended[t] != built["ids"][t]} == changed
    assert len(changed) == len(EXTRA_FACTS)


def test_extra_plants_sit_on_their_own_pages_and_add_no_probe_string():
    used = {round(p.at, 4) for f in FACTS for p in f.plants}
    ats = [p.at for f in EXTRA_FACTS for p in f.plants]
    assert len(set(ats)) == len(ats) and not {round(a, 4) for a in ats} & used
    turns = {planted_turn(p.at, 6000) for f in FACTS for p in f.plants}
    assert not {planted_turn(a, 6000) for a in ats} & turns  # no page of an existing fact changes at 6,000
    pool_text = fold("\n".join(line for lines in scale_synth.pools().values() for line in lines))
    bare_pool = re.sub(r"[_\-.]", "", pool_text)
    for fact in EXTRA_FACTS:
        assert not fact.queries and not fact.near and fact.rename is None  # nothing is drawn for them
        for plant in fact.plants:
            text = "\n".join([plant.user, plant.assistant, plant.tool_result])
            assert plant.marker in text
            assert not [w for w in scale_synth.forbidden() if w in text], fact.id
    for p in of("identifier"):
        if p.expected[0][0] in {f.id for f in EXTRA_FACTS}:
            assert re.sub(r"[_\-.]", "", fold(p.original)) not in bare_pool, p.id  # only its plant holds it


def test_extra_markers_are_on_their_planted_page_only(small):
    _, store, ids = small
    for fact in EXTRA_FACTS:
        (plant,) = fact.plants
        assert store.search(plant.marker) == [ids[planted_turn(plant.at, SMALL)]], fact.id


# The bench


def rows_of(folder: Path, name: str = "quality") -> list[dict]:
    with (folder / f"{name}.csv").open(newline="") as f:
        return list(csv.DictReader(f))


def files_of(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())}


class Hit:
    def __init__(self, page_id: str) -> None:
        self.id = page_id


class FakeStore:
    def __init__(self, strict: list[str], approximate: list[str] | None) -> None:
        self.strict = strict
        if approximate is not None:
            self.search_approximate = lambda query: [Hit(i) for i in approximate]

    def search(self, query: str) -> list[str]:
        return self.strict


class FakeChild:
    """Stands in for an MCP child: every call takes ``seconds`` and is recorded."""

    def __init__(self, seconds: float, calls: list) -> None:
        self.seconds, self.calls, self.startup_s = seconds, calls, 0.5

    def call(self, name: str, arguments: dict) -> tuple[float, str]:
        self.calls.append((self.seconds, name, arguments["query"]))
        return self.seconds, "no pages"

    def close(self) -> int:
        return 40_000_000


def test_ranked_ids_come_from_strict_search_and_from_the_fallback_only_when_strict_is_empty():
    assert fallback_bench.ranked(FakeStore(["pg_a"], ["pg_b"]), "q") == (["pg_a"], "strict", 0)
    assert fallback_bench.ranked(FakeStore([], ["pg_b", "pg_c"]), "q") == (["pg_b", "pg_c"], "fallback", 0)
    assert fallback_bench.ranked(FakeStore([], []), "q") == ([], "none", 0)
    assert fallback_bench.ranked(FakeStore([], None), "q") == ([], "none", 0)  # main: no search_approximate


def test_a_page_the_fallback_returns_twice_is_counted_and_keeps_its_first_rank(tmp_path: Path):
    ranked = fallback_bench.ranked(FakeStore([], ["pg_b", "pg_c", "pg_b", "pg_d", "pg_c"]), "q")
    assert ranked == (["pg_b", "pg_c", "pg_d"], "fallback", 2)
    rows = [quality_row(0, "typo", "tune", source="fallback", returned=3, duplicates=2),
            quality_row(1, "typo", "tune", source="fallback", returned=3, duplicates=0)]
    write_quality(tmp_path, rows)
    assert "- probes the fallback answered with a page id more than once: 1 of 2 (0 is right)." in \
        fallback_bench.summarize(tmp_path)


def test_score_counts_a_probe_at_k_only_when_every_expected_page_is_in_the_top_k():
    ranked = [f"pg_{i}" for i in range(12)]
    assert fallback_bench.score(["pg_2"], ranked) == {"ranks": "3", "top3": True, "top10": True, "any3": True,
                                                      "any10": True}
    assert fallback_bench.score(["pg_1", "pg_7"], ranked) == {"ranks": "2;8", "top3": False, "top10": True,
                                                              "any3": True, "any10": True}
    assert fallback_bench.score(["pg_11", "pg_x"], ranked) == {"ranks": "12;-", "top3": False, "top10": False,
                                                               "any3": False, "any10": False}
    assert fallback_bench.score([], ranked) == {"ranks": "", "top3": "", "top10": "", "any3": "", "any10": ""}


def test_ceiling_query_spells_the_probe_the_pages_way():
    by_id = {p.id: p for p in PROBES}
    ceiling = fallback_bench.ceiling_query
    assert ceiling(by_id["identifier-03"]) == "refreshToken"
    assert ceiling(by_id["identifier-04"]) == "replaced fetch_rows"  # the other words stay
    assert ceiling(by_id["typo-02"]) == "Redis eviction policy" and ceiling(by_id["typo-16"]) == "allkeys-lfu"
    assert ceiling(by_id["extra_word-13"]) == "HTTP client timeout"
    assert ceiling(by_id["exact-01"]) == "staging DB port"
    assert ceiling(by_id["question-10"]) == "What is the max upload size"  # only the question mark goes
    assert ceiling(by_id["paraphrase-01"]) == ceiling(by_id["two_page-01"]) == ceiling(by_id["none-01"]) == ""


def test_latest_first_needs_the_last_plant_above_every_older_plant_of_a_changing_fact():
    ids = [f"pg_{t:016x}" for t in range(6000)]
    by_id = {p.id: p for p in PROBES}
    old, mid, new = (ids[planted_turn(at, 6000)] for at in (0.10, 0.45, 0.85))  # c01's three plants
    probe = by_id["exact-13"]
    latest = fallback_bench.latest_first
    assert latest(probe, ids, [new, old, mid]) is True and latest(probe, ids, ["pg_x", new]) is True
    assert latest(probe, ids, [old, new, mid]) is False  # a stale value first, though all three are in the top 3
    assert latest(probe, ids, [old, mid]) is False and latest(probe, ids, []) is False
    assert latest(by_id["exact-01"], ids, []) == "" and latest(by_id["none-01"], ids, []) == ""  # no changing fact
    both = by_id["two_page-04"]  # c01 and c08, both changing
    c08 = [ids[planted_turn(at, 6000)] for at in (0.22, 0.57, 0.88)]
    assert latest(both, ids, [new, c08[2], old, c08[0]]) is True
    assert latest(both, ids, [new, c08[0], c08[2]]) is False


def test_tool_pages_reads_detailed_and_listed_lines_and_ignores_the_rest():
    text = "\n".join(["[approximate matches: no page holds every word]", "pg_00000000000000aa (turn 9)",
                      "> 12: a line", "  pg_00000000000000ee (turn 3)", "pg_00000000000000bb (turn 4)",
                      "[more matching pages, newest first; recall one at its offset:]",
                      "pg_00000000000000cc (turn 2) @40", "[approximate] pg_00000000000000dd (turn 1) @0",
                      "[7 more matching pages not shown; use a longer or more specific query]"])
    assert fallback_bench.tool_pages(text) == (["pg_00000000000000aa", "pg_00000000000000bb"],
                                               ["pg_00000000000000cc"])
    assert fallback_bench.tool_pages("No pages match.") == ([], [])


def test_quality_run_writes_one_row_per_probe_and_the_tables(run):
    folder, _ = run
    rows = rows_of(folder)
    assert [r["id"] for r in rows] == [p.id for p in PROBES]
    assert set(rows[0]) == set(fallback_bench.QUALITY_FIELDS)
    by_id = {r["id"]: r for r in rows}
    for p in PROBES:
        r = by_id[p.id]
        assert (r["class"], r["query"], r["split"]) == (p.cls, p.query, fallback_probes.split_of(p))
        assert r["flags"].split() == list(p.flags)
        assert r["source"] in ("strict", "fallback", "none")
        assert (r["source"] == "strict") == (int(r["strict_pages"]) > 0)
        assert r["ceiling_query"] == fallback_bench.ceiling_query(p)
        assert (r["reach3"] == "") == (r["ceiling_query"] == "")
    exact = [r for r in rows if r["class"] == "exact"]
    assert all(r["source"] == "strict" and r["top10"] == "True" for r in exact)  # the control
    assert all((r["reach3"], r["reach10"], r["ceiling_ranks"]) == (r["top3"], r["top10"], r["ranks"]) for r in exact)
    assert all(r["tool_detailed"] == "1" for r in exact if r["top3"] == "True")  # the tool shows the top 3
    assert all(r["altered_pages"] == "0" and r["strict_pages"] == "0" for r in rows if r["class"] == "none")
    typos = [r for r in rows if r["class"] == "typo"]
    assert all(r["altered_pages"] == "0" and r["reach10"] == "True" for r in typos)  # spelled right, it is found
    assert {r["latest_first"] for r in rows if r["expected"].startswith("c")} <= {"True", "False"}
    assert {r["latest_first"] for r in rows if r["expected"].startswith("s") and r["class"] != "two_page"} == {""}
    summary = (folder / "summary.md").read_text(encoding="utf-8")
    for heading in (f"## Recall at {SMALL} pages", "## Groups", "## Nothing to find", "## Through the tool",
                    "## Probe checks"):
        assert heading in summary, heading
    assert "| exact | all | 15 | " in summary and "| two_page, either page | all | 10 | " in summary
    assert "| typo, the misspelled word carries the query | 15 | " in summary
    assert "| typo, the misspelled word carries the query, without identifier typos | 9 | " in summary
    assert "| typo, the misspelled word carries the query, identifier typos only | 6 | " in summary
    assert all(r["duplicates"] == "0" for r in rows)  # main has no fallback
    assert "| identifier, the identifier alone | 20 | " in summary
    assert "- exact probes whose page strict search returns: 15 of 15." in summary
    assert "absent word on some page: 0 of 16 (0 is right)." in summary
    assert f"- Quality: commit {scale_bench.git_head()[:8]}" in summary and f", {SMALL} pages, " in summary
    assert "## Latency" not in summary and "- Latency:" not in summary  # --quality-only
    assert not list(folder.glob("*.tmp"))


def test_env_records_each_part_with_what_makes_two_runs_comparable(run):
    folder, _ = run
    env = json.loads((folder / "env.json").read_text(encoding="utf-8"))
    assert set(env) == {"label", "quality"} and env["label"] == "before"
    part = env["quality"]
    assert {"commit", "python", "os", "cpu", "sizes", "probes", "probe_set", "corpus", "search_approximate",
            "started", "finished"} <= set(part)
    assert part["sizes"] == [SMALL] and part["probes"] == len(PROBES)
    assert part["probe_set"] == fallback_bench.probe_set_id() and len(part["probe_set"]) == 16
    assert part["corpus"] == {"id": fallback_bench.corpus_id(), "seed": 7, "floor": 0.0,
                              "extra_plants": [f.id for f in EXTRA_FACTS]}
    text = json.dumps(env)
    assert str(Path.home()) not in text and "hostname" not in text  # numbers and versions only


def quality_row(i: int, cls: str, split: str, **kw) -> dict:
    base = dict.fromkeys(fallback_bench.QUALITY_FIELDS, "")
    base.update({"n": 60, "id": f"{cls}-{i}", "class": cls, "split": split, "source": "none", "strict_pages": 0,
                 "returned": 0, "tool_detailed": 0, "tool_listed": 0, "tool_pages": 0, "expected": "s01:0",
                 "top3": False, "top10": False, "any3": False, "any10": False})
    base.update(kw)
    return base


def write_quality(folder: Path, rows: list[dict]) -> None:
    with (folder / "quality.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fallback_bench.QUALITY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_summary_counts_recall_reach_and_latest_first_per_class_half_and_group(tmp_path: Path):
    hit = {"top3": True, "top10": True, "any3": True, "any10": True}
    rows = [quality_row(0, "typo", "tune", source="fallback", returned=4, **hit, ceiling_query="x", reach3=True,
                        reach10=True, sub_kind="swap", flags="carries_query after_review", altered_pages=0),
            quality_row(1, "typo", "held_out", source="fallback", returned=20, top10=True, any10=True,
                        ceiling_query="x", reach3=False, reach10=True, latest_first=True, sub_kind="deletion",
                        flags="short_typo", altered_pages=0),
            quality_row(2, "typo", "tune", ceiling_query="x", reach3=True, reach10=True, latest_first=False,
                        sub_kind="swap", altered_pages=3),
            quality_row(0, "two_page", "tune", source="fallback", returned=9, any3=True, any10=True),
            quality_row(0, "paraphrase", "tune", source="strict", strict_pages=25, returned=25, ranks="-",
                        query="staging database port", altered="database", altered_pages=25),
            quality_row(0, "none", "tune", sub_kind="absent", altered_pages=0, expected=""),
            quality_row(1, "none", "held_out", sub_kind="absent", altered_pages=0, expected=""),
            quality_row(2, "none", "tune", sub_kind="mixed", source="fallback", returned=7, tool_pages=3,
                        altered_pages=0, expected=""),
            quality_row(3, "none", "held_out", sub_kind="mixed", altered_pages=0, expected="")]
    write_quality(tmp_path, rows)
    summary = fallback_bench.summarize(tmp_path)
    for line in ("| typo | tune | 2 | 1 | 2 | 1 | 2 | 0 of 1 | 0 / 1 / 1 |",
                 "| typo | held-out | 1 | 0 | 0 | 1 | 1 | 1 of 1 | 0 / 1 / 0 |",
                 "| typo | all | 3 | 1 (33%) | 2 | 2 (67%) | 3 | 1 of 2 | 0 / 2 / 1 |",
                 "| two_page | all | 1 | 0 (0%) | - | 0 (0%) | - | - | 0 / 1 / 0 |",
                 "| two_page, either page | all | 1 | 1 (100%) | - | 1 (100%) | - | - | 0 / 1 / 0 |",
                 "| typo, swap | 2 | 1 (50%) | 2 | 1 (50%) | 2 | 0 of 1 | 0 / 1 / 1 |",
                 "| typo, deletion | 1 | 0 (0%) | 0 | 1 (100%) | 1 | 1 of 1 | 0 / 1 / 0 |",
                 "| typo, the misspelled word carries the query | 1 | 1 (100%) | 1 | 1 (100%) | 1 | - | 0 / 1 / 0 |",
                 "| typo, the other words are the page's own | 2 | 0 (0%) | 1 | 1 (50%) | 2 | 1 of 2 | 0 / 1 / 1 |",
                 "| typo, the misspelled word carries the query, without identifier typos | 1 | 1 (100%) | 1 | 1 (100%) "
                 "| 1 | - | 0 / 1 / 0 |",
                 "| typo, typed word under 6 characters | 1 | 0 (0%) | 0 | 1 (100%) | 1 | 1 of 1 | 0 / 1 / 0 |",
                 "| paraphrase, off-page word on some page | 1 | 0 (0%) | - | 0 (0%) | - | - | 1 / 0 / 0 |",
                 "| paraphrase-0 | staging database port | 25 |",
                 "| absent | 2 | 0/1 | 0/1 | 0/2 (0%) | 0 | 0 |",
                 "| mixed | 2 | 1/1 | 0/1 | 1/2 (50%) | 3.5 | 1 |",
                 "| all | 4 | 1/2 | 0/2 | 1/4 (25%) | 0 | 1 |"):
        assert line in summary, line
    assert "paraphrase, off-page word on no page" not in summary  # an empty group has no row
    assert "identifier typos only" not in summary
    assert "typo, off-page word" not in summary  # a typo is on no page by construction: no split


def test_groups_take_a_flag_added_later_from_the_probe_set(tmp_path: Path):
    """A row written before ``identifier_typo`` existed is grouped by the flags its probe has now."""
    hit = {"top3": True, "top10": True, "any3": True, "any10": True}
    rows = [quality_row(16, "typo", "tune", query="allkeys-lru", flags="carries_query after_review",
                        source="fallback", returned=2, **hit),
            quality_row(21, "typo", "tune", query="aragon2id", flags="carries_query after_review"),
            quality_row(99, "typo", "tune", query="allkeys-lru", flags="carries_query")]  # not a probe of the set
    write_quality(tmp_path, rows)
    summary = fallback_bench.summarize(tmp_path)
    for line in ("| typo, the misspelled word carries the query | 3 | 1 (33%) |",
                 "| typo, the misspelled word carries the query, identifier typos only | 1 | 1 (100%) |",
                 "| typo, the misspelled word carries the query, without identifier typos | 2 | 0 (0%) |"):
        assert line in summary, line
    measured = fallback_bench.measures(tmp_path)
    assert measured["typo, the misspelled word carries the query, identifier typos only (1): top 3 / top 10"] == "1 / 1"


def test_a_later_run_reuses_the_store(run, small, capsys):
    _, home = run
    _, _, ids = small
    manifest = home / f"d65-{SMALL}.json"
    written = manifest.stat().st_mtime_ns
    capsys.readouterr()
    assert fallback_bench.open_store(home, SMALL) == ids and len(ids) == SMALL
    assert "reusing" in capsys.readouterr().out and manifest.stat().st_mtime_ns == written


def test_compare_prints_what_each_folder_is_and_two_runs_as_one_table(run, tmp_path: Path, capsys):
    folder, _ = run
    after = tmp_path / "after"
    shutil.copytree(folder, after)
    env = json.loads((after / "env.json").read_text(encoding="utf-8"))
    env["quality"]["search_approximate"] = True
    (after / "env.json").write_text(json.dumps({**env, "label": "after"}), encoding="utf-8")
    capsys.readouterr()
    assert fallback_bench.main(["--compare", str(folder), str(after)]) == 0
    table = capsys.readouterr().out
    lines = [line for line in table.splitlines() if line.startswith("|")]
    assert lines[0] == "| measure | before | after |" and len(lines) == len(table.strip().splitlines())
    commit = env["quality"]["commit"][:8]
    for line in (f"| pages | {SMALL} | {SMALL} |", f"| commit, quality part | {commit} | {commit} |",
                 "| commit, latency part | - | - |",
                 f"| probes (probe set) | {len(PROBES)} ({env['quality']['probe_set']}) | "
                 f"{len(PROBES)} ({env['quality']['probe_set']}) |",
                 "| `search_approximate` found, quality part | no | yes |",
                 "| `search_approximate` found, latency part | - | - |"):
        assert line in lines, line
    assert any(line.startswith("| exact, all (15): top 3 / top 10 |") for line in lines)
    assert any(line.startswith("| none, all (16): probes with a page |") for line in lines)
    assert any(line.startswith("| typo, the misspelled word carries the query (15): top 3 / top 10 |") for line in lines)


def test_compare_refuses_folders_that_are_not_comparable(run, tmp_path: Path, capsys):
    folder, _ = run
    env = json.loads((folder / "env.json").read_text(encoding="utf-8"))
    for name, change in (("pages", {"sizes": [300]}), ("probes", {"probe_set": "0" * 16}),
                         ("corpus", {"corpus": {**env["quality"]["corpus"], "extra_plants": []}})):
        other = tmp_path / name
        shutil.copytree(folder, other)
        (other / "env.json").write_text(json.dumps({**env, "quality": {**env["quality"], **change}}), encoding="utf-8")
        capsys.readouterr()
        assert fallback_bench.main(["--compare", str(folder), str(other)]) == 2, name
        captured = capsys.readouterr()
        assert captured.out == "" and "not comparable" in captured.err, name
    smaller = tmp_path / "smaller"  # a whole, consistent run at another size
    shutil.copytree(folder, smaller)
    (smaller / "env.json").write_text(json.dumps({**env, "quality": {**env["quality"], "sizes": [17]}}), encoding="utf-8")
    rows = rows_of(folder)
    with (smaller / "quality.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fallback_bench.QUALITY_FIELDS)
        writer.writeheader()
        writer.writerows({**r, "n": 17} for r in rows)
    assert fallback_bench.main(["--compare", str(folder), str(smaller)]) == 2
    assert "differ in store size" in capsys.readouterr().err
    assert fallback_bench.main(["--compare", str(folder), str(tmp_path / "missing")]) == 2
    assert "no results" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert fallback_bench.main(["--compare", str(empty), str(folder)]) == 2


def test_a_run_that_fails_leaves_the_folder_as_it_was(run, tmp_path: Path, safe_home, monkeypatch):
    folder, home = run
    out = tmp_path / "results"
    shutil.copytree(folder, out / "before")
    before = files_of(out / "before")

    def broken(home, n, ids, writer):
        writer.writerow(dict.fromkeys(fallback_bench.QUALITY_FIELDS, "x"))  # some rows, then the fallback raises
        raise RuntimeError("search_approximate raised")

    monkeypatch.setattr(fallback_bench, "quality", broken)
    with pytest.raises(RuntimeError):
        fallback_bench.main(["--sizes", str(SMALL), "--quality-only", "--out", str(out), "--store-home", str(home)])
    assert files_of(out / "before") == before  # the earlier run, whole: no half-written csv, no temporary file
    fresh = tmp_path / "new" / "results"
    with pytest.raises(RuntimeError):
        fallback_bench.main(["--sizes", str(SMALL), "--quality-only", "--out", str(fresh), "--label", "first",
                             "--store-home", str(home)])
    assert not (tmp_path / "new").exists()  # a run that fails on first use of a label leaves no empty folder
    assert not safe_home.exists()


def test_one_part_run_again_keeps_the_other_parts_record(run, tmp_path: Path, safe_home, monkeypatch):
    folder, home = run
    out = tmp_path / "results"
    shutil.copytree(folder, out / "before")
    quality_part = json.loads((folder / "env.json").read_text(encoding="utf-8"))["quality"]
    calls: list = []
    monkeypatch.setattr(scale_bench, "McpChild", lambda home, session, **kw: FakeChild(0.01, calls))
    assert fallback_bench.main(["--sizes", str(SMALL), "--latency-only", "--passes", "2", "--reps", "1", "--out",
                                str(out), "--store-home", str(home)]) == 0
    env = json.loads((out / "before" / "env.json").read_text(encoding="utf-8"))
    assert set(env) == {"label", "quality", "latency"} and env["quality"] == quality_part
    assert (env["latency"]["passes"], env["latency"]["reps"], env["latency"]["sizes"]) == (2, 1, [SMALL])
    assert rows_of(out / "before") == rows_of(folder)  # quality.csv is the earlier run's
    summary = (out / "before" / "summary.md").read_text(encoding="utf-8")
    assert "- Quality: commit " in summary and "- Latency: commit " in summary
    assert f"## Recall at {SMALL} pages" in summary and f"## Latency at {SMALL} pages" in summary
    assert "were not measured on the same" not in summary
    # a latency part from another store size is named as such
    assert env["latency"]["queries"] == {str(SMALL): [list(q) for q in fallback_bench.latency_queries(
        SessionStore(home, f"d65-{SMALL}"))]}
    for change, named in (({"sizes": [37]}, "store size"), ({"search_approximate": True}, "`search_approximate`"),
                          ({"src_matches_commit": False}, "uncommitted changes under src/")):
        (out / "before" / "env.json").write_text(json.dumps({**env, "latency": {**env["latency"], **change}}),
                                                 encoding="utf-8")
        warning = [line for line in fallback_bench.summarize(out / "before").splitlines()
                   if "were not measured on the same" in line]
        assert len(warning) == 1 and named in warning[0], (change, warning)
    assert not safe_home.exists()


def test_compare_refuses_latency_runs_that_timed_different_queries(run, tmp_path: Path, safe_home, monkeypatch, capsys):
    folder, home = run
    calls: list = []
    monkeypatch.setattr(scale_bench, "McpChild", lambda home, session, **kw: FakeChild(0.01, calls))
    out = tmp_path / "results"
    for label in ("one", "two"):
        shutil.copytree(folder, out / label)
        assert fallback_bench.main(["--sizes", str(SMALL), "--latency-only", "--passes", "2", "--reps", "1",
                                    "--out", str(out), "--label", label, "--store-home", str(home)]) == 0
    capsys.readouterr()
    assert fallback_bench.main(["--compare", str(out / "one"), str(out / "two")]) == 0
    capsys.readouterr()
    env = json.loads((out / "two" / "env.json").read_text(encoding="utf-8"))
    timed = env["latency"]["queries"][str(SMALL)]
    env["latency"]["queries"] = {str(SMALL): timed[:-1]}
    (out / "two" / "env.json").write_text(json.dumps(env), encoding="utf-8")
    assert fallback_bench.main(["--compare", str(out / "one"), str(out / "two")]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "timed different queries" in captured.err and timed[-1][1] in captured.err
    # a run from before the list was recorded: the list is read from its latency.csv
    del env["latency"]["queries"]
    (out / "two" / "env.json").write_text(json.dumps(env), encoding="utf-8")
    assert fallback_bench.main(["--compare", str(out / "one"), str(out / "two")]) == 0
    capsys.readouterr()
    rows = rows_of(out / "two", "latency")
    with (out / "two" / "latency.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fallback_bench.LATENCY_FIELDS)
        writer.writeheader()
        writer.writerows(r for r in rows if r["query"] != timed[0][1])
    assert fallback_bench.main(["--compare", str(out / "one"), str(out / "two")]) == 2
    assert "timed different queries" in capsys.readouterr().err


def test_a_store_that_is_not_whole_is_built_again(tmp_path: Path, capsys):
    home = tmp_path / "home"
    first = fallback_bench.open_store(home, 20)
    store = SessionStore(home, "d65-20")
    (store.pages_dir / f"{first[3]}.json").unlink()  # a build that was cut short
    capsys.readouterr()
    assert fallback_bench.open_store(home, 20) != [] and "building" in capsys.readouterr().out
    assert len(SessionStore(home, "d65-20").page_ids()) == 20


def test_the_store_home_may_not_be_the_real_contexto_home(tmp_path: Path, safe_home, monkeypatch):
    out = tmp_path / "results"
    for under in ("", "sessions", "sessions/d65"):
        with pytest.raises(SystemExit, match="~/.contexto"):
            fallback_bench.main(["--sizes", "20", "--out", str(out), "--store-home",
                                 str(safe_home / ".contexto" / under)])
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "live"))
    for under in ("", "sessions"):
        with pytest.raises(SystemExit, match="CONTEXTO_HOME"):
            fallback_bench.main(["--sizes", "20", "--out", str(out), "--store-home", str(tmp_path / "live" / under)])
    fallback_bench.refuse_live_home(safe_home / ".contexto-bench")  # a sibling is not the live home
    fallback_bench.refuse_live_home(tmp_path / "live-bench")
    assert not out.exists() and not safe_home.exists() and not (tmp_path / "live").exists()  # nothing was written


def test_sigterm_removes_the_temporary_store(tmp_path: Path, safe_home, monkeypatch):
    made = tmp_path / "store"

    def mkdtemp(prefix: str) -> str:
        made.mkdir()
        return str(made)

    def killed(home, n):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM did not stop the run")

    monkeypatch.setattr(fallback_bench.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(fallback_bench, "open_store", killed)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as stopped:
        fallback_bench.main(["--sizes", "20", "--quality-only", "--out", str(tmp_path / "results")])
    assert stopped.value.code == 128 + signal.SIGTERM
    assert not made.exists() and not list((tmp_path / "results").rglob("*.csv*"))
    assert signal.getsignal(signal.SIGTERM) is before  # the handler is put back


def test_latency_queries_are_the_strict_hits_three_misses_per_class_and_the_costly_shapes(run):
    _, home = run
    store = SessionStore(home, f"d65-{SMALL}")
    queries = fallback_bench.latency_queries(store)
    kinds = Counter(kind for kind, _ in queries)
    assert {k: v for k, v in kinds.items() if k not in dict(fallback_bench.TIMED_PROBES)} == {
        "rare": 3, "common": 3, "multi": 3, "miss_identifier": 3, "miss_question": 3, "miss_typo": 3,
        "miss_extra_word": 3, "miss_none": 3, "miss_paraphrase": 3}
    assert kinds["miss_typo_carried"] == 3  # a typo is a strict miss at every size
    assert [kind for kind, _ in queries].index("miss_identifier") == 9  # the first strict miss, as on main
    by_query = {p.query: p for p in PROBES}
    fixed = {i for _, ids in fallback_bench.TIMED_PROBES for i in ids}
    for kind, query in queries:
        if kind.startswith("miss_"):
            assert store.search(query) == [], query
            assert kind.removeprefix("miss_").startswith(by_query[query].cls), query
        if kind in dict(fallback_bench.TIMED_PROBES):
            assert by_query[query].id in dict(fallback_bench.TIMED_PROBES)[kind], query
    assert {"typo-16", "typo-21", "typo-24", "extra_word-16", "extra_word-18", "paraphrase-33",
            "paraphrase-36"} <= fixed


def test_the_fixed_timed_probes_are_strict_misses_of_the_6000_page_store():
    rows = {r["id"]: r for r in rows_of(BEFORE)}
    fixed = [i for _, ids in fallback_bench.TIMED_PROBES for i in ids]
    assert [i for i in fixed if rows[i]["source"] != "none"] == []


def latency_rows(passes: dict[int, dict[str, float]], miss: dict[int, dict[str, float]]) -> list[dict]:
    """latency.csv rows of a two-sided run: per pass, each side's first search and first strict miss."""
    rows = []
    for p in sorted(passes):
        names = ["tree", "baseline"] if p % 2 else ["baseline", "tree"]
        base = {"n": 6000, "pass": p, "load1": f"{10 * p:.2f}", "rep": 0, "chars": 9}
        rows += [{**base, "side": side, "kind": kind, "query": "E4127", "seconds": passes[p][side]}
                 for side, kind in zip(names, fallback_bench.FIRST)]
        rows += [{**base, "side": side, "kind": "rare", "query": "E4127", "seconds": 0.5} for side in names]
        for rep in range(3):
            rows += [{**base, "rep": rep, "side": side, "kind": "miss_identifier", "query": "refresh_token",
                      "seconds": miss[p][side] if rep == 0 else 0.5} for side in names]
    return rows


def write_latency(folder: Path, rows: list[dict]) -> None:
    with (folder / "latency.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fallback_bench.LATENCY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_first_calls_are_compared_within_a_pass_and_paired_so_position_cancels(tmp_path: Path):
    # the side that goes first is 1.5 times slower, and pass 1 and 2 run under twice the load of 3 and 4
    first = {1: {"tree": 12.0, "baseline": 8.0}, 2: {"tree": 8.0, "baseline": 12.0},
             3: {"tree": 6.6, "baseline": 4.0}, 4: {"tree": 4.4, "baseline": 6.0}}
    miss = {1: {"tree": 3.0, "baseline": 1.0}, 2: {"tree": 3.0, "baseline": 1.0},
            3: {"tree": 3.0, "baseline": 1.0}, 4: {"tree": 3.0, "baseline": 1.0}}
    write_latency(tmp_path, latency_rows(first, miss))
    summary = fallback_bench.summarize(tmp_path)
    # within each pass: 1.5, 0.667, 1.65, 0.733; pairs sqrt(1.5 * 0.667) = 1.00 and sqrt(1.65 * 0.733) = 1.10
    for line in ("| first search | E4127 | 7.300 | 7.000 | 1.05 | passes 1-2: 1.00; passes 3-4: 1.10 |",
                 "| first strict miss | refresh_token | 3.000 | 1.000 | 3.00 | passes 1-2: 3.00; passes 3-4: 3.00 |",
                 "| 1 | 10.00 | tree | 12.000 | 8.000 | 1.50 | 3.000 | 1.000 | 3.00 |",
                 "| 2 | 20.00 | baseline | 8.000 | 12.000 | 0.67 | 3.000 | 1.000 | 3.00 |"):
        assert line in summary, line
    assert "| miss_identifier | 1 | 0.500 | 0.500 | 1.00 |" in summary  # the one-time cost is lost in the median
    assert "| first (searched" not in summary  # no ratio across passes
    measured = fallback_bench.measures(tmp_path)
    assert measured["latency, first search of the process, s"] == \
        "7.300 (baseline 7.000); tree / baseline paired 1.05, pairs 1.00, 1.10"
    assert measured["latency, first strict miss of the process, s"] == \
        "3.000 (baseline 1.000); tree / baseline paired 3.00, pairs 3.00, 3.00"
    first[5], miss[5] = {"tree": 1.0, "baseline": 1.0}, {"tree": 1.0, "baseline": 1.0}  # a fifth pass has no pair
    write_latency(tmp_path, latency_rows(first, miss))
    assert "Pass 5 has no pair" in fallback_bench.summarize(tmp_path)


def test_latency_pass_warms_the_cache_and_times_the_first_search_in_both_orders(run, tmp_path: Path):
    _, home = run
    calls: list = []
    sides = {"tree": lambda h, s: FakeChild(0.02, calls), "baseline": lambda h, s: FakeChild(0.01, calls)}
    queries = [("rare", "E4127"), ("miss_typo", "stagng DB port")]
    with (tmp_path / "latency.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fallback_bench.LATENCY_FIELDS)
        writer.writeheader()
        for pass_no in (1, 2):
            fallback_bench.latency_pass(home, SMALL, pass_no, 2, queries, writer, sides)
    rows = rows_of(tmp_path, "latency")
    timed = [r for r in rows if r["kind"] in ("rare", "miss_typo")]
    assert len(timed) == 2 * 2 * 2 * 2  # passes, queries, reps, sides
    assert Counter(r["side"] for r in timed) == {"tree": 8, "baseline": 8}
    order = {p: [r["side"] for r in timed if r["pass"] == p and r["query"] == "E4127"] for p in ("1", "2")}
    assert order == {"1": ["tree", "baseline", "baseline", "tree"],  # neither side always goes first
                     "2": ["baseline", "tree", "tree", "baseline"]}
    # in each pass a throwaway child searches before either side, then each side's first search is timed
    for p, (first, second) in (("1", ("tree", "baseline")), ("2", ("baseline", "tree"))):
        kinds = [(r["side"], r["kind"]) for r in rows if r["pass"] == p][:6]
        assert kinds == [("warm-up", "warm"), (first, "startup"), (second, "startup"),
                         (first, "first (searched first)"), (second, "first (searched second)"), (first, "rare")]
    assert {(r["side"], r["kind"]) for r in rows if r["kind"] == "peak_rss"} == {("tree", "peak_rss"),
                                                                                  ("baseline", "peak_rss")}
    assert all(float(r["load1"]) >= 0 for r in rows)
    assert len(calls) == len(timed) + 2 * 3  # a warm-up search and two first searches in each pass
    summary = fallback_bench.summarize(tmp_path)
    assert f"## Latency at {SMALL} pages" in summary and f"## First calls of a process at {SMALL} pages" in summary
    for line in ("| miss_typo | 1 | 0.020 | 0.010 | 2.00 |",
                 "| first search | E4127 | 0.020 | 0.010 | 2.00 | passes 1-2: 2.00 |",
                 "| first strict miss | stagng DB port | 0.020 | 0.010 | 2.00 | passes 1-2: 2.00 |"):
        assert line in summary, line
    assert "| warm" not in summary and "has no pair" not in summary
    with (tmp_path / "latency.csv").open("a", newline="") as f:  # a third pass: the tree first twice
        fallback_bench.latency_pass(home, SMALL, 3, 2, queries, csv.DictWriter(f, fallback_bench.LATENCY_FIELDS), sides)
    assert "Pass 3 has no pair" in fallback_bench.summarize(tmp_path)


class SlowFirstMiss(FakeChild):
    """A child with a one-time cost: its first strict miss takes ``extra`` seconds more."""

    def __init__(self, seconds: float, calls: list, extra: float) -> None:
        super().__init__(seconds, calls)
        self.extra, self.paid = extra, False

    def call(self, name: str, arguments: dict) -> tuple[float, str]:
        seconds, text = super().call(name, arguments)
        if arguments["query"] == "stagng DB port" and not self.paid:
            self.paid = True
            return seconds + self.extra, text
        return seconds, text


def test_a_one_time_cost_of_the_first_strict_miss_shows_in_the_summary(run, tmp_path: Path):
    _, home = run
    calls: list = []
    sides = {"tree": lambda h, s: SlowFirstMiss(0.01, calls, 3.0), "baseline": lambda h, s: FakeChild(0.01, calls)}
    queries = [("rare", "E4127"), ("miss_typo", "stagng DB port")]
    with (tmp_path / "latency.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fallback_bench.LATENCY_FIELDS)
        writer.writeheader()
        for pass_no in (1, 2):
            fallback_bench.latency_pass(home, SMALL, pass_no, 3, queries, writer, sides)
    summary = fallback_bench.summarize(tmp_path)
    assert "| miss_typo | 1 | 0.010 | 0.010 | 1.00 |" in summary  # the median of 3 calls hides it
    assert "| first strict miss | stagng DB port | 3.010 | 0.010 | 301.00 | passes 1-2: 301.00 |" in summary
    assert fallback_bench.measures(tmp_path)["latency, first strict miss of the process, s"].startswith(
        "3.010 (baseline 0.010); tree / baseline paired 301.00")


def test_an_mcp_child_that_does_not_answer_fails_the_bench_instead_of_hanging_it(tmp_path: Path, monkeypatch):
    started = []
    real = subprocess.Popen

    def silent(args, **kwargs):  # a child that reads its input and never replies
        started.append(real([sys.executable, "-c", "import sys; sys.stdin.read()"], **kwargs))
        return started[-1]

    monkeypatch.setattr(scale_bench.subprocess, "Popen", silent)
    with pytest.raises(RuntimeError, match="did not answer initialize within 0.3 s"):
        scale_bench.McpChild(tmp_path, "s", reply_timeout=0.3)
    assert started[0].wait(timeout=10) != 0  # and the child was stopped
