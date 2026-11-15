"""What one project-scope search holds of the earlier sessions' pages (review B on D69, round 2, N2).

A project-scope search reads the best pages of up to 50 earlier sessions. It used to keep every one of
those page records whole until the reply was built: 50 sessions of 3 pages of 8 MB reached 1.8 GB of
RSS (poc12). Each session's shown pages are now made into what the reply shows of them (its rows, or the
command's lines) as soon as they are read, and the records are let go. Two things are tested here:

- the replies are what they were: the MCP reply, its log spans, the command's text and ``--json``, for a
  project with every kind of earlier match, equal ``fixtures/project_search_replies.json``, recorded with
  ``python tests/test_project_search_memory.py`` on code that holds the pages: first at b54c5041, before
  the change, then again on this branch rebased onto 65f33e4f (whose D63 changed search's coverage line)
  with the change reverted (``git apply -R`` of ab83f1ba's ``src/`` diff), so only that line moved;
- the peak memory of one search over many sessions of large pages stays near one session's pages.

Helpers come from ``tests/test_project_search.py``.
"""

from __future__ import annotations

import gc
import io
import json
import os
import sys
import tracemalloc
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from contexto import cli, mcp
from contexto.store import SessionStore
from test_project_search import call, events, new_session, repo, serve

FIXTURE = Path(__file__).parent / "fixtures" / "project_search_replies.json"
KEY = bytes(range(32)).hex()  # a fixed page key, so page ids are the same at every run
MB = 1 << 20

def keyed(home: Path, sid: str, root: Path, n: float) -> SessionStore:
    """A session created on day ``n`` whose page ids do not change from run to run."""
    store = new_session(home, sid, root, n)
    path = store.dir / "page.key"
    stamp = path.stat().st_mtime_ns
    path.write_text(KEY)
    os.utime(path, ns=(stamp, stamp))
    store._key_cache = None
    return store

def user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}

def answer(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}

def plain(prompt: str, reply: str = "ok") -> list[dict]:
    return [user(prompt), answer(reply)]

def reminder(text: str) -> list[dict]:
    """A paged context block: a user message made only of a <system-reminder> block."""
    return [user(f"<system-reminder>{text}</system-reminder>")]

def own_call(path: str) -> list[dict]:
    """A turn whose path is only in Contexto's own call, which the page blanks: a stub-only match."""
    return [user("look for the deploy notes"),
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1",
                                               "name": "mcp__contexto__contexto_search",
                                               "input": {"query": "deploy notes", "path": path}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "no hit"}]},
            answer("ok")]

