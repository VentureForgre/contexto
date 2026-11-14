"""``contexto proxy``: run the Contexto proxy alone, mainly for tests.

Prints ``listening on 127.0.0.1:<port> session <id> upstream <source>`` once bound, then serves
until interrupted. The upstream URL itself is not printed or logged, only which source won.
Request rewriting (the engine) and the launcher are wired in by ``contexto run``; this command
forwards with the identity transform. A session this command creates is removed again if the proxy
cannot start (D57).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

MAX_PORT = 65535


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(f"invalid port {text!r}: use 0 to {MAX_PORT} (0 picks a free port)")
    return port


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("proxy", help="run the local proxy alone (for tests)",
                                   description="Run the Contexto proxy alone, without the rewrite engine or a "
                                               "CLI, mainly for tests. Prints the port, session, and upstream "
                                               "source once listening, then serves until interrupted.")
    parser.add_argument("--session", default=None,
                        help="full session id to resume or create; a prefix of an existing id is refused "
                             "(default: a new one)")
    parser.add_argument("--port", type=_port, default=0, help="port on 127.0.0.1 (default 0: a free port)")
    parser.add_argument("--upstream", default=None,
                        help="upstream base URL (default: what Claude Code would use from this directory)")
    parser.add_argument("--observe-thinking", action="store_true",
                        help="add the thinking-binding beta so dropped thinking is reported and logged "
                             "(changes the request: one extra anthropic-beta flag)")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from contexto.launcher import LaunchError, open_session, session_affinity_key
    from contexto.paths import contexto_home, session_dir
    from contexto.proxy import ContextoProxy
    from contexto.store.prune import discard_new_session
    from contexto.upstream import resolve_upstream, untrusted_note

    session_id = args.session if args.session is not None else uuid.uuid4().hex
    upstream = resolve_upstream(args.upstream, cwd=Path.cwd())
    if upstream.ignored:  # CLAUDE_CODE_IGNORE_PERSISTED_TRUST is set: no project base URL (D52)
        note = untrusted_note("env.ANTHROPIC_BASE_URL", upstream.ignored, "Pass --upstream", os.environ)
        print(f"contexto proxy: {note}", file=sys.stderr)
    try:
        created = not session_dir(contexto_home(), session_id).exists()
    except ValueError as exc:  # an id no session can have ('' included): a usage error, as in `run` (D57)
        print(f"contexto proxy: {exc}", file=sys.stderr)
        return 2
    store = None
    try:
        # Through the launcher's check (D57): an id that only starts existing sessions is refused, so a new
        # session under it does not hide the one meant from the readers.
        store = open_session(contexto_home(), session_id, Path.cwd())
        proxy = ContextoProxy(upstream.url, store, observe_thinking=args.observe_thinking,
                              upstream_source=upstream.source, affinity_key=session_affinity_key(store, upstream.url))
        port = proxy.start(args.port)
    except LaunchError as exc:
        print(f"contexto proxy: {exc}", file=sys.stderr)
        return exc.code
    except (OSError, ValueError) as exc:
        print(f"contexto proxy: cannot start: {exc}", file=sys.stderr)
        if store is not None and created:
            discard_new_session(store)
        return 1
    print(f"listening on 127.0.0.1:{port} session {session_id} upstream {upstream.source}", flush=True)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.stop()
    return 0
