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
from contexto.store.search import RECENCY_WEIGHT, Query, SearchEntry, fold, rank, search_key, search_text

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


def said(*texts, prompt="continue"):
    """A turn whose reply is ``texts``, each its own text block (a text is what search counts, D41)."""
    return [{"role": "user", "content": prompt},
            {"role": "assistant", "content": [{"type": "text", "text": t} for t in texts]}]


def test_a_page_about_the_query_outranks_newer_pages_that_mention_it_once(store):
    about = ["The rate limit is 40 requests per minute per key.", "Raised the rate limit warning threshold.",
             "rate limit headers: X-RateLimit-Remaining", "The rate limit resets every 60 seconds."]
    [target] = store.page_out([said(FILLER, *about)], 1)
    store.page_out([page(FILLER + "\nwe may hit a rate limit here") for _ in range(30)], 2)
    assert store.search("rate limit")[0] == target
    assert 1 in detailed(search(store, "rate limit"))  # today's newest-first order would bury it


def edit_turn(t):
    """The turn that sets the timeout: an Edit of the constant, then a one-line reply."""
    return [{"role": "user", "content": "raise the client timeout"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{t}", "name": "Edit", "input": {
                "file_path": "src/app/http.py", "old_string": "HTTP_TIMEOUT_S = 10",
                "new_string": f"HTTP_TIMEOUT_S = {t}"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{t}", "content": "updated"}]},
            {"role": "assistant",
             "content": [{"type": "text", "text": f"Set the HTTP client timeout to {t} seconds."}]}]


def test_the_newest_of_equally_relevant_statements_is_shown_first(store):
    """A value set three times among newer pages that mention the word in passing: the reply's first
    value page is the latest, wherever the distractors fall."""
    turns = {}
    for t in range(1, 61):
        if t in (8, 30, 45):
            turns[t] = edit_turn(t)
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
        store.page_out([said(FILLER, *["key.alpha=on"] * mentions)], t)
    reply = search(store, "key.alpha")
    assert detailed(reply) == [80, 73, 63]  # the newest match (turn 80) takes the third place
    more = listed(reply)
    assert len(more) == 50 and more == sorted(more, reverse=True)
    assert {53, 43, 3} <= set(more)  # the other pages about the key are among the next best
    assert reply.splitlines()[-1] == "[27 more matching pages not shown; use a longer or more specific query]"


def test_the_recency_prior_is_bounded():
    """Relevance and recency combine as relevance x (1 + RECENCY_WEIGHT x position among the matches)."""
    assert RECENCY_WEIGHT == 0.5
    ranked = rank([entry("old", 1, ["k"] * 8 + ["x " * 200]), entry("new", 2, ["k", "x " * 207])], "k")
    assert ranked == ["old", "new"]  # 8 texts holding it beat 1 even at the far end of the session
    ranked = rank([entry("old", 1, ["k", "k", "x " * 206]), entry("new", 2, ["k", "x " * 207])], "k")
    assert ranked == ["new", "old"]  # 2 texts against 1 do not


def entry(page_id, turn, text, context=False):
    """A search entry of one text (a string) or of several (a list), as ``search_text`` joins them."""
    return SearchEntry(page_id, turn, search_text([text] if isinstance(text, str) else text), context)


SOAK_OUTPUT = "\n".join(f"{i:04d} INFO worker {i % 7} processed batch {i} in {i % 13} ms" for i in range(900))
PASSING = ["note: max_retries is read once at startup", "max_retries applies per request",
           "the max_retries default stays in config.toml", "grep shows max_retries in two places"]


def soak_turn():
    """A long turn: 900 lines of tool output that never name the key, then the turn's own statement."""
    return [{"role": "user", "content": "run the soak test and raise the retry limit"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t10", "name": "Bash",
                                               "input": {"command": "./soak.sh"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t10", "content": SOAK_OUTPUT}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Soak test passed. max_retries is now 7"}]}]


