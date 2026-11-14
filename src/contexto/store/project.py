"""Project memory: which sessions are one project, and what a session carries from the earlier ones (D69).

A project is a directory: the top of the repository a session was started in (``project_root``), else
the working directory itself. Nothing is shared between two roots, which are compared as path strings
in the file system's own spelling where it can give one (``canonical``: macOS's ``F_GETPATH``).
A session records its root in ``meta.json`` as ``project`` (``SessionStore.set_workdir``); a session
of an earlier release has only ``workdir``, and its root is computed from that when it is read. A
session with neither is in no project.

What the main conversation made: the engine records in a session's ``meta.json`` (``main_records``, the
newest ``MAIN_RECORDS_KEPT``) the id of every record whose ``contexto_pin`` or ``contexto_close`` call
it saw in the main conversation's own history, paired with the server's success reply
(``contexto.mainrecords``, which states the evidence and its limits: only a main conversation the
engine trusts is read, not one that took main by the turn-start rule from a known main, such as a
subagent without its header or a Codex child; and a pin whose reply never reaches a later request of
the main conversation is not recorded). Pins are written with the
author ``"model"`` as every release wrote them; the author says nothing about who pinned.

What a session S sees:

- Its **earlier sessions**: the sessions under ``$CONTEXTO_HOME/sessions`` with the same root whose
  ``project_memory`` is not ``false``, created before S (the modification time of ``page.key``, as
  ``sessions list`` shows it; ties by id). Only they add records to what S carries, so of two sessions
  running at once the later carries from the earlier and never the reverse. There is no index file: a
  pruned session is gone at the next listing.
- The **project state**: ``derive()`` over the records of every other session of the root that still
  exists (earlier or later than S, project memory on or off, taken out by ``forget`` or not) and S's
  own, in ``created_at`` order (ties by session creation, then by line; a session's own line order is
  always kept). Removals are project-wide: a closure, or a record's ``supersedes``, made in any of them
  closes or replaces the record in every session of the project, S's own records included, whenever it
  is made; but one that names a record of another session counts outside its own session only when
  the main conversation made it (its id is in its session's ``main_records``, below). Inside its own
  session a removal applies as it always did. A record that supersedes one of another session's
  replaces it whatever the two ``created_at`` say, since one session's clock may be behind another's.
  Pruning the session that holds a closure or a replacement deletes it, so the record is open or
  active again for the sessions that read the project after that: this is the one way a closed or
  replaced record comes back.
- Its **carried records**: the open obligations and active decisions of that state that come from an
  earlier session, were made by its main conversation (their ids are in that session's
  ``main_records``), were created no later than S, and are not in their own session's ``forgotten``
  list. So what is carried is fixed when S starts and only shrinks after: a closure or a replacement in
  any session, ``forget`` or a prune of the record's own session takes a record away from S's texts
  when it happens, and from what S's requests hold at S's next turn start, or at once when S is resumed
  (rule 19; ``forget --session`` of S, or a resume with ``--no-project-memory``, takes all of them away
  the same way). Verified facts are never carried (their file sources are relative to another working
  directory), nor is anything an earlier release pinned (it has no ``main_records``). A carried decision
  with another active decision on its topic, carried or S's own, is a conflict, as two of S's own are.

Another session is read without changing it: no lock is taken and nothing is created or logged in
it. ``meta.json`` and ``ledger.jsonl`` are read as regular files only (never through a symlink, never
waiting on a FIFO), and a ledger line a live writer has half written is dropped like a crash-truncated
tail. A ledger that is unreadable, does not verify, holds a line nested too deep to parse, or raises
anything else while it is read is skipped whole and counted; a session whose ``meta.json`` cannot be
read is left out and counted; neither stops the session that reads. A ledger over ``LEDGER_LIMIT`` (64
MB) or a ``meta.json`` over ``META_LIMIT`` (1 MB) is skipped or left out the same way, from its size
alone, without being read. Reads are
cached per process by each file's (mtime_ns, size, inode). No subprocess is ever run: ``git`` is not
asked, since a repository's configuration could run code (D52).

The API, for the engine, the launcher, the MCP server and the commands:

- ``store.project() -> Project | None``: the view of a ``SessionStore``; None when the session has no
  project or its project memory is off (``MemoryStore`` has none). A store opened with
  ``SessionStore.open_existing`` gives the same view and writes nothing. Each call is a new view.
- ``store.carried_text(budget_tokens) -> str``: the carried section alone, ``""`` when nothing is
  carried (what the engine sends before the first rotation); ``store.pinned_text`` and
  ``store.state_text`` include the section (``contexto.store.pinned``). ``store.carried()`` is the
  records themselves.
- ``Project(home, root)``: the view of a session started now, for ``contexto project show``; every
  session of the root with memory on is earlier.
- ``Project.root``, ``.id`` (``project_id``), ``.session_id``, ``.sessions`` (every session of the root,
  newest first, memory off included), ``.earlier`` (``Member``: ``id``, ``created``, ``dir``,
  ``pages_dir``; newest first), ``.carried()`` (``Carried``: ``record``, ``session``, ``label``,
  ``date``, ``conflict``; open obligations first, then decisions, newest first in each), ``.state()``
  (the project state), ``.session_state(own, root)`` (S's own state with the project's removals and
  conflicts, and what it carries), ``.label(id)`` and ``.earlier_session(id or label)``, and the
  content-free counts ``.read`` (earlier sessions whose ledger was read), ``.skipped`` (``Skipped``:
  ``session``, ``error``, an error class name; any other session of the project) and
  ``.unreadable`` (session directories left out because ``meta.json`` could not be read). One view
  reads each ledger once, so its answers do not change; ask the store for a new one.
- ``project_sessions(home, root) -> Listing``: the sessions of a root without a store.
- ``read_meta(session_dir)`` and ``read_records(session_dir)``: the read-only readers.
- ``forget(home, session_id, records=..., leave=...)``: for ``contexto project forget``, the one place
  another session's ``meta.json`` is written, and only by that command.
- ``main_record_ids(value)``, ``merged_main_records(kept, new)``: how ``main_records`` is read and kept
  (``SessionStore.add_main_records``, which the engine calls).
- ``EarlierPages``, ``read_page(pages_dir, page_id)``: the pages of earlier sessions, for a search with
  ``scope: "project"`` and a recall with ``session`` (``SessionStore.search_earlier``,
  ``SessionStore.earlier_session`` and ``SessionStore.earlier_page``). Pages are read as the ledgers
  are: without a lock, and with nothing created or logged in the session read. A search reads each
  earlier session's last retained-window snapshot (``retained.json``) the same way, for what was in view
  at its last request (``InViewLine``; a session created earlier may still be running): those lines are
  on no page, so they cannot be recalled.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from contexto.canonical import canonical_bytes
from contexto.paths import session_dir
from contexto.private import NotRegularFileError, open_private, read_regular, read_regular_text, write_atomic
from contexto.store.catalog import PageCatalog, load_page
from contexto.store.ledger import LEDGER_FILE, LedgerCorruptError, _intact, _parse, _problems
from contexto.store.model import _RECORD_ID, Record
from contexto.store.render import retained_matches
from contexto.store.search import Query, rank
from contexto.store.state import Conflict, State, derive

__all__ = ["MAIN_RECORDS_KEPT", "Carried", "EarlierPages", "EarlierSearch", "EarlierSession", "InViewLine", "Listing",
           "Member", "NotEarlierSessionError", "Project", "SessionHits", "Skipped", "forget", "forget_cache",
           "main_record_ids", "merged_main_records", "project_id", "project_root", "project_sessions", "read_meta",
           "read_page", "read_records", "recorded_root", "session_date", "session_project", "shown_label"]

MAIN_RECORDS_KEPT = 4096  # record ids in a session's main_records; the oldest are dropped past it
LEDGER_LIMIT = 64 << 20  # bytes of another session's ledger.jsonl read at most: a larger one is skipped unread
META_LIMIT = 1 << 20  # bytes of another session's meta.json read at most: a larger one is left out unread
META_FILE = "meta.json"
KEY_FILE = "page.key"
LABEL_CHARS = 8  # of a session id, in a carried record's line
CACHE_HOMES = 8  # data directories whose sessions are cached at once (one, outside tests)
REPORTED_KEPT = 4096

_EPOCH = datetime(1970, 1, 1)


def main_record_ids(value: Any) -> frozenset[str]:
    """The record ids a ``main_records`` value of ``meta.json`` holds; empty for anything else."""
    return frozenset(x for x in value if isinstance(x, str)) if isinstance(value, list) else frozenset()


def merged_main_records(kept: Any, new: Iterable[str]) -> list[str] | None:
    """``main_records`` with the record ids ``new`` added once each, oldest dropped past
    ``MAIN_RECORDS_KEPT``; None when that changes nothing."""
    before = [x for x in kept if isinstance(x, str)] if isinstance(kept, list) else []
    merged = list(dict.fromkeys([*before, *(x for x in new if isinstance(x, str) and _RECORD_ID.fullmatch(x))]))
    merged = merged[-MAIN_RECORDS_KEPT:]
    return None if merged == kept else merged


def project_root(workdir: Path, home: Path | None = None) -> Path:
    """The project a session started in ``workdir`` belongs to: the nearest directory, from the resolved
    ``workdir`` up, that holds a ``.git`` entry, else the resolved ``workdir`` itself.

    So the subdirectories of a repository are one project; a linked worktree or a submodule (whose
    ``.git`` is a file) is its own, since it is another checkout on another task; a directory outside
    any repository is its own by its exact real path; and a symlinked path and its target are the same.
    A ``.git`` at the filesystem root or at the user's home directory (``home``, default
    ``Path.home()``) does not count: dotfiles kept in a repository would make every directory one
    project. A directory is named by the file system's own spelling where it can say (``canonical``), so
    two spellings of one directory, in another case or Unicode form, are one project. Raises what
    ``Path.resolve`` raises for a path that cannot be resolved."""
    start = canonical(Path(workdir).resolve())
    if home is None:
        try:
            home = Path.home()
        except RuntimeError:  # no home directory can be found
            home = None
    if home is not None:
        try:
            home = canonical(Path(home).resolve())
        except (OSError, RuntimeError, ValueError):
            home = None
    for directory in (start, *start.parents):
        if directory.parent == directory or directory == home:
            continue
        if os.path.lexists(directory / ".git"):
            return directory
    return start


def canonical(path: Path) -> Path:
    """``path`` (resolved) as the file system spells it: on macOS, where a case-insensitive or
    normalisation-insensitive volume takes ``~/Proj`` for ``~/proj`` and an NFD name for its NFC form,
    the name ``F_GETPATH`` gives for the open directory; elsewhere, or when it cannot be opened (it does
    not exist, it is not a directory, it cannot be read), ``path`` itself."""
    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is None:
        return path
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NONBLOCK)
    except (OSError, ValueError):
        return path
    try:
        found = fcntl.fcntl(fd, getpath, bytes(1024)).split(b"\0", 1)[0]
    except OSError:
        return path
    finally:
        os.close(fd)
    return Path(os.fsdecode(found)) if found.startswith(b"/") else path


def project_id(root: Path | str) -> str:
    """A short name for a root, for display: the first 12 hex characters of the SHA-256 of its path."""
    return hashlib.sha256(os.fsencode(str(root))).hexdigest()[:12]


def recorded_root(meta: dict[str, Any]) -> Path | None:
    """The project root session metadata names: ``project``, else the root of ``workdir`` (a store an
    earlier release wrote), else None, the session is in no project."""
    project = meta.get("project")
    if isinstance(project, str) and project:
        return Path(project)
    workdir = meta.get("workdir")
    if isinstance(workdir, str) and workdir:
        try:
            return project_root(Path(workdir))
        except (OSError, RuntimeError, ValueError):  # a path that cannot be resolved names no project
            return None
    return None


# Reading another session: regular files only, no lock, nothing created, cached by file identity.

Identity = tuple[int, int, int]  # (st_mtime_ns, st_size, st_ino)


@dataclass(frozen=True)
class _Damaged:
    """A file that was read and is not what it should be; kept in the cache so it is not parsed again."""

    error: str  # an error class name, never the file's text
    transient: bool = False  # a read error that may not happen next time (see ``_transient``); never cached


@dataclass(frozen=True)
class _Meta:
    root: str | None  # None: in no project
    memory: bool
    forgotten: frozenset[str]
    main_records: frozenset[str]


_LOCK = threading.Lock()
# sessions directory -> session id -> file name -> (identity, parsed value)
_CACHE: dict[str, dict[str, dict[str, tuple[Identity, Any]]]] = {}
_REPORTED: set[tuple[str, str | None, str]] = set()  # (home, reading session, skipped session) already logged


def forget_cache() -> None:
    """Drop everything this process has cached about other sessions. Results never depend on the cache."""
    with _LOCK:
        _CACHE.clear()
        _REPORTED.clear()


def _slot(directory: Path) -> dict[str, tuple[Identity, Any]]:
    with _LOCK:
        if str(directory.parent) not in _CACHE and len(_CACHE) >= CACHE_HOMES:
            _CACHE.clear()
        return _CACHE.setdefault(str(directory.parent), {}).setdefault(directory.name, {})


def _keep_only(sessions_dir: Path, names: set[str]) -> None:
    """Forget the cached files of sessions that are no longer listed (pruned)."""
    with _LOCK:
        held = _CACHE.get(str(sessions_dir))
        if held is not None and not held.keys() <= names:
            _CACHE[str(sessions_dir)] = {name: files for name, files in held.items() if name in names}


def _identity(path: Path) -> Identity | None:
    """The file's identity, None when it is absent; NotRegularFileError for a symlink, a FIFO, a
    directory or a device in its place, which is then never opened."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):  # the session was deleted, or was never a directory
        return None
    if not stat.S_ISREG(st.st_mode):
        raise NotRegularFileError(f"{path.name} is not a regular file")
    return st.st_mtime_ns, st.st_size, st.st_ino


