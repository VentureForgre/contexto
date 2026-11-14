"""Project memory at launch (D69, design section 4): ``contexto run --no-project-memory`` and
``CONTEXTO_PROJECT_MEMORY`` turn it off for both CLIs, the session records it, and the launcher says on
stderr, before the CLI starts, what the session carries from its project's earlier sessions.

The launches run in this process (``cli.main``) against stub ``claude`` and ``codex`` programs that
print one line and exit; ``capfd`` reads the launcher's lines and the stub's in the order they reached
the terminal. No request is made: the upstream is a closed port nothing connects to.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import cli, launcher, paths
from contexto.launcher import EXIT_PROXY, EXIT_USAGE, LaunchError, LaunchOptions
from contexto.store import SessionStore
from contexto.store import project as project_module
from test_launcher import FAKE_CLAUDE, Upstream
from test_project import main_pin, new_session, repo

HINT = "(list: contexto project show; off: --no-project-memory)"
STUBS = {
    "claude": '#!/bin/sh\necho "stub claude ran" >&2\nexit 0\n',
    "codex": '#!/bin/sh\ncase "$1" in\n--version) echo "codex-cli 0.166.0"; exit 0 ;;\n'
             '--help) echo "Usage: codex [OPTIONS] [PROMPT]"; exit 0 ;;\nesac\necho "stub codex ran" >&2\nexit 0\n',
}
CLI_ARGS = {
    "claude": ["--upstream", "http://127.0.0.1:9", "claude", "-p", "hi"],
    "codex": ["--codex-key-helper", "echo k", "--codex-upstream", "http://127.0.0.1:9/v1", "codex", "exec", "hi"],
}
BOTH = pytest.mark.parametrize("name", ["claude", "codex"])
ZEROS = {"earlier": 0, "carried": 0, "carried_sessions": 0, "skipped": 0, "unreadable": 0}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp ``CONTEXTO_HOME``, stub CLIs first on PATH, and nothing of the developer's Claude
    configuration or environment in reach."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in STUBS.items():
        (bin_dir / name).write_text(script)
        (bin_dir / name).chmod(0o755)
    (tmp_path / "claude-config").mkdir()
    for name in [n for n in os.environ if n.startswith(("ANTHROPIC_", "CLAUDE", "OPENRIG_", "RIGGED_", "CONTEXTO_"))]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "contexto-home"))
    project_module.forget_cache()
    return tmp_path / "contexto-home"


@pytest.fixture
def root(tmp_path, monkeypatch, home):
    """The project the launches run in: a repository, and the working directory."""
    root = repo(tmp_path)
    monkeypatch.chdir(root)
    return root


def earlier(home: Path, sid: str, root: Path, age_s: float, pins=(), main: bool = True) -> SessionStore:
    """A session of ``root`` created ``age_s`` seconds ago, released, with ``pins`` as (kind, text), made
    by the main conversation unless ``main`` is false."""
    store = SessionStore(home, sid)
    store.set_workdir(root)
    for kind, text in pins:
        if main:
            main_pin(store, kind, text)
        else:
            store.pin(kind, text)
    stamp = time.time() - age_s
    os.utime(store.dir / "page.key", (stamp, stamp))
    store.release()
    return store


def run(capfd, name: str, *options: str, session: str = "new") -> tuple[int, str]:
    """``contexto run [options] --session <session> <name> ...`` in this process: (exit code, stderr)."""
    code = cli.main(["run", "--no-version-check", "--session", session, *options, *CLI_ARGS[name]])
    return code, capfd.readouterr().err


def meta(home: Path, session: str = "new") -> dict:
    return json.loads((home / "sessions" / session / "meta.json").read_text())


def events(home: Path, session: str = "new", kind: str = "project_memory") -> list[dict]:
    lines = (home / "sessions" / session / "log.jsonl").read_text().splitlines()
    return [{k: v for k, v in e.items() if k != "ts"} for e in map(json.loads, lines) if e["event"] == kind]


def project_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("contexto: project memory:")]


def three_earlier(home: Path, root: Path) -> None:
    """Three earlier sessions of ``root``: four records the main conversation pinned in two of them, and
    one session whose only pin is not the main conversation's."""
    earlier(home, "aaaa1111", root, 300, [("obligation", "ship the notes"), ("decision", "use postgres")])
    earlier(home, "bbbb2222", root, 200, [("decision", "tabs"), ("obligation", "write the changelog")])
    earlier(home, "cccc3333", root, 100, [("decision", "a subagent's pin")], main=False)


