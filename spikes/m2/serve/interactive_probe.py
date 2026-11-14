"""Drive interactive Claude Code in a pty against the stub, for the D70 spike (as spikes/m2/subagents/drive.py).

    python3 interactive_probe.py <run>

Plain ``claude`` (no --mcp-config, no --settings): the recording stdio MCP server is registered at user
scope in ``$CLAUDE_CONFIG_DIR/.claude.json`` and the recording SessionStart and PreToolUse hooks and
the permission rule are in ``$CLAUDE_CONFIG_DIR/settings.json``. Types ``D70CALL1 one``, then
``/clear``, then ``D70CALL1 two``, and exits. Zero spend: the same scrubbed environment as
mcp_probe.py. Captures in /tmp/d70-mcp/<run>/ (``mcp.jsonl``, ``hooks.jsonl``, ``stub/``, ``screen.log``).
"""

import json
import os
import pty
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    run = Path("/tmp/d70-mcp") / sys.argv[1]
    here = Path(__file__).resolve().parent
    claude = os.environ.get("CLAUDE") or str(Path.home() / ".local/bin/claude")
    shutil.rmtree(run, ignore_errors=True)
    cfg, home, work, cap = run / "config", run / "home", run / "work", run / "stub"
    for d in (cfg, home, work, cap):
        d.mkdir(parents=True)
    key = "stub-key"
    hook = f"{sys.executable} {here / 'hook_record.py'} {run / 'hooks.jsonl'}"
    (cfg / ".claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True, "theme": "dark", "numStartups": 5,
        "customApiKeyResponses": {"approved": [key[-20:]], "rejected": []},
        "projects": {str(work.resolve()): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}},
        "mcpServers": {"probe": {"type": "stdio", "command": sys.executable,
                                 "args": [str(here / "recording_mcp.py"), str(run / "mcp.jsonl")]}},
    }))
    (cfg / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["mcp__probe__probe_echo"]},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": hook}]}],
                  "PreToolUse": [{"matcher": "mcp__probe__.*", "hooks": [{"type": "command", "command": hook}]}]},
    }))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stub_log = run / "stub.log"
    stub = subprocess.Popen([sys.executable, str(here / "stub_upstream.py"), str(port), str(cap)],
                            stdout=open(stub_log, "w"), stderr=subprocess.STDOUT)
    for _ in range(50):
        if stub_log.read_text().splitlines()[:1] == ["listening"] or stub.poll() is not None:
            break
        time.sleep(0.1)
    if stub.poll() is not None or stub_log.read_text().splitlines()[:1] != ["listening"]:
        stub.kill()
        sys.exit(f"stub not listening on port {port} (see {stub_log})")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm-256color",
           "CLAUDE_CONFIG_DIR": str(cfg), "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}", "ANTHROPIC_API_KEY": key,
           "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
           "D70_START_MARK": "set-when-claude-was-started"}
    (run / "start-env.json").write_text(json.dumps(env, indent=1))
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(work)
        os.execve(claude, [claude, "--model", "claude-haiku-5-5"], env)
    screen = open(run / "screen.log", "wb")


    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                screen.write(data)
                screen.flush()


    def captured(kind):
        return sorted(f.name for f in cap.glob("*.json") if kind in f.name)


    def wait_for(pred, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            pump(0.5)
        return False


    def type_line(text):
        for ch in text:
            os.write(fd, ch.encode())
            pump(0.05)
        pump(0.7)
        os.write(fd, b"\r")


    pump(8)
    type_line("D70CALL1 one")
    print("first call requested:", wait_for(lambda: len(captured("call1")) >= 1, 40))
    pump(5)
    type_line("/clear")
    pump(5)
    type_line("D70CALL1 two")
    print("second call requested:", wait_for(lambda: len(captured("call1")) >= 2, 40))
    pump(5)
    os.kill(pid, signal.SIGTERM)
    pump(2)
    stub.terminate()
    print("claude pid:", pid, "stub requests:", sorted(f.name for f in cap.glob("*.json")))


if __name__ == "__main__":
    main()
