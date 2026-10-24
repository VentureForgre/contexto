"""``contexto sessions prune`` (D37): selection, dry run, live-session refusal, symlink safety, partial failure."""

import fcntl
import gc
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from contexto import cli
from contexto.store import LIVE_LOCK, SessionStore
from contexto.store import prune as prune_mod
from contexto.store.prune import parse_duration
from contexto.store.sessions import disk_usage

DAY = 86400


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    monkeypatch.delenv("CONTEXTO_SESSION", raising=False)
    return home


def age(path, when):
    for p in [path, *path.rglob("*")]:
        os.utime(p, (when, when), follow_symlinks=False)


def make(home, sid, days_idle, text="hello"):
    """A closed session whose files all last changed ``days_idle`` days ago."""
    store = SessionStore(home, sid)
    store.page_out([[{"role": "user", "content": text}, {"role": "assistant", "content": "ok"}]], 1)
    store.log({"event": "test"})
    path = store.dir
    del store
    gc.collect()  # the store's live lock is released when the object goes
    age(path, time.time() - days_idle * DAY)
    return path


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def remaining(home):
    return sorted(p.name for p in (home / "sessions").iterdir())


# arguments


@pytest.mark.parametrize("text,seconds", [("30d", 30 * DAY), ("12h", 43200), ("90m", 5400), ("2w", 14 * DAY),
                                          ("45s", 45)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "30", "d", "0d", "-1d", "1.5d", "30 days", "1y", "1d2h"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize("argv", [["sessions", "prune"], ["sessions", "prune", "--yes"],
                                  ["sessions", "prune", "--older-than", "soon"],
                                  ["sessions", "prune", "--keep", "-1"], ["sessions", "prune", "--keep", "x"]])
def test_prune_usage_errors_exit_2(home, capsys, argv):
    make(home, "s", 40)
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2
    assert remaining(home) == ["s"]


# selection


@pytest.fixture
def three(home):
    for sid, days in (("old", 40), ("mid", 10), ("new", 1)):
        make(home, sid, days)
    return home


def would_remove(capsys, *flags):
    code, out, _ = run(capsys, "sessions", "prune", *flags)
    assert code == 0, out
    rows = out.split("\n  ID", 1)[1].splitlines()[1:] if "Would remove" in out else []
    return [r.split()[0] for r in rows if r.startswith("  ")]


def test_selection(three, capsys):
    assert would_remove(capsys, "--older-than", "30d") == ["old"]
    assert would_remove(capsys, "--older-than", "5d") == ["mid", "old"]
    assert would_remove(capsys, "--keep", "1") == ["mid", "old"]
    assert would_remove(capsys, "--keep", "0") == ["new", "mid", "old"]
    assert would_remove(capsys, "--keep", "5") == []
    assert would_remove(capsys, "--keep", "2", "--older-than", "5d") == ["old"]
    assert would_remove(capsys, "--keep", "1", "--older-than", "20d") == ["old"]
    assert would_remove(capsys, "--session", "mid") == ["mid"]
    assert would_remove(capsys, "--session", "mid", "--older-than", "30d") == []
    assert would_remove(capsys, "--session", "new", "--keep", "1") == []


def test_unknown_or_invalid_session_exits_1(three, capsys):
    for bad in ("ghost", "..", "a/b"):
        code, _, err = run(capsys, "sessions", "prune", "--session", bad, "--yes")
        assert code == 1 and "error" in err
    assert remaining(three) == ["mid", "new", "old"]


def test_no_sessions_is_success(home, capsys):
    code, out, _ = run(capsys, "sessions", "prune", "--keep", "0")
    assert code == 0 and "No sessions to remove." in out


# dry run and delete


def test_dry_run_prints_sizes_and_total_and_changes_nothing(three, capsys):
    before = tree(three)
    code, out, _ = run(capsys, "sessions", "prune", "--older-than", "5d")
    assert code == 0
    sizes = {sid: disk_usage(three / "sessions" / sid) for sid in ("old", "mid")}
    from contexto.commands.sessions import human_size
    assert f"Would remove 2 sessions, {human_size(sum(sizes.values()))}:" in out
    for sid, size in sizes.items():
        assert any(line.split()[0] == sid and line.endswith(human_size(size)) for line in out.splitlines()
                   if line.startswith("  "))
    assert "Dry run: nothing was deleted" in out
    assert tree(three) == before  # not even a lock file is created


def test_yes_deletes_only_the_selection(three, capsys):
    size = disk_usage(three / "sessions" / "old")
    code, out, _ = run(capsys, "sessions", "prune", "--older-than", "30d", "--yes")
    assert code == 0
    assert remaining(three) == ["mid", "new"]
    from contexto.commands.sessions import human_size
    assert f"removed old ({human_size(size)})" in out
    assert f"Removed 1 session, {human_size(size)} freed." in out


def test_list_shows_size_and_total(three, capsys):
    code, out, _ = run(capsys, "sessions", "list")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split()[-1] == "SIZE"
    from contexto.commands.sessions import human_size
    total = sum(disk_usage(three / "sessions" / s) for s in ("old", "mid", "new"))
    assert lines[-1] == f"Total: {human_size(total)} in 3 sessions"
    data = json.loads(run(capsys, "sessions", "list", "--json")[1])
    assert {d["id"]: d["size_bytes"] for d in data} == {s: disk_usage(three / "sessions" / s)
                                                        for s in ("old", "mid", "new")}


def test_disk_usage_counts_links_without_following(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "f").write_bytes(b"x" * 10_000)
    big = tmp_path / "big"
    big.write_bytes(b"y" * 1_000_000)
    (d / "link").symlink_to(big)
    (d / "dirlink").symlink_to(tmp_path)
    assert disk_usage(d) < 100_000


# live sessions


def test_open_store_is_never_removed(three, capsys):
    store = SessionStore(three, "old")  # this process now holds old's live lock
    age(store.dir, time.time() - 40 * DAY)
    code, out, _ = run(capsys, "sessions", "prune", "--older-than", "5d", "--yes")
    assert code == 0  # skipping part of a broad selection is not a failure
    assert "skip old: in use by a running Contexto proxy or MCP server" in out
    assert remaining(three) == ["new", "old"]
    code, out, _ = run(capsys, "sessions", "prune", "--session", "old", "--yes")
    assert code == 1 and "skip old: in use" in out  # refusing the named session is
    assert (three / "sessions" / "old" / "pages").is_dir()
    del store
    gc.collect()
    assert run(capsys, "sessions", "prune", "--session", "old", "--yes")[0] == 0
    assert remaining(three) == ["new"]


def test_session_held_by_another_process_is_refused_until_it_exits(home, capsys):
    make(home, "s", 40)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys, pathlib; from contexto.store import SessionStore;"
         "s = SessionStore(pathlib.Path(sys.argv[1]), 's'); print('ready', flush=True); sys.stdin.read()",
         str(home)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        age(home / "sessions" / "s", time.time() - 40 * DAY)
        code, out, _ = run(capsys, "sessions", "prune", "--session", "s", "--yes")
        assert code == 1 and "in use by a running Contexto proxy or MCP server" in out
        assert remaining(home) == ["s"]
    finally:
        child.stdin.close()
        child.wait(timeout=30)
    code, out, _ = run(capsys, "sessions", "prune", "--session", "s", "--yes")
    assert code == 0 and "removed s" in out
    assert remaining(home) == []


def test_recent_activity_is_refused_without_a_lock(home, capsys):
    make(home, "s", 0)  # closed, but changed just now (a pre-D37 process would hold no lock)
    code, out, _ = run(capsys, "sessions", "prune", "--keep", "0", "--yes")
    assert code == 0 and "skip s: active 0 min ago" in out
    code, out, _ = run(capsys, "sessions", "prune", "--session", "s")
    assert code == 1
    assert remaining(home) == ["s"]


def test_store_opening_during_a_prune_waits_and_gets_a_fresh_directory(home):
    path = make(home, "s", 40)
    fd = os.open(path / LIVE_LOCK, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)  # what remove_session holds while it deletes
    opened = {}
    t = threading.Thread(target=lambda: opened.setdefault("store", SessionStore(home, "s")))
    t.start()
    time.sleep(0.3)
    assert "store" not in opened  # blocked on prune's lock
    prune_mod._delete_contents(path)
    os.close(fd)
    t.join(timeout=30)
    store = opened["store"]
    lock = path / LIVE_LOCK
    assert lock.exists() and list((path / "pages").iterdir()) == []  # nothing of the pruned session is left
    assert store.page_out([[{"role": "user", "content": "again"}]], 1)  # the new directory is usable
    probe = os.open(lock, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):  # and the new store holds the lock on the new file
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)


# symlink safety


def test_symlinked_session_is_never_followed(home, tmp_path, capsys):
    make(home, "real", 40)
    outside = tmp_path / "outside"
    (outside / "pages").mkdir(parents=True)
    (outside / "keep.txt").write_text("precious")
    (home / "sessions" / "evil").symlink_to(outside, target_is_directory=True)
    code, _, err = run(capsys, "sessions", "prune", "--session", "evil", "--yes")
    assert code == 1 and "symlink" in err
    code, _, _ = run(capsys, "sessions", "prune", "--keep", "0", "--yes")
    assert code == 0
    assert (outside / "keep.txt").read_text() == "precious"
    assert remaining(home) == ["evil"]


def test_links_inside_a_session_are_removed_as_links(home, tmp_path, capsys):
    path = make(home, "s", 40)
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep.txt").write_text("precious")
    (path / "filelink").symlink_to(outside / "sub" / "keep.txt")
    (path / "pages" / "dirlink").symlink_to(outside / "sub", target_is_directory=True)
    (path / "dirlink").symlink_to(outside, target_is_directory=True)
    age(path, time.time() - 40 * DAY)
    code, _, _ = run(capsys, "sessions", "prune", "--session", "s", "--yes")
    assert code == 0 and remaining(home) == []
    assert (outside / "sub" / "keep.txt").read_text() == "precious"


def test_sessions_dir_resolving_outside_home_is_refused(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    make(elsewhere, "s", 40)
    home.mkdir()
    (home / "sessions").symlink_to(elsewhere / "sessions", target_is_directory=True)
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    code, _, err = run(capsys, "sessions", "prune", "--session", "s", "--yes")
    assert code == 1 and "outside" in err
    code, out, _ = run(capsys, "sessions", "prune", "--keep", "0", "--yes")
    assert "skip s:" in out and "outside" in out
    assert (elsewhere / "sessions" / "s" / "pages").is_dir()


def test_symlinked_home_is_fine(tmp_path, monkeypatch, capsys):
    real = tmp_path / "real"
    make(real, "s", 40)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "link"))
    code, _, _ = run(capsys, "sessions", "prune", "--session", "s", "--yes")
    assert code == 0 and not (real / "sessions" / "s").exists()


