"""What search and the index read from each page file, kept per process (D34), with search text held
up to a byte budget (D39).

Pages are immutable, so what a page file yields never changes while the file stays the same.
``PageCatalog`` lists ``pages/`` on every call (one ``scandir`` and a ``stat`` per file) and reads
only the files it has not seen, or whose inode, size, or modification time changed; files that
are gone (quarantined) are dropped. Nothing is written to the store: the format is unchanged and
sessions written before D34 are read as they are.

Each process keeps its own catalog, because the proxy and the MCP server are separate processes:

- the proxy calls ``index_text`` twice per rotation. It needs each page's id, turn, and stub, and
  the line of only the newest pages that fit the index budget. The pages it writes itself are
  added by ``page_out`` without a read; after a restart each page is read once.
- the MCP server calls ``search``, which needs each page's folded searchable text (UTF-8 bytes,
  about the UTF-8 size of the page's text). The catalog keeps each page's id, turn, and whether it
  holds only context blocks for the life of the process (about 700 bytes per page; not the stub,
  which only the index reads), and holds search text for at most
  ``text_budget`` bytes: the newest pages' text, by turn. Every search reads every page's text (a
  page matches when each query word is a substring of it), so a page whose text is not held is read
  again from its file during the search and let go after it. Oldest out first, rather than least
  recently used, because a search reads every page in the same order: under LRU a store larger than
  the budget would evict each page just before the next search needs it, and no read would be saved.
  The budget is ``CONTEXTO_SEARCH_CACHE_MB`` (default 64), or ``contexto mcp --search-cache-mb``.
"""

from __future__ import annotations

import heapq
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

ENV_VAR = "CONTEXTO_SEARCH_CACHE_MB"
DEFAULT_TEXT_BUDGET_MB = 64
MB = 1024 * 1024


