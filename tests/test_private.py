import errno
import os
import stat
import subprocess
import sys

import pytest

from contexto.store import KeyFileError, SessionStore


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def permissive_umask():
    old = os.umask(0)
    yield
    os.umask(old)


def test_session_dirs_0700_and_files_0600_under_a_permissive_umask(tmp_path, permissive_umask):
    home = tmp_path / "home"
    store = SessionStore(home, "s1")
    store.page_out([[{"role": "user", "content": "secret tool output"}]], 1)
    store.log({"event": "x"})
    for d in (home, home / "sessions", store.dir, store.pages_dir):
        assert mode(d) == 0o700, d
    files = [store.dir / "page.key", store.dir / "log.jsonl", *store.pages_dir.iterdir()]
    assert len(files) == 3
    for f in files:
        assert mode(f) == 0o600, f


def test_an_existing_home_keeps_its_mode_and_sessions_dir_is_narrowed(tmp_path):
    # D55: CONTEXTO_HOME is the user's choice (it may be $HOME or a shared directory), so an existing one
    # is not chmodded; sessions/ and everything under it is Contexto's own.
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    os.chmod(home, 0o755)
    os.chmod(home / "sessions", 0o777)
    SessionStore(home, "s1")
    assert mode(home) == 0o755 and mode(home / "sessions") == 0o700


def test_a_home_that_is_a_file_is_refused_before_any_chmod(tmp_path):
    home = tmp_path / "home"
    home.write_text("x")
    os.chmod(home, 0o644)
    with pytest.raises(NotADirectoryError):
        SessionStore(home, "s1")
    assert mode(home) == 0o644


def test_existing_wide_session_dirs_are_narrowed(tmp_path):
    session = tmp_path / "sessions" / "s1"
    (session / "pages").mkdir(parents=True)
    os.chmod(session, 0o755)
    os.chmod(session / "pages", 0o777)
    SessionStore(tmp_path, "s1")
    assert mode(session) == 0o700 and mode(session / "pages") == 0o700


@pytest.mark.parametrize("content", ["", "abcd", "zz" * 32, "00" * 31])
def test_a_bad_key_file_is_refused(tmp_path, content):
    session = tmp_path / "sessions" / "s1"
    session.mkdir(parents=True)
    (session / "page.key").write_text(content)
    with pytest.raises(KeyFileError):
        SessionStore(tmp_path, "s1")


RACER = """
import sys, time
from pathlib import Path
from contexto.store import SessionStore
home, go = Path(sys.argv[1]), Path(sys.argv[2])
while not go.exists():
    time.sleep(0.0005)
store = SessionStore(home, "race")
print((store.dir / "page.key").read_text())
"""


def test_concurrent_first_use_agrees_on_one_complete_key(tmp_path):
    go = tmp_path / "go"
    procs = [subprocess.Popen([sys.executable, "-c", RACER, str(tmp_path / "home"), str(go)],
                              stdout=subprocess.PIPE, text=True) for _ in range(4)]
    go.touch()
    keys = {p.communicate(timeout=60)[0].strip() for p in procs}
    assert all(p.returncode == 0 for p in procs)
    assert len(keys) == 1 and len(keys.pop()) == 64


def test_private_dir_refuses_symlinks_without_chmodding_target(tmp_path):
    from contexto.private import private_dir

    target = tmp_path / "target"
    target.mkdir()
    os.chmod(target, 0o755)
    link = tmp_path / "sessions"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(NotADirectoryError):
        private_dir(link, narrow=True)
    assert mode(target) == 0o755

    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises(NotADirectoryError):
        private_dir(dangling, narrow=True)
    with pytest.raises(NotADirectoryError):
        private_dir(dangling, narrow=False)
    assert not (tmp_path / "nowhere").exists()

    # User-chosen home (narrow=False) may be a symlink to an existing directory and is not chmodded.
    assert private_dir(link, narrow=False) == link
    assert mode(target) == 0o755


@pytest.mark.parametrize("err", [errno.EPERM, errno.EOPNOTSUPP], ids=["EPERM", "EOPNOTSUPP"])
def test_write_new_falls_back_safely_when_hard_link_is_unsupported(tmp_path, monkeypatch, err):
    from contexto.private import write_new

    def no_link(src, dst):
        raise OSError(err, "hard links unsupported", str(src), None, str(dst))

    monkeypatch.setattr(os, "link", no_link)
    target = tmp_path / "page.json"
    assert write_new(target, b'{"ok":1}') is True
    assert target.read_bytes() == b'{"ok":1}' and mode(target) == 0o600
    assert list(tmp_path.iterdir()) == [target]  # temp file cleaned up

    # Existing file is never overwritten.
    assert write_new(target, b'{"ok":2}') is False
    assert target.read_bytes() == b'{"ok":1}'

    # Existing or dangling symlink is never followed or replaced.
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"original")
    sym = tmp_path / "sym.json"
    sym.symlink_to(outside)
    assert write_new(sym, b"overwrite") is False
    assert outside.read_bytes() == b"original"

    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "missing.txt")
    assert write_new(dangling, b"created") is False
    assert not (tmp_path / "missing.txt").exists()


def test_write_new_fallback_cleans_up_partial_file_on_write_failure(tmp_path, monkeypatch):
    from contexto import private as private_mod

    monkeypatch.setattr(os, "link", lambda src, dst: (_ for _ in ()).throw(OSError(errno.EPERM, "no link")))
    real_write_all = private_mod.write_all
    calls = 0

    def fail_second(fd, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.ENOSPC, "disk full")
        real_write_all(fd, data)

    monkeypatch.setattr(private_mod, "write_all", fail_second)
    target = tmp_path / "partial.json"
    with pytest.raises(OSError):
        private_mod.write_new(target, b"hello")
    assert not target.exists() and list(tmp_path.iterdir()) == []


def test_read_regular_max_bytes_exact_boundary(tmp_path):
    from contexto.private import read_regular

    f = tmp_path / "exact.bin"
    f.write_bytes(b"0123456789")
    assert read_regular(f, max_bytes=10) == b"0123456789"
    with pytest.raises(ValueError):
        read_regular(f, max_bytes=9)
