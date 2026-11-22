"""Lazily built, incrementally maintained per-session SQLite FTS5 search index (zero external deps).

Uses Python stdlib ``sqlite3`` with two FTS5 virtual tables inside ``<session_dir>/search.sqlite``:

- ``pages_tri`` (``trigram`` tokenizer over case-folded page text): exact-recall substring candidate
  filtering for strict queries (words of 3+ chars) in O(matches) instead of O(N) file scans.
- ``pages_fts`` (``unicode61`` tokenizer over code-split tokens): fast code-aware lookup across
  ``camelCase``, ``snake_case``, ``kebab-case``, and space-separated tokens.

The index is purely a derived cache over immutable ``pages/*.json`` (and ``.json.gz``) files. If
SQLite FTS5 is unavailable, or if ``search.sqlite`` is damaged, locked, or unreadable, every method
returns ``None`` and the caller falls back transparently to ``PageCatalog`` scanning.

Read-only callers (``SessionStore.open_existing``, ``EarlierPages``) open an existing
``search.sqlite`` with ``?mode=ro`` and never create or modify any file in that session directory.
"""

from __future__ import annotations

import functools
import os
import sqlite3
import stat
import threading
import urllib.parse
import weakref
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from contexto.private import FILE_MODE
from contexto.store.pages import _search_fields, search_entry
from contexto.store.search import (
    TEXT_SEP,
    Approximate,
    Query,
    SearchEntry,
    code_tokens,
    rank_approximate,
    score_matched,
    search_key,
    term_frequency,
)

FTS_FILE = "search.sqlite"
SCHEMA_VERSION = "1"
Signature = tuple[int, int, int]  # (st_ino, st_size, st_mtime_ns)
_BUSY_TIMEOUT_MS = 2_000


@functools.lru_cache(maxsize=1)
def fts5_available() -> bool:
    """True when this Python's stdlib ``sqlite3`` supports FTS5 with both ``unicode61`` and ``trigram``."""
    conn = None
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE _probe_u USING fts5(body, tokenize='unicode61')")
        conn.execute("CREATE VIRTUAL TABLE _probe_t USING fts5(body, tokenize='trigram')")
        return True
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def _fts_safe_text(text: str) -> str:
    """Valid UTF-8 text for SQLite FTS5 (lone surrogates replaced so SQLite never rejects a page)."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _fts_quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _is_regular_or_missing(path: Path) -> bool:
    """True if ``path`` does not exist or is a regular file (never a symlink, FIFO, or directory)."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _is_regular(path: Path) -> bool:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _file_uri(path: Path, *, read_only: bool) -> str:
    encoded = urllib.parse.quote(str(path.resolve()), safe="/")
    return f"file:{encoded}?mode=ro" if read_only else f"file:{encoded}"


def _close_conn_box(box: list[sqlite3.Connection | None]) -> None:
    conn = box[0]
    box[0] = None
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


