"""The live benchmark run: every unit of every arm, one after another, into one output directory.

A unit is one arm on one seed's script (and, for the first seed, one repeat). For each seed and
repeat the arms run in an order rotated by one each time (``contexto.bench.plan.units``). Each unit
gets its own meter (``contexto.bench.meter``) and its own working directory, which is removed when
the unit ends unless ``keep_work`` is set; the working directories hold Claude Code's and Contexto's
session files and are never inside the output directory.

What is written, and when:

- ``scripts/seed-<n>.json`` for every seed, before the first unit;
- ``requests.jsonl``: one record as each billed request's response ends;
- ``probes.jsonl``: the lines of a unit, together, only when the unit is ``complete``;
- ``manifest.json``: rewritten atomically before every unit (the unit as ``running``) and after it.

One run at a time per output directory: the run holds an exclusive lock (``fcntl.flock``) on
``<out>/.lock`` from before it reads the manifest until it returns, and a second run into the same
directory is refused at once. The lock file stays, empty; the lock goes with the process.

Units are independent and a run can be resumed: the same command with the same ``--out`` keeps the
units the manifest records ``complete`` and runs the rest again. Attempts are numbered before a unit
starts: the next attempt of a unit is one more than the largest attempt seen for it in the manifest
or in ``requests.jsonl``, and it is written to the manifest (status ``running``) before the unit's
first request, so every request record, even of an attempt cut short by SIGKILL, carries an attempt
number no other attempt shares. On resume a ``running`` unit is recorded as ``stopped``
(``interrupted``). Each unit's ``attempts`` lists every attempt with its status, requests and tokens;
all of them stay in ``requests.jsonl``, because all of them were billed. A different model, bound,
protocol, turn size or first seed for an existing ``--out`` is refused.

Runaway caps. Each unit's meter refuses a request past ``turn_request_cap`` billed requests in one
script turn, or past the unit's token cap (``unit_token_cap``, by default twice the unit's planned
worst case from ``contexto.bench.plan``); the unit stops as ``runaway`` and the run goes on.

The budget (``--budget-tokens``) covers the whole output directory: what earlier attempts recorded in
``requests.jsonl`` counts as spent. A unit that stops writes no ``probes.jsonl`` lines; the run goes
on with the next unit unless the stop was the budget or a refused credential. Ctrl+C (or SIGTERM)
stops the child processes, records the unit as ``stopped`` and exits 130.

Fields this module adds to the shared schema. ``manifest.json``: ``label`` (set for a run against
a local upstream), ``budget_tokens``, ``spent_tokens``, ``credential_source``, ``upstream_source``,
``upstream_note``, ``cli_environment``, ``arm_differences``, ``metering``, ``compaction_counting``,
``request_classes``, ``full_arm``, ``runaway_caps``, and per unit ``stop``, ``attempt``, ``attempts``,
``turns_completed``, ``requests``, ``tokens``, ``max_input``, ``over_bound``, ``compaction_requests``,
``compaction_tokens``, ``compaction_max_input``, ``usage_unreadable``, ``token_cap``,
``toolloop_requests``, ``toolloop_max_input``, ``toolloop_tool_calls``. A unit's ``status`` may also
be ``running`` while it runs. ``requests.jsonl``: ``attempt``, and ``usage_unreadable`` with
``reserved_tokens`` on a response whose usage could not be read (``contexto.bench.meter``).
``probes.jsonl``: ``given_truncated`` (the answer was longer than 200 characters; it was scored whole).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from contexto.bench import arms as bench_arms
from contexto.bench import meter as bench_meter
from contexto.bench import plan as bench_plan
from contexto.bench import script as bench_script
from contexto.bench.model import Script

FORMAT = 1
EXIT_STOPPED = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 127
EXIT_INTERRUPTED = 130
MAX_ANSWER_CHARS = 200
TURN_REQUEST_CAP = 60  # see runaway_caps in the manifest
UNIT_TOKEN_CAP_FACTOR = 2
LOCK_NAME = ".lock"
STUB_LABEL = ("Stub run: the upstream was a server on this machine (127.0.0.1), not a model API, so the answers, "
              "tokens and costs in it were made up by that stub.")
PINNED = ("model", "bound", "protocol", "turn_kbytes", "seed")  # what an existing output directory fixes
PINNED_NAMES = {"model": "--model", "bound": "--bound", "protocol": "--protocol", "turn_kbytes": "--turn-kbytes",
                "seed": "--seed"}

ARM_DIFFERENCES = (
    "contexto and plain run the same Claude Code executable with the same arguments (-p, stream-json in and out, "
    "--verbose, --model), a fresh isolated configuration directory with the same settings file, the same "
    "environment, the same turn texts and the same files.",
    "contexto only: Claude Code is started by 'contexto run', which adds a --settings file (the base URL of "
    "Contexto's proxy and permission rules for Contexto's five tools) and an --mcp-config file (Contexto's MCP "
    "server), so its requests carry Contexto's tool definitions and server instructions, and which runs "
    "'claude --version' once before the session. Its environment also has CONTEXTO_HOME and CONTEXTO_SESSION.",
    "plain only: CLAUDE_CODE_AUTO_COMPACT_WINDOW is set (see plain_compaction) and ANTHROPIC_BASE_URL is the meter. "
    "The contexto arm leaves auto-compaction at Claude Code's default for the model.",
    "Both Claude Code arms have a base URL on 127.0.0.1 (Contexto's proxy, or the meter). Claude Code behaves "
    "differently when its base URL is not the vendor's host (read in the 2.1.322 bundle: for one, it leaves fields "
    "out of the billing line of its system prompt, does not send request-class headers unless asked, and does not "
    "send the x-cc-context-compacted and x-cc-compaction-request headers it sends to the vendor's host around a "
    "compaction), and it does so in both arms alike; a plain session without the meter, straight to the vendor, "
    "would not.",
    "Both Claude Code arms alike run with CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1: Claude Code runs no subagent or "
    "shell command in the background, so a subagent the model starts runs in the foreground within its turn. "
    "Without it a background task's report starts a run of its own with its own result, and the replies of every "
    "later turn would be matched to the wrong turn; a unit where that still happens stops as desync.",
    "full is not Claude Code: its requests carry a one-line system prompt, the script's turns with scripted "
    "replies, one declared tool with tool_choice none when the script has a tool-loop turn, no thinking parameter, "
    "no beta header, and max_tokens 8,192. It answers each probe turn in one request with no tools, where the "
    "Claude Code arms may use theirs. Its decision replies and tool-loop file reads are scripted and always right "
    "(see full_arm), so its concluded and tool-loop probes test recall only.",
)
COMPACTION_COUNTING = (
    "compactions counts the compact_boundary events Claude Code printed: one per compaction it carried out. "
    "compaction_requests counts the billed requests Claude Code flagged as compaction requests "
    "(x-claude-code-request-class compaction, or x-claude-code-compaction), whether or not a boundary followed. "
    "The two differ: against the stub every compaction took two flagged requests (the second may be a retry, "
    "because the stub's summary is not in the form Claude Code asks for), and a flagged request can end with no "
    "boundary. Compaction requests carry the whole context, so they count in max_input, over_bound and tokens "
    "with the main requests, and on their own in compaction_tokens and compaction_max_input."
)
FULL_ARM_SCRIPTED = {
    "scripted": ["decision replies", "tool-loop file reads"],
    "not_applicable": ["concluded_ok", "toolloop_reads"],
    "note": "The full arm's decision replies and the file reads of its tool-loop turn come from the script and are "
            "always right; it never decides or reads anything itself. So concluded_ok and toolloop_reads do not "
            "apply to it, and its concluded and tool-loop probes measure recall of a conversation in which those "
            "steps were done right.",
}
METERING = (
    "Every arm's Messages requests pass through a pass-through on 127.0.0.1 that forwards them to the upstream and "
    "records counts only: token usage by class from the response, the status, the turn, main or side.",
    "The pass-through sets Host, drops hop-by-hop headers, and asks for an unencoded response on the Messages "
    "paths (Accept-Encoding: identity), as Contexto's own proxy does.",
    "It removes X-Session-Affinity when the upstream is a vendor's host: Contexto adds that header for any other "
    "upstream, and the pass-through is one.",
    "Both Claude Code arms run with CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 so that each request names its class; the "
    "pass-through removes those headers again, except for the plain arm to a vendor's host, where Claude Code "
    "sends them unasked.",
)


@dataclass
class Config:
    model: str
    bound: int
    protocol: str
    turn_kbytes: int
    seed: int
    seeds: list[int]
    repeats: int
    arms: tuple[str, ...]
    budget: int | None
    out: Path
    upstream: str
    credentials: Any  # contexto.bench.credentials.Credentials
    work: Path | None = None  # where unit directories are made; None is a new temporary directory
    keep_work: bool = False
    cache_ttl: str = "5m"
    turn_timeout: float = 1_800.0
    context_window: int | None = None
    upstream_source: str = "flag"  # "flag", "environment" ($ANTHROPIC_BASE_URL) or "default"
    turn_request_cap: int = TURN_REQUEST_CAP
    unit_token_cap: int | None = None  # None: UNIT_TOKEN_CAP_FACTOR times the unit's planned worst case


class Refused(Exception):
    """The run cannot start; the message says why. ``code`` is the exit code."""

    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def inside_git(path: Path) -> bool:
    """True when ``path``, or the nearest directory above it that exists, is inside a git work tree.
    Read from the file system (a ``.git`` entry in the directory or a parent), so git is not needed."""
    try:
        here = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return any(os.path.lexists(top / ".git") for top in (here, *here.parents))


def write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_manifest(out: Path) -> dict[str, Any] | None:
    path = out / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Refused(f"{path.name} in the output directory cannot be read ({type(exc).__name__}); "
                      "use another --out") from None
    if not isinstance(data, dict) or data.get("kind") != "live" or not isinstance(data.get("units"), list):
        raise Refused("the output directory holds a manifest that is not a live run's; use another --out")
    return data


def unit_key(unit: dict[str, Any]) -> tuple[str, int, int]:
    return str(unit["arm"]), int(unit["seed"]), int(unit["repeat"])


def build_scripts(config: Config) -> dict[int, Script]:
    return {seed: bench_script.build(seed, config.protocol, config.bound, config.turn_kbytes * 1_000)
            for seed in config.seeds}


def write_scripts(out: Path, scripts: dict[int, Script]) -> None:
    """Write each seed's script, or check that the one already there is the same script."""
    for seed, script in scripts.items():
        path = out / "scripts" / f"seed-{seed}.json"
        if path.exists():
            try:
                same = Script.read(path).digest() == script.digest()
            except (OSError, ValueError):
                same = False
            if not same:
                raise Refused(f"scripts/seed-{seed}.json in the output directory is not the script this command "
                              "generates; use another --out")
        else:
            script.write(path)


