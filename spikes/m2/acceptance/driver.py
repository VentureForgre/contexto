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


class AuthWatcher:
    """Tail the session log; on the first 401 or 403, terminate the process and remember it."""

    def __init__(self, session_log: Path, proc: subprocess.Popen) -> None:
        self.session_log = session_log
        self.proc = proc
        self.status: int | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        offset = 0
        while not self._stop.is_set() and self.status is None:
            try:
                with self.session_log.open("r", encoding="utf-8") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    offset = fh.tell()
            except FileNotFoundError:
                chunk = ""
            for line in chunk.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                status = event.get("status")
                if event.get("event") in ("upstream_4xx", "request") and status in (401, 403):
                    self.status = int(status)
                    self.proc.terminate()
                    return
            time.sleep(0.5)

    def stop(self) -> None:
        self._stop.set()


class StreamJsonTransport:
    """One ``contexto run claude`` process; stream-json in, stream-json out."""

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str], session_log: Path | None = None) -> None:
        if (cwd / ".git").exists() or inside_git(cwd):
            raise ValueError("the scratch directory must not be inside a git repository")
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self.watcher = AuthWatcher(session_log, self.proc) if session_log else None
        self.compactions = 0  # stream-json system events with subtype compact_boundary

    def send(self, text: str) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
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
        raise RuntimeError(f"claude exited before the turn finished (code {self.proc.poll()})")

    def close(self) -> int:
        if self.watcher:
            self.watcher.stop()
        if self.proc.stdin:
            self.proc.stdin.close()
        return self.proc.wait(timeout=120)


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


def run_session(turns: list[str], run: Run, on_turn: Callable[[int], None] | None = None) -> str:
    """Send every turn; return the probe turn's answer text (the last turn)."""
    answer = ""
    for i, text in enumerate(turns):
        answer = run.turn(i, text)
        if on_turn:
            on_turn(i)
    return answer
