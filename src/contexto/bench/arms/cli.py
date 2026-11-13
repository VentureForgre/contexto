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

Organization-managed Claude Code settings on the machine still apply to both arms; nothing here can
turn them off.

What is read from the stream: the ``result`` event's text (a decision reply, a probe turn's answers)
and error flag, ``compact_boundary`` events with ``compact_metadata.pre_tokens``, and how many
file-read tool calls the assistant made in a turn. Nothing of it is written anywhere except the
parsed probe answers.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contexto.bench import script as bench_script
from contexto.bench.arms import Stop, Unit, UnitResult, ledger_stop

STRIPPED_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "CLAUDECODE", "CONTEXTO_", "OPENRIG_")
STRIPPED_NAMES = frozenset({"DISABLE_AUTO_COMPACT", "DISABLE_COMPACT", "FORCE_PROMPT_CACHING_5M", "MAX_THINKING_TOKENS",
                            "ENABLE_TOOL_SEARCH", "DISABLE_PROMPT_CACHING"})
# A user's opt-out of Claude Code's own traffic is kept, for both arms alike.
KEPT_NAMES = frozenset({"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"})
# Set for both arms. DISABLE_AUTOUPDATER: no update in the middle of a run. The hint headers let the
# meter tell main requests, side requests and compactions apart without reading a request's content;
# the meter removes them again (contexto.bench.meter).
ENV = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
READ_TOOL = "Read"
THRASHING = "Autocompact is thrashing"
CLAUDE_ARGS = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose")
CLOSE_TIMEOUT_S = 60
KILL_TIMEOUT_S = 10
_EOF = object()


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
    text: str = ""
    reads: int = 0


@dataclass
class Session:
    """One child process speaking stream-json, in its own process group."""

    command: list[str]
    cwd: Path
    env: dict[str, str]
    compaction_pre_tokens: list[int | None] = field(default_factory=list)
    thrashing: bool = False

    def __post_init__(self) -> None:
        self.proc = subprocess.Popen(self.command, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
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

        # In a thread: a turn is larger than a pipe's buffer, and a child that has stopped reading must
        # not hold the run past the turn's timeout.
        threading.Thread(target=write, name="contexto-bench-stdin", daemon=True).start()
        turn = Turn()
        deadline = time.monotonic() + unit.turn_timeout
        ledger = unit.meter.ledger
        number = index + 1
        while True:
            if ledger.stopped.is_set() and (stop := ledger_stop(unit)) is not None:
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
            elif kind == "assistant":
                message = event.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                turn.reads += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
                                  and b.get("name") == READ_TOOL) if isinstance(content, list) else 0
            elif kind == "result":
                text = str(event.get("result") or "")
                if event.get("is_error") or THRASHING in text:
                    if (stop := ledger_stop(unit)) is not None:
                        raise stop
                    if self.thrashing or THRASHING in text:
                        raise Stop("thrashing", f"Claude Code stopped on turn {number}: auto-compaction was thrashing")
                    subtype = event.get("subtype") if isinstance(event.get("subtype"), str) else "error"
                    raise Stop("cli_error", f"Claude Code reported an error on turn {number} ({subtype[:40]})")
                turn.text = text
                return turn

    def _wait(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> int | None:
        """End the session: close stdin and wait; if the child does not exit, stop its process group."""
        if self.proc.stdin:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()
        code = self._wait(CLOSE_TIMEOUT_S) if self.proc.poll() is None else self.proc.returncode
        if code is None:
            code = self.kill()
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
    session = Session(command, working_directory(unit), env)
    try:
        for index, text in enumerate(script.turns):
            unit.meter.turn = index
            turn = session.send(index, text, unit)
            kind = script.kinds[index]
            if kind == "decision":
                result.concluded_ok[decisions[index]] = bench_script.decision_reply_ok(script, index, turn.text)
            elif kind == "toolloop":
                result.toolloop_reads = turn.reads
            elif kind == "probe":
                result.answers[index] = turn.text
                result.probe_turns_completed += 1
            result.turns_completed += 1
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
