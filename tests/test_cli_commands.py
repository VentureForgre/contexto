import json
import os

import pytest

from contexto import cli
from contexto.store import SessionStore
from contexto.store.render import RECALL_LIMIT, recall_chunk, render_page


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    return tmp_path


def make(home, sid, when, prompt="hello", pins=0):
    store = SessionStore(home, sid)
    pid = store.page_out([[
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "SECRET-THOUGHT"},
                                          {"type": "text", "text": "answer"}]},
    ]], 1)[0]
    for i in range(pins):
        store.pin("obligation", f"duty {i}")
    for p in [store.dir, *store.dir.iterdir(), *store.pages_dir.iterdir()]:
        os.utime(p, (when, when))
    return store, pid


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# sessions list


def test_sessions_list_text_and_json_most_recent_first(home, capsys):
    make(home, "older", 1_000_000)
    make(home, "newer", 2_000_000, pins=2)
    code, out, _ = run(capsys, "sessions", "list")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["ID", "CREATED", "PAGES", "RECORDS", "LAST", "ACTIVITY"]
    assert [ln.split()[0] for ln in lines[1:]] == ["newer", "older"]
    code, out, _ = run(capsys, "sessions", "list", "--json")
    data = json.loads(out)
    assert [d["id"] for d in data] == ["newer", "older"]
    assert data[0]["pages"] == 1 and data[0]["records"] == 2 and data[1]["records"] == 0
    assert set(data[0]) == {"id", "created", "pages", "records", "last_activity"}


def test_sessions_list_ties_sort_by_id(home, capsys):
    for sid in ("b", "a", "c"):
        make(home, sid, 1_500_000)
    _, out, _ = run(capsys, "sessions", "list", "--json")
    assert [d["id"] for d in json.loads(out)] == ["c", "b", "a"]


def test_sessions_list_skips_symlinks_out_of_sessions(home, capsys, tmp_path_factory):
    make(home, "real", 1_000_000)
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    SessionStore(elsewhere, "foreign")
    (home / "sessions" / "linked").symlink_to(elsewhere / "sessions" / "foreign")
    _, out, _ = run(capsys, "sessions", "list", "--json")
    assert [d["id"] for d in json.loads(out)] == ["real"]


def test_sessions_list_empty_home(home, capsys):
    code, out, _ = run(capsys, "sessions", "list")
    assert code == 0 and "No sessions" in out
    assert run(capsys, "sessions", "list", "--json")[1].strip() == "[]"
    assert not (home / "sessions").exists()


# sessions show


def test_sessions_show_counts_from_store_ledger_and_log(home, capsys):
    store, _ = make(home, "s1", 1_000_000, pins=3)
    store.close(store.state().open_obligations[0].id, "done")
    store.log({"event": "over_bound", "tokens": 160_000})
    store.log({"event": "upstream_error", "status": 400, "epoch": {"k": 4}})
    store.log({"event": "upstream_error", "status": 529})
    code, out, _ = run(capsys, "sessions", "show", "s1", "--json")
    summary = json.loads(out)
    assert code == 0
    assert summary["pages"] == 1 and summary["records"] == 4 and summary["open_obligations"] == 2
    assert summary["over_bound"] == 1 and summary["upstream_4xx"] == 1 and summary["log_events"] == 3
    assert summary["last_epoch"] == {"k": 4} and summary["ledger_problems"] == []
    assert summary["events_by_type"] == {"over_bound": 1, "upstream_error": 2}
    code, out, _ = run(capsys, "sessions", "show", "s1")
    assert "Open obligations: 2" in out and "Ledger: intact" in out and 'Last epoch: {"k": 4}' in out


@pytest.mark.parametrize("bad", ["missing", "../escape", "a/b"])
def test_sessions_show_unknown_or_unsafe_id_exits_1(home, capsys, bad):
    code, out, err = run(capsys, "sessions", "show", bad)
    assert code == 1 and out == "" and err.startswith("contexto: error:")


def test_sessions_requires_a_subcommand(home, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["sessions"])
    assert exc.value.code == 2


# recall


def test_recall_renders_like_mcp_and_omits_thinking(home, capsys):
    store, pid = make(home, "s1", 1_000_000, prompt="where is the config")
    code, out, err = run(capsys, "recall", pid)
    assert code == 0
    assert out.rstrip("\n") == render_page(store.get_page(pid))
    assert "where is the config" in out and "SECRET-THOUGHT" not in out
    assert err.strip() == "session s1 (most recent)"


def test_recall_offsets_and_continuation_hint(home, capsys):
    long = "x" * (RECALL_LIMIT + 500)
    store, pid = make(home, "s1", 1_000_000, prompt=long)
    store.set_window(200_000, 2.0)  # a large window, so the 32,000-character cap applies (B9)
    code, out, err = run(capsys, "recall", pid)
    assert code == 0 and len(out.rstrip("\n")) == RECALL_LIMIT
    total = len(render_page(SessionStore(home, "s1").get_page(pid)))
    assert f"[{total - RECALL_LIMIT} more characters: rerun with --offset {RECALL_LIMIT}; " in err
    code, out, err = run(capsys, "recall", pid, "--offset", str(RECALL_LIMIT))
    assert code == 0 and "more characters" not in err
    assert len(out.rstrip("\n")) == total - RECALL_LIMIT