def test_the_newest_matching_page_is_always_shown_in_detail(store):
    """Older pages that say the key in more texts outrank the newest turn, which states the current value
    once, but the newest matching page always takes one of the three detailed places, so the current
    value is shown (D28; after reviewer-2's repro for PR #204)."""
    older = [("Set max_retries to 3 in the client config.", "max_retries is read once."),
             ("note: max_retries is read once at startup", "and max_retries applies per request"),
             ("max_retries applies per request;", "max_retries=3 in config.toml"),
             ("the max_retries default stays in config.toml", "(max_retries = 3)")]
    ids = [store.page_out([said(*texts)], t)[0] for t, texts in enumerate(older, 1)]
    [current] = store.page_out([soak_turn()], 5)
    for t in range(6, 9):
        store.page_out([page("ran the linter, no findings")], t)
    found = store.search("max_retries")
    assert found.index(current) == 2 and set(found[:2]) <= set(ids)  # third: by relevance it ranks last
    reply = search(store, "max_retries")
    assert detailed(reply)[0] == 5 and "max_retries is now 7" in reply


@pytest.mark.parametrize("mentions", [(5, 6, 13), (6, 13), (6, 8, 12, 13), (13,)])
def test_a_long_turn_stating_the_current_value_beats_an_older_short_statement(store, mentions):
    """Reviewer-2's 13-turn cases (PR #204, D41): turn 4 sets max_retries to 3, the long turn 10 sets it
    to 7, and short turns mention it in passing. The current value is shown in detail and before the old
    one, wherever the passing mentions fall. Before D41 BM25 discounted long pages (b 0.75), so the soak
    output, which never names the key, buried turn 10 under the short pages (detailed 13, 6, 4 for
    mentions 6 and 13: only the old value was shown)."""
    for t in range(1, 14):
        if t == 4:
            turn = page("Set max_retries to 3 in the client config.")
        elif t == 10:
            turn = soak_turn()
        elif t in mentions:
            turn = page(PASSING[mentions.index(t) % len(PASSING)])
        else:
            turn = page("ran the linter, no findings")
        store.page_out([turn], t)
    reply = search(store, "max_retries")
    shown = detailed(reply)
    assert 10 in shown and "max_retries is now 7" in reply, shown
    assert [t for t in shown if t in (4, 10)][0] == 10, shown


def test_a_term_counts_once_per_text_and_page_length_does_not_count():
    """D41: a term's frequency is the number of the page's texts that hold it, and BM25's b is 0. Two
    pages whose texts hold the word as often score alike however long they are, so recency orders them.
    An old grep whose output repeats the word 30 times counts twice, for the command and the output, so
    the newest one-line statement outranks it (1.5 against 1.38); counted per occurrence the grep would
    win (2.12 against 1.5). A page with the word in 3 texts still outranks a newer page with it in one
    (1.57 against 1.5), and any number of texts scores at most K1 + 1 = 2.2 times one."""
    assert rank([entry("long", 1, ["retry_budget ok", SOAK_OUTPUT]), entry("short", 2, "retry_budget ok")],
                "retry_budget") == ["short", "long"]
    assert rank([entry("short", 1, "retry_budget ok"), entry("long", 2, ["retry_budget ok", SOAK_OUTPUT])],
                "retry_budget") == ["long", "short"]
    log = "\n".join(f"{i:04d} retry_budget ok" if i % 30 == 0 else f"{i:04d} INFO tick {i}" for i in range(900))
    assert rank([entry("log", 1, ["grep -rn retry_budget", log]), entry("set", 2, "retry_budget is now 5")],
                "retry_budget") == ["set", "log"]
    thrice = ["set retry_budget to 3", "retry_budget = 3", "retry_budget is 3 now"]
    assert rank([entry("old", 1, thrice), entry("set", 2, "retry_budget is now 5")], "retry_budget") == ["old", "set"]


