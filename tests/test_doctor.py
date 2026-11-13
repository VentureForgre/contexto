"""``contexto doctor`` (D64): what it checks, what it prints, and what it must never do.

Everything is synthetic: fake ``claude`` and ``codex`` scripts that only answer ``--version``, settings
files and a store home under ``tmp_path``, and a temporary ``HOME``. The MCP check starts the real
server from this checkout. Nothing here contacts a model or an upstream; the last test asserts that,
with an audit hook in this process and in the MCP server doctor starts.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pwd
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from contexto import cli
from contexto.cliversions import TESTED_VERSIONS
from contexto.commands import doctor
from contexto.mcp import TOOLS

CHECK_IDS = ["python", "claude", "codex", "settings", "claude-upstream", "codex-upstream", "claude-key",
             "codex-key", "store", "mcp", "provider"]
TESTED_CLAUDE = TESTED_VERSIONS["claude"][-1]
TESTED_CODEX = TESTED_VERSIONS["codex"][-1]
SECRET = "sk-doctor-SECRET-0123456789abcdefghijklmnopqrstuvwxyz"  # 53 characters
HELPER_SECRET = "helper-SECRET-zyxwvutsrqponmlkjihgfedcba"
URL_SECRET = "url-SECRET-password"


def fake_cli(world, name: str, version_line: str | None, *, body: str | None = None) -> Path:
    """A ``name`` on PATH that prints ``version_line`` for ``--version`` (or runs ``body``). The script
    is written once per test session and linked into the test's PATH directory: macOS checks each new
    executable file the first time it runs, which takes seconds on a busy machine."""
    body = body if body is not None else f"#!/bin/sh\necho '{version_line}'\n"
    script = world.scripts / f"cli-{hashlib.sha256(body.encode()).hexdigest()[:16]}"
    if not script.exists():
        script.write_text(body)
        script.chmod(0o755)
    path = world.bin / name
    path.symlink_to(script)
    return path


def write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture(scope="session")
def scripts(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("doctor-scripts")


@pytest.fixture
def world(tmp_path, monkeypatch, scripts):
    """A clean environment: a temporary HOME and working directory, an empty PATH directory, a store
    home that does not exist yet, and none of the variables doctor reads. ``--version`` is read by the
    real ``installed_version`` with a longer timeout than its 3 s: these tests are not about how fast a
    script starts, and a busy machine can take longer."""
    real_version = doctor.installed_version
    monkeypatch.setattr(doctor, "installed_version", lambda path, env=None: real_version(path, env=env, timeout=60))
    for name in list(os.environ):
        if name.startswith(("ANTHROPIC_", "CLAUDE", "CONTEXTO_", "OPENRIG_")):
            monkeypatch.delenv(name)
    home, cwd, bin_dir = tmp_path / "userhome", tmp_path / "project", tmp_path / "bin"
    for d in (home, cwd, bin_dir):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "store"))
    monkeypatch.chdir(cwd)

    class World:
        pass

    w = World()
    w.home, w.cwd, w.bin, w.store, w.tmp, w.scripts = home, cwd, bin_dir, tmp_path / "store", tmp_path, scripts
    return w


@pytest.fixture
def both(world):
    fake_cli(world, "claude", f"{TESTED_CLAUDE} (Claude Code)")
    fake_cli(world, "codex", f"codex-cli {TESTED_CODEX}")
    return world


@pytest.fixture
def fast_mcp(monkeypatch):
    """The MCP check answered at once, for tests about the other checks (the real one runs in its own)."""
    names = [t["name"] for t in TOOLS]
    monkeypatch.setattr(doctor, "check_mcp", lambda *a, **k: doctor.Check(
        "mcp", doctor.PASS, "MCP server started and listed its 5 tools", details={"tools": names}))


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(["doctor", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def checks(capsys) -> tuple[int, dict[str, dict]]:
    code, out, _ = run(capsys, "--json")
    data = json.loads(out)
    return code, {c["id"]: c for c in data["checks"]}


def line_of(out: str, text: str) -> str:
    return next(line for line in out.splitlines() if text in line)


# The whole command ----------------------------------------------------------------------------------


def test_everything_in_place_passes_every_check_and_exits_0(both, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("CONTEXTO_CODEX_UPSTREAM", "https://api.openai.com/v1")
    monkeypatch.setenv("CONTEXTO_CODEX_KEY_HELPER", f"echo {HELPER_SECRET}")
    code, out, err = run(capsys)
    assert code == 0 and err == ""
    lines = out.splitlines()
    marked = [line for line in lines if line.startswith("[")]
    assert len(marked) == len(CHECK_IDS) and all(line.startswith("[ok]") for line in marked), out
    assert not any(line.lstrip().startswith("->") for line in lines)  # nothing to do
    assert lines[-1] == "Ready: 11 passed."
    assert f"Claude Code {TESTED_CLAUDE}" in out and f"Codex {TESTED_CODEX}" in out
    assert "https://api.anthropic.com" in out and "https://api.openai.com" in out
    assert "listed its 5 tools" in out and str(both.store) in out


def test_json_has_the_documented_shape(both, capsys):
    code, out, _ = run(capsys, "--json")
    data = json.loads(out)
    assert list(data) == ["schema", "contexto", "ok", "summary", "checks"]
    assert data["schema"] == 1 and data["ok"] is (code == 0) is True
    assert list(data["summary"]) == ["pass", "warn", "fail", "skip"]
    assert sum(data["summary"].values()) == len(data["checks"])
    assert [c["id"] for c in data["checks"]] == CHECK_IDS
    for check in data["checks"]:
        assert list(check) == ["id", "status", "summary", "action", "details"]
        assert check["status"] in ("pass", "warn", "fail", "skip")
        assert isinstance(check["summary"], str) and "\n" not in check["summary"]
        assert isinstance(check["details"], dict)
        # What to do next, in one line, for exactly the warnings and failures.
        assert (check["action"] is not None) == (check["status"] in ("warn", "fail")), check
        assert check["action"] is None or "\n" not in check["action"]


def test_text_and_json_report_the_same_results(both, capsys, fast_mcp, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    code_text, out, _ = run(capsys)
    code_json, data = checks(capsys)
    assert code_text == code_json == 0
    marks = {"pass": "[ok]", "warn": "[warn]", "fail": "[FAIL]", "skip": "[skip]"}
    lines = out.splitlines()
    for check in data.values():
        line = line_of(out, check["summary"])
        assert line.startswith(marks[check["status"]]), line
        if check["action"]:
            assert lines[lines.index(line) + 1].strip() == f"-> {check['action']}"


def test_warnings_alone_exit_0_and_a_failure_exits_1(world, capsys, fast_mcp):
    fake_cli(world, "claude", "2.1.999 (Claude Code)")  # untested, no key, no codex: warnings only
    code, out, _ = run(capsys)
    assert code == 0 and "[warn]" in out and "[FAIL]" not in out
    assert out.splitlines()[-1].startswith("Ready: ") and "warning" in out.splitlines()[-1]
    world.store.write_text("a file where the store home should be")
    code, out, _ = run(capsys)
    assert code == 1 and "[FAIL]" in out
    assert out.splitlines()[-1].startswith("Not ready: 1 failed")


def test_doctor_is_a_registered_command_with_help(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["doctor", "--help"])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    assert "--json" in text and "network" in text


def test_a_check_that_raises_is_reported_as_failed_by_type_only_and_the_rest_still_run(both, capsys, fast_mcp,
                                                                                         monkeypatch):
    def broken(ctx):
        raise RuntimeError(f"a message that could hold {URL_SECRET}")

    monkeypatch.setattr(doctor, "check_claude_key", broken)
    code, out, _ = run(capsys)
    line = line_of(out, "Claude Code key")
    assert code == 1 and line.startswith("[FAIL]") and "RuntimeError" in line and URL_SECRET not in out
    assert line_of(out, "Store home").startswith("[ok]") and line_of(out, "MCP server").startswith("[ok]")


def test_ctrl_c_exits_130_without_a_traceback(both, capsys, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(doctor, "check_mcp", interrupted)
    code, out, err = run(capsys)
    assert code == 130 and err.strip() == "contexto: interrupted"
    # The lines of the checks that finished are out already; there is no MCP line and no verdict.
    assert line_of(out, "Store home").startswith("[ok]") and "MCP server" not in out and "eady: " not in out
    code, out, err = run(capsys, "--json")  # JSON is one object or nothing
    assert code == 130 and out == "" and err.strip() == "contexto: interrupted"


def test_each_line_is_printed_when_its_check_is_done_and_json_only_at_the_end(both, capsys, monkeypatch):
    """Reviewer B, PR #258: nothing was printed until every check was done, 16 s of silence when both
    ``--version`` calls and the MCP server hang."""
    printed: list[str] = []

    def slow_mcp(*args, **kwargs):
        printed.append(capsys.readouterr().out)  # what is on stdout when the MCP check starts
        return doctor.Check("mcp", doctor.PASS, "MCP server started and listed its 5 tools")

    monkeypatch.setattr(doctor, "check_mcp", slow_mcp)
    code, out, _ = run(capsys)
    before = printed.pop()
    assert before.startswith("contexto doctor (contexto ") and before.endswith("\n")
    assert [line.split()[0] for line in before.splitlines() if line.startswith("[")].count("[ok]") >= 4
    assert "Store home" in before and "MCP server" not in before and "eady: " not in before
    assert out.startswith("[ok]   MCP server") and out.splitlines()[-1].startswith("Ready: ")
    code, out, _ = run(capsys, "--json")
    assert printed == [""] and json.loads(out)["ok"] is True


def test_an_unusable_claude_waits_for_codex_and_a_usable_one_is_printed_at_once(world, capsys, monkeypatch):
    """With neither CLI usable both lines are failures, so claude's is held until codex is known."""
    printed: list[str] = []
    real = doctor.check_cli

    def watched(cli, env):
        printed.append(capsys.readouterr().out)
        return real(cli, env)

    monkeypatch.setattr(doctor, "check_cli", watched)
    monkeypatch.setattr(doctor, "check_mcp", lambda *a, **k: doctor.Check("mcp", doctor.PASS, "MCP server ok"))
    code, out, _ = run(capsys)
    assert code == 1 and "Claude Code" not in printed[1]  # held: it became a failure
    assert line_of(out, "Claude Code:").startswith("[FAIL]") and line_of(out, "Codex:").startswith("[FAIL]")
    printed.clear()
    fake_cli(world, "claude", f"{TESTED_CLAUDE} (Claude Code)")
    code, out, _ = run(capsys)
    assert code == 0 and "[ok]   Claude Code " in printed[1]  # final as soon as it is known to be usable


