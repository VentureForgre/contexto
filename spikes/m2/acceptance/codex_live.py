"""Codex acceptance step 1, the live entry point (M2 A22-prep): the H1 arm alone (acceptance 1 and 3)
through ``contexto run codex`` against ``--upstream``. The same code drives a zero-spend run when
``--upstream`` is a 127.0.0.1 stub (codex_stub, or a test's).

    uv run python spikes/m2/acceptance/codex_live.py <contexto executable> <out dir> \\
        --upstream <base URL> --key-helper <command> --approved <tokens>
        [--model gpt-6-astra] [--reasoning-effort low] [--bound 64000] [--turn-kbytes 20] [--seed S]

Before any request it prints the pre-flight: the running code's commit and tree, the Codex command
template (the key helper elided), the scratch directories, and codex_budget's step-1 worst case. It
refuses to start when the working tree is dirty or not at a commit, or when ``--approved`` is below
that worst case. The key helper command is passed to ``contexto run``, which hands it to Codex's
provider config; the key itself is never in a file, an argument, a log, or the record.

It stops (the turn's process group is killed, or the session ends after the turn) and records why on:
- any 401 or 403, and any other 4xx or 5xx, in the proxy's log;
- a request whose output exceeds 32,000 tokens;
- a Responses request that ended other than ``completed`` (A23's ``end_event``: incomplete, failed, or
  none for a stream cut off with no end event; or its ``response_status``), when the entry carries it;
- a refusal reply that arrives as a 200. Codex 0.154.0 drops a message that has any ``refusal`` content
  part (measured with a stub: no ``agent_message`` event and nothing in its rollout), so a turn that ends
  with no ``agent_message`` item stops the run; a ``refusal`` part in the rollout's ``response_item``
  payloads, for a Codex that keeps them, stops it too (both read by type only);
- Codex's ``--json`` events reporting ``turn.failed`` or ``error``;
- the spend watcher, or any other failure.
Answer text is never read. One request whose output passes 32,000 tokens can overrun the approval by
that request's excess output: the stop comes once the request is logged.

The record says where Codex went in two ways, neither complete: the hosts named in Codex's own local
logs (its ``*.log`` files, ``log`` directory and SQLite logs, with the files and rows scanned; not a
network observation), and a best-effort sample of each turn's process group's remote addresses
(``lsof`` every second, so short connections can be missed; it never stops the run, and the record
says if it could not run).

It writes ``<out>/codex-step1-record.json``: content-free numbers and event counts only."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import codex_budget, codex_driver, codex_recall_credit, script  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
OUTPUT_CAP = codex_budget.OUTPUT_ALLOWANCE  # 32,000 output tokens per request: more stops the run
FAILED_TURN_EVENTS = ("turn.failed", "error")  # Codex --json event types for a failed turn or an error
END_STATUS_KEYS = ("end_event", "response_status")  # A23: how a Responses request ended, as the proxy logs it
SAMPLE_SECONDS = 1.0  # the network sampler's period
OVERRUN = ("one request whose output passes 32,000 tokens can overrun the approval by that request's excess "
           "output: the stop comes once the request is logged")
_REMOTE = re.compile(r"->(\S+?)(?:\s|$)")
_HOST = re.compile(rb"https?://([A-Za-z0-9.\-]+)")


class LiveStop(RuntimeError):
    """A stop rule fired; the message is the content-free reason recorded."""

    stop_rule = True


def preflight(repo: Path) -> dict[str, Any]:
    """The running code's commit and tree; refuses a dirty working tree or a checkout not at a commit."""
    def git(*args: str) -> str:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if done.returncode != 0:
            raise LiveStop(f"git {args[0]} failed: not a checkout at a commit")
        return done.stdout.strip()

    head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise LiveStop("the working tree has local changes; run from a clean checkout of the approved commit")
    return {"commit": head, "tree": tree}


