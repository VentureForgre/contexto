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
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from acceptance import script
from acceptance.detectors import FAILURE_EVENTS

MODEL = "gpt-6-astra"
# Codex retries a failed request up to 30 times by default against a failing upstream [measured, A16:
# one turn, 30 POSTs]. Pinned to none, so every billed request is counted once: a failure ends the turn
# (1 POST measured) and the session stops. Keys: the provider's request_max_retries and
# stream_max_retries (codex-cli 0.154.0 config schema).
RETRY_PIN = ("-c", "model_providers.contexto.request_max_retries=0",
             "-c", "model_providers.contexto.stream_max_retries=0")
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
            "-c", "model_reasoning_effort=low", *RETRY_PIN, "-o", str(last_message), *extra]
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


def complete_lines(data: bytes) -> tuple[list[dict[str, Any]], bool]:
    """The events of every newline-terminated line, and whether an unterminated last line was left out
    (a write in progress: re-read later, never parsed). A malformed complete line raises ValueError."""
    *complete, rest = data.split(b"\n")
    return [json.loads(line) for line in complete if line.strip()], bool(rest.strip())


def read_log(session_log: Path) -> list[dict[str, Any]]:
    """The session log's complete events; none before the proxy has created it (before a first turn)."""
    if not session_log.exists():
        return []
    return complete_lines(session_log.read_bytes())[0]


class WatchFailed(RuntimeError):
    """The spend watcher could not read the session log; the turn is stopped (fail closed)."""


@dataclass
class SpendWatch:
    """The in-turn spend watcher (intake and reviewer-1 on PR #154): called each poll while a turn runs,
    True when the turn must stop because ``spent + 2 x inflight`` would pass ``approved``. It fails
    closed: a malformed complete line or an unreadable log raises at once, and a log that stays missing
    or keeps an unterminated last line raises once the grace is over: at most ``grace_polls`` polls and
    at most ``grace_seconds``, whichever comes first (intake, 2026-10-16 11:48Z). CodexTurns then stops
    the turn. An unterminated line is a write in progress: it is never parsed, only re-read."""

    session_log: Path
    approved: int
    inflight: int
    grace_polls: int = 2
    grace_seconds: float = 5.0
    startup_seconds: float = 30.0
    _pending_since: float | None = None
    _pending_polls: int = 0
    _turn_started: float | None = None

    def begin_turn(self) -> None:
        """A new turn starts (run_session calls this before each turn). For ``startup_seconds`` a missing
        log is the turn's process still starting (reviewer-1 on PR #154): the launcher writes
        ``proxy_start`` to the log before it starts Codex, so no request can be billed while the log is
        missing, and the dry run's turns start well within a second. Past the window a missing log
        counts toward the fail-closed grace as before."""
        self._turn_started = time.monotonic()
        self._pending_since, self._pending_polls = None, 0

    def __call__(self) -> bool:
        try:
            data = self.session_log.read_bytes()
        except FileNotFoundError:
            starting = self._turn_started is not None and time.monotonic() - self._turn_started < self.startup_seconds
            if not starting:
                self._pending("the session log is missing")
            return False
        except OSError as exc:
            raise WatchFailed(f"the session log cannot be read: {type(exc).__name__}") from exc
        try:
            events, partial = complete_lines(data)
        except ValueError as exc:
            raise WatchFailed("the session log has a malformed line") from exc
        if partial:
            self._pending("the session log's last line stays unterminated")
        else:
            self._pending_since, self._pending_polls = None, 0
        return spend(events) + 2 * self.inflight > self.approved

    def _pending(self, why: str) -> None:
        """One more poll in the pending state; past the grace (polls or seconds), fail closed."""
        now = time.monotonic()
        if self._pending_since is None:
            self._pending_since = now
        self._pending_polls += 1
        if self._pending_polls > self.grace_polls or now - self._pending_since > self.grace_seconds:
            raise WatchFailed(f"{why} past the grace ({self.grace_polls} polls, {self.grace_seconds:g} s)")


def responses_requests(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("event") == "request" and e.get("method") == "POST"
            and e.get("path") in RESPONSES_PATHS]


def spend(events: list[dict[str, Any]]) -> int:
    return sum(i + o for i, _, o in (usage_tokens(e.get("usage")) for e in responses_requests(events)))