def test_texts_holding_counts_each_text_once():
    from contexto.store.search import TEXT_SEP, texts_holding
    text = search_text(["k k k", "", "x", "K and k", "kk"])
    assert text == TEXT_SEP.join([b"k k k", b"", b"x", b"k and k", b"kk"])
    assert texts_holding(text, b"k") == 3 and texts_holding(text, b"kk") == 1 and texts_holding(text, b"y") == 0
    assert texts_holding(search_key(fold("k k\nk")), b"k") == 1  # one text: newlines do not split it


def test_a_page_entry_holds_the_texts_page_matches_reads():
    """``search_entry`` folds the page's texts one by one and joins them with ``TEXT_SEP``; with the
    separator read as a newline it is the text ``page_matches`` folds whole, including characters that
    lower to two and capital sigma at the end of a word."""
    from contexto.store.pages import search_entry, searchable_text
    from contexto.store.search import TEXT_SEP
    store = MemoryStore("s1")
    [pid] = store.page_out([[{"role": "user", "content": "İstanbul ΟΔΟΣ"},
                             {"role": "assistant", "content": [{"type": "text", "text": "ΑΣ then Σ"},
                                                               {"type": "tool_use", "id": "t", "name": "Read",
                                                                "input": {"file_path": "/tmp/ΣΑ.txt"}}]},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                                           "content": "line one\nline ΟΔΟΣ two"}]}]], 1)
    page_record = store.get_page(pid)
    found = search_entry(page_record)
    assert found.text.replace(TEXT_SEP, b"\n") == search_key(searchable_text(page_record))
    assert found.text.count(TEXT_SEP) >= 5


def test_the_newest_page_takes_the_third_place_only_when_it_is_not_already_shown():
    texts = {1: ["k"] * 4, 2: ["k"] * 4, 3: ["k"] * 4, 4: "y", 5: "k " + "x " * 400}
    ranked = rank([entry(f"t{t}", t, text) for t, text in texts.items()], "k")
    assert ranked == ["t3", "t2", "t5", "t1"]
    texts[5] = ["k"] * 5
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
    from contexto.store.pages import REMINDER_TAG, build_page, is_context_page

    assert REMINDER_TAG == CONTEXT_TAG
    key = b"k" * 32
    note = build_page(key, block_page(file_note(34)), 34)
    assert note["stub"]["user_text"] == "" and note["stub"]["reminder_text"].startswith("Note: ")  # D32's stub
    marked = {**note, "stub": {**note["stub"], "user_text": rem(file_note(34))["text"][:200]}}  # any stub
    del marked["stub"]["reminder_text"]
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


def test_codex_developer_pages_are_context_pages_and_hold_no_searchable_text(store):
    # The fallback pages a carried Codex developer item on its own (test_carried_bound). Search reads
    # Responses items through as_message, which drops system-like items (B23), so such a page never
    # matches; it is still classed as context, so it would rank after every turn page if it did.
    import test_codex as codex
    from contexto.store.pages import build_page, is_context_page

    dev = build_page(b"k" * 32, [codex.dev("synthetic standing note: HTTP_TIMEOUT_S is 30")], 3)
    assert is_context_page(dev)
    assert not is_context_page(build_page(b"k" * 32, [codex.prompt("HTTP_TIMEOUT_S"), codex.dev("note")], 3))
    store.page_out([[codex.dev("synthetic standing note: HTTP_TIMEOUT_S is 30")]], 3)
    assert store.search("HTTP_TIMEOUT_S") == []


STATED = ("The deploy config changed and the current state is green. Fixed the timeout in "
          "src/contexto/mcp.py: HTTP_TIMEOUT_S is now 45")


