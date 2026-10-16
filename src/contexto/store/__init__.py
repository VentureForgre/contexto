"""The session store: paged-out turns, the page index, pinned text, and the session log.

``SessionStore`` keeps one directory per session (``$CONTEXTO_HOME/sessions/<id>/``):

- ``pages/<page id>.json``: one immutable page per paged-out turn (see ``contexto.store.pages``).
- ``page.key``: the random per-store HMAC key for page ids. It never leaves the store.
- ``log.jsonl``: the session log, one canonical JSON event per line.

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
from contexto.store.pages import build_page, render_index, render_pinned, search_pages

__all__ = ["SessionStore", "PageConflictError", "redact"]

_SECRET_KEYS = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}


class PageConflictError(RuntimeError):
    """A page file exists under an id with different content; pages are immutable."""


def redact(event: Any) -> Any:
    """Copy of ``event`` with credential-bearing keys replaced, at any depth."""
    if isinstance(event, dict):
        return {k: ("[redacted]" if str(k).lower() in _SECRET_KEYS else redact(v)) for k, v in event.items()}
    if isinstance(event, list):
        return [redact(v) for v in event]
    return event


def _write_new(path: Path, data: bytes) -> bool:
    """Create ``path`` with ``data`` atomically; False if it already exists."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.link(tmp, path)  # fails if path exists, so a page is never replaced
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


class SessionStore:
    def __init__(self, home: Path, session_id: str) -> None:
        self.session_id = session_id
        self.dir = session_dir(home, session_id)
        self.pages_dir = self.dir / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self._key = self._load_key()

    def _load_key(self) -> bytes:
        path = self.dir / "page.key"
        try:
            return bytes.fromhex(path.read_text().strip())
        except FileNotFoundError:
            pass
        key = secrets.token_bytes(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # another process created it first
            return bytes.fromhex(path.read_text().strip())
        with os.fdopen(fd, "w") as f:
            f.write(key.hex())
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
            if not _write_new(path, data) and path.read_bytes() != data:
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

    def pinned_text(self, budget_tokens: int) -> str:
        return render_pinned(budget_tokens)

    def index_text(self, budget_tokens: int) -> str:
        return render_index(self._pages(), budget_tokens)

    def log(self, event: dict) -> None:
        """Append ``event`` (credentials redacted, ``ts`` added if absent) to ``log.jsonl``."""
        record = redact(dict(event))
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        line = canonical_json(record) + "\n"
        with open(self.dir / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(line)
