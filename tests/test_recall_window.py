"""Window-aware recall size (M2 B9, contract PR #53)."""

import json
import subprocess
import sys
import threading

import pytest

from contexto import cli, mcp
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.render import DEFAULT_RECALL_LIMIT, RECALL_LIMIT, recall_chunk, recall_limit, render_page


def test_limit_is_a_quarter_of_the_window_at_the_clamped_ratio():
    assert recall_limit(33_000, 1.2)[0] == 9_900  # 33,000 / 4 * 1.2
    assert recall_limit(33_000, 1.8)[0] == 12_375  # the measured 1.8 is clamped to 1.5
    assert recall_limit(33_000, 1.5)[0] == 12_375
    limit, note = recall_limit(33_000, 1.2)
    assert note == "chunks are 9,900 characters, about a quarter of this session's window budget"


def test_limit_never_exceeds_32000_characters():
    limit, note = recall_limit(1_000_000, 3.0)
    assert limit == RECALL_LIMIT and note == "chunks are at most 32,000 characters"


@pytest.mark.parametrize("w, r", [(None, None), (None, 2.0), (33_000, None), (0, 1.5)])
def test_without_a_recorded_window_the_limit_is_12000(w, r):
    limit, note = recall_limit(w, r)
    assert limit == DEFAULT_RECALL_LIMIT == 12_000
    assert "12,000-character default" in note and "not recorded yet" in note


def test_offsets_return_the_exact_remainder():
    page = {"turn": 1, "messages": [{"role": "user", "content": "y" * 25_000}]}
    limit, _ = recall_limit(33_000, 1.2)
    text, offset, parts = render_page(page), 0, []
    while offset is not None:
        part = recall_chunk(page, offset, limit)
        assert len(part["text"]) <= limit
        parts.append(part["text"])
        offset = part["next_offset"]
    assert "".join(parts) == text and len(parts) == -(-len(text) // limit)


# store side


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")


def test_window_round_trips_and_validates(store):
    assert store.window() == (None, None)
    store.set_window(33_000, 1.8)
    assert store.window() == (33_000, 1.8)
    for bad in [(0, 1.5), (-1, 1.5), (True, 1.5), (33_000.0, 1.5), (33_000, 0), (33_000, -1.0),
                (33_000, float("inf")), (33_000, "2")]:
        with pytest.raises(ValueError):
            store.set_window(*bad)
    assert store.window() == (33_000, 1.8)


def test_set_window_keeps_workdir_and_the_file_stays_private(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.set_workdir(tmp_path)
    store.set_window(33_000, 1.8)
    meta = json.loads((store.dir / "meta.json").read_text())
    assert meta == {"workdir": str(tmp_path.resolve()), "window_tokens": 33_000, "bytes_per_token": 1.8}
    assert (store.dir / "meta.json").stat().st_mode & 0o777 == 0o600


def test_malformed_window_metadata_reads_as_unknown(tmp_path):
    store = SessionStore(tmp_path, "s1")
    (store.dir / "meta.json").write_text(json.dumps({"window_tokens": "big", "bytes_per_token": -2}))
    assert store.window() == (None, None)


WRITER = """
import sys
from pathlib import Path
from contexto.store import SessionStore
store = SessionStore(Path(sys.argv[1]), "s1")
for i in range(150):
    if sys.argv[2] == "window":
        store.set_window(30_000 + i, 1.5)
    else:
        store.set_workdir(Path(sys.argv[1]) / f"w{i}")
"""


def test_concurrent_writers_keep_both_fields_and_readers_never_see_a_partial_file(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.set_window(1, 1.5)
    stop, bad = threading.Event(), []

    def read():
        while not stop.is_set():
            try:
                json.loads((store.dir / "meta.json").read_text())
            except ValueError as e:
                bad.append(e)

    reader = threading.Thread(target=read)
    reader.start()
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(tmp_path), kind]) for kind in ("window", "workdir")]
    for p in procs:
        assert p.wait(timeout=60) == 0
    stop.set()
    reader.join()
    assert bad == []
    meta = json.loads((store.dir / "meta.json").read_text())
    assert meta["window_tokens"] == 30_149 and meta["workdir"].endswith("w149")


# readers: the MCP tool and the CLI


def call_recall(store, page_id, offset=0):
    server = mcp.Server(store, __import__("io").StringIO(), __import__("io").StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_recall", "arguments": {"page_id": page_id, "offset": offset}}})
    return server.handle_line(line)["result"]


def test_mcp_recall_uses_the_recorded_window():
    store = MemoryStore("s1")
    [pid] = store.page_out([[{"role": "user", "content": "z" * 30_000}]], 1)
    store.set_window(33_000, 1.2)
    result = call_recall(store, pid)
    assert len(result["content"][0]["text"]) == 9_900
    assert result["structuredContent"]["next_offset"] == 9_900
    assert result["content"][1]["text"].endswith(
        "offset 9900; chunks are 9,900 characters, about a quarter of this session's window budget]")


def test_mcp_recall_falls_back_to_12000_and_says_so():
    store = MemoryStore("s1")
    [pid] = store.page_out([[{"role": "user", "content": "z" * 30_000}]], 1)
    result = call_recall(store, pid)
    assert len(result["content"][0]["text"]) == 12_000
    assert "12,000-character default because this session's window budget is not recorded yet" in \
        result["content"][1]["text"]


def test_mcp_recall_without_a_window_method_still_works():
    class Legacy:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            if name == "window":
                raise AttributeError(name)
            return getattr(self.inner, name)

    inner = MemoryStore("s1")
    [pid] = inner.page_out([[{"role": "user", "content": "z" * 20_000}]], 1)
    assert len(call_recall(Legacy(inner), pid)["content"][0]["text"]) == 12_000


def test_cli_recall_uses_the_recorded_window(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    store = SessionStore(tmp_path, "s1")
    [pid] = store.page_out([[{"role": "user", "content": "q" * 30_000}]], 1)
    store.set_window(33_000, 1.2)
    assert cli.main(["recall", pid]) == 0
    out, err = capsys.readouterr()
    assert len(out.rstrip("\n")) == 9_900
    assert "rerun with --offset 9900; chunks are 9,900 characters" in err
