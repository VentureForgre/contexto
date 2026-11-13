"""Command polish (D64): ``--json`` on ``state``, ``search`` and ``recall``, ``contexto version`` and ``-V``,
``contexto sessions`` alone, what a fresh install says, launch errors that say what to do, the MCP server's
version, and which commands take a session id prefix."""

import io
import json
import os
import subprocess
import sys
import tomllib

import pytest

import contexto
from contexto import cli, codex, launcher, mcp
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore

FIRST_RUN = "run `contexto run claude` (or `contexto run codex`) first"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    return tmp_path / "ctx"


def turn(prompt: str, answer: str = "ok") -> list[dict]:
    return [{"role": "user", "content": prompt}, {"role": "assistant", "content": [{"type": "text", "text": answer}]}]


def make(home, sid, prompts=("hello",)):
    store = SessionStore(home, sid)
    pids = store.page_out([turn(p) for p in prompts], 0) if prompts else []
    store.release()  # the live lock, held until collection otherwise
    return store, pids


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def sessions(home) -> list[str]:
    root = home / "sessions"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


# 1. --json on state, search and recall


def test_state_json_groups_the_pins_and_counts_the_pages(home, capsys):
    store, _ = make(home, "s1", prompts=("first", "second"))
    duty = store.pin("obligation", "ship the\nrelease notes", owner="user")
    old = store.pin("decision", "use sqlite", topic="storage")
    new = store.pin("decision", "use postgres", topic="storage")
    plain = store.pin("decision", "tabs are four spaces")
    fact = store.pin("event", "the build passed", epistemic="verified",
                     provenance=[{"type": "command", "locator": "make test"}])
    store.pin("event", "someone said the build was slow")  # reported, not verified: not pinned state
    code, out, err = run(capsys, "state", "--json")
    assert code == 0 and err == "session s1 (most recent)\n"  # the session line stays on stderr
    data = json.loads(out)
    assert out == json.dumps(data, indent=2, sort_keys=True) + "\n"
    assert sorted(data) == ["conflicts", "pages", "pinned", "session", "staleness_checked"]
    assert data["session"] == "s1" and data["pages"] == 2
    assert data["staleness_checked"] is False  # no working directory recorded for this session
    pinned = data["pinned"]
    assert sorted(pinned) == ["active_decisions", "open_obligations", "verified_facts"]
    assert [r["id"] for r in pinned["open_obligations"]] == [duty]
    obligation = pinned["open_obligations"][0]
    assert sorted(obligation) == ["body", "conflict", "created_at", "id", "kind", "owner", "stale", "topic"]
    assert obligation["body"] == "ship the\nrelease notes" and obligation["owner"] == "user"  # the text as pinned
    assert obligation["kind"] == "obligation" and obligation["topic"] is None and obligation["stale"] is None
    assert [r["id"] for r in pinned["active_decisions"]] == [plain, new, old]  # newest first, as the text lists them
    assert [r["conflict"] for r in pinned["active_decisions"]] == [False, True, True]
    assert [(r["id"], r["kind"]) for r in pinned["verified_facts"]] == [(fact, "event")]
    assert data["conflicts"] == [{"topic": "storage", "record_ids": [old, new]}]


def test_state_json_on_a_session_with_nothing_pinned_or_paged(home, capsys):
    make(home, "s1", prompts=())
    code, out, _ = run(capsys, "state", "--json")
    assert code == 0
    assert json.loads(out) == {"session": "s1", "pages": 0, "conflicts": [], "staleness_checked": False,
                               "pinned": {"open_obligations": [], "active_decisions": [], "verified_facts": []}}


