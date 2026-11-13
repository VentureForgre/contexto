"""Search falls back to approximate matches when no page holds every word (D65).

The ladder, in ``contexto.store.search``: punctuation at the ends of a word and stop words are
dropped, identifier styles fold together, pages are ranked by the words they hold with a floor, and
one word of 6 or more letters that no page holds may differ by one letter. Strict search is not
changed (``tests/test_search_golden.py``).
"""

from __future__ import annotations

import io
import json
import math
import re

import pytest

from contexto import cli, mcp
from contexto.store import SessionStore
from contexto.store import search as S
from contexto.store.memory import MemoryStore
from contexto.store.search import SearchEntry, rank, rank_approximate, search_text

FILLER = "\n".join(f"step {i}: ran the build and checked the output, nothing unusual" for i in range(40))
DETAILED = re.compile(r"^(pg_[0-9a-f]{16}) \(turn (\d+)\)$", re.M)
LISTED = re.compile(r"^(pg_[0-9a-f]{16}) \(turn (\d+)\) @", re.M)


def page(text, prompt="continue"):
    return [{"role": "user", "content": prompt},
            {"role": "assistant", "content": [{"type": "text", "text": text}]}]


TURNS = [
    page(FILLER + "\nNightly backups run at 02:00 from the replica."),
    page("The login flow stores the refreshToken in the keychain and rotates it monthly.", "how does login work"),
    page("I set retry_budget to 5 in settings.toml.", "raise the retry budget"),
    page("The staging database listens on port 6543 and production on 5432.", "which ports are in use"),
    page("Deploys go out on Tuesday after the freeze ends.", "when do we ship"),
    page(FILLER + "\nthe output of the build was fine"),
    page("The canary runs for an hour before the rollout widens.", "how long is the canary"),
]


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    s = SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")
    s.page_out(TURNS, 0)
    return s


def ids(store):
    return {page["turn"]: page["id"] for page in (store.get_page(i) for i in store.page_ids())}


def call(store, query):
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_search", "arguments": {"query": query}}})
    result = server.handle_line(line)["result"]
    assert result["isError"] is False
    return [block["text"] for block in result["content"]]


