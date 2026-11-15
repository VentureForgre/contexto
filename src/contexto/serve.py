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
with no percent-decoding and no path normalisation; a request without it gets one 403
(``REFUSED_MESSAGE``). Before the token, ``Host`` must name ``127.0.0.1`` or ``localhost`` and no
``Origin`` may be present, or the request gets the same 403. The prefix is then removed, so the
path forwarded upstream, every log entry and every error is the path without the token; a request
header whose value holds the token is dropped before forwarding. The token protects the service's
port: without it a request is neither forwarded upstream nor routed to a store, and a local endpoint
does not answer. It is as private as the file it is copied into.

Before the token check. Every answer sent before the checks pass is that one 403, byte for byte: a
request ``http.server`` cannot parse (a malformed or over-long request line, a request line without
an HTTP version, an unknown version, too many headers, an unsupported method) gets it too, with no
echo of what was sent and no library version, and ``Expect: 100-continue`` is answered only after
the checks. Every refusal and every failure before the check is logged through one limiter
(``ServeService.refused``). A connection that has not passed the check has ``UNCHECKED_DEADLINE_S``
in total for its request head and for a refused request's body, then its read side is shut. At most
``MAX_UNCHECKED_CONNECTIONS`` such connections are open: one more takes the place of the oldest whose
request line has not shown the token, which is shut (``ServeService._make_room``), so holding the
cap full keeps no token holder out. A connection that passed the check is outside both limits for
the rest of its life.