# The variable and the option ----------------------------------------------------------------------

@pytest.mark.parametrize(("value", "expected"), [
    (None, (True, None)), ("", (True, None)), ("   ", (True, None)),
    ("0", (False, None)), ("false", (False, None)), ("off", (False, None)), ("no", (False, None)),
    ("FALSE", (False, None)), ("Off", (False, None)), ("  No\t", (False, None)), (" 0 ", (False, None)),
    ("1", (True, None)), ("true", (True, None)), ("ON", (True, None)), (" Yes ", (True, None)),
    ("maybe", (True, "maybe")), ("2", (True, "2")), ("disabled", (True, "disabled")), (" nope ", (True, "nope")),
])
def test_the_variable_turns_project_memory_off_only_for_its_four_values(value, expected):
    env = {} if value is None else {paths.PROJECT_MEMORY_ENV: value}
    assert paths.PROJECT_MEMORY_ENV == "CONTEXTO_PROJECT_MEMORY"
    assert paths.project_memory_env(env) == expected


def test_the_variable_is_read_from_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("CONTEXTO_PROJECT_MEMORY", "off")
    assert paths.project_memory_env() == (False, None)
    monkeypatch.delenv("CONTEXTO_PROJECT_MEMORY")
    assert paths.project_memory_env() == (True, None)


def test_an_unrecognised_value_gets_one_line_naming_the_accepted_ones_and_stays_on():
    err = io.StringIO()
    assert launcher.project_memory_wanted(LaunchOptions(), err, {"CONTEXTO_PROJECT_MEMORY": "maybe"}) is True
    assert err.getvalue() == ("contexto: CONTEXTO_PROJECT_MEMORY='maybe' is not a value Contexto knows and is "
                              "ignored: 0, false, off or no turns project memory off; 1, true, on, yes or unset "
                              "leaves it on\n")
    long = io.StringIO()
    assert launcher.project_memory_wanted(LaunchOptions(), long, {"CONTEXTO_PROJECT_MEMORY": "x" * 500}) is True
    assert len(long.getvalue()) < 300 and "\n" not in long.getvalue()[:-1]  # one line, however long the value


@pytest.mark.parametrize(("flag_on", "value", "wanted"), [
    (True, None, True), (True, "1", True), (True, "off", False), (False, None, False), (False, "1", False),
    (False, "off", False)])
def test_the_option_or_the_variable_turns_it_off(flag_on, value, wanted):
    err = io.StringIO()
    env = {} if value is None else {"CONTEXTO_PROJECT_MEMORY": value}
    assert launcher.project_memory_wanted(LaunchOptions(project_memory=flag_on), err, env) is wanted
    assert err.getvalue() == ""


def test_options_from_args_reads_the_option():
    base = dict(cli="claude", cli_args=[], bound=150_000, allow_large_bound=False, window_fraction=None,
                headroom_floor=None, session=None, upstream=None, observe_thinking=False)
    assert LaunchOptions().project_memory is True
    assert launcher.options_from_args(argparse.Namespace(**base)).project_memory is True
    assert launcher.options_from_args(argparse.Namespace(**base, no_project_memory=True)).project_memory is False
    parsed = cli.build_parser().parse_args(["run", "--no-project-memory", "codex", "exec"])
    assert parsed.no_project_memory is True and launcher.options_from_args(parsed).project_memory is False
    assert cli.build_parser().parse_args(["run", "claude"]).no_project_memory is False


