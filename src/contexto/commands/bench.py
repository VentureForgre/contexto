"""``contexto bench``: benchmark Contexto against full context and plain Claude Code (D66).

One command with modes. With no mode flag it is the live three-arm run; a mode flag selects another
mode (``--script-only``, ``--dry-run``, ``--replay``, ``--report DIR``, ``--chart RESULTS``). Exactly
one mode runs.

Modes are discovered from the ``contexto.bench.modes`` package the way ``contexto.cli`` discovers
commands, so a mode is added by adding a module there and this file does not change. A mode module
provides ``FLAG`` (its option, or None for the one default mode), ``add_arguments(parser)`` and
``run(args) -> int``; the rules are in ``contexto.bench.modes``. The options every mode shares are
defined here, once.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path
from types import ModuleType

from contexto.bench import modes
from contexto.bench.model import PROTOCOLS
from contexto.bench.script import DEFAULT_SEED
from contexto.commands._common import EXIT_USAGE

DEFAULT_BOUND = 150_000
DEFAULT_TURN_KBYTES = 40


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
                        help="the script protocol: d27 is the 54 probes of the published runs; v2 (the default) "
                             "adds 18 probes that do not name their subject (paraphrased, concluded by the "
                             "assistant, and read in a tool loop)")
    parser.add_argument("--turn-kbytes", type=_count, default=DEFAULT_TURN_KBYTES, metavar="KB",
                        help=f"the size of a content turn, in thousands of bytes (default {DEFAULT_TURN_KBYTES})")
    parser.add_argument("--out", type=Path, default=None, metavar="DIR",
                        help="the output directory: scripts, per-probe results and the report")
    found = discover()
    for module in found:
        module.add_arguments(parser.add_argument_group(f"{mode_name(module)} mode"))
    parser.set_defaults(func=run, _bench_modes=found)


def run(args: argparse.Namespace) -> int:
    found = args._bench_modes
    flagged = [m for m in found if m.FLAG is not None]
    chosen = [m for m in flagged if getattr(args, _dest(m.FLAG), None) not in (None, False)]
    if len(chosen) > 1:
        print(f"contexto bench: error: give one mode, not {' and '.join(m.FLAG for m in chosen)}", file=sys.stderr)
        return EXIT_USAGE
    if chosen:
        return int(chosen[0].run(args) or 0)
    default = [m for m in found if m.FLAG is None]
    if len(default) == 1:
        return int(default[0].run(args) or 0)
    if default:
        print("contexto bench: error: more than one default mode is installed: "
              f"{', '.join(mode_name(m) for m in default)}", file=sys.stderr)
        return EXIT_USAGE
    print("contexto bench: error: this build has no live run; the modes it has are "
          f"{', '.join(m.FLAG for m in flagged) or 'none'}", file=sys.stderr)
    return EXIT_USAGE