class FileTooLargeError(OSError):
    """Another session's file is larger than Contexto reads (``LEDGER_LIMIT``, ``META_LIMIT``)."""


def _cached(directory: Path, name: str, parse: Callable[[bytes], Any], limit: int) -> Any:
    """``parse`` of the bytes of ``directory/name``, read again only when the file's identity changed;
    None when the file is absent, at the stat or by the time it is read. OSError when it cannot be read,
    ``FileTooLargeError`` when the stat says it is larger than ``limit``, without reading it."""
    path = directory / name
    identity = _identity(path)
    if identity is None:
        return None
    if identity[1] > limit:
        raise FileTooLargeError(f"{name} is larger than {limit} bytes")
    slot = _slot(directory)
    held = slot.get(name)
    if held is not None and held[0] == identity:
        return held[1]
    try:
        data = read_regular(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    # A file that grew between the stat and the read is kept under the older identity, so the next
    # look reads it again; it is never kept under an identity newer than its bytes.
    value = parse(data)
    slot[name] = (identity, value)
    return value


def _parse_meta(data: bytes) -> _Meta | _Damaged:
    try:
        meta = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError, MemoryError) as e:  # not UTF-8, not JSON, nested too deep or too large
        return _Damaged(type(e).__name__)
    if not isinstance(meta, dict):
        return _Damaged("TypeError")
    root = recorded_root(meta)
    forgotten = meta.get("forgotten")
    return _Meta(None if root is None else str(root), meta.get("project_memory") is not False,
                 frozenset(x for x in forgotten if isinstance(x, str)) if isinstance(forgotten, list) else frozenset(),
                 main_record_ids(meta.get("main_records")))