def drop_unrecorded_probes(out: Path, manifest: dict[str, Any]) -> int:
    """Remove ``probes.jsonl`` lines of units the manifest does not record ``complete``, and return how
    many. There are some only when an earlier run was killed between writing a unit's probe lines and
    its manifest entry; that unit runs again, and its lines must not be there twice."""
    path = out / "probes.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    complete = {unit_key(u) for u in manifest["units"] if u.get("status") == "complete"}
    kept = []
    for line in lines:
        try:
            if unit_key(json.loads(line)) in complete:
                kept.append(line)
        except (ValueError, KeyError, TypeError):
            continue  # not a probe line
    if len(kept) != len(lines):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
        os.replace(tmp, path)
    return len(lines) - len(kept)


def probe_lines(unit: dict[str, Any], model: str, script: Script, answers: dict[int, str]) -> list[dict[str, Any]]:
    """The ``probes.jsonl`` lines of a complete unit: one per probe, in the order asked. Each answer is
    scored whole and then cut to ``MAX_ANSWER_CHARS`` for the file; ``given_truncated`` says it was."""
    by_id = {p.id: p for p in script.probes}
    probe_turns = [i for i, k in enumerate(script.kinds) if k == "probe"]
    lines = []
    for position, turn in enumerate(probe_turns):
        asked = [by_id[i] for i in script.probe_turn_ids[position]]
        for row in bench_script.score(asked, answers.get(turn, ""))["rows"]:
            given = row["given"]
            lines.append({"arm": unit["arm"], "seed": unit["seed"], "repeat": unit["repeat"], "model": model,
                          "id": row["id"], "cls": row["cls"], "type": row["type"], "tier": row["tier"],
                          "kind": row["kind"], "turn": turn, "overlap": by_id[row["id"]].overlap,
                          "expected": row["expected"], "given": None if given is None else given[:MAX_ANSWER_CHARS],
                          "given_truncated": given is not None and len(given) > MAX_ANSWER_CHARS,
                          "correct": bool(row["correct"]), "stale": bool(row["stale"])})
    return lines


