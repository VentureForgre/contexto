"""In-memory ``SessionStore`` stand-in with the same methods, for tests (lane A imports this path)."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

from contexto.store import PageConflictError, check_window, redact
from contexto.store.ledger import MemoryLedger
from contexto.store.pages import build_page, render_index, same_content, search_pages
from contexto.store.records import LedgerOps


class MemoryStore(LedgerOps):
    def __init__(self, session_id: str = "memory", *, key: bytes | None = None) -> None:
        self.session_id = session_id
        self._key = key if key is not None else secrets.token_bytes(32)
        self.pages: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.ledger = MemoryLedger()
        self._workdir: Path | None = None

    def set_workdir(self, path: Path) -> None:
        self._workdir = Path(path).resolve()

    def workdir(self) -> Path | None:
        return self._workdir

    def set_window(self, window_tokens: int, bytes_per_token: float) -> None:
        check_window(window_tokens, bytes_per_token)
        self._window = (window_tokens, float(bytes_per_token))

    def window(self) -> tuple[int | None, float | None]:
        return getattr(self, "_window", (None, None))

    def page_out(self, turns: list[list[dict]], first_turn: int) -> list[str]:
        ids = []
        for offset, turn in enumerate(turns):
            page = build_page(self._key, turn, first_turn + offset)
            existing = self.pages.get(page["id"])
            if existing is None:
                self.pages[page["id"]] = page
            elif not same_content(existing, page):  # same id, different turn or messages
                raise PageConflictError(f"page {page['id']} exists with different turn or messages")
            ids.append(page["id"])
        return ids

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        return self.pages.get(page_id)

    def page_ids(self) -> list[str]:
        return [p["id"] for p in sorted(self.pages.values(), key=lambda p: (p["turn"], p["id"]), reverse=True)]

    def search(self, query: str) -> list[str]:
        return search_pages(list(self.pages.values()), query)

    def index_text(self, budget_tokens: int) -> str:
        return render_index(list(self.pages.values()), budget_tokens)

    def log(self, event: dict) -> None:
        record = redact(dict(event))
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.events.append(record)
