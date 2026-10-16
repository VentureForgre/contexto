"""Private files and directories for session data (M2 contract PR #25: 0700 directories, 0600 files).

Session data holds raw conversation content, so every directory Contexto creates under
``$CONTEXTO_HOME`` is 0700 and every file 0600, whatever the process umask. Modes are set with
an explicit chmod after creation, because a mode passed to ``open`` or ``mkdir`` is only ever
narrowed by the umask, never widened or fixed.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def private_dir(path: Path) -> Path:
    """Create ``path`` and any missing parents as 0700; force ``path`` itself to 0700."""
    path = Path(path)
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        try:
            os.mkdir(d, DIR_MODE)
        except FileExistsError:
            pass
        os.chmod(d, DIR_MODE)
    os.chmod(path, DIR_MODE)
    return path


def open_private(path: Path, flags: int) -> int:
    """``os.open`` with ``O_CREAT`` semantics that leaves the file at 0600; returns the fd."""
    fd = os.open(path, flags | os.O_CREAT, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_new(path: Path, data: bytes) -> bool:
    """Atomically create ``path`` (0600) holding exactly ``data``; False if ``path`` already exists.

    The data is written and fsynced to a private temporary file first and then hard-linked into
    place, so a reader never sees a partial file and an existing file is never replaced.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = open_private(tmp, os.O_WRONLY | os.O_EXCL)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)