def test_recall_errors_exit_1(home, capsys):
    _, pid = make(home, "s1", 1_000_000)
    assert run(capsys, "recall", "pg_0000000000000000")[0] == 1
    assert run(capsys, "recall", pid, "--offset", "99999999")[0] == 1
    assert run(capsys, "recall", pid, "--offset", "-1")[0] == 1
    assert run(capsys, "recall", pid, "--session", "nope")[0] == 1


def test_session_selection_order(home, capsys, monkeypatch):
    make(home, "old", 1_000_000)
    _, new_pid = make(home, "new", 2_000_000)
    _, old_pid = make(home, "old2", 500_000)
    assert run(capsys, "recall", new_pid)[2].strip() == "session new (most recent)"
    monkeypatch.setenv("CONTEXTO_SESSION", "old2")
    code, _, err = run(capsys, "recall", old_pid)
    assert code == 0 and err.strip() == "session old2 (CONTEXTO_SESSION)"
    code, _, err = run(capsys, "recall", new_pid, "--session", "new")
    assert code == 0 and err.strip() == "session new (--session)"
    monkeypatch.setenv("CONTEXTO_SESSION", "ghost")
    code, _, err = run(capsys, "state")
    assert code == 1 and "ghost" in err and "CONTEXTO_SESSION" in err


# state


def test_state_prints_the_full_uncapped_state(home, capsys):
    store, _ = make(home, "s1", 1_000_000, pins=300)
    code, out, err = run(capsys, "state")
    assert code == 0 and err.strip() == "session s1 (most recent)"
    assert out.rstrip("\n") == store.state_text()
    assert out.count("obligation, owner assistant") == 300
    assert len(store.pinned_text(6000).splitlines()) < len(out.splitlines())  # the cap does not apply


def test_state_with_no_sessions_exits_1(home, capsys):
    code, _, err = run(capsys, "state")
    assert code == 1 and "no sessions" in err


def test_inspecting_creates_nothing(home, capsys):
    store, pid = make(home, "s1", 1_000_000)
    key = (store.dir / "page.key").read_bytes()
    before = sorted(p.relative_to(home) for p in home.rglob("*"))
    for argv in (["sessions", "list"], ["sessions", "show", "s1"], ["recall", pid], ["state"],
                 ["sessions", "show", "ghost"], ["recall", pid, "--session", "ghost"]):
        run(capsys, *argv)
    after = sorted(p.relative_to(home) for p in home.rglob("*"))
    assert after == before
    assert (store.dir / "page.key").read_bytes() == key
    assert not (home / "sessions" / "ghost").exists()


# shared rendering (moved from contexto.mcp; builder-4's malformed-input test carried over)


def test_render_page_survives_malformed_blocks():
    text = render_page({"turn": 2, "messages": [
        "not a message",
        {"role": "user", "content": 42},
        {"role": "assistant", "content": ["bare string", {"type": "text", "text": "fine"},
                                         {"type": "tool_result", "content": [7, {"type": "image"}]},
                                         {"type": "tool_result", "content": None}]},
    ]})
    assert "[malformed message omitted]" in text and "[malformed content omitted]" in text
    assert "[malformed block omitted]" in text and "fine" in text and "[image omitted]" in text


def test_recall_chunk_edges_match_the_mcp_tool():
    page = {"turn": 1, "messages": [{"role": "user", "content": "abcdef"}]}
    text = render_page(page)
    full = recall_chunk(page)
    assert full == {"text": text, "offset": 0, "next_offset": None, "total_chars": len(text)}
    assert recall_chunk(page, 0, limit=4) == {"text": text[:4], "offset": 0, "next_offset": 4,
                                              "total_chars": len(text)}
    assert recall_chunk(page, len(text)) == {"text": "", "offset": len(text), "next_offset": None,
                                             "total_chars": len(text)}
    for bad in (-1, len(text) + 1, True):
        with pytest.raises(ValueError):
            recall_chunk(page, bad)


# mcp: the home resolution (B24)


@pytest.mark.parametrize("value", ["~/x", "rel", None])
def test_mcp_resolves_contexto_home_like_the_other_subcommands(tmp_path, monkeypatch, value):
    # `contexto mcp` used the raw $CONTEXTO_HOME: '~/x' opened a literal ./~/x while `contexto sessions`
    # read the expanded path. Both now use paths.contexto_home(): expanded and absolute.
    import contexto.mcp
    from contexto import paths

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    if value is None:
        monkeypatch.delenv("CONTEXTO_HOME", raising=False)
    else:
        monkeypatch.setenv("CONTEXTO_HOME", value)
    seen = {}
    monkeypatch.setattr(contexto.mcp, "serve_stdio", lambda store: seen.setdefault("dir", store.dir) and 0)
    assert cli.main(["mcp", "--session", "s1"]) == 0
    expected = {"~/x": tmp_path / "home" / "x", "rel": tmp_path / "rel", None: tmp_path / "home" / ".contexto"}[value]
    assert seen["dir"] == expected / "sessions" / "s1"
    assert seen["dir"].is_absolute() and paths.contexto_home() == expected  # what `contexto sessions` reads
    assert not (tmp_path / "~").exists()  # no literal ./~ directory
