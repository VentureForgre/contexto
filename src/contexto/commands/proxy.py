"""``contexto proxy``: run the Contexto proxy alone (M2 contract, CLI), mainly for tests.

Prints ``listening on 127.0.0.1:<port> session <id> upstream <source>`` once bound, then serves
until interrupted. The upstream URL itself is not printed or logged, only which source won.
Request rewriting (the engine) and the launcher are wired in by ``contexto run``; this command
forwards with the identity transform.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("proxy", help="run the local proxy alone (for tests)",
                                   description=__doc__.splitlines()[0])
    parser.add_argument("--session", default=None, help="session id (default: a new one)")
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default 0: a free port)")
    parser.add_argument("--upstream", default=None,
                        help="upstream base URL (default: what Claude Code would use from this directory)")
    parser.add_argument("--observe-thinking", action="store_true",
                        help="add the thinking-binding beta so dropped thinking is reported and logged "
                             "(changes the request: one extra anthropic-beta flag)")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from contexto.paths import contexto_home
    from contexto.launcher import session_affinity_key
    from contexto.proxy import ContextoProxy
    from contexto.store import SessionStore
    from contexto.upstream import resolve_upstream

    session_id = args.session or uuid.uuid4().hex
    upstream = resolve_upstream(args.upstream, cwd=Path.cwd())
    try:
        store = SessionStore(contexto_home(), session_id)
        proxy = ContextoProxy(upstream.url, store, observe_thinking=args.observe_thinking,
                              upstream_source=upstream.source, affinity_key=session_affinity_key(store))
        port = proxy.start(args.port)
    except (OSError, ValueError) as exc:
        print(f"contexto proxy: cannot start: {exc}", file=sys.stderr)
        return 1
    print(f"listening on 127.0.0.1:{port} session {session_id} upstream {upstream.source}", flush=True)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.stop()
    return 0
