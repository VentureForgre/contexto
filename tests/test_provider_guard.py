"""The cloud-provider guard fails closed, and Claude Code's settings files are read once, with a bound
(D70, review round 2).

The rows marked "measured" were run with the real ``claude`` 2.1.322 at zero spend (a base stub and a
Bedrock stub on 127.0.0.1): Claude Code sent its requests to the Bedrock stub. Every test here gives
its own home, working directory and managed settings path; none reads this machine's Claude files.
"""

from __future__ import annotations

import json
import math
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


@pytest.mark.parametrize("text", ['{"env": {}} // a comment', '{"env": {"OTHER": "1",}}', '{"permissions": ',
                                  '/* note */ {"model": "x"}'])
def test_a_file_claude_code_cannot_load_and_that_names_no_switch_does_not_refuse(tree, text):
    """Reviewer A, round 2: Claude Code does not load a settings file with a comment, a trailing comma
    or a syntax error (measured on 2.1.322), so such a file cannot turn a provider on, and refusing on
    it stopped launches that ran before."""
    write(tree["cwd"] / PROJECT, text)
    assert guard(tree) is None


@pytest.mark.parametrize("text", ['{"env": {"CLAUDE_CODE_USE_BEDROCK": "1",}}',
                                  '{"env": {"CLAUDE_CODE\\u005fUSE_BEDROCK": "1"}} // x',
                                  b'\xff{"env": {"CLAUDE_CODE_USE_VERTEX": "1"}}'])
def test_a_file_that_does_not_parse_and_could_name_a_switch_still_refuses(tree, text):
    write(tree["cwd"] / PROJECT, text)
    found = guard(tree)
    assert found is not None and found.unread


@pytest.mark.parametrize("where", ["user", "project"])
def test_a_utf16le_file_with_a_byte_order_mark_is_read_as_claude_code_reads_it(tree, where):
    """Reviewer A, round 3 (measured on 2.1.322): Claude Code loads a UTF-16LE settings file that starts
    with a byte order mark, and with the switch on there it went to Bedrock while the guard, which saw
    no UTF-8 text naming the switch, let the launch through."""
    path = tree["home"] / USER if where == "user" else tree["cwd"] / PROJECT
    write(path, b"\xff\xfe" + env_block('"1"').encode("utf-16-le"))
    assert places(guard(tree)) == {BEDROCK: (f"settings:{where}",)}
    off = tree["home"] / "off" / USER
    write(off, b"\xff\xfe" + env_block('"0"').encode("utf-16-le"))
    assert upstream.read_settings_file(off) == upstream.SettingsFile({"env": {BEDROCK: "0"}}, None)


@pytest.mark.parametrize("encoding", ["utf-16-be", "utf-16-le", "utf-32"])
def test_a_wide_file_claude_code_does_not_load_still_refuses_when_it_could_name_a_switch(tree, encoding):
    """UTF-16BE, UTF-16LE without the mark and UTF-32 are not loaded by Claude Code (measured), but the
    switch's name in them is not the UTF-8 bytes the parse rule looks for; their NUL bytes count."""
    write(tree["cwd"] / PROJECT, env_block('"1"').encode(encoding))
    found = guard(tree)
    assert found is not None and found.unread


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


@pytest.mark.parametrize("content", [b'{"env": {"CLAUDE_CODE_USE_BEDROCK": "1"},}',
                                     b'{not json "CLAUDE_CODE_USE_MANTLE"',
                                     b'\xff\xfe{"env": {"CLAUDE_CODE_USE_FOUNDRY": "1"}}',
                                     b"[" * 100_000 + b'"CLAUDE_CODE_USE_BEDROCK"'])
def test_a_file_contexto_cannot_parse_refuses(tree, content):
    """Not JSON (a syntax error, or UTF-8 text after a UTF-16LE byte order mark, which decodes to text
    that is not JSON), or nested deeper than the bounded loader allows, and its text could name a
    switch: the guard does not take such a file for "off"."""
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
    write(tree["cwd"] / LOCAL, '{broken "CLAUDE_CODE_USE_BEDROCK"')
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


# The sixth switch (PR #258's spike) ---------------------------------------------------------------------

GOOGLE_CLOUD = "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD"


