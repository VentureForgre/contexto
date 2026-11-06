"""Read-only views of sessions under ``$CONTEXTO_HOME`` for the CLI: listing, selection, summaries.

Nothing here creates or changes a session. Session selection is ``--session``, else the
``CONTEXTO_SESSION`` environment variable (each an id or a unique prefix of one), else the session with
the most recent activity that has pages or ledger records (``select_session``).
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


AMBIGUOUS_SHOWN = 5  # matching ids an ambiguous-prefix error names


def resolve_session(home: Path, given: str, how: str | None = None) -> str:
    """The session ``given`` names: that exact id, else the one session id that starts with it (D57).
    SessionNotFoundError for an invalid id, no match, or a prefix of several ids."""
    try:
        path = session_dir(home, given)
    except ValueError as e:
        raise SessionNotFoundError(str(e)) from e
    if path.is_dir():
        return given
    matches = sorted(i for i in session_ids(home) if i.startswith(given))
    if len(matches) == 1:
        return matches[0]
    if matches:
        shown = ", ".join(matches[:AMBIGUOUS_SHOWN]) + (", ..." if len(matches) > AMBIGUOUS_SHOWN else "")
        raise SessionNotFoundError(f"session id prefix {given!r} matches {len(matches)} sessions: {shown}; "
                                   "give more characters")
    raise SessionNotFoundError(f"no session {given!r} under {Path(home) / 'sessions'}"
                               + (f" (from {how})" if how else ""))


def is_empty(info: SessionInfo) -> bool:
    """No pages and no ledger records: nothing ``recall`` or ``state`` could show."""
    return info.pages == 0 and info.records == 0


def select_session(home: Path, explicit: str | None) -> tuple[str, str]:
    """(session id, how it was chosen). Raises SessionNotFoundError with a clear message.

    ``--session`` (even an empty one, which is refused as invalid), else a non-empty ``CONTEXTO_SESSION``,
    each an exact id or a unique prefix; else the most recently active session with pages or ledger
    records, so a failed launch or a session that never paged does not hide the last one with content;
    else, when every session is empty, the most recent one."""
    if explicit is not None:
        how, sid = "--session", explicit
    elif os.environ.get("CONTEXTO_SESSION"):
        how, sid = "CONTEXTO_SESSION", os.environ["CONTEXTO_SESSION"]
    else:
        sessions = list_sessions(home)
        if not sessions:
            raise SessionNotFoundError(f"no sessions under {Path(home) / 'sessions'}")
        if not is_empty(sessions[0]):
            return sessions[0].id, "most recent"
        filled = next((s for s in sessions if not is_empty(s)), None)
        if filled is None:
            return sessions[0].id, "most recent"
        return filled.id, "most recent with pages or pins"
    return resolve_session(home, sid, how), how


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


def _is_upstream_4xx(event: dict[str, Any]) -> bool:
    """A 4xx from upstream, counted once: the proxy logs each as its ``request`` entry and again as
    ``upstream_4xx``, so ``request`` entries are not counted (a ``request`` 4xx with no ``upstream_4xx``
    is the proxy's own answer, such as malformed framing). D57."""
    status = event.get("status")
    return event.get("event") != "request" and isinstance(status, int) and 400 <= status < 500


def _is_model_path(event: dict[str, Any]) -> bool:
    from contexto.proxy import TRANSFORMED_PATHS  # lazily: the proxy is not needed to read a session

    path = event.get("path")
    return isinstance(path, str) and path in TRANSFORMED_PATHS


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
    upstream_4xx = [e for e in events if _is_upstream_4xx(e)]
    out["upstream_4xx"] = len(upstream_4xx)
    # Claude Code's keyless HEAD /api/hello probe gets a 401 at every launch; model requests are apart.
    out["upstream_4xx_model"] = sum(1 for e in upstream_4xx if _is_model_path(e))
    epochs = [e for e in events if "epoch" in e]
    out["last_epoch"] = epochs[-1]["epoch"] if epochs else None
    return out
