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


def test_the_cut_keeps_identifiers_numbers_and_paths_before_the_longest_words(store):
    query = "what is the staging database port 6543 for deploys tuesday nightly backups server"
    plan = S.plan_words(query)
    # 6543 is the shortest word and the most specific: it is kept, and "backups" is cut in its place.
    assert plan.words == ("staging", "database", "6543", "deploys", "tuesday", "nightly") and plan.cut == 3
    assert plan.searched() == "[searched for: staging, database, 6543, deploys, tuesday, nightly]"
    hits = store.search_approximate(query)
    assert hits[0].id == ids(store)[3] and [m.word for m in hits[0].matched] == ["staging", "database", "6543"]
    many = S.plan_words("a1 b2 c3 d4 e5 f6 g7 h8 longword")
    assert many.words == ("a1", "b2", "c3", "d4", "e5", "longword")  # two characters are not specific: by length
    paths = S.plan_words("compare src/a.py src/b.py E4127 retry_budget v1.2 refreshToken with the nightly backups")
    assert paths.words == ("src/a.py", "src/b.py", "e4127", "retry_budget", "v1.2", "refreshtoken")


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
    # A separator between two digits is not dropped on the page either (reviewer B on PR #267).
    for typed, on_page in (("port-80", "port8.0"), ("rev-12", "rev1.2"), ("garnet-4821", "garnet-48.21"),
                           ("error-404", "error_40.4"), ("log-20261105", "log-2026-11-05")):
        assert rank_approximate(entries(f"the {on_page} here", "nothing"), typed) == [], typed
    both = rank_approximate(entries("sha-2.56 and sha_256", "nothing"), "sha.256")
    assert how(both[0]) == {"sha.256": ("folded", "sha_256")}  # the form that is the same number
    kept = rank_approximate(entries("see build_20.26 and build-2026", "nothing"), "build.2026")
    assert how(kept[0]) == {"build.2026": ("folded", "build-2026")}
    six = S.plan_words("refresh_token refreshToken UserCache REFRESH Staging E4127")
    assert [six.shaped(j) for j in range(6)] == [True, True, True, False, False, False]
    assert [six.specific(j) for j in range(6)] == [True, True, True, False, False, True]
    assert [six.plain(j) for j in range(6)] == [False, False, False, True, True, False]
    assert S.plan_words("HTTPTimeout").shaped(0) and not S.plan_words("HTTP").shaped(0)


def test_a_separator_at_the_start_or_the_end_of_a_word_does_not_make_it_an_identifier():
    for typed, on_page in (("--force", "we enforce the rule"), ("__init__", "initialize the pool"),
                           ("--limit", "the rate limit moved"), (".gitignore", "the gitignore docs"),
                           ("retry_", "a retry here")):
        plan = S.plan_words(typed)
        assert not plan.shaped(0), typed
        assert rank_approximate(entries(on_page, "nothing"), typed) == [], typed
    assert found(rank_approximate(entries("run it with --force", "we enforce the rule"), "--force zzqq")) == [0]
    assert found(rank_approximate(entries("def __init__(self):", "initialize the pool"), "__init__ zzqq")) == [0]
    # a separator inside still folds, whatever stands at the ends
    hits = rank_approximate(entries("the dryRun flag", "nothing"), "--dry-run")
    assert how(hits[0]) == {"--dry-run": ("folded", "dryrun")}


def test_a_word_is_an_identifier_when_any_of_its_spellings_in_the_query_is_one():
    # Reviewer B on PR #267, round 2: the first spelling typed decided, so the order of the words did.
    pages = entries(*[f"page {i} ran the build" for i in range(6)], "the refresh_token is stored")
    for query in ("refreshtoken refreshToken", "refreshToken refreshtoken"):
        plan = S.plan_words(query)
        assert plan.words == ("refreshtoken",) and plan.typed == ("refreshToken",), query
        assert plan.shaped(0) and plan.specific(0), query
        hits = rank_approximate(pages, query)
        assert found(hits) == [6] and how(hits[0]) == {"refreshtoken": ("folded", "refresh_token")}, query
    assert S.plan_words("Staging staging").typed == ("Staging",)  # no spelling is an identifier: the first is kept


def test_a_specific_word_has_three_characters_or_more_and_an_accent_is_a_letter():
    plan = S.plan_words("5 42 v2 E41 6543 a.b caf\u00e9 cafe\u0301 it's")
    assert [w for j, w in enumerate(plan.words) if plan.specific(j)] == ["e41", "6543", "a.b"]
    assert S.SPECIFIC_MIN_CHARS == 3


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


