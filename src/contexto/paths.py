"""Where Contexto keeps its data: ``$CONTEXTO_HOME``, else ``~/.contexto``, one directory per session."""

from __future__ import annotations

import os
from pathlib import Path


def contexto_home() -> Path:
    home = os.environ.get("CONTEXTO_HOME")
    return Path(home).expanduser() if home else Path.home() / ".contexto"


def session_dir(home: Path, session_id: str) -> Path:
    """``home/sessions/<session_id>``; the id must be a single safe path segment."""
    if (not session_id or session_id in (".", "..") or any(c in session_id for c in "/\\\0")):
        raise ValueError(f"invalid session id: {session_id!r}")
    return Path(home) / "sessions" / session_id
