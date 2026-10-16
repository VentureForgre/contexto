"""The Codex variant of the acceptance driver (M2 A16): the D27 script through ``contexto run codex``.

Codex has no stream-json stdin session like ``claude -p``, so each user turn is one process:
``contexto run ... codex exec --json -o <last message> [resume <thread id>] -`` with the turn's text
on stdin. The first turn's ``thread.started`` event names the Codex thread; later turns resume it
by id (``resume --last -`` would read ``-`` as a session id). Each turn therefore also starts a new
proxy and engine on the same Contexto session (the A8 restore applies at its first request).

Spend is read from the proxy's session log: ``input_tokens`` (the Responses API counts cached
input inside it) plus ``output_tokens`` of every request on a transformed Responses path. Before a
turn starts, ``spent + estimate + reserve`` must fit the approved H1 cap, as driver.Run does for
Claude; a turn is never started that could pass it.

Isolation (D22, D12): API-key mode only, a key helper command (the key is never in a file here), a
non-Git scratch directory, ``env -i`` style environment without OpenRig, Claude Code, or credential
variables, and Codex's home inside the Contexto session directory (run_codex). The run sets
CONTEXTO_HOME (since PR #148, v0.1.2, Codex's MCP server receives it as an absolute path); for an
older build, ``mcp_home_workaround`` points HOME at a scratch home and leaves CONTEXTO_HOME unset
instead, so the proxy and the MCP server still open the same store."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from acceptance import script
from acceptance.detectors import FAILURE_EVENTS

MODEL = "gpt-6-astra"
RESPONSES_PATHS = ("/responses", "/v1/responses")
_DROP_PREFIXES = ("OPENRIG_", "CLAUDE", "ANTHROPIC_", "OPENAI_", "CODEX_", "RIGGED_", "CONTEXTO_")


def turn_command(contexto: str, session: str, bound: int, upstream: str, key_helper: str, last_message: Path,
                 thread: str | None = None, codex_upstream: str | None = None, model: str = MODEL,
                 extra: tuple[str, ...] = ()) -> list[str]:
    """One user turn: a new thread when ``thread`` is None, else that thread resumed; the prompt is
    read from stdin (``-``)."""
    cmd = [contexto, "run", "--bound", str(bound), "--session", session, "--upstream", upstream,
           "--codex-key-helper", key_helper]
    if codex_upstream:
        cmd += ["--codex-upstream", codex_upstream]
    cmd += ["codex", "exec", "--skip-git-repo-check", "--json", "-c", f"model={model}",
            "-c", "model_reasoning_effort=low", "-o", str(last_message), *extra]
    return cmd + (["resume", thread, "-"] if thread else ["-"])


def child_env(store_home: Path, scratch_home: Path, mcp_home_workaround: bool = False,
              base: dict[str, str] | None = None) -> tuple[dict[str, str], Path]:
    """The child's environment and the Contexto home the run's session lives in. Nothing from the
    seat's environment that could select another credential, provider, or home is passed."""
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if k in ("PATH", "TERM", "LANG", "TMPDIR")
           and not k.startswith(_DROP_PREFIXES)}
    env.setdefault("TERM", "dumb")
    if mcp_home_workaround:
        env["HOME"] = str(scratch_home)
        return env, scratch_home / ".contexto"
    env["HOME"] = str(scratch_home)
    env["CONTEXTO_HOME"] = str(store_home)
    return env, store_home


def usage_tokens(usage: dict[str, Any] | None) -> tuple[int, int, int]:
    """``(input, cached, output)`` from a Responses API usage (cached is inside input)."""
    usage = usage or {}
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
    return usage.get("input_tokens") or 0, cached, usage.get("output_tokens") or 0


def read_log(session_log: Path) -> list[dict[str, Any]]:
    if not session_log.exists():
        return []
    return [json.loads(line) for line in session_log.read_text().splitlines() if line.strip()]


def responses_requests(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("event") == "request" and e.get("method") == "POST"
            and e.get("path") in RESPONSES_PATHS]


def spend(events: list[dict[str, Any]]) -> int:
    return sum(i + o for i, _, o in (usage_tokens(e.get("usage")) for e in responses_requests(events)))


