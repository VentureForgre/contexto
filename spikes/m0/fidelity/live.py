"""Plain-CLI fidelity baseline: a throwaway headless Claude Code session on a synthetic script.

Usage, from spikes/m0 (dry run is the default and makes no model call):
    python3 -m fidelity.live --out ../../.contexto/spikes/l2b
    python3 -m fidelity.live --out ../../.contexto/spikes/l2b --live --settings PATH [--model ID]

Live mode runs ``claude -p --bare`` in an empty run directory, resumed across turns by session id.
``--settings`` names an existing settings file whose apiKeyHelper the CLI runs itself. Routing
caveat: if that file has an ``env`` block, the CLI applies it over the inherited environment, so
the file's ``ANTHROPIC_BASE_URL`` (and any other variable it sets) wins. The runner records which
source decides the base URL and whether it equals the inherited value, without recording either
value. ``--bare`` keeps the settings file's hooks from running. Nothing here reads, copies, or logs
a credential. The child environment drops OpenRig and parent Claude Code session variables.

After the build turns, the session is forked twice: one fork answers the probes directly
(plain CLI, full history), the other runs ``/compact`` first and then answers the same probes.

Recorded per call: phase, token usage fields, duration, and error flag. Recorded per probe: correct
or not, and whether a wrong answer equals a superseded value. Prompts, responses, and answers are
not written anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fidelity.scoring import normalize, parse_answers, score
from fidelity.session import Planted, Probe, _code, _filler_text

DEFAULT_SEED = 20261015
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_CAP = 500_000  # tokens per experiment, from the M0 brief
_DROP_PREFIXES = ("OPENRIG_", "CLAUDE_CODE_", "CLAUDE_EFFORT", "CLAUDE_PID", "CLAUDECODE")
_REPLY = "Reply with exactly: OK"


@dataclass(frozen=True)
class LiveScript:
    seed: int
    prompts: tuple[str, ...]
    probes: tuple[Probe, ...]
    planted: dict[str, Planted]

    def probe_prompt(self) -> str:
        lines = [
            "Answer each question from this conversation only. Use one line per question, in the form",
            "q_id: answer",
            "with the value only, and no other text.",
            "",
        ]
        lines += [f"{p.id}: {p.question}" for p in self.probes]
        return "\n".join(lines)


def script(seed: int = DEFAULT_SEED, filler_lines: int = 160, dense: int = 0) -> LiveScript:
    """Twelve build turns: planted items spread among pasted logs, with two supersession chains.

    ``dense`` adds that many unflagged ``cfg <key>=<value>`` lines to every pasted log, each one
    a probe. They are not called out in the prompt, which is what a summary is likely to drop.

    Each filler turn pastes about ``filler_lines`` synthetic log lines (roughly 25 tokens each),
    which stands in for tool output; a headless session cannot be fed fake tool results.
    """
    rng = random.Random(seed)
    planted: dict[str, Planted] = {}
    prompts: list[str] = []

    def fact(key: str, subject: str, hint: str, category: str = "pinned_fact") -> str:
        value = _code(rng)
        planted[key] = Planted(key, category, subject, value, len(prompts), hint, ((len(prompts), value),))
        return f"the {subject} is {value}"

    def decide(key: str, subject: str, hint: str) -> str:
        value = _code(rng)
        old = planted.get(key)
        history = (old.history if old else ()) + ((len(prompts), value),)
        planted[key] = Planted(key, "decision", subject, value, len(prompts), hint, history)
        return f"the {subject} is {'now ' if old else ''}{value}"

    def oblige(oid: str, text: str, state: str) -> str:
        old = planted.get(f"obligation:{oid}")
        history = (old.history if old else ()) + ((len(prompts), state),)
        planted[f"obligation:{oid}"] = Planted(
            f"obligation:{oid}", "obligation", oid, state, len(prompts), oid, history, text
        )
        return f"Action item {oid}: {text}. Keep it open until it is done." if state == "open" else (
            f"Action item {oid} is done."
        )

    def logs(label: str, detail: str | None = None) -> str:
        lines = _filler_text(rng, filler_lines, label).split("\n")
        if detail:
            lines.insert(rng.randrange(10, len(lines) - 10), f"# note: {detail}")
        for k in range(dense):
            key = f"{label}.shard_{k:02d}.map_version"
            value = _code(rng)
            planted[f"detail:{key}"] = Planted(f"detail:{key}", "detail", key, value, len(prompts), label, ((len(prompts), value),))
            lines.insert(rng.randrange(10, len(lines) - 10), f"cfg {key}={value}")
        return "\n".join(lines)

    def turn(*parts: str) -> None:
        prompts.append("\n".join([*parts, _REPLY]))

    turn(
        f"Please remember: {fact('fact:staging', 'staging database port', 'staging')}.",
        f"Please remember: {fact('fact:release', 'release codename', 'release')}.",
        f"Decision: {decide('decision:logging', 'chosen logging library', 'logging')}.",
        oblige("ob00", "rotate the staging API key", "open"),
    )
    turn("Here is the billing config for reference:", logs("billing", fact("detail:billing", "billing shard map version", "billing", "detail")))
    turn(
        f"Please remember: {fact('fact:rotation', 'on-call rotation owner', 'rotation')}.",
        f"Decision: {decide('decision:queue', 'chosen queue backend', 'queue')}.",
        oblige("ob01", "write the rollback runbook", "open"),
    )
    turn("Here is a worker log excerpt:", logs("worker"))
    turn("Here is a scheduler log excerpt:", logs("scheduler"))
    turn(f"Change of plan: {decide('decision:logging', 'chosen logging library', 'logging')}.", oblige("ob00", "", "closed"))
    turn("Here is the search config for reference:", logs("search", fact("detail:search", "search shard map version", "search", "detail")))
    turn(
        f"Please remember: {fact('fact:region', 'primary deploy region', 'region')}.",
        oblige("ob02", "pin the base image digest", "open"),
    )
    turn("Here is a gateway log excerpt:", logs("gateway"))
    turn(f"Change of plan: {decide('decision:queue', 'chosen queue backend', 'queue')}.", oblige("ob02", "", "closed"), oblige("ob03", "add a canary for the gateway", "open"))
    turn("Here is an ingest log excerpt:", logs("ingest"))
    turn(
        f"Please remember: {fact('fact:flag', 'feature flag prefix', 'flag')}.",
        f"Change of plan: {decide('decision:logging', 'chosen logging library', 'logging')}.",
    )

    probes: list[Probe] = []
    for key in sorted(planted):
        p = planted[key]
        if p.category == "obligation":
            probes.append(Probe(f"q_{key.replace(':', '_')}", "obligation_status", key,
                                f"Is action item {p.subject} open or closed? Answer open or closed.", p.value))
        elif p.category == "decision":
            probes.append(Probe(f"q_{key.replace(':', '_')}", "current_decision", key,
                                f"What is the current {p.subject}? Answer with the value only.", p.value))
        elif p.category == "detail" and "." in p.subject:
            probes.append(Probe(f"q_{key.replace(':', '_').replace('.', '_')}", "detail_unflagged", key,
                                f"What value does {p.subject} have in the pasted logs? Answer with the value only.", p.value))
        elif p.category == "detail":
            probes.append(Probe(f"q_{key.replace(':', '_')}", "detail", key,
                                f"What is the {p.subject} in the {p.hint} config? Answer with the value only.", p.value))
        else:
            category = "recent_fact" if p.turn == len(prompts) - 1 else "early_fact"
            probes.append(Probe(f"q_{key.replace(':', '_')}", category, key,
                                f"What is the {p.subject}? Answer with the value only.", p.value))
    return LiveScript(seed, tuple(prompts), tuple(probes), planted)


def _numbers(obj: Any) -> Any:
    """Keep only numeric leaves (and the dict structure around them)."""
    if isinstance(obj, dict):
        kept = {k: _numbers(v) for k, v in obj.items()}
        return {k: v for k, v in kept.items() if v not in (None, {})}
    return obj if isinstance(obj, (int, float)) and not isinstance(obj, bool) else None


def _model_usage_total(model_usage: dict[str, Any]) -> int:
    keys = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
    return sum(int(v.get(k) or 0) for v in model_usage.values() if isinstance(v, dict) for k in keys)


def base_url_source(settings: Path) -> dict[str, Any]:
    """Which source sets the child's base URL, and whether it matches the inherited value. No values."""
    try:
        env = json.loads(settings.read_text(encoding="utf-8")).get("env") or {}
    except (OSError, json.JSONDecodeError):
        return {"source": "unknown"}
    if "ANTHROPIC_BASE_URL" in env:
        return {"source": "settings_env", "equals_inherited": env["ANTHROPIC_BASE_URL"] == os.environ.get("ANTHROPIC_BASE_URL"),
                "settings_env_keys": sorted(env)}
    return {"source": "inherited_env", "settings_env_keys": sorted(env)}


