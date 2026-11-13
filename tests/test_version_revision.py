"""``contexto --version`` names the revision of a git install, from the PEP 610 ``direct_url.json`` the
installer wrote. Every case here uses fabricated metadata; nothing is installed."""

import json
import os
import subprocess
import sys
from importlib import metadata

import pytest

import contexto
from contexto import cli

COMMIT = "d38e87d2c0ffee0123456789abcdef0123456789"
URL = "https://github.com/VentureForgre/contexto"
PLAIN = f"contexto {contexto.__version__}"


def dist_info(root, direct_url=None, *, raw: bytes | None = None, version: str = "0.0.0+checkout"):
    """A fabricated ``contexto_proxy-<version>.dist-info`` under ``root``, with ``direct_url.json`` holding
    ``direct_url`` (a JSON value) or the bytes ``raw``; neither given means the file is absent."""
    info = root / f"contexto_proxy-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.4\nName: contexto-proxy\nVersion: {version}\n")
    if raw is not None:
        (info / "direct_url.json").write_bytes(raw)
    elif direct_url is not None:
        (info / "direct_url.json").write_text(json.dumps(direct_url))
    return info


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Make ``contexto`` read its metadata from a fabricated dist-info: ``installed(direct_url)``."""
    def install(direct_url=None, *, raw=None):
        info = dist_info(tmp_path / "site", direct_url, raw=raw)
        wanted = []

        def distribution(name):
            wanted.append(name)
            return metadata.PathDistribution(info)

        monkeypatch.setattr(contexto, "distribution", distribution)
        return wanted
    return install


def vcs(**vcs_info):
    return {"url": URL, "vcs_info": {"vcs": "git", **vcs_info}}


def test_a_tag_install_names_the_tag_and_the_commit(installed):
    wanted = installed(vcs(requested_revision="v0.5.24", commit_id=COMMIT))
    assert contexto.version_line() == f"{PLAIN} (git v0.5.24, commit d38e87d2)"
    assert wanted == ["contexto-proxy"]


@pytest.mark.parametrize("revision", ["main", "claude/pypi-packaging", "release_1.2+fix", "Release/V2", "x" * 64])
def test_a_branch_install_names_the_branch(installed, revision):
    installed(vcs(requested_revision=revision, commit_id=COMMIT))
    assert contexto.version_line() == f"{PLAIN} (git {revision}, commit d38e87d2)"


def test_no_requested_revision_names_the_commit_only(installed):
    installed(vcs(commit_id=COMMIT))
    assert contexto.version_line() == f"{PLAIN} (git commit d38e87d2)"


@pytest.mark.parametrize("revision", [COMMIT, COMMIT[:8], COMMIT[:12], COMMIT.upper()])
def test_a_requested_revision_that_is_the_commit_is_not_printed_twice(installed, revision):
    installed(vcs(requested_revision=revision, commit_id=COMMIT))
    assert contexto.version_line() == f"{PLAIN} (git commit d38e87d2)"


def test_a_revision_shorter_than_gits_shortest_abbreviation_is_a_name(installed):
    """Seven characters is git's shortest default abbreviation: a 7-character prefix of the commit is the
    commit, a 6-character one is printed as the name it may be (a branch can be called ``d38e87``)."""
    installed(vcs(requested_revision=COMMIT[:7], commit_id=COMMIT))
    assert contexto.version_line() == f"{PLAIN} (git commit d38e87d2)"
    assert contexto._names_commit(COMMIT[:7], COMMIT) is True
    assert contexto._names_commit(COMMIT[:6], COMMIT) is False


def test_a_six_character_prefix_of_the_commit_is_printed_as_a_revision(installed):
    installed(vcs(requested_revision=COMMIT[:6], commit_id=COMMIT))
    assert contexto.version_line() == f"{PLAIN} (git d38e87, commit d38e87d2)"


def test_an_uppercase_commit_id_is_printed_in_lowercase(installed):
    installed(vcs(requested_revision="main", commit_id=COMMIT.upper()))
    assert contexto.version_line() == f"{PLAIN} (git main, commit d38e87d2)"


def test_an_uppercase_commit_id_alone_is_printed_in_lowercase(installed):
    installed(vcs(commit_id=COMMIT.upper()))
    assert contexto.version_line() == f"{PLAIN} (git commit d38e87d2)"


@pytest.mark.parametrize("fill,over,expected", [
    ("x", 0, f"{PLAIN} (git v0.5.24, commit d38e87d2)"),  # exactly the limit: parsed
    ("x", 1, PLAIN),  # one character over: not parsed
    ("é", 0, f"{PLAIN} (git v0.5.24, commit d38e87d2)"),  # the limit counts characters, not bytes
])
def test_the_size_limit_of_direct_url_json_is_65536_characters(installed, fill, over, expected):
    empty = json.dumps({**vcs(requested_revision="v0.5.24", commit_id=COMMIT), "pad": ""}, ensure_ascii=False)
    text = empty.replace('"pad": ""', '"pad": "' + fill * (65_536 + over - len(empty)) + '"')
    assert len(text) == 65_536 + over
    installed(raw=text.encode())
    assert contexto.version_line() == expected


def test_version_goes_to_stderr_when_stdout_is_closed(installed, capsys, monkeypatch):
    installed(vcs(commit_id=COMMIT))
    monkeypatch.setattr(sys, "stdout", None)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    monkeypatch.undo()
    assert exc.value.code == 0
    assert capsys.readouterr().err == f"{PLAIN} (git commit d38e87d2)\n"


def test_a_sha256_commit_id_is_cut_to_eight_characters(installed):
    installed(vcs(requested_revision="main", commit_id="0123456789abcdef" * 4))
    assert contexto.version_line() == f"{PLAIN} (git main, commit 01234567)"


@pytest.mark.parametrize("revision", [
    "",  # empty
    "x" * 65,  # not short
    "v1 && rm -rf x",  # spaces and shell characters
    "v1\x1b[31m",  # a terminal escape
    "v1\nsecond line",
    "café",  # not ASCII
    "-rf",  # reads as an option
    7,
    ["v1"],
    {"tag": "v1"},
    True,
    None,
])
def test_a_requested_revision_that_is_not_a_short_safe_string_is_left_out(installed, revision):
    installed(vcs(requested_revision=revision, commit_id=COMMIT))
    assert contexto.version_line() == f"{PLAIN} (git commit d38e87d2)"


@pytest.mark.parametrize("commit", [
    None,
    "",
    "d38e87d",  # shorter than what is printed
    "not-hex-" * 5,
    "d38e87d2 extra",
    "d38e87d2\n",
    "٤" * 40,  # digits, but not ASCII hex
    "a" * 65,
    0xD38E87D2,
    [COMMIT],
])
def test_without_a_hex_commit_id_nothing_is_added(installed, commit):
    installed(vcs(requested_revision="v0.5.24", commit_id=commit))
    assert contexto.version_line() == PLAIN


def test_a_missing_commit_id_adds_nothing(installed):
    installed(vcs(requested_revision="v0.5.24"))
    assert contexto.version_line() == PLAIN


@pytest.mark.parametrize("direct_url", [
    None,  # no direct_url.json: an install from an index or from a wheel file
    {"url": "file:///src/contexto", "dir_info": {}},
    {"url": "file:///src/contexto", "dir_info": {"editable": True}},
    {"url": URL + "/releases/download/v0.5.24/contexto_proxy-0.5.24-py3-none-any.whl",
     "archive_info": {"hash": "sha256=" + "0" * 64, "hashes": {"sha256": "0" * 64}}},
    {"url": URL},
    {"url": URL, "vcs_info": None},
    {"url": URL, "vcs_info": "git"},
    {"url": URL, "vcs_info": [{"vcs": "git", "commit_id": COMMIT}]},
    {"url": URL, "vcs_info": {"commit_id": COMMIT}},  # no vcs
    {"url": URL, "vcs_info": {"vcs": "hg", "commit_id": COMMIT}},
    {"url": URL, "vcs_info": {"vcs": ["git"], "commit_id": COMMIT}},
    [],
    "git",
    7,
])
def test_anything_but_a_git_install_prints_the_version_alone(installed, direct_url):
    installed(direct_url)
    assert contexto.version_line() == PLAIN


@pytest.mark.parametrize("raw", [
    b"",
    b"{",
    b"not json",
    b"null",
    b'{"vcs_info": {"vcs": "git", "commit_id": "' + COMMIT.encode() + b'"',  # cut short
    b"\xff\xfe\x00{",  # not UTF-8
    b"[" * 100_000,  # nested past the JSON decoder's recursion limit
    b'{"vcs_info": {"vcs": "git", "commit_id": NaN}}',
])
def test_malformed_direct_url_json_prints_the_version_alone(installed, raw):
    installed(raw=raw)
    assert contexto.version_line() == PLAIN


def test_an_unreadable_direct_url_json_prints_the_version_alone(tmp_path, monkeypatch):
    info = dist_info(tmp_path)
    (info / "direct_url.json").mkdir()  # a directory where the file should be
    monkeypatch.setattr(contexto, "distribution", lambda name: metadata.PathDistribution(info))
    assert contexto.version_line() == PLAIN


@pytest.mark.parametrize("error", [
    metadata.PackageNotFoundError("contexto-proxy"), OSError("unreadable"), ValueError("bad"), RecursionError(),
])
def test_a_failing_metadata_lookup_never_raises(monkeypatch, error):
    def distribution(name):
        raise error

    monkeypatch.setattr(contexto, "distribution", distribution)
    assert contexto.version_line() == PLAIN


def test_the_revision_does_not_change_dunder_version(installed):
    before = contexto.__version__
    installed(vcs(requested_revision="v0.5.24", commit_id=COMMIT))
    assert contexto.version_line().startswith(f"contexto {before} (git ")
    assert contexto.__version__ == before == metadata.version("contexto-proxy")


def test_version_flag_prints_the_revision(installed, capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    installed(vcs(requested_revision="v0.5.24", commit_id=COMMIT))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"{PLAIN} (git v0.5.24, commit d38e87d2)\n"


@pytest.mark.parametrize("columns", ["20", "54", None])
def test_the_version_line_is_never_wrapped(installed, capsys, monkeypatch, columns):
    """argparse's own version action fills its text to the terminal width (80 when piped), which cut a
    git install's line in two, once inside a branch name."""
    if columns is None:
        monkeypatch.delenv("COLUMNS", raising=False)
    else:
        monkeypatch.setenv("COLUMNS", columns)
    revision = "claude/" + "a-long-branch-name-" * 3
    assert len(revision) == 64
    installed(vcs(requested_revision=revision, commit_id=COMMIT))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"{PLAIN} (git {revision}, commit d38e87d2)\n"


