"""``contexto run claude``, the gateway hint headers and the cloud-provider guard (D70), with a fake
``claude`` executable and a recording upstream. No live model."""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import codex
from contexto.hints import GATED_HINT_HEADERS, HINT_HEADERS_KEY, HINT_MARKER_KEY
from contexto.launcher import EXIT_USAGE, LaunchError, check_cloud_provider, settings_payload
from contexto.upstream import CLOUD_PROVIDER_KEYS, cloud_provider, cloud_provider_note
from test_launcher import FAKE_CLAUDE, launch, session_log

CLAUDE_SESSION = "237dedfa-8ca9-43b9-ad3c-4a6ebcb1a084"
HINTS = {"x-claude-code-request-class": "main", "x-claude-code-prompt-id": "c484ea9c-0000-4000-8000-000000000001",
         "x-claude-code-agent-type": "general-purpose", "x-claude-code-prev-tool-durations": "Bash=30",
         "x-claude-code-compaction": "manual", "x-claude-code-context-compacted": "manual"}
UNGATED = {"x-claude-code-agent-id": "a800b6f5b41866e3f", "x-claude-code-parent-agent-id": "a1"}


class Recording(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict[str, str]] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Recording.seen.append({name.lower(): value for name, value in self.headers.items()})
        body = json.dumps({"type": "message", "content": [], "usage": {"input_tokens": 5, "output_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def recording():
    Recording.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Recording)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def launch_env(tmp_path: Path) -> dict[str, str]:
    """A fake ``claude`` first on PATH, a temporary HOME and CONTEXTO_HOME, and none of the developer's
    Claude or Anthropic variables."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    fake.chmod(0o755)
    home = tmp_path / "userhome"
    home.mkdir()
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE", "OPENRIG_", "RIGGED_"))}
    clean.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", HOME=str(home),
                 CONTEXTO_HOME=str(tmp_path / "contexto"), FAKE_RECORD=str(tmp_path / "record.json"))
    return clean


def user_settings(env: dict[str, str], **values) -> None:
    path = Path(env["HOME"]) / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": values}))


def run_with_hints(env: dict[str, str], upstream: str, cwd: Path) -> tuple[dict[str, str], list[dict]]:
    """One launch whose fake claude sends a main request with every hint header. Returns the headers
    the upstream received and the session's log."""
    env.update(FAKE_REQUEST="1", FAKE_TOOLS="Read", FAKE_CLAUDE_SESSION=CLAUDE_SESSION,
               FAKE_HEADERS=json.dumps({**HINTS, **UNGATED}))
    proc = launch(env, "--upstream", upstream, "--session", "s1", "claude", "-p", "hello", cwd=cwd)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert len(Recording.seen) == 1
    return Recording.seen[0], session_log(env, "s1")


# The launcher asks for the hints ------------------------------------------------------------------------


def test_the_settings_file_asks_claude_code_for_the_hint_headers():
    payload = settings_payload("http://127.0.0.1:1")
    assert payload["env"] == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", HINT_HEADERS_KEY: "1",
                              HINT_MARKER_KEY: "stripped"}
    assert HINT_HEADERS_KEY == "CLAUDE_CODE_GATEWAY_HINT_HEADERS" and HINT_MARKER_KEY == "CONTEXTO_HINT_HEADERS"


def test_the_settings_file_carries_the_launch_s_decision_for_a_nested_launch():
    """Claude Code applies the ``env`` block to its own environment, so every process it starts inherits
    it. The marker tells a nested ``contexto run claude`` whose setting the inherited variable is."""
    url = "http://127.0.0.1:1"
    assert settings_payload(url, "forwarded")["env"] == {"ANTHROPIC_BASE_URL": url, HINT_HEADERS_KEY: "1",
                                                         HINT_MARKER_KEY: "forwarded"}
    # The user turned the hints off: Contexto does not turn them on.
    assert settings_payload(url, "off")["env"] == {"ANTHROPIC_BASE_URL": url, HINT_MARKER_KEY: "off"}
    with pytest.raises(ValueError):
        settings_payload(url, "sideways")


def test_contexto_run_codex_does_not_ask_for_them():
    """Codex sends no such headers: its launcher writes no Claude Code settings file, sets no such
    variable, and starts the proxy without the strip keyword."""
    source = Path(codex.__file__).read_text()
    assert HINT_HEADERS_KEY not in source and "settings_payload" not in source
    assert "strip_hint_headers" not in source and "hints" not in source


def test_the_upstream_gets_no_gated_header_and_the_log_has_the_class(launch_env, recording, tmp_path):
    received, events = run_with_hints(launch_env, recording, tmp_path)
    assert not set(received) & GATED_HINT_HEADERS
    assert received["x-claude-code-session-id"] == CLAUDE_SESSION
    assert {name: received[name] for name in UNGATED} == UNGATED  # sent with or without the variable
    record = json.loads(Path(launch_env["FAKE_RECORD"]).read_text())
    assert record["settings"]["env"][HINT_HEADERS_KEY] == "1"
    launched = next(e for e in events if e["event"] == "launch")
    assert launched["hint_headers"] == "stripped"
    request = next(e for e in events if e["event"] == "request")
    engine_event = request["transform_events"][-1]
    assert engine_event["request_class"] == "main" and engine_event["main"] is True
    assert engine_event["compaction"] == engine_event["context_compacted"] == "manual"
    logged = json.dumps(events)
    for value in (HINTS["x-claude-code-prompt-id"], "general-purpose", "Bash=30", UNGATED["x-claude-code-agent-id"]):
        assert value not in logged


@pytest.mark.parametrize("where", ["environment", "user settings"])
def test_hints_the_user_turned_on_pass_through(launch_env, recording, tmp_path, where):
    if where == "environment":
        launch_env[HINT_HEADERS_KEY] = "1"
    else:
        user_settings(launch_env, **{HINT_HEADERS_KEY: "true"})
    received, events = run_with_hints(launch_env, recording, tmp_path)
    assert {name: received[name] for name in HINTS} == HINTS
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "forwarded"
    assert next(e for e in events if e["event"] == "request")["transform_events"][-1]["request_class"] == "main"


def child_settings_env(env: dict[str, str]) -> dict[str, str]:
    """The ``env`` block of the ``--settings`` file the launcher handed the fake claude."""
    return json.loads(Path(env["FAKE_RECORD"]).read_text())["settings"]["env"]


def test_a_settings_file_that_turns_them_off_outranks_the_environment(launch_env, recording, tmp_path):
    """Measured on 2.1.322: with the environment at 1 and the user settings at 0 Claude Code sends no
    hint header. The user's file says off, so Contexto does not ask for the hints either."""
    launch_env[HINT_HEADERS_KEY] = "1"
    user_settings(launch_env, **{HINT_HEADERS_KEY: "0"})
    _, events = run_with_hints(launch_env, recording, tmp_path)
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "off"
    assert HINT_HEADERS_KEY not in child_settings_env(launch_env)


@pytest.mark.parametrize("where", ["environment", "user settings"])
@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_an_explicit_off_is_respected(launch_env, recording, tmp_path, where, value):
    """The user set the variable to an off value: the launcher leaves it out of its ``env`` block, so
    Claude Code sends no hint header and the engine reads its signatures, and the proxy removes
    nothing, as before D70 (the fake claude sends the headers regardless, which shows that)."""
    if where == "environment":
        launch_env[HINT_HEADERS_KEY] = value
    else:
        user_settings(launch_env, **{HINT_HEADERS_KEY: value})
    received, events = run_with_hints(launch_env, recording, tmp_path)
    block = child_settings_env(launch_env)
    assert block == {"ANTHROPIC_BASE_URL": block["ANTHROPIC_BASE_URL"], HINT_MARKER_KEY: "off"}
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "off"
    assert {name: received[name] for name in HINTS} == HINTS


def test_the_child_gets_the_decision_of_its_own_launch(launch_env, recording, tmp_path):
    run_with_hints(launch_env, recording, tmp_path)
    assert child_settings_env(launch_env)[HINT_MARKER_KEY] == "stripped"
    assert child_settings_env(launch_env)[HINT_HEADERS_KEY] == "1"


def nested(env: dict[str, str], outer_decision: str) -> None:
    """Make ``env`` the environment of a process started inside an outer ``contexto run claude``
    session: Claude Code applied the outer launcher's ``env`` block to itself, and its children
    inherit it. The base URL is left out; ``--upstream`` stands in for one the user configured."""
    block = settings_payload("http://127.0.0.1:1", outer_decision)["env"]
    env.update({name: value for name, value in block.items() if name != "ANTHROPIC_BASE_URL"})


def test_a_nested_launch_does_not_take_contexto_s_own_setting_for_the_user_s(launch_env, recording, tmp_path):
    """Reviewer A, major 1 (measured with the real 2.1.322): the outer session stripped the hints, a
    command inside it ran ``contexto run claude``, and that launch forwarded all six to its upstream
    because it read the inherited ``1`` as the user's."""
    nested(launch_env, "stripped")
    assert launch_env[HINT_HEADERS_KEY] == "1"
    received, events = run_with_hints(launch_env, recording, tmp_path)
    assert not set(received) & GATED_HINT_HEADERS
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "stripped"
    assert child_settings_env(launch_env)[HINT_MARKER_KEY] == "stripped"
    assert next(e for e in events if e["event"] == "request")["transform_events"][-1]["request_class"] == "main"


def test_a_nested_launch_forwards_when_the_outer_launch_did(launch_env, recording, tmp_path):
    """The outer launch forwarded because the user had the hints on, so the inherited ``1`` is theirs."""
    nested(launch_env, "forwarded")
    received, events = run_with_hints(launch_env, recording, tmp_path)
    assert {name: received[name] for name in HINTS} == HINTS
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "forwarded"
    assert child_settings_env(launch_env)[HINT_MARKER_KEY] == "forwarded"


def test_a_nested_launch_still_forwards_for_the_user_s_settings_file(launch_env, recording, tmp_path):
    """The marker only discounts the process environment. A settings file's value is the user's."""
    nested(launch_env, "stripped")
    user_settings(launch_env, **{HINT_HEADERS_KEY: "1"})
    received, events = run_with_hints(launch_env, recording, tmp_path)
    assert {name: received[name] for name in HINTS} == HINTS
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "forwarded"


def test_a_nested_launch_under_an_outer_off_stays_off(launch_env, recording, tmp_path):
    """The outer launch left the variable alone, so the nested one sees the user's own ``0``."""
    launch_env[HINT_HEADERS_KEY] = "0"
    nested(launch_env, "off")
    assert launch_env[HINT_HEADERS_KEY] == "0"  # the outer env block did not set it
    _, events = run_with_hints(launch_env, recording, tmp_path)
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "off"


# The cloud-provider guard -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CLOUD_PROVIDER_KEYS))
@pytest.mark.parametrize("value, on", [("1", True), ("true", True), ("yes", True), ("on", True), ("TRUE", True),
                                       (" 1 ", True), ("0", False), ("false", False), ("", False), ("no", False),
                                       ("2", False), ("enabled", False)])
def test_the_values_claude_code_reads_as_on(tmp_path, name, value, on):
    """Measured on 2.1.322 with ``CLAUDE_CODE_USE_BEDROCK``: with each "on" value every request went to
    the Bedrock stub and none to ``ANTHROPIC_BASE_URL``; with each "off" value all went to it. The other
    four switches were measured with ``1``, ``true`` and ``0``."""
    home = tmp_path / "home"
    home.mkdir()
    found = cloud_provider(env={name: value}, cwd=tmp_path, home=home)
    assert (found is not None) is on
    if on:
        assert found.on == ((name, ("env",)),) and found.unread == ()


def test_no_switch_no_provider(tmp_path):
    assert cloud_provider(env={"ANTHROPIC_BASE_URL": "http://x.invalid"}, cwd=tmp_path, home=tmp_path) is None


def provider_settings(path: Path, value, name: str = "CLAUDE_CODE_USE_BEDROCK") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {name: value}}))