Routing. A ``POST /v1/messages`` or ``/v1/messages/count_tokens`` is routed by Claude Code's session
id: the ``X-Claude-Code-Session-Id`` header (``CLI_SESSION_ID``), else ``metadata.user_id``'s
``session_id``. A header and a body id that differ, an id that is not a plain id, and a request with
no id at all (the HEAD, the GET, Codex's ``/responses``, anything else) are forwarded unchanged
through the identity transform and logged in the service log; they never reach a store. A
``/v1/messages`` forwarded that way is not bounded, so it is logged as ``unrouted`` with its reason
and stderr gets a warning, at most one per reason per minute, with the count.

A teammate that runs as its own process has its own id and names the lead's in
``parent_session_id`` (D50). A ``/v1/messages`` whose body names a valid parent P is routed to P's
session, and the store that request opens or makes is recorded under P, never under the child's id,
so the lead's own request finds the same store whichever came first. The service remembers child ->
parent in memory only, and only for the child's ``count_tokens`` (header only, no ``metadata``); a
``/v1/messages`` of the child that names no parent goes to the child's own session and ends that
memory. Subagents and side requests carry their main conversation's id, so they reach its engine,
whose own rules (D50) attach them.

At the first request of an id the service reuses the Contexto session that last served that Claude
Code session (``ServedIndex``, read from the stores' ``cli_sessions`` records: a session ``contexto
run claude`` made counts, so a conversation keeps its pages when the user moves between the launcher
and a ``serve`` client), else makes a new one named ``uuid.uuid4().hex``. A string a client sent
never names a directory. Only a ``POST /v1/messages`` makes a session; a ``count_tokens`` for an id
no session served is forwarded unchanged. The id a request was routed by is recorded in the
session's metadata (``RouteRecorder``), so ``contexto run claude --resume <id>`` finds a session
``serve`` made. A store that cannot be opened leaves no new directory behind, and the failure is
remembered for ``OPEN_FAILED_TTL_S`` so the next requests of that id do not repeat it. A session ``serve`` makes
records no working directory: the service does not run in the client's directory, so pinned file
sources are not checked for staleness (``staleness_checked`` is false, as for any session without
one) and ``contexto run claude --continue`` does not pick the session by directory; a session the
launcher made keeps the directory it recorded.

Each open session has its own engine, transform lock and session log: the handler takes one
unstarted ``ContextoProxy`` per session as its ``owner`` for the request, so degraded mode, the
stream hold and fail-open are the launcher path's own code. At most ``max_sessions`` sessions are
open; one idle for ``idle_s`` is closed (recorder flushed, ``report_missing_tools``, logged, store
released), and the least recently used one is closed to make room. A session with a request in
flight is never closed: when every open session is busy, a request for another id waits for one to
finish until ``CAP_WAIT_S`` after it arrived (in total, however many requests of its id were ahead
of it), and is then forwarded unchanged and logged (``session_cap``). A later request reopens a
closed session and its engine resumes from the epoch record (contract rule 16).

Shutdown (``ServeService.stop``): routing stops first, so a request that arrives on a connection
already open gets a 503 (``STOPPING_MESSAGE``) and its connection is closed, and nothing opens or
makes a session; ``serve.json`` is removed while the port is still the service's, then the port is
closed; the requests already in flight have up to ``STOP_DRAIN_S`` to finish, then every session is
closed and the lock released. A store a request made while ``stop`` began is removed again.

Local endpoints, under the token prefix and never forwarded: ``GET /contexto/health`` and ``POST
/contexto/resolve``. Claude Code 2.1.322 puts the model's ``tool_use`` id in each MCP ``tools/call``
(``_meta["claudecode/toolUseId"]``) and nothing else that names the session, so the MCP server asks
``resolve`` which Contexto session a tool call belongs to. The map (the newest ``TOOL_CALLS_KEPT``
ids) is filled by the proxy's ``on_tool_calls`` hook, which hands over the id of each client tool
call as soon as the proxy has read it off the response and before it writes the bytes that carry it:
in a stream it relays event by event and in a stream it relays chunk by chunk. A JSON body is read
only once it has ended, so in either framing (``Content-Length`` or chunked) the proxy holds back
what completes the body (its last chunk, and the whitespace after it) until the ids are handed
over. That holds whether or not the response has ``usage``. So an id resolves before the client can
act on the call whenever the response is not content-encoded. For a response upstream encoded
although the proxy asked for identity, the id is read from a decoded side copy, which may trail the
client's own decoder: there the MCP client's retry covers the gap. A response the proxy cannot read (an encoding
it cannot decode, a JSON body past its limit) registers nothing, and neither does one that answers
a request forwarded unrouted: those calls resolve as unknown and the MCP server refuses them. An id
two sessions registered is ambiguous and resolves as unknown too (``tool_use_ambiguous`` in the log).

Files under ``$CONTEXTO_HOME``, all 0600 (``contexto.servecontract``): ``serve.token``;
``serve.lock`` (empty; the service holds an exclusive ``flock`` on it from before it binds until it
exits, so a second service on the same home is refused whatever its port, and "running" means the
lock is held, never that a recorded pid is alive); ``serve.json`` (``{"port", "pid"}``: a record
left by an earlier run is removed as soon as the lock is taken, the new one is written once the port
is listening and removed before the port is closed, so it names a port only while the service
listens on it; ``pid`` is for messages); ``serve.log`` (JSON lines: events, reasons,
counts, paths without the token, status codes; never header values, bodies, the token or the
upstream URL), rotated once to ``serve.log.1`` at 5 MB.

Limits: loopback only, one user, Claude Code's Messages API only. Another local process that takes
the port while the service is down receives whatever a client then sends, the token included.
"""

from __future__ import annotations

import errno
import hmac
import http.client
import json
import os
import signal
import socket
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contexto import __version__, servecontract
from contexto.canonical import loads_bounded
from contexto.hints import FORWARDED, HINT_HEADERS_KEY, HINT_MARKER_KEY, STRIPPED
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
    in_turn_paging,
    report_missing_tools,
    session_affinity_key,
    start_warning,
)
from contexto.mcp import SERVER_NAME as MCP_SERVER_NAME
from contexto.paths import contexto_home, mcp_env
from contexto.private import open_private, private_dir, write_all, write_atomic, write_new
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
    decode_request_body,
    utc_now,
)
from contexto.servecontract import (
    HEALTH_PATH,
    LOCAL_PREFIX,
    LOCK_FILE,
    MAX_LOCAL_BODY_BYTES,
    MAX_TOOL_USE_ID_CHARS,
    RESOLVE_PATH,
    RESOLVE_UNKNOWN,
    STATE_FILE,
    TOKEN_FILE,
    TOKEN_PATTERN,
    ServeFileError,
    base_url,
    hold_lock,
    is_loopback,
    local_target,
    read_state,
    requested_tool_use,
    resolve_found,
    running_state,
    service_running,
)
from contexto.store import SessionStore, redact
from contexto.store.prune import SessionLiveError, SessionNotFoundError, remove_session
from contexto.upstream import (
    CloudProvider,
    SettingsFiles,
    Upstream,
    cloud_provider,
    cloud_provider_note,
    resolve_upstream,
    untrusted_note,
)

DEFAULT_PORT = 7837
DEFAULT_MAX_SESSIONS = 64
DEFAULT_IDLE_MINUTES = 60.0
LOG_FILE = "serve.log"
MAX_LOG_BYTES = 5 * 1024 * 1024  # serve.log is renamed to serve.log.1 before a write would pass this
ROTATE_HINT = "Run `contexto serve --rotate-token` to replace it"
REFUSAL_LOG_INTERVAL_S = 1.0  # at most one refusal line this often; the rest are counted (``not_logged``)
UNCHECKED_DEADLINE_S = 5.0  # a connection has this long, in total, to pass the token check
MAX_LINGER_BYTES = 1024 * 1024  # read and dropped after the 403 to a request that could not be parsed
MAX_UNCHECKED_CONNECTIONS = 64  # connections that have not passed the token check; one more takes the oldest's place
TOKEN_PEEK_BYTES = 16  # room for the method and " /" in front of the token, when a request line is peeked at
CAP_WAIT_S = 0.3  # how long a request waits for a session to finish before it is forwarded as session_cap
UNBOUNDED_WARN_INTERVAL_S = 60.0  # at most one stderr warning per reason this often, with a count
OPEN_FAILED_TTL_S = 5.0  # how long a store that could not be opened is not tried again
OPEN_FAILED_KEPT = 1024
TOOL_CALLS_KEPT = 8192  # tool-use ids the resolve endpoint remembers, newest kept
PARENTS_KEPT = 4096  # child session id -> parent session id pairs remembered (agent-team teammates)
NOTICES_KEPT = 4096  # sessions whose printed warnings are remembered, so a reopened one does not repeat them
UNSERVED_KEPT = 1024  # ids a count_tokens found no session for, remembered for UNSERVED_TTL_S
UNSERVED_TTL_S = 5.0  # /context sends 26 count_tokens in a row (2.1.322): one lookup on disk, not 26
STATUS_TIMEOUT_S = 3.0
STOPPING_MESSAGE = "contexto serve is stopping; the request was not forwarded"
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# Why a /v1/messages request was forwarded unchanged, for the stderr warning (``ServeService.unrouted``).
UNBOUNDED_REASONS = {
    "session_cap": "every open session had a request in flight; raise --max-sessions",
    "session_open_failed": "its session store could not be opened",
    "session_id_mismatch": "its header and its body name different sessions",
    "invalid_session_id": "its session id is not a plain id",
    "no_session_id": "it carries no session id",
    "route_error": "routing it failed",
}


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
        return servecontract.read_token(home)
    except ServeFileError as exc:
        if exc.kind == "not_regular":
            raise ServeError(f"{path} is not a regular file. Remove it, then {ROTATE_HINT[0].lower()}"
                             f"{ROTATE_HINT[1:]}") from None
        raise ServeError(f"{path} {exc.problem}. {ROTATE_HINT}") from None


def ensure_token(home: Path) -> str:
    """The token, created first if ``home/serve.token`` does not exist: written to a private temporary
    file (0600 from its first byte) and linked into place, so two first uses agree on one token."""
    path = token_path(home)
    if not os.path.lexists(path):
        try:
            private_dir(Path(home), narrow=False)
            write_new(path, (servecontract.new_token() + "\n").encode("ascii"))
        except OSError as exc:
            raise ServeError(f"cannot create {path}: {exc.strerror or type(exc).__name__}") from None
    return read_token(home)


def rotate_token(home: Path) -> str:
    """Replace the token: a new private file is renamed over ``home/serve.token``, so whatever stood
    there (a symlink, a file with wide permissions) is replaced, never followed or rewritten."""
    path = token_path(home)
    try:
        private_dir(Path(home), narrow=False)
        write_atomic(path, (servecontract.new_token() + "\n").encode("ascii"))
    except OSError as exc:
        raise ServeError(f"cannot replace {path}: {exc.strerror or type(exc).__name__}") from None
    return read_token(home)


def settings_payload(port: int, token: str) -> dict[str, Any]:
    """A Claude Code settings file for a ``serve`` client: the base URL with the token, the gateway
    hint headers on (off by default for a custom base URL; Claude Code 2.1.273 and later), and an
    allow rule for each of Contexto's own MCP tools (D44).

    The marker ``CONTEXTO_HINT_HEADERS=stripped`` goes beside the hint setting, as in the launcher's
    ``--settings`` file: the ``1`` is the service's, not the user's. Claude Code applies the block to
    its own environment, and a user may merge it into their settings file, so a ``contexto run
    claude`` that inherits it or reads it there strips the hints rather than forwarding them
    (``contexto.hints.hint_headers_state``). ``stripped`` whatever ``--forward-hint-headers`` says:
    that flag is the service's own choice, and the user's settings still do not turn the hints on."""
    return {"env": {"ANTHROPIC_BASE_URL": base_url(port, token), HINT_HEADERS_KEY: "1", HINT_MARKER_KEY: STRIPPED},
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


# Guards


def cloud_provider_warning(found: CloudProvider) -> str:
    """The warning for what ``contexto.upstream.cloud_provider`` found: the check ``contexto run claude``
    refuses a launch by, read from this process's environment and directory (D70). The service warns
    and does not refuse: its clients are started elsewhere, with their own environment and directory,
    and a Claude Code started here would bypass it."""
    return (f"warning: {cloud_provider_note(found)}. A Claude Code client with a cloud-provider switch on does "
            f"not reach contexto serve, and its prompt is not bounded.")


def check_upstream_loop(upstream: Upstream, port: int, token: str) -> None:
    """Refuse an upstream that is the service itself: this machine (``servecontract.is_loopback``) on
    the service's own port, or a URL that carries the service's token. A user who set Claude Code up
    for ``serve`` has the service's URL as ``ANTHROPIC_BASE_URL``, which is also where the default
    upstream is read from, so without this the service would forward every request to itself. The URL
    is not printed: it can hold the token."""
    try:
        parts = urlsplit(upstream.url)
        target = parts.port or (443 if parts.scheme == "https" else 80)
        host = parts.hostname or ""
    except ValueError:  # malformed; check_upstream_url reports it, unless it carries the token
        host, target = "", None
    carries_token = token in upstream.url
    if not carries_token and not (is_loopback(host) and target == port):
        return
    what = "carries this service's token" if carries_token else f"is a loopback address on this service's port {port}"
    raise ServeError(
        f"the upstream (from {upstream.source}) {what}, so every request would come back to contexto serve. "
        f"That is the ANTHROPIC_BASE_URL a client of this service is given; the service itself needs the real "
        f"upstream: pass --upstream URL (for example --upstream https://api.anthropic.com)", EXIT_USAGE)


def check_upstream_url(upstream: Upstream) -> None:
    """A malformed upstream is a usage error naming where it came from. The URL is not quoted: a
    service's stderr may be kept for a long time."""
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
    own log, and the warnings (bound too small, MCP tools missing, tools denied) go to the service's
    stderr at once, each with the Contexto session id in front. ``shown`` is the service's record for
    the session, so a warning is printed once per session however often the session is closed and
    reopened; the missing-tools report a close makes repeats nothing already printed."""

    def __init__(self, store: Any, stderr, session_id: str, shown: set[str] | None = None) -> None:
        super().__init__(store, stderr)
        self.session_id = session_id
        if shown is not None:
            self.shown = shown

    def _once(self, key: str, message: str) -> None:
        key = "mcp_tools_missing" if key == "mcp_tools_missing_exit" else key
        if key in self.shown:
            return
        self.shown.add(key)
        print(f"contexto serve: session {self.session_id}: {message}", file=self.stderr, flush=True)


class RouteRecorder(SessionRecorder):
    """The launcher's recorder with the service's rule for which id a store records: the id a request
    was routed by (``ServeService.acquire`` calls ``note``), which for a teammate's request is its
    lead's, never the id the request carries. ``flush`` writes the last-seen times."""

    def __call__(self, request: dict[str, Any], headers: dict[str, str], endpoint: str) -> Any:
        return self.transform(request, headers, endpoint)

    def note(self, key: str) -> bool:
        """Record ``key`` as served now; True when this open of the session had not seen it before."""
        first = key not in self.seen
        try:
            self._note(key)
        except Exception:  # bookkeeping for a later lookup; never fail a request for it
            pass
        return first


def build_session_proxy(upstream: Upstream, log: Any, transform: Callable[..., Any],
                        observe_usage: Callable[..., Any] | None, *, observe_thinking: bool,
                        affinity_key: str | None,
                        on_tool_calls: Callable[[tuple[str, ...]], None] | None = None,
                        strip_hint_headers: bool = True) -> ContextoProxy:
    """The one place a session's proxy is built. It is never started: the service's handler uses it as
    its ``owner`` for a request of that session (transform, lock, usage callback, tool-call hook, log,
    upstream). A ``ContextoProxy`` option that every ``serve`` session should have is passed here:
    ``strip_hint_headers`` leaves Claude Code's gated hint headers out of what is sent upstream (D70);
    the engine reads them either way."""
    return ContextoProxy(upstream.url, log, transform, observe_usage=observe_usage,
                         observe_thinking=observe_thinking, upstream_source=upstream.source,
                         affinity_key=affinity_key, on_tool_calls=on_tool_calls,
                         strip_hint_headers=strip_hint_headers)


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


class ServedIndex:
    """Which Contexto session last served each Claude Code session id, from the ``cli_sessions``
    records in the stores' metadata. Built once when the service starts and kept current two ways:
    the service adds what it records itself (``note``), and ``refresh`` re-reads only the stores whose
    ``meta.json`` changed since it was read (one ``stat`` per store), which is how a session another
    Contexto command made or used meanwhile is found."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self._lock = threading.Lock()
        self._last: dict[str, tuple[float, str]] = {}  # Claude Code session id -> (last seen, Contexto session)
        self._read: dict[str, tuple[int, int] | None] = {}  # Contexto session -> its meta.json's stamp when read

    def _stamp(self, name: str) -> tuple[int, int] | None:
        try:
            status = os.stat(self.home / "sessions" / name / "meta.json", follow_symlinks=False)
        except OSError:
            return None
        return status.st_mtime_ns, status.st_size

    def refresh(self) -> None:
        with self._lock:
            try:
                names = os.listdir(self.home / "sessions")
            except OSError:
                names = []
            gone = set(self._read) - set(names)
            if gone:
                for name in gone:
                    del self._read[name]
                self._last = {key: found for key, found in self._last.items() if found[1] not in gone}
            for name in names:
                stamp = self._stamp(name)
                if name in self._read and self._read[name] == stamp:
                    continue
                self._read[name] = stamp
                try:
                    served = SessionStore.open_existing(self.home, name).cli_sessions("claude")
                except Exception:  # a session that cannot be read is skipped: the lookup never fails a request
                    continue
                for key, seen_at in served.items():
                    self._offer(key, seen_at, name)

    def _offer(self, key: str, seen_at: float, name: str) -> None:
        known = self._last.get(key)
        if known is None or seen_at >= known[0]:
            self._last[key] = (seen_at, name)

    def note(self, key: str, name: str, seen_at: float) -> None:
        with self._lock:
            self._offer(key, seen_at, name)

    def lookup(self, key: str) -> str | None:
        with self._lock:
            found = self._last.get(key)
        return found[1] if found is not None else None


class _Session:
    """One open Contexto session: its store, and the unstarted proxy that owns its requests."""

    def __init__(self, session_id: str, store: Any, proxy: ContextoProxy, recorder: RouteRecorder,
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
    monotonic clock idle times and log limits are measured on, and ``reaper`` starts the thread that
    closes idle sessions; tests replace the first two and turn the third off. ``cap_wait_s``,
    ``unchecked_deadline_s`` and ``max_unchecked`` are the limits named in the module docstring."""

    def __init__(self, home: Path, upstream: Upstream, token: str, *, config: dict[str, int | float] | None = None,
                 observe_thinking: bool = False, max_sessions: int = DEFAULT_MAX_SESSIONS,
                 idle_s: float = DEFAULT_IDLE_MINUTES * 60, stderr=None,
                 engine_factory: Callable[..., Any] = build_engine,
                 clock: Callable[[], float] = time.monotonic, reaper: bool = True,
                 cap_wait_s: float = CAP_WAIT_S, unchecked_deadline_s: float = UNCHECKED_DEADLINE_S,
                 max_unchecked: int = MAX_UNCHECKED_CONNECTIONS, forward_hint_headers: bool = False) -> None:
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("not a serve token")
        self.home = Path(home)
        self.upstream = upstream
        self.token = token
        self.token_bytes = token.encode("ascii")
        self.config = dict(config) if config else {"bound": DEFAULT_BOUND}
        self.observe_thinking = observe_thinking
        # D70: the printed settings turn Claude Code's gateway hint headers on for the engine; the six
        # gated ones are removed before anything is forwarded unless --forward-hint-headers asks for them.
        self.forward_hint_headers = forward_hint_headers
        self.max_sessions = max_sessions
        self.idle_s = idle_s
        self.stderr = stderr or sys.stderr
        self.engine_factory = engine_factory
        self.clock = clock
        self.reaper = reaper  # close idle sessions from a background thread (tests call reap themselves)
        self.cap_wait_s = cap_wait_s
        self.unchecked_deadline_s = unchecked_deadline_s
        self.max_unchecked = max_unchecked
        self.log = ServeLog(self.home, token)
        # Requests that belong to no session: forwarded with the identity transform, logged in serve.log.
        self.passthrough = ContextoProxy(upstream.url, self.log, upstream_source=upstream.source,
                                         strip_hint_headers=not forward_hint_headers)
        self.port = 0
        self._lock = threading.Lock()  # the registry: held to look up or change it, never across I/O
        self._freed = threading.Condition(self._lock)  # notified when a session's last request finishes
        self._sessions: dict[str, _Session] = {}  # Contexto session id -> open session
        self._routes: dict[str, str] = {}  # Claude Code session id -> Contexto session id, for open sessions
        self._parents: OrderedDict[str, str] = OrderedDict()  # child Claude Code session id -> its parent's
        self._unserved: OrderedDict[str, float] = OrderedDict()  # id -> when a count_tokens found no session
        self._open_failed: OrderedDict[str, float] = OrderedDict()  # id -> when its store could not be opened
        self._guards: dict[tuple[str, str], list[Any]] = {}  # per-id locks for resolving and opening
        self._opening = 0  # sessions being opened: they count toward max_sessions
        self._cap_waiting = 0  # requests waiting in _reserve for a session to finish
        self._stopping = False
        self._served = ServedIndex(self.home)
        self._shown: OrderedDict[str, set[str]] = OrderedDict()  # Contexto session id -> warnings printed for it
        self._tool_calls: OrderedDict[str, str | None] = OrderedDict()  # tool-use id -> session; None: ambiguous
        self._tool_lock = threading.Lock()
        self._resolved = {"found": 0, "unknown": 0}  # resolve answers given, for health
        self._in_flight = 0  # requests being handled, local endpoints included
        self._idle = threading.Condition()
        self._refusal_lock = threading.Lock()
        self._refused_at: float | None = None
        self._refusals_not_logged = 0
        self._unbounded: dict[str, list[Any]] = {}  # reason -> [when last warned, requests since]
        self._unchecked: dict[socket.socket, float] = {}  # connections before the token check -> their deadline
        self._vouched: set[socket.socket] = set()  # unchecked connections whose request line showed the token
        self._unchecked_changed = threading.Condition()
        self._server: _ServeServer | None = None
        self._thread: threading.Thread | None = None
        self._reaper_stop = threading.Event()
        self._lock_fd: int | None = None

    # Lifecycle

    def start(self, port: int = DEFAULT_PORT) -> int:
        """Take the home's lock, remove a ``serve.json`` left by an earlier run, bind 127.0.0.1:port (0
        picks a free port) and listen, write ``serve.json``, serve in a thread, and return the bound
        port. ``serve.json`` names a port only while this service listens on it.
        ``ServeError`` when another service holds the lock (whatever its port) or the lock file cannot
        be used; OSError when the port cannot be bound or the record cannot be written."""
        service = self
        try:
            lock = hold_lock(self.home)
        except OSError as exc:
            raise ServeError(f"cannot use {self.home / LOCK_FILE} ({exc.strerror or type(exc).__name__}): remove "
                             f"it, then start the service again") from None
        if lock is None:
            raise ServeError(self._already_running())
        try:  # first: a record left by a service that was killed names a port that is not ours
            os.unlink(self.home / STATE_FILE)
        except OSError:
            pass

        class Handler(_ServeHandler):
            pass

        Handler.service = service
        server = None
        try:
            self._served.refresh()
            server = _ServeServer((LISTEN_HOST, port), Handler, bind_and_activate=False)
            server.service = service
            server.daemon_threads = True
            server.server_bind()
            server.server_activate()
            self.port = server.server_address[1]
            write_atomic(self.home / STATE_FILE, json.dumps({"port": self.port, "pid": os.getpid()}).encode())
        except BaseException:
            if server is not None:
                server.server_close()
            os.close(lock)
            raise
        self._lock_fd = lock
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="contexto-serve", daemon=True)
        self._thread.start()
        threading.Thread(target=self._expire_unchecked, name="contexto-serve-deadline", daemon=True).start()
        if self.reaper:
            threading.Thread(target=self._reap_loop, name="contexto-serve-reaper", daemon=True).start()
        self.log.log({"event": "serve_start", "port": self.port, "pid": os.getpid(), "version": __version__,
                      "upstream_source": self.upstream.source, "config": self.config,
                      "observe_thinking": self.observe_thinking, "max_sessions": self.max_sessions,
                      "idle_s": self.idle_s, "hint_headers": FORWARDED if self.forward_hint_headers else STRIPPED})
        return self.port

    def _already_running(self) -> str:
        try:
            state = read_state(self.home)
        except ServeFileError:
            return (f"contexto serve is already running for this Contexto home ({self.home / LOCK_FILE} is held); "
                    f"one service runs per home")
        return (f"contexto serve is already running on {LISTEN_HOST}:{state['port']} (pid {state['pid']}) for this "
                f"Contexto home; one service runs per home")

    def stop(self, *, hurry: Callable[[], bool] | None = None, signal_name: str | None = None) -> None:
        """Stop routing (``_stopping``: a request that arrives from now on, on a connection already
        open, gets a 503 with ``STOPPING_MESSAGE`` and its connection is closed; nothing opens or makes
        a session), remove ``serve.json`` while the port is still this service's, then stop accepting
        connections and close the port, wait up to ``STOP_DRAIN_S`` for the requests already in flight
        (``hurry`` returning true ends the wait, as a second signal does), close every session, release
        the lock and log ``serve_stop``. So whoever binds the port once it is closed is never named by
        ``serve.json``: a caller holds back the token while the lock is held and the record is missing."""
        if self._server is None:
            return
        with self._lock:
            self._stopping = True
            self._freed.notify_all()  # a request waiting at the cap gives up now
        try:
            os.unlink(self.home / STATE_FILE)  # ours: nobody else writes it while the lock is held
        except OSError:
            pass
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._reaper_stop.set()
        deadline = time.monotonic() + STOP_DRAIN_S
        hurried = False
        with self._idle:
            try:
                while self._in_flight and (left := deadline - time.monotonic()) > 0:
                    if hurry is not None and hurry():
                        hurried = True
                        break
                    self._idle.wait(min(left, 0.05))
            except KeyboardInterrupt:
                hurried = True  # a second Ctrl+C ends the wait early, with no traceback
            in_flight = self._in_flight
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._routes.clear()
            self._freed.notify_all()
        for session in sessions:
            self._close(session, "shutdown")
        with self._unchecked_changed:
            self._unchecked.clear()
            self._vouched.clear()
            self._unchecked_changed.notify_all()  # the deadline thread sees _reaper_stop and ends
        self._release_home()
        with self._refusal_lock:
            not_logged, self._refusals_not_logged = self._refusals_not_logged, 0
        event: dict[str, Any] = {"event": "serve_stop", "in_flight": in_flight, "sessions_closed": len(sessions)}
        if signal_name:
            event["signal"] = signal_name
        if hurried:
            event["hurried"] = True  # the wait for requests in flight was cut short
        if not_logged:
            event["refusals_not_logged"] = not_logged
        self.log.log(event)

    def _release_home(self) -> None:
        """Remove this service's ``serve.json`` (``stop`` has already), then let go of the lock."""
        lock, self._lock_fd = self._lock_fd, None
        if lock is None:
            return
        try:
            os.unlink(self.home / STATE_FILE)  # ours: nobody else writes it while the lock is held
        except OSError:
            pass
        try:
            os.close(lock)
        except OSError:
            pass

    def health(self) -> dict[str, Any]:
        """``ok``, the version, the port, the open sessions, and how many resolve requests named a
        session (``found``) and how many were answered unknown (``unknown``): counts only."""
        with self._tool_lock:
            resolved = dict(self._resolved)
        with self._lock:
            return {"ok": True, "version": __version__, "port": self.port, "sessions": len(self._sessions),
                    "resolve": resolved}

    def say(self, message: str) -> None:
        print(f"contexto serve: {message}", file=self.stderr, flush=True)

    # Refusals, and connections that have not passed the token check

    def refused(self, reason: str) -> None:
        """Log a refused request or connection by its reason, never its path, and at most once every
        ``REFUSAL_LOG_INTERVAL_S``: the ones in between are counted and the count goes on the next
        line, so a local process cannot grow the log without limit. Everything that can happen before
        the token check is logged here and nowhere else: ``origin``, ``host``, ``token``, ``malformed``
        (a request ``http.server`` could not parse, or one with no HTTP version), ``error`` (a
        failure while checking), ``deadline``, ``connection_cap``, ``connection_error`` and
        ``accept_error``."""
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

    def admit(self, connection: socket.socket) -> bool:
        """A new connection: give it its deadline. When ``max_unchecked`` connections are already
        waiting to pass the token check, the oldest of them that has not shown the token is shut to
        make room (``_make_room``), so a process without the token cannot keep a token holder out by
        holding the cap full: its own connections are what make way. False only when every waiting
        connection has shown the token; the caller then closes this one at once."""
        with self._unchecked_changed:
            evicted = self._make_room() if len(self._unchecked) >= self.max_unchecked else None
            admitted = len(self._unchecked) < self.max_unchecked
            if admitted:
                self._watch(connection)
        if evicted is not None or not admitted:
            self.refused("connection_cap")
        return admitted

    def _make_room(self) -> socket.socket | None:
        """With the deadline lock held: shut the read side of the unchecked connection nearest its
        deadline (the oldest) whose request line has not shown the token, and stop counting it; its
        handler reads an end of stream and closes, as at its deadline. Return it, or None when every
        one has shown the token.

        Why closing the oldest is enough: a client sends its request line as soon as it connects, so
        the time it spends as the newest connection is the time a connection would need to become the
        oldest of 64, and the line is in the socket from that moment. Whether it is the token is read
        here without taking it from the socket (``shows_token``, a peek), and the handler takes the
        same look before it reads anything (``vouch``), so a request line that has arrived up to the
        slash after the token is recognised whichever of the two looks first. A process without the
        token then cannot close such a connection by opening more of its own, however fast it opens
        them; it can only close its own. Not covered: a request line that arrives in pieces. The
        handler's look sees only the first piece, its buffered read then takes the rest off the
        socket, and this thread's later peek finds nothing, so that connection can be closed here
        while it is the oldest unchecked one (round-3 review, with the cap held full: a 2 ms gap
        between the pieces was answered 25 of 25 times, 5 ms 15 of 25, 30 ms 1 of 23). A client that
        connects and waits before it sends is in the same position (docs/serve.md, "Limits")."""
        for connection, _ in sorted(self._unchecked.items(), key=lambda item: item[1]):
            if connection in self._vouched:
                continue
            if self.shows_token(connection, wait=False):
                self._vouched.add(connection)
                continue
            del self._unchecked[connection]
            try:
                connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass
            return connection
        return None

    def shows_token(self, connection: socket.socket, *, wait: bool) -> bool:
        """Whether the bytes waiting on ``connection`` start a request line whose target's first segment
        is the token, compared in constant time. The bytes are peeked at, never taken from the socket;
        ``wait`` waits for the first of them (the handler's look), else only what has arrived counts. A
        line that has not arrived whole up to the slash after the token counts as not showing it."""
        want = TOKEN_PEEK_BYTES + len(self.token_bytes) + 1
        flags = socket.MSG_PEEK if wait else socket.MSG_PEEK | socket.MSG_DONTWAIT
        try:
            head = connection.recv(want, flags)
        except OSError:  # nothing has arrived (BlockingIOError), or the connection is gone
            return False
        _, space, target = head.partition(b" ")
        segment = target[1:1 + len(self.token_bytes)]
        return (bool(space) and target[:1] == b"/" and target[1 + len(self.token_bytes):][:1] == b"/"
                and hmac.compare_digest(segment, self.token_bytes))

    def vouch(self, connection: socket.socket) -> None:
        """The handler's look before it reads a new connection's first request: once its request line
        has arrived and shows the token, the connection is never the one shut to make room (it keeps
        its deadline)."""
        if self.shows_token(connection, wait=True):
            with self._unchecked_changed:
                if connection in self._unchecked:
                    self._vouched.add(connection)

    def _watch(self, connection: socket.socket) -> None:
        self._unchecked[connection] = time.monotonic() + self.unchecked_deadline_s
        self._vouched.discard(connection)
        self._unchecked_changed.notify_all()

    def watch(self, connection: socket.socket) -> None:
        """Put a connection that passed the check earlier back under a deadline: a later request on it
        was refused, and its body is about to be read and dropped."""
        with self._unchecked_changed:
            self._watch(connection)

    def passed(self, connection: socket.socket) -> bool:
        """The connection passed the token check: its deadline is lifted for good. False when the
        deadline came first, or it was shut to make room, and the connection is already shut."""
        with self._unchecked_changed:
            self._vouched.discard(connection)
            return self._unchecked.pop(connection, None) is not None

    def forget(self, connection: socket.socket) -> None:
        with self._unchecked_changed:
            self._vouched.discard(connection)
            self._unchecked.pop(connection, None)

    def _expire_unchecked(self) -> None:
        """Shut the read side of every connection whose deadline has passed: a read blocked on it
        returns end of stream, so its handler answers what it can and closes. The write side stays
        open for that answer."""
        while not self._reaper_stop.is_set():
            with self._unchecked_changed:
                now = time.monotonic()
                due = [connection for connection, deadline in self._unchecked.items() if deadline <= now]
                for connection in due:
                    del self._unchecked[connection]
                    self._vouched.discard(connection)
                    try:
                        connection.shutdown(socket.SHUT_RD)
                    except OSError:
                        pass
                if not due:
                    nearest = min(self._unchecked.values(), default=None)
                    self._unchecked_changed.wait(None if nearest is None else max(0.0, nearest - now))
            for _ in due:
                self.refused("deadline")

    # Requests forwarded unchanged

    def unrouted(self, reason: str, method: str | None, path: str, error: str | None = None) -> None:
        """Log a request that could have had a session and was forwarded unchanged. For a
        ``/v1/messages`` that means its prompt was not bounded, so stderr says so: at most one line per
        reason every ``UNBOUNDED_WARN_INTERVAL_S``, with how many there were since the last one."""
        event: dict[str, Any] = {"event": "unrouted", "reason": reason, "method": method, "path": path}
        if error is not None:
            event["error"] = error
        self.log.log(event)
        why = UNBOUNDED_REASONS.get(reason)
        if why is None or path != MESSAGES_PATH:
            return
        now = self.clock()
        with self._lock:
            state = self._unbounded.setdefault(reason, [None, 0])
            state[1] += 1
            if state[0] is not None and 0 <= now - state[0] < UNBOUNDED_WARN_INTERVAL_S:
                return
            count, state[0], state[1] = state[1], now, 0
        if reason == "session_cap":
            why = f"all {self.max_sessions} open sessions had a request in flight; raise --max-sessions"
        self.say(f"warning: {count} request{'' if count == 1 else 's'} forwarded unchanged, so "
                 f"{'its prompt is' if count == 1 else 'their prompts are'} not bounded ({reason}: {why}).")

    # The tool-call map

    def register_tool_calls(self, tool_call_ids: tuple[str, ...], session_id: str) -> None:
        """Remember which session each client tool call belongs to, keeping the newest
        ``TOOL_CALLS_KEPT``. An id a different session registered before becomes ambiguous and stays
        so: it then resolves as unknown, so a call is never handed to another conversation's store."""
        ambiguous = 0
        with self._tool_lock:
            for tool_use_id in tool_call_ids:
                if not isinstance(tool_use_id, str) or not 0 < len(tool_use_id) <= MAX_TOOL_USE_ID_CHARS:
                    continue
                if tool_use_id not in self._tool_calls:
                    self._tool_calls[tool_use_id] = session_id
                elif self._tool_calls[tool_use_id] not in (None, session_id):
                    self._tool_calls[tool_use_id] = None
                    ambiguous += 1
                self._tool_calls.move_to_end(tool_use_id)
            while len(self._tool_calls) > TOOL_CALLS_KEPT:
                self._tool_calls.popitem(last=False)
        if ambiguous:
            self.log.log({"event": "tool_use_ambiguous", "count": ambiguous})

    def resolve(self, tool_use_id: str) -> str | None:
        """The Contexto session a tool-use id was issued in; None when unknown or ambiguous."""
        with self._tool_lock:
            session_id = self._tool_calls.get(tool_use_id)
            self._resolved["found" if session_id is not None else "unknown"] += 1
            return session_id

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

    def route_key(self, cli_session: str, parent: str | None, *, messages: bool) -> str:
        """The Claude Code session id whose store serves a request of ``cli_session``. A
        ``/v1/messages`` (``messages``) that names a parent goes to the parent's, and one that names
        none to its own; either way the request decides, and what it said is remembered for the id's
        ``count_tokens`` requests only, which cannot name a parent themselves."""
        with self._lock:
            if not messages:
                return self._parents.get(cli_session, cli_session)
            if parent is None or parent == cli_session:
                self._parents.pop(cli_session, None)
                return cli_session
            self._parents[cli_session] = parent
            self._parents.move_to_end(cli_session)
            while len(self._parents) > PARENTS_KEPT:
                self._parents.popitem(last=False)
            return parent

    def _leased(self, key: str) -> _Session | None:
        """The open session ``key`` is routed to, leased; ``_Unrouted("stopping")`` once ``stop`` began."""
        with self._lock:
            if self._stopping:
                raise _Unrouted("stopping")
            session = self._sessions.get(self._routes.get(key, ""))
            if session is not None:
                session.leases += 1
                session.last_used = self.clock()
            return session

    def _last_served(self, key: str) -> str | None:
        """The Contexto session that most recently served Claude Code session ``key`` and still exists."""
        self._served.refresh()
        return self._served.lookup(key)

    def acquire(self, cli_session: str, parent: str | None = None, *, create: bool,
                messages: bool = True) -> _Session | None:
        """The open session for a request of Claude Code session ``cli_session``, leased until
        ``release``. Opens it at first sight (see the module docstring); ``create`` allows a new
        session when none served the id. None when there is no session and none may be made;
        ``_Unrouted`` when one cannot be opened. The id the request was routed by is recorded in the
        session's store."""
        key = self.route_key(cli_session, parent, messages=messages)
        session = self._acquire(key, create)
        if session is not None and session.recorder.note(key):
            self._served.note(key, session.session_id, time.time())
        return session

    def _acquire(self, key: str, create: bool) -> _Session | None:
        # The wait at the cap is measured from here, so a request queued behind others of its id, each
        # of which waited at the cap holding the id's guard, has no wait of its own left.
        cap_deadline = time.monotonic() + self.cap_wait_s
        session = self._leased(key)
        if session is not None:
            return session
        with self._guarded("cli", key):
            session = self._leased(key)
            if session is not None:
                return session
            if self._stopping:
                raise _Unrouted("stopping")
            if self._recent(self._open_failed, key, OPEN_FAILED_TTL_S):
                raise _Unrouted("session_open_failed")
            if not create and self._recent(self._unserved, key, UNSERVED_TTL_S):
                return None
            session_id = self._last_served(key)
            created = session_id is None
            if session_id is None:
                if not create:
                    self._remember(self._unserved, key, UNSERVED_KEPT)
                    return None
                session_id = uuid.uuid4().hex
            with self._guarded("session", session_id):
                with self._lock:
                    session = self._sessions.get(session_id)
                    if session is not None:  # open already, for another id it served
                        self._lease_new_route(session, key)
                        return session
                reserved, victim = self._reserve(cap_deadline)
                if victim is not None:
                    self._close(victim, "cap")
                if not reserved:
                    raise _Unrouted("stopping" if self._stopping else "session_cap")
                try:
                    session = self._open(session_id, created)
                except Exception as exc:
                    with self._lock:
                        self._opening -= 1
                        self._freed.notify_all()
                    self._remember(self._open_failed, key, OPEN_FAILED_KEPT)
                    self.log.log({"event": "session_open_failed", "session": session_id, "created": created,
                                  "error": type(exc).__name__, "errno": getattr(exc, "errno", None)})
                    raise _Unrouted("session_open_failed") from None
                with self._lock:
                    self._opening -= 1
                    stopping = self._stopping  # stop() began meanwhile: do not add to the registry
                    if not stopping:
                        self._sessions[session_id] = session
                        self._lease_new_route(session, key)
                        self._unserved.pop(key, None)
                    count = len(self._sessions)
                    self._freed.notify_all()
                if stopping:
                    self._discard(session, created)
                    raise _Unrouted("stopping")
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
            if not session.leases:
                self._freed.notify_all()

    def _recent(self, memory: OrderedDict[str, float], key: str, ttl: float) -> bool:
        with self._lock:
            seen = memory.get(key)
            return seen is not None and 0 <= self.clock() - seen < ttl

    def _remember(self, memory: OrderedDict[str, float], key: str, kept: int) -> None:
        with self._lock:
            memory[key] = self.clock()
            memory.move_to_end(key)
            while len(memory) > kept:
                memory.popitem(last=False)

    def _reserve(self, deadline: float) -> tuple[bool, _Session | None]:
        """Take a place for a session about to open: (True, None) when there is room, (True, victim)
        when the least recently used session without a request in flight was taken out of the registry
        to make room (the caller closes it), (False, None) when every open session stayed in use until
        ``deadline`` (the request's ``cap_wait_s``, counted from when it arrived) or ``stop`` began. The
        wait is what keeps a request that arrives just after another session's response completed, a
        moment before that session's lease is returned, from going unbounded."""
        with self._lock:
            while True:
                if len(self._sessions) + self._opening < self.max_sessions:
                    self._opening += 1
                    return True, None
                idle = [session for session in self._sessions.values() if session.leases == 0]
                if idle:
                    victim = min(idle, key=lambda session: session.last_used)
                    self._remove(victim)
                    self._opening += 1
                    return True, victim
                left = deadline - time.monotonic()
                if left <= 0 or self._stopping:
                    return False, None
                self._cap_waiting += 1
                try:
                    self._freed.wait(left)
                finally:
                    self._cap_waiting -= 1

    def _remove(self, session: _Session) -> None:
        """Under the registry lock: take ``session`` out, so no new request is routed to it."""
        self._sessions.pop(session.session_id, None)
        for key in session.keys:
            if self._routes.get(key) == session.session_id:
                del self._routes[key]
        self._freed.notify_all()

    def _notices(self, session_id: str) -> set[str]:
        """The warnings already printed for a session, kept while the service runs."""
        with self._lock:
            shown = self._shown.setdefault(session_id, set())
            self._shown.move_to_end(session_id)
            while len(self._shown) > NOTICES_KEPT:
                self._shown.popitem(last=False)
            return shown

    def _open(self, session_id: str, created: bool) -> _Session:
        """Open the store and build its engine and proxy, as ``launcher.start_proxy`` does for a launch.
        No working directory is recorded (see the module docstring). A session the service makes is
        kept out of project memory (D69): the service routes by session id and cannot know the client's
        directory, so it records ``project_memory: false`` before the engine reads it, and a later
        ``contexto run claude --resume <id>`` never turns it back on. When this fails for a session
        being made, the directory it made is removed again."""
        store = None
        try:
            store = SessionStore(self.home, session_id)
            if created:
                store.set_project_memory(False)
            transform, observe_usage, engine_name = self.engine_factory(store, self.config)
            recorder = RouteRecorder(transform, store)
            notices = SessionNotices(store, self.stderr, session_id, self._notices(session_id))
            proxy = build_session_proxy(
                self.upstream, notices, recorder, observe_usage, observe_thinking=self.observe_thinking,
                affinity_key=session_affinity_key(store, self.upstream.url),
                on_tool_calls=lambda tool_call_ids: self.register_tool_calls(tool_call_ids, session_id),
                strip_hint_headers=not self.forward_hint_headers)
            store.log({"event": "serve_open", "cli": "claude", "session": session_id, "created": created,
                       "config": self.config, "engine": engine_name, "upstream_source": self.upstream.source,
                       "observe_thinking": self.observe_thinking,
                       "affinity_header": proxy.affinity_header is not None})
        except BaseException:
            if store is not None:
                store.release()
            if created:
                try:  # no idle grace: it was made a moment ago, and only its lock says whether it is in use
                    remove_session(self.home, session_id, now=0.0, grace=float("-inf"))
                except (SessionLiveError, SessionNotFoundError, ValueError, OSError):
                    pass
            raise
        return _Session(session_id, store, proxy, recorder, self.clock())

    def _discard(self, session: _Session, created: bool) -> None:
        """With the session's guard held: a session opened by a request that lost the race with
        ``stop`` and never added to the registry. One it made is removed again, as a failed open's is:
        no Claude Code id leads to it yet, since the id is recorded only once the request is routed.
        One that existed is closed like any other at shutdown."""
        if not created:
            self._finish(session, "shutdown")
            return
        try:
            session.store.release()
        except Exception:
            pass
        try:
            remove_session(self.home, session.session_id, now=0.0, grace=float("-inf"))
        except (SessionLiveError, SessionNotFoundError, ValueError, OSError):
            pass
        self.log.log({"event": "session_discarded", "session": session.session_id, "reason": "stopping"})

    def _close(self, session: _Session, reason: str) -> None:
        """Close a session already taken out of the registry, under the session's guard, so the close
        and an open of the same session never overlap. A request that reopens the session meanwhile
        may take the guard first; this close then waits for that open, which is harmless: the session
        being closed has no request in flight and writes nothing but its log line and its times."""
        with self._guarded("session", session.session_id):
            self._finish(session, reason)

    def _finish(self, session: _Session, reason: str) -> None:
        """With the session's guard held: the exit-time work that applies (the missing-tools report,
        the recorder's last-seen times), a log line in both logs, and the store's lock released."""
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
    """The proxy's loopback server with the service's limits on connections that have not passed the
    token check (``ServeService.admit``), and with a failure outside a request (socketserver's default
    prints a traceback) logged by its kind only, through the refusal limiter."""

    service: ServeService

    def get_request(self) -> Any:
        try:
            return super().get_request()
        except OSError:
            self.service.refused("accept_error")  # out of descriptors, for one
            raise

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.service.admit(request):
            self.shutdown_request(request)  # over the cap: closed before it costs a thread
            return
        super().process_request(request, client_address)

    def shutdown_request(self, request: Any) -> None:
        self.service.forget(request)
        super().shutdown_request(request)

    def handle_error(self, request: Any, client_address: Any) -> None:
        self.service.refused("connection_error")


