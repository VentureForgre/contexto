"""Cross-platform portability helpers for POSIX (macOS, Linux) and Windows (``win32``).

Keeps POSIX security guarantees intact on macOS and Linux (``fcntl.flock``, ``O_NOFOLLOW``,
``O_NONBLOCK``, ``0700``/``0600`` modes, ownership checks, directory ``fsync``, process groups,
POSIX signals, ``/bin/sh -c``) while providing clean Windows equivalents using the Python 3.12+
standard library only (Win32 ``LockFileEx``/``UnlockFileEx`` via ``ctypes``, ``O_BINARY``,
``CREATE_NEW_PROCESS_GROUP``, ``cmd.exe /c``, Windows command quoting, and non-blocking socket peek).
"""

from __future__ import annotations

import ctypes
import errno
import os
import queue
import select
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # POSIX
    _msvcrt = None

IS_WINDOWS: bool = sys.platform == "win32"

# POSIX-only open(2) flags default to 0 on platforms that do not define them.
O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)
O_CLOEXEC: int = getattr(os, "O_CLOEXEC", 0)
O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)
# On Windows os.open defaults to text mode (CRLF translation, Ctrl-Z EOF) unless O_BINARY is set.
O_BINARY: int = getattr(os, "O_BINARY", 0)

HAS_POSIX_PERMS: bool = hasattr(os, "geteuid")
HAS_DIR_FD: bool = os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")

# Win32 LockFileEx / UnlockFileEx / OpenProcess constants.
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_ERROR_ACCESS_DENIED = 5
_ERROR_LOCK_VIOLATION = 33
_ERROR_BROKEN_PIPE = 109
_ERROR_NOT_LOCKED = 158
_ERROR_IO_PENDING = 997
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
# Lock a single sentinel byte near INT64_MAX so mandatory Win32 LockFileEx on readable files such as
# log.jsonl never collides with concurrent readers of real file bytes (Win32 allows locking past EOF).
_WIN_LOCK_OFFSET_LOW = 0xFFFFFFFE
_WIN_LOCK_OFFSET_HIGH = 0x7FFFFFFF


class _Overlapped(ctypes.Structure):
    """Win32 OVERLAPPED structure for LockFileEx / UnlockFileEx."""

    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


_cached_kernel32: Any = None


def _kernel32() -> Any:
    """Lazily load kernel32 with explicit argtypes/restype for 64-bit pointer safety."""
    global _cached_kernel32
    if _cached_kernel32 is not None:
        return _cached_kernel32
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LockFileEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_Overlapped),
    ]
    k32.LockFileEx.restype = ctypes.c_int
    k32.UnlockFileEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_Overlapped),
    ]
    k32.UnlockFileEx.restype = ctypes.c_int
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    k32.GetExitCodeProcess.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = ctypes.c_int
    k32.PeekNamedPipe.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    k32.PeekNamedPipe.restype = ctypes.c_int
    _cached_kernel32 = k32
    return k32


def _win_osfhandle(fd: int) -> int:
    if _msvcrt is None:
        raise OSError(errno.ENOSYS, "msvcrt is unavailable on this platform")
    return _msvcrt.get_osfhandle(fd)


def _get_last_error() -> int:
    return ctypes.get_last_error()


def _win_error(err: int) -> OSError:
    win_error = getattr(ctypes, "WinError", None)
    if win_error is not None:
        return win_error(err)
    return OSError(err, f"Win32 error {err}")


def _win_lock(fd: int, flags: int) -> None:
    handle = _win_osfhandle(fd)
    ov = _Overlapped(Offset=_WIN_LOCK_OFFSET_LOW, OffsetHigh=_WIN_LOCK_OFFSET_HIGH)
    ok = _kernel32().LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(ov))
    if not ok:
        err = _get_last_error()
        if err in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
            raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))
        raise _win_error(err)


def _win_unlock(fd: int) -> None:
    handle = _win_osfhandle(fd)
    ov = _Overlapped(Offset=_WIN_LOCK_OFFSET_LOW, OffsetHigh=_WIN_LOCK_OFFSET_HIGH)
    ok = _kernel32().UnlockFileEx(handle, 0, 1, 0, ctypes.byref(ov))
    if not ok:
        err = _get_last_error()
        if err == _ERROR_NOT_LOCKED:
            return
        raise _win_error(err)


# File locking -----------------------------------------------------------------------------------------


