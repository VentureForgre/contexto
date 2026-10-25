"""Read-only views of sessions under ``$CONTEXTO_HOME`` for the CLI: listing, selection, summaries.

Nothing here creates or changes a session. Session selection is ``--session``, else the
``CONTEXTO_SESSION`` environment variable, else the session with the most recent activity.
"""

from __future__ import annotations

import json
import os
import stat
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contexto.paths import session_dir
from contexto.store import SessionNotFoundError, SessionStore
from contexto.store.ledger import LedgerError


@dataclass(frozen=True)
class SessionInfo:
    id: str
    created: str  # ISO 8601 UTC
    pages: int
    records: int | None  # None when the ledger cannot be read
    last_activity: str
    size_bytes: int  # disk space the session directory uses, symlinks not followed


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


ACTIVITY_NAMES = ("log.jsonl", "ledger.jsonl", "pages")  # with the directory itself: what "last activity" reads


def _times(path: Path) -> tuple[float, float]:
    """(created, last activity) for a session directory, from file times."""
    key = path / "page.key"
    created = key.stat().st_mtime if key.exists() else path.stat().st_mtime
    last = path.stat().st_mtime
    for name in ACTIVITY_NAMES:
        p = path / name
        if p.exists():
            last = max(last, p.stat().st_mtime)
    return created, last


def last_activity(path: Path) -> float:
    """When a session directory last changed, from file times: the newest of the directory itself,
    ``log.jsonl``, ``ledger.jsonl``, and ``pages/`` (metadata writes replace ``meta.json``, which
    changes the directory's time)."""
    return _times(path)[1]


def _allocated(st: os.stat_result) -> int:
    return st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size


def disk_usage_fd(dir_fd: int) -> int:
    """Bytes the open directory ``dir_fd`` and everything under it occupy on disk (allocated blocks,
    so it is the space a delete frees). Symlinks are counted as links and never followed; entries
    that vanish while walking are skipped."""
    total = 0
    for _, dirs, files, root_fd in os.fwalk(".", dir_fd=dir_fd):
        total += _allocated(os.fstat(root_fd))  # each directory once, as the root of its own step
        # A symlink to a directory is listed in ``dirs`` but not walked, so it is counted here.
        for name in [*files, *dirs]:
            try:
                st = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                continue
            if name in files or stat.S_ISLNK(st.st_mode):
                total += _allocated(st)
    return total


def disk_usage(path: Path) -> int:
    """``disk_usage_fd`` of the directory at ``path``; 0 when it cannot be opened."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return 0
    try:
        return disk_usage_fd(fd)
    finally:
        os.close(fd)


def session_ids(home: Path) -> list[str]:
    root = Path(home) / "sessions"
    if not root.is_dir():
        return []
    ids = []
    for p in root.iterdir():
        if p.is_symlink():  # never follow a link out of sessions/
            continue
        try:
            if p.is_dir() and session_dir(home, p.name) == p:
                ids.append(p.name)
        except ValueError:
            continue
    return ids


def session_info(home: Path, session_id: str) -> SessionInfo:
    store = SessionStore.open_existing(home, session_id)
    created, last = _times(store.dir)
    pages = sum(1 for p in store.pages_dir.glob("pg_*.json") if p.is_file()) if store.pages_dir.is_dir() else 0
    try:
        records = len(store.ledger.records())
    except LedgerError:
        records = None
    return SessionInfo(session_id, _iso(created), pages, records, _iso(last), disk_usage(store.dir))


def list_sessions(home: Path) -> list[SessionInfo]:
    """Every session, most recent activity first; ties by id, so the order is stable."""
    infos = [session_info(home, i) for i in session_ids(home)]
    return sorted(infos, key=lambda s: (s.last_activity, s.id), reverse=True)


def select_session(home: Path, explicit: str | None) -> tuple[str, str]:
    """(session id, how it was chosen). Raises SessionNotFoundError with a clear message."""
    if explicit:
        how, sid = "--session", explicit
    elif os.environ.get("CONTEXTO_SESSION"):
        how, sid = "CONTEXTO_SESSION", os.environ["CONTEXTO_SESSION"]
    else:
        sessions = list_sessions(home)
        if not sessions:
            raise SessionNotFoundError(f"no sessions under {Path(home) / 'sessions'}")
        return sessions[0].id, "most recent"
    try:
        path = session_dir(home, sid)
    except ValueError as e:
        raise SessionNotFoundError(str(e)) from e
    if not path.is_dir():
        raise SessionNotFoundError(f"no session {sid!r} under {Path(home) / 'sessions'} (from {how})")
    return sid, how


def _log_events(path: Path) -> list[dict[str, Any]]:
    events = []
    try:
        lines = (path / "log.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return events
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _is_over_bound(event: dict[str, Any]) -> bool:
    return "over_bound" in str(event.get("event", "")) or event.get("over_bound") is True


def _is_4xx(event: dict[str, Any]) -> bool:
    status = event.get("status")
    return isinstance(status, int) and 400 <= status < 500


def session_summary(home: Path, session_id: str) -> dict[str, Any]:
    """Summary of one session: counts from the store, the ledger, and the session log."""
    store = SessionStore.open_existing(home, session_id)
    info = session_info(home, session_id)
    out: dict[str, Any] = asdict(info)
    try:
        state = store.state()
        out["open_obligations"] = len(state.open_obligations)
        out["active_records"] = len(state.active)
        out["ledger_problems"] = store.ledger.verify()
    except LedgerError as e:
        out["open_obligations"] = None
        out["active_records"] = None
        out["ledger_problems"] = [str(e)]
    events = _log_events(store.dir)
    out["log_events"] = len(events)
    out["events_by_type"] = dict(sorted(Counter(str(e.get("event", "")) for e in events).items()))
    out["over_bound"] = sum(1 for e in events if _is_over_bound(e))
    out["upstream_4xx"] = sum(1 for e in events if _is_4xx(e))
    epochs = [e for e in events if "epoch" in e]
    out["last_epoch"] = epochs[-1]["epoch"] if epochs else None
    return out
