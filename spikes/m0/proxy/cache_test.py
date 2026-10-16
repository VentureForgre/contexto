"""Seat cache-miss test: which prompt block changes between consecutive Claude Code requests?

    python3 cache_test.py a   # seat-like: the lab seat's registered hook commands, hooks enabled
    python3 cache_test.py b   # identical, with "disableAllHooks": true

Three tiny turns in one session, about 60 s apart (inside the 5 minute cache TTL), through the
measuring proxy with per-block keyed hashes. Model pinned to claude-haiku-5-5. The child runs under
this worktree so it inherits the seat credential as configured; a flag-level --settings carries the
spike URL (and, for "a", hook command paths only). Its environment drops this seat's CLAUDE*,
OPENRIG_*, RIGGED_* variables, so no seat identity is present. The session is persisted so hook
attachments can be counted; that transcript stays local under ~/.claude/projects.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from extract import first_changed_block, total_tokens
from proxy import SpikeProxy
from run_session import REPO, SPIKE_ROOT, child_env, inherited_claude_base

# The lab seat's own worktree, by default the sibling worktree named lab-subject; override with
# M0_LAB_WORKTREE. Its registered activity hook is what condition "a" reproduces.
LAB_HOOK_SCRIPT = Path(".openrig") / "hooks" / "scripts" / "activity-relay.cjs"
SEAT_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "Notification")
PROMPT = "Reply with the single word OK. Do not use any tools."


def lab_hook_command() -> str:
    worktree = Path(os.environ.get("M0_LAB_WORKTREE") or REPO.parent / "lab-subject")
    script = worktree / LAB_HOOK_SCRIPT
    if not script.is_file():
        sys.exit(f"lab seat hook script not found under {worktree}; set M0_LAB_WORKTREE")
    return f"node {shlex.quote(str(script))}"


def settings_for(condition: str, proxy_url: str) -> dict[str, Any]:
    settings: dict[str, Any] = {"env": {"ANTHROPIC_BASE_URL": proxy_url}}
    if condition == "a":
        hook = lab_hook_command()
        settings["hooks"] = {event: [{"hooks": [{"type": "command", "command": hook}]}] for event in SEAT_HOOK_EVENTS}
    else:
        settings["disableAllHooks"] = True
    return settings


def transcript_facts(scratch: Path, session_id: str | None) -> dict[str, Any]:
    """Counts of attachment and system entry types in the child's transcript (names only)."""
    project = Path.home() / ".claude" / "projects" / str(scratch.resolve()).replace("/", "-").replace(".", "-")
    files = [project / f"{session_id}.jsonl"] if session_id else sorted(project.glob("*.jsonl"))
    counts: dict[str, int] = {}
    for path in files:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") == "attachment":
                kind = "attachment:" + str((entry.get("attachment") or {}).get("type"))
            elif entry.get("type") == "system":
                kind = "system:" + str(entry.get("subtype"))
            else:
                continue
            counts[kind] = counts.get(kind, 0) + 1
    return {"transcript_found": any(p.exists() for p in files), "entry_counts": counts}


def analyze(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls = [r for r in records if r.get("endpoint", "").endswith("/messages") and r.get("status") == 200]
    out, previous = [], None
    for record in calls:
        usage = record.get("usage") or {}
        blocks = record.get("blocks") or []
        out.append({
            "seq": record["seq"], "turn": record.get("turn"), "ts": record["ts"],
            "input": usage.get("input_tokens"), "cache_write": usage.get("cache_creation_input_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"), "output": usage.get("output_tokens"),
            "blocks": len(blocks), "cache_control_at": [b[0] for b in blocks if b[3]],
            "first_changed_vs_previous": first_changed_block(previous, blocks) if previous is not None else None,
        })
        previous = blocks
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("condition", choices=("a", "b"))
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--gap", type=float, default=60.0)
    parser.add_argument("--cap", type=int, default=190_000)
    args = parser.parse_args(argv)
    upstream = inherited_claude_base()
    if not upstream:
        sys.exit("no inherited ANTHROPIC_BASE_URL")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = SPIKE_ROOT / f"cachetest-{args.condition}-{stamp}"
    scratch = run_dir / "scratch"
    scratch.mkdir(parents=True)
    proxy = SpikeProxy(upstream, run_dir, "claude", cap_tokens=args.cap, block_hashes=True)
    url = proxy.start()
    cmd = ["claude", "-p", "--model", "claude-haiku-5-5", "--settings", json.dumps(settings_for(args.condition, url)),
           "--strict-mcp-config", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    child = subprocess.Popen(cmd, cwd=scratch, env=child_env(None, url), text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    session_id, events = None, {}
    try:
        for turn in range(1, args.turns + 1):
            if turn > 1:
                time.sleep(args.gap)
            if proxy.over_cap():
                break
            proxy.turn = turn
            child.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": PROMPT}}) + "\n")
            child.stdin.flush()
            for line in child.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                key = f'{event.get("type")}:{event.get("subtype", "")}'.rstrip(":")
                events[key] = events.get(key, 0) + 1
                session_id = session_id or event.get("session_id")
                if event.get("type") == "result":
                    break
            proxy.wait_idle()
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
        proxy.stop()
    result = {
        "condition": args.condition, "run": run_dir.name, "spent": proxy.spent, "refused": proxy.refused,
        "cli_events": events, "transcript": transcript_facts(scratch, session_id), "calls": analyze(proxy.records),
    }
    (run_dir / "cache_test.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