def test_state_json_reports_staleness_when_the_working_directory_is_recorded(home, capsys, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    store = SessionStore(home, "s1")
    store.set_workdir(work)
    store.pin("decision", "keep it small")
    store.release()
    _, out, _ = run(capsys, "state", "--json")
    data = json.loads(out)
    assert data["staleness_checked"] is True and data["pinned"]["active_decisions"][0]["stale"] is False


def test_state_json_and_raw_do_not_combine(home, capsys):
    make(home, "s1")
    with pytest.raises(SystemExit) as exc:
        cli.main(["state", "--json", "--raw"])
    out = capsys.readouterr()
    assert exc.value.code == 2 and out.out == "" and "not allowed with" in out.err


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_state_json_with_unreadable_pages_is_still_json_and_exits_1(home, capsys):
    store, _ = make(home, "s1")
    store.pin("decision", "keep it small")
    store.pages_dir.chmod(0o000)
    try:
        code, out, err = run(capsys, "state", "--json")
    finally:
        store.pages_dir.chmod(0o700)
    data = json.loads(out)  # as `sessions show --json`: what could be read, then the error and exit 1
    assert code == 1 and data["pages"] is None and len(data["pinned"]["active_decisions"]) == 1
    assert "contexto: error: the pages of session s1 cannot be read" in err


def test_state_json_with_an_unreadable_ledger_prints_no_json(home, capsys):
    store, _ = make(home, "s1")
    (store.dir / "ledger.jsonl").unlink(missing_ok=True)
    (store.dir / "ledger.jsonl").mkdir()  # damage, read as such (D58)
    code, out, err = run(capsys, "state", "--json")
    assert code == 1 and out == ""
    assert "contexto: error: cannot read the ledger of session s1: ledger.jsonl is not a regular file" in err


def test_search_json_lists_hits_with_offsets_recall_accepts(home, capsys):
    _, pids = make(home, "s1", prompts=("set cache.ttl_seconds=900 for the api", "unrelated",
                                        "cache.ttl_seconds is 900 again"))
    code, out, err = run(capsys, "search", "--json", "cache.ttl_seconds", "900")
    assert code == 0 and err == "session s1 (most recent)\n"
    data = json.loads(out)
    assert out == json.dumps(data, indent=2, sort_keys=True) + "\n"
    assert sorted(data) == ["hits", "query", "session", "total"]
    assert data["session"] == "s1" and data["query"] == "cache.ttl_seconds 900" and data["total"] == 2
    assert sorted(h["page_id"] for h in data["hits"]) == sorted([pids[0], pids[2]])
    for hit in data["hits"]:
        assert sorted(hit) == ["first_match_offset", "line", "matches", "matching_lines", "page_id", "turn"]
        assert hit["line"].startswith(f"{hit['page_id']} turn {hit['turn']}: ")
        matching = [m for m in hit["matches"] if m["match"]]
        assert len(matching) == hit["matching_lines"] == 1 and "cache.ttl_seconds" in matching[0]["text"]
        assert sorted(matching[0]) == ["match", "offset", "text"]
        assert hit["first_match_offset"] == matching[0]["offset"]
        # the offset is the one `contexto recall PAGE_ID --offset N` reads on from
        _, text, _ = run(capsys, "recall", hit["page_id"], "--offset", str(matching[0]["offset"]))
        assert text.startswith(matching[0]["text"])


def test_search_json_matches_the_text_output(home, capsys):
    make(home, "s1", prompts=("alpha one", "alpha two", "alpha three"))
    _, text, _ = run(capsys, "search", "alpha")
    _, out, _ = run(capsys, "search", "--json", "alpha")
    hits = json.loads(out)["hits"]
    assert [h["line"] for h in hits] == [line for line in text.splitlines() if line.startswith("pg_")]
    shown = [f"  {'>' if m['match'] else ' '} {m['offset']}: {m['text']}" for h in hits for m in h["matches"]]
    assert shown == [line for line in text.splitlines() if line.startswith("  ")]


def test_search_json_limit_cuts_the_hits_and_total_says_how_many_matched(home, capsys):
    make(home, "s1", prompts=[f"needle number {i}" for i in range(4)])
    code, out, err = run(capsys, "search", "--json", "--limit", "1", "needle")
    data = json.loads(out)
    assert code == 0 and len(data["hits"]) == 1 and data["total"] == 4
    assert err == "session s1 (most recent)\n"  # the "more matching pages" note is `total` here


def test_search_json_with_no_hit_is_an_empty_list_and_exit_0(home, capsys):
    make(home, "s1")
    code, out, err = run(capsys, "search", "--json", "absent-word")
    assert code == 0 and json.loads(out) == {"session": "s1", "query": "absent-word", "total": 0, "hits": []}
    assert err == "session s1 (most recent)\n"


def test_search_json_counts_every_matching_line_of_a_long_page(home, capsys):
    body = "\n".join(f"line {i} mentions zebra" for i in range(6))
    _, pids = make(home, "s1", prompts=(body,))
    _, out, _ = run(capsys, "search", "--json", "zebra")
    hit = json.loads(out)["hits"][0]
    shown = [m for m in hit["matches"] if m["match"]]
    assert hit["matching_lines"] == 6 and len(shown) == 3  # the three latest, as the text output shows
    assert hit["first_match_offset"] < shown[0]["offset"]  # where `recall --offset` starts to read them all
    _, text, _ = run(capsys, "recall", pids[0], "--offset", str(hit["first_match_offset"]))
    assert text.startswith("line 0 mentions zebra")


def test_recall_json_carries_the_text_and_where_to_read_on(home, capsys):
    store, pids = make(home, "s1", prompts=("x" * 30_000,))
    _, text, _ = run(capsys, "recall", pids[0])
    code, out, err = run(capsys, "recall", "--json", pids[0])
    assert code == 0 and err == "session s1 (most recent)\n"  # no "more characters" note: next_offset says it
    data = json.loads(out)
    assert out == json.dumps(data, indent=2, sort_keys=True) + "\n"
    assert sorted(data) == ["next_offset", "offset", "page_id", "session", "text", "total_chars"]
    assert data["session"] == "s1" and data["page_id"] == pids[0] and data["offset"] == 0
    assert data["text"] == text.rstrip("\n") and 0 < data["next_offset"] < data["total_chars"]
    code, out, _ = run(capsys, "recall", "--json", pids[0], "--offset", str(data["total_chars"] - 5))
    last = json.loads(out)
    assert code == 0 and last["next_offset"] is None and len(last["text"]) == 5
    assert last["offset"] == data["total_chars"] - 5


def test_json_escapes_a_lone_surrogate_the_store_holds(home, capsys):
    store, pids = make(home, "s1", prompts=("half a pair \ud83d here",))
    store.pin("decision", "keep \ud83d as it is")
    for argv in (["recall", "--json", pids[0]], ["search", "--json", "pair"], ["state", "--json"]):
        code, out, _ = run(capsys, *argv)
        assert code == 0 and out.isascii() and "\\ud83d" in out, argv
        assert "\ud83d" in json.dumps(json.loads(out), ensure_ascii=False), argv


@pytest.mark.parametrize("argv, message", [
    (["state", "--json", "--session", "nope"], "no session 'nope'"),
    (["search", "--json", "--session", "nope", "word"], "no session 'nope'"),
    (["recall", "--json", "--session", "nope", "pg_0123456789abcdef"], "no session 'nope'"),
    (["recall", "--json", "pg_0123456789abcdef"], "no page 'pg_0123456789abcdef' in session s1"),
    (["recall", "--json", "not-a-page"], "page ids are pg_ and 16 hex digits"),
    (["state", "--json", "--session", ""], "invalid session id"),
])
def test_json_errors_go_to_stderr_with_exit_1_and_stdout_stays_empty(home, capsys, argv, message):
    make(home, "s1")
    code, out, err = run(capsys, *argv)
    assert code == 1 and out == "" and message in err and "contexto: error: " in err


def test_recall_json_refuses_an_offset_past_the_end_with_empty_stdout(home, capsys):
    _, pids = make(home, "s1")
    code, out, err = run(capsys, "recall", "--json", pids[0], "--offset", "999999")
    assert code == 1 and out == "" and "past the end of the page" in err


@pytest.mark.parametrize("argv", [["state", "--json"], ["search", "--json", "word"],
                                  ["recall", "--json", "pg_0123456789abcdef"]])
def test_json_on_a_fresh_install_is_an_error_with_empty_stdout(home, capsys, argv):
    code, out, err = run(capsys, *argv)
    assert code == 1 and out == "" and FIRST_RUN in err


@pytest.mark.parametrize("argv", [["state", "--help"], ["search", "--help"], ["recall", "--help"]])
def test_help_describes_the_json_shape(capsys, argv):
    with pytest.raises(SystemExit):
        cli.main(argv)
    text = " ".join(capsys.readouterr().out.split())
    assert "--json" in text and "session" in text
    keys = {"state": ("pinned", "open_obligations", "active_decisions", "verified_facts", "conflicts", "pages"),
            "search": ("hits", "page_id", "matches", "offset", "total"),
            "recall": ("page_id", "text", "next_offset", "total_chars")}[argv[0]]
    for key in keys:
        assert key in text, key


# 2. contexto version and -V


def test_version_command_and_short_flag_print_what_the_version_flag_prints(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    long = capsys.readouterr()
    assert exc.value.code == 0 and long.out == f"contexto {contexto.__version__}\n"
    with pytest.raises(SystemExit) as exc:
        cli.main(["-V"])
    assert exc.value.code == 0 and capsys.readouterr().out == long.out
    code, out, err = run(capsys, "version")
    assert code == 0 and out == long.out and err == ""


def test_version_is_listed_in_the_top_level_help_and_needs_no_store(home, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = capsys.readouterr().out
    assert "-V, --version" in text and "version" in text.split("commands:")[1]
    run(capsys, "version")
    assert not home.exists()


# 3. contexto sessions with no subcommand


def test_sessions_alone_lists_sessions_like_sessions_list(home, capsys):
    make(home, "s1")
    make(home, "s2", prompts=("a", "b"))
    listed = run(capsys, "sessions", "list")
    assert listed[0] == 0 and "s1" in listed[1] and "s2" in listed[1]
    assert run(capsys, "sessions") == listed
    as_json = run(capsys, "sessions", "--json")
    assert as_json == run(capsys, "sessions", "list", "--json")
    assert sorted(s["id"] for s in json.loads(as_json[1])) == ["s1", "s2"]


def test_sessions_json_before_a_subcommand_is_not_dropped(home, capsys):
    make(home, "s1")
    assert run(capsys, "sessions", "--json", "list") == run(capsys, "sessions", "list", "--json")
    assert run(capsys, "sessions", "--json", "show", "s1") == run(capsys, "sessions", "show", "s1", "--json")
    code, out, _ = run(capsys, "sessions", "--json", "prune", "--keep", "5")
    assert code == 0 and json.loads(out)["dry_run"] is True
    for argv in (["sessions", "list"], ["sessions", "show", "s1"], ["sessions", "prune", "--keep", "5"]):
        code, out, _ = run(capsys, *argv)  # and without --json the text output is unchanged
        assert code == 0 and not out.lstrip().startswith(("{", "["))


def test_sessions_with_an_unknown_subcommand_is_still_a_usage_error(home, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["sessions", "lst"])
    assert exc.value.code == 2 and "invalid choice: 'lst'" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["sessions", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "With no command, lists the sessions" in text


# 4. a fresh install says what to run first


@pytest.mark.parametrize("sessions_dir", [False, True], ids=["no home", "empty sessions directory"])
@pytest.mark.parametrize("argv", [["state"], ["state", "--raw"], ["search", "word"],
                                  ["recall", "pg_0123456789abcdef"]])
def test_readers_on_a_fresh_install_say_to_run_contexto_first(home, capsys, argv, sessions_dir):
    if sessions_dir:
        (home / "sessions").mkdir(parents=True)
    code, out, err = run(capsys, *argv)
    assert code == 1 and out == ""
    assert err == f"contexto: error: no sessions yet under {home / 'sessions'}: {FIRST_RUN}\n"
    assert sessions(home) == []  # and nothing was created


def test_sessions_list_on_a_fresh_install_says_the_same_and_exits_0(home, capsys):
    code, out, err = run(capsys, "sessions", "list")
    assert code == 0 and err == ""
    assert out == f"No sessions yet under {home / 'sessions'}: {FIRST_RUN}.\n"
    assert run(capsys, "sessions") == (code, out, err)
    assert run(capsys, "sessions", "list", "--json") == (0, "[]\n", "")
    assert not home.exists()


@pytest.mark.parametrize("argv, origin", [(["sessions", "show", "abc"], ""),
                                          (["state", "--session", "abc"], " (from --session)"),
                                          (["search", "--session", "abc", "word"], " (from --session)"),
                                          (["recall", "--session", "abc", "pg_0123456789abcdef"],
                                           " (from --session)")])
def test_a_named_session_on_a_fresh_install_says_there_are_none_yet(home, capsys, argv, origin):
    code, out, err = run(capsys, *argv)
    assert code == 1 and out == ""
    assert err == (f"contexto: error: no session 'abc'{origin}: no sessions yet under {home / 'sessions'}: "
                   f"{FIRST_RUN}\n")
    make(home, "other")  # with a session there, an unknown id is only unknown
    code, _, err = run(capsys, *argv)
    assert code == 1 and err == f"contexto: error: no session 'abc' under {home / 'sessions'}{origin}\n"


def test_contexto_session_naming_no_session_on_a_fresh_install(home, capsys, monkeypatch):
    monkeypatch.setenv("CONTEXTO_SESSION", "abc")
    code, _, err = run(capsys, "state")
    assert code == 1 and "no session 'abc' (from CONTEXTO_SESSION): no sessions yet under" in err and FIRST_RUN in err


# 5. a CLI that is not installed: how to install it, one prefix


INSTALL = {"claude": ("Install Claude Code: curl -fsSL https://claude.ai/install.sh | bash",
                      "https://code.claude.com/docs/en/setup"),
           "codex": ("Install Codex: curl -fsSL https://chatgpt.com/codex/install.sh | sh",
                     "https://learn.chatgpt.com/docs/codex/cli")}


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_cli_not_found_says_how_to_install_it(tmp_path, name):
    command, page = INSTALL[name]
    assert launcher.cli_not_found(name, path=str(tmp_path)) == (
        f"'{name}' not found on PATH. {command} (other ways: {page}). If it is installed, add its directory to PATH.")


def test_cli_not_found_for_another_program_names_no_installer(tmp_path):
    assert launcher.cli_not_found("my-claude-wrapper", path=str(tmp_path)) == "'my-claude-wrapper' not found on PATH"
    broken = tmp_path / "claude"
    broken.symlink_to(tmp_path / "gone")  # what is wrong with a file that is there needs no installer either
    assert "Install" not in launcher.cli_not_found("claude", path=str(tmp_path))


def bare_env(tmp_path, monkeypatch):
    """No claude and no codex on PATH, and nothing of the developer's read."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    for name in ("ANTHROPIC_BASE_URL", "CONTEXTO_CODEX_UPSTREAM", "CONTEXTO_CODEX_KEY_HELPER", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_run_without_the_cli_says_how_to_install_it_under_one_prefix(home, capsys, tmp_path, monkeypatch, name):
    bare_env(tmp_path, monkeypatch)
    command, page = INSTALL[name]
    code, out, err = run(capsys, "run", "--upstream", "http://127.0.0.1:9", name, "-p", "hi")
    assert code == 127 and out == ""
    assert err == (f"contexto: '{name}' not found on PATH. {command} (other ways: {page}). If it is installed, "
                   "add its directory to PATH.\n")
    assert sessions(home) == []


def test_run_claude_usage_errors_use_the_prefix_run_codex_uses(home, capsys, tmp_path, monkeypatch):
    bare_env(tmp_path, monkeypatch)
    errors = []
    for name in ("claude", "codex"):
        code, _, err = run(capsys, "run", "--bound", "0", name)
        assert code == 2
        errors.append(err)
    assert errors[0] == errors[1] == "contexto: --bound must be positive, got 0\n"


# 6. the Codex "no key helper" error: a working example, and OPENAI_API_KEY


def test_the_no_key_helper_error_gives_an_example_that_works(tmp_path):
    empty = tmp_path / "empty-home"
    empty.mkdir()
    with pytest.raises(codex.CodexLaunchError) as exc:
        codex.plan_codex([], "http://127.0.0.1:43210", "S", env={}, cwd=tmp_path, home=empty,
                         upstream="https://u.invalid/v1")
    message = str(exc.value)
    assert message.startswith("no key helper: ") and "OPENAI_API_KEY" in message
    assert "--codex-key-helper" in message and codex.KEY_HELPER_ENV in message and "apiKeyHelper" in message
    # The whole command, with the upstream: without it the example fails with "no upstream" on a default
    # setup, or sends the OpenAI key to whatever host Claude Code's base URL names (PR #257 review B).
    assert codex.KEY_HELPER_COMMAND == ("contexto run --codex-upstream https://api.openai.com/v1 "
                                        f"--codex-key-helper '{codex.KEY_HELPER_EXAMPLE}' codex")
    assert f"`{codex.KEY_HELPER_COMMAND}`" in message
    # The example, taken through the code path: the config Codex reads, then the command Codex would run.
    launch = codex.plan_codex([], "http://127.0.0.1:43210", "S", env={}, cwd=tmp_path, home=empty,
                              upstream="https://u.invalid/v1", key_helper=codex.KEY_HELPER_EXAMPLE)
    try:
        auth = tomllib.loads((launch.codex_home / "config.toml").read_text())["model_providers"]["contexto"]["auth"]
        assert auth == {"command": "/bin/sh", "args": ["-c", codex.KEY_HELPER_EXAMPLE]}
        env = {"PATH": os.defpath, "OPENAI_API_KEY": "sk-synthetic-NOT-A-KEY"}
        out = subprocess.run([auth["command"], *auth["args"]], capture_output=True, text=True, timeout=10, env=env)
        assert out.returncode == 0 and out.stdout == "sk-synthetic-NOT-A-KEY\n"
        unset = subprocess.run([auth["command"], *auth["args"]], capture_output=True, text=True, timeout=10,
                               env={"PATH": os.defpath})
        assert unset.returncode != 0 and unset.stdout == ""  # no key: the helper fails, it prints no empty key
    finally:
        launch.cleanup()


def test_the_example_parses_as_a_contexto_run_command(capsys):
    """The example's words, as a shell would split them, are a `run codex` command line."""
    import shlex

    words = shlex.split(codex.KEY_HELPER_COMMAND)
    args = cli.build_parser().parse_args(words[1:])
    assert args.cli == "codex" and args.codex_key_helper == codex.KEY_HELPER_EXAMPLE and args.cli_args == []
    assert args.codex_upstream == "https://api.openai.com/v1"


def codex_stub(tmp_path, monkeypatch):
    """A `codex` that answers --version and exits 0, first on PATH; ``bare_env`` made its directory."""
    stub = tmp_path / "empty-bin" / "codex"
    stub.write_text('#!/bin/sh\n[ "$1" = --version ] && echo "codex-cli 0.166.0"\nexit 0\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.defpath}")


def test_run_codex_without_a_key_helper_prints_the_example(home, capsys, tmp_path, monkeypatch):
    bare_env(tmp_path, monkeypatch)
    codex_stub(tmp_path, monkeypatch)
    code, out, err = run(capsys, "run", "--upstream", "http://127.0.0.1:9", "codex", "exec", "hi")
    assert code == 2 and out == ""
    assert err.startswith("contexto: no key helper: ") and f"`{codex.KEY_HELPER_COMMAND}`" in err
    assert sessions(home) == []


@pytest.mark.parametrize("claude_base_url", [None, "https://gateway.invalid"])
def test_the_example_typed_as_shown_launches_and_sends_the_key_only_to_openai(home, capsys, tmp_path, monkeypatch,
                                                                             claude_base_url):
    """PR #257 review B: on a default setup the example without an upstream ended in "no upstream"; with
    Claude Code's base URL set to another host it started Codex with that host as the upstream, which then
    got the OpenAI key. The stub codex sends nothing, so no request leaves the machine."""
    import shlex

    bare_env(tmp_path, monkeypatch)
    codex_stub(tmp_path, monkeypatch)
    if claude_base_url:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", claude_base_url)
    code, _, err = run(capsys, *shlex.split(codex.KEY_HELPER_COMMAND)[1:])
    assert code == 0 and "no upstream" not in err and "no key helper" not in err
    assert "upstream codex-flag" in err  # the flag's URL, not Claude Code's upstream plus /v1
    [session] = sessions(home)
    log = (home / "sessions" / session / "log.jsonl").read_text()
    [launch] = [e for e in map(json.loads, log.splitlines()) if e.get("event") == "launch"]
    assert launch["upstream_source"] == "codex-flag"


def test_the_untrusted_helper_form_of_the_error_gives_the_example_too(tmp_path):
    """PR #257 review A: a project's apiKeyHelper skipped in an untrusted folder is the other way to reach
    "no key helper", and it named neither the example nor OPENAI_API_KEY."""
    repo, user_home = tmp_path / "repo", tmp_path / "user-home"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({"apiKeyHelper": "echo sk-from-the-project"}))
    user_home.mkdir()
    with pytest.raises(codex.CodexLaunchError) as exc:
        codex.plan_codex([], "http://127.0.0.1:43210", "S", env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cc")},
                         cwd=repo, home=user_home, upstream="https://u.invalid/v1")
    message = str(exc.value)
    assert message.startswith("no key helper: ignored apiKeyHelper from .claude/settings.json")
    assert f"`{codex.KEY_HELPER_COMMAND}`" in message and "$OPENAI_API_KEY" in message
    assert "sk-from-the-project" not in message  # the skipped helper's command is never quoted


def test_the_example_helper_prints_no_key_text_when_the_variable_is_empty():
    """PR #257 review B: set but empty, `printenv` exits 0 with an empty line, so the helper hands Codex an
    empty key (Codex 0.167.2 then refuses the token and sends no Authorization header). Pinned here so the
    comment on KEY_HELPER_EXAMPLE stays true to what the command does."""
    out = subprocess.run(["/bin/sh", "-c", codex.KEY_HELPER_EXAMPLE], capture_output=True, text=True, timeout=10,
                         env={"PATH": os.defpath, "OPENAI_API_KEY": ""})
    assert out.returncode == 0 and out.stdout == "\n"


def test_the_key_helper_help_names_the_example(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    assert codex.KEY_HELPER_EXAMPLE in " ".join(capsys.readouterr().out.split())


# 7. the MCP server reports the package version


def test_mcp_server_info_carries_the_package_version():
    server = mcp.Server(MemoryStore("s1"), io.StringIO(), io.StringIO())
    legacy = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                            "params": {"protocolVersion": "2025-06-18"}}))
    assert legacy["result"]["serverInfo"] == {"name": "contexto", "version": contexto.__version__}
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    modern = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "server/discover",
                                            "params": {"_meta": meta}}))
    assert modern["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["version"] == contexto.__version__
    assert mcp.SERVER_VERSION == contexto.__version__ != "0.1.0"


def test_contexto_mcp_reports_the_version_contexto_version_prints(home):
    make(home, "s1")
    env = {**os.environ, "CONTEXTO_HOME": str(home)}
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}}}) + "\n"
    served = subprocess.run([sys.executable, "-m", "contexto", "mcp", "--session", "s1"], input=request,
                            capture_output=True, text=True, env=env, timeout=60)
    printed = subprocess.run([sys.executable, "-m", "contexto", "version"], capture_output=True, text=True,
                             env=env, timeout=60)
    version = json.loads(served.stdout.splitlines()[0])["result"]["serverInfo"]["version"]
    assert printed.returncode == 0 and printed.stdout == f"contexto {version}\n"


