"""``contexto doctor`` (D64): what it checks, what it prints, and what it must never do.

Everything is synthetic: fake ``claude`` and ``codex`` scripts that only answer ``--version``, settings
files and a store home under ``tmp_path``, and a temporary ``HOME``. The MCP check starts the real
server from this checkout. Nothing here contacts a model or an upstream; the last test asserts that,
with a socket audit hook in every Python process of a doctor run.
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
from contexto.upstream import CLOUD_PROVIDER_KEYS

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
    monkeypatch.setattr(doctor, "installed_version",
                        lambda path, env=None, **kw: real_version(path, env=env, timeout=60, **kw))
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
    assert code_text == code_json == 1  # the provider check fails, as `contexto run claude` refuses (D70)
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


def test_a_claude_that_cannot_be_started_still_gets_the_provider_check(world, capsys, fast_mcp, monkeypatch):
    """Reviewer A, PR #258 round 5: the launcher stops early only for a `claude` that is not on PATH, so
    one that is there but cannot start still meets the refusal. Doctor skipped the check and exited 0
    while `contexto run claude` exited 2."""
    fake_cli(world, "claude", None, body="#!/nonexistent/interpreter\n")
    fake_cli(world, "codex", f"codex-cli {TESTED_CODEX}")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    code, found = checks(capsys)
    assert code == 1 and found["claude"]["status"] == "warn" and found["provider"]["status"] == "fail"
    assert [found[i]["status"] for i in ("claude-upstream", "claude-key")] == ["skip"] * 2


def test_a_missing_cli_names_its_install_command_once(world, capsys, fast_mcp):
    """PR #258 review (both): since #257 the line carries the install command and "If it is installed, add
    its directory to PATH", and the next step said "Install Codex and put it on PATH" again."""
    fake_cli(world, "claude", f"{TESTED_CLAUDE} (Claude Code)")
    _, out, _ = run(capsys)
    line = line_of(out, "Codex: ")
    assert line == ("[warn] Codex: 'codex' not found on PATH. Install Codex: curl -fsSL https://chatgpt.com/codex/"
                    "install.sh | sh (other ways: https://learn.chatgpt.com/docs/codex/cli). If it is installed, add "
                    "its directory to PATH.")
    step = out.splitlines()[out.splitlines().index(line) + 1]
    assert step.strip() == "-> Only `contexto run codex` needs it"
    assert out.count("Install Codex") == 1


@pytest.mark.parametrize("kind", ["not executable", "broken symlink", "directory"])
def test_the_provider_skip_line_agrees_with_the_claude_line(world, capsys, fast_mcp, kind):
    """PR #258 review (both): for a `claude` on PATH that is not executable or a broken symlink the skip line
    said "claude is not on PATH", four lines under a line that names it on PATH."""
    fake_cli(world, "codex", f"codex-cli {TESTED_CODEX}")
    target = world.bin / "claude"
    if kind == "not executable":
        target.write_text("#!/bin/sh\necho 2.1.318\n")  # mode 0644
    elif kind == "broken symlink":
        target.symlink_to(world.tmp / "nowhere")
    else:
        target.mkdir()
    code, found = checks(capsys)
    assert code == 0 and found["provider"]["status"] == "skip"
    assert found["provider"]["summary"] == "Cloud provider: not checked, claude is not on PATH or not executable"
    assert "not on PATH" not in found["claude"]["summary"]
    target.unlink() if kind != "directory" else target.rmdir()  # missing: the same words, also true
    _, found = checks(capsys)
    assert found["provider"]["summary"] == "Cloud provider: not checked, claude is not on PATH or not executable"


