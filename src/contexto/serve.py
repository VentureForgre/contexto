"""``contexto serve``: the engine as a standing local service for clients Contexto does not launch (D70).

``contexto run claude`` starts one proxy for one launch. A client Contexto does not start (an IDE
extension, the desktop app, the Agent SDK, a CI job running ``claude -p``) cannot use that, but each
accepts ``ANTHROPIC_BASE_URL``. ``contexto serve`` listens on ``127.0.0.1`` at a fixed port (7837 by
default) until it is stopped, and gives every Claude Code session its own session store and engine.

What Claude Code sends a base URL (measured on 2.1.322 against a stub, zero spend;
``docs/research/D70-serve-probes.md``): it keeps a path prefix of ``ANTHROPIC_BASE_URL`` on every
request (``HEAD /<prefix>/api/hello``, ``POST /<prefix>/v1/messages?beta=true``,
``POST /<prefix>/v1/messages/count_tokens?beta=true``, ``GET /<prefix>/v1/models?limit=1000``), sends
``Host: 127.0.0.1:<port>`` or ``localhost:<port>`` and never ``Origin``, sends
``X-Claude-Code-Session-Id`` on every POST (not on the HEAD or the GET), and ``metadata.user_id`` (a
JSON string with ``session_id``) on ``/v1/messages`` only.

Token. ``serve`` requires a token on every request: 32 random bytes (``secrets.token_urlsafe``) in
``$CONTEXTO_HOME/serve.token``, mode 0600, created on first use. It is the first path segment of the
base URL, ``http://127.0.0.1:<port>/<token>``, so every client that takes a base URL can carry it.
The raw first segment of the request target is compared in constant time (``hmac.compare_digest``),
with no percent-decoding and no path normalisation; a request without it gets the proxy's one 403
(``REFUSED_MESSAGE``), after the Host and Origin checks of D53. The prefix is then removed, so the
path forwarded upstream, every log entry and every error is the path without the token. The token
protects the service's port: without it a request is neither forwarded upstream nor routed to a
store, and a local endpoint does not answer. It is as private as the file it is copied into.

Routing. A ``POST /v1/messages`` or ``/v1/messages/count_tokens`` is routed by Claude Code's session
id: the ``X-Claude-Code-Session-Id`` header (``CLI_SESSION_ID``), else ``metadata.user_id``'s
``session_id``. A header and a body id that differ, an id that is not a plain id, and a request with
no id at all (the HEAD, the GET, Codex's ``/responses``, anything else) are forwarded unchanged
through the identity transform and logged in the service log; they never reach a store. A teammate
that runs as its own process has its own id and names the lead's in ``parent_session_id`` (D50): its
requests go to the lead's session, and the service remembers child -> parent so the teammate's
``count_tokens`` (header only, no ``metadata``) follows. Subagents and side requests carry their main
conversation's id, so they reach its engine, whose own rules (D50) attach them.

At the first request of an id the service reuses the Contexto session that last served that Claude
Code session (``served_sessions``: a session ``contexto run claude`` made counts, so a conversation
keeps its pages when the user moves between the launcher and a ``serve`` client), else makes a new
one named ``uuid.uuid4().hex``. A string a client sent never names a directory. Only a ``POST
/v1/messages`` makes a session; a ``count_tokens`` for an id no session served is forwarded
unchanged. The id is recorded in the session's metadata as the launcher does (``SessionRecorder``),
so ``contexto run claude --resume <id>`` finds a session ``serve`` made. A session ``serve`` makes
records no working directory: the service does not run in the client's directory, so pinned file
sources are not checked for staleness (``staleness_checked`` is false, as for any session without
one) and ``contexto run claude --continue`` does not pick the session by directory; a session the
launcher made keeps the directory it recorded.

Each open session has its own engine, transform lock and session log: the handler takes one
unstarted ``ContextoProxy`` per session as its ``owner`` for the request, so degraded mode, the
stream hold and fail-open are the launcher path's own code. At most ``max_sessions`` sessions are
open; one idle for ``idle_s`` is closed (recorder flushed, ``report_missing_tools``, logged, store
released), and the least recently used one is closed to make room. A session with a request in
flight is never closed: when every open session is busy, a request for another id is forwarded
unchanged and logged (``session_cap``). A later request reopens a closed session and its engine
resumes from the epoch record (contract rule 16).

Local endpoints, under the token prefix and never forwarded: ``GET /contexto/health`` and ``POST
/contexto/resolve``. Claude Code 2.1.322 puts the model's ``tool_use`` id in each MCP ``tools/call``
(``_meta["claudecode/toolUseId"]``) and nothing else that names the session, so the MCP server asks
``resolve`` which Contexto session a tool call belongs to. The map (the newest ``TOOL_CALLS_KEPT``
ids) is filled from the ids the proxy reads off each response, in the usage callback: the stream
hold runs that callback before it releases a tool call to the client, so an id resolves before the
client can act on it.

Files under ``$CONTEXTO_HOME``, all 0600: ``serve.token``; ``serve.json`` (``{"port", "pid"}``,
written once listening, removed on a clean shutdown); ``serve.log`` (JSON lines: events, reasons,
counts, paths without the token, status codes; never header values, bodies, the token or the
upstream URL), rotated once to ``serve.log.1`` at 5 MB.

Limits: loopback only, one user, Claude Code's Messages API only. Another local process that takes
the port while the service is down receives whatever a client then sends, the token included.
"""

from __future__ import annotations

import errno
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import signal
import stat
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contexto import __version__
from contexto.canonical import loads_bounded
from contexto.launcher import (
    CLAUDE_SESSION_HEADER,
    CLI_SESSION_ID,
    DEFAULT_BOUND,
    EXIT_PROXY,
    EXIT_USAGE,
    LaunchError,
    LaunchOptions,
    NoticeLog,
    SessionRecorder,
    assumed_overhead,
    build_engine,
    check_home,
    check_mcp_values,
    check_options,
    claude_session,
    contexto_tool_rules,
    engine_config,
    report_missing_tools,
    served_sessions,
    session_affinity_key,
    start_warning,
)
from contexto.mcp import SERVER_NAME as MCP_SERVER_NAME
from contexto.paths import contexto_home, mcp_env
from contexto.private import open_private, private_dir, read_regular, write_all, write_atomic, write_new
from contexto.proxy import (
    COUNT_TOKENS_PATH,
    LISTEN_HOST,
    MAX_DECODED_BODY_BYTES,
    MESSAGES_PATH,
    REFUSED_MESSAGE,
    STOP_DRAIN_S,
    ContextoProxy,
    EncodedBodyError,
    _Handler,
    _LoopbackServer,
    _takes_keyword,
    decode_request_body,
    utc_now,
)
from contexto.store import SessionStore, redact
from contexto.upstream import JS_WHITESPACE, TRUTHY, Upstream, resolve_upstream, untrusted_note

