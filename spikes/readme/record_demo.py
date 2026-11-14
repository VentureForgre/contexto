"""Record the README's terminal demo: a rotation and a recall, from one run of this checkout's code.

    uv run python spikes/readme/record_demo.py              # run, capture, render
    uv run python spikes/readme/record_demo.py --render-only   # render again from the committed capture

No model is called and nothing leaves 127.0.0.1. What runs:

- the real ``contexto run`` of this checkout (``contexto`` on the session's ``PATH`` is the running
  Python with ``-m contexto``), with a temporary ``HOME`` and ``CONTEXTO_HOME``;
- ``stub_claude.py`` as the ``claude`` on ``PATH``, and through it the real ``contexto mcp`` server
  and the real ``contexto sessions show``;
- ``demo_upstream.py`` in place of the model API.

All three write to one pseudo-terminal, 90 columns wide. The recorder reads it and keeps each chunk
with the time it arrived. It adds only the note line at the top and the command line it then runs with
``/bin/sh -c``, exactly as shown. It starts the session only once the capture holds the fake upstream's
own "listening" line, and it ends a session that runs longer than ``SESSION_TIMEOUT`` seconds. It
writes, under ``docs/assets``:

- ``demo.cast.json``: the chunks and their times, the source of the other two;
- ``demo.txt``: the chunks joined, so the capture can be read without playing it;
- ``demo.svg``: the animation (``termsvg.render``).

Before writing, it refuses a capture that holds the user name, the home directory, the host name or
the temporary directory.
"""

from __future__ import annotations

import argparse
import codecs
import fcntl
import getpass
import json
import os
import pty
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import demo_script as script  # noqa: E402
import termsvg  # noqa: E402

ROOT = HERE.parents[1]
ASSETS = ROOT / "docs" / "assets"
PORT = 8399
BOUND = 60_000
HEADROOM_FLOOR = 5_000
PACE = 0.8
LISTEN_TIMEOUT = 20.0  # seconds to wait for the fake upstream's listening line
SESSION_TIMEOUT = 120.0  # seconds the session may run; it takes about 25 at the default pace
TITLE = "synthetic session: a stub CLI and a fake upstream, no model"
CAST_VERSION = 1
MAX_SVG_BYTES = 1_000_000


def command_line(port: int) -> str:
    """The command the recording shows and runs. The small bound makes the session rotate after a few
    turns; ``--headroom-floor`` keeps the window budget above the launcher's warning level."""
    return (f"contexto run --bound {BOUND} --headroom-floor {HEADROOM_FLOOR} --no-version-check \\\n"
            f"    --upstream http://127.0.0.1:{port} claude")


