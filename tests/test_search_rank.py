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
from contexto.store.search import RECENCY_WEIGHT, Query, SearchEntry, fold, rank, search_key

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


def test_fold_lowers_each_character_alone_and_writes_every_sigma_one_way():
    """``str.lower`` on a whole string lowers capital sigma by its position in a word (final ``ς``,
    else ``σ``), so a query and the page holding it could fold differently (reviewer-1, PR #204)."""
    assert fold("ΟΔΟΣ") == "οδοσ" and fold("ΟΔΟΣΑ") == "οδοσα" and fold("Σ") == "σ" and fold("ς") == "σ"
    assert fold("ΟΔΟΣ İstanbul") == "οδοσ İstanbul"  # İ (two characters lowered) is kept as it is
    for text in ("ΟΔΟΣ x", "ΑΣΑ", "λόγος", "ΟΔΟΣ İ", "aİΣ", "Σİ"):
        assert len(fold(text)) == len(text)  # offsets into the folded text are offsets into the text


def test_capital_sigma_matches_wherever_it_falls_in_a_word(store):
    [odos] = store.page_out([page("ΟΔΟΣ x")], 1)
    [asa] = store.page_out([page("ΑΣΑ")], 2)
    [both] = store.page_out([page("ΟΔΟΣ on the İstanbul map")], 3)
    assert set(store.search("Σ")) == {odos, asa, both}
    assert store.search("ΑΣ") == [asa]
    assert set(store.search("ΟΔΟΣ")) == {odos, both}
    assert set(store.search("οδος")) == {odos, both}
    assert "> " in search(store, "ΟΔΟΣ")


def _old_rule(page_record, query):
    """origin/main's page_matches: an exact, case-sensitive substring of the stub or of a text of the turn."""
    stub = page_record["stub"]
    haystacks = [stub["user_text"], stub.get("system_text", ""), *stub["tools"], *stub["paths"],
                 *_texts(page_record["messages"])]
    return bool(query) and any(query in h for h in haystacks)


def test_every_page_the_exact_rule_matched_still_matches_random_text():
    """Property check over random stores (after reviewer-1's harness for PR #204): for any query with a
    word, every page the old exact rule matched is still matched. Greek sigma in every position, dotted
    capital I, ß, ligatures, regex specials, quotes, backslashes, tabs and newlines."""
    import random

    from contexto.store.pages import build_page, search_pages

    rng = random.Random(204)
    alphabet = (list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
                + list(" .,;:/_-\\\"'()[]{}*+?^$|\t\n") + list("ΑΣΑσςΟΔΟΣİıßẞÉéÅå😀Ǆǅǆﬁ"))
    words = ["E4127", "src/contexto/mcp.py", "HTTP_TIMEOUT_S", "Timeout", "flaky test", "ΟΔΟΣ", "ΟΔΟΣΑ",
             "İstanbul", "straße", "a.b*c", "(x|y)", "$HOME", "\"quoted\"", "C:\\temp\\x"]

    def blob():
        parts = [rng.choice(words) if rng.random() < 0.4 else "".join(rng.choice(alphabet)
                 for _ in range(rng.randint(1, 15))) for _ in range(rng.randint(1, 12))]
        return rng.choice([" ", "\n", "", "  "]).join(parts)

    checked = 0
    for _ in range(25):
        pages = []
        for t in range(rng.randint(5, 30)):
            turn = [{"role": "user", "content": blob()},
                    {"role": "assistant", "content": [
                        {"type": "text", "text": blob()},
                        {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": blob(), "n": 3}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": blob()}]}]
            pages.append(build_page(b"k" * 32, turn, t))
        for _ in range(80):
            source = rng.choice([h for p in pages for h in _texts(p["messages"]) if h])
            start = rng.randrange(len(source))
            query = source[start:start + rng.randint(1, 20)]
            if not query.strip():
                continue  # no words: no hits by design (D34)
            old = {p["id"] for p in pages if _old_rule(p, query)}
            assert old <= set(search_pages(pages, query)), query
            checked += 1
    assert checked > 1500


@pytest.mark.parametrize("query", [" ", "  \n", "\t"])
def test_a_query_with_no_words_finds_nothing(store, query):
    store.page_out([page("a b c\n  \n\td")], 1)
    assert store.search(query) == []
    assert search(store, query) == mcp.NO_HIT