def test_the_switches_are_the_six_claude_code_reads():
    """Read from the 2.1.318 and 2.1.322 programs (spikes/m2/providers); the five of D70 and
    ``CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD``, which no documentation page names."""
    assert sorted(CLOUD_PROVIDER_KEYS) == sorted([
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS", GOOGLE_CLOUD])
    assert all(name.startswith(upstream.PROVIDER_PREFIX) for name in CLOUD_PROVIDER_KEYS)


@pytest.mark.parametrize("where", ["env", "project"])
def test_claude_platform_on_google_cloud_refuses_the_launch(tree, where):
    """Measured on 2.1.318 (spikes/m2/providers, zero spend): with the switch at 1 in the environment or
    in the project's ``env`` block, every request went to the provider's stub and none to the proxy."""
    env = {GOOGLE_CLOUD: "1"} if where == "env" else {}
    if where == "project":
        write(tree["cwd"] / PROJECT, env_block('"1"', GOOGLE_CLOUD))
    found = guard(tree, env)
    assert found is not None and places(found) == {GOOGLE_CLOUD: ("env" if where == "env" else "settings:project",)}
    assert "sends its requests to Claude Platform on Google Cloud" in cloud_provider_note(found)
    with pytest.raises(LaunchError) as raised:
        check_cloud_provider(tree["cwd"], env, home=tree["home"], managed=tree["managed"])
    assert raised.value.code == EXIT_USAGE


def test_claude_platform_on_google_cloud_off_does_not_refuse(tree):
    assert guard(tree, {GOOGLE_CLOUD: "0"}) is None


# Read exactly when Claude Code reads (D70, review round 4) ----------------------------------------------


def utf16le_settings(*, value: str = '"1"', extra: str = "", odd: bool = False) -> bytes:
    """A UTF-16LE settings file with its byte order mark, the switch at ``value``, and ``extra`` (JSON
    text, which may hold a lone surrogate) as another member; ``odd`` adds one stray byte at the end."""
    text = '{"env": {"' + BEDROCK + '": ' + value + "}" + extra + "}"
    return b"\xff\xfe" + text.encode("utf-16-le", "surrogatepass") + (b"\x20" if odd else b"")


MALFORMED_UTF16LE = {
    "a lone high surrogate": {"extra": ', "note": "a\ud800b"'},
    "a lone low surrogate": {"extra": ', "note": "\udc00"'},
    "an odd trailing byte": {"odd": True},
    "both": {"extra": ', "note": "\ud83d"', "odd": True},
}


@pytest.mark.parametrize("case", sorted(MALFORMED_UTF16LE))
@pytest.mark.parametrize("where", ["user", "project", "local"])
def test_a_malformed_utf16le_file_claude_code_loads_is_read_and_its_switch_refuses(tree, where, case):
    """Reviewers A and B, round 4 (measured on 2.1.322): Claude Code loads a UTF-16LE file with the byte
    order mark even when it holds a lone surrogate or ends in one odd byte, and with the switch on it
    went to Bedrock. Contexto decodes it as Claude Code does, so the refusal names the switch and the
    file rather than calling the file unreadable."""
    base = tree["home"] if where == "user" else tree["cwd"]
    write(base / (LOCAL if where == "local" else USER), utf16le_settings(**MALFORMED_UTF16LE[case]))
    found = guard(tree)
    assert found is not None and places(found) == {BEDROCK: (f"settings:{where}",)} and found.unread == ()
    with pytest.raises(LaunchError) as raised:
        check_cloud_provider(tree["cwd"], {}, home=tree["home"], managed=tree["managed"])
    message = str(raised.value)
    assert f"{BEDROCK} is set in" in message and "could not read" not in message
    assert ("your Claude Code user settings" if where == "user" else
            ".claude/settings.local.json" if where == "local" else ".claude/settings.json") in message


@pytest.mark.parametrize("case", sorted(MALFORMED_UTF16LE))
def test_a_malformed_utf16le_file_with_the_switch_off_launches_and_gives_its_values(tree, case):
    """The same files with the switch at ``"0"``: nothing to refuse, and the file's own values are read
    as Claude Code reads them, lone surrogate included."""
    extra = MALFORMED_UTF16LE[case].get("extra", "") + ', "x": 1'
    path = write(tree["cwd"] / PROJECT, utf16le_settings(value='"0"', extra=extra,
                                                         odd=MALFORMED_UTF16LE[case].get("odd", False)))
    read = upstream.read_settings_file(path)
    assert read.problem is None and read.data is not None and read.data["env"] == {BEDROCK: "0"}
    if "extra" in MALFORMED_UTF16LE[case]:
        assert any(0xD800 <= ord(c) <= 0xDFFF for c in read.data["note"])
        assert upstream.js_string(read.data["note"]) == read.data["note"]
    assert guard(tree) is None
    check_cloud_provider(tree["cwd"], {}, home=tree["home"], managed=tree["managed"])  # no error


def test_a_malformed_utf16le_file_s_base_url_and_hint_setting_are_used(tree):
    """Measured (Reviewer A, round 4): Claude Code sent its requests to the base URL of a UTF-16LE user
    file with an odd trailing byte, as it does for a well-formed one."""
    from contexto.launcher import claude_launch_settings

    text = ('{"env": {"ANTHROPIC_BASE_URL": "http://wide.invalid", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},'
            ' "note": "\ud800"}')
    write(tree["home"] / USER, b"\xff\xfe" + text.encode("utf-16-le", "surrogatepass") + b"\x0a")
    found, hints = claude_launch_settings(None, tree["cwd"], {"ANTHROPIC_BASE_URL": "http://env.invalid"},
                                          home=tree["home"], managed=tree["managed"])
    assert (found.url, found.source, hints) == ("http://wide.invalid", "settings:user", "forwarded")


@pytest.mark.parametrize("content", [
    b"\xff\xfe" + '{"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}} // on'.encode("utf-16-le"),
    b"\xff\xfe" + '{"env": {"CLAUDE_CODE_USE_BEDROCK": "1",}}'.encode("utf-16-le") + b"\x20",
    '{"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}'.encode("utf-16-le"),
], ids=["utf-16-le-mark-comment", "utf-16-le-mark-trailing-comma-odd", "utf-16-le-no-mark"])
def test_a_wide_file_that_still_does_not_parse_refuses_through_its_nul_bytes(tree, content):
    """The NUL rule is the backstop: these files hold the switch's name only as UTF-16LE, never as the
    UTF-8 bytes the parse rule looks for, and no ``\\u`` escape, so the NUL bytes alone mark them."""
    assert upstream.PROVIDER_PREFIX.encode() not in content and b"\\u" not in content and b"\x00" in content
    write(tree["cwd"] / PROJECT, content)
    found = guard(tree)
    assert found is not None and found.on == () and found.unread == (("settings:project", "is not valid JSON"),)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_a_settings_file_claude_code_does_not_load_is_not_read_for_any_setting(tree, constant):
    """Measured (Reviewer B, 2.1.322): Claude Code does not load a settings file holding ``NaN`` or
    ``Infinity``, which Python's parser accepts. Contexto reads no setting from such a file: not the
    base URL, the hint setting or ``apiKeyHelper``."""
    from contexto.launcher import claude_launch_settings

    text = ('{"env": {"ANTHROPIC_BASE_URL": "http://file.invalid", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},'
            ' "apiKeyHelper": "echo key", "x": ' + constant + "}")
    path = write(tree["home"] / USER, text)
    assert upstream.read_settings_file(path) == upstream.SettingsFile(None, "is not valid JSON", False)
    env = {"ANTHROPIC_BASE_URL": "http://env.invalid"}
    found, hints = claude_launch_settings(None, tree["cwd"], env, home=tree["home"], managed=tree["managed"])
    path.unlink()
    absent, absent_hints = claude_launch_settings(None, tree["cwd"], env, home=tree["home"], managed=tree["managed"])
    assert (found, hints) == (absent, absent_hints) and (found.url, found.source) == ("http://env.invalid", "env")
    write(tree["home"] / USER, text)
    assert upstream.claude_key_helper(tree["cwd"], tree["home"], {}).value is None


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_a_file_holding_nan_that_names_a_switch_still_refuses(tree, constant):
    """Not read, and its text names a switch, so the guard refuses on it as on any file that does not
    parse, even with the switch at ``"0"`` there."""
    write(tree["cwd"] / PROJECT, '{"env": {"CLAUDE_CODE_USE_BEDROCK": "0"}, "x": [' + constant + "]}")
    found = guard(tree)
    assert found is not None and found.unread == (("settings:project", "is not valid JSON"),)


def test_the_constants_are_refused_only_in_settings_files(tree):
    """``NaN`` inside a string is ordinary text, an overflowing number is not a constant, and the loader
    the proxy uses for request bodies still accepts the bare constants as before."""
    from contexto.canonical import loads_bounded

    assert math.isnan(loads_bounded('{"x": NaN}')["x"]) and loads_bounded("[-Infinity]") == [float("-inf")]
    path = write(tree["home"] / USER, '{"env": {"ANTHROPIC_BASE_URL": "http://x.invalid/NaN"}, "n": [1e400]}')
    assert upstream.read_settings_file(path) == upstream.SettingsFile(
        {"env": {"ANTHROPIC_BASE_URL": "http://x.invalid/NaN"}, "n": [float("inf")]}, None)


@pytest.mark.parametrize("form", ["utf-16-le", "escape"])
@pytest.mark.parametrize("url", ["http://127.0.0.1:9/a\ud800", "http://\udc00.invalid"])
def test_a_base_url_with_a_lone_surrogate_is_a_usage_error_not_a_crash(tree, url, form):
    """A lone surrogate can reach a settings value (a ``\\ud800`` escape, or a malformed UTF-16LE file).
    Such a URL cannot go into a request line, so the launch stops with a usage error naming the source."""
    from contexto.launcher import check_upstream, claude_launch_settings
    from contexto.proxy import ContextoProxy
    from contexto.store.memory import MemoryStore

    document = {"env": {"ANTHROPIC_BASE_URL": url}}
    write(tree["home"] / USER, json.dumps(document).encode() if form == "escape" else
          b"\xff\xfe" + json.dumps(document, ensure_ascii=False).encode("utf-16-le", "surrogatepass"))
    found, _ = claude_launch_settings(None, tree["cwd"], {}, home=tree["home"], managed=tree["managed"])
    assert (found.url, found.source) == (url, "settings:user")
    with pytest.raises(LaunchError) as raised:
        check_upstream(found)
    assert raised.value.code == EXIT_USAGE and "settings:user" in str(raised.value)
    str(raised.value).encode("utf-8")  # printable
    with pytest.raises(ValueError):
        ContextoProxy(url, MemoryStore())


def test_a_utf8_file_with_an_invalid_byte_is_read_as_claude_code_reads_it(tree):
    """Measured on 2.1.322: Claude Code loads a UTF-8 settings file with a byte that is not UTF-8 (a
    Latin-1 ``é`` in another value), reading the byte as U+FFFD. Its base URL was used, its switch at
    ``"1"`` went to Bedrock, and its ``"0"`` overrode the environment's ``1``."""
    path = write(tree["home"] / USER, b'{"env": {"ANTHROPIC_BASE_URL": "http://file.invalid"}, "x": "caf\xe9"}')
    assert upstream.read_settings_file(path) == upstream.SettingsFile(
        {"env": {"ANTHROPIC_BASE_URL": "http://file.invalid"}, "x": "caf�"}, None)
    found = resolve_upstream(env={"ANTHROPIC_BASE_URL": "http://env.invalid"}, cwd=tree["cwd"], home=tree["home"])
    assert (found.url, found.source) == ("http://file.invalid", "settings:user")
    write(tree["cwd"] / PROJECT, b'{"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}, "x": "caf\xe9"}')
    found = guard(tree)
    assert places(found) == {BEDROCK: ("settings:project",)} and found.unread == ()
    write(tree["cwd"] / PROJECT, b'{"env": {"CLAUDE_CODE_USE_BEDROCK": "\xc0\xb1"}}')  # an overlong 1: off, measured
    assert guard(tree) is None


def test_a_derived_upstream_with_a_lone_surrogate_names_its_source(tmp_path):
    """``contexto run codex`` derives its upstream from Claude Code's and starts the proxy with it; the
    proxy refuses a lone surrogate, and the usage error names where the URL came from."""
    from contexto.launcher import start_proxy
    from contexto.store.memory import MemoryStore

    with pytest.raises(LaunchError) as raised:
        start_proxy(MemoryStore(), "http://127.0.0.1:9/a\ud800/v1", "claude-settings:user+/v1", {}, False)
    assert raised.value.code == EXIT_USAGE
    assert str(raised.value) == "invalid upstream (from claude-settings:user+/v1): upstream holds a lone surrogate"