class Capture:
    """Chunks of terminal output with their arrival times, read from a pseudo-terminal."""

    def __init__(self, cols: int, rows: int) -> None:
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.events: list[tuple[float, str]] = []
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last = self.started
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            self.add(decoder.decode(data).replace("\r", ""))

    def add(self, text: str) -> None:
        if not text:
            return
        with self.lock:
            now = time.monotonic()
            self.events.append((round(now - self.started, 3), text))
            self.last = now

    def text(self) -> str:
        """Everything captured so far."""
        with self.lock:
            return "".join(text for _, text in self.events)

    def settle(self, quiet: float = 0.15, timeout: float = 5.0) -> None:
        """Wait until nothing has arrived for ``quiet`` seconds, so a typed line lands after earlier output."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                idle = time.monotonic() - self.last
            if idle >= quiet:
                return
            time.sleep(0.02)

    def close(self) -> list[tuple[float, str]]:
        self.settle()
        os.close(self.slave)
        os.close(self.master)
        with self.lock:
            return list(self.events)


def wait_for_listening(capture: Capture, port: int, process: subprocess.Popen,
                       timeout: float = LISTEN_TIMEOUT) -> None:
    """Wait until the capture holds the fake upstream's own listening line for ``port``.

    The upstream prints that line after it has bound the port, so the line in the capture means two
    things: this upstream, not another server on the port, will answer; and the command line the
    recorder adds next lands after it. A port that only accepts connections proves neither."""
    line = f"{script.UPSTREAM_LABEL} listening on 127.0.0.1:{port},"
    deadline = time.monotonic() + timeout
    while True:
        text = capture.text()
        at = text.find(line)
        if at >= 0 and "\n" in text[at:]:
            return
        if process.poll() is not None:
            capture.settle()
            raise RuntimeError(f"the fake upstream exited with code {process.returncode} before it listened on "
                               f"127.0.0.1:{port}; is the port in use? Pass --port. Captured:\n{capture.text()}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"the fake upstream did not print its listening line for 127.0.0.1:{port} within "
                               f"{timeout:g} s. Captured:\n{capture.text()}")
        time.sleep(0.02)


def stop_group(process: subprocess.Popen, grace: float = 5.0) -> None:
    """End a process started with ``start_new_session=True`` and everything in its process group:
    SIGTERM, then SIGKILL after ``grace`` seconds."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def run_session(line: str, capture: Capture, timeout: float, **popen: object) -> int:
    """Run the shown command line with ``/bin/sh -c`` and return its exit code. A session that is still
    running after ``timeout`` seconds is killed with its children, and the error holds the capture."""
    session = subprocess.Popen(["/bin/sh", "-c", line], start_new_session=True, **popen)
    try:
        return session.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_group(session)
        capture.settle()
        raise RuntimeError(f"`contexto run` did not finish within {timeout:g} s and was killed. Captured:\n"
                           f"{capture.text()}") from None
    finally:
        # Nothing the session started may outlive it: a child left holding the terminal (a shell
        # command the stub gave up on, say) would block the capture's close.
        stop_group(session)


def check_checkout() -> None:
    """The running Python must import this checkout's ``contexto``: that is the code the recording shows."""
    try:
        import contexto
    except ImportError as exc:
        raise RuntimeError("this Python cannot import contexto: run with `uv run python ...` from the "
                           "checkout") from exc
    source = Path(contexto.__file__).resolve()
    if ROOT not in source.parents:
        raise RuntimeError(f"contexto is imported from outside this checkout ({source.parent.name}): run with "
                           f"`uv run python ...` from the checkout")


def record(port: int = PORT, pace: float = PACE, cols: int = termsvg.COLS, rows: int = termsvg.ROWS,
           timeout: float = SESSION_TIMEOUT) -> tuple[list[tuple[float, str]], list[str]]:
    """Run the session. Returns the capture and the strings that must not appear in it."""
    check_checkout()
    work = Path(tempfile.mkdtemp(prefix="contexto-demo-")).resolve()
    capture = Capture(cols, rows)
    upstream = None
    try:
        for name in ("bin", "home", "contexto-home", "cwd"):
            (work / name).mkdir()
        # The two commands the session finds on PATH: this checkout's `contexto`, and the stub as `claude`.
        for name, command in (("contexto", f'"{sys.executable}" -m contexto'),
                              ("claude", f'"{sys.executable}" "{HERE / "stub_claude.py"}"')):
            wrapper = work / "bin" / name
            wrapper.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
        env = {"PATH": os.pathsep.join([str(work / "bin"), "/usr/bin", "/bin"]),
               "HOME": str(work / "home"), "CONTEXTO_HOME": str(work / "contexto-home"), "TERM": "dumb",
               "LC_ALL": "C.UTF-8", "TZ": "UTC", "README_DEMO_PACE": str(pace)}
        std = {"stdin": subprocess.DEVNULL, "stdout": capture.slave, "stderr": capture.slave}

        capture.add(f"# {script.SYNTHETIC_LABEL}\n")
        upstream = subprocess.Popen([sys.executable, str(HERE / "demo_upstream.py"), "--port", str(port)],
                                    env=env, cwd=work / "cwd", **std)
        wait_for_listening(capture, port, upstream)
        capture.settle()
        time.sleep(2 * pace)
        line = command_line(port)
        capture.add(f"$ {line}\n")
        time.sleep(pace)
        code = run_session(line, capture, timeout, env=env, cwd=work / "cwd", **std)
        if code != 0:
            capture.settle()
            raise RuntimeError(f"`contexto run` exited with code {code}:\n{capture.text()}")
    finally:
        if upstream is not None:
            upstream.terminate()
            try:
                upstream.wait(timeout=5)
            except subprocess.TimeoutExpired:
                upstream.kill()
        events = capture.close()
        shutil.rmtree(work, ignore_errors=True)
    return events, [str(work), work.name]


