"""Drive the acceptance session through ``contexto run claude`` as one long-lived headless process.

The launcher (lane A packet A3, proposed shape per builder-1) is
``contexto run [--bound N] [--session ID] claude [claude args]``; everything after ``claude`` passes
through. The driver keeps one process for the whole session, ``claude -p --input-format stream-json
--output-format stream-json --verbose --model claude-haiku-5-5``, so the proxy, its epoch state, and
the MCP server live for the whole run. Each user turn is one stream-json line on stdin; the turn ends
at the ``result`` event on stdout.

The guard reserves the full output allowance before every turn and stops the run instead of
passing the approved budget.

Credential (intake, D12): the run uses an isolated Claude Code config directory in the scratch
directory, so the owner's claude.ai login is never found. Its settings.json names ``keyhelper.py``
as the apiKeyHelper, with only the path of the seat's settings file as argument; the seat's own
helper runs at call time, read-only. No key and no copy of the helper command is written anywhere.
Credential variables are removed from the child environment. The first 401 or 403 in the session
log stops the run.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from acceptance.detectors import BILLED_PATHS, request_path
from contexto.proxy import TRANSFORMED_PATHS

MODEL = "claude-haiku-5-5"
KEYHELPER = Path(__file__).resolve().parent / "keyhelper.py"
_CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


class AuthFailure(RuntimeError):
    pass


def inside_git(path: Path) -> bool:
    """True when ``path`` is inside any git work tree, including as a nested directory."""
    proc = subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
                          capture_output=True, text=True)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def isolated_config(scratch: Path, seat_settings: Path) -> Path:
    """Create the isolated CLAUDE_CONFIG_DIR whose only setting is the apiKeyHelper shim."""
    if not seat_settings.is_file():
        raise ValueError("the seat settings file does not exist")
    config = scratch / "claude-config"
    config.mkdir(parents=True, exist_ok=True)
    helper = " ".join(shlex.quote(x) for x in (sys.executable, str(KEYHELPER), str(seat_settings.resolve())))
    (config / "settings.json").write_text(json.dumps({"apiKeyHelper": helper}, indent=2) + "\n", encoding="utf-8")
    return config


class Transport(Protocol):
    def send(self, text: str) -> dict[str, Any]: ...  # returns the turn's result event
    def close(self) -> int: ...


def launcher_command(session: str, bound: int, upstream: str, contexto: str = "contexto") -> list[str]:
    return [contexto, "run", "--bound", str(bound), "--session", session, "--upstream", upstream, "claude", "-p",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--model", MODEL]


# Paths whose 401 or 403 means the credential failed: the model requests the proxy transforms
# (contract PR #61). Claude Code also sends an unauthenticated ``HEAD /api/hello`` connectivity probe
# at startup, which the owner's proxy answers with 401; that is not a credential failure (first live
# H1 attempt, 2026-10-16), so other paths and methods are ignored.
AUTH_PATHS = TRANSFORMED_PATHS


def is_auth_failure(event: dict) -> bool:
    """A 401 or 403 on a POST to a model path (Messages or Responses, with their token counts), as a
    request record or an upstream_4xx event."""
    if event.get("event") not in ("upstream_4xx", "request") or event.get("status") not in (401, 403):
        return False
    if request_path(event) not in AUTH_PATHS:
        return False
    # The proxy logs upstream_4xx for any method but records none, so one on a model path is taken as
    # a POST: the conservative reading, since it stops the run.
    return event.get("method", "POST") == "POST"


def _usage_tokens(usage: dict) -> tuple[int, int]:
    """(input, total) tokens of one response's usage: input = input + cache write + cache read."""
    inp = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return inp, inp + int(usage.get("output_tokens") or 0)