# partial failure


def test_one_failure_does_not_stop_the_rest(three, capsys, monkeypatch):
    original = prune_mod._delete_contents

    def flaky(real):
        if real.name == "mid":
            raise PermissionError(13, "Permission denied", str(real))
        original(real)

    monkeypatch.setattr(prune_mod, "_delete_contents", flaky)
    code, out, err = run(capsys, "sessions", "prune", "--keep", "0", "--yes")
    assert code == 1
    assert "could not remove session mid" in err and "Permission denied" in err
    assert "removed old" in out and "removed new" in out and "Removed 2 sessions" in out and "1 failed." in out
    assert remaining(three) == ["mid"]


def test_real_permission_failure_leaves_the_rest_removed(three, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    pages = three / "sessions" / "mid" / "pages"
    os.chmod(pages, 0o500)
    try:
        code, _, err = run(capsys, "sessions", "prune", "--keep", "0", "--yes")
    finally:
        os.chmod(pages, 0o700)
    assert code == 1 and "could not remove session mid" in err
    assert remaining(three) == ["mid"]
    # The partial delete changed the directory, so it counts as active until the grace period passes.
    assert "active 0 min ago" in run(capsys, "sessions", "prune", "--session", "mid", "--yes")[1]
    age(three / "sessions" / "mid", time.time() - 40 * DAY)
    assert run(capsys, "sessions", "prune", "--session", "mid", "--yes")[0] == 0  # a rerun finishes it
    assert remaining(three) == []
