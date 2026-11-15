"""``contexto version``: print the version, as ``contexto --version`` and ``-V`` do (D64).

A command as well as a flag because that is what people type first; all three print the same line,
``contexto.version_line()``, which names the git revision of an install made from a git URL.
"""

from __future__ import annotations

import argparse
import sys

from contexto import version_line
from contexto.commands._common import EXIT_OK


def register(subparsers) -> None:
    parser = subparsers.add_parser("version", help="print the version",
                                   description="Print the Contexto version, as --version and -V do.")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    # With stdout closed, fall back to stderr, as --version and -V do (PR #257 review A).
    print(version_line(), file=sys.stdout or sys.stderr)
    return EXIT_OK
