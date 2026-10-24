"""``contexto sessions prune`` (D37): selection, dry run, live-session refusal, symlink safety, partial failure."""

import fcntl
import gc
import json
import os
import signal
import stat
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
    code, out, err = run(capsys, "sessions", "prune", "--session", "old", "--yes")
    assert code == 1  # refusing the named session is, and it says so on stderr
    assert err.strip() == "contexto: error: session old: in use by a running Contexto proxy or MCP server"
    assert out == ""
    assert (three / "sessions" / "old" / "pages").is_dir()
    del store
    gc.collect()
    assert run(capsys, "sessions", "prune", "--session", "old", "--yes")[0] == 0
    assert remaining(three) == ["new"]


HOLD = ("import sys, pathlib; from contexto.store import SessionStore;"
        "s = SessionStore(pathlib.Path(sys.argv[1]), sys.argv[2]); print('ready', flush=True); sys.stdin.read()")


def holder(home, sid):
    child = subprocess.Popen([sys.executable, "-c", HOLD, str(home), sid], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"
    return child


def release(child):
    child.stdin.close()
    child.wait(timeout=30)


def test_session_held_by_another_process_is_refused_until_it_exits(home, capsys):
    make(home, "s", 40)
    child = holder(home, "s")
    try:
        age(home / "sessions" / "s", time.time() - 40 * DAY)
        code, _, err = run(capsys, "sessions", "prune", "--session", "s", "--yes")
        assert code == 1 and "in use by a running Contexto proxy or MCP server" in err
        assert remaining(home) == ["s"]
    finally:
        release(child)
    code, out, _ = run(capsys, "sessions", "prune", "--session", "s", "--yes")
    assert code == 0 and "removed s" in out
    assert remaining(home) == []


def test_two_processes_open_one_session_at_once(home, capsys):
    """A proxy and its MCP servers share a session: the lock is shared, so neither waits for the other."""
    make(home, "s", 40)
    first = holder(home, "s")
    try:
        second = holder(home, "s")  # holder() would hang here if the lock were exclusive
        try:
            release(first)
            first = None
            age(home / "sessions" / "s", time.time() - 40 * DAY)
            code, _, err = run(capsys, "sessions", "prune", "--session", "s", "--yes")
            assert code == 1 and "in use" in err  # the second still holds it
        finally:
            release(second)
    finally:
        if first is not None:
            release(first)
    assert run(capsys, "sessions", "prune", "--session", "s", "--yes")[0] == 0


def test_a_child_of_the_holder_does_not_inherit_the_lock(home, capsys):
    """The launcher starts claude; claude (and what it starts) must not keep the session pinned."""
    make(home, "s", 40)
    script = ("import sys, pathlib, subprocess; from contexto.store import SessionStore;"
              "s = SessionStore(pathlib.Path(sys.argv[1]), 's');"
              "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], close_fds=False,"
              " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
              "print(c.pid, flush=True)")
    out = subprocess.run([sys.executable, "-c", script, str(home)], capture_output=True, text=True, timeout=60,
                         check=True).stdout
    grandchild = int(out.strip())
    try:
        os.kill(grandchild, 0)  # still running after its parent, the lock holder, exited
        age(home / "sessions" / "s", time.time() - 40 * DAY)
        code, out, _ = run(capsys, "sessions", "prune", "--session", "s", "--yes")
        assert code == 0 and "removed s" in out
    finally:
        os.kill(grandchild, signal.SIGKILL)


def test_recent_activity_is_refused_without_a_lock(home, capsys):
    make(home, "s", 0)  # closed, but changed just now (a pre-D37 process would hold no lock)
    code, out, _ = run(capsys, "sessions", "prune", "--keep", "0", "--yes")
    assert code == 0 and "skip s: active 0 min ago" in out
    code, _, err = run(capsys, "sessions", "prune", "--session", "s")
    assert code == 1 and "contexto: error: session s: active 0 min ago" in err
    assert remaining(home) == ["s"]


def test_the_in_use_probe_opens_read_write_without_creating():
    # flock emulated over NFS needs a writable descriptor for an exclusive lock (reviewer B).
    assert prune_mod._LOCK_FLAGS & os.O_ACCMODE == os.O_RDWR
    assert not prune_mod._LOCK_FLAGS & os.O_CREAT


def test_dry_run_on_a_session_without_a_lock_file_creates_none(home, capsys):
    path = make(home, "s", 40)
    (path / LIVE_LOCK).unlink()
    age(path, time.time() - 40 * DAY)
    before = tree(home)
    assert "Would remove 1 session" in run(capsys, "sessions", "prune", "--keep", "0")[1]
    assert tree(home) == before


def test_store_resumed_during_a_prune_waits_and_starts_empty_with_a_warning(home, capsys, monkeypatch):
    path = make(home, "s", 40)
    original = prune_mod._delete_contents
    opened = {}

    def delete_while_a_store_opens(fd):
        # remove_session holds the exclusive lock here; a resume of the session starts opening now.
        t = threading.Thread(target=lambda: opened.setdefault("store", SessionStore(home, "s")), daemon=True)
        t.start()
        time.sleep(0.3)
        assert "store" not in opened  # blocked on prune's lock
        opened["thread"] = t
        original(fd)

    monkeypatch.setattr(prune_mod, "_delete_contents", delete_while_a_store_opens)
    code, out, err = run(capsys, "sessions", "prune", "--session", "s", "--yes")
    assert code == 0 and "removed s" in out
    opened["thread"].join(timeout=30)
    store = opened["store"]
    err += capsys.readouterr().err
    assert "contexto: warning: session s was deleted by `contexto sessions prune` while it was being opened" in err
    assert list((path / "pages").iterdir()) == []  # nothing of the pruned session is left
    assert store.page_out([[{"role": "user", "content": "again"}]], 1)  # the new directory is usable
    events = [json.loads(line)["event"] for line in (path / "log.jsonl").read_text().splitlines()]
    assert "pruned_while_opening" in events
    probe = os.open(path / LIVE_LOCK, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):  # and the new store holds the lock on the new file
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)