# 8. which commands take a session id prefix


def test_readers_take_a_unique_prefix_and_writers_need_the_full_id(home, capsys, monkeypatch):
    """The table: the read-only commands resolve a unique prefix; a command that creates a session, serves
    one to a CLI, or deletes one refuses the prefix and names the session it starts."""
    _, pids = make(home, "52385048ed0d")
    make(home, "abc123")
    for argv in (["state", "--session", "5238"], ["search", "--session", "5238", "hello"],
                 ["recall", "--session", "5238", pids[0]], ["sessions", "show", "5238"],
                 ["state", "--json", "--session", "5238"], ["search", "--json", "--session", "5238", "hello"],
                 ["recall", "--json", "--session", "5238", pids[0]]):
        code, out, err = run(capsys, *argv)
        assert code == 0 and "52385048ed0d" in out + err, argv
    start = "--session '5238' is not a session but the start of session 52385048ed0d"
    code, _, err = run(capsys, "sessions", "prune", "--session", "5238", "--yes")
    assert code == 2 and f"{start}; prune needs the full id" in err
    code, _, err = run(capsys, "proxy", "--session", "5238", "--upstream", "http://127.0.0.1:9")
    assert code == 2 and f"contexto proxy: {start}; give the full id to resume one" in err
    bin_dir = home.parent / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):  # never run: the launch is refused before the CLI starts
        (bin_dir / name).write_text('#!/bin/sh\n[ "$1" = --version ] && echo "0.166.0"\nexit 99\n')
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.defpath}")
    monkeypatch.setenv("HOME", str(home.parent))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home.parent / "cc"))
    monkeypatch.chdir(home.parent)
    for cli_args in (["claude", "-p", "hi"], ["--codex-auth", "chatgpt", "codex", "exec", "hi"]):
        code, _, err = run(capsys, "run", "--upstream", "http://127.0.0.1:9", "--session", "5238", *cli_args)
        assert code == 2 and f"contexto: {start}; give the full id to resume one" in err, cli_args
    assert sessions(home) == ["52385048ed0d", "abc123"]  # nothing deleted, nothing created


