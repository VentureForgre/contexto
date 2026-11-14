"""The hook D63 fills in: how to recognise a stub in a forwarded request.

In-turn paging takes a tool result out of the request and leaves a stub that names where the original
is stored. ``find_stubs`` is the one place the harness needs the stub's format (D63:
``contexto.inturn.stub_text`` and ``turn_note``); the harness checks every stub it finds (``harness.Stubs``).

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

from contexto import inturn


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

    D63, Messages: a ``tool_result`` whose content is ``contexto.inturn.stub_text``'s string names the
    result page that holds its call and result. A stub of one of Contexto's own search, recall or state
    results names no page (``page_id`` None): the result was a copy of stored pages, and the stub gives
    the call's arguments. A turn's fold note in a user message (``contexto.inturn.turn_note``) names the
    newest fold's rounds page, each earlier fold's rounds page, and the folds pages that list the oldest
    folds; each is a reference to the page as a whole, and the ids the note lists for a page (the newest
    folds' only, within its cap) are a reference to those calls as well. A rounds page holds its rounds
    as sent (a result that was already a stub there is its stub); a folds page of level 0 holds each
    listed fold's note with every call id, and one of a higher level a line in fold-note form for each
    folds page it names, which this function reads the same way. Codex has no in-turn paging yet, so no
    stubs."""
    if dialect != "claude":
        return []
    found = []
    for message in request.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if (isinstance(block, dict) and block.get("type") == "text" and message.get("role") == "user"
                    and inturn.note_page(block.get("text")) is not None):
                for page, ids in inturn.note_refs(block["text"]):
                    found.append(StubRef(page, ()))
                    if ids:
                        found.append(StubRef(page, ids))
                continue
            if not (isinstance(block, dict) and block.get("type") == "tool_result"
                    and isinstance(block.get("tool_use_id"), str)):
                continue
            page = inturn.stub_page(block.get("content"))
            if page is not None:
                found.append(StubRef(page, (block["tool_use_id"],)))
            elif inturn.is_own_stub(block.get("content")):
                found.append(StubRef(None, (block["tool_use_id"],)))
    return found