@pytest.mark.parametrize("value", ["1", "true", True, 1])
def test_a_switch_in_a_settings_env_block_counts(tmp_path, value):
    """Measured on 2.1.322: the switch only in the config directory's settings.json sent every request
    to Bedrock, as the text ``1`` or ``true`` and as a JSON ``true`` or number ``1``."""
    home = tmp_path / "home"
    provider_settings(home / ".claude" / "settings.json", value)
    found = cloud_provider(env={}, cwd=tmp_path, home=home)
    assert found is not None and found.on == (("CLAUDE_CODE_USE_BEDROCK", ("settings:user",)),)
    assert "user settings" in cloud_provider_note(found) and str(home) not in cloud_provider_note(found)


def test_the_guard_refuses_wherever_a_switch_is_on_whatever_the_precedence(tmp_path):
    """Measured on 2.1.322: environment 1 with settings 0 went to ``ANTHROPIC_BASE_URL``, and environment
    0 with settings 1 went to Bedrock. The guard does not predict the winner (review round 2: Claude
    Code does not apply every file Contexto can read), so both are refused, each naming where the
    switch is on."""
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    provider_settings(home / ".claude" / "settings.json", "0")
    found = cloud_provider(env={"CLAUDE_CODE_USE_BEDROCK": "1"}, cwd=project, home=home)
    assert found.on == (("CLAUDE_CODE_USE_BEDROCK", ("env",)),)
    provider_settings(home / ".claude" / "settings.json", "1")
    found = cloud_provider(env={"CLAUDE_CODE_USE_BEDROCK": "0"}, cwd=project, home=home)
    assert found.on == (("CLAUDE_CODE_USE_BEDROCK", ("settings:user",)),)
    # An empty value in a file does not hide the environment's either.
    provider_settings(home / ".claude" / "settings.json", "")
    assert cloud_provider(env={"CLAUDE_CODE_USE_BEDROCK": "1"}, cwd=project, home=home).on[0][1] == ("env",)
    assert cloud_provider(env={}, cwd=project, home=home) is None
    # A project file is named by its kind; CLAUDE_CONFIG_DIR moves the user file.
    provider_settings(project / ".claude" / "settings.local.json", "1", "CLAUDE_CODE_USE_VERTEX")
    found = cloud_provider(env={}, cwd=project, home=home)
    assert found.on == (("CLAUDE_CODE_USE_VERTEX", ("settings:local",)),)
    note = cloud_provider_note(found)
    assert ".claude/settings.local.json" in note and "Google Vertex AI" in note
    config = tmp_path / "config"
    provider_settings(config / "settings.json", "1", "CLAUDE_CODE_USE_FOUNDRY")
    moved = cloud_provider(env={"CLAUDE_CONFIG_DIR": str(config)}, cwd=tmp_path, home=home)
    assert moved.on == (("CLAUDE_CODE_USE_FOUNDRY", ("settings:user",)),)
    # While CLAUDE_CODE_IGNORE_PERSISTED_TRUST is set claude -p drops a project file's env (D52), and the
    # interactive CLI applies it after its dialog. The guard refuses either way.
    found = cloud_provider(env={"CLAUDE_CODE_IGNORE_PERSISTED_TRUST": "1"}, cwd=project, home=home)
    assert found.on == (("CLAUDE_CODE_USE_VERTEX", ("settings:local",)),)