def label(upstream: str) -> str | None:
    """The manifest's ``label``: a run against a server on this machine is a stub run."""
    return STUB_LABEL if bench_meter.upstream_kind(upstream) == "local" else None


def upstream_note(source: str) -> str:
    where = {"flag": "--upstream", "environment": "$ANTHROPIC_BASE_URL (no --upstream was given)",
             "default": "the default, https://api.anthropic.com"}.get(source, source)
    return ("The upstream is --upstream, else $ANTHROPIC_BASE_URL, else https://api.anthropic.com; this run's came "
            f"from {where}. Only its kind is recorded, never its host: anthropic (the vendor's host), local (this "
            "machine) or custom.")


def runaway_caps(config: Config) -> dict[str, Any]:
    return {"turn_requests": config.turn_request_cap, "unit_tokens": config.unit_token_cap,
            "note": f"A unit stops as runaway when one script turn asks for more than {config.turn_request_cap:,} "
                    "billed requests, or when the unit would pass its token cap (token_cap in each unit: "
                    + (f"{config.unit_token_cap:,} tokens, set by --max-unit-tokens" if config.unit_token_cap else
                       f"{UNIT_TOKEN_CAP_FACTOR} times the unit's planned worst case from the token plan")
                    + "). The recorded live runs needed at most 6 requests in a probe turn and 4 in a content turn "
                      "(docs/research/M2-acceptance-run4.md and M2-acceptance-opus.md), and the tool-loop turn needs "
                      "one request per file plus one (11 for the v2 script's 10 files). The run goes on with the "
                      "next unit."}


