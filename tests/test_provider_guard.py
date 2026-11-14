"""The cloud-provider guard fails closed, and Claude Code's settings files are read once, with a bound
(D70, review round 2).

The rows marked "measured" were run with the real ``claude`` 2.1.322 at zero spend (a base stub and a
Bedrock stub on 127.0.0.1): Claude Code sent its requests to the Bedrock stub. Every test here gives
its own home, working directory and managed settings path; none reads this machine's Claude files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from contexto import upstream
from contexto.launcher import EXIT_USAGE, LaunchError, check_cloud_provider
from contexto.upstream import CLOUD_PROVIDER_KEYS, cloud_provider, cloud_provider_note, resolve_upstream

BEDROCK = "CLAUDE_CODE_USE_BEDROCK"
ON = {BEDROCK: "1"}


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    """A home, a project folder and a managed settings path, all temporary and all empty."""
    paths = {"home": tmp_path / "home", "cwd": tmp_path / "project", "managed": tmp_path / "managed-settings.json"}
    paths["home"].mkdir()
    paths["cwd"].mkdir()
    return paths


def guard(tree: dict[str, Path], env: dict[str, str] | None = None):
    return cloud_provider(env=env or {}, cwd=tree["cwd"], home=tree["home"], managed=tree["managed"])


def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


def env_block(value: str, name: str = BEDROCK) -> str:
    """A settings file whose ``env`` block holds ``name`` with the JSON text ``value``."""
    return '{"env": {"' + name + '": ' + value + "}}"


def places(found) -> dict[str, tuple[str, ...]]:
    return dict(found.on)


USER = Path(".claude") / "settings.json"
PROJECT = Path(".claude") / "settings.json"
LOCAL = Path(".claude") / "settings.local.json"


# Reviewer B's table: Claude Code went to Bedrock and the guard let the launch through ------------------


@pytest.mark.parametrize("value", ["1.0", "1e0", '["1"]', "[1]", '[["1"]]', "[true]", '" On "', "true", "1", '"yes"'])
@pytest.mark.parametrize("where", ["user", "project", "local"])
def test_a_value_claude_code_reads_as_on_refuses_whatever_its_json_type(tree, where, value):
    """Measured: ``1.0``, ``1e0`` and ``["1"]`` in a user or project file, and ``[1]``, ``[["1"]]``,
    ``[true]`` and ``" On "`` in the user file, sent every request to the Bedrock stub. JavaScript's
    ``String()`` gives ``1`` for the numbers and joins a list's items with commas."""
    base = tree["home"] if where == "user" else tree["cwd"]
    write(base / (LOCAL if where == "local" else USER), env_block(value))
    found = guard(tree)
    assert found is not None and places(found) == {BEDROCK: (f"settings:{where}",)}


@pytest.mark.parametrize("value", ['"0"', "0", "false", "1.5", "[]", '["1","1"]', "null", '{"a": 1}', '"1.0"', '""',
                                   '"2"', '"enabled"', "[null]", '[[], 1]'])
def test_a_value_claude_code_reads_as_off_does_not_refuse(tree, value):
    """Measured in the user file: ``false``, ``1.5``, ``[]``, ``["1","1"]`` (the text ``1,1``), ``null``,
    an object (``[object Object]``) and the text ``"1.0"`` sent every request to the base URL."""
    write(tree["home"] / USER, env_block(value))
    assert guard(tree) is None


@pytest.mark.parametrize("where", ["user", "project"])
def test_a_file_that_starts_with_a_byte_order_mark_is_read(tree, where):
    """Measured: Claude Code reads a settings file that starts with a UTF-8 BOM, and went to Bedrock."""
    base = tree["home"] if where == "user" else tree["cwd"]
    write(base / USER, b"\xef\xbb\xbf" + env_block('"1"').encode())
    assert places(guard(tree)) == {BEDROCK: (f"settings:{where}",)}


@pytest.mark.parametrize("project_file", [
    '{"env": {"CLAUDE_CODE_USE_BEDROCK": "0"}, "x": NaN}',  # measured: Claude Code does not apply this file
    '{"env": {"CLAUDE_CODE_USE_BEDROCK": "0"}, "permissions": "garbage"}',  # measured: a schema error, the same
    '{"env": {"CLAUDE_CODE_USE_BEDROCK": "0"}}',  # measured: not applied with --setting-sources user
])
def test_a_project_file_that_says_off_does_not_hide_the_environment(tree, project_file):
    """Measured: with the process environment at 1, Claude Code went to Bedrock in all three cases,
    because it did not apply the project file. Contexto cannot tell which files Claude Code applies,
    so a file can only add a refusal."""
    write(tree["cwd"] / PROJECT, project_file)
    assert places(guard(tree, ON)) == {BEDROCK: ("env",)}


