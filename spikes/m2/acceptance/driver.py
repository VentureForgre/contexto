"""Drive the acceptance session through ``contexto run claude`` as one long-lived headless process.

The launcher (lane A packet A3, proposed shape per builder-1) is
``contexto run [--bound N] [--session ID] claude [claude args]``; everything after ``claude`` passes
through. The driver keeps one process for the whole session, ``claude -p --input-format stream-json
--output-format stream-json --verbose --model claude-haiku-5-5``, so the proxy, its epoch state, and
the MCP server live for the whole run. Each user turn is one stream-json line on stdin; the turn ends
at the ``result`` event on stdout.

The guard reserves the full output allowance before every turn and stops the run instead of
passing the approved budget.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MODEL = "claude-haiku-5-5"


class Transport(Protocol):
    def send(self, text: str) -> dict[str, Any]: ...  # returns the turn's result event
    def close(self) -> int: ...


def launcher_command(session: str, bound: int, contexto: str = "contexto") -> list[str]:
    return [contexto, "run", "--bound", str(bound), "--session", session, "claude", "-p",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--model", MODEL]


class StreamJsonTransport:
    """One ``contexto run claude`` process; stream-json in, stream-json out."""

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str]) -> None:
        if (cwd / ".git").exists():
            raise ValueError("the scratch directory must not be a git repository")
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)

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
            if event.get("type") == "result":
                return event
        raise RuntimeError(f"claude exited before the turn finished (code {self.proc.poll()})")

    def close(self) -> int:
        if self.proc.stdin:
            self.proc.stdin.close()
        return self.proc.wait(timeout=120)


def child_env(contexto_home: Path) -> dict[str, str]:
    """Inherited environment minus rig and parent-session variables, plus the run's CONTEXTO_HOME."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENRIG_", "CLAUDE_CODE_", "CLAUDECODE"))}
    env["CONTEXTO_HOME"] = str(contexto_home)
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
