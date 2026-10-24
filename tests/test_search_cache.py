"""The search text budget of the MCP server (D39, ``contexto.store.catalog``): bounded memory, same results."""

import json
import math
import os
import random
import tomllib
from pathlib import Path

import pytest

from contexto import cli
from contexto.codex import plan_codex
from contexto.launcher import mcp_payload
from contexto.store import SessionStore
from contexto.store.catalog import DEFAULT_TEXT_BUDGET_MB, MB, text_budget_bytes
from contexto.store.pages import search_entry
from contexto.store.search import (
    K1,
    RECENCY_WEIGHT,
    SHOWN_IN_DETAIL,
    B,
    Query,
    SearchEntry,
    rank,
    search_key,
)

VOCAB = ["alpha", "beta", "gamma", "delta", "E4127", "max_retries", "HTTP_TIMEOUT_S", "timeout", "src/app/main.py",
         "flaky", "test", "the", "ΟΔΟΣ", "straße", "a.b*c", "cache", "budget", "page", "query", "Retries"]


def page(text, prompt="continue"):
    return [{"role": "user", "content": prompt},
            {"role": "assistant", "content": [{"type": "text", "text": text}]}]


def reminder(text):
    return [{"role": "user", "content": [{"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}]}]


def random_store(tmp_path, seed=39, pages=120):
    """A store of pages of mixed sizes (a few words to a few thousand), some of them context pages."""
    rng = random.Random(seed)
    writer = SessionStore(tmp_path, "s1")
    for t in range(pages):
        words = [rng.choice(VOCAB) for _ in range(rng.choice([3, 20, 200, 2000]))]
        text = " ".join(words) + ("\n" if rng.random() < 0.5 else " ") + " ".join(rng.sample(VOCAB, 3))
        writer.page_out([reminder(text) if rng.random() < 0.1 else page(text, prompt=rng.choice(VOCAB))], t)
    return writer


def queries(seed=39):
    rng = random.Random(seed)
    out = ["alpha", "E4127", "e4127 timeout", "max_retries retries", "absent", "alpha absent", "src/app/main.py",
           "the", "flaky test", "test flaky", "ΟΔΟΣ", "STRASSE", "a.b*c", " ".join(VOCAB),
           "absent " + " ".join(VOCAB), " ".join(VOCAB) + " absent", "cache budget page query"]
    out += [" ".join(rng.sample(VOCAB, rng.randint(1, 9))) for _ in range(60)]
    return out


def reference_rank(entries, query):
    """D34 as the module docstring states it, with no narrowing and no fetch: every word on every page."""
    q = Query.parse(query)
    if q is None or not entries:
        return []
    words = [search_key(w) for w in q.words]
    phrase = search_key(q.phrase)
    matched = [e for e in entries if all(w in e.text for w in words)]
    if not matched:
        return []
    n = len(entries)
    terms = words + ([phrase] if q.multiword else [])
    df = {w: sum(w in e.text for e in entries) for w in words}
    on = {e.id: (phrase in e.text) if q.multiword else True for e in matched}
    if q.multiword:
        df[phrase] = sum(on.values())
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in terms if df[t]}
    avg = sum(len(e.text) for e in entries) / n or 1.0
    by_age = sorted(matched, key=lambda e: (e.turn, e.id))
    position = {e.id: (k / (len(by_age) - 1) if len(by_age) > 1 else 1.0) for k, e in enumerate(by_age)}
    scored = []
    for e in matched:
        norm = K1 * (1 - B + B * len(e.text) / avg)
        relevance = 0.0
        for term, weight in idf.items():
            if term == phrase and q.multiword and not on[e.id]:
                continue
            tf = e.text.count(term)
            relevance += weight * tf * (K1 + 1) / (tf + norm)
        scored.append((not e.context, on[e.id], relevance * (1 + RECENCY_WEIGHT * position[e.id]), e.turn, e.id))
    scored.sort(reverse=True)
    ranked = [s[4] for s in scored]
    real = [s for s in scored if s[0]] or scored
    newest = max(real, key=lambda s: (s[3], s[4]))[4]
    if ranked.index(newest) >= SHOWN_IN_DETAIL:
        ranked.remove(newest)
        ranked.insert(SHOWN_IN_DETAIL - 1, newest)
    return ranked


def server(tmp_path, budget):
    """The MCP server's store: another process, so its own catalog, with ``budget`` bytes of text."""
    store = SessionStore(tmp_path, "s1")
    store.set_search_cache(budget)
    return store


def test_search_results_are_the_same_under_any_budget(tmp_path):
    writer = random_store(tmp_path)
    pages = [writer.get_page(p) for p in writer.page_ids()]
    entries = [search_entry(p) for p in pages]
    total = sum(len(e.text) for e in entries)
    budgets = [4096, total // 8, total // 3, 0]
    unlimited, others = server(tmp_path, None), [server(tmp_path, b) for b in budgets]
    checked = 0
    for _ in range(2):  # the first search reads every page; the second reads only the pages not held
        for query in queries():
            expected = unlimited.search(query)
            assert expected == reference_rank(entries, query), query
            for store in others:
                assert store.search(query) == expected, (query, store._catalog.text_budget)
            checked += bool(expected)
    assert checked > 80
    assert unlimited._catalog.held_bytes == total and set(unlimited._catalog.held().values()) == {"raw"}
    for budget, store in zip(budgets, others):
        assert store._catalog.held_bytes <= budget
    assert others[-1]._catalog.held() == {}
    assert "packed" in others[1]._catalog.held().values()  # an eighth of the text holds compressed pages


def written_one_at_a_time(tmp_path, texts, budget):
    """A server that searched after each page was written, as in a session: pages arrive newest last."""
    writer = SessionStore(tmp_path, "s1")
    store = server(tmp_path, budget)
    ids = []
    for t, text in enumerate(texts):
        ids += writer.page_out([page(text)], t)
        assert store.search("needle")[0] == ids[-1]
    return writer, store, ids


def noise(seed, n=2000):
    """Text that zlib barely compresses (random letters and digits)."""
    rng = random.Random(seed)
    return "".join(rng.choices("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=n))


def test_the_budget_holds_the_newest_pages_as_they_are_and_older_ones_compressed(tmp_path):
    texts = [f"needle {t:02d} " + "the build passed and the output was clean\n" * 25 for t in range(10)]
    writer, store, ids = written_one_at_a_time(tmp_path, texts, None)
    size = len(search_entry(writer.get_page(ids[0])).text)
    store.set_search_cache(8 * size)  # 10 pages do not fit as they are: a quarter, 2 pages, stay so
    held = store._catalog.held()
    assert set(held) == set(ids)  # every page fits once the older ones are compressed
    assert [held[p] for p in ids] == ["packed"] * 8 + ["raw"] * 2  # the newest as they are
    assert store._catalog.held_bytes <= 8 * size
    assert store.search("needle 03") == [ids[3]]  # a compressed page is searched like any other
    store.set_search_cache(10 * size)  # a store that fits as it is: new pages stay as they are
    [new] = writer.page_out([page("needle 10 " + "the build passed and the output was clean\n" * 25)], 10)
    assert store.search("needle")[0] == new
    assert store._catalog.held()[new] == "raw"


def test_a_store_that_fits_the_budget_is_held_as_it_is(tmp_path):
    writer = random_store(tmp_path, pages=40)
    total = sum(len(search_entry(writer.get_page(p)).text) for p in writer.page_ids())
    store = server(tmp_path, int(1.3 * total))  # fits by an estimate from file sizes, so not to the byte
    store.search("alpha")
    assert set(store._catalog.held().values()) == {"raw"} and store._catalog.held_bytes == total


def test_when_compressed_text_does_not_fit_the_oldest_goes_first(tmp_path, monkeypatch):
    texts = [f"needle {t:02d} " + noise(t) for t in range(10)]
    writer, store, _ = written_one_at_a_time(tmp_path, texts, None)
    ids = writer.page_ids()[::-1]  # oldest first
    size = len(search_entry(writer.get_page(ids[0])).text)
    store.set_search_cache(3 * size)
    held = store._catalog.held()
    kept = [i for i, p in enumerate(ids) if p in held]
    assert kept == list(range(10 - len(kept), 10)) and len(kept) >= 3  # the newest pages
    assert set(held.values()) == {"packed"}  # random text compresses a little: more pages fit compressed
    assert store._catalog.held_bytes <= 3 * size
    reads = Reads(monkeypatch)
    store.search("needle")  # the older pages are read and not held: they would be the first to go
    assert sorted(reads.take()) == sorted(f"{p}.json" for p in ids[:10 - len(kept)])
    assert store._catalog.held() == held
    [new] = writer.page_out([page("needle 10 " + noise(10))], 10)
    assert store.search("needle")[0] == new
    held = store._catalog.held()
    assert new in held and ids[10 - len(kept)] not in held  # the newest came in, the oldest held went
    store.set_search_cache(size)
    assert set(store._catalog.held()) <= {new} and store._catalog.held_bytes <= size


def test_search_keeps_no_stub_and_the_index_still_reads_one(tmp_path):
    writer = SessionStore(tmp_path, "s1")
    ids = writer.page_out([page(f"needle {t}", prompt=f"task {t}") for t in range(5)], 0)
    store = server(tmp_path, 0)
    assert len(store.search("needle")) == 5
    assert all(e.view is None and e.search.text is None for e in store._catalog._entries.values())
    assert store.page_ids() == ids[::-1]
    assert store.index_text(400) == writer.index_text(400)
    assert len(store.search("needle")) == 5


class Reads:
    """Counts reads of page files (the catalog reads them with ``Path.read_text``)."""

    def __init__(self, monkeypatch):
        self.names: list[str] = []
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name.startswith("pg_"):
                self.names.append(path.name)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)

    def take(self):
        out, self.names[:] = list(self.names), []
        return out


def test_a_page_whose_text_is_not_held_is_read_once_per_search(tmp_path, monkeypatch):
    texts = [f"needle alpha beta tag{t} " + noise(t, 500) for t in range(8)]
    writer, store, _ = written_one_at_a_time(tmp_path, texts, None)
    ids = writer.page_ids()[::-1]
    store.set_search_cache(2 * len(search_entry(writer.get_page(ids[0])).text))
    out = [f"{p}.json" for p in ids if p not in store._catalog.held()]
    assert len(out) >= 4
    reads = Reads(monkeypatch)
    assert len(store.search("alpha")) == 8 and sorted(reads.take()) == sorted(out)
    # A short query tests every word of a page it has just read, so its frequencies need no second read.
    assert len(store.search("beta alpha tag3")) == 1 and len(reads.take()) == len(out)
    assert len(store.search("absent alpha beta")) == 0 and len(reads.take()) == len(out)
    # A long one stops at the first word a page lacks, and reads those pages again only if a page matches.
    long_query = "alpha tag3 " + " ".join(f"beta{i}" for i in range(10))
    assert store.search(long_query) == [] and len(reads.take()) == len(out)
    assert len(store.search("tag7 alpha beta alpha beta alpha beta beta")) == 1  # 3 distinct words: short
    assert len(reads.take()) == len(out)


def test_a_long_matching_query_reads_a_page_again_only_until_a_match_is_found(tmp_path, monkeypatch):
    writer = SessionStore(tmp_path, "s1")
    words = " ".join(f"w{i}" for i in range(10))
    writer.page_out([page(f"t{t} common") for t in range(6)] + [page(f"{words} common")], 0)
    store = server(tmp_path, 0)
    long_query = words + " common"
    [match] = store.search(long_query)  # learns every page
    before = [e.id for e in store._catalog.search_entries()[0]].index(match)  # pages read before the match
    reads = Reads(monkeypatch)
    assert store.search(long_query) == [match]
    assert len(reads.take()) == 7 + before  # after the match, a page is tested for every word when read


@pytest.mark.parametrize("budget", [None, 10**9, 60, 0])
def test_a_page_quarantined_during_a_search_is_left_out_whatever_the_budget(tmp_path, monkeypatch, budget):
    """Held text would still rank a page whose file went during the search, a read would not: the search
    runs again on a new listing (reviewer-1 on PR #215)."""
    import contexto.store as store_module

    writer = SessionStore(tmp_path, "s1")
    a, b = writer.page_out([page("needle a"), page("needle b")], 0)
    store = server(tmp_path, budget)
    assert set(store.search("needle")) == {a, b}  # warm: held where the budget takes it
    calls = []
    original = store_module.rank

    def rank_while_quarantining(entries, query, fetch=None):
        calls.append(len(entries))
        if len(calls) == 1:
            writer.quarantine_page(b)
        return original(entries, query, fetch)

    monkeypatch.setattr(store_module, "rank", rank_while_quarantining)
    assert store.search("needle") == [a]
    assert calls == [2, 1]


def test_rank_reads_held_text_without_fetch_and_skips_what_fetch_cannot_read():
    held = SearchEntry("pg_1", 1, b"needle one", False)
    lazy = SearchEntry("pg_2", 2, None, False)
    calls = []

    def fetch(i):
        calls.append(i)
        return SearchEntry("pg_2", 2, b"needle two", False) if i == 1 else None

    assert rank([held, lazy], "needle", fetch) == ["pg_2", "pg_1"] and calls == [1]
    assert rank([held, lazy], "needle", lambda i: None) == ["pg_1"]  # gone since the listing
    assert rank([held, lazy], "needle") == ["pg_1"]  # no fetch: a page without text is left out


@pytest.mark.parametrize("budget", [None, 10**9, 0])
@pytest.mark.parametrize("field, value", [("turn", "3"), ("turn", None), ("turn", True), ("turn", 2.5),
                                          ("id", 7)])
def test_a_page_with_a_damaged_id_or_turn_is_left_out_and_not_counted(tmp_path, budget, field, value):
    writer = SessionStore(tmp_path, "s1")
    a, b = writer.page_out([page("needle a"), page("needle b")], 0)
    path = writer.pages_dir / f"{b}.json"
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    store = server(tmp_path, budget)
    for _ in range(2):
        assert store.search("needle") == [a]
        assert store.page_ids() == [a]
        held = sum(e.size for e in store._catalog._entries.values() if e.held)
        assert store._catalog.held_bytes == held
    log = [json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
    assert {e["path"] for e in log if e["event"] == "page_unreadable"} == {f"{b}.json"}


@pytest.mark.parametrize("budget", [None, 10**9])
def test_rewritten_pages_leave_no_heap_items_behind(tmp_path, budget):
    writer = SessionStore(tmp_path, "s1")
    [a] = writer.page_out([page("needle a")], 0)
    path = writer.pages_dir / f"{a}.json"
    store = server(tmp_path, budget)
    for i in range(300):  # not something Contexto does: pages are immutable
        record = json.loads(path.read_text())
        record["messages"][1]["content"][0]["text"] = f"needle {i}"
        path.write_text(json.dumps(record))
        os.utime(path, ns=(i * 10**9, i * 10**9))
        assert store.search(f"needle {i}") == [a]
    catalog = store._catalog
    assert len(catalog._raw) + len(catalog._packed) <= 2 * 65
    assert catalog.held_bytes == sum(e.size for e in catalog._entries.values() if e.held)


# Configuration


@pytest.mark.parametrize("value, expected", [(None, 64 * MB), ("", 64 * MB), (" ", 64 * MB), ("0", 0),
                                             ("1", MB), ("0.5", MB // 2), ("256", 256 * MB), (8, 8 * MB)])
def test_text_budget_bytes(value, expected):
    assert DEFAULT_TEXT_BUDGET_MB == 64
    assert text_budget_bytes(value) == expected


@pytest.mark.parametrize("value", ["-1", "lots", "nan", "inf", "64MB"])
def test_text_budget_bytes_refuses_what_is_not_a_size(value):
    with pytest.raises(ValueError, match="CONTEXTO_SEARCH_CACHE_MB"):
        text_budget_bytes(value)


def served_budget(tmp_path, monkeypatch, argv, env=None):
    """The budget ``contexto mcp`` gives its store, from the flag or the environment."""
    seen = []
    monkeypatch.delenv("CONTEXTO_SEARCH_CACHE_MB", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("contexto.mcp.serve_stdio", lambda store: seen.append(store) or 0)
    assert cli.main(["mcp", "--session", "s1", "--home", str(tmp_path), *argv]) == 0
    return seen[0]._catalog.text_budget, seen[0]


def test_contexto_mcp_takes_the_budget_from_the_flag_then_the_environment(tmp_path, monkeypatch):
    assert served_budget(tmp_path, monkeypatch, [])[0] == 64 * MB
    assert served_budget(tmp_path, monkeypatch, [], {"CONTEXTO_SEARCH_CACHE_MB": "16"})[0] == 16 * MB
    assert served_budget(tmp_path, monkeypatch, ["--search-cache-mb", "0"],
                         {"CONTEXTO_SEARCH_CACHE_MB": "16"})[0] == 0


def test_an_invalid_environment_budget_is_logged_and_the_default_used(tmp_path, monkeypatch):
    budget, store = served_budget(tmp_path, monkeypatch, [], {"CONTEXTO_SEARCH_CACHE_MB": "lots"})
    assert budget == 64 * MB
    log = [json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
    assert [e for e in log if e["event"] == "search_cache_invalid"][0]["value"] == "lots"


def test_an_invalid_flag_is_a_usage_error(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["mcp", "--session", "s1", "--home", str(tmp_path), "--search-cache-mb", "-3"])
    assert exc.value.code == 2 and "megabytes" in capsys.readouterr().err


def test_contexto_run_passes_the_budget_to_the_mcp_server(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTEXTO_SEARCH_CACHE_MB", raising=False)
    assert mcp_payload("s", tmp_path)["mcpServers"]["contexto"]["env"] == {"CONTEXTO_HOME": str(tmp_path)}
    monkeypatch.setenv("CONTEXTO_SEARCH_CACHE_MB", "16")
    assert mcp_payload("s", tmp_path)["mcpServers"]["contexto"]["env"] == {
        "CONTEXTO_HOME": str(tmp_path), "CONTEXTO_SEARCH_CACHE_MB": "16"}


def test_contexto_run_codex_passes_the_budget_to_the_mcp_server(tmp_path):
    env = {"CONTEXTO_SEARCH_CACHE_MB": "16"}
    home = tmp_path / "ctx"
    launch = plan_codex([], "http://127.0.0.1:1", "S1", env=env, cwd=tmp_path, home=tmp_path,
                        key_helper="echo k", upstream="https://u.invalid/v1", contexto_home=home)
    try:
        config = tomllib.loads((launch.codex_home / "config.toml").read_text())
        assert config["mcp_servers"]["contexto"]["env"] == {
            "CONTEXTO_HOME": str(home), "CONTEXTO_SEARCH_CACHE_MB": "16"}
    finally:
        launch.cleanup()
    launch = plan_codex([], "http://127.0.0.1:1", "S1", auth_mode="chatgpt", env=env, cwd=tmp_path, home=tmp_path,
                        contexto_home=home)
    assert 'mcp_servers.contexto.env.CONTEXTO_SEARCH_CACHE_MB="16"' in launch.argv
    assert f'mcp_servers.contexto.env.CONTEXTO_HOME="{home}"' in launch.argv
