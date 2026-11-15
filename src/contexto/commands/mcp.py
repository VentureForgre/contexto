"""`contexto mcp`: serve session stores to Claude Code or Codex over stdio (MCP), in one of two modes.

``--session S`` serves one session. The session must exist: ``contexto run`` creates it before the
CLI spawns this server, and a server started for a mistyped id would otherwise create an empty
session that later became the default for ``state`` and ``recall`` (D57).

``--serve`` is for clients of ``contexto serve``, which Contexto does not launch: the server is
registered once in the user's Claude Code configuration, starts with no session, and finds the
session of each tool call by asking the service (``contexto.mcp.ServeServer``, D70). It needs neither
a session nor the service to start and to list its tools, and it never creates a session either.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, TYPE_CHECKING

from contexto.paths import absolute_home, contexto_home, session_dir

if TYPE_CHECKING:
    from contexto.mcp import ServeServer
    from contexto.store import SessionStore


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "mcp",
        help="run the MCP server on stdio, for one session or for the service's sessions (the CLI spawns this)",
        description="Serve contexto_recall, contexto_pin, contexto_close, contexto_state, and contexto_search "
        "over stdio. Give exactly one of --session and --serve. With --session ID it serves that one existing "
        "session; `contexto run` starts it this way for Claude Code and Codex. With --serve it serves the "
        "sessions of a running `contexto serve`: each tool call goes to the session of the conversation that "
        "made it, and fails with a message when that session cannot be found. Only protocol messages are "
        "written to stdout.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--session", help="session id (the full id of an existing session; a prefix is refused)")
    mode.add_argument("--serve", action="store_true",
                      help="find each tool call's session by asking the running `contexto serve`; for an MCP "
                      "server registered in Claude Code's own configuration")
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
    from contexto.mcp import UnopenedStore, serve_stdio
    from contexto.store import LockFileDamagedError, SessionStore
    from contexto.store.sessions import named, starting_with

    # The same resolution as the other subcommands, and --home under the same rule (B24): a client that
    # writes --home=~/y without a shell gets the expanded absolute path, not a literal ./~/y.
    home = absolute_home(args.home) if args.home else contexto_home()
    if args.serve:  # no session and no service needed to start: the tools must always list
        return serve_server(home, args.search_cache_mb, sys.stdout, sys.stderr).serve(sys.stdin)
    try:
        # lexists: an entry that is there but cannot be opened (a dangling link) gets the store's own error
        if not os.path.lexists(session_dir(home, args.session)):
            # The CLI's MCP config carries the full id, so a prefix here is a hand-typed one: it is not
            # resolved (the server would write another session's log and ledger), and the error names
            # the session it starts, as `run` and `prune` do (D64).
            starts = starting_with(home, args.session)
            raise ValueError(f"no session {args.session!r} under {home / 'sessions'}"
                             + (f" (it is the start of {named(starts)}; mcp needs the full id)" if starts else ""))
        store = SessionStore(home, args.session)
    except LockFileDamagedError as exc:  # each tool call says so, naming the file (D59)
        print(f"contexto mcp: {exc}", file=sys.stderr)
        return serve_stdio(UnopenedStore(args.session, exc))
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


class ExistingSessions:
    """The sessions under one home, for ``contexto mcp --serve``: opened only when they already exist
    as real directories, each with the search text budget ``--session`` mode gives its one store (the
    flag, else ``CONTEXTO_SEARCH_CACHE_MB``, else the default; per store, so the server holds at most
    ``contexto.mcp.STORE_CACHE`` times that)."""

    def __init__(self, home: Path, search_cache: int | None) -> None:
        self.home = home
        self.search_cache = search_cache

    def check(self, session_id: str) -> None:
        """``SessionUnavailable`` unless ``session_id`` is a valid id naming an existing session directory
        that is not a symlink. Nothing is created or changed, so it is also run before every call on a
        store that is already open: a session deleted since is refused, not written back."""
        from contexto.mcp import SessionUnavailable
        from contexto.store import refuse_symlinked_session

        try:
            path = session_dir(self.home, session_id)
        except ValueError:
            raise SessionUnavailable("it is not a valid session id") from None
        try:
            refuse_symlinked_session(path)
        except OSError:
            raise SessionUnavailable("its directory is a symlink, and Contexto uses only real directories") from None
        if not os.path.lexists(path):  # as --session mode: the MCP server never creates a session (D57)
            raise SessionUnavailable(f"there is no such session under {self.home / 'sessions'}")

    def open(self, session_id: str) -> SessionStore:
        """The session's store, opened without creating anything (``SessionStore(create=False)``), so a
        session removed after ``check``, or pruned while the open waits for its lock, is refused rather
        than made again. ``SessionUnavailable`` as ``check``, or when opening fails;
        ``LockFileDamagedError`` passes through, since its reply names the file to delete (D59)."""
        from contexto.mcp import SessionUnavailable
        from contexto.store import LockFileDamagedError, SessionNotFoundError, SessionRemovedError, SessionStore

        self.check(session_id)
        try:
            store = SessionStore(self.home, session_id, create=False)
        except LockFileDamagedError:
            raise
        except SessionRemovedError:
            raise SessionUnavailable("it was removed while it was being opened") from None
        except SessionNotFoundError:
            raise SessionUnavailable(f"there is no such session under {self.home / 'sessions'}") from None
        except (OSError, ValueError) as exc:  # the class only: the text can hold paths and stored values
            raise SessionUnavailable(f"opening it failed with {type(exc).__name__}") from None
        store.set_search_cache(self.search_cache if self.search_cache is not None else env_budget(store))
        return store


def serve_server(home: Path, search_cache: int | None, stdout: IO[str], stderr: IO[str], *,
                 environ: Mapping[str, str] | None = None, cwd: Path | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> ServeServer:
    """The ``--serve`` server for ``home``: it asks the service named by ``home``'s ``serve.json`` and
    ``serve.token`` (``contexto.serveclient``) and opens sessions under ``home``. ``environ`` (default
    the process environment) and ``cwd`` (default the working directory) are read only for the
    provider switches that make Claude Code bypass the service (``contexto.mcp.provider_note``; the
    user's home is ``environ``'s ``HOME``); ``clock`` and ``sleep`` drive the retry, so a test does not
    wait."""
    from contexto import serveclient
    from contexto.mcp import ServeServer, provider_note

    def resolve(tool_use_id: str) -> str:
        return serveclient.resolve(home, tool_use_id, clock=clock, sleep=sleep)

    environ = os.environ if environ is None else environ
    user_home = Path(environ["HOME"]) if environ.get("HOME") else Path.home()
    return ServeServer(resolve, ExistingSessions(home, search_cache), stdout, stderr,
                       provider=provider_note(environ, cwd=cwd or Path.cwd(), home=user_home))
