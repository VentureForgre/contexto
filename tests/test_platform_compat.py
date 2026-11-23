"""Unit tests for ``contexto.platform_compat`` covering both native POSIX/Windows and simulated Windows paths."""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from contexto import codex, platform_compat, statusline, upstream
from contexto.platform_compat import (
    O_BINARY,
    O_CLOEXEC,
    O_DIRECTORY,
    O_NOFOLLOW,
    O_NONBLOCK,
    available_signals,
    chmod_private,
    entry_is_link_or_junction,
    fchmod_private,
    fcntl_getpath,
    flock_ex,
    flock_ex_nb,
    flock_sh,
    flock_sh_nb,
    flock_un,
    fsync_dir,
    has_fcntl_getpath,
    has_group_or_other_perms,
    is_link_or_junction,
    is_owned_by_current_user,
    is_owned_by_current_user_or_root,
    key_helper_shell,
    kill_process_group,
    new_process_group_kwargs,
    pid_alive,
    pread,
    quote_command,
    read_fd_until,
    run_shell_command,
    set_blocking,
    shell_command,
    socket_peek,
    terminate_process_group,
    unlock_and_close,
)

# Native file locking ----------------------------------------------------------------------------------


def test_flock_exclusive_blocks_nonblocking_exclusive_and_shared(tmp_path):
    lock = tmp_path / "test.lock"
    fd1 = os.open(lock, os.O_RDWR | os.O_CREAT | O_BINARY, 0o600)
    fd2 = os.open(lock, os.O_RDWR | O_BINARY, 0o600)
    try:
        flock_ex(fd1)
        with pytest.raises(BlockingIOError):
            flock_ex_nb(fd2)
        with pytest.raises(BlockingIOError):
            flock_sh_nb(fd2)
        flock_un(fd1)
        flock_ex_nb(fd2)
        flock_un(fd2)
    finally:
        unlock_and_close(fd1)
        unlock_and_close(fd2)


def test_flock_shared_allows_concurrent_shared_and_blocks_exclusive(tmp_path):
    lock = tmp_path / "shared.lock"
    fd1 = os.open(lock, os.O_RDWR | os.O_CREAT | O_BINARY, 0o600)
    fd2 = os.open(lock, os.O_RDWR | O_BINARY, 0o600)
    fd3 = os.open(lock, os.O_RDWR | O_BINARY, 0o600)
    try:
        flock_sh(fd1)
        flock_sh_nb(fd2)
        with pytest.raises(BlockingIOError):
            flock_ex_nb(fd3)
        flock_un(fd1)
        with pytest.raises(BlockingIOError):
            flock_ex_nb(fd3)
        flock_un(fd2)
        flock_ex_nb(fd3)
        flock_un(fd3)
    finally:
        unlock_and_close(fd1)
        unlock_and_close(fd2)
        unlock_and_close(fd3)


# Simulated Windows locking (runs on macOS/Linux as well as Windows) -----------------------------------