def engine_store(monkeypatch, shape):
    """A store paged by PR #203's RewriteEngine over 59 turns of Claude Code 2.1.302 shapes, with
    file-changed notes that quote ``HTTP_TIMEOUT_S = 30`` and turn 36 saying it is now 45. Turns are
    paged at each rotation and the context blocks paged at that rotation get the next page turns, so
    a note sent at turn 32 or 35 is numbered after turn 36's page (reviewer-2 on PR #204)."""
    import test_carried_bound as cb
    from contexto.rewrite import Config, RewriteEngine

    def note(t):
        return ("Note: /repo/src/contexto/mcp.py changed on disk since you last read it. That's usually "
                "deliberate, so take it as the current state.\n"
                + f"{t}\tHTTP_TIMEOUT_S = 30  # synthetic changed line\n" * 12)

    monkeypatch.setattr(cb, "file_note", note)
    paged = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(paged, config=Config(**cb.RESUME_CONFIG))
    msgs: list = []
    for t in range(1, 60):
        turn = shape(cb, t)
        if t == 36:
            turn[-1] = {"role": "assistant", "content": [{"type": "text", "text": f"ok 36 {cb.FILL}\n{STATED}"}]}
        start = next(i for i, m in enumerate(turn) if m["role"] == "assistant")
        msgs = msgs + turn[:start]
        engine(cb.request(msgs), {}, cb.MESSAGES)
        msgs = msgs + turn[start:]
    paged.set_window(200_000, 2.0)
    return paged


@pytest.mark.parametrize("shape", [lambda cb, t: cb.turn_4x(t), lambda cb, t: cb.turn_5x(t),
                                   lambda cb, t: cb.turn_5x(t, opus=False)], ids=["4x", "opus-5-5", "sonnet-5"])
def test_turn_pages_are_shown_before_paged_reminders_numbered_after_them(monkeypatch, shape):
    from contexto.store.pages import is_context_page

    paged = engine_store(monkeypatch, shape)
    pages = list(paged.pages.values())
    [stated] = [p for p in pages if STATED in json.dumps(p["messages"])]
    notes = [p for p in pages if is_context_page(p) and "HTTP_TIMEOUT_S = 30" in json.dumps(p["messages"])]
    assert len(notes) >= 5 and max(p["turn"] for p in notes) > stated["turn"]  # the case this guards
    turns = {p["id"]: (is_context_page(p), p["turn"]) for p in pages}
    for query in ("HTTP_TIMEOUT_S", "changed"):
        reply = search(paged, query)
        lines = reply.split("\n")
        rows = [i for i, line in enumerate(lines) if re.match(r"pg_[0-9a-f]{16} \(turn \d+\)", line)]
        # The turn that set the current value is the first page shown, above every stale note.
        assert lines[rows[0]] == f"{stated['id']} (turn {stated['turn']})", query
        assert detailed(reply)[0] == stated["turn"] and listed(reply), query
        headers = [i for i, line in enumerate(lines) if line == mcp.SEARCH_CONTEXT_HEADER]
        assert len(headers) == 2, query  # once in the detailed part, once in the list
        more = lines.index("[more matching pages, newest first; recall one at its offset:]")
        for part, (lo, hi) in zip(headers, ((0, more), (more, len(lines)))):
            assert lo < part < hi
            shown = [lines[i].split()[0] for i in rows if lo < i < hi]
            assert [turns[pid] for pid in shown] == sorted((turns[pid] for pid in shown),
                                                           key=lambda k: (not k[0], k[1]), reverse=True)
            first_context = next(i for i in rows if lo < i < hi and turns[lines[i].split()[0]][0])
            assert lines[first_context - 1] == mcp.SEARCH_CONTEXT_HEADER


def test_the_reply_has_no_context_line_when_no_context_page_is_shown(store):
    store.page_out([page(FILLER + f"\nneedle {t}") for t in range(5)], 1)
    store.page_out([block_page("needle in a reminder")], 2)
    assert mcp.SEARCH_CONTEXT_HEADER in search(store, "needle")
    assert mcp.SEARCH_CONTEXT_HEADER not in search(store, "needle 3")


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