def test_the_help_names_the_option_the_variable_and_what_a_resume_does(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--no-project-memory" in text and "CONTEXTO_PROJECT_MEMORY" in text
    assert "0, false, off or no" in text
    assert "resuming it later stays off without this option" in text
    assert "search" not in text.split("keep this session out of project memory", 1)[1].split("stays off", 1)[0]


def test_the_resume_command_keeps_the_option():
    assert launcher.resume_command(LaunchOptions(), "s1", "c1") == "contexto run --session s1 claude --resume c1"
    assert (launcher.resume_command(LaunchOptions(project_memory=False), "s1", "c1")
            == "contexto run --session s1 --no-project-memory claude --resume c1")
    full = LaunchOptions(bound=120_000, observe_thinking=True, version_check=False, project_memory=False)
    assert (launcher.resume_command(full, "s1", "c1") == "contexto run --session s1 --bound 120000 "
            "--observe-thinking --no-version-check --no-project-memory claude --resume c1")


def test_the_option_after_the_cli_name_is_refused_with_the_command_to_run():
    assert launcher.RUN_OPTIONS["--no-project-memory"] is False  # it takes no value
    with pytest.raises(LaunchError) as raised:
        launcher.check_misplaced_options("claude", ["--no-project-memory", "--bound", "90000", "-p", "hi"])
    assert raised.value.code == EXIT_USAGE
    assert str(raised.value) == (
        "--no-project-memory after 'claude' is a Contexto option, and everything after 'claude' goes to the CLI "
        "unchanged. Put Contexto's options before 'claude': contexto run --no-project-memory --bound 90000 claude "
        "-p hi")


def test_a_misplaced_option_starts_nothing(home, root, capfd):
    code = cli.main(["run", "--upstream", "http://127.0.0.1:9", "claude", "--no-project-memory", "-p", "hi"])
    err = capfd.readouterr().err
    assert code == EXIT_USAGE and "--no-project-memory after 'claude' is a Contexto option" in err
    assert "contexto run --no-project-memory claude -p hi" in err
    assert "stub claude ran" not in err and not (home / "sessions").exists()


# What a launch records ----------------------------------------------------------------------------

@BOTH
@pytest.mark.parametrize("how", ["flag", "0", "false", "off", "no", " OFF ", "No"])
def test_off_is_recorded_and_nothing_is_said_about_the_project(home, root, capfd, monkeypatch, name, how):
    three_earlier(home, root)
    if how != "flag":
        monkeypatch.setenv("CONTEXTO_PROJECT_MEMORY", how)
    code, err = run(capfd, name, *(["--no-project-memory"] if how == "flag" else []))
    assert code == 0, err
    assert f"stub {name} ran" in err and "contexto: session new" in err
    assert project_lines(err) == [] and str(root) not in err and "project memory" not in err
    assert meta(home)["project_memory"] is False
    assert events(home) == [{"event": "project_memory", "on": False, **ZEROS}]
    store = SessionStore.open_existing(home, "new")
    assert store.project() is None and store.carried_text(6000) == ""


@BOTH
@pytest.mark.parametrize("value", [None, "", "1", "yes", "maybe"])
def test_on_records_nothing_new(home, root, capfd, monkeypatch, name, value):
    three_earlier(home, root)
    if value is not None:
        monkeypatch.setenv("CONTEXTO_PROJECT_MEMORY", value)
    code, err = run(capfd, name)
    assert code == 0, err
    assert "project_memory" not in meta(home) and meta(home)["project"] == str(root)
    assert len(project_lines(err)) == 1
    unknown = [line for line in err.splitlines() if "CONTEXTO_PROJECT_MEMORY" in line]
    assert len(unknown) == (1 if value == "maybe" else 0)
    assert all("0, false, off or no" in line and "'maybe'" in line for line in unknown)


@BOTH
def test_a_resumed_session_that_recorded_off_stays_off_without_the_option(home, root, capfd, monkeypatch, name):
    three_earlier(home, root)
    assert run(capfd, name, "--no-project-memory")[0] == 0
    monkeypatch.setenv("CONTEXTO_PROJECT_MEMORY", "1")  # an "on" never writes true over the record
    code, err = run(capfd, name)
    assert code == 0 and project_lines(err) == [] and f"stub {name} ran" in err
    assert meta(home)["project_memory"] is False
    assert events(home) == [{"event": "project_memory", "on": False, **ZEROS}] * 2


@BOTH
def test_a_resumed_session_is_turned_off_by_the_option(home, root, capfd, name):
    three_earlier(home, root)
    code, err = run(capfd, name)
    assert code == 0 and len(project_lines(err)) == 1 and "project_memory" not in meta(home)
    code, err = run(capfd, name, "--no-project-memory")
    assert code == 0 and project_lines(err) == [] and meta(home)["project_memory"] is False
    assert [e["on"] for e in events(home)] == [True, False]


@BOTH
def test_a_launch_that_cannot_record_off_does_not_start_the_cli(home, root, capfd, monkeypatch, name):
    def refuse(self, on):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(SessionStore, "set_project_memory", refuse)
    code, err = run(capfd, name, "--no-project-memory")
    assert code == EXIT_PROXY and f"stub {name} ran" not in err and "Traceback" not in err
    assert "cannot record that project memory is off for this session: " in err
    assert not (home / "sessions" / "new").exists()  # the session this launch made is removed again


# The start line -----------------------------------------------------------------------------------

@BOTH
def test_the_start_line_says_what_is_carried_before_the_cli_starts(home, root, capfd, name):
    three_earlier(home, root)
    code, err = run(capfd, name)
    assert code == 0, err
    lines = err.splitlines()
    line = f"contexto: project memory: 4 records carried from 2 of 3 earlier sessions in {root} {HINT}"
    assert project_lines(err) == [line]
    session = next(i for i, text in enumerate(lines) if text.startswith("contexto: session new "))
    assert session < lines.index(line) < lines.index(f"stub {name} ran")  # with the start notices, before the CLI
    assert events(home) == [{"event": "project_memory", "on": True, "earlier": 3, "carried": 4,
                             "carried_sessions": 2, "skipped": 0, "unreadable": 0}]
    log = (home / "sessions" / "new" / "log.jsonl").read_text()
    for private in (str(root), root.name, "aaaa1111", "bbbb2222", "cccc3333", "postgres", "changelog"):
        assert private not in log  # no path, no id, no text


def notice(store) -> tuple[str, list[dict]]:
    err = io.StringIO()
    before = len(events(store.home, store.session_id)) if (store.dir / "log.jsonl").exists() else 0
    assert launcher.project_memory_notice(store, err) is None
    return err.getvalue(), events(store.home, store.session_id)[before:]


def test_the_line_with_nothing_carried_says_so_and_promises_nothing_else(home, root):
    for n, sid in enumerate(("one", "two", "three"), 1):
        new_session(home, sid, root, n).pin("decision", f"not a main pin {n}")
    said, logged = notice(new_session(home, "reader", root, 9))
    assert said == f"contexto: project memory: 0 records carried from 3 earlier sessions in {root} {HINT}\n"
    assert "search" not in said
    assert logged == [{"event": "project_memory", "on": True, **ZEROS, "earlier": 3}]


def test_the_line_counts_one_session_and_one_record_in_the_singular(home, root):
    main_pin(new_session(home, "only", root, 1), "obligation", "ship it")
    said, logged = notice(new_session(home, "reader", root, 9))
    assert said == f"contexto: project memory: 1 record carried from 1 of 1 earlier session in {root} {HINT}\n"
    assert logged == [{"event": "project_memory", "on": True, **ZEROS, "earlier": 1, "carried": 1,
                       "carried_sessions": 1}]
    project_module.forget_cache()
    lone = repo(root.parent, "other")
    new_session(home, "empty", lone, 1)
    said, _ = notice(new_session(home, "reader2", lone, 9))
    assert said == f"contexto: project memory: 0 records carried from 1 earlier session in {lone} {HINT}\n"


def test_the_line_says_how_many_sessions_were_skipped(home, root):
    main_pin(new_session(home, "good", root, 1), "decision", "keep this")
    bad = new_session(home, "bad", root, 2)
    main_pin(bad, "obligation", "the first record")
    main_pin(bad, "decision", "the second record")
    lines = bad.ledger.path.read_bytes().split(b"\n")
    bad.ledger.path.write_bytes(b"\n".join([b"{not json", *lines[1:]]))  # damaged above its tail: skipped whole
    said, logged = notice(new_session(home, "reader", root, 9))
    assert said == (f"contexto: project memory: 1 record carried from 1 of 2 earlier sessions in {root}; 1 session "
                    f"skipped as damaged or unreadable {HINT}\n")
    assert logged == [{"event": "project_memory", "on": True, "earlier": 2, "carried": 1, "carried_sessions": 1,
                       "skipped": 1, "unreadable": 0}]
    # "reader" is now an earlier session too; one more cannot say which project it is in.
    torn = new_session(home, "torn", root, 3)
    (torn.dir / "meta.json").write_text("{not json")  # its project cannot be read: left out, and counted
    said, logged = notice(new_session(home, "reader2", root, 10))
    assert said == (f"contexto: project memory: 1 record carried from 1 of 3 earlier sessions in {root}; 1 session "
                    f"skipped as damaged or unreadable {HINT}\n")
    assert logged == [{"event": "project_memory", "on": True, "earlier": 3, "carried": 1, "carried_sessions": 1,
                       "skipped": 1, "unreadable": 1}]


def test_an_unreadable_session_of_another_project_is_not_counted_in_the_line(home, root):
    main_pin(new_session(home, "good", root, 1), "decision", "keep this")
    other = new_session(home, "other", repo(root.parent, "other"), 2)
    (other.dir / "meta.json").write_text("{not json")  # of any project: not this project's damage
    said, logged = notice(new_session(home, "reader", root, 9))
    assert said == f"contexto: project memory: 1 record carried from 1 of 1 earlier session in {root} {HINT}\n"
    assert logged == [{"event": "project_memory", "on": True, "earlier": 1, "carried": 1, "carried_sessions": 1,
                       "skipped": 0, "unreadable": 1}]


def sent_records(text: str) -> int:
    return sum(1 for line in text.split("\n") if line.startswith("- "))


@pytest.mark.parametrize("budget", [None, 1500])
def test_the_line_counts_what_the_first_request_sends_when_the_budget_cuts_it(home, root, budget):
    first = new_session(home, "first", root, 1)
    for i in range(40):
        main_pin(first, "obligation", f"rule {i}: " + "word " * 120)
    main_pin(new_session(home, "second", root, 2), "decision", "use postgres")
    reader = new_session(home, "reader", root, 9)
    err = io.StringIO()
    launcher.project_memory_notice(reader, err, **({} if budget is None else {"pin_budget": budget}))
    fits = sent_records(reader.carried_text(budget or 6000))  # the engine's pin budget is 6,000 by default
    assert 0 < fits < 41
    assert err.getvalue() == (f"contexto: project memory: {fits} of 41 records carried ({41 - fits} did not fit the "
                              f"pin budget; contexto state lists them all) from 2 of 2 earlier sessions in {root} "
                              f"{HINT}\n")


def test_no_earlier_session_no_line(home, root):
    said, logged = notice(new_session(home, "first", root, 1))
    assert said == "" and logged == [{"event": "project_memory", "on": True, **ZEROS}]
    second = repo(root.parent, "second")
    new_session(home, "elsewhere", root, 0)  # another project's session is not earlier
    new_session(home, "later", second, 5)  # nor is one created after
    said, logged = notice(new_session(home, "reader", second, 2))
    assert said == "" and logged == [{"event": "project_memory", "on": True, **ZEROS}]


def test_a_resumed_session_that_was_carrying_says_it_stops_sending_the_records(home, root):
    main_pin(new_session(home, "before", root, 1), "obligation", "ship it")
    reader = new_session(home, "reader", root, 9)
    record = {"start_text": reader.carried_text(6000)}
    reader.set_project_memory(False)  # resumed with --no-project-memory
    reader.epoch_record = lambda: record
    said, logged = notice(reader)
    assert said == ("contexto: project memory: off for this session; the records it carried are not sent again "
                    "from its next request\n")
    assert logged == [{"event": "project_memory", "on": False, **ZEROS}]


def test_the_help_and_the_forget_note_say_what_a_running_or_resumed_session_does(capsys):
    from contexto.commands.project import IN_USE_NOTE

    assert IN_USE_NOTE == ("in use: it keeps running, stops contributing to later sessions, and stops sending "
                           "what it carried from its next turn start before its first rotation, or from its next "
                           "rotation after it")
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert ("a resumed session stops sending what it carried: at once before its first rotation; after it, from "
            "its next rotation") in text


def test_memory_off_no_line(home, root):
    main_pin(new_session(home, "before", root, 1), "obligation", "ship it")
    said, logged = notice(new_session(home, "reader", root, 9, memory=False))
    assert said == "" and logged == [{"event": "project_memory", "on": False, **ZEROS}]


def test_a_session_with_no_project_no_line(home, root):
    """What ``contexto proxy`` and ``contexto mcp`` run by hand open: no working directory, no project."""
    main_pin(new_session(home, "before", root, 1), "obligation", "ship it")
    said, logged = notice(SessionStore(home, "by-hand"))
    assert said == "" and logged == [{"event": "project_memory", "on": True, **ZEROS}]


def test_a_store_without_projects_gets_no_line_and_no_event():
    class Plain:
        events: list = []

        def log(self, event):
            self.events.append(event)

    err = io.StringIO()
    launcher.project_memory_notice(Plain(), err)
    assert err.getvalue() == "" and Plain.events == []


def test_a_root_that_would_break_the_line_is_quoted(home, tmp_path):
    odd = repo(tmp_path, "re\npo \x1b[31m")
    main_pin(new_session(home, "before", odd, 1), "obligation", "ship it")
    said, logged = notice(new_session(home, "reader", odd, 9))
    assert said == f"contexto: project memory: 1 record carried from 1 of 1 earlier session in {str(odd)!r} {HINT}\n"
    assert said.count("\n") == 1 and "\x1b" not in said
    spaced = repo(tmp_path, "my repo")  # a printable path is shown as it is
    new_session(home, "before2", spaced, 1)
    assert f"0 records carried from 1 earlier session in {spaced} {HINT}" in notice(new_session(home, "reader2", spaced,
                                                                                               9))[0]


@pytest.mark.parametrize("where", ["project", "carried"])
def test_an_error_building_the_line_is_one_event_and_no_line(home, root, monkeypatch, where):
    main_pin(new_session(home, "before", root, 1), "obligation", "ship it")
    reader = new_session(home, "reader", root, 9)

    def fail(*args, **kwargs):
        raise RuntimeError(f"text that must not be logged: {root}")

    monkeypatch.setattr(SessionStore if where == "project" else project_module.Project, where, fail)
    said, logged = notice(reader)
    assert said == "" and logged == [{"event": "project_memory", "error": "RuntimeError"}]
    assert str(root) not in (reader.dir / "log.jsonl").read_text()


def test_a_log_that_cannot_be_written_does_not_stop_the_line(home, root, monkeypatch):
    main_pin(new_session(home, "before", root, 1), "obligation", "ship it")
    reader = new_session(home, "reader", root, 9)
    monkeypatch.setattr(SessionStore, "log", lambda self, event: (_ for _ in ()).throw(OSError(28, "No space")))
    err = io.StringIO()
    launcher.project_memory_notice(reader, err)
    assert err.getvalue().startswith("contexto: project memory: 1 record carried from 1 of 1 earlier session in ")


@BOTH
def test_an_error_building_the_line_leaves_the_launch_working(home, root, capfd, monkeypatch, name):
    three_earlier(home, root)

    def fail(self):
        raise RuntimeError("no view")

    monkeypatch.setattr(SessionStore, "project", fail)
    code, err = run(capfd, name)
    assert code == 0 and f"stub {name} ran" in err and project_lines(err) == [] and "Traceback" not in err
    assert events(home) == [{"event": "project_memory", "error": "RuntimeError"}]


def test_claude_version_alone_opens_no_session_and_reads_no_variable(home, root, capfd, monkeypatch):
    monkeypatch.setenv("CONTEXTO_PROJECT_MEMORY", "maybe")
    assert cli.main(["run", "claude", "--version"]) == 0
    err = capfd.readouterr().err
    assert "CONTEXTO_PROJECT_MEMORY" not in err and not (home / "sessions").exists()


# The whole path, as a user runs it ----------------------------------------------------------------

def test_a_real_launch_prints_the_line_before_claude_and_the_resume_hint_keeps_the_option(tmp_path):
    """``python -m contexto run`` against the fake Claude Code of test_launcher, which makes one request
    through the proxy: the start line is on stderr before anything Claude Code writes, and the resume
    command printed after it exits carries ``--no-project-memory`` when the launch had it."""
    Upstream.seen, Upstream.body_delay = [], 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        upstream = f"http://127.0.0.1:{server.server_address[1]}"
        bin_dir, root, home = tmp_path / "bin", repo(tmp_path), tmp_path / "contexto"
        bin_dir.mkdir()
        (bin_dir / "claude").write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
        (bin_dir / "claude").chmod(0o755)
        (tmp_path / "userhome").mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ANTHROPIC_", "CLAUDE", "OPENRIG_", "RIGGED_", "CONTEXTO_"))}
        env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", HOME=str(tmp_path / "userhome"),
                   CONTEXTO_HOME=str(home), FAKE_RECORD=str(tmp_path / "record.json"), FAKE_REQUEST="1",
                   FAKE_CLAUDE_SESSION="0acd81f6-1f21-40f8-ab64-5c09545905cf", FAKE_SAY="claude says hello")
        earlier(home, "aaaa1111", root, 300, [("obligation", "ship the notes")])

        def launch(*options):
            argv = [sys.executable, "-m", "contexto", "run", "--upstream", upstream, *options, "claude", "-p", "hi"]
            done = subprocess.run(argv, env=env, cwd=root, capture_output=True, text=True, timeout=60)
            assert done.returncode == 0, done.stderr
            return done.stderr.splitlines()

        lines = launch("--session", "on1")
        line = f"contexto: project memory: 1 record carried from 1 of 1 earlier session in {root} {HINT}"
        assert lines.index("contexto: session on1 (bound 150,000, upstream flag)") < lines.index(line)
        assert lines.index(line) < lines.index("claude says hello")
        assert lines[-1] == (f"contexto: resume this conversation through Contexto with: contexto run --session on1 "
                             f"--upstream {upstream} claude --resume 0acd81f6-1f21-40f8-ab64-5c09545905cf")
        lines = launch("--session", "off1", "--no-project-memory")
        assert not [text for text in lines if "project memory" in text] and "claude says hello" in lines
        assert lines[-1] == (f"contexto: resume this conversation through Contexto with: contexto run --session off1 "
                             f"--upstream {upstream} --no-project-memory claude --resume "
                             f"0acd81f6-1f21-40f8-ab64-5c09545905cf")
        assert json.loads((home / "sessions" / "off1" / "meta.json").read_text())["project_memory"] is False
        # The MCP server Claude Code would start is unchanged: the store's record is what it reads.
        record = json.loads((tmp_path / "record.json").read_text())
        assert record["mcp"]["mcpServers"]["contexto"]["args"] == ["-P", "-m", "contexto", "mcp", "--session=off1"]
        assert record["mcp"]["mcpServers"]["contexto"]["env"] == {"CONTEXTO_HOME": str(home)}
    finally:
        server.shutdown()
        server.server_close()


