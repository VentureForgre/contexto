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


def test_existing_wide_home_and_sessions_dirs_are_narrowed_on_first_use(tmp_path):
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    os.chmod(home, 0o755)
    os.chmod(home / "sessions", 0o777)
    SessionStore(home, "s1")
    assert mode(home) == 0o700 and mode(home / "sessions") == 0o700


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
