"""The proxy and Claude Code's gateway hint headers (D70): what reaches the upstream.

``contexto run claude`` turns the hints on for the rewrite engine. The upstream must get the headers
it got before that, so ``ContextoProxy(strip_hint_headers=True)`` leaves the six gated headers out of
every request it sends, after the transform has read them. Synthetic requests against the stub
upstream of ``test_proxy_server``; no model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contexto.hints import GATED_HINT_HEADERS, user_enabled_hint_headers
from contexto.rewrite import RewriteEngine
from contexto.store.memory import MemoryStore
from contexto.upstream import resolve_upstream
from test_clear_main import OLD, metadata
from test_proxy_server import AUTH, REQUEST_BYTES, StubEngine, Upstream, call
from test_rewrite import CONFIG, history, request, user
from test_upstream import write_settings

SESSION = {"X-Claude-Code-Session-Id": OLD, "User-Agent": "claude-cli/2.1.322 (external, cli)"}
UNGATED = {**SESSION, "x-claude-code-agent-id": "a800b6f5b41866e3f", "x-claude-code-parent-agent-id": "a1"}
# The gated headers as 2.1.322 sent them, in mixed case to show the names match in any case.
HINTS = {"x-claude-code-request-class": "main", "X-Claude-Code-Prompt-Id": "c484ea9c-0000-4000-8000-000000000001",
         "x-claude-code-agent-type": "general-purpose", "x-claude-code-prev-tool-durations": "Bash=30;Read=9",
         "X-CLAUDE-CODE-COMPACTION": "manual", "x-claude-code-context-compacted": "manual"}


def gated(headers: dict[str, str]) -> set[str]:
    return {name.lower() for name in headers} & GATED_HINT_HEADERS


def with_hints(headers: dict[str, str]) -> dict[str, str]:
    """``headers`` with the hint headers in the middle, where Claude Code's own header order puts them."""
    items = list(headers.items())
    return dict([*items[:2], *HINTS.items(), *items[2:]])


class Seeing:
    """A transform that records the headers it was given."""

    preserves_input = True

    def __init__(self) -> None:
        self.headers: list[dict[str, str]] = []

    def __call__(self, request, headers, endpoint):
        self.headers.append(dict(headers))
        return request, headers, [{"epoch": {"k": 0}}]


def test_the_set_covers_all_six_headers_of_the_test():
    assert gated(HINTS) == GATED_HINT_HEADERS


def test_stripping_removes_the_six_gated_headers_after_the_transform_read_them(make_proxy):
    seeing = Seeing()
    proxy, log = make_proxy(transform=seeing, strip_hint_headers=True)
    response, _ = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, with_hints({**AUTH, **UNGATED}))
    assert response.status == 200
    assert gated(seeing.headers[0]) == GATED_HINT_HEADERS  # the engine reads them
    forwarded = Upstream.received[0][2]
    assert gated(forwarded) == set()
    for name, value in {**AUTH, **UNGATED}.items():  # the session, agent and parent agent ids stay
        assert forwarded[name] == value
    assert Upstream.received[0][3] == REQUEST_BYTES
    assert "main" not in json.dumps([e for e in log.events if e["event"] == "request"])  # the proxy logs no value


def test_without_the_keyword_every_header_is_forwarded_as_before(make_proxy):
    seeing = Seeing()
    proxy, _ = make_proxy(transform=seeing)
    assert proxy.strip_hint_headers is False
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, with_hints({**AUTH, **UNGATED}))
    forwarded = Upstream.received[0][2]
    for name, value in HINTS.items():
        assert forwarded[name] == value


@pytest.mark.parametrize("method, path, body", [("POST", "/v1/messages/count_tokens", REQUEST_BYTES),
                                                ("GET", "/v1/models?limit=1000", b""),
                                                ("HEAD", "/api/hello", b""),
                                                ("POST", "/v1/messages", b"not json")])
def test_stripping_covers_every_path_the_proxy_forwards(make_proxy, method, path, body):
    proxy, _ = make_proxy(strip_hint_headers=True)
    call(proxy, method, path, body, with_hints({**AUTH, **UNGATED}))
    assert len(Upstream.received) == 1 and gated(Upstream.received[0][2]) == set()
    assert Upstream.received[0][2]["x-claude-code-agent-id"] == UNGATED["x-claude-code-agent-id"]


