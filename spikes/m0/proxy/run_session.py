"""Scripted synthetic session through the measuring proxy, for Claude Code or Codex.

    python3 run_session.py claude --turns 32 --budget 450000
    python3 run_session.py codex --turns 32 --budget 450000

Each run gets its own directory under <repo>/.contexto/spikes/L1-a/ holding the proxy logs, a
synthetic scratch project the CLI works in, and turns.jsonl (one content-free line per user turn).

Rules this driver follows (docs/milestones/M0-infinite-session-study.md, Rules for the study):
- The proxy upstream is exactly the base URL the throwaway CLI would otherwise use, read at run
  time and never written to any file or log.
- The base URL override reaches only the throwaway child: a flag-level --settings JSON for
  Claude Code, the process environment for Codex. No settings or config file is edited.
- The child uses the credential it already inherits. Nothing is copied; this script never reads
  a key, token, or key helper.
- Every hook is disabled in the Claude Code child, and the seat's OpenRig identity variables are
  removed from both children's environment.
- The run stops before a user turn whose projected spend would cross --budget (all token
  categories counted, cache reads included).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from extract import total_tokens
from proxy import SpikeProxy

REPO = Path(__file__).resolve().parents[3]
SPIKE_ROOT = REPO / ".contexto" / "spikes" / "L1-a"
# Codex's built-in base URL under ChatGPT sign-in (vendor default; see the M0 measurements doc).
CODEX_CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
TURN_TIMEOUT_S = 900

PROMPTS = [
    "List the files in this project and tell me in two sentences what it does.",
    "Read ledgerlite/parse.py and briefly explain parse_row.",
    "Run `python3 -m unittest discover -s tests` and report the result.",
    "Read data/transactions.csv and tell me how many rows have category travel.",
    "Add a function total_by_month(rows) to ledgerlite/report.py that returns a dict of YYYY-MM to total amount.",
    "Add a unit test for total_by_month to tests/test_report.py and run the tests.",
    "Search the project for TODO comments and list them.",
    "In ledgerlite/rules.py add a rule that maps merchant names containing AIR to travel.",
    "Use python3 -c with ledgerlite.parse to print the number of distinct merchants in data/transactions.csv.",
    "Rename the function summarize to summarize_by_category everywhere it is used.",
    "Run the test suite again and report the result.",
    "Read NOTES.md and add a short changelog of what we changed so far.",
    "Make parse_row raise ValueError when the amount is not a number.",
    "Add a test for that ValueError and run the tests.",
    "Use a python3 one-liner with ledgerlite to show the five largest transactions by amount.",
    "Add top_n(rows, n) to ledgerlite/report.py and make python3 -m ledgerlite print the top 5 transactions.",
    "Run `python3 -m ledgerlite data/transactions.csv` and report the output.",
    "Without reading any files: what was the first function I asked you to add in this session?",
    "Read ledgerlite/report.py in full and point out any duplicated logic.",
    "Refactor the duplicated logic you found into a small helper.",
    "Run the test suite and report the result.",
    "Add a --month YYYY-MM option to python3 -m ledgerlite that filters rows to that month.",
    "Run `python3 -m ledgerlite data/transactions.csv --month 2026-03` and report the output.",
    "Find where amounts are converted with float( and switch them to decimal.Decimal.",
    "Run the tests and fix any failure.",
    "Add a one-line docstring to every public function in ledgerlite/report.py.",
    "Count lines per file in ledgerlite/ and tests/ with wc -l.",
    "Read tests/test_report.py and list which report functions have no test.",
    "Add tests for those functions and run the suite.",
    "Update the changelog in NOTES.md.",
    "Without reading any files: which merchant rule did I ask you to add earlier?",
    "Summarize everything we changed in this session in five bullets.",
]

SEED_FILES = {
    "ledgerlite/__init__.py": '"""Tiny ledger toolkit used as a synthetic workload."""\n',
    "ledgerlite/parse.py": '''import csv


def parse_row(row):
    """Convert one CSV row dict into a transaction dict."""
    return {
        "date": row["date"],
        "merchant": row["merchant"].strip(),
        "category": row["category"].strip() or "uncategorized",
        "amount": float(row["amount"]),
    }


def load(path):
    with open(path, newline="") as f:
        return [parse_row(r) for r in csv.DictReader(f)]
''',
    "ledgerlite/report.py": '''from collections import defaultdict


def summarize(rows):
    totals = defaultdict(float)
    for r in rows:
        totals[r["category"]] += r["amount"]
    return dict(totals)


def merchant_totals(rows):
    totals = defaultdict(float)
    for r in rows:
        totals[r["merchant"]] += r["amount"]
    return dict(totals)
''',
    "ledgerlite/rules.py": '''# TODO: load rules from a config file
RULES = {
    "GROCER": "food",
    "CAFE": "food",
    "RAIL": "travel",
}


def categorize(merchant):
    for needle, category in RULES.items():
        if needle in merchant.upper():
            return category
    return "uncategorized"
''',
    "tests/test_report.py": '''import unittest

from ledgerlite.report import summarize


class SummarizeTest(unittest.TestCase):
    def test_sums_by_category(self):
        rows = [{"category": "food", "amount": 2.0}, {"category": "food", "amount": 3.0}]
        self.assertEqual(summarize(rows), {"food": 5.0})


if __name__ == "__main__":
    unittest.main()
''',
    "NOTES.md": "# ledgerlite notes\n\nTODO: write a changelog.\n",
}

MERCHANTS = [("CITY GROCER", "food"), ("CORNER CAFE", "food"), ("NORTH RAIL", "travel"),
             ("SKY AIR", ""), ("BOOK NOOK", "books"), ("POWER CO", "utilities"), ("GYM ONE", "health")]


PROBE_PROMPT = "Reply with the single word OK. Do not use any tools."


# Large-read scenario: one turn reads about 200 KB of synthetic logs, so a single call carries a
# realistic burst of tool output. Owner decision D3: always with default compaction settings.
COMPACT_PROMPTS = [
    "Read data/logs/part1.log, data/logs/part2.log, data/logs/part3.log and data/logs/part4.log in full "
    "(all four, using the Read tool), then tell me how many ERROR lines each file has.",
    "Which of the four files had the most ERROR lines?",
    "What request id appears on the first line of part1.log? Answer from memory if you can.",
    "Say OK.",
]


def prompts_for(args: argparse.Namespace) -> list[str]:
    if args.probe:
        return [PROBE_PROMPT]
    return (COMPACT_PROMPTS if args.scenario == "compact" else PROMPTS)[: args.turns]


def seed_scratch(scratch: Path) -> None:
    for rel, text in SEED_FILES.items():
        path = scratch / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    rng = random.Random(7)
    lines = ["date,merchant,category,amount"]
    for _ in range(120):
        merchant, category = rng.choice(MERCHANTS)
        lines.append(f"2026-{rng.randint(1, 6):02d}-{rng.randint(1, 28):02d},{merchant},{category},{rng.uniform(2, 400):.2f}")
    (scratch / "data").mkdir(exist_ok=True)
    (scratch / "data" / "transactions.csv").write_text("\n".join(lines) + "\n")
    # Four ~60 KB logs, each under the Read tool's per-call size limit.
    (scratch / "data" / "logs").mkdir(exist_ok=True)
    levels = ["INFO"] * 8 + ["WARN", "ERROR"]
    for part in range(1, 5):
        log = [f"2026-03-{part:02d}T10:{i // 60 % 60:02d}:{i % 60:02d}Z {rng.choice(levels):5} "
               f"req-{rng.randrange(16**8):08x} svc={rng.choice(['api', 'db', 'cache', 'queue'])} "
               f"latency_ms={rng.randint(1, 900)}" for i in range(780)]
        (scratch / "data" / "logs" / f"part{part}.log").write_text("\n".join(log) + "\n")


# This seat's own Claude Code session wiring and OpenRig identity. A child that inherited them
# could attach to the seat's session or be attributed to the seat by a stray hook.
_SEAT_ENV_PREFIXES = ("CLAUDE", "OPENRIG_", "RIGGED_")


def child_env(base_url_var: str | None, proxy_url: str) -> dict[str, str]:
    """Parent environment minus this seat's session wiring and identity, plus the base URL."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SEAT_ENV_PREFIXES)}
    if base_url_var:
        env[base_url_var] = proxy_url
    return env


