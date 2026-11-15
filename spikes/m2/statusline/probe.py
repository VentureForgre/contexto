"""Measure how Claude Code treats a ``statusLine`` given in a ``--settings`` file (D64). Zero spend.

Real Claude Code runs against ``spikes/m2/subagents/stub.py`` (a local stand-in for the Messages API)
with a scratch ``HOME``, ``CLAUDE_CONFIG_DIR`` and ``CONTEXTO_HOME`` under ``$CX_SPIKE_DIR`` (default
``/tmp/cx-statusline-spike``), a fake key and ``DISABLE_AUTOUPDATER=1``; the user's own Claude Code and
Contexto data are never read or written. ``CLAUDE=<binary>`` picks the Claude Code to run.

usage: probe.py <case>

  flag       interactive; statusLine only in the --settings file
  print      ``claude -p``; statusLine only in the --settings file
  both       interactive; statusLine in the user settings and in the --settings file
  user       interactive; statusLine only in the user settings, a --settings file without one
  untrusted  interactive, in a folder Claude Code has not been told to trust; the dialog is left open
  untrusted-accept  the same, and the dialog is then accepted (Down, Enter: "Yes, I trust this folder")
  empty      interactive; the --settings command prints nothing
  none       interactive; no statusLine anywhere (what the screen looks like without one)
  late       interactive; the user settings get a statusLine after the session started, then a prompt
  contexto   interactive ``contexto run claude`` (this checkout) with an empty user configuration
  contexto-late  the same, and the user settings get a statusLine after the session started

Each case prints one JSON object: how often each status line command ran, what its first run saw (the
shell, the working directory, the variables of the launching environment, the JSON on stdin), and
which of the lines reached the screen.
"""

import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

case = sys.argv[1]
root = Path(os.environ.get("CX_SPIKE_DIR", "/tmp/cx-statusline-spike"))
here = Path(__file__).resolve().parent
stub_dir = here.parent / "subagents"
for name in ("cap", "work", "config", "home", "store"):
    shutil.rmtree(root / f"{name}-{case}", ignore_errors=True)
cap, work, cfg, home, store = (root / f"{name}-{case}" for name in ("cap", "work", "config", "home", "store"))
if case.startswith("contexto"):  # a store path the installed command must carry through Claude Code's shell
    store = root / f"store-{case}" / "it's a \"store\" $HOME"
for directory in (work, cfg, home):
    directory.mkdir(parents=True)
record = root / f"record-{case}.txt"
record.unlink(missing_ok=True)
key = "sk-ant-test-stub-0000000000"
trusted = not case.startswith("untrusted")
(cfg / ".claude.json").write_text(json.dumps({
    "hasCompletedOnboarding": True, "theme": "dark", "numStartups": 5,
    "customApiKeyResponses": {"approved": [key[-20:]], "rejected": []},
    "projects": {str(work.resolve()): {"hasTrustDialogAccepted": trusted, "hasCompletedProjectOnboarding": True}},
}))


def status_line(label):
    # The single-quoted ${...} reaches the script unexpanded unless Claude Code itself expands it.
    command = f"/bin/sh {shlex.quote(str(here / 'probe.sh'))} {shlex.quote(str(record))} {label} '${{CX_PROBE_MARK}}'"
    return {"statusLine": {"type": "command", "command": command}}


user_settings = cfg / "settings.json"
if case in ("both", "user"):
    user_settings.write_text(json.dumps(status_line("USER")))
flag_file = root / f"flag-{case}.json"
flag_file.write_text(json.dumps({} if case in ("user", "none") else
                                status_line("EMPTY" if case == "empty" else "FLAG")))

with socket.socket() as free:
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
stub_log = root / f"stub-{case}.log"
stub = subprocess.Popen([sys.executable, str(stub_dir / "stub.py"), str(port), str(cap), str(here / "scenario.py")],
                        stdout=open(stub_log, "w"), stderr=subprocess.STDOUT)
for _ in range(100):
    if stub_log.read_text().splitlines()[:1] == ["listening"] or stub.poll() is not None:
        break
    time.sleep(0.1)
if stub.poll() is not None or stub_log.read_text().splitlines()[:1] != ["listening"]:
    stub.kill()
    sys.exit(f"stub not listening on port {port} (see {stub_log})")

claude = os.environ.get("CLAUDE") or shutil.which("claude") or "claude"
url = f"http://127.0.0.1:{port}"
path = os.environ.get("PATH", "/usr/bin:/bin")
if os.environ.get("CLAUDE"):  # `contexto run claude` finds claude on PATH: put the chosen binary first
    (root / "bin").mkdir(exist_ok=True)
    (root / "bin" / "claude").unlink(missing_ok=True)
    (root / "bin" / "claude").symlink_to(claude)
    path = f"{root / 'bin'}{os.pathsep}{path}"