def test_a_failed_transform_forwards_the_clis_request_without_the_hints(make_proxy):
    """Contract rule 14: the CLI's own bytes and headers are forwarded. Its own headers are the ones
    it would have sent without Contexto's ``--settings``, so the hints are left out here too."""
    Upstream.mode = "json"
    proxy, log = make_proxy(transform=StubEngine("raise"), strip_hint_headers=True)
    response, _ = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, with_hints({**AUTH, **UNGATED}))
    assert response.status == 200 and log.requests()[0]["transform_error"] == "RuntimeError"
    assert Upstream.received[0][3] == REQUEST_BYTES and gated(Upstream.received[0][2]) == set()
    assert Upstream.received[0][2]["X-Claude-Code-Session-Id"] == OLD


def test_the_last_resort_fallback_forwards_the_clis_request_without_the_hints(make_proxy, monkeypatch):
    def broken(headers, beta):
        raise RuntimeError("boom")

    Upstream.mode = "json"
    monkeypatch.setattr("contexto.proxy.add_beta", broken)
    proxy, log = make_proxy(transform=StubEngine("drop-first"), observe_thinking=True, strip_hint_headers=True)
    response, _ = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, with_hints({**AUTH, **UNGATED}))
    assert response.status == 200 and log.requests()[0]["fallback"] is True
    assert Upstream.received[0][3] == REQUEST_BYTES and gated(Upstream.received[0][2]) == set()


def engine_run(make_proxy, headers_for) -> tuple[list[tuple], list[dict]]:
    """Two main turns through the rewrite engine, the second long enough to be paged. Returns what the
    upstream received, as (method, path, header items in order, body), and the request log entries."""
    Upstream.received = []
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    proxy, log = make_proxy(transform=engine, observe_usage=engine.observe_usage, strip_hint_headers=True)
    turns = [*history(8), user("one more prompt")]
    for messages in (turns[:2], turns):
        body = json.dumps(request(messages, metadata=metadata(OLD)), separators=(",", ":")).encode()
        response, _ = call(proxy, "POST", "/v1/messages", body, headers_for({**AUTH, **SESSION}))
        assert response.status == 200
    received = [(method, path, list(headers.items()), body) for method, path, headers, body in Upstream.received]
    return received, log.requests(2)


def test_what_is_forwarded_with_hints_stripped_equals_what_is_forwarded_without_hints(make_proxy):
    """Byte stability and the prompt cache: a request Claude Code sent with the hints on reaches the
    upstream as the same bytes, with the same headers in the same order, as the request it sends
    with them off. The engine read the class in the first run and paged the second request in both."""
    hinted, hinted_log = engine_run(make_proxy, with_hints)
    plain, plain_log = engine_run(make_proxy, dict)
    assert hinted == plain
    assert [e["transform_events"][-1].get("request_class") for e in hinted_log] == ["main", "main"]
    assert all("request_class" not in e["transform_events"][-1] for e in plain_log)
    sent_bytes = json.dumps(request([*history(8), user("one more prompt")], metadata=metadata(OLD)),
                            separators=(",", ":")).encode()
    assert hinted[1][3] != sent_bytes and len(hinted[1][3]) < len(sent_bytes)  # the second request was paged
    assert not any(name.lower() in GATED_HINT_HEADERS for _, _, headers, _ in hinted for name, _ in headers)


# Who turned the hints on --------------------------------------------------------------------------------


@pytest.mark.parametrize("value, on", [("1", True), ("true", True), ("yes", True), ("on", True), ("TRUE", True),
                                       (" 1 ", True), ("0", False), ("false", False), ("2", False), ("", False)])
def test_the_user_turned_the_hints_on_in_the_environment(tmp_path, value, on):
    home = tmp_path / "home"
    home.mkdir()
    env = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": value}
    assert user_enabled_hint_headers(env=env, cwd=tmp_path, home=home) is on
    assert user_enabled_hint_headers(env={}, cwd=tmp_path, home=home) is False


