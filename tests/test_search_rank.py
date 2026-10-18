"""contexto_search matching and ranking (D34)."""

import io
import json
import re

import pytest

from contexto import mcp
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.pages import _texts
from contexto.store.render import match_lines
from contexto.store.search import RECENCY_WEIGHT, Query, fold, rank

FILLER = "\n".join(f"step {i}: ran the build and checked the output, nothing unusual" for i in range(40))


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")


def page(text, prompt="continue"):
    return [{"role": "user", "content": prompt},
            {"role": "assistant", "content": [{"type": "text", "text": text}]}]


def search(store, query):
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_search", "arguments": {"query": query}}})
    return server.handle_line(line)["result"]["content"][0]["text"]


def detailed(reply):
    return [int(m.group(1)) for m in re.finditer(r"^pg_[0-9a-f]{16} \(turn (\d+)\)$", reply, re.M)]


def listed(reply):
    return [int(m.group(1)) for m in re.finditer(r"^pg_[0-9a-f]{16} \(turn (\d+)\) @", reply, re.M)]


# Matching


def test_a_query_matches_in_any_case_and_its_words_in_any_order(store):
    [pid] = store.page_out([page(FILLER + "\nNightly backups run at 02:00 from the replica.")], 1)
    for query in ("nightly backup", "NIGHTLY BACKUPS", "backup nightly", "replica 02:00 nightly"):
        assert store.search(query) == [pid], query
    assert store.search("nightly restore") == []  # every word must be on the page
    assert "> " in search(store, "backup nightly")


@pytest.mark.parametrize("query", ["E4127", "src/contexto/mcp.py", "TIMEOUT", "SUP-48213", "timeout_s = 30",
                                   "mcp.py:412"])
def test_identifiers_paths_and_their_parts_still_match(store, query):
    text = (FILLER + "\nerror E4127 in src/contexto/mcp.py:412 (ticket SUP-48213)\n"
            "HTTP_TIMEOUT_S = 30")
    [pid] = store.page_out([page(text)], 1)
    store.page_out([page(FILLER)], 2)
    assert store.search(query) == [pid]
    reply = search(store, query)
    assert detailed(reply) == [1] and re.search(r"^> \d+: ", reply, re.M)


def test_every_page_the_exact_rule_matched_still_matches(store):
    texts = [FILLER + f"\nthe cache.ttl_seconds is {i * 60}" if i % 3 else FILLER + "\nretries=4, Owner: ops"
             for i in range(12)]
    ids = store.page_out([page(t) for t in texts], 1)
    for query in ("cache.ttl_seconds", "retries=4", "Owner: ops", "is 120", "nothing unusual", "step 3:"):
        exact = {pid for pid, t in zip(ids, texts) if any(query in h for h in _texts(page(t)))}
        assert exact and exact <= set(store.search(query)), query


# Ranking


def test_pages_holding_the_whole_query_rank_above_pages_holding_only_its_words(store):
    phrase = store.page_out([page(FILLER + "\nThe flaky test is test_resume_after_crash.")], 1)
    scattered = store.page_out([page(FILLER + "\nflaky flaky flaky runner\n" + "test suite passed\n" * 5)
                                for _ in range(6)], 2)
    found = store.search("flaky test")
    assert found[0] == phrase[0] and set(found[1:]) == set(scattered)
    assert detailed(search(store, "flaky test"))[-1] == 1  # among the best three, still shown newest first


def test_a_page_about_the_query_outranks_newer_pages_that_mention_it_once(store):
    about = "\n".join(["The rate limit is 40 requests per minute per key.", "Raised the rate limit warning threshold.",
                       "rate limit headers: X-RateLimit-Remaining", "The rate limit resets every 60 seconds."])
    [target] = store.page_out([page(FILLER + "\n" + about)], 1)
    store.page_out([page(FILLER + "\nwe may hit a rate limit here") for _ in range(30)], 2)
    assert store.search("rate limit")[0] == target
    assert 1 in detailed(search(store, "rate limit"))  # today's newest-first order would bury it


def test_the_newest_of_equally_relevant_statements_is_shown_first(store):
    """A value set three times among newer pages that mention the word in passing: the reply's first
    value page is the latest, wherever the distractors fall."""
    turns = {}
    for t in range(1, 61):
        if t in (8, 30, 45):
            turns[t] = page(FILLER + f"\nSet the HTTP client timeout to {t} seconds.\nHTTP_TIMEOUT_S = {t}")
        else:
            turns[t] = page(FILLER + ("\na client timeout surfaces as a 504" if t % 4 == 0 else ""))
    ids = {t: store.page_out([turn], t)[0] for t, turn in turns.items()}
    for query in ("timeout", "client timeout", "HTTP_TIMEOUT_S"):
        shown = detailed(search(store, query))
        values = [t for t in shown if t in (8, 30, 45)]
        assert values and values[0] == 45, (query, shown)
        assert shown == sorted(shown, reverse=True)  # the best pages, newest first
    assert store.search("HTTP_TIMEOUT_S")[0] == ids[45]