def test_hold_live_retries_when_the_directory_vanishes_mid_create(tmp_path, monkeypatch):
    # prune's rmdir landing between private_dir's exists() and chmod (reviewer A4).
    import contexto.store as store_mod
    real = store_mod.private_dir
    calls = []

    def flaky(path):
        calls.append(path)
        if len(calls) == 1:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real(path)

    monkeypatch.setattr(store_mod, "private_dir", flaky)
    fd, pruned = store_mod.hold_live(tmp_path / "sessions" / "s")
    os.close(fd)
    assert pruned and len(calls) == 2 and (tmp_path / "sessions" / "s" / LIVE_LOCK).is_file()


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


def victim(tmp_path):
    outside = tmp_path / "victim"
    (outside / "sub").mkdir(parents=True)
    (outside / "precious.txt").write_text("keep me")
    (outside / "sub" / "f").write_text("x")
    return outside


def victim_intact(outside):
    return (sorted(p.name for p in outside.iterdir()) == ["precious.txt", "sub"]
            and (outside / "precious.txt").read_text() == "keep me" and (outside / "sub" / "f").exists())


def swap_for_link(path, outside, moved):
    os.rename(path, moved)
    os.symlink(outside, path, target_is_directory=True)


def test_swap_after_the_check_deletes_nothing_outside(home, tmp_path, capsys, monkeypatch):
    """Reviewer A's reproduction: sessions/<id> swapped for a symlink after the in-use check passed."""
    import contexto.commands.sessions as cmd
    path = make(home, "s1", 40)
    outside = victim(tmp_path)
    original = cmd.check_not_live

    def check_then_swap(*a, **kw):
        original(*a, **kw)
        swap_for_link(path, outside, tmp_path / "moved-s1")

    monkeypatch.setattr(cmd, "check_not_live", check_then_swap)
    code, _, err = run(capsys, "sessions", "prune", "--session", "s1", "--yes")
    assert code == 1 and "could not remove session s1" in err and "symlink" in err
    assert victim_intact(outside)
    assert not (outside / LIVE_LOCK).exists()  # not even a lock file was created there
    assert (tmp_path / "moved-s1" / "pages").is_dir()  # and the moved session was left alone


