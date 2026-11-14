"""``contexto bench``: benchmark Contexto against full context and plain Claude Code (D66).

One command with modes. With no mode flag it is the live three-arm run; a mode flag selects another
mode (``--script-only``, ``--dry-run``, ``--replay``, ``--report DIR``, ``--chart RESULTS``). Exactly
one mode runs.

Modes are discovered from the ``contexto.bench.modes`` package the way ``contexto.cli`` discovers
commands, so a mode is added by adding a module there and this file does not change. A mode module
provides ``FLAG`` (its option, or None for the one default mode), ``add_arguments(parser)`` and
``run(args) -> int``, and may list the options that belong to it alone in ``OWN_OPTIONS``; the
rules are in ``contexto.bench.modes``. The options every mode shares are defined here, once.

Before a mode runs, this command refuses (exit 2) a mode whose ``OWN_OPTIONS`` lists an option its
``add_arguments`` did not add, an option that belongs to a mode that is not the one running, and a
``--turn-kbytes`` so small for the bound that the script would need more than ``MAX_CONTENT_TURNS``
content turns.
"""

from __future__ import annotations

import argparse
import importlib
import math
import pkgutil
import sys
from pathlib import Path
from types import ModuleType

from contexto.bench import modes
from contexto.bench.model import DEFAULT_SEED, PROTOCOLS
from contexto.commands._common import EXIT_USAGE

DEFAULT_BOUND = 150_000
DEFAULT_TURN_KBYTES = 40
MAX_CONTENT_TURNS = 400  # 2,000-byte turns at the default bound need 332; 1,000-byte turns need 652


def _count(text: str) -> int:
    if not (text.isascii() and text.isdecimal()) or int(text) == 0:
        raise argparse.ArgumentTypeError(f"invalid count {text!r}: use a whole number, 1 or more")
    return int(text)


def _seed(text: str) -> int:
    if not (text.isascii() and text.isdecimal()):
        raise argparse.ArgumentTypeError(f"invalid seed {text!r}: use a whole number, 0 or more")
    return int(text)


