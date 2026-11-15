"""Run the installed Claude Code against the local stub upstream, at zero spend, for the D70 spike.

    python3 mcp_probe.py <run> [--reuse RUN] [--parallel N] [--stdin-file F] [--env K=V ...]
                         [--timeout S] [--no-stub] -- <claude arguments>

Everything for a run lives in /tmp/d70-mcp/<run>/: ``stub/`` (every request the stub got, with all
headers), ``config/`` (CLAUDE_CONFIG_DIR), ``home/`` (HOME), ``work/`` (the working directory),
``claude-<i>.out`` and ``.err``, and ``start-env.json`` (the environment claude was started with).
``--reuse RUN`` takes config, home and work from an earlier run (for ``--resume``).

Claude Code gets an environment built from nothing: a temporary HOME and CLAUDE_CONFIG_DIR,
``ANTHROPIC_API_KEY=stub-key``, ``ANTHROPIC_BASE_URL`` at the stub on 127.0.0.1, telemetry, other
nonessential traffic and the auto-updater off. No ANTHROPIC_AUTH_TOKEN, Bedrock or Vertex variable
exists in it. In the claude arguments ``{run}`` is the run directory, ``{here}`` this directory,
``{py}`` the Python interpreter, ``{port}`` the stub's port and ``{i}`` the index of a parallel run.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path("/tmp/d70-mcp")
HERE = Path(__file__).resolve().parent
CLAUDE = os.environ.get("CLAUDE") or str(Path.home() / ".local/bin/claude")


def feed(proc, path, delay):
    try:
        for line in Path(path).read_text().splitlines():
            proc.stdin.write((line + "\n").encode())
            proc.stdin.flush()
            time.sleep(delay)
        proc.stdin.close()
    except OSError:
        pass


def main():
    argv = sys.argv[1:]
    if "--" not in argv:
        sys.exit(__doc__)
    split = argv.index("--")
    parser = argparse.ArgumentParser()
    parser.add_argument("run")
    parser.add_argument("--reuse")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--stdin-file")
    parser.add_argument("--stdin-delay", type=float, default=0, help="seconds to wait after each stdin line")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--no-stub", action="store_true")
    args = parser.parse_args(argv[:split])
    claude_args = argv[split + 1:]

    run = ROOT / args.run
    base = ROOT / args.reuse if args.reuse else run
    for d in (run, run / "stub", base / "config", base / "home", base / "work"):
        d.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stub = None
    if not args.no_stub:
        stub_log = run / "stub.log"
        stub = subprocess.Popen([sys.executable, str(HERE / "stub_upstream.py"), str(port), str(run / "stub")],
                                stdout=open(stub_log, "w"), stderr=subprocess.STDOUT)
        for _ in range(50):
            if stub_log.read_text().splitlines()[:1] == ["listening"] or stub.poll() is not None:
                break
            time.sleep(0.1)
        if stub.poll() is not None or stub_log.read_text().splitlines()[:1] != ["listening"]:
            stub.kill()
            sys.exit(f"stub not listening on port {port} (see {stub_log})")
    env = {"HOME": str(base / "home"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "dumb",
           "CLAUDE_CONFIG_DIR": str(base / "config"), "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
           "ANTHROPIC_API_KEY": "stub-key", "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
           "DISABLE_AUTOUPDATER": "1", "D70_START_MARK": "set-when-claude-was-started"}
    for pair in args.env:
        key, _, value = pair.partition("=")
        env[key] = value.replace("{run}", str(run)).replace("{port}", str(port))
    (run / "start-env.json").write_text(json.dumps(env, indent=1))
    procs = []
    try:
        for i in range(args.parallel):
            cmd = [CLAUDE] + [a.replace("{run}", str(run)).replace("{here}", str(HERE)).replace("{py}", sys.executable)
                              .replace("{port}", str(port)).replace("{i}", str(i)) for a in claude_args]
            paced = bool(args.stdin_file and args.stdin_delay)
            stdin = (subprocess.PIPE if paced else open(args.stdin_file.replace("{run}", str(run)))
                     if args.stdin_file else subprocess.DEVNULL)
            procs.append(subprocess.Popen(cmd, cwd=base / "work", env=env, stdin=stdin,
                                          stdout=open(run / f"claude-{i}.out", "w"),
                                          stderr=open(run / f"claude-{i}.err", "w")))
            if paced:  # one line, a pause for the turn to finish, the next line; then end of input
                threading.Thread(target=feed, args=(procs[-1], args.stdin_file.replace("{run}", str(run)),
                                                    args.stdin_delay), daemon=True).start()
        deadline = time.time() + args.timeout
        for p in procs:
            try:
                p.wait(timeout=max(1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
    finally:
        if stub is not None:
            stub.terminate()
    sessions, agents, keys, kinds = set(), set(), set(), []
    for f in sorted((run / "stub").glob("*.json")):
        captured = json.loads(f.read_text())
        headers = {k.lower(): v for k, v in captured["headers"]}
        kinds.append(f.stem)
        if "x-claude-code-session-id" in headers:
            sessions.add(headers["x-claude-code-session-id"])
        if "x-claude-code-agent-id" in headers:
            agents.add(headers["x-claude-code-agent-id"])
        keys.update(v for k, v in headers.items() if k in ("x-api-key", "authorization"))
    print(json.dumps({"run": str(run), "claude_pids": [p.pid for p in procs], "exit": [p.returncode for p in procs],
                      "stub_requests": kinds, "session_ids": sorted(sessions), "agent_ids": sorted(agents),
                      "credentials_seen": sorted(keys)}, indent=1))


if __name__ == "__main__":
    main()