@pytest.mark.parametrize("hook", ["_check_idle", "disk_usage_fd"])
def test_swap_while_removing_deletes_nothing_outside(home, tmp_path, monkeypatch, hook):
    """The swap lands after the session directory is open: before taking the lock (_check_idle), or
    after the last entry check and before the delete (disk_usage_fd)."""
    path = make(home, "s1", 40)
    outside = victim(tmp_path)
    moved = tmp_path / "moved-s1"
    original = getattr(prune_mod, hook)

    def then_swap(*a, **kw):
        result = original(*a, **kw)
        if not moved.exists():
            swap_for_link(path, outside, moved)
        return result

    monkeypatch.setattr(prune_mod, hook, then_swap)
    with pytest.raises(prune_mod.UnsafeSessionPathError, match="moved or replaced"):
        prune_mod.remove_session(home, "s1", now=time.time())
    assert victim_intact(outside)
    assert not (outside / LIVE_LOCK).exists()
    assert path.is_symlink()  # the swapped-in entry is left in place
    # Swapped before the lock: nothing deleted. Swapped later: only the checked directory was emptied.
    assert (moved / "pages").is_dir() == (hook == "_check_idle")


def test_sessions_dir_swapped_for_a_link_after_listing_is_refused(home, tmp_path, monkeypatch):
    make(home, "s1", 40)
    outside = tmp_path / "victim"
    (outside / "s1" / "sub").mkdir(parents=True)
    (outside / "s1" / "precious.txt").write_text("keep me")
    candidate = prune_mod.select(home, older_than=None, keep=0, session_id=None, now=time.time())[0]
    swap_for_link(home / "sessions", outside, tmp_path / "moved-sessions")
    with pytest.raises(prune_mod.UnsafeSessionPathError, match="symlink"):
        prune_mod.remove_session(home, "s1", now=time.time(), expect=candidate.ident)
    assert (outside / "s1" / "precious.txt").read_text() == "keep me"


def test_entry_replaced_by_another_directory_since_selection_is_refused(home, tmp_path, capsys):
    path = make(home, "s1", 40)
    candidate = prune_mod.select(home, older_than=None, keep=0, session_id=None, now=time.time())[0]
    os.rename(path, tmp_path / "moved-s1")
    os.mkdir(path)
    (path / "other.txt").write_text("not the selected session")
    age(path, time.time() - 40 * DAY)
    with pytest.raises(prune_mod.UnsafeSessionPathError, match="replaced since it was selected"):
        prune_mod.remove_session(home, "s1", now=time.time(), expect=candidate.ident)
    assert (path / "other.txt").exists()


def test_symlinked_live_lock_is_never_followed(home, tmp_path, capsys):
    """Reviewer A2: a dangling link must not create a file outside, a link to a file must not chmod it."""
    out2 = tmp_path / "out2"
    out2.mkdir()
    mine = out2 / "mine.txt"
    mine.write_text("x")
    os.chmod(mine, 0o644)
    for sid, target in (("s2", out2 / "created-by-prune"), ("s3", mine)):
        path = make(home, sid, 40)
        (path / LIVE_LOCK).unlink()
        os.symlink(target, path / LIVE_LOCK)
        age(path, time.time() - 40 * DAY)
    for flags in ([], ["--yes"]):
        code, out, _ = run(capsys, "sessions", "prune", "--older-than", "1d", *flags)
        assert code == 0
        assert "skip s2: live.lock is a symlink" in out and "skip s3: live.lock is a symlink" in out
    assert sorted(p.name for p in out2.iterdir()) == ["mine.txt"]
    assert stat.S_IMODE(mine.stat().st_mode) == 0o644
    assert remaining(home) == ["s2", "s3"]


