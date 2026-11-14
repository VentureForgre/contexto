"""D64: the variables that make Claude Code send model requests to another provider, past the proxy.

The variable list and the truthy rule were read from the Claude Code 2.1.318 and 2.1.322 binaries (see
``contexto.providers``). Everything here is synthetic: settings files in a temporary directory and the
launcher's fake ``claude``. No model, no real upstream.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contexto import providers
from contexto.providers import (
    PROVIDER_VARIABLES,
    ProviderOverride,
    bypass_event,
    bypass_warning,
    launch_check,
    provider_overrides,
    source_text,
)
from test_launcher import env, launch, session_log, upstream  # noqa: F401 - env and upstream are fixtures

BEDROCK = "CLAUDE_CODE_USE_BEDROCK"
VERTEX = "CLAUDE_CODE_USE_VERTEX"


def settings(path: Path, block: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": block}))


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def cwd(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


def found(cwd: Path, home: Path, environ: dict[str, str]) -> list[tuple[str, str]]:
    return [(o.variable, o.source) for o in provider_overrides(cwd, home, environ)]


def test_the_variables_are_the_six_claude_code_selects_a_provider_by_in_its_order():
    assert list(PROVIDER_VARIABLES) == [
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_VERTEX"]


@pytest.mark.parametrize("variable", list(PROVIDER_VARIABLES))
def test_each_variable_set_in_the_environment_is_found(cwd, home, variable):
    assert found(cwd, home, {variable: "1"}) == [(variable, "env")]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " 1 ", "true\n", "﻿on"])
def test_claude_code_s_truthy_values_count(cwd, home, value):
    assert found(cwd, home, {BEDROCK: value}) == [(BEDROCK, "env")]


@pytest.mark.parametrize("value", ["", " ", "0", "false", "no", "off", "2", "enabled", "bedrock"])
def test_an_empty_or_other_value_selects_no_provider(cwd, home, value):
    assert found(cwd, home, {BEDROCK: value}) == []


def test_nothing_set_finds_nothing(cwd, home):
    assert provider_overrides(cwd, home, {}) == []


def test_a_settings_env_block_counts_and_names_its_file(cwd, home):
    settings(cwd / ".claude" / "settings.json", {VERTEX: "1"})
    assert found(cwd, home, {}) == [(VERTEX, "settings:project")]
    settings(home / ".claude" / "settings.json", {BEDROCK: "true"})
    assert found(cwd, home, {}) == [(BEDROCK, "settings:user"), (VERTEX, "settings:project")]


def test_the_highest_settings_file_decides_and_settings_outrank_the_environment(cwd, home):
    # Claude Code merges the env blocks key by key, the higher file winning, and applies the result
    # over the environment it started with (M0 finding 1), so an off value above hides an on value below.
    settings(home / ".claude" / "settings.json", {BEDROCK: "1"})
    assert found(cwd, home, {}) == [(BEDROCK, "settings:user")]
    settings(cwd / ".claude" / "settings.json", {BEDROCK: "0"})
    assert found(cwd, home, {BEDROCK: "1"}) == []
    settings(cwd / ".claude" / "settings.local.json", {BEDROCK: "1"})
    assert found(cwd, home, {}) == [(BEDROCK, "settings:local")]
    assert found(cwd, home, {VERTEX: "1"}) == [(BEDROCK, "settings:local"), (VERTEX, "env")]


def test_an_empty_value_in_a_settings_file_unsets_the_variable(cwd, home):
    settings(cwd / ".claude" / "settings.json", {BEDROCK: ""})
    assert found(cwd, home, {BEDROCK: "1"}) == []


@pytest.mark.parametrize("value,on", [(True, True), (1, True), (False, False), (0, False), (2, False)])
def test_a_json_boolean_or_number_is_read_as_its_string(cwd, home, value, on):
    settings(cwd / ".claude" / "settings.json", {BEDROCK: value})
    assert found(cwd, home, {}) == ([(BEDROCK, "settings:project")] if on else [])


@pytest.mark.parametrize("value", [None, ["1"], {"a": "1"}])
def test_a_value_that_is_no_scalar_decides_nothing(cwd, home, value):
    settings(cwd / ".claude" / "settings.json", {BEDROCK: value})
    assert found(cwd, home, {BEDROCK: "1"}) == [(BEDROCK, "env")]


@pytest.mark.parametrize("content", ["not json", "[]", '{"env": "x"}', '{"env": ["x"]}', "{" * 5000])
def test_a_settings_file_that_cannot_be_read_counts_as_absent(cwd, home, content):
    path = cwd / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert found(cwd, home, {BEDROCK: "1"}) == [(BEDROCK, "env")]


def test_the_user_settings_follow_claude_config_dir(cwd, home, tmp_path):
    settings(home / ".claude" / "settings.json", {BEDROCK: "1"})
    settings(tmp_path / "cfg" / "settings.json", {VERTEX: "1"})
    assert found(cwd, home, {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}) == [(VERTEX, "settings:user")]


IGNORING = {"CLAUDE_CODE_IGNORE_PERSISTED_TRUST": "1"}


def test_a_project_file_is_named_as_applying_after_the_dialog_while_persisted_trust_is_ignored(cwd, home):
    # Claude Code in -p mode then drops a project file's env (D52; measured for this variable on 2.1.322,
    # spikes/m2/providers), so the user's file decides. Interactively it applies the project's env once
    # its dialog is accepted (D52, measured for the base URL), so the project's variable is still named.
    settings(cwd / ".claude" / "settings.json", {BEDROCK: "1"})
    settings(home / ".claude" / "settings.json", {VERTEX: "1"})
    overrides = provider_overrides(cwd, home, IGNORING)
    assert [(o.variable, o.source, o.after_trust) for o in overrides] == [
        (VERTEX, "settings:user", False), (BEDROCK, "settings:project", True)]
    assert overrides[0].provider == "Google Vertex AI"  # the one Claude Code uses now comes first


def test_a_project_file_alone_still_warns_while_persisted_trust_is_ignored_and_says_it_may_apply(cwd, home):
    settings(cwd / ".claude" / "settings.local.json", {BEDROCK: "1"})
    overrides = provider_overrides(cwd, home, IGNORING)
    assert [(o.variable, o.source, o.after_trust) for o in overrides] == [(BEDROCK, "settings:local", True)]
    text = bypass_warning(overrides, IGNORING)
    assert text.startswith("warning: CLAUDE_CODE_USE_BEDROCK is set in the env block of .claude/settings.local.json")
    assert "CLAUDE_CODE_IGNORE_PERSISTED_TRUST" in text and "trust dialog" in text and "may" in text
    assert "so Claude Code sends model requests" not in text  # not stated as a fact
    assert "Remove CLAUDE_CODE_USE_BEDROCK from that env block" in text and "\n" not in text
    assert bypass_event(overrides) == {
        "event": "provider_bypass", "provider": "Amazon Bedrock",
        "variables": [{"variable": BEDROCK, "source": "settings:local", "after_trust_dialog": True}]}


def test_a_project_file_that_turns_a_variable_off_changes_nothing_while_persisted_trust_is_ignored(cwd, home):
    settings(cwd / ".claude" / "settings.json", {BEDROCK: "0"})
    assert [(o.variable, o.source, o.after_trust) for o in provider_overrides(cwd, home, {**IGNORING, BEDROCK: "1"})] \
        == [(BEDROCK, "env", False)]
    assert found(cwd, home, {BEDROCK: "1"}) == []  # without the variable the file decides


def test_a_settings_file_that_starts_with_a_byte_order_mark_is_read(cwd, home):
    # Claude Code reads it (measured on 2.1.322, spikes/m2/providers: every request went to the provider).
    path = cwd / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"env": {BEDROCK: "1"}}).encode())
    assert found(cwd, home, {}) == [(BEDROCK, "settings:project")]


@pytest.mark.parametrize("digits", [309, 400, 4300, 4301, 20000])
def test_an_integer_too_large_for_a_float_decides_like_any_other_number(cwd, home, digits):
    # 400 digits raised OverflowError and stopped `contexto run claude`; past Python's limit on integer
    # text (4,300 digits) the whole file counted as absent, and with it a variable the same file turns on.
    path = cwd / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {BEDROCK: 0, VERTEX: "1"}}).replace(": 0,", f": 1{'0' * (digits - 1)},"))
    assert found(cwd, home, {BEDROCK: "1"}) == [(VERTEX, "settings:project")]  # the number is not "1": off


@pytest.mark.parametrize("text", ["1e999", "-1e999", "1.5e300", "NaN", "Infinity"])
def test_a_number_that_does_not_print_as_1_is_off(cwd, home, text):
    path = cwd / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {BEDROCK: 0}}).replace(": 0", f": {text}"))
    assert found(cwd, home, {}) == []


def test_source_text_names_the_place_and_never_a_path(tmp_path):
    assert source_text("env") == "the environment"
    assert source_text("settings:project") == "the env block of .claude/settings.json"
    assert source_text("settings:local") == "the env block of .claude/settings.local.json"
    assert source_text("settings:user") == "the env block of ~/.claude/settings.json"
    config = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert source_text("settings:user", config) == "the env block of $CLAUDE_CONFIG_DIR/settings.json"
    assert str(tmp_path) not in source_text("settings:user", config)


def test_the_warning_names_the_variable_its_source_the_provider_and_what_to_do():
    one = [ProviderOverride(BEDROCK, "Amazon Bedrock", "env")]
    text = bypass_warning(one)
    assert text.startswith("warning: CLAUDE_CODE_USE_BEDROCK is set in the environment, so Claude Code sends ")
    assert "Amazon Bedrock" in text and "not through Contexto" in text
    assert "Unset CLAUDE_CODE_USE_BEDROCK" in text and "\n" not in text
    two = [*one, ProviderOverride(VERTEX, "Google Vertex AI", "settings:project")]
    text = bypass_warning(two)
    # Claude Code takes the first in its own order; the other is still named, with where it is set.
    assert "CLAUDE_CODE_USE_BEDROCK is set in the environment" in text
    assert "CLAUDE_CODE_USE_VERTEX is set in the env block of .claude/settings.json" in text
    assert "Amazon Bedrock" in text and "Google Vertex AI" not in text


def test_the_event_holds_names_and_labels_only():
    event = bypass_event([ProviderOverride(BEDROCK, "Amazon Bedrock", "settings:user")])
    assert event == {"event": "provider_bypass", "provider": "Amazon Bedrock",
                     "variables": [{"variable": BEDROCK, "source": "settings:user"}]}


def test_the_launch_check_gives_the_warning_and_the_event_or_nothing(cwd, home, monkeypatch):
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))
    assert launch_check({}) == (None, None)
    warning, event = launch_check({BEDROCK: "1"})
    assert warning == bypass_warning([ProviderOverride(BEDROCK, "Amazon Bedrock", "env")])
    assert event == bypass_event([ProviderOverride(BEDROCK, "Amazon Bedrock", "env")])


@pytest.mark.parametrize("error", [OverflowError("int too large"), RuntimeError(f"holds {'s' * 8}ecret"),
                                   RecursionError(), MemoryError(), FileNotFoundError()])
def test_the_launch_check_never_raises_and_says_that_it_could_not_check(monkeypatch, error):
    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(providers, "provider_overrides", broken)
    warning, event = launch_check({BEDROCK: "1"})
    # Not silence, which would hide a real bypass; and the error's type only, since its text can hold a path.
    assert warning.startswith("warning: could not check ") and type(error).__name__ in warning
    assert "ecret" not in warning and "\n" not in warning
    assert event == {"event": "provider_bypass_check_failed", "error": type(error).__name__}


# contexto run claude ------------------------------------------------------------------------------


def warnings_in(err: str) -> list[str]:
    return [line for line in err.splitlines() if "CLAUDE_CODE_USE_" in line]


def test_run_claude_warns_before_claude_starts_and_still_launches(env, upstream, tmp_path):  # noqa: F811
    env.update({BEDROCK: "1", "FAKE_REQUEST": "1", "FAKE_SAY": "CLAUDE-IS-RUNNING"})
    proc = launch(env, "--upstream", upstream, "--session", "b1", "claude", "-p", "hi", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    lines = warnings_in(err)
    assert len(lines) == 1, err
    assert lines[0].startswith("contexto: warning: CLAUDE_CODE_USE_BEDROCK is set in the environment")
    assert "Amazon Bedrock" in lines[0]
    assert err.index(lines[0]) < err.index("CLAUDE-IS-RUNNING")  # before Claude Code has the terminal
    assert Path(env["FAKE_RECORD"]).exists()  # claude ran
    events = [e for e in session_log(env, "b1") if e["event"] == "provider_bypass"]
    assert events == [{**events[0], "provider": "Amazon Bedrock",
                       "variables": [{"variable": BEDROCK, "source": "env"}]}]


def test_run_claude_warns_for_a_settings_env_block_and_names_the_file(env, upstream, tmp_path):  # noqa: F811
    settings(tmp_path / ".claude" / "settings.json", {VERTEX: "true"})
    proc = launch(env, "--upstream", upstream, "--session", "b2", "claude", "-p", "hi", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    lines = warnings_in(err)
    assert len(lines) == 1, err
    assert "CLAUDE_CODE_USE_VERTEX is set in the env block of .claude/settings.json" in lines[0]
    assert "Google Vertex AI" in lines[0] and str(tmp_path) not in lines[0]
    assert Path(env["FAKE_RECORD"]).exists()


@pytest.mark.parametrize("value", [None, "", "0", "false"])
def test_run_claude_says_nothing_when_no_provider_is_selected(env, upstream, tmp_path, value):  # noqa: F811
    if value is not None:
        env[BEDROCK] = value
    proc = launch(env, "--upstream", upstream, "--session", "b3", "claude", "-p", "hi", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert warnings_in(err) == []
    assert not any(e["event"] == "provider_bypass" for e in session_log(env, "b3"))


def test_a_settings_value_cannot_stop_the_launch(env, upstream, tmp_path):  # noqa: F811
    # Reviewers A and B, PR #258: a JSON integer of about 400 digits ended the launch with an
    # OverflowError traceback, exit 1, and an empty session left behind.
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {VERTEX: 0}}).replace(": 0", f": 1{'0' * 400}"))
    env.update({"FAKE_REQUEST": "1", "FAKE_SAY": "CLAUDE-IS-RUNNING"})
    proc = launch(env, "--upstream", upstream, "--session", "b4", "claude", "-p", "hi", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0 and "Traceback" not in err, err
    assert "CLAUDE-IS-RUNNING" in err and warnings_in(err) == []
    assert Path(env["FAKE_RECORD"]).exists()


def test_run_claude_warns_for_a_settings_file_with_a_byte_order_mark(env, upstream, tmp_path):  # noqa: F811
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"env": {BEDROCK: "1"}}).encode())
    proc = launch(env, "--upstream", upstream, "--session", "b5", "claude", "-p", "hi", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert len(warnings_in(err)) == 1 and "the env block of .claude/settings.json" in warnings_in(err)[0], err
    assert [e["variables"] for e in session_log(env, "b5") if e["event"] == "provider_bypass"] \
        == [[{"variable": BEDROCK, "source": "settings:project"}]]


def test_a_check_that_raises_is_contained_by_the_launcher(env, upstream, tmp_path):  # noqa: F811
    """Whatever the check raises, Claude Code still starts, and one line says the check did not run. The
    fault is planted in the launcher's own process: a ``sitecustomize`` on its path replaces the lookup."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "sitecustomize.py").write_text(
        "import contexto.providers\n\n\ndef broken(*args, **kwargs):\n    raise ZeroDivisionError('planted')\n\n\n"
        "contexto.providers.provider_overrides = broken\n")
    env.update({BEDROCK: "1", "FAKE_REQUEST": "1", "FAKE_SAY": "CLAUDE-IS-RUNNING",
                "PYTHONPATH": f"{planted}{':' + env['PYTHONPATH'] if env.get('PYTHONPATH') else ''}"})
    proc = launch(env, "--upstream", upstream, "--session", "b6", "claude", "-p", "hi", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0 and "Traceback" not in err, err
    lines = [line for line in err.splitlines() if "could not check" in line]
    assert len(lines) == 1 and lines[0].startswith("contexto: warning: could not check "), err
    assert "ZeroDivisionError" in lines[0] and "planted" not in lines[0]
    assert err.index(lines[0]) < err.index("CLAUDE-IS-RUNNING")
    assert Path(env["FAKE_RECORD"]).exists()  # claude ran
    events = [e for e in session_log(env, "b6") if e["event"].startswith("provider_bypass")]
    assert [(e["event"], e["error"]) for e in events] == [("provider_bypass_check_failed", "ZeroDivisionError")]


# Re-review of the fixes (PR #258)


def test_a_fifo_in_place_of_a_settings_file_is_skipped_without_being_opened(cwd, home):
    """Reviewer A's re-review: the provider check read every settings file, so a named pipe at any of
    their paths blocked `contexto run claude` for ever, where main launched with `--upstream` or with a
    higher file setting the base URL. Claude Code 2.1.322 skips such a file and runs."""
    import os
    import time

    settings(home / ".claude" / "settings.json", {BEDROCK: "1"})
    pipe = cwd / ".claude" / "settings.json"
    pipe.parent.mkdir()
    os.mkfifo(pipe)
    started = time.monotonic()
    assert found(cwd, home, {}) == [(BEDROCK, "settings:user")]
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_settings_file_with_a_non_json_number_counts_as_absent(cwd, home, literal):
    """Reviewer A's re-review: Python accepts these, Claude Code 2.1.322 rejects the file. A "0" in such
    a project file hid an exported 1 from the check, so a real bypass got no warning."""
    path = cwd / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(f'{{"env": {{"{BEDROCK}": "0"}}, "n": {literal}}}')
    assert found(cwd, home, {BEDROCK: "1"}) == [(BEDROCK, "env")]


@pytest.mark.parametrize("value,on", [(1.0000000001, False), (1.0, True), (1e0, True), (0.99999999999, False)])
def test_a_number_is_on_only_when_javascript_prints_it_as_1(cwd, home, value, on):
    """Reviewer A's re-review: numbers were rounded to six digits, so 1.0000000001 read as on."""
    settings(cwd / ".claude" / "settings.json", {BEDROCK: value})
    assert bool(found(cwd, home, {})) is on
