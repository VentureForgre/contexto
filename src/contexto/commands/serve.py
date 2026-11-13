"""``contexto serve``: run the engine as a standing local service for clients Contexto does not launch.

Listens on ``127.0.0.1`` only, at a fixed port (7837 unless ``--port``), until SIGINT, SIGTERM or
SIGHUP. Prints ``contexto serve: listening on 127.0.0.1:<port> (bound N, upstream <source>)`` on
stderr once it is listening; the upstream URL and the token are never printed there. The engine
options mean what they mean for ``contexto run`` and apply to every session. See ``contexto.serve``
for the token, the routing, the files it writes and its limits, and ``docs/serve.md`` for use.
"""

from __future__ import annotations

import argparse

from contexto.launcher import BOUND_GUARD, DEFAULT_BOUND

MAX_PORT = 65535


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(f"invalid port {text!r}: use 1 to {MAX_PORT} (0 picks a free port)")
    return port


def register(subparsers: argparse._SubParsersAction) -> None:
    from contexto.serve import DEFAULT_IDLE_MINUTES, DEFAULT_MAX_SESSIONS, DEFAULT_PORT

    parser = subparsers.add_parser(
        "serve", help="run the engine as a local service for clients Contexto does not launch",
        description="Run the Contexto engine as a standing service on 127.0.0.1 for clients Contexto does not "
                    "launch (IDE extensions, the desktop app, the Agent SDK, claude -p in CI): each takes the "
                    "service's URL as ANTHROPIC_BASE_URL. Every request must carry the service's token, which is "
                    "the first path segment of that URL. Each Claude Code session gets its own session store. "
                    "Serves until SIGINT, SIGTERM or SIGHUP.",
        epilog="Exit codes: 0 after a clean shutdown, a print, a rotation, or a --status that found the service; "
               "1 the service cannot start, the token file is refused, or --status found no service; 2 usage "
               "error.")
    parser.add_argument("--port", type=_port, default=None,
                        help=f"port on 127.0.0.1 (default {DEFAULT_PORT}; 0 picks a free port)")
    parser.add_argument("--upstream", default=None,
                        help="upstream base URL (default: what Claude Code would use from this directory; "
                             "required once that is this service's own URL)")
    parser.add_argument("--bound", type=int, default=DEFAULT_BOUND,
                        help=f"upstream prompt bound in tokens, for every session (default {DEFAULT_BOUND:,}; "
                             f"above {BOUND_GUARD:,} needs --allow-large-bound)")
    parser.add_argument("--allow-large-bound", action="store_true",
                        help=f"allow --bound above {BOUND_GUARD:,} (Claude Code may then compact on its own)")
    parser.add_argument("--window-fraction", type=float, default=None,
                        help="share of the window budget kept as recent turns after a rotation (default 0.5)")
    parser.add_argument("--headroom-floor", type=int, default=None,
                        help="minimum headroom below the bound that triggers a rotation (default 25,000; "
                             "must be below the bound)")
    parser.add_argument("--observe-thinking", action="store_true",
                        help="add the thinking-binding beta so dropped thinking is reported and logged "
                             "(changes the request: one extra anthropic-beta flag)")
    parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS,
                        help=f"sessions open at once (default {DEFAULT_MAX_SESSIONS}); the least recently used "
                             "one is closed to make room, and reopened by its next request")
    parser.add_argument("--idle-minutes", type=float, default=DEFAULT_IDLE_MINUTES,
                        help=f"close a session idle this long (default {DEFAULT_IDLE_MINUTES:g}); its next "
                             "request reopens it")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--print-settings", action="store_true",
                       help="print a Claude Code settings JSON for a client (the base URL with the token, hint "
                            "headers on, allow rules for Contexto's tools) and exit; it holds the token")
    modes.add_argument("--print-mcp-config", action="store_true",
                       help="print an MCP config JSON that starts Contexto's MCP server for a client, and exit; "
                            "it holds no token")
    modes.add_argument("--rotate-token", action="store_true",
                       help="replace the token and exit; a running service must be restarted and every client "
                            "given the new URL")
    modes.add_argument("--status", action="store_true",
                       help="say whether the service is running, with its port and open sessions; exit 0 if "
                            "it is, 1 if not")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from contexto.serve import run as run_serve

    return run_serve(args)