# Python ---------------------------------------------------------------------------------------------


def test_python_version():
    assert doctor.check_python((3, 12, 0)).status == doctor.PASS
    assert doctor.check_python(sys.version_info[:3]).status == doctor.PASS
    old = doctor.check_python((3, 11, 9))
    assert old.status == doctor.FAIL and "3.11.9" in old.summary and "3.12" in old.action


# The CLIs -------------------------------------------------------------------------------------------


def test_a_tested_version_passes_and_names_the_path(both, capsys, fast_mcp):
    _, found = checks(capsys)
    assert found["claude"]["status"] == found["codex"]["status"] == "pass"
    assert found["claude"]["details"] == {"cli": "claude", "path": str(both.bin / "claude"),
                                          "version": TESTED_CLAUDE, "tested": True,
                                          "tested_versions": list(TESTED_VERSIONS["claude"])}
    assert found["codex"]["details"]["version"] == TESTED_CODEX


def test_an_untested_version_is_a_warning_that_lists_the_tested_ones(world, capsys, fast_mcp):
    fake_cli(world, "claude", "2.1.999 (Claude Code)")
    fake_cli(world, "codex", f"codex-cli {TESTED_CODEX}")
    code, found = checks(capsys)
    claude = found["claude"]
    assert code == 0 and claude["status"] == "warn" and claude["details"]["tested"] is False
    assert "2.1.999" in claude["summary"] and ", ".join(TESTED_VERSIONS["claude"]) in claude["summary"]
    assert "newer Contexto" in claude["action"]


def test_a_missing_second_cli_is_a_warning_and_its_other_checks_are_skipped(world, capsys, fast_mcp):
    fake_cli(world, "claude", f"{TESTED_CLAUDE} (Claude Code)")
    code, found = checks(capsys)
    assert code == 0
    assert found["codex"]["status"] == "warn" and "not found on PATH" in found["codex"]["summary"]
    assert found["codex"]["action"]
    assert found["codex-upstream"]["status"] == found["codex-key"]["status"] == "skip"
    assert "codex" in found["codex-upstream"]["summary"]
    assert found["claude-upstream"]["status"] == "pass"


def test_codex_alone_is_enough_and_the_claude_only_checks_are_skipped(world, capsys, fast_mcp):
    fake_cli(world, "codex", f"codex-cli {TESTED_CODEX}")
    code, found = checks(capsys)
    assert code == 0 and found["claude"]["status"] == "warn" and found["codex"]["status"] == "pass"
    assert [found[i]["status"] for i in ("claude-upstream", "claude-key", "provider")] == ["skip"] * 3


def test_neither_cli_fails_both_and_exits_1(world, capsys, fast_mcp):
    code, found = checks(capsys)
    assert code == 1
    assert found["claude"]["status"] == found["codex"]["status"] == "fail"
    assert "at least one" in found["claude"]["action"]