class FtsIndex:
    """Per-session SQLite FTS5 index, lazily opened on first search and synced incrementally.

    Read-only instances open ``?mode=ro`` per call and close immediately afterwards so multi-session
    project searches never hold open file descriptors. Writable instances cache their connection with
    a ``weakref.finalize`` guard so file descriptors are always released on GC or explicit ``close()``.
    """

    def __init__(self, session_dir: Path, *, read_only: bool = False) -> None:
        self.session_dir = Path(session_dir)
        self.path = self.session_dir / FTS_FILE
        self.read_only = read_only
        self._lock = threading.Lock()
        self._conn_box: list[sqlite3.Connection | None] = [None]
        self._finalizer = weakref.finalize(self, _close_conn_box, self._conn_box)
        self._disabled = False

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        _close_conn_box(self._conn_box)

    @contextmanager
    def _connection_locked(self) -> Iterator[sqlite3.Connection | None]:
        """Yield a SQLite connection under ``self._lock``; closes transient read-only connections on exit."""
        if self._disabled or not fts5_available():
            yield None
            return
        if self.read_only:
            if not _is_regular(self.path):
                yield None
                return
            conn: sqlite3.Connection | None = None
            try:
                conn = sqlite3.connect(_file_uri(self.path, read_only=True), uri=True,
                                       timeout=_BUSY_TIMEOUT_MS / 1000.0, check_same_thread=False)
                row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                if row is None or row[0] != SCHEMA_VERSION:
                    conn.close()
                    conn = None
            except (sqlite3.Error, OSError):
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                    conn = None
            if conn is None:
                yield None
                return
            try:
                yield conn
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            return
        conn = self._conn_box[0]
        if conn is not None:
            yield conn
            return
        if not _is_regular_or_missing(self.path):
            yield None
            return
        try:
            conn = sqlite3.connect(str(self.path), timeout=_BUSY_TIMEOUT_MS / 1000.0, check_same_thread=False)
            try:
                os.chmod(self.path, FILE_MODE)
            except OSError:
                pass
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._ensure_schema(conn)
            self._conn_box[0] = conn
        except (sqlite3.Error, OSError):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._reset_corrupt_locked()
            yield None
            return
        yield conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is not None and row[0] != SCHEMA_VERSION:
            conn.executescript(
                "DROP TABLE IF EXISTS pages_fts;"
                "DROP TABLE IF EXISTS pages_tri;"
                "DROP TABLE IF EXISTS pages;"
                "DELETE FROM meta;"
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pages ("
            "  rowid INTEGER PRIMARY KEY,"
            "  page_id TEXT UNIQUE NOT NULL,"
            "  file_name TEXT UNIQUE NOT NULL,"
            "  turn INTEGER NOT NULL,"
            "  context INTEGER NOT NULL,"
            "  sig_ino INTEGER NOT NULL,"
            "  sig_size INTEGER NOT NULL,"
            "  sig_mtime INTEGER NOT NULL,"
            "  fields_z BLOB NOT NULL"
            ")"
        )
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(code_text, tokenize='unicode61')")
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages_tri USING fts5(raw_text, tokenize='trigram')")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        conn.commit()

    def _reset_corrupt_locked(self) -> None:
        self._close_locked()
        if not self.read_only and _is_regular(self.path):
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                self._disabled = True

    def _sync_locked(self, conn: sqlite3.Connection, listing: dict[str, Signature],
                     read_page: Callable[[str], dict[str, Any] | None]) -> bool:
        """Synchronize the index with ``listing``; returns True when ready, False to fall back."""
        rows = conn.execute("SELECT rowid, file_name, sig_ino, sig_size, sig_mtime FROM pages").fetchall()
        stored = {file_name: (rowid, (ino, size, mtime)) for rowid, file_name, ino, size, mtime in rows}
        if self.read_only:
            # Read-only caller never writes: use the DB only if every listed file matches its stored signature.
            return len(stored) == len(listing) and all(stored.get(n, (None, None))[1] == sig
                                                       for n, sig in listing.items())
        removed = [rowid for file_name, (rowid, sig) in stored.items() if listing.get(file_name) != sig]
        to_add = [(file_name, sig) for file_name, sig in listing.items()
                  if stored.get(file_name, (None, None))[1] != sig]
        if not removed and not to_add:
            return True
        with conn:
            for rowid in removed:
                conn.execute("DELETE FROM pages_fts WHERE rowid = ?", (rowid,))
                conn.execute("DELETE FROM pages_tri WHERE rowid = ?", (rowid,))
                conn.execute("DELETE FROM pages WHERE rowid = ?", (rowid,))
            for file_name, (ino, size, mtime) in to_add:
                page = read_page(file_name)
                if page is None:
                    continue
                try:
                    entry = search_entry(page)
                    raw_fields = "\n".join(_search_fields(page))
                except (KeyError, TypeError, AttributeError):
                    continue
                if entry.text is None:
                    continue
                fields_z = zlib.compress(entry.text, 1)
                raw_text = _fts_safe_text(entry.text.replace(TEXT_SEP, b"\n").decode("utf-8", "surrogatepass"))
                code_text = _fts_safe_text(code_tokens(raw_fields))
                # If a page_id moved between .json and .json.gz, remove any prior row with the same page_id.
                old = conn.execute("SELECT rowid FROM pages WHERE page_id = ?", (entry.id,)).fetchone()
                if old is not None:
                    conn.execute("DELETE FROM pages_fts WHERE rowid = ?", (old[0],))
                    conn.execute("DELETE FROM pages_tri WHERE rowid = ?", (old[0],))
                    conn.execute("DELETE FROM pages WHERE rowid = ?", (old[0],))
                cur = conn.execute(
                    "INSERT INTO pages(page_id, file_name, turn, context, sig_ino, sig_size, sig_mtime, fields_z) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (entry.id, file_name, entry.turn, int(entry.context), ino, size, mtime, sqlite3.Binary(fields_z)),
                )
                rowid = cur.lastrowid
                conn.execute("INSERT INTO pages_fts(rowid, code_text) VALUES (?, ?)", (rowid, code_text))
                conn.execute("INSERT INTO pages_tri(rowid, raw_text) VALUES (?, ?)", (rowid, raw_text))
        return True

    def search(self, query: str, listing: dict[str, Signature],
               read_page: Callable[[str], dict[str, Any] | None]) -> list[str] | None:
        """Strict search using SQLite FTS5; returns ``None`` to signal fallback to ``PageCatalog``."""
        q = Query.parse(query)
        if q is None:
            return []
        if _has_surrogate(query):
            return None
        with self._lock:
            for attempt in range(2):
                try:
                    with self._connection_locked() as conn:
                        if conn is None:
                            return None
                        if not self._sync_locked(conn, listing, read_page):
                            return None
                        return self._run_strict(conn, q)
                except sqlite3.DatabaseError:
                    self._reset_corrupt_locked()
                    if self.read_only or attempt == 1:
                        return None
                except (sqlite3.Error, OSError):
                    return None
            return None

    def _run_strict(self, conn: sqlite3.Connection, q: Query) -> list[str]:
        n = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        if n == 0:
            return []
        words = [search_key(w) for w in q.words]
        phrase = search_key(q.phrase)
        terms = words + ([phrase] if q.multiword else [])
        tri_words = [(idx, w) for idx, w in enumerate(q.words) if len(w) >= 3]
        if tri_words:
            match_expr = " AND ".join(_fts_quote(w) for _, w in tri_words)
            rows = conn.execute(
                "SELECT p.page_id, p.turn, p.context, p.fields_z "
                "FROM pages p JOIN pages_tri t ON p.rowid = t.rowid "
                "WHERE t.raw_text MATCH ?",
                (match_expr,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT page_id, turn, context, fields_z FROM pages").fetchall()
        if not rows:
            return []
        matched = []
        for page_id, turn, context, fields_z in rows:
            text = zlib.decompress(fields_z)
            if not all(w in text for w in words):
                continue
            on = (phrase in text) if q.multiword else True
            tf = tuple(term_frequency(text, t) if t != phrase or on else 0 for t in terms)
            matched.append((page_id, turn, bool(context), on, tf))
        if not matched:
            return []
        freq: dict[bytes, int] = {}
        if len(words) == 1:
            freq[words[0]] = len(matched)
        else:
            for w_str, w_bytes in zip(q.words, words):
                freq[w_bytes] = self._word_df(conn, w_str, w_bytes)
        if q.multiword:
            freq[phrase] = sum(1 for m in matched if m[3])
        return score_matched(matched, freq, n, terms, phrase, q.multiword)

    def _word_df(self, conn: sqlite3.Connection, word_str: str, word_bytes: bytes) -> int:
        """Exact document frequency of ``word_bytes`` across indexed pages."""
        if len(word_str) >= 3:
            if word_str.isascii() and "\n" not in word_str:
                # For ASCII terms >= 3 chars, verify only if needed or count via trigram.
                # Note: raw_text joins fields with '\n' and word_str has no whitespace, and trigram is
                # case-insensitive while raw_text is already folded, so trigram phrase matches are exact
                # for ASCII terms (no multi-char Unicode folding edge cases).
                return conn.execute(
                    "SELECT COUNT(*) FROM pages_tri WHERE raw_text MATCH ?",
                    (_fts_quote(word_str),),
                ).fetchone()[0]
            rows = conn.execute(
                "SELECT p.fields_z FROM pages p JOIN pages_tri t ON p.rowid = t.rowid "
                "WHERE t.raw_text MATCH ?",
                (_fts_quote(word_str),),
            )
            return sum(1 for (fz,) in rows if word_bytes in zlib.decompress(fz))
        rows = conn.execute("SELECT fields_z FROM pages")
        return sum(1 for (fz,) in rows if word_bytes in zlib.decompress(fz))

    def search_approximate(self, query: str, listing: dict[str, Signature],
                           read_page: Callable[[str], dict[str, Any] | None]) -> Approximate | None:
        """Approximate search backed by the SQLite index (avoids JSON rescans and RAM cache thrashing);
        returns ``None`` to signal fallback to ``PageCatalog``."""
        if _has_surrogate(query):
            return None
        with self._lock:
            for attempt in range(2):
                try:
                    with self._connection_locked() as conn:
                        if conn is None:
                            return None
                        if not self._sync_locked(conn, listing, read_page):
                            return None
                        rows = conn.execute("SELECT rowid, page_id, turn, context FROM pages").fetchall()
                        if not rows:
                            return rank_approximate([], query)
                        entries = [SearchEntry(page_id, turn, None, bool(context))
                                   for _, page_id, turn, context in rows]
                        rowids = [rowid for rowid, *_ in rows]

                        def fetch(i: int, _conn=conn, _rowids=rowids, _entries=entries) -> SearchEntry | None:
                            got = _conn.execute("SELECT fields_z FROM pages WHERE rowid = ?", (_rowids[i],)).fetchone()
                            if got is None:
                                return None
                            return _entries[i]._replace(text=zlib.decompress(got[0]))

                        return rank_approximate(entries, query, fetch)
                except sqlite3.DatabaseError:
                    self._reset_corrupt_locked()
                    if self.read_only or attempt == 1:
                        return None
                except (sqlite3.Error, OSError):
                    return None
            return None

    def code_search(self, query: str, listing: dict[str, Signature],
                    read_page: Callable[[str], dict[str, Any] | None]) -> list[str] | None:
        """Fast code-aware token lookup via ``pages_fts`` (matches camelCase/snake_case/kebab-case)."""
        tokens = [t for t in code_tokens(query).split() if len(t) >= 2]
        if not tokens or _has_surrogate(query):
            return None
        unique = list(dict.fromkeys(tokens))[:8]
        with self._lock:
            try:
                with self._connection_locked() as conn:
                    if conn is None:
                        return None
                    if not self._sync_locked(conn, listing, read_page):
                        return None
                    expr = " AND ".join(_fts_quote(t) for t in unique)
                    rows = conn.execute(
                        "SELECT p.page_id FROM pages p JOIN pages_fts f ON p.rowid = f.rowid "
                        "WHERE f.code_text MATCH ? ORDER BY bm25(pages_fts), p.turn DESC",
                        (expr,),
                    ).fetchall()
                    return [r[0] for r in rows]
            except (sqlite3.Error, OSError):
                return None