@dataclass
class LiveWatch(codex_driver.SpendWatch):
    """The spend watcher plus the live stop rules read from the proxy's log: any 4xx or 5xx on a
    Responses request (401 and 403 named), and any request whose output passes ``OUTPUT_CAP``."""

    def __call__(self) -> bool:
        stop = super().__call__()
        for e in codex_driver.responses_requests(codex_driver.read_log(self.session_log)):
            status = e.get("status")
            if status in (401, 403):
                raise LiveStop(f"upstream answered {status} (auth)")
            if isinstance(status, int) and status >= 400:
                raise LiveStop(f"upstream answered {status}")
            if codex_driver.usage_tokens(e.get("usage"))[2] > OUTPUT_CAP:
                raise LiveStop(f"a request's output passed {OUTPUT_CAP:,} tokens")
            for key in END_STATUS_KEYS:
                ended = e.get(key)
                if ended is not None and ended != "completed":
                    detail = e.get("incomplete_reason") or e.get("error_code")
                    raise LiveStop(f"a response ended with {key} {ended}" + (f" ({detail})" if detail else ""))
        return stop


def refusal_parts(codex_home: Path | None) -> int:
    """Refusal content parts in Codex's rollout (``response_item`` payloads), counted by part type only."""
    count = 0
    for path in (codex_home.rglob("rollout-*.jsonl") if codex_home and codex_home.exists() else []):
        for line in path.read_text().splitlines():
            item = json.loads(line) if line.startswith("{") else {}
            payload = item.get("payload") if item.get("type") == "response_item" else None
            parts = payload.get("content") if isinstance(payload, dict) else None
            count += sum(1 for p in parts or [] if isinstance(p, dict) and p.get("type") == "refusal")
    return count


def _loopback(address: str) -> bool:
    return address.split()[-1].startswith(("127.", "[::1]", "localhost"))


def remote_addresses(lsof_output: str) -> set[str]:
    """``command host:port`` for each connection's remote end in ``lsof -nP -i`` output (the command name
    lsof shows, so an outside connection says which program made it)."""
    return {f"{line.split()[0]} {m.group(1)}" for line in lsof_output.splitlines()
            for m in [_REMOTE.search(line)] if m}


@dataclass
class LiveTurns(codex_driver.CodexTurns):
    """CodexTurns with the live checks after each turn (the spend watcher once more, a refusal part in the
    rollout, Codex's failed-turn events) and a best-effort network sample during it."""

    codex_home: Path | None = None
    network: dict[str, Any] = field(default_factory=lambda: {"remote": set(), "samples": 0, "errors": []})
    refusals_seen: int = 0

    def _sample(self, proc: Any) -> None:
        """Remote addresses of the turn's process group, every SAMPLE_SECONDS; it never stops the run."""
        while proc.poll() is None:
            try:
                done = subprocess.run(["lsof", "-nP", "-i", "-a", "-g", str(proc.pid)], capture_output=True,
                                      text=True, timeout=5)
                self.network["remote"].update(remote_addresses(done.stdout))
                self.network["samples"] += 1
            except (OSError, subprocess.SubprocessError) as exc:
                self.network["errors"].append(type(exc).__name__)
                return
            time.sleep(SAMPLE_SECONDS)

    def send(self, index: int, text: str) -> str:
        popen, sampler = self.popen, []

        def started(cmd, **kwargs):
            proc = popen(cmd, **kwargs)
            sampler.append(threading.Thread(target=self._sample, args=(proc,), daemon=True))
            sampler[-1].start()
            return proc

        self.popen = started
        try:
            answer = super().send(index, text)
        finally:
            self.popen = popen
            for thread in sampler:
                thread.join(timeout=SAMPLE_SECONDS + 5)
        if isinstance(self.watch, LiveWatch) and self.watch():
            # Checked once more after the turn: a 401, or a request that passed its estimate, can end Codex
            # before the next poll, and the log's reason is what is recorded, not Codex's exit.
            raise LiveStop(f"stopped by the spend watcher (approved {self.watch.approved:,})")
        refused = refusal_parts(self.codex_home)
        if refused > self.refusals_seen:
            self.refusals_seen = refused
            raise LiveStop(f"a refusal content part on turn {index + 1}")
        events = [json.loads(line) for line in (self.scratch / f"events-{index}.jsonl").read_text().splitlines()
                  if line.startswith("{")]
        kinds = {e.get("type") for e in events}
        failed = sorted(k for k in kinds if k in FAILED_TURN_EVENTS)
        if failed:
            raise LiveStop(f"codex reported {', '.join(failed)} on turn {index + 1}")
        if not any(e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message"
                   for e in events):
            raise LiveStop(f"turn {index + 1} ended with no assistant message")
        return answer


