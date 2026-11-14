"""What the two Claude Code arms share: one long-lived headless session per unit.

``claude -p --input-format stream-json --output-format stream-json --verbose --model M`` runs for
the whole session, under ``contexto run`` or alone. Each script turn is one stream-json line on its
stdin, and the turn ends at the ``result`` event on its stdout (the transport of the acceptance
harness, spikes/m2/acceptance/driver.py).

Both arms get the same things, and a fresh copy per unit:

- a working directory holding the script's files and nothing else, outside any git work tree, so
  Claude Code loads no project settings;
- an isolated ``CLAUDE_CONFIG_DIR`` whose ``settings.json`` allows the file-read tool without a
  prompt (``permissions.allow: ["Read"]``) and, for a key that comes from a helper, names the
  credential shim as ``apiKeyHelper`` (``contexto.bench.credentials``). The user's own Claude Code
  settings, hooks, MCP servers and login are never read;
- the same environment: the caller's, minus every variable that configures Claude Code, Contexto
  or a credential (``STRIPPED_PREFIXES``, ``STRIPPED_NAMES``; an opt-out of Claude Code's own
  traffic is kept), plus ``ENV`` below, the prompt cache
  TTL, and the credential's own variable.

Organization-managed Claude Code settings on the machine still apply to both arms and nothing here
can turn them off, so the run refuses to start when they set what the arms set
(``contexto.bench.arms.managed``), and a turn that ends with no request through the meter stops the
unit (``unmetered``): something sent Claude Code around it.

What is read from the stream:

- the text of every top-level assistant message of a turn (``parent_tool_use_id`` null), in order,
  joined with newlines: a decision reply, or a probe turn's answers. A model may answer some probes,
  call a tool, and answer the rest in a later message; the ``result`` event holds only the last
  message's text, so it is used only when the turn wrote no text block. The scoring lets the last
  line for an id win;
- the ``result`` event's error flag, and ``compact_boundary`` events with
  ``compact_metadata.pre_tokens``;
- how many ``result`` events there were. Each turn sent must get exactly one: a run that answers no
  turn (Claude Code starts one when a background task reports back, which ``ENV`` turns off) would
  shift every later reply onto the wrong turn. A result beyond the turns sent, seen at any point up to
  the session's exit, stops the unit (``desync``), so such a unit is never ``complete``;
- the tool calls of a turn: how many, and the ``file_path`` of each file-read call.

Nothing of it is written anywhere except the parsed probe answers and those counts.

The child runs in its own process group, and a watchdog process (``WATCHDOG``, the pattern of
``contexto run``'s own, D55) holds a pipe from the benchmark: if the benchmark dies without closing
the session (SIGKILL), the pipe closes and the watchdog stops the whole group, SIGTERM then SIGKILL.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contexto.bench import script as bench_script
from contexto.bench.arms import Stop, Unit, UnitResult, ledger_stop, stop_pending

STRIPPED_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "CLAUDECODE", "CONTEXTO_", "OPENRIG_")
STRIPPED_NAMES = frozenset({"DISABLE_AUTO_COMPACT", "DISABLE_COMPACT", "FORCE_PROMPT_CACHING_5M", "MAX_THINKING_TOKENS",
                            "ENABLE_TOOL_SEARCH", "DISABLE_PROMPT_CACHING"})
# A user's opt-out of Claude Code's own traffic is kept, for both arms alike.
KEPT_NAMES = frozenset({"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"})
# Set for both arms. DISABLE_AUTOUPDATER: no update in the middle of a run. The hint headers let the
# meter tell main requests, side requests and compactions apart without reading a request's content;
# the meter removes them again (contexto.bench.meter). CLAUDE_CODE_DISABLE_BACKGROUND_TASKS (documented
# on Claude Code's environment-variable page; read in the 2.1.322 bundle): no subagent or shell command
# runs in the background, so no background task reports back with a run, and a result, of its own.
ENV = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
READ_TOOL = "Read"
THRASHING = "Autocompact is thrashing"
CLAUDE_ARGS = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose")
CLOSE_TIMEOUT_S = 60
UNMETERED_GRACE_S = 30  # how long a result with no metered request waits for a sign that it answered no turn
KILL_TIMEOUT_S = 10
_EOF = object()
# The watchdog of a session's process group. Lines from the benchmark: "group <n>" once the child has
# started (its pid is the group's id), "exited" once the child is reaped (the id may then be reused, so
# it is never signalled after that), "done" when the session is closed. End of file without "done": the
# benchmark is gone, so the group gets SIGTERM, then SIGKILL if it is still there after a grace period.
WATCHDOG = r"""
import os, signal, sys, time
group = None
for line in iter(sys.stdin.buffer.readline, b""):
    if line == b"done\n":
        sys.exit(0)
    if line.startswith(b"group "):
        group = int(line[6:])
    if line == b"exited\n":
        group = None
