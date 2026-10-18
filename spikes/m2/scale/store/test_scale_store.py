"""Fast checks of the store-scale harness helpers (no stores of size, no subprocess, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import scale_bench
import scale_calibrate
import scale_synth
from contexto.store import SessionStore
from contexto.store.pages import _texts
from contexto.tokens import estimate_message_tokens
from scale_probes import FACTS, LATENCY_QUERIES, planted_turn


def text_of(turn: list[dict]) -> str:
    return "\n".join(_texts(turn))


def test_planted_turn_spreads_over_the_session_and_stays_in_range():
    assert planted_turn(0.0, 6000) == 0 and planted_turn(1.0, 6000) == 5999
    assert planted_turn(0.5, 17) == 8 and planted_turn(0.97, 1) == 0
    assert all(0 <= planted_turn(p.at, n) < n for f in FACTS for p in f.plants for n in (17, 100, 6000))


def test_facts_are_thirty_with_two_or_three_queries_and_three_values_when_changing():
    assert len(FACTS) == 30 and len({f.id for f in FACTS}) == 30
    assert all(2 <= len(f.queries) <= 3 for f in FACTS)
    assert all(len(f.plants) == 3 for f in FACTS if f.kind == "changing")
    assert all(f.rename for f in FACTS if f.kind == "rename")


def test_markers_and_rename_names_never_appear_in_distractors_or_pools():
    markers = [p.marker for f in FACTS for p in f.plants]
    near = [s for f in FACTS for s in f.near] + list(scale_synth.NEAR_TEMPLATES)
    assert not [m for m in markers for s in near if m in s]
    pool_text = "\n".join(line for lines in scale_synth.pools().values() for line in lines)
    assert not [w for w in scale_synth.forbidden() if w in pool_text]
    assert not set(LATENCY_QUERIES["common"]) & set(scale_synth.forbidden())


def test_turns_are_deterministic_sized_and_carry_their_plants_only_where_planted():
    n = 40
    assert scale_synth.session_turn(3, n) == scale_synth.session_turn(3, n)
    sizes = []
    for t in range(n):
        turn = scale_synth.session_turn(t, n)
        sizes.append(estimate_message_tokens(turn))
        text = text_of(turn)
        here = {p.marker for _, _, p in scale_synth.plants_for(t, n)}
        for f in FACTS:
            for p in f.plants:
                assert (p.marker in text) == (p.marker in here or any(p.marker in m for m in here)), (t, p.marker)
    assert 19_000 <= min(sizes) and max(sizes) <= 32_000


def test_a_distractor_drawn_for_a_query_carries_no_longer_query_of_its_fact():
    for t in range(300):
        for line in scale_synth.near_sentences(t, 0.0, 7):
            for fact in FACTS:
                hits = [q for q in fact.queries if q in line]
                # every query on the line is contained in the longest one on it (nested phrasings only)
                assert all(q in max(hits, key=len) for q in hits), line


def test_background_and_distractor_draws_do_not_depend_on_session_length():
    assert scale_synth.near_sentences(5, 0.0, 7) == scale_synth.near_sentences(5, 0.0, 7)
    # page 1 has no plants at either length, so it is the same turn
    if not scale_synth.plants_for(1, 6000) and not scale_synth.plants_for(1, 3000):
        assert scale_synth.session_turn(1, 6000) == scale_synth.session_turn(1, 3000)


def test_responses_variant_holds_the_same_searchable_text():
    turn = scale_synth.session_turn(2, 17)
    items = scale_synth.to_responses(turn)
    assert all("type" in i for i in items)
    assert text_of(items).replace("\n", "") == text_of(turn).replace("\n", "")


def test_parse_reply_reads_detailed_listed_and_not_shown_counts():
    text = "\n".join(["pg_00000000000000aa (turn 9)", "> 12: a line", "pg_00000000000000bb (turn 4)", "> 0: x",
                      "[more matching pages, newest first; recall one at its offset:]",
                      "pg_00000000000000cc (turn 2) @40", "pg_00000000000000dd (turn 1) @0..9",
                      "[7 more matching pages not shown; use a longer or more specific query]"])
    reply = scale_bench.parse_reply(text)
    assert reply == {"detailed": ["pg_00000000000000aa", "pg_00000000000000bb"],
                     "listed": ["pg_00000000000000cc", "pg_00000000000000dd"], "total": 11}
    assert scale_bench.detailed_section(text, "pg_00000000000000bb") == "> 0: x"


def test_store_built_through_page_out_has_one_page_per_turn(tmp_path: Path):
    built = scale_bench.build_store(tmp_path, "s", 17, dialect="messages", floor=0.0, seed=7)
    assert built["markers_ok"] and built["row"]["pages"] == 17 and len(set(built["ids"])) == 17
    store = SessionStore(tmp_path, "s")
    assert store.page_ids()[-1] == built["ids"][0]  # newest first
    target = built["ids"][planted_turn(next(f for f in FACTS if f.id == "s14").plants[0].at, 17)]
    assert store.search("E4127") == [target]


def test_calibration_splits_turns_at_user_prompts_and_skips_sidechains(tmp_path: Path):
    records = [
        {"type": "user", "message": {"role": "user", "content": "first prompt with a rate limit"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "out"}]}},
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "sub rate limit"}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "second"}]}},
    ]
    path = tmp_path / "proj" / "s.jsonl"
    path.parent.mkdir()
    path.write_text("\n".join(json.dumps(r) for r in records) + "\nnot json\n", encoding="utf-8")
    turns = list(scale_calibrate.turns_of(path))
    assert [len(t) for t in turns] == [3, 1]
    result = scale_calibrate.calibrate([tmp_path])
    assert result["turns"] == 2 and result["df_counts"]["rate limit"] == 1 and result["df"]["rate limit"] == 0.5