def test_a_long_query_stops_reading_pages_once_no_page_can_match():
    """Pages are narrowed word by word: once no page holds every word so far, the rest are not read."""
    reads = [0]

    class Text(bytes):
        def __contains__(self, item):
            reads[0] += 1
            return bytes.__contains__(self, item)

    entries = [SearchEntry(f"pg_{i}", i, Text(f"word{i:03d} common filler".encode()), False) for i in range(200)]
    assert rank(entries, "absent " + " ".join(f"word{i:03d}" for i in range(120))) == []
    assert reads[0] == 200  # the first word, once per page
    reads[0] = 0
    assert rank(entries, "common word007 " + " ".join(f"other{i}" for i in range(100))) == []
    assert reads[0] == 200 + 200 + 1  # common (every page), word007 (every page), other0 (one page)


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
    assert detailed(reply) == [80, 73, 63]  # the newest match (turn 80) takes the third place
    more = listed(reply)
    assert len(more) == 50 and more == sorted(more, reverse=True)
    assert {53, 43, 3} <= set(more)  # the other pages about the key are among the next best
    assert reply.splitlines()[-1] == "[27 more matching pages not shown; use a longer or more specific query]"


def test_the_recency_prior_is_bounded():
    """Relevance and recency combine as relevance x (1 + RECENCY_WEIGHT x position among the matches)."""
    assert RECENCY_WEIGHT == 0.5
    texts = {"old": "k " * 8 + "x " * 200, "new": "k " + "x " * 207}  # the same length
    ranked = rank([entry("old", 1, texts["old"]), entry("new", 2, texts["new"])], "k")
    assert ranked == ["old", "new"]  # 8 mentions beat 1 even at the far end of the session
    ranked = rank([entry("old", 1, "k k " + "x " * 206), entry("new", 2, "k " + "x " * 207)], "k")
    assert ranked == ["new", "old"]  # 2 mentions against 1 do not


def entry(page_id, turn, text, context=False):
    return SearchEntry(page_id, turn, search_key(fold(text)), context)


def test_the_newest_matching_page_is_always_shown_in_detail(store):
    """A long newest turn that states the current value loses to short older ones on relevance (BM25
    discounts long pages), but the newest matching page always takes one of the three detailed places,
    so the current value is shown (D28; after reviewer-2's repro for PR #204)."""
    output = "\n".join(f"{i:04d} INFO worker {i % 7} processed batch {i} in {i % 13} ms" for i in range(900))
    older = ["Set max_retries to 3 in the client config.", "note: max_retries is read once at startup",
             "max_retries applies per request", "the max_retries default stays in config.toml"]
    ids = [store.page_out([page(text)], t)[0] for t, text in enumerate(older, 1)]
    newest = [{"role": "user", "content": "run the soak test and raise the retry limit"},
              {"role": "assistant", "content": [{"type": "tool_use", "id": "t5", "name": "Bash",
                                                 "input": {"command": "./soak.sh"}}]},
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t5", "content": output}]},
              {"role": "assistant", "content": [{"type": "text", "text": "Soak test passed. max_retries is now 7"}]}]
    [current] = store.page_out([newest], 5)
    for t in range(6, 9):
        store.page_out([page("ran the linter, no findings")], t)
    found = store.search("max_retries")
    assert found.index(current) == 2 and set(found[:2]) <= set(ids)  # third: BM25 alone ranks it last
    reply = search(store, "max_retries")
    assert detailed(reply)[0] == 5 and "max_retries is now 7" in reply


def test_the_newest_page_takes_the_third_place_only_when_it_is_not_already_shown():
    texts = {1: "k k k k", 2: "k k k k", 3: "k k k k", 4: "y", 5: "k " + "x " * 400}
    ranked = rank([entry(f"t{t}", t, text) for t, text in texts.items()], "k")
    assert ranked == ["t3", "t2", "t5", "t1"]
    texts[5] = "k k k k k"
    ranked = rank([entry(f"t{t}", t, text) for t, text in texts.items()], "k")
    assert ranked[0] == "t5" and ranked[1:3] == ["t3", "t2"]  # already first: nothing moves


# Paged context blocks (PR #203: each paged <system-reminder> block is its own page)


def rem(body):
    """A context block as Claude Code 4.x ids send it in a turn-start message (PR #203's tests)."""
    return {"type": "text", "text": f"<system-reminder>\n{body}\n</system-reminder>\n"}


def file_note(t, path="/tmp/synthetic/project/out/f{t}.txt"):
    return (f"Note: {path.format(t=t)} changed on disk since you last read it. That's usually deliberate, "
            "so take it as the current state.\n" + f"{t}\tsynthetic changed line HTTP_TIMEOUT_S = 30\n" * 12)


def countdown(t):
    return f"<total_tokens>{15_000_000 - 37 * t} tokens left</total_tokens>"


def block_page(*bodies):
    """A paged context block as PR #203 stores it: a user message holding the block, as the CLI sent it."""
    return [{"role": "user", "content": [rem(body) for body in bodies]}]