if group is not None:
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, signum)
        except OSError:
            break
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                os.killpg(group, 0)
            except OSError:
                sys.exit(0)
            time.sleep(0.05)
"""


def child_env(unit: Unit, config_dir: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment both Claude Code arms start from."""
    environ = os.environ if environ is None else environ
    env = {k: v for k, v in environ.items()
           if k in KEPT_NAMES or not (k.startswith(STRIPPED_PREFIXES) or k in STRIPPED_NAMES)}
    env.update(ENV)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = unit.cache_ttl
    env.update(unit.credentials.child_env())
    return env


def isolated_config(unit: Unit) -> Path:
    """Create the unit's Claude Code configuration directory: one settings file, mode 0600."""
    config = unit.directory / "claude-config"
    config.mkdir(parents=True, exist_ok=True)
    settings = {"permissions": {"allow": [READ_TOOL]}, **unit.credentials.claude_settings()}
    path = config / "settings.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    return config


def working_directory(unit: Unit) -> Path:
    """Create the session's working directory with the files the tool-loop turn reads."""
    cwd = unit.directory / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    for name, text in unit.script.files.items():
        target = cwd / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return cwd


def claude_version(claude: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The version ``claude --version`` prints, for example ``2.1.322``, or None."""
    environ = os.environ if environ is None else environ
    env = {k: v for k, v in environ.items() if not k.startswith(STRIPPED_PREFIXES)}
    try:
        proc = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=60, env=env,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    words = proc.stdout.split()
    return words[0] if proc.returncode == 0 and words else None


@dataclass
class Turn:
    text: str = ""  # every top-level assistant text block of the turn, in order, joined with newlines
    read_paths: list[str] = field(default_factory=list)  # file_path of each file-read call, in order
    tool_calls: int = 0


def files_read(read_paths: list[str], wanted: tuple[str, ...] | list[str], cwd: Path) -> int:
    """How many of ``wanted`` (paths relative to ``cwd``) were read, each counted once, whether the
    call named it relative or absolute."""
    try:
        root = cwd.resolve()
    except OSError:
        root = cwd
    seen = set()
    for raw in read_paths:
        path = Path(raw)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
        seen.add(path.as_posix().removeprefix("./"))
    return sum(1 for name in wanted if Path(name).as_posix() in seen)


class Watchdog:
    """Stops a session's process group if the benchmark dies without closing it (``WATCHDOG``)."""

    def __init__(self) -> None:
        try:
            self.process: subprocess.Popen | None = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", WATCHDOG], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except (OSError, subprocess.SubprocessError):  # the session goes on without it
            self.process = None

    def send(self, line: bytes) -> None:
        try:
            if self.process is not None and self.process.stdin is not None:
                self.process.stdin.write(line)
                self.process.stdin.flush()
        except (OSError, ValueError):
            pass

    def done(self) -> None:
        if self.process is None:
            return
        self.send(b"done\n")
        with contextlib.suppress(OSError):
            if self.process.stdin is not None:
                self.process.stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=5)