def test_without_codex_support_the_codex_checks_say_so_and_are_not_a_bug(both, capsys, fast_mcp, monkeypatch):
    """PR #283 reviews A and B: with contexto.codex missing and `codex` on PATH, the two Codex checks failed
    as "the check itself failed (ModuleNotFoundError)" with "Report it at ...". `contexto run codex` says
    "codex support is not installed in this build" and exits 2, so doctor now says that."""
    monkeypatch.setitem(sys.modules, "contexto.codex", None)  # behaves as not installed
    code, found = checks(capsys)
    assert code == 0  # Claude Code is usable, so this is a warning, as a missing codex CLI is
    assert found["codex"]["status"] == "warn"
    assert found["codex"]["summary"].endswith(", but codex support is not installed in this build")
    assert found["codex"]["action"] == "Reinstall Contexto to use `contexto run codex`; until then it exits 2"
    for check_id, label in (("codex-upstream", "Codex upstream"), ("codex-key", "Codex key helper")):
        assert found[check_id]["status"] == "skip"
        assert found[check_id]["summary"] == f"{label}: not checked, codex support is not installed in this build"
    assert not any("Report it" in (c["action"] or "") for c in found.values())
    _, out, _ = run(capsys)
    assert "ModuleNotFoundError" not in out
    # And with Claude Code unusable too, its step does not tell the user to install Codex, which is there
    # (Reviewer B, PR #290).
    (both.bin / "claude").unlink()
    fake_cli(both, "claude", None, body="#!/nonexistent/interpreter\n")
    code, found = checks(capsys)
    assert code == 1 and found["claude"]["status"] == found["codex"]["status"] == "fail"
    assert found["claude"]["action"].startswith("Install Claude Code and put it on PATH")
    assert "no Codex support" in found["claude"]["action"] and "or Codex" not in found["claude"]["action"]
    assert found["codex"]["action"] == "Reinstall Contexto to use `contexto run codex`; until then it exits 2"


def test_without_codex_support_or_a_usable_claude_both_cli_checks_fail(world, capsys, fast_mcp, monkeypatch):
    fake_cli(world, "codex", f"codex-cli {TESTED_CODEX}")
    monkeypatch.setitem(sys.modules, "contexto.codex", None)
    code, found = checks(capsys)
    assert code == 1 and found["claude"]["status"] == found["codex"]["status"] == "fail"
    assert "at least one" in found["claude"]["action"] and found["codex"]["action"].startswith("Reinstall Contexto")


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


def test_an_upstream_that_is_this_home_s_service_fails_as_the_launcher_refuses_it(both, capsys, fast_mcp,
                                                                                   monkeypatch):
    """D70: ``contexto run claude`` refuses an upstream that is this home's ``contexto serve``
    (``launcher.check_not_service``), the base URL docs/serve.md puts in Claude Code's settings. Doctor
    fails the same setup, and neither its text nor its JSON holds the token in the URL."""
    import os
    from pathlib import Path

    store = Path(os.environ["CONTEXTO_HOME"])
    store.mkdir(parents=True, exist_ok=True)
    token = "Tk" + "x" * 40
    (store / "serve.token").write_text(token + "\n")
    os.chmod(store / "serve.token", 0o600)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:7837/{token}")
    code, out, _ = run(capsys)
    _, json_out, _ = run(capsys, "--json")
    found = {c["id"]: c for c in json.loads(json_out)["checks"]}
    upstream = found["claude-upstream"]
    assert code != 0 and upstream["status"] == "fail"
    assert "contexto serve" in upstream["summary"] and "--upstream" in upstream["action"]
    assert upstream["details"] == {"origin": "http://127.0.0.1:7837", "source": "env"}
    for text in (out, json_out):
        assert token not in text
    # Another token, or none on disk, is an ordinary loopback upstream.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:7837/" + "y" * 42)
    _, found = checks(capsys)
    assert found["claude-upstream"]["status"] == "pass"


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
    assert check["details"] == {"files": [{"kind": kind, "state": "absent"}
                                          for kind in ("local", "project", "user", "managed")]}
    write_settings(both.home / ".claude" / "settings.json", {})
    (both.cwd / ".claude").mkdir()
    (both.cwd / ".claude" / "settings.json").write_bytes(b"\xef\xbb\xbf{}")  # a byte order mark is fine
    code, check, line = settings_line(capsys)
    assert check["status"] == "pass" and line.endswith("read .claude/settings.json and ~/.claude/settings.json")
    assert [f["state"] for f in check["details"]["files"]] == ["absent", "read", "read", "absent"]
    assert str(both.home) not in line and str(both.cwd) not in line


@pytest.mark.parametrize("fault,problem", [
    ("not-json", "is not valid JSON"), ("not-utf8", "is not valid JSON"),
    ("env-list", "has an env that is not a JSON object, so Claude Code loads nothing from it")])
