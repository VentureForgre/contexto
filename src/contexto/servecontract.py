"""What ``contexto serve`` and the programs that talk to it agree on (D70). Each item is defined here
once; the service (``contexto.serve``), the MCP server's client (``contexto.serveclient``) and the
commands that must recognise the service's address import it.

Files in ``$CONTEXTO_HOME``, each mode 0600 and owned by the user, never read through a symlink:

- ``serve.token``: the token ``serve`` requires on every request, one line of ``TOKEN_PATTERN``.
- ``serve.lock``: empty. The service holds an exclusive ``flock`` on it from before it binds its port
  until it exits, so the lock, not a process id, says whether a service runs for this home: a
  non-blocking shared lock attempt fails exactly while one does (``service_running``). The kernel
  drops the lock when the process ends, however it ends, so a killed service never looks alive.
- ``serve.json``: ``{"port": N, "pid": N}``, written once the service listens and removed when it
  stops. It is believed only while the lock is held; ``pid`` is there for messages.

Nothing here sends the token anywhere. A caller sends it only to the port ``serve.json`` names, and
only after ``service_running`` said the lock is held (``endpoint``).

The service's own endpoints, under ``/<token>``: ``GET /contexto/health`` and ``POST
/contexto/resolve``. The resolve request is ``{"tool_use_id": "<id>"}`` (``resolve_request``); the
answers are 200 ``{"session": "<Contexto session id>"}`` (``resolve_found``) and 404
``{"error": "unknown_tool_use"}`` (``RESOLVE_UNKNOWN``), which also answers an id that two sessions
registered.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contexto.canonical import loads_bounded
from contexto.platform_compat import (
    IS_WINDOWS,
    O_BINARY,
    O_NOFOLLOW,
    O_NONBLOCK,
    flock_ex_nb,
    flock_sh_nb,
    has_group_or_other_perms,
    is_owned_by_current_user,
    unlock_and_close,
)
from contexto.private import open_private, private_dir

TOKEN_FILE = "serve.token"
STATE_FILE = "serve.json"
LOCK_FILE = "serve.lock"
SERVICE_HOST = "127.0.0.1"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")  # secrets.token_urlsafe(32) is 43 of these characters
MAX_FILE_BYTES = 4096  # serve.json and serve.token are each well under this
LOCK_WAIT_S = 0.25  # how long a starting service waits out the instant a status check holds the lock shared
LOCAL_PREFIX = "/contexto"
HEALTH_PATH = LOCAL_PREFIX + "/health"
RESOLVE_PATH = LOCAL_PREFIX + "/resolve"
ACTIVE_PATH = LOCAL_PREFIX + "/active"
MCP_PATH = "/mcp"
CONTEXTO_SESSION_HEADER = "X-Contexto-Session"
CONTEXTO_WORKDIR_HEADER = "X-Contexto-Workdir"
MCP_SESSION_HEADER = "Mcp-Session-Id"
PROVIDER_NAMES = ("anthropic", "openai", "gemini")
DEFAULT_PROVIDER_UPSTREAMS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "gemini": "https://generativelanguage.googleapis.com",
}
MAX_LOCAL_BODY_BYTES = 64 * 1024
MAX_TOOL_USE_ID_CHARS = 256  # Anthropic's ids are about 30 characters; a longer value is not an id
MAX_WORKDIR_BYTES = 4096
UNKNOWN_TOOL_USE = "unknown_tool_use"
RESOLVE_UNKNOWN = {"error": UNKNOWN_TOOL_USE}
class ServeFileError(Exception):
    """One of the service's files cannot be used. ``name`` is the file's name, ``kind`` one of
    ``missing``, ``symlink``, ``not_regular``, ``unreadable``, ``owner``, ``mode`` and ``content``,
    and ``problem`` a fixed phrase that follows the name in a message. None holds the file's content."""

    def __init__(self, name: str, kind: str, problem: str) -> None:
        super().__init__(f"{name} {problem}")
        self.name = name
        self.kind = kind
        self.problem = problem