def test_mcp_names_the_session_a_prefix_starts(home, capsys, monkeypatch):
    monkeypatch.setattr(mcp, "serve_stdio", lambda store: pytest.fail("served a session nobody named"))
    make(home, "52385048ed0d")
    make(home, "abc123")
    make(home, "abd456")
    code, out, err = run(capsys, "mcp", "--session", "5238")
    assert code == 1 and out == ""
    assert err == (f"contexto mcp: cannot start: no session '5238' under {home / 'sessions'} (it is the start of "
                   "session 52385048ed0d; mcp needs the full id)\n")
    code, _, err = run(capsys, "mcp", "--session", "ab")
    assert code == 1 and "(it is the start of 2 sessions: abc123, abd456; mcp needs the full id)" in err
    code, _, err = run(capsys, "mcp", "--session", "typo-id")
    assert code == 1 and err == f"contexto mcp: cannot start: no session 'typo-id' under {home / 'sessions'}\n"
    assert sessions(home) == ["52385048ed0d", "abc123", "abd456"]


@pytest.mark.parametrize("argv, words", [
    (["run", "--help"], "full session id to resume or create; a prefix of an existing id is refused"),
    (["proxy", "--help"], "full session id to resume or create; a prefix of an existing id is refused"),
    (["mcp", "--help"], "the full id of an existing session"),
    (["sessions", "prune", "--help"], "its full id; a prefix is refused, since prune deletes"),
    (["state", "--help"], "session id, or a unique prefix of one"),
    (["search", "--help"], "session id, or a unique prefix of one"),
    (["recall", "--help"], "session id, or a unique prefix of one"),
    (["sessions", "show", "--help"], "session id, or a unique prefix of one"),
])
def test_help_says_whether_a_session_id_may_be_a_prefix(capsys, argv, words):
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert words in " ".join(capsys.readouterr().out.split())