@dataclass
class CodexTurns:
    """Runs one user turn per call (module docstring); ``runner`` is subprocess.run, swappable in tests."""

    contexto: str
    session: str
    bound: int
    upstream: str
    key_helper: str
    scratch: Path
    env: dict[str, str]
    codex_upstream: str | None = None
    timeout: float = 900.0
    extra: tuple[str, ...] = ()
    runner: Callable[..., Any] = subprocess.run
    thread: str | None = None
    exits: list[int] = field(default_factory=list)

    def send(self, index: int, text: str) -> str:
        last = self.scratch / f"last-{index}.txt"
        cmd = turn_command(self.contexto, self.session, self.bound, self.upstream, self.key_helper, last,
                           self.thread, self.codex_upstream, extra=self.extra)
        done = self.runner(cmd, input=text, cwd=self.scratch, env=self.env, capture_output=True, text=True,
                           timeout=self.timeout)
        self.exits.append(done.returncode)
        if self.thread is None:
            for line in done.stdout.splitlines():
                if line.startswith("{") and '"thread.started"' in line:
                    self.thread = json.loads(line).get("thread_id")
                    break
        return last.read_text() if last.exists() else ""


def run_session(s: script.Script, turns: CodexTurns, session_log: Path, approved: int, estimates: list[int],
                reserve: int = 0) -> dict[str, Any]:
    """Every turn of ``s`` in order, refusing any turn that could pass ``approved``; returns the
    record (spend, turns completed, why it stopped, and the probe-turn answers)."""
    if len(estimates) != len(s.turns):
        raise ValueError("one estimate per turn")
    answers: list[str] = []
    stopped, completed = None, 0
    for i, text in enumerate(s.turns):
        spent = spend(read_log(session_log))
        if spent + estimates[i] + reserve > approved:
            stopped = f"turn {i + 1} could pass the approved {approved:,} (spent {spent:,})"
            break
        answer = turns.send(i, text)
        completed += 1
        if i >= s.n_content:
            answers.append(answer)
        if turns.exits[-1] != 0:
            stopped = f"turn {i + 1}: codex exited {turns.exits[-1]}"
            break
    return {"spent": spend(read_log(session_log)), "approved": approved, "turns": len(s.turns),
            "turns_completed": completed, "probe_turns_completed": len(answers), "stopped": stopped,
            "thread": bool(turns.thread), "codex_exits": turns.exits, "answers": "\n".join(answers)}


def analyze(events: list[dict[str, Any]], bound: int) -> dict[str, Any]:
    """The acceptance numbers for a Codex session log (Responses paths; detectors.analyze reads only
    /v1/messages). Content-free: counts, sizes, and event names."""
    requests = responses_requests(events)
    hits = eligible = 0
    rotations: dict[str, int] = {}
    max_input = over_real = 0
    for e in requests:
        inp, cached, _ = usage_tokens(e.get("usage"))
        max_input = max(max_input, inp)
        rotated = [t for t in e.get("transform_events") or [] if t.get("event") == "rotation"]
        for t in rotated:
            rotations[t.get("reason", "?")] = rotations.get(t.get("reason", "?"), 0) + 1
        over_real += sum(1 for u in e.get("usage_events") or []
                         if u.get("event") == "over_bound" and u.get("source") == "real")
        # Criterion 4: a request whose prefix the previous one sent can read the cache. Every Codex turn
        # is a new proxy (epochs restart per process), so the first request and rotations are skipped.
        if e is not requests[0] and not rotated and e.get("status") == 200:
            eligible += 1
            hits += int(cached > 0)
    mcp = [e for e in events if e.get("event") == "mcp_call"]
    failures = sorted({e["event"] for e in events if e.get("event") in FAILURE_EVENTS})
    model_4xx = sum(1 for e in requests if isinstance(e.get("status"), int) and 400 <= e["status"] < 500)
    over_real += sum(1 for e in events if e.get("event") == "over_bound" and e.get("source") == "real")
    return {"requests": len(requests), "model_4xx": model_4xx, "max_input": max_input, "bound": bound,
            "over_bound_real": over_real, "failure_events": failures, "rotations": rotations,
            "cache_hit_rate": round(hits / eligible, 3) if eligible else None,
            "mcp_calls": {t: sum(1 for e in mcp if e.get("tool") == t) for t in sorted({e.get("tool") for e in mcp})},
            "mcp_failed": sum(1 for e in mcp if not e.get("ok")), "spent": spend(events),
            "acceptance_1": model_4xx == 0 and over_real == 0 and not failures and max_input <= bound}
