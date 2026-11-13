"""Client of the Contexto service's resolve endpoint: which session does a tool call belong to? (D70)

``contexto serve`` routes each Claude Code conversation to its own session store, and it sees every
model response, so it knows which session each ``tool_use`` id belongs to. ``contexto mcp --serve``
(``contexto.mcp.ServeServer``) is one MCP server for every conversation of a Claude Code process: it
gets the ``tool_use`` id with each ``tools/call`` and asks the service here.

How the service is found: it writes two files in ``$CONTEXTO_HOME``, ``serve.json``
(``{"port": N, "pid": N}``) and ``serve.token`` (the token the service requires: ASCII, one line).
Both are read without following a symlink and without blocking on a FIFO (``contexto.private``), at
every call, so a restarted service is found without restarting the MCP server.

The request: ``POST http://127.0.0.1:<port>/<token>/contexto/resolve`` with the JSON body
``{"tool_use_id": "<id>"}``. The answers: 200 ``{"session": "<Contexto session id>"}``, 404
``{"error": "unknown_tool_use"}``, 400 for a malformed body, 403 for a bad token. The connection is
made with ``http.client`` to ``127.0.0.1`` only: it reads no proxy setting from the environment, sends
``Host: 127.0.0.1:<port>`` and never an ``Origin``.

A 404 is asked again a few times inside ``RETRY_WINDOW_S``: Claude Code can run a tool while the
service is still reading the end of the response that carried the call. Nothing else is retried.

What is never sent or said: the token goes into the request path and nowhere else. A token that is
not one line of URL-safe ASCII is not sent at all, and when ``serve.json`` names a process that no
longer runs nothing is sent, since another program may hold the port by then. ``ResolveError`` holds
a kind and a fixed phrase, never the token, the request path, a file's content, or the text of an
exception that could quote one of them.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from contexto.canonical import loads_bounded
from contexto.private import NotRegularFileError, open_regular

# The service side (`contexto serve`) defines the same three names.
SERVE_FILE = "serve.json"
TOKEN_FILE = "serve.token"
RESOLVE_PATH = "/contexto/resolve"

SERVICE_HOST = "127.0.0.1"
TIMEOUT_S = 2.0  # connect and each read; the service is on this machine and answers from memory
# Waits between tries after a 404: six tries over 1.9 s when the service answers at once. A wait that
# would end after RETRY_WINDOW_S, measured on the caller's clock from the first try, is not started.
RETRY_DELAYS_S = (0.1, 0.2, 0.3, 0.5, 0.8)
RETRY_WINDOW_S = 2.0
MAX_FILE_BYTES = 4096  # serve.json and serve.token are each well under this
MAX_ANSWER_BYTES = 65536
# One line of URL-safe ASCII (RFC 3986 unreserved), so it is a single path segment whatever it holds.
_TOKEN = re.compile(r"[A-Za-z0-9._~-]{1,512}")

NOT_RUNNING = "not_running"  # the files are missing or unreadable, or nothing answers on the port
TOKEN_REFUSED = "token_refused"  # 403
UNKNOWN_TOOL_USE = "unknown_tool_use"  # 404 unknown_tool_use, still after the retries
UNEXPECTED_ANSWER = "unexpected_answer"  # any other answer: another version, or another program on the port


class ResolveError(Exception):
    """The service did not name a session. ``kind`` is one of the four constants above and ``reason``
    a short fixed phrase a tool error can show; neither holds the token or file content."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


def _read(home: Path, name: str) -> bytes:
    """The bytes of ``home/name``, a regular file of at most ``MAX_FILE_BYTES`` opened without following
    a symlink. A failure is ``NOT_RUNNING`` and names the file, never its content."""
    try:
        fd = open_regular(Path(home) / name)
    except FileNotFoundError:
        raise ResolveError(NOT_RUNNING, f"{name} is missing") from None
    except NotRegularFileError:
        raise ResolveError(NOT_RUNNING, f"{name} is a symlink or not a regular file") from None
    except OSError as exc:
        raise ResolveError(NOT_RUNNING, f"{name} cannot be read ({type(exc).__name__})") from None
    try:
        data = os.read(fd, MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ResolveError(NOT_RUNNING, f"{name} cannot be read ({type(exc).__name__})") from None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise ResolveError(NOT_RUNNING, f"{name} is not the file the service writes (too long)")
    return data


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, ValueError):
        return True  # another user's process, or an id this system cannot check: let the connection decide
    return True