def corrected_spend(calls: list[dict[str, Any]]) -> int:
    """Re-total a recorded run, replacing any zero-usage call by its modelUsage growth over the last build call."""
    total = 0
    parent: dict[str, Any] | None = None
    for c in calls:
        if c["phase"].startswith("build_"):
            parent = c.get("model_usage")
        usage = c.get("usage") or {}
        if any(usage.get(k) for k in usage):
            total += usage_total(usage)
        elif c.get("model_usage") is not None:
            base = _model_usage_total(parent) if parent and c["phase"] == "compact" else 0
            total += max(0, _model_usage_total(c["model_usage"]) - base)
    return total


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(_DROP_PREFIXES)}


def usage_total(usage: dict[str, Any]) -> int:
    """Every token the call was billed for, counted conservatively against the cap."""
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return sum(int(usage.get(k) or 0) for k in keys)


class CapExceeded(Exception):
    pass


class Runner:
    def __init__(self, run_dir: Path, settings: Path, model: str, cap: int, spent: int = 0) -> None:
        self.run_dir = run_dir
        self.settings = settings
        self.model = model
        self.cap = cap
        self.spent = spent  # tokens already spent by this experiment, for continuation runs
        self.last_context = 0
        self.calls: list[dict[str, Any]] = []
        # Session id -> cumulative modelUsage as last reported. The CLI reports modelUsage per
        # session, and a fork inherits its parent's totals, so a call's own cost is a difference.
        self.session_usage: dict[str, dict[str, Any]] = {}

    def call(self, phase: str, prompt: str, session: str, *, new: bool = False, fork: bool = False) -> tuple[str, str]:
        """Run one headless turn. Returns (session id, result text). The text is never persisted."""
        projected = self.spent + self.last_context + len(prompt) // 3 + 4_000
        if projected > self.cap:
            raise CapExceeded(f"next call projected to reach {projected} tokens, cap {self.cap}")
        cmd = [
            "claude", "-p", "--bare", "--settings", str(self.settings), "--model", self.model,
            "--tools", "", "--output-format", "json",
        ]
        cmd += ["--session-id", session] if new else ["--resume", session]
        if fork:
            cmd.append("--fork-session")
        started = time.monotonic()
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, cwd=self.run_dir, env=child_env(), timeout=600
        )
        elapsed = round(time.monotonic() - started, 2)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # Do not echo stdout or stderr: they could carry prompt text. Sizes only.
            self.calls.append({"phase": phase, "exit": proc.returncode, "parse_error": True,
                               "stdout_chars": len(proc.stdout), "stderr_chars": len(proc.stderr), "seconds": elapsed})
            raise RuntimeError(f"{phase}: claude exited {proc.returncode} without JSON output") from None
        usage = out.get("usage") or {}
        total = usage_total(usage)
        model_usage = _numbers(out.get("modelUsage") or {})
        source = "usage"
        if total == 0:
            # /compact reports zero usage although it calls the model. Use the growth of the
            # session's cumulative modelUsage over its parent's, or the whole cumulative figure
            # (an overcount) when the parent's is unknown.
            parent = self.session_usage.get(session)
            total = max(0, _model_usage_total(model_usage) - (_model_usage_total(parent) if parent else 0))
            source = "modelUsage_delta" if parent else "modelUsage_cumulative"
        new_session = str(out.get("session_id") or session)
        self.session_usage[new_session] = model_usage
        self.spent += total
        self.last_context = total
        self.calls.append({
            "phase": phase,
            "exit": proc.returncode,
            "is_error": bool(out.get("is_error")),
            "seconds": elapsed,
            "prompt_chars": len(prompt),
            "result_chars": len(out.get("result") or ""),
            "usage": {k: usage.get(k) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")},
            "total_tokens": total,
            "cumulative_tokens": self.spent,
            "usage_source": source,
            "model_usage": model_usage,
        })
        if out.get("is_error"):
            raise RuntimeError(f"{phase}: claude reported an error (subtype {out.get('subtype')!r})")
        if self.spent > self.cap:
            # The pre-call projection can undershoot; stop as soon as the recorded spend is over.
            raise CapExceeded(f"{phase}: spend {self.spent} is over the cap {self.cap}")
        return new_session, str(out.get("result") or "")


def score_answers(ls: LiveScript, text: str) -> dict[str, Any]:
    answers = parse_answers(text)
    older = {key: [v for _, v in p.history[:-1]] for key, p in ls.planted.items()}
    result = score(ls.probes, answers, older)
    return {
        "correct": result.correct,
        "total": result.total,
        "by_category": {k: list(v) for k, v in result.by_category.items()},
        "wrong": list(result.wrong),
        "stale": list(result.stale),
        "unanswered": [p.id for p in ls.probes if not normalize(answers.get(p.id))],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plain-CLI fidelity baseline (dry run unless --live).")
    parser.add_argument("--out", type=Path, required=True, help="output root, under the gitignored .contexto/spikes/l2b")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--live", action="store_true", help="call the model through a throwaway claude -p process")
    parser.add_argument("--settings", type=Path, help="existing settings file whose apiKeyHelper the CLI runs")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP, help="token cap for the whole run")
    parser.add_argument("--skip-compact", action="store_true")
    parser.add_argument("--dense", type=int, default=0, help="unflagged cfg lines per pasted log")
    parser.add_argument("--compact-only", type=Path, metavar="RUN_DIR",
                        help="resume a finished build from RUN_DIR/summary.json: run /compact, then the probes")
    parser.add_argument("--session", help="build session id, for --compact-only runs recorded before ids were kept")
    args = parser.parse_args(argv)

    ls = script(args.seed, dense=args.dense)
    est = [len(p) // 4 for p in ls.prompts]
    summary: dict[str, Any] = {
        "seed": args.seed,
        "build_turns": len(ls.prompts),
        "probes": len(ls.probes),
        "probe_categories": sorted({p.category for p in ls.probes}),
        "prompt_tokens_est": est,
        "history_tokens_est": sum(est),
        "mode": "live" if args.live else "dry-run",
        "dense": args.dense,
    }
    if not args.live:
        print(json.dumps(summary, indent=2))
        return 0
    if args.settings is None or not args.settings.is_file():
        parser.error("--live needs --settings pointing at an existing settings file")

    if args.compact_only:
        return compact_only(args, ls)
    run_dir = args.out.resolve() / f"baseline-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    run_dir.mkdir(parents=True, exist_ok=False)
    runner = Runner(run_dir, args.settings.resolve(), args.model, args.cap)
    summary.update({"model": args.model, "cap": args.cap, "run_dir_name": run_dir.name,
                    "base_url": base_url_source(args.settings)})
    status = 0
    try:
        session = str(uuid.uuid4())
        for n, prompt in enumerate(ls.prompts):
            session, _ = runner.call(f"build_{n:02d}", prompt, session, new=(n == 0))
        summary["build_tokens"] = runner.spent
        summary["build_session_id"] = session

        _, text = runner.call("probe_plain", ls.probe_prompt(), session, fork=True)
        summary["plain"] = score_answers(ls, text)
        summary["after_plain_tokens"] = runner.spent

        if not args.skip_compact:
            summary["compacted"] = _compact_and_probe(runner, ls, session)
    except (CapExceeded, RuntimeError, subprocess.TimeoutExpired) as exc:
        summary["stopped"] = str(exc)
        status = 1
    summary["total_tokens"] = runner.spent
    summary["calls"] = runner.calls
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "calls"}, indent=2, sort_keys=True))
    return status


