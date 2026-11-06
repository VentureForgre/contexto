"""`contexto mcp --session S`: serve one session's store to Claude Code or Codex over stdio (MCP).

The session must exist: ``contexto run`` creates it before the CLI spawns this server, and a server
started for a mistyped id would otherwise create an empty session that later became the default for
``state`` and ``recall`` (D57).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from contexto.paths import absolute_home, contexto_home, session_dir


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "mcp",
        help="run the MCP server for a session on stdio (the CLI spawns this)",
        description="Serve contexto_recall, contexto_pin, contexto_close, contexto_state, and contexto_search "
        "for one existing session over stdio. `contexto run` starts it for Claude Code and Codex. Only "
        "protocol messages are written to stdout.",
    )
    parser.add_argument("--session", required=True, help="session id (the exact id of an existing session)")
    parser.add_argument("--home", type=Path, default=None,
                        help="data directory (default: $CONTEXTO_HOME, else ~/.contexto)")
    parser.add_argument("--search-cache-mb", type=_megabytes, default=None, metavar="MB",
                        help="page text held in memory for search, in MB (default: $CONTEXTO_SEARCH_CACHE_MB, "
                        "else 64; 0 holds none). Pages beyond it are read from the store at each search.")
    parser.set_defaults(func=run)


def _megabytes(value: str) -> int:
    from contexto.store.catalog import text_budget_bytes

    try:
        return text_budget_bytes(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number of megabytes, 0 or more; got {value!r}") from None


def run(args: argparse.Namespace) -> int:
    from contexto.mcp import serve_stdio
    from contexto.store import SessionStore

    # The same resolution as the other subcommands, and --home under the same rule (B24): a client that
    # writes --home=~/y without a shell gets the expanded absolute path, not a literal ./~/y.
    home = absolute_home(args.home) if args.home else contexto_home()
    try:
        # lexists: an entry that is there but cannot be opened (a dangling link) gets the store's own error
        if not os.path.lexists(session_dir(home, args.session)):
            raise ValueError(f"no session {args.session!r} under {home / 'sessions'}")
        store = SessionStore(home, args.session)
    except (OSError, ValueError) as exc:  # stderr only: stdout carries the protocol
        print(f"contexto mcp: cannot start: {exc}", file=sys.stderr)
        return 1
    store.set_search_cache(args.search_cache_mb if args.search_cache_mb is not None else env_budget(store))
    return serve_stdio(store)


def env_budget(store) -> int:
    """The search text budget from ``CONTEXTO_SEARCH_CACHE_MB``. An invalid value is logged
    (``search_cache_invalid``) and the default used: a CLI that cannot start its MCP server reports
    nothing, so the server must not refuse to start over it."""
    from contexto.store.catalog import ENV_VAR, text_budget_bytes

    value = os.environ.get(ENV_VAR)
    try:
        return text_budget_bytes(value)
    except ValueError:
        store.log({"event": "search_cache_invalid", "value": str(value)[:100]})
        return text_budget_bytes(None)