def flock_ex(fd: int) -> None:
    """Acquire a blocking exclusive lock on ``fd``."""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
    else:
        _win_lock(fd, _LOCKFILE_EXCLUSIVE_LOCK)


def flock_sh(fd: int) -> None:
    """Acquire a blocking shared lock on ``fd``."""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_SH)
    else:
        _win_lock(fd, 0)


def flock_ex_nb(fd: int) -> None:
    """Acquire a non-blocking exclusive lock on ``fd``; raises ``BlockingIOError`` if contended."""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    else:
        _win_lock(fd, _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY)


def flock_sh_nb(fd: int) -> None:
    """Acquire a non-blocking shared lock on ``fd``; raises ``BlockingIOError`` if contended."""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
    else:
        _win_lock(fd, _LOCKFILE_FAIL_IMMEDIATELY)


def flock_un(fd: int) -> None:
    """Release any lock held on ``fd``."""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
    else:
        _win_unlock(fd)


def unlock_and_close(fd: int) -> None:
    """Release any Windows file lock synchronously before closing ``fd``; on POSIX closes ``fd``."""
    if _fcntl is None:
        try:
            _win_unlock(fd)
        except OSError:
            pass
    os.close(fd)


# File permissions, descriptors & metadata -------------------------------------------------------------


def set_blocking(fd: int, blocking: bool) -> None:
    """``os.set_blocking`` on POSIX; no-op on Windows (where Python 3.12+ only supports pipes)."""
    if IS_WINDOWS:
        return
    fn = getattr(os, "set_blocking", None)
    if fn is not None:
        fn(fd, blocking)


def fchmod_private(fd: int, mode: int) -> None:
    """``os.fchmod`` on POSIX; no-op on Windows."""
    if IS_WINDOWS:
        return
    fn = getattr(os, "fchmod", None)
    if fn is not None:
        fn(fd, mode)


def chmod_private(path: Path | str, mode: int) -> None:
    """Set POSIX mode bits on ``path``; no-op on Windows where ACLs govern privacy."""
    if HAS_POSIX_PERMS:
        os.chmod(path, mode)


def is_owned_by_current_user(st: os.stat_result) -> bool:
    """Whether ``st`` belongs to the effective user (POSIX); always True on Windows."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return True
    return st.st_uid == geteuid()


def is_owned_by_current_user_or_root(st: os.stat_result) -> bool:
    """Whether ``st`` belongs to the effective user or the process is root (POSIX); True on Windows."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return True
    euid = geteuid()
    return st.st_uid == euid or euid == 0


def has_group_or_other_perms(st: os.stat_result) -> bool:
    """Whether ``st`` grants any group or other permission bit (POSIX); False on Windows."""
    if not hasattr(os, "geteuid"):
        return False
    return bool(stat.S_IMODE(st.st_mode) & 0o077)


def fsync_dir(path: Path | str) -> None:
    """Flush directory metadata on POSIX; no-op on Windows where directories cannot be ``os.open``ed."""
    if IS_WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fcntl_getpath(fd: int) -> bytes | None:
    """macOS ``fcntl(fd, F_GETPATH)`` canonical path bytes, or None where unsupported."""
    if _fcntl is None:
        return None
    getpath = getattr(_fcntl, "F_GETPATH", None)
    if getpath is None:
        return None
    return _fcntl.fcntl(fd, getpath, bytes(1024)).split(b"\0", 1)[0]


def has_fcntl_getpath() -> bool:
    """Whether ``fcntl.F_GETPATH`` is available on this platform (macOS)."""
    return _fcntl is not None and hasattr(_fcntl, "F_GETPATH")


def pid_alive(pid: int) -> bool:
    """Whether process ``pid`` is currently running (without sending a terminating signal on Windows)."""
    if pid == os.getpid():
        return True
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OverflowError, ValueError):
            return pid > 0
        return True
    if pid > 0xFFFFFFFF:
        return False
    k32 = _kernel32()
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return _get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_uint32()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def pread(fd: int, size: int, offset: int) -> bytes:
    """Read up to ``size`` bytes from ``fd`` at ``offset`` without changing its file offset."""
    os_pread = getattr(os, "pread", None)
    if os_pread is not None:
        return os_pread(fd, size, offset)
    saved = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, size)
    finally:
        os.lseek(fd, saved, os.SEEK_SET)


