"""D44 zero-spend check: does Claude Code run Contexto's MCP tools under `contexto run claude -p`?

A local stub upstream answers the first request that carries a tool list with the tool_use blocks the
scenario names, and every other request with "OK". Nothing is forwarded anywhere; the key is a fake
from apiKeyHelper. Claude Code runs with a temporary CLAUDE_CONFIG_DIR and CONTEXTO_HOME, so the
user's own settings are not read. The transcript then shows, per call, whether the tool ran or was
refused, and the session log shows which calls reached the MCP server (`mcp_call`).

CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, DISABLE_TELEMETRY, DISABLE_ERROR_REPORTING,
DISABLE_AUTOUPDATER, and CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL are set to 1 (see
spikes/m2/argv/argstub.py). Sampling every descendant process of this script on 2.1.302 (ps, and lsof
on inet sockets, every 0.1 s) found no socket to anything but 127.0.0.1. `--work` is made absolute, and an existing one is deleted
only if it is empty or holds this spike's `.permstub-work` marker.

Usage (from the repository root, with the package installed in the running Python):

    python spikes/m2/permissions/permstub.py --claude-version 2.1.302 --work /tmp/permstub
    python spikes/m2/permissions/permstub.py --claude-version 2.1.302 --work /tmp/permstub --before
    python spikes/m2/permissions/permstub.py --claude-version 2.1.302 --work /tmp/permstub \
        --user-settings '{"permissions": {"ask": ["mcp__contexto__contexto_search"]}}'

`--before` runs the launcher with the pre-D44 `--settings` content (the base URL only). The Claude
Code binary is ~/.local/share/claude/versions/<version>. Prints one JSON line.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ALL_FIVE = [["mcp__contexto__contexto_search", {"query": "hello"}],
            ["mcp__contexto__contexto_recall", {"page_id": "pg_0000000000000000"}],
            ["mcp__contexto__contexto_state", {}],
            ["mcp__contexto__contexto_pin", {"kind": "decision", "text": "stub decision"}],
            ["mcp__contexto__contexto_close", {"obligation_id": "ob_x", "note": "n"}]]
BEFORE = ("import sys; import contexto.launcher as launcher; "
          "launcher.settings_payload = lambda url: {'env': {'ANTHROPIC_BASE_URL': url}}; "
          "from contexto.cli import main; sys.argv[0] = 'contexto'; sys.exit(main())")

# Keep Claude Code from contacting anything but the stub where it can (as the other spikes do).
QUIET_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
             "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
             # Interactive Claude Code otherwise clones the official plugin marketplace with
             # `git clone git@github.com:...`: over SSH, outside HTTPS_PROXY, with this machine's SSH key.
             "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1"}
WORK_MARKER = ".permstub-work"


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


def make_handler(model: str, tools: list, state: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            body = json.loads(raw or b"{}")
            if self.path.split("?")[0].endswith("count_tokens"):
                return self._send(json.dumps({"input_tokens": 1000}).encode(), "application/json")
            use_tool = bool(body.get("tools")) and not state["tool_sent"]
            state["tool_sent"] = state["tool_sent"] or use_tool
            msg = {"id": "msg_stub", "type": "message", "role": "assistant", "model": model, "content": [],
                   "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                             "output_tokens": 1}}
            events: list[dict] = [{"type": "message_start", "message": msg}]
            if use_tool:
                for i, (name, arguments) in enumerate(tools):
                    events += [{"type": "content_block_start", "index": i, "content_block":
                                {"type": "tool_use", "id": f"toolu_stub{i}", "name": name, "input": {}}},
                               {"type": "content_block_delta", "index": i,
                                "delta": {"type": "input_json_delta", "partial_json": json.dumps(arguments)}},
                               {"type": "content_block_stop", "index": i}]
            else:
                events += [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                           {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
                           {"type": "content_block_stop", "index": 0}]
            events += [{"type": "message_delta", "usage": {"output_tokens": 5},
                        "delta": {"stop_reason": "tool_use" if use_tool else "end_turn", "stop_sequence": None}},
                       {"type": "message_stop"}]
            data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            self._send(data, "text/event-stream")

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


def tool_results(projects: Path) -> list[dict]:
    """Each tool result in the first transcript: the tool, whether it is an error or a refusal, and its text."""
    uses: dict[str, str] = {}
    results: dict[str, dict] = {}  # by tool_use_id: a transcript can record the same result twice
    for path in sorted(projects.rglob("*.jsonl"))[:1]:
        for line in path.read_text().splitlines():
            for block in (json.loads(line).get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    uses[block.get("id")] = block.get("name")
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                    text = json.dumps(block.get("content"))
                    results[block["tool_use_id"]] = {"tool": uses[block["tool_use_id"]],
                                                     "is_error": bool(block.get("is_error")),
                                                     "refused": "haven't granted" in text, "text": text[:120]}
    return list(results.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--claude-version", required=True)
    parser.add_argument("--work", type=Path, required=True,
                        help="scratch directory, replaced on each run (only one this spike made, or an empty one)")
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--tools", default=json.dumps(ALL_FIVE), help="JSON list of [tool name, input]")
    parser.add_argument("--user-settings", default="{}", help="JSON merged into the temporary user settings")
    parser.add_argument("--before", action="store_true", help="the pre-D44 --settings content (base URL only)")
    parser.add_argument("claude_args", nargs="*", help="extra claude arguments, after --")
    a = parser.parse_args()
    binary = Path.home() / ".local/share/claude/versions" / a.claude_version
    if not binary.exists():
        print(f"permstub: no Claude Code at {binary}", file=sys.stderr)
        return 2
    state = {"tool_sent": False}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(a.model, json.loads(a.tools), state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        work = prepare_work(a.work)
        config, home, scratch, bindir = (work / d for d in ("cfg", "home", "scratch", "bin"))
        for d in (config, home, scratch, bindir):
            d.mkdir(parents=True)
        settings = {"apiKeyHelper": "echo stub-not-a-key", **json.loads(a.user_settings)}
        (config / "settings.json").write_text(json.dumps(settings))
        (bindir / "claude").symlink_to(binary)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OPENRIG_", "CLAUDE_CODE_", "CLAUDECODE"))
               and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
               and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        env.update(CLAUDE_CONFIG_DIR=str(config), CONTEXTO_HOME=str(home), PATH=f"{bindir}:{env['PATH']}", **QUIET_ENV)
        launcher = [sys.executable, "-I", "-c", BEFORE] if a.before else [sys.executable, "-I", "-m", "contexto"]
        url = f"http://127.0.0.1:{server.server_address[1]}"
        proc = subprocess.run([*launcher, "run", "--bound", "150000", "--session", "perm", "--upstream", url,
                               "claude", "-p", "Say OK", "--model", a.model, "--output-format", "json",
                               *a.claude_args],
                              cwd=scratch, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=180)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"permstub: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        server.shutdown()
    log = home / "sessions" / "perm" / "log.jsonl"
    calls = [e.get("tool") for e in map(json.loads, log.read_text().splitlines())
             if e.get("event") == "mcp_call"] if log.exists() else None
    print(json.dumps({"claude_version": a.claude_version, "before": a.before, "exit": proc.returncode,
                      "tool_sent": state["tool_sent"], "tool_results": tool_results(config / "projects"),
                      "mcp_calls": calls,
                      "warnings": [line for line in proc.stderr.splitlines() if "warning" in line]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
