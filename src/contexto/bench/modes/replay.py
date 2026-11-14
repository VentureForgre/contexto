"""``contexto bench --replay``: replay a pinned synthetic session through the real proxy. No key.

It needs no key, no ``claude`` and no network, and calls no model: the session is regenerated from
its seed, checked against the SHA-256 of every turn pinned in the shipped trace, and sent through the
proxy to a fake upstream on 127.0.0.1 that replies from a script (``contexto.bench.replay``).

With the default options it replays the pinned trace and exits 0 when the bound held, the session log
has no failure event, and every measurement equals the trace's expectations; 1 otherwise, with each
difference listed. With another ``--seed``, ``--bound``, ``--protocol``, ``--turn-kbytes`` or
``--replay-turns`` it still runs, says that it is not the pinned trace, and checks nothing against
recorded hashes or expectations. A bound outside 25,001 to 170,000 tokens is refused (exit 2): those
are the limits the proxy starts with when no ``contexto run`` option widens them.

``--out DIR`` writes ``manifest.json``, ``scripts/seed-<seed>.json``, ``requests.jsonl``,
``reach.jsonl``, ``results.json`` and ``results.md`` and keeps the session's store in ``DIR/store``;
without it the store is a temporary directory and only the summary is printed. A directory that
already holds any file the replay or the live run writes is refused (exit 2), so a replay never
overwrites the results of a run.

``--replay --dump-trace DIR`` writes the regenerated session (every turn's text, the files, the
scripted replies, the synthetic head) and does not replay. ``--replay --record-trace FILE`` is for
maintainers: it replays and writes the trace file, which is never edited by hand. It refuses an
existing FILE unless ``--force`` is given, and writes nothing when the run it would record failed.

Ctrl+C, SIGTERM, SIGHUP and the launcher's other terminating signals stop the replay with one line
on stderr and exit 128 + the signal's number (130 for Ctrl+C); the proxy and the fake upstream are
stopped and a temporary store is removed on the way. A store kept with ``--out`` loses its
``page.key`` and ``affinity.salt`` however the replay ends.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

FLAG = "--replay"
# The options only a replay takes (the mode contract, ``contexto.bench.modes``): given without --replay,
# contexto bench refuses them (exit 2) instead of running another mode, so ``contexto bench --dump-trace
# DIR`` never starts the live run with the user's key. A mode that later needs --force too shares it
# with ``modes.add_option`` and takes it out of this list.
OWN_OPTIONS = ("--replay-turns", "--dump-trace", "--record-trace", "--force")
MIN_TURNS = 4  # the generator's own minimum of d27 content turns
# Everything a replay or a live run writes into --out (the shared output schema): a directory that holds
# any of them already holds results, which a replay must not overwrite.
OUTPUT_NAMES = ("manifest.json", "requests.jsonl", "probes.jsonl", "reach.jsonl", "results.json", "results.md",
                "scripts", "charts", "store")
SHOWN_DIFFERENCES = 5  # the differences a failed pinned replay lists before giving their count
RECORD_COMMAND = "contexto bench --replay --record-trace src/contexto/bench/data/{name} --force"
EXIT_FAILED, EXIT_USAGE = 1, 2


def _turns(text: str) -> int:
    if not (text.isascii() and text.isdecimal()) or int(text) < MIN_TURNS:
        raise argparse.ArgumentTypeError(f"invalid turn count {text!r}: use a whole number, {MIN_TURNS} or more")
    return int(text)


def add_arguments(parser) -> None:
    parser.add_argument(FLAG, action="store_true",
                        help="replay a pinned synthetic session through the real proxy against a fake upstream on "
                             "127.0.0.1: no key, no claude, no network, no model call. Shows whether the bound "
                             "held, the size of every request and which planted facts search can reach; cannot "
                             "show answer accuracy, cache reads or real token counts")
    parser.add_argument("--replay-turns", type=_turns, default=None, metavar="N",
                        help="with --replay: the number of pasted-log turns of the d27 part of the session "
                             "(default: the pinned trace's). Any other number is not the pinned trace")
    parser.add_argument("--dump-trace", type=Path, default=None, metavar="DIR",
                        help="with --replay: write the regenerated session to DIR (every turn's text, the files, "
                             "the scripted replies, the synthetic head) and do not replay")
    parser.add_argument("--record-trace", type=Path, default=None, metavar="FILE",
                        help="with --replay, for maintainers: replay and write the trace file (hashes and expected "
                             "measurements) to FILE. The shipped trace is written this way, never by hand")
    parser.add_argument("--force", action="store_true",
                        help="with --replay --record-trace: replace FILE when it exists")


def _error(message: str) -> None:
    print(f"contexto bench: error: {message}", file=sys.stderr)


def _usage(args: argparse.Namespace, bound_limits: tuple[int, int]) -> str | None:
    """Why these options cannot be replayed, or None."""
    low, high = bound_limits
    if args.seeds != 1 or args.repeats != 1:
        return "--replay replays one session, so --seeds and --repeats do not apply"
    if args.dump_trace is not None and args.record_trace is not None:
        return "give one of --dump-trace and --record-trace: one writes the session without replaying it, the other " \
               "replays it to record its trace"
    if args.force and args.record_trace is None:
        return "--force only applies to --record-trace"
    if not low <= args.bound <= high:
        return (f"--bound {args.bound:,} cannot be replayed: a replay takes a bound from {low:,} to {high:,} tokens, "
                "the limits the proxy starts with by default")
    if args.record_trace is not None and args.record_trace.exists() and not args.force:
        return f"{args.record_trace} exists: --record-trace replaces a file only when --force is given"
    if args.out is not None:
        held = [name for name in OUTPUT_NAMES if (args.out / name).exists()]
        if held:
            return (f"{args.out} already holds {', '.join(held)}: a replay never overwrites results and starts from an "
                    "empty store, so give a new --out directory or remove those")
    return None


def _mismatch(differences: list[str], seed: int) -> str:
    """What a maintainer reads when the pinned replay measured something else: the differences, the
    command that records the trace again, and what to check in the trace's diff before committing it."""
    from contexto.bench.replay import trace

    shown = differences[:SHOWN_DIFFERENCES]
    more = len(differences) - len(shown)
    return "\n".join([
        f"The measurements differ from the pinned trace in {len(differences)} "
        + ("place:" if len(differences) == 1 else "places:"),
        *[f"  {line}" for line in shown],
        *([f"  and {more} more (all of them are in results.json, replay.differences, with --out)"] if more else []),
        "If the proxy's behaviour was changed on purpose, record the trace again, from the repository's root:",
        "  " + RECORD_COMMAND.format(name=trace.file_name(seed)),
        "and check three things in the trace's diff before committing it:",
        "  1. request sizes grew or shrank only where the change was meant to act (expected.request_tokens);",
        "  2. the bound still holds: over_bound and over_bound_proxy are 0, failure_events and compaction_markers "
        "empty;",
        "  3. reach did not fall (expected.reach, overall and by class), unless the change was to search.",
    ])