def outside_hosts(codex_home: Path, proxy_host: str) -> dict[str, Any]:
    """Hosts named in Codex's own local logs (``*.log``, its ``log`` directory, and its SQLite logs),
    other than the proxy. Host names only, and what was scanned; this is not a network observation."""
    hosts: set[bytes] = set()
    files = rows = 0
    for path in codex_home.rglob("*") if codex_home.exists() else []:
        rel = path.relative_to(codex_home).parts
        if path.is_file() and (path.suffix == ".log" or rel[:1] == ("log",)):
            files += 1
            hosts.update(_HOST.findall(path.read_bytes()))
        elif path.is_file() and path.suffix == ".sqlite":
            try:
                db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                    for row in db.execute(f'SELECT * FROM "{table}"'):
                        rows += 1
                        hosts.update(_HOST.findall(" ".join(str(v) for v in row).encode()))
                db.close()
            except sqlite3.Error:
                pass
    names = sorted({h.decode() for h in hosts} - {"127.0.0.1", "localhost", proxy_host})
    return {"hosts": names, "files_scanned": files, "sqlite_rows_scanned": rows,
            "label": f"hosts named in Codex's local logs: {names}, {files} files and {rows} rows scanned; "
                     "not a network observation"}


def record(s: script.Script, ran: dict[str, Any], events: list[dict[str, Any]], session_dir: Path,
           approved: int, bound: int, extra: dict[str, Any]) -> dict[str, Any]:
    requests = codex_driver.responses_requests(events)
    usage = [codex_driver.usage_tokens(e.get("usage")) for e in requests]
    pages = [json.loads(p.read_text()) for p in sorted((session_dir / "pages").glob("pg_*.json"))]
    credit = codex_recall_credit.credit(list(s.probes), ran.pop("answers"), events, pages,
                                        probe_turn_ids=s.probe_turn_ids, expected_turns=len(s.turns))
    held = [(e.get("stream_hold") or {}).get("held_events", 0) for e in requests]
    in_view = sum(1 for e in events if e.get("event") == "mcp_call" for r in e.get("returned") or []
                  if r.get("source") == "in_view")
    history = sum(codex_budget.turn_tokens(x) + codex_budget.REPLY_TOKENS for x in s.content_bytes)
    spent = {"input": sum(u[0] for u in usage), "cached": sum(u[1] for u in usage),
             "output": sum(u[2] for u in usage)}
    spent["total"] = spent["input"] + spent["output"]
    return {**extra, "ran": ran, "turns": {"content": s.n_content, "probe": len(s.probe_turn_ids)},
            "history_tokens": history, "history_needed": int(2.5 * bound),
            "acceptance_1": codex_driver.analyze(events, bound),
            "acceptance_3": {"credit": credit, "expected_turns": len(s.turns), "in_view_spans": in_view},
            "spent": {**spent, "approved": approved, "within_line": spent["total"] <= approved},
            "requests": len(requests), "max_input": max((u[0] for u in usage), default=0),
            "max_output": max((u[2] for u in usage), default=0),
            "held_events": {"requests_held": sum(1 for h in held if h), "requests_unheld": sum(1 for h in held if not h),
                            "max": max(held, default=0)}}


