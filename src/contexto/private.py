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

from contexto.platform_compat import (
    IS_WINDOWS,
    O_BINARY,
    O_DIRECTORY,
    O_NOFOLLOW,
    O_NONBLOCK,
    fchmod_private,
    is_link_or_junction,
    pid_alive,
    set_blocking,
)

DIR_MODE = 0o700
FILE_MODE = 0o600
AFFINITY_SALT_FILE = "affinity.salt"
AFFINITY_SALT_BYTES = 32


def _chmod_dir_nofollow(path: Path) -> None:
    """Narrow ``path`` to ``DIR_MODE`` without following a symlink at ``path``, refusing non-directories."""
    if IS_WINDOWS or O_DIRECTORY == 0:
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            raise
        if is_link_or_junction(path) or not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", str(path))
        return
    try:
        fd = os.open(path, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", str(path)) from e
        raise
    try:
        fchmod_private(fd, DIR_MODE)
    finally:
        os.close(fd)


def private_dir(path: Path, *, narrow: bool = True) -> Path:
    """Create ``path`` and any missing parents as 0700; force an existing ``path`` to 0700 too, unless
    ``narrow`` is false (a directory the user chose, such as ``$CONTEXTO_HOME``, D55). A ``path`` that
    exists and is not a directory raises NotADirectoryError before any mode is changed. Symlinks are
    never followed when creating or chmodding directories."""
    path = Path(path)
    missing = []
    p = path
    while not os.path.lexists(p):
        missing.append(p)
        parent = p.parent
        if parent == p:
            break
        p = parent
    if not missing:
        if narrow:
            _chmod_dir_nofollow(path)
            return path
        if not path.is_dir():
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", str(path))
        return path
    for d in reversed(missing):
        try:
            os.mkdir(d, DIR_MODE)
        except FileExistsError:
            pass
        _chmod_dir_nofollow(d)
    return path


class NotRegularFileError(OSError):
    """A FIFO, device, directory or socket stands where a session file should be."""


def _open_nofollow(path: Path, flags: int, mode: int = 0o777) -> int:
    """``os.open`` with ``O_NOFOLLOW``; a symlink at ``path``, or a directory opened for writing, is refused
    as ``NotRegularFileError``, so every caller treats it as it treats a FIFO there (D58)."""
    if O_NOFOLLOW == 0 and is_link_or_junction(path):
        # Match POSIX open(O_CREAT | O_EXCL | O_NOFOLLOW): an existing symlink (live or dangling)
        # raises FileExistsError rather than ELOOP, and never creates the dangling symlink's target.
        if (flags & (os.O_CREAT | os.O_EXCL)) == (os.O_CREAT | os.O_EXCL):
            raise FileExistsError(errno.EEXIST, "file exists", str(path))
        raise NotRegularFileError(f"{path} is a symlink")
    try:
        return os.open(path, flags | O_NOFOLLOW | O_BINARY, mode)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK) and os.path.islink(path):  # EMLINK on FreeBSD
            raise NotRegularFileError(f"{path} is a symlink") from e
        if e.errno == errno.EISDIR or (IS_WINDOWS and e.errno == errno.EACCES and os.path.isdir(path)):
            raise NotRegularFileError(f"{path} is a directory") from e
        raise


def _check_regular(fd: int, path: Path) -> None:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotRegularFileError(f"{path} is not a regular file")