def test_a_word_that_no_page_holds_counts_against_every_page(store):
    by_turn = ids(store)
    # 7 pages. A word on 1 page weighs ln(1 + 6.5 / 1.5) = 1.67; a word on no page ln(1 + 7.5 / 0.5) = 2.77.
    known, unknown = math.log(1 + 6.5 / 1.5), math.log(1 + 7.5 / 0.5)
    assert known / (known + unknown) > S.APPROX_FLOOR > known / (known + 2 * unknown)  # 38 and 23 percent
    hits = store.search_approximate("nightly zzqqxx")
    assert [h.id for h in hits] == [by_turn[0]] and hits[0].missing == ("zzqqxx",)
    assert store.search_approximate("nightly zzqqxx yyqqxx") == []
    assert store.search_approximate("kubernetes ingress certificate") == []
    assert store.search_approximate("which engineer approved the kubernetes ingress") == []
    hits = store.search_approximate("which engineer approved the nightly backups")  # 2 of 4: 38 percent
    assert [h.id for h in hits] == [by_turn[0]] and hits[0].missing == ("engineer", "approved")
    assert store.search_approximate("which engineer approved and signed the nightly backups") == []  # 2 of 5: 29
    hits = store.search_approximate("nightly backups zzqqxx")
    assert [h.id for h in hits] == [by_turn[0]] and hits[0].missing == ("zzqqxx",)


def test_the_floor_is_one_share_with_no_rule_that_depends_on_the_number_of_pages():
    # The cases of reviewer B on PR #267, which the earlier floor answered differently.
    hundred = [f"page {i} ran the build" for i in range(100)]
    hundred[40] += " on kubernetes ingress"
    hundred[41] += " with kube_proxy"
    pages = entries(*hundred)
    assert found(rank_approximate(pages, "kubernetes zzqqxx")) == [40]  # a plain word and an identifier alike
    assert found(rank_approximate(pages, "kube_proxy zzqqxx")) == [41]
    assert found(rank_approximate(pages, "kubernetes ingress zzqqxx yyqqxx")) == [40]  # 2 rare words of 4
    assert rank_approximate(pages, "kubernetes zzqqxx yyqqxx") == []  # 1 of 3: 28 percent
    for size in (9, 10, 11):  # no cliff: a word on every page weighs next to nothing in a store of any size
        assert rank_approximate(entries(*hundred[:size]), "ran build zzqqxx") == [], size
    assert rank_approximate(entries(*seven_pages()), "5 zzqqxx") == []  # a digit is not a specific word
    assert found(rank_approximate(entries(*seven_pages()), "6543 zzqqxx")) == [3]


def test_a_specific_word_on_more_than_half_the_pages_is_not_enough_on_its_own():
    # Reviewer A on PR #267, round 2: with an odd number of pages, 4 of 7 passed as "not more than half".
    for held, size, expected in ((4, 7, []), (3, 7, [2, 1, 0]), (4, 8, [3, 2, 1, 0]), (5, 8, [])):
        texts = [f"page {i}" + (" code 6543" if i < held else "") for i in range(size)]
        assert found(rank_approximate(entries(*texts), "6543 zzqqxx yyqqxx wwqqxx")) == expected, (held, size)


def seven_pages():
    return [turn[1]["content"][0]["text"] for turn in TURNS]


def test_a_typo_that_is_not_found_does_not_make_its_word_weigh_less():
    # "payment" is on 1 page of 100; "invoice" and "overdue" on none. "invoice" is tried as a typo and not
    # found, so it still counts in full: 4.21 of 4.21 + 2 * 5.31 is 28 percent, under the floor.
    texts = [f"page {i} ran the build" for i in range(100)]
    texts[7] += " and took the payment"
    assert rank_approximate(entries(*texts), "invoice payment overdue") == []
    assert found(rank_approximate(entries(*texts), "payment overdue")) == [7]  # 44 percent


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


def test_words_that_most_pages_hold_do_not_carry_a_query_whose_other_word_is_unknown():
    texts = [f"page {i}: the function will return a value" for i in range(20)] + ["the staging port value moved"]
    pages = entries(*texts)
    # "foo" is on no page and weighs 3.78. "return" (20 of 21 pages) weighs 0.07, "value" (21 of 21) 0.02.
    assert rank_approximate(pages, "what is the return value of foo") == []
    assert rank_approximate(pages, "what does foo return as a value") == []
    assert found(rank_approximate(pages, "what is the staging value of foo")) == [20]  # "staging" is on 1 page
    assert len(rank_approximate(pages, "what is the return value")) == 20  # no word unknown: every word held
    assert rank_approximate(entries(*texts[:5]), "what is the return value of foo") == []  # the same in a small store