def _meta_info(directory: Path) -> _Meta | _Damaged | None:
    """What ``directory/meta.json`` says about the session's project; None when there is no such file."""
    try:
        return _cached(directory, META_FILE, _parse_meta, META_LIMIT)
    except OSError as e:
        return _Damaged(type(e).__name__, _transient(e))


def _when(created_at: str) -> datetime:
    return datetime.fromisoformat(created_at[:-1])  # validated when the record was read: naive, UTC


def _parse_ledger(data: bytes) -> tuple[tuple[Record, datetime], ...] | _Damaged:
    """The records before a truncated tail, each with the latest ``created_at`` up to its line (so a
    session's lines keep their order when sessions are merged by time); ``_Damaged`` for a ledger that
    does not verify above the tail: a line that is not a record, a changed record, a broken chain."""
    try:
        lines = _parse(data)
    except (RecursionError, MemoryError) as e:  # a line nested too deep, or too large, to parse: damage
        return _Damaged(type(e).__name__)
    tail = lines[-1].number if lines and lines[-1].truncated else None
    if any(number != tail for number, _ in _problems(lines)):
        return _Damaged(LedgerCorruptError.__name__)
    out, latest = [], datetime.min
    for line in _intact(lines):
        latest = max(latest, _when(line.record.created_at))
        out.append((line.record, latest))
    return tuple(out)


def _ledger(directory: Path) -> tuple[tuple[Record, datetime], ...]:
    found = _cached(directory, LEDGER_FILE, _parse_ledger, LEDGER_LIMIT)
    if isinstance(found, _Damaged):
        raise LedgerCorruptError(f"{LEDGER_FILE} does not verify")
    return found or ()


