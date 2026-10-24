"""The search text budget of the MCP server (D39, ``contexto.store.catalog``): bounded memory, same results."""

import json
import math
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
from contexto.store.search import B, K1, RECENCY_WEIGHT, SHOWN_IN_DETAIL, Query, SearchEntry, rank, search_key

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


def test_search_results_are_the_same_under_a_tiny_budget_and_no_budget(tmp_path):
    writer = random_store(tmp_path)
    pages = [writer.get_page(p) for p in writer.page_ids()]
    entries = [search_entry(p) for p in pages]
    total = sum(len(e.text) for e in entries)
    unlimited, tiny, none = server(tmp_path, None), server(tmp_path, 4096), server(tmp_path, 0)
    checked = 0
    for _ in range(2):  # the first search reads every page; the second reads only the pages not held
        for query in queries():
            expected = unlimited.search(query)
            assert expected == reference_rank(entries, query), query
            assert tiny.search(query) == expected, query
            assert none.search(query) == expected, query
            checked += bool(expected)
    assert checked > 80
    assert unlimited._catalog.held_bytes == total
    assert 0 < tiny._catalog.held_bytes <= 4096 and none._catalog.held_bytes == 0


def test_the_budget_holds_the_newest_pages_text(tmp_path):
    writer = SessionStore(tmp_path, "s1")
    ids = writer.page_out([page(f"needle {t:02d} " + "x" * 1000) for t in range(10)], 0)
    size = len(search_entry(writer.get_page(ids[0])).text)
    store = server(tmp_path, 3 * size + 10)
    assert store.search("needle")[0] == ids[-1]
    held = {e.id for e in store._catalog.search_entries()[0] if e.text is not None}
    assert held == set(ids[-3:]) and store._catalog.held_bytes == 3 * size
    [new] = writer.page_out([page("needle 10 " + "x" * 1000)], 10)  # the same size
    store.search("needle")
    held = {e.id for e in store._catalog.search_entries()[0] if e.text is not None}
    assert held == {ids[-2], ids[-1], new}  # the oldest went
    store.set_search_cache(size)
    held = {e.id for e in store._catalog.search_entries()[0] if e.text is not None}
    assert held == {new} and store._catalog.held_bytes == size


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
    writer = SessionStore(tmp_path, "s1")
    ids = writer.page_out([page(f"alpha beta {t}") for t in range(8)], 0)
    size = len(search_entry(writer.get_page(ids[0])).text)
    store = server(tmp_path, 2 * size)
    reads = Reads(monkeypatch)
    assert len(store.search("alpha")) == 8 and len(reads.take()) == 8
    assert len(store.search("alpha")) == 8 and sorted(reads.take()) == sorted(f"{p}.json" for p in ids[:6])
    # A short query tests every word of a page it has just read, so its frequencies need no second read.
    assert len(store.search("beta alpha 3")) == 1 and len(reads.take()) == 6
    assert len(store.search("absent alpha beta")) == 0 and len(reads.take()) == 6
    # A long one stops at the first word a page lacks, and reads those pages again only if a page matches.
    long_query = "alpha 3 " + " ".join(f"beta{i}" for i in range(10))
    assert store.search(long_query) == [] and len(reads.take()) == 6
    assert len(store.search("7 alpha beta alpha beta alpha beta beta")) == 1  # 3 distinct words: short
    assert len(reads.take()) == 6


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


def test_a_page_quarantined_during_a_search_is_left_out(tmp_path):
    writer = SessionStore(tmp_path, "s1")
    a, b = writer.page_out([page("needle a"), page("needle b")], 0)
    store = server(tmp_path, 0)
    entries, fetch = store._catalog.search_entries()
    writer.quarantine_page(b)
    assert rank(entries, "needle", fetch) == [a]


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
        assert config["mcp_servers"]["contexto"]["env"] == {"CONTEXTO_HOME": str(home), "CONTEXTO_SEARCH_CACHE_MB": "16"}
    finally:
        launch.cleanup()
    launch = plan_codex([], "http://127.0.0.1:1", "S1", auth_mode="chatgpt", env=env, cwd=tmp_path, home=tmp_path,
                        contexto_home=home)
    assert 'mcp_servers.contexto.env.CONTEXTO_SEARCH_CACHE_MB="16"' in launch.argv
    assert f'mcp_servers.contexto.env.CONTEXTO_HOME="{home}"' in launch.argv