class Run:
    def __init__(self, cli: str, args: argparse.Namespace, upstream: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = SPIKE_ROOT / f"{cli}-{stamp}"
        self.scratch = self.dir / "scratch"
        self.scratch.mkdir(parents=True)
        seed_scratch(self.scratch)
        # The proxy enforces the budget per request too: a single user turn can run many model calls
        # (measured: one compaction-loop turn used 720k tokens), so a between-turn guard is not enough.
        self.proxy = SpikeProxy(upstream, self.dir, cli, cap_tokens=args.budget)
        self.args = args
        self.turns_path = self.dir / "turns.jsonl"
        self.spent = 0

    def spent_in(self, turn: int) -> tuple[int, int]:
        self.proxy.wait_idle()
        rows = [r for r in self.proxy.records if r.get("turn") == turn]
        return sum(total_tokens(r.get("usage") or {}) for r in rows), len(rows)

    def guard(self, turn: int, last_turn_tokens: int) -> bool:
        """True when the next turn may run: spend so far plus the last request's size times the
        average requests per turn stays within the budget."""
        rows = [r for r in self.proxy.records if r.get("usage")]
        turns_seen = len({r.get("turn") for r in rows}) or 1
        last_request = total_tokens(rows[-1]["usage"]) if rows else last_turn_tokens
        projected = self.spent + last_request * max(len(rows) / turns_seen, 1.0)
        if turn > 1 and projected > self.args.budget:
            self.note({"turn": turn, "stopped": "budget", "spent": self.spent, "projected": projected})
            return False
        return True

    def note(self, line: dict[str, Any]) -> None:
        with self.turns_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
        print(json.dumps(line, sort_keys=True), flush=True)


def inherited_claude_base() -> str | None:
    """The base URL a throwaway Claude Code child would use without the spike, in Claude Code's
    settings precedence (local, project, user, then process env). Held in memory only."""
    for path in (REPO / ".claude" / "settings.local.json", REPO / ".claude" / "settings.json",
                 Path.home() / ".claude" / "settings.json"):
        try:
            value = (json.loads(path.read_text()).get("env") or {}).get("ANTHROPIC_BASE_URL")
        except (OSError, ValueError, AttributeError):
            continue
        if value:
            return value
    return os.environ.get("ANTHROPIC_BASE_URL")


def claude_session(args: argparse.Namespace) -> Path:
    upstream = inherited_claude_base()
    if not upstream:
        sys.exit("no inherited ANTHROPIC_BASE_URL; nothing to forward to")
    label = "probe" if args.probe else args.scenario
    run = Run(f"claude-{label}", args, upstream)
    run.proxy.cli = "claude"
    url = run.proxy.start()
    # Recipe agreed with manager-1 and intake: the child runs under this worktree so it inherits
    # the seat credential exactly as configured. A flag-level --settings override, which outranks
    # local settings, points it at the spike proxy and disables every hook so the throwaway cannot
    # report activity for the seat. --strict-mcp-config without --mcp-config loads no MCP servers.
    settings = {"disableAllHooks": True, "env": {"ANTHROPIC_BASE_URL": url}}
    cmd = [
        args.claude_bin, "-p", "--settings", json.dumps(settings), "--strict-mcp-config",
        "--model", args.model or "haiku", "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "--no-session-persistence",
        "--allowedTools", "Read,Write,Edit,Glob,Grep,Bash(python3:*),Bash(ls:*),Bash(wc:*),Bash(grep:*)",
    ]
    if args.claude_tools:
        cmd += ["--tools", args.claude_tools]
    child = subprocess.Popen(cmd, cwd=run.scratch, env=child_env("ANTHROPIC_BASE_URL", url), text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_lines: list[str] = []
    threading_stderr(child, stderr_lines)
    last = 0
    try:
        for turn, prompt in enumerate(prompts_for(args), start=1):
            if not run.guard(turn, last):
                break
            run.proxy.turn = turn
            child.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            child.stdin.flush()
            events: dict[str, int] = {}
            result: dict[str, Any] = {}
            for line in child.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                key = f'{event.get("type")}:{event.get("subtype", "")}'.rstrip(":")
                events[key] = events.get(key, 0) + 1
                if event.get("type") == "result":
                    result = event
                    break
            else:
                run.note({"turn": turn, "stopped": "child exited", "returncode": child.poll()})
                break
            last, requests = run.spent_in(turn)
            run.spent += last
            run.note({
                "turn": turn, "requests": requests, "turn_tokens": last, "cumulative_tokens": run.spent,
                "cli_num_turns": result.get("num_turns"), "is_error": result.get("is_error"),
                "result_subtype": result.get("subtype"), "cli_events": events,
                "compaction_events": {k: v for k, v in events.items() if "compact" in k},
            })
    finally:
        if child.stdin:
            child.stdin.close()
        try:
            child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
        run.proxy.stop()
        write_stderr_flags(run, stderr_lines)
    return run.dir


def inherited_codex_base() -> str:
    """The base URL a throwaway Codex process would use: env, then config, then the default."""
    if os.environ.get("OPENAI_BASE_URL"):
        return os.environ["OPENAI_BASE_URL"]
    config = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        value = tomllib.loads(config.read_text()).get("openai_base_url")
    except (OSError, tomllib.TOMLDecodeError):
        value = None
    return value or CODEX_CHATGPT_BASE


def codex_session(args: argparse.Namespace) -> Path:
    run = Run("codex-probe" if args.probe else "codex", args, inherited_codex_base())
    run.proxy.cli = "codex"
    url = run.proxy.start()
    # Measured: under ChatGPT sign-in, codex 0.154 ignores the OPENAI_BASE_URL env var. The
    # documented per-process override is the openai_base_url config key, passed with -c.
    env = child_env(None, url)
    model = args.model or "gpt-6-astra"
    # --ignore-user-config skips config.toml (auth still comes from CODEX_HOME) and --disable hooks
    # keeps the owner's user hooks from firing for a throwaway run. Neither edits any file.
    common = ["--json", "--skip-git-repo-check", "--ignore-user-config", "--disable", "hooks", "-m", model,
              "-c", f'openai_base_url="{url}"',
              "-c", 'model_reasoning_effort="low"', "-c", 'sandbox_mode="workspace-write"']
    if args.probe:
        common.append("--ephemeral")
    thread_id = None
    stderr_lines: list[str] = []
    last = 0
    try:
        for turn, prompt in enumerate(prompts_for(args), start=1):
            if not run.guard(turn, last):
                break
            run.proxy.turn = turn
            if thread_id is None:
                cmd = [args.codex_bin, "exec", *common, prompt]
            else:
                cmd = [args.codex_bin, "exec", "resume", thread_id, *common, prompt]
            try:
                done = subprocess.run(cmd, cwd=run.scratch, env=env, text=True, capture_output=True,
                                      timeout=TURN_TIMEOUT_S, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                run.note({"turn": turn, "stopped": "timeout"})
                break
            stderr_lines += done.stderr.splitlines()
            events: dict[str, int] = {}
            errors: list[str] = []
            cli_usage: dict[str, Any] = {}
            for line in done.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = str(event.get("type"))
                item_type = (event.get("item") or {}).get("type")
                key = f"{kind}:{item_type}" if item_type else kind
                events[key] = events.get(key, 0) + 1
                if kind in ("error", "turn.failed"):
                    # CLI-generated error text (not model content), kept short for diagnosis.
                    message = event.get("message") or (event.get("error") or {}).get("message") or ""
                    errors.append(str(message)[:300])
                if kind == "thread.started":
                    thread_id = thread_id or event.get("thread_id")
                elif kind == "turn.completed":
                    cli_usage = {k: v for k, v in (event.get("usage") or {}).items() if isinstance(v, int)}
            last, requests = run.spent_in(turn)
            run.spent += last
            run.note({
                "turn": turn, "requests": requests, "turn_tokens": last, "cumulative_tokens": run.spent,
                "returncode": done.returncode, "cli_events": events, "cli_reported_usage": cli_usage,
                "cli_errors": errors,
                "compaction_events": {k: v for k, v in events.items() if "compact" in k},
            })
            if done.returncode != 0 or thread_id is None:
                run.note({"turn": turn, "stopped": "codex error", "returncode": done.returncode})
                break
    finally:
        run.proxy.stop()
        write_stderr_flags(run, stderr_lines)
    return run.dir


def threading_stderr(child: subprocess.Popen, sink: list[str]) -> None:
    def pump() -> None:
        for line in child.stderr:
            sink.append(line.rstrip("\n"))

    threading.Thread(target=pump, daemon=True).start()


def write_stderr_flags(run: Run, lines: list[str]) -> None:
    """Record only whether stderr mentioned compaction or context limits, with counts."""
    words = ("compact", "context window", "context limit", "too long", "auto-compact")
    flags = {w: sum(w in line.lower() for line in lines) for w in words}
    run.note({"stderr_lines": len(lines), "stderr_flags": {k: v for k, v in flags.items() if v}})


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["seed"]:
        # Used by a seat driven over rig send to create the synthetic project in its own scratch dir.
        if len(argv) != 2:
            sys.exit("usage: run_session.py seed DIR")
        target = Path(argv[1])
        target.mkdir(parents=True, exist_ok=True)
        seed_scratch(target)
        print(f"seeded {target}")
        return 0
    parser = argparse.ArgumentParser(description="Synthetic session through the M0 measuring proxy.")
    parser.add_argument("cli", choices=("claude", "codex"))
    parser.add_argument("--turns", type=int, default=len(PROMPTS))
    parser.add_argument("--scenario", choices=("workload", "compact"), default="workload")
    parser.add_argument("--budget", type=int, default=450_000, help="token cap for this experiment, all categories")
    parser.add_argument("--model", default=None)
    parser.add_argument("--probe", action="store_true",
                        help="one tiny no-tool turn to verify routing and auth before a real run")
    parser.add_argument("--claude-tools", default=None,
                        help='Claude Code --tools list to trim tool definitions, e.g. "Bash,Read,Edit"')
    parser.add_argument("--claude-bin", default=shutil.which("claude") or "claude")
    parser.add_argument("--codex-bin", default=shutil.which("codex") or "codex")
    args = parser.parse_args(argv)
    out = claude_session(args) if args.cli == "claude" else codex_session(args)
    print(f"run directory: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
