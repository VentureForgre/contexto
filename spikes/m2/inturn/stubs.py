"""The hook D63 fills in: how to recognise a stub in a forwarded request.

In-turn paging takes a tool result out of the request and leaves a stub that names where the original
is stored. At v0.5.24 the engine pages only whole turns at a turn start and leaves no stub, so the
stub's format does not exist yet. ``find_stubs`` is the one place the harness needs it: the engine
pull request replaces the body, and the harness then checks every stub it finds (``harness.Stubs``).

What the harness holds a stub to:

- It names the calls it stands for. A ``StubRef`` with no ``call_ids`` fails.
- For each named call, the page ``page_id`` holds that call's original result byte for byte: a
  ``tool_result`` block (Responses: an ``*_output`` item) with that call id whose content is the
  original's, compared as canonical JSON.
- Or the page holds a stub for that call in turn (a page of several rounds keeps a result that was
  already stubbed as its stub), and following it leads to a page that holds the original. The
  harness follows the chain by calling ``find_stubs`` on the page's messages, at most
  ``harness.STUB_DEPTH`` pages deep.
- ``page_id`` may be None only for the result of one of Contexto's own search, recall and state
  calls (``contexto.store.render.own_call_ids``), which the engine may stub without storing a copy.
"""

from __future__ import annotations

from typing import NamedTuple


class StubRef(NamedTuple):
    # The store page the stub names (``SessionStore.get_page``). None: no copy was stored, which is
    # allowed only when every call in ``call_ids`` is one of Contexto's own search, recall or state calls.
    page_id: str | None
    # The tool calls whose results the stub stands for: Messages ``tool_use`` ids, Responses
    # ``call_id``s. Never empty.
    call_ids: tuple[str, ...]


def find_stubs(dialect: str, request: dict) -> list[StubRef]:
    """Every stub in ``request``. ``dialect`` is ``claude`` or ``codex``.

    ``request`` is a forwarded request exactly as the engine returned it for the upstream, or, when
    the harness follows a chain, ``{"messages": page["messages"]}`` (``claude``) or
    ``{"input": page["messages"]}`` (``codex``) for a store page: read only that one field.

    D63: replace this body. Until then the harness reports the check as not run, and the target tests
    fail on it."""
    raise NotImplementedError("D63: the stub format does not exist yet")
