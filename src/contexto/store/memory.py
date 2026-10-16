"""In-memory ``SessionStore`` stand-in with the same methods, for tests (lane A imports this path)."""

from __future__ import annotations

import secrets
import time
from typing import Any

from contexto.canonical import canonical_bytes
from contexto.store import PageConflictError, redact
from contexto.store.pages import build_page, render_index, render_pinned, search_pages


class MemoryStore:
    def __init__(self, session_id: str = "memory", *, key: bytes | None = None) -> None:
        self.session_id = session_id
        self._key = key if key is not None else secrets.token_bytes(32)
        self.pages: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    def page_out(self, turns: list[list[dict]], first_turn: int) -> list[str]:
        ids = []
        for offset, turn in enumerate(turns):
            page = build_page(self._key, turn, first_turn + offset)
            existing = self.pages.get(page["id"])
            if existing is not None and canonical_bytes(existing) != canonical_bytes(page):
                raise PageConflictError(f"page {page['id']} exists with different content")
            self.pages[page["id"]] = page
            ids.append(page["id"])
        return ids

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        return self.pages.get(page_id)

    def page_ids(self) -> list[str]:
        return [p["id"] for p in sorted(self.pages.values(), key=lambda p: (p["turn"], p["id"]), reverse=True)]

    def search(self, query: str) -> list[str]:
        return search_pages(list(self.pages.values()), query)

    def pinned_text(self, budget_tokens: int) -> str:
        return render_pinned(budget_tokens)

    def index_text(self, budget_tokens: int) -> str:
        return render_index(list(self.pages.values()), budget_tokens)

    def log(self, event: dict) -> None:
        record = redact(dict(event))
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.events.append(record)
