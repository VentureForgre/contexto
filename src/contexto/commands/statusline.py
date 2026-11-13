"""``contexto statusline``: one short line about a session, for Claude Code's status line (D64).

``contexto run claude`` installs it when the user has no status line of their own, and a status line
script can call it and append what it prints. The work is in ``contexto.statusline``; ``contexto.cli``
hands ``contexto statusline ...`` straight to it, without loading the other commands, because a status
line runs after every assistant message. This module lists the command in ``contexto --help``.
"""

from __future__ import annotations

from contexto.statusline import DESCRIPTION, add_arguments, run


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "statusline", help="print one line about a session for Claude Code's status line",
        description=DESCRIPTION)
    add_arguments(parser)
    parser.set_defaults(func=run)