DEFAULT_PORT = 7837
DEFAULT_MAX_SESSIONS = 64
DEFAULT_IDLE_MINUTES = 60.0
TOKEN_FILE = "serve.token"
STATE_FILE = "serve.json"
LOG_FILE = "serve.log"
MAX_LOG_BYTES = 5 * 1024 * 1024  # serve.log is renamed to serve.log.1 before a write would pass this
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")  # token_urlsafe(32) is 43 of these characters
ROTATE_HINT = "Run `contexto serve --rotate-token` to replace it"
REFUSAL_LOG_INTERVAL_S = 1.0  # at most one refusal line this often; the rest are counted (``not_logged``)
TOOL_CALLS_KEPT = 8192  # tool-use ids the resolve endpoint remembers, newest kept
PARENTS_KEPT = 4096  # child session id -> parent session id pairs remembered (agent-team teammates)
PARENT_DEPTH = 8  # how far a chain of parents is followed (a teammate's teammate)
UNSERVED_KEPT = 1024  # ids a count_tokens found no session for, remembered for UNSERVED_TTL_S
UNSERVED_TTL_S = 5.0  # /context sends 26 count_tokens in a row (2.1.322): one lookup on disk, not 26
LOCAL_PREFIX = "/contexto"
HEALTH_PATH = LOCAL_PREFIX + "/health"
RESOLVE_PATH = LOCAL_PREFIX + "/resolve"
MAX_LOCAL_BODY_BYTES = 64 * 1024
MAX_TOOL_USE_ID_CHARS = 256
STATUS_TIMEOUT_S = 3.0
# Claude Code with one of these set sends nothing to ANTHROPIC_BASE_URL, not even HEAD /api/hello
# (measured on 2.1.322; each has its own base-URL variable).
CLOUD_PROVIDER_VARIABLES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                            "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS")
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class ServeError(Exception):
    """``contexto serve`` cannot do what was asked. The message never holds the token."""

    def __init__(self, message: str, code: int = EXIT_PROXY) -> None:
        super().__init__(message)
        self.code = code


# The token file


def token_path(home: Path) -> Path:
    return Path(home) / TOKEN_FILE


def read_token(home: Path) -> str:
    """The token in ``home/serve.token``. The file is opened without following a symlink and without
    blocking on a FIFO, and must be a regular file of the current user with no group or other
    permission bit, holding one token; anything else raises ``ServeError`` naming the remedy."""
    path = token_path(home)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):  # EMLINK on FreeBSD
            raise ServeError(f"{path} is a symlink, and the token file is never read through one. "
                             f"{ROTATE_HINT}") from None
        raise ServeError(f"cannot read {path}: {exc.strerror}. {ROTATE_HINT}") from None
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise ServeError(f"{path} is not a regular file. Remove it, then {ROTATE_HINT[0].lower()}"
                             f"{ROTATE_HINT[1:]}")
        if status.st_uid != os.geteuid():
            raise ServeError(f"{path} belongs to another user. {ROTATE_HINT}")
        if stat.S_IMODE(status.st_mode) & 0o077:
            raise ServeError(f"{path} has mode {stat.S_IMODE(status.st_mode):04o}: the token file must be "
                             f"readable by its owner only (0600). {ROTATE_HINT}")
        data = os.read(fd, 1024)
    except OSError as exc:
        raise ServeError(f"cannot read {path}: {exc.strerror}. {ROTATE_HINT}") from None
    finally:
        os.close(fd)
    token = data.decode("ascii", "replace").strip()
    if not TOKEN_PATTERN.fullmatch(token):
        raise ServeError(f"{path} does not hold a token. {ROTATE_HINT}")
    return token


def ensure_token(home: Path) -> str:
    """The token, created first if ``home/serve.token`` does not exist: written to a private temporary
    file (0600 from its first byte) and linked into place, so two first uses agree on one token."""
    path = token_path(home)
    if not os.path.lexists(path):
        try:
            private_dir(Path(home), narrow=False)
            write_new(path, (secrets.token_urlsafe(32) + "\n").encode("ascii"))
        except OSError as exc:
            raise ServeError(f"cannot create {path}: {exc.strerror or type(exc).__name__}") from None
    return read_token(home)


def rotate_token(home: Path) -> str:
    """Replace the token: a new private file is renamed over ``home/serve.token``, so whatever stood
    there (a symlink, a file with wide permissions) is replaced, never followed or rewritten."""
    path = token_path(home)
    try:
        private_dir(Path(home), narrow=False)
        write_atomic(path, (secrets.token_urlsafe(32) + "\n").encode("ascii"))
    except OSError as exc:
        raise ServeError(f"cannot replace {path}: {exc.strerror or type(exc).__name__}") from None
    return read_token(home)


def base_url(port: int, token: str) -> str:
    """What a client takes as ``ANTHROPIC_BASE_URL``: the token is the first path segment."""
    return f"http://{LISTEN_HOST}:{port}/{token}"


