"""Removing whole sessions from ``$CONTEXTO_HOME/sessions/`` for ``contexto sessions prune`` (D37).

A session is in use while any process holds the shared lock on its ``live.lock`` (every writable
``SessionStore`` does, see ``contexto.store.hold_live``). Prune takes the exclusive lock without
waiting before it deletes anything, so it never removes a session a running proxy, launcher, or MCP
server has open. A process from a Contexto older than D37 holds no such lock, so prune also refuses
any session with activity in the last ``ACTIVE_GRACE_S`` seconds.

Only a real directory directly inside the resolved ``sessions/`` directory, which itself resolves
inside the resolved home, is ever deleted. Symlinks are never followed: a session entry that is a
symlink is refused, and links inside a session are removed as links.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from contexto.paths import session_dir
from contexto.private import open_private
from contexto.store import LIVE_LOCK, SessionNotFoundError
from contexto.store.sessions import disk_usage, last_activity, session_ids

ACTIVE_GRACE_S = 600  # a session changed this recently counts as in use, lock or not

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION = re.compile(r"(\d+)([smhdw])")


class SessionLiveError(RuntimeError):
    """The session is in use, or was active too recently to be removed safely."""


class UnsafeSessionPathError(ValueError):
    """The session path is a symlink or resolves outside the Contexto home's ``sessions/``."""


def parse_duration(text: str) -> int:
    """Seconds in a duration such as ``30d``, ``12h``, ``90m``, ``2w``, or ``45s``; ValueError otherwise."""
    m = _DURATION.fullmatch(text.strip())
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"invalid duration {text!r}: use a positive whole number and one of s, m, h, d, w "
                         "(for example 30d or 12h)")
    return int(m.group(1)) * _UNITS[m.group(2)]


def safe_session_path(home: Path, session_id: str) -> Path:
    """The resolved directory of an existing session, checked to be safe to delete.

    ValueError for an invalid id, SessionNotFoundError when there is no such directory, and
    UnsafeSessionPathError when the entry is a symlink or anything resolves outside the home.
    """
    path = session_dir(home, session_id)
    if path.is_symlink():
        raise UnsafeSessionPathError(f"session {session_id!r} is a symlink; prune never follows links")
    if not path.is_dir():
        raise SessionNotFoundError(f"no session {session_id!r} under {Path(home) / 'sessions'}")
    real_home = Path(home).resolve()
    real_root = (Path(home) / "sessions").resolve()
    real = path.resolve()
    if real_root == real_home or not real_root.is_relative_to(real_home) or real.parent != real_root:
        raise UnsafeSessionPathError(f"session {session_id!r} resolves to {real}, outside {real_home / 'sessions'}")
    return real


def _locked_by_others(fd: int) -> bool:
    """Try the exclusive lock without waiting; True when another holder has the session open."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    return False


def check_not_live(path: Path, *, now: float, grace: float = ACTIVE_GRACE_S) -> None:
    """SessionLiveError when the session at ``path`` is in use or was active within ``grace`` seconds.

    Changes nothing on disk: a session without ``live.lock`` has never been opened by a D37 writer,
    so no current process holds it.
    """
    idle = now - last_activity(path)
    if idle < grace:
        raise SessionLiveError(f"active {max(0, int(idle // 60))} min ago (prune skips sessions active in the "
                               f"last {int(grace // 60)} min)")
    try:
        fd = os.open(path / LIVE_LOCK, os.O_RDONLY)
    except FileNotFoundError:
        return
    try:
        if _locked_by_others(fd):
            raise SessionLiveError("in use by a running Contexto proxy or MCP server")
    finally:
        os.close(fd)  # releases the probe's lock


def _delete_contents(real: Path) -> None:
    """Delete ``real`` and everything in it, the lock file last, so a store opening the session during
    the delete still finds the lock file prune holds and waits for it."""
    with os.scandir(real) as it:
        entries = list(it)
    for entry in entries:
        if entry.name == LIVE_LOCK:
            continue
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)  # a symlink is removed as a link
    (real / LIVE_LOCK).unlink(missing_ok=True)
    os.rmdir(real)


def remove_session(home: Path, session_id: str, *, now: float, grace: float = ACTIVE_GRACE_S) -> int:
    """Delete one session's directory and return the bytes it occupied.

    Raises SessionLiveError (nothing deleted) when the session is in use, the errors of
    ``safe_session_path``, or OSError when the delete fails part way.
    """
    real = safe_session_path(home, session_id)
    size = disk_usage(real)
    check_not_live(real, now=now, grace=grace)
    fd = open_private(real / LIVE_LOCK, os.O_RDWR)
    try:
        if _locked_by_others(fd):
            raise SessionLiveError("in use by a running Contexto proxy or MCP server")
        _delete_contents(real)
    finally:
        os.close(fd)
    return size


@dataclass(frozen=True)
class Candidate:
    id: str
    last_activity: float
    size_bytes: int


def select(home: Path, *, older_than: int | None, keep: int | None, session_id: str | None,
           now: float) -> list[Candidate]:
    """Sessions every given condition selects, most recent activity first.

    ``keep`` keeps the ``keep`` sessions with the most recent activity (ranked over all sessions);
    ``older_than`` selects sessions idle at least that many seconds; ``session_id`` selects that one
    session (SessionNotFoundError, ValueError, or UnsafeSessionPathError if it cannot be pruned).
    """
    every = []
    for sid in session_ids(home):
        path = session_dir(home, sid)
        try:
            every.append(Candidate(sid, last_activity(path), disk_usage(path)))
        except FileNotFoundError:  # removed while listing
            continue
    every.sort(key=lambda c: (c.last_activity, c.id), reverse=True)
    chosen = every[keep:] if keep is not None else every
    if session_id is not None:
        safe_session_path(home, session_id)
        chosen = [c for c in chosen if c.id == session_id]
    if older_than is not None:
        chosen = [c for c in chosen if now - c.last_activity >= older_than]
    return chosen