def hint_settings(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": value}}))


def test_a_settings_file_decides_before_the_environment(tmp_path):
    """Measured on 2.1.322: environment 1 with settings 0 sends no hint header, and environment 0 with
    settings 1 sends them."""
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    hint_settings(home / ".claude" / "settings.json", "1")
    assert user_enabled_hint_headers(env={}, cwd=project, home=home) is True
    assert user_enabled_hint_headers(env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "0"}, cwd=project, home=home) is True
    hint_settings(home / ".claude" / "settings.json", "0")
    assert user_enabled_hint_headers(env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}, cwd=project, home=home) is False
    # A project file outranks the user's, as for the base URL; and CLAUDE_CONFIG_DIR moves the user file.
    hint_settings(project / ".claude" / "settings.local.json", "true")
    assert user_enabled_hint_headers(env={}, cwd=project, home=home) is True
    config = tmp_path / "config"
    hint_settings(config / "settings.json", True)  # a JSON true is the text "true"
    assert user_enabled_hint_headers(env={"CLAUDE_CONFIG_DIR": str(config)}, cwd=tmp_path, home=home) is True
    # While CLAUDE_CODE_IGNORE_PERSISTED_TRUST is set no project file counts (D52), so the user's 0 decides.
    trustless = {"CLAUDE_CODE_IGNORE_PERSISTED_TRUST": "1"}
    assert user_enabled_hint_headers(env=trustless, cwd=project, home=home) is False


def test_a_settings_file_that_sets_another_variable_does_not_decide(tmp_path):
    home = tmp_path / "home"
    write_settings(home / ".claude" / "settings.json", "http://user.invalid")
    assert user_enabled_hint_headers(env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}, cwd=tmp_path, home=home) is True
    assert user_enabled_hint_headers(env={}, cwd=tmp_path, home=home) is False


# Review round 2: whose setting it is, an explicit off, and the managed settings file -------------------


def state(tmp_path: Path, env: dict[str, str], *, cwd: Path | None = None) -> str:
    from contexto.hints import hint_headers_state

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return hint_headers_state(env=env, cwd=cwd or tmp_path, home=home, managed=tmp_path / "managed-settings.json")


