"""D58: the store and the MCP server stay up and say what is wrong on damaged, hostile, or odd input.

QA 2026-11-05 (store-mcp, verify-surrogate, scale-engine finding 2): lone UTF-16 surrogates, a damaged
ledger, a full disk, FIFOs and directories in a file's place, a symlinked session directory, huge
arguments echoed back, JSON-RPC shape gaps, ``open_existing`` without a clock, and turn numbers that
differ between paged and in-view search lines.
"""

from __future__ import annotations

import errno
import io
import json
import os
import subprocess
import sys
import threading

import pytest

from contexto import cli, mcp
from contexto.rewrite import Config, RewriteEngine
from contexto.store import SessionNotFoundError, SessionStore
from contexto.store.memory import MemoryStore
from contexto.tokens import estimate_tokens

LONE = "\ud83d"
TURN = [{"role": "user", "content": [{"type": "text", "text": "the staging port is garnet-4821"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "noted"}]}]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    return tmp_path


def call(server, method, params=None, msg_id=1):
    return server.handle_line(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}))


def tool(server, name, arguments):
    return call(server, "tools/call", {"name": name, "arguments": arguments})["result"]


def text_of(result):
    return result["content"][0]["text"]


def events(store, name):
    found = []
    for line in (store.dir / "log.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:  # a line cut short by a full disk
            continue
        if event.get("event") == name:
            found.append(event)
    return found


def strict_server(store):
    """A server whose stdout is strict UTF-8, as the real sys.stdout is."""
    raw = io.BytesIO()
    return mcp.Server(store, io.TextIOWrapper(raw, encoding="utf-8", write_through=True), io.StringIO()), raw


# 1. Lone surrogates: stored, hashed, counted and written back as their JSON escape


def test_a_turn_with_a_lone_surrogate_is_paged_and_recalled_exactly(home):
    store = SessionStore(home, "s1")
    turn = [{"role": "user", "content": [{"type": "text", "text": f"cut emoji {LONE} here"}]}]
    (pid,) = store.page_out([turn], 0)
    data = (store.pages_dir / f"{pid}.json").read_bytes()
    assert b"\\ud83d" in data and json.loads(data.decode("utf-8"))["messages"] == turn  # valid UTF-8 JSON
    assert store.page_out([turn], 0) == [pid]  # the same id again: stable, not a conflict
    server, raw = strict_server(store)
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_recall", "arguments": {"page_id": pid}}})
    assert server.serve([line + "\n"]) == 0
    reply = json.loads(raw.getvalue().decode("utf-8"))
    assert f"cut emoji {LONE} here" in text_of(reply["result"])  # the client reads back the same string


def test_a_pin_with_a_lone_surrogate_is_recorded_and_the_ledger_verifies(home):
    store = SessionStore(home, "s1")
    rid = store.pin("decision", f"keep {LONE} as is")
    assert store.ledger.verify() == []
    assert [r.id for r in SessionStore.open_existing(home, "s1").ledger.records()] == [rid]
    assert f"keep {LONE} as is" in store.state_text()


def test_a_logged_lone_surrogate_keeps_the_line_valid(home):
    store = SessionStore(home, "s1")
    store.log({"event": "probe", "value": "x" + LONE})
    assert events(store, "probe")[0]["value"] == "x" + LONE


def test_a_lone_surrogate_counts_as_its_escape_on_the_wire():
    assert estimate_tokens(LONE) == estimate_tokens("\\ud83d")  # six bytes, as the request carries it


def test_a_lone_surrogate_in_a_diagnostic_does_not_break_stderr(home):
    raw = io.BytesIO()
    server = mcp.Server(MemoryStore("s1"), io.StringIO(), io.TextIOWrapper(raw, encoding="utf-8", write_through=True))
    server.handlers["boom" + LONE] = lambda params: 1 / 0
    reply = call(server, "boom" + LONE)
    assert reply["error"]["code"] == mcp.INTERNAL_ERROR
    assert b"ZeroDivisionError" in raw.getvalue()


# 2. A damaged ledger, and any unexpected failure, is a tool error the model can act on


def damaged_ledger(home):
    store = SessionStore(home, "s1")
    store.pin("obligation", "ship it")
    (store.dir / "ledger.jsonl").write_bytes(b"garbage\nmore garbage\n")
    return store


@pytest.mark.parametrize(("name", "arguments", "opening"), [
    ("contexto_state", {}, "The pinned state cannot be read: "),
    ("contexto_pin", {"kind": "decision", "text": "use port 4821"}, "Not recorded: "),
    ("contexto_close", {"obligation_id": "rec_" + "0" * 24, "note": "done"}, "Not closed: "),
])
def test_a_damaged_ledger_is_a_tool_error_naming_the_cause(home, name, arguments, opening):
    store = damaged_ledger(home)
    stderr = io.StringIO()
    result = tool(mcp.Server(store, io.StringIO(), stderr), name, arguments)
    assert result["isError"] is True
    assert text_of(result) == mcp.LEDGER_DAMAGED.format(what=opening.removesuffix(": "), session="s1")
    assert "contexto sessions show s1" in text_of(result)
    assert "ledger is damaged" in stderr.getvalue()
    assert events(store, "mcp_call")[-1] == {**events(store, "mcp_call")[-1], "tool": name, "ok": False,
                                             "error": "LedgerCorruptError"}


def test_one_edited_record_refuses_pins_with_the_same_tool_error(home):
    store = SessionStore(home, "s1")
    store.pin("decision", "first")
    store.pin("decision", "second")
    path = store.dir / "ledger.jsonl"
    path.write_bytes(path.read_bytes().replace(b"first", b"FIRST", 1))
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    assert tool(server, "contexto_state", {})["isError"] is False  # the records still decode
    result = tool(server, "contexto_pin", {"kind": "decision", "text": "third"})
    assert result["isError"] is True and text_of(result).startswith("Not recorded: this session's ledger is damaged")


def test_an_unexpected_failure_in_a_tool_fails_that_call_only(home):
    store = SessionStore(home, "s1")
    store.page_out([TURN], 0)
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    store.search = lambda query: (_ for _ in ()).throw(RuntimeError("boom"))
    reply = call(server, "tools/call", {"name": "contexto_search", "arguments": {"query": "garnet"}})
    assert "error" not in reply and reply["result"]["isError"] is True
    assert text_of(reply["result"]) == mcp.TOOL_FAILED.format(tool="contexto_search", error="RuntimeError")
    assert events(store, "mcp_call")[-1]["error"] == "RuntimeError"
    assert call(server, "ping") == {"jsonrpc": "2.0", "id": 1, "result": {}}


# 3. A full disk: no partial temporary file, and log lines stay whole


def failing_write(monkeypatch, after_bytes):
    """os.write that writes ``after_bytes`` in total, then fails with ENOSPC, as a full disk does."""
    real, left = os.write, [after_bytes]

    def write(fd, data):
        if left[0] <= 0:
            raise OSError(errno.ENOSPC, "No space left on device")
        n = real(fd, bytes(data[:left[0]]))
        left[0] -= n
        return n

    monkeypatch.setattr(os, "write", write)


def temps(directory):
    return [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]


def test_a_page_write_that_fills_the_disk_leaves_no_temporary_file(home, monkeypatch):
    store = SessionStore(home, "s1")
    failing_write(monkeypatch, 10)
    with pytest.raises(OSError):
        store.page_out([TURN], 0)
    assert temps(store.pages_dir) == [] and list(store.pages_dir.glob("pg_*.json")) == []


def test_a_meta_write_that_fills_the_disk_leaves_meta_and_no_temporary_file(home, monkeypatch):
    store = SessionStore(home, "s1")
    store.set_window(100_000, 3.0)
    before = (store.dir / "meta.json").read_bytes()
    failing_write(monkeypatch, 5)
    with pytest.raises(OSError):
        store.set_window(120_000, 3.5)
    monkeypatch.undo()
    assert (store.dir / "meta.json").read_bytes() == before and temps(store.dir) == []


def test_short_writes_still_write_whole_files(home, monkeypatch):
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data[:7])))
    store = SessionStore(home, "s1")
    store.set_window(100_000, 3.0)
    (pid,) = store.page_out([TURN], 0)
    store.log({"event": "probe"})
    monkeypatch.undo()
    assert store.window() == (100_000, 3.0) and store.get_page(pid)["messages"] == TURN
    assert events(store, "probe")


