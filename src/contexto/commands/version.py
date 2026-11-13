"""``contexto version``: print the version, as ``contexto --version`` and ``-V`` do (D64).

A command as well as a flag because that is what people type first; all three print the same line.
"""

from __future__ import annotations

import argparse

from contexto import __version__
from contexto.commands._common import EXIT_OK


def register(subparsers) -> None:
    parser = subparsers.add_parser("version", help="print the version",
                                   description="Print the Contexto version, as --version and -V do.")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    print(f"contexto {__version__}")
    return EXIT_OK