def test_context_pages_are_recognised_by_their_content_whatever_their_stub():
    from contexto.rewrite import CONTEXT_TAG
    from contexto.store.pages import CONTEXT_TAG as PAGES_TAG, build_page, is_context_page

    assert PAGES_TAG == CONTEXT_TAG
    key = b"k" * 32
    note = build_page(key, block_page(file_note(34)), 34)
    assert note["stub"]["user_text"].startswith("<system-reminder>")  # today's stub: the raw block
    marked = {**note, "stub": {**note["stub"], "user_text": "", "system_text": "Note: /tmp/synthetic changed"}}
    tagged_system = build_page(key, [{"role": "system", "content": rem("Plan mode is active.")["text"]}], 9)
    plain_system = build_page(key, [{"role": "system", "content": "Plan mode is active. Do not edit."}], 9)
    string_block = build_page(key, [{"role": "user", "content": rem(countdown(3))["text"]}], 3)
    for context in (note, marked, tagged_system, plain_system, string_block):
        assert is_context_page(context), context["stub"]
    turn_start = [{"role": "user", "content": [rem(countdown(5)), {"type": "text", "text": "fix the build"}]},
                  {"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
    for real in (page("the deploy config changed"), turn_start, [{"role": "user", "content": "hi"}],
                 [{"role": "user", "content": [rem("x"), {"type": "tool_result", "tool_use_id": "t", "content": "y"}]}]):
        assert not is_context_page(build_page(key, real, 1)), real


def test_paged_context_blocks_rank_below_every_real_turn_page(store):
    """Reviewer-2's #203 case: file-changed notes quote the query words more often than the turn that
    says the state changed, and are shorter. They stay searchable, ranked after every real turn page."""
    ids, notes = {}, set()
    for t in range(1, 36):
        if t in (29, 32, 34):
            notes.add(store.page_out([block_page(file_note(t, "src/contexto/mcp.py"))], t)[0])
            notes.add(store.page_out([block_page(countdown(t))], t)[0])
        elif t == 35:
            ids[t] = store.page_out([page(FILLER + "\nThe deploy config changed; the current state is green.\n"
                                          "Fixed the timeout in src/contexto/mcp.py: HTTP_TIMEOUT_S is now 45")], t)[0]
        elif t == 20:
            ids[t] = store.page_out([page(FILLER + "\nsrc/contexto/mcp.py reads HTTP_TIMEOUT_S = 30, changed "
                                          "only by hand; current state unknown")], t)[0]
        else:
            ids[t] = store.page_out([page(FILLER)], t)[0]
    for query in ("changed", "current state", "src/contexto/mcp.py", "HTTP_TIMEOUT_S"):
        found = store.search(query)
        assert found[:2] == [ids[35], ids[20]], query
        assert set(found[2:]) <= notes and len(found) >= 5, query
        assert detailed(search(store, query))[0] == 35, query
    found = store.search("take it as the current state")  # only the notes hold it: still found
    assert len(found) == 3 and set(found) <= notes
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    reply = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "contexto_recall", "arguments": {"page_id": found[0]}}}))
    assert "changed on disk since you last read it" in reply["result"]["content"][0]["text"]


def test_the_newest_page_shown_in_detail_is_a_real_turn_page_when_one_matches(store):
    real = store.page_out([page("current state: current state: current state") for _ in range(4)], 1)
    [newest_real] = store.page_out([page(FILLER + "\n" + FILLER + "\nthe current state is green")], 5)
    blocks = store.page_out([block_page(file_note(t)) for t in range(6, 12)], 6)
    found = store.search("current state")
    assert newest_real in found[:3] and set(found[:3]) <= {newest_real, *real}
    assert set(found[5:]) == set(blocks)


# The reply


def test_the_reply_says_how_its_detailed_pages_were_chosen(store):
    store.page_out([page(FILLER + f"\nneedle {t}") for t in range(5)], 1)
    lines = search(store, "needle").split("\n")
    assert lines[0] == mcp.SEARCH_BEST_HEADER == "[best matching pages by relevance, shown newest first:]"
    assert re.match(r"pg_[0-9a-f]{16} \(turn 5\)$", lines[1])


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
    assert lines[2] == mcp.SEARCH_BEST_HEADER and lines[3].endswith("(turn 1)")


def test_a_word_on_every_line_of_a_large_in_view_message_is_still_fast(store):
    import time

    store.set_window(200_000, 2.0)
    text = "\n".join(f"    self.value_{i} = self.compute({i})" for i in range(20_000))  # about 700 KB
    store.set_retained(1, 0, [{"turn": 9, "message_index": 0, "role": "tool_result", "text": text}])
    store.set_turn_room("r", 10_000)
    started = time.perf_counter()
    lines = search(store, "self").split("\n")
    assert time.perf_counter() - started < 0.5  # 0.03 s; about 4 s before ffb635f
    assert lines[1] == "> in view (turn 9):     self.value_19999 = self.compute(19999)"
    assert lines[4] == "  [19997 more matching lines in view]"