def test_the_metadata_is_read_only_when_the_version_is_asked_for(installed, capsys):
    wanted = installed(vcs(requested_revision="v0.5.24", commit_id=COMMIT))
    parser = cli.build_parser()
    assert "--version" in parser.format_help()
    assert wanted == []
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])
    assert wanted == ["contexto-proxy"]
    capsys.readouterr()


def test_version_flag_without_a_git_install_is_unchanged(installed, capsys):
    installed({"url": "file:///src/contexto", "dir_info": {"editable": True}})
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out == f"{PLAIN}\n"


def run_version(tmp_path, direct_url, version, columns="80"):
    """``python -m contexto --version`` in a new process that finds a fabricated dist-info first. The
    ``contexto`` package itself is still the one under test: the fabricated directory holds metadata only."""
    site = tmp_path / "site"
    dist_info(site, direct_url, version=version)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(site), os.environ.get("PYTHONPATH")]))}
    env.pop("COLUMNS", None)
    if columns is not None:
        env["COLUMNS"] = columns
    return subprocess.run([sys.executable, "-m", "contexto", "--version"], capture_output=True, text=True, env=env)


def test_a_new_process_reads_the_revision_from_installed_metadata(tmp_path):
    out = run_version(tmp_path, vcs(requested_revision="v9.9.9", commit_id=COMMIT), "9.9.9")
    assert (out.returncode, out.stdout, out.stderr) == (0, "contexto 9.9.9 (git v9.9.9, commit d38e87d2)\n", "")


@pytest.mark.parametrize("columns", ["20", None])
def test_a_new_process_prints_a_long_revision_on_one_line(tmp_path, columns):
    """Piped, with COLUMNS unset, is how a script or a CI step reads the version."""
    revision = "x" * 64
    out = run_version(tmp_path, vcs(requested_revision=revision, commit_id=COMMIT), "9.9.9", columns=columns)
    assert (out.returncode, out.stdout, out.stderr) == (
        0, f"contexto 9.9.9 (git {revision}, commit d38e87d2)\n", "")


def test_a_new_process_with_a_local_directory_install_prints_the_version_alone(tmp_path):
    out = run_version(tmp_path, {"url": "file:///src/contexto", "dir_info": {}}, "9.9.9")
    assert (out.returncode, out.stdout, out.stderr) == (0, "contexto 9.9.9\n", "")


def test_reading_the_revision_starts_no_process_and_opens_no_socket(installed, monkeypatch):
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("contexto --version must not run a process or use the network")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(os, "system", refuse)
    installed(vcs(requested_revision="v0.5.24", commit_id=COMMIT))
    assert contexto.version_line() == f"{PLAIN} (git v0.5.24, commit d38e87d2)"
