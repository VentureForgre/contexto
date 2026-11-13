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
- ``manifest.json``: rewritten atomically after every unit.

Units are independent and a run can be resumed: the same command with the same ``--out`` keeps the
units the manifest records ``complete`` and runs the rest again. A rerun unit gets the next
``attempt`` number, which its request records carry, so a reader can tell the records of the attempt
the manifest describes from those of an earlier, stopped one; all of them stay in ``requests.jsonl``,
because all of them were billed. A different model, bound, protocol, turn size or first seed for an
existing ``--out`` is refused.

The budget (``--budget-tokens``) covers the whole output directory: what earlier attempts recorded in
``requests.jsonl`` counts as spent. A unit that stops writes no ``probes.jsonl`` lines; the run goes
on with the next unit unless the stop was the budget or a refused credential. Ctrl+C (or SIGTERM)
stops the child processes, records the unit as ``stopped`` and exits 130.

Fields this module adds to the shared schema. ``manifest.json``: ``budget_tokens``, ``spent_tokens``,
``credential_source``, ``cli_environment``, ``arm_differences``, ``metering``, ``request_classes``,
``full_arm``, and per unit ``stop``, ``attempt``, ``turns_completed``, ``requests``, ``tokens``,
``max_input``, ``over_bound``, ``toolloop_requests``, ``toolloop_max_input``. ``requests.jsonl``:
``attempt``.
"""

from __future__ import annotations

import contextlib
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
    "out of the billing line of its system prompt and does not send request-class headers unless asked), and it "
    "does so in both arms alike; a plain session without the meter, straight to the vendor, would not.",
    "full is not Claude Code: its requests carry a one-line system prompt, the script's turns with scripted "
    "replies, one declared tool with tool_choice none when the script has a tool-loop turn, no thinking parameter, "
    "no beta header, and max_tokens 8,192. It answers each probe turn in one request with no tools, where the "
    "Claude Code arms may use theirs.",
)
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
    """The ``probes.jsonl`` lines of a complete unit: one per probe, in the order asked."""
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
                          "correct": bool(row["correct"]), "stale": bool(row["stale"])})
    return lines


def new_manifest(config: Config, claude_version: str | None) -> dict[str, Any]:
    from contexto import __version__
    from contexto.bench.arms import cli, full, plain

    return {
        "format": FORMAT, "kind": "live", "contexto_version": __version__, "claude_code_version": claude_version,
        "model": config.model, "bound": config.bound, "protocol": config.protocol, "turn_kbytes": config.turn_kbytes,
        "seed": config.seed, "seeds": list(config.seeds), "repeats": config.repeats, "arms": list(config.arms),
        "upstream": bench_meter.upstream_kind(config.upstream),
        "plain_compaction": plain.compaction_setting(config.bound) if "plain" in config.arms else None,
        "cache_ttl": config.cache_ttl if set(config.arms) & set(bench_arms.CLI_ARMS) else None,
        "started": utc_now(), "finished": None,
        "budget_tokens": config.budget, "spent_tokens": 0,
        "credential_source": config.credentials.source,
        "cli_environment": {**cli.ENV, "CLAUDE_CODE_PROMPT_CACHE_TTL": config.cache_ttl,
                            **{name: os.environ[name] for name in sorted(cli.KEPT_NAMES) if name in os.environ},
                            "settings": {"permissions": {"allow": [cli.READ_TOOL]}}},
        "arm_differences": list(ARM_DIFFERENCES), "metering": list(METERING),
        "request_classes": None,  # "header" when Claude Code named each request's class, else "inferred"
        "full_arm": {"max_tokens": full.MAX_TOKENS, "system": full.SYSTEM,
                     "context_window": full.context_window(config.model, config.context_window)},
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
               script: Script) -> dict[str, Any]:
    summary = meter.summary()
    has_loop = script.toolloop_turn is not None and arm in bench_arms.CLI_ARMS
    loop = [r for r in meter.records if r["main"] and has_loop and r["turn"] == script.toolloop_turn]
    loop_inputs = [r["uncached"] + r["cache_write"] + r["cache_read"] for r in loop]
    return {
        "arm": arm, "seed": seed, "repeat": repeat, "status": result.status, "reason": result.reason,
        "cli_exit": result.cli_exit, "compactions": result.compactions,
        "compaction_pre_tokens": list(result.compaction_pre_tokens), "concluded_ok": dict(result.concluded_ok),
        "toolloop_reads": result.toolloop_reads, "probe_turns_completed": result.probe_turns_completed,
        "stop": result.stop, "attempt": attempt, "turns_completed": result.turns_completed,
        "requests": summary["requests"], "tokens": summary["tokens"], "max_input": summary["max_input"],
        "over_bound": summary["over_bound"],
        "toolloop_requests": len(loop) if has_loop else None,
        "toolloop_max_input": max(loop_inputs, default=0) if has_loop else None,
    }


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
        from contexto.bench.arms import cli

        claude = cli.find_claude()
        if claude is None:
            raise Refused("'claude' was not found on PATH: the contexto and plain arms run Claude Code "
                          "(--arms full runs without it)", EXIT_NOT_FOUND)
        version = cli.claude_version(claude)
    old = read_manifest(config.out)
    manifest = merge_manifest(old, config, version) if old else new_manifest(config, version)
    done = {unit_key(u) for u in manifest["units"] if u.get("status") == "complete"}
    todo = [u for u in bench_plan.units(config.seeds, config.repeats, config.arms) if u not in done]
    spent = bench_meter.read_spent(config.out / "requests.jsonl")
    return {"scripts": scripts, "manifest": manifest, "todo": todo, "spent": spent, "claude": claude,
            "plan": bench_plan.estimate(scripts, todo, config.bound), "resumed": old is not None,
            "skipped": len(done & set(bench_plan.units(config.seeds, config.repeats, config.arms)))}


def run(config: Config, scripts: dict[int, Script] | None = None, *, stdout: TextIO | None = None,
        stderr: TextIO | None = None) -> int:
    """Run the benchmark. ``scripts`` (tests) replaces the scripts generated from ``config``."""
    out_stream, err_stream = stdout or sys.stdout, stderr or sys.stderr

    def say(text: str) -> None:
        print(text, file=out_stream, flush=True)

    try:
        ready = prepare(config, scripts)
        if config.budget is None:
            raise Refused("a live run needs --budget-tokens; 'contexto bench --dry-run' with the same options "
                          "prints the figure to pass")
        worst = ready["plan"]["total"].worst_total
        if ready["spent"] + worst > config.budget:
            already = (f" on top of the {ready['spent']:,} already recorded in this output directory"
                       if ready["spent"] else "")
            raise Refused(f"--budget-tokens {config.budget:,} is below the plan's worst case of {worst:,} tokens"
                          f"{already}; pass at least {bench_plan.budget_to_pass(ready['spent'] + worst):,}")
        if config.work is not None and inside_git(config.work):
            raise Refused("the --work directory is inside a git work tree, where Claude Code would load the "
                          "repository's settings; use a directory outside any repository")
        config.out.mkdir(parents=True, exist_ok=True)
        write_scripts(config.out, ready["scripts"])
    except Refused as exc:
        print(f"contexto bench: refusing: {exc}", file=err_stream)
        return exc.code
    manifest, todo, all_scripts = ready["manifest"], ready["todo"], ready["scripts"]
    ledger = bench_meter.Ledger(config.out / "requests.jsonl", config.budget, ready["spent"])
    manifest["spent_tokens"] = ledger.spent
    drop_unrecorded_probes(config.out, manifest)
    write_json_atomic(config.out / "manifest.json", manifest)
    if ready["resumed"]:
        say(f"resuming: {ready['skipped']} units are complete, {len(todo)} to run")
    work = config.work or Path(tempfile.mkdtemp(prefix="contexto-bench-"))
    work.mkdir(parents=True, exist_ok=True)
    histories = {seed: bench_meter.history_tokens(script) for seed, script in all_scripts.items()}
    interrupted = False
    try:
        with _sigterm_as_interrupt():
            for position, (arm, seed, repeat) in enumerate(todo, start=1):
                script = all_scripts[seed]
                attempt = sum(1 for u in manifest["units"] if unit_key(u) == (arm, seed, repeat))
                directory = work / f"{arm}-{seed}-{repeat}-{attempt}"
                meter = bench_meter.Meter(config.upstream, ledger, arm=arm, seed=seed, repeat=repeat,
                                          model=config.model, history=histories[seed], bound=config.bound,
                                          attempt=attempt)
                result = bench_arms.UnitResult()
                try:
                    if directory.exists():
                        shutil.rmtree(directory)
                    directory.mkdir(parents=True)
                    unit = bench_arms.Unit(arm=arm, script=script, model=config.model, bound=config.bound,
                                           credentials=config.credentials, upstream=meter.start(), directory=directory,
                                           meter=meter, cache_ttl=config.cache_ttl, turn_timeout=config.turn_timeout,
                                           context_window=config.context_window, claude=ready["claude"] or "claude")
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
                entry = unit_entry(arm, seed, repeat, attempt, result, meter, script)
                if result.status == "complete":
                    lines = probe_lines(entry, config.model, script, result.answers)
                    with (config.out / "probes.jsonl").open("a", encoding="utf-8") as f:
                        f.write("".join(json.dumps(line) + "\n" for line in lines))
                manifest["units"] = [u for u in manifest["units"] if unit_key(u) != (arm, seed, repeat)] + [entry]
                manifest["spent_tokens"] = ledger.spent
                if meter.records and manifest["request_classes"] != "header" and arm in bench_arms.CLI_ARMS:
                    manifest["request_classes"] = "header" if meter.classes_seen else "inferred"
                write_json_atomic(config.out / "manifest.json", manifest)
                detail = f": {result.reason}" if result.reason else ""
                budget = f" of {ledger.budget:,}" if ledger.budget is not None else ""
                say(f"unit {position}/{len(todo)} {arm} seed {seed} repeat {repeat}: {result.status}{detail} "
                    f"({entry['requests']} requests, {entry['tokens']:,} tokens; {ledger.spent:,}{budget} spent)")
                if interrupted or ledger.stop is not None:
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
    if ledger.stop is None:
        manifest["finished"] = utc_now()
        write_json_atomic(config.out / "manifest.json", manifest)
    else:
        why = "the token budget" if ledger.stop == "budget" else "a refused credential"
        print(f"contexto bench: the run stopped on {why}; the same command with the same --out resumes it",
              file=err_stream)
    report(config.out, out_stream)
    return EXIT_STOPPED if stopped or ledger.stop is not None else 0


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