def test_words_that_follow_each_other_in_the_query_rank_first_when_the_page_has_them_so():
    texts = [["the db is up", "the port is open"], "the DB port is 6543", ["db", "port", "db and port", "port and db"],
             ["db checked", "port checked"], *[f"filler {i}" for i in range(8)]]
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
    pages = entries("the staging database listens on port 6543", *[f"filler {i}" for i in range(5)])
    hits = rank_approximate(pages, "stagign databse port")  # two typos: the longer word is tried
    assert how(hits[0]) == {"port": ("exact", "port"), "stagign": ("typo", "staging")}
    assert hits[0].missing == ("databse",)
    assert rank_approximate(pages, "a" * 41) == [] and S.TYPO_MAX_CHARS == 40
    monkeypatch.setattr(S, "TYPO_WORK", 3)
    many = entries(*["data data data data database"] * 5)
    assert rank_approximate(many, "databse") == []  # more candidate positions than the bound: the typo is not searched


@pytest.mark.parametrize("held", [True, False])
def test_the_typo_work_bound_is_for_the_whole_query(monkeypatch, held):
    # Reviewer A on PR #267, round 2: the count started again at 0 for the next typo word. "kubernetes"
    # is looked for first and checks the 3 places of "kube" on page 0; page 1 holds it, so "databse" is
    # looked for from there, and on page 2 it is found at its third place, the sixth of the query.
    texts = ["kube kube kube", "kubernetes runs it", "data data database"]
    pages = entries(*texts) if held else [e._replace(text=None) for e in entries(*texts)]
    fetch = (lambda i: entries(*texts)[i]) if not held else None
    for work, typo_found in ((5, False), (6, True)):
        monkeypatch.setattr(S, "TYPO_WORK", work)
        hits = rank_approximate(pages, "kubernetes databse", fetch)
        assert (2 in found(hits)) is typo_found, work
        assert 1 in found(hits)


def test_the_next_typo_word_is_looked_for_from_the_page_that_held_the_longer_one():
    # "kubernetes" is the longest plain word, so it is looked for first; page 3 holds it. From there on
    # "databse" is looked for, and only pages 0 to 2 are read again (reviewer B on PR #267).
    texts = ["the database is up", "filler one", "the staging database", "kubernetes runs it", "filler two",
             "a staging database again", "filler three", "filler four"]
    held = entries(*texts)
    reads: list[int] = []

    def fetch(i):
        reads.append(i)
        return held[i]

    unheld = [e._replace(text=None) for e in held]
    hits = rank_approximate(unheld, "kubernetes databse staging", fetch)
    assert sorted(reads) == [0, 0, 1, 1, 2, 2, 3, 4, 5, 6, 7]  # the three pages before the switch are read twice
    typo_pages = sorted(int(h.id[3:], 16) for h in hits if "databse" in how(h))
    # page 2 is before the switch and page 5 after it; page 0 holds the typo's word alone, under the floor
    assert typo_pages == [2, 5] and all(how(h)["databse"] == ("typo", "database") for h in hits if "databse" in how(h))
    assert hits == rank_approximate(held, "kubernetes databse staging")  # the same with every page held
    # the longest word is on the first page: nothing is read twice
    reads.clear()
    rank_approximate(unheld, "database kubernetse", fetch)
    assert sorted(reads) == list(range(8))


@pytest.mark.parametrize("seed", range(40))
def test_the_typo_word_and_its_pages_do_not_depend_on_where_the_longer_words_sit(seed):
    import random

    rng = random.Random(seed)
    vocabulary = ["database", "kubernetes", "staging", "replica", "backups", "nightly", "rollout", "canary"]
    texts = [" ".join(rng.choice(vocabulary) for _ in range(rng.randrange(1, 4))) for _ in range(12)]
    query = " ".join(rng.sample(["databse", "kubernetse", "stagign", "replcia", "bakcups", "database", "kubernetes",
                                 "nightly", "zzqqxx"], 3))
    held = entries(*texts)
    plan = S.plan_words(query)
    absent = [w for w in sorted(plan.words, key=lambda w: (-len(w), plan.words.index(w)))
              if plan.plain(plan.words.index(w)) and not any(w in t for t in texts)]
    expected = {}
    if absent:  # the reference: the longest plain word no page holds, looked for on every page in order
        finder = S._TypoFinder(absent[0])
        for i, text in enumerate(texts):
            form = finder.find(S.search_key(text))
            if form is not None:
                expected[i] = (absent[0], form.decode())
    hits = rank_approximate(held, query)
    got = {int(h.id[3:], 16): (m.word, m.form) for h in hits for m in h.matched if m.how == "typo"}
    assert got.items() <= expected.items()
    assert hits == rank_approximate([e._replace(text=None) for e in held], query, lambda i: held[i])
    solo = rank_approximate(held, absent[0]) if absent else []  # alone, every page with the form passes the floor
    assert {int(h.id[3:], 16): (m.word, m.form) for h in solo for m in h.matched} == (
        expected if absent and not any(absent[0] in t for t in texts) else {})


# Two facts on two pages