class SessionWatcher:
    """Tail the session log and terminate the process on the first credential failure
    (``is_auth_failure``) or, when ``approved`` is given, before real spend can pass it.

    The spend stop is the harness's hard limit inside a turn, which the model never sees: the
    pre-turn projection in ``Run`` cannot see a tool loop's requests. After each Messages API
    response it adds the real usage, then stops the run if two more requests of the last input size
    plus the full output reserve could pass ``approved``. Two, because the proxy logs a request when
    its response ends, by which time Claude Code may already have sent the next one.
    """

    def __init__(self, session_log: Path, proc: subprocess.Popen, approved: int | None = None,
                 reserve: int = 0, poll: float = 0.1) -> None:
        self.session_log = session_log
        self.proc = proc
        self.approved = approved
        self.reserve = reserve
        self.poll = poll
        self.status: int | None = None
        self.spent = 0
        self.spend_stop: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _check(self, event: dict) -> bool:
        """True when the run must stop."""
        if is_auth_failure(event):
            self.status = int(event["status"])
            return True
        if (self.approved is None or event.get("event") != "request"
                or request_path(event) not in BILLED_PATHS or not event.get("usage")):
            return False  # count_tokens and other paths bill nothing; a trailing slash is the same path
        last_input, total = _usage_tokens(event["usage"])
        self.spent += total
        projected = self.spent + 2 * (last_input + self.reserve)
        if projected > self.approved:
            self.spend_stop = (f"real spend {self.spent:,} plus two more requests of up to "
                               f"{last_input + self.reserve:,} could pass the approved {self.approved:,}")
            return True
        return False

    def _run(self) -> None:
        offset = 0
        pending = ""  # a line still being written
        while not self._stop.is_set():
            try:
                with self.session_log.open("r", encoding="utf-8") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    offset = fh.tell()
            except FileNotFoundError:
                chunk = ""
            lines = (pending + chunk).split("\n")
            pending = lines.pop()
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and self._check(event):
                    self.proc.terminate()
                    return
            time.sleep(self.poll)

    def stop(self) -> None:
        self._stop.set()


class StreamJsonTransport:
    """One ``contexto run claude`` process; stream-json in, stream-json out."""

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str], session_log: Path | None = None,
                 approved: int | None = None, reserve: int = 0) -> None:
        if (cwd / ".git").exists() or inside_git(cwd):
            raise ValueError("the scratch directory must not be inside a git repository")
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self.watcher = SessionWatcher(session_log, self.proc, approved, reserve) if session_log else None
        self.compactions = 0  # stream-json system events with subtype compact_boundary

    def send(self, text: str) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:  # the watcher stopped the process between turns; say why below
            self.proc.wait(timeout=30)
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "compact_boundary":
                self.compactions += 1
            if event.get("type") == "result":
                return event
        if self.watcher and self.watcher.status:
            raise AuthFailure(f"upstream returned {self.watcher.status}; run stopped at the first auth error")
        if self.watcher and self.watcher.spend_stop:
            raise BudgetExceeded(f"stopped inside the turn: {self.watcher.spend_stop}")
        raise RuntimeError(f"claude exited before the turn finished (code {self.proc.poll()})")

    def close(self) -> int:
        if self.watcher:
            self.watcher.stop()
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
        code = self.proc.wait(timeout=120)
        for pipe in (self.proc.stdout, self.proc.stderr):  # release the pipes, so nothing is left open
            if pipe:
                pipe.close()
        return code


def child_env(contexto_home: Path, config_dir: Path | None = None) -> dict[str, str]:
    """Inherited environment minus rig, parent-session, and credential variables; plus the run's
    CONTEXTO_HOME and, when given, the isolated CLAUDE_CONFIG_DIR."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENRIG_", "CLAUDE_CODE_", "CLAUDECODE")) and k not in _CREDENTIAL_VARS}
    env["CONTEXTO_HOME"] = str(contexto_home)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


class BudgetExceeded(Exception):
    pass


@dataclass
class Run:
    transport: Transport
    approved: int  # tokens approved by intake for H1
    reserve: int  # output allowance reserved before each turn
    estimates: list[int]  # estimated upstream input per turn, from the dry-run budget
    spent: int = 0
    turns: list[dict[str, Any]] = field(default_factory=list)

    def turn(self, index: int, text: str) -> str:
        projected = self.spent + self.estimates[index] + self.reserve
        if projected > self.approved:
            raise BudgetExceeded(f"turn {index + 1}: projected {projected:,} would pass the approved {self.approved:,}")
        result = self.transport.send(text)
        usage = result.get("usage") or {}
        cost = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                     "cache_read_input_tokens", "output_tokens"))
        self.spent += cost
        self.turns.append({"turn": index + 1, "tokens": cost, "is_error": bool(result.get("is_error")),
                           "num_turns": result.get("num_turns"), "result_chars": len(str(result.get("result") or ""))})
        if result.get("is_error"):
            raise RuntimeError(f"turn {index + 1}: claude reported an error")
        if self.spent > self.approved:
            raise BudgetExceeded(f"turn {index + 1}: spend {self.spent:,} passed the approved {self.approved:,}")
        return str(result.get("result") or "")


def run_session_all(turns: list[str], run: Run) -> list[str]:
    """Send every turn; return each turn's answer text, in order (D27: several probe turns)."""
    return [run.turn(i, text) for i, text in enumerate(turns)]


def run_session(turns: list[str], run: Run, on_turn: Callable[[int], None] | None = None) -> str:
    """Send every turn; return the probe turn's answer text (the last turn)."""
    answer = ""
    for i, text in enumerate(turns):
        answer = run.turn(i, text)
        if on_turn:
            on_turn(i)
    return answer