def test_a_file_that_says_off_does_not_hide_a_file_below_it(tree):
    write(tree["cwd"] / LOCAL, env_block('"0"'))
    write(tree["cwd"] / PROJECT, env_block('"false"'))
    write(tree["home"] / USER, env_block('"1"'))
    assert places(guard(tree)) == {BEDROCK: ("settings:user",)}


def test_every_place_a_switch_is_on_is_reported_in_the_order_of_the_files(tree):
    write(tree["cwd"] / LOCAL, env_block("true"))
    write(tree["cwd"] / PROJECT, env_block('"0"'))
    write(tree["home"] / USER, env_block('"on"'))
    write(tree["managed"], env_block('"1"', "CLAUDE_CODE_USE_VERTEX"))
    found = guard(tree, {BEDROCK: "yes", "CLAUDE_CODE_USE_FOUNDRY": "0"})
    assert places(found) == {BEDROCK: ("env", "settings:local", "settings:user"),
                             "CLAUDE_CODE_USE_VERTEX": ("settings:managed",)}
    note = cloud_provider_note(found)
    assert f"{BEDROCK} is set in the process environment, in the env block of .claude/settings.local.json" in note
    assert "your Claude Code user settings" in note and "Amazon Bedrock" in note
    assert "CLAUDE_CODE_USE_VERTEX is set in the env block of Claude Code's managed settings file" in note
    assert str(tree["home"]) not in note and str(tree["cwd"]) not in note  # no path of the user's


# Trust: a project file can only cause a refusal ---------------------------------------------------------


@pytest.mark.parametrize("trust", ["1", "true", "on"])
@pytest.mark.parametrize("kind", ["local", "project"])
def test_a_project_file_counts_while_persisted_trust_is_ignored(tree, kind, trust):
    """``claude -p`` drops a project file's ``env`` while ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST`` is set
    (measured: the requests then went to the base URL), and the interactive CLI applies it once its
    dialog is accepted. The guard does not predict which: the switch is on in a file, so it refuses."""
    write(tree["cwd"] / (LOCAL if kind == "local" else PROJECT), env_block('"1"'))
    found = guard(tree, {"CLAUDE_CODE_IGNORE_PERSISTED_TRUST": trust})
    assert places(found) == {BEDROCK: (f"settings:{kind}",)}


def test_the_main_checkout_s_local_file_counts_from_a_subdirectory(tree):
    repo = tree["cwd"]
    (repo / ".git").mkdir()
    sub = repo / "sub"
    sub.mkdir()
    write(repo / LOCAL, env_block('"1"'))
    found = cloud_provider(env={}, cwd=sub, home=tree["home"], managed=tree["managed"])
    assert places(found) == {BEDROCK: ("settings:main-checkout-local",)}


def test_a_folder_with_no_trust_record_is_checked_like_any_other(tree):
    """No trust record anywhere under the temporary home: the project file still counts."""
    write(tree["cwd"] / PROJECT, env_block('"1"', "CLAUDE_CODE_USE_MANTLE"))
    assert places(guard(tree)) == {"CLAUDE_CODE_USE_MANTLE": ("settings:project",)}


# Managed settings ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CLOUD_PROVIDER_KEYS))
def test_a_switch_in_the_managed_settings_file_refuses(tree, name):
    write(tree["managed"], env_block("true", name))
    found = guard(tree)
    assert places(found) == {name: ("settings:managed",)}
    assert "managed settings file" in cloud_provider_note(found)


def test_a_managed_file_that_says_off_does_not_hide_the_user_s_switch(tree):
    write(tree["managed"], env_block('"0"'))
    write(tree["home"] / USER, env_block('"1"'))
    assert places(guard(tree)) == {BEDROCK: ("settings:user",)}


def test_the_managed_settings_path_is_the_platform_s_file_and_no_variable_moves_it(monkeypatch, tmp_path):
    monkeypatch.setattr(upstream, "MANAGED_SETTINGS_FILES",
                        {"darwin": "/Library/Application Support/ClaudeCode/managed-settings.json",
                         "linux": "/etc/claude-code/managed-settings.json"})
    monkeypatch.setenv("CONTEXTO_CLAUDE_MANAGED_SETTINGS", str(tmp_path / "m.json"))  # the old override
    monkeypatch.setattr(upstream.sys, "platform", "darwin")
    assert upstream.managed_settings_path() == Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    monkeypatch.setattr(upstream.sys, "platform", "linux")
    assert upstream.managed_settings_path() == Path("/etc/claude-code/managed-settings.json")
    monkeypatch.setattr(upstream.sys, "platform", "win32")
    assert upstream.managed_settings_path() is None