def test_a_cli_whose_version_cannot_be_read_is_a_warning(world, capsys, fast_mcp):
    fake_cli(world, "claude", None, body="#!/bin/sh\necho no version here\n")
    fake_cli(world, "codex", None, body="#!/bin/sh\nexit 3\n")
    code, found = checks(capsys)
    assert code == 0
    assert found["claude"]["status"] == "warn" and "unparsed" in found["claude"]["summary"]
    assert found["codex"]["status"] == "warn" and "exit_3" in found["codex"]["summary"]
    assert found["claude"]["details"]["version"] is None and found["claude"]["details"]["tested"] is None


def test_a_cli_that_cannot_be_started_counts_as_missing(world, capsys, fast_mcp):
    fake_cli(world, "claude", None, body="#!/nonexistent/interpreter\n")
    code, found = checks(capsys)
    assert code == 1 and found["claude"]["status"] == "fail"
    assert "cannot be run" in found["claude"]["summary"]
    not_executable = world.bin / "codex"
    not_executable.write_text("#!/bin/sh\necho codex-cli 0.166.0\n")  # mode 0644
    _, found = checks(capsys)
    assert found["codex"]["status"] == "fail" and "not executable" in found["codex"]["summary"]


# The upstream ---------------------------------------------------------------------------------------


def test_the_default_upstream_and_its_source(both, capsys, fast_mcp):
    _, found = checks(capsys)
    upstream = found["claude-upstream"]
    assert upstream["status"] == "pass"
    assert upstream["details"] == {"origin": "https://api.anthropic.com", "source": "default"}
    assert "https://api.anthropic.com" in upstream["summary"] and "default" in upstream["summary"]


def test_an_upstream_from_settings_is_shown_as_its_origin_without_credentials(both, capsys, fast_mcp):
    write_settings(both.cwd / ".claude" / "settings.json", {"env": {
        "ANTHROPIC_BASE_URL": f"https://user:{URL_SECRET}@gateway.example.com:8443/secret-path?token={URL_SECRET}"}})
    code, out, _ = run(capsys)
    line = line_of(out, "Claude Code upstream")
    assert line.startswith("[ok]") and "https://gateway.example.com:8443 " in line
    assert ".claude/settings.json" in line
    _, json_out, _ = run(capsys, "--json")
    for text in (out, json_out):
        assert URL_SECRET not in text and "secret-path" not in text and "user:" not in text
    found = {c["id"]: c for c in json.loads(json_out)["checks"]}
    assert found["claude-upstream"]["details"] == {"origin": "https://gateway.example.com:8443",
                                                   "source": "settings:project"}


def test_an_upstream_from_the_environment(both, capsys, fast_mcp, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8317")
    _, found = checks(capsys)
    assert found["claude-upstream"]["details"] == {"origin": "http://127.0.0.1:8317", "source": "env"}
    assert "ANTHROPIC_BASE_URL in the environment" in found["claude-upstream"]["summary"]
    # Codex derives its own from it, plus /v1 (contexto.codex.codex_upstream_with_source).
    assert found["codex-upstream"]["status"] == "pass"
    assert found["codex-upstream"]["details"] == {"origin": "http://127.0.0.1:8317", "source": "claude-env+/v1"}


@pytest.mark.parametrize("url", ["http://[::1", "ftp://example.com", "gateway.example.com", "http://h:port"])
def test_an_upstream_contexto_run_would_refuse_fails_without_echoing_it(both, capsys, fast_mcp, monkeypatch, url):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", url + f"/{URL_SECRET}")
    code, out, _ = run(capsys)
    line = line_of(out, "Claude Code upstream")
    assert code == 1 and line.startswith("[FAIL]") and URL_SECRET not in out
    assert "ANTHROPIC_BASE_URL in the environment" in line


def test_codex_has_no_upstream_when_claude_code_s_is_anthropic(both, capsys, fast_mcp):
    code, found = checks(capsys)
    codex = found["codex-upstream"]
    assert code == 0 and codex["status"] == "warn"
    assert "api.anthropic.com" in codex["summary"] and "--codex-upstream" in codex["action"]


def test_codex_upstream_from_its_own_variable(both, capsys, fast_mcp, monkeypatch):
    monkeypatch.setenv("CONTEXTO_CODEX_UPSTREAM", f"https://k:{URL_SECRET}@api.openai.com/v1")
    code, out, _ = run(capsys, "--json")
    found = {c["id"]: c for c in json.loads(out)["checks"]}
    assert found["codex-upstream"]["status"] == "pass" and URL_SECRET not in out
    assert found["codex-upstream"]["details"] == {"origin": "https://api.openai.com", "source": "codex-env"}
    assert "CONTEXTO_CODEX_UPSTREAM" in found["codex-upstream"]["summary"]


def test_codex_ignores_a_project_base_url_in_an_untrusted_folder_and_says_so(both, capsys, fast_mcp):
    write_settings(both.cwd / ".claude" / "settings.json", {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}})
    _, found = checks(capsys)
    assert found["claude-upstream"]["details"]["source"] == "settings:project"  # as Claude Code -p reads it
    codex = found["codex-upstream"]
    assert codex["status"] == "warn" and "not recorded this folder as trusted" in codex["action"]
    assert codex["details"]["ignored"] == ["project"]


def test_a_host_that_does_not_print_is_escaped_in_the_text_form(both, capsys, fast_mcp):
    """Reviewer A, PR #258: a host from a project's settings file reached the terminal as written, so
    backspaces on an ``[ok]`` line could show another host than the one in the file."""
    write_settings(both.cwd / ".claude" / "settings.json",
                   {"env": {"ANTHROPIC_BASE_URL": "https://evil.example\x08\x08\x08\x08\x07good.example"}})
    code, out, _ = run(capsys)
    line = line_of(out, "Claude Code upstream")
    assert line.startswith("[ok]") and "https://evil.example\\x08\\x08\\x08\\x08\\x07good.example " in line
    assert not any(ord(c) < 32 and c != "\n" for c in out)
    _, json_out, _ = run(capsys, "--json")
    assert not any(ord(c) < 32 and c != "\n" for c in json_out)


@pytest.mark.parametrize("host,shown", [("\u0430pple.com", "xn--pple-43d.com"),  # a Cyrillic a
                                        ("b\u00fccher.example", "xn--bcher-kva.example"),
                                        ("exa\u202emple.com", "exa\\u202emple.com")])  # no name may hold it
def test_a_host_that_is_not_ascii_is_shown_as_it_goes_on_the_wire(both, capsys, fast_mcp, monkeypatch, host, shown):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"https://{host}:8443/path")
    code, out, _ = run(capsys)
    line = line_of(out, "Claude Code upstream")
    assert f"https://{shown}:8443 " in line and host not in out
    _, found = checks(capsys)
    assert found["claude-upstream"]["details"]["origin"] == f"https://{shown}:8443"


