"""Private files and directories for session data (M2 contract PR #25: 0700 directories, 0600 files).

Session data holds raw conversation content, so every directory Contexto creates under
``$CONTEXTO_HOME`` is 0700 and every file 0600, whatever the process umask. Modes are set with
an explicit chmod after creation, because a mode passed to ``open`` or ``mkdir`` is only ever
narrowed by the umask, never widened or fixed.

Files in a session are opened without following a symlink, and a FIFO, device or directory in a
file's place is refused without blocking (``open_regular``, D58). Writes are whole: a short write
continues, and a temporary file is removed when its write fails (``write_new``, ``remove_stale_temps``).
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
import time
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


class NotRegularFileError(OSError):
    """A FIFO, device, directory or socket stands where a session file should be."""


def _open_nofollow(path: Path, flags: int, mode: int = 0o777) -> int:
    """``os.open`` with ``O_NOFOLLOW``; a symlink at ``path`` is refused as ``NotRegularFileError``, so every
    reader treats it as it treats a FIFO or a directory there (D58)."""
    try:
        return os.open(path, flags | os.O_NOFOLLOW, mode)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK) and os.path.islink(path):  # EMLINK on FreeBSD
            raise NotRegularFileError(f"{path} is a symlink") from e
        raise


def _check_regular(fd: int, path: Path) -> None:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotRegularFileError(f"{path} is not a regular file")


def open_private(path: Path, flags: int) -> int:
    """``os.open`` with ``O_CREAT`` semantics that leaves the file at 0600; returns the fd.

    A symlink at ``path`` is never followed (``NotRegularFileError``), and anything but a regular file there
    is refused (``NotRegularFileError``) without blocking: the open is ``O_NONBLOCK``, so a FIFO does
    not wait for a reader. The descriptor is then made blocking again."""
    fd = _open_nofollow(path, flags | os.O_CREAT | os.O_NONBLOCK, FILE_MODE)
    _check_regular(fd, path)
    try:
        os.set_blocking(fd, True)
        os.fchmod(fd, FILE_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_regular(path: Path, *, follow: bool = False) -> int:
    """A read-only descriptor for the regular file at ``path``. Never blocks on a FIFO and refuses
    anything but a regular file (``NotRegularFileError``), a symlink included unless ``follow``, for a
    file that is not Contexto's own (a pinned file source)."""
    flags = os.O_RDONLY | os.O_NONBLOCK
    fd = os.open(path, flags) if follow else _open_nofollow(path, flags)
    _check_regular(fd, path)
    return fd


def read_regular(path: Path) -> bytes:
    """The bytes of the regular file at ``path``, read as ``open_regular`` opens it."""
    fd = open_regular(path)
    try:
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_regular_text(path: Path) -> str:
    """``read_regular`` decoded as UTF-8 (UnicodeDecodeError, a ValueError, when it is not)."""
    return read_regular(path).decode("utf-8")


def write_all(fd: int, data: bytes) -> None:
    """Write all of ``data``: ``os.write`` may write less than asked, on a full disk for one."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # left for remove_stale_temps


def temp_name(path: Path) -> Path:
    """A private temporary name beside ``path``: ``.<name>.<pid>.<8 hex>.tmp``."""
    return path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")


_TEMP = re.compile(r"\..+\.(\d+)\.[0-9a-f]{8}\.tmp")
# A temporary file lives for milliseconds while it is written. One older than this whose process is gone
# is left over; the age also covers a writer whose pid this process cannot see (another pid namespace,
# such as a container sharing the home, or another machine), whose process check would say gone.
STALE_TEMP_AGE_S = 600


def remove_stale_temps(directory: Path, min_age_s: float = STALE_TEMP_AGE_S) -> int:
    """Remove the temporary files (``temp_name``) in ``directory`` left by processes that no longer
    run, such as one killed between writing a page and removing its temporary name; return how many.
    Only a file modified more than ``min_age_s`` ago whose process is not running is removed; a file
    whose process still runs (or whose id was reused) is left alone."""
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        match = _TEMP.fullmatch(name)
        if match is None or _alive(int(match.group(1))):
            continue
        try:
            st = os.stat(Path(directory) / name, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode) and time.time() - st.st_mtime > min_age_s:
                os.unlink(Path(directory) / name)
                removed += 1
        except OSError:
            continue
    return removed


def _alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, ValueError):
        return pid > 0  # another user's process, or an id that cannot be one: leave the file
    return True


def write_new(path: Path, data: bytes) -> bool:
    """Atomically create ``path`` (0600) holding exactly ``data``; False if ``path`` already exists.

    The data is written and fsynced to a private temporary file first and then hard-linked into
    place, so a reader never sees a partial file and an existing file is never replaced. The
    temporary file is removed whether or not the write succeeds (a full disk, say).
    """
    path = Path(path)
    tmp = temp_name(path)
    fd = open_private(tmp, os.O_WRONLY | os.O_EXCL)
    try:
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        _discard(tmp)


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with a private (0600) file holding exactly ``data``: written and fsynced to a
    temporary file, then renamed into place, so a reader sees the old or the new complete file. The
    temporary file is removed when any step fails."""
    path = Path(path)
    tmp = temp_name(path)
    fd = open_private(tmp, os.O_WRONLY | os.O_EXCL)
    try:
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        _discard(tmp)
        raise


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
