"""The live benchmark killed with SIGKILL: attempts stay apart, and no process it started outlives it.

The runner runs as ``python -m contexto bench`` in a child process, against the stub upstream (in this
test process, on 127.0.0.1) and the stub ``claude``, with a fake key and a temp HOME; nothing reaches a
model API. Processes are found and checked by the PIDs this test records (the runner's process tree,
read with ``ps`` just before the kill), never by name, and only the runner is signalled.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import bench_stubs
from contexto.bench import meter
from contexto.bench import script as bench_script

SEED = 3
BOUND = 30_000
TURN_KBYTES = 2
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and process groups")


@pytest.fixture(scope="module")
def script():
    return bench_script.build(SEED, "v2", bound=BOUND, turn_bytes=TURN_KBYTES * 1_000)


# Run as the runner instead of "python -m contexto": of the nth request record only the first ``cut``
# bytes are written (half the line when ``cut`` is 0), then the runner creates the marker file and waits
# there to be killed.
TEAR = """
import json, runpy, sys, time
from contexto.bench import meter
marker, nth, cut = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
sys.argv = ["contexto", *sys.argv[4:]]
real, seen = meter.Ledger.settle, [0]
def settle(self, reserved, record, kept=0):
    if record is not None:
        seen[0] += 1
        if seen[0] == nth:
            line = (json.dumps(record) + "\\n").encode()
            with self.path.open("ab") as f:
                f.write(line[: cut or len(line) // 2])
                f.flush()
            open(marker, "w").close()
            time.sleep(600)
    return real(self, reserved, record, kept)
meter.Ledger.settle = settle
runpy.run_module("contexto", run_name="__main__")
"""


def start_runner(tmp: Path, url: str, arms: str, out: Path, *, prefix: list[str] | None = None,
                 **extra_env: str) -> subprocess.Popen:
    home = tmp / bench_stubs.HOME_NAME
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE", "CONTEXTO_"))}
    env.update({"HOME": str(home), "ANTHROPIC_API_KEY": bench_stubs.FAKE_KEY, "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "PATH": str(bench_stubs.install_stub_claude(tmp / "bin")) + os.pathsep + os.environ["PATH"],
                **extra_env})
    command = [sys.executable, *(prefix or ["-m", "contexto"]), "bench", "--model", "claude-haiku-5-5",
               "--seed", str(SEED), "--bound", str(BOUND), "--turn-kbytes", str(TURN_KBYTES), "--arms", arms,
               "--upstream", url, "--out", str(out), "--work", str(tmp / "work"), "--budget-tokens", "2000000000"]
    return subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)


def lines(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass  # a line torn by the kill
    return out


def wait_for(condition, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def process_tree(root: int) -> set[int]:
    """``root`` and every process below it, by PID, from one ``ps`` listing."""
    listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
    children: dict[int, list[int]] = {}
    for row in listing.splitlines():
        pid, ppid = (int(x) for x in row.split())
        children.setdefault(ppid, []).append(pid)
    tree, todo = set(), [root]
    while todo:
        pid = todo.pop()
        tree.add(pid)
        todo += children.get(pid, [])
    return tree


def alive(pids: set[int]) -> set[int]:
    """The PIDs of ``pids`` still running (a zombie has finished)."""
    if not pids:
        return set()
    listing = subprocess.run(["ps", "-o", "pid=,stat=", "-p", ",".join(map(str, sorted(pids)))],
                             capture_output=True, text=True).stdout
    return {int(row.split()[0]) for row in listing.splitlines() if row.split() and not row.split()[1].startswith("Z")}


def test_kills_at_many_points_leave_every_request_with_exactly_one_attempt(tmp_path, script):
    stub = bench_stubs.StubModel([script], key=bench_stubs.FAKE_KEY)
    url = stub.start()
    out = tmp_path / "out"
    requests = out / "requests.jsonl"
    try:
        for kill_after in (1, 4, 9, 17, 26, 40, 3):  # new request lines before the kill, a different point each time
            before = len(lines(requests))
            runner = start_runner(tmp_path, url, "full,plain", out)
            target = before + kill_after
            reached = wait_for(lambda r=runner, n=target: len(lines(requests)) >= n or r.poll() is not None)
            tree = process_tree(runner.pid) - {runner.pid}
            runner.kill()
            runner.wait(timeout=30)
            assert reached, "the run made no progress"
            assert wait_for(lambda t=tree: not alive(t), timeout=5.0), "a child outlived the kill"
        final = start_runner(tmp_path, url, "full,plain", out)
        _, err = final.communicate(timeout=300)
        assert final.returncode == 0, err.decode(errors="replace")[-2_000:]
    finally:
        stub.stop()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    units = {(u["arm"], u["seed"], u["repeat"]): u for u in manifest["units"]}
    assert set(units) == {("full", SEED, 0), ("plain", SEED, 0)}
    assert all(u["status"] == "complete" for u in units.values())
    records = lines(requests)
    by_attempt: dict[tuple, list[dict]] = {}
    for record in records:
        by_attempt.setdefault((record["arm"], record["seed"], record["repeat"], record["attempt"]), []).append(record)
    for key, unit in units.items():
        numbers = [a["attempt"] for a in unit["attempts"]]
        assert len(numbers) == len(set(numbers)) and numbers == sorted(numbers), unit["attempts"]
        assert unit["attempt"] == numbers[-1] and unit["attempts"][-1]["status"] == "complete"
        for attempt in unit["attempts"][:-1]:
            assert attempt["status"] == "stopped" and attempt["stop"] == "interrupted"
        # Every record of this unit belongs to exactly one attempt the manifest lists, with its counts.
        recorded = {k[3] for k in by_attempt if k[:3] == key}
        assert recorded <= set(numbers), (recorded, numbers)
        for attempt in unit["attempts"]:
            mine = by_attempt.get((*key, attempt["attempt"]), [])
            assert attempt["requests"] == len(mine), (key, attempt)
            assert attempt["tokens"] == sum(r["uncached"] + r["cache_write"] + r["cache_read"] + r["output"]
                                            for r in mine), (key, attempt)
        assert unit["requests"] == len(by_attempt[(*key, unit["attempt"])])
    assert sum(len(u["attempts"]) for u in units.values()) >= 4  # the kills landed in both arms' units
    probes = lines(out / "probes.jsonl")
    assert len(probes) == 2 * len(script.probes) == len({(p["arm"], p["id"]) for p in probes})


def test_kills_by_pid_with_requests_in_flight_leave_every_billed_request_counted_once(tmp_path, script):
    """#286 Reviewer A low 2 at 99ee7b07: a request in flight when the runner is killed was billed and
    counted nowhere (4 of 153 over 6 kills). The stub holds each request 0.25 s, and each kill lands
    while the journal has a request with no record yet, the first before the directory's first record.
    After the run is resumed to the end: every request the stub received has a whole record, a journal
    line saying it got no response, or an entry in flight; no request is in two of them; and
    ``spent_tokens`` is the records plus the reservations of the entries in flight."""
    stub = bench_stubs.StubModel([script], key=bench_stubs.FAKE_KEY, delay_s={"full": 0.25, "plain": 0.25})
    url = stub.start()
    out = tmp_path / "out"
    requests, journal_path = out / "requests.jsonl", out / meter.JOURNAL_NAME

    def opened() -> int:
        return sum(1 for e in lines(journal_path) if not e.get("unanswered"))

    def pending() -> bool:  # a request with a journal line, no record and no second line
        return bool(meter.load_journal(out).in_flight) if journal_path.exists() else False

    kills = (1, 3, 8, 2, 13, 5)
    try:
        for kill_after in kills:  # new journal lines before the kill, a different point each time
            target = opened() + kill_after
            runner = start_runner(tmp_path, url, "full,plain", out)
            reached = wait_for(lambda r=runner, n=target: (opened() >= n and pending()) or r.poll() is not None)
            if kill_after == kills[0]:
                assert not lines(requests), "the first kill comes before the directory's first record"
            runner.kill()
            runner.wait(timeout=30)
            assert reached and runner.returncode == -signal.SIGKILL, "the run made no progress"
        final = start_runner(tmp_path, url, "full,plain", out)
        _, err = final.communicate(timeout=300)
        assert final.returncode == 0, err.decode(errors="replace")[-2_000:]
    finally:
        stub.stop()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    records = lines(requests)
    keys = [(r["arm"], r["seed"], r["repeat"], r["attempt"], r["n"]) for r in records]
    assert len(keys) == len(set(keys))  # no record twice
    journal = meter.load_journal(out)
    flying = {(e["arm"], e["seed"], e["repeat"], e["attempt"], e["n"]) for e in journal.in_flight}
    assert not flying & set(keys)  # no request both recorded and in flight
    unanswered = sum(len(v) for v in journal.unanswered.values())
    assert opened() == len(keys) + len(flying) + unanswered  # each journal line counted exactly once
    billed = len(stub.messages_requests())
    assert billed <= len(keys) + len(flying) + unanswered  # no billed request uncounted
    assert len(flying) >= 1 and len(flying) - (billed - len(keys) - unanswered) <= len(kills)
    assert manifest["in_flight"] == journal.in_flight and manifest["requests_in_flight"] == len(flying)
    assert manifest["spent_tokens"] == (meter.read_spent(requests) + journal.in_flight_tokens
                                        + journal.unanswered_tokens)
    assert all(u["status"] == "complete" for u in manifest["units"])


def test_no_process_the_run_started_outlives_a_sigkill_of_the_runner(tmp_path, script):
    stub = bench_stubs.StubModel([script], key=bench_stubs.FAKE_KEY, delay_s={"contexto": 0.05})
    url = stub.start()
    out = tmp_path / "out"
    try:
        # The stub claude backs off 30 seconds before each retry, as Claude Code does: once the runner and
        # its meter are gone, nothing but the watchdog would end it for minutes.
        runner = start_runner(tmp_path, url, "contexto", out, STUB_CLAUDE_RETRY_WAIT="30")
        # Several requests in: contexto run, its watchdog, claude, Contexto's MCP server and the
        # benchmark's own watchdog are all running.
        assert wait_for(lambda: len(stub.messages_requests("contexto")) >= 4 or runner.poll() is not None)
        tree = process_tree(runner.pid) - {runner.pid}
        assert runner.poll() is None and len(tree) >= 4, tree
        runner.send_signal(signal.SIGKILL)
        runner.wait(timeout=30)
        killed = time.monotonic()
        assert wait_for(lambda: not alive(tree), timeout=5.0), f"still running: {sorted(alive(tree))}"
        assert time.monotonic() - killed < 5.0
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        (unit,) = manifest["units"]
        assert (unit["status"], unit["attempt"]) == ("running", 0)  # attributed before its first request
        assert {r["attempt"] for r in lines(out / "requests.jsonl")} == {0}
    finally:
        stub.stop()


@pytest.mark.parametrize(("nth", "cut"), [(5, 0), (1, 20), (1, 95)])
def test_a_record_torn_by_a_kill_mid_write_is_moved_aside_and_the_next_run_appends_after_it(tmp_path, script, nth,
                                                                                             cut):
    """SIGKILL while writing a request record leaves a last line with no newline. The resumed run moves
    that fragment, byte for byte, to requests.jsonl.torn-1 and records it in the manifest (torn_lines)
    before it appends anything, so its first record starts a line of its own. The request was answered,
    so it may have been billed: the reservation journal counts it at its own reservation, also when the
    cut falls before the record's reservation in a new directory's first record (#286 Reviewer A low 1
    at 99ee7b07: it was counted at 0)."""
    stub = bench_stubs.StubModel([script], key=bench_stubs.FAKE_KEY)
    url = stub.start()
    out = tmp_path / "out"
    requests = out / "requests.jsonl"
    marker = tmp_path / "torn"
    tear = tmp_path / "tear.py"
    tear.write_text(TEAR, encoding="utf-8")
    try:
        runner = start_runner(tmp_path, url, "full", out, prefix=[str(tear), str(marker), str(nth), str(cut)])
        reached = wait_for(lambda: marker.exists() or runner.poll() is not None)
        runner.kill()
        runner.wait(timeout=30)
        assert reached and marker.exists(), runner.stderr.read().decode(errors="replace")[-2_000:]
        data = requests.read_bytes()
        assert not data.endswith(b"\n") and data.count(b"\n") == nth - 1
        fragment = data[data.rfind(b"\n") + 1:]
        assert not cut or len(fragment) == cut
        (torn_entry,) = [e for e in lines(out / meter.JOURNAL_NAME) if e["n"] == nth - 1]
        billed_before = len(stub.messages_requests())
        assert billed_before == nth  # the full arm sends one request at a time: the torn one is the last
        final = start_runner(tmp_path, url, "full", out)
        _, err = final.communicate(timeout=300)
        assert final.returncode == 0, err.decode(errors="replace")[-2_000:]
    finally:
        stub.stop()
    assert (out / "requests.jsonl.torn-1").read_bytes() == fragment
    text = requests.read_text(encoding="utf-8")
    assert text.endswith("\n") and all(json.loads(line) for line in text.splitlines())  # every line whole
    assert text.encode().startswith(data[: data.rfind(b"\n") + 1])  # the whole records are kept as they were
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    (torn,) = manifest["torn_lines"]
    assert {k: torn[k] for k in ("file", "action", "moved_to", "bytes")} == {
        "file": "requests.jsonl", "action": "moved", "moved_to": "requests.jsonl.torn-1", "bytes": len(fragment)}
    (unit,) = manifest["units"]
    assert unit["status"] == "complete" and [a["attempt"] for a in unit["attempts"]] == [0, 1]
    assert unit["attempts"][0]["requests"] == nth - 1  # the torn record is not counted as one of the file's
    # The journal counts the torn request, once, at the reservation written before it was forwarded.
    assert torn["counted_from"] == "journal" and "counted_tokens" not in torn
    assert torn_entry["reserved_tokens"] > 0 and torn_entry["attempt"] == 0
    assert manifest["in_flight"] == [torn_entry]
    assert (manifest["requests_in_flight"], manifest["in_flight_tokens"]) == (1, torn_entry["reserved_tokens"])
    assert manifest["spent_tokens"] == meter.read_spent(requests) + torn_entry["reserved_tokens"]
    # Every request the stub answered is counted once: a whole record, or the torn one in flight.
    assert len(stub.messages_requests()) == len(lines(requests)) + 1
