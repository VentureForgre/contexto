"""User-facing command fixes (D57): finding a page id, a readable ``state``, the 4xx count, id prefixes,
``--session`` edge cases, sessions a failed command leaves behind, and help text."""

import json
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

from contexto import cli
from contexto.store import SessionStore

LONG_ID = "a" * 300  # past NAME_MAX (255 bytes)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    return tmp_path


def turn(prompt: str, answer: str = "ok") -> list[dict]:
    return [{"role": "user", "content": prompt}, {"role": "assistant", "content": [{"type": "text", "text": answer}]}]


def make(home, sid, when, prompts=("hello",), pins=0):
    store = SessionStore(home, sid)
    pids = store.page_out([turn(p) for p in prompts], 0) if prompts else []
    for i in range(pins):
        store.pin("obligation", f"duty {i}")
    for p in [store.dir, *store.dir.iterdir(), *store.pages_dir.iterdir()]:
        os.utime(p, (when, when))
    store.release()  # the live lock, held until collection otherwise: prune would see the session in use
    return store, pids


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def sessions(home) -> list[str]:
    root = home / "sessions"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


# 1. finding a page id: `sessions show` lists pages, `search` finds them, `recall` takes what both print


def test_sessions_show_lists_pages_newest_first_with_ids_turns_and_first_lines(home, capsys):
    _, pids = make(home, "s1", 1_000_000, prompts=("first question", "second question\nmore", "third"))
    code, out, _ = run(capsys, "sessions", "show", "s1")
    assert code == 0
    listing = out[out.index("Pages, newest first:"):].splitlines()[1:]
    assert listing == [f"  {pids[2]} turn 2: third", f"  {pids[1]} turn 1: second question more",
                       f"  {pids[0]} turn 0: first question"]
    code, out, _ = run(capsys, "sessions", "show", "s1", "--json")
    pages = json.loads(out)["page_list"]
    assert [(p["id"], p["turn"]) for p in pages] == [(pids[2], 2), (pids[1], 1), (pids[0], 0)]
    assert pages[0]["line"] == f"{pids[2]} turn 2: third"


def test_sessions_show_lists_the_newest_pages_and_all_with_a_flag(home, capsys):
    make(home, "s1", 1_000_000, prompts=[f"question {i}" for i in range(25)])
    _, out, _ = run(capsys, "sessions", "show", "s1")
    assert out.count(" turn ") == 20
    assert "5 older pages not listed: add --all, or find one with contexto search" in out
    _, out, _ = run(capsys, "sessions", "show", "s1", "--all")
    assert out.count(" turn ") == 25 and "older pages" not in out
    _, out, _ = run(capsys, "sessions", "show", "s1", "--json")
    assert len(json.loads(out)["page_list"]) == 20
    _, out, _ = run(capsys, "sessions", "show", "s1", "--json", "--all")
    assert len(json.loads(out)["page_list"]) == 25


def test_sessions_show_with_no_pages_says_so(home, capsys):
    make(home, "s1", 1_000_000, prompts=(), pins=1)
    _, out, _ = run(capsys, "sessions", "show", "s1")
    assert "Pages, newest first: none (no turns paged out yet)" in out


def test_search_finds_pages_best_first_and_recall_accepts_the_printed_id(home, capsys):
    store, pids = make(home, "s1", 1_000_000, prompts=("set cache.ttl_seconds=900 for the api", "unrelated",
                                                      "cache.ttl_seconds is 900 again"))
    code, out, err = run(capsys, "search", "cache.ttl_seconds")
    assert code == 0 and err.strip() == "session s1 (most recent)"
    headers = [line for line in out.splitlines() if line.startswith("pg_")]
    assert {h.split()[0] for h in headers} == {pids[0], pids[2]}
    assert headers[0].split()[1:3] == ["turn", "2:"] or headers[0].split()[1:3] == ["turn", "0:"]
    assert any(line.lstrip().startswith(">") and "cache.ttl_seconds" in line for line in out.splitlines())
    # The page id and the offset of a matching line are what `recall` takes.
    match = next(line for line in out.splitlines() if line.lstrip().startswith(">"))
    offset = match.split(">", 1)[1].split(":", 1)[0].strip()
    code, recalled, _ = run(capsys, "recall", headers[0].split()[0], "--offset", offset)
    assert code == 0 and "cache.ttl_seconds" in recalled.splitlines()[0]