def private_strings() -> list[str]:
    """Strings that identify this machine or its user. None may appear in a committed file."""
    host = socket.gethostname()
    found = [getpass.getuser(), str(Path.home()), os.environ.get("USER", ""), host, host.split(".")[0]]
    return sorted({s for s in found if len(s) >= 3})


def check_private(texts: dict[str, str], extra: list[str]) -> None:
    for name, text in texts.items():
        for needle in [*private_strings(), *extra]:
            if needle.lower() in text.lower():
                raise RuntimeError(f"{name} would hold {needle!r}, which identifies this machine or run")


def outputs(events: list[tuple[float, str]]) -> dict[str, str]:
    """The three files' contents for a capture. Deterministic in ``events``."""
    cast = {"version": CAST_VERSION, "cols": termsvg.COLS, "rows": termsvg.ROWS,
            "note": "Chunks of terminal output from spikes/readme/record_demo.py: [seconds, text].",
            "events": [[at, text] for at, text in events]}
    return {
        "demo.cast.json": json.dumps(cast, indent=1, ensure_ascii=False) + "\n",
        "demo.txt": "".join(text for _, text in events),
        "demo.svg": termsvg.render(events, title=TITLE, highlight=script.PLANT_FACT),
    }


def load_cast(path: Path) -> list[tuple[float, str]]:
    cast = json.loads(path.read_text(encoding="utf-8"))
    if cast.get("version") != CAST_VERSION:
        raise RuntimeError(f"{path.name} has version {cast.get('version')!r}, expected {CAST_VERSION}")
    return [(float(at), str(text)) for at, text in cast["events"]]


def check_capture(text: str) -> None:
    """The capture must show what the recording is for; a run that did not is not written."""
    for needle in (script.SYNTHETIC_LABEL, "contexto: session ", "Over-bound turns: 0", script.PLANT_FACT,
                   "contexto_search"):
        if needle not in text:
            raise RuntimeError(f"the capture has no {needle!r}; nothing was written")
    if text.count(script.PLANT_FACT) < 3:
        raise RuntimeError("the planted fact did not come back in a search result; nothing was written")


def write(out_dir: Path, files: dict[str, str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if len(files["demo.svg"].encode("utf-8")) > MAX_SVG_BYTES:
        raise RuntimeError("demo.svg would be over 1 MB")
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
        print(f"wrote {out_dir / name} ({len(text.encode('utf-8')):,} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ASSETS, help="directory for the three files (default docs/assets)")
    parser.add_argument("--port", type=int, default=PORT, help=f"port of the fake upstream (default {PORT})")
    parser.add_argument("--pace", type=float, default=PACE, help="seconds the stub pauses before each step")
    parser.add_argument("--render-only", action="store_true",
                        help="do not run a session: render demo.txt and demo.svg from --out/demo.cast.json")
    parser.add_argument("--still", nargs=2, metavar=("SECONDS", "FILE"),
                        help="also write the frame at this capture time to FILE, as an SVG without animation")
    args = parser.parse_args(argv)
    try:
        if args.render_only:
            events, extra = load_cast(args.out / "demo.cast.json"), []
        else:
            events, extra = record(args.port, args.pace)
        files = outputs(events)
        check_capture(files["demo.txt"])
        check_private(files, extra)
        write(args.out, files)
        if args.still is not None:
            still = termsvg.render(termsvg.frame_at(events, float(args.still[0])), title=TITLE,
                                   highlight=script.PLANT_FACT, animate=False)
            Path(args.still[1]).write_text(still, encoding="utf-8")
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"record_demo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