# The cost -----------------------------------------------------------------------------------------

def test_the_line_reads_each_earlier_session_once_with_fifty_of_them(home, root, monkeypatch, capsys):
    """Fifty earlier sessions of twenty records each: the line reads each session's ``meta.json`` and
    ledger once, cold, and no other file of theirs. The time is printed for the record (``pytest -s``);
    the ceiling is far above it (it is tens of milliseconds on an idle machine), so only a change that
    makes the launch read the project many times over can fail it."""
    for n in range(50):
        store = SessionStore(home, f"{n:02d}" + "ab" * 15)
        store.set_workdir(root)
        for i in range(20):
            main_pin(store, "obligation" if i % 2 else "decision", f"record {i} of session {n}")
        os.utime(store.dir / "page.key", (time.time() - 1000 + n,) * 2)
        store.release()
    reader = SessionStore(home, "reader")
    reader.set_workdir(root)
    project_module.forget_cache()
    read: list[tuple[str, str]] = []
    real = project_module.read_regular

    def counting(path, *args, **kwargs):
        read.append((Path(path).parent.name, Path(path).name))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(project_module, "read_regular", counting)
    err = io.StringIO()
    started = time.perf_counter()
    launcher.project_memory_notice(reader, err)
    took = time.perf_counter() - started
    fits = sent_records(reader.carried_text(6000))
    assert err.getvalue() == (f"contexto: project memory: {fits} of 1000 records carried ({1000 - fits} did not fit "
                              f"the pin budget; contexto state lists them all) from 50 of 50 earlier sessions in "
                              f"{root} {HINT}\n")
    others = [entry for entry in read if entry[0] != "reader"]
    assert sorted(others) == sorted([(f"{n:02d}" + "ab" * 15, name) for n in range(50)
                                     for name in ("meta.json", "ledger.jsonl")])
    with capsys.disabled():
        print(f"\nproject memory start line, 50 earlier sessions x 20 records, cold: {took * 1000:.1f} ms")
    assert took < 10.0


def test_a_resume_from_another_project_says_the_session_stays_in_its_own(home, root):
    sandbox = repo(root.parent, "sandbox")
    x = new_session(home, "x", sandbox, 1)
    x.release()
    resumed = launcher.open_session(home, "x", root)
    said, _ = notice(resumed)
    assert said == (f"contexto: project memory: session x stays in the project it was started in, {sandbox}; it "
                    f"neither carries from nor gives to {root}\n")
    assert notice(launcher.open_session(home, "x", sandbox))[0] == ""  # back in its own project: no such line
