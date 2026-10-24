"""Removing whole sessions from ``$CONTEXTO_HOME/sessions/`` for ``contexto sessions prune`` (D37).

A session is in use while any process holds the shared lock on its ``live.lock`` (every writable
``SessionStore`` does, see ``contexto.store.hold_live``). Prune takes the exclusive lock without
waiting before it deletes anything, so it never removes a session a running proxy, launcher, or MCP
server has open. A process from a Contexto older than D37 holds no such lock, so prune also refuses
any session with activity in the last ``ACTIVE_GRACE_S`` seconds.

What is guaranteed about paths. The home is opened once (it may be a symlink); ``sessions/`` must be a
real directory in it, and each session a real directory in ``sessions/``. Every later step works
through those open directory descriptors, never by path: the session is opened with ``O_NOFOLLOW``
and must be the same inode (``st_dev``, ``st_ino``) as the entry that was checked and selected,
``live.lock`` is opened with ``O_NOFOLLOW | O_NONBLOCK`` and must be a regular file, and the contents
are deleted with ``unlink``/``rmtree`` relative to the session's descriptor (``shutil.rmtree`` in its
symlink-attack-safe mode; prune refuses to delete on a platform without it). Symlinks inside a
session are removed as links. So even if another process swaps ``sessions/<id>`` for a symlink at
any moment, prune deletes nothing outside the directory it checked; if the entry no longer names that
directory before its final ``rmdir``, prune stops with an error and leaves the entry alone.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from contexto.paths import session_dir
from contexto.private import FILE_MODE
from contexto.store import LIVE_LOCK, SessionNotFoundError
from contexto.store.sessions import ACTIVITY_NAMES, disk_usage_fd

ACTIVE_GRACE_S = 600  # a session changed this recently counts as in use, lock or not

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION = re.compile(r"(\d+)([smhdw])")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_LOCK_FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK  # read-write so flock works on NFS too
_LINK_ERRNOS = {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}  # what O_NOFOLLOW gives for a symlink
IN_USE = "in use by a running Contexto proxy or MCP server"
_LOCK_LINK = f"{LIVE_LOCK} is a symlink; prune never follows links"
_LOCK_NOT_FILE = f"{LIVE_LOCK} is not a regular file; remove the session by hand"

Ident = tuple[int, int]  # (st_dev, st_ino)


class SessionLiveError(RuntimeError):
    """The session is in use, or was active too recently to be removed safely."""


class UnsafeSessionPathError(ValueError):
    """A symlink or non-regular file where prune needs a real directory or lock file, or an entry
    that changed while prune was working on it."""


def parse_duration(text: str) -> int:
    """Seconds in a duration such as ``30d``, ``12h``, ``90m``, ``2w``, or ``45s``; ValueError otherwise."""
    m = _DURATION.fullmatch(text.strip())
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"invalid duration {text!r}: use a positive whole number and one of s, m, h, d, w "
                         "(for example 30d or 12h)")
    return int(m.group(1)) * _UNITS[m.group(2)]


def _ident(st: os.stat_result) -> Ident:
    return st.st_dev, st.st_ino


def _open_checked(parent_fd: int, name: str, st: os.stat_result, what: str) -> int:
    """Open directory ``name`` under ``parent_fd`` without following a link; it must be the inode ``st``."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise UnsafeSessionPathError(f"{what} disappeared while it was being checked") from None
    except OSError as e:
        if e.errno in _LINK_ERRNOS:
            raise UnsafeSessionPathError(f"{what} stopped being a directory while it was being checked") from None
        raise
    if _ident(os.fstat(fd)) != _ident(st):
        os.close(fd)
        raise UnsafeSessionPathError(f"{what} was replaced while it was being checked")
    return fd