def test_search_with_no_match_and_its_errors(home, capsys):
    make(home, "s1", 1_000_000)
    code, out, err = run(capsys, "search", "nothing-like-this")
    assert code == 0 and out == "" and "no paged-out turn in session s1 contains every word of" in err
    assert run(capsys, "search", "x", "--session", "ghost")[0] == 1
    with pytest.raises(SystemExit) as exc:
        cli.main(["search"])
    assert exc.value.code == 2


def test_search_limit_lists_the_rest_on_stderr(home, capsys):
    make(home, "s1", 1_000_000, prompts=[f"needle {i}" for i in range(4)])
    code, out, err = run(capsys, "search", "needle", "--limit", "1")
    assert code == 0 and sum(line.startswith("pg_") for line in out.splitlines()) == 1
    assert "[3 more matching pages: rerun with --limit 4]" in err


def test_recall_rejects_a_malformed_or_overlong_page_id_cleanly(home, capsys):
    store, _ = make(home, "s1", 1_000_000)
    for bad in ("pg_" + "a" * 300, "pg_xyz", "turn-3"):
        code, out, err = run(capsys, "recall", bad)
        assert code == 1 and out == "" and "Traceback" not in err
        assert "page ids are pg_ and 16 hex digits" in err
    assert not (store.dir / "log.jsonl").exists()  # not logged as an unreadable page


# 2. a readable state view


def test_state_on_an_empty_session_is_plain_and_does_not_claim_paged_turns(home, capsys):
    make(home, "s1", 1_000_000, prompts=())
    code, out, err = run(capsys, "state", "--session", "s1")
    assert code == 0 and err.strip() == "session s1 (--session)"
    assert out.splitlines() == ["No pinned records.", "", "No turns paged out yet."]


def test_state_lists_pins_and_points_at_the_page_commands(home, capsys):
    store, _ = make(home, "s1", 1_000_000, prompts=("a", "b"), pins=2)
    store.pin("decision", "use sqlite", topic="db")
    code, out, _ = run(capsys, "state")
    assert code == 0
    assert "contexto_search" not in out and "page index below" not in out
    lines = out.splitlines()
    assert lines[0] == "Open obligations:" and lines[1].endswith("obligation, owner assistant: duty 1")
    assert "Active decisions:" in lines and lines[-1] == (
        "2 turns paged out: list them with `contexto sessions show s1`, "
        "search them with `contexto search --session s1 WORDS`.")


def test_state_raw_prints_the_model_text(home, capsys):
    store, _ = make(home, "s1", 1_000_000, pins=300)
    code, out, _ = run(capsys, "state", "--raw")
    assert code == 0 and out.rstrip("\n") == store.state_text()
    assert out.count("obligation, owner assistant") == 300


# 3. upstream 4xx counted once


def test_sessions_show_counts_each_upstream_4xx_once_and_splits_model_requests(home, capsys):
    store, _ = make(home, "s1", 1_000_000)
    for _ in range(6):  # Claude Code's keyless probe, as the proxy logs it: the request, then upstream_4xx
        store.log({"event": "request", "method": "HEAD", "path": "/api/hello", "status": 401})
        store.log({"event": "upstream_4xx", "status": 401, "path": "/api/hello", "model": None})
    store.log({"event": "request", "method": "POST", "path": "/v1/messages", "status": 429})
    store.log({"event": "upstream_4xx", "status": 429, "path": "/v1/messages", "model": "m"})
    store.log({"event": "request", "method": "POST", "path": "/v1/messages", "status": 400,
               "error": "malformed request framing"})  # answered by the proxy itself: not upstream
    _, out, _ = run(capsys, "sessions", "show", "s1", "--json")
    summary = json.loads(out)
    assert summary["upstream_4xx"] == 7 and summary["upstream_4xx_model"] == 1
    _, out, _ = run(capsys, "sessions", "show", "s1")
    assert "Upstream 4xx: 7 (1 on model requests)" in out