def settings_payload(port: int, token: str) -> dict[str, Any]:
    """A Claude Code settings file for a ``serve`` client: the base URL with the token, the gateway
    hint headers on (off by default for a custom base URL; Claude Code 2.1.273 and later), and an
    allow rule for each of Contexto's own MCP tools (D44)."""
    return {"env": {"ANTHROPIC_BASE_URL": base_url(port, token), "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},
            "permissions": {"allow": contexto_tool_rules()}}


def mcp_config_payload(home: Path) -> dict[str, Any]:
    """An MCP config for a ``serve`` client. It holds no token and no session: ``contexto mcp --serve``
    reads the token file itself and asks the service which session a tool call belongs to. ``-P``
    keeps the client's project directory off ``sys.path``, as in the launcher's config."""
    return {"mcpServers": {MCP_SERVER_NAME: {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-P", "-m", "contexto", "mcp", "--serve"],
        "env": mcp_env(home),
    }}}


# The state file


def read_state(home: Path) -> dict[str, int] | None:
    """``home/serve.json`` as ``{"port", "pid"}``, or None when it is absent or not such a record."""
    try:
        state = json.loads(read_regular(Path(home) / STATE_FILE))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    port, pid = state.get("port"), state.get("pid")
    if not all(type(v) is int for v in (port, pid)) or not 0 < port <= 65535 or pid <= 0:
        return None
    return {"port": port, "pid": pid}


def _process_runs(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # another user's process
    except (OverflowError, ValueError, OSError):
        return False
    return True


def running_state(home: Path) -> dict[str, int] | None:
    """The record of a service whose process still runs, or None. A service that was killed leaves its
    record behind; its pid is then gone (or, rarely, reused by another process)."""
    state = read_state(home)
    return state if state is not None and _process_runs(state["pid"]) else None


# Guards


def cloud_provider_variables(env: Mapping[str, str] | None = None) -> list[str]:
    """The cloud-provider switches ``env`` sets (``CLOUD_PROVIDER_VARIABLES``), read as Claude Code
    reads a boolean variable: ``1``, ``true``, ``yes`` or ``on``, any case, trimmed."""
    env = os.environ if env is None else env
    return [name for name in CLOUD_PROVIDER_VARIABLES if env.get(name, "").lower().strip(JS_WHITESPACE) in TRUTHY]


def cloud_provider_warning(names: list[str], what: str) -> str:
    return (f"warning: {', '.join(names)} {'is' if len(names) == 1 else 'are'} set in {what}. Claude Code with a "
            f"cloud-provider switch sends nothing to ANTHROPIC_BASE_URL, so a client started that way does not "
            f"reach contexto serve and its prompt is not bounded.")


def _is_loopback(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified  # 0.0.0.0 connects to this machine too


def check_upstream_loop(upstream: Upstream, port: int, token: str) -> None:
    """Refuse an upstream that is the service itself: a loopback address on the service's own port, or
    a URL that carries the service's token. A user who set Claude Code up for ``serve`` has the
    service's URL as ``ANTHROPIC_BASE_URL``, which is also where the default upstream is read from, so
    without this the service would forward every request to itself. The URL is not printed: it can
    hold the token."""
    try:
        parts = urlsplit(upstream.url)
        target = parts.port or (443 if parts.scheme == "https" else 80)
        host = parts.hostname or ""
    except ValueError:  # malformed; check_upstream_url reports it, unless it carries the token
        host, target = "", None
    carries_token = token in upstream.url
    if not carries_token and not (_is_loopback(host) and target == port):
        return
    what = "carries this service's token" if carries_token else f"is a loopback address on this service's port {port}"
    raise ServeError(
        f"the upstream (from {upstream.source}) {what}, so every request would come back to contexto serve. "
        f"That is the ANTHROPIC_BASE_URL a client of this service is given; the service itself needs the real "
        f"upstream: pass --upstream URL (for example --upstream https://api.anthropic.com)", EXIT_USAGE)


def check_upstream_url(upstream: Upstream) -> None:
    """A malformed upstream is a usage error naming where it came from. Unlike the launcher's check it
    does not quote the URL, which a service's stderr may keep for a long time."""
    try:
        parts = urlsplit(upstream.url)
        _ = parts.port  # parsed on access; raises for a bad one
        ok, problem = parts.scheme in ("http", "https") and bool(parts.hostname), "it must be an http(s) base URL"
    except ValueError as exc:
        ok, problem = False, str(exc)
    if not ok:
        raise ServeError(f"invalid upstream (from {upstream.source}): {problem}", EXIT_USAGE)


# The service log


class ServeLog:
    """``$CONTEXTO_HOME/serve.log``: one JSON line per event, content-free like a session log. The file
    is 0600 and never opened through a symlink. Before a write would take it past ``MAX_LOG_BYTES`` it
    is renamed to ``serve.log.1`` (replacing the last one), so the two hold at most twice that. The
    token is never written: nothing logs it, and a line that would hold it is written with
    ``[token]`` in its place. A log that cannot be written never fails a request."""

    def __init__(self, home: Path, token: str) -> None:
        self.home = Path(home)
        self.path = self.home / LOG_FILE
        self.token = token
        self._lock = threading.Lock()

    def log(self, event: dict[str, Any]) -> None:
        try:
            record = redact({"ts": utc_now(), **event})
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
            if self.token and self.token in line:
                line = line.replace(self.token, "[token]")
            data = line.encode("utf-8", "backslashreplace") + b"\n"
            with self._lock:
                private_dir(self.home, narrow=False)
                fd = open_private(self.path, os.O_WRONLY | os.O_APPEND)
                try:
                    if os.fstat(fd).st_size + len(data) > MAX_LOG_BYTES:
                        os.close(fd)
                        fd = -1
                        os.replace(self.path, self.path.with_name(LOG_FILE + ".1"))
                        fd = open_private(self.path, os.O_WRONLY | os.O_APPEND)
                    write_all(fd, data)
                finally:
                    if fd >= 0:
                        os.close(fd)
        except Exception:  # a broken log must never break a request
            pass


class SessionNotices(NoticeLog):
    """The launcher's ``NoticeLog`` for one session of the service: every event goes to the session's
    own log, and the warnings the launcher prints (bound too small, MCP tools missing, tools denied)
    go to the service's stderr at once, each with the Contexto session id in front."""

    def __init__(self, store: Any, stderr, session_id: str) -> None:
        super().__init__(store, stderr)
        self.session_id = session_id

    def _once(self, key: str, message: str) -> None:
        if key in self.shown:
            return
        self.shown.add(key)
        print(f"contexto serve: session {self.session_id}: {message}", file=self.stderr, flush=True)


def build_session_proxy(upstream: Upstream, log: Any, transform: Callable[..., Any],
                        observe_usage: Callable[..., Any] | None, *, observe_thinking: bool,
                        affinity_key: str | None) -> ContextoProxy:
    """The one place a session's proxy is built. It is never started: the service's handler uses it as
    its ``owner`` for a request of that session (transform, lock, usage callback, log, upstream).
    A ``ContextoProxy`` option that every ``serve`` session should have (such as one that strips the
    gated hint headers before forwarding) is passed here."""
    return ContextoProxy(upstream.url, log, transform, observe_usage=observe_usage,
                         observe_thinking=observe_thinking, upstream_source=upstream.source,
                         affinity_key=affinity_key)


def body_session(request: Any) -> tuple[str | None, str | None]:
    """(session id, parent session id) from a Messages request's ``metadata.user_id``, each None when
    absent or not a plain id (``CLI_SESSION_ID``). The parent is looked for only when the string names
    one: an agent-team teammate running as its own process (D50)."""
    session = claude_session({}, request)
    metadata = request.get("metadata") if isinstance(request, dict) else None
    user_id = metadata.get("user_id") if isinstance(metadata, dict) else None
    if not isinstance(user_id, str) or "parent_session_id" not in user_id:
        return session, None
    try:
        fields = loads_bounded(user_id)
    except ValueError:
        return session, None
    parent = fields.get("parent_session_id") if isinstance(fields, dict) else None
    return session, parent if isinstance(parent, str) and CLI_SESSION_ID.fullmatch(parent) else None


class _Session:
    """One open Contexto session: its store, and the unstarted proxy that owns its requests."""

    def __init__(self, session_id: str, store: Any, proxy: ContextoProxy, recorder: SessionRecorder,
                 now: float) -> None:
        self.session_id = session_id
        self.store = store
        self.proxy = proxy
        self.recorder = recorder
        self.keys: set[str] = set()  # the Claude Code session ids routed here
        self.leases = 0  # requests routed here and not yet finished
        self.last_used = now
        self.requests = 0


class _Unrouted(Exception):
    """A request that could have had a session and is forwarded unchanged instead; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ServeService:
    """The service: one listening socket, a registry of open sessions, and the service log.

    ``config`` is the engine configuration every session gets (``launcher.engine_config``).
    ``engine_factory`` builds a session's engine (``launcher.build_engine``), ``clock`` is the
    monotonic clock idle times are measured on, and ``reaper`` starts the thread that closes idle
    sessions; tests replace the first two and turn the third off."""

    def __init__(self, home: Path, upstream: Upstream, token: str, *, config: dict[str, int | float] | None = None,
                 observe_thinking: bool = False, max_sessions: int = DEFAULT_MAX_SESSIONS,
                 idle_s: float = DEFAULT_IDLE_MINUTES * 60, stderr=None,
                 engine_factory: Callable[..., Any] = build_engine,
                 clock: Callable[[], float] = time.monotonic, reaper: bool = True) -> None:
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("not a serve token")
        self.home = Path(home)
        self.upstream = upstream
        self.token = token
        self.token_bytes = token.encode("ascii")
        self.config = dict(config) if config else {"bound": DEFAULT_BOUND}
        self.observe_thinking = observe_thinking
        self.max_sessions = max_sessions
        self.idle_s = idle_s
        self.stderr = stderr or sys.stderr
        self.engine_factory = engine_factory
        self.clock = clock
        self.reaper = reaper  # close idle sessions from a background thread (tests call reap themselves)
        self.log = ServeLog(self.home, token)
        # Requests that belong to no session: forwarded with the identity transform, logged in serve.log.
        self.passthrough = ContextoProxy(upstream.url, self.log, upstream_source=upstream.source)
        self.port = 0
        self._lock = threading.Lock()  # the registry: held to look up or change it, never across I/O
        self._sessions: dict[str, _Session] = {}  # Contexto session id -> open session
        self._routes: dict[str, str] = {}  # Claude Code session id -> Contexto session id, for open sessions
        self._parents: OrderedDict[str, str] = OrderedDict()  # child Claude Code session id -> its parent's
        self._unserved: OrderedDict[str, float] = OrderedDict()  # id -> when a count_tokens found no session
        self._guards: dict[tuple[str, str], list[Any]] = {}  # per-id locks for resolving and opening
        self._opening = 0  # sessions being opened: they count toward max_sessions
        self._stopping = False
        self._tool_calls: OrderedDict[str, str] = OrderedDict()  # tool-use id -> Contexto session id
        self._tool_lock = threading.Lock()
        self._in_flight = 0  # requests being handled, local endpoints included
        self._idle = threading.Condition()
        self._refusal_lock = threading.Lock()
        self._refused_at: float | None = None
        self._refusals_not_logged = 0
        self._warned: set[str] = set()
        self._server: _LoopbackServer | None = None
        self._thread: threading.Thread | None = None
        self._reaper_stop = threading.Event()
        self._state_written = False

    # Lifecycle

    def start(self, port: int = DEFAULT_PORT) -> int:
        """Bind 127.0.0.1:port (0 picks a free port), serve in a thread, write ``serve.json``, and return
        the bound port. OSError when the port cannot be bound or the record cannot be written."""
        service = self

        class Handler(_ServeHandler):
            pass

        Handler.service = service
        server = _ServeServer((LISTEN_HOST, port), Handler)
        server.service = service
        server.daemon_threads = True
        self.port = server.server_address[1]
        try:
            private_dir(self.home, narrow=False)
            write_atomic(self.home / STATE_FILE, json.dumps({"port": self.port, "pid": os.getpid()}).encode())
        except OSError:
            server.server_close()
            raise
        self._state_written = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="contexto-serve", daemon=True)
        self._thread.start()
        if self.reaper:
            threading.Thread(target=self._reap_loop, name="contexto-serve-reaper", daemon=True).start()
        self.log.log({"event": "serve_start", "port": self.port, "pid": os.getpid(), "version": __version__,
                      "upstream_source": self.upstream.source, "config": self.config,
                      "observe_thinking": self.observe_thinking, "max_sessions": self.max_sessions,
                      "idle_s": self.idle_s})
        return self.port

    def stop(self, *, hurry: Callable[[], bool] | None = None, signal_name: str | None = None) -> None:
        """Stop accepting connections, wait up to ``STOP_DRAIN_S`` for requests in flight (``hurry``
        returning true ends the wait, as a second signal does), close every session, remove
        ``serve.json`` and log ``serve_stop``."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._reaper_stop.set()
        deadline = time.monotonic() + STOP_DRAIN_S
        with self._idle:
            try:
                while self._in_flight and (left := deadline - time.monotonic()) > 0 and not (hurry and hurry()):
                    self._idle.wait(min(left, 0.05))
            except KeyboardInterrupt:
                pass  # a second Ctrl+C ends the wait early, with no traceback
            in_flight = self._in_flight
        with self._lock:
            self._stopping = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._routes.clear()
        for session in sessions:
            self._close(session, "shutdown")
        self._remove_state()
        with self._refusal_lock:
            not_logged, self._refusals_not_logged = self._refusals_not_logged, 0
        event: dict[str, Any] = {"event": "serve_stop", "in_flight": in_flight, "sessions_closed": len(sessions)}
        if signal_name:
            event["signal"] = signal_name
        if not_logged:
            event["refusals_not_logged"] = not_logged
        self.log.log(event)

    def _remove_state(self) -> None:
        """Remove ``serve.json`` if it is still this process's record."""
        if not self._state_written:
            return
        self._state_written = False
        state = read_state(self.home)
        if state is not None and state["pid"] == os.getpid():
            try:
                os.unlink(self.home / STATE_FILE)
            except OSError:
                pass

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {"ok": True, "version": __version__, "port": self.port, "sessions": len(self._sessions)}

    def say(self, message: str) -> None:
        print(f"contexto serve: {message}", file=self.stderr, flush=True)

    def _warn_once(self, key: str, message: str) -> None:
        with self._lock:
            if key in self._warned:
                return
            self._warned.add(key)
        self.say(message)

    # Refusals

    def refused(self, reason: str) -> None:
        """Log a refused request by its reason (``origin``, ``host`` or ``token``), never its path, and
        at most once every ``REFUSAL_LOG_INTERVAL_S``: the ones in between are counted and the count
        goes on the next line, so a local process cannot grow the log without limit."""
        now = self.clock()
        with self._refusal_lock:
            if self._refused_at is not None and 0 <= now - self._refused_at < REFUSAL_LOG_INTERVAL_S:
                self._refusals_not_logged += 1
                return
            self._refused_at = now
            not_logged, self._refusals_not_logged = self._refusals_not_logged, 0
        event: dict[str, Any] = {"event": "request_refused", "reason": reason}
        if not_logged:
            event["not_logged"] = not_logged
        self.log.log(event)

    # The tool-call map

    def register_tool_calls(self, tool_call_ids: tuple[str, ...], session_id: str) -> None:
        """Remember which session each client tool call belongs to, keeping the newest ``TOOL_CALLS_KEPT``."""
        if not tool_call_ids:
            return
        with self._tool_lock:
            for tool_use_id in tool_call_ids:
                if isinstance(tool_use_id, str) and 0 < len(tool_use_id) <= MAX_TOOL_USE_ID_CHARS:
                    self._tool_calls[tool_use_id] = session_id
                    self._tool_calls.move_to_end(tool_use_id)
            while len(self._tool_calls) > TOOL_CALLS_KEPT:
                self._tool_calls.popitem(last=False)

    def resolve(self, tool_use_id: str) -> str | None:
        """The Contexto session a tool-use id was issued in, or None."""
        with self._tool_lock:
            return self._tool_calls.get(tool_use_id)

    def _registering(self, session_id: str, observe_usage: Callable[..., Any] | None) -> Callable[..., Any]:
        """A session's usage callback: the ids of the response's client tool calls go into the map first,
        then the engine's own callback runs. The proxy calls it under the session's lock before the
        stream hold releases a tool call to the client (``StreamHold.on_trigger``), so the id resolves
        by the time the client can start the call."""
        takes_calls = _takes_keyword(observe_usage, "tool_calls")
        takes_ids = _takes_keyword(observe_usage, "tool_call_ids")

        def observe(endpoint: str, usage: dict[str, Any], request: dict[str, Any], tool_calls: bool | None = None,
                    tool_call_ids: tuple[str, ...] = ()) -> Any:
            self.register_tool_calls(tool_call_ids, session_id)
            if observe_usage is None:
                return []
            extra: dict[str, Any] = {}
            if takes_calls:
                extra["tool_calls"] = tool_calls
            if takes_ids:
                extra["tool_call_ids"] = tool_call_ids
            return observe_usage(endpoint, usage, request, **extra)

        return observe

    # The registry

    @contextmanager
    def _guarded(self, kind: str, key: str) -> Iterator[None]:
        """Hold the lock for one id (a Claude Code session id being resolved, or a Contexto session being
        opened or closed), so two first requests of one id open one store while other ids go on."""
        with self._lock:
            guard = self._guards.setdefault((kind, key), [threading.Lock(), 0])
            guard[1] += 1
        try:
            with guard[0]:
                yield
        finally:
            with self._lock:
                guard[1] -= 1
                if not guard[1]:
                    self._guards.pop((kind, key), None)

    def _root(self, cli_session: str, parent: str | None) -> str:
        """The Claude Code session whose store serves ``cli_session``: itself, or the parent it named
        now or in an earlier request, followed up the chain."""
        with self._lock:
            if parent is not None and parent != cli_session:
                self._parents[cli_session] = parent
                self._parents.move_to_end(cli_session)
                while len(self._parents) > PARENTS_KEPT:
                    self._parents.popitem(last=False)
            key = cli_session
            for _ in range(PARENT_DEPTH):
                above = self._parents.get(key)
                if above is None:
                    break
                key = above
            return key

    def _leased(self, key: str) -> _Session | None:
        with self._lock:
            session = self._sessions.get(self._routes.get(key, ""))
            if session is not None:
                session.leases += 1
                session.last_used = self.clock()
            return session

    def _last_served(self, key: str) -> str | None:
        """The Contexto session that most recently served Claude Code session ``key``, from the stores."""
        matches = [found for found in served_sessions(self.home) if found[2] == key]
        return max(matches, key=lambda found: found[0])[1] if matches else None

    def acquire(self, cli_session: str, parent: str | None = None, *, create: bool) -> _Session | None:
        """The open session for a request of Claude Code session ``cli_session``, leased until
        ``release``. Opens it at first sight (see the module docstring); ``create`` allows a new
        session when none served the id. None when there is no session and none may be made;
        ``_Unrouted`` when one cannot be opened."""
        key = self._root(cli_session, parent)
        session = self._leased(key)
        if session is not None:
            return session
        with self._guarded("cli", key):
            session = self._leased(key)
            if session is not None:
                return session
            if self._stopping:
                raise _Unrouted("stopping")
            if not create and self._recently_unserved(key):
                return None
            session_id = self._last_served(key)
            created = session_id is None
            if session_id is None:
                if not create:
                    self._note_unserved(key)
                    return None
                session_id = uuid.uuid4().hex
            with self._guarded("session", session_id):
                with self._lock:
                    session = self._sessions.get(session_id)
                    if session is not None:  # open already, for another id it served
                        self._lease_new_route(session, key)
                        return session
                reserved, victim = self._reserve()
                if victim is not None:
                    self._close(victim, "cap")
                if not reserved:
                    self._warn_once("cap", f"warning: all {self.max_sessions} sessions have a request in flight, "
                                           f"so a request of another session was forwarded unchanged (its prompt "
                                           f"is not bounded). Raise --max-sessions.")
                    raise _Unrouted("session_cap")
                try:
                    session = self._open(session_id, created)
                except Exception as exc:
                    with self._lock:
                        self._opening -= 1
                    self.log.log({"event": "session_open_failed", "session": session_id,
                                  "error": type(exc).__name__, "errno": getattr(exc, "errno", None)})
                    self._warn_once("open:" + type(exc).__name__,
                                    f"warning: cannot open a session store ({type(exc).__name__}); its requests "
                                    f"are forwarded unchanged, and their prompts are not bounded.")
                    raise _Unrouted("session_open_failed") from None
                with self._lock:
                    self._opening -= 1
                    self._sessions[session_id] = session
                    self._lease_new_route(session, key)
                    self._unserved.pop(key, None)
                    count = len(self._sessions)
                self.log.log({"event": "session_open", "session": session_id, "created": created, "open": count})
                return session

    def _lease_new_route(self, session: _Session, key: str) -> None:
        """Under the registry lock: route ``key`` to ``session`` and lease it."""
        self._routes[key] = session.session_id
        session.keys.add(key)
        session.leases += 1
        session.last_used = self.clock()

    def release(self, session: _Session) -> None:
        with self._lock:
            session.leases -= 1
            session.requests += 1
            session.last_used = self.clock()

    def _recently_unserved(self, key: str) -> bool:
        with self._lock:
            seen = self._unserved.get(key)
            return seen is not None and 0 <= self.clock() - seen < UNSERVED_TTL_S

    def _note_unserved(self, key: str) -> None:
        with self._lock:
            self._unserved[key] = self.clock()
            self._unserved.move_to_end(key)
            while len(self._unserved) > UNSERVED_KEPT:
                self._unserved.popitem(last=False)

    def _reserve(self) -> tuple[bool, _Session | None]:
        """Take a place for a session about to open: (True, None) when there is room, (True, victim)
        when the least recently used session without a request in flight was taken out of the registry
        to make room (the caller closes it), (False, None) when every open session is in use."""
        with self._lock:
            if len(self._sessions) + self._opening < self.max_sessions:
                self._opening += 1
                return True, None
            idle = [session for session in self._sessions.values() if session.leases == 0]
            if not idle:
                return False, None
            victim = min(idle, key=lambda session: session.last_used)
            self._remove(victim)
            self._opening += 1
            return True, victim

    def _remove(self, session: _Session) -> None:
        """Under the registry lock: take ``session`` out, so no new request is routed to it."""
        self._sessions.pop(session.session_id, None)
        for key in session.keys:
            if self._routes.get(key) == session.session_id:
                del self._routes[key]

    def _open(self, session_id: str, created: bool) -> _Session:
        """Open the store and build its engine and proxy, as ``launcher.start_proxy`` does for a launch.
        No working directory is recorded (see the module docstring)."""
        store = SessionStore(self.home, session_id)
        try:
            transform, observe_usage, engine_name = self.engine_factory(store, self.config)
            recorder = SessionRecorder(transform, store)
            proxy = build_session_proxy(self.upstream, SessionNotices(store, self.stderr, session_id), recorder,
                                        self._registering(session_id, observe_usage),
                                        observe_thinking=self.observe_thinking,
                                        affinity_key=session_affinity_key(store, self.upstream.url))
            store.log({"event": "serve_open", "cli": "claude", "session": session_id, "created": created,
                       "config": self.config, "engine": engine_name, "upstream_source": self.upstream.source,
                       "observe_thinking": self.observe_thinking,
                       "affinity_header": proxy.affinity_header is not None})
        except BaseException:
            store.release()
            raise
        return _Session(session_id, store, proxy, recorder, self.clock())

    def _close(self, session: _Session, reason: str) -> None:
        """Close a session already taken out of the registry: the launcher's exit-time work that applies
        (the missing-tools report, the recorder's last-seen times), a log line in both logs, and the
        store's lock released. A request that reopens the session meanwhile waits for this."""
        with self._guarded("session", session.session_id):
            for step in (lambda: report_missing_tools(session.proxy), session.recorder.flush,
                         lambda: session.store.log({"event": "serve_close", "reason": reason,
                                                    "requests": session.requests})):
                try:
                    step()
                except Exception:
                    pass
            self.log.log({"event": "session_close", "session": session.session_id, "reason": reason,
                          "requests": session.requests})
            try:
                session.store.release()
            except Exception:
                pass

    def reap(self) -> int:
        """Close every session idle for ``idle_s`` with no request in flight; return how many."""
        now = self.clock()
        with self._lock:
            idle = [session for session in self._sessions.values()
                    if session.leases == 0 and now - session.last_used >= self.idle_s]
            for session in idle:
                self._remove(session)
        for session in idle:
            self._close(session, "idle")
        return len(idle)

    def _reap_loop(self) -> None:
        interval = min(60.0, max(1.0, self.idle_s / 4))
        while not self._reaper_stop.wait(interval):
            try:
                self.reap()
            except Exception as exc:
                self.log.log({"event": "reap_error", "error": type(exc).__name__})


class _ServeServer(_LoopbackServer):
    """The proxy's loopback server, with a failure outside a request (socketserver's default prints a
    traceback) logged by its type only, through the refusal limiter."""

    service: ServeService

    def handle_error(self, request: Any, client_address: Any) -> None:
        self.service.refused("connection_error")


class _ServeHandler(_Handler):
    """The proxy's handler with a front door: the Host and Origin checks, the token, the local
    endpoints, and the choice of the request's ``owner`` (its session's proxy, or the service's
    pass-through one). Everything after that is ``_Handler``'s own code."""

    service: ServeService

    def _handle(self) -> None:
        service = self.service
        with service._idle:
            service._in_flight += 1
        try:
            self._serve()
        except Exception as exc:  # never the path: before the prefix is removed it holds the token
            service.log.log({"event": "serve_error", "method": self.command, "error": type(exc).__name__})
            self.close_connection = True
        finally:
            with service._idle:
                service._in_flight -= 1
                service._idle.notify_all()

    def _serve(self) -> None:
        service = self.service
        self._body: bytes | ValueError | None = None  # the request body, read once
        self._parsed: tuple[bytes, Any] | None = None  # the body the route parsed, and its value or error
        reason = self._refusal() or self._take_token()
        if reason is not None:
            service.refused(reason)
            self._discard_body()  # closing on unread bytes sends a reset, which can discard the 403
            self._send_local_error(403, REFUSED_MESSAGE, "permission_error")
            self.close_connection = True
            return
        path_only = self.path.split("?", 1)[0]
        if path_only == LOCAL_PREFIX or path_only.startswith(LOCAL_PREFIX + "/"):
            self._local(path_only)
            return
        session: _Session | None = None
        try:
            session = self._route(path_only)
        except _Unrouted as unrouted:
            service.log.log({"event": "unrouted", "reason": unrouted.reason, "method": self.command,
                             "path": path_only})
        except Exception as exc:  # routing must never cost the client its request: forward it unchanged
            service.log.log({"event": "unrouted", "reason": "route_error", "error": type(exc).__name__,
                             "method": self.command, "path": path_only})
        self.owner = session.proxy if session is not None else service.passthrough
        try:
            self._handle_counted()
        finally:
            if session is not None:
                service.release(session)

    def _take_token(self) -> str | None:
        """``"token"`` unless the request target starts with ``/<token>/``; else None, with the prefix
        removed from ``self.path``. The target is read from the request line as it was sent:
        ``http.server`` has already collapsed leading slashes in ``self.path``, and nothing here
        percent-decodes or normalises it. The first segment is compared in constant time. A target
        that holds the token again after the prefix is refused too, so the token is never part of a
        path or query sent upstream."""
        words = self.requestline.split()
        target = words[1] if len(words) >= 2 else ""
        segment, slash, rest = target[1:].partition("/")
        try:
            given = segment.encode("ascii")
        except UnicodeEncodeError:
            given = b""
        matches = hmac.compare_digest(given, self.service.token_bytes)
        if not (matches and slash and target.startswith("/")):
            return "token"
        if self.service.token in rest:  # a doubled prefix, or the token in the query: it would be forwarded
            return "token"
        path = "/" + rest
        if path.startswith("//"):  # as http.server does for a request without a prefix
            path = "/" + path.lstrip("/")
        self.path = path
        return None

    def _discard_body(self) -> None:
        """Read and drop a refused request's body, at most ``MAX_DECODED_BODY_BYTES`` of it, keeping none."""
        left = MAX_DECODED_BODY_BYTES

        def drop(size: int) -> bool:
            nonlocal left
            while size > 0 and left > 0:
                chunk = self.rfile.read(min(size, 65536, left))
                if not chunk:
                    return False
                size -= len(chunk)
                left -= len(chunk)
            return size == 0

        try:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                while left > 0:
                    size = int(self.rfile.readline(1024).split(b";", 1)[0].strip() or b"0", 16)
                    if size <= 0 or not drop(size):
                        return
                    self.rfile.readline(1024)
            else:
                drop(int(self.headers.get("Content-Length") or 0))
        except (ValueError, OSError):
            pass

    def _read_body(self) -> bytes:
        """The request body, read once: the route reads it, and ``_Handler`` gets the same bytes (or the
        same framing error, which it answers with its 400)."""
        if self._body is None:
            try:
                self._body = super()._read_body()
            except ValueError as exc:
                self._body = exc
        if isinstance(self._body, ValueError):
            raise self._body
        return self._body

    def _parse(self, body: bytes) -> Any:
        """The body as JSON, parsed once: the route's result when ``body`` is what it parsed."""
        if self._parsed is not None and (self._parsed[0] is body or self._parsed[0] == body):
            if isinstance(self._parsed[1], ValueError):
                raise self._parsed[1]
            return self._parsed[1]
        return super()._parse(body)

    def _route(self, path_only: str) -> _Session | None:
        """The session this request belongs to (leased), or None for a request that has none. Raises
        ``_Unrouted`` for a request that names a session and cannot have it."""
        if self.command != "POST" or path_only not in (MESSAGES_PATH, COUNT_TOKENS_PATH):
            return None
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            return None  # _Handler refuses it
        given = self.headers.get_all(CLAUDE_SESSION_HEADER) or []
        if len(given) > 1 or (given and not CLI_SESSION_ID.fullmatch(given[0])):
            raise _Unrouted("invalid_session_id")
        header_id = given[0] if given else None
        try:
            body = self._read_body()
        except ValueError:
            return None  # _Handler answers 400
        encoding = (self.headers.get("Content-Encoding") or "").strip().lower()
        if encoding not in ("", "identity"):
            try:
                body = decode_request_body(body, encoding, MAX_DECODED_BODY_BYTES)
            except EncodedBodyError:
                return None  # _Handler answers 415
        try:
            request = loads_bounded(body)
        except ValueError as exc:
            self._parsed, request = (body, exc), None
        else:
            self._parsed = (body, request)
        body_id, parent = body_session(request)
        if header_id is not None and body_id is not None and header_id != body_id:
            raise _Unrouted("session_id_mismatch")
        cli_session = header_id or body_id
        if cli_session is None:
            return None
        creates = path_only == MESSAGES_PATH and isinstance(request, dict)
        session = self.service.acquire(cli_session, parent, create=creates)
        if session is None:
            raise _Unrouted("no_session")
        return session

    # Local endpoints

    def _local(self, path_only: str) -> None:
        service = self.service
        body = self._local_body()
        if body is None:
            return
        if path_only == HEALTH_PATH:
            if self.command != "GET":
                return self._json(405, {"error": "method_not_allowed"})
            return self._json(200, service.health())
        if path_only == RESOLVE_PATH:
            if self.command != "POST":
                return self._json(405, {"error": "method_not_allowed"})
            try:
                request = loads_bounded(body)
            except ValueError:
                request = None
            tool_use_id = request.get("tool_use_id") if isinstance(request, dict) else None
            if not isinstance(tool_use_id, str) or not 0 < len(tool_use_id) <= MAX_TOOL_USE_ID_CHARS:
                return self._json(400, {"error": "invalid_request"})
            session_id = service.resolve(tool_use_id)
            if session_id is None:
                return self._json(404, {"error": "unknown_tool_use"})
            return self._json(200, {"session": session_id})
        return self._json(404, {"error": "not_found"})

    def _local_body(self) -> bytes | None:
        """A local endpoint's body, at most ``MAX_LOCAL_BODY_BYTES``; None after answering 413 or 400."""
        try:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                raise OverflowError  # no client of these endpoints sends one; refused, not parsed
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError
            if length > MAX_LOCAL_BODY_BYTES:
                raise OverflowError
            return self.rfile.read(length) if length else b""
        except OverflowError:
            self._json(413, {"error": "body_too_large"})
        except ValueError:
            self._json(400, {"error": "invalid_request"})
        self.close_connection = True
        return None

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            self.close_connection = True


# The command


class _StopSignals:
    """SIGINT, SIGTERM and SIGHUP for the command. The first one ends ``wait``; a later one only sets
    ``again``, which ends the shutdown's wait for requests in flight. The handler raises only while
    ``wait`` is sleeping, so nothing else is ever interrupted part-way. A SIGHUP that was ignored when
    the command started (``nohup``) stays ignored."""

    class _Wake(BaseException):
        pass

    def __init__(self) -> None:
        self.received: int | None = None
        self.again = False
        self._waiting = False
        self._previous: dict[int, Any] = {}

    def install(self) -> None:
        for signum in STOP_SIGNALS:
            try:
                if signum == signal.SIGHUP and signal.getsignal(signum) is signal.SIG_IGN:
                    continue  # started under nohup: a hangup stays ignored
                self._previous[signum] = signal.signal(signum, self._handle)
            except ValueError:  # not the main thread
                break

    def restore(self) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)

    def _handle(self, signum: int, frame: Any) -> None:
        if self.received is not None:
            self.again = True
            return
        self.received = signum
        if self._waiting:
            raise self._Wake

    def wait(self) -> int:
        try:
            self._waiting = True
            while self.received is None:
                time.sleep(3600)
        except self._Wake:
            pass
        finally:
            self._waiting = False
        return self.received or signal.SIGTERM


def _chosen_port(args: Any, home: Path) -> int:
    """``--port`` when given; else the port of a service that is running; else the default."""
    if args.port is not None:
        return args.port
    state = running_state(home)
    return state["port"] if state is not None else DEFAULT_PORT


def print_settings(args: Any, home: Path, stdout, stderr) -> int:
    token = ensure_token(home)
    names = cloud_provider_variables()
    if names:
        print(f"contexto serve: {cloud_provider_warning(names, 'this environment')}", file=stderr)
    print(json.dumps(settings_payload(_chosen_port(args, home), token), indent=2), file=stdout)
    return 0


def print_mcp_config(home: Path, stdout) -> int:
    ensure_token(home)
    try:
        check_mcp_values("serve", home, sys.executable)
    except LaunchError as exc:
        raise ServeError(str(exc), exc.code) from None
    print(json.dumps(mcp_config_payload(home), indent=2), file=stdout)
    return 0


def rotate(home: Path, stdout) -> int:
    rotate_token(home)
    state = running_state(home)
    running = (f"contexto serve is running (pid {state['pid']}) and still expects the old token: restart it. "
               if state is not None else "A running contexto serve must be restarted to use it. ")
    print(f"contexto serve: the token in {token_path(home)} was replaced. {running}Then give every client the new "
          f"URL (contexto serve --print-settings).", file=stdout)
    return 0


def ask_health(port: int, token: str) -> dict[str, Any] | None:
    """The service's health answer, or None when nothing that is the service answers on ``port``."""
    conn = http.client.HTTPConnection(LISTEN_HOST, port, timeout=STATUS_TIMEOUT_S)
    try:
        conn.request("GET", f"/{token}{HEALTH_PATH}")
        response = conn.getresponse()
        data = response.read(MAX_LOCAL_BODY_BYTES)
        health = json.loads(data) if response.status == 200 else None
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        conn.close()
    return health if isinstance(health, dict) and health.get("ok") is True else None


def status(home: Path, stdout) -> int:
    """Whether the service runs: exit 0 with its port and open sessions, else 1. The health endpoint is
    asked only on the port ``serve.json`` names for a process that still runs, so the token is never
    sent to a port the service did not record."""
    token = ensure_token(home)
    state = running_state(home)
    health = ask_health(state["port"], token) if state is not None else None
    if state is None or health is None:
        left = read_state(home)
        detail = ""
        if state is not None:
            detail = (f" (pid {state['pid']} is recorded for port {state['port']}, but it did not answer with this "
                      f"token: restart the service if the token was rotated)")
        elif left is not None:
            detail = f" ({STATE_FILE} is left from pid {left['pid']}, which no longer runs)"
        print(f"contexto serve: not running{detail}", file=stdout)
        return 1
    count = health.get("sessions")
    print(f"contexto serve: running on {LISTEN_HOST}:{state['port']} (pid {state['pid']}, {count} open "
          f"session{'' if count == 1 else 's'}, version {health.get('version')})", file=stdout)
    return 0


def serve(args: Any, home: Path, stderr) -> int:
    signals = _StopSignals()
    signals.install()
    try:
        return _serve(args, home, stderr, signals)
    finally:
        signals.restore()


def _serve(args: Any, home: Path, stderr, signals: _StopSignals) -> int:
    options = LaunchOptions(bound=args.bound, upstream=args.upstream, observe_thinking=args.observe_thinking,
                            allow_large_bound=args.allow_large_bound, window_fraction=args.window_fraction,
                            headroom_floor=args.headroom_floor)
    check_options(options)
    if args.max_sessions < 1:
        raise ServeError(f"--max-sessions must be at least 1, got {args.max_sessions}", EXIT_USAGE)
    if not args.idle_minutes > 0:
        raise ServeError(f"--idle-minutes must be above 0, got {args.idle_minutes}", EXIT_USAGE)
    upstream = resolve_upstream(args.upstream, cwd=Path.cwd())
    token = ensure_token(home)
    port = DEFAULT_PORT if args.port is None else args.port
    check_upstream_loop(upstream, port, token)  # first: a malformed URL's error can quote part of it
    check_upstream_url(upstream)
    try:
        service = ServeService(home, upstream, token, config=engine_config(options),
                               observe_thinking=options.observe_thinking, max_sessions=args.max_sessions,
                               idle_s=args.idle_minutes * 60, stderr=stderr)
    except ValueError as exc:
        raise ServeError(f"invalid upstream (from {upstream.source}): {exc}", EXIT_USAGE) from None
    try:
        port = service.start(port)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise ServeError(f"cannot start on {LISTEN_HOST}:{port}: {exc.strerror or type(exc).__name__}") from None
        state = running_state(home)
        if state is not None and state["port"] == port and state["pid"] != os.getpid():
            raise ServeError(f"contexto serve is already running on {LISTEN_HOST}:{port} "
                             f"(pid {state['pid']})") from None
        raise ServeError(f"port {port} on {LISTEN_HOST} is in use; stop what holds it, or pass --port N") from None
    try:
        check_upstream_loop(upstream, port, token)  # a port the system picked (--port 0)
        print(f"contexto serve: listening on {LISTEN_HOST}:{port} (bound {options.bound:,}, upstream "
              f"{upstream.source})", file=stderr, flush=True)
        if upstream.ignored:  # CLAUDE_CODE_IGNORE_PERSISTED_TRUST is set: no project base URL (D52)
            note = untrusted_note("env.ANTHROPIC_BASE_URL", upstream.ignored, "Pass --upstream", os.environ)
            print(f"contexto serve: {note}", file=stderr, flush=True)
        names = cloud_provider_variables()
        if names:
            print(f"contexto serve: {cloud_provider_warning(names, 'the environment of this service')}",
                  file=stderr, flush=True)
        warning = start_warning(options.bound, options.headroom_floor, assumed_overhead())
        if warning:
            print(f"contexto serve: {warning}", file=stderr, flush=True)
        received = signals.wait()
    except BaseException:
        service.stop()
        raise
    service.stop(hurry=lambda: signals.again, signal_name=signal.Signals(received).name)
    return 0


def run(args: Any, *, stdout=None, stderr=None) -> int:
    """The ``contexto serve`` command. Exit codes: 0 after a clean shutdown (or for a print, a rotation,
    or a running ``--status``); 1 when the service cannot start, the token file is refused, or
    ``--status`` finds no service; 2 for a usage error."""
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    try:
        home = contexto_home()
        check_home(home)
        if args.print_settings:
            return print_settings(args, home, stdout, stderr)
        if args.print_mcp_config:
            return print_mcp_config(home, stdout)
        if args.rotate_token:
            return rotate(home, stdout)
        if args.status:
            return status(home, stdout)
        return serve(args, home, stderr)
    except (ServeError, LaunchError) as exc:
        print(f"contexto serve: {exc}", file=stderr)
        return exc.code