def test_fifo_live_lock_is_refused_without_hanging(home):
    """Reviewer A3: opening a FIFO for reading blocks until a writer appears; prune must not."""
    path = make(home, "f1", 40)
    (path / LIVE_LOCK).unlink()
    os.mkfifo(path / LIVE_LOCK)
    age(path, time.time() - 40 * DAY)
    for flags in ([], ["--yes"]):
        r = subprocess.run([sys.executable, "-m", "contexto", "sessions", "prune", "--older-than", "1d", *flags],
                           env={**os.environ, "CONTEXTO_HOME": str(home)}, capture_output=True, text=True,
                           timeout=30)
        assert r.returncode == 0 and "skip f1: live.lock is not a regular file" in r.stdout
    assert stat.S_ISFIFO(os.lstat(path / LIVE_LOCK).st_mode)


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


def test_symlinked_sessions_dir_is_refused(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    make(elsewhere, "s", 40)
    home.mkdir()
    (home / "sessions").symlink_to(elsewhere / "sessions", target_is_directory=True)
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    for argv in (["--session", "s", "--yes"], ["--keep", "0", "--yes"], ["--keep", "0"]):
        code, _, err = run(capsys, "sessions", "prune", *argv)
        assert code == 1 and "is a symlink or not a directory" in err
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
    mid = os.stat(three / "sessions" / "mid")

    def flaky(fd):
        if os.path.samestat(os.fstat(fd), mid):
            raise PermissionError(13, "Permission denied", "pages")
        original(fd)

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
    assert "active 0 min ago" in run(capsys, "sessions", "prune", "--session", "mid", "--yes")[2]
    age(three / "sessions" / "mid", time.time() - 40 * DAY)
    assert run(capsys, "sessions", "prune", "--session", "mid", "--yes")[0] == 0  # a rerun finishes it
    assert remaining(three) == []


# --json


def test_json_dry_run_and_delete(three, capsys, monkeypatch):
    store = SessionStore(three, "mid")
    age(store.dir, time.time() - 10 * DAY)
    code, out, _ = run(capsys, "sessions", "prune", "--keep", "1", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["dry_run"] is True
    by_id = {s["id"]: s for s in data["sessions"]}
    assert by_id["old"]["status"] == "would_remove" and by_id["old"]["reason"] is None
    assert by_id["mid"]["status"] == "skipped" and "in use" in by_id["mid"]["reason"]
    assert data["total_bytes"] == by_id["old"]["size_bytes"] == disk_usage(three / "sessions" / "old")
    assert set(by_id["old"]) == {"id", "last_activity", "status", "reason", "size_bytes"}

    original = prune_mod._delete_contents

    def fail_old(fd):
        raise PermissionError(13, "Permission denied", "pages")

    monkeypatch.setattr(prune_mod, "_delete_contents", fail_old)
    code, out, err = run(capsys, "sessions", "prune", "--keep", "1", "--json", "--yes")
    assert code == 1 and "could not remove session old" in err
    data = json.loads(out)
    assert data["dry_run"] is False and data["total_bytes"] == 0
    assert {s["id"]: s["status"] for s in data["sessions"]} == {"old": "failed", "mid": "skipped"}
    monkeypatch.setattr(prune_mod, "_delete_contents", original)
    del store
    gc.collect()
    age(three / "sessions" / "mid", time.time() - 10 * DAY)
    age(three / "sessions" / "old", time.time() - 40 * DAY)
    code, out, _ = run(capsys, "sessions", "prune", "--keep", "1", "--json", "--yes")
    data = json.loads(out)
    assert code == 0 and {s["id"]: s["status"] for s in data["sessions"]} == {"old": "removed", "mid": "removed"}
    assert data["total_bytes"] == sum(s["size_bytes"] for s in data["sessions"])
    assert remaining(three) == ["new"]