# 4. unique id prefixes


def test_a_unique_prefix_names_a_session_everywhere(home, capsys, monkeypatch):
    _, (pid,) = make(home, "7f3a91c2", 1_000_000)
    make(home, "b0", 2_000_000)
    code, _, err = run(capsys, "recall", pid, "--session", "7f3")
    assert code == 0 and err.strip() == "session 7f3a91c2 (--session)"
    assert run(capsys, "state", "--session", "7f")[0] == 0
    assert run(capsys, "search", "hello", "--session", "7f")[0] == 0
    code, out, _ = run(capsys, "sessions", "show", "7f3a")
    assert code == 0 and out.startswith("Session: 7f3a91c2\n")
    monkeypatch.setenv("CONTEXTO_SESSION", "7f3a")
    code, _, err = run(capsys, "state")
    assert code == 0 and err.strip() == "session 7f3a91c2 (CONTEXTO_SESSION)"
    monkeypatch.delenv("CONTEXTO_SESSION")
    code, out, _ = run(capsys, "sessions", "prune", "--session", "7f3a")
    assert code == 0 and "7f3a91c2" in out and "Dry run" in out


def test_an_ambiguous_prefix_is_an_error_naming_the_matches(home, capsys):
    make(home, "7f3a01", 1_000_000)
    make(home, "7f3a02", 1_000_000)
    for argv in (["state", "--session", "7f3a"], ["sessions", "show", "7f3a"],
                 ["sessions", "prune", "--session", "7f3a", "--yes"]):
        code, out, err = run(capsys, *argv)
        assert code == 1 and "Traceback" not in err
        assert "session id prefix '7f3a' matches 2 sessions: 7f3a01, 7f3a02; give more characters" in err
    assert sessions(home) == ["7f3a01", "7f3a02"]


def test_an_exact_id_wins_over_longer_ids_it_prefixes(home, capsys):
    make(home, "abc", 1_000_000)
    make(home, "abcd", 2_000_000)
    code, _, err = run(capsys, "state", "--session", "abc")
    assert code == 0 and err.strip() == "session abc (--session)"


# 5. --session edge cases


@pytest.mark.parametrize("argv", [["state"], ["recall", "pg_0000000000000000"], ["search", "hello"]])
def test_an_empty_session_option_is_refused_not_ignored(home, capsys, argv):
    make(home, "s1", 1_000_000)
    code, out, err = run(capsys, *argv, "--session", "")
    assert code == 1 and out == "" and "invalid session id: ''" in err


@pytest.mark.parametrize("argv", [
    ["state", "--session", LONG_ID], ["recall", "pg_0000000000000000", "--session", LONG_ID],
    ["sessions", "show", LONG_ID], ["search", "x", "--session", LONG_ID],
    ["sessions", "prune", "--session", LONG_ID], ["mcp", "--session", LONG_ID],
    ["proxy", "--session", LONG_ID, "--upstream", "http://127.0.0.1:9"],
])
def test_an_overlong_session_id_is_a_clean_error(home, capsys, argv):
    make(home, "s1", 1_000_000)
    code, out, err = run(capsys, *argv)
    assert code == 1 and out == "" and "Traceback" not in err
    assert "invalid session id" in err and "longer than 255 bytes" in err
    assert len(err) < 400  # the id is not echoed in full


@pytest.mark.parametrize("port", ["-1", "65536", "99999", "x"])
def test_proxy_port_out_of_range_is_a_usage_error(home, capsys, port):
    with pytest.raises(SystemExit) as exc:
        cli.main(["proxy", "--port", port, "--upstream", "http://127.0.0.1:9"])
    err = capsys.readouterr().err
    assert exc.value.code == 2 and "Traceback" not in err and "--port" in err
    assert sessions(home) == []


