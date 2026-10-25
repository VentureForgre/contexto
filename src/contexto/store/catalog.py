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
  which only the index reads), and holds search text for at most ``text_budget`` bytes, counted as
  the bytes of the text held (a held text costs about 210 bytes more in object headers and its heap
  item, outside the budget).

What the budget holds, by page age (turn, then id): the newest pages' text as it is, and older pages'
text compressed (zlib level 1, about 3.2 times smaller on the synthetic store; 27 us to decompress a
32 KB page against about 110 us to read, parse, and fold it again). When the held bytes exceed the
budget, the oldest uncompressed text is compressed; when none is left uncompressed, the oldest
compressed text is let go. So a store whose compressed text fits keeps every page, and a larger one
keeps the newest pages compressed. When the store's text does not fit as it is, at most
``RAW_SHARE`` of the budget is held uncompressed (see there). Every search reads every page's text
(a page matches when each query word is a substring of it), so a page whose text is not held is read
again from its file during the search and let go after it, unless the budget can take it. Oldest out
first, rather than least
recently used, because a search reads every page in the same order: under LRU a store larger than the
budget would evict each page just before the next search needs it, and no read would be saved. The
budget is ``CONTEXTO_SEARCH_CACHE_MB`` (default 64), or ``contexto mcp --search-cache-mb``.
"""

from __future__ import annotations

import heapq
import os
import threading
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contexto.canonical import loads_bounded
from contexto.store.pages import current_stub, index_line, search_entry
from contexto.store.search import SearchEntry

Signature = tuple[int, int, int]  # inode, size, modification time (ns)

ENV_VAR = "CONTEXTO_SEARCH_CACHE_MB"
DEFAULT_TEXT_BUDGET_MB = 64
MB = 1024 * 1024
ZLIB_LEVEL = 1  # level 6 compresses a little smaller and several times slower (reviewer B on PR #215)
# When the store's text does not fit the budget as it is, at most this share of the budget is held
# uncompressed. Text held uncompressed and compressed later leaves its memory with the allocator, which
# does not give it back: at 6,000 pages and 64 MB, peak RSS was 185 MB with no limit, 145 MB with half,
# and 124 MB with a quarter, while the uncompressed pages left at the end were the same. The cost: a
# budget raised above the store's compressed size but below its uncompressed size is partly left
# unused, and search is slower than it could be (reviewer-2 on PR #215: 80 of 128 MiB held, 0.31 s
# against 0.25 s, for 140 MB of peak RSS against 232).
RAW_SHARE = 0.25


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
    # for search; ``text`` (folded, UTF-8) only while the budget holds it uncompressed.
    search: SearchEntry | None = None
    held: int = 0  # the admission number of the held text, 0 when no text is held
    packed: bytes | None = None  # the held text compressed, when it is held that way
    size: int = 0  # the bytes the held text counts against the budget
    packed_size: int = 0  # the text's compressed size, once compressing it found it larger than the budget
    lines: dict[bool, str] = field(default_factory=dict)  # the index line, by whether it lists identifiers

    @property
    def id(self) -> str:
        return self.view["id"]

    @property
    def turn(self) -> int:
        return self.view["turn"]


def _signature(stat: os.stat_result) -> Signature:
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


HeapItem = tuple[int, str, int, str]  # turn, id, admission, file name


class PageCatalog:
    def __init__(self, pages_dir: Path, log: Callable[[dict[str, Any]], None],
                 text_budget: int | None = DEFAULT_TEXT_BUDGET_MB * MB) -> None:
        self.pages_dir = pages_dir
        self._log = log
        self._entries: dict[str, Entry] = {}  # by file name
        self._lock = threading.Lock()
        self._budget = text_budget  # None: no limit
        self._held_bytes = 0
        self._raw: list[HeapItem] = []  # texts held as they are, oldest first
        self._packed: list[HeapItem] = []  # texts held compressed, oldest first
        self._raw_count = 0  # live items in _raw
        self._raw_bytes = 0  # bytes held uncompressed
        self._listed_bytes = 0  # file bytes of the pages in the last search's listing
        self._seen_files = self._seen_bytes = 0  # file bytes and text bytes of the pages read for search
        self._stale = 0  # heap items of dropped entries, cleared when they reach half the heaps
        self._admissions = 0

    @property
    def text_budget(self) -> int | None:
        return self._budget

    @property
    def held_bytes(self) -> int:
        """Bytes counted against the budget now: held text, compressed or not (never more than the budget)."""
        return self._held_bytes

    def held(self) -> dict[str, str]:
        """How each page's text is held, by page id: ``raw`` or ``packed`` (pages not held are left out)."""
        with self._lock:
            return {e.search.id: ("packed" if e.packed is not None else "raw")
                    for e in self._entries.values() if e.held}

    def set_text_budget(self, budget: int | None) -> None:
        """Hold at most ``budget`` bytes of search text (None: no limit), compressing or letting go now."""
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

    def search_entries(self) -> tuple[list[SearchEntry], Callable[[int], SearchEntry | None], list[str]]:
        """What ``contexto.store.search.rank`` reads: every page, with its text where it is held as it is
        (else None), the ``fetch`` that gives the page at an index (decompressing held text, else reading
        the file), and the file names listed. A page read from its file is held if the budget takes it;
        either way its id, turn, and kind are kept, and a page that cannot be read is logged
        ``page_unreadable`` and left out."""
        with self._lock:
            listing = list(self._listing().items())
            self._listed_bytes = sum(signature[1] for _, signature in listing)
            out = []
            for name, _ in listing:
                entry = self._entries.get(name)
                if entry is not None and entry.search is not None:
                    out.append(entry.search)
                else:  # not read for search yet: fetch reads it
                    out.append(SearchEntry(name.removesuffix(".json"), -1, None))
        return out, lambda i: self._fetch(*listing[i]), [name for name, _ in listing]

    def all_present(self, names: list[str]) -> bool:
        """Whether every file in ``names`` is still in ``pages/`` (one directory read, no ``stat``)."""
        try:
            present = set(os.listdir(self.pages_dir))
        except FileNotFoundError:
            present = set()
        return all(name in present for name in names)

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
        if entry is None or not entry.held:
            return
        self._held_bytes -= entry.size
        if entry.packed is None:
            self._raw_count -= 1
            self._raw_bytes -= entry.size
        self._stale += 1  # its heap item is skipped when it comes up, or cleared below
        if self._stale > 64 and 2 * self._stale > len(self._raw) + len(self._packed):
            self._raw = [item for item in self._raw if self._live(item, packed=False)]
            self._packed = [item for item in self._packed if self._live(item, packed=True)]
            heapq.heapify(self._raw)
            heapq.heapify(self._packed)
            self._stale = 0

    def _live(self, item: HeapItem, *, packed: bool) -> bool:
        entry = self._entries.get(item[3])
        return entry is not None and entry.held == item[2] and (entry.packed is not None) == packed

    def _oldest(self, heap: list[HeapItem], *, packed: bool) -> HeapItem | None:
        """The oldest live item of ``heap``, with stale items above it removed."""
        while heap and not self._live(heap[0], packed=packed):
            heapq.heappop(heap)
            self._stale = max(0, self._stale - 1)
        return heap[0] if heap else None

    def _hold(self, name: str, entry: Entry, found: SearchEntry, file_bytes: int) -> None:
        """Keep ``found`` as the entry's search fields, and its text if the budget takes it."""
        entry.search, entry.held, entry.packed, entry.size = found._replace(text=None), 0, None, 0
        size = len(found.text)
        self._seen_files += file_bytes
        self._seen_bytes += size
        if self._budget is not None and size > self._budget and not entry.packed_size:
            entry.packed_size = len(zlib.compress(found.text, ZLIB_LEVEL))
        if self._budget is not None and entry.packed_size > self._budget:
            return  # larger than the budget even compressed: holding it would let every other text go first
        if self._budget is not None and self._held_bytes + size > self._budget and not self._raw_count:
            # Every held text is compressed already, so this one would be compressed and then let go first
            # if it is the oldest: skip the compression (a search over a store larger than the budget).
            oldest = self._oldest(self._packed, packed=True)
            if oldest is None or (found.turn, found.id) < oldest[:2]:
                return
        self._admissions += 1
        item = (found.turn, found.id, self._admissions, name)
        heapq.heappush(self._raw, item)  # first: turn and id are validated, but nothing is counted yet
        entry.search, entry.held, entry.size = found, item[2], size
        self._held_bytes += size
        self._raw_count += 1
        self._raw_bytes += size
        self._evict()

    def _raw_limit(self) -> float:
        """The budget, when the store's text looks like it fits as it is (the listed pages' file bytes times
        the text bytes per file byte of the pages read so far), else ``RAW_SHARE`` of it."""
        if not self._seen_files or self._listed_bytes * self._seen_bytes / self._seen_files <= self._budget:
            return self._budget
        return RAW_SHARE * self._budget

    def _evict(self) -> None:
        """Until the held bytes fit the budget, and the uncompressed bytes their limit: compress the oldest
        uncompressed text, or, when every held text is compressed, let the oldest go."""
        if self._budget is None:
            return
        limit = self._raw_limit()
        while self._held_bytes > self._budget or self._raw_bytes > limit:
            item = self._oldest(self._raw, packed=False)
            if item is not None:
                heapq.heappop(self._raw)
                entry = self._entries[item[3]]
                packed = zlib.compress(entry.search.text, ZLIB_LEVEL)
                entry.search = entry.search._replace(text=None)
                entry.packed = packed
                self._raw_bytes -= entry.size
                self._held_bytes += len(packed) - entry.size
                entry.size = len(packed)
                self._raw_count -= 1
                if entry.size > self._budget:  # larger than the budget even compressed: let it go now,
                    self._held_bytes -= entry.size  # not every older text first, and never hold it again
                    entry.held, entry.packed, entry.size, entry.packed_size = 0, None, 0, len(packed)
                    continue
                heapq.heappush(self._packed, item)
                continue
            if self._held_bytes <= self._budget:
                break
            item = self._oldest(self._packed, packed=True)
            if item is None:
                break
            heapq.heappop(self._packed)
            entry = self._entries[item[3]]
            self._held_bytes -= entry.size
            entry.held, entry.packed, entry.size = 0, None, 0

    # Reads

    def _fetch(self, name: str, signature: Signature) -> SearchEntry | None:
        with self._lock:
            entry = self._entries.get(name)
            if entry is not None and entry.signature == signature and entry.held:
                if entry.packed is None:
                    return entry.search
                held, packed = entry.search, entry.packed
            else:
                held = packed = None
        if packed is not None:
            return held._replace(text=zlib.decompress(packed))
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
                return found
            self._hold(name, entry, found, signature[1])
        return found

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            page = current_stub(loads_bounded(path.read_text(encoding="utf-8")))
            _view(page)  # the fields every reader needs
            return page
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self._log({"event": "page_unreadable", "path": path.name, "error": str(e)})
            return None


def _view(page: dict[str, Any]) -> dict[str, Any]:
    """The fields every reader needs. A page whose id is not a string or whose turn is not an integer is
    damaged: it is refused here, so it is logged unreadable and left out, rather than breaking the sort
    of every search and index (reviewer-1 on PR #215)."""
    stub = page["stub"]
    if not isinstance(stub, dict):
        raise TypeError("page stub is not an object")
    if not isinstance(page["id"], str):
        raise TypeError("page id is not a string")
    turn = page["turn"]
    if not isinstance(turn, int) or isinstance(turn, bool):
        raise TypeError("page turn is not an integer")
    return {"id": page["id"], "turn": turn, "stub": stub}


def check_page(page: Any) -> dict[str, Any]:
    """``page`` if it has the shape of a page record (``_view``'s fields and a ``messages`` list),
    else TypeError or KeyError: what ``get_page`` returns must be safe to render and recall."""
    if not isinstance(page, dict):
        raise TypeError("page record is not an object")
    _view(page)
    if not isinstance(page["messages"], list):
        raise TypeError("page messages is not a list")
    return page
