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
from contexto.hints import GATED_HINT_HEADERS, HINT_HEADERS_KEY
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
    assert payload["env"] == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", HINT_HEADERS_KEY: "1"}
    assert HINT_HEADERS_KEY == "CLAUDE_CODE_GATEWAY_HINT_HEADERS"


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


def test_a_settings_file_that_turns_them_off_outranks_the_environment(launch_env, recording, tmp_path):
    """Measured on 2.1.322: with the environment at 1 and the user settings at 0 Claude Code sends no
    hint header, so the upstream got none before and gets none now."""
    launch_env[HINT_HEADERS_KEY] = "1"
    user_settings(launch_env, **{HINT_HEADERS_KEY: "0"})
    received, events = run_with_hints(launch_env, recording, tmp_path)
    assert not set(received) & GATED_HINT_HEADERS
    assert next(e for e in events if e["event"] == "launch")["hint_headers"] == "stripped"


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
        assert (found.name, found.value, found.source) == (name, value, "env")


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
    assert found is not None and (found.name, found.source) == ("CLAUDE_CODE_USE_BEDROCK", "settings:user")
    assert "user settings" in cloud_provider_note(found) and str(home) not in cloud_provider_note(found)


def test_a_settings_file_decides_before_the_environment_in_both_directions(tmp_path):
    """Measured on 2.1.322: environment 1 with settings 0 went to ``ANTHROPIC_BASE_URL``; environment 0
    with settings 1 went to Bedrock."""
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    provider_settings(home / ".claude" / "settings.json", "0")
    assert cloud_provider(env={"CLAUDE_CODE_USE_BEDROCK": "1"}, cwd=project, home=home) is None
    provider_settings(home / ".claude" / "settings.json", "1")
    assert cloud_provider(env={"CLAUDE_CODE_USE_BEDROCK": "0"}, cwd=project, home=home).source == "settings:user"
    # An empty value in a file is still a value: it overrides the environment's.
    provider_settings(home / ".claude" / "settings.json", "")
    assert cloud_provider(env={"CLAUDE_CODE_USE_BEDROCK": "1"}, cwd=project, home=home) is None
    # A project file outranks the user's and is named by its kind; CLAUDE_CONFIG_DIR moves the user file.
    provider_settings(project / ".claude" / "settings.local.json", "1", "CLAUDE_CODE_USE_VERTEX")
    found = cloud_provider(env={}, cwd=project, home=home)
    assert (found.name, found.source) == ("CLAUDE_CODE_USE_VERTEX", "settings:local")
    note = cloud_provider_note(found)
    assert ".claude/settings.local.json" in note and "Google Vertex AI" in note
    config = tmp_path / "config"
    provider_settings(config / "settings.json", "1", "CLAUDE_CODE_USE_FOUNDRY")
    moved = cloud_provider(env={"CLAUDE_CONFIG_DIR": str(config)}, cwd=tmp_path, home=home)
    assert moved.name == "CLAUDE_CODE_USE_FOUNDRY"
    # While CLAUDE_CODE_IGNORE_PERSISTED_TRUST is set no project file counts (D52).
    assert cloud_provider(env={"CLAUDE_CODE_IGNORE_PERSISTED_TRUST": "1"}, cwd=project, home=home) is None


def test_check_cloud_provider_raises_a_usage_error_naming_the_variable_and_the_remedy(tmp_path):
    check_cloud_provider(tmp_path, {"HOME": str(tmp_path)})
    with pytest.raises(LaunchError) as raised:
        check_cloud_provider(tmp_path, {"CLAUDE_CODE_USE_MANTLE": "1"})
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