def last_event(store):
    log = store.events if isinstance(store, MemoryStore) else [
        json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
    return [e for e in log if e.get("event") == "mcp_call" and e.get("tool") == "contexto_search"][-1]


def entries(*texts):
    """One page per text, oldest first; a text is one field or a list of fields."""
    return [SearchEntry(f"pg_{i:016x}", i, search_text([t] if isinstance(t, str) else t)) for i, t in enumerate(texts)]


def found(hits):
    return [int(h.id[3:], 16) for h in hits]


def how(hit):
    return {m.word: (m.how, m.form) for m in hit.matched}


# The words searched


def test_stop_words_and_end_punctuation_are_dropped_from_the_query():
    plan = S.plan_words("What port is the staging database on?")
    assert plan.words == ("port", "staging", "database") and plan.dropped == ("what", "is", "the", "on")
    assert S.plan_words('where did we set "retry_budget", please').words == ("set", "retry_budget")
    for word in ("what", "which", "where", "how", "is", "the", "of", "on", "a", "an", "does", "do", "did"):
        assert word in S.STOP_WORDS, word
    assert all(w == w.lower() and w.replace("'", "").isalpha() for w in S.STOP_WORDS)
    for kept in ("not", "no", "set", "get", "all", "new", "self", "return", "import"):
        assert kept not in S.STOP_WORDS, kept


def test_a_word_with_a_digit_or_a_symbol_is_never_dropped():
    assert S.plan_words("is the a1 on i/o").words == ("a1", "i/o")
    assert S.plan_words("what is x=the").words == ("x=the",)
    assert S.plan_words("the 2").words == ("2",)


def test_a_query_of_only_stop_words_or_no_words_has_no_fallback():
    for query in ("what is the", "the", "", "   ", "?", "... !!"):
        assert S.plan_words(query) is None, query
        assert rank_approximate(entries("what is the time"), query) == []
    assert S.plan_words(None) is None


def test_at_most_six_words_are_searched_the_longest_in_query_order():
    plan = S.plan_words("fix intermittent failure upload retry test staging last week")
    assert S.APPROX_WORDS == 6
    assert plan.words == ("intermittent", "failure", "upload", "retry", "test", "staging")
    assert plan.cut == 3
    long_query = " ".join(f"w{i}" for i in range(120))
    assert len(S.plan_words(long_query).words) == 6 and S.plan_words(long_query).cut == 114


# Identifier styles


FORMS = ["refresh_token", "refreshToken", "refresh-token", "refresh.token", "REFRESH_TOKEN"]


@pytest.mark.parametrize("on_page", FORMS)
@pytest.mark.parametrize("typed", FORMS)
def test_identifier_styles_match_each_other_in_both_directions(on_page, typed):
    pages = entries(f"the login flow keeps the {on_page} in the keychain", "nothing about tokens here")
    strict = rank(pages, typed)
    hits = rank_approximate(pages, typed)
    assert found(hits) == [0]
    kind, form = how(hits[0])[S.fold(typed)]
    assert form == S.fold(on_page)
    assert kind == ("exact" if S.fold(typed) in S.fold(on_page) else "folded")
    assert (strict == [hits[0].id]) == (kind == "exact")


def test_only_a_word_written_as_an_identifier_is_folded():
    pages = entries("version 1.2 shipped", "release 12 shipped", "e.g. the io-py thing", "hash is sha-256",
                    "the sha256 sum", "a data_base of sorts")
    assert found(rank_approximate(pages, "12")) == [1]
    assert rank_approximate(pages, "1-2") == [] and rank_approximate(pages, "1.2.0") == []  # a number is as typed
    assert rank_approximate(pages, "e-g") == []  # fewer than 4 characters without the separator
    assert found(rank_approximate(pages, "ioPy")) == [2] and found(rank_approximate(pages, "io_py")) == [2]
    assert rank_approximate(pages, "iopy") == []  # a plain word is matched as typed
    assert rank_approximate(pages, "database") == []
    assert found(rank_approximate(pages, "dataBase")) == [5]
    assert sorted(found(rank_approximate(pages, "sha-256"))) == [3, 4]
    versions = entries("released v0.52.4 today", "nothing")
    assert rank_approximate(versions, "v0.5.24") == []  # a separator between digits is never dropped
    assert found(rank_approximate(versions, "v0.52.4")) == [0]
    six = S.plan_words("refresh_token refreshToken UserCache REFRESH Staging E4127")
    assert [six.shaped(j) for j in range(6)] == [True, True, True, False, False, False]
    assert [six.specific(j) for j in range(6)] == [True, True, True, False, False, True]
    assert [six.plain(j) for j in range(6)] == [False, False, False, True, True, False]
    assert S.plan_words("HTTPTimeout").shaped(0) and not S.plan_words("HTTP").shaped(0)


def test_a_folded_match_is_found_through_the_store(store):
    by_turn = ids(store)
    for query, turn, form in (("refresh_token", 1, "refreshtoken"), ("retryBudget", 2, "retry_budget"),
                              ("retry-budget", 2, "retry_budget"), ("refresh.token", 1, "refreshtoken")):
        assert store.search(query) == []
        hits = store.search_approximate(query)
        assert [h.id for h in hits] == [by_turn[turn]], query
        assert how(hits[0]) == {S.fold(query): ("folded", form)} and hits[0].missing == ()


# Ranking by the words a page holds, and the floor


def test_a_question_finds_the_page_that_holds_its_content_words(store):
    by_turn = ids(store)
    query = "what port is the staging database on?"
    assert store.search(query) == []
    hits = store.search_approximate(query)
    assert hits[0].id == by_turn[3]
    assert [m.word for m in hits[0].matched] == ["port", "staging", "database"] and hits[0].missing == ()
    assert all(m.how == "exact" for m in hits[0].matched)


def test_one_extra_word_still_finds_the_page_and_names_the_word(store):
    hits = store.search_approximate("staging database port zzqqxx")
    assert [h.id for h in hits] == [ids(store)[3]]
    assert hits[0].missing == ("zzqqxx",) and len(hits[0].matched) == 3


def test_two_facts_on_different_pages_are_both_found(store):
    by_turn = ids(store)
    hits = store.search_approximate("staging port deploys tuesday")
    assert {h.id for h in hits} == {by_turn[3], by_turn[4]}
    assert {h.id: set(h.missing) for h in hits} == {by_turn[3]: {"deploys", "tuesday"}, by_turn[4]: {"staging", "port"}}


def test_pages_holding_every_word_come_first_then_the_rarer_words_win():
    pages = entries("alpha bravo together", "alpha with common", "common again", "common three", "common four",
                    "alpha bravo common all three", "filler", "filler two", "filler three", "filler four")
    assert rank(pages, "alpha bravo common charlie") == []
    hits = rank_approximate(pages, "alpha bravo common charlie")
    # charlie is on no page: nothing holds every word. Page 5 holds three words, page 0 the two rare ones.
    assert found(hits)[:2] == [5, 0]
    assert 2 not in found(hits) and 3 not in found(hits)  # one common word of four is under the floor
    full = rank_approximate(pages, "alpha bravo common")
    assert found(full)[0] == 5 and full[0].missing == ()
    assert found(full).index(0) < found(full).index(1)  # two rare words outrank one rare and one common


def test_the_floor_keeps_out_a_page_with_one_common_word_of_five():
    texts = [f"page {i} talks about the build" for i in range(40)]
    for i, word in ((3, "alpha"), (9, "bravo"), (17, "charlie"), (25, "delta")):
        texts[i] += f" and {word}"
    pages = entries(*texts)
    # Each page holds one rare word of four and "build": 25 percent of the query, under the floor.
    assert rank_approximate(pages, "build alpha bravo charlie delta") == []
    hits = rank_approximate(pages, "build alpha bravo")
    assert sorted(found(hits)) == [3, 9] and all(len(h.matched) == 2 for h in hits)  # 50 percent each
    # the numbers: "build" is on 40 of 40 pages, so its weight is ln(1 + 0.5 / 40.5) = 0.012; each rare
    # word is on 1 page and weighs ln(1 + 39.5 / 1.5) = 3.31. A page holding only "build" holds
    # 0.012 / 13.25 = 0.1 percent of the query, under the floor of 30 percent.
    common, rare = math.log(1 + 0.5 / 40.5), math.log(1 + 39.5 / 1.5)
    assert common / (common + 4 * rare) < 0.001 < S.APPROX_FLOOR == 0.3
    assert not any(len(h.matched) == 1 and h.matched[0].word == "build" for h in hits)
    assert rank_approximate(pages, "build zzqqxx yyqqxx") == []  # 1 word held, 2 found nowhere


def test_a_page_must_hold_more_words_than_the_store_lacks(store):
    assert store.search_approximate("nightly zzqqxx") == []  # 1 held, 1 on no page
    assert store.search_approximate("kubernetes ingress certificate") == []
    assert store.search_approximate("which engineer approved the kubernetes ingress") == []
    assert store.search_approximate("which engineer approved the nightly backups") == []  # 2 held, 2 on no page
    hits = store.search_approximate("nightly backups zzqqxx")  # 2 held, 1 on no page
    assert [h.id for h in hits] == [ids(store)[0]] and hits[0].missing == ("zzqqxx",)


def test_an_identifier_a_number_or_a_path_is_enough_on_its_own(store):
    by_turn = ids(store)
    # 1 word held, 3 on no page: returned because the word held is an identifier, not a plain word
    for query, turn in (("What value did retry_budget have earlier in this conversation?", 2),
                        ("refreshToken expiry policy rotation", 1), ("kubernetes ingress 6543", 3),
                        ("zzqqxx yyqqxx settings.toml", 2)):
        hits = store.search_approximate(query)
        assert [h.id for h in hits] == [by_turn[turn]], query
        assert len(hits[0].matched) == 1 and len(hits[0].missing) >= 2
    hits = store.search_approximate("refresh_token expiry policy rotation")  # folded counts too
    assert [h.id for h in hits] == [by_turn[1]] and hits[0].matched[0].how == "folded"


def test_words_that_most_pages_hold_together_are_not_evidence_when_another_word_is_unknown():
    texts = [f"page {i}: the function will return a value" for i in range(20)] + ["the staging port value moved"]
    texts[7] += "; check the return value"
    pages = entries(*texts)
    # "foo" is on no page. 20 of 21 pages hold "return" and "value": too common together to mean anything.
    # Page 7 holds them one after the other, as the query does, and no other page does.
    assert found(rank_approximate(pages, "what is the return value of foo")) == [7]
    assert rank_approximate(pages, "what does foo return as a value") == []
    assert found(rank_approximate(pages, "what is the staging value of foo")) == [20]  # 1 page holds both
    assert len(rank_approximate(pages, "what is the return value")) == 20  # no word unknown: every word held
    assert S.COMMON_FROM_PAGES == 10  # in a store of fewer pages every word counts
    assert found(rank_approximate(entries(*texts[:5]), "what is the return value of foo")) == [4, 3, 2, 1, 0]


def test_words_that_follow_each_other_in_the_query_rank_first_when_the_page_has_them_so():
    texts = [["the db is up", "the port is open"], "the DB port is 6543", ["db", "port", "db and port", "port and db"],
             ["db checked", "port checked"]]
    pages = entries(*texts)
    assert rank(pages, "DB port")[0] == pages[1].id  # strict: the page holding the whole query comes first
    assert rank(pages, "what is the DB port?") == []
    assert found(rank_approximate(pages, "what is the DB port?"))[0] == 1
    assert found(rank_approximate(pages, "DB port zzqqxx"))[0] == 1
    apart = entries(["staging checked", "the database is big", "staging and database"], "the staging database is big",
                    ["staging", "database"])
    assert found(rank_approximate(apart, "what is the staging databse"))[0] == 1  # the typo's form beside its neighbour
    assert S.plan_words("what DB is the port on").pairs == ()  # a dropped word stood between them
    assert S.plan_words("staging DB port").pairs == ((0, 1), (1, 2))


def test_the_newest_page_holding_every_word_takes_a_detailed_place():
    often = ["retry budget", "retry budget again", "retry and budget"]
    texts = ["retry budget noted once"] + [often] * 4 + ["retry budget"]
    pages = entries(*texts)
    hits = rank_approximate(pages, "what is the retry budget")
    assert len(hits) == 6 and all(h.missing == () for h in hits)
    assert 5 in found(hits)[:S.SHOWN_IN_DETAIL]


def test_results_are_capped_and_say_how_many_passed():
    pages = entries(*[f"deploy number {i} finished" for i in range(80)])
    hits = rank_approximate(pages, "what deploy finished")
    assert len(hits) == S.APPROX_LIMIT == 50 and hits.total == 80


def test_the_same_store_and_query_give_the_same_list(store):
    for query in ("what port is the staging database on", "refresh_token", "staging port deploys tuesday",
                  "nightly bakcups"):
        first = store.search_approximate(query)
        assert first and store.search_approximate(query) == first
        assert all(isinstance(h.id, str) and h.id.startswith("pg_") for h in first)


# One-letter typos


@pytest.mark.parametrize("typed, kind", [("databse", "swap"), ("datavase", "substitution"), ("databasse", "extra"),
                                         ("datbase", "missing"), ("adtabase", "swap at the start"),
                                         ("databaes", "swap at the end"), ("xatabase", "first letter"),
                                         ("databasw", "last letter"), ("datbaase", "swap in the middle")])
def test_a_one_letter_typo_in_a_word_of_six_or_more_letters_is_found(typed, kind):
    pages = entries("the staging database listens on port 6543", "nothing here", "the DATABASE again")
    assert rank(pages, typed) == []
    hits = rank_approximate(pages, typed)
    assert sorted(found(hits)) == [0, 2], kind
    assert how(hits[0]) == {typed: ("typo", "database")}


def test_typos_are_for_long_words_of_letters_that_no_page_holds():
    pages = entries("the staging database listens on port 6543", "ticket SUP-48213 is open", "the databases page",
                    "a tokens page")
    assert S.TYPO_MIN_CHARS == 6
    assert rank_approximate(pages, "tokns") == []  # 5 letters
    assert rank_approximate(pages, "sup-48231") == []  # a digit: numbers are exact
    assert rank_approximate(pages, "data_bsae") == [] and rank_approximate(pages, "dataBsae") == []  # identifiers too
    assert rank_approximate(pages, "datxbxse") == []  # two letters differ
    assert rank_approximate(pages, "stagin") != [] and rank_approximate(pages, "stagin")[0].matched[0].how == "exact"
    # only a whole word on the page counts: "listen" is in "listens", "listem" is not a typo of a whole word
    assert rank_approximate(pages, "listem") == []
    assert found(rank_approximate(pages, "listenss")) == [0]


def test_a_typo_combines_with_the_other_words(store):
    by_turn = ids(store)
    hits = store.search_approximate("nightly bakcups")
    assert [h.id for h in hits] == [by_turn[0]]
    assert how(hits[0]) == {"nightly": ("exact", "nightly"), "bakcups": ("typo", "backups")} and hits[0].missing == ()
    hits = store.search_approximate("what port is the staging databse on")
    assert hits[0].id == by_turn[3] and how(hits[0])["databse"] == ("typo", "database")


def test_only_one_word_per_query_is_tried_as_a_typo_and_work_is_bounded(monkeypatch):
    pages = entries("the staging database listens on port 6543")
    hits = rank_approximate(pages, "stagign databse port")  # two typos: the longer word is tried
    assert how(hits[0]) == {"port": ("exact", "port"), "stagign": ("typo", "staging")}
    assert hits[0].missing == ("databse",)
    assert rank_approximate(pages, "a" * 41) == [] and S.TYPO_MAX_CHARS == 40
    monkeypatch.setattr(S, "TYPO_WORK", 3)
    many = entries(*["data data data data database"] * 5)
    assert rank_approximate(many, "databse") == []  # more candidate positions than the bound: the typo is not searched


# Safety


@pytest.mark.parametrize("query", ["", " ", "\t\n", "é", "\ud83d", "😀 emoji", "ΟΔΟΣ ΑΘΗΝΑ", "what is the",
                                   " ".join(f"w{i}" for i in range(120)), "a" * 5000, "x" * 7 + "\ud83d", "_-.", "-",
                                   "??", "refresh_tokén", "\x00\xff"])
def test_odd_queries_return_a_list_and_never_raise(store, query):
    hits = store.search_approximate(query)
    assert isinstance(hits, list) and store.search_approximate(query) == hits
    if query.strip():  # an empty query is a tool error, as before
        text = call(store, query)[0]
        assert text == mcp.NO_HIT or text.startswith((mcp.SEARCH_APPROX_HEADER, mcp.SEARCH_BEST_HEADER)), text[:80]


def test_unicode_words_match_as_typed(store):
    store.page_out([page("η οδός Αθηνάς is closed; café résumé noted")], 20)
    [hit] = store.search_approximate("what is the café zzqq résumé")
    assert [m.word for m in hit.matched] == ["café", "résumé"] and hit.missing == ("zzqq",)


def test_a_damaged_or_vanished_page_is_left_out(tmp_path):
    store = SessionStore(tmp_path, "s1")
    pids = store.page_out(TURNS, 0)
    assert [h.id for h in store.search_approximate("what is the staging database port")] == [pids[3]]
    (store.pages_dir / f"{pids[3]}.json").write_text("{not json", encoding="utf-8")
    assert store.search_approximate("what is the staging database port") == []
    (store.pages_dir / f"{pids[1]}.json").unlink()
    assert store.search_approximate("refresh_token") == []
    assert [h.id for h in store.search_approximate("retryBudget")] == [pids[2]]
    assert call(store, "refresh_token")[0] == mcp.NO_HIT


def test_search_itself_is_unchanged(store):
    for query in ("refresh_token", "what port is the staging database on", "nightly bakcups", "staging zzqqxx"):
        assert store.search(query) == []
    assert store.search("refreshToken") == [ids(store)[1]]


# The tool reply


def test_the_reply_says_the_matches_are_approximate_and_names_the_words(store):
    by_turn = ids(store)
    text, coverage = call(store, "what port is the staging databse on?")
    lines = text.split("\n")
    assert lines[0] == mcp.SEARCH_APPROX_HEADER and "approximate" in lines[0] and "every word" in lines[0]
    assert lines[1] == "[searched for: port, staging, databse]"
    assert lines[2] == f"{by_turn[3]} (turn 3)"
    assert lines[3] == "  [matched: port, staging, databse as database]"
    assert any(line.startswith("> ") and "staging database listens on port 6543" in line for line in lines)
    assert mcp.SEARCH_BEST_HEADER not in text and coverage == mcp.SEARCH_COVERAGE_UNCHECKED
    assert DETAILED.findall(text) == [(by_turn[3], "3")]


def test_the_matching_lines_show_the_form_found_on_the_page(store):
    by_turn = ids(store)
    text, _ = call(store, "refresh_token")
    lines = text.split("\n")
    assert lines[:3] == [mcp.SEARCH_APPROX_HEADER, f"{by_turn[1]} (turn 1)",
                         "  [matched: refresh_token as refreshToken]"]
    [match] = [line for line in lines if line.startswith("> ")]
    assert "refreshToken in the keychain" in match
    text, _ = call(store, "retryBudget zzqqxx settings.toml")
    assert "  [matched: retryBudget as retry_budget, settings.toml; not matched: zzqqxx]" in text.split("\n")
    assert any(line.startswith("> ") and "retry_budget to 5" in line for line in text.split("\n"))


def test_an_exact_strict_reply_has_no_approximate_text(store):
    text, _ = call(store, "refreshToken")
    assert text.startswith(mcp.SEARCH_BEST_HEADER) and "approximate" not in text and "[matched:" not in text
    assert last_event(store)["approximate"] is False and "approximate_steps" not in last_event(store)


def test_nothing_approximate_is_no_hit_as_before(store):
    for query in ("zzqqxx", "nightly zzqqxx", "what is the", "kubernetes ingress"):
        text, coverage = call(store, query)
        assert text == mcp.NO_HIT and coverage == mcp.SEARCH_COVERAGE_UNCHECKED
        event = last_event(store)
        assert event["approximate"] is False and event["hits"] == 0 and event["total_matches"] == 0


def test_at_most_three_detailed_pages_and_ten_listed(tmp_path):
    store = MemoryStore("s1")
    store.page_out([page(f"build number {i} passed") for i in range(40)], 0)
    store.page_out([page(f"deploy number {i} finished in region r{i % 5}") for i in range(40)], 40)
    text, _ = call(store, "what deploy finished zzqqxx")
    assert len(DETAILED.findall(text)) == 3 and len(LISTED.findall(text)) == mcp.APPROX_MORE_LIMIT == 10
    assert "[more approximate matches, recall one at its offset:]" in text
    assert "[27 more approximate matches not shown; use a more specific query]" in text
    listed = [line for line in text.split("\n") if LISTED.match(line)]
    assert all(re.fullmatch(r"pg_[0-9a-f]{16} \(turn \d+\) @\d+ \[matched: deploy, finished; not matched: zzqqxx\]",
                            line) for line in listed), listed[:2]
    turns = [int(t) for _, t in DETAILED.findall(text)]
    assert turns == sorted(turns, reverse=True) and 79 in turns  # the newest page holding the words is detailed
    event = last_event(store)
    assert event["hits"] == 13 and event["total_matches"] == 40 and event["approximate"] is True


def test_detailed_pages_show_most_words_first_then_newest(store):
    by_turn = ids(store)
    store.page_out([page("the staging database moved to port 7000 for the canary")], 30)
    text, _ = call(store, "staging database port canary rollout")
    shown = [pid for pid, _ in DETAILED.findall(text)]
    newest = store.page_ids()[0]
    assert shown[0] == newest  # holds 4 of the 5 words
    assert set(shown[1:]) == {by_turn[3], by_turn[6]}  # 3 words, and the 2 rarer ones
    assert f"{newest} (turn 30)\n  [matched: staging, database, port, canary; not matched: rollout]" in text


def test_the_log_says_approximate_and_which_steps_without_the_words(store):
    call(store, "what port is the staging databse on")
    event = last_event(store)
    assert event["approximate"] is True and event["approximate_steps"] == ["stop_words", "typo"]
    call(store, "refresh_token zzqqxx keychain")
    assert last_event(store)["approximate_steps"] == ["fold", "any_word"]
    call(store, "staging port deploys tuesday")
    event = last_event(store)
    assert event["approximate_steps"] == ["any_word"] and event["hits"] == 2 and event["total_matches"] == 2
    assert event["returned"] and all(span["source"] == "paged" for span in event["returned"])
    dumped = json.dumps(event)
    for word in ("staging", "port", "deploys", "tuesday", "refresh", "databse"):
        assert word not in dumped


def test_an_approximate_reply_is_charged_to_the_room_and_held_to_the_limit(store):
    store.set_window(200_000, 2.0)
    store.set_retained(1, 0, [{"turn": 40, "message_index": 0, "role": "user", "text": "what changed?"}])
    store.set_turn_room("7", 10_000)
    text, coverage = call(store, "what port is the staging database on")
    event = last_event(store)
    assert coverage == mcp.SEARCH_COVERAGE and text.startswith(mcp.SEARCH_APPROX_HEADER)
    assert event["turn_budget"] == "charged" and event["charged_tokens"] == math.ceil(len(text) / 1.5)
    assert event["room_left"] == 10_000 - event["charged_tokens"]
    store.set_turn_room("8", 40)  # room for 60 characters: not even the header
    text, _ = call(store, "what port is the staging database on")
    assert text == mcp.BUDGET_SPENT and last_event(store)["charged_tokens"] == 0
    # A hit too long for the room: its page line, its words and its matching lines are kept, without context.
    wide = "x" * 150
    store.page_out([page("\n".join([wide, "the quokka cache is warm", wide, wide, wide, "quokka cache again", wide,
                                    wide, wide, "last quokka cache line", wide]))], 50)
    store.set_turn_room("9", 300)  # room for 450 characters
    text, _ = call(store, "what quokka cache zzqq")
    lines = text.split("\n")
    assert lines[0] == mcp.SEARCH_APPROX_HEADER and len(text) <= 450
    assert lines[3] == "  [matched: quokka, cache; not matched: zzqq]"
    assert [line.split(": ", 1)[1] for line in lines if line.startswith("> ")] == [
        "the quokka cache is warm", "quokka cache again", "last quokka cache line"]
    assert lines[-1] == "  [the rest of this hit is not shown; the recall budget for this turn is used up]"


def test_paged_reminders_come_after_turn_pages_under_their_own_line(store):
    reminder = "<system-reminder>\nThe staging database port changed to 7000. Take it as current.\n</system-reminder>\n"
    [context] = store.page_out([[{"role": "user", "content": [{"type": "text", "text": reminder}]}]], 60)
    text, _ = call(store, "what is the staging database port?")
    lines = text.split("\n")
    turn_page = ids(store)[3]
    assert [pid for pid, _ in DETAILED.findall(text)] == [turn_page, context]  # the reminder is newer, shown second
    assert lines[lines.index(f"{context} (turn 60)") - 1] == mcp.SEARCH_CONTEXT_HEADER
    assert lines[lines.index(f"{context} (turn 60)") + 1] == "  [matched: staging, database, port]"


def test_a_codex_page_is_found_approximately_and_shows_its_line(store):
    turn = [{"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "cfg checkout-api.retention_days=juniper-2573\nReply: OK"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}]
    [pid] = store.page_out([turn], 70)
    text, _ = call(store, "What value did checkoutApi.retentionDays have earlier?")
    lines = text.split("\n")
    assert lines[2:4] == [f"{pid} (turn 70)", "  [matched: checkoutApi.retentionDays as checkout-api.retention_days; "
                                             "not matched: value, earlier]"]
    assert any(line.startswith("> ") and line.endswith("cfg checkout-api.retention_days=juniper-2573")
               for line in lines)


def test_words_that_match_once_their_end_punctuation_is_dropped_are_logged_as_such(store):
    text, _ = call(store, '"staging database", port?')
    assert text.split("\n")[:3] == [mcp.SEARCH_APPROX_HEADER, f"{ids(store)[3]} (turn 3)",
                                    "  [matched: staging, database, port]"]
    assert last_event(store)["approximate_steps"] == ["punctuation"]


def test_a_fallback_that_raises_fails_this_call_only(store, monkeypatch):
    monkeypatch.setattr(store, "search_approximate", lambda query: (_ for _ in ()).throw(RuntimeError("boom")))
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_search", "arguments": {"query": "refresh_token"}}})
    result = server.handle_line(line)["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == mcp.TOOL_FAILED.format(tool="contexto_search", error="RuntimeError")
    assert call(store, "refreshToken")[0].startswith(mcp.SEARCH_BEST_HEADER)  # strict search still answers


def test_an_in_view_match_stops_the_fallback(store):
    store.set_window(200_000, 2.0)
    store.set_retained(1, 0, [
        {"turn": 40, "message_index": 0, "role": "assistant", "text": "the refresh_token expired"},
        {"turn": 41, "message_index": 0, "role": "user", "text": "what changed?"}])
    store.set_turn_room("7", 10_000)
    text, _ = call(store, "refresh_token")
    assert text == "[in view, newest first:]\n> in view (turn 39): the refresh_token expired"
    assert last_event(store)["approximate"] is False


def test_a_store_without_the_fallback_behaves_as_before():
    class Double:
        def __init__(self):
            self.events = []

        def search(self, query):
            return []

        def get_page(self, page_id):
            return None

        def log(self, event):
            self.events.append(event)

    double = Double()
    assert call(double, "what port is the staging database on")[0] == mcp.NO_HIT
    assert double.events[-1]["approximate"] is False


# What the model is told


def test_the_instructions_and_the_tool_description_say_what_search_does():
    search = next(t for t in mcp.TOOLS if t["name"] == "contexto_search")["description"]
    assert "exact substring" not in mcp.INSTRUCTIONS and "No fuzzy" not in search
    for text in (mcp.INSTRUCTIONS, search):
        assert "every word" in text and "approximate" in text
    tail = ("When the user states a standing rule, preference, or obligation (whenever, from now on, always, never), "
            "pin it at once with contexto_pin {\"kind\": \"obligation\", \"text\": ...}, even if your reply is one "
            "word, and keep it open until the user withdraws it: once its turn is paged out, only what is pinned "
            "stays in view.")
    assert mcp.INSTRUCTIONS.endswith(tail) and len(mcp.INSTRUCTIONS) <= 750


# The command line


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    store = SessionStore(tmp_path, "s1")
    pids = store.page_out(TURNS, 0)
    store.release()
    return pids


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_the_command_prints_approximate_matches_with_a_note_and_the_words(home, capsys):
    code, out, err = run(capsys, "search", "what", "port", "is", "the", "staging", "databse", "on")
    assert code == 0
    assert ("no paged-out turn in session s1 contains every word of 'what port is the staging databse on'; "
            "approximate matches, best first:") in err
    lines = out.splitlines()
    assert lines[0].startswith(f"{home[3]} turn 3:")
    assert lines[1] == "  [matched: port, staging, databse as database]"
    assert any(line.lstrip().startswith(">") and "staging database listens" in line for line in lines)
    code, out, err = run(capsys, "search", "refresh_token", "zzqqxx", "keychain")
    assert code == 0 and "  [matched: refresh_token as refreshToken, keychain; not matched: zzqqxx]" in out.splitlines()
    assert any(line.lstrip().startswith(">") and "refreshToken" in line for line in out.splitlines())


def test_the_command_is_unchanged_for_a_strict_hit_and_for_nothing(home, capsys):
    code, out, err = run(capsys, "search", "refreshToken")
    assert code == 0 and out.splitlines()[0].startswith(f"{home[1]} turn 1:") and "approximate" not in out + err
    assert "[matched" not in out
    code, out, err = run(capsys, "search", "zzqqxx", "nightly")
    assert code == 0 and out == ""
    assert err.splitlines()[-1] == "no paged-out turn in session s1 contains every word of 'zzqqxx nightly'"


def test_the_command_says_when_more_pages_matched_than_it_can_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    monkeypatch.setattr(S, "APPROX_LIMIT", 3)
    store = SessionStore(tmp_path, "s1")
    store.page_out([page(f"deploy number {i} finished") for i in range(8)], 0)
    store.release()
    code, out, err = run(capsys, "search", "what", "deploy", "finished")
    assert code == 0 and sum(line.startswith("pg_") for line in out.splitlines()) == 3
    assert "[5 more approximate matches: use more specific words]" in err
    code, out, err = run(capsys, "search", "what", "deploy", "finished", "--limit", "1")
    assert "[7 more approximate matches: rerun with --limit 3 for 2 of them, and use more specific words]" in err


def test_the_command_limits_approximate_matches_and_says_how_many_more(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    store = SessionStore(tmp_path, "s1")
    store.page_out([page(f"deploy number {i} finished") for i in range(8)], 0)
    store.release()
    code, out, err = run(capsys, "search", "what", "deploy", "finished", "--limit", "2")
    assert code == 0 and sum(line.startswith("pg_") for line in out.splitlines()) == 2
    assert "[6 more approximate matches: rerun with --limit 8]" in err
