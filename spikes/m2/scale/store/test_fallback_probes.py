"""Fast checks of the D65 probe set and its bench (small stores only, no subprocess, no network)."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path

import fallback_bench
import fallback_probes
import pytest
import scale_bench
import scale_synth
from fallback_probes import CLASSES, EXTRA_FACTS, MINIMUM, PROBES, Probe
from scale_probes import FACTS, planted_turn

from contexto.store import SessionStore
from contexto.store.search import fold

SMALL = 40  # pages of the one store these tests build through the bench
# sha256 over the JSON of every turn of the default corpus (seed 7, floor 0), recorded on main at
# b0e2dbc before scale_synth took ``extra``. Python 3.12 to 3.15 give the same value.
DEFAULT_CORPUS_60 = "2647552be3c73245eb87d787a97ff86490883a1599c3e1141389abbb8ec56771"  # all 60 turns
DEFAULT_SAMPLE_6000 = "b466d2f86770ad3f9843ad091cca08d19a5bb48d60f260eea32b241317eb5d1a"  # turns 0, 1499, 2220, 5999


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


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> tuple[Path, Path]:
    """One quality run of the bench at ``SMALL`` pages: (the result folder, the store home it built)."""
    root = tmp_path_factory.mktemp("d65-bench")
    home, out = root / "home", root / "results"
    assert fallback_bench.main(["--sizes", str(SMALL), "--quality-only", "--label", "before", "--out", str(out),
                                "--store-home", str(home)]) == 0
    return out / "before", home


@pytest.fixture(scope="module")
def small(run) -> tuple[Path, SessionStore, list[str]]:
    """The store of that run (the extended corpus) and its page ids, one per turn."""
    _, home = run
    ids = json.loads((home / f"d65-{SMALL}.json").read_text(encoding="utf-8"))["ids"]
    return home, SessionStore(home, f"d65-{SMALL}"), ids


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
    assert len({p.id for p in PROBES}) == len(PROBES)
    assert len({p.query for p in PROBES}) == len(PROBES)
    assert len({fold(p.query) for p in PROBES}) == len(PROBES)  # no two differ by case alone
    assert all(p.query == p.query.strip() and p.query.split() for p in PROBES)


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


def test_split_alternates_within_each_class_and_is_balanced():
    for cls in CLASSES:
        probes = of(cls)
        splits = [fallback_probes.split_of(p) for p in probes]
        assert splits == ["tune" if i % 2 == 0 else "held_out" for i in range(len(probes))], cls
        assert abs(splits.count("tune") - splits.count("held_out")) <= 1, cls


def test_exact_probes_use_only_words_of_their_page():
    for p in of("exact"):
        text = planted(p)
        assert all(w in text for w in words(p.query)), p.id
    assert len({p.expected for p in of("exact")}) == len(of("exact"))  # one fact each


def test_typo_probes_are_an_exact_query_with_one_edit_in_a_word_of_six_or_more():
    exact = {p.query: p for p in of("exact")}
    kinds = Counter()
    for p in of("typo"):
        base = exact[p.base]
        assert p.expected == base.expected, p.id
        before, after = p.base.split(), p.query.split()
        assert len(before) == len(after), p.id
        changed = [(a, b) for a, b in zip(before, after) if a != b]
        assert changed == [(p.original, p.altered)], p.id
        assert len(p.original) >= 6, p.id
        assert edit_kind(p.original, p.altered) == p.sub_kind, p.id
        assert fold(p.altered) not in planted(p), p.id  # the typo is not on the page, so strict search misses
        kinds[p.sub_kind] += 1
    assert set(kinds) == {"substitution", "deletion", "insertion", "swap"} and min(kinds.values()) >= 3, kinds
    for half in ("tune", "held_out"):  # every kind of edit in each half
        assert {p.sub_kind for p in of("typo") if fallback_probes.split_of(p) == half} == set(kinds), half


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
        page_styles[here] += 1
        query_styles[there] += 1
    four = {"snake", "camel", "kebab", "dotted"}
    assert set(page_styles) == four and set(query_styles) == four, (page_styles, query_styles)
    assert min(page_styles.values()) >= 2 and min(query_styles.values()) >= 3, (page_styles, query_styles)


def test_paraphrase_probes_share_a_word_with_the_page_and_hold_one_it_lacks():
    for p in of("paraphrase"):
        text = planted(p)
        on = [w in text for w in words(p.query)]
        assert any(on) and not all(on), p.id
    assert len({p.expected for p in of("paraphrase")}) == len(of("paraphrase"))


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
        elif p.cls in ("typo", "identifier", "extra_word", "two_page"):
            assert not set(fallback_probes.expected_pages(p, ids)) & set(store.search(p.query)), p.id


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
    assert fallback_bench.ranked(FakeStore(["pg_a"], ["pg_b"]), "q") == (["pg_a"], "strict")
    assert fallback_bench.ranked(FakeStore([], ["pg_b", "pg_c"]), "q") == (["pg_b", "pg_c"], "fallback")
    assert fallback_bench.ranked(FakeStore([], []), "q") == ([], "none")
    assert fallback_bench.ranked(FakeStore([], None), "q") == ([], "none")  # main: no search_approximate


def test_score_counts_a_probe_at_k_only_when_every_expected_page_is_in_the_top_k():
    ranked = [f"pg_{i}" for i in range(12)]
    assert fallback_bench.score(["pg_2"], ranked) == {"ranks": "3", "top3": True, "top10": True, "any3": True,
                                                      "any10": True}
    assert fallback_bench.score(["pg_1", "pg_7"], ranked) == {"ranks": "2;8", "top3": False, "top10": True,
                                                              "any3": True, "any10": True}
    assert fallback_bench.score(["pg_11", "pg_x"], ranked) == {"ranks": "12;-", "top3": False, "top10": False,
                                                               "any3": False, "any10": False}
    assert fallback_bench.score([], ranked) == {"ranks": "", "top3": "", "top10": "", "any3": "", "any10": ""}


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
        assert r["source"] in ("strict", "fallback", "none")
        assert (r["source"] == "strict") == (int(r["strict_pages"]) > 0)
    exact = [r for r in rows if r["class"] == "exact"]
    assert all(r["source"] == "strict" and r["top10"] == "True" for r in exact)  # the control
    assert all(r["tool_detailed"] == "1" for r in exact if r["top3"] == "True")  # the tool shows the top 3
    assert all(r["altered_pages"] == "0" and r["strict_pages"] == "0" for r in rows if r["class"] == "none")
    summary = (folder / "summary.md").read_text(encoding="utf-8")
    for heading in (f"## Recall at {SMALL} pages", "## Nothing to find", "## Through the tool", "## Probe checks"):
        assert heading in summary, heading
    assert "| exact | 15 | " in summary and "| two_page, either page | 10 | " in summary
    assert "- exact probes whose page strict search returns: 15 of 15." in summary
    assert "returns an expected page: 0 of 63 (0 is right)." in summary
    assert "absent word on some page: 0 of 16 (0 is right)." in summary
    assert "## Latency" not in summary  # --quality-only
    env = json.loads((folder / "env.json").read_text(encoding="utf-8"))
    assert {"commit", "python", "os", "cpu", "label", "sizes", "probes"} <= set(env)
    text = json.dumps(env)
    assert str(Path.home()) not in text and "hostname" not in text  # numbers and versions only


def test_summary_counts_recall_per_class_and_split_and_the_false_hit_rate(tmp_path: Path):
    def row(i: int, cls: str, split: str, **kw) -> dict:
        base = dict.fromkeys(fallback_bench.QUALITY_FIELDS, "")
        base.update({"n": 60, "id": f"{cls}-{i}", "class": cls, "split": split, "source": "none", "strict_pages": 0,
                     "returned": 0, "tool_detailed": 0, "tool_listed": 0, "tool_pages": 0, "top3": False,
                     "top10": False, "any3": False, "any10": False})
        base.update(kw)
        return base

    rows = [row(0, "typo", "tune", source="fallback", returned=4, top3=True, top10=True, any3=True, any10=True),
            row(1, "typo", "held_out", source="fallback", returned=20, top10=True, any10=True),
            row(2, "typo", "tune"),
            row(0, "two_page", "tune", source="fallback", returned=9, any3=True, any10=True),
            row(0, "none", "tune", sub_kind="absent", altered_pages=0),
            row(1, "none", "held_out", sub_kind="absent", altered_pages=0),
            row(2, "none", "tune", sub_kind="mixed", source="fallback", returned=7, tool_pages=3, altered_pages=0),
            row(3, "none", "held_out", sub_kind="mixed", altered_pages=0)]
    with (tmp_path / "quality.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fallback_bench.QUALITY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary = fallback_bench.summarize(tmp_path)
    assert "| typo | 3 | 1/2 | 0/1 | 1/3 (33%) | 1/2 | 1/1 | 2/3 (67%) | 0 / 2 / 1 |" in summary
    assert "| two_page | 1 | 0/1 | 0/0 | 0/1 (0%) | 0/1 | 0/0 | 0/1 (0%) | 0 / 1 / 0 |" in summary
    assert "| two_page, either page | 1 | 1/1 | 0/0 | 1/1 (100%) | 1/1 | 0/0 | 1/1 (100%) | |" in summary
    assert "| absent | 2 | 0/1 | 0/1 | 0/2 (0%) | 0 | 0 |" in summary
    assert "| mixed | 2 | 1/1 | 0/1 | 1/2 (50%) | 3.5 | 1 |" in summary
    assert "| all | 4 | 1/2 | 0/2 | 1/4 (25%) | 0 | 1 |" in summary


def test_a_later_run_reuses_the_store(run, small, capsys):
    _, home = run
    _, _, ids = small
    manifest = home / f"d65-{SMALL}.json"
    written = manifest.stat().st_mtime_ns
    capsys.readouterr()
    assert fallback_bench.open_store(home, SMALL) == ids and len(ids) == SMALL
    assert "reusing" in capsys.readouterr().out and manifest.stat().st_mtime_ns == written


def test_compare_prints_two_result_folders_as_one_table(run, tmp_path: Path, capsys):
    folder, _ = run
    after = tmp_path / "after"
    shutil.copytree(folder, after)
    env = json.loads((after / "env.json").read_text(encoding="utf-8"))
    (after / "env.json").write_text(json.dumps({**env, "label": "after"}), encoding="utf-8")
    capsys.readouterr()
    assert fallback_bench.main(["--compare", str(folder), str(after)]) == 0
    table = capsys.readouterr().out
    lines = [line for line in table.splitlines() if line.startswith("|")]
    assert lines[0] == "| measure | before | after |" and len(lines) == len(table.strip().splitlines())
    assert any(line.startswith("| exact, all (15): top 3 / top 10 |") for line in lines)
    assert any(line.startswith("| none, all (16): probes with a page |") for line in lines)


def test_a_store_that_is_not_whole_is_built_again(tmp_path: Path, capsys):
    home = tmp_path / "home"
    first = fallback_bench.open_store(home, 20)
    store = SessionStore(home, "d65-20")
    (store.pages_dir / f"{first[3]}.json").unlink()  # a build that was cut short
    capsys.readouterr()
    assert fallback_bench.open_store(home, 20) != [] and "building" in capsys.readouterr().out
    assert len(SessionStore(home, "d65-20").page_ids()) == 20
    with pytest.raises(SystemExit):
        fallback_bench.main(["--sizes", "20", "--store-home", str(Path.home() / ".contexto")])


def test_latency_queries_are_the_nine_strict_hits_and_three_strict_misses_of_five_classes(run):
    _, home = run
    store = SessionStore(home, f"d65-{SMALL}")
    queries = fallback_bench.latency_queries(store)
    kinds = Counter(kind for kind, _ in queries)
    assert kinds == {"rare": 3, "common": 3, "multi": 3, "miss_identifier": 3, "miss_question": 3, "miss_typo": 3,
                     "miss_extra_word": 3, "miss_none": 3}
    by_query = {p.query: p for p in PROBES}
    for kind, query in queries:
        if kind.startswith("miss_"):
            assert store.search(query) == [] and by_query[query].cls == kind.removeprefix("miss_"), query


def test_latency_pass_times_both_sides_alternately_and_the_summary_shows_them(run, tmp_path: Path):
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
    assert {(r["side"], r["kind"]) for r in rows if r["kind"] in ("first", "startup", "peak_rss")} == {
        (side, kind) for side in sides for kind in ("first", "startup", "peak_rss")}
    assert all(float(r["load1"]) >= 0 for r in rows)
    assert len(calls) == len(timed) + 4  # the first search of each child in each pass
    summary = fallback_bench.summarize(tmp_path)
    assert f"## Latency at {SMALL} pages" in summary
    assert "| miss_typo | 1 | 0.020 | 0.010 | 2.00 |" in summary and "| first | 1 | 0.020 | 0.010 | 2.00 |" in summary