def new_manifest(config: Config, claude_version: str | None) -> dict[str, Any]:
    from contexto import __version__
    from contexto.bench.arms import cli, full, plain

    return {
        "format": FORMAT, "kind": "live", "label": label(config.upstream),
        "contexto_version": __version__, "claude_code_version": claude_version,
        "model": config.model, "bound": config.bound, "protocol": config.protocol, "turn_kbytes": config.turn_kbytes,
        "seed": config.seed, "seeds": list(config.seeds), "repeats": config.repeats, "arms": list(config.arms),
        "upstream": bench_meter.upstream_kind(config.upstream), "upstream_source": config.upstream_source,
        "upstream_note": upstream_note(config.upstream_source),
        "plain_compaction": plain.compaction_setting(config.bound) if "plain" in config.arms else None,
        "cache_ttl": config.cache_ttl if set(config.arms) & set(bench_arms.CLI_ARMS) else None,
        "started": utc_now(), "finished": None,
        "budget_tokens": config.budget, "spent_tokens": 0,
        "credential_source": config.credentials.source,
        "cli_environment": {**cli.ENV, "CLAUDE_CODE_PROMPT_CACHE_TTL": config.cache_ttl,
                            **{name: os.environ[name] for name in sorted(cli.KEPT_NAMES) if name in os.environ},
                            "settings": {"permissions": {"allow": [cli.READ_TOOL]}}},
        "arm_differences": list(ARM_DIFFERENCES), "metering": list(METERING),
        "compaction_counting": COMPACTION_COUNTING,
        "request_classes": None,  # "header" when Claude Code named each request's class, else "inferred"
        "full_arm": {"max_tokens": full.MAX_TOKENS, "system": full.SYSTEM,
                     "context_window": full.context_window(config.model, config.context_window),
                     **FULL_ARM_SCRIPTED},
        "runaway_caps": runaway_caps(config),
        "units": [],
    }


