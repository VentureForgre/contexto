"""``contexto doctor`` (D64): what it checks, what it prints, and what it must never do.

Everything is synthetic: fake ``claude`` and ``codex`` scripts that only answer ``--version``, settings
files and a store home under ``tmp_path``, and a temporary ``HOME``. The MCP check starts the real
server from this checkout. Nothing here contacts a model or an upstream; two tests assert that.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from contexto import cli
from contexto.cliversions import TESTED_VERSIONS
from contexto.commands import doctor
from contexto.mcp import TOOLS

CHECK_IDS = ["python", "claude", "codex", "claude-upstream", "codex-upstream", "claude-key", "codex-key",
             "store", "mcp", "provider"]
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
    assert lines[-1] == "Ready: 10 passed."
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
    assert code == 130 and out == "" and err.strip() == "contexto: interrupted"


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


def test_a_key_helper_is_reported_and_never_run_or_shown(both, capsys, fast_mcp):
    marker = both.tmp / "HELPER-RAN"
    helper = f"sh -c 'touch {marker}; echo {HELPER_SECRET}'"
    write_settings(both.home / ".claude" / "settings.json", {"apiKeyHelper": helper})
    code, out, _ = run(capsys)
    _, json_out, _ = run(capsys, "--json")
    found = {c["id"]: c for c in json.loads(json_out)["checks"]}
    assert not marker.exists()  # doctor never runs a helper
    for text in (out, json_out):
        assert HELPER_SECRET not in text and "touch" not in text  # nor prints the command, which can hold a key
    assert found["claude-key"]["details"] == {"found": [{"name": "apiKeyHelper", "source": "settings:user"}]}
    assert found["codex-key"]["status"] == "pass"
    assert found["codex-key"]["details"] == {"name": "apiKeyHelper", "source": "settings:user"}
    assert "not run" in line_of(out, "Codex key helper")


def test_codex_key_helper_from_its_own_variable(both, capsys, fast_mcp, monkeypatch):
    marker = both.tmp / "HELPER-RAN"
    monkeypatch.setenv("CONTEXTO_CODEX_KEY_HELPER", f"touch {marker}; echo {HELPER_SECRET}")
    code, out, _ = run(capsys, "--json")
    found = {c["id"]: c for c in json.loads(out)["checks"]}
    assert found["codex-key"]["status"] == "pass" and not marker.exists() and HELPER_SECRET not in out
    assert found["codex-key"]["details"] == {"name": "CONTEXTO_CODEX_KEY_HELPER", "source": "env"}


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
@pytest.mark.parametrize("where", ["home", "sessions", "parent"])
def test_a_home_that_cannot_be_written_fails_and_is_left_as_it_was(both, capsys, fast_mcp, monkeypatch, where):
    locked = {"home": both.store, "sessions": both.store / "sessions", "parent": both.tmp / "locked"}[where]
    if where == "parent":
        locked.mkdir()
        monkeypatch.setenv("CONTEXTO_HOME", str(locked / "deep" / "store"))
    elif where == "sessions":
        (both.store / "sessions").mkdir(parents=True)
    else:
        both.store.mkdir()
    locked.chmod(0o500)
    try:
        code, found = checks(capsys)
        assert code == 1 and found["store"]["status"] == "fail", found["store"]
        assert found["store"]["action"]
        assert stat.S_IMODE(locked.stat().st_mode) == 0o500 and list(locked.iterdir()) == []
    finally:
        locked.chmod(0o700)


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


# No network -----------------------------------------------------------------------------------------


def test_doctor_connects_to_nothing_and_never_contacts_the_upstream(both, capsys, monkeypatch):
    """The upstream it reports is a listening socket here: nothing may connect to it, from doctor or from
    the MCP server it starts. In this process, every way a socket connects is made to fail the test."""
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
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("CONTEXTO_CODEX_UPSTREAM", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    connects: list[object] = []

    class Watched(socket.socket):
        def connect(self, address):
            connects.append(address)
            raise AssertionError(f"doctor connected to {address!r}")

        def connect_ex(self, address):
            connects.append(address)
            raise AssertionError(f"doctor connected to {address!r}")

    def refuse(*args, **kwargs):
        connects.append(args)
        raise AssertionError(f"doctor resolved or connected: {args!r}")

    monkeypatch.setattr(socket, "socket", Watched)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    try:
        for argv in ((), ("--json",)):
            code, out, _ = run(capsys, *argv)
            assert code == 0, out
            assert f"http://127.0.0.1:{port}" in out  # reported, not contacted
        time.sleep(0.2)
    finally:
        stop.set()
        thread.join(timeout=2)
        listener.close()
    assert connects == [] and accepted == []
