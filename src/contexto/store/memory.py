"""In-memory ``SessionStore`` stand-in with the same methods, for tests (lane A imports this path)."""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from contexto.store import (PageConflictError, PageNotFoundError, charge_room, check_epoch, check_model,
                             check_page_id, check_retained, check_turn_room, check_window, redact)
from contexto.store.ledger import MemoryLedger
from contexto.store.recall import lists_identifiers
from contexto.store.pages import build_page, current_stub, render_index, same_content, search_pages
from contexto.store.records import LedgerOps


class MemoryStore(LedgerOps):
    def __init__(self, session_id: str = "memory", *, key: bytes | None = None) -> None:
        self.session_id = session_id
        self._key = key if key is not None else secrets.token_bytes(32)
        self.pages: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.ledger = MemoryLedger()
        self.quarantined: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
        self._workdir: Path | None = None
        self._turn_room: tuple[str | None, int | None, int] = (None, None, 0)
        self._room_stamp: int | None = None
        self._request_seq = 0
        self._retained_stamp: int | None = None
        self._retained: tuple[int, int, int, list[dict[str, Any]]] | None = None
        self._room_lock = threading.Lock()

    def set_workdir(self, path: Path) -> None:
        self._workdir = Path(path).resolve()

    def workdir(self) -> Path | None:
        return self._workdir

    def set_window(self, window_tokens: int, bytes_per_token: float, model: str | None = None) -> None:
        check_window(window_tokens, bytes_per_token)
        self._window_model = check_model(model)
        self._window = (window_tokens, float(bytes_per_token))

    def window(self) -> tuple[int | None, float | None]:
        return getattr(self, "_window", (None, None))

    def window_model(self) -> str | None:
        return getattr(self, "_window_model", None)

    def set_mcp_listed(self) -> None:
        self._mcp_listed_at = time.time()

    def mcp_listed_at(self) -> float | None:
        return getattr(self, "_mcp_listed_at", None)

    def set_turn_room(self, turn_id: str, room_tokens: int) -> None:
        turn_id = check_turn_room(turn_id, room_tokens)
        with self._room_lock:  # every publish resets what was spent (see SessionStore.set_turn_room)
            self._turn_room = (turn_id, room_tokens, 0)
            self._room_stamp = self._retained_stamp

    def set_retained(self, epoch: int, k: int, turns: list[dict[str, Any]]) -> None:
        entries = check_retained(epoch, k, turns)
        with self._room_lock:
            self._request_seq += 1
            self._retained_stamp = self._request_seq
            self._retained = (epoch, k, self._request_seq, [dict(e) for e in entries])

    def retained(self) -> tuple[int, int, int, list[dict[str, Any]]] | None:
        return self._retained

    def set_epoch(self, record: dict[str, Any]) -> None:
        self._epoch = check_epoch(record)

    def epoch_record(self) -> dict[str, Any] | None:
        epoch = getattr(self, "_epoch", None)
        return None if epoch is None else json.loads(json.dumps(epoch))  # a copy: callers cannot change it

    def retained_current(self) -> tuple[int, int, int, list[dict[str, Any]]] | None:
        r = self._retained
        return r if r is not None and r[2] == self._request_seq == self._room_stamp else None

    def room_stamp(self) -> int | None:
        return self._room_stamp

    def turn_room(self) -> tuple[str | None, int | None, int]:
        return self._turn_room

    def charge_turn_room(self, charge: Callable[[int | None], int]) -> None:
        with self._room_lock:
            record = charge_room(self._turn_room, charge)
            if record is not None:
                self._turn_room = (record["turn_id"], record["room_tokens"], record["spent_tokens"])

    def page_out(self, turns: list[list[dict]], first_turn: int) -> list[str]:
        ids = []
        for offset, turn in enumerate(turns):
            page = build_page(self._key, turn, first_turn + offset)
            existing = self.pages.get(page["id"])
            if existing is None:
                self.pages[page["id"]] = page
            elif not same_content(existing, page):  # same id, different turn or messages
                raise PageConflictError(f"page {page['id']} exists with different turn or messages",
                                        page_id=page["id"])
            ids.append(page["id"])
        return ids

    def quarantine_page(self, page_id: str) -> Path:
        """In-memory counterpart of ``SessionStore.quarantine_page``; the returned path is a label."""
        check_page_id(page_id)
        page = self.pages.pop(page_id, None)
        if page is None:
            earlier = self.quarantined.get(page_id)
            if not earlier:
                raise PageNotFoundError(f"no stored or quarantined page {page_id}")
            return earlier[-1][0]
        path = Path("quarantine") / f"{page_id}.{time.time_ns()}.json"
        self.quarantined.setdefault(page_id, []).append((path, page))
        self.log({"event": "page_quarantined", "page_id": page_id, "path": str(path)})
        return path

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        page = self.pages.get(page_id)
        return None if page is None else current_stub(page)

    def _pages(self) -> list[dict[str, Any]]:
        return [current_stub(p) for p in self.pages.values()]

    def page_ids(self) -> list[str]:
        return [p["id"] for p in sorted(self.pages.values(), key=lambda p: (p["turn"], p["id"]), reverse=True)]

    def search(self, query: str) -> list[str]:
        return search_pages(self._pages(), query)

    def index_text(self, budget_tokens: int, variant: str | None = None) -> str:
        return render_index(self._pages(), budget_tokens,
                            salient=lists_identifiers(self.recall_variant(variant)))

    def _recorded_variant(self) -> str | None:
        return getattr(self, "_variant", None)

    def _record_variant(self, variant: str) -> None:
        self._variant = variant

    def log(self, event: dict) -> None:
        record = redact(dict(event))
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.events.append(record)