# Review follow-ups (PR #257)


def test_a_sessions_path_that_is_a_file_is_not_a_fresh_install(home, capsys):
    """Review A: with `sessions` a regular file, `contexto run` cannot create a session either, so the
    first-run hint would send the user in a circle. Every reader names the file instead."""
    home.mkdir()
    (home / "sessions").write_text("not a directory")
    for argv in (["state"], ["search", "hello"], ["recall", "pg_0001"], ["sessions"], ["sessions", "list", "--json"],
                 ["sessions", "show", "abc"], ["state", "--json"]):
        code, out, err = run(capsys, *argv)
        assert code == 1 and out == "", argv
        assert err == f"contexto: error: {home / 'sessions'} is not a directory (check $CONTEXTO_HOME)\n", argv
        assert FIRST_RUN not in err, argv


def test_a_session_id_that_cannot_be_printed_is_quoted_in_the_prefix_errors(home, capsys, monkeypatch):
    """Review B: an id holding a newline forged a second `contexto: error:` line in the messages that name
    the sessions a prefix starts (`mcp`, `run`, `prune`, and the readers' ambiguous-prefix error)."""
    from contexto.store.sessions import named

    forged = "zz\ncontexto: error: forged"
    assert named([forged]) == f"session {forged!r}" and "\n" not in named([forged, "zz1"])
    assert named(["abc"]) == "session abc" and named(["abc", "abd"]) == "2 sessions: abc, abd"  # unchanged
    monkeypatch.setattr(mcp, "serve_stdio", lambda store: pytest.fail("served a session nobody named"))
    (home / "sessions" / forged).mkdir(parents=True)
    code, out, err = run(capsys, "mcp", "--session", "zz")
    assert code == 1 and out == "" and err.count("\n") == 1 and repr(forged) in err


def test_sessions_usage_shows_the_command_as_optional(capsys):
    with pytest.raises(SystemExit):
        cli.main(["sessions", "--help"])
    usage = " ".join(capsys.readouterr().out.split())
    assert "[list|show|prune]" in usage and "<list|show|prune>" not in usage


def test_the_json_help_says_what_the_output_can_hold(capsys):
    """Review A and B: a long line's `text` is an excerpt, and `pages` can be null."""
    with pytest.raises(SystemExit):
        cli.main(["search", "--help"])
    assert "for a long line text is an excerpt and offset is where the excerpt starts" in " ".join(
        capsys.readouterr().out.split())
    with pytest.raises(SystemExit):
        cli.main(["state", "--help"])
    assert "null, with the error on stderr and exit 1, when they cannot be listed" in " ".join(
        capsys.readouterr().out.split())
