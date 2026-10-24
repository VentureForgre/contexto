"""D38: the tested CLI versions, the launch-time ``--version`` check, and the proxy's runtime checks.

Shapes are the ones captured with zero spend for D38 (Claude Code 2.1.302 and codex-cli 0.163.0, each
sending one request to a loopback stub that answered 401, temporary homes and fake keys), trimmed to
the parts the checks read; texts are placeholders.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from contexto import cliversions
from contexto.cliversions import (
    NO_CHECK_ENV,
    REPORT_URL,
    TESTED_VERSIONS,
    ClientVersions,
    client_version,
    installed_version,
    launch_check,
    parse_version,
    untested_warning,
)
from contexto.codex import CONTEXT_SECTIONS_CODEX_VERSION, RESPONSES
from contexto.rewrite import (
    ANTHROPIC,
    CONTEXT_BLOCK_KINDS_CLAUDE_CODE_VERSION,
    SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION,
    Config,
    RewriteEngine,
)
from contexto.store.memory import MemoryStore

CLAUDE_UA = "claude-cli/2.1.302 (external, sdk-cli)"
CODEX_UA = "codex_exec/0.163.0 (Mac OS 26.6.1; arm64) dumb (codex_exec; 0.163.0)"


# The single list and the constants that must follow it -----------------------------------------------

def test_tested_versions_run_oldest_to_newest_and_name_both_clis():
    assert set(TESTED_VERSIONS) == {"claude", "codex"}
    for versions in TESTED_VERSIONS.values():
        assert versions and all(parse_version(v) == v for v in versions)
        keys = [tuple(int(x) for x in v.split(".")) for v in versions]
        assert keys == sorted(keys) and len(set(keys)) == len(keys)


def test_measured_on_constants_name_the_newest_tested_version():
    # Adding a version to TESTED_VERSIONS means re-running the spikes for it (docs/cli-updates.md) and
    # bumping these with it; a version added without them fails here.
    assert SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION == TESTED_VERSIONS["claude"][-1]
    assert CONTEXT_BLOCK_KINDS_CLAUDE_CODE_VERSION == TESTED_VERSIONS["claude"][-1]
    assert CONTEXT_SECTIONS_CODEX_VERSION == TESTED_VERSIONS["codex"][-1]
    assert ANTHROPIC.session_start_version == SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION
    assert RESPONSES.session_start_version == CONTEXT_SECTIONS_CODEX_VERSION


# Parsing --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,version", [
    ("2.1.302 (Claude Code)\n", "2.1.302"),
    ("codex-cli 0.163.0\n", "0.163.0"),
    ("codex-cli 0.170.0-alpha.2\n", "0.170.0-alpha.2"),
    ("Claude Code v2.2.0", None),  # "v2.2.0": a letter before the digits is not a version boundary
    ("no version here", None),
    ("1.2", None),
    ("", None),
])
def test_parse_version(text, version):
    assert parse_version(text) == version


def test_client_version_from_the_measured_user_agents():
    assert client_version({"User-Agent": CLAUDE_UA}) == ("claude", "claude-cli", "2.1.302")
    assert client_version({"user-agent": "claude-cli/2.1.302 (external, cli)"}) == ("claude", "claude-cli", "2.1.302")
    assert client_version({"user-agent": CODEX_UA}) == ("codex", "codex_exec", "0.163.0")
    assert client_version({"user-agent": "codex_cli_rs/0.163.0 (Mac OS 26.6.1; arm64)"}) == (
        "codex", "codex_cli_rs", "0.163.0")
    for other in ("contexto-acceptance-harness/0.1 (+VentureForgre/contexto)", "Python-urllib/3.12",
                  "curl/8.7.1", "claude-cli", ""):
        assert client_version({"user-agent": other}) is None, other
    assert client_version({}) is None


def test_untested_warning_is_one_line_naming_tested_versions_report_and_silence():
    line = untested_warning("claude", "2.1.999")
    assert "\n" not in line and line.startswith("warning: Claude Code 2.1.999 ")
    assert "tested: 2.1.302" in line and REPORT_URL in line
    assert "--no-version-check" in line and f"{NO_CHECK_ENV}=1" in line
    codex = untested_warning("codex", "0.170.0")
    assert "Codex 0.170.0" in codex and "tested: 0.154.0, 0.163.0" in codex and "\n" not in codex


# The launch check -----------------------------------------------------------------------------------

def fake_cli(tmp_path: Path, body: str, name: str = "fakecli") -> Path:
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    path.chmod(0o755)
    return path


def test_installed_version_reads_stdout(tmp_path):
    assert installed_version(str(fake_cli(tmp_path, "print('2.1.302 (Claude Code)')"))) == ("2.1.302", None)


def test_installed_version_reasons_when_it_cannot_tell(tmp_path):
    assert installed_version(str(tmp_path / "missing")) == (None, "FileNotFoundError")
    assert installed_version("contexto-no-such-cli-on-path") == (None, "not_found")
    assert installed_version(str(fake_cli(tmp_path, "sys.exit(3)", "exits"))) == (None, "exit_3")
    assert installed_version(str(fake_cli(tmp_path, "print('unknown')", "unparsed"))) == (None, "unparsed")


def test_installed_version_gives_up_after_its_timeout(tmp_path):
    hang = fake_cli(tmp_path, "time.sleep(30)")
    started = time.monotonic()
    assert installed_version(str(hang), timeout=0.5) == (None, "timeout")
    assert time.monotonic() - started < 10


def test_installed_version_closes_stdin(tmp_path):
    # A CLI that reads stdin for --version gets end of file at once instead of waiting for the terminal.
    reader = fake_cli(tmp_path, "data = sys.stdin.read(); print('2.1.302' if data == '' else 'x')")
    assert installed_version(str(reader), timeout=5) == ("2.1.302", None)


def test_launch_check_events_and_warning(tmp_path):
    tested = fake_cli(tmp_path, "print('2.1.302 (Claude Code)')", "tested")
    event, warning = launch_check("claude", str(tested), env={})
    assert event == {"event": "cli_version_check", "cli": "claude", "version": "2.1.302", "tested": True,
                     "tested_versions": "2.1.302"} and warning is None
    newer = fake_cli(tmp_path, "print('2.1.400 (Claude Code)')", "newer")
    event, warning = launch_check("claude", str(newer), env={})
    assert event["tested"] is False and event["version"] == "2.1.400"
    assert warning == untested_warning("claude", "2.1.400")
    event, warning = launch_check("codex", "contexto-no-such-cli-on-path", env={})
    assert event["version"] is None and event["tested"] is None and event["error"] == "not_found" and warning is None


@pytest.mark.parametrize("value,skipped", [("1", True), ("yes", True), ("true", True), ("0", False),
                                           ("false", False), ("", False)])
def test_launch_check_is_silenced_by_flag_or_env(tmp_path, value, skipped):
    newer = fake_cli(tmp_path, "print('2.1.400 (Claude Code)')")
    event, warning = launch_check("claude", str(newer), env={NO_CHECK_ENV: value})
    assert (event.get("skipped") is True) == skipped and (warning is None) == skipped
    event, warning = launch_check("claude", str(newer), enabled=False, env={})
    assert event == {"event": "cli_version_check", "cli": "claude", "skipped": True} and warning is None


def test_launch_check_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cliversions, "installed_version", boom)
    event, warning = launch_check("claude", "claude", env={})
    assert event["error"] == "RuntimeError" and warning is None


# Session-start recognition ---------------------------------------------------------------------------

def environment(text: str = "# Environment\nYou have been invoked in the following environment: ...") -> dict:
    return {"role": "system", "content": [{"type": "text", "text": text}]}


def claude_request(messages: list[dict], *, tools: bool = True) -> dict:
    request = {"model": "claude-opus-5-5", "max_tokens": 32000, "stream": True,
               "system": [{"type": "text", "text": "You are Claude Code."}], "messages": messages}
    if tools:
        request["tools"] = [{"name": "Read", "description": "d", "input_schema": {"type": "object"}}]
    return request


def prompt(text: str = "say ok", *blocks: str) -> dict:
    return {"role": "user", "content": [*({"type": "text", "text": b} for b in blocks), {"type": "text", "text": text}]}


ATTRIBUTION = "<system-reminder>\nAttribution for git commits ...\n</system-reminder>"


def test_anthropic_session_start_seen_on_the_measured_shapes():
    # 5.x ids (the D38 capture, claude-opus-5-5): messages[0] user with a context block, messages[1]
    # the environment system message.
    assert ANTHROPIC.session_start_seen([prompt("probe", ATTRIBUTION), environment()]) is True
    # Wrapped in <system-reminder> (claude-sonnet-5) and with settings fields (effort on opus/haiku 5.5).
    wrapped = environment("<system-reminder>\n# Environment\n...</system-reminder>")
    assert ANTHROPIC.session_start_seen([prompt(), wrapped]) is True
    assert ANTHROPIC.session_start_seen([prompt(), {**environment(), "output_config": {"effort": "high"}}]) is True
    # 4.x ids: the environment is a context block in messages[0].
    block = "<system-reminder>\n# Environment\nYou have been invoked ...\n</system-reminder>"
    assert ANTHROPIC.session_start_seen([prompt("probe", ATTRIBUTION, block)]) is True


def test_anthropic_session_start_not_recognised_when_the_heading_changes():
    renamed = environment("# Session environment\nYou have been invoked ...")
    assert ANTHROPIC.session_start_seen([prompt("probe", ATTRIBUTION), renamed]) is False
    renamed_block = "<system-reminder>\n## Environment details\n...\n</system-reminder>"
    assert ANTHROPIC.session_start_seen([prompt("probe", renamed_block)]) is False


def test_anthropic_session_start_cannot_tell_without_injected_context():
    assert ANTHROPIC.session_start_seen([prompt("plain API client")]) is None
    assert ANTHROPIC.session_start_seen([{"role": "user", "content": "a string prompt"}, "not a dict"]) is None
    assert ANTHROPIC.session_start_seen([]) is None
    assert ANTHROPIC.judges_session_start(claude_request([])) is True
    assert ANTHROPIC.judges_session_start(claude_request([], tools=False)) is False


def test_anthropic_session_start_reads_only_a_block_head():
    huge = "<system-reminder>\n# Environment\n" + "x" * 5_000_000
    assert ANTHROPIC.session_start_seen([prompt("probe", huge)]) is True
    late = "<system-reminder>\n" + " " * 5000 + "# Environment\n"
    assert ANTHROPIC.session_start_seen([prompt("probe", late)]) is False


def codex_lead(*, sections: bool = True, developer: bool = True) -> list[dict]:
    """codex-cli 0.163.0's lead from the D38 capture: additional_tools, base instructions, tagged
    developer messages, then the user <environment_context> message, then the prompt."""
    def message(role, text):
        return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}

    env = "<environment_context>\n  <cwd>/synthetic</cwd>\n</environment_context>" if sections else \
        "<session_environment>\n  <cwd>/synthetic</cwd>\n</session_environment>"
    items = [{"type": "additional_tools", "role": "developer", "tools": []}] if developer else []
    if developer:
        items += [message("developer", "You are Codex."), message("developer", "<skills_instructions>x"),
                  message("developer", "<multi_agent_role>x"), message("developer", "<multi_agent_mode>x")]
    return [*items, message("user", env), message("user", "probe")]


def test_responses_session_start_seen():
    assert RESPONSES.session_start_seen(codex_lead()) is True
    assert RESPONSES.session_start_seen(codex_lead(sections=False)) is False  # its context became a turn start
    assert RESPONSES.session_start_seen(codex_lead(developer=False)) is True
    assert RESPONSES.session_start_seen(codex_lead(sections=False, developer=False)) is None
    assert RESPONSES.judges_session_start({"input": []}) is True  # code mode sends no top-level tools


# The runtime checks ----------------------------------------------------------------------------------

def names(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


def test_client_versions_logs_each_version_once_and_warns_once():
    check = ClientVersions()
    request = claude_request([prompt("probe", ATTRIBUTION), environment()])
    first = check.observe(request, {"User-Agent": CLAUDE_UA}, ANTHROPIC, main=True)
    assert first == [{"event": "cli_version", "cli": "claude", "client": "claude-cli", "version": "2.1.302",
                      "tested": True, "tested_versions": "2.1.302"}]
    assert check.observe(request, {"User-Agent": CLAUDE_UA}, ANTHROPIC, main=True) == []
    newer = check.observe(request, {"user-agent": "claude-cli/2.1.400 (external, cli)"}, ANTHROPIC, main=False)
    assert names(newer) == ["cli_version", "cli_version_untested"]
    assert newer[0]["tested"] is False and newer[1] == {"event": "cli_version_untested", "cli": "claude",
                                                        "version": "2.1.400", "tested_versions": "2.1.302"}
    again = check.observe(request, {"user-agent": "claude-cli/2.1.401 (external, cli)"}, ANTHROPIC, main=False)
    assert names(again) == ["cli_version"]  # the warning is once per engine
    assert check.observe(request, {"user-agent": "curl/8.7.1"}, ANTHROPIC, main=False) == []


def test_client_versions_caps_distinct_versions():
    check = ClientVersions()
    request = claude_request([prompt()], tools=False)
    logged = 0
    for minor in range(cliversions.MAX_SEEN + 5):
        events = check.observe(request, {"user-agent": f"claude-cli/2.{minor}.0"}, ANTHROPIC, main=False)
        logged += names(events).count("cli_version")
    assert logged == cliversions.MAX_SEEN


def test_session_start_unrecognised_once_on_the_first_judged_main_request():
    check = ClientVersions()
    renamed = claude_request([prompt("probe", ATTRIBUTION), environment("# Session environment\n...")])
    side = claude_request([prompt("title this")], tools=False)
    assert check.observe(side, {}, ANTHROPIC, main=True) == []  # no tool list: a side request, not judged
    assert check.observe(renamed, {}, ANTHROPIC, main=False) == []  # not main
    ua = {"user-agent": "claude-cli/2.1.400 (external, cli)"}
    events = check.observe(renamed, ua, ANTHROPIC, main=True)
    assert names(events) == ["cli_version", "cli_version_untested", "session_start_unrecognised"]
    assert events[-1] == {"event": "session_start_unrecognised", "dialect": "anthropic",
                          "signature_version": "2.1.302", "cli_version": "2.1.400", "tested_versions": "2.1.302"}
    assert check.observe(renamed, ua, ANTHROPIC, main=True) == []


def test_session_start_recognised_or_unknowable_is_never_warned_and_judged_once(monkeypatch):
    check = ClientVersions()
    good = claude_request([prompt("probe", ATTRIBUTION), environment()])
    assert check.observe(good, {}, ANTHROPIC, main=True) == []
    calls = []
    monkeypatch.setattr(type(ANTHROPIC), "session_start_seen", lambda self, items: calls.append(1) or False)
    assert check.observe(good, {}, ANTHROPIC, main=True) == [] and calls == []  # judged already: no scan
    monkeypatch.undo()
    plain = ClientVersions()
    assert plain.observe(claude_request([prompt("plain client")]), {}, ANTHROPIC, main=True) == []
    assert plain.session_start_judged


def test_codex_session_start_unrecognised():
    check = ClientVersions()
    events = check.observe({"input": codex_lead(sections=False)}, {"user-agent": CODEX_UA}, RESPONSES, main=True)
    assert names(events) == ["cli_version", "session_start_unrecognised"]
    assert events[-1] == {"event": "session_start_unrecognised", "dialect": "responses",
                          "signature_version": "0.163.0", "cli_version": "0.163.0",
                          "tested_versions": "0.154.0, 0.163.0"}
    ok = ClientVersions()
    assert names(ok.observe({"input": codex_lead()}, {"user-agent": CODEX_UA}, RESPONSES, main=True)) == ["cli_version"]


def copy_of(request: dict) -> dict:
    return json.loads(json.dumps(request))


def test_engine_logs_the_events_before_its_request_event_and_keeps_the_rewrite():
    engine = RewriteEngine(MemoryStore(), config=Config(bound=150_000))
    request = claude_request([prompt("probe", ATTRIBUTION), environment("# Session environment\n...")])
    reference = RewriteEngine(MemoryStore(), config=Config(bound=150_000))
    expected, _, _ = reference(copy_of(request), {}, "/v1/messages")
    ua = {"User-Agent": "claude-cli/2.1.400 (external, cli)"}
    upstream, headers, events = engine(copy_of(request), dict(ua), "/v1/messages")
    found = names(events)
    assert {"cli_version", "cli_version_untested", "session_start_unrecognised"} <= set(found)
    assert found.index("session_start_unrecognised") < len(found) - 1 and events[-1].get("main") is True
    assert upstream == expected and headers == ua
    _, _, events = engine(copy_of(request), dict(ua), "/v1/messages")
    assert not {"cli_version", "cli_version_untested", "session_start_unrecognised"} & set(names(events))


def test_engine_check_that_raises_keeps_the_rewrite_and_logs_only_its_type():
    request = claude_request([prompt("probe", ATTRIBUTION), environment()])
    expected, _, _ = RewriteEngine(MemoryStore(), config=Config(bound=150_000))(copy_of(request), {}, "/v1/messages")

    class Raising:
        def observe(self, *args, **kwargs):
            raise ValueError("secret request text")

    engine = RewriteEngine(MemoryStore(), config=Config(bound=150_000))
    engine.clients = Raising()
    upstream, _, events = engine(copy_of(request), {}, "/v1/messages")
    assert upstream == expected
    assert [e for e in events if e["event"] == "cli_version_error"] == [{"event": "cli_version_error",
                                                                          "error": "ValueError"}]


def test_engine_count_tokens_is_not_checked():
    engine = RewriteEngine(MemoryStore(), config=Config(bound=150_000))
    request = claude_request([prompt("probe", ATTRIBUTION), environment("# Session environment\n...")])
    _, _, events = engine(request, {"User-Agent": "claude-cli/2.1.400"}, "/v1/messages/count_tokens")
    assert names(events) == ["count_tokens"]