@pytest.mark.parametrize("scope", ["user", "project"])
def test_an_unusable_settings_file_is_a_warning_that_names_it_and_what_is_wrong(both, capsys, fast_mcp, scope,
                                                                                 fault, problem):
    """Reviewer B, PR #258: with a broken file that sets a base URL, doctor said "nothing sets
    ANTHROPIC_BASE_URL" and nothing more. That is what the launcher does with the file, so the other
    checks stay as they are, and this one names the file."""
    path = (both.home if scope == "user" else both.cwd) / ".claude" / "settings.json"
    name = "~/.claude/settings.json" if scope == "user" else ".claude/settings.json"
    usable = json.dumps({"env": {"ANTHROPIC_BASE_URL": f"https://broken.example/{URL_SECRET}"}})
    path.parent.mkdir(parents=True)
    path.write_bytes({"not-json": (usable + ",").encode(), "not-utf8": b"\xfe\xff" + usable.encode(),
                      "env-list": b'{"env": []}'}[fault])
    code, check, line = settings_line(capsys)
    _, found = checks(capsys)
    assert code == 0 and check["status"] == "warn" and line.startswith("[warn]"), line
    assert f"{name} {problem}" in line and str(path.parent.parent) not in line
    assert "Fix or remove it" in check["action"] and URL_SECRET not in json.dumps(check)
    assert "refuses" not in check["action"]  # none of these can turn a switch on (reviewer B, round 5)
    assert {"kind": scope, "state": "unusable", "problem": problem} in check["details"]["files"]
    assert found["claude-upstream"]["details"]["source"] == "default"  # as the launcher reads it
    assert found["provider"]["status"] == "pass"  # the file cannot name a provider switch, so the launch goes on


