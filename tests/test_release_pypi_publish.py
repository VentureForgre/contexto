"""The PyPI job's checks (.github/scripts/pypi-publish.py).

The script never uploads. It verifies the files downloaded from a GitHub Release, asks PyPI what it
already holds for the version, and says which files, if any, may be uploaded. Nothing here talks to
pypi.org: the index is an injected function or a server on 127.0.0.1.
"""

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "pypi-publish.py"
PROJECT = "contexto-proxy"
VERSION = "9.9.9"
TAG = "v9.9.9"
WHEEL = "contexto_proxy-9.9.9-py3-none-any.whl"
SDIST = "contexto_proxy-9.9.9.tar.gz"
INDEX = "https://index.invalid"


def _load():
    spec = importlib.util.spec_from_file_location("release_pypi_publish", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pypi = _load()


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_dist(directory, *, name=PROJECT, version=VERSION, sdist_name=None, sdist_version=None, stem=None):
    """A wheel, an sdist and SHA256SUMS shaped like release-build.sh's output."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = stem or f"{name.replace('-', '_')}-{version}"
    wheel = directory / f"{stem}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{stem}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nSummary: test\n\nName: not-a-header\n",
        )
        archive.writestr("contexto/__init__.py", "")
    sdist = directory / f"{stem}.tar.gz"
    pkg_info = (
        f"Metadata-Version: 2.4\nName: {sdist_name or name}\nVersion: {sdist_version or version}\n\nbody\n"
    ).encode()
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo(f"{stem}/PKG-INFO")
        member.size = len(pkg_info)
        archive.addfile(member, io.BytesIO(pkg_info))
        # A nested file of the same name must not be taken for the sdist's metadata.
        decoy = b"Metadata-Version: 2.4\nName: decoy\nVersion: 0\n"
        member = tarfile.TarInfo(f"{stem}/tests/PKG-INFO")
        member.size = len(decoy)
        archive.addfile(member, io.BytesIO(decoy))
    write_sums(directory, [sdist, wheel])
    return wheel, sdist


def write_sums(directory, paths):
    lines = [f"{_sha256(path)}  {path.name}\n" for path in sorted(paths, key=lambda p: p.name)]
    (directory / "SHA256SUMS").write_text("".join(lines), encoding="ascii")


@pytest.fixture
def dist(tmp_path):
    directory = tmp_path / "dist"
    make_dist(directory)
    return directory


@pytest.fixture
def release(dist):
    return pypi.load_release(dist, PROJECT, TAG)


def listing(release, *names, changed=()):
    """A PyPI JSON answer that lists `names`, with a wrong digest for those in `changed`."""
    urls = [
        {
            "filename": name,
            "digests": {"sha256": "0" * 64 if name in changed else release.digests[name], "md5": "x"},
            "yanked": False,
        }
        for name in names
    ]
    return 200, json.dumps({"info": {"name": PROJECT, "version": release.version}, "urls": urls}).encode()


class Index:
    """An injected fetch function that returns the queued answers, then repeats the last one."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def no_sleep(_seconds):
    return None


# --- the downloaded files -------------------------------------------------------------------------


def test_a_release_build_is_accepted(dist):
    release = pypi.load_release(dist, PROJECT, TAG)
    assert release.version == VERSION
    assert sorted(release.files) == [WHEEL, SDIST]
    assert release.digests == {WHEEL: _sha256(dist / WHEEL), SDIST: _sha256(dist / SDIST)}


def test_the_name_is_compared_normalized(tmp_path):
    make_dist(tmp_path / "d", name="Contexto_Proxy", stem="contexto_proxy-9.9.9")
    assert pypi.load_release(tmp_path / "d", PROJECT, TAG).version == VERSION


@pytest.mark.parametrize("tag", ["9.9.9", "v9.9", "v9.9.9rc1", "main", "", "v9.9.9 ", "v09.9.9x"])
def test_only_a_release_tag_is_accepted(dist, tag):
    with pytest.raises(pypi.Refused, match="not a release tag"):
        pypi.load_release(dist, PROJECT, tag)


def test_an_old_contexto_artifact_is_refused(tmp_path):
    make_dist(tmp_path / "d", name="contexto", version="0.5.24")
    with pytest.raises(pypi.Refused) as refusal:
        pypi.load_release(tmp_path / "d", PROJECT, "v0.5.24")
    message = str(refusal.value)
    assert "'contexto'" in message and "contexto-proxy" in message
    assert "cannot be published" in message


def test_a_checkout_build_is_refused(tmp_path):
    make_dist(tmp_path / "d", version="0.0.0+checkout")
    with pytest.raises(pypi.Refused) as refusal:
        pypi.load_release(tmp_path / "d", PROJECT, TAG)
    assert "0.0.0+checkout" in str(refusal.value)
    assert VERSION in str(refusal.value)


def test_a_version_other_than_the_tag_is_refused(dist):
    with pytest.raises(pypi.Refused, match="9.9.9"):
        pypi.load_release(dist, PROJECT, "v9.9.8")


def test_an_sdist_with_other_metadata_than_the_wheel_is_refused(tmp_path):
    make_dist(tmp_path / "a", sdist_name="contexto")
    with pytest.raises(pypi.Refused, match="sdist"):
        pypi.load_release(tmp_path / "a", PROJECT, TAG)
    make_dist(tmp_path / "b", sdist_version="9.9.8")
    with pytest.raises(pypi.Refused, match="sdist"):
        pypi.load_release(tmp_path / "b", PROJECT, TAG)


def test_a_file_name_that_does_not_carry_the_name_and_version_is_refused(tmp_path):
    make_dist(tmp_path / "d", stem="contexto-9.9.9")
    with pytest.raises(pypi.Refused, match="contexto_proxy-9.9.9"):
        pypi.load_release(tmp_path / "d", PROJECT, TAG)


def test_two_wheels_are_refused(dist):
    (dist / "contexto_proxy-9.9.9-py2-none-any.whl").write_bytes((dist / WHEEL).read_bytes())
    with pytest.raises(pypi.Refused, match="exactly one wheel and one sdist"):
        pypi.load_release(dist, PROJECT, TAG)


@pytest.mark.parametrize("missing", [WHEEL, SDIST])
def test_a_missing_wheel_or_sdist_is_refused(dist, missing):
    (dist / missing).unlink()
    with pytest.raises(pypi.Refused, match="exactly one wheel and one sdist"):
        pypi.load_release(dist, PROJECT, TAG)


def test_a_release_without_checksums_is_refused(dist):
    (dist / "SHA256SUMS").unlink()
    with pytest.raises(pypi.Refused, match="SHA256SUMS"):
        pypi.load_release(dist, PROJECT, TAG)


@pytest.mark.parametrize("unlisted", [WHEEL, SDIST])
def test_a_file_missing_from_the_checksums_is_refused(dist, unlisted):
    write_sums(dist, [dist / name for name in (WHEEL, SDIST) if name != unlisted])
    with pytest.raises(pypi.Refused) as refusal:
        pypi.load_release(dist, PROJECT, TAG)
    assert "does not list" in str(refusal.value) and unlisted in str(refusal.value)


def test_checksums_for_a_file_that_was_not_downloaded_are_refused(dist):
    with (dist / "SHA256SUMS").open("a", encoding="ascii") as sums:
        sums.write(f"{'a' * 64}  contexto_proxy-9.9.9-py3-none-win_amd64.whl\n")
    with pytest.raises(pypi.Refused, match="win_amd64"):
        pypi.load_release(dist, PROJECT, TAG)


@pytest.mark.parametrize("changed", [WHEEL, SDIST])
def test_a_checksum_mismatch_is_refused(dist, changed):
    with (dist / changed).open("ab") as artifact:
        artifact.write(b"\0")
    with pytest.raises(pypi.Refused) as refusal:
        pypi.load_release(dist, PROJECT, TAG)
    assert "does not match" in str(refusal.value) and changed in str(refusal.value)


@pytest.mark.parametrize(
    "line",
    ["not a checksum line\n", f"{'a' * 63}  {WHEEL}\n", f"{'A' * 64}  {WHEEL}\n", f"{'a' * 64} {WHEEL}\n"],
)
def test_a_malformed_checksum_line_is_refused(dist, line):
    with (dist / "SHA256SUMS").open("a", encoding="ascii") as sums:
        sums.write(line)
    with pytest.raises(pypi.Refused, match="SHA256SUMS"):
        pypi.load_release(dist, PROJECT, TAG)


def test_a_file_listed_twice_is_refused(dist):
    text = (dist / "SHA256SUMS").read_text(encoding="ascii")
    (dist / "SHA256SUMS").write_text(text + text.splitlines(keepends=True)[0], encoding="ascii")
    with pytest.raises(pypi.Refused, match="twice"):
        pypi.load_release(dist, PROJECT, TAG)


def test_a_wheel_that_is_not_a_zip_is_refused(dist):
    (dist / WHEEL).write_bytes(b"not a zip")
    write_sums(dist, [dist / WHEEL, dist / SDIST])
    with pytest.raises(pypi.Refused, match="Cannot read the metadata"):
        pypi.load_release(dist, PROJECT, TAG)


# --- what may be uploaded --------------------------------------------------------------------------


def test_a_version_pypi_does_not_have_uploads_both_files(release):
    index = Index((404, b'{"message": "Not Found"}'))
    assert pypi.plan(release, PROJECT, INDEX, index) == [WHEEL, SDIST]
    assert index.urls == [f"{INDEX}/pypi/contexto-proxy/9.9.9/json"]


def test_identical_files_on_pypi_upload_nothing(release):
    assert pypi.plan(release, PROJECT, INDEX, Index(listing(release, WHEEL, SDIST))) == []


@pytest.mark.parametrize(("present", "missing"), [(WHEEL, SDIST), (SDIST, WHEEL)])
def test_a_partial_upload_uploads_only_the_missing_file(release, present, missing):
    assert pypi.plan(release, PROJECT, INDEX, Index(listing(release, present))) == [missing]


def test_a_version_with_no_files_listed_uploads_both(release):
    assert pypi.plan(release, PROJECT, INDEX, Index(listing(release))) == [WHEEL, SDIST]


@pytest.mark.parametrize("changed", [WHEEL, SDIST])
def test_a_different_file_on_pypi_fails_and_says_a_new_release_is_needed(release, changed):
    with pytest.raises(pypi.Refused) as refusal:
        pypi.plan(release, PROJECT, INDEX, Index(listing(release, WHEEL, SDIST, changed=(changed,))))
    message = str(refusal.value)
    assert changed in message
    assert "cannot be replaced" in message
    assert "new release" in message


def test_a_different_file_fails_even_when_the_other_is_missing(release):
    with pytest.raises(pypi.Refused, match="cannot be replaced"):
        pypi.plan(release, PROJECT, INDEX, Index(listing(release, WHEEL, changed=(WHEEL,))))


def test_a_file_on_pypi_that_the_release_does_not_have_fails(release):
    answer = listing(release, WHEEL, SDIST)
    body = json.loads(answer[1])
    body["urls"].append({"filename": "contexto_proxy-9.9.9-py3-none-win_amd64.whl", "digests": {"sha256": "1" * 64}})
    with pytest.raises(pypi.Refused, match="win_amd64"):
        pypi.plan(release, PROJECT, INDEX, Index((200, json.dumps(body).encode())))


@pytest.mark.parametrize("status", [500, 502, 503, 429, 403, 301])
def test_any_other_answer_fails_without_a_plan(release, status):
    index = Index((status, b"upstream error"))
    with pytest.raises(pypi.Refused) as refusal:
        pypi.plan(release, PROJECT, INDEX, index, attempts=3, sleep=no_sleep)
    assert str(status) in str(refusal.value)
    assert "Nothing was uploaded" in str(refusal.value)
    assert len(index.urls) == 3


def test_a_network_error_fails_without_a_plan(release):
    index = Index(OSError("connection refused"))
    with pytest.raises(pypi.Refused, match="connection refused"):
        pypi.plan(release, PROJECT, INDEX, index, attempts=2, sleep=no_sleep)
    assert len(index.urls) == 2


def test_a_passing_error_is_retried(release):
    index = Index((503, b""), OSError("timed out"), (404, b""))
    assert pypi.plan(release, PROJECT, INDEX, index, attempts=3, sleep=no_sleep) == [WHEEL, SDIST]
    assert len(index.urls) == 3


@pytest.mark.parametrize(
    "body",
    [b"<html>maintenance</html>", b"[]", b'{"info": {}}', b'{"urls": {}}', b'{"urls": [{"filename": "x"}]}', b"\xff"],
)
def test_an_answer_that_cannot_be_read_fails(release, body):
    with pytest.raises(pypi.Refused, match="Nothing was uploaded"):
        pypi.plan(release, PROJECT, INDEX, Index((200, body)), attempts=1, sleep=no_sleep)


def test_a_404_is_not_retried(release):
    index = Index((404, b""))
    pypi.plan(release, PROJECT, INDEX, index, attempts=5, sleep=no_sleep)
    assert len(index.urls) == 1


# --- after the upload ------------------------------------------------------------------------------


def test_confirm_waits_until_pypi_lists_both_files(release):
    index = Index((404, b""), listing(release, WHEEL), (502, b""), listing(release, WHEEL, SDIST))
    waits = []
    pypi.confirm(release, PROJECT, INDEX, index, attempts=10, delay=7, sleep=waits.append)
    assert len(index.urls) == 4
    assert waits == [7, 7, 7]


def test_confirm_gives_up_and_says_how_to_check(release):
    index = Index(listing(release, WHEEL))
    with pytest.raises(pypi.Refused) as refusal:
        pypi.confirm(release, PROJECT, INDEX, index, attempts=4, delay=1, sleep=no_sleep)
    assert len(index.urls) == 4
    assert SDIST in str(refusal.value)
    assert "action=publish" in str(refusal.value)


def test_confirm_fails_at_once_on_a_different_file(release):
    index = Index(listing(release, WHEEL, SDIST, changed=(SDIST,)))
    with pytest.raises(pypi.Refused, match="cannot be replaced"):
        pypi.confirm(release, PROJECT, INDEX, index, attempts=10, delay=1, sleep=no_sleep)
    assert len(index.urls) == 1


# --- the command, against a server on 127.0.0.1 ------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.paths.append(self.path)
        self.server.agents.append(self.headers.get("User-Agent", ""))
        status, body = self.server.answer
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return None


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.answer = (404, b"{}")
    httpd.paths = []
    httpd.agents = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _command(*args):
    return pypi.main([*args, "--project", PROJECT, "--tag", TAG])


def test_verify_command_prints_what_it_checked(dist, capsys):
    assert _command("verify", "--dist", str(dist)) == 0
    out = capsys.readouterr().out
    assert WHEEL in out and SDIST in out
    assert _sha256(dist / WHEEL) in out
    assert "Name: contexto-proxy" in out and "Version: 9.9.9" in out


def test_verify_command_fails_with_an_error_annotation(tmp_path, capsys):
    make_dist(tmp_path / "d", name="contexto", version="0.5.24")
    assert pypi.main(["verify", "--dist", str(tmp_path / "d"), "--project", PROJECT, "--tag", "v0.5.24"]) == 1
    assert "::error" in capsys.readouterr().out


def test_plan_command_writes_the_files_to_upload(dist, server, tmp_path, monkeypatch, capsys):
    output = tmp_path / "upload.txt"
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    assert _command("plan", "--dist", str(dist), "--index", server.url, "--output", str(output)) == 0
    assert output.read_text(encoding="utf-8").splitlines() == [str(dist / WHEEL), str(dist / SDIST)]
    assert github_output.read_text(encoding="utf-8") == "upload_count=2\n"
    assert server.paths == ["/pypi/contexto-proxy/9.9.9/json"]
    # A generic client name: nothing about the repository or its owner goes to the index.
    assert server.agents and all("contexto" not in agent.lower() for agent in server.agents)
    assert "not on PyPI" in capsys.readouterr().out


def test_plan_command_uploads_only_what_is_missing(dist, release, server, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    output = tmp_path / "upload.txt"
    server.answer = listing(release, WHEEL)
    assert _command("plan", "--dist", str(dist), "--index", server.url, "--output", str(output)) == 0
    assert output.read_text(encoding="utf-8").splitlines() == [str(dist / SDIST)]
    assert "::notice" in capsys.readouterr().out


def test_plan_command_uploads_nothing_when_pypi_has_the_files(dist, release, server, tmp_path, monkeypatch, capsys):
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    output = tmp_path / "upload.txt"
    server.answer = listing(release, WHEEL, SDIST)
    assert _command("plan", "--dist", str(dist), "--index", server.url, "--output", str(output)) == 0
    assert output.read_text(encoding="utf-8") == ""
    assert github_output.read_text(encoding="utf-8") == "upload_count=0\n"
    out = capsys.readouterr().out
    assert "::notice" in out and "nothing to upload" in out.lower()


def test_plan_command_fails_on_a_different_file(dist, release, server, tmp_path, monkeypatch, capsys):
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    output = tmp_path / "upload.txt"
    server.answer = listing(release, WHEEL, SDIST, changed=(WHEEL,))
    assert _command("plan", "--dist", str(dist), "--index", server.url, "--output", str(output)) == 1
    assert not output.exists()
    assert not github_output.exists()
    out = capsys.readouterr().out
    assert "::error" in out and "cannot be replaced" in out


def test_plan_command_fails_on_a_server_error(dist, server, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    output = tmp_path / "upload.txt"
    server.answer = (503, b"try later")
    args = ("plan", "--dist", str(dist), "--index", server.url, "--output", str(output))
    assert _command(*args, "--attempts", "2", "--delay", "0") == 1
    assert not output.exists()
    assert len(server.paths) == 2
    assert "503" in capsys.readouterr().out


def test_plan_command_fails_when_the_index_cannot_be_reached(dist, server, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    output = tmp_path / "upload.txt"
    url = server.url
    server.shutdown()
    server.server_close()
    args = ("plan", "--dist", str(dist), "--index", url, "--output", str(output))
    assert _command(*args, "--attempts", "1", "--delay", "0") == 1
    assert not output.exists()
    assert "Nothing was uploaded" in capsys.readouterr().out


def test_plan_command_does_not_ask_the_index_about_files_it_refuses(tmp_path, server, capsys):
    make_dist(tmp_path / "d", name="contexto")
    output = tmp_path / "upload.txt"
    assert _command("plan", "--dist", str(tmp_path / "d"), "--index", server.url, "--output", str(output)) == 1
    assert server.paths == []
    assert not output.exists()


def test_confirm_command_passes_when_pypi_lists_both_files(dist, release, server, capsys):
    server.answer = listing(release, WHEEL, SDIST)
    assert _command("confirm", "--dist", str(dist), "--index", server.url, "--attempts", "2", "--delay", "0") == 0
    assert "lists both files" in capsys.readouterr().out


def test_confirm_command_fails_when_pypi_never_lists_them(dist, server, capsys):
    assert _command("confirm", "--dist", str(dist), "--index", server.url, "--attempts", "2", "--delay", "0") == 1
    assert len(server.paths) == 2
    assert "::error" in capsys.readouterr().out


def test_the_script_uses_only_the_standard_library():
    allowed = set(sys.stdlib_module_names)
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] in ("import", "from") and not line.startswith((" ", "\t")):
            assert words[1].split(".")[0].rstrip(",") in allowed, line


def test_the_script_has_no_upload_code():
    text = SCRIPT.read_text(encoding="utf-8")
    for word in ("upload.pypi.org", "method=", "POST", "subprocess", "os.system"):
        assert word not in text, word
