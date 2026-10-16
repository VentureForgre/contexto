"""The session store: paged-out turns, the page index, pinned text, and the session log.

``SessionStore`` keeps one directory per session (``$CONTEXTO_HOME/sessions/<id>/``):

Directories are 0700 and files 0600 whatever the umask (``contexto.private``, contract PR #25).

- ``pages/<page id>.json``: one immutable page per paged-out turn (see ``contexto.store.pages``).
- ``page.key``: the random per-store HMAC key for page ids. It never leaves the store.
- ``ledger.jsonl`` (with ``ledger.lock`` and ``quarantine/``): the hash-chained session ledger.
- ``log.jsonl``: the session log, one canonical JSON event per line.

Ledger methods (``pin``, ``close``, ``state``, ``state_text``, ``pinned_text``) come from
``contexto.store.records.LedgerOps``; see that module for the API the MCP server uses.

``contexto.store.memory.MemoryStore`` has the same methods and keeps everything in memory.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from contexto.canonical import canonical_bytes, canonical_json
from contexto.paths import session_dir
from contexto.private import open_private, private_dir, write_new
from contexto.store.ledger import Ledger
from contexto.store.pages import build_page, render_index, search_pages
from contexto.store.records import LedgerOps

__all__ = ["SessionStore", "PageConflictError", "KeyFileError", "redact"]

KEY_BYTES = 32

_SECRET_KEYS = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}


class PageConflictError(RuntimeError):
    """A page file exists under an id with different content; pages are immutable."""


class KeyFileError(RuntimeError):
    """The store's page key file is empty, short, or not hex; page ids cannot be trusted."""


def redact(event: Any) -> Any:
    """Copy of ``event`` with credential-bearing keys replaced, at any depth.

    Redaction is by key name only (``authorization``, ``x-api-key``, cookies). A credential inside
    a string value under another key is not detected, so callers must not log raw header blobs
    or request bodies.
    """
    if isinstance(event, dict):
        return {k: ("[redacted]" if str(k).lower() in _SECRET_KEYS else redact(v)) for k, v in event.items()}
    if isinstance(event, list):
        return [redact(v) for v in event]
    return event


class SessionStore(LedgerOps):
    def __init__(self, home: Path, session_id: str) -> None:
        self.session_id = session_id
        self.dir = session_dir(home, session_id)
        self.pages_dir = self.dir / "pages"
        private_dir(self.dir)
        private_dir(self.pages_dir)
        self._key = self._load_key()
        self.ledger = Ledger(self.dir)

    def _load_key(self) -> bytes:
        """The store's HMAC key, created atomically on first use; refuses an empty or short key."""
        path = self.dir / "page.key"
        if not path.exists():
            # A losing racer's write_new returns False and reads the winner's complete key below.
            write_new(path, secrets.token_bytes(KEY_BYTES).hex().encode("ascii"))
        try:
            key = bytes.fromhex(path.read_text(encoding="ascii").strip())
        except (ValueError, UnicodeDecodeError) as e:
            raise KeyFileError(f"{path} is not a hex key: {e}") from e
        if len(key) != KEY_BYTES:
            raise KeyFileError(f"{path} holds a {len(key)}-byte key, expected {KEY_BYTES}")
        return key

    def page_out(self, turns: list[list[dict]], first_turn: int) -> list[str]:
        """Write one page per turn (turn numbers from ``first_turn``); return their ids in order.

        Re-writing a turn with identical content is a no-op that returns the same id.
        """
        ids = []
        for offset, turn in enumerate(turns):
            page = build_page(self._key, turn, first_turn + offset)
            path = self.pages_dir / f"{page['id']}.json"
            data = canonical_bytes(page)
            if not write_new(path, data) and path.read_bytes() != data:
                raise PageConflictError(f"{path} exists with different content")
            ids.append(page["id"])
        return ids

    def _pages(self) -> list[dict[str, Any]]:
        out = []
        for path in self.pages_dir.glob("pg_*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as e:
                self.log({"event": "page_unreadable", "path": path.name, "error": str(e)})
        return out

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        """The page record for ``page_id``, or None if this store has no such page."""
        if not page_id.startswith("pg_") or not all(c in "0123456789abcdef" for c in page_id[3:]):
            return None
        try:
            return json.loads((self.pages_dir / f"{page_id}.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def page_ids(self) -> list[str]:
        """Every page id in this store, newest turn first."""
        return [p["id"] for p in sorted(self._pages(), key=lambda p: (p["turn"], p["id"]), reverse=True)]

    def search(self, query: str) -> list[str]:
        """Ids of pages whose stub or text contains ``query`` exactly, newest first."""
        return search_pages(self._pages(), query)

    def index_text(self, budget_tokens: int) -> str:
        return render_index(self._pages(), budget_tokens)

    def log(self, event: dict) -> None:
        """Append ``event`` (credentials redacted, ``ts`` added if absent) to ``log.jsonl``."""
        record = redact(dict(event))
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        line = (canonical_json(record) + "\n").encode("utf-8")
        fd = open_private(self.dir / "log.jsonl", os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
