"""`contexto mcp --session S`: serve one session's store to Claude Code over stdio (MCP)."""

from __future__ import annotations

import argparse
from pathlib import Path

from contexto.paths import contexto_home


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "mcp",
        help="run the MCP server for a session on stdio (Claude Code spawns this)",
        description="Serve contexto_recall, contexto_pin, contexto_close, contexto_state, and contexto_search "
        "for one session over stdio. Only protocol messages are written to stdout.",
    )
    parser.add_argument("--session", required=True, help="session id")
    parser.add_argument("--home", type=Path, default=None,
                        help="data directory (default: $CONTEXTO_HOME, else ~/.contexto)")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from contexto.mcp import serve_stdio
    from contexto.store import SessionStore

    home = args.home or contexto_home()  # the same resolution as the other subcommands (B24)
    return serve_stdio(SessionStore(home, args.session))