def is_link_or_junction(path: Path | str) -> bool:
    """Whether ``path`` is a symbolic link or (on Windows) an NTFS directory junction."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction(path)) if isjunction is not None else False


def entry_is_link_or_junction(entry: os.DirEntry[str]) -> bool:
    """Whether directory entry ``entry`` is a symbolic link or (on Windows) an NTFS directory junction."""
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    return bool(is_junction()) if is_junction is not None else False


# Sockets & pipes --------------------------------------------------------------------------------------


def socket_peek(sock: Any, n: int, *, wait: bool) -> bytes:
    """Peek up to ``n`` bytes from ``sock`` without removing them.

    With ``wait`` False, uses ``MSG_DONTWAIT`` when available (POSIX) and falls back to a zero-timeout
    ``select.select`` check on Windows so blocking sockets are never mutated across threads. On
    platforms without ``MSG_DONTWAIT``, callers must ensure no other thread concurrently drains ``sock``
    between ``select`` and ``recv``. Raises ``BlockingIOError`` when no data is ready.
    """
    import socket

    if wait:
        return sock.recv(n, socket.MSG_PEEK)
    dontwait = getattr(socket, "MSG_DONTWAIT", 0)
    if dontwait:
        return sock.recv(n, socket.MSG_PEEK | dontwait)
    ready, _, _ = select.select([sock], [], [], 0.0)
    if not ready:
        raise BlockingIOError(errno.EAGAIN, "no data available")
    return sock.recv(n, socket.MSG_PEEK)


def _read_fd_until_win32(fd: int, *, max_bytes: int, deadline: float, chunk_size: int,
                         stop: Callable[[bytes], bool] | None,
                         read_fn: Callable[[int, int], bytes]) -> tuple[bytes, bool]:
    """Non-blocking Win32 ``PeekNamedPipe`` loop for pipe descriptors (never deadlocks UCRT ``os.close``)."""
    k32 = _kernel32()
    handle = _win_osfhandle(fd)
    data = b""
    avail = ctypes.c_uint32()
    while len(data) < max_bytes:
        left = deadline - time.monotonic()
        if left <= 0:
            return data, True
        avail.value = 0
        ok = k32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None)
        if not ok:
            err = _get_last_error()
            if err == _ERROR_BROKEN_PIPE:
                return data, False
            # Not a pipe (e.g. a regular file or NUL device): read directly without blocking.
            chunk = read_fn(fd, min(chunk_size, max_bytes - len(data)))
            if not chunk:
                return data, False
            data += chunk
            if stop is not None and stop(data):
                return data, False
            continue
        if avail.value == 0:
            time.sleep(min(0.01, left))
            continue
        to_read = min(chunk_size, max_bytes - len(data), avail.value)
        chunk = read_fn(fd, to_read)
        if not chunk:
            return data, False
        data += chunk
        if stop is not None and stop(data):
            return data, False
    return data, False


def read_fd_until(fd: int, *, max_bytes: int, deadline: float, chunk_size: int = 65536,
                  stop: Callable[[bytes], bool] | None = None,
                  read_fn: Callable[[int, int], bytes] = os.read) -> tuple[bytes, bool]:
    """Read from pipe descriptor ``fd`` until ``stop(data)`` is True, EOF, ``max_bytes``, or ``deadline``.

    Returns ``(data, timed_out)``. On Windows uses Win32 ``PeekNamedPipe`` so a timed-out reader never
    leaves a thread blocked inside UCRT ``os.read`` (which would deadlock ``os.close``).
    """
    if IS_WINDOWS:
        return _read_fd_until_win32(fd, max_bytes=max_bytes, deadline=deadline, chunk_size=chunk_size,
                                    stop=stop, read_fn=read_fn)
    chunks: queue.Queue[bytes | BaseException] = queue.Queue()

    def reader() -> None:
        remaining = max_bytes
        while remaining > 0:
            try:
                chunk = read_fn(fd, min(chunk_size, remaining))
            except BaseException as exc:
                chunks.put(exc)
                return
            chunks.put(chunk)
            if not chunk:
                return
            remaining -= len(chunk)

    threading.Thread(target=reader, name="contexto-pipe-read", daemon=True).start()
    data = b""
    while len(data) < max_bytes:
        left = deadline - time.monotonic()
        if left <= 0:
            return data, True
        try:
            item = chunks.get(timeout=left)
        except queue.Empty:
            return data, True
        if isinstance(item, BaseException):
            raise item
        if not item:
            return data, False
        data += item
        if stop is not None and stop(data):
            return data, False
    return data, False


# Signals & process groups -----------------------------------------------------------------------------


def available_signals(*names: str) -> tuple[int, ...]:
    """The signal numbers for each name in ``names`` that exists on this platform."""
    return tuple(sig for name in names if (sig := getattr(signal, name, None)) is not None)


def new_process_group_kwargs(*, platform: str | None = None) -> dict[str, Any]:
    """``subprocess.Popen`` kwargs that place the child in its own process group."""
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


def kill_process_group(child: subprocess.Popen, *, timeout: float = 1.0) -> None:
    """Forcibly stop ``child`` and (on POSIX) its process group, then reap it up to ``timeout``."""
    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if killpg is not None and sigkill is not None:
        try:
            killpg(child.pid, sigkill)
        except (OSError, AttributeError):
            pass
    try:
        child.kill()
    except OSError:
        pass
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def terminate_process_group(child: subprocess.Popen, *, grace_timeout: float = 10.0,
                            wait_fn: Callable[[float], int | None] | None = None) -> int | None:
    """Gracefully terminate ``child``'s process group, then force-kill if still running."""
    def _wait(limit: float) -> int | None:
        if wait_fn is not None:
            return wait_fn(limit)
        try:
            return child.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            return None

    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if killpg is not None and sigkill is not None:
        for signum in (signal.SIGTERM, sigkill):
            if child.poll() is not None:
                break
            try:
                killpg(child.pid, signum)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            _wait(grace_timeout)
        return child.poll()
    for action in (child.terminate, child.kill):
        if child.poll() is not None:
            break
        try:
            action()
        except OSError:
            pass
        _wait(grace_timeout)
    return child.poll()