@contextmanager
def _sessions_fd(home: Path) -> Iterator[int | None]:
    """A descriptor for ``home/sessions``, or None when there is none. The home may be a symlink;
    ``sessions/`` must be a real directory (UnsafeSessionPathError otherwise)."""
    try:
        home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        yield None
        return
    try:
        try:
            st = os.stat("sessions", dir_fd=home_fd, follow_symlinks=False)
        except FileNotFoundError:
            fd = None
        else:
            if not stat.S_ISDIR(st.st_mode):
                raise UnsafeSessionPathError(f"{Path(home) / 'sessions'} is a symlink or not a directory; "
                                             "prune only deletes inside a real sessions directory")
            fd = _open_checked(home_fd, "sessions", st, f"{Path(home) / 'sessions'}")
    finally:
        os.close(home_fd)
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _open_session(sessions_fd: int | None, home: Path, session_id: str, expect: Ident | None) -> tuple[int, Ident]:
    """(descriptor, identity) of session ``session_id``, a real directory directly in ``sessions/``.

    ValueError for an invalid id, SessionNotFoundError when absent, UnsafeSessionPathError for a symlink
    or for an entry that is no longer the directory ``expect`` names.
    """
    session_dir(home, session_id)  # ValueError for an id that is not one safe path segment
    where = Path(home) / "sessions"
    if sessions_fd is None:
        raise SessionNotFoundError(f"no session {session_id!r} under {where}")
    try:
        st = os.stat(session_id, dir_fd=sessions_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise SessionNotFoundError(f"no session {session_id!r} under {where}") from None
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeSessionPathError(f"session {session_id!r} is a symlink; prune never follows links")
    if not stat.S_ISDIR(st.st_mode):
        raise SessionNotFoundError(f"{where / session_id} is not a session directory")
    if expect is not None and _ident(st) != expect:
        raise UnsafeSessionPathError(f"session {session_id!r} was replaced since it was selected")
    return _open_checked(sessions_fd, session_id, st, f"session {session_id!r}"), _ident(st)


def _last_activity(fd: int) -> float:
    """``contexto.store.sessions.last_activity``, read through the session's descriptor."""
    last = os.fstat(fd).st_mtime
    for name in ACTIVITY_NAMES:
        try:
            last = max(last, os.stat(name, dir_fd=fd, follow_symlinks=False).st_mtime)
        except FileNotFoundError:
            continue
    return last


def _check_idle(fd: int, now: float, grace: float) -> None:
    idle = now - _last_activity(fd)
    if idle < grace:
        raise SessionLiveError(f"active {max(0, int(idle // 60))} min ago (prune skips sessions active in the "
                               f"last {int(grace // 60)} min)")


def _open_lock(fd: int, *, create: bool) -> int | None:
    """``live.lock`` in the session ``fd``, never through a symlink and never blocking on a FIFO; None
    when it is absent and ``create`` is false. UnsafeSessionPathError unless it is a regular file."""
    try:
        st = os.stat(LIVE_LOCK, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        st = None
    if st is not None:  # the type decides the message; the open below checks the file it gets again
        if stat.S_ISLNK(st.st_mode):
            raise UnsafeSessionPathError(_LOCK_LINK)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeSessionPathError(_LOCK_NOT_FILE)
    elif not create:
        return None
    try:
        lock = os.open(LIVE_LOCK, _LOCK_FLAGS | (os.O_CREAT if create else 0), FILE_MODE, dir_fd=fd)
    except FileNotFoundError:
        if create:
            raise
        return None
    except OSError as e:
        if e.errno in _LINK_ERRNOS:
            raise UnsafeSessionPathError(_LOCK_LINK) from None
        if e.errno in (errno.EISDIR, errno.ENXIO, errno.EOPNOTSUPP):  # replaced since the stat above
            raise UnsafeSessionPathError(_LOCK_NOT_FILE) from None
        raise
    if not stat.S_ISREG(os.fstat(lock).st_mode):
        os.close(lock)
        raise UnsafeSessionPathError(_LOCK_NOT_FILE)
    return lock


def _held_by_others(lock: int) -> bool:
    """Try the exclusive lock without waiting; True when another holder has the session open."""
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    return False


def check_not_live(home: Path, session_id: str, *, now: float, grace: float = ACTIVE_GRACE_S,
                   expect: Ident | None = None) -> None:
    """SessionLiveError when the session is in use or was active within ``grace`` seconds, and the
    errors of ``_open_session`` and ``_open_lock``. Writes nothing: a session without ``live.lock``
    has never been opened by a D37 writer, so no current process holds it."""
    with _sessions_fd(home) as sessions_fd:
        fd, _ = _open_session(sessions_fd, home, session_id, expect)
        try:
            _check_idle(fd, now, grace)
            lock = _open_lock(fd, create=False)
            if lock is not None:
                try:
                    if _held_by_others(lock):
                        raise SessionLiveError(IN_USE)
                finally:
                    os.close(lock)  # releases the probe's lock
        finally:
            os.close(fd)


def _delete_contents(fd: int) -> None:
    """Delete everything in the session directory ``fd``, the lock file last, so a store opening the
    session during the delete still finds the lock file prune holds and waits for it."""
    with os.scandir(fd) as it:
        entries = list(it)
    for entry in entries:
        if entry.name == LIVE_LOCK:
            continue
        if entry.is_dir(follow_symlinks=False):
            try:
                shutil.rmtree(entry.name, dir_fd=fd)  # refuses a symlink, even one swapped in now
            except OSError as e:
                if e.errno is None:  # rmtree's own "Cannot call rmtree on a symbolic link"
                    raise UnsafeSessionPathError(f"{entry.name} became a symlink while it was being deleted; "
                                                 "stopped without following it") from e
                raise
        else:
            os.unlink(entry.name, dir_fd=fd)  # a symlink is removed as a link
    try:
        os.unlink(LIVE_LOCK, dir_fd=fd)
    except FileNotFoundError:
        pass


def _check_entry(sessions_fd: int, session_id: str, fd: int) -> None:
    """``sessions/<id>`` still names the directory ``fd`` is open on."""
    try:
        st = os.stat(session_id, dir_fd=sessions_fd, follow_symlinks=False)
    except FileNotFoundError:
        st = None
    if st is None or _ident(st) != _ident(os.fstat(fd)):
        raise UnsafeSessionPathError(f"session {session_id!r} was moved or replaced while it was being "
                                     "deleted; stopped without removing the entry")


def remove_session(home: Path, session_id: str, *, now: float, grace: float = ACTIVE_GRACE_S,
                   expect: Ident | None = None) -> int:
    """Delete one session's directory and return the bytes it occupied.

    Raises SessionLiveError, with nothing deleted, when the session is in use; SessionLiveError after
    the delete if a store opened the session again during it (its new files are kept); the errors of
    ``_open_session`` and ``_open_lock``; or OSError when the delete fails part way.
    """
    if not shutil.rmtree.avoids_symlink_attacks:
        raise UnsafeSessionPathError("this platform's shutil.rmtree can follow symlinks; prune does not delete")
    with _sessions_fd(home) as sessions_fd:
        fd, _ = _open_session(sessions_fd, home, session_id, expect)
        try:
            _check_idle(fd, now, grace)
            lock = _open_lock(fd, create=True)
            try:
                if _held_by_others(lock):
                    raise SessionLiveError(IN_USE)
                _check_entry(sessions_fd, session_id, fd)
                size = disk_usage_fd(fd)
                _delete_contents(fd)
                _check_entry(sessions_fd, session_id, fd)
                try:
                    os.rmdir(session_id, dir_fd=sessions_fd)
                except OSError as e:
                    if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
                        raise SessionLiveError("opened again while it was being deleted: its old contents "
                                               "were removed and the new files kept") from None
                    raise
            finally:
                os.close(lock)
        finally:
            os.close(fd)
    return size


@dataclass(frozen=True)
class Candidate:
    id: str
    last_activity: float
    size_bytes: int
    ident: Ident  # the directory that was selected; later steps refuse any other


def select(home: Path, *, older_than: int | None, keep: int | None, session_id: str | None,
           now: float) -> list[Candidate]:
    """Sessions every given condition selects, most recent activity first.

    ``keep`` keeps the ``keep`` sessions with the most recent activity (ranked over all sessions);
    ``older_than`` selects sessions idle at least that many seconds; ``session_id`` selects that one
    session (SessionNotFoundError, ValueError, or UnsafeSessionPathError if it cannot be pruned).
    Symlinks in ``sessions/`` are not sessions and are never selected.
    """
    every = []
    with _sessions_fd(home) as sessions_fd:
        if session_id is not None:
            os.close(_open_session(sessions_fd, home, session_id, None)[0])
        names = []
        if sessions_fd is not None:
            with os.scandir(sessions_fd) as it:
                names = [e.name for e in it if e.is_dir(follow_symlinks=False)]
        for name in names:
            try:
                fd, ident = _open_session(sessions_fd, home, name, None)
            except (ValueError, SessionNotFoundError):  # not a valid id, or changed while listing
                continue
            try:
                every.append(Candidate(name, _last_activity(fd), disk_usage_fd(fd), ident))
            finally:
                os.close(fd)
    every.sort(key=lambda c: (c.last_activity, c.id), reverse=True)
    chosen = every[keep:] if keep is not None else every
    if session_id is not None:
        chosen = [c for c in chosen if c.id == session_id]
    if older_than is not None:
        chosen = [c for c in chosen if now - c.last_activity >= older_than]
    return chosen