def big(prompt: str, output: str) -> list[dict]:
    """A turn that read a large tool output."""
    return [user(prompt),
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_2", "name": "Bash",
                                               "input": {"command": "cat build.log"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": output}]},
            answer("read it")]

def project(home: Path, root: Path) -> SessionStore:
    """Four earlier sessions with every kind of match a reply shows, and the current session."""
    s1 = keyed(home, "e1aaaaaa" + "0" * 24, root, 1)
    s1.page_out([plain(f"the deploy key was amber-{n}, see the quokka notes") for n in range(5)], 0)
    s1.release()
    s2 = keyed(home, "e2bbbbbb" + "0" * 24, root, 2)
    s2.page_out([reminder("deploy key rotation is due, quokka"), plain("lunch plans"),
                 plain("rotate the deploy key to birch-22"), reminder("another deploy key reminder")], 0)
    s2.set_retained(2, 8, [{"turn": 5, "message_index": 9, "role": "user", "text": "the deploy key is walnut-9"},
                           {"turn": 5, "message_index": 10, "role": "tool_result",
                            "text": "deploy key log line\nsecond deploy key line"}])
    s2.release()
    s3 = keyed(home, "e3cccccc" + "0" * 24, root, 3)
    many = "\n".join(f"deploy key line {n}: quokka" for n in range(12))
    s3.page_out([plain(many, "the deploy key lines above"), own_call("quokka-only.md"),
                 big("read the build log", "deploy key\n" + "filler line\n" * 400)], 0)
    s3.release()
    s4 = keyed(home, "e4dddddd" + "0" * 24, root, 4)
    s4.page_out([reminder("the deploy key rotation is due"), plain("nothing about it"),
                 plain("the deploy key moved to the vault")], 0)
    s4.release()
    cur = keyed(home, "c5eeeeee" + "0" * 24, root, 5)
    cur.page_out([plain("the deploy key is now cedar-31"), plain("deploy notes for the quokka release")], 0)
    cur.set_window(200_000, 2.0)
    return cur

QUERIES = ("deploy key", "quokka", "quokka-only", "walnut-9", "birch-22", "no-such-word", "deploy kee")

def replies(home: Path, root: Path) -> dict[str, object]:
    """Every reply the project gives: MCP replies with their log fields, at no room, a small per-call
    limit and a few turn rooms; then the command's text and --json output."""
    cur = project(home, root)
    out: dict[str, object] = {}
    server = serve(cur)
    for query in QUERIES:
        result = call(server, "contexto_search", {"query": query, "scope": "project"})
        event = events(cur)[-1]
        out[f"mcp {query}"] = {"content": result["content"], "returned": event["returned"],
                               "fields": {k: v for k, v in event.items() if k.startswith("earlier_")}}
    for n, room in enumerate((200, 600, 1_500, 4_000)):
        cur.set_turn_room(f"room-{n}", room)
        result = call(server, "contexto_search", {"query": "deploy key", "scope": "project"})
        event = events(cur)[-1]
        out[f"mcp room {room}"] = {"content": result["content"], "returned": event["returned"],
                                   "charged": event.get("charged_tokens")}
    cur.release()
    old = os.environ.get("CONTEXTO_HOME")
    os.environ["CONTEXTO_HOME"] = str(home)
    try:
        for query in ("deploy key", "quokka"):
            for json_flag in ([], ["--json"]):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(["search", "--project", *json_flag, query, "--session", cur.session_id])
                out[f"cli {query} {json_flag}"] = {"code": code, "stdout": stdout.getvalue(),
                                                   "stderr": stderr.getvalue()}
    finally:
        if old is None:
            os.environ.pop("CONTEXTO_HOME", None)
        else:
            os.environ["CONTEXTO_HOME"] = old
    return out

def test_the_replies_are_byte_for_byte_what_they_were_before_pages_were_let_go(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    got = json.loads(json.dumps(replies(tmp_path / "contexto-home", repo(tmp_path))))
    expected = json.loads(FIXTURE.read_text())
    assert got.keys() == expected.keys()
    for key in expected:
        assert got[key] == expected[key], key

def test_the_fixture_shows_every_kind_of_earlier_match():
    """So the comparison above covers each kind of row an earlier session can show."""
    replies_ = json.loads(FIXTURE.read_text())
    deploy = replies_["mcp deploy key"]["content"][0]["text"]
    assert mcp.SEARCH_CONTEXT_HEADER in deploy and "> in view at its last request" in deploy
    assert "the best 3 of 5 matching pages" in deploy and "more matching lines in this page" in deploy
    assert "> stub: " in replies_["mcp quokka-only"]["content"][0]["text"]
    assert any("not shown; the recall budget" in r["content"][0]["text"]
               for k, r in replies_.items() if k.startswith("mcp room"))
    assert "[session e3cccccc" in replies_["cli deploy key []"]["stdout"]
    assert json.loads(replies_["cli deploy key ['--json']"]["stdout"])["earlier"]["sessions"]

SESSIONS, PAGES, PAGE_MB = 12, 3, 2

def test_one_project_search_holds_about_one_sessions_pages_at_a_time(tmp_path):
    """poc12 at a size CI can afford: 12 sessions of 3 pages of 2 MB, each page a match. Holding every
    shown page until the reply is built peaked at 77 MB under tracemalloc (b54c5041); one session's pages
    at a time, at 11 MB. The search cache holds no text here, so the peak is the search's own."""
    home, root = tmp_path / "contexto-home", repo(tmp_path)
    filler = ("lorem ipsum dolor sit amet " * 3 + "\n") * (PAGE_MB * MB // 82)
    for n in range(SESSIONS):
        store = new_session(home, f"e{n:03d}-zzzz", root, n)
        store.page_out([big(f"read log {k}", "maple-77 marker\n" + filler) for k in range(PAGES)], 0)
        store.release()
    del filler
    cur = new_session(home, "current-zz", root, SESSIONS + 5)
    cur.set_window(200_000, 2.0)
    cur.set_search_cache(0)
    server = serve(cur)
    gc.collect()
    tracemalloc.start()
    try:
        result = call(server, "contexto_search", {"query": "maple-77", "scope": "project"})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    text = result["content"][0]["text"]
    assert result["isError"] is False and text.count("[session e0") == SESSIONS
    total = SESSIONS * PAGES * PAGE_MB * MB
    print(f"peak {peak / MB:.1f} MB for {total / MB:.0f} MB of pages", file=sys.stderr)
    assert peak < total / 2, f"peak {peak / MB:.1f} MB"

if __name__ == "__main__":  # record the fixture
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        os.environ.pop("CONTEXTO_SESSION", None)
        recorded = replies(base / "contexto-home", repo(base))
    FIXTURE.write_text(json.dumps(recorded, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {FIXTURE}")
