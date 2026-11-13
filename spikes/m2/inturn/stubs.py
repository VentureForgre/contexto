"""The hook D63 fills in: how to recognise a stub in a forwarded request.

In-turn paging takes content out of the request and leaves a stub that names where the original is
stored. ``find_stubs`` is the one place the harness needs the stub's format (D63:
``contexto.inturn.stub_text`` and ``fold_note``); the target tests check that every stub's original
can be read back from the session store (``harness.Run.check_stubs``).
"""

from __future__ import annotations

from typing import NamedTuple

from contexto import inturn


class StubRef(NamedTuple):
    # The store page the stub names (``SessionStore.get_page``); None only for the result of one of
    # Contexto's own tools, which has no page.
    page_id: str | None
    # The tool calls whose results the stub stands for: Messages ``tool_use`` ids, Responses
    # ``call_id``s. The harness checks that the page holds each one's original result. Leave it empty
    # for a stub that names no call; then only the page's existence is checked.
    call_ids: tuple[str, ...] = ()


def find_stubs(dialect: str, request: dict) -> list[StubRef]:
    """Every stub in one forwarded request. ``dialect`` is ``claude`` (a Messages request) or
    ``codex`` (a Responses request); ``request`` is exactly what the engine returned for the upstream.

    D63, Messages: a ``tool_result`` whose content is ``contexto.inturn.stub_text``'s string names the
    result page that holds its call and result, and a fold note in a user message names the rounds page
    that holds the folded tool rounds (no call is named: a result folded with its round may itself be a
    stub there, its original in its own result page). A stub of one of Contexto's own search, recall or
    state results names no page (``page_id`` None): the result was a copy of stored pages, and the stub
    gives the call's arguments. Codex has no in-turn paging yet, so no stubs."""
    if dialect != "claude":
        return []
    found = []
    for message in request.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                page = inturn.stub_page(block.get("content"))
                if page is not None:
                    found.append(StubRef(page, (block["tool_use_id"],)))
                elif inturn.is_own_stub(block.get("content")):
                    found.append(StubRef(None, (block["tool_use_id"],)))
            elif block.get("type") == "text" and message.get("role") == "user":
                page = inturn.note_page(block.get("text"))
                if page is not None:
                    found.append(StubRef(page))
    return found
