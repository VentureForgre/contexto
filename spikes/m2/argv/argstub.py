"""Zero-spend check: does each launcher command-line form keep the user's first claude argument?

`contexto run claude` puts its own `--settings` and `--mcp-config` before the user's arguments.
`--mcp-config` takes several files, so in the two-token form (`--mcp-config <file>`) Claude Code
2.1.302 and 2.1.310 also read a prompt right after `claude` as a config file and exit with "MCP
config file not found". This runs the real launcher (`python -m contexto run ... claude ...`)
against each scenario below with a local stub upstream that answers "OK" and forwards nothing; the
key is a fake from apiKeyHelper. Claude Code runs with a temporary CLAUDE_CONFIG_DIR and
CONTEXTO_HOME, so the user's own settings are not read. Interactive scenarios run in a pseudo
terminal and are stopped once the stub has the prompt (or, with no prompt, once the MCP server has
listed its tools).

Outside connections: Claude Code runs with CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, DISABLE_TELEMETRY,
DISABLE_ERROR_REPORTING, DISABLE_AUTOUPDATER, and CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL
set to 1. Without the last one, interactive Claude Code installs the official
plugin marketplace: it tries `downloads.claude.ai:443` and `github.com:443`, runs `ssh -T ... git@github.com`,
and runs `git clone` over SSH, which ignores HTTPS_PROXY and uses this machine's SSH key (seen on
2.1.310 with GIT_SSH_COMMAND pointed at a fake that fails, and in review on 2.1.302). As a second
guard, HTTPS_PROXY points at a local proxy in this script that refuses every CONNECT and lists the hosts
under `blocked`. Measured on 2.1.302 and 2.1.310, all eight scenarios: `blocked` stays empty, and
sampling every descendant process of this script every 0.1 s (ps, and lsof on inet sockets) found no
git or ssh process and no socket to anything but 127.0.0.1. A sample can miss a very short connection.
The `--work` directory is made absolute, and an existing one is deleted only if it is empty or holds
this spike's `.argstub-work` marker.

A scenario passes when Claude Code starts Contexto's MCP server and it lists its tools (so
`--mcp-config` was applied) and a request reaches the stub with the scenario's prompt in its last user
message (so `--settings` was applied: the stub is only reachable through the proxy's base URL). In
`-p` mode a request must also name Contexto's tools; an interactive session can send its first
request before the MCP server connects, so there the server's tools/list is the check. For the
no-prompt scenario, it passes when the MCP server lists its tools and Claude Code is still running.

Usage (from the repository root, with the package installed in the running Python):

    python spikes/m2/argv/argstub.py --claude-version 2.1.310 --work /tmp/argstub
    python spikes/m2/argv/argstub.py --claude-version 2.1.302 --work /tmp/argstub --form split

`--form joined` (default) is the launcher as installed; `--form split` swaps in the old two-token
form, `--form joined-settings-split` keeps `--settings <file>` split and joins only `--mcp-config`.
The Claude Code binary is ~/.local/share/claude/versions/<version>. Prints one JSON line per scenario.
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FORMS = {
    "joined": None,
    "split": "lambda c, s, m, a: [c, '--settings', str(s), '--mcp-config', str(m), *a]",
    "joined-settings-split": "lambda c, s, m, a: [c, '--settings', str(s), f'--mcp-config={m}', *a]",
}
PATCHED = ("import sys; import contexto.launcher as launcher; launcher.claude_argv = {form}; "
           "from contexto.cli import main; sys.argv[0] = 'contexto'; sys.exit(main())")
MODEL = "claude-haiku-5-5"
SCENARIOS = [  # name, claude arguments, prompt marker (None: no prompt), run in a pseudo terminal
    ("prompt_first_interactive", ["ARGSTUB_PROMPT_FIRST_TTY"], "ARGSTUB_PROMPT_FIRST_TTY", True),
    ("prompt_first_then_print", ["ARGSTUB_PROMPT_FIRST", "-p", "--model", MODEL], "ARGSTUB_PROMPT_FIRST", False),
    ("prompt_after_flags", ["-p", "--model", MODEL, "--output-format", "json", "ARGSTUB_AFTER_FLAGS"],
     "ARGSTUB_AFTER_FLAGS", False),
    ("print_prompt", ["-p", "ARGSTUB_PRINT", "--model", MODEL], "ARGSTUB_PRINT", False),
    ("dashdash_prompt", ["-p", "--model", MODEL, "--", "ARGSTUB_DASHDASH"], "ARGSTUB_DASHDASH", False),
    ("dashdash_first_interactive", ["--", "ARGSTUB_DASHDASH_TTY"], "ARGSTUB_DASHDASH_TTY", True),
    # -c continues the newest conversation in the scratch directory, which an earlier scenario left;
    # with --only and no earlier conversation it has nothing to continue.
    ("prompt_first_continue_print", ["ARGSTUB_CONTINUE", "-c", "-p", "--model", MODEL], "ARGSTUB_CONTINUE", False),
    ("no_prompt_interactive", [], None, True),
]
CONTEXTO_TOOL = "mcp__contexto__contexto_recall"

# Keep Claude Code from contacting anything but the stub where it can (as the other spikes do).
QUIET_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
             "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
             # Interactive Claude Code otherwise clones the official plugin marketplace with
             # `git clone git@github.com:...`: over SSH, outside HTTPS_PROXY, with this machine's SSH key.
             "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1"}
WORK_MARKER = ".argstub-work"


def prepare_work(work: Path) -> Path:
    """``work`` as an absolute path, emptied for this run. Claude Code runs in a subdirectory, so a
    relative CLAUDE_CONFIG_DIR would point elsewhere and Claude Code could fall back to the machine's own
    login. An existing directory is deleted only if it is empty or holds the marker this spike writes.
    ``work`` itself must not be a symlink; links among its parents (such as /tmp on macOS) are fine."""
    work = work.expanduser().absolute()
    if work.is_symlink():
        raise ValueError(f"{work} is a symlink; pass a real directory as --work")
    work = work.resolve()
    if work.exists():
        if not work.is_dir() or (any(work.iterdir()) and not (work / WORK_MARKER).is_file()):
            raise ValueError(f"{work} exists and was not made by this spike (no {WORK_MARKER}); pick another --work")
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / WORK_MARKER).write_text("scratch directory of a Contexto spike; deleted on its next run\n")
    return work


class RefusingProxy(socketserver.ThreadingTCPServer):
    """HTTPS_PROXY for Claude Code: answers each CONNECT with 403 and records its host:port."""

    daemon_threads = True

    def __init__(self, state: dict) -> None:
        state.setdefault("blocked", [])

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                parts = self.rfile.readline().decode("latin-1").split()
                if len(parts) >= 2:
                    state["blocked"].append(parts[1] if parts[0] == "CONNECT" else f"{parts[0]} {parts[1]}")
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")

        super().__init__(("127.0.0.1", 0), Handler)


def make_handler(state: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            body = json.loads(raw or b"{}")
            if self.path.split("?")[0].endswith("count_tokens"):
                return self._send(json.dumps({"input_tokens": 1000}).encode(), "application/json")
            # Claude Code 2.1.310 defers MCP tools behind ToolSearch and names them in a deferred-tool
            # listing rather than in "tools", so look for the name anywhere in the body.
            state["requests"].append({"contexto_tools": CONTEXTO_TOOL in raw.decode("utf-8", "replace"),
                                      "last_user": last_user_text(body.get("messages") or [])})
            msg = {"id": "msg_stub", "type": "message", "role": "assistant", "model": body.get("model", MODEL),
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                             "output_tokens": 1}}
            events = [{"type": "message_start", "message": msg},
                      {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                      {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
                      {"type": "content_block_stop", "index": 0},
                      {"type": "message_delta", "usage": {"output_tokens": 1},
                       "delta": {"stop_reason": "end_turn", "stop_sequence": None}},
                      {"type": "message_stop"}]
            self._send("".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode(),
                       "text/event-stream")

        def do_HEAD(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def _send(self, data: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


def last_user_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            return " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))
    return ""


def mcp_listed(home: Path, session: str) -> bool:
    """The MCP server answered tools/list for this launch (it records ``mcp_listed_at`` in meta.json)."""
    try:
        return bool(json.loads((home / "sessions" / session / "meta.json").read_text()).get("mcp_listed_at"))
    except (OSError, ValueError):
        return False


def run_scenario(launcher: list[str], args: list[str], marker: str | None, tty: bool, session: str,
                 url: str, scratch: Path, home: Path, env: dict, state: dict) -> dict:
    cmd = [*launcher, "run", "--bound", "150000", "--session", session, "--upstream", url, "claude", *args]

    def done() -> bool:
        if marker is None:
            return mcp_listed(home, session)
        return any(marker in r["last_user"] for r in state["requests"])

    if not tty:
        proc = subprocess.run(cmd, cwd=scratch, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=180)
        return {"exit": proc.returncode, "output": (proc.stdout + proc.stderr)[-300:], "running": False}
    master, slave = pty.openpty()
    proc = subprocess.Popen(cmd, cwd=scratch, env={**env, "TERM": "xterm-256color"}, stdin=slave, stdout=slave,
                            stderr=slave, start_new_session=True)
    os.close(slave)
    out = bytearray()

    def drain() -> None:
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            out.extend(chunk)

    threading.Thread(target=drain, daemon=True).start()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and proc.poll() is None and not done():
        time.sleep(0.25)
    if marker is None and proc.poll() is None:
        time.sleep(2)  # still up a moment after the MCP server listed its tools
    running = proc.poll() is None
    if running:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    os.close(master)
    text = out.decode("utf-8", "replace")
    return {"exit": None if running else proc.returncode, "running": running,
            "output": "MCP config file not found" if "MCP config file not found" in text else text[-300:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--claude-version", required=True)
    parser.add_argument("--work", type=Path, required=True,
                        help="scratch directory, replaced on each run (only one this spike made, or an empty one)")
    parser.add_argument("--form", choices=sorted(FORMS), default="joined")
    parser.add_argument("--only", help="run only this scenario")
    a = parser.parse_args()
    binary = Path.home() / ".local/share/claude/versions" / a.claude_version
    if not binary.exists():
        print(f"argstub: no Claude Code at {binary}", file=sys.stderr)
        return 2
    state: dict = {"requests": []}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    refuser = RefusingProxy(state)
    threading.Thread(target=refuser.serve_forever, daemon=True).start()
    https_proxy = f"http://127.0.0.1:{refuser.server_address[1]}"
    form = FORMS[a.form]
    launcher = ([sys.executable, "-I", "-c", PATCHED.format(form=form)] if form
                else [sys.executable, "-I", "-m", "contexto"])
    try:
        work = prepare_work(a.work)
        config, home, scratch, bindir = (work / d for d in ("cfg", "home", "scratch", "bin"))
        for d in (config, home, scratch, bindir):
            d.mkdir(parents=True)
        scratch = scratch.resolve()
        (config / "settings.json").write_text(json.dumps({"apiKeyHelper": "echo stub-not-a-key"}))
        # Skip first-run onboarding and the folder trust dialog in the interactive scenarios.
        (config / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True, "theme": "dark",
            "projects": {str(scratch): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
        (bindir / "claude").symlink_to(binary)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OPENRIG_", "CLAUDE_CODE_", "CLAUDECODE"))
               and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
               and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        env.update(CLAUDE_CONFIG_DIR=str(config), CONTEXTO_HOME=str(home), PATH=f"{bindir}:{env['PATH']}", **QUIET_ENV,
                   CONTEXTO_NO_VERSION_CHECK="1", HTTPS_PROXY=https_proxy, https_proxy=https_proxy,
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        for i, (name, args, marker, tty) in enumerate(SCENARIOS):
            if a.only and name != a.only:
                continue
            state["requests"].clear()
            state["blocked"].clear()
            result = run_scenario(launcher, args, marker, tty, f"argv{i}", url, scratch, home, env, state)
            prompted = None if marker is None else any(marker in r["last_user"] for r in state["requests"])
            tools = any(r["contexto_tools"] for r in state["requests"])
            listed = mcp_listed(home, f"argv{i}")
            ok = listed and (result["running"] if marker is None else prompted and (tty or tools))
            print(json.dumps({"claude_version": a.claude_version, "form": a.form, "scenario": name,
                              "pass": ok, "requests": len(state["requests"]), "prompt_seen": prompted,
                              "contexto_tools": tools, "mcp_listed": listed,
                              "blocked": sorted(set(state["blocked"])), **result}), flush=True)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"argstub: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        server.shutdown()
        refuser.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