def two_fact_pages():
    """40 pages. Fact A (page 5): "redis" is on that page only, "eviction policy" on 3 more. Fact B (page
    30): each of its three words is on 10 pages, so the page weighs less than a page that shares two
    words with fact A."""
    texts = [f"page {i} ran the build" for i in range(40)]
    texts[5] += "; the redis eviction policy is allkeys-lru"
    texts[30] += "; the staging gateway port is 6543"
    for i in (8, 12, 16, 20, 24, 28, 32, 36, 38):
        texts[i] += "; the gateway port was checked"
    for i in (2, 6, 10, 14, 18, 22, 26, 34, 37):
        texts[i] += "; staging was fine"
    for i in (9, 13, 17):
        texts[i] += "; the eviction policy was discussed"
    return texts


def test_the_second_fact_of_a_query_takes_the_third_detailed_place():
    pages = entries(*two_fact_pages())
    query = "staging gateway port redis eviction policy"
    assert rank(pages, query) == []
    hits = rank_approximate(pages, query)
    order = found(hits)
    # Page 5 holds the three rarest words. The 3 pages that share "eviction policy" with it come next (4.42
    # of the weight each), and page 30, which holds the other fact (4.09), would be fifth: it takes the
    # third place.
    assert order[0] == 5 and order[2] == 30 and set(order[1:2]) <= {9, 13, 17} and order.index(30) == 2
    S_second = S._second_page
    try:
        S._second_page = lambda *args: None
        assert found(rank_approximate(pages, query)).index(30) == 4  # without the place
    finally:
        S._second_page = S_second
    assert [m.word for m in hits[2].matched] == ["staging", "gateway", "port"]
    assert hits[2].missing == ("redis", "eviction", "policy")
    assert S.SHOWN_IN_DETAIL == 3 and hits.total == len(hits) == len(set(order))


def test_the_second_page_is_added_even_when_it_is_under_the_floor():
    pages = entries(*two_fact_pages())
    # "gateway" and "port" are on 10 pages each: 2.72 of the query's 10.45, which is 26 percent, under the
    # floor. They are what page 5 lacks, and they are not common words (a word on no page weighs 4.4), so
    # the best page for them is shown third: the newest of the 10 that hold both.
    hits = rank_approximate(pages, "gateway port redis eviction policy")
    assert found(hits) == [5, 17, 38, 13, 9] and hits.total == 5
    assert [m.word for m in hits[2].matched] == ["gateway", "port"]
    assert hits[2].missing == ("redis", "eviction", "policy")


def test_no_second_page_when_the_best_page_holds_every_word_or_only_a_common_word_is_left():
    texts = two_fact_pages()
    pages = entries(*texts)
    full = rank_approximate(pages, "what is the redis eviction policy")  # page 5 holds every word
    assert found(full)[0] == 5 and all("build" not in how(h) for h in full)
    assert set(found(full)) == {5, 9, 13, 17}
    # "build" is on every page: it is what page 5 lacks of this query, and no page is shown for it
    left = rank_approximate(pages, "redis eviction policy zzqqxx build")
    assert found(left)[0] == 5 and set(found(left)) <= {5, 9, 13, 17}
    # a word on a third of the pages is a leftover too: it weighs 0.7 against 4.4 for a word on no page
    for i in range(0, 40, 3):
        texts[i] += " in region west"
    left = rank_approximate(entries(*texts), "redis eviction policy region")
    assert found(left)[0] == 5 and all("redis" in how(h) or "eviction" in how(h) for h in left)


def hundred_pages(extra):
    """100 filler pages, oldest first, with ``extra[i]`` added to page ``i``."""
    texts = [f"page {i} ran the build" for i in range(100)]
    for i, text in extra.items():
        texts[i] += text
    return texts


def test_the_bar_for_the_second_page_is_half_the_weight_of_a_word_on_no_page():
    # The lacked words a page holds must weigh SECOND_BAR of a word on no page: one word is on at most
    # sqrt((n + 1) / 2) - 1/2 pages, 6 of 100 (6.6 percent) and 54 of 6,000 (0.9 percent).
    assert S.SECOND_BAR == 0.5
    for n, most in ((100, 6), (6000, 54)):
        nowhere = math.log(1 + (n + 0.5) / 0.5)
        weight = [math.log(1 + (n - d + 0.5) / (d + 0.5)) for d in (most, most + 1)]
        assert weight[0] >= S.SECOND_BAR * nowhere > weight[1], n
        assert most == math.floor(math.sqrt((n + 1) / 2) - 0.5)
    texts = ["page 5 the redis eviction policy is allkeys-lru"] + [f"page {i} ran the build" for i in range(1, 100)]
    for df, shown in ((6, True), (7, False)):
        extra = {i: "; the cutover went fine" for i in range(99, 99 - df, -1)}
        pages = entries(*[t + extra.get(i, "") for i, t in enumerate(texts)])
        order = found(rank_approximate(pages, "redis eviction policy cutover"))
        assert (order[1:2] == [99]) is shown, df


