"""Launch an isolated Claude Code process against the fake upstream and monitor it.

Isolation (every launch): a scrubbed environment (env -i style), HOME and CLAUDE_CONFIG_DIR in a
temp root, a fake API key, ANTHROPIC_BASE_URL on 127.0.0.1, nonessential traffic, telemetry,
error reporting and the autoupdater off, and HTTPS_PROXY/HTTP_PROXY pointed at the fake upstream
(which refuses and logs every CONNECT) with NO_PROXY for loopback. While the process runs, the
monitor samples the RSS of the whole process tree and the tree's inet sockets (lsof); any socket
whose remote end is not loopback is recorded and makes the run abort.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

CLAUDE_BIN = str(Path.home() / ".local/bin/claude")
FAKE_KEY = "sk-ant-fake-not-a-credential-0000"
LOOPBACK = ("127.0.0.1", "[::1]", "localhost", "::1")
MAX_TREE_RSS_KB = int(os.environ.get("SCALE_MAX_RSS_GB", "14")) * 2**20


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_upstream(here: Path, port: int, log: Path, *args: str) -> subprocess.Popen:
    err = open(log.with_suffix(".err"), "w")
    p = subprocess.Popen(["python3", "-I", str(here / "fake_upstream.py"), str(port), str(log), *args],
                         stdout=err, stderr=err)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.05)
    p.kill()
    raise RuntimeError("fake upstream did not start")


def claude_env(root: Path, port: int, extra: dict | None = None) -> dict:
    home = root / "home"
    cfg = root / "config"
    home.mkdir(parents=True, exist_ok=True)
    cfg.mkdir(parents=True, exist_ok=True)
    proxy = f"http://127.0.0.1:{port}"
    env = {
        "HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TERM": "dumb", "LANG": "en_US.UTF-8",
        "TMPDIR": str(root / "tmp"),
        "CLAUDE_CONFIG_DIR": str(cfg), "ANTHROPIC_BASE_URL": proxy, "ANTHROPIC_API_KEY": FAKE_KEY,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_MAX_RETRIES": "0",
        "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
        "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
    }
    (root / "tmp").mkdir(exist_ok=True)
    env.update(extra or {})
    return env


def tree_pids(root_pid: int) -> list[int]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True).stdout
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen, stack = [], [root_pid]
    while stack:
        p = stack.pop()
        seen.append(p)
        stack.extend(kids.get(p, []))
    return seen


def tree_rss_kb(root_pid: int) -> tuple[int, int]:
    """(sum of tree RSS, RSS of the root process) in KB."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True).stdout
    rss, kids = {}, {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            pid, ppid, r = map(int, parts)
            rss[pid] = r
            kids.setdefault(ppid, []).append(pid)
    total, stack = 0, [root_pid]
    while stack:
        p = stack.pop()
        total += rss.get(p, 0)
        stack.extend(kids.get(p, []))
    return total, rss.get(root_pid, 0)


def inet_sockets(pids: list[int]) -> list[str]:
    if not pids:
        return []
    out = subprocess.run(["lsof", "-a", "-i", "-P", "-n", "-p", ",".join(map(str, pids))],
                         capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines()[1:] if ln.strip()]


def non_loopback(lines: list[str]) -> list[str]:
    bad = []
    for ln in lines:
        name = ln.split()[8] if len(ln.split()) > 8 else ln
        ends = name.replace("->", " ").split()
        for e in ends:
            host = e.rsplit(":", 1)[0]
            if host not in LOOPBACK and host != "*":
                bad.append(ln)
                break
    return bad


def run_claude(args: list[str], cwd: Path, env: dict, out_prefix: Path, timeout: float) -> dict:
    """Run claude with args; sample tree RSS every 0.05 s and the tree's inet sockets back to
    back (one lsof after another) in a thread for the whole run."""
    stdout = open(f"{out_prefix}.stdout", "w")
    stderr = open(f"{out_prefix}.stderr", "w")
    t0 = time.time()
    # /usr/bin/time -l reports the CLI's own peak RSS exactly; sampling covers the whole tree.
    p = subprocess.Popen(["/usr/bin/time", "-l", CLAUDE_BIN, *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                         stdout=stdout, stderr=stderr, start_new_session=True)
    peak_tree = peak_root = 0
    sockets: set[str] = set()
    bad: list[str] = []
    lsof_samples = [0]
    timed_out = rss_capped = False

    def sock_loop() -> None:
        while p.poll() is None:
            lines = inet_sockets(tree_pids(p.pid))
            lsof_samples[0] += 1
            for ln in lines:
                f = ln.split()
                sockets.add(" ".join(f[7:]) if len(f) > 7 else ln)
            b = non_loopback(lines)
            if b:
                bad.extend(b)
                os.killpg(p.pid, signal.SIGKILL)
                return

    th = threading.Thread(target=sock_loop, daemon=True)
    th.start()
    while p.poll() is None:
        now = time.time()
        if now - t0 > timeout:
            timed_out = True
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
            break
        tot, r = tree_rss_kb(p.pid)
        peak_tree, peak_root = max(peak_tree, tot), max(peak_root, r)
        if tot > MAX_TREE_RSS_KB:  # shared machine: never let one run take more than this
            rss_capped = True
            os.killpg(p.pid, signal.SIGKILL)
            break
        time.sleep(0.05)
    p.wait()
    th.join(5)
    t1 = time.time()
    stdout.close()
    stderr.close()
    m = re.search(r"(\d+)\s+maximum resident set size", Path(f"{out_prefix}.stderr").read_text(errors="replace"))
    # peak_root is the /usr/bin/time wrapper; maxrss_mb is the CLI itself.
    res = {"t_launch": t0, "maxrss_mb": round(int(m.group(1)) / 2**20, 1) if m else "", "t_exit": t1,
           "wall_s": round(t1 - t0, 2), "exit": p.returncode, "timed_out": timed_out, "rss_capped": rss_capped, "peak_tree_rss_mb": round(peak_tree / 1024, 1),
           "peak_root_rss_mb": round(peak_root / 1024, 1), "sockets": sorted(sockets),
           "non_loopback": bad, "lsof_samples": lsof_samples[0]}
    Path(f"{out_prefix}.monitor.json").write_text(json.dumps(res, indent=1))
    return res


def read_log(log: Path, since: float = 0.0) -> list[dict]:
    if not log.exists():
        return []
    out = []
    for line in log.read_text().splitlines():
        r = json.loads(line)
        if r.get("t", 0) >= since:
            out.append(r)
    return out