env = {"HOME": str(home), "PATH": path, "TERM": "xterm-256color",
       "CLAUDE_CONFIG_DIR": str(cfg), "ANTHROPIC_API_KEY": key, "DISABLE_TELEMETRY": "1",
       "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
       "CX_PROBE_MARK": "launcher-environment", "CONTEXTO_HOME": str(store)}
if case.startswith("contexto"):
    argv = [sys.executable, "-m", "contexto", "run", "--upstream", url, "--no-version-check", "claude",
            "--model", "claude-haiku-5-5"]
else:
    env["ANTHROPIC_BASE_URL"] = url
    argv = [claude, f"--settings={flag_file}", "--model", "claude-haiku-5-5"]
version = subprocess.run([claude, "--version"], capture_output=True, text=True, env=env).stdout.strip()

if case == "print":
    done = subprocess.run([*argv, "-p", "say hi"], env=env, cwd=work, capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)
    screen_text = done.stdout + done.stderr
else:
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(work)
        os.execve(argv[0], argv, env)
    screen = bytearray()

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    screen.extend(os.read(fd, 65536))
                except OSError:
                    return

    def requests():
        return len(list(cap.glob("*.json"))) if cap.is_dir() else 0

    def type_line(text):
        for ch in text:
            os.write(fd, ch.encode())
            pump(0.05)
        pump(0.5)
        os.write(fd, b"\r")

    pump(12)
    runs_before_accept = record.read_text().count("=== ") if record.exists() else 0
    if case == "untrusted-accept":
        os.write(fd, b"\x1b[B")  # the dialog opens on "No, exit"; Down selects "Yes, I trust this folder"
        pump(1)
        os.write(fd, b"\r")
        pump(10)
    if case in ("late", "contexto-late"):
        user_settings.write_text(json.dumps(status_line("USER")))
        pump(4)
    if case in ("late", "contexto", "contexto-late", "flag", "both"):
        before = requests()
        type_line("say hi")
        end = time.time() + 40
        while time.time() < end and requests() <= before:
            pump(0.5)
        pump(6)
    os.kill(pid, signal.SIGTERM)
    pump(3)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    (root / f"screen-{case}.log").write_bytes(bytes(screen))
    screen_text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", screen.decode("utf-8", "replace"))
stub.terminate()

runs = record.read_text().split("=== ")[1:] if record.exists() else []
first = runs[0].splitlines() if runs else []
seen_env = {line[4:].split("=", 1)[0]: line[4:].split("=", 1)[1] for line in first if line.startswith("env:") and "=" in line}
stdin_line = next((line[6:] for line in first if line.startswith("stdin:")), "")
try:
    stdin_keys = sorted(json.loads(stdin_line))
except ValueError:
    stdin_keys = stdin_line[:80]
contexto_line = re.search(r"(ctx |contexto: restart)[^\r\n]{0,80}", screen_text)
print(json.dumps({
    "case": case, "claude": version, "trusted": trusted,
    "runs_before_the_dialog_was_answered": None if case == "print" else runs_before_accept,
    "runs": {label: sum(1 for run in runs if run.startswith(label)) for label in ("FLAG", "USER", "EMPTY")},
    "screen_has": {label: f"PROBE-{label}-LINE" in screen_text for label in ("FLAG", "USER")},
    "screen_trust_dialog": "trust" in screen_text.lower(),
    "contexto_line_on_screen": contexto_line.group(0).strip() if contexto_line else None,
    "first_run": {
        "argument_with_dollar_brace": next((line[5:] for line in first if line.startswith("arg3=")), None),
        "parent": next((line[7:] for line in first if line.startswith("parent=")), None),
        "cwd_is_work_dir": next((line[4:] for line in first if line.startswith("cwd=")), None) == str(work.resolve()),
        "stdin": next((line[4:] for line in first if line.startswith("tty=")), None),
        "inherits_marker": seen_env.get("CX_PROBE_MARK"),
        "inherits_claude_config_dir": seen_env.get("CLAUDE_CONFIG_DIR") == str(cfg),
        "inherits_home": seen_env.get("HOME") == str(home),
        "columns_lines": [seen_env.get("COLUMNS"), seen_env.get("LINES")],
        "stdin_keys": stdin_keys,
    } if first else None,
    "requests_to_stub": len(list(cap.glob("*.json"))) if cap.is_dir() else 0,
}, indent=1))
