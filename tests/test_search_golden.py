"""Strict search is unchanged: ids and replies recorded at main b0e2dbc, before any fallback (D65).

``tests/fixtures/make_search_golden.py`` builds four synthetic corpora and wrote
``search_golden.json`` from them. A query with a strict hit (a paged page holding every word, or a
matching line in view) must still give the recorded ranked ids and the recorded reply, byte for byte.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).with_name("fixtures")
_spec = importlib.util.spec_from_file_location("make_search_golden", FIXTURES / "make_search_golden.py")
golden_gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden_gen)

GOLDEN = json.loads((FIXTURES / "search_golden.json").read_text(encoding="utf-8"))


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