def test_the_marker_of_an_outer_launch_decides_whose_the_inherited_value_is(tmp_path):
    """Claude Code applies the launcher's ``env`` block to its own environment, so a nested launch
    inherits ``CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`` and the outer launch's marker with it."""
    on = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
    assert state(tmp_path, on) == "forwarded"
    assert state(tmp_path, {**on, "CONTEXTO_HINT_HEADERS": "stripped"}) == "stripped"  # Contexto's own value
    assert state(tmp_path, {**on, "CONTEXTO_HINT_HEADERS": "forwarded"}) == "forwarded"  # the user had them on
    assert state(tmp_path, {**on, "CONTEXTO_HINT_HEADERS": "off"}) == "forwarded"  # the outer launch set nothing
    assert state(tmp_path, {**on, "CONTEXTO_HINT_HEADERS": "STRIPPED "}) == "forwarded"  # only the exact word
    assert user_enabled_hint_headers(env={**on, "CONTEXTO_HINT_HEADERS": "stripped"}, cwd=tmp_path,
                                     home=tmp_path / "home") is False
    # A settings file's value is the user's whatever the marker says.
    hint_settings(tmp_path / "home" / ".claude" / "settings.json", "1")
    assert state(tmp_path, {**on, "CONTEXTO_HINT_HEADERS": "stripped"}) == "forwarded"


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", " 0 "])
def test_an_off_value_in_the_environment_or_the_user_file_is_an_explicit_off(tmp_path, value):
    assert state(tmp_path, {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": value}) == "off"
    hint_settings(tmp_path / "home" / ".claude" / "settings.json", value)
    assert state(tmp_path, {}) == "off"
    assert state(tmp_path, {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}) == "off"  # the file outranks the environment


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_an_explicit_off_counts_inside_a_session_an_outer_launch_stripped(tmp_path, value):
    """Reviewer A, round 2: ``CLAUDE_CODE_GATEWAY_HINT_HEADERS=0 contexto run claude`` run inside a
    session whose launch stripped the hints was ``stripped``, so its --settings asked for them again.
    Only the inherited on value is the outer launch's; an off value is the user's."""
    assert state(tmp_path, {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": value, "CONTEXTO_HINT_HEADERS": "stripped"}) == "off"


@pytest.mark.parametrize("value", ["", "2", "enabled", "maybe"])
def test_a_value_that_is_neither_on_nor_off_leaves_contexto_s_default(tmp_path, value):
    assert state(tmp_path, {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": value}) == "stripped"
    assert state(tmp_path, {}) == "stripped"


def test_on_wins_over_an_explicit_off_where_claude_code_would_send_the_hints(tmp_path):
    """Environment 0 with the user file at 1: Claude Code sends the hints (measured), so the upstream
    was getting them. A project file's 1 outranks the user file's 0 the same way (measured)."""
    hint_settings(tmp_path / "home" / ".claude" / "settings.json", "1")
    assert state(tmp_path, {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "0"}) == "forwarded"
    hint_settings(tmp_path / "home" / ".claude" / "settings.json", "0")
    project = tmp_path / "project"
    hint_settings(project / ".claude" / "settings.json", "1")
    assert state(tmp_path, {}, cwd=project) == "forwarded"
    # A project file's off is not the user's: Contexto's --settings value outranks it (measured).
    hint_settings(project / ".claude" / "settings.json", "0")
    hint_settings(tmp_path / "home" / ".claude" / "settings.json", "2")
    assert state(tmp_path, {}, cwd=project) == "stripped"


def test_a_project_file_follows_the_base_url_s_trust_rule(tmp_path):
    """Read as ``claude -p`` reads it, with no trust record needed, and not at all while
    ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST`` is set: the rule ``contexto run claude`` has for the base URL."""
    project = tmp_path / "project"
    hint_settings(project / ".claude" / "settings.local.json", "1")
    write_settings(project / ".claude" / "settings.json", "http://project.invalid")
    assert state(tmp_path, {}, cwd=project) == "forwarded"
    assert resolve_upstream(env={}, cwd=project, home=tmp_path / "home").source == "settings:project"
    trustless = {"CLAUDE_CODE_IGNORE_PERSISTED_TRUST": "1"}
    assert state(tmp_path, trustless, cwd=project) == "stripped"
    assert resolve_upstream(env=trustless, cwd=project, home=tmp_path / "home").source == "default"


def test_the_managed_settings_file_counts_as_the_user_s(tmp_path):
    """Reviewer A, minor 2: managed settings are the documented place for the variable. They outrank
    every other file and Contexto's own ``--settings``."""
    managed = tmp_path / "managed-settings.json"
    managed.write_text(json.dumps({"env": {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}}))
    assert state(tmp_path, {}) == "forwarded"
    assert state(tmp_path, {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "0"}) == "forwarded"
    nested = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1", "CONTEXTO_HINT_HEADERS": "stripped"}
    assert state(tmp_path, nested) == "forwarded"
    hint_settings(tmp_path / "home" / ".claude" / "settings.json", "0")
    assert state(tmp_path, {}) == "forwarded"
    managed.write_text(json.dumps({"env": {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "0"}}))
    hint_settings(tmp_path / "home" / ".claude" / "settings.json", "1")
    assert state(tmp_path, {}) == "off"  # Contexto cannot turn on what managed settings turn off


@pytest.mark.parametrize("content", [b'\xef\xbb\xbf{"env": {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}}',
                                     b'{"env": {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": 1.0}}',
                                     b'{"env": {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": ["true"]}}'])
def test_the_user_s_file_is_read_as_claude_code_reads_it(tmp_path, content):
    """A byte order mark, a number or a list: Claude Code sends the hints, so the user's gateway keeps them."""
    path = tmp_path / "home" / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    assert state(tmp_path, {}) == "forwarded"


def test_a_file_that_cannot_be_read_decides_nothing_for_the_hints(tmp_path):
    path = tmp_path / "home" / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"env": {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},}')
    assert state(tmp_path, {}) == "stripped"
    assert state(tmp_path, {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}) == "forwarded"
