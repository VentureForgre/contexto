"""The hook D63 fills in: how to recognise a stub in a forwarded request.

In-turn paging takes content out of the request and leaves a stub that names where the original is
stored. At v0.5.24 the engine pages only whole turns at a turn start and leaves no stub, so the stub's
format does not exist yet. ``find_stubs`` is the one place the harness needs it: the engine pull
request replaces the body, and the target tests then check that every stub's original can be read
back from the session store (``harness.Run.check_stubs``).
"""

from __future__ import annotations

from typing import NamedTuple


class StubRef(NamedTuple):
    page_id: str  # the store page the stub names (``SessionStore.get_page``)
    # The tool calls whose results the stub stands for: Messages ``tool_use`` ids, Responses
    # ``call_id``s. The harness checks that the page holds each one's original result. Leave it empty
    # for a stub that names no call; then only the page's existence is checked.
    call_ids: tuple[str, ...] = ()


def find_stubs(dialect: str, request: dict) -> list[StubRef]:
    """Every stub in one forwarded request. ``dialect`` is ``claude`` (a Messages request) or
    ``codex`` (a Responses request); ``request`` is exactly what the engine returned for the upstream.

    D63: replace this body. Until then the harness reports the check as not run, and the target tests
    fail on it."""
    raise NotImplementedError("D63: the stub format does not exist yet")