@pytest.mark.parametrize("fault,state", [("directory", "absent"), ("mode-000", "absent"), ("list", "read")])
def test_a_settings_file_claude_code_cannot_read_either_is_absent(both, capsys, fast_mcp, fault, state):
    """``contexto.upstream.read_settings_file``: a directory and a file this user cannot open are absent,
    since Claude Code runs as the same user and cannot read them either; a document that is not an
    object sets nothing. None of them stops `contexto run claude` (#271, D70)."""
    if fault == "mode-000" and os.geteuid() == 0:
        pytest.skip("root can read anything")
    path = both.cwd / ".claude" / "settings.json"
    path.parent.mkdir()
    if fault == "directory":
        path.mkdir()
    else:
        path.write_text({"list": "[]", "mode-000": json.dumps({"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}})}[fault])
    if fault == "mode-000":
        path.chmod(0)
    try:
        code, check, _ = settings_line(capsys)
        _, found = checks(capsys)
    finally:
        path.chmod(0o700)
    assert code == 0 and check["status"] == "pass"
    assert check["details"]["files"][1] == {"kind": "project", "state": state}
    assert found["provider"]["status"] == "pass"


def test_a_settings_symlink_that_leads_nowhere_is_an_absent_file(both, capsys, fast_mcp):
    (both.cwd / ".claude").mkdir()
    (both.cwd / ".claude" / "settings.json").symlink_to(both.tmp / "no-such-file")
    _, check, line = settings_line(capsys)
    assert check["status"] == "pass" and "no settings file" in line
    assert check["details"]["files"][1] == {"kind": "project", "state": "absent"}


def test_the_managed_settings_file_is_read_and_named_as_the_guard_reads_it(both, capsys, fast_mcp, monkeypatch):
    """The cloud-provider guard reads Claude Code's managed settings file (D70), so the settings line
    names it too."""
    from contexto import upstream

    managed = both.tmp / "managed-settings.json"
    managed.write_text("{")
    monkeypatch.setitem(upstream.MANAGED_SETTINGS_FILES, sys.platform, str(managed))
    _, check, line = settings_line(capsys)
    assert check["status"] == "warn"
    assert "Claude Code's managed settings file (managed-settings.json) is not valid JSON" in line
    assert check["details"]["files"][-1] == {"kind": "managed", "state": "unusable", "problem": "is not valid JSON"}
    managed.write_text("{}")
    _, check, line = settings_line(capsys)
    assert check["status"] == "pass"
    assert line.endswith("read Claude Code's managed settings file (managed-settings.json)")


def test_source_text_names_the_place_and_never_a_path(tmp_path):
    assert doctor.source_text("env") == "the environment"
    assert doctor.source_text("settings:project") == "the env block of .claude/settings.json"
    assert doctor.source_text("settings:local") == "the env block of .claude/settings.local.json"
    assert doctor.source_text("settings:user") == "the env block of ~/.claude/settings.json"
    config = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert doctor.source_text("settings:user", config) == "the env block of $CLAUDE_CONFIG_DIR/settings.json"
    assert str(tmp_path) not in doctor.source_text("settings:user", config)
    assert doctor.source_text("settings:managed") == ("the env block of Claude Code's managed settings file "
                                                      "(managed-settings.json)")


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


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_that_lands_while_the_server_is_being_started_still_stops_it(world, monkeypatch, signum):
    """Seen once in four runs of the test above on a busy machine: SIGHUP arrived while
    ``subprocess.Popen`` was still returning, the exception was raised inside it, and the check never got
    the process it had to kill. Here the signal is sent at exactly that point."""
    children = []
    real = doctor.subprocess.Popen

    def popen_then_signal(argv, **kwargs):
        children.append(real(argv, **kwargs))
        os.kill(os.getpid(), signum)
        time.sleep(0.05)  # the handler runs here, in the main thread, before Popen has returned
        return children[-1]

    monkeypatch.setattr(doctor.subprocess, "Popen", popen_then_signal)
    scratch = world.tmp / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", None)  # re-read TMPDIR
    try:
        with pytest.raises(doctor.Terminated) as stopped, doctor.termination_raises():
            mcp_check(world, fake_server(world.tmp, "import time\ntime.sleep(120)\n"))
        assert stopped.value.signum == signum
        assert len(children) == 1 and children[0].returncode == -signal.SIGKILL  # killed and reaped
        assert list(scratch.iterdir()) == []  # and the temporary home is gone
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
    assert signal.getsignal(signum) is signal.SIG_DFL  # the handlers are put back


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


# Cloud providers ------------------------------------------------------------------------------------


def launcher_message(both) -> str:
    """What `contexto run claude` says from the same directory: the launcher's own check."""
    from contexto.launcher import LaunchError, check_cloud_provider

    with pytest.raises(LaunchError) as raised:
        check_cloud_provider(both.cwd, os.environ, home=both.home)
    assert raised.value.code == 2
    return str(raised.value)


def test_no_switch_on_passes(both, capsys, fast_mcp, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "0")
    _, found = checks(capsys)
    assert found["provider"]["status"] == "pass"
    assert found["provider"]["details"] == {"variables": [], "unread": []}


@pytest.mark.parametrize("variable", list(CLOUD_PROVIDER_KEYS))
def test_a_switch_in_the_environment_fails_as_the_launcher_refuses(both, capsys, fast_mcp, monkeypatch, variable):
    """D70 (#271): `contexto run claude` refuses this launch with exit 2, so doctor fails it, with the
    launcher's words: the summary is ``cloud_provider_note`` and the next step the rest of the message."""
    monkeypatch.setenv(variable, "1")
    code, found = checks(capsys)
    provider = found["provider"]
    assert code == 1 and provider["status"] == "fail"
    assert provider["summary"].startswith(f"{variable} is set in the process environment. With it on, Claude Code "
                                          f"sends its requests to {CLOUD_PROVIDER_KEYS[variable]}")
    assert provider["summary"].endswith("`contexto run claude` refuses to launch (exit 2)")
    assert f"env -u {variable} contexto run" in provider["action"]
    note = provider["summary"].removesuffix(": `contexto run claude` refuses to launch (exit 2)")
    assert f"{note}. {provider['action']}" == launcher_message(both)
    assert provider["details"] == {"variables": [{"variable": variable, "provider": CLOUD_PROVIDER_KEYS[variable],
                                                  "sources": ["env"]}], "unread": []}


def test_a_switch_in_a_settings_env_block_names_the_file(both, capsys, fast_mcp):
    write_settings(both.cwd / ".claude" / "settings.local.json", {"env": {"CLAUDE_CODE_USE_BEDROCK": "true"}})
    code, found = checks(capsys)
    provider = found["provider"]
    assert code == 1 and provider["status"] == "fail"
    assert "CLAUDE_CODE_USE_BEDROCK is set in the env block of .claude/settings.local.json" in provider["summary"]
    assert "remove CLAUDE_CODE_USE_BEDROCK from that env block" in provider["action"]
    assert provider["details"]["variables"] == [{"variable": "CLAUDE_CODE_USE_BEDROCK", "provider": "Amazon Bedrock",
                                                 "sources": ["settings:local"]}]
    assert str(both.cwd) not in json.dumps(provider)


def test_a_project_switch_fails_also_while_persisted_trust_is_ignored(both, capsys, fast_mcp, monkeypatch):
    """#258 warned "may apply" here; the launcher refuses whichever file Claude Code would apply (D70),
    so doctor fails as it does."""
    monkeypatch.setenv("CLAUDE_CODE_IGNORE_PERSISTED_TRUST", "1")
    write_settings(both.cwd / ".claude" / "settings.json", {"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}})
    code, found = checks(capsys)
    assert code == 1 and found["provider"]["status"] == "fail"
    assert found["provider"]["details"]["variables"][0]["sources"] == ["settings:project"]


def test_a_settings_file_that_may_name_a_switch_and_cannot_be_read_fails(both, capsys, fast_mcp):
    """D70 errs toward refusing: a file that is there, could not be read and may turn a switch on."""
    path = both.cwd / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"env": {"CLAUDE_CODE_USE_BEDROCK": "1",}}')
    code, found = checks(capsys)
    provider = found["provider"]
    assert code == 1 and provider["status"] == "fail" and found["settings"]["status"] == "warn"
    assert "refuses to launch while it may turn on a cloud provider" in found["settings"]["action"]
    assert provider["summary"].startswith("Contexto could not read .claude/settings.json (it is not valid JSON)")
    assert provider["details"] == {"variables": [], "unread": [{"source": "settings:project",
                                                                 "problem": "is not valid JSON"}]}
    note = provider["summary"].removesuffix(": `contexto run claude` refuses to launch (exit 2)")
    assert f"{note}. {provider['action']}" == launcher_message(both)


# No network -----------------------------------------------------------------------------------------

# A sitecustomize for every Python process of a doctor run (doctor itself and the MCP server it starts):
# an audit hook appends each socket event to the file SOCKET_AUDIT names. That covers a socket made, a
# connection, a datagram, and a name lookup by any route; the first version patched socket.socket and
# getaddrinfo in the test's process, which a gethostbyname call passed (reviewer A, PR #258).
SOCKET_AUDIT = '''
import os, sys


def _watch(event, args, path=os.environ.get("SOCKET_AUDIT")):
    if path and event.startswith("socket."):
        with open(path, "a") as f:
            f.write(f"{os.getpid()} {event} {args!r}"[:200] + "\\n")


sys.addaudithook(_watch)
'''


def audited(world, *command: str, **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run ``python <command>`` in the world with the socket audit on; the result and the events seen."""
    planted, audit = world.tmp / "planted", world.tmp / "sockets.log"
    planted.mkdir(exist_ok=True)
    (planted / "sitecustomize.py").write_text(SOCKET_AUDIT)
    path = os.pathsep.join(filter(None, [str(planted), os.environ.get("PYTHONPATH")]))
    done = subprocess.run([sys.executable, *command], cwd=world.cwd, capture_output=True, text=True, timeout=120,
                          env={**os.environ, **env, "PYTHONPATH": path, "SOCKET_AUDIT": str(audit)})
    events = audit.read_text().splitlines() if audit.exists() else []
    audit.unlink(missing_ok=True)
    return done, [line.split()[1] for line in events]


def test_the_socket_audit_sees_a_name_lookup_and_a_connection(world):
    """The control for the test below: the audit records the lookup the first version missed."""
    _, events = audited(world, "-c", "import socket; socket.gethostbyname('127.0.0.1')")
    assert events == ["socket.gethostbyname"]
    _, events = audited(world, "-c", "import socket\ntry:\n    socket.create_connection(('127.0.0.1', 9), 0.2)\n"
                                     "except OSError:\n    pass")
    assert {"socket.getaddrinfo", "socket.connect"} <= set(events)


def test_doctor_connects_to_nothing_resolves_no_name_and_never_contacts_the_upstream(both):
    """The upstream doctor reports is a listening socket here, named by host name, so reaching it takes a
    lookup and a connection. Nothing may connect to it, and no process of the run (doctor, the MCP
    server) may touch a socket at all."""
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
    write_settings(both.cwd / ".claude" / "settings.json", {"env": {"ANTHROPIC_BASE_URL": f"http://localhost:{port}"}})
    env = {"CONTEXTO_CODEX_UPSTREAM": f"http://localhost:{port}/v1", "ANTHROPIC_API_KEY": SECRET,
           "CLAUDE_CODE_USE_BEDROCK": "1"}
    try:
        for argv in ((), ("--json",)):
            done, events = audited(both, "-m", "contexto", "doctor", *argv, **env)
            assert done.returncode == 1, done.stdout + done.stderr  # only the provider check fails (D70)
            assert "CLAUDE_CODE_USE_BEDROCK is set" in done.stdout
            assert f"http://localhost:{port}" in done.stdout  # reported, not contacted
            assert "MCP server started and listed its 5 tools" in done.stdout  # the real server ran, audited
            assert events == []
        time.sleep(0.2)
    finally:
        stop.set()
        thread.join(timeout=2)
        listener.close()
    assert accepted == []


# Re-review of the fixes (PR #258) ---------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read and enter any directory")
@pytest.mark.parametrize("mode", [0o000, 0o100, 0o200, 0o300, 0o400, 0o600])
def test_a_sessions_directory_that_cannot_be_read_and_entered_fails_as_the_launcher_does(both, capsys, fast_mcp,
                                                                                         mode):
    """Reviewer B's re-review: doctor passed modes where both launchers exit 1. Round 3 (Reviewer A): which
    modes launch depends on the Python (from 3.14 a missing search permission no longer stops the launch),
    so the test holds doctor to what a real launch does on this Python, mode by mode."""
    sessions = both.store / "sessions"
    sessions.mkdir(parents=True)
    sessions.chmod(mode)
    try:
        code, found = checks(capsys)
        store = found["store"]
        assert stat.S_IMODE(sessions.stat().st_mode) == mode  # doctor changes no mode
        started = launch(both, "claude", "-p", "hi")
        launches = started.returncode == 0
        if launches:
            assert store["status"] != "fail" and "sets it to mode 0700" in store["summary"], (store, started.stderr)
        else:
            assert code == 1 and store["status"] == "fail", (store, started.stderr)
            assert "sessions directory cannot be read and entered" in store["summary"]
            assert store["action"] == f"Run: chmod u+rwx {shlex.quote(str(sessions))}"
        if mode in (0o000, 0o100, 0o200, 0o300):  # no read permission: no Python launches
            assert not launches
    finally:
        sessions.chmod(0o700)


def test_a_reader_that_closes_the_pipe_early_gets_no_traceback(both):
    """Reviewer B's re-review: with each line printed as its check finishes, ``contexto doctor | head -1``
    ended in a ``BrokenPipeError`` traceback."""
    proc = subprocess.Popen([sys.executable, "-m", "contexto", "doctor"], cwd=both.cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().startswith("contexto doctor")
        proc.stdout.close()
        err = proc.stderr.read()
        code = proc.wait(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "Traceback" not in err and "BrokenPipeError" not in err, err
    assert code == 1  # the run did not finish, so it is not a pass


def test_a_second_termination_signal_during_the_cleanup_is_dropped_by_a_handler(monkeypatch):
    """Reviewer B's re-review: after the first signal the others were set to SIG_IGN, and a signal that
    had already arrived for the old handler was then reported by Python as "ignored due to race
    condition", with a traceback on stderr. A handler that does nothing drops it quietly."""
    with pytest.raises(doctor.Terminated) as stop, doctor.termination_raises():
        signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
        pytest.fail("the first signal did not raise")
    assert stop.value.signum == signal.SIGHUP
    with pytest.raises(doctor.Terminated), doctor.termination_raises():
        try:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        finally:
            for signum in (signal.SIGTERM, signal.SIGHUP):
                handler = signal.getsignal(signum)
                assert callable(handler) and handler is not signal.SIG_IGN
                assert handler(signum, None) is None  # and a second one raises nothing


def test_a_settings_file_with_a_very_long_number_is_read_by_every_check(both, capsys, fast_mcp):
    """Reviewer B's re-review: the settings line called a file holding an integer past Python's 4,300
    digits "cannot be read as JSON" while the provider check below read it, and the upstream reader
    still took it as absent, although Claude Code 2.1.322 reads such a file."""
    text = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://long-number.invalid", "CLAUDE_CODE_USE_BEDROCK": "1"},
                       "cleanupPeriodDays": 0}).replace(": 0}", f": 1{'0' * 5000}}}")
    path = both.cwd / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    _, found = checks(capsys)
    assert found["settings"]["status"] == "pass", found["settings"]
    assert found["claude-upstream"]["details"]["origin"] == "http://long-number.invalid"
    assert found["provider"]["status"] == "fail"  # its CLAUDE_CODE_USE_BEDROCK is read, as Claude Code reads it


@pytest.mark.parametrize("env_block", [None, [], "x"])
def test_a_file_whose_env_is_not_an_object_is_not_read_at_all(both, capsys, fast_mcp, env_block):
    """Reviewer B, round 3: Claude Code 2.1.322 loads nothing from a settings file with a wrong-typed key
    ("These settings files did not load"), so its apiKeyHelper never runs and `contexto run claude -p` ended
    "Not logged in" while doctor reported the helper and "Ready". No reader takes anything from such a file."""
    write_settings(both.home / ".claude" / "settings.json",
                   {"env": env_block, "apiKeyHelper": f"echo {HELPER_SECRET}"})
    code, found = checks(capsys)
    settings = found["settings"]
    assert settings["status"] == "warn" and "has an env that is not a JSON object" in settings["summary"]
    assert found["claude-key"]["status"] == "warn" and found["claude-key"]["details"] == {"found": []}


def test_a_fifo_settings_file_is_named_and_the_other_checks_go_on(both, capsys, fast_mcp, within):
    """Reviewer A's re-review: a named pipe at a settings path is never opened for reading
    (``read_settings_file``), so doctor names it and finishes. The launcher refuses on it (D70: a file
    that is there and cannot be read may turn on a provider), so the provider check fails as it does."""
    pipe = both.cwd / ".claude" / "settings.json"
    pipe.parent.mkdir()
    os.mkfifo(pipe)
    code, found = within(60, lambda: checks(capsys))  # a doctor that blocks on the pipe fails, not hangs
    assert found["settings"]["status"] == "warn"
    assert ".claude/settings.json is not a regular file" in found["settings"]["summary"]
    assert found["provider"]["status"] == "fail"
    assert "could not read .claude/settings.json (it is not a regular file)" in found["provider"]["summary"]


def test_a_signal_while_a_version_command_starts_leaves_no_process(both, monkeypatch):
    """Reviewer A's re-review: a signal that landed inside `subprocess.Popen` for `--version` left the
    command running (27 of 150 timed runs with a hanging `--version`). Doctor now holds signals while
    the command starts, as it does for the MCP server. Here the signal is raised from inside Popen, the
    way it lands on a busy machine."""
    real_popen = subprocess.Popen
    started: list[subprocess.Popen] = []

    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        started.append(child)
        signal.raise_signal(signal.SIGTERM)  # lands before Popen has returned to its caller
        return child

    hang = fake_cli(both, "hangs", None, body="#!/bin/sh\nexec sleep 60\n")
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(doctor.Terminated), doctor.termination_raises():
        doctor.read_version(str(hang), {})
    monkeypatch.setattr(subprocess, "Popen", real_popen)
    assert started and started[0].poll() is not None  # killed and reaped, not left running


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_settings_file_with_a_non_json_number_is_named_as_unusable(both, capsys, fast_mcp, literal):
    """Reviewer A, round 3: no test covered the settings line's rule for these, which Python accepts and
    Claude Code rejects."""
    path = both.cwd / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(f'{{"cleanupPeriodDays": {literal}}}')
    _, found = checks(capsys)
    assert found["settings"]["status"] == "warn" and "is not valid JSON" in found["settings"]["summary"]
