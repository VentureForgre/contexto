"""Private files and directories for session data (M2 contract PR #25: 0700 directories, 0600 files).

Session data holds raw conversation content, so every directory Contexto creates under
``$CONTEXTO_HOME`` is 0700 and every file 0600, whatever the process umask. Modes are set with
an explicit chmod after creation, because a mode passed to ``open`` or ``mkdir`` is only ever
narrowed by the umask, never widened or fixed.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600
AFFINITY_SALT_FILE = "affinity.salt"
AFFINITY_SALT_BYTES = 32


def private_dir(path: Path, *, narrow: bool = True) -> Path:
    """Create ``path`` and any missing parents as 0700; force an existing ``path`` to 0700 too, unless
    ``narrow`` is false (a directory the user chose, such as ``$CONTEXTO_HOME``, D55). A ``path`` that
    exists and is not a directory raises NotADirectoryError before any mode is changed."""
    path = Path(path)
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    if not missing and not path.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "not a directory", str(path))
    for d in reversed(missing):
        try:
            os.mkdir(d, DIR_MODE)
        except FileExistsError:
            pass
        os.chmod(d, DIR_MODE)
    if narrow:
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


def affinity_salt(home: Path) -> bytes:
    """The install's random salt for upstream affinity keys (D36), ``home/affinity.salt`` (0600),
    created atomically on first use and the same for every later session and resume. It is read
    without following a symlink (OSError) or blocking on a FIFO, and must be a regular file holding
    a hex salt of the right size (ValueError otherwise)."""
    path = Path(home) / AFFINITY_SALT_FILE
    if not path.exists():
        private_dir(Path(home), narrow=False)
        # A losing racer's write_new returns False and reads the winner's complete salt below.
        write_new(path, secrets.token_bytes(AFFINITY_SALT_BYTES).hex().encode("ascii"))
    # O_NOFOLLOW: a symlink raises OSError (ELOOP). O_NONBLOCK: a FIFO opens at once and fails the
    # regular-file check below instead of blocking the launch.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path} is not a regular file")
        data = os.read(fd, 4 * AFFINITY_SALT_BYTES)
    finally:
        os.close(fd)
    try:
        salt = bytes.fromhex(data.decode("ascii").strip())
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not a hex salt") from exc
    if len(salt) != AFFINITY_SALT_BYTES:
        raise ValueError(f"{path} holds a {len(salt)}-byte salt, expected {AFFINITY_SALT_BYTES}")
    return salt