def test_the_suite_never_reads_this_machine_s_managed_settings():
    """conftest points the table of managed settings paths at a temporary directory for this process."""
    path = upstream.managed_settings_path()
    assert path is not None and not path.exists()
    assert not str(path).startswith(("/Library", "/etc"))


def test_a_number_past_the_largest_double_reads_as_infinity_and_is_off(tree):
    """Reviewer B, round 2: a JSON integer of 309 or more digits raised OverflowError and the launch
    ended with a traceback. Claude Code reads such a number as Infinity, which is off."""
    huge = "9" * 400
    assert upstream.js_string(int(huge)) == "Infinity" and upstream.js_string(-int(huge)) == "-Infinity"
    write(tree["home"] / USER, env_block(huge))
    assert guard(tree) is None


# The reader ---------------------------------------------------------------------------------------------


def test_a_missing_file_is_empty_and_no_problem(tree):
    found = upstream.read_settings_file(tree["home"] / USER)
    assert found.data is None and found.problem is None
    assert guard(tree) is None and guard(tree, {BEDROCK: "0"}) is None


def test_an_empty_or_blank_file_holds_nothing(tree):
    for content in (b"", b"  \n", b"\xef\xbb\xbf\n"):
        path = write(tree["home"] / USER, content)
        assert upstream.read_settings_file(path) == upstream.SettingsFile({}, None)
    assert guard(tree) is None


def test_a_document_that_is_not_an_object_holds_nothing(tree):
    path = write(tree["home"] / USER, '["CLAUDE_CODE_USE_BEDROCK", "1"]')
    assert upstream.read_settings_file(path) == upstream.SettingsFile({}, None)
    write(tree["cwd"] / PROJECT, '{"env": "CLAUDE_CODE_USE_BEDROCK=1"}')
    assert guard(tree) is None


def test_a_file_over_the_cap_is_not_read_and_refuses(tree):
    """A file larger than the cap is not read. It is not evidence that the switch is off, so the
    guard refuses and names the file. A file of exactly the cap is read."""
    padding = " " * upstream.SETTINGS_FILE_LIMIT
    body = '{"env": {"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BASE_URL": "http://big.invalid"}' + padding + "}"
    path = write(tree["cwd"] / PROJECT, body)
    read = upstream.read_settings_file(path)
    assert read.data is None and read.problem == "is larger than 4 MiB"
    found = guard(tree)
    assert found is not None and found.on == () and found.unread == (("settings:project", "is larger than 4 MiB"),)
    assert ".claude/settings.json" in cloud_provider_note(found) and "4 MiB" in cloud_provider_note(found)
    assert resolve_upstream(env={}, cwd=tree["cwd"], home=tree["home"]).source == "default"
    at_the_cap = '{"env": {"ANTHROPIC_BASE_URL": "http://fits.invalid"}}'
    path.write_text(at_the_cap + " " * (upstream.SETTINGS_FILE_LIMIT - len(at_the_cap)))
    assert path.stat().st_size == upstream.SETTINGS_FILE_LIMIT
    assert guard(tree) is None
    assert resolve_upstream(env={}, cwd=tree["cwd"], home=tree["home"]).url == "http://fits.invalid"


@pytest.mark.parametrize("content", [b'{"env": {"CLAUDE_CODE_USE_BEDROCK": "1"},}', b"{not json",
                                     b'\xff\xfe{"env": {}}', b"[" * 100_000])
def test_a_file_contexto_cannot_parse_refuses(tree, content):
    """Not JSON for Python, not UTF-8, or nested deeper than the bounded loader allows: Claude Code's
    reading of such a file is not known, so the guard does not take it for "off"."""
    write(tree["home"] / USER, content)
    found = guard(tree)
    assert found is not None and found.unread == (("settings:user", "is not valid JSON"),)
    note = cloud_provider_note(found)
    assert "could not read" in note and "your Claude Code user settings" in note


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_is_never_opened_for_reading_and_refuses(tree):
    """Opened without blocking, found not to be a regular file, and not read."""
    path = tree["cwd"] / PROJECT
    path.parent.mkdir()
    os.mkfifo(path)
    read = upstream.read_settings_file(path)
    assert read.data is None and read.problem == "is not a regular file"
    assert guard(tree).unread == (("settings:project", "is not a regular file"),)
    assert resolve_upstream(env={}, cwd=tree["cwd"], home=tree["home"]).source == "default"


def test_a_directory_or_a_file_this_user_cannot_open_passes(tree):
    """Claude Code, run by the same user, cannot read these either, so they cannot turn a switch on."""
    (tree["cwd"] / PROJECT).mkdir(parents=True)
    assert upstream.read_settings_file(tree["cwd"] / PROJECT) == upstream.SettingsFile(None, None)
    locked = write(tree["home"] / USER, env_block('"1"'))
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as a user that can read any file")
        assert upstream.read_settings_file(locked) == upstream.SettingsFile(None, None)
        assert guard(tree) is None
    finally:
        locked.chmod(0o600)