class _FakeKernel32:
    """In-memory mock of Win32 LockFileEx / UnlockFileEx / OpenProcess / GetExitCodeProcess / CloseHandle."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.last_error = 0
        self.lock_result = 1
        self.unlock_result = 1
        self.open_handle = 42
        self.exit_code = 259  # STILL_ACTIVE
        self.get_exit_ok = 1
        self.peek_ok = 1
        self.peek_avail = 0

    def LockFileEx(self, handle, flags, reserved, low, high, overlapped):
        ov = ctypes.cast(overlapped, ctypes.POINTER(platform_compat._Overlapped)).contents
        self.calls.append(("LockFileEx", (handle, flags, reserved, low, high, ov.Offset, ov.OffsetHigh)))
        return self.lock_result

    def UnlockFileEx(self, handle, reserved, low, high, overlapped):
        ov = ctypes.cast(overlapped, ctypes.POINTER(platform_compat._Overlapped)).contents
        self.calls.append(("UnlockFileEx", (handle, reserved, low, high, ov.Offset, ov.OffsetHigh)))
        return self.unlock_result

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("OpenProcess", (access, inherit, pid)))
        return self.open_handle

    def GetExitCodeProcess(self, handle, out_ptr):
        self.calls.append(("GetExitCodeProcess", (handle,)))
        if self.get_exit_ok:
            ctypes.cast(out_ptr, ctypes.POINTER(ctypes.c_uint32))[0] = self.exit_code
        return self.get_exit_ok

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", (handle,)))
        return 1

    def PeekNamedPipe(self, handle, buf, size, read_ptr, avail_ptr, left_ptr):
        self.calls.append(("PeekNamedPipe", (handle,)))
        if self.peek_ok:
            ctypes.cast(avail_ptr, ctypes.POINTER(ctypes.c_uint32))[0] = self.peek_avail
        return self.peek_ok


@pytest.fixture
def fake_win32_locks(monkeypatch):
    fake = _FakeKernel32()
    monkeypatch.setattr(platform_compat, "_fcntl", None)
    monkeypatch.setattr(platform_compat, "_kernel32", lambda: fake)
    monkeypatch.setattr(platform_compat, "_win_osfhandle", lambda fd: 1000 + fd)
    monkeypatch.setattr(platform_compat, "_get_last_error", lambda: fake.last_error)
    return fake


def test_simulated_windows_lock_flags_and_unlock(fake_win32_locks):
    off_lo = platform_compat._WIN_LOCK_OFFSET_LOW
    off_hi = platform_compat._WIN_LOCK_OFFSET_HIGH
    flock_ex(7)
    flock_sh(7)
    flock_ex_nb(7)
    flock_sh_nb(7)
    flock_un(7)
    assert fake_win32_locks.calls == [
        ("LockFileEx", (1007, 0x2, 0, 1, 0, off_lo, off_hi)),
        ("LockFileEx", (1007, 0x0, 0, 1, 0, off_lo, off_hi)),
        ("LockFileEx", (1007, 0x3, 0, 1, 0, off_lo, off_hi)),
        ("LockFileEx", (1007, 0x1, 0, 1, 0, off_lo, off_hi)),
        ("UnlockFileEx", (1007, 0, 1, 0, off_lo, off_hi)),
    ]


@pytest.mark.parametrize("win_err", [33, 997])  # ERROR_LOCK_VIOLATION, ERROR_IO_PENDING
def test_simulated_windows_lock_contention_raises_blocking_io_error(fake_win32_locks, win_err):
    fake_win32_locks.lock_result = 0
    fake_win32_locks.last_error = win_err
    with pytest.raises(BlockingIOError):
        flock_ex_nb(5)


def test_simulated_windows_lock_unexpected_error_raises_oserror(fake_win32_locks):
    fake_win32_locks.lock_result = 0
    fake_win32_locks.last_error = 6  # ERROR_INVALID_HANDLE
    with pytest.raises(OSError) as exc:
        flock_ex(5)
    assert not isinstance(exc.value, BlockingIOError)


def test_simulated_windows_unlock_not_locked_is_noop(fake_win32_locks):
    fake_win32_locks.unlock_result = 0
    fake_win32_locks.last_error = 158  # ERROR_NOT_LOCKED
    flock_un(5)


def test_simulated_windows_unlock_and_close_unlocks_before_closing(fake_win32_locks, tmp_path):
    p = tmp_path / "f"
    fd = os.open(p, os.O_RDWR | os.O_CREAT | O_BINARY, 0o600)
    unlock_and_close(fd)
    assert fake_win32_locks.calls == [
        ("UnlockFileEx", (1000 + fd, 0, 1, 0, platform_compat._WIN_LOCK_OFFSET_LOW,
                          platform_compat._WIN_LOCK_OFFSET_HIGH)),
    ]
    with pytest.raises(OSError):
        os.fstat(fd)  # descriptor is closed


# Permissions, ownership, fsync_dir, fcntl_getpath, pid_alive -----------------------------------------


def test_open_flags_are_integers():
    for flag in (O_NOFOLLOW, O_NONBLOCK, O_CLOEXEC, O_DIRECTORY, O_BINARY):
        assert isinstance(flag, int) and flag >= 0


def test_simulated_windows_permission_guards(monkeypatch, tmp_path):
    f = tmp_path / "file"
    f.write_text("hi")
    st = f.stat()
    monkeypatch.setattr(platform_compat, "HAS_POSIX_PERMS", False)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(os, "geteuid", None, raising=False)
    monkeypatch.delattr(os, "geteuid", raising=False)
    monkeypatch.delattr(os, "fchmod", raising=False)
    # Python 3.12+ defines os.set_blocking on Windows (for pipes only), where calling it on a
    # regular file raises WinError 87; set_blocking must be a no-op on IS_WINDOWS even when present.
    monkeypatch.setattr(
        os, "set_blocking",
        lambda _fd, _b: (_ for _ in ()).throw(OSError("WinError 87")),
        raising=False,
    )

    # All guards must be safe no-ops / permissive on Windows without raising AttributeError.
    chmod_private(f, 0o600)
    fchmod_private(0, 0o600)
    set_blocking(0, True)
    fsync_dir(tmp_path)
    assert is_owned_by_current_user(st) is True
    assert is_owned_by_current_user_or_root(st) is True
    assert has_group_or_other_perms(st) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions check")
def test_posix_permission_helpers_enforce_owner_and_mode(tmp_path):
    f = tmp_path / "secret"
    f.write_text("x")
    os.chmod(f, 0o600)
    st_private = f.stat()
    assert is_owned_by_current_user(st_private) is True
    assert is_owned_by_current_user_or_root(st_private) is True
    assert has_group_or_other_perms(st_private) is False

    os.chmod(f, 0o640)
    st_wide = f.stat()
    assert has_group_or_other_perms(st_wide) is True

    other_st = types.SimpleNamespace(st_uid=os.geteuid() + 1, st_mode=stat.S_IFREG | 0o600)
    assert is_owned_by_current_user(other_st) is False  # type: ignore[arg-type]
    if os.geteuid() != 0:
        assert is_owned_by_current_user_or_root(other_st) is False  # type: ignore[arg-type]


def test_fcntl_getpath_when_fcntl_absent(monkeypatch):
    monkeypatch.setattr(platform_compat, "_fcntl", None)
    assert has_fcntl_getpath() is False
    assert fcntl_getpath(0) is None


def test_pid_alive_native_and_simulated_windows(monkeypatch):
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False
    assert pid_alive(-1) is False

    fake = _FakeKernel32()
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "_kernel32", lambda: fake)
    monkeypatch.setattr(platform_compat, "_get_last_error", lambda: fake.last_error)

    # Running process (STILL_ACTIVE == 259)
    fake.open_handle = 99
    fake.exit_code = 259
    assert pid_alive(999999) is True
    # Exited process (exit code 0)
    fake.exit_code = 0
    assert pid_alive(999999) is False
    # Access denied means process exists
    fake.open_handle = 0
    fake.last_error = 5  # ERROR_ACCESS_DENIED
    assert pid_alive(999999) is True
    # Invalid parameter means process does not exist
    fake.last_error = 87  # ERROR_INVALID_PARAMETER
    assert pid_alive(999999) is False
    # Out-of-range DWORD PID must not wrap modulo 2**32 into a live PID
    fake.open_handle = 99
    fake.exit_code = 259
    assert pid_alive((1 << 32) + 4) is False


def test_pread_native_and_simulated_windows_preserves_offset(monkeypatch, tmp_path):
    p = tmp_path / "data.bin"
    p.write_bytes(b"0123456789abcdef")
    fd = os.open(p, os.O_RDONLY | O_BINARY)
    try:
        assert os.read(fd, 3) == b"012"
        # Native pread
        assert pread(fd, 4, 10) == b"abcd"
        assert os.lseek(fd, 0, os.SEEK_CUR) == 3
        # Simulated Windows fallback (no os.pread) must also preserve the fd offset
        monkeypatch.delattr(os, "pread", raising=False)
        assert pread(fd, 4, 10) == b"abcd"
        assert os.lseek(fd, 0, os.SEEK_CUR) == 3
        assert os.read(fd, 3) == b"345"
    finally:
        os.close(fd)


def test_is_link_or_junction_detects_junctions(monkeypatch, tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    assert is_link_or_junction(real_dir) is False
    monkeypatch.setattr(os.path, "isjunction", lambda p: Path(p) == real_dir, raising=False)
    assert is_link_or_junction(real_dir) is True

    class _FakeEntry:
        def is_symlink(self) -> bool:
            return False

        def is_junction(self) -> bool:
            return True

    assert entry_is_link_or_junction(_FakeEntry()) is True  # type: ignore[arg-type]


def test_session_readers_and_project_forget_reject_junctions(monkeypatch, tmp_path):
    from contexto import stats
    from contexto.store import SessionNotFoundError, SessionStore, project, sessions

    home = tmp_path / "home"
    s = SessionStore(home, "junc-s1")
    s.release()
    sdir = s.dir

    monkeypatch.setattr(os.path, "isjunction", lambda p: Path(p) == sdir, raising=False)

    with pytest.raises(SessionNotFoundError):
        sessions.resolve_session(home, "junc-s1")
    with pytest.raises(SessionNotFoundError):
        stats.session_stats(home, "junc-s1")
    assert statusline.find_session(home, "junc-s1") is None
    assert statusline.session_numbers(home, "junc-s1") is None
    with pytest.raises(SessionNotFoundError):
        project.forget(home, "junc-s1", leave=True)
    with pytest.raises(NotADirectoryError):
        project._real_directory(sdir)


# Sockets & pipes --------------------------------------------------------------------------------------


def test_socket_peek_blocking_and_nonblocking_with_and_without_msg_dontwait(monkeypatch):
    s1, s2 = socket.socketpair()
    try:
        # Native path: nothing sent yet -> BlockingIOError
        with pytest.raises(BlockingIOError):
            socket_peek(s1, 16, wait=False)
        # Simulated Windows path (no MSG_DONTWAIT): nothing sent yet -> BlockingIOError
        monkeypatch.delattr(socket, "MSG_DONTWAIT", raising=False)
        with pytest.raises(BlockingIOError):
            socket_peek(s1, 16, wait=False)
        # Send data: non-blocking peek sees it without consuming it
        s2.sendall(b"GET /tok/health")
        assert socket_peek(s1, 15, wait=False) == b"GET /tok/health"
        assert socket_peek(s1, 15, wait=True) == b"GET /tok/health"
        assert s1.recv(15) == b"GET /tok/health"
    finally:
        s1.close()
        s2.close()


def test_read_fd_until_reads_and_times_out():
    r_fd, w_fd = os.pipe()
    try:
        os.write(w_fd, b"hello 1.2.3\n")
        data, timed_out = read_fd_until(
            r_fd,
            max_bytes=1024,
            deadline=time.monotonic() + 2.0,
            stop=lambda buf: b"\n" in buf,
        )
        assert data == b"hello 1.2.3\n" and timed_out is False

        # Pipe stays open with no more data -> times out cleanly
        data2, timed_out2 = read_fd_until(r_fd, max_bytes=1024, deadline=time.monotonic() + 0.1)
        assert data2 == b"" and timed_out2 is True
    finally:
        os.close(w_fd)
        os.close(r_fd)


def test_simulated_windows_read_fd_until_uses_peek_named_pipe(monkeypatch):
    fake = _FakeKernel32()
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "_kernel32", lambda: fake)
    monkeypatch.setattr(platform_compat, "_win_osfhandle", lambda fd: 2000 + fd)
    monkeypatch.setattr(platform_compat, "_get_last_error", lambda: fake.last_error)

    # Available bytes -> reads and stops on newline
    fake.peek_ok = 1
    fake.peek_avail = 12
    data, timed_out = read_fd_until(
        3,
        max_bytes=1024,
        deadline=time.monotonic() + 2.0,
        stop=lambda buf: b"\n" in buf,
        read_fn=lambda fd, n: b"hello 1.2.3\n"[:n],
    )
    assert data == b"hello 1.2.3\n" and timed_out is False

    # No bytes available -> times out without calling read_fn
    fake.peek_avail = 0
    data2, timed_out2 = read_fd_until(
        3,
        max_bytes=1024,
        deadline=time.monotonic() + 0.05,
        read_fn=lambda fd, n: pytest.fail("should not read when PeekNamedPipe reports 0 bytes"),
    )
    assert data2 == b"" and timed_out2 is True

    # ERROR_BROKEN_PIPE (109) -> clean EOF without timeout
    fake.peek_ok = 0
    fake.last_error = 109
    data3, timed_out3 = read_fd_until(
        3,
        max_bytes=1024,
        deadline=time.monotonic() + 2.0,
        read_fn=lambda fd, n: pytest.fail("should not read on broken pipe"),
    )
    assert data3 == b"" and timed_out3 is False


# Signals & process groups -----------------------------------------------------------------------------


def test_available_signals_filters_missing_names():
    found = available_signals("SIGINT", "SIGTERM", "SIG_DEFINITELY_NOT_REAL_12345")
    assert signal.SIGINT in found and signal.SIGTERM in found
    assert len(found) == 2


def test_new_process_group_kwargs_posix_and_windows():
    assert new_process_group_kwargs(platform="linux") == {"start_new_session": True}
    assert new_process_group_kwargs(platform="darwin") == {"start_new_session": True}
    win = new_process_group_kwargs(platform="win32")
    assert "creationflags" in win and "start_new_session" not in win


def test_kill_and_terminate_process_group_fallback_without_killpg(monkeypatch):
    monkeypatch.delattr(os, "killpg", raising=False)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        kill_process_group(proc, timeout=5.0)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    proc2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        code = terminate_process_group(proc2, grace_timeout=5.0)
        assert code is not None
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait()


# Shell commands, quoting & managed settings paths -----------------------------------------------------


def test_key_helper_shell_and_shell_command():
    assert key_helper_shell(platform="darwin") == ("/bin/sh", ["-c"])
    assert key_helper_shell(platform="linux") == ("/bin/sh", ["-c"])
    assert key_helper_shell(platform="win32", env={}) == ("cmd.exe", ["/c"])
    assert key_helper_shell(platform="win32", env={"COMSPEC": r"C:\Windows\System32\cmd.exe"}) == (
        r"C:\Windows\System32\cmd.exe",
        ["/c"],
    )
    assert shell_command("echo hi", platform="linux") == ["/bin/sh", "-c", "echo hi"]
    assert shell_command("echo hi", platform="win32", env={}) == ["cmd.exe", "/c", "echo hi"]


def test_run_shell_command_uses_shell_true_on_windows_and_sh_on_posix(monkeypatch):
    seen: list[tuple[object, dict[str, object]]] = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    run_shell_command('"C:\\Program Files\\Python\\python.exe" -c "print(1)"', capture_output=True)
    assert seen[-1] == ('"C:\\Program Files\\Python\\python.exe" -c "print(1)"',
                        {"shell": True, "capture_output": True})

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "linux")
    run_shell_command("echo ok", capture_output=True)
    assert seen[-1] == (["/bin/sh", "-c", "echo ok"], {"capture_output": True})


def test_statusline_launch_rejects_windows_unsafe_cmd_chars(monkeypatch, tmp_path):
    monkeypatch.setattr(statusline, "IS_WINDOWS", True)
    cwd = tmp_path / "work"
    cwd.mkdir()
    user_home = tmp_path / "user"
    user_home.mkdir()
    # Normal path with spaces is allowed on Windows (quoted cleanly).
    ok_home = tmp_path / "my home"
    settings, reason = statusline.launch_status_line(
        "s1", ok_home, cwd, env={}, user_home=user_home, managed_dir=tmp_path / "none",
    )
    assert reason == "added" and "statusLine" in settings
    # Paths with % or ! (cmd.exe variable expansion) are rejected as unsafe_path.
    bad_home = tmp_path / "home%PATH%"
    settings2, reason2 = statusline.launch_status_line(
        "s1", bad_home, cwd, env={}, user_home=user_home, managed_dir=tmp_path / "none",
    )
    assert settings2 == {} and reason2 == "unsafe_path"


def test_codex_api_key_config_uses_platform_shell(tmp_path):
    posix_cfg = codex.api_key_config("http://127.0.0.1:9999", "echo tok", "s1", tmp_path, env={}, platform="linux")
    assert 'command = "/bin/sh"' in posix_cfg
    assert 'args = ["-c", "echo tok"]' in posix_cfg

    win_cfg = codex.api_key_config("http://127.0.0.1:9999", "echo tok", "s1", tmp_path, env={}, platform="win32")
    assert 'command = "cmd.exe"' in win_cfg
    assert 'args = ["/c", "echo tok"]' in win_cfg

    win_comspec = codex.api_key_config(
        "http://127.0.0.1:9999", "echo tok", "s1", tmp_path,
        env={"COMSPEC": r"C:\Windows\System32\cmd.exe"}, platform="win32",
    )
    assert r'command = "C:\\Windows\\System32\\cmd.exe"' in win_comspec


def test_quote_command_posix_and_windows():
    argv = [r"C:\Program Files\Python312\python.exe", "-P", "-m", "contexto", "statusline",
            "--session=s1", r"--home=C:\Users\my name\.contexto", r'--own-settings=C:\a&b\say "hi"\s.json']
    win_cmd = quote_command(argv, platform="win32")
    assert win_cmd.startswith(r'"C:\Program Files\Python312\python.exe" -P -m contexto statusline --session=s1 ')
    assert r'"--home=C:\Users\my name\.contexto"' in win_cmd
    assert r'"--own-settings=C:\a&b\say \"hi\"\s.json"' in win_cmd

    # Trailing backslash inside a quoted Windows argument must be doubled so it doesn't escape the closing quote.
    assert quote_command(["C:\\my dir\\"], platform="win32") == '"C:\\my dir\\\\"'
    assert quote_command([""], platform="win32") == '""'

    # Statusline helper delegates platform cleanly
    sl_win = statusline.installed_command("s1", Path(r"C:\my home"), [], color=True,
                                          executable=r"C:\Program Files\Python\python.exe", platform="win32")
    assert r'"C:\Program Files\Python\python.exe"' in sl_win
    assert "--color" in sl_win


def test_windows_managed_settings_paths():
    assert "win32" in upstream.MANAGED_SETTINGS_FILES
    out = subprocess.check_output(
        [sys.executable, "-c", "import contexto.upstream as u; print(u.MANAGED_SETTINGS_FILES['win32'])"],
        text=True,
    ).strip()
    assert out == r"C:\ProgramData\ClaudeCode\managed-settings.json"
    assert statusline.MANAGED_DIRS["win32"] == r"C:\ProgramData\ClaudeCode"
    assert statusline.managed_settings_dir("win32") == Path(r"C:\ProgramData\ClaudeCode")


def test_every_package_module_imports_without_posix_only_apis():
    """Simulate a Windows runtime (no fcntl, no POSIX open flags/signals/MSG_DONTWAIT/poll) and import every module."""
    probe = r"""