@dataclass
class Session:
    """One child process speaking stream-json, in its own process group."""

    command: list[str]
    cwd: Path
    env: dict[str, str]
    compaction_pre_tokens: list[int | None] = field(default_factory=list)
    thrashing: bool = False

    def __post_init__(self) -> None:
        # The watchdog first, so the child cannot inherit its pipe (close_fds does that too).
        self.watchdog = Watchdog()
        self._reaped = False
        self.sent = 0  # turns written to the child
        self.results = 0  # result events read from it
        try:
            self.proc = subprocess.Popen(self.command, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except BaseException:
            self.watchdog.done()
            raise
        self.watchdog.send(f"group {self.proc.pid}\n".encode())
        self._events: queue.Queue[Any] = queue.Queue()
        self._stderr: deque[bytes] = deque(maxlen=50)
        threading.Thread(target=self._read_stdout, name="contexto-bench-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="contexto-bench-stderr", daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            if THRASHING.encode() in line:
                self.thrashing = True
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                self._events.put(event)
        self._events.put(_EOF)

    def _read_stderr(self) -> None:
        assert self.proc.stderr
        for line in self.proc.stderr:  # kept in memory only, to recognise a named stop
            if THRASHING.encode() in line:
                self.thrashing = True
            self._stderr.append(line)

    def send(self, index: int, text: str, unit: Unit) -> Turn:
        """Send turn ``index`` and wait for its ``result``. Raises ``Stop``."""
        assert self.proc.stdin
        message = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        line = (json.dumps(message) + "\n").encode("utf-8")

        def write() -> None:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass  # the child is gone, or the session was closed: the loop below says why

        number = index + 1
        self.drain()
        if self.results > self.sent:
            raise self.desync(f"found before turn {number} was sent")
        # In a thread: a turn is larger than a pipe's buffer, and a child that has stopped reading must
        # not hold the run past the turn's timeout.
        threading.Thread(target=write, name="contexto-bench-stdin", daemon=True).start()
        self.sent += 1
        turn = Turn()
        texts: list[str] = []
        deadline = time.monotonic() + unit.turn_timeout
        while True:
            if stop_pending(unit) and (stop := ledger_stop(unit)) is not None:
                raise stop
            if time.monotonic() > deadline:
                raise Stop("timeout", f"turn {number} had no result within {unit.turn_timeout:.0f} seconds")
            try:
                event = self._events.get(timeout=0.1)
            except queue.Empty:
                continue
            if event is _EOF:
                code = self._wait(KILL_TIMEOUT_S)
                if (stop := ledger_stop(unit)) is not None:
                    raise stop
                if self.thrashing:
                    raise Stop("thrashing", f"Claude Code stopped on turn {number}: auto-compaction was thrashing")
                raise Stop("cli_exit", f"Claude Code exited before turn {number} finished (exit code {code})")
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "compact_boundary":
                metadata = event.get("compact_metadata") if isinstance(event.get("compact_metadata"), dict) else {}
                pre = metadata.get("pre_tokens")
                self.compaction_pre_tokens.append(pre if isinstance(pre, int) and not isinstance(pre, bool) else None)
            elif kind == "assistant" and event.get("parent_tool_use_id") is None:
                message = event.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        texts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        turn.tool_calls += 1
                        call = block.get("input") if isinstance(block.get("input"), dict) else {}
                        path = call.get("file_path")
                        if block.get("name") == READ_TOOL and isinstance(path, str):
                            turn.read_paths.append(path)
            elif kind == "result":
                self.results += 1
                if self.results > self.sent:
                    raise self.desync(f"found while turn {number} was in flight")
                text = str(event.get("result") or "")
                if event.get("is_error") or THRASHING in text:
                    if (stop := ledger_stop(unit)) is not None:
                        raise stop
                    if self.thrashing or THRASHING in text:
                        raise Stop("thrashing", f"Claude Code stopped on turn {number}: auto-compaction was thrashing")
                    subtype = event.get("subtype") if isinstance(event.get("subtype"), str) else "error"
                    raise Stop("cli_error", f"Claude Code reported an error on turn {number} ({subtype[:40]})")
                if unit.meter.turn_requests(index) == 0:
                    raise self.unmetered_or_desync(index, unit)
                turn.text = "\n".join(texts) if texts else text
                return turn

    def _note(self, event: dict[str, Any]) -> None:
        """What is kept of an event read between turns or after the last: compactions and results."""
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "compact_boundary":
            metadata = event.get("compact_metadata") if isinstance(event.get("compact_metadata"), dict) else {}
            pre = metadata.get("pre_tokens")
            self.compaction_pre_tokens.append(pre if isinstance(pre, int) and not isinstance(pre, bool) else None)
        elif kind == "result":
            self.results += 1

    def drain(self, until_eof: float | None = None) -> None:
        """Take the events already read; with ``until_eof``, wait up to that many seconds for the end of
        the child's output. The end-of-file marker is put back for the turn loop."""
        deadline = None if until_eof is None else time.monotonic() + until_eof
        while True:
            try:
                if deadline is None:
                    event = self._events.get_nowait()
                else:
                    event = self._events.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return
            if event is _EOF:
                self._events.put(_EOF)
                return
            self._note(event)

    def unmetered_or_desync(self, index: int, unit: Unit) -> Stop:
        """The stop for a result that came with no request through the meter in its turn. Either Claude
        Code went around the meter, or the result answered no turn: a run of its own, printed before the
        turn's own. In the second case the turn's own request reaches the meter, or its result arrives,
        within ``UNMETERED_GRACE_S``."""
        number = index + 1
        deadline = time.monotonic() + UNMETERED_GRACE_S
        while time.monotonic() < deadline:
            if unit.meter.turn_requests(index) > 0:
                return self.desync(f"found when turn {number}'s own request followed its result")
            seen = self.results
            self.drain(until_eof=0.1)
            if self.results > seen:
                return self.desync(f"found while turn {number} was in flight")
        return Stop("unmetered", f"turn {number} ended with no request through the benchmark's meter: something "
                                 "sent Claude Code around it (organization-managed settings can), so its tokens "
                                 "could not be counted")

    def desync(self, where: str) -> Stop:
        return Stop("desync", f"Claude Code printed a result that answered no turn of the script ({self.results} "
                              f"results for {self.sent} turns sent, {where}; a background task's report starts "
                              "a run of its own), so its replies could not be matched to the turns and none of the "
                              "unit's answers are scored")

    def finish(self) -> None:
        """After the last turn: end the session and read the rest of its output. Raises ``Stop``
        (``desync``) when it held a result beyond the turns sent."""
        if self.proc.stdin:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()
        if self._wait(CLOSE_TIMEOUT_S) is None:
            self.kill()
        self.drain(until_eof=5.0)
        if self.results > self.sent:
            raise self.desync("found after the script's last turn")

    def _wait(self, timeout: float) -> int | None:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        self._mark_reaped()
        return code

    def _mark_reaped(self) -> None:
        if not self._reaped:
            self._reaped = True
            self.watchdog.send(b"exited\n")

    def close(self) -> int | None:
        """End the session: close stdin and wait; if the child does not exit, stop its process group."""
        if self.proc.stdin:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()
        code = self._wait(CLOSE_TIMEOUT_S) if self.proc.poll() is None else self.proc.returncode
        if code is None:
            code = self.kill()
        self._mark_reaped()
        self.watchdog.done()
        for pipe in (self.proc.stdout, self.proc.stderr):
            if pipe:
                with contextlib.suppress(OSError):
                    pipe.close()
        return code

    def kill(self) -> int | None:
        """Stop the child and everything it started: SIGTERM to its process group, then SIGKILL if
        the child has not exited. The group is signalled only while the child has not been reaped,
        so its id cannot have passed to another process."""
        for signum in (signal.SIGTERM, signal.SIGKILL):
            if self.proc.poll() is not None:
                break
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signum)
            self._wait(KILL_TIMEOUT_S)
        return self.proc.poll()


def run_session(unit: Unit, command: list[str], env: dict[str, str]) -> UnitResult:
    """Send every turn of the script through one session and collect what the benchmark records."""
    script = unit.script
    result = UnitResult(compactions=0, toolloop_reads=None)
    decisions = {d.turn: d.probe for d in script.decisions}
    cwd = working_directory(unit)
    session = Session(command, cwd, env)
    try:
        for index, text in enumerate(script.turns):
            unit.meter.turn = index
            turn = session.send(index, text, unit)
            kind = script.kinds[index]
            if kind == "decision":
                result.concluded_ok[decisions[index]] = bench_script.decision_reply_ok(script, index, turn.text)
            elif kind == "toolloop":
                result.toolloop_reads = files_read(turn.read_paths, script.reads, cwd)
                result.toolloop_tool_calls = turn.tool_calls
            elif kind == "probe":
                result.answers[index] = turn.text
                result.probe_turns_completed += 1
            result.turns_completed += 1
        session.finish()
    except Stop as stop:
        session.kill()
        result.stopped(stop.code, stop.reason)
    except BaseException:
        session.kill()
        raise
    finally:
        result.cli_exit = session.close()
        result.compactions = len(session.compaction_pre_tokens)
        result.compaction_pre_tokens = [x for x in session.compaction_pre_tokens if x is not None]
    return result


def find_claude() -> str | None:
    return shutil.which("claude")