def run(args: argparse.Namespace) -> int:
    import signal

    from contexto import __version__, launcher
    from contexto.bench import script as bench_script
    from contexto.bench.replay import output, session, trace

    problem = _usage(args, (launcher.DEFAULT_HEADROOM_FLOOR + 1, launcher.BOUND_GUARD))
    if problem:
        _error(problem)
        return EXIT_USAGE
    turns = args.replay_turns if args.replay_turns is not None else trace.PINNED["turns"]
    params = {"seed": args.seed, "protocol": args.protocol, "bound": args.bound,
              "turn_bytes": args.turn_kbytes * 1_000, "turns": turns}
    recording = args.record_trace is not None
    pinned = params == trace.PINNED and not recording
    try:
        script = bench_script.build(args.seed, args.protocol, args.bound, args.turn_kbytes * 1_000, turns)
    except ValueError as exc:
        _error(f"seed {args.seed}: {exc} (--replay-turns N makes the session longer)")
        return EXIT_USAGE
    pinned_trace = None
    if pinned:
        try:
            pinned_trace = trace.load_packaged(args.seed)
            trace.check(pinned_trace, script, turns)
        except trace.TraceError as exc:
            _error(str(exc))
            return EXIT_FAILED

    if args.dump_trace is not None:
        try:
            written = trace.dump(args.dump_trace, script, session.session_name(script))
        except (OSError, trace.TraceError) as exc:
            _error(f"cannot write the session to {args.dump_trace}: {exc}")
            return EXIT_FAILED
        print(f"wrote the regenerated session to {args.dump_trace}: {len(script.turns)} turns, "
              f"{len(script.files)} files, {len(written)} files in all; nothing was replayed"
              + (" (checked against the pinned trace's hashes)" if pinned else " (not the pinned trace)"))
        return 0

    started = output.utc_now()
    try:
        with launcher.LaunchSignals() as signals:  # SIGTERM, SIGHUP and the rest unwind like Ctrl+C
            try:
                if args.out is not None:
                    # The store is kept to be published: its private files go on every way out of the
                    # replay, a signal or a failed request as well as a finished run.
                    try:
                        measured = session.replay(script, args.out / "store")
                    finally:
                        removed = session.remove_private_files(args.out / "store")
                    measured.private_files_removed = removed
                else:
                    with tempfile.TemporaryDirectory(prefix="contexto-bench-replay-") as scratch:
                        measured = session.replay(script, Path(scratch))
            finally:
                signals.disarm()
    except (KeyboardInterrupt, launcher.Terminated) as exc:
        signum = exc.signum if isinstance(exc, launcher.Terminated) else signal.SIGINT
        name = "Ctrl+C" if signum == signal.SIGINT else signal.Signals(signum).name
        kept = (f"; what was replayed so far is in {args.out / 'store'}, without its page.key and affinity.salt"
                if args.out is not None else "; its temporary store was removed")
        print(f"contexto bench: the replay was stopped by {name}{kept}.", file=sys.stderr)
        return 128 + int(signum)
    except launcher.LaunchError as exc:  # check_options: refused before anything started
        _error(f"the proxy does not start with this bound: {exc}")
        return exc.code
    except (session.ReplayError, OSError) as exc:
        _error(f"the replay stopped: {exc}")
        return EXIT_FAILED
    finished = output.utc_now()

    differences = trace.differences(pinned_trace["expected"], trace.expected(measured)) if pinned_trace else []
    failed = output.failures(script, measured, differences)
    manifest = output.manifest(script, turns, measured, __version__, pinned, started, finished)
    rows = output.request_rows(script, measured)
    data = output.results(script, manifest, rows, measured, pinned, differences, failed, recording)
    print("\n".join(output.summary(script, data)))
    try:
        if args.out is not None:
            written = output.write(args.out, script, manifest, rows, measured.reach, data)
            print(f"Written to {args.out}: " + ", ".join(str(p.relative_to(args.out)) for p in written)
                  + f"; the session's store is in store/ (session {measured.session_id}).")
        if recording and failed:
            _error(f"the run failed, so no trace was written to {args.record_trace}: a trace records a passing run")
            return EXIT_FAILED
        if recording:
            trace.record(args.record_trace, script, turns, measured, __version__)
            print(f"Trace written to {args.record_trace}: {len(measured.requests)} requests, the largest "
                  f"{measured.max_input:,} estimated tokens, {measured.rotations} rotations. It records what this run "
                  "measured; review the difference before committing it.")
    except OSError as exc:
        _error(f"cannot write the output: {exc}")
        return EXIT_FAILED
    if differences:
        print(_mismatch(differences, script.seed), file=sys.stderr)
    return EXIT_FAILED if failed else 0
