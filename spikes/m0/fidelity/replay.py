"""L2e: run the strip-thinking arm, a direct-API replay of the L2d plan's replay turns.

Usage, from the repository root:
    python3 spikes/m0/fidelity/replay.py --settings .claude/settings.local.json \
        --out .contexto/spikes/l2e [--cap 450000] [--dry-run]

Sequence (one request per turn; probe requests are forks that never enter the shared history):
    RS, RG               shared history (group 0 rules, opening, growth)
    RP0 preserved, RP0 stripped
    RF                   shared history (filler standing in for the seat head, then seat content)
    RP1 preserved, RP1 stripped

"Stripped" removes every thinking block from the shared history before the probe, as the policy
does at an epoch. Synthetic content only. The credential comes from the settings file's
apiKeyHelper at runtime and is held in memory for request headers only. Records hold statuses,
usage numbers, block types, and character counts; the synthetic answers are written under the
output directory for scoring and never committed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fidelity import l2d  # noqa: E402

MODEL = "claude-haiku-5-5"
EFFORT = "medium"  # Haiku 5.5's default effort, set explicitly so the run is reproducible
MAX_TOKENS = 16_000
DEFAULT_CAP = 450_000
SYSTEM = "You are a careful assistant in a long working session. Follow the user's instructions exactly."


class CapExceeded(Exception):
    pass


class RequestFailed(Exception):
    pass


Post = Callable[..., tuple[int, dict[str, Any]]]  # (body, extra headers=None) -> (status, payload)


def http_post(settings: Path) -> Post:
    """Build a poster bound to the worktree's base URL and credential helper. Nothing is printed."""
    data = json.loads(settings.read_text(encoding="utf-8"))
    helper = data.get("apiKeyHelper")
    if not helper:
        raise SystemExit("settings file has no apiKeyHelper; stopping rather than looking for a key elsewhere")
    base = ((data.get("env") or {}).get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    if not base:
        raise SystemExit("no base URL in the settings env block or the environment")
    proc = subprocess.run(helper, shell=True, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit(f"apiKeyHelper failed (exit {proc.returncode}); output not shown")
    key = proc.stdout.strip()

    def post(body: dict[str, Any], headers: dict[str, str] | None = None, path: str = "/v1/messages") -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"), method="POST")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        req.add_header("content-type", "application/json")
        req.add_header("user-agent", "contexto-replay/0.1")
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("authorization", f"Bearer {key}")
        req.add_header("x-api-key", key)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace").replace(key, "[redacted]")
            try:
                return err.code, json.loads(raw)
            except json.JSONDecodeError:
                return err.code, {"error": {"type": "unparsed", "message": raw[:80]}}

    return post


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def strip_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = copy.deepcopy(messages)
    for m in out:
        if m["role"] == "assistant":
            m["content"] = [b for b in m["content"] if b.get("type") not in ("thinking", "redacted_thinking")]
    return out


def usage_total(usage: dict[str, Any]) -> int:
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return sum(int(usage.get(k) or 0) for k in keys)


class Runner:
    def __init__(self, post: Post, cap: int, estimates: dict[str, int], spent: int = 0, margin: float = 1.2,
                 max_tokens: int = MAX_TOKENS, effort: str = EFFORT) -> None:
        self.post = post
        self.max_tokens = max_tokens
        self.effort = effort
        self.cap = cap
        self.estimates = estimates  # turn id -> estimated input context, from l2d.replay_requests
        self.margin = margin  # applied to estimates; 1.0 when the estimate is a measured figure
        self.spent = spent
        self.records: list[dict[str, Any]] = []

    def call(self, label: str, turn_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        # Reserve the full output allowance: a fork can think until max_tokens (L2f pilot 2 did).
        projected = self.spent + int(self.margin * self.estimates.get(turn_id, 0)) + self.max_tokens
        if projected > self.cap:
            raise CapExceeded(f"{label}: projected {projected} would pass the cap {self.cap}")
        body = {
            "model": MODEL, "max_tokens": self.max_tokens, "system": SYSTEM, "messages": messages,
            "thinking": {"type": "adaptive"}, "output_config": {"effort": self.effort},
        }
        started = time.monotonic()
        status, payload = self.post(body)
        usage = payload.get("usage") or {}
        record = {
            "label": label,
            "status": status,
            "seconds": round(time.monotonic() - started, 2),
            "thinking_blocks_sent": sum(
                1 for m in messages if m["role"] == "assistant" for b in m["content"] if b.get("type") == "thinking"
            ),
            "usage": {k: v for k, v in usage.items() if isinstance(v, int)},
            "total_tokens": usage_total(usage),
            "blocks": [b.get("type") for b in payload.get("content", [])],
            "stop_reason": payload.get("stop_reason"),
            "input_transformations": [
                {"type": t.get("type"), "reason": t.get("reason")} for t in payload.get("input_transformations") or []
            ],
            "text_chars": sum(len(b.get("text", "")) for b in payload.get("content", []) if b.get("type") == "text"),
        }
        if status != 200:
            err = payload.get("error") or {}
            record["error_type"] = err.get("type")
            record["error_message"] = str(err.get("message", ""))[:300]
        self.spent += record["total_tokens"]
        record["cumulative_tokens"] = self.spent
        self.records.append(record)
        if status != 200:
            raise RequestFailed(f"{label}: HTTP {status} {record.get('error_type')}")
        if self.spent > self.cap:
            raise CapExceeded(f"{label}: spend {self.spent} is over the cap {self.cap}")
        return payload


def text_of(payload: dict[str, Any]) -> str:
    return "\n".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")


def run(plan: l2d.Plan, runner: Runner, answers_dir: Path) -> None:
    turns = {t.id: t for t in plan.replay_turns}
    history: list[dict[str, Any]] = []

    def build(turn_id: str) -> None:
        history.append(user(turns[turn_id].text))
        payload = runner.call(turn_id, turn_id, list(history))
        history.append({"role": "assistant", "content": payload["content"]})

    def probe(turn_id: str) -> None:
        for arm, msgs in (("preserved", history), ("stripped", strip_thinking(history))):
            payload = runner.call(f"{turn_id}_{arm}", turn_id, [*msgs, user(turns[turn_id].text)])
            (answers_dir / f"replay_{turn_id}_{arm}.txt").write_text(text_of(payload) + "\n", encoding="utf-8")

    build("RS")
    build("RG")
    probe("RP0")
    build("RF")
    probe("RP1")


EXPECTED_REPLIES = {"RS": "OK", "RG": "DONE", "RF": "OK"}


def continue_stripped_p1(plan: l2d.Plan, run_dir: Path, post: Post, cap: int) -> dict[str, Any]:
    """Finish a run that stopped before RP1_stripped, without stored responses.

    The stripped fork removes every thinking block, so its history is the user turns plus each
    assistant reply's text. The run records only reply sizes; this rebuilds the replies from the
    instructed exact replies and refuses unless every recorded size and block layout matches.
    The cap projection uses RP1_preserved's measured cost (same content plus thinking blocks).
    """
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    records = {r["label"]: r for r in summary["records"]}
    if "RP1_stripped" in records or "RP1_preserved" not in records:
        raise SystemExit("run does not stop right before RP1_stripped")
    for turn_id, reply in EXPECTED_REPLIES.items():
        r = records[turn_id]
        if r["text_chars"] != len(reply) or r["blocks"] != ["thinking", "text"]:
            raise SystemExit(f"{turn_id} reply does not match the instructed exact reply; not reconstructing")
    turns = {t.id: t for t in plan.replay_turns}
    messages: list[dict[str, Any]] = []
    for turn_id in ("RS", "RG", "RF"):
        messages += [user(turns[turn_id].text), {"role": "assistant", "content": [{"type": "text", "text": EXPECTED_REPLIES[turn_id]}]}]
    measured = records["RP1_preserved"]["total_tokens"] - records["RP1_preserved"]["usage"].get("output_tokens", 0)
    runner = Runner(post, cap, {"RP1": measured}, spent=summary["total_tokens"], margin=1.0)
    payload = runner.call("RP1_stripped", "RP1", [*messages, user(turns["RP1"].text)])
    (run_dir / "answers" / "replay_RP1_stripped.txt").write_text(text_of(payload) + "\n", encoding="utf-8")
    record = runner.records[-1]
    record["reconstructed_history"] = True
    summary["records"].append(record)
    summary["total_tokens"] = runner.spent
    summary["continued"] = "RP1_stripped from reconstructed stripped history"
    summary.pop("stopped", None)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the L2e strip-thinking replay arm.")
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP)
    parser.add_argument("--dry-run", action="store_true", help="write the plan and estimates only; no API calls")
    parser.add_argument("--continue-stripped-p1", type=Path, metavar="RUN_DIR",
                        help="finish a run that stopped right before RP1_stripped")
    args = parser.parse_args(argv)

    plan = l2d.build()
    if args.continue_stripped_p1:
        run_dir = args.continue_stripped_p1
        try:
            summary = continue_stripped_p1(plan, run_dir, http_post(args.settings), args.cap)
        except (CapExceeded, RequestFailed) as exc:
            print(f"stopped: {exc}")
            return 1
        plan_dir = run_dir / "plan"
        truth = [json.loads(line) for line in (plan_dir / "truth.jsonl").read_text(encoding="utf-8").splitlines()]
        meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        results = l2d.score_dir(run_dir / "answers", truth, meta)
        summary["scores"] = {k: {kk: v[kk] for kk in ("correct", "total", "interval", "instruction", "tables")}
                             for k, v in results.items() if "correct" in v}
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(l2d.report(results))
        print(f"run_dir={run_dir} total_tokens={summary['total_tokens']}")
        return 0
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = args.out / f"replay-{stamp}"
    plan_dir = run_dir / "plan"
    answers_dir = run_dir / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)
    l2d.write_outputs(plan, plan_dir)
    estimates: dict[str, int] = {}
    for r in l2d.replay_requests(plan):
        estimates[r.turn] = max(estimates.get(r.turn, 0), r.context)
    summary: dict[str, Any] = {"model": MODEL, "effort": EFFORT, "cap": args.cap, "seed": plan.seed,
                               "estimates": estimates, "mode": "dry-run" if args.dry_run else "live"}
    status = 0
    if not args.dry_run:
        runner = Runner(http_post(args.settings), args.cap, estimates)
        try:
            run(plan, runner, answers_dir)
        except (CapExceeded, RequestFailed) as exc:
            summary["stopped"] = str(exc)
            status = 1
        summary["total_tokens"] = runner.spent
        summary["records"] = runner.records
        truth = [json.loads(line) for line in (plan_dir / "truth.jsonl").read_text(encoding="utf-8").splitlines()]
        meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        results = l2d.score_dir(answers_dir, truth, meta)
        summary["scores"] = {k: {kk: v[kk] for kk in ("correct", "total", "interval", "instruction", "tables")}
                             for k, v in results.items() if "correct" in v}
        print(l2d.report(results))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"run_dir={run_dir} total_tokens={summary.get('total_tokens', 0)} stopped={summary.get('stopped')}")
    return status


if __name__ == "__main__":
    sys.exit(main())