def test_an_event_after_a_cut_log_line_starts_its_own_line(home, monkeypatch):
    store = SessionStore(home, "s1")
    store.log({"event": "before"})
    failing_write(monkeypatch, 12)
    with pytest.raises(OSError):
        store.log({"event": "store_error", "detail": "x" * 50})
    monkeypatch.undo()  # space is freed
    store.log({"event": "proxy_start"})
    lines = (store.dir / "log.jsonl").read_text(encoding="utf-8").split("\n")
    assert json.loads(lines[-2]) == {**json.loads(lines[-2]), "event": "proxy_start"}
    assert lines[-3].startswith('{"detail"') and not lines[-3].endswith("}")  # the cut line stands alone
    assert [e["event"] for e in events(store, "before")] == ["before"]


def test_temporary_files_of_dead_processes_are_removed_when_a_store_opens(home):
    store = SessionStore(home, "s1")
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    pid = int(dead.stdout)
    stale = store.pages_dir / f".pg_0123456789abcdef.json.{pid}.0a1b2c3d.tmp"
    stale.write_bytes(b"{")
    mine = store.pages_dir / f".pg_0123456789abcdef.json.{os.getpid()}.0a1b2c3d.tmp"
    mine.write_bytes(b"{")  # a live process's write in progress
    other = store.dir / "notes.tmp"
    other.write_bytes(b"x")
    reopened = SessionStore(home, "s1")
    assert not stale.exists() and mine.exists() and other.exists()
    assert events(reopened, "stale_temp_removed")[-1]["files"] == 1