import getpass, http.client, importlib, os, pathlib, pkgutil, plistlib
import select, shutil, signal, socket, ssl, subprocess, sys, tempfile, urllib.request

sys.modules["fcntl"] = None
sys.platform = "win32"
for attr in (
    "O_NOFOLLOW", "O_NONBLOCK", "O_DIRECTORY", "fchmod", "set_blocking", "geteuid", "killpg", "fwalk", "pread",
):
    if hasattr(os, attr):
        delattr(os, attr)
for sig in ("SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2", "SIGALRM", "SIGKILL"):
    if hasattr(signal, sig):
        delattr(signal, sig)
if hasattr(select, "poll"):
    delattr(select, "poll")
if hasattr(socket, "MSG_DONTWAIT"):
    delattr(socket, "MSG_DONTWAIT")

import contexto
count = 0
for info in pkgutil.walk_packages(contexto.__path__, contexto.__name__ + "."):
    if info.name.endswith(".__main__"):
        continue
    importlib.import_module(info.name)
    count += 1
assert count > 20, count
print(f"imported {count} modules cleanly")
"""
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "imported " in proc.stdout


def test_private_simulated_windows_open_nofollow_refuses_symlink_and_dir(monkeypatch, tmp_path):
    from contexto import private

    monkeypatch.setattr(private, "O_NOFOLLOW", 0)
    monkeypatch.setattr(private, "IS_WINDOWS", True)

    target = tmp_path / "real.txt"
    target.write_text("ok")
    subdir = tmp_path / "subdir"
    subdir.mkdir()

    # Refuses directory
    with pytest.raises(private.NotRegularFileError):
        private.open_regular(subdir)

    # Refuses symlink even when O_NOFOLLOW == 0
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks require elevated privileges on this Windows host")
    with pytest.raises(private.NotRegularFileError):
        private.open_regular(link)

    # affinity_salt refuses both a symlink and a directory when O_NOFOLLOW == 0
    salt_home = tmp_path / "salt-link"
    salt_home.mkdir()
    (salt_home / private.AFFINITY_SALT_FILE).symlink_to(target)
    with pytest.raises(OSError):
        private.affinity_salt(salt_home)

    salt_dir_home = tmp_path / "salt-dir"
    (salt_dir_home / private.AFFINITY_SALT_FILE).mkdir(parents=True)
    with pytest.raises(ValueError, match="not a regular file"):
        private.affinity_salt(salt_dir_home)


def test_store_sessions_and_prune_simulated_windows_paths(monkeypatch, tmp_path):
    """Exercise the path-based disk_usage and prune fallbacks used when dir_fd/fwalk are unavailable."""
    from contexto.store import SessionStore
    from contexto.store import prune as prune_mod
    from contexto.store import sessions as sessions_mod

    home = tmp_path / "home"
    store = SessionStore(home, "win-s1")
    store.page_out([[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}]], 1)
    store.log({"event": "test"})
    store.log({"event": "test2"})  # exercises pread on non-empty log.jsonl
    store.release()

    monkeypatch.setattr(sessions_mod, "HAS_DIR_FD", False)
    monkeypatch.setattr(prune_mod, "HAS_DIR_FD", False)

    assert sessions_mod.disk_usage(store.dir) > 0
    selected = prune_mod.select(home, older_than=None, keep=0, session_id=None, now=time.time() + 3600)
    assert [c.id for c in selected] == ["win-s1"]

    # Live session is refused
    live_store = SessionStore(home, "win-s1")
    try:
        with pytest.raises(prune_mod.SessionLiveError):
            prune_mod.check_not_live(home, "win-s1", now=time.time() + 3600)
    finally:
        live_store.release()

    freed = prune_mod.remove_session(home, "win-s1", now=time.time() + 3600)
    assert freed > 0 and not store.dir.exists()


def test_servecontract_simulated_windows_guards(monkeypatch, tmp_path):
    from contexto import servecontract

    monkeypatch.setattr(servecontract, "O_NOFOLLOW", 0)
    monkeypatch.setattr(servecontract, "IS_WINDOWS", True)
    monkeypatch.setattr(servecontract, "is_owned_by_current_user", lambda st: True)
    monkeypatch.setattr(servecontract, "has_group_or_other_perms", lambda st: False)

    token = servecontract.new_token()
    (tmp_path / servecontract.TOKEN_FILE).write_text(token + "\n", encoding="utf-8")
    assert servecontract.read_token(tmp_path) == token

    # Directory in place of serve.token -> ServeFileError("not_regular")
    (tmp_path / servecontract.STATE_FILE).mkdir()
    with pytest.raises(servecontract.ServeFileError) as exc:
        servecontract.read_state(tmp_path)
    assert exc.value.kind == "not_regular"


def test_serve_make_room_skips_nonblocking_peek_on_reading_socket_without_msg_dontwait(monkeypatch, tmp_path):
    """On Windows (no MSG_DONTWAIT), _make_room must never peek a socket whose handler is past vouch()."""
    from contexto.serve import ServeService
    from contexto.servecontract import new_token
    from contexto.upstream import Upstream

    svc = ServeService(tmp_path, Upstream("https://api.anthropic.com", "default"), new_token(), reaper=False)
    s1, s2 = socket.socketpair()
    try:
        svc.admit(s1)
        s2.sendall(b"GET /wrong/health HTTP/1.1\r\n")
        svc.vouch(s1)
        assert s1 in svc._reading
        assert s1 not in svc._vouched

        monkeypatch.delattr(socket, "MSG_DONTWAIT", raising=False)
        peek_calls = []
        monkeypatch.setattr(svc, "shows_token", lambda conn, wait: peek_calls.append((conn, wait)) or False)
        with svc._unchecked_changed:
            evicted = svc._make_room()
        assert evicted is s1
        assert peek_calls == []
    finally:
        s1.close()
        s2.close()


def test_private_open_nofollow_excl_on_symlink_when_o_nofollow_is_zero(monkeypatch, tmp_path):
    """When O_NOFOLLOW == 0 (Windows), O_CREAT | O_EXCL on a live or dangling symlink raises FileExistsError
    (matching POSIX open(2)) so _write_new_excl returns False without following or creating the target."""
    from contexto import private as private_mod

    monkeypatch.setattr(private_mod, "O_NOFOLLOW", 0)
    monkeypatch.setattr(os, "link", lambda src, dst: (_ for _ in ()).throw(OSError(errno.EPERM, "no link")))

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"original")
    sym = tmp_path / "sym.json"
    try:
        sym.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks require elevated privileges on this host")

    assert private_mod.write_new(sym, b"overwrite") is False
    assert outside.read_bytes() == b"original"

    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "missing.txt")
    assert private_mod.write_new(dangling, b"created") is False
    assert not (tmp_path / "missing.txt").exists()

    # Non-exclusive open still refuses the symlink as NotRegularFileError.
    with pytest.raises(private_mod.NotRegularFileError):
        private_mod.open_regular(sym)


def test_write_atomic_retries_permission_error_on_simulated_windows(monkeypatch, tmp_path):
    """On Windows, os.replace can transiently raise PermissionError when a reader holds dst open."""
    from contexto import private as private_mod

    monkeypatch.setattr(private_mod, "IS_WINDOWS", True)
    monkeypatch.setattr(private_mod.time, "sleep", lambda _: None)

    real_replace = os.replace
    attempts = 0

    def flaky_replace(src, dst):
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise PermissionError("sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr(private_mod.os, "replace", flaky_replace)
    target = tmp_path / "meta.json"
    target.write_bytes(b'{"v":1}')
    private_mod.write_atomic(target, b'{"v":2}')
    assert attempts == 4
    assert target.read_bytes() == b'{"v":2}'
    assert list(tmp_path.iterdir()) == [target]

    # Exhausting retries propagates PermissionError and removes the temporary file.
    def always_denied(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(private_mod.os, "replace", always_denied)
    with pytest.raises(PermissionError):
        private_mod.write_atomic(target, b'{"v":3}')
    assert target.read_bytes() == b'{"v":2}'
    assert list(tmp_path.iterdir()) == [target]


def test_proxy_stale_pool_errors_include_connection_aborted():
    from contexto import proxy as proxy_mod

    assert ConnectionAbortedError in proxy_mod._STALE_POOL_ERRORS
