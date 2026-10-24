"""Drive interactive Claude Code 2.1.302 in a pty against the stub (isolated config dir).

usage: drive.py <name> <mode>
mode esc:    type MAIN-RUN; when the subagent's first request has been answered (its 30 s Bash runs),
             press Esc; then type HUMAN-NEXT; wait for main's reply; exit.
mode bgidle: type MAIN-RUN; once main ended its turn, type HUMAN-NEXT while the background agent runs;
             wait for the completion notification turn; exit.
"""

import json
import os
import pty
import random
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

name, mode = sys.argv[1], sys.argv[2]
root = Path(os.environ.get("CX_SPIKE_DIR", "/tmp/cx-subagent-spike"))
here = Path(__file__).resolve().parent
cap = root / f"cap-{name}"
work = root / f"work-{name}"
cfg = root / f"config-{name}"
for p in (cap,):
    subprocess.run(["rm", "-rf", str(p)])
work.mkdir(parents=True, exist_ok=True)
cfg.mkdir(parents=True, exist_ok=True)
key = "sk-ant-test-stub-0000000000"
(cfg / ".claude.json").write_text(json.dumps({
    "hasCompletedOnboarding": True, "theme": "dark", "numStartups": 5,
    "customApiKeyResponses": {"approved": [key[-20:]], "rejected": []},
    "bypassPermissionsModeAccepted": True,
    "projects": {str(work.resolve()): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}},
}))
port = 18900 + random.randrange(90)
env = dict(os.environ, SCEN_MODE=mode, PYTHONPATH=str(here))
stub = subprocess.Popen([sys.executable, str(here / "stub.py"), str(port), str(cap),
                         str(here / "esc_scenario.py")], env=env,
                        stdout=open(root / f"stub-{name}.log", "w"), stderr=subprocess.STDOUT)
time.sleep(1)
for var in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
    env.pop(var, None)
env.update(CLAUDE_CONFIG_DIR=str(cfg), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_API_KEY=key,
           DISABLE_TELEMETRY="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1",
           TERM="xterm-256color")
pid, fd = pty.fork()
if pid == 0:
    os.chdir(work)
    os.execvpe("claude", ["claude", "--dangerously-skip-permissions", "--model", "claude-haiku-5-5"], env)
screen = open(root / f"screen-{name}.log", "wb")


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


def files(kind=None):
    return sorted(f.name for f in cap.glob("*.json") if kind is None or kind in f.name)


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
    pump(0.5)
    os.write(fd, b"\r")


pump(6)
type_line("MAIN-RUN")
if mode == "esc":
    ok = wait_for(lambda: len(files("sub")) >= 1, 40)
    print("subagent started:", ok, files())
    pump(4)  # the subagent's 30 s Bash is running
    os.write(fd, b"\x1b")
    print("sent Esc")
    pump(3)
    before = len(files("main"))
    type_line("HUMAN-NEXT after esc")
    ok = wait_for(lambda: len(files("main")) > before, 40)
    print("main after human:", ok, files())
    ok = wait_for(lambda: len(files("sub")) >= 2, 40)  # did the subagent survive the Esc?
    print("subagent continued after Esc:", ok, files())
    pump(8)
    print("after the subagent:", files())
else:
    ok = wait_for(lambda: len(files("main")) >= 2, 40)
    print("main turn 1 done:", ok, files())
    pump(2)
    before = len(files("main"))
    type_line("HUMAN-NEXT while bg runs")
    ok = wait_for(lambda: len(files("main")) > before, 40)
    print("main after human:", ok, files())
    ok = wait_for(lambda: len(files("main")) > before + 1, 40)
    print("main after notification:", ok, files())
    pump(3)
os.kill(pid, signal.SIGTERM)
pump(1)
stub.terminate()
print("final:", files())
