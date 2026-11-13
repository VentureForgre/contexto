"""``contexto bench --replay``: replay a pinned synthetic session through the real proxy. No key.

It needs no key, no ``claude`` and no network, and calls no model: the session is regenerated from
its seed, checked against the SHA-256 of every turn pinned in the shipped trace, and sent through the
proxy to a fake upstream on 127.0.0.1 that replies from a script (``contexto.bench.replay``).

With the default options it replays the pinned trace and exits 0 when the bound held, the session log
has no failure event, and every measurement equals the trace's expectations; 1 otherwise, with each
difference listed. With another ``--seed``, ``--bound``, ``--protocol``, ``--turn-kbytes`` or
``--replay-turns`` it still runs, says that it is not the pinned trace, and checks nothing against
recorded hashes or expectations.

``--out DIR`` writes ``manifest.json``, ``scripts/seed-<seed>.json``, ``requests.jsonl``,
``reach.jsonl``, ``results.json`` and ``results.md`` and keeps the session's store in ``DIR/store``;
without it the store is a temporary directory and only the summary is printed.

``--replay --dump-trace DIR`` writes the regenerated session (every turn's text, the files, the
scripted replies, the synthetic head) and does not replay. ``--replay --record-trace FILE`` is for
maintainers: it replays and writes the trace file, which is never edited by hand.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

FLAG = "--replay"
MIN_TURNS = 4  # the generator's own minimum of d27 content turns


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


def _error(message: str) -> None:
    print(f"contexto bench: error: {message}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    from contexto import __version__
    from contexto.bench import script as bench_script
    from contexto.bench.replay import output, session, trace
    from contexto.launcher import LaunchError

    if args.seeds != 1 or args.repeats != 1:
        _error("--replay replays one session, so --seeds and --repeats do not apply")
        return 2
    turns = args.replay_turns if args.replay_turns is not None else trace.PINNED["turns"]
    params = {"seed": args.seed, "protocol": args.protocol, "bound": args.bound,
              "turn_bytes": args.turn_kbytes * 1_000, "turns": turns}
    recording = args.record_trace is not None
    pinned = params == trace.PINNED and not recording
    try:
        script = bench_script.build(args.seed, args.protocol, args.bound, args.turn_kbytes * 1_000, turns)
    except ValueError as exc:
        _error(f"seed {args.seed}: {exc} (--replay-turns N makes the session longer)")
        return 2
    pinned_trace = None
    if pinned:
        try:
            pinned_trace = trace.load_packaged(args.seed)
            trace.check(pinned_trace, script, turns)
        except trace.TraceError as exc:
            _error(str(exc))
            return 1

    if args.dump_trace is not None:
        try:
            written = trace.dump(args.dump_trace, script, session.session_name(script))
        except (OSError, trace.TraceError) as exc:
            _error(f"cannot write the session to {args.dump_trace}: {exc}")
            return 1
        print(f"wrote the regenerated session to {args.dump_trace}: {len(script.turns)} turns, "
              f"{len(script.files)} files, {len(written)} files in all; nothing was replayed"
              + (" (checked against the pinned trace's hashes)" if pinned else " (not the pinned trace)"))
        return 0

    if args.out is not None and (args.out / "store").exists():
        _error(f"{args.out / 'store'} already exists: a replay starts from an empty store, so give a new --out "
               "directory or remove that one")
        return 2
    started = output.utc_now()
    try:
        if args.out is not None:
            measured = session.replay(script, args.out / "store")
        else:
            with tempfile.TemporaryDirectory(prefix="contexto-bench-replay-") as scratch:
                measured = session.replay(script, Path(scratch))
    except LaunchError as exc:
        _error(str(exc))
        return exc.code
    except (session.ReplayError, OSError) as exc:
        _error(f"the replay stopped: {exc}")
        return 1
    finished = output.utc_now()

    differences = trace.differences(pinned_trace["expected"], trace.expected(measured)) if pinned_trace else []
    failed = output.failures(script, measured, differences)
    manifest = output.manifest(script, turns, measured, __version__, pinned, started, finished)
    rows = output.request_rows(script, measured)
    data = output.results(script, manifest, rows, measured, pinned, differences, failed)
    print("\n".join(output.summary(script, data)))
    try:
        if args.out is not None:
            written = output.write(args.out, script, manifest, rows, measured.reach, data)
            print(f"Written to {args.out}: " + ", ".join(str(p.relative_to(args.out)) for p in written)
                  + f"; the session's store is in store/ (session {measured.session_id}).")
        if recording:
            trace.record(args.record_trace, script, turns, measured, __version__)
            print(f"Trace written to {args.record_trace}: {len(measured.requests)} requests, largest "
                  f"{measured.max_input:,}, {measured.rotations} rotations. It records what this run measured; "
                  "review the difference before committing it.")
    except OSError as exc:
        _error(f"cannot write the output: {exc}")
        return 1
    if differences:
        print("The measurements differ from the pinned trace. If the proxy's behaviour was changed on purpose, a "
              "maintainer records the trace again (contexto bench --replay --record-trace FILE) and reviews the "
              "difference.", file=sys.stderr)
    return 1 if failed else 0