def test_equally_relevant_pages_come_newest_first(store):
    ids = store.page_out([page(FILLER + "\nneedle") for _ in range(8)], 1)
    assert store.search("needle") == ids[::-1]


def test_the_best_three_and_the_next_fifty_are_each_shown_newest_first():
    store = MemoryStore("s1")
    store.set_window(200_000, 2.0)
    for t in range(1, 81):
        mentions = 4 if t % 10 == 3 else 1  # turns 3, 13, ..., 73 are about the key
        store.page_out([page(FILLER + "\nkey.alpha=on" * mentions)], t)
    reply = search(store, "key.alpha")
    assert detailed(reply) == [73, 63, 53]
    more = listed(reply)
    assert len(more) == 50 and more == sorted(more, reverse=True)
    assert 43 in more and 3 in more  # the other pages about the key are among the next best
    assert reply.splitlines()[-1] == "[27 more matching pages not shown; use a longer or more specific query]"


def test_the_recency_prior_is_bounded():
    """Relevance and recency combine as relevance x (1 + RECENCY_WEIGHT x position among the matches)."""
    assert RECENCY_WEIGHT == 0.5
    texts = {"old": "k " * 8 + "x " * 200, "new": "k " + "x " * 207}  # the same length
    ranked = rank([("old", 1, fold(texts["old"])), ("new", 2, fold(texts["new"]))], "k")
    assert ranked == ["old", "new"]  # 8 mentions beat 1 even at the far end of the session
    ranked = rank([("old", 1, fold("k k " + "x " * 206)), ("new", 2, fold("k " + "x " * 207))], "k")
    assert ranked == ["new", "old"]  # 2 mentions against 1 do not


# Lines


def test_lines_holding_the_whole_query_are_shown_before_lines_with_its_words():
    text = "backup ran nightly\nthe nightly backup finished\nnothing\nnightly: backup skipped"
    shown, total = match_lines(text, "nightly backup")
    assert total == 1 and [line for _, _, m, line in shown if m] == ["the nightly backup finished"]
    shown, total = match_lines(text.replace("the nightly backup", "the backup"), "nightly backup")
    assert total == 2 and [line for _, _, m, line in shown if m] == ["backup ran nightly", "nightly: backup skipped"]


def test_words_on_separate_lines_show_the_lines_with_the_rarer_word():
    text = "\n".join(["port 1", "port 2", "staging host is db-7", "port 3"])
    shown, total = match_lines(text, "staging port")
    assert total == 1 and [line for _, _, m, line in shown if m] == ["staging host is db-7"]


def test_matching_lines_ignore_case_and_keep_exact_offsets():
    text = "head\nCFG Deploy.Target=blue\ntail"
    [(start, end, is_match, line)] = match_lines(text, "deploy.target", context=0)[0]
    assert is_match and text[start:end] == line == "CFG Deploy.Target=blue"
    assert Query.parse("  ") is None and match_lines(text, " ") == ([], 0)


def test_in_view_lines_match_like_pages(store):
    store.set_window(200_000, 2.0)
    store.page_out([page(FILLER + "\nnightly backup at 01:00")], 1)
    store.set_retained(1, 0, [{"turn": 9, "message_index": 0, "role": "assistant",
                               "text": "intro\nNightly backups now run at 03:00\nend"},
                              {"turn": 10, "message_index": 0, "role": "user", "text": "when is the nightly backup?"}])
    store.set_turn_room("r", 10_000)
    lines = search(store, "backup nightly").split("\n")
    assert lines[:2] == ["[in view, newest first:]", "> in view (turn 9): Nightly backups now run at 03:00"]
    assert lines[2].endswith("(turn 1)")


def test_a_word_on_every_line_of_a_large_in_view_message_is_still_fast(store):
    import time

    store.set_window(200_000, 2.0)
    text = "\n".join(f"    self.value_{i} = self.compute({i})" for i in range(20_000))  # about 700 KB
    store.set_retained(1, 0, [{"turn": 9, "message_index": 0, "role": "tool_result", "text": text}])
    store.set_turn_room("r", 10_000)
    started = time.perf_counter()
    lines = search(store, "self").split("\n")
    assert time.perf_counter() - started < 2
    assert lines[1] == "> in view (turn 9):     self.value_19999 = self.compute(19999)"
    assert lines[4] == "  [19997 more matching lines in view]"
