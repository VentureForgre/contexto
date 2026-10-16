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

It stops at once (the turn's process group is killed) and records why on: any 401 or 403, any other
4xx or 5xx, a request whose output exceeds 32,000 tokens, Codex reporting a failed turn or an error
(``turn.failed`` or ``error`` in its ``--json`` events: how a refusal or an upstream error reaches
the harness, read by event type only, never by answer text), the spend watcher, or any other failure.

It writes ``<out>/codex-step1-record.json``: content-free numbers and event counts only."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import codex_budget, codex_driver, codex_recall_credit, script  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
OUTPUT_CAP = codex_budget.OUTPUT_ALLOWANCE  # 32,000 output tokens per request: more stops the run
FAILED_TURN_EVENTS = ("turn.failed", "error")  # Codex --json event types for a failed turn or an error
_HOST = re.compile(rb"https?://([A-Za-z0-9.\-]+)")


class LiveStop(RuntimeError):
    """A stop rule fired; the message is the content-free reason recorded."""


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
        return stop


class LiveTurns(codex_driver.CodexTurns):
    """CodexTurns that also stops when Codex's own ``--json`` events report a failed turn or an error."""

    def send(self, index: int, text: str) -> str:
        answer = super().send(index, text)
        if isinstance(self.watch, LiveWatch) and self.watch():
            # Checked once more after the turn: a 401, or a request that passed its estimate, can end Codex
            # before the next poll, and the log's reason is what is recorded, not Codex's exit.
            raise LiveStop(f"stopped by the spend watcher (approved {self.watch.approved:,})")
        events = self.scratch / f"events-{index}.jsonl"
        kinds = {json.loads(line).get("type") for line in events.read_text().splitlines() if line.startswith("{")}
        failed = sorted(k for k in kinds if k in FAILED_TURN_EVENTS)
        if failed:
            raise LiveStop(f"codex reported {', '.join(failed)} on turn {index + 1}")
        return answer


def outside_hosts(codex_home: Path, proxy_host: str) -> list[str]:
    """Hosts named in Codex's own local logs other than the proxy (host names only; nothing is probed)."""
    hosts: set[bytes] = set()
    for path in codex_home.rglob("*"):
        if path.is_file() and (path.suffix in (".log", ".txt") or "log" in path.parts):
            hosts.update(_HOST.findall(path.read_bytes()))
    names = {h.decode() for h in hosts}
    return sorted(n for n in names if n not in ("127.0.0.1", "localhost", proxy_host))


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
           "codex_command": template, "scratch": str(root), "codex_home": str(store / "sessions" / "codex-step1" / "codex-home"),
           "contexto_home": str(store)}
    print(json.dumps({"preflight": pre}, indent=2))
    if a.approved < estimate:
        return refuse(f"--approved {a.approved:,} is below the step-1 worst case {estimate:,}")
    env, store = codex_driver.child_env(store, home, mcp_home_workaround=False)
    session = "codex-step1"
    log = store / "sessions" / session / "log.jsonl"
    turns = LiveTurns(a.contexto, session, a.bound, upstream, a.key_helper, scratch, env,
                      codex_upstream=upstream + "/v1")
    turns.watch = LiveWatch(log, a.approved, a.bound + OUTPUT_CAP)
    per = a.bound + OUTPUT_CAP
    estimates = [per] * s.n_content + [(1 + codex_budget.TOOL_ROUNDS) * per] * len(s.probe_turn_ids)
    ran = codex_driver.run_session(s, turns, log, a.approved, estimates)
    events = codex_driver.read_log(log)
    proxy_host = re.sub(r"^https?://", "", upstream).split("/")[0].split(":")[0]
    out = record(s, ran, events, log.parent, a.approved, a.bound,
                 {"preflight": pre, "outside_hosts": outside_hosts(log.parent / "codex-home", proxy_host)})
    (a.out / "codex-step1-record.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: out[k] for k in ("ran", "spent", "requests", "max_input", "max_output")}, indent=2))
    return 0 if ran["stopped"] is None and ran["turns_completed"] == ran["turns"] else 1


if __name__ == "__main__":
    sys.exit(main())