@pytest.mark.parametrize("word, df", [("server", 12), ("server", 14), ("using", 20)])
def test_a_leftover_common_word_takes_no_detailed_place(word, df):
    # Reviewers A and B on PR #267, round 2: a page that held only the word the best page lacked took the
    # third place, and a page that holds the query's other words fell to the list.
    extra = {i: f"; {word} it" for i in range(99, 99 - 5 * df, -5)}
    extra.update({20: "; the staging database port is 6543", 21: "; the staging database was migrated",
                  22: "; staging database backups ran"})
    hits = rank_approximate(entries(*hundred_pages(extra)), f"staging database port {word}")
    assert found(hits) == [20, 22, 21]
    extra = {i: "; the server restarted" for i in (*range(11, 99, 8), 99)}
    extra.update({30: "; the canary rollout plan is 5 percent first", 40: "; the canary was healthy",
                  50: "; the rollout plan was approved", 60: "; rollout notes"})
    assert found(rank_approximate(entries(*hundred_pages(extra)), "canary rollout plan server")) == [30, 50]


def test_a_word_on_two_of_seven_pages_is_a_leftover_too(store):
    by_turn = ids(store)
    # "output" is on the two build-log pages (0 and 5): it weighs 1.16 against 2.77 for a word on no page
    hits = store.search_approximate("canary rollout output")
    assert [h.id for h in hits] == [by_turn[6]] and hits[0].missing == ("output",)


def test_the_second_page_is_the_one_that_also_holds_the_query_s_other_words():
    # Reviewer B on PR #267, round 2: "restart" is what the best page lacks. Page 30 holds it with
    # "nginx"; page 90 holds it alone and is newer. Page 30 is the answer.
    extra = {40: "; nginx proxy timeouts were raised", 41: "; nginx proxy buffers set",
             30: "; to restart the nginx service run systemctl", 90: "; restart the laptop"}
    hits = rank_approximate(entries(*hundred_pages(extra)), "how do I restart the nginx proxy")
    assert found(hits)[:3] == [41, 40, 30] and found(hits)[3:] == [90]


def test_the_second_page_is_the_newest_value_when_two_pages_hold_the_lacked_words():
    # Reviewer B on PR #267, round 2: the older value said in two texts took the place (D28).
    texts = [f"page {i} ran the build" for i in range(20)]
    for i, line in ((10, "staging database port is 6543"), (11, "the staging database port moved"),
                    (12, "staging database port checked")):
        texts[i] += "; " + line
    texts[13] = [texts[13] + "; prod host is db1", "confirmed: prod host db1"]
    texts[15] += "; prod host is now db2"
    hits = rank_approximate(entries(*texts), "staging database port prod host")
    assert found(hits)[:3] == [12, 11, 15] and set(found(hits)[3:]) == {10, 13}


def sized_pages(n, extra):
    """``n`` filler pages, oldest first, with ``extra[i]`` added to page ``i``."""
    texts = [f"page {i} ran the build" for i in range(n)]
    for i, text in extra.items():
        texts[i] += text
    return entries(*texts)


STAGING = {20: "; the staging database port is 6543", 21: "; the staging database port moved",
           22: "; staging database port checked"}


@pytest.mark.parametrize("n", [100, 1000])
def test_a_second_fact_matched_through_a_typo_takes_the_place(n):
    # Reviewer B on PR #267, round 3: a typo word weighs half, and half the weight of a word on one page
    # is under SECOND_BAR at every store size, so page 40 dropped out. The bar reads the unhalved weight.
    pages = sized_pages(n, {**STAGING, 40: "; backups run at 02:00"})
    for query in ("staging database port bakcups", "staging database port backups"):
        hits = rank_approximate(pages, query)
        assert found(hits) == [22, 21, 40, 20] and hits.total == 4, query
    assert how(rank_approximate(pages, "staging database port bakcups")[2]) == {"bakcups": ("typo", "backups")}


def test_a_typo_word_on_a_common_page_is_still_refused_the_place():
    # Unhalved, "backups" on 8 of 100 pages is over the 6 that one word may be on
    extra = {**STAGING, **{i: "; backups run" for i in range(99, 91, -1)}}
    for query in ("staging database port bakcups", "staging database port backups"):
        assert found(rank_approximate(sized_pages(100, extra), query)) == [22, 21, 20], query


def test_a_single_word_second_fact_matched_through_a_typo_is_shown():
    # Reviewer A on PR #267, round 3: typed correctly, page 70 is third; with the typo it was not returned
    extra = {5: "; the redis eviction policy is allkeys-lru", 6: "; eviction policy noted",
             70: "; kubernetes runs the workers"}
    for query in ("redis eviction policy kuberntes", "redis eviction policy kubernetes"):
        assert found(rank_approximate(sized_pages(100, extra), query)) == [5, 6, 70], query