def text_budget_bytes(value: str | float | None) -> int:
    """The search text budget in bytes from a number of megabytes (1 MB = 1,048,576 bytes); None or an
    empty string is the default. Raises ValueError for anything that is not a number 0 or above (0
    holds no text: every search reads every page)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_TEXT_BUDGET_MB * MB
    try:
        mb = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{ENV_VAR} must be a number of megabytes, 0 or more; got {value!r}") from None
    if not (mb >= 0 and mb != float("inf")):  # also rejects NaN
        raise ValueError(f"{ENV_VAR} must be a number of megabytes, 0 or more; got {value!r}")
    return int(mb * MB)


@dataclass
class Entry:
    signature: Signature
    # id, turn, stub: what the index line and page ids read; None for a page read only for search (the
    # MCP server never builds the index, and a stub is about 2.4 KB of objects per page)
    view: dict[str, Any] | None
    # What search reads: id, turn, and whether the page holds only context blocks, from its first read
    # for search; ``text`` (folded, UTF-8) only while the budget holds it.
    search: SearchEntry | None = None
    held: int = 0  # the admission number of the held text, 0 when no text is held
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
    def __init__(self, pages_dir: Path, log: Callable[[dict[str, Any]], None],
                 text_budget: int | None = DEFAULT_TEXT_BUDGET_MB * MB) -> None:
        self.pages_dir = pages_dir
        self._log = log
        self._entries: dict[str, Entry] = {}  # by file name
        self._lock = threading.Lock()
        self._budget = text_budget  # None: no limit
        self._held_bytes = 0
        self._oldest: list[tuple[int, str, int, str]] = []  # held texts: heap of (turn, id, admission, file name)
        self._admissions = 0

    @property
    def text_budget(self) -> int | None:
        return self._budget

    @property
    def held_bytes(self) -> int:
        """Bytes of search text held now (never more than the budget)."""
        return self._held_bytes

    def set_text_budget(self, budget: int | None) -> None:
        """Hold at most ``budget`` bytes of search text (None: no limit), letting the oldest go now."""
        with self._lock:
            self._budget = budget
            self._evict()

    def entries(self) -> list[Entry]:
        """Every readable page. An unreadable page is logged ``page_unreadable`` on every call, as before,
        and left out."""
        with self._lock:
            out = []
            for name, signature in self._listing().items():
                entry = self._entries.get(name)
                if entry is None or entry.view is None:
                    page = self._read(self.pages_dir / name)
                    if page is None:
                        continue
                    if entry is None:
                        entry = self._entries[name] = Entry(signature, None)
                    entry.view = _view(page)
                out.append(entry)
            return out

    def search_entries(self) -> tuple[list[SearchEntry], Callable[[int], SearchEntry | None]]:
        """What ``contexto.store.search.rank`` reads: every page, with its text where it is held (else
        None), and the ``fetch`` that reads the page at an index from its file. A fetched page's text
        is held if the budget keeps it (it is among the newest); either way its id, turn, and kind are
        kept, and a page that cannot be read is logged ``page_unreadable`` and left out."""
        with self._lock:
            listing = list(self._listing().items())
            out = []
            for name, _ in listing:
                entry = self._entries.get(name)
                if entry is not None and entry.search is not None:
                    out.append(entry.search)
                else:  # not read for search yet: fetch reads it
                    out.append(SearchEntry(name.removesuffix(".json"), -1, None))
        return out, lambda i: self._fetch(*listing[i])

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
                self._drop(path.name)
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

    # Under the lock

    def _listing(self) -> dict[str, Signature]:
        """The page files now, by name; entries for files that are gone or changed are dropped."""
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
        for name in [n for n, e in self._entries.items() if listing.get(n) != e.signature]:
            self._drop(name)
        return listing

    def _drop(self, name: str) -> None:
        entry = self._entries.pop(name, None)
        if entry is not None and entry.held:
            self._held_bytes -= len(entry.search.text)  # its heap item is skipped when it comes up

    def _hold(self, name: str, entry: Entry, found: SearchEntry) -> None:
        """Keep ``found`` as the entry's search fields, with its text if the budget keeps it."""
        if self._budget is not None and len(found.text) > self._budget:
            entry.search, entry.held = found._replace(text=None), 0
            return
        entry.search = found
        self._admissions += 1
        entry.held = self._admissions
        self._held_bytes += len(found.text)
        heapq.heappush(self._oldest, (found.turn, found.id, entry.held, name))
        self._evict()

    def _evict(self) -> None:
        """Let the oldest held texts go, by (turn, id), until the held bytes fit the budget."""
        if self._budget is None:
            return
        while self._held_bytes > self._budget and self._oldest:
            _, _, admission, name = heapq.heappop(self._oldest)
            entry = self._entries.get(name)
            if entry is None or entry.held != admission:
                continue  # dropped, or let go already
            self._held_bytes -= len(entry.search.text)
            entry.search = entry.search._replace(text=None)
            entry.held = 0

    # Reads

    def _fetch(self, name: str, signature: Signature) -> SearchEntry | None:
        with self._lock:
            entry = self._entries.get(name)
            if entry is not None and entry.signature == signature and entry.held:
                return entry.search
        page = self._read(self.pages_dir / name)
        found = None
        if page is not None:
            try:
                found = search_entry(page)
            except (KeyError, TypeError, AttributeError) as e:
                self._log({"event": "page_unreadable", "path": name, "error": str(e)})
        with self._lock:
            if found is None:
                self._drop(name)
                return None
            entry = self._entries.get(name)
            if entry is None or entry.signature != signature:
                self._drop(name)
                entry = self._entries[name] = Entry(signature, None)
            elif entry.held:  # held meanwhile
                return entry.search
            self._hold(name, entry, found)
        return found

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            page = current_stub(json.loads(path.read_text(encoding="utf-8")))
            _view(page)  # the fields every reader needs
            return page
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self._log({"event": "page_unreadable", "path": path.name, "error": str(e)})
            return None


def _view(page: dict[str, Any]) -> dict[str, Any]:
    stub = page["stub"]
    if not isinstance(stub, dict):
        raise TypeError("page stub is not an object")
    return {"id": page["id"], "turn": page["turn"], "stub": stub}