def main(argv: list[str] | None = None, repo: Path = REPO) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("contexto")
    ap.add_argument("out", type=Path)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--key-helper", required=True)
    ap.add_argument("--approved", type=int, required=True)
    ap.add_argument("--model", default=codex_driver.MODEL, choices=[codex_driver.MODEL])
    ap.add_argument("--reasoning-effort", default="low", choices=["low"])  # what codex_driver.turn_command sends
    ap.add_argument("--bound", type=int, default=64_000)
    ap.add_argument("--turn-kbytes", type=int, default=20)
    ap.add_argument("--seed", type=int, default=script.DEFAULT_SEED)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)

    def refuse(reason: str) -> int:
        (a.out / "codex-step1-record.json").write_text(json.dumps({"refused": reason}, indent=2))
        print(f"codex_live: refused: {reason}", file=sys.stderr)
        return 2

    try:
        code = preflight(repo)
    except LiveStop as exc:
        return refuse(str(exc))
    s = codex_budget.history_turns(a.bound, a.turn_kbytes * 1000, a.seed)
    estimate = codex_budget.staged(s, a.bound)["step1_h1"]
    root = Path(tempfile.mkdtemp(prefix="contexto-codex-live-"))  # outside the repository
    scratch, home, store = root / "scratch", root / "home", root / "store"
    scratch.mkdir()
    home.mkdir()
    upstream = a.upstream.rstrip("/")
    template = codex_driver.turn_command("<contexto>", "codex-step1", a.bound, upstream, "<key helper elided>",
                                         Path("<last message>"), codex_upstream=upstream + "/v1", model=a.model)
    pre = {"commit": code["commit"], "tree": code["tree"], "model": a.model, "reasoning_effort": a.reasoning_effort,
           "bound": a.bound, "approved": a.approved, "step1_worst_case": estimate, "turns": len(s.turns),
           "codex_command": template, "codex_pins": [*codex_driver.RETRY_PIN, *codex_driver.QUIET_PIN],
           "overrun_bound": OVERRUN, "scratch": str(root), "codex_home": str(store / "sessions" / "codex-step1" / "codex-home"),
           "contexto_home": str(store)}
    print(json.dumps({"preflight": pre}, indent=2))
    if a.approved < estimate:
        return refuse(f"--approved {a.approved:,} is below the step-1 worst case {estimate:,}")
    env, store = codex_driver.child_env(store, home, mcp_home_workaround=False)
    session = "codex-step1"
    log = store / "sessions" / session / "log.jsonl"
    turns = LiveTurns(a.contexto, session, a.bound, upstream, a.key_helper, scratch, env,
                      codex_upstream=upstream + "/v1")
    turns.codex_home = log.parent / "codex-home"
    turns.watch = LiveWatch(log, a.approved, a.bound + OUTPUT_CAP)
    per = a.bound + OUTPUT_CAP
    estimates = [per] * s.n_content + [(1 + codex_budget.TOOL_ROUNDS) * per] * len(s.probe_turn_ids)
    ran = codex_driver.run_session(s, turns, log, a.approved, estimates)
    events = codex_driver.read_log(log)
    proxy_host = re.sub(r"^https?://", "", upstream).split("/")[0].split(":")[0]
    out = record(s, ran, events, log.parent, a.approved, a.bound,
                 {"preflight": pre, "outside_hosts": outside_hosts(log.parent / "codex-home", proxy_host),
                  "network_sample": {"remote": sorted(turns.network["remote"]),
                                     "outside": sorted(r for r in turns.network["remote"] if not _loopback(r)),
                                     "samples": turns.network["samples"],
                                     "could_not_run": sorted(set(turns.network["errors"])),
                                     "label": f"sampled every {SAMPLE_SECONDS:g}s; short connections can be missed"},
                  "codex_pins": [*codex_driver.RETRY_PIN, *codex_driver.QUIET_PIN], "overrun_bound": OVERRUN})
    (a.out / "codex-step1-record.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: out[k] for k in ("ran", "spent", "requests", "max_input", "max_output")}, indent=2))
    return 0 if ran["stopped"] is None and ran["turns_completed"] == ran["turns"] else 1


if __name__ == "__main__":
    sys.exit(main())