def merge_manifest(old: dict[str, Any], config: Config, claude_version: str | None) -> dict[str, Any]:
    """An existing manifest brought up to this command: the same run, possibly with more seeds,
    repeats or arms. Raises ``Refused`` when the command is for a different run."""
    current = {"model": config.model, "bound": config.bound, "protocol": config.protocol,
               "turn_kbytes": config.turn_kbytes, "seed": config.seed}
    changed = [name for name in PINNED if old.get(name) != current[name]]
    if changed:
        raise Refused("the output directory holds a run with a different "
                      + ", ".join(f"{PINNED_NAMES[n]} ({old.get(n)!r}, not {current[n]!r})" for n in changed)
                      + "; use another --out")
    manifest = {**new_manifest(config, claude_version), **{k: old[k] for k in ("started", "units") if k in old}}
    manifest["seeds"] = sorted(set(old.get("seeds") or []) | set(config.seeds))
    manifest["repeats"] = max(int(old.get("repeats") or 1), config.repeats)
    manifest["arms"] = [a for a in bench_arms.ARMS if a in set(old.get("arms") or []) | set(config.arms)]
    if manifest["plain_compaction"] is None:
        manifest["plain_compaction"] = old.get("plain_compaction")
    manifest["cache_ttl"] = manifest["cache_ttl"] or old.get("cache_ttl")
    manifest["request_classes"] = old.get("request_classes")
    return manifest


def unit_entry(arm: str, seed: int, repeat: int, attempt: int, result: bench_arms.UnitResult, meter: Any,
               script: Script, earlier: list[dict[str, Any]] | None = None,
               token_cap: int | None = None) -> dict[str, Any]:
    summary = meter.summary()
    has_loop = script.toolloop_turn is not None and arm in bench_arms.CLI_ARMS
    loop = [r for r in meter.records if r["main"] and has_loop and r["turn"] == script.toolloop_turn]
    loop_inputs = [bench_meter.record_input(r) for r in loop]
    this = {"attempt": attempt, "status": result.status, "stop": result.stop, "requests": summary["requests"],
            "tokens": summary["tokens"]}
    return {
        "arm": arm, "seed": seed, "repeat": repeat, "status": result.status, "reason": result.reason,
        "cli_exit": result.cli_exit, "compactions": result.compactions,
        "compaction_pre_tokens": list(result.compaction_pre_tokens), "concluded_ok": dict(result.concluded_ok),
        "toolloop_reads": result.toolloop_reads, "probe_turns_completed": result.probe_turns_completed,
        "stop": result.stop, "attempt": attempt, "attempts": [*(earlier or []), this],
        "turns_completed": result.turns_completed,
        "requests": summary["requests"], "tokens": summary["tokens"], "max_input": summary["max_input"],
        "over_bound": summary["over_bound"], "compaction_requests": summary["compaction_requests"],
        "compaction_tokens": summary["compaction_tokens"], "compaction_max_input": summary["compaction_max_input"],
        "usage_unreadable": summary["usage_unreadable"], "token_cap": token_cap,
        "toolloop_requests": len(loop) if has_loop else None,
        "toolloop_max_input": max(loop_inputs, default=0) if has_loop else None,
        "toolloop_tool_calls": result.toolloop_tool_calls,
    }


def running_entry(arm: str, seed: int, repeat: int, attempt: int, earlier: list[dict[str, Any]],
                  token_cap: int | None) -> dict[str, Any]:
    """The manifest entry of a unit about to start: its attempt is taken before its first request."""
    return {"arm": arm, "seed": seed, "repeat": repeat, "status": "running", "reason": None, "cli_exit": None,
            "compactions": None, "compaction_pre_tokens": [], "concluded_ok": {}, "toolloop_reads": None,
            "probe_turns_completed": 0, "stop": None, "attempt": attempt,
            "attempts": [*earlier, {"attempt": attempt, "status": "running", "stop": None, "requests": 0,
                                    "tokens": 0}],
            "turns_completed": 0, "requests": 0, "tokens": 0, "max_input": 0, "over_bound": 0,
            "token_cap": token_cap}


def settle_killed(manifest: dict[str, Any], recorded: dict[tuple[str, int, int], dict[int, dict[str, int]]]) -> int:
    """Record every ``running`` unit of an earlier run as stopped: that run was killed while the unit
    ran. Its requests and tokens come from ``requests.jsonl``. Returns how many there were."""
    count = 0
    for unit in manifest["units"]:
        if unit.get("status") != "running":
            continue
        count += 1
        attempt = int(unit.get("attempt") or 0)
        seen = recorded.get(unit_key(unit), {}).get(attempt, {"requests": 0, "tokens": 0})
        reason = "the run ended while this unit was running (the process was killed)"
        unit.update({"status": "stopped", "stop": "interrupted", "reason": reason,
                     "requests": seen["requests"], "tokens": seen["tokens"]})
        history = [a for a in unit.get("attempts") or [] if a.get("attempt") != attempt]
        unit["attempts"] = [*history, {"attempt": attempt, "status": "stopped", "stop": "interrupted", **seen}]
    return count


