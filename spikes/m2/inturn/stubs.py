"""The hook D63 fills in: how to recognise a stub in a forwarded request.

In-turn paging takes a tool result out of the request and leaves a stub that names where the original
is stored. ``find_stubs`` is the one place the harness needs the stub's format (D63:
``contexto.inturn.stub_text``); the harness checks every stub it finds (``harness.Stubs``).

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

from contexto import inturn


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

    D63, Messages: a ``tool_result`` whose content is ``contexto.inturn.stub_text``'s string names the
    result page that holds its call and result. A stub of one of Contexto's own search, recall or state
    results names no page (``page_id`` None): the result was a copy of stored pages, and the stub gives
    the call's arguments. A fold note in a user message (``contexto.inturn.fold_note``) names the
    rounds page and every call it folded (its ``call ids:`` line); a result that was already a stub
    when its round was folded is a stub in that page too, which the harness follows. Codex has no
    in-turn paging yet, so no stubs."""
    if dialect != "claude":
        return []
    found = []
    for message in request.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if (isinstance(block, dict) and block.get("type") == "text" and message.get("role") == "user"
                    and inturn.note_page(block.get("text")) is not None):
                found.append(StubRef(inturn.note_page(block["text"]), inturn.note_calls(block["text"])))
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