class _ServeHandler(_Handler):
    """The proxy's handler with a front door: the Host and Origin checks, the token, the local
    endpoints, and the choice of the request's ``owner`` (its session's proxy, or the service's
    pass-through one). Everything after that is ``_Handler``'s own code.

    Until a request has passed the checks, the only thing this handler writes is the one 403
    (``_refuse``). ``http.server`` answers what it cannot parse through ``send_error`` and an
    ``Expect: 100-continue`` through ``handle_expect_100``, both before any ``do_`` method runs, so
    both are replaced here."""

    service: ServeService

    def setup(self) -> None:
        super().setup()
        self._trusted = False  # this connection has passed the token check: no deadline applies to it
        self._passed = False  # the request being handled has passed the checks
        self.service.vouch(self.connection)  # before anything is read: see ServeService._make_room

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """What ``http.server`` calls for a request it will not hand to a ``do_`` method: a malformed
        or over-long request line, an unknown HTTP version, too many or over-long headers, an
        unsupported method. Its own answer is an HTML page that echoes the request line and names the
        library and Python version; this one is the refusal every other unchecked request gets."""
        self._refuse("malformed")

    def handle_expect_100(self) -> bool:
        return True  # "100 Continue" is sent by _serve, once the request has passed the checks

    def _refuse(self, reason: str, *, discard: bool = False) -> None:
        """Count the refusal, send the one 403 and close. Closing on unread bytes sends a reset, which
        can discard the 403, so what the client still sends is read and dropped, under the connection's
        deadline: the request's body before the answer when its framing was parsed (``discard``), else
        whatever follows the answer (``_linger``)."""
        self.service.refused(reason)
        if self._trusted:  # an earlier request on this connection passed: this one is under a deadline again
            self._trusted = False
            self.service.watch(self.connection)
        if discard:
            self._discard_body()
        self.request_version = "HTTP/1.1"  # a request line without a version would get a body and no head
        self._send_local_error(403, REFUSED_MESSAGE, "permission_error")
        if not discard:
            self._linger()
        self.close_connection = True

    def _linger(self) -> None:
        """After the 403 to a request whose framing is unknown: end the response, then drop what the
        client still sends until it closes, ``MAX_LINGER_BYTES`` have gone by, or the deadline shuts the
        read side."""
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            left = MAX_LINGER_BYTES
            while left > 0 and (chunk := self.rfile.read1(65536)):
                left -= len(chunk)
        except (OSError, ValueError):
            pass

    def _handle(self) -> None:
        service = self.service
        with service._idle:
            service._in_flight += 1
        self._passed = False
        try:
            self._serve()
        except Exception as exc:
            if self._passed:  # never the path: before the prefix is removed it holds the token
                service.log.log({"event": "serve_error", "method": self.command, "error": type(exc).__name__})
            else:  # before the token check nothing is logged outside the limiter
                try:
                    self._refuse("error")
                except Exception:
                    pass
            self.close_connection = True
        finally:
            with service._idle:
                service._in_flight -= 1
                service._idle.notify_all()

    def _serve(self) -> None:
        service = self.service
        self._body: bytes | ValueError | None = None  # the request body, read once
        self._parsed: tuple[bytes, Any] | None = None  # the body the route parsed, and its value or error
        if self.request_version not in ("HTTP/1.0", "HTTP/1.1"):
            self._refuse("malformed")  # a request line with no version (HTTP/0.9)
            return
        reason = self._refusal() or self._take_token()
        if reason is not None:
            self._refuse(reason, discard=True)
            return
        if not self._trusted:
            if not service.passed(self.connection):  # the deadline came first: the connection is shut
                self.close_connection = True
                return
            self._trusted = True
        self._passed = True
        self._drop_token_headers()
        path_only = self.path.split("?", 1)[0]
        if service._stopping:
            return self._refuse_stopping(path_only)
        if (self.headers.get("Expect") or "").strip().lower() == "100-continue" and self.request_version == "HTTP/1.1":
            BaseHTTPRequestHandler.handle_expect_100(self)
        if path_only == LOCAL_PREFIX or path_only.startswith(LOCAL_PREFIX + "/"):
            self._local(path_only)
            return
        session: _Session | None = None
        try:
            session = self._route(path_only)
        except _Unrouted as unrouted:
            if unrouted.reason == "stopping":
                return self._refuse_stopping(path_only)
            service.unrouted(unrouted.reason, self.command, path_only)
        except Exception as exc:  # routing must never cost the client its request: forward it unchanged
            service.unrouted("route_error", self.command, path_only, type(exc).__name__)
        self.owner = session.proxy if session is not None else service.passthrough
        try:
            self._handle_counted()
        finally:
            if session is not None:
                service.release(session)

    def _refuse_stopping(self, path_only: str) -> None:
        """A request that passed the checks after ``stop`` began: not routed or forwarded, answered with
        a 503 and its connection closed, so a kept-alive connection sends nothing more to a service
        that is going away. The body is read and dropped first, so the answer is not lost to a reset,
        unless the client waits for ``100 Continue`` before sending it: that one is never sent here."""
        self.service.log.log({"event": "stopping_refused", "method": self.command, "path": path_only})
        expects = (self.headers.get("Expect") or "").strip().lower() == "100-continue"
        if self._body is None and not expects:
            self._discard_body()
        self.close_connection = True
        payload = json.dumps({"type": "error", "error": {"type": "overloaded_error",
                                                         "message": STOPPING_MESSAGE}}).encode()
        try:
            self.send_response_only(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass

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

    def _drop_token_headers(self) -> None:
        """Remove every request header whose name or value holds the token (a ``Referer`` that quotes
        the base URL, say) before anything is forwarded; the log gets the count and no name or value."""
        token = self.service.token
        names = {name for name, value in self.headers.items() if token in name or token in value}
        if not names:
            return
        count = sum(len(self.headers.get_all(name) or []) for name in names)
        for name in names:
            del self.headers[name]
        self.service.log.log({"event": "token_header_dropped", "count": count})

    def _discard_body(self) -> None:
        """Read and drop a refused request's body, at most ``MAX_DECODED_BODY_BYTES`` of it, keeping none.
        A body that never arrives ends at the connection's deadline, when its read side is shut."""
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
        ``_Unrouted`` for a request that could have had one and cannot: it names a session it cannot
        have, or it is a ``/v1/messages`` that names none."""
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
        messages = path_only == MESSAGES_PATH
        if cli_session is None:
            if messages:
                raise _Unrouted("no_session_id")
            return None
        session = self.service.acquire(cli_session, parent, create=messages and isinstance(request, dict),
                                       messages=messages)
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
            tool_use_id = requested_tool_use(body)
            if tool_use_id is None:
                return self._json(400, {"error": "invalid_request"})
            session_id = service.resolve(tool_use_id)
            if session_id is None:
                return self._json(404, RESOLVE_UNKNOWN)
            return self._json(200, resolve_found(session_id))
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
    """``--port`` when given; else the port of the service that runs for this home (it holds the
    lock); else the default."""
    if args.port is not None:
        return args.port
    state = running_state(home)
    return state["port"] if state is not None else DEFAULT_PORT


def print_settings(args: Any, home: Path, stdout, stderr) -> int:
    token = ensure_token(home)
    found = cloud_provider(cwd=Path.cwd())
    if found is not None:
        print(f"contexto serve: {cloud_provider_warning(found)}", file=stderr)
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
    if service_running(home):
        state = running_state(home)
        pid = f" (pid {state['pid']})" if state is not None and state["pid"] is not None else ""
        running = f"contexto serve is running{pid} and still expects the old token: restart it. "
    else:
        running = "A running contexto serve must be restarted to use it. "
    print(f"contexto serve: the token in {token_path(home)} was replaced. {running}Then give every client the new "
          f"URL (contexto serve --print-settings).", file=stdout)
    return 0


def ask_health(port: int, token: str) -> dict[str, Any] | None:
    """The service's health answer, or None when nothing that is the service answers on ``port``."""
    conn = http.client.HTTPConnection(LISTEN_HOST, port, timeout=STATUS_TIMEOUT_S)
    try:
        conn.request("GET", local_target(token, HEALTH_PATH))
        response = conn.getresponse()
        data = response.read(MAX_LOCAL_BODY_BYTES)
        health = json.loads(data) if response.status == 200 else None
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        conn.close()
    return health if isinstance(health, dict) and health.get("ok") is True else None


def status(home: Path, stdout) -> int:
    """Whether the service runs: exit 0 with its port and open sessions, else 1. "Runs" means a
    service holds the home's lock. Only then is ``serve.json`` believed and the health endpoint asked,
    so the token is never sent to a port named by a record that a killed service left behind. Nothing
    is created: the token is read only if its file exists."""
    token = read_token(home) if os.path.lexists(token_path(home)) else None
    if not service_running(home):
        try:
            left = read_state(home)
        except ServeFileError:
            left = None
        detail = (f" ({STATE_FILE} is left from an earlier service, which recorded pid {left['pid']}; nothing "
                  f"holds {LOCK_FILE} now)" if left is not None else "")
        print(f"contexto serve: not running{detail}", file=stdout)
        return 1
    state = running_state(home)
    if state is None:
        print(f"contexto serve: starting or stopping (a service holds {LOCK_FILE} and has no {STATE_FILE})",
              file=stdout)
        return 1
    health = ask_health(state["port"], token) if token is not None else None
    if health is None:
        print(f"contexto serve: not answering ({STATE_FILE} records pid {state['pid']} on port {state['port']}, and "
              f"a service holds {LOCK_FILE}, but it did not answer with the token in {TOKEN_FILE}: restart the "
              f"service if the token was rotated)", file=stdout)
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


def serve_options(args: Any, env: dict[str, str] | None = None) -> LaunchOptions:
    """The launch options every session of the service gets. D63's in-turn paging is on unless
    ``--no-in-turn-paging`` is given or ``CONTEXTO_IN_TURN_PAGING`` turns it off, as for ``contexto run``."""
    return LaunchOptions(bound=args.bound, upstream=args.upstream, observe_thinking=args.observe_thinking,
                         allow_large_bound=args.allow_large_bound, window_fraction=args.window_fraction,
                         headroom_floor=args.headroom_floor,
                         in_turn_paging=in_turn_paging(getattr(args, "no_in_turn_paging", False), env))


def _serve(args: Any, home: Path, stderr, signals: _StopSignals) -> int:
    options = serve_options(args)
    check_options(options)
    if args.max_sessions < 1:
        raise ServeError(f"--max-sessions must be at least 1, got {args.max_sessions}", EXIT_USAGE)
    if not args.idle_minutes > 0:
        raise ServeError(f"--idle-minutes must be above 0, got {args.idle_minutes}", EXIT_USAGE)
    files = SettingsFiles()  # each settings file read once, for the provider check and the upstream
    provider = cloud_provider(cwd=Path.cwd(), files=files)
    upstream = resolve_upstream(args.upstream, cwd=Path.cwd(), files=files)
    token = ensure_token(home)
    port = DEFAULT_PORT if args.port is None else args.port
    check_upstream_loop(upstream, port, token)  # first: a malformed URL's error can quote part of it
    check_upstream_url(upstream)
    try:
        service = ServeService(home, upstream, token, config=engine_config(options),
                               observe_thinking=options.observe_thinking, max_sessions=args.max_sessions,
                               idle_s=args.idle_minutes * 60, stderr=stderr,
                               forward_hint_headers=args.forward_hint_headers)
    except ValueError as exc:
        raise ServeError(f"invalid upstream (from {upstream.source}): {exc}", EXIT_USAGE) from None
    try:
        port = service.start(port)  # ServeError when a service already runs for this home
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise ServeError(f"cannot start on {LISTEN_HOST}:{port}: {exc.strerror or type(exc).__name__}") from None
        raise ServeError(f"port {port} on {LISTEN_HOST} is in use; stop what holds it, or pass --port N") from None
    try:
        check_upstream_loop(upstream, port, token)  # a port the system picked (--port 0)
        print(f"contexto serve: listening on {LISTEN_HOST}:{port} (bound {options.bound:,}, upstream "
              f"{upstream.source})", file=stderr, flush=True)
        if upstream.ignored:  # CLAUDE_CODE_IGNORE_PERSISTED_TRUST is set: no project base URL (D52)
            note = untrusted_note("env.ANTHROPIC_BASE_URL", upstream.ignored, "Pass --upstream", os.environ)
            print(f"contexto serve: {note}", file=stderr, flush=True)
        if provider is not None:
            print(f"contexto serve: {cloud_provider_warning(provider)}", file=stderr, flush=True)
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