def test_check_cloud_provider_raises_a_usage_error_naming_the_variable_and_the_remedy(tmp_path):
    check_cloud_provider(tmp_path, {}, home=tmp_path)
    with pytest.raises(LaunchError) as raised:
        check_cloud_provider(tmp_path, {"CLAUDE_CODE_USE_MANTLE": "1"}, home=tmp_path)
    message = str(raised.value)
    assert raised.value.code == EXIT_USAGE
    assert "CLAUDE_CODE_USE_MANTLE is set in the process environment" in message
    assert "no bound" in message and "env -u CLAUDE_CODE_USE_MANTLE" in message and "without contexto" in message


@pytest.mark.parametrize("name", sorted(CLOUD_PROVIDER_KEYS))
def test_run_claude_refuses_a_cloud_provider_launch_before_any_session(launch_env, recording, tmp_path, name):
    launch_env[name] = "1"
    launch_env["FAKE_REQUEST"] = "1"
    proc = launch(launch_env, "--upstream", recording, "--session", "s1", "claude", "-p", "hello", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_USAGE == 2
    assert err.startswith(f"contexto run: {name} is set in the process environment")
    assert "ANTHROPIC_BASE_URL" in err and "no bound" in err and f"unset {name}" in err
    assert "Traceback" not in err
    assert not Path(launch_env["CONTEXTO_HOME"]).exists()  # no session, no home
    assert not Path(launch_env["FAKE_RECORD"]).exists() and Recording.seen == []  # claude never ran


def test_run_claude_refuses_a_switch_set_in_the_user_settings(launch_env, recording, tmp_path):
    user_settings(launch_env, CLAUDE_CODE_USE_BEDROCK="true")
    proc = launch(launch_env, "--upstream", recording, "claude", "-p", "hello", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 2
    assert "CLAUDE_CODE_USE_BEDROCK is set in the env block of your Claude Code user settings" in err
    assert "remove CLAUDE_CODE_USE_BEDROCK from that env block" in err and "Amazon Bedrock" in err
    assert not Path(launch_env["CONTEXTO_HOME"]).exists() and not Path(launch_env["FAKE_RECORD"]).exists()


@pytest.mark.parametrize("content", [b'{"env": {"CLAUDE_CODE_USE_BEDROCK": 1.0}}',
                                     b'{"env": {"CLAUDE_CODE_USE_BEDROCK": ["1"]}}',
                                     b'\xef\xbb\xbf{"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}'])
def test_run_claude_refuses_a_repository_file_claude_code_reads_as_on(launch_env, recording, tmp_path, content):
    """Reviewer B, major 1, through the launcher: each of these project files sent the real Claude Code
    to Bedrock while the launch opened a session and bounded nothing."""
    project = tmp_path / "repo"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_bytes(content)
    launch_env["FAKE_REQUEST"] = "1"
    proc = launch(launch_env, "--upstream", recording, "claude", "-p", "hello", cwd=project)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 2
    assert err.startswith("contexto run: CLAUDE_CODE_USE_BEDROCK is set in the env block of .claude/settings.json")
    assert not Path(launch_env["CONTEXTO_HOME"]).exists() and not Path(launch_env["FAKE_RECORD"]).exists()
    assert Recording.seen == []


def test_run_claude_refuses_the_environment_s_switch_beside_a_project_file_that_says_off(launch_env, recording,
                                                                                       tmp_path):
    """Measured: a project file holding ``NaN`` is not applied by Claude Code, so the environment's 1
    stayed in force. Refused also when Claude Code would have applied the file: the accepted cost."""
    project = tmp_path / "repo"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text('{"env": {"CLAUDE_CODE_USE_BEDROCK": "0"}, "x": NaN}')
    launch_env["CLAUDE_CODE_USE_BEDROCK"] = "1"
    proc = launch(launch_env, "--upstream", recording, "claude", "-p", "hello", cwd=project)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 2 and "CLAUDE_CODE_USE_BEDROCK is set in the process environment" in err
    assert not Path(launch_env["CONTEXTO_HOME"]).exists() and not Path(launch_env["FAKE_RECORD"]).exists()


def test_a_switch_that_is_off_does_not_stop_the_launch(launch_env, recording, tmp_path):
    launch_env.update(CLAUDE_CODE_USE_BEDROCK="0", CLAUDE_CODE_USE_VERTEX="false", FAKE_REQUEST="1")
    proc = launch(launch_env, "--upstream", recording, "--session", "s1", "claude", "-p", "hello", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert len(Recording.seen) == 1


@pytest.mark.parametrize("info", ["--version", "--help", "-v", "-h"])
def test_version_and_help_alone_run_without_the_check(launch_env, tmp_path, info):
    """``INFO_ARGS``: claude prints and exits, with no model request, so there is nothing to bound and
    nothing to refuse. The fake claude answers ``--version`` and fails on the others for lack of a
    ``--settings`` flag, which shows it was run directly."""
    launch_env.update(CLAUDE_CODE_USE_BEDROCK="1", FAKE_VERSION="2.1.322 (Claude Code)")
    proc = launch(launch_env, "claude", info, cwd=tmp_path)
    out, err = proc.communicate(timeout=30)
    assert "contexto run:" not in err and "CLAUDE_CODE_USE_BEDROCK" not in err
    if info == "--version":
        assert proc.returncode == 0 and out.strip() == "2.1.322 (Claude Code)"
    else:
        assert proc.returncode == 1 and "StopIteration" in err  # the fake claude itself, started with no flags
    assert not Path(launch_env["CONTEXTO_HOME"]).exists()