def next_attempt(key: tuple[str, int, int], manifest: dict[str, Any],
                 recorded: dict[tuple[str, int, int], dict[int, dict[str, int]]]) -> int:
    """One more than the largest attempt seen for ``key`` in the manifest or in ``requests.jsonl``."""
    seen = list(recorded.get(key, {}))
    for unit in manifest["units"]:
        if unit_key(unit) == key:
            seen.append(int(unit.get("attempt") or 0))
            seen += [int(a["attempt"]) for a in unit.get("attempts") or [] if isinstance(a.get("attempt"), int)]
    return max(seen, default=-1) + 1


@contextlib.contextmanager
def out_lock(out: Path):
    """Hold the output directory's lock, or raise ``Refused`` at once when another run holds it."""
    fd = os.open(out / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("another contexto bench run is writing to this output directory (it holds "
                          f"{LOCK_NAME}); wait for it to end, or use another --out") from None
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def _sigterm_as_interrupt():
    """While a run is in progress, SIGTERM unwinds like Ctrl+C, so child processes are stopped."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def prepare(config: Config, scripts: dict[int, Script] | None = None) -> dict[str, Any]:
    """Everything a run decides before its first call: the scripts, the manifest (new or resumed),
    the units still to run, the plan for them and what is already spent. Raises ``Refused``."""
    unknown = [a for a in config.arms if a not in bench_arms.ARMS]
    if unknown or not config.arms:
        raise Refused(f"unknown arm {unknown[0]!r}: the arms are {', '.join(bench_arms.ARMS)}" if unknown
                      else "no arm to run")
    try:
        scripts = scripts or build_scripts(config)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    claude, version = None, None
    if set(config.arms) & set(bench_arms.CLI_ARMS):
        from contexto.bench.arms import cli, managed

        claude = cli.find_claude()
        if claude is None:
            raise Refused("'claude' was not found on PATH: the contexto and plain arms run Claude Code "
                          "(--arms full runs without it)", EXIT_NOT_FOUND)
        if found := managed.problems():
            raise Refused(managed.refusal(found))
        version = cli.claude_version(claude)
    old = read_manifest(config.out)
    manifest = merge_manifest(old, config, version) if old else new_manifest(config, version)
    recorded = bench_meter.read_recorded(config.out / "requests.jsonl")
    killed = settle_killed(manifest, recorded)
    done = {unit_key(u) for u in manifest["units"] if u.get("status") == "complete"}
    todo = [u for u in bench_plan.units(config.seeds, config.repeats, config.arms) if u not in done]
    spent = bench_meter.read_spent(config.out / "requests.jsonl")
    return {"scripts": scripts, "manifest": manifest, "todo": todo, "spent": spent, "claude": claude,
            "plan": bench_plan.estimate(scripts, todo, config.bound, model=config.model or None),
            "resumed": old is not None,
            "recorded": recorded, "killed": killed,
            "skipped": len(done & set(bench_plan.units(config.seeds, config.repeats, config.arms)))}


def run(config: Config, scripts: dict[int, Script] | None = None, *, stdout: TextIO | None = None,
        stderr: TextIO | None = None) -> int:
    """Run the benchmark. ``scripts`` (tests) replaces the scripts generated from ``config``."""
    err_stream = stderr or sys.stderr
    with contextlib.ExitStack() as stack:
        try:
            if config.out.is_dir():  # an existing directory: refuse at once if another run holds it
                stack.enter_context(out_lock(config.out))
            ready = prepare(config, scripts)
            if config.budget is None:
                raise Refused("a live run needs --budget-tokens; 'contexto bench --dry-run' with the same --model, "
                              "--arms, --seed, --seeds, --repeats, --bound, --protocol, --turn-kbytes and --out "
                              "prints the figure to pass")
            worst = ready["plan"]["total"].worst_total
            if ready["spent"] + worst > config.budget:
                already = (f" on top of the {ready['spent']:,} already recorded in this output directory"
                           if ready["spent"] else "")
                raise Refused(f"--budget-tokens {config.budget:,} is below the plan's worst case of {worst:,} "
                              f"tokens{already}; pass at least {bench_plan.budget_to_pass(ready['spent'] + worst):,}")
            if config.work is not None and inside_git(config.work):
                raise Refused("the --work directory is inside a git work tree, where Claude Code would load the "
                              "repository's settings; use a directory outside any repository")
            if not config.out.is_dir():
                config.out.mkdir(parents=True, exist_ok=True)
                stack.enter_context(out_lock(config.out))
                if read_manifest(config.out) is not None:  # another run started it between the checks and the lock
                    ready = prepare(config, scripts)
            write_scripts(config.out, ready["scripts"])
        except Refused as exc:
            print(f"contexto bench: refusing: {exc}", file=err_stream)
            return exc.code
        return _run_units(config, ready, stdout or sys.stdout, err_stream)
    raise AssertionError("unreachable")


def _run_units(config: Config, ready: dict[str, Any], out_stream: TextIO, err_stream: TextIO) -> int:
    def say(text: str) -> None:
        print(text, file=out_stream, flush=True)

    manifest, todo, all_scripts = ready["manifest"], ready["todo"], ready["scripts"]
    recorded: dict[tuple[str, int, int], dict[int, dict[str, int]]] = ready["recorded"]
    ledger = bench_meter.Ledger(config.out / "requests.jsonl", config.budget, ready["spent"])
    manifest["spent_tokens"] = ledger.spent
    drop_unrecorded_probes(config.out, manifest)
    write_json_atomic(config.out / "manifest.json", manifest)
    if ready["resumed"]:
        killed = f" ({ready['killed']} found running, from a run that was killed, now recorded as stopped)" \
            if ready["killed"] else ""
        say(f"resuming: {ready['skipped']} units are complete, {len(todo)} to run{killed}")
    work = config.work or Path(tempfile.mkdtemp(prefix="contexto-bench-"))
    work.mkdir(parents=True, exist_ok=True)
    histories = {seed: bench_meter.history_tokens(script) for seed, script in all_scripts.items()}
    plans: dict[tuple[str, int], int] = {}
    interrupted = False
    try:
        with _sigterm_as_interrupt():
            for position, (arm, seed, repeat) in enumerate(todo, start=1):
                script = all_scripts[seed]
                key = (arm, seed, repeat)
                previous = next((u for u in manifest["units"] if unit_key(u) == key), None)
                earlier = [a for a in (previous or {}).get("attempts") or [] if isinstance(a, dict)]
                attempt = next_attempt(key, manifest, recorded)
                if config.unit_token_cap is not None:
                    token_cap = config.unit_token_cap
                else:
                    if (arm, seed) not in plans:
                        plans[(arm, seed)] = bench_plan.unit_estimate(arm, script, config.bound).worst_total
                    token_cap = UNIT_TOKEN_CAP_FACTOR * plans[(arm, seed)]
                # The attempt is in the manifest before the unit's first request: a SIGKILL from here on
                # leaves the unit's records attributed to it.
                manifest["units"] = [u for u in manifest["units"] if unit_key(u) != key] + [
                    running_entry(arm, seed, repeat, attempt, earlier, token_cap)]
                write_json_atomic(config.out / "manifest.json", manifest)
                recorded.setdefault(key, {}).setdefault(attempt, {"requests": 0, "tokens": 0})
                directory = work / f"{arm}-{seed}-{repeat}-{attempt}"
                meter = bench_meter.Meter(config.upstream, ledger, arm=arm, seed=seed, repeat=repeat,
                                          model=config.model, history=histories[seed], bound=config.bound,
                                          attempt=attempt, turn_request_cap=config.turn_request_cap,
                                          token_cap=token_cap)
                result = bench_arms.UnitResult()
                try:
                    found = []
                    if arm in bench_arms.CLI_ARMS:
                        from contexto.bench.arms import managed

                        found = managed.problems()
                    if found:
                        result = bench_arms.UnitResult().stopped("managed_settings", managed.refusal(found))
                    else:
                        if directory.exists():
                            shutil.rmtree(directory)
                        directory.mkdir(parents=True)
                        unit = bench_arms.Unit(arm=arm, script=script, model=config.model, bound=config.bound,
                                               credentials=config.credentials, upstream=meter.start(),
                                               directory=directory, meter=meter, cache_ttl=config.cache_ttl,
                                               turn_timeout=config.turn_timeout,
                                               context_window=config.context_window,
                                               claude=ready["claude"] or "claude")
                        result = bench_arms.runner(arm)(unit)
                except KeyboardInterrupt:
                    interrupted = True
                    result = bench_arms.UnitResult().stopped("interrupted", "the run was interrupted")
                except Exception as exc:  # noqa: BLE001 - one unit's failure must not lose the run's record
                    result = bench_arms.UnitResult().stopped(
                        "error", f"the benchmark itself failed in this unit ({type(exc).__name__})")
                finally:
                    try:
                        meter.stop()
                        if not config.keep_work:
                            shutil.rmtree(directory, ignore_errors=True)
                    except KeyboardInterrupt:  # a second Ctrl+C: the unit is still recorded below
                        interrupted = True
                if result.status == "complete" and ledger.stop is not None:
                    result.stopped(ledger.stop, "the run was stopped while this unit was finishing")
                entry = unit_entry(arm, seed, repeat, attempt, result, meter, script, earlier, token_cap)
                recorded[key][attempt] = {"requests": entry["requests"], "tokens": entry["tokens"]}
                if result.status == "complete":
                    lines = probe_lines(entry, config.model, script, result.answers)
                    with (config.out / "probes.jsonl").open("a", encoding="utf-8") as f:
                        f.write("".join(json.dumps(line) + "\n" for line in lines))
                manifest["units"] = [u for u in manifest["units"] if unit_key(u) != key] + [entry]
                manifest["spent_tokens"] = ledger.spent
                if meter.records and manifest["request_classes"] != "header" and arm in bench_arms.CLI_ARMS:
                    manifest["request_classes"] = "header" if meter.classes_seen else "inferred"
                write_json_atomic(config.out / "manifest.json", manifest)
                detail = f": {result.reason}" if result.reason else ""
                budget = f" of {ledger.budget:,}" if ledger.budget is not None else ""
                tries = f", attempt {attempt + 1}" if attempt else ""
                say(f"unit {position}/{len(todo)} {arm} seed {seed} repeat {repeat}{tries}: {result.status}{detail} "
                    f"({entry['requests']} requests, {entry['tokens']:,} tokens; {ledger.spent:,}{budget} spent)")
                if interrupted or ledger.stop is not None or result.stop == "managed_settings":
                    break
    except KeyboardInterrupt:  # outside an arm: while a unit's record was being written
        interrupted = True
    finally:
        if not config.keep_work and config.work is None:
            shutil.rmtree(work, ignore_errors=True)
    if interrupted:
        print("contexto bench: interrupted; the same command with the same --out resumes the run", file=err_stream)
        return EXIT_INTERRUPTED
    stopped = [u for u in manifest["units"] if u["status"] == "stopped"]
    run_stop = ledger.stop or next((u["stop"] for u in stopped if u.get("stop") == "managed_settings"), None)
    if run_stop is None:
        manifest["finished"] = utc_now()
        write_json_atomic(config.out / "manifest.json", manifest)
    else:
        why = {"budget": "the token budget", "auth": "a refused credential",
               "managed_settings": "organization-managed Claude Code settings"}[run_stop]
        print(f"contexto bench: the run stopped on {why}; the same command with the same --out resumes it",
              file=err_stream)
    report(config.out, out_stream)
    return EXIT_STOPPED if stopped or run_stop is not None else 0


def report(out: Path, stream: TextIO) -> None:
    """Write the results with the report code when this build has it; else say how to get them."""
    try:
        from contexto.bench import report as bench_report
    except ImportError:
        bench_report = None
    write = getattr(bench_report, "write", None)
    if not callable(write):
        print(f"contexto bench: this build has no report code; run 'contexto bench --report {out.name}' with a "
              "build that has it to get results.json and results.md", file=stream)
        return
    try:
        write(out)
    except Exception as exc:  # noqa: BLE001 - the run's own files are written; the report can be made again
        print(f"contexto bench: the report could not be written ({type(exc).__name__}); the run's files are "
              f"complete, and 'contexto bench --report {out.name}' makes it again", file=stream)