# 4. Unknown and hostile ids, FIFOs and directories, symlinked sessions, huge echoes, JSON-RPC shapes


@pytest.mark.parametrize("page_id", ["pg_" + "a" * 300, "pg_" + "a" * 15, "pg_" + "A" * 16])
def test_a_malformed_page_id_is_unknown_not_unreadable(home, capsys, page_id):
    store = SessionStore(home, "s1")
    store.page_out([TURN], 0)
    result = tool(mcp.Server(store, io.StringIO(), io.StringIO()), "contexto_recall", {"page_id": page_id})
    assert result["isError"] is True and text_of(result).startswith("Unknown page id: ")
    code = cli.main(["recall", page_id, "--session", "s1"])
    assert code != 0 and "no page" in capsys.readouterr().err
    assert events(store, "page_unreadable") == []


def finishes(fn, timeout=10.0):
    """Run ``fn`` in a daemon thread; fail if it blocks (a FIFO opened for reading waits for a writer)."""
    outcome = {}

    def target():
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the test below
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "blocked"
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def planted(home, name, kind):
    store = SessionStore(home, "s1")
    (pid,) = store.page_out([TURN], 0)
    store.pin("obligation", "ship it")
    store.set_window(100_000, 3.0)
    store.set_retained(1, 0, [])
    path = store.pages_dir / f"{pid}.json" if name == "page" else store.dir / name
    path.unlink(missing_ok=True)
    os.mkfifo(path) if kind == "fifo" else path.mkdir()
    return pid


@pytest.mark.parametrize("kind", ["fifo", "directory"])
@pytest.mark.parametrize("name", ["meta.json", "ledger.jsonl", "log.jsonl", "retained.json", "epoch.json", "page"])
def test_a_fifo_or_directory_in_a_files_place_never_blocks_or_crashes(home, capsys, name, kind):
    pid = planted(home, name, kind)

    def serve():
        store = SessionStore(home, "s1")
        server = mcp.Server(store, io.StringIO(), io.StringIO())
        replies = [tool(server, "contexto_recall", {"page_id": pid}), tool(server, "contexto_state", {}),
                   tool(server, "contexto_search", {"query": "garnet"}),
                   tool(server, "contexto_pin", {"kind": "decision", "text": "x"}),
                   tool(server, "contexto_close", {"obligation_id": "rec_" + "0" * 24, "note": "done"})]
        store.epoch_record(), store.retained(), store.window()
        return replies

    assert all("content" in r for r in finishes(serve))
    for argv in (["state", "--session", "s1"], ["recall", pid, "--session", "s1"], ["sessions", "show", "s1"],
                 ["sessions", "list"]):
        finishes(lambda argv=argv: cli.main(argv))
    assert "Traceback" not in capsys.readouterr().err