def open_private(path: Path, flags: int, *, create: bool = True) -> int:
    """``os.open`` with ``O_CREAT`` semantics that leaves the file at 0600; returns the fd. With
    ``create`` false a missing file is ``FileNotFoundError`` and nothing is made.

    A symlink at ``path`` is never followed (``NotRegularFileError``), and anything but a regular file there
    is refused (``NotRegularFileError``) without blocking: the open is ``O_NONBLOCK``, so a FIFO does
    not wait for a reader. The descriptor is then made blocking again."""
    fd = _open_nofollow(path, flags | (os.O_CREAT if create else 0) | O_NONBLOCK, FILE_MODE)
    _check_regular(fd, path)
    try:
        set_blocking(fd, True)
        fchmod_private(fd, FILE_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_regular(path: Path, *, follow: bool = False) -> int:
    """A read-only descriptor for the regular file at ``path``. Never blocks on a FIFO and refuses
    anything but a regular file (``NotRegularFileError``), a symlink included unless ``follow``, for a
    file that is not Contexto's own (a pinned file source)."""
    flags = os.O_RDONLY | O_NONBLOCK | O_BINARY
    if follow:
        try:
            fd = os.open(path, flags)
        except OSError as e:
            if IS_WINDOWS and e.errno == errno.EACCES and os.path.isdir(path):
                raise NotRegularFileError(f"{path} is a directory") from e
            raise
    else:
        fd = _open_nofollow(path, flags)
    _check_regular(fd, path)
    return fd


def read_regular(path: Path, *, follow: bool = False, max_bytes: int | None = None) -> bytes:
    """The bytes of the regular file at ``path``, read as ``open_regular`` opens it. When
    ``max_bytes`` is set, refuses any file larger than that bound with ``ValueError``."""
    fd = open_regular(path, follow=follow)
    try:
        if max_bytes is not None and os.fstat(fd).st_size > max_bytes:
            raise ValueError(f"{path} exceeds {max_bytes} bytes")
        chunks = []
        total = 0
        while True:
            to_read = 1 << 20 if max_bytes is None else min(1 << 20, max_bytes + 1 - total)
            chunk = os.read(fd, to_read)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError(f"{path} exceeds {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_regular_text(path: Path, *, follow: bool = False, max_bytes: int | None = None) -> str:
    """``read_regular`` decoded as UTF-8 (UnicodeDecodeError, a ValueError, when it is not)."""
    return read_regular(path, follow=follow, max_bytes=max_bytes).decode("utf-8")


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
    return pid_alive(pid)


_LINK_UNSUPPORTED = frozenset({
    errno.EPERM,
    errno.EOPNOTSUPP,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    getattr(errno, "ENOSYS", errno.EOPNOTSUPP),
})


def fsync_dir(path: Path) -> None:
    """Flush directory entries of ``path`` to disk; ignores filesystems where directory fsync is unsupported."""
    if IS_WINDOWS:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as e:
        ignored = {errno.EINVAL, errno.EBADF, errno.EROFS}
        for name in ("EOPNOTSUPP", "ENOTSUP"):
            code = getattr(errno, name, None)
            if code is not None:
                ignored.add(code)
        if e.errno not in ignored:
            raise
    finally:
        os.close(fd)


def write_new(path: Path, data: bytes) -> bool:
    """Atomically create ``path`` (0600) holding exactly ``data``; False if ``path`` already exists.

    The data is written and fsynced to a private temporary file first, hard-linked into place, and
    the parent directory is fsynced so a reader never sees a partial file and an existing file is
    never replaced. If the filesystem refuses ``os.link`` with ``EPERM``/``EOPNOTSUPP``/``ENOSYS``,
    falls back to ``O_CREAT | O_EXCL | O_NOFOLLOW`` so existing files and symlinks are still never
    overwritten or followed. The temporary file is removed whether or not the write succeeds.
    """
    path = Path(path)
    tmp = temp_name(path)
    fd = open_private(tmp, os.O_WRONLY | os.O_EXCL)
    created = False
    try:
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
            created = True
            return True
        except OSError as exc:
            if exc.errno not in _LINK_UNSUPPORTED:
                raise
            _discard(tmp)
            created = _write_new_excl(path, data)
            return created
    except FileExistsError:
        return False
    finally:
        _discard(tmp)
        if created:
            fsync_dir(path.parent)


def _write_new_excl(path: Path, data: bytes) -> bool:
    """Fallback for filesystems where ``os.link`` is disallowed: create exclusively with
    ``O_CREAT | O_EXCL | O_NOFOLLOW``, write and fsync, removing partial output on failure."""
    try:
        fd = open_private(path, os.O_WRONLY | os.O_EXCL)
    except FileExistsError:
        return False
    try:
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except BaseException:
        _discard(path)
        raise


def _replace_atomic(src: Path, dst: Path) -> None:
    """Rename ``src`` over ``dst`` atomically, retrying briefly on Windows when a concurrent reader
    transiently holds ``dst`` open (WinError 5 / 32)."""
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    for attempt in range(25):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 24:
                raise
            time.sleep(min(0.001 * (attempt + 1), 0.02))


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with a private (0600) file holding exactly ``data``: written and fsynced to a
    temporary file, renamed into place, and the parent directory fsynced so a reader sees the old or
    the new complete file and the rename survives a crash. The temporary file is removed when any
    step fails."""
    path = Path(path)
    tmp = temp_name(path)
    fd = open_private(tmp, os.O_WRONLY | os.O_EXCL)
    try:
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        _replace_atomic(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
    fsync_dir(path.parent)


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
    if O_NOFOLLOW == 0 and os.path.islink(path):
        raise OSError(errno.ELOOP, "symlink refused", str(path))
    if IS_WINDOWS and os.path.isdir(path):
        raise ValueError(f"{path} is not a regular file")
    fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_BINARY)
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