def test_sessions_list_reports_a_home_that_is_not_a_directory(tmp_path, monkeypatch, capsys):
    (tmp_path / "file").write_text("")
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "file"))
    code, out, err = run(capsys, "sessions", "list")
    assert code == 1 and out == "" and "is not a directory" in err


# 6. no junk sessions


def test_mcp_does_not_create_a_session(home, capsys, monkeypatch):
    import contexto.mcp

    monkeypatch.setattr(contexto.mcp, "serve_stdio", lambda store: pytest.fail("served a session that is absent"))
    code, out, err = run(capsys, "mcp", "--session", "typo-id")
    assert code == 1 and out == ""
    assert err.startswith("contexto mcp: cannot start: no session 'typo-id' under ")
    assert sessions(home) == []


def test_mcp_serves_an_existing_session(home, capsys, monkeypatch):
    import contexto.mcp

    make(home, "s1", 1_000_000)
    seen = []
    monkeypatch.setattr(contexto.mcp, "serve_stdio", lambda store: seen.append(store.session_id) or 0)
    assert run(capsys, "mcp", "--session", "s1")[0] == 0 and seen == ["s1"]


def test_a_proxy_that_cannot_start_leaves_no_session(home, capsys):
    code, _, err = run(capsys, "proxy", "--upstream", "not-a-url")
    assert code == 1 and "contexto proxy: cannot start:" in err
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    try:
        code, _, err = run(capsys, "proxy", "--session", "new-one", "--port", str(taken.getsockname()[1]),
                           "--upstream", "http://127.0.0.1:9")
    finally:
        taken.close()
    assert code == 1 and "contexto proxy: cannot start:" in err
    assert sessions(home) == []


def test_a_failed_proxy_keeps_a_session_that_already_existed(home, capsys):
    store, _ = make(home, "kept", 1_000_000)
    code, _, _ = run(capsys, "proxy", "--session", "kept", "--upstream", "not-a-url")
    assert code == 1 and sessions(home) == ["kept"] and len(list(store.pages_dir.iterdir())) == 1


def test_the_default_session_skips_empty_ones(home, capsys):
    _, (pid,) = make(home, "with-pages", 1_000_000)
    make(home, "pins-only", 500_000, prompts=(), pins=1)
    make(home, "empty-newest", 3_000_000, prompts=())
    code, _, err = run(capsys, "recall", pid)
    assert code == 0 and err.strip() == "session with-pages (most recent with pages or pins)"
    shutil.rmtree(home / "sessions" / "with-pages")
    code, _, err = run(capsys, "state")
    assert code == 0 and err.strip() == "session pins-only (most recent with pages or pins)"


def test_the_default_is_the_newest_session_when_all_are_empty(home, capsys):
    make(home, "old", 1_000_000, prompts=())
    make(home, "new", 2_000_000, prompts=())
    code, _, err = run(capsys, "state")
    assert code == 0 and err.strip() == "session new (most recent)"


# 7. help text


def test_help_mentions_codex_and_no_internal_ids(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    top = capsys.readouterr().out
    assert "run Claude Code or Codex" in top and "(the CLI spawns this)" in top
    for argv in (["run", "--help"], ["proxy", "--help"], ["mcp", "--help"], ["recall", "--help"],
                 ["search", "--help"], ["state", "--help"], ["sessions", "show", "--help"]):
        with pytest.raises(SystemExit):
            cli.main(argv)
        text = capsys.readouterr().out
        assert "M2" not in text and "``" not in text and "lane" not in text, argv
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    text = capsys.readouterr().out
    assert "'claude' or 'codex'" in text and "Codex" in text


def test_python_dash_m_help_runs_without_a_store(tmp_path):
    import subprocess

    proc = subprocess.run([sys.executable, "-m", "contexto", "search", "--help"], capture_output=True, text=True,
                          env={**os.environ, "CONTEXTO_HOME": str(tmp_path / "h")}, timeout=60)
    assert proc.returncode == 0 and "WORDS" in proc.stdout
    assert not Path(tmp_path / "h").exists()