def test_a_fifo_page_key_is_refused_at_once(home, capsys):
    store = SessionStore(home, "s1")
    (store.dir / "page.key").unlink()
    os.mkfifo(store.dir / "page.key")
    with pytest.raises(ValueError, match="not a regular file"):  # KeyFileError, which commands report
        finishes(lambda: SessionStore(home, "s1"))
    assert finishes(lambda: cli.main(["mcp", "--session", "s1"])) == 1
    assert "cannot start" in capsys.readouterr().err


def test_a_symlinked_session_directory_is_refused_by_every_command(home, tmp_path_factory, capsys):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    SessionStore(elsewhere, "real").page_out([TURN], 0)
    target = elsewhere / "sessions" / "real"
    os.chmod(target, 0o755)
    (home / "sessions").mkdir(mode=0o700)
    (home / "sessions" / "linked").symlink_to(target, target_is_directory=True)
    with pytest.raises(NotADirectoryError, match="symlink"):
        SessionStore(home, "linked")
    with pytest.raises(SessionNotFoundError, match="symlink"):
        SessionStore.open_existing(home, "linked")
    for argv in (["state", "--session", "linked"], ["recall", "pg_" + "0" * 16, "--session", "linked"],
                 ["sessions", "show", "linked"], ["mcp", "--session", "linked"]):
        assert cli.main(argv) != 0
        assert "symlink" in capsys.readouterr().err
    assert os.stat(target).st_mode & 0o777 == 0o755  # never chmodded through the link
    assert cli.main(["sessions", "list", "--json"]) == 0 and json.loads(capsys.readouterr().out) == []


def test_a_symlinked_log_is_not_followed(home, tmp_path_factory):
    store = SessionStore(home, "s1")
    outside = tmp_path_factory.mktemp("outside") / "victim"
    outside.write_text("keep\n")
    os.chmod(outside, 0o644)
    (store.dir / "log.jsonl").unlink(missing_ok=True)
    (store.dir / "log.jsonl").symlink_to(outside)
    with pytest.raises(OSError):
        store.log({"event": "probe"})
    assert outside.read_text() == "keep\n" and os.stat(outside).st_mode & 0o777 == 0o644


def test_tool_errors_do_not_echo_huge_arguments(home):
    store = SessionStore(home, "s1")
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    huge = "pg_" + "x" * 10_000_000
    results = [tool(server, "contexto_recall", {"page_id": huge}),
               tool(server, "contexto_close", {"obligation_id": "y" * 10_000_000, "note": "done"}),
               tool(server, "contexto_pin", {"kind": "decision", "text": "t",
                                             "supersedes": [f"rec_{i:024x}" for i in range(100_000)]}),
               tool(server, "contexto_state", {f"k{i}": 1 for i in range(100_000)})]
    for result in results:
        assert result["isError"] is True and len(text_of(result)) <= mcp.ERROR_TEXT_LIMIT
    assert text_of(results[0]).startswith("Unknown page id: pg_xxx") and "(10000003 characters)" in text_of(results[0])
    for method, params in (("tools/call", {"name": "n" * 1_000_000}), ("m" * 1_000_000, {})):
        assert len(call(server, method, params)["error"]["message"]) < 200


@pytest.mark.parametrize("name", [["contexto_state"], {"a": 1}, 7, None])
def test_a_tool_name_that_is_not_a_string_is_invalid_params(name):
    server = mcp.Server(MemoryStore("s1"), io.StringIO(), io.StringIO())
    assert call(server, "tools/call", {"name": name})["error"] == {"code": mcp.INVALID_PARAMS,
                                                                   "message": "Tool name must be a string"}


