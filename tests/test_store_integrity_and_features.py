"""Tests for storage integrity, concurrency, caching, pruning, gzip pages, and secret redaction."""

from __future__ import annotations

import fcntl
import gzip
import io
import json
import os
import threading
import time

import pytest

from contexto import mcp, private
from contexto.store import MetaCorruptError, SessionStore
from contexto.store import project as project_module
from contexto.store.catalog import PageCatalog
from contexto.store.prune import RETENTION_KEEP_ENV, RETENTION_MAX_AGE_ENV, auto_prune
from contexto.store.sessions import session_info


def make_turn(text: str, reply: str = "ok") -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": text}]},
        {"role": "assistant", "content": [{"type": "text", "text": reply}]},
    ]


# 1. CRITICAL meta.json wipe protection


@pytest.mark.parametrize("corrupt", [
    b"{truncated json",
    b"[]",
    b'"just a string"',
    b"\xff\xfe\xfd",
])
def test_corrupt_meta_json_is_never_silently_overwritten(tmp_path, corrupt):
    store = SessionStore(tmp_path, "s1")
    workdir = tmp_path / "repo"
    (workdir / ".git").mkdir(parents=True)
    store.set_workdir(workdir)
    store.set_project_memory(False)
    store.add_main_records(["rec_" + "a" * 24])
    meta_path = store.dir / "meta.json"
    meta_path.write_bytes(corrupt)

    with pytest.raises(MetaCorruptError):
        store.set_window(100_000, 2.0)
    assert meta_path.read_bytes() == corrupt

    with pytest.raises(MetaCorruptError):
        store.set_turn_room("1", 5_000)
    assert meta_path.read_bytes() == corrupt

    with pytest.raises(MetaCorruptError):
        store.set_project_memory(True)
    assert meta_path.read_bytes() == corrupt

    # Read-only project() must not re-enable project memory on corrupt meta.json.
    assert store.project() is None
    assert store.has_project() is False

    # MCP tool call returns an actionable tool error and does not wipe meta.json.
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    reply = server.handle_line(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "contexto_state", "arguments": {}},
    }))["result"]
    assert reply["isError"] is True
    assert "meta.json" in reply["content"][0]["text"]
    assert meta_path.read_bytes() == corrupt


def test_missing_meta_json_initializes_cleanly(tmp_path):
    store = SessionStore(tmp_path, "s1")
    meta_path = store.dir / "meta.json"
    assert not meta_path.exists()
    store.set_window(50_000, 2.5)
    assert store.window() == (50_000, 2.5)


# 2. Concurrency & durability


def test_log_jsonl_concurrent_appends_stay_valid_jsonl(tmp_path):
    store = SessionStore(tmp_path, "s1")
    errors: list[BaseException] = []

    def worker(tid: int):
        own = SessionStore(tmp_path, "s1")
        try:
            for i in range(40):
                own.log({"event": "concurrent_test", "tid": tid, "i": i, "pad": "x" * 200})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    lines = (store.dir / "log.jsonl").read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]
    mine = [e for e in parsed if e.get("event") == "concurrent_test"]
    assert len(mine) == 8 * 40


def test_set_retained_concurrent_writers_never_leave_stale_stamp(tmp_path):
    store = SessionStore(tmp_path, "s1")
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def writer(tid: int):
        own = SessionStore(tmp_path, "s1")
        barrier.wait()
        try:
            for i in range(15):
                own.set_retained(i, 0, [{"turn": i, "message_index": 0, "role": "user", "text": f"t{tid}-{i}"}])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    meta = json.loads((store.dir / "meta.json").read_text(encoding="utf-8"))
    retained = store.retained()
    assert retained is not None
    assert retained[2] == meta["request_seq"] == 6 * 15