def rate_limit_pages(df):
    """100 pages. The first fact, ``webhook signing secret``, is on pages 10 to 12; ``rate`` and
    ``limit`` are each on ``df`` pages and stand together only on page 60."""
    extra = {10: "; the webhook signing secret for the API is in vault", 11: "; webhook signing secret rotated",
             12: "; webhook signing secret checked", 60: "; the API rate limit is 600 a minute"}
    for i in range(df - 1):
        extra[30 + i] = extra.get(30 + i, "") + "; the error rate was low"
        extra[99 - i] = extra.get(99 - i, "") + "; the disk limit was raised"
    return sized_pages(100, extra)


@pytest.mark.parametrize("df", [15, 18, 30])
def test_a_second_fact_whose_words_stand_together_as_typed_takes_the_place(df):
    # The orchestrator on PR #267, round 3: at 6,000 pages SECOND_BAR refused real second facts of fairly
    # common words. A page that holds a lacked word beside its neighbour as the query has them passes.
    pages = rate_limit_pages(df)
    hits = rank_approximate(pages, "webhook signing secret and API rate limit")
    # page 10 also holds "API", so what it lacks is "rate limit": 2.4 of a word on no page's 5.3 at df 30
    assert found(hits)[:3] == [10, 12, 60] and list(how(hits[2])) == ["api", "rate", "limit"]


def test_a_single_lacked_word_beside_a_held_neighbour_takes_the_place():
    extra = {10: "; the worker pool size is 8", 11: "; worker pool drained", 12: "; worker pool size checked",
             70: "; the batch size is 500"}
    for i in range(14):
        extra[30 + i] = "; the batch finished"
    query = "worker pool size and batch size"
    assert found(rank_approximate(sized_pages(100, extra), query))[:3] == [12, 10, 70]
    extra[70] = "; size the batch to 500"  # the same words, not side by side: "batch" alone is too common
    assert 70 not in found(rank_approximate(sized_pages(100, extra), query))[:3]


@pytest.mark.parametrize("query, extra, noise, beside", [
    ("staging database port server", {**STAGING, **{i: "; server it" for i in range(99, 39, -5)}}, 99,
     "; the port server it"),
    ("staging database port using", {**STAGING, **{i: "; using it" for i in range(99, 0, -5)}}, 99,
     "; the port using it"),
])
def test_a_leftover_word_is_refused_because_it_stands_beside_no_query_neighbour(query, extra, noise, beside):
    # The noise cases of rounds 2 and 3 are single words standing alone. Put the same word next to its
    # neighbour in the query and the page takes the place. (``restart the laptop`` is not refused by the
    # bar: "restart" is on 2 of 100 pages and passes it. It loses the place to the page that also holds
    # "nginx", test_the_second_page_is_the_one_that_also_holds_the_query_s_other_words.)
    assert noise not in found(rank_approximate(sized_pages(100, extra), query))[:3]
    assert found(rank_approximate(sized_pages(100, {**extra, noise: beside}), query))[2] == noise


def test_output_on_two_of_seven_pages_is_refused_and_beside_rollout_is_shown(store):
    by_turn = ids(store)
    assert [h.id for h in store.search_approximate("canary rollout output")] == [by_turn[6]]
    store.page_out([page("the rollout output was clean")], 30)
    assert store.search_approximate("canary rollout output")[1].id == store.page_ids()[0]


def test_a_specific_word_takes_the_place_only_with_30_percent_of_the_lacked_weight():
    # Reviewer B on PR #267, round 3: "2024" on 40 of 100 pages took the place from page 95, which holds
    # the rarer "retention" with "deploy". A specific word passes the bar, not the 30 percent.
    extra = {i: "; released in 2024" for i in range(40, 80)}
    extra.update({i: "; retention set" for i in range(85, 94)})
    extra.update({30: "; the deploy pipeline is green", 31: "; deploy pipeline rebuilt",
                  95: "; deploy retention is 30 days"})
    hits = rank_approximate(sized_pages(100, extra), "deploy pipeline 2024 retention")
    assert found(hits)[:3] == [31, 30, 95] and 59 not in found(hits)[:3]
    # with "2024" on 30 percent of the lacked weight or more, it takes the place as before
    extra = {**{i: "; released in 2024" for i in range(40, 48)}, 30: "; the deploy pipeline is green",
             31: "; deploy pipeline rebuilt"}
    assert found(rank_approximate(sized_pages(100, extra), "deploy pipeline 2024 retention"))[:3] == [31, 30, 47]