@pytest.mark.parametrize("msg_id", [{"a": 1}, [1], True, False])
def test_an_id_that_is_not_a_string_number_or_null_is_an_invalid_request(msg_id):
    server = mcp.Server(MemoryStore("s1"), io.StringIO(), io.StringIO())
    reply = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": "ping"}))
    assert reply["id"] is None and reply["error"]["code"] == mcp.INVALID_REQUEST


@pytest.mark.parametrize("params", [[], 0, "x", False])
def test_params_that_are_not_an_object_are_invalid_params(params):
    server = mcp.Server(MemoryStore("s1"), io.StringIO(), io.StringIO())
    reply = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": params}))
    assert reply == {"jsonrpc": "2.0", "id": 3, "error": {"code": mcp.INVALID_PARAMS,
                                                          "message": "params must be an object"}}
    ok = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping", "params": None}))
    assert ok == {"jsonrpc": "2.0", "id": 4, "result": {}}


def test_a_blank_pin_or_close_note_is_refused():
    store = MemoryStore("s1")
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    assert text_of(tool(server, "contexto_pin", {"kind": "decision", "text": "\n\n \t"})) == "text must not be blank"
    rid = store.pin("obligation", "ship it")
    assert text_of(tool(server, "contexto_close", {"obligation_id": rid, "note": "  "})) == "note must not be blank"
    assert [r.id for r in store.ledger.records()] == [rid]


@pytest.mark.parametrize(("arguments", "noted"), [
    ({"kind": "reference", "text": "always run the linter"}, True),
    ({"kind": "event", "text": "deployed", "epistemic": "hypothesis"}, True),
    ({"kind": "decision", "text": "use port 4821"}, False),
    ({"kind": "obligation", "text": "run the linter after each edit"}, False),
])
def test_a_pin_the_pinned_state_will_not_show_says_so(arguments, noted):
    store = MemoryStore("s1")
    result = tool(mcp.Server(store, io.StringIO(), io.StringIO()), "contexto_pin", arguments)
    rid = result["structuredContent"]["record_id"]
    assert result["isError"] is False and text_of(result).startswith(f"Recorded {arguments['kind']} {rid}.")
    assert ("not in the pinned state" in text_of(result)) is noted
    assert (rid in store.pinned_text(10_000)) is not noted


# 5. A store opened for reading has the rooms API too


def test_open_existing_reads_and_charges_rooms(home):
    store = SessionStore(home, "s1")
    store.set_turn_room("1", 1_000)
    store.set_side_room("side-a", 500)
    reader = SessionStore.open_existing(home, "s1")
    assert reader.side_rooms() == {"side-a": (500, 0)}
    assert reader.charge_turn_room(lambda remaining: 100) == {"limited_by": "side", "side_rooms": 1,
                                                              "limiting_age_s": 0}
    assert store.side_rooms() == {"side-a": (500, 100)} and store.turn_room() == ("1", 1_000, 100)


# 6. A turn shows the same number paged or in view


def test_a_turn_reads_the_same_number_paged_and_in_view(home):
    filler = "word " * 4_000  # about 20 KB per turn, so most turns are paged
    messages = []
    for i in range(40):
        messages += [{"role": "user", "content": [{"type": "text", "text": f"mk{i:03d}z {filler}"}]},
                     {"role": "assistant", "content": [{"type": "text", "text": f"reply {i}"}]}]
    messages.append({"role": "user", "content": [{"type": "text", "text": "next"}]})
    store = SessionStore(home, "s1")
    RewriteEngine(store, config=Config(bound=100_000))(
        {"model": "claude-opus-5-5", "max_tokens": 100, "stream": True, "messages": messages}, {}, "/v1/messages")
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    where = set()
    for i in range(40):
        store.set_turn_room("41", 10**9)  # room for every search; the stamp stays this request's
        body = text_of(tool(server, "contexto_search", {"query": f"mk{i:03d}z"}))
        paged = f"(turn {i})" in body.split("\n")[1] if body.startswith(mcp.SEARCH_BEST_HEADER) else False
        in_view = body.startswith("[in view, newest first:]") and f"> in view (turn {i}): mk{i:03d}z" in body
        assert paged or in_view, (i, body[:300])
        where.add("paged" if paged else "in view")
    assert where == {"paged", "in view"}
