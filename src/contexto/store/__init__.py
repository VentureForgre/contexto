"""The session store: paged-out turns, the page index, pinned text, and the session log.

``SessionStore`` keeps one directory per session (``$CONTEXTO_HOME/sessions/<id>/``):

Directories are 0700 and files 0600 whatever the umask (``contexto.private``, contract PR #25).

- ``pages/<page id>.json``: one immutable page per paged-out turn (see ``contexto.store.pages``).
- ``page.key``: the random per-store HMAC key for page ids. It never leaves the store.
- ``ledger.jsonl`` (with ``ledger.lock`` and ``quarantine/``): the hash-chained session ledger.
  Opening a store repairs a crash-truncated tail and logs ``ledger_repaired``; other damage is
  left for a human and logged as ``ledger_damaged`` (pins and closes then fail).
- ``meta.json``: session metadata, today ``{"workdir": ...}``, the directory file sources are
  checked against. Lane A's launcher records it with ``set_workdir`` at session start.
- ``log.jsonl``: the session log, one canonical JSON event per line.

Ledger methods (``pin``, ``close``, ``state``, ``state_text``, ``pinned_text``) come from
``contexto.store.records.LedgerOps``; see that module for the API the MCP server uses.

``contexto.store.memory.MemoryStore`` has the same methods and keeps everything in memory.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from contexto.canonical import canonical_bytes, canonical_json
from contexto.paths import session_dir
from contexto.private import open_private, private_dir, write_new
from contexto.store.ledger import Ledger, LedgerCorruptError
from contexto.store.pages import build_page, render_index, same_content, search_pages
from contexto.store.records import LedgerOps

__all__ = ["SessionStore", "PageConflictError", "PageNotFoundError", "KeyFileError", "SessionNotFoundError",
           "redact"]

_PAGE_ID = re.compile(r"pg_[0-9a-f]{16}")


def check_page_id(page_id: Any) -> str:
    """``page_id`` if it is a well-formed page id; ValueError before any path is built from it."""
    if not isinstance(page_id, str) or not _PAGE_ID.fullmatch(page_id):
        raise ValueError(f"not a page id: {page_id!r}")
    return page_id

KEY_BYTES = 32

_SECRET_KEYS = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}


class PageConflictError(RuntimeError):
    """A page file exists under an id with different content; pages are immutable."""


class SessionNotFoundError(LookupError):
    """``SessionStore.open_existing`` was asked for a session that does not exist."""


class PageNotFoundError(LookupError):
    """``quarantine_page`` was given a page that is neither stored nor already quarantined."""


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
        # Narrow an existing wide CONTEXTO_HOME and sessions/ too, not only directories we create.
        private_dir(Path(home))
        private_dir(Path(home) / "sessions")
        private_dir(self.dir)
        private_dir(self.pages_dir)
        self._key_cache = self._load_key()
        self.ledger = Ledger(self.dir)
        self._repair_ledger()

    def _repair_ledger(self) -> None:
        if not self.ledger.path.exists():
            return
        try:
            quarantined = self.ledger.repair()
        except LedgerCorruptError as e:
            # Class and position only: the message can quote stored values.
            line = re.search(r"\bline (\d+)\b", str(e))
            self.log({"event": "ledger_damaged", "error": type(e).__name__,
                      "line": int(line.group(1)) if line else None})
            return
        if quarantined is not None:
            self.log({"event": "ledger_repaired", "quarantine": quarantined.name})

    def set_workdir(self, path: Path) -> None:
        """Record the session's working directory (absolute), which file sources are checked against."""
        meta = self._meta()
        meta["workdir"] = str(Path(path).resolve())
        tmp = self.dir / f".meta.json.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = open_private(tmp, os.O_WRONLY | os.O_EXCL)
        try:
            os.write(fd, canonical_bytes(meta))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.dir / "meta.json")

    def _meta(self) -> dict[str, Any]:
        try:
            meta = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):  # missing, unreadable, not UTF-8, or not JSON: treat as unrecorded
            return {}
        return meta if isinstance(meta, dict) else {}

    def workdir(self) -> Path | None:
        value = self._meta().get("workdir")
        return Path(value) if isinstance(value, str) and value else None

    @classmethod
    def open_existing(cls, home: Path, session_id: str) -> SessionStore:
        """Open a session for reading without creating anything; SessionNotFoundError if it is absent.

        Writing pages through a store opened this way loads (or creates) the page key on first use.
        """
        path = session_dir(home, session_id)
        if not path.is_dir():
            raise SessionNotFoundError(f"no session {session_id!r} under {Path(home) / 'sessions'}")
        store = cls.__new__(cls)
        store.session_id = session_id
        store.dir = path
        store.pages_dir = path / "pages"
        store._key_cache = None
        store.ledger = Ledger(path)
        return store

    @property
    def _key(self) -> bytes:
        if self._key_cache is None:
            private_dir(self.pages_dir)
            self._key_cache = self._load_key()
        return self._key_cache

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

        Re-paging an existing id is a no-op that keeps the stored page, even if the stub or the
        estimates were computed differently when it was written; ``PageConflictError`` only when the
        stored ``{turn, messages}`` differ under the same id (contract PR #47).
        """
        ids = []
        for offset, turn in enumerate(turns):
            page = build_page(self._key, turn, first_turn + offset)
            path = self.pages_dir / f"{page['id']}.json"
            data = canonical_bytes(page)
            if not write_new(path, data):
                try:
                    stored = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    raise PageConflictError(f"{path} exists but cannot be read: {e}") from e
                if not same_content(stored, page):
                    raise PageConflictError(f"{path} exists with different turn or messages")
            ids.append(page["id"])
        return ids

    def quarantine_page(self, page_id: str) -> Path:
        """Move a stored page aside so the same id can be paged afresh (contract rule 14, PR #52).

        ``pages/<id>.json`` is renamed atomically to ``quarantine/<id>.<UTC timestamp>.<suffix>.json``
        (directory 0700, file 0600); its content is never changed or deleted. Returns the quarantine
        path and logs ``page_quarantined`` with the page id and file name only. Idempotent: if the
        page is no longer stored but was quarantined before, returns its most recent quarantine
        path; if it was never stored, raises ``PageNotFoundError``. Concurrent calls move the file
        once and all return that path.
        """
        check_page_id(page_id)
        source = self.pages_dir / f"{page_id}.json"
        quarantine = self.dir / "quarantine"
        private_dir(quarantine)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time_ns() % 1_000_000_000:09d}Z"
        target = quarantine / f"{page_id}.{stamp}.{secrets.token_hex(4)}.json"
        try:
            os.rename(source, target)  # atomic on one filesystem; the unique name never overwrites
        except FileNotFoundError:
            earlier = self._quarantined(page_id)
            if earlier is None:
                raise PageNotFoundError(f"no stored or quarantined page {page_id}") from None
            return earlier
        os.chmod(target, 0o600)
        fd = os.open(quarantine, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.log({"event": "page_quarantined", "page_id": page_id, "path": f"quarantine/{target.name}"})
        return target

    def _quarantined(self, page_id: str) -> Path | None:
        found = sorted((self.dir / "quarantine").glob(f"{page_id}.*.json"))
        return found[-1] if found else None

    def _pages(self) -> list[dict[str, Any]]:
        out = []
        if not self.pages_dir.is_dir():
            return out
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