# Shell commands & quoting -----------------------------------------------------------------------------

_WIN_QUOTE_CHARS = frozenset(' \t\r\n"&|<>^()%!')


def key_helper_shell(*, platform: str | None = None,
                     env: Mapping[str, str] | None = None) -> tuple[str, list[str]]:
    """``(shell, flag_args)`` for running a key-helper command string on ``platform``."""
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        source = os.environ if env is None else env
        comspec = (source.get("COMSPEC") or "").strip()
        return comspec or "cmd.exe", ["/c"]
    return "/bin/sh", ["-c"]


def shell_command(command: str, *, platform: str | None = None,
                  env: Mapping[str, str] | None = None) -> list[str]:
    """Argv to run ``command`` through the platform's shell."""
    shell, flags = key_helper_shell(platform=platform, env=env)
    return [shell, *flags, command]


def run_shell_command(command: str, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run shell string ``command`` via ``/bin/sh -c`` on POSIX or ``shell=True`` (``cmd.exe /c``) on Windows.

    On Windows, passing ``["cmd.exe", "/c", command]`` causes ``subprocess.list2cmdline`` to escape
    embedded double quotes as ``\\"``, which breaks ``cmd.exe`` command lines containing quoted paths;
    ``shell=True`` passes ``command`` directly to ``%COMSPEC% /c`` without escaping quotes.
    """
    if IS_WINDOWS:
        return subprocess.run(command, shell=True, **kwargs)
    return subprocess.run(shell_command(command), **kwargs)


def _quote_windows_arg(arg: str) -> str:
    """Quote one argument for Windows ``CreateProcess`` / ``cmd.exe`` command lines."""
    if arg and not any(ch in _WIN_QUOTE_CHARS for ch in arg):
        return arg
    out = ['"']
    backslashes = 0
    for ch in arg:
        if ch == "\\":
            backslashes += 1
        elif ch == '"':
            out.append("\\" * (backslashes * 2 + 1))
            out.append('"')
            backslashes = 0
        else:
            if backslashes:
                out.append("\\" * backslashes)
                backslashes = 0
            out.append(ch)
    if backslashes:
        out.append("\\" * (backslashes * 2))
    out.append('"')
    return "".join(out)


def quote_command(argv: Sequence[str], *, platform: str | None = None) -> str:
    """Join ``argv`` into a shell command line for ``platform`` (POSIX ``shlex`` or Windows quoting)."""
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        return " ".join(_quote_windows_arg(arg) for arg in argv)
    return shlex.join(argv)
