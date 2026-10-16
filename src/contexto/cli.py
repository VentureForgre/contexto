"""Command-line entry point. Subcommands are discovered from contexto.commands."""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from collections.abc import Sequence

from contexto import __version__, commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contexto",
        description="Durable, recoverable context for very long-running AI sessions.",
    )
    parser.add_argument("--version", action="version", version=f"contexto {__version__}")
    subparsers = parser.add_subparsers(title="commands", metavar="<command>")
    for info in sorted(pkgutil.iter_modules(commands.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{commands.__name__}.{info.name}")
        register = getattr(module, "register", None)
        if callable(register):
            register(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        # No subcommand given: show usage and signal a usage error.
        parser.print_help()
        return 2
    return int(func(args) or 0)
