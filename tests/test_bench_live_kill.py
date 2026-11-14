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
from contexto.bench import script as bench_script

SEED = 3
BOUND = 30_000
TURN_KBYTES = 2
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and process groups")


@pytest.fixture(scope="module")
def script():
    return bench_script.build(SEED, "v2", bound=BOUND, turn_bytes=TURN_KBYTES * 1_000)


# Run as the runner instead of "python -m contexto": the nth request record is written in two halves,
# and after the first half the runner creates the marker file and waits there to be killed.
TEAR = """
import json, runpy, sys, time
from contexto.bench import meter
marker, nth = sys.argv[1], int(sys.argv[2])
sys.argv = ["contexto", *sys.argv[3:]]
real, seen = meter.Ledger.settle, [0]
def settle(self, reserved, record, kept=0):
    if record is not None:
        seen[0] += 1
        if seen[0] == nth:
            line = (json.dumps(record) + "\\n").encode()
            with self.path.open("ab") as f:
                f.write(line[: len(line) // 2])
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
               "--seed", str(SEED), "--bound", str(BOUND), "--turn-kbytes", str(TURN_KBYTES), "--arms", arms, "--upstream", url,
               "--out", str(out), "--work", str(tmp / "work"), "--budget-tokens", "2000000000"]
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


def test_a_record_torn_by_a_kill_mid_write_is_moved_aside_and_the_next_run_appends_after_it(tmp_path, script):
    """SIGKILL halfway through writing a request record leaves a last line with no newline. The resumed
    run moves that fragment, byte for byte, to requests.jsonl.torn-1 and records it in the manifest
    (torn_lines) before it appends anything, so its first record starts a line of its own."""
    stub = bench_stubs.StubModel([script], key=bench_stubs.FAKE_KEY)
    url = stub.start()
    out = tmp_path / "out"
    requests = out / "requests.jsonl"
    marker = tmp_path / "torn"
    tear = tmp_path / "tear.py"
    tear.write_text(TEAR, encoding="utf-8")
    try:
        runner = start_runner(tmp_path, url, "full", out, prefix=[str(tear), str(marker), "5"])
        reached = wait_for(lambda: marker.exists() or runner.poll() is not None)
        runner.kill()
        runner.wait(timeout=30)
        assert reached and marker.exists(), runner.stderr.read().decode(errors="replace")[-2_000:]
        data = requests.read_bytes()
        assert not data.endswith(b"\n") and data.count(b"\n") == 4
        fragment = data[data.rfind(b"\n") + 1:]
        final = start_runner(tmp_path, url, "full", out)
        _, err = final.communicate(timeout=300)
        assert final.returncode == 0, err.decode(errors="replace")[-2_000:]
    finally:
        stub.stop()
    assert (out / "requests.jsonl.torn-1").read_bytes() == fragment
    text = requests.read_text(encoding="utf-8")
    assert text.endswith("\n") and all(json.loads(line) for line in text.splitlines())  # every line whole
    assert text.encode().startswith(data[: data.rfind(b"\n") + 1])  # the four whole records are kept as they were
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    (torn,) = manifest["torn_lines"]
    assert {k: torn[k] for k in ("file", "action", "moved_to", "bytes")} == {
        "file": "requests.jsonl", "action": "moved", "moved_to": "requests.jsonl.torn-1", "bytes": len(fragment)}
    (unit,) = manifest["units"]
    assert unit["status"] == "complete" and [a["attempt"] for a in unit["attempts"]] == [0, 1]
    assert unit["attempts"][0]["requests"] == 4  # the torn record is not counted as one of the file's