def test_printable_escapes_what_a_terminal_would_act_on():
    assert doctor.printable("plain caf\u00e9 /tmp/x") == "plain caf\u00e9 /tmp/x"
    assert doctor.printable("a\x1b[2Kb\rc\x7f") == "a\\x1b[2Kb\\rc\\x7f"
    assert doctor.printable("left\u202eright\u200b") == "left\\u202eright\\u200b"
    assert doctor.printable("bytes\udcff") == "bytes\\udcff"  # a path that is not UTF-8


# Keys -----------------------------------------------------------------------------------------------


def test_no_key_is_a_warning_that_allows_for_a_login(both, capsys, fast_mcp):
    code, found = checks(capsys)
    assert code == 0
    assert found["claude-key"]["status"] == "warn" and "login" in found["claude-key"]["action"]
    assert found["claude-key"]["details"] == {"found": []}
    assert found["codex-key"]["status"] == "warn" and "--codex-key-helper" in found["codex-key"]["action"]


def test_a_key_is_reported_by_name_and_place_and_never_shown(both, capsys, fast_mcp, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    write_settings(both.home / ".claude" / "settings.json", {"env": {"ANTHROPIC_AUTH_TOKEN": SECRET[::-1]}})
    code, out, _ = run(capsys)
    _, json_out, _ = run(capsys, "--json")
    found = {c["id"]: c for c in json.loads(json_out)["checks"]}
    assert found["claude-key"]["status"] == "pass"
    assert found["claude-key"]["details"] == {"found": [{"name": "ANTHROPIC_API_KEY", "source": "env"},
                                                        {"name": "ANTHROPIC_AUTH_TOKEN", "source": "settings:user"}]}
    line = line_of(out, "Claude Code key")
    assert "ANTHROPIC_API_KEY" in line and "the environment" in line and "~/.claude/settings.json" in line
    for text in (out, json_out):
        assert SECRET not in text and SECRET[::-1] not in text
        assert SECRET[:8] not in text and SECRET[-8:] not in text  # no part of it
    for text in (line, json.dumps(found["claude-key"])):  # nor its length (53); other lines hold paths
        assert str(len(SECRET)) not in text


def test_an_empty_key_variable_is_not_a_key(both, capsys, fast_mcp, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    _, found = checks(capsys)
    assert found["claude-key"]["status"] == "warn"


def helper_leaving(marker: Path) -> str:
    """A key helper that creates ``marker`` and prints a key. It can run in these tests' environment,
    whose PATH holds only the fake CLIs: the shell by its full path, and a redirect, which needs no
    program. The first version used ``sh`` and ``touch``, which that PATH does not have, so no marker
    could appear even if doctor had run the helper (reviewer B, PR #258)."""
    return f"/bin/sh -c ': > {marker}; echo {HELPER_SECRET}'"


def run_as_a_cli_would(helper: str, marker: Path) -> None:
    """The control for the "never run" assertions: run through a shell (as Codex and Claude Code run a
    helper) and as a plain command line, the helper does leave its marker and print its key here."""
    for command, shell in ((helper, True), (shlex.split(helper), False)):
        done = subprocess.run(command, shell=shell, capture_output=True, text=True, timeout=60)
        assert done.stdout.strip() == HELPER_SECRET and marker.exists(), done.stderr
        marker.unlink()


def test_a_key_helper_is_reported_and_never_run_or_shown(both, capsys, fast_mcp):
    marker = both.tmp / "HELPER-RAN"
    helper = helper_leaving(marker)
    run_as_a_cli_would(helper, marker)
    write_settings(both.home / ".claude" / "settings.json", {"apiKeyHelper": helper})
    code, out, _ = run(capsys)
    _, json_out, _ = run(capsys, "--json")
    found = {c["id"]: c for c in json.loads(json_out)["checks"]}
    assert not marker.exists()  # doctor never runs a helper
    for text in (out, json_out):  # nor prints its command, which can hold a key, nor its output
        assert HELPER_SECRET not in text and "/bin/sh -c" not in text and marker.name not in text
    assert found["claude-key"]["details"] == {"found": [{"name": "apiKeyHelper", "source": "settings:user"}]}
    assert found["codex-key"]["status"] == "pass"
    assert found["codex-key"]["details"] == {"name": "apiKeyHelper", "source": "settings:user"}
    assert "not run" in line_of(out, "Codex key helper")


def test_codex_key_helper_from_its_own_variable(both, capsys, fast_mcp, monkeypatch):
    marker = both.tmp / "HELPER-RAN"
    helper = helper_leaving(marker)
    run_as_a_cli_would(helper, marker)
    monkeypatch.setenv("CONTEXTO_CODEX_KEY_HELPER", helper)
    code, out, _ = run(capsys, "--json")
    found = {c["id"]: c for c in json.loads(out)["checks"]}
    assert found["codex-key"]["status"] == "pass" and not marker.exists()
    assert HELPER_SECRET not in out and "/bin/sh -c" not in out and marker.name not in out
    assert found["codex-key"]["details"] == {"name": "CONTEXTO_CODEX_KEY_HELPER", "source": "env"}


def launch(world, *argv: str, **env: str) -> subprocess.CompletedProcess:
    """``contexto run ...`` in the world, with the fake CLIs: what the launcher does where doctor looked."""
    return subprocess.run([sys.executable, "-m", "contexto", "run", *argv], cwd=world.cwd, capture_output=True,
                          text=True, timeout=120, env={**os.environ, **env})


@pytest.mark.parametrize("value", [" ", "\t \n"])
def test_a_blank_codex_key_helper_variable_is_what_the_launcher_refuses(both, capsys, fast_mcp, monkeypatch, value):
    """The launcher takes any value that is not empty as the helper and then refuses a blank one. Doctor
    stripped it, read on, and reported an ``apiKeyHelper`` the launch never reaches."""
    write_settings(both.home / ".claude" / "settings.json", {"apiKeyHelper": f"echo {HELPER_SECRET}"})
    monkeypatch.setenv("CONTEXTO_CODEX_KEY_HELPER", value)
    monkeypatch.setenv("CONTEXTO_CODEX_UPSTREAM", "http://127.0.0.1:9/v1")
    code, found = checks(capsys)
    key = found["codex-key"]
    assert code == 0 and key["status"] == "warn" and "blank" in key["summary"], key
    assert "CONTEXTO_CODEX_KEY_HELPER" in key["action"] and "--codex-key-helper" in key["action"]
    assert key["details"] == {"name": "CONTEXTO_CODEX_KEY_HELPER", "source": "env", "error": "blank"}
    refused = launch(both, "codex", "exec", "hi")
    assert refused.returncode == 2 and "the key helper command is empty" in refused.stderr
    monkeypatch.setenv("CONTEXTO_CODEX_KEY_HELPER", "")  # empty is unset, for the launcher too
    _, found = checks(capsys)
    assert found["codex-key"]["details"] == {"name": "apiKeyHelper", "source": "settings:user"}


def test_a_project_key_helper_counts_for_codex_only_in_a_trusted_folder(both, capsys, fast_mcp):
    # D52: Codex runs the helper unasked, so a project's counts only once Claude Code recorded the folder.
    write_settings(both.cwd / ".claude" / "settings.json", {"apiKeyHelper": f"echo {HELPER_SECRET}"})
    _, found = checks(capsys)
    assert found["claude-key"]["details"] == {"found": [{"name": "apiKeyHelper", "source": "settings:project"}]}
    codex = found["codex-key"]
    assert codex["status"] == "warn" and "not recorded this folder as trusted" in codex["action"]
    assert codex["details"] == {"name": None, "source": None, "ignored": ["project"]}
    (both.home / ".claude.json").write_text(json.dumps(
        {"projects": {str(both.cwd.resolve()): {"hasTrustDialogAccepted": True}}}))
    _, found = checks(capsys)
    assert found["codex-key"]["status"] == "pass"
    assert found["codex-key"]["details"] == {"name": "apiKeyHelper", "source": "settings:project"}


# The store home -------------------------------------------------------------------------------------


def snapshot(path: Path) -> tuple[int, list[str]]:
    return stat.S_IMODE(path.stat().st_mode), sorted(p.name for p in path.iterdir())


def test_a_home_that_does_not_exist_can_be_created_and_is_not_created(both, capsys):
    code, found = checks(capsys)  # the real MCP check too: it must not create the home either
    assert code == 0 and found["store"]["status"] == "pass"
    assert "does not exist yet" in found["store"]["summary"]
    assert found["store"]["details"] == {"path": str(both.store), "exists": False}
    assert not both.store.exists()


@pytest.mark.parametrize("mode", [0o700, 0o755, 0o777])
def test_an_existing_home_keeps_its_mode_and_gets_nothing_left_in_it(both, capsys, mode):
    both.store.mkdir()
    (both.store / "sessions").mkdir()
    (both.store / "sessions" / "s1").mkdir()
    for path in (both.store, both.store / "sessions"):
        path.chmod(mode)
    before = snapshot(both.store), snapshot(both.store / "sessions")
    code, found = checks(capsys)  # with the real MCP check: the user's store gets no session from it
    assert code == 0 and found["store"]["status"] == "pass" and found["mcp"]["status"] == "pass"
    assert found["store"]["details"] == {"path": str(both.store), "exists": True}
    assert (snapshot(both.store), snapshot(both.store / "sessions")) == before


def test_a_home_that_is_a_file_fails(both, capsys, fast_mcp):
    both.store.write_text("x")
    code, found = checks(capsys)
    assert code == 1 and found["store"]["status"] == "fail"
    assert "not a directory" in found["store"]["summary"] and "CONTEXTO_HOME" in found["store"]["action"]
    assert both.store.read_text() == "x"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
@pytest.mark.parametrize("where", ["home", "parent"])
def test_a_home_that_cannot_be_written_fails_and_is_left_as_it_was(both, capsys, fast_mcp, monkeypatch, where):
    locked = {"home": both.store, "parent": both.tmp / "locked"}[where]
    locked.mkdir()
    if where == "parent":
        monkeypatch.setenv("CONTEXTO_HOME", str(locked / "deep" / "store"))
    locked.chmod(0o500)
    try:
        code, found = checks(capsys)
        assert code == 1 and found["store"]["status"] == "fail", found["store"]
        assert found["store"]["action"]
        assert stat.S_IMODE(locked.stat().st_mode) == 0o500 and list(locked.iterdir()) == []
        assert launch(both, "claude", "-p", "hi").returncode != 0  # and the launcher cannot start either
    finally:
        locked.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
@pytest.mark.parametrize("home_mode,status", [(0o700, "pass"), (0o500, "warn")])
def test_a_read_only_sessions_directory_of_the_user_s_own_is_no_obstacle(both, capsys, fast_mcp, home_mode, status):
    """Reviewers A and B, PR #258: the store sets ``sessions`` to 0700 itself, so ``contexto run claude``
    works where doctor's required check failed. Doctor says so and changes no mode."""
    sessions = both.store / "sessions"
    sessions.mkdir(parents=True)
    sessions.chmod(0o500)
    both.store.chmod(home_mode)
    try:
        code, found = checks(capsys)
        store = found["store"]
        assert code == 0 and store["status"] == status, store
        assert "sets it to mode 0700" in store["summary"]
        assert (store["action"] is not None) == (status == "warn")
        assert stat.S_IMODE(sessions.stat().st_mode) == 0o500 and list(sessions.iterdir()) == []
        assert stat.S_IMODE(both.store.stat().st_mode) == home_mode and snapshot(both.store)[1] == ["sessions"]
        started = launch(both, "claude", "-p", "hi")  # the launcher agrees, and it is the one that changes the mode
        assert started.returncode == 0, started.stderr
        assert stat.S_IMODE(sessions.stat().st_mode) == 0o700
    finally:
        both.store.chmod(0o700)
        sessions.chmod(0o700)


def test_a_sessions_directory_of_another_user_fails(both, capsys, fast_mcp, monkeypatch):
    # The launcher's chmod of it fails then, whatever its mode. Another user is simulated: ours is not its owner.
    (both.store / "sessions").mkdir(parents=True)
    monkeypatch.setattr(doctor.os, "geteuid", lambda: (both.store / "sessions").stat().st_uid + 1)
    code, found = checks(capsys)
    assert code == 1 and found["store"]["status"] == "fail" and "another user" in found["store"]["summary"]
    (both.store / "sessions").rmdir()
    (both.store / "sessions").write_text("a file")
    code, found = checks(capsys)
    assert code == 1 and "not a directory" in found["store"]["summary"]


def test_a_home_with_dollar_brace_fails_as_run_claude_refuses_it(both, capsys, fast_mcp, monkeypatch):
    """Reviewers A and B, PR #258: doctor said the home could be created and "Ready"; ``contexto run
    claude`` exits 2 (``launcher.check_mcp_values``, which doctor now asks)."""
    home = both.tmp / "a${b}"
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    code, found = checks(capsys)
    store = found["store"]
    assert code == 1 and store["status"] == "fail" and "${" in store["summary"] and "${" in store["action"], store
    refused = launch(both, "claude", "-p", "hi")
    assert refused.returncode == 2 and "contains '${'" in refused.stderr
    assert not home.exists()


def test_a_home_with_dollar_brace_is_fine_for_codex_alone(world, capsys, fast_mcp, monkeypatch):
    # `contexto run codex` writes no MCP config that Claude Code expands, and takes such a home.
    fake_cli(world, "codex", f"codex-cli {TESTED_CODEX}")
    monkeypatch.setenv("CONTEXTO_HOME", str(world.tmp / "a${b}"))
    code, found = checks(capsys)
    assert code == 0 and found["store"]["status"] == "pass"
    started = launch(world, "codex", "exec", "hi", CONTEXTO_CODEX_KEY_HELPER="echo k",
                     CONTEXTO_CODEX_UPSTREAM="http://127.0.0.1:9/v1")
    assert started.returncode == 0, started.stderr


# Faults in the environment --------------------------------------------------------------------------


def test_a_deleted_working_directory_is_a_failed_line_that_says_so(both, capsys, monkeypatch):
    """Reviewer B, PR #258: ``Path.cwd()`` ran outside the guard, so this was a traceback."""
    gone = both.tmp / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    code, found = checks(capsys)
    assert code == 1
    for check_id in ("settings", "claude-upstream", "codex-upstream", "claude-key", "codex-key", "mcp", "provider"):
        check = found[check_id]
        assert check["status"] == "fail" and check["details"] == {"error": "no_working_directory"}, check
        assert "working directory" in check["summary"] and check["action"].startswith("cd ")
        assert "issues" not in check["action"]  # a fault in the setup, not a bug to report
    assert found["python"]["status"] == found["store"]["status"] == "pass"


def test_no_home_directory_is_a_failed_line_that_says_so(both, capsys, fast_mcp, monkeypatch):
    def no_entry(uid):
        raise KeyError(uid)

    monkeypatch.delenv("HOME")
    monkeypatch.setattr(pwd, "getpwuid", no_entry)
    code, found = checks(capsys)
    assert code == 1
    assert found["claude-upstream"]["details"] == {"error": "no_home_directory"}
    assert "HOME" in found["claude-upstream"]["summary"] and "Set HOME" in found["claude-upstream"]["action"]
    assert found["store"]["status"] == "pass"  # CONTEXTO_HOME names it
    monkeypatch.delenv("CONTEXTO_HOME")
    _, found = checks(capsys)
    store = found["store"]
    assert store["status"] == "fail" and store["details"] == {"error": "no_store_home"}, store
    assert "CONTEXTO_HOME is not set" in store["summary"] and "Set CONTEXTO_HOME" in store["action"]
    assert "issues" not in store["action"]


def test_no_temporary_directory_is_named_with_what_to_set(both, capsys, monkeypatch):
    def none_usable(*args, **kwargs):
        raise FileNotFoundError(errno.ENOENT, "No usable temporary directory found")

    monkeypatch.setattr(doctor.tempfile, "mkdtemp", none_usable)
    code, found = checks(capsys)
    mcp = found["mcp"]
    assert code == 1 and mcp["status"] == "fail" and mcp["details"] == {"error": "no_temp_directory"}, mcp
    assert "temporary directory" in mcp["summary"] and "TMPDIR" in mcp["action"] and "issues" not in mcp["action"]


# Settings files -------------------------------------------------------------------------------------


def settings_line(capsys) -> tuple[int, dict, str]:
    code, out, _ = run(capsys)
    _, found = checks(capsys)
    return code, found["settings"], line_of(out, "Claude Code settings")


def test_settings_files_that_are_read_are_named_by_scope(both, capsys, fast_mcp):
    code, check, line = settings_line(capsys)
    assert check["status"] == "pass" and "no settings file" in line
    assert check["details"] == {"files": [{"kind": kind, "state": "absent"} for kind in ("local", "project", "user")]}
    write_settings(both.home / ".claude" / "settings.json", {})
    (both.cwd / ".claude").mkdir()
    (both.cwd / ".claude" / "settings.json").write_bytes(b"\xef\xbb\xbf{}")  # a byte order mark is fine
    code, check, line = settings_line(capsys)
    assert check["status"] == "pass" and line.endswith("read .claude/settings.json and ~/.claude/settings.json")
    assert [f["state"] for f in check["details"]["files"]] == ["absent", "read", "read"]
    assert str(both.home) not in line and str(both.cwd) not in line


@pytest.mark.parametrize("fault,problem", [
    ("not-json", "cannot be read as JSON"), ("not-utf8", "cannot be read as JSON"), ("list", "is not a JSON object"),
    ("env-list", "env block"), ("directory", "is a directory"), ("mode-000", "Permission denied")])
@pytest.mark.parametrize("scope", ["user", "project"])
def test_an_unusable_settings_file_is_a_warning_that_names_it_and_what_is_wrong(both, capsys, fast_mcp, scope,
                                                                                 fault, problem):
    """Reviewer B, PR #258: with a broken file that sets a base URL, doctor said "nothing sets
    ANTHROPIC_BASE_URL" and nothing more. That is what the launcher does with the file, so the other
    checks stay as they are, and this one names the file."""
    if fault == "mode-000" and os.geteuid() == 0:
        pytest.skip("root can read anything")
    path = (both.home if scope == "user" else both.cwd) / ".claude" / "settings.json"
    name = "~/.claude/settings.json" if scope == "user" else ".claude/settings.json"
    usable = json.dumps({"env": {"ANTHROPIC_BASE_URL": f"https://broken.example/{URL_SECRET}"}})
    path.parent.mkdir(parents=True)
    if fault == "directory":
        path.mkdir()
    else:
        path.write_bytes({"not-json": (usable + ",").encode(), "not-utf8": b"\xff\xfe" + usable.encode(),
                          "list": b"[]", "env-list": b'{"env": []}', "mode-000": usable.encode()}[fault])
    if fault == "mode-000":
        path.chmod(0)
    try:
        code, check, line = settings_line(capsys)
        _, found = checks(capsys)
    finally:
        path.chmod(0o700)
    assert code == 0 and check["status"] == "warn" and line.startswith("[warn]"), line
    assert f"{name} " in line and problem in line and str(path.parent.parent) not in line
    assert "Fix or remove it" in check["action"] and URL_SECRET not in json.dumps(check)
    assert {"kind": scope, "state": "unusable", "problem": check["details"]["files"][-1 if scope == "user" else 1]
            ["problem"]} in check["details"]["files"]
    assert found["claude-upstream"]["details"]["source"] == "default"  # as the launcher reads it


def test_a_fifo_in_place_of_a_settings_file_is_named_without_being_opened(both):
    path = both.cwd / ".claude" / "settings.json"
    path.parent.mkdir()
    os.mkfifo(path)
    started = time.monotonic()
    assert "FIFO" in doctor.settings_problem(path)  # opening it would block until something writes to it
    assert time.monotonic() - started < 5


# The MCP server -------------------------------------------------------------------------------------


def test_the_real_server_starts_and_lists_the_five_tools(both, capsys, monkeypatch):
    scratch = both.tmp / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", None)  # re-read TMPDIR
    code, found = checks(capsys)
    mcp = found["mcp"]
    assert code == 0 and mcp["status"] == "pass", mcp
    assert mcp["details"]["tools"] == sorted(t["name"] for t in TOOLS) and len(mcp["details"]["tools"]) == 5
    assert mcp["details"]["missing"] == mcp["details"]["unexpected"] == []
    assert 0 < mcp["details"]["seconds"] < doctor.MCP_TIMEOUT_S
    assert list(scratch.iterdir()) == []  # the temporary home is gone
    assert not both.store.exists()  # and the user's store was never opened


def fake_server(tmp: Path, body: str) -> list[str]:
    script = tmp / "fake_mcp.py"
    script.write_text(body)
    return [sys.executable, str(script)]


ANSWERING = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {}, "serverInfo": {"name": "contexto"}}
    elif message.get("method") == "tools/list":
        result = {"tools": [{"name": n} for n in NAMES]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
'''


def mcp_check(world, command, timeout=doctor.MCP_TIMEOUT_S):
    return doctor.check_mcp(dict(os.environ), world.cwd, command=command, timeout=timeout)


def test_a_server_missing_a_tool_or_listing_another_fails_and_names_it(world):
    names = [t["name"] for t in TOOLS]
    fewer = mcp_check(world, fake_server(world.tmp, f"NAMES = {names[:-1]!r}\n{ANSWERING}"))
    assert fewer.status == doctor.FAIL and names[-1] in fewer.summary
    assert fewer.details["missing"] == [names[-1]] and fewer.action
    more = mcp_check(world, fake_server(world.tmp, f"NAMES = {[*names, 'contexto_other']!r}\n{ANSWERING}"))
    assert more.status == doctor.FAIL and more.details["unexpected"] == ["contexto_other"]
    same = mcp_check(world, fake_server(world.tmp, f"NAMES = {names!r}\n{ANSWERING}"))
    assert same.status == doctor.PASS


def test_a_server_that_exits_fails_with_its_last_error_line(world):
    command = fake_server(world.tmp, "import sys\nprint('first line', file=sys.stderr)\n"
                                     "print('No module named contexto', file=sys.stderr)\nsys.exit(1)\n")
    check = mcp_check(world, command)
    assert check.status == doctor.FAIL
    assert "exited with code 1" in check.summary and "No module named contexto" in check.summary
    assert "first line" not in check.summary and "-P -m contexto" in check.action


def test_a_server_that_never_answers_is_stopped_at_the_timeout(world, monkeypatch):
    children = []
    real = doctor.subprocess.Popen

    def spy(argv, **kwargs):
        children.append(real(argv, **kwargs))
        return children[-1]

    monkeypatch.setattr(doctor.subprocess, "Popen", spy)
    started = time.monotonic()
    check = mcp_check(world, fake_server(world.tmp, "import time\ntime.sleep(120)\n"), timeout=1.0)
    assert time.monotonic() - started < 60
    assert check.status == doctor.FAIL and "did not answer within 1 s" in check.summary
    assert check.details["seconds"] >= 1.0
    assert len(children) == 1 and children[0].returncode == -9  # killed and reaped, not left running


HOLDING = r'''
import subprocess, sys, time
held = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({HOLD})"], start_new_session=NEW_SESSION)
with open(PIDFILE + ".tmp", "w") as f:
    f.write(str(held.pid))
import os
os.replace(PIDFILE + ".tmp", PIDFILE)
time.sleep(120)
'''


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not alive(pid)


@pytest.mark.parametrize("new_session", [False, True])
def test_the_bound_holds_when_the_server_leaves_a_child_holding_its_pipes(world, new_session):
    """Reviewer B, PR #258: after the kill, ``communicate`` waited for the pipes' end of file, which the
    server's own child put off: 8.1 s against a 1 s timeout. A child in the server's process group is
    killed with it; one that left the group is not waited for."""
    hold, timeout = 45, 5.0
    pidfile = world.tmp / "held.pid"
    body = (f"HOLD = {hold}\nNEW_SESSION = {new_session}\nPIDFILE = {str(pidfile)!r}\n{HOLDING}")
    started = time.monotonic()
    check = mcp_check(world, fake_server(world.tmp, body), timeout=timeout)
    elapsed = time.monotonic() - started
    held = int(pidfile.read_text())
    try:
        assert check.status == doctor.FAIL and "did not answer within 5 s" in check.summary
        assert elapsed < timeout + 20, elapsed  # not the child's 45 s
        if new_session:  # it left the group, so the kill did not reach it, and nothing waited for it
            assert not gone_within(held, 0.5)
        else:  # in the server's group: killed with it
            assert gone_within(held, 10)
    finally:
        if alive(held):
            os.kill(held, signal.SIGKILL)


HANGING_SERVER = '''
import os, time
if "contexto-doctor-" in os.environ.get("CONTEXTO_HOME", ""):  # the MCP server doctor starts, and only it
    with open(os.environ["HANG_MARKER"] + ".tmp", "w") as f:
        f.write(str(os.getpid()))
    os.replace(os.environ["HANG_MARKER"] + ".tmp", os.environ["HANG_MARKER"])
    time.sleep(120)
'''


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_a_terminating_signal_during_the_mcp_check_leaves_nothing_behind(both, signum):
    """Reviewers A and B, PR #258: SIGTERM or SIGHUP (a closed terminal) while the MCP server ran left
    the ``contexto-doctor-*`` home in the temporary directory and a hung server running. Here the server
    hangs (a ``sitecustomize`` on its path sleeps), doctor gets the signal, and both must be gone."""
    planted, scratch, marker = both.tmp / "planted", both.tmp / "scratch", both.tmp / "server.pid"
    planted.mkdir()
    scratch.mkdir()
    (planted / "sitecustomize.py").write_text(HANGING_SERVER)
    env = {**os.environ, "TMPDIR": str(scratch), "HANG_MARKER": str(marker),
           "PYTHONPATH": os.pathsep.join(filter(None, [str(planted), os.environ.get("PYTHONPATH")]))}
    proc = subprocess.Popen([sys.executable, "-m", "contexto", "doctor"], cwd=both.cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert time.monotonic() < deadline and proc.poll() is None, "the MCP server never started"
            time.sleep(0.05)
        server = int(marker.read_text())
        assert [p.name for p in scratch.iterdir() if p.name.startswith("contexto-doctor-")]  # its home is there
        proc.send_signal(signum)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 128 + signum, (proc.returncode, err)
    assert "Traceback" not in err and "Store home" in out and "MCP server" not in out and "eady: " not in out
    assert [p.name for p in scratch.iterdir()] == []  # the temporary home is gone
    assert gone_within(server, 10)  # and the server was killed, not orphaned
    assert not both.store.exists()


def test_a_server_that_cannot_be_started_or_talks_nonsense_fails(world):
    missing = mcp_check(world, [str(world.tmp / "no-such-python")])
    assert missing.status == doctor.FAIL and "cannot be started" in missing.summary
    nonsense = mcp_check(world, fake_server(world.tmp, "import sys\nsys.stdin.read()\nprint('not json')\n"))
    assert nonsense.status == doctor.FAIL and "did not list its tools" in nonsense.summary


def test_the_server_is_started_the_way_the_launcher_starts_it(world, monkeypatch):
    from contexto import launcher

    seen = {}
    real = doctor.subprocess.Popen

    def spy(argv, **kwargs):
        seen["argv"], seen["env"], seen["cwd"] = list(argv), kwargs["env"], kwargs["cwd"]
        return real(argv, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "Popen", spy)
    check = doctor.check_mcp(dict(os.environ), world.cwd)
    assert check.status == doctor.PASS, check
    home = Path(seen["env"]["CONTEXTO_HOME"])
    payload = launcher.mcp_payload(doctor.MCP_SESSION, home)["mcpServers"]["contexto"]
    assert seen["argv"] == [sys.executable, "-P", "-m", "contexto", "mcp", f"--session={doctor.MCP_SESSION}"]
    assert seen["argv"] == [payload["command"], *payload["args"]]
    assert home != world.store and not home.exists()  # a temporary home, removed afterwards
    assert seen["cwd"] == world.cwd  # the project directory, where the CLI starts it


# Provider variables ---------------------------------------------------------------------------------


def test_no_provider_variable_passes(both, capsys, fast_mcp, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "0")
    _, found = checks(capsys)
    assert found["provider"]["status"] == "pass" and found["provider"]["details"] == {"variables": []}


def test_a_provider_variable_in_the_environment_is_a_warning_naming_it(both, capsys, fast_mcp, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    code, found = checks(capsys)
    provider = found["provider"]
    assert code == 0 and provider["status"] == "warn"
    assert "CLAUDE_CODE_USE_VERTEX is set in the environment" in provider["summary"]
    assert "Google Vertex AI" in provider["summary"] and "Unset CLAUDE_CODE_USE_VERTEX" in provider["action"]
    assert provider["details"] == {"provider": "Google Vertex AI",
                                   "variables": [{"variable": "CLAUDE_CODE_USE_VERTEX", "source": "env"}]}


def test_a_provider_variable_in_a_settings_env_block_names_the_file(both, capsys, fast_mcp):
    write_settings(both.cwd / ".claude" / "settings.local.json", {"env": {"CLAUDE_CODE_USE_BEDROCK": "true"}})
    _, found = checks(capsys)
    provider = found["provider"]
    assert provider["status"] == "warn"
    assert "CLAUDE_CODE_USE_BEDROCK is set in the env block of .claude/settings.local.json" in provider["summary"]
    assert provider["details"]["variables"] == [{"variable": "CLAUDE_CODE_USE_BEDROCK", "source": "settings:local"}]


def test_a_project_variable_under_ignored_trust_is_a_warning_that_says_may(both, capsys, fast_mcp, monkeypatch):
    """Reviewer B, PR #258: with CLAUDE_CODE_IGNORE_PERSISTED_TRUST set, a project file that turns a
    provider variable on got no warning. Claude Code -p drops it; interactively it may apply (D52)."""
    monkeypatch.setenv("CLAUDE_CODE_IGNORE_PERSISTED_TRUST", "1")
    write_settings(both.cwd / ".claude" / "settings.json", {"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}})
    code, found = checks(capsys)
    provider = found["provider"]
    assert code == 0 and provider["status"] == "warn"
    assert "CLAUDE_CODE_USE_BEDROCK is set in the env block of .claude/settings.json" in provider["summary"]
    assert "may apply it once its trust dialog is accepted" in provider["summary"]
    assert provider["details"] == {"provider": "Amazon Bedrock", "variables": [
        {"variable": "CLAUDE_CODE_USE_BEDROCK", "source": "settings:project", "after_trust_dialog": True}]}


# No network -----------------------------------------------------------------------------------------

# Every socket event in this process while WATCHING is a list: sockets made, names resolved (also by
# gethostbyname, which the first version's patches of socket.socket and getaddrinfo did not see;
# reviewer A, PR #258), connections, datagrams. An audit hook cannot be removed, so it is added once.
WATCHING: list[tuple[str, str]] | None = None


def _watch_sockets(event: str, args: tuple) -> None:
    if WATCHING is not None and event.startswith("socket."):
        WATCHING.append((event, repr(args)[:80]))


sys.addaudithook(_watch_sockets)

# The same hook for the Python processes doctor starts (the MCP server), through a sitecustomize on
# their path: each event is appended to the file SOCKET_AUDIT names.
CHILD_AUDIT = '''
import os, sys


def _watch(event, args, path=os.environ.get("SOCKET_AUDIT")):
    if path and event.startswith("socket."):
        with open(path, "a") as f:
            f.write(f"{os.getpid()} {event} {args!r}"[:200] + "\\n")


sys.addaudithook(_watch)
'''


def test_the_socket_watch_sees_a_name_lookup_and_a_connection():
    """The control for the test below: both ways the first version missed or caught are recorded."""
    global WATCHING
    WATCHING = []
    try:
        socket.gethostbyname("127.0.0.1")
        try:
            socket.create_connection(("127.0.0.1", 9), timeout=0.2).close()
        except OSError:
            pass
        seen = {event for event, _ in WATCHING}
    finally:
        WATCHING = None
    assert {"socket.gethostbyname", "socket.getaddrinfo", "socket.connect"} <= seen


def test_doctor_connects_to_nothing_resolves_no_name_and_never_contacts_the_upstream(both, capsys, monkeypatch):
    """The upstream it reports is a listening socket here: nothing may connect to it, from doctor or from
    the MCP server it starts. In this process and in that server, every socket event is recorded: a
    connection, a datagram, and a name lookup by any route (the upstreams are host names, which only a
    lookup would turn into addresses)."""
    global WATCHING
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.05)
    port = listener.getsockname()[1]
    accepted: list[object] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                accepted.append(listener.accept()[1])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    planted, audit = both.tmp / "planted", both.tmp / "child-sockets.log"
    planted.mkdir()
    (planted / "sitecustomize.py").write_text(CHILD_AUDIT)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(planted), os.environ.get("PYTHONPATH")])))
    monkeypatch.setenv("SOCKET_AUDIT", str(audit))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://localhost:{port}")
    monkeypatch.setenv("CONTEXTO_CODEX_UPSTREAM", f"http://localhost:{port}/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    write_settings(both.cwd / ".claude" / "settings.json", {"env": {"ANTHROPIC_BASE_URL": f"http://localhost:{port}"}})
    WATCHING = []
    try:
        for argv in ((), ("--json",)):
            code, out, _ = run(capsys, *argv)
            assert code == 0, out
            assert f"http://localhost:{port}" in out  # reported, not contacted
            assert "MCP server started and listed its 5 tools" in out  # the real server ran, under the hook
        time.sleep(0.2)
        seen = list(WATCHING)
    finally:
        WATCHING = None
        stop.set()
        thread.join(timeout=2)
        listener.close()
    assert seen == [] and accepted == []
    assert audit.exists() is False or audit.read_text() == ""  # nor did the MCP server touch a socket
