"""The modes of ``contexto bench``. ``contexto.commands.bench`` discovers them from this package.

A mode is one module here whose name does not start with an underscore. It provides:

``FLAG``
    The option that selects the mode, for example ``"--script-only"`` or ``"--report"``. The one
    default mode, which runs when no mode flag is given (the live run), sets ``FLAG = None``.
``add_arguments(parser)``
    Adds the mode's options to ``parser``, an argparse argument group titled with the mode's name.
    A mode with a flag adds that flag here, so it chooses whether the flag is a switch
    (``action="store_true"``) or takes a value (``--report DIR``). The mode is selected when the
    flag's value is neither None nor False, so a flag that takes a value must default to None.
``run(args) -> int``
    Runs the mode with the parsed namespace and returns the exit code.
``OWN_OPTIONS`` (optional)
    The option strings, as added in ``add_arguments``, that belong to this mode alone, for example
    ``("--dump-trace",)``. ``contexto.commands.bench`` exits 2 with a message when one of them is
    set (its value differs from its default) while another mode runs, so ``contexto bench
    --dump-trace DIR`` without ``--replay`` does not quietly start the live run with the user's key.
    The live run lists its own options too (a key helper, say), so they are refused beside a mode
    flag. List every option the mode adds except its ``FLAG`` and the ones it shares through
    ``add_option``. Listing anything else is a mode bug, reported with exit 2 for every ``contexto
    bench`` run and naming the mode and the option: an option the mode's ``add_arguments`` did not
    add (one no mode adds, another mode's, or a common option), the mode's own ``FLAG``, or an
    option shared through ``add_option``, whichever mode added it first.

The common options (``--seed``, ``--seeds``, ``--repeats``, ``--bound``, ``--protocol``,
``--turn-kbytes``, ``--out``) are defined once in ``contexto.commands.bench``; a mode reads them from
``args`` and must not add them again. An option that two modes need (``--model``, say) is added by
both with ``add_option`` below, which keeps the first definition, because argparse refuses a second
definition of an option and that error would break every ``contexto`` command.

To add a mode, add a module here. Nothing else changes: no edit to ``contexto.commands.bench``.

Every mode module is imported when any ``contexto`` command starts, because the command line is
built from all of them. So a mode imports only what ``add_arguments`` needs at the top of its file
and imports the rest (the proxy, an HTTP client) inside ``run``.
"""

from __future__ import annotations

import argparse

SHARED = "_contexto_bench_shared"  # set on an action add_option made, so no mode can claim it as its own


def add_option(parser, *names: str, **kwargs) -> None:
    """``parser.add_argument(*names, **kwargs)``, unless another mode already added the option."""
    try:
        action = parser.add_argument(*names, **kwargs)
    except argparse.ArgumentError:  # a conflicting option string: the first definition stands
        return
    setattr(action, SHARED, True)


def seeds(args: argparse.Namespace) -> list[int]:
    """The seeds a run uses: ``--seed BASE`` and ``--seeds N`` give BASE, BASE + 1, and so on."""
    return [args.seed + k for k in range(args.seeds)]
