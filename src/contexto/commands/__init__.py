"""Subcommand modules for the contexto CLI.

Each module in this package exposes ``register(subparsers) -> None`` and is
discovered by ``contexto.cli`` at startup. A subcommand sets its handler with
``parser.set_defaults(func=handler)``; the handler takes the parsed namespace
and returns an exit code.
"""