def _read_private(home: Path, name: str) -> bytes:
    """The bytes of ``home/name``: opened without following a symlink and without blocking on a FIFO,
    and required to be a regular file of the current user with no group or other permission bit, at
    most ``MAX_FILE_BYTES`` long. ``ServeFileError`` otherwise."""
    target = Path(home) / name
    if O_NOFOLLOW == 0 and os.path.islink(target):
        raise ServeFileError(name, "symlink", "is a symlink, and it is never read through one")
    try:
        fd = os.open(target, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_BINARY)
    except FileNotFoundError:
        raise ServeFileError(name, "missing", "is missing") from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):  # EMLINK on FreeBSD
            raise ServeFileError(name, "symlink", "is a symlink, and it is never read through one") from None
        if IS_WINDOWS and exc.errno == errno.EACCES and target.is_dir():
            raise ServeFileError(name, "not_regular", "is not a regular file") from None
        raise ServeFileError(name, "unreadable", f"cannot be read ({type(exc).__name__})") from None
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise ServeFileError(name, "not_regular", "is not a regular file")
        if not is_owned_by_current_user(status):
            raise ServeFileError(name, "owner", "belongs to another user")
        if has_group_or_other_perms(status):
            raise ServeFileError(name, "mode", f"has mode {stat.S_IMODE(status.st_mode):04o}: it must be readable "
                                               f"by its owner only (0600)")
        data = os.read(fd, MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ServeFileError(name, "unreadable", f"cannot be read ({type(exc).__name__})") from None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise ServeFileError(name, "content", "is not the file the service writes (too long)")
    return data


def new_token() -> str:
    """A fresh token: 32 random bytes as ``secrets.token_urlsafe`` writes them, 43 characters of
    ``TOKEN_PATTERN``."""
    return secrets.token_urlsafe(32)


def read_token(home: Path) -> str:
    """The token in ``home/serve.token`` (``ServeFileError`` when the file cannot be used or does not
    hold one token)."""
    token = _read_private(home, TOKEN_FILE).decode("ascii", "replace").strip()
    if not TOKEN_PATTERN.fullmatch(token):
        raise ServeFileError(TOKEN_FILE, "content", "does not hold a token")
    return token


def read_state(home: Path) -> dict[str, int | None]:
    """``home/serve.json`` as ``{"port", "pid"}`` (``pid`` None when the record has none), read under
    the same owner and mode rules as the token file. ``ServeFileError`` when it is not such a record."""
    try:
        state = loads_bounded(_read_private(home, STATE_FILE))
    except ValueError:  # not UTF-8, not JSON, or nested too deep
        state = None
    port = state.get("port") if isinstance(state, dict) else None
    if type(port) is not int or not 0 < port <= 65535:
        raise ServeFileError(STATE_FILE, "content", "does not name the service's port")
    pid = state.get("pid")
    return {"port": port, "pid": pid if type(pid) is int and pid > 0 else None}


def hold_lock(home: Path, wait_s: float = LOCK_WAIT_S) -> int | None:
    """Take the service's lock for ``home``: an exclusive ``flock`` on ``home/serve.lock`` (created
    0600, never opened through a symlink). Returns the descriptor that holds it, which the caller
    keeps open for as long as it serves, or None when another service holds the lock. A status check
    holds the lock shared for an instant, so the attempt is repeated for up to ``wait_s``.
    OSError when the file cannot be opened (``NotRegularFileError`` for a symlink or a directory)."""
    private_dir(Path(home), narrow=False)
    fd = open_private(Path(home) / LOCK_FILE, os.O_RDWR)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            flock_ex_nb(fd)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.01)
        except BaseException:
            os.close(fd)
            raise


def service_running(home: Path) -> bool:
    """Whether a service holds ``home``'s lock: a non-blocking shared lock attempt on ``serve.lock``
    fails. False when the file is missing, is not the user's own private regular file, or is not
    locked. Nothing is created and nothing is sent."""
    target = Path(home) / LOCK_FILE
    if O_NOFOLLOW == 0 and os.path.islink(target):
        return False
    try:
        fd = os.open(target, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_BINARY)
    except OSError:
        return False
    try:
        status = os.fstat(fd)
        if (not stat.S_ISREG(status.st_mode) or not is_owned_by_current_user(status)
                or has_group_or_other_perms(status)):
            return False
        try:
            flock_sh_nb(fd)
        except BlockingIOError:
            return True
        return False  # the shared lock was granted: nobody serves; closing the descriptor drops it
    except OSError:
        return False
    finally:
        unlock_and_close(fd)


def running_state(home: Path) -> dict[str, int | None] | None:
    """The record of the service that runs for ``home``, or None: when no service holds the lock, and
    when one does but its record cannot be read (it is starting or stopping)."""
    if not service_running(home):
        return None
    try:
        return read_state(home)
    except ServeFileError:
        return None


def endpoint(home: Path) -> tuple[int, str]:
    """The running service's port and token, for a caller about to send it a request. The lock is
    checked first, so with no service running nothing is read and the token goes nowhere:
    ``ServeFileError`` then names ``serve.lock`` with kind ``missing``, and otherwise the file that
    cannot be used."""
    port, _, token = endpoint_record(home)
    return port, token


def endpoint_record(home: Path) -> tuple[int, int | None, str]:
    """``endpoint`` with the process id ``serve.json`` records: ``(port, pid, token)``. A caller that
    sends more than one request compares it before each later one, so a request goes only to the
    service that answered the first."""
    if not service_running(home):
        raise ServeFileError(LOCK_FILE, "missing", "is not held: no contexto serve is running for this Contexto home")
    state = read_state(home)
    return state["port"], state["pid"], read_token(home)


