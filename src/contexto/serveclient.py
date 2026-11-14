"""Client of the Contexto service's resolve endpoint: which session does a tool call belong to? (D70)

``contexto serve`` routes each Claude Code conversation to its own session store, and it reads every
model response, so it knows which session each ``tool_use`` id belongs to. ``contexto mcp --serve``
(``contexto.mcp.ServeServer``) is one MCP server for every conversation of a Claude Code process: it
gets the ``tool_use`` id with each ``tools/call`` and asks the service here.

How the service is found (``contexto.servecontract``, which the service shares): it holds a lock on
``serve.lock`` in ``$CONTEXTO_HOME`` for as long as it runs, and keeps ``serve.json`` (its port) and
``serve.token`` (the token it requires) there. The lock is checked first, at every call, so a
restarted service is found without restarting the MCP server, and with no service running nothing is
read and nothing is sent: a ``serve.json`` left by a service that was killed is never believed.

The request: ``POST http://127.0.0.1:<port>/<token>/contexto/resolve`` with the JSON body
``{"tool_use_id": "<id>"}``. The answers: 200 ``{"session": "<Contexto session id>"}``, 404
``{"error": "unknown_tool_use"}``, 400 for a malformed body, 403 for a bad token. The connection is
made with ``http.client`` to ``127.0.0.1`` only: it reads no proxy setting from the environment, sends
``Host: 127.0.0.1:<port>`` and never an ``Origin``.

A 404 is asked again a few times inside ``RETRY_WINDOW_S``. The service registers a tool call's id
before it passes the call on wherever it reads the response as it relays it; the retry covers the
responses where it cannot promise that order (``docs/serve.md``, "Endpoints of the service itself").
Nothing else is retried.

What is never sent or said: the token goes into the request path and nowhere else, and only to the
port recorded by a service that holds the lock. ``ResolveError`` holds a kind and a fixed phrase,
never the token, the request path, a file's content, or the text of an exception that could quote
one of them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from contexto.canonical import loads_bounded
from contexto.servecontract import (
    RESOLVE_PATH,
    SERVICE_HOST,
    UNKNOWN_TOOL_USE,  # also this module's kind for a 404 that outlasts the retries
    ServeFileError,
    endpoint,
    found_session,
    is_unknown,
    local_target,
    resolve_request,
)

TIMEOUT_S = 2.0  # connect and each read; the service is on this machine and answers from memory
# Waits between tries after a 404: six tries over 1.9 s when the service answers at once. A wait that
# would end after RETRY_WINDOW_S, measured on the caller's clock from the first try, is not started.
RETRY_DELAYS_S = (0.1, 0.2, 0.3, 0.5, 0.8)
RETRY_WINDOW_S = 2.0
MAX_ANSWER_BYTES = 65536

NOT_RUNNING = "not_running"  # no service holds the lock, its files cannot be used, or nothing answers on the port
TOKEN_REFUSED = "token_refused"  # 403
UNEXPECTED_ANSWER = "unexpected_answer"  # any other answer: another version, or another program on the port


class ResolveError(Exception):
    """The service did not name a session. ``kind`` is one of the four constants above and ``reason``
    a short fixed phrase a tool error can show; neither holds the token or file content."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


def read_endpoint(home: Path) -> tuple[int, str]:
    """The running service's port and token (``contexto.servecontract.endpoint``).

    ``ResolveError(NOT_RUNNING)`` when no service holds ``home``'s lock (nothing else is then read),
    and when ``serve.json`` or ``serve.token`` is missing, is not the user's own private regular
    file, or does not hold what the service writes. The reason names the file, never its content."""
    try:
        return endpoint(home)
    except ServeFileError as exc:
        raise ResolveError(NOT_RUNNING, f"{exc.name} {exc.problem}") from None


def _ask(port: int, token: str, tool_use_id: str, timeout: float) -> tuple[int, object]:
    """One request: the status and the answer's JSON (None when the body is not JSON or too long)."""
    import http.client  # here, not at the top: `contexto mcp --session` imports this module and never asks

    body = resolve_request(tool_use_id)
    conn = http.client.HTTPConnection(SERVICE_HOST, port, timeout=timeout)
    try:
        conn.request("POST", local_target(token, RESOLVE_PATH), body=body,
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
        if status == 200:
            session = found_session(answer)
            if session is not None:
                return session
            raise ResolveError(UNEXPECTED_ANSWER, "a 200 answer that names no session")
        if status == 403:
            raise ResolveError(TOKEN_REFUSED, "the service answered 403")
        if status != 404 or not is_unknown(answer):
            raise ResolveError(UNEXPECTED_ANSWER, f"status {status}")
        delay = next(delays, None)
        if delay is None or clock() + delay > deadline:
            raise ResolveError(UNKNOWN_TOOL_USE, "the service answered 404")
        sleep(delay)
