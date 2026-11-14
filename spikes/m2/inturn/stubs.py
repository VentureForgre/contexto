"""The hook D63 fills in: how to recognise a stub in a forwarded request.

In-turn paging takes a tool result out of the request and leaves a stub that names where the original
is stored. At v0.5.24 the engine pages only whole turns at a turn start and leaves no stub, so the
stub's format does not exist yet. ``find_stubs`` is the one place the harness needs it: the engine
pull request replaces the body, and the harness then checks every stub it finds (``harness.Stubs``).

What the harness holds a stub to:

- It names the calls it stands for, or, with empty ``call_ids``, a page as a whole (a fold note naming
  a rounds or folds page). A reference to a page as a whole covers every call reachable from that
  page; one whose page reaches no delivered result fails.
- A call is reachable from a page when the page holds that call's original result byte for byte (a
  ``tool_result`` block, Responses: an ``*_output`` item, with that call id whose content is the
  original's, compared as canonical JSON), or holds a stub or fold reference whose page leads to it.
  A page of several rounds keeps a result that was already stubbed as its stub. The harness follows
  the chain by calling ``find_stubs`` on the page's messages, at most ``harness.STUB_DEPTH`` (8)
  pages deep. For each call a reference names, that call has to be reachable from its page.
- ``page_id`` may be None only for the result of one of Contexto's own search, recall and state
  calls (``contexto.store.render.own_call_ids``), which the engine may stub without storing a copy.

``targets.storage`` says when a result left out of a request is accounted for.
"""

from __future__ import annotations

from typing import NamedTuple


class StubRef(NamedTuple):
    # The store page the stub names (``SessionStore.get_page``). None: no copy was stored, which is
    # allowed only when every call in ``call_ids`` is one of Contexto's own search, recall or state calls.
    page_id: str | None
    # The tool calls whose results the stub stands for: Messages ``tool_use`` ids, Responses
    # ``call_id``s. Empty: the reference names ``page_id`` as a whole and covers every call reachable
    # from it (a fold note naming a rounds or folds page).
    call_ids: tuple[str, ...]


def find_stubs(dialect: str, request: dict) -> list[StubRef]:
    """Every stub in ``request``. ``dialect`` is ``claude`` or ``codex``.

    ``request`` is a forwarded request exactly as the engine returned it for the upstream, or, when
    the harness follows a chain, ``{"messages": page["messages"]}`` (``claude``) or
    ``{"input": page["messages"]}`` (``codex``) for a store page: read only that one field.

    D63: replace this body. Until then the harness reports the check as not run, and the target tests
    fail on it."""
    raise NotImplementedError("D63: the stub format does not exist yet")
