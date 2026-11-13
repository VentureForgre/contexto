"""Strict search is unchanged: ids and replies recorded at main b0e2dbc, before any fallback (D65).

``tests/fixtures/make_search_golden.py`` builds four synthetic corpora and wrote
``search_golden.json`` from them. A query with a strict hit (a paged page holding every word, or a
matching line in view) must still give the recorded ranked ids and the recorded reply, byte for byte.

A query with no strict hit is the only kind whose reply the fallback may change.
``search_golden_fallback.json`` records what the fallback finds for each of those, and the reply must
be either the recorded "no hit" or approximate matches, said to be, of exactly the recorded pages.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from contexto import mcp

FIXTURES = Path(__file__).with_name("fixtures")
_spec = importlib.util.spec_from_file_location("make_search_golden", FIXTURES / "make_search_golden.py")
golden_gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden_gen)

GOLDEN = json.loads((FIXTURES / "search_golden.json").read_text(encoding="utf-8"))
FALLBACK = json.loads((FIXTURES / "search_golden_fallback.json").read_text(encoding="utf-8"))
PAGE_LINE = re.compile(r"^(pg_[0-9a-f]{16}) \(turn \d+\)(?:$| @)", re.M)


def test_the_golden_file_covers_every_corpus_and_stays_small():
    assert set(GOLDEN) == set(golden_gen.CORPORA)
    assert (FIXTURES / "search_golden.json").stat().st_size < 400_000
    for name, rows in GOLDEN.items():
        assert sum(row["strict"] for row in rows) >= 8, name
        assert sum(not row["strict"] for row in rows) >= 2, name


@pytest.fixture(scope="module", params=sorted(golden_gen.CORPORA))
def corpus(request):
    store, queries = golden_gen.CORPORA[request.param]()
    rows = GOLDEN[request.param]
    assert [row["query"] for row in rows] == queries  # the corpus and the record list the same queries
    return store, rows


def test_search_returns_the_recorded_ids_for_every_query(corpus):
    store, rows = corpus
    for row in rows:
        assert store.search(row["query"]) == row["ids"], row["query"]


def test_a_query_with_a_strict_hit_gets_the_recorded_reply_byte_for_byte(corpus):
    store, rows = corpus
    for row in rows:
        if row["strict"]:
            reply = golden_gen.search_reply(store, row["query"])
            assert reply == {"texts": row["reply"], "error": row["error"]}, row["query"]


def test_only_a_query_with_no_strict_hit_gets_approximate_matches_and_they_are_the_recorded_ones(corpus, request):
    store, rows = corpus
    expected = {row["query"]: row for row in FALLBACK[request.node.callspec.params["corpus"]]}
    assert list(expected) == [row["query"] for row in rows if not row["strict"]]
    for row in rows:
        if row["strict"]:
            continue
        want = expected[row["query"]]
        hits = store.search_approximate(row["query"])
        assert (hits.total, [h.id for h in hits[:golden_gen.FALLBACK_SHOWN]]) == (want["total"], want["ids"])
        reply = golden_gen.search_reply(store, row["query"])
        if not want["ids"]:  # nothing approximate either: the reply is the one recorded before the fallback
            assert reply == {"texts": row["reply"], "error": row["error"]}, row["query"]
            continue
        body, coverage = reply["texts"]
        assert reply["error"] is False and coverage == row["reply"][1]  # the coverage block is unchanged
        assert body.split("\n")[0] == mcp.SEARCH_APPROX_HEADER, row["query"]
        assert sorted(PAGE_LINE.findall(body)) == sorted(want["ids"]), row["query"]


def test_the_fallback_changes_some_recorded_misses_and_leaves_others():
    changed = {name: sum(bool(row["ids"]) for row in rows) for name, rows in FALLBACK.items()}
    assert changed == {"scale17": 9, "acceptance": 54, "small": 13, "many": 1}
    assert {name: len(rows) for name, rows in FALLBACK.items()} == {"scale17": 14, "acceptance": 54, "small": 17,
                                                                    "many": 2}