def test_each_settings_file_is_read_once_for_the_whole_launch(tree, monkeypatch):
    """The guard's five switches, the base URL and the hint setting all come from one read of each file."""
    from contexto.launcher import claude_launch_settings

    write(tree["cwd"] / LOCAL, env_block('"0"'))
    write(tree["cwd"] / PROJECT, '{"env": {"ANTHROPIC_BASE_URL": "http://project.invalid"}}')
    write(tree["home"] / USER, env_block('"1"', "CLAUDE_CODE_GATEWAY_HINT_HEADERS"))
    write(tree["managed"], "{}")
    opened: list[Path] = []
    real = upstream.read_settings_file
    monkeypatch.setattr(upstream, "read_settings_file", lambda path: opened.append(path) or real(path))
    found, hints = claude_launch_settings(None, tree["cwd"], {}, home=tree["home"], managed=tree["managed"])
    assert (found.url, found.source, hints) == ("http://project.invalid", "settings:project", "forwarded")
    assert sorted(opened) == sorted([tree["cwd"] / LOCAL, tree["cwd"] / PROJECT, tree["home"] / USER, tree["managed"]])


def test_a_json_number_is_coerced_as_javascript_would():
    cases = [("x", "x"), (True, "true"), (False, "false"), (None, "null"), (1, "1"), (1.0, "1"), (1e0, "1"),
             (-0.0, "0"), (1.5, "1.5"), (2, "2"), (10.0, "10"), (1e21, "1e+21"), ([], ""), (["1"], "1"),
             ([1, 2], "1,2"), ([[1, 2], 3], "1,2,3"), ([None, 1], ",1"), ([[], [1]], ",1"),
             ({"a": 1}, "[object Object]"), ([{"a": 1}], "[object Object]"), (float("nan"), "NaN"),
             (float("inf"), "Infinity"), (float("-inf"), "-Infinity")]
    for value, text in cases:
        assert upstream.js_string(value) == text, value
    deep: list = []
    for _ in range(5000):
        deep = [deep]
    assert upstream.js_string(deep) == ""  # no recursion


# The launcher's check -----------------------------------------------------------------------------------


def test_check_cloud_provider_takes_the_home_and_names_every_place_and_what_to_do(tree):
    write(tree["cwd"] / PROJECT, env_block("1.0"))
    check_cloud_provider(tree["home"], {}, home=tree["home"], managed=tree["managed"])  # nothing on: no error
    with pytest.raises(LaunchError) as raised:
        check_cloud_provider(tree["cwd"], ON, home=tree["home"], managed=tree["managed"])
    message = str(raised.value)
    assert raised.value.code == EXIT_USAGE
    assert f"{BEDROCK} is set in the process environment and in the env block of .claude/settings.json" in message
    assert f"env -u {BEDROCK}" in message and f"remove {BEDROCK} from" in message
    assert "no bound" in message and "without contexto" in message
    assert "whichever" in message  # it says a file that turns the switch off does not lift the refusal


def test_check_cloud_provider_with_only_an_unreadable_file_says_how_to_get_past_it(tree):
    write(tree["cwd"] / LOCAL, "{broken")
    with pytest.raises(LaunchError) as raised:
        check_cloud_provider(tree["cwd"], {}, home=tree["home"], managed=tree["managed"])
    message = str(raised.value)
    assert raised.value.code == EXIT_USAGE
    assert "could not read .claude/settings.local.json (it is not valid JSON)" in message
    assert "cannot tell" in message and "Fix or remove" in message


def test_the_home_parameter_keeps_the_check_off_the_real_home(tree, monkeypatch):
    """Reviewer B, minor 2: a switch in the real home's settings must not reach a check given another home."""
    real = tree["cwd"] / "realhome"
    write(real / USER, env_block('"1"'))
    monkeypatch.setenv("HOME", str(real))
    check_cloud_provider(tree["cwd"], {}, home=tree["home"], managed=tree["managed"])
    with pytest.raises(LaunchError):
        check_cloud_provider(tree["cwd"], {}, home=real, managed=tree["managed"])


def test_a_refused_launch_reads_nothing_twice_and_decides_nothing_else(tree):
    from contexto.launcher import claude_launch_settings

    write(tree["home"] / USER, json.dumps({"env": {BEDROCK: ["1"], "ANTHROPIC_BASE_URL": "http://user.invalid"}}))
    with pytest.raises(LaunchError):
        claude_launch_settings(None, tree["cwd"], {}, home=tree["home"], managed=tree["managed"])