def _compact_and_probe(runner: Runner, ls: LiveScript, session: str) -> dict[str, Any]:
    compacted, _ = runner.call("compact", "/compact", session, fork=True)
    _, text = runner.call("probe_compacted", ls.probe_prompt(), compacted)
    return score_answers(ls, text)


def compact_only(args: argparse.Namespace, ls: LiveScript) -> int:
    """Finish a run whose build and plain probes completed but whose compaction step did not."""
    path = args.compact_only / "summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    if summary.get("seed") != args.seed or summary.get("dense", 0) != args.dense:
        raise SystemExit("seed or --dense differs from the recorded run")
    session = summary.get("build_session_id") or args.session
    if not session:
        raise SystemExit("no build session id recorded; pass --session")
    build_calls = [c for c in summary["calls"] if c["phase"].startswith("build_")]
    prior = corrected_spend(summary["calls"] + summary.get("continuation", {}).get("calls", []))
    runner = Runner(args.compact_only.resolve(), args.settings.resolve(), args.model, args.cap, spent=prior)
    runner.session_usage[session] = build_calls[-1]["model_usage"]
    continuation: dict[str, Any] = {"build_session_id": session}
    status = 0
    try:
        continuation["compacted"] = _compact_and_probe(runner, ls, session)
    except (CapExceeded, RuntimeError, subprocess.TimeoutExpired) as exc:
        continuation["stopped"] = str(exc)
        status = 1
    continuation["prior_tokens"] = prior
    continuation["tokens"] = runner.spent - prior
    continuation["calls"] = runner.calls
    summary["continuation"] = continuation
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in continuation.items() if k != "calls"}, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    sys.exit(main())