def read_meta(directory: Path) -> dict[str, Any] | None:
    """The metadata of the session in ``directory``, read without a lock and without creating
    anything; None when ``meta.json`` is absent, unreadable, not a regular file or not a JSON object."""
    try:
        meta = json.loads(read_regular(Path(directory) / META_FILE).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return meta if isinstance(meta, dict) else None


def read_records(directory: Path) -> tuple[Record, ...]:
    """The intact records of the session in ``directory``, in ledger order, read without a lock and
    without creating anything: ``()`` when there is no ledger (or no such session), and a line a live
    writer has not finished is left out. OSError when the ledger cannot be read, is not a regular file,
    or is larger than ``LEDGER_LIMIT`` (``FileTooLargeError``, not read); ``LedgerCorruptError`` when
    it does not verify above its tail."""
    return tuple(record for record, _ in _ledger(Path(directory)))


def _created_ns(directory: Path) -> int | None:
    """When the session was created, as ``sessions._times`` reads it: the modification time of
    ``page.key``, else of the directory; None when the directory is gone."""
    for path in (directory / KEY_FILE, directory):
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        if path is directory or stat.S_ISREG(st.st_mode):
            return st.st_mtime_ns
    return None


@dataclass(frozen=True)
class Member:
    """A session of a project."""

    id: str
    created: float  # seconds since the epoch, as `contexto sessions list` shows it
    dir: Path  # the session directory
    pages_dir: Path
    memory: bool  # False: project memory is off for it; it is listed and never read
    forgotten: frozenset[str]  # record ids `contexto project forget --record` took out of what is carried
    created_ns: int
    main_records: frozenset[str] = frozenset()  # record ids the engine saw the main conversation make

    @property
    def order(self) -> tuple[int, str]:
        """Sessions are ordered by creation time, then id."""
        return self.created_ns, self.id


@dataclass(frozen=True)
class Listing:
    root: Path
    sessions: tuple[Member, ...]  # every session of the root, newest first, memory off included
    unreadable: int  # session directories left out because meta.json could not be read as an object
    error: OSError | None = None  # why sessions/ could not be listed, when it exists (else None)
    passing: int = 0  # of ``unreadable``: those whose meta.json read failed in a way that may pass (D69)


def project_sessions(home: Path, root: Path) -> Listing:
    """The sessions under ``home`` whose project root is ``root`` (as ``project_root`` gives it),
    newest first. Read-only. Only real directories under ``sessions/`` are sessions: an entry that is
    a symlink is never followed. A home that does not exist or cannot be listed has no sessions; for
    one that exists but cannot be listed the listing keeps the error (``Listing.error``)."""
    sessions_dir, wanted = Path(home) / "sessions", str(root)
    failure: OSError | None = None
    try:
        with os.scandir(sessions_dir) as listing:
            entries = list(listing)
    except (FileNotFoundError, NotADirectoryError):
        entries = []
    except OSError as error:
        entries, failure = [], error
    found, names, unreadable, passing = [], set(), 0, 0
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            directory = session_dir(home, entry.name)
        except (OSError, ValueError):
            continue
        names.add(entry.name)
        info = _meta_info(directory)
        if isinstance(info, _Damaged):
            unreadable += 1
            passing += info.transient
            continue
        if info is None or info.root != wanted:
            continue
        created_ns = _created_ns(directory)
        if created_ns is None:  # deleted since it was listed
            continue
        found.append(Member(entry.name, created_ns / 1e9, directory, directory / "pages", info.memory,
                            info.forgotten, created_ns, info.main_records))
    _keep_only(sessions_dir, names)
    found.sort(key=lambda m: m.order, reverse=True)
    return Listing(Path(root), tuple(found), unreadable, failure, passing)


@dataclass(frozen=True)
class Carried:
    """A record a session carries from an earlier session of its project."""

    record: Record
    session: str  # the id of the session that pinned it
    label: str  # that id as it is shown: its first 8 characters, more when two earlier sessions share them
    date: str  # the UTC date the record was created, YYYY-MM-DD
    conflict: bool = False  # a decision with another active one on its topic, carried or the session's own


@dataclass(frozen=True)
class Skipped:
    """An earlier session whose ledger could not be read, so none of its records count."""

    session: str
    error: str  # the error class name, never text from the file
    # A read that may succeed next time (permission, I/O, too many open files), as opposed to damage, a file
    # over the size limit, or a session deleted since the listing. The engine's strict refresh treats one as
    # a failure to read the project, so a passing error never removes what a session carries (rule 19).
    transient: bool = False


def _transient(error: BaseException) -> bool:
    return isinstance(error, OSError) and not isinstance(
        error, (NotRegularFileError, FileTooLargeError, FileNotFoundError, NotADirectoryError, IsADirectoryError))


class Project:
    """A session's view of its project (see the module docstring), or with no ``session_id`` the view
    of a session started now. The sessions are listed when the view is made and each earlier ledger is
    read once, when first asked for, so one view always answers the same."""

    def __init__(self, home: Path, root: Path, session_id: str | None = None, *,
                 own: Callable[[], Sequence[Record]] | None = None,
                 log: Callable[[dict[str, Any]], None] | None = None) -> None:
        """``own`` gives the session's own records; ``log``, for a store that may write, receives one
        content-free ``project_session_skipped`` event per skipped session per process."""
        self.home, self.root, self.session_id = Path(home), Path(root), session_id
        self.id = project_id(root)
        listing = project_sessions(home, root)
        self.sessions = listing.sessions
        self.unreadable = listing.unreadable
        self.listing_error = listing.error  # the sessions directory exists but could not be listed
        self.passing_unreadable = listing.passing  # directories whose meta.json may read next time
        created_ns = None
        if session_id is not None:
            try:
                created_ns = _created_ns(session_dir(home, session_id))
            except ValueError:
                created_ns = None
        self.created = None if created_ns is None else created_ns / 1e9
        # Additions are frozen at the session's start: a record created after this is not carried.
        self._frozen_at = None if created_ns is None else _EPOCH + timedelta(microseconds=created_ns // 1000)
        before = None if session_id is None or created_ns is None else (created_ns, session_id)
        self.earlier = tuple(m for m in listing.sessions
                             if m.memory and m.id != session_id and (before is None or m.order < before))
        # Removals are project-wide: every other session of the project is read, later ones and ones with
        # memory off included, but only an earlier session's records can be carried.
        self._others = tuple(m for m in listing.sessions if m.id != session_id)
        self._own = own if own is not None else tuple
        self._log = log
        self._ledgers: list[tuple[Member, tuple[tuple[Record, datetime], ...]]] | None = None
        self._skipped: tuple[Skipped, ...] = ()
        self._labels: dict[str, str] = {}

    def _load(self) -> list[tuple[Member, tuple[tuple[Record, datetime], ...]]]:
        """The ledgers of the project's other sessions, oldest session first; a session whose ledger is
        unreadable or damaged is skipped whole, and one deleted since the listing reads as empty."""
        if self._ledgers is None:
            ledgers, skipped = [], []
            for member in reversed(self._others):
                try:
                    ledgers.append((member, _ledger(member.dir)))
                except Exception as e:  # whatever one session's damage raises, it is that session's alone
                    skipped.append(Skipped(member.id, type(e).__name__, _transient(e)))
            self._ledgers, self._skipped = ledgers, tuple(skipped)
            self._report()
        return self._ledgers

    def _report(self) -> None:
        if self._log is None:
            return
        for skipped in self._skipped:
            key = (str(self.home), self.session_id, skipped.session)
            with _LOCK:
                if key in _REPORTED:
                    continue
                if len(_REPORTED) >= REPORTED_KEPT:
                    _REPORTED.clear()
                _REPORTED.add(key)
            try:
                self._log({"event": "project_session_skipped", "error": skipped.error})
            except Exception:  # a log write must never fail a render or a pin
                pass

    @property
    def read(self) -> int:
        """How many earlier sessions' ledgers were read (one with no ledger counts: it has no records)."""
        earlier = {m.id for m in self.earlier}
        return sum(1 for member, _ in self._load() if member.id in earlier)

    @property
    def skipped(self) -> tuple[Skipped, ...]:
        """The other sessions of the project, earlier or not, whose ledger could not be read, oldest first."""
        self._load()
        return self._skipped

    def _merged(self, own: Sequence[Record]) -> tuple[list[Record], dict[str, Member | None]]:
        """Every record of the other sessions and ``own`` in the project's order, and the session each
        came from (None: this one). A record id found twice (a copied session directory) counts once,
        for the session that comes first."""
        keyed: list[tuple[datetime, int, int, Record, Member | None]] = []
        ledgers = self._load()
        for index, (member, entries) in enumerate(ledgers):
            keyed += [(when, index, line, record, member) for line, (record, when) in enumerate(entries)]
        latest = datetime.min
        for line, record in enumerate(own):
            latest = max(latest, _when(record.created_at))
            keyed.append((latest, len(ledgers), line, record, None))
        keyed.sort(key=lambda item: item[:3])
        records: list[Record] = []
        origin: dict[str, Member | None] = {}
        for _, _, _, record, member in keyed:
            if record.id not in origin:
                origin[record.id] = member
                records.append(record)
        return records, origin

    def _whole(self, own: Sequence[Record]) -> tuple[State, dict[str, Member | None]]:
        """The project state over ``own`` and every other session's records, and each record's session.
        A closure or a supersedes that names a record of another session counts only when it was made
        in the main conversation of its own session (its id is in that session's ``main_records``; this
        session's own count as they always did), and then whatever the two ``created_at`` say: one
        session's clock may be behind another's. Inside one session its line order rules."""
        records, origin = self._merged(own)

        def session(record_id: str) -> str | None:
            member = origin[record_id]
            return None if member is None else member.id

        def confirmed(record: Record) -> bool:
            member = origin[record.id]
            return member is None or record.id in member.main_records

        across = {(r.id, target) for r in records for target in r.supersedes
                  if target in origin and session(target) != session(r.id)}
        across |= {(r.id, r.target) for r in records if r.kind == "closure" and r.target in origin
                   and session(r.target) != session(r.id)}
        by_id = {r.id: r for r in records}
        ignored = {pair for pair in across if not confirmed(by_id[pair[0]])}
        return derive(records, unordered=across - ignored, ignored=ignored), origin

    def _own_records(self, own: Sequence[Record] | None) -> tuple[Record, ...]:
        return tuple(self._own() if own is None else own)

    def state(self, own: Sequence[Record] | None = None) -> State:
        """The project state as this session sees it; ``own`` is the session's own records when the
        caller already holds them (a precondition under the ledger lock). File staleness is not checked:
        a file source is relative to the working directory of the session that recorded it."""
        return self._whole(self._own_records(own))[0]

    def _view(self, own: Sequence[Record]) -> tuple[State, tuple[Carried, ...], tuple[Conflict, ...]]:
        """(the project state, the carried records, the conflicts among the session's own and carried
        decisions) over the session's records ``own``."""
        whole, origin = self._whole(own)
        own_ids = {r.id for r in own}
        earlier = {m.id for m in self.earlier}

        def eligible(record: Record) -> bool:
            member = origin[record.id]
            return (member is not None and member.id in earlier and record.id not in own_ids
                    and record.id in member.main_records
                    and (self._frozen_at is None or _when(record.created_at) <= self._frozen_at)
                    and record.id not in member.forgotten)

        newest = [r for r in reversed(whole.active) if eligible(r)]
        open_ids = {r.id for r in whole.open_obligations}
        chosen = [r for r in newest if r.id in open_ids] + [r for r in newest if r.kind == "decision"]
        shown = own_ids | {r.id for r in chosen}
        by_topic: dict[str, list[str]] = {}
        for r in whole.active:
            if r.kind == "decision" and r.topic is not None and r.id in shown:
                by_topic.setdefault(r.topic, []).append(r.id)
        conflicts = tuple(Conflict(t, tuple(ids)) for t, ids in by_topic.items() if len(ids) > 1)
        conflicted = {rid for c in conflicts for rid in c.record_ids}
        carried = tuple(Carried(r, origin[r.id].id, self.label(origin[r.id].id),
                                _when(r.created_at).date().isoformat(), r.id in conflicted) for r in chosen)
        return whole, carried, conflicts

    def carried(self, own: Sequence[Record] | None = None) -> tuple[Carried, ...]:
        """The records this session carries: open obligations, then active decisions, newest first in each."""
        return self._view(self._own_records(own))[1]

    def session_state(self, own: Sequence[Record] | None = None,
                      root: Path | None = None) -> tuple[State, tuple[Carried, ...]]:
        """The session's own state with the project's removals applied (a record another session closed
        or replaced is closed or replaced here too), and what the session carries. The state's conflicts
        are the topics with two or more active decisions among the session's own and carried ones, so a
        conflict with a carried decision is one; staleness is checked against ``root`` for its own records."""
        mine = self._own_records(own)
        state = derive(mine, root)
        whole, carried, conflicts = self._view(mine)
        own_ids = {r.id for r in mine}
        open_ids = {r.id for r in whole.open_obligations}
        return replace(state, active=tuple(r for r in state.active if r.id not in whole.superseded_by),
                       superseded_by=MappingProxyType({k: v for k, v in whole.superseded_by.items()
                                                       if k in own_ids}),
                       open_obligations=tuple(r for r in state.open_obligations if r.id in open_ids),
                       conflicts=conflicts), carried

    def label(self, session_id: str) -> str:
        """How an earlier session is named to the model and the user: the first ``LABEL_CHARS``
        characters of its id, or as many more as tell it from every other earlier session; the whole id
        when it is that short or starts another's."""
        label = self._labels.get(session_id)
        if label is None:
            others = [m.id for m in self.earlier if m.id != session_id]
            label = next((session_id[:n] for n in range(LABEL_CHARS, len(session_id))
                          if not any(other.startswith(session_id[:n]) for other in others)), session_id)
            self._labels[session_id] = label
        return label

    def earlier_session(self, given: str) -> Member:
        """The earlier session ``given`` names: that id, else the one earlier session whose id starts
        with it (a label does). LookupError, naming no path, for anything else: no such session, several,
        this session itself, a later session, a session of another project."""
        if isinstance(given, str) and given:
            matches = [m for m in self.earlier if m.id == given] or [m for m in self.earlier if m.id.startswith(given)]
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise LookupError(f"{given!r} is the start of {len(matches)} earlier sessions of this project; "
                                  "give more characters")
        raise LookupError(f"{given!r} is not an earlier session of this project")


def session_project(home: Path, session_id: str, meta: dict[str, Any], own: Callable[[], Sequence[Record]],
                    log: Callable[[dict[str, Any]], None] | None = None) -> Project | None:
    """The view of the session whose metadata is ``meta``; None when project memory is off for it
    (``project_memory`` is false) or it is in no project (``recorded_root``)."""
    if meta.get("project_memory") is False:
        return None
    root = recorded_root(meta)
    return None if root is None else Project(home, root, session_id, own=own, log=log)


def forget(home: Path, session_id: str, *, records: Iterable[str] = (), leave: bool = False) -> None:
    """Take records, or a whole session, out of project memory, for ``contexto project forget``.

    ``records`` are added to the session's ``forgotten`` list in ``meta.json`` (they are its own
    records: a carried record names its session); ``leave`` sets its ``project_memory`` to false, so
    it gives nothing to later sessions (its closures and replacements still count). No file is deleted
    and the ledger is not changed: the text stays on disk until the session is pruned.

    This is the one place another session's ``meta.json`` is written, by the user's command and never
    by a running session. The write holds the session's ``live.lock`` shared, as every writer does, so
    a prune cannot remove the session under it, then ``meta.lock``, and replaces the file atomically.
    The session directory gets back its modification time, so the session's last activity is unchanged,
    unless something else changed the directory after this call's own write.
    It creates no directory: a session that prune removed, a session entry that is a symlink, and a
    session whose ``meta.json`` is gone once the locks are held (a prune is emptying it; the lock files
    this call created there are removed again) are ``SessionNotFoundError``. ValueError for an id that
    is not a record id, for nothing to forget, and for a session whose ``meta.json`` is not a JSON object
    (the file is never replaced by a guess); OSError when the file or a lock cannot be opened or is not
    a regular file."""
    from contexto.store import LIVE_LOCK, SessionNotFoundError  # the package imports this module

    ids = set(records)
    bad = sorted(repr(x) for x in ids if not isinstance(x, str) or not _RECORD_ID.fullmatch(x))
    if bad:
        raise ValueError(f"not a record id: {', '.join(bad[:5])}")
    if not ids and not leave:
        raise ValueError("nothing to forget: give record ids, or leave=True for the whole session")
    try:
        directory = session_dir(home, session_id)
    except ValueError as e:
        raise SessionNotFoundError(str(e)) from e
    gone = SessionNotFoundError(f"no session {session_id!r} under {Path(home) / 'sessions'}")
    created: list[tuple[Path, int]] = []  # the lock files this call created, each with its descriptor
    try:
        before = os.stat(directory, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise SessionNotFoundError(f"session {session_id!r} under {Path(home) / 'sessions'} is not a session "
                                       "directory")
        live = _open_lock(directory / LIVE_LOCK, created)  # creates the file at most, never the directory
    except (FileNotFoundError, NotADirectoryError):
        raise gone from None
    written: int | None = None
    try:
        fcntl.flock(live, fcntl.LOCK_SH)  # waits out a prune that is deleting the session
        try:
            held, current = os.fstat(live), os.stat(directory / LIVE_LOCK, follow_symlinks=False)
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                raise gone
            lock = _open_lock(directory / "meta.lock", created)
        except (FileNotFoundError, NotADirectoryError):  # prune deleted it while this waited
            raise gone from None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                meta = json.loads(read_regular(directory / META_FILE).decode("utf-8"))
            except (FileNotFoundError, NotADirectoryError):
                # Every session of a project has one, so a prune is emptying the directory: the session
                # is gone, and the lock files this call made there are taken back.
                _remove_created(created)
                raise SessionNotFoundError(f"session {session_id!r} no longer exists: it is being pruned") from None
            except (ValueError, RecursionError):
                meta = None
            if not isinstance(meta, dict):
                raise ValueError(f"the {META_FILE} of session {session_id!r} cannot be read; nothing was changed")
            updates: dict[str, Any] = {}
            if ids:
                kept = meta.get("forgotten")
                kept = {x for x in kept if isinstance(x, str)} if isinstance(kept, list) else set()
                if not ids <= kept or meta.get("forgotten") != sorted(kept):
                    updates["forgotten"] = sorted(kept | ids)
            if leave and meta.get("project_memory") is not False:
                updates["project_memory"] = False
            if updates:
                write_atomic(directory / META_FILE, canonical_bytes({**meta, **updates}))
            written = _dir_time(directory)
        finally:
            os.close(lock)  # releases the flock
        try:
            # Forgetting is not activity in the session: its directory gets back its time, so `sessions
            # list` shows the same last activity and a prune right after is not refused as too recent.
            # Only while the time is still the one this call's own changes left: a write the running
            # session made since then keeps its time.
            if written is not None and _dir_time(directory) == written:
                os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns), follow_symlinks=False)
        except OSError:
            pass
    finally:
        os.close(live)


def _dir_time(directory: Path) -> int | None:
    """The modification time of ``directory`` in nanoseconds; None when it cannot be read."""
    try:
        return os.stat(directory, follow_symlinks=False).st_mtime_ns
    except OSError:
        return None


def _open_lock(path: Path, created: list[tuple[Path, int]]) -> int:
    """``open_private(path, O_RDWR)``, noting in ``created`` a file this call made."""
    try:
        fd = open_private(path, os.O_RDWR | os.O_EXCL)
    except FileExistsError:
        return open_private(path, os.O_RDWR)
    created.append((path, fd))
    return fd


def _remove_created(created: list[tuple[Path, int]]) -> None:
    """Unlink the lock files ``forget`` made, each only while the name is still that file."""
    for path, fd in created:
        try:
            held, current = os.fstat(fd), os.stat(path, follow_symlinks=False)
            if (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino):
                os.unlink(path)
        except OSError:
            pass


# The pages of earlier sessions, for search and recall (D69, design section 7).

EARLIER_SESSIONS = 50  # the newest earlier sessions one project-scope search reads
# Page files one project-scope search lists across those sessions, at most: a session that would take
# the count past this, and every older one, is not searched (the newest readable session always is).
EARLIER_PAGES = 20_000
# Seconds after which one project-scope search starts on no further session: the older ones are left
# alone and counted (the newest readable session is always searched, and one begun is finished). Reading
# 15,000 pages of 8 KB for the first time took about 16 s on a heavily loaded machine, and a client
# gives a tool call a limited time (not measured here; the value leaves room under a one-minute limit).
# A later search finds the text of the sessions already read held, as far as the search budget goes,
# and gets further.
EARLIER_SECONDS = 30.0
# Matches of one earlier session a search returns, its best: lines in view at its last request, then pages.
EARLIER_HITS = 3
_clock = time.monotonic  # what the deadline reads
_PAGE_ID = re.compile(r"pg_[0-9a-f]{16}")  # as ``contexto.store.check_page_id``
# What a page file that is not a page record raises when it is loaded (``contexto.store.catalog.load_page``).
PAGE_DAMAGE = (ValueError, KeyError, TypeError, AttributeError)
# An earlier session's last retained-window snapshot (contract PR #125: its engine rewrites it at every main
# request, ``{stamp, epoch, k, entries}``): the turns in view at its last request (when it ended, or now for
# one still running), which no page holds.
RETAINED_FILE = "retained.json"
RETAINED_LIMIT = 16 << 20  # bytes of an earlier session's retained.json read at most: a larger one is skipped unread


class NotEarlierSessionError(LookupError):
    """The value names no earlier session of the project. ``matches`` is how many earlier sessions' ids
    start with it (several: it is ambiguous). The message never holds the value or a path."""

    def __init__(self, matches: int = 0) -> None:
        super().__init__("not an earlier session of this project" if matches < 2 else
                         f"the start of {matches} earlier sessions of this project")
        self.matches = matches


@dataclass(frozen=True)
class EarlierSession:
    """An earlier session a recall may read a page of (``SessionStore.earlier_session``)."""

    id: str
    label: str  # ``Project.label`` of it, as a reply shows it (``shown_label``)
    date: str  # the UTC date it was created, YYYY-MM-DD
    pages_dir: Path


@dataclass(frozen=True)
class InViewLine:
    """A matching line of what was in view at an earlier session's last request (its last ``retained.json``:
    when it ended, or, for a session created earlier that still runs, its current window). It is
    on no page, so ``contexto_recall`` cannot read on from it: the line is all there is of it."""

    turn: int  # the snapshot entry's turn: turn starts counted from 1, as the engine's snapshot counts them
    message_index: int  # the entry's place in that session's history
    start: int  # the part of the entry's text the line shows: text[start:end]
    end: int
    line: str
    role: str  # the entry's role: user, assistant or tool_result (``contexto.store.RETAINED_ROLES``)

    @property
    def shown_role(self) -> str:
        """The role as a reply names it (``tool result``): an earlier session's tool output must never
        read like its user's words (review B on D69, finding 1)."""
        return self.role.replace("_", " ")


@dataclass(frozen=True)
class SessionHits:
    """What a project-scope search found in one earlier session."""

    session: str  # its id
    label: str  # ``Project.label`` of it, as a reply shows it (``shown_label``)
    date: str  # the UTC date it was created, YYYY-MM-DD
    found: int  # its pages that hold the query and can be read
    pages: tuple[tuple[str, dict[str, Any]], ...]  # the best of them, best first
    in_view: tuple[InViewLine, ...] = ()  # matching lines in view at its last request, newest first; before the pages
    in_view_found: int = 0  # every matching line in view at its last request; with ``found``, all its matches


@dataclass(frozen=True)
class EarlierSearch:
    """A project-scope search over the earlier sessions: the sessions with a match, newest first, and
    content-free counts."""

    hits: tuple[SessionHits, ...] = ()
    searched: int = 0  # sessions whose pages were ranked
    skipped: int = 0  # sessions whose pages directory is missing, a symlink, or cannot be listed
    not_searched: int = 0  # older sessions left alone: beyond EARLIER_SESSIONS, EARLIER_PAGES or EARLIER_SECONDS
    pages: int = 0  # page files listed in the sessions searched
    in_view_skipped: int = 0  # sessions searched whose retained.json could not be read: their pages only


def shown_label(label: str) -> str:
    """A session's label as a reply shows it: as JSON writes a string, without the quotes. A session id
    is any directory name, so one with a line break or a quote must not forge a line of a reply; and
    written inside a JSON string, as a tool argument is, what is shown is the label again."""
    return json.dumps(label, ensure_ascii=False)[1:-1]


def session_date(created: float) -> str:
    """The UTC date of a session's creation time, as a search result's label shows it."""
    return (_EPOCH + timedelta(seconds=created)).date().isoformat()


def _no_log(event: dict[str, Any]) -> None:
    """Where a catalog of another session's pages logs: nowhere. Its ``page_unreadable`` events name
    files of a session this process must not write to, and they are not this session's either."""


def _parse_retained(data: bytes) -> tuple[dict[str, Any], ...]:
    """The entries of a retained-window snapshot; ValueError for anything that is not one. Its stamp is
    not compared with anything: it matters only to the session that wrote it, while that session runs."""
    from contexto.store import read_retained  # the package imports this module

    snapshot = read_retained(json.loads(data.decode("utf-8")))
    if snapshot is None:
        raise ValueError(f"{RETAINED_FILE} is not a retained-window snapshot")
    return tuple(snapshot[3])


def _window_matches(entries: Sequence[dict[str, Any]], query: str) -> list[tuple[int, int, int, int, str, str]]:
    """``render.retained_matches`` of an earlier session's snapshot (no question is being asked in it:
    ``asked=False``), each match with its entry's role after it, in the same order."""
    q = Query.parse(query)
    if q is None:
        return []
    found = [(*match, entry["role"]) for entry in entries for match in retained_matches([entry], q, asked=False)]
    found.sort(key=lambda m: (m[0], m[1], m[2]), reverse=True)  # stable, as retained_matches sorts
    return found


def _real_directory(path: Path) -> None:
    """Raise OSError unless ``path`` is a directory itself, not a symlink to one (FileNotFoundError when
    it is gone, NotADirectoryError for anything else in its place)."""
    if not stat.S_ISDIR(os.stat(path, follow_symlinks=False).st_mode):
        raise NotADirectoryError(f"{path.name} is not a directory")


def read_page(pages_dir: Path, page_id: str) -> dict[str, Any] | None:
    """The page ``page_id`` of the session whose pages are in ``pages_dir``, read without a lock and
    without creating or logging anything; None when there is no such page (a prune, or an id that is
    not a page id, from which no path is built). OSError when the session directory or ``pages_dir`` is
    a symlink, or the file cannot be read or is not a regular file; one of ``PAGE_DAMAGE`` when the file
    is not a page record."""
    if not isinstance(page_id, str) or not _PAGE_ID.fullmatch(page_id):
        return None
    try:
        _real_directory(pages_dir.parent)
        _real_directory(pages_dir)
        text = read_regular_text(pages_dir / f"{page_id}.json")
    except FileNotFoundError:
        return None
    return load_page(text, page_id)


class EarlierPages:
    """One process's reader of the pages of a project's earlier sessions (the MCP server's, or a command's).

    Nothing is ever written to those sessions: no lock is taken, nothing is logged in them, and no file
    or directory is created; a session directory or a ``pages`` directory that is a symlink is not read.
    Each session searched gets a ``PageCatalog`` of its own, which lists ``pages/`` at every search and
    reads only the files it has not seen, so a page that a prune removes is gone at the next listing.

    The search text held for earlier sessions counts against the same budget as the current session's
    (``own``, the current session's catalog: ``CONTEXTO_SEARCH_CACHE_MB``). The current session's text
    has the first claim, then the earlier sessions' in the order they are searched, newest first: a
    catalog may grow only into what the others leave, and ``trim`` gives back what the current session
    has since taken, oldest session first. Results never depend on it: a page whose text is not held
    is read again at the next search."""

    def __init__(self, own: PageCatalog) -> None:
        self._own = own
        self._catalogs: dict[str, PageCatalog] = {}  # by session id, newest session first
        # session id -> (identity, entries, bytes counted) of its retained.json, for the sessions in _catalogs
        self._snapshots: dict[str, tuple[Identity, tuple[dict[str, Any], ...], int]] = {}

    @property
    def held_bytes(self) -> int:
        """Bytes of earlier sessions' search text held now: their pages' and their last snapshots'."""
        return (sum(catalog.held_bytes for catalog in self._catalogs.values())
                + sum(size for _, _, size in self._snapshots.values()))

    def _room(self, session_id: str) -> int | None:
        """The budget ``session_id``'s catalog may hold: what the current session, the snapshots held and
        the other earlier sessions' catalogs leave of the one budget (None: no limit)."""
        total = self._own.text_budget
        if total is None:
            return None
        others = sum(catalog.held_bytes for sid, catalog in self._catalogs.items() if sid != session_id)
        snapshots = sum(size for _, _, size in self._snapshots.values())
        return max(0, total - self._own.held_bytes - others - snapshots)

    def _drop(self, session_id: str) -> None:
        self._catalogs.pop(session_id, None)
        self._snapshots.pop(session_id, None)

    def trim(self) -> None:
        """Let go of earlier sessions' text until it fits what the current session leaves of the budget,
        the oldest session's first (a session's snapshot before its pages)."""
        total = self._own.text_budget
        if total is None:
            return
        left = total - self._own.held_bytes
        for sid, catalog in self._catalogs.items():
            held = self._snapshots.get(sid)
            if held is not None:
                if held[2] > left:
                    del self._snapshots[sid]
                else:
                    left -= held[2]
            if catalog.held_bytes > max(0, left):
                catalog.set_text_budget(max(0, left))
            left -= catalog.held_bytes

    def _snapshot(self, member: Member) -> tuple[dict[str, Any], ...] | None:
        """The entries of ``member``'s last retained-window snapshot, () when it has none, None when it
        cannot be used: not a regular file (a symlink, a FIFO: never opened), larger than
        ``RETAINED_LIMIT`` (never read), unreadable, or not a snapshot. Read as the other files of an
        earlier session are: no lock, nothing created or logged; held, while the budget leaves room for
        its bytes, until the file changes."""
        path = member.dir / RETAINED_FILE
        try:
            identity = _identity(path)
            if identity is None:
                self._snapshots.pop(member.id, None)
                return ()
            if identity[1] > RETAINED_LIMIT:
                raise FileTooLargeError(f"{RETAINED_FILE} is larger than {RETAINED_LIMIT} bytes")
            held = self._snapshots.get(member.id)
            if held is not None and held[0] == identity:
                return held[1]
            self._snapshots.pop(member.id, None)
            data = read_regular(path)
            if len(data) > RETAINED_LIMIT:  # grew since the stat
                raise FileTooLargeError(f"{RETAINED_FILE} is larger than {RETAINED_LIMIT} bytes")
            entries = _parse_retained(data)
        except (FileNotFoundError, NotADirectoryError):  # gone since the stat: a prune
            return ()
        except Exception:  # whatever one session's damage raises, it is that session's alone
            self._snapshots.pop(member.id, None)
            return None
        room = self._room(member.id)
        if room is None or len(data) <= room - self._catalogs[member.id].held_bytes:
            self._snapshots[member.id] = (identity, entries, len(data))
        return entries

    def _ranked(self, catalog: PageCatalog, query: str, allowed: int | None) -> tuple[list[str] | None, int]:
        """The session's matching page ids, best first, and how many page files it lists; None for a
        session with more than ``allowed`` (which is then not read). As ``SessionStore.search``, a page
        removed while the search runs is left out by ranking again on a new listing."""
        found: list[str] = []
        listed = 0
        for _ in range(3):
            entries, fetch, names = catalog.search_entries()
            listed = len(entries)
            if allowed is not None and listed > allowed:
                return None, listed
            found = rank(entries, query, fetch)
            if catalog.all_present(names):
                break
        return found, listed

    def search(self, project: Project, query: str) -> EarlierSearch:
        """Search the newest ``EARLIER_SESSIONS`` earlier sessions of ``project``, newest first, and
        return the best ``EARLIER_HITS`` matches of each that has one: first the newest matching lines of
        what was in view at its last request (its last ``retained.json``, matched as the current session's
        window is, ``render.retained_matches``; at most ``EARLIER_HITS - 1`` when a page matches too, so
        its best page is always shown), then its best readable pages.

        Read at most: the page files of those sessions, each once for ranking (not at all while its text
        is held), up to ``EARLIER_PAGES`` files in all and for ``EARLIER_SECONDS`` (then the older
        sessions are left alone and counted), each searched session's ``retained.json`` (not while it is
        held), and for each session with a match its best pages once more to show them. A session whose
        pages cannot be listed is skipped and counted; a snapshot that cannot be used is left out and
        counted (``in_view_skipped``), its session's pages searched as ever; a page that is damaged or
        was removed meanwhile is left out; none of it raises."""
        started = _clock()
        members = project.earlier[:EARLIER_SESSIONS]
        not_searched = len(project.earlier) - len(members)
        order = {member.id: n for n, member in enumerate(members)}
        self._catalogs = {sid: catalog for sid, catalog in self._catalogs.items() if sid in order}
        self._snapshots = {sid: held for sid, held in self._snapshots.items() if sid in order}
        self.trim()
        hits: list[SessionHits] = []
        searched = skipped = listed = unusable = 0
        for n, member in enumerate(members):
            if searched and _clock() - started > EARLIER_SECONDS:
                not_searched += len(members) - n  # their held text stays: the next search may reach them
                break
            try:
                _real_directory(member.dir)
                _real_directory(member.pages_dir)
                catalog = self._catalogs.get(member.id)
                if catalog is None:
                    catalog = self._catalogs[member.id] = PageCatalog(member.pages_dir, _no_log, 0)
                catalog.set_text_budget(self._room(member.id))
                found, count = self._ranked(catalog, query, EARLIER_PAGES - listed if searched else None)
            except OSError:  # gone (a prune), a symlink, or a directory that cannot be listed
                self._drop(member.id)
                skipped += 1
                continue
            if found is None:  # the page limit: this session and every older one are left alone
                for older in members[n:]:
                    self._drop(older.id)
                not_searched += len(members) - n
                break
            searched += 1
            listed += count
            pages: list[tuple[str, dict[str, Any]]] = []
            gone = 0
            for page_id in found:
                if len(pages) == EARLIER_HITS:
                    break
                try:
                    page = read_page(member.pages_dir, page_id)
                except (OSError, *PAGE_DAMAGE):
                    page = None
                if page is None:  # removed or damaged since it was ranked
                    gone += 1
                else:
                    pages.append((page_id, page))
            entries = self._snapshot(member)
            if entries is None:
                unusable += 1
            matched = _window_matches(entries or (), query)
            lines = [InViewLine(*m) for m in matched[:EARLIER_HITS - (1 if pages else 0)]]
            pages = pages[:EARLIER_HITS - len(lines)]
            if pages or lines:
                label = shown_label(project.label(member.id))
                hits.append(SessionHits(member.id, label, session_date(member.created),
                                        len(found) - gone, tuple(pages), tuple(lines), len(matched)))
        self._catalogs = dict(sorted(self._catalogs.items(), key=lambda item: order[item[0]]))
        self._snapshots = {sid: self._snapshots[sid] for sid in self._catalogs if sid in self._snapshots}
        return EarlierSearch(tuple(hits), searched, skipped, not_searched, listed, unusable)
