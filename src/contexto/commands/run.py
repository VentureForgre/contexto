"""``contexto run claude|codex [args...]``: run Claude Code or Codex through the Contexto proxy.

Contexto options go before ``claude``; everything after ``claude`` is passed to Claude Code
unchanged. See ``contexto.launcher`` for what the launcher does, its limits, and exit codes.
"""

from __future__ import annotations

import argparse
import sys

from contexto.launcher import BOUND_GUARD, DEFAULT_BOUND, options_from_args, run_claude


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "run", help="run Claude Code with its prompt kept under a bound",
        description="Run Claude Code with every model request going through a local Contexto proxy "
                    "that keeps the upstream prompt under --bound. Contexto options go before "
                    "'claude'; everything after 'claude' is passed to Claude Code unchanged.",
        epilog="Exit codes: Claude Code's own code; 128+N if it died from signal N; 2 usage error; "
               "1 proxy failed to start; 127 'claude' not found on PATH; 130 Ctrl+C during the version check.")
    parser.add_argument("--bound", type=int, default=DEFAULT_BOUND,
                        help=f"upstream prompt bound in tokens (default {DEFAULT_BOUND:,}; "
                             f"above {BOUND_GUARD:,} needs --allow-large-bound)")
    parser.add_argument("--allow-large-bound", action="store_true",
                        help=f"allow --bound above {BOUND_GUARD:,} (Claude Code may then compact on its own)")
    parser.add_argument("--window-fraction", type=float, default=None,
                        help="share of the window budget kept as recent turns after a rotation (default 0.5)")
    parser.add_argument("--headroom-floor", type=int, default=None,
                        help="minimum headroom below the bound that triggers a rotation (default 25,000)")
    parser.add_argument("--session", default=None, help="session id (default: a new one, printed at start)")
    parser.add_argument("--upstream", default=None,
                        help="upstream base URL (default: what Claude Code would use from this directory)")
    parser.add_argument("--observe-thinking", action="store_true",
                        help="add the thinking-binding beta so dropped thinking is reported and logged "
                             "(changes the request: one extra anthropic-beta flag)")
    parser.add_argument("--no-version-check", action="store_true",
                        help="do not compare the CLI's --version with the versions Contexto was tested with "
                             "(also CONTEXTO_NO_VERSION_CHECK=1)")
    parser.add_argument("cli", choices=["claude", "codex"], help="the CLI to run")
    parser.add_argument("cli_args", nargs=argparse.REMAINDER, help="arguments passed to the CLI unchanged")
    try:
        from contexto.codex import add_arguments  # lane A packet A5 (builder-2)
    except ImportError:
        pass
    else:
        add_arguments(parser)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.cli == "codex":
        try:
            from contexto.codex import run_codex
        except ImportError:
            print("contexto run: codex support is not installed in this build", file=sys.stderr)
            return 2
        return run_codex(args)
    return run_claude(options_from_args(args))