def base_url(port: int, token: str) -> str:
    """What a client takes as ``ANTHROPIC_BASE_URL``: the token is the first path segment."""
    return f"http://{SERVICE_HOST}:{port}/{token}"


def mcp_url(port: int, token: str, session: str | None = None) -> str:
    """Streamable HTTP MCP endpoint URL for ``contexto serve``."""
    if session:
        return f"http://{SERVICE_HOST}:{port}/{token}/s/{session}{MCP_PATH}"
    return f"http://{SERVICE_HOST}:{port}/{token}{MCP_PATH}"


def encode_workdir_segment(workdir: str) -> str:
    """Encode an absolute directory path into a URL-safe ``/w/<encoded>/`` path segment."""
    raw = workdir.encode("utf-8")
    if not raw or len(raw) > MAX_WORKDIR_BYTES or b"\x00" in raw:
        raise ValueError("invalid workdir")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_workdir_segment(segment: str) -> str | None:
    """Decode a ``/w/<encoded>/`` segment back into an absolute path string, or None when invalid."""
    if not segment or len(segment) > MAX_WORKDIR_BYTES * 2:
        return None
    pad = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode((segment + pad).encode("ascii"))
        text = raw.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeError):
        return None
    if not text or len(raw) > MAX_WORKDIR_BYTES or "\x00" in text or not os.path.isabs(text):
        return None
    return text


def local_target(token: str, path: str) -> str:
    """The request target of one of the service's own endpoints (``HEALTH_PATH``, ``RESOLVE_PATH``)."""
    return f"/{token}{path}"


# POST /contexto/resolve


def resolve_request(tool_use_id: str) -> bytes:
    return json.dumps({"tool_use_id": tool_use_id}).encode("utf-8")


def resolve_session_request(session_key: str) -> bytes:
    return json.dumps({"session": session_key}).encode("utf-8")


def requested_resolve(body: bytes) -> tuple[str | None, str | None]:
    """``(tool_use_id, session_key)`` from a resolve request body; both None when invalid."""
    try:
        request = loads_bounded(body)
    except ValueError:
        return None, None
    if not isinstance(request, dict):
        return None, None
    tool_use_id = request.get("tool_use_id")
    if isinstance(tool_use_id, str) and 0 < len(tool_use_id) <= MAX_TOOL_USE_ID_CHARS:
        return tool_use_id, None
    session_key = request.get("session")
    if isinstance(session_key, str) and 0 < len(session_key) <= MAX_TOOL_USE_ID_CHARS:
        return None, session_key
    return None, None


def requested_tool_use(body: bytes) -> str | None:
    """The tool-use id a resolve request asks about, or None when the body is not such a request."""
    tool_use_id, _ = requested_resolve(body)
    return tool_use_id


def resolve_found(session_id: str) -> dict[str, str]:
    return {"session": session_id}


def found_session(answer: Any) -> str | None:
    """The session a 200 answer names, or None when it names none."""
    session = answer.get("session") if isinstance(answer, dict) else None
    return session if isinstance(session, str) and session else None


def is_unknown(answer: Any) -> bool:
    """Whether a 404 answer is the service's own (``RESOLVE_UNKNOWN``) and not another program's."""
    return isinstance(answer, dict) and answer.get("error") == UNKNOWN_TOOL_USE


# Recognising the service's address


def is_loopback(host: str) -> bool:
    """Whether ``host`` (a URL's host, brackets or not) is this machine: ``localhost`` and its
    subdomains, any loopback or unspecified address, and the other spellings a resolver accepts for an
    IPv4 address (``127.1``, ``2130706433``, ``0x7f.1``). A name that only DNS maps to loopback is not
    looked up."""
    host = host.strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.split("%", 1)[0]  # an IPv6 zone id
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except (OSError, ValueError):
            return False
    mapped = getattr(address, "ipv4_mapped", None)
    address = mapped or address
    return address.is_loopback or address.is_unspecified  # 0.0.0.0 connects to this machine too


def names_service(url: str, home: Path) -> bool:
    """Whether ``url`` is the address of ``home``'s service: a loopback host whose first path segment is
    the token in ``home/serve.token``. The token file is read only if it exists, the comparison takes
    constant time, and a file that cannot be used means no."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return False
    segment = parts.path.lstrip("/").split("/", 1)[0]
    if not segment or not is_loopback(host) or not os.path.lexists(Path(home) / TOKEN_FILE):
        return False
    try:
        token = read_token(home)
    except ServeFileError:
        return False
    return hmac.compare_digest(segment.encode("utf-8", "replace"), token.encode("ascii"))