def test_a_turn_page_takes_the_place_before_a_context_block():
    texts = [f"page {i} ran the build" for i in range(100)]
    for i, line in STAGING.items():
        texts[i] += line
    texts[40] += "; deploys go out on tuesday"
    texts[41] += "; deploys were paused"
    pages = entries(*texts)
    pages[40] = pages[40]._replace(context=True)  # holds both lacked words, but is a context block
    hits = rank_approximate(pages, "staging database port deploys tuesday")
    assert found(hits)[:3] == [22, 21, 41] and 40 in found(hits)[3:]
    pages[41] = pages[41]._replace(context=True)
    assert found(rank_approximate(pages, "staging database port deploys tuesday"))[:3] == [22, 21, 40]


def test_the_page_under_the_floor_is_counted_in_total_and_in_the_reply_s_more_line(monkeypatch):
    # The place can go to a page under the floor. It is one of the pages returned, so ``total`` counts it,
    # and the reply's "N more not shown" line, total less the pages shown and listed, does too.
    texts = [f"page {i} ran the build" for i in range(100)]
    for i in range(40):
        texts[i] += "; the gateway restarted"
        texts[40 + i] += "; the port opened"
    for i in range(80, 96):
        texts[i] += "; the redis eviction policy was reviewed"
    texts[90] = "the staging gateway port is 6543"  # "gateway port" weighs 1.8 of 7.3, 25 percent
    texts[96] += "; the redis eviction policy is allkeys-lru"
    store = MemoryStore("s1")
    store.page_out([page(t) for t in texts], 0)
    query = "gateway port redis eviction policy"
    hits = store.search_approximate(query)
    assert hits[2].id == ids(store)[90] and len(hits) == hits.total == 17
    monkeypatch.setattr(S, "_second_page", lambda *args: None)
    assert ids(store)[90] not in [h.id for h in store.search_approximate(query)]
    assert store.search_approximate(query).total == 16
    monkeypatch.undo()
    text, _ = call(store, query)
    assert len(DETAILED.findall(text)) == 3 and len(LISTED.findall(text)) == 10
    assert "[4 more approximate matches not shown; use a more specific query]" in text


@pytest.mark.parametrize("words", [("refreshtoken", "RefreshToken", "refreshToken"),
                                   ("refreshToken", "refreshtoken", "RefreshToken"),
                                   ("RefreshToken", "refreshToken", "refreshtoken")])
def test_three_spellings_of_one_word_are_one_word(store, words):
    by_turn = ids(store)
    hits = store.search_approximate(" ".join(words) + " keychain")
    assert hits.plan.words == ("refreshtoken", "keychain")
    assert hits.plan.typed[0] == next(w for w in words if w != "refreshtoken")  # the first in camelCase
    assert [h.id for h in hits] == [by_turn[1]]
    assert how(hits[0]) == {"refreshtoken": ("exact", "refreshtoken"), "keychain": ("exact", "keychain")}


def test_four_equally_rare_facts_on_four_pages_find_nothing():
    pages = entries("the kestrel nests", "an osprey dived", "a harrier hunted", "the condor soared",
                    *[f"filler {i}" for i in range(6)])
    assert rank_approximate(pages, "kestrel osprey harrier condor") == []  # a quarter each, under the floor
    assert sorted(found(rank_approximate(pages, "kestrel osprey harrier"))) == [0, 1, 2]  # a third each


def test_a_single_fact_keeps_its_page_among_the_first_three():
    pages = entries(*two_fact_pages())
    for query, page_no in (("what is the redis eviction policy?", 5), ("redis eviction zzqqxx", 5),
                           ("staging gateway port zzqqxx", 30), ("which port is the staging gateway on", 30),
                           ("staging gatewya port", 30), ("allkeys-lru policy for the cache", 5)):
        assert page_no in found(rank_approximate(pages, query))[:3], query


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
    for query in ("zzqqxx", "nightly zzqqxx yyqqxx", "what is the", "kubernetes ingress"):
        text, coverage = call(store, query)
        assert text == mcp.NO_HIT and coverage == mcp.SEARCH_COVERAGE_UNCHECKED
        event = last_event(store)
        assert event["approximate"] is False and event["hits"] == 0 and event["total_matches"] == 0


def test_at_most_three_detailed_pages_and_ten_listed(tmp_path):
    store = MemoryStore("s1")
    store.page_out([page(f"build number {i} passed") for i in range(400)], 0)  # so that the 40 are 1 page in 11
    store.page_out([page(f"deploy number {i} finished in region r{i % 5}") for i in range(40)], 400)
    text, _ = call(store, "what deploy finished zzqqxx")
    assert len(DETAILED.findall(text)) == 3 and len(LISTED.findall(text)) == mcp.APPROX_MORE_LIMIT == 10
    assert "[more approximate matches, recall one at its offset:]" in text
    assert "[27 more approximate matches not shown; use a more specific query]" in text
    listed = [line for line in text.split("\n") if LISTED.match(line)]
    assert all(re.fullmatch(r"pg_[0-9a-f]{16} \(turn \d+\) @\d+ \[matched: deploy, finished; not matched: zzqqxx\]",
                            line) for line in listed), listed[:2]
    turns = [int(t) for _, t in DETAILED.findall(text)]
    assert turns == sorted(turns, reverse=True) and 439 in turns  # the newest page holding the words is detailed
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


