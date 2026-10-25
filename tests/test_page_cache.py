"""The per-process page cache behind search, the index, and page ids (D34, ``contexto.store.catalog``)."""

import json
import os
from pathlib import Path

import pytest

from contexto.store import PageConflictError, SessionStore
from contexto.store.pages import render_index

FILLER = "\n".join(f"step {i}: ran the build and checked the output, nothing unusual" for i in range(40))


def page(text, prompt="continue"):
    return [{"role": "user", "content": prompt},
            {"role": "assistant", "content": [{"type": "text", "text": text}]}]


class Reads:
    """Counts reads of page files."""

    def __init__(self, monkeypatch):
        self.paths: list[str] = []
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name.startswith("pg_"):
                self.paths.append(path.name)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)

    def take(self):
        out, self.paths[:] = list(self.paths), []
        return out


def test_search_reads_each_page_once_per_process(tmp_path, monkeypatch):
    writer = SessionStore(tmp_path, "s1")  # the proxy
    writer.page_out([page(FILLER + f"\nneedle {i}") for i in range(5)], 1)
    server = SessionStore(tmp_path, "s1")  # the MCP server, another process
    reads = Reads(monkeypatch)
    assert len(server.search("needle")) == 5 and len(reads.take()) == 5
    assert len(server.search("needle")) == 5 and reads.take() == []
    [new] = writer.page_out([page(FILLER + "\nneedle 6")], 6)
    reads.take()
    assert server.search("needle")[0] == new and reads.take() == [f"{new}.json"]  # only the new page


def test_search_drops_quarantined_pages_and_rereads_a_changed_file(tmp_path):
    store = SessionStore(tmp_path, "s1")
    a, b = store.page_out([page(FILLER + "\nneedle a"), page(FILLER + "\nneedle b")], 1)
    assert set(store.search("needle")) == {a, b}
    store.quarantine_page(b)
    assert store.search("needle") == [a]
    path = store.pages_dir / f"{a}.json"
    record = json.loads(path.read_text())
    record["messages"][1]["content"][0]["text"] += " and a haystack"
    path.write_text(json.dumps(record))  # not something Contexto does: pages are immutable
    assert store.search("haystack") == [a]


def test_an_unreadable_page_is_logged_once_until_its_file_changes_and_skipped(tmp_path):
    """Logged once per file, not at every index build and search: each request builds the index, so one
    damaged page added a line per request (reviewers on PR #228). Changed, it is read and logged again."""
    store = SessionStore(tmp_path, "s1")
    [a] = store.page_out([page("needle")], 1)
    bad = store.pages_dir / "pg_00000000000000ff.json"
    bad.write_text("{not json")

    def logged() -> list[str]:
        log = [json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
        return [e["path"] for e in log if e["event"] == "page_unreadable"]

    assert store.search("needle") == [a] and store.search("needle") == [a]
    assert a in store.index_text(400) and store.page_ids() == [a]
    assert logged() == ["pg_00000000000000ff.json"]
    bad.write_text("{still not json")  # another size: the file changed
    assert store.search("needle") == [a] and store.index_text(400)
    assert logged() == ["pg_00000000000000ff.json"] * 2
    bad.unlink()
    assert store.search("needle") == [a] and logged() == ["pg_00000000000000ff.json"] * 2


@pytest.mark.parametrize("stub", ["not an object", None])
def test_repaging_a_stored_page_with_a_malformed_stub_is_a_page_conflict(tmp_path, stub):
    """A stored page with the same content but a tampered stub is no page record (rule 18). Re-paging
    it used to return its id (reviewer-1 on PR #204), and the page then stayed out of the index and
    search for good. A conflict lets the engine quarantine the file and page the turn afresh (rule 14;
    reviewers on PR #228)."""
    store = SessionStore(tmp_path, "s1")
    [a] = store.page_out([page("needle")], 1)
    path = store.pages_dir / f"{a}.json"
    record = json.loads(path.read_text())
    record["stub"] = stub
    path.write_text(json.dumps(record))
    fresh = SessionStore(tmp_path, "s1")
    with pytest.raises(PageConflictError) as e:
        fresh.page_out([page("needle")], 1)
    assert e.value.page_id == a and fresh.search("needle") == []
    fresh.quarantine_page(a)
    assert fresh.page_out([page("needle")], 1) == [a] and fresh.search("needle") == [a]


@pytest.mark.parametrize("variant", ["default", "legacy"])
def test_index_text_reads_no_page_the_proxy_wrote_and_matches_the_full_render(tmp_path, monkeypatch, variant):
    store = SessionStore(tmp_path, "s1")
    for t in range(60):
        store.page_out([page(FILLER + f'\ncfg = {{"key_{t}": 1}}', prompt=f"task {t}")], t)
    pages = [store.get_page(p) for p in store.page_ids()]
    expected = render_index(pages, 400, salient=variant == "default")
    reads = Reads(monkeypatch)
    assert store.index_text(400, variant) == expected and "older pages omitted" in expected
    measured = expected.count("\n") + 1  # the lines shown, and the first that did not fit
    assert len(reads.take()) == (measured if variant == "default" else 0)  # read for their identifiers only
    assert store.index_text(400, variant) == expected and reads.take() == []
    resumed = SessionStore(tmp_path, "s1")  # a restarted proxy reads each page once, the shown ones once more
    assert resumed.index_text(400, variant) == expected
    assert len(reads.take()) == 60 + (measured if variant == "default" else 0)
    assert resumed.index_text(400, variant) == expected and reads.take() == []


def test_page_ids_after_a_restart_and_a_quarantine(tmp_path):
    store = SessionStore(tmp_path, "s1")
    ids = store.page_out([page(f"turn {t}") for t in range(4)], 1)
    other = SessionStore(tmp_path, "s1")
    assert other.page_ids() == ids[::-1]
    store.quarantine_page(ids[1])
    assert other.page_ids() == [ids[3], ids[2], ids[0]]
    assert sorted(os.listdir(store.pages_dir)) == sorted(f"{p}.json" for p in (ids[0], ids[2], ids[3]))


def deep_record(record: dict, depth: int = 20_000) -> str:
    """The page record with one message nested ``depth`` levels deeper: past MAX_JSON_DEPTH, and past
    what json.loads parses on 3.12 (about 10,000 levels)."""
    return json.dumps({**record, "messages": []})[:-1] + ', "extra": ' + "[" * depth + "]" * depth + "}"


def test_a_page_file_nested_past_the_limit_is_logged_unreadable_and_skipped(tmp_path):
    store = SessionStore(tmp_path, "s1")
    a, b = store.page_out([page("needle one"), page("needle two")], 1)
    path = store.pages_dir / f"{b}.json"
    path.write_text(deep_record(json.loads(path.read_text())))  # not something Contexto writes
    fresh = SessionStore(tmp_path, "s1")
    assert fresh.search("needle") == [a]
    text = fresh.index_text(2_000)
    assert a in text and b not in text
    log = [json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
    assert {e["path"] for e in log if e["event"] == "page_unreadable"} == {f"{b}.json"}


def test_repaging_onto_a_page_file_nested_past_the_limit_is_a_page_conflict(tmp_path):
    store = SessionStore(tmp_path, "s1")
    [a] = store.page_out([page("needle")], 1)
    path = store.pages_dir / f"{a}.json"
    path.write_text(deep_record(json.loads(path.read_text())))
    with pytest.raises(PageConflictError) as caught:
        SessionStore(tmp_path, "s1").page_out([page("needle")], 1)
    assert caught.value.page_id == a and "cannot be read" in str(caught.value)
