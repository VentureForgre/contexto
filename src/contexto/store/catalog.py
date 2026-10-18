"""What search and the index read from each page file, kept for the life of the process (D34).

Pages are immutable, so what a page file yields never changes while the file stays the same.
``PageCatalog`` lists ``pages/`` on every call (one ``scandir`` and a ``stat`` per file) and reads
only the files it has not seen, or whose inode, size, or modification time changed; files that
are gone (quarantined) are dropped. Nothing is written to the store: the format is unchanged and
sessions written before D34 are read as they are.

Each process keeps its own catalog, because the proxy and the MCP server are separate processes:

- the proxy calls ``index_text`` twice per rotation. It needs each page's id, turn, and stub, and
  the line of only the newest pages that fit the index budget. The pages it writes itself are
  added by ``page_out`` without a read; after a restart each page is read once.
- the MCP server calls ``search``, which also needs each page's folded searchable text. It is read
  once per page and held in memory as UTF-8 bytes, about the UTF-8 size of the page's text.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contexto.store.pages import current_stub, index_line, search_entry
from contexto.store.search import SearchEntry

Signature = tuple[int, int, int]  # inode, size, modification time (ns)


@dataclass
class Entry:
    signature: Signature
    view: dict[str, Any]  # id, turn, stub: what the index line reads
    search: SearchEntry | None = None  # what search reads (folded text as UTF-8), read when search first needs it
    lines: dict[bool, str] = field(default_factory=dict)  # the index line, by whether it lists identifiers

    @property
    def id(self) -> str:
        return self.view["id"]

    @property
    def turn(self) -> int:
        return self.view["turn"]


def _signature(stat: os.stat_result) -> Signature:
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


class PageCatalog:
    def __init__(self, pages_dir: Path, log: Callable[[dict[str, Any]], None]) -> None:
        self.pages_dir = pages_dir
        self._log = log
        self._entries: dict[str, Entry] = {}  # by file name
        self._lock = threading.Lock()

    def entries(self, texts: bool = False) -> list[Entry]:
        """Every readable page, with its searchable text when ``texts``. An unreadable page is logged
        ``page_unreadable`` on every call, as before, and left out."""
        with self._lock:
            listing: dict[str, Signature] = {}
            try:
                with os.scandir(self.pages_dir) as it:
                    for de in it:
                        if de.name.startswith("pg_") and de.name.endswith(".json"):
                            try:
                                listing[de.name] = _signature(de.stat())
                            except FileNotFoundError:  # quarantined between the listing and the stat
                                continue
            except FileNotFoundError:
                listing = {}
            for name in [n for n in self._entries if n not in listing]:
                del self._entries[name]
            out = []
            for name, signature in listing.items():
                entry = self._entries.get(name)
                if entry is None or entry.signature != signature or (texts and entry.search is None):
                    entry = self._load(name, signature, texts)
                    if entry is None:
                        self._entries.pop(name, None)
                        continue
                    self._entries[name] = entry
                out.append(entry)
            return out

    def add(self, path: Path, page: dict[str, Any]) -> None:
        """Record a page this process has just written or found stored, without reading it again."""
        try:
            signature = _signature(path.stat())
        except OSError:
            return  # the next listing reads it
        try:
            view = _view(current_stub(page))
        except (KeyError, TypeError, AttributeError):
            return  # a stored page with a malformed stub: the next listing reads it, and logs it
        with self._lock:
            known = self._entries.get(path.name)
            if known is None or known.signature != signature:
                self._entries[path.name] = Entry(signature, view)

    def line(self, entry: Entry, salient: bool) -> str:
        """The page's index line, built once per page; listing identifiers reads the page's text."""
        line = entry.lines.get(salient)
        if line is not None:
            return line
        page = self._read(self.pages_dir / f"{entry.id}.json") if salient else entry.view
        if page is None:  # unreadable now (logged): the line without identifiers, not kept
            return index_line(entry.view, False)
        line = entry.lines[salient] = index_line(page, salient)
        return line

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            page = current_stub(json.loads(path.read_text(encoding="utf-8")))
            _view(page)  # the fields every reader needs
            return page
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self._log({"event": "page_unreadable", "path": path.name, "error": str(e)})
            return None

    def _load(self, name: str, signature: Signature, texts: bool) -> Entry | None:
        page = self._read(self.pages_dir / name)
        if page is None:
            return None
        try:
            search = search_entry(page) if texts else None
        except (KeyError, TypeError, AttributeError) as e:
            self._log({"event": "page_unreadable", "path": name, "error": str(e)})
            return None
        return Entry(signature, _view(page), search)


def _view(page: dict[str, Any]) -> dict[str, Any]:
    stub = page["stub"]
    if not isinstance(stub, dict):
        raise TypeError("page stub is not an object")
    return {"id": page["id"], "turn": page["turn"], "stub": stub}