def test_a_strict_match_whose_pages_cannot_be_read_is_no_hit_and_never_approximate(tmp_path):
    # The fallback runs only when search itself finds no page (reviewer A on PR #267).
    store = SessionStore(tmp_path, "s1")
    pids = store.page_out(TURNS, 0)
    calls = []
    real = store.search_approximate
    store.search_approximate = lambda query: calls.append(query) or real(query)
    strict = store.search("staging database")
    assert strict == [pids[3]]
    store.search = lambda query: list(strict)  # the page goes between the search and the reply
    (store.pages_dir / f"{pids[3]}.json").unlink()
    text, _ = call(store, "staging database")
    assert text == mcp.NO_HIT and calls == []
    event = last_event(store)
    assert event["approximate"] is False and event["hits"] == 0 and event["total_matches"] == 0
    del store.search  # search now finds no page for it, so the fallback is asked
    assert call(store, "nightly zzqqxx")[0].startswith(mcp.SEARCH_APPROX_HEADER) and len(calls) == 1


def test_a_reply_that_shows_no_page_is_not_logged_as_approximate(store):
    store.set_window(200_000, 2.0)
    store.set_retained(1, 0, [{"turn": 40, "message_index": 0, "role": "user", "text": "what changed?"}])
    store.set_turn_room("7", 40)  # room for 60 characters: not even the header
    text, _ = call(store, "what port is the staging databse on")
    event = last_event(store)
    assert text == mcp.BUDGET_SPENT and event["hits"] == 0 and event["returned"] == []
    assert event["approximate"] is False and "approximate_steps" not in event
    store.page_out([page("the quokka cache " + "x" * 20_000)], 50)  # one matching line longer than a call returns
    store.set_turn_room("8", 10**6)
    store.set_window(4_000, 2.0)  # a per-call limit of 1,500 characters
    text, _ = call(store, "what quokka cache zzqq " + "y" * 1_600)
    assert text in (mcp.SEARCH_TOO_LONG, mcp.NO_HIT)
    assert last_event(store)["approximate"] is False


def test_the_logged_steps_are_those_of_the_pages_the_reply_showed(store):
    store.set_window(200_000, 2.0)
    store.set_retained(1, 0, [{"turn": 40, "message_index": 0, "role": "user", "text": "what changed?"}])
    store.page_out([page("the refreshToken staging note")], 60)
    store.set_turn_room("7", 10_000)
    text, _ = call(store, "refresh_token staging database port")
    assert len(DETAILED.findall(text)) == 3 and last_event(store)["approximate_steps"] == ["fold", "any_word"]
    assert "refreshToken" not in text.split("\n")[3]  # the first page holds the three plain words
    for room in range(150, 400, 5):  # the smallest room that shows a page: the first page only
        store.set_turn_room(str(room), room)
        text, _ = call(store, "refresh_token staging database port")
        if text != mcp.BUDGET_SPENT:
            break
    assert len(DETAILED.findall(text)) == 1 and "refreshToken" not in text
    event = last_event(store)
    assert event["approximate"] is True and event["approximate_steps"] == ["any_word"] and event["hits"] == 1


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
    # an approximate reply lists 10 further pages, a strict one 50: the description promises neither count
    assert "up to 50 more are listed" in search and "the next 50" not in search and mcp.APPROX_MORE_LIMIT == 10
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
    assert err.splitlines()[-1] == "[searched for: port, staging, databse]"  # as the tool's reply says
    lines = out.splitlines()
    assert lines[0].startswith(f"{home[3]} turn 3:")
    assert lines[1] == "  [matched: port, staging, databse as database]"
    assert any(line.lstrip().startswith(">") and "staging database listens" in line for line in lines)
    code, out, err = run(capsys, "search", "refresh_token", "zzqqxx", "keychain")
    assert "[searched for" not in err  # every word was searched
    assert code == 0 and "  [matched: refresh_token as refreshToken, keychain; not matched: zzqqxx]" in out.splitlines()
    assert any(line.lstrip().startswith(">") and "refreshToken" in line for line in out.splitlines())


def test_the_command_is_unchanged_for_a_strict_hit_and_for_nothing(home, capsys):
    code, out, err = run(capsys, "search", "refreshToken")
    assert code == 0 and out.splitlines()[0].startswith(f"{home[1]} turn 1:") and "approximate" not in out + err
    assert "[matched" not in out
    code, out, err = run(capsys, "search", "zzqqxx", "yyqqxx", "nightly")
    assert code == 0 and out == ""
    assert err.splitlines()[-1] == "no paged-out turn in session s1 contains every word of 'zzqqxx yyqqxx nightly'"


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