def read_endpoint(home: Path) -> tuple[int, str]:
    """The service's port and token from ``serve.json`` and ``serve.token`` in ``home``.

    ``ResolveError(NOT_RUNNING)`` when either file is missing, is not a regular file, cannot be read,
    or does not hold what the service writes, and when ``serve.json`` names a ``pid`` that is not
    running (the service ended without removing its files)."""
    try:
        record = loads_bounded(_read(home, SERVE_FILE))
    except ValueError:  # not UTF-8, not JSON, or nested too deep
        record = None
    port = record.get("port") if isinstance(record, dict) else None
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ResolveError(NOT_RUNNING, f"{SERVE_FILE} does not name the service's port")
    pid = record.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and not _running(pid):
        raise ResolveError(NOT_RUNNING, f"the service process recorded in {SERVE_FILE} is not running")
    try:
        token = _read(home, TOKEN_FILE).decode("ascii")
    except UnicodeDecodeError:
        token = ""
    token = token.removesuffix("\n").removesuffix("\r")
    if not _TOKEN.fullmatch(token):
        raise ResolveError(NOT_RUNNING, f"{TOKEN_FILE} does not hold the service's token")
    return port, token


def _ask(port: int, token: str, tool_use_id: str, timeout: float) -> tuple[int, object]:
    """One request: the status and the answer's JSON (None when the body is not JSON or too long)."""
    import http.client  # here, not at the top: `contexto mcp --session` imports this module and never asks

    body = json.dumps({"tool_use_id": tool_use_id}).encode("utf-8")
    conn = http.client.HTTPConnection(SERVICE_HOST, port, timeout=timeout)
    try:
        conn.request("POST", f"/{token}{RESOLVE_PATH}", body=body,
                     headers={"Host": f"{SERVICE_HOST}:{port}", "Content-Type": "application/json",
                              "Content-Length": str(len(body)), "Connection": "close"})
        response = conn.getresponse()
        raw = response.read(MAX_ANSWER_BYTES + 1)
        status = response.status
    except ConnectionRefusedError:
        raise ResolveError(NOT_RUNNING, f"nothing is listening on port {port}") from None
    except TimeoutError:
        raise ResolveError(NOT_RUNNING, f"no answer from port {port} within {timeout:g} s") from None
    except (OSError, http.client.HTTPException) as exc:  # the text can quote the request line: name only
        raise ResolveError(NOT_RUNNING, f"the request to port {port} failed ({type(exc).__name__})") from None
    finally:
        conn.close()
    if len(raw) > MAX_ANSWER_BYTES:
        return status, None
    try:
        return status, loads_bounded(raw)
    except ValueError:
        return status, None


def resolve(home: Path, tool_use_id: str, *, clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep, timeout: float = TIMEOUT_S) -> str:
    """The Contexto session id the service gives for ``tool_use_id``, or ``ResolveError``.

    The id is what the service answered and nothing more: the caller validates it and decides whether
    that session can be opened. ``clock`` (monotonic seconds) and ``sleep`` drive the retry after a
    404, so a test injects them and does not wait."""
    port, token = read_endpoint(home)
    deadline = clock() + RETRY_WINDOW_S
    delays = iter(RETRY_DELAYS_S)
    while True:
        status, answer = _ask(port, token, tool_use_id, timeout)
        answer = answer if isinstance(answer, dict) else {}
        if status == 200:
            session = answer.get("session")
            if isinstance(session, str) and session:
                return session
            raise ResolveError(UNEXPECTED_ANSWER, "a 200 answer that names no session")
        if status == 403:
            raise ResolveError(TOKEN_REFUSED, "the service answered 403")
        if status != 404 or answer.get("error") != UNKNOWN_TOOL_USE:
            raise ResolveError(UNEXPECTED_ANSWER, f"status {status}")
        delay = next(delays, None)
        if delay is None or clock() + delay > deadline:
            raise ResolveError(UNKNOWN_TOOL_USE, "the service answered 404")
        sleep(delay)