def discover() -> list[ModuleType]:
    """The mode modules of ``contexto.bench.modes``, by name. A module without the three parts of a
    mode (``FLAG``, ``add_arguments``, ``run``) is not a mode and is skipped."""
    found = []
    for info in sorted(pkgutil.iter_modules(modes.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{modes.__name__}.{info.name}")
        if hasattr(module, "FLAG") and callable(getattr(module, "add_arguments", None)) \
                and callable(getattr(module, "run", None)):
            found.append(module)
    return found


def mode_name(module: ModuleType) -> str:
    return module.__name__.rsplit(".", 1)[-1].replace("_", "-")


def _dest(flag: str) -> str:
    return flag.lstrip("-").replace("-", "_")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "bench", help="benchmark Contexto against full context and plain Claude Code",
        description="Run the benchmark: the same generated session against Contexto, a full-context replay and "
                    "plain Claude Code, scored by exact match against planted answers. With no mode option it "
                    "is the live run, which calls the model with your own key. The mode options below do "
                    "something else instead; give at most one.")
    parser.add_argument("--seed", type=_seed, default=DEFAULT_SEED, metavar="BASE",
                        help=f"the first seed (default {DEFAULT_SEED}, the seed of the published runs)")
    parser.add_argument("--seeds", type=_count, default=1, metavar="N",
                        help="run N seeds: BASE, BASE + 1, and so on (default 1)")
    parser.add_argument("--repeats", type=_count, default=1, metavar="R",
                        help="repeat the first seed R times, to measure run-to-run noise (default 1)")
    parser.add_argument("--bound", type=_count, default=DEFAULT_BOUND, metavar="TOKENS",
                        help=f"the token bound of the Contexto arm, which the session is sized against "
                             f"(default {DEFAULT_BOUND})")
    parser.add_argument("--protocol", choices=PROTOCOLS, default="v2",
                        help="the script protocol: d27 is the 54 probes of the published runs, each naming its "
                             "subject's key; v2 (the default) adds 18 harder probes: questions that name the "
                             "service but not the attribute, among several lines for that service, facts the "
                             "assistant concluded, and facts read in a tool loop")
    parser.add_argument("--turn-kbytes", type=_count, default=DEFAULT_TURN_KBYTES, metavar="KB",
                        help=f"the size of a content turn, in thousands of bytes (default {DEFAULT_TURN_KBYTES}). "
                             f"Refused when the script would need more than {MAX_CONTENT_TURNS} content turns to "
                             "reach 2.5 times the bound: below 2 at the default bound")
    parser.add_argument("--out", type=Path, default=None, metavar="DIR",
                        help="the output directory: scripts, per-probe results and the report")
    # argparse has no public way to look an option up, hence its private table of option strings,
    # which argument groups share with their parser.
    actions = parser._option_string_actions
    common = set(actions)
    found = discover()
    added_by: dict[str, ModuleType] = {}
    for module in found:
        before = set(actions)
        module.add_arguments(parser.add_argument_group(f"{mode_name(module)} mode"))
        added_by.update((option, module) for option in set(actions) - before)
    # Each mode's own options, as (module, option, dest, default), and the mode bugs in OWN_OPTIONS.
    own, bugs = [], []
    for module in found:
        for option in getattr(module, "OWN_OPTIONS", ()):
            problem = _own_problem(module, option, actions, common, added_by)
            if problem:
                bugs.append(f"mode {mode_name(module)} lists {option} in OWN_OPTIONS{problem}")
            else:
                own.append((module, option, actions[option].dest, actions[option].default))
    parser.set_defaults(func=run, _bench_modes=found, _bench_own=own, _bench_bugs=bugs)


def _own_problem(module: ModuleType, option: str, actions: dict, common: set[str],
                 added_by: dict[str, ModuleType]) -> str | None:
    """Why ``module`` may not list ``option`` as its own (the end of the error message), or None."""
    if option not in actions:
        return " but adds no such option"
    if option in common:
        reason = "it is a common option of contexto bench"
    elif option == module.FLAG:
        reason = "it is the mode's own FLAG"
    elif getattr(actions[option], modes.SHARED, False):
        reason = "it is shared through add_option"
    elif added_by.get(option) is not module:
        reason = f"the {mode_name(added_by[option])} mode adds it"
    else:
        return None
    return f", but {reason}; a mode lists only options its add_arguments adds, other than its FLAG"


def _turn_problem(args: argparse.Namespace) -> str | None:
    """Why ``--turn-kbytes`` is too small for ``--bound``, or None."""
    from contexto.bench import d27  # imported here, not at every contexto start

    def needed(kbytes: int) -> int:
        return math.ceil(d27.HISTORY_FACTOR * args.bound / (d27.turn_tokens(kbytes * 1_000) + d27.REPLY_TOKENS))

    turns = needed(args.turn_kbytes)
    if turns <= MAX_CONTENT_TURNS:
        return None
    floor = next(k for k in range(args.turn_kbytes, 10 ** 9) if needed(k) <= MAX_CONTENT_TURNS)
    return (f"--turn-kbytes {args.turn_kbytes} at --bound {args.bound:,} needs about {turns:,} content turns, more "
            f"than the {MAX_CONTENT_TURNS} a script may have; use --turn-kbytes {floor} or more, or a smaller bound")


def _foreign_option(args: argparse.Namespace, running: ModuleType) -> str | None:
    """The error for an option that belongs to a mode other than ``running`` and was given, or None."""
    if args._bench_bugs:
        return args._bench_bugs[0]
    for module, option, dest, default in args._bench_own:
        if module is running or getattr(args, dest, default) == default:
            continue
        if module.FLAG is None:
            return (f"{option} belongs to the live run ({mode_name(module)}), which runs only when no mode option "
                    f"is given; leave it out with {running.FLAG}")
        return f"{option} belongs to the {mode_name(module)} mode; give {module.FLAG} with it, or leave it out"
    return None


def run(args: argparse.Namespace) -> int:
    found = args._bench_modes
    flagged = [m for m in found if m.FLAG is not None]
    chosen = [m for m in flagged if getattr(args, _dest(m.FLAG), None) not in (None, False)]
    if len(chosen) > 1:
        print(f"contexto bench: error: give one mode, not {' and '.join(m.FLAG for m in chosen)}", file=sys.stderr)
        return EXIT_USAGE
    if chosen:
        running = chosen[0]
    else:
        default = [m for m in found if m.FLAG is None]
        if len(default) > 1:
            print("contexto bench: error: more than one default mode is installed: "
                  f"{', '.join(mode_name(m) for m in default)}", file=sys.stderr)
            return EXIT_USAGE
        if not default:
            print("contexto bench: error: this build has no live run; the modes it has are "
                  f"{', '.join(m.FLAG for m in flagged) or 'none'}", file=sys.stderr)
            return EXIT_USAGE
        running = default[0]
    problem = _foreign_option(args, running) or _turn_problem(args)
    if problem:
        print(f"contexto bench: error: {problem}", file=sys.stderr)
        return EXIT_USAGE
    return int(running.run(args) or 0)