@dataclass
class CodexTurns:
    """Runs one user turn per call (module docstring). The turn runs in its own process group so the
    in-turn spend watcher can stop it: ``watch`` is polled while the turn runs, and when it returns True
    the whole group (contexto, codex, the MCP server) is terminated. ``popen`` is swappable in tests."""

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
    popen: Callable[..., Any] = subprocess.Popen
    watch: Callable[[], bool] | None = None
    poll: float = 0.25
    thread: str | None = None
    exits: list[int] = field(default_factory=list)
    stopped_by_watcher: list[int] = field(default_factory=list)

    def send(self, index: int, text: str) -> str:
        last = self.scratch / f"last-{index}.txt"
        events = self.scratch / f"events-{index}.jsonl"
        cmd = turn_command(self.contexto, self.session, self.bound, self.upstream, self.key_helper, last,
                           self.thread, self.codex_upstream, extra=self.extra)
        with events.open("w") as out, (self.scratch / f"stderr-{index}.txt").open("w") as err:
            proc = self.popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=self.scratch, env=self.env,
                              text=True, start_new_session=True)
            try:
                proc.stdin.write(text)
                proc.stdin.close()
                deadline = time.monotonic() + self.timeout
                while proc.poll() is None:
                    if self.watch is not None and self.watch():
                        self.stopped_by_watcher.append(index)
                        self._stop(proc)
                        break
                    if time.monotonic() > deadline:
                        self._stop(proc)
                        break
                    time.sleep(self.poll)
                code = proc.wait()
            except BaseException:
                # Fail closed: whatever went wrong (a watcher that cannot read the log, an interrupt),
                # the turn's whole process group is stopped before the error goes on.
                self.stopped_by_watcher.append(index)
                self._stop(proc)
                self.exits.append(proc.wait())
                raise
        self.exits.append(code)
        if self.thread is None:
            for line in events.read_text().splitlines():
                if line.startswith("{") and '"thread.started"' in line:
                    self.thread = json.loads(line).get("thread_id")
                    break
        return last.read_text() if last.exists() else ""

    @staticmethod
    def _stop(proc: Any) -> None:
        """Terminate the turn's process group, then kill it if it has not exited after 5 seconds."""
        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, None)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue


def run_session(s: script.Script, turns: CodexTurns, session_log: Path, approved: int, estimates: list[int],
                reserve: int = 0, inflight: int = 0, start: int = 0) -> dict[str, Any]:
    """Every turn of ``s`` in order; returns the record (spend, turns completed, why it stopped, and
    the probe-turn answers). Two stops keep spend at or under ``approved``: a turn is never started
    when ``spent + estimate + reserve`` could pass it, and inside a turn the spend watcher stops the
    turn's processes once ``spent + 2 x inflight`` would (``inflight`` is one request's worst case,
    bound plus output; twice it covers a request that started before the last one was logged).

    Resume: with retries pinned, a failed request ends the turn and the session. ``start`` (the first
    turn not completed) and ``turns.thread`` (the record's ``thread_id``) continue the same Contexto
    session and Codex thread; spend is read from the same session log, so the cap counts what the
    failed attempt was billed."""
    if inflight:
        turns.watch = SpendWatch(session_log, approved, inflight)
    if len(estimates) != len(s.turns):
        raise ValueError("one estimate per turn")
    answers: list[str] = []
    by_turn: dict[str, str] = {}
    stopped, completed, failed_turn = None, start, None
    for i, text in enumerate(s.turns):
        if i < start:
            continue
        try:
            spent = spend(read_log(session_log))
        except ValueError:
            stopped = f"turn {i + 1}: not started, the session log has a malformed line"
            break
        if spent + estimates[i] + reserve > approved:
            stopped = f"turn {i + 1} could pass the approved {approved:,} (spent {spent:,})"
            break
        if isinstance(turns.watch, SpendWatch):
            turns.watch.begin_turn()
        try:
            answer = turns.send(i, text)
        except Exception as exc:  # noqa: BLE001 - fail closed: the turn was stopped, the session stops
            stopped = f"turn {i + 1}: stopped, the spend watcher failed closed ({type(exc).__name__}: {exc})"
            failed_turn = i
            break
        if turns.stopped_by_watcher and turns.stopped_by_watcher[-1] == i:
            stopped = f"turn {i + 1}: stopped by the spend watcher (approved {approved:,})"
            failed_turn = i
            break
        if turns.exits[-1] != 0:  # not completed: a resume starts again at this turn
            stopped = f"turn {i + 1}: codex exited {turns.exits[-1]}"
            failed_turn = i
            break
        completed += 1
        if i >= s.n_content:
            answers.append(answer)
            by_turn[str(i)] = answer
    try:
        spent_total = spend(read_log(session_log))
    except ValueError:
        spent_total = None
    return {"spent": spent_total, "approved": approved, "turns": len(s.turns), "started_at": start,
            "turns_completed": completed, "probe_turns_completed": len(answers), "stopped": stopped,
            "thread_id": turns.thread, "codex_exits": turns.exits, "failed_turn": failed_turn,
            "answers_by_turn": by_turn,
            "stopped_by_watcher": list(turns.stopped_by_watcher), "answers": "\n".join(answers)}


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


def join_records(s: script.Script, records: list[dict[str, Any]]) -> dict[str, Any]:
    """One scored record from an original attempt and its resumes (reviewer-1 on PR #154), in the order
    they ran. Answers join by turn index, a later attempt's answer for a turn replacing an earlier one;
    each attempt is listed with the turn it failed on, so the record shows which turn was attempted
    twice. ``answers`` follows the probe turns' order; ``complete`` says every probe turn has one."""
    by_turn: dict[str, str] = {}
    for r in records:
        by_turn.update(r.get("answers_by_turn") or {})
    probe = [str(i) for i in range(s.n_content, len(s.turns))]
    last = records[-1] if records else {}
    return {"attempts": [{k: r.get(k) for k in ("started_at", "turns_completed", "failed_turn", "stopped")}
                         for r in records],
            "answers_by_turn": by_turn, "answers": "\n".join(by_turn[t] for t in probe if t in by_turn),
            "complete": all(t in by_turn for t in probe), "turns_completed": last.get("turns_completed"),
            "spent": last.get("spent"), "approved": last.get("approved"), "thread_id": last.get("thread_id")}