def test_set_epoch_concurrent_writers_stay_valid(tmp_path):
    from test_epoch_record import RECORD

    store = SessionStore(tmp_path, "s1")
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def writer(tid: int):
        own = SessionStore(tmp_path, "s1")
        barrier.wait()
        try:
            for i in range(10):
                own.set_epoch({**RECORD, "epoch": tid * 100 + i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rec = store.epoch_record()
    assert rec is not None and rec["version"] == 1


def test_atomic_writes_fsync_parent_directory(tmp_path, monkeypatch):
    synced: list[str] = []
    real_fsync_dir = private.fsync_dir

    def tracked(path):
        synced.append(str(path))
        return real_fsync_dir(path)

    monkeypatch.setattr(private, "fsync_dir", tracked)
    target_a = tmp_path / "a.json"
    target_b = tmp_path / "b.json"
    private.write_atomic(target_a, b'{"ok":true}')
    assert private.write_new(target_b, b'{"new":true}') is True
    assert synced == [str(tmp_path), str(tmp_path)]


def test_charge_turn_room_does_not_hold_meta_lock_during_callback(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store.set_turn_room("1", 1_000)
    probe_results: list[bool] = []

    def charge(remaining):
        # Verify meta.lock is NOT held while response formatting runs.
        fd = os.open(store.dir / "meta.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            probe_results.append(True)
        except BlockingIOError:
            probe_results.append(False)
        finally:
            os.close(fd)
        # Another store instance can update metadata without blocking!
        SessionStore(tmp_path, "s1").set_window(120_000, 2.5)
        return min(remaining, 100)

    summary = store.charge_turn_room(charge)
    assert probe_results == [True]
    assert summary == {"limited_by": "main", "side_rooms": 0}
    assert store.turn_room() == ("1", 1_000, 100)
    assert store.window() == (120_000, 2.5)


# 3. project._CACHE cap and PageCatalog follow_symlinks=False


def test_project_cache_enforces_session_and_byte_caps(tmp_path, monkeypatch):
    project_module.forget_cache()
    monkeypatch.setattr(project_module, "CACHE_SESSIONS", 5)
    monkeypatch.setattr(project_module, "CACHE_MAX_BYTES", 2_048)

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    home = tmp_path / "home"
    for i in range(10):
        s = SessionStore(home, f"s{i:02d}")
        s.set_workdir(root)
        s.release()

    listing = project_module.project_sessions(home, root)
    assert len(listing.sessions) == 10
    assert project_module._CACHE_SESSIONS <= 5
    assert project_module._CACHE_BYTES <= 2_048
    project_module.forget_cache()


def test_page_catalog_listing_ignores_symlinks(tmp_path):
    store = SessionStore(tmp_path, "s1")
    [good] = store.page_out([make_turn("real page content")], 0)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(store.get_page(good)), encoding="utf-8")
    link = store.pages_dir / "pg_0123456789abcdef.json"
    link.symlink_to(outside)

    catalog = PageCatalog(store.pages_dir, store.log)
    with catalog._lock:
        listed = catalog._listing()
    assert f"{good}.json" in listed
    assert link.name not in listed
    assert store.page_ids() == [good]


# 4. Optional automatic session pruning/retention


def test_auto_prune_disabled_by_default_and_works_when_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(RETENTION_MAX_AGE_ENV, raising=False)
    monkeypatch.delenv(RETENTION_KEEP_ENV, raising=False)
    home = tmp_path / "home"
    old_time = time.time() - 10 * 86_400

    for sid in ("old1", "old2"):
        s = SessionStore(home, sid)
        s.page_out([make_turn(f"page {sid}")], 0)
        s.release()
        for p in [s.dir, *s.dir.rglob("*")]:
            os.utime(p, (old_time, old_time))

    # Default: opening a new session does NOT prune anything.
    cur = SessionStore(home, "cur1")
    assert (home / "sessions" / "old1").is_dir()
    assert (home / "sessions" / "old2").is_dir()
    cur.release()
    for p in [cur.dir, *cur.dir.rglob("*")]:
        os.utime(p, (old_time + 86_400, old_time + 86_400))

    # Opt-in via env vars: keeps newest 2 (plus current live session) and prunes old1/old2 older than 5d.
    monkeypatch.setenv(RETENTION_MAX_AGE_ENV, "5d")
    monkeypatch.setenv(RETENTION_KEEP_ENV, "1")
    cur2 = SessionStore(home, "cur2")
    assert not (home / "sessions" / "old1").exists()
    assert not (home / "sessions" / "old2").exists()
    assert (home / "sessions" / "cur2").is_dir()
    cur2.release()

    # Direct auto_prune call returns empty when no bounds given.
    assert auto_prune(home) == []


# 5. Gzip support for archived pages


def test_gzip_pages_round_trip_search_and_archive(tmp_path):
    store = SessionStore(tmp_path, "s1", compress_pages=True)
    [p1, p2] = store.page_out([make_turn("alpha rocket launch"), make_turn("beta submarine dive")], 0)
    assert (store.pages_dir / f"{p1}.json.gz").is_file()
    assert not (store.pages_dir / f"{p1}.json").exists()
    # Verify actual gzip format on disk
    raw = gzip.decompress((store.pages_dir / f"{p1}.json.gz").read_bytes())
    assert json.loads(raw)["id"] == p1

    # Reopen default store (compress_pages=False): reads .json.gz transparently!
    reopened = SessionStore(tmp_path, "s1")
    assert reopened.get_page(p1)["id"] == p1
    assert reopened.search("rocket") == [p1]
    assert reopened.search("submarine") == [p2]
    assert reopened.page_ids() == [p2, p1]
    assert session_info(tmp_path, "s1").pages == 2
    assert p1 in reopened.index_text(4000)

    # Quarantine works on .json.gz pages too
    qpath = reopened.quarantine_page(p1)
    assert qpath.name.endswith(".json.gz")
    assert reopened.get_page(p1) is None
    assert reopened.search("rocket") == []

    # archive_pages compresses older .json pages while leaving recent ones uncompressed
    store2 = SessionStore(tmp_path, "s2", compress_pages=False)
    ids = store2.page_out([make_turn(f"turn content {i}") for i in range(4)], 0)
    archived = store2.archive_pages(min_age_turns=2)
    assert archived == 2
    assert (store2.pages_dir / f"{ids[0]}.json.gz").is_file() and not (store2.pages_dir / f"{ids[0]}.json").exists()
    assert (store2.pages_dir / f"{ids[1]}.json.gz").is_file() and not (store2.pages_dir / f"{ids[1]}.json").exists()
    assert (store2.pages_dir / f"{ids[2]}.json").is_file()
    assert (store2.pages_dir / f"{ids[3]}.json").is_file()
    assert store2.search("turn content 0") == [ids[0]]


def test_corrupt_gzip_page_is_treated_as_damaged(tmp_path):
    store = SessionStore(tmp_path, "s1", compress_pages=True)
    [pid] = store.page_out([make_turn("hello world")], 0)
    (store.pages_dir / f"{pid}.json.gz").write_bytes(b"not valid gzip bytes")
    reopened = SessionStore(tmp_path, "s1")
    assert reopened.search("hello") == []
    assert reopened.page_ids() == []


# 6. Optional secret-pattern redaction


def test_secret_redaction_is_opt_in_and_redacts_before_persisting(tmp_path):
    secret_turn = make_turn(
        "Use key sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345 and "
        "token ghp_1234567890abcdefghij1234567890abcd and "
        "aws AKIAIOSFODNN7EXAMPLE and "
        "postgres://admin:supersecretpass@db.internal:5432/prod"
    )
    # Disabled by default
    plain = SessionStore(tmp_path, "plain")
    [p_plain] = plain.page_out([secret_turn], 0)
    plain_raw = (plain.pages_dir / f"{p_plain}.json").read_text(encoding="utf-8")
    assert "sk-ant-api03-" in plain_raw and "supersecretpass" in plain_raw

    # Enabled via opt-in parameter
    safe = SessionStore(tmp_path, "safe", redact_secrets=True)
    [p_safe] = safe.page_out([secret_turn], 0)
    safe_raw = (safe.pages_dir / f"{p_safe}.json").read_text(encoding="utf-8")
    for leaked in ("sk-ant-api03-", "ghp_1234567890", "AKIAIOSFODNN7EXAMPLE", "supersecretpass"):
        assert leaked not in safe_raw
    assert "[REDACTED]" in safe_raw
    assert "postgres://admin:[REDACTED]@db.internal:5432/prod" in safe_raw


# 7. Review regression tests: archive safety, charge_turn_room concurrency, stats gzip support


def test_archive_pages_refuses_to_unlink_json_when_existing_gz_differs(tmp_path):
    store = SessionStore(tmp_path, "s-archive-safe")
    [p0] = store.page_out([make_turn("turn zero critical payload")], 0)
    [p1] = store.page_out([make_turn("turn one newest")], 1)
    src = store.pages_dir / f"{p0}.json"
    dst = store.pages_dir / f"{p0}.json.gz"
    dst.write_bytes(b"corrupt existing gzip bytes")

    archived = store.archive_pages(min_age_turns=1)
    assert archived == 0
    assert src.exists()
    assert store.get_page(p0)["turn"] == 0
    assert p1 != p0


def test_charge_turn_room_does_not_undercharge_main_when_tighter_side_room_arrives(tmp_path):
    store = SessionStore(tmp_path, "s-charge-side")
    store.set_turn_room("t1", 1000)

    def callback(remaining: int | None) -> int:
        assert remaining == 1000
        # A tighter side room arrives while formatting the response outside meta.lock.
        store.set_side_room("side-a", 100)
        return 900

    summary = store.charge_turn_room(callback)
    # Summary reflects the snapshot that bounded the call (main, 0 side rooms).
    assert summary == {"limited_by": "main", "side_rooms": 0}
    # Main must be charged the full 900 tokens, not clamped to 100.
    assert store.turn_room() == ("t1", 1000, 900)


def test_stats_counts_and_totals_gzipped_pages(tmp_path):
    from contexto.stats import count_pages, page_totals, session_stats

    store = SessionStore(tmp_path, "s-stats-gz", compress_pages=True)
    [p0, p1] = store.page_out([make_turn("first compressed turn"), make_turn("second compressed turn")], 0)
    assert (store.pages_dir / f"{p0}.json.gz").is_file()
    assert (store.pages_dir / f"{p1}.json.gz").is_file()

    assert count_pages(store.pages_dir) == 2
    totals = page_totals(store.pages_dir)
    assert totals["count"] == 2
    assert totals["unreadable"] == 0
    assert totals["tokens_estimate"] > 0
    assert totals["file_bytes"] > 0

    st = session_stats(tmp_path, "s-stats-gz")
    assert st["pages"]["count"] == 2
    assert st["pages"]["tokens_estimate"] == totals["tokens_estimate"]


def test_corrupt_deflate_gzip_handled_cleanly_in_archive_and_stats(tmp_path):
    from contexto.stats import page_totals, session_stats

    store = SessionStore(tmp_path, "s-bad-deflate")
    [p0, p1] = store.page_out([make_turn("turn zero"), make_turn("turn one")], 0)
    # Valid 10-byte gzip header followed by invalid deflate stream triggers zlib.error (not OSError/ValueError).
    bad_gz = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\xff\xff\xff\xff"
    (store.pages_dir / f"{p0}.json.gz").write_bytes(bad_gz)

    # archive_pages must skip p0 without crashing or unlinking p0.json.
    assert store.archive_pages(min_age_turns=1) == 0
    assert (store.pages_dir / f"{p0}.json").is_file()

    # Now remove p0.json so stats reads the bad-deflate .json.gz directly: must count unreadable, never crash.
    (store.pages_dir / f"{p0}.json").unlink()
    totals = page_totals(store.pages_dir)
    assert totals["count"] == 2
    assert totals["unreadable"] == 1
    assert totals["tokens_estimate"] > 0
    st = session_stats(tmp_path, "s-bad-deflate")
    assert st["pages"]["unreadable"] == 1
    assert p1 != p0
