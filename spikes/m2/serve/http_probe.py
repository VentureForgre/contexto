"""D70 spike: record what the installed Claude Code sends over HTTP, against a local stub upstream.

Zero spend and fully local. Every ``claude`` run gets a scrubbed environment (a fresh HOME, config
directory and working directory, the key ``stub-key``), and runs under a macOS sandbox profile that
denies every outbound connection except to localhost, DNS included, so a request that ignores the
base URL fails on this machine instead of leaving it.

The stub records the request line, every header (name and value, in arrival order) and the body of
every request, to ``<out>/<run>/NNN-<METHOD>-<kind>.json``, and writes ``<out>/<run>/index.tsv``.

usage: http_probe.py <run-name> [options]            (see --help)
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT = Path(os.environ.get("D70_OUT", "/tmp/d70-http"))
KEY = "stub-key"
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
"""
HINT = ("x-claude-code-request-class", "x-claude-code-context-compacted", "x-claude-code-compaction",
        "x-claude-code-prompt-id", "x-claude-code-session-id", "x-claude-code-agent-id",
        "x-claude-code-agent-type", "x-claude-code-parent-agent-id", "x-claude-code-prev-tool-durations")
COLUMNS = ("seq", "stub", "method", "path", "kind", "status", "host", "origin", "tools", "metadata_user_id",
           *HINT, "user-agent")


def texts(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]


def all_user_text(body: dict) -> str:
    return "\n".join(t for m in body.get("messages") or [] if m.get("role") == "user" for t in texts(m))


def first_user_text(body: dict) -> str:
    for m in body.get("messages") or []:
        if m.get("role") == "user":
            return "\n".join(texts(m))
    return ""


def has_tool_result(body: dict) -> bool:
    for m in body.get("messages") or []:
        content = m.get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return True
    return False


def scenario(body: dict) -> tuple[str, list[dict], str]:
    """The stub's reply to a Messages request: (kind, content blocks, stop reason)."""
    tools = {t.get("name") for t in body.get("tools") or []}
    first = first_user_text(body)
    if not tools:
        return "notools", [{"type": "text", "text": "<summary>stub summary of the conversation so far</summary>"}], "end_turn"
    messages = body.get("messages") or []
    if messages and "Respond with TEXT ONLY" in "\n".join(texts(messages[-1])):
        return "compact", [{"type": "text", "text": "<summary>stub summary of the conversation so far</summary>"}], "end_turn"
    if "SUB-TASK" in first:
        if not has_tool_result(body) and "Bash" in tools:
            block = {"type": "tool_use", "id": "toolu_sub_bash_1", "name": "Bash",
                     "input": {"command": "echo sub-work", "description": "sub work"}}
            return "sub", [block], "tool_use"
        return "sub", [{"type": "text", "text": "subagent result"}], "end_turn"
    if "MAIN-AGENT" in first and not has_tool_result(body):
        name = "Agent" if "Agent" in tools else "Task"
        block = {"type": "tool_use", "id": "toolu_main_agent_1", "name": name,
                 "input": {"description": "the agent", "prompt": "SUB-TASK do the work",
                           "subagent_type": "general-purpose"}}
        return "main", [block], "tool_use"
    return "main", [{"type": "text", "text": "hi from the stub"}], "end_turn"


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Recorder:
    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        self.counter = itertools.count(1)
        self.lock = threading.Lock()
        self.rows: list[dict] = []

    def save(self, stub: str, handler: BaseHTTPRequestHandler, raw: bytes, kind: str, status: int) -> None:
        try:
            body = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {"_raw": raw[:4000].decode("latin-1")}
        headers = [[k, v] for k, v in handler.headers.items()]
        lower = {k.lower(): v for k, v in headers}
        meta = body.get("metadata") if isinstance(body, dict) else None
        row = {"seq": 0, "stub": stub, "method": handler.command, "path": handler.path, "kind": kind,
               "status": status, "host": lower.get("host", "-"), "origin": lower.get("origin", "-"),
               "tools": len(body["tools"]) if isinstance(body, dict) and "tools" in body else "-",
               "metadata_user_id": (meta or {}).get("user_id", "-") if isinstance(meta, dict) else "-",
               **{h: lower.get(h, "-") for h in HINT}, "user-agent": lower.get("user-agent", "-")}
        with self.lock:
            row["seq"] = next(self.counter)
            self.rows.append(row)
            record = {"seq": row["seq"], "stub": stub, "time": time.time(), "request_line": handler.requestline,
                      "method": handler.command, "path": handler.path, "headers": headers, "reply_status": status,
                      "kind": kind, "body": body}
            name = f"{row['seq']:03d}-{handler.command}-{kind}.json"
            (self.dir / name).write_text(json.dumps(record, indent=1))
            with open(self.dir / "index.tsv", "w") as f:
                f.write("\t".join(COLUMNS) + "\n")
                for r in self.rows:
                    f.write("\t".join(str(r[c]) for c in COLUMNS) + "\n")


def make_handler(recorder: Recorder, stub: str, reply_mode: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        def _body(self) -> bytes:
            if "chunked" in (self.headers.get("transfer-encoding") or "").lower():
                out = b""
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        self.rfile.readline()
                        return out
                    out += self.rfile.read(size)
                    self.rfile.readline()
            return self.rfile.read(int(self.headers.get("content-length") or 0))

        def _json(self, status: int, obj: dict, head: bool = False) -> None:
            out = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0" if head else str(len(out)))
            self.end_headers()
            if not head:
                self.wfile.write(out)

        def _other(self) -> None:
            raw = self._body()
            bare = self.path.split("?")[0]
            if self.command == "HEAD":
                recorder.save(stub, self, raw, "head", 200)
                self._json(200, {}, head=True)
            elif self.command == "GET" and bare.endswith("/v1/models"):
                recorder.save(stub, self, raw, "models", 200)
                self._json(200, {"data": [], "has_more": False, "first_id": None, "last_id": None})
            else:
                recorder.save(stub, self, raw, "other", 404)
                self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub: no route"}})

        do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other  # noqa: N815

        def do_POST(self) -> None:  # noqa: N802
            raw = self._body()
            bare = self.path.split("?")[0]
            try:
                body = json.loads(raw or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {}
            if bare.endswith("/count_tokens") or bare.endswith("/count-tokens"):
                recorder.save(stub, self, raw, "count", 200)
                self._json(200, {"input_tokens": 100})
                return
            is_messages = bare.endswith("/v1/messages") or "/model/" in bare or "redict" in bare
            if not is_messages:
                recorder.save(stub, self, raw, "otherpost", 404)
                self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "stub: no route"}})
                return
            if reply_mode == "401":
                recorder.save(stub, self, raw, "messages", 401)
                self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "stub upstream"}})
                return
            kind, blocks, stop = scenario(body)
            recorder.save(stub, self, raw, kind, 200)
            usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0}
            message = {"id": f"msg_stub_{time.time_ns()}", "type": "message", "role": "assistant",
                       "model": body.get("model", "m"), "content": [], "stop_reason": None, "stop_sequence": None,
                       "usage": usage}
            if not body.get("stream"):
                self._json(200, {**message, "content": blocks, "stop_reason": stop})
                return
            chunks = [sse("message_start", {"type": "message_start", "message": message})]
            for i, block in enumerate(blocks):
                if block["type"] == "text":
                    chunks += [sse("content_block_start", {"type": "content_block_start", "index": i,
                                                           "content_block": {"type": "text", "text": ""}}),
                               sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                           "delta": {"type": "text_delta", "text": block["text"]}})]
                else:
                    chunks += [sse("content_block_start", {"type": "content_block_start", "index": i,
                                                           "content_block": {**block, "input": {}}}),
                               sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                           "delta": {"type": "input_json_delta",
                                                                     "partial_json": json.dumps(block["input"])}})]
                chunks.append(sse("content_block_stop", {"type": "content_block_stop", "index": i}))
            chunks += [sse("message_delta", {"type": "message_delta",
                                             "delta": {"stop_reason": stop, "stop_sequence": None},
                                             "usage": {"output_tokens": 10}}),
                       sse("message_stop", {"type": "message_stop"})]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for c in chunks:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(c), c))
            self.wfile.write(b"0\r\n\r\n")

    return Handler


def start_stub(recorder: Recorder, name: str, reply_mode: str) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(recorder, name, reply_mode))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("run")
    p.add_argument("--prefix", default="/tok_abc123", help="path appended to the stub base URL ('' for none)")
    p.add_argument("--base-via", choices=("env", "settings-flag", "config-settings"), default="env")
    p.add_argument("--hint", choices=("off", "env", "settings-flag", "config-settings", "zero"), default="off")
    p.add_argument("--bare", action="store_true")
    p.add_argument("--reply", choices=("401", "sse"), default="sse")
    p.add_argument("--mode", choices=("print", "streamjson", "pty"), default="print")
    p.add_argument("--prompt", action="append", default=[], help="repeat for several turns (streamjson, pty)")
    p.add_argument("--resume-prompt", action="append", default=[],
                   help="after the run, run again with --continue and this prompt (print mode)")
    p.add_argument("--env", action="append", default=[], help="extra K=V for the claude process")
    p.add_argument("--bedrock", action="store_true", help="CLAUDE_CODE_USE_BEDROCK=1 with a second local stub")
    p.add_argument("--flag-env", action="append", default=[], help="extra K=V for the --settings file's env block")
    p.add_argument("--cfg-env", action="append", default=[], help="extra K=V for <config dir>/settings.json's env")
    p.add_argument("--second", action="store_true",
                   help="start a second local stub, named 'provider'; {PORT2} in --env values is its port")
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=float, default=90)
    p.add_argument("--arg", action="append", default=[], help="extra argument for claude")
    return p.parse_args()


def run_claude(argv: list[str], env: dict, cwd: Path, log: Path, timeout: float, stdin_lines: list[str] | None,
               stop_when=None) -> int | None:
    profile = OUT / "localonly.sb"
    profile.write_text(SANDBOX_PROFILE)
    with open(log, "ab") as out:
        out.write(("\n$ " + " ".join(argv) + "\n").encode())
        out.flush()
        proc = subprocess.Popen(["/usr/bin/sandbox-exec", "-f", str(profile), *argv], cwd=cwd, env=env,
                                stdin=subprocess.PIPE if stdin_lines is not None else subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        if stdin_lines is not None:
            for line in stdin_lines:
                proc.stdin.write((line + "\n").encode())
                proc.stdin.flush()
                time.sleep(float(os.environ.get("D70_TURN_GAP", "8")))
            proc.stdin.close()
        while time.monotonic() < deadline and proc.poll() is None:
            if stop_when is not None and stop_when():
                time.sleep(1.0)
                break
            time.sleep(0.2)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return proc.returncode


def run_pty(argv: list[str], env: dict, cwd: Path, log: Path, prompts: list[str], recorder: Recorder,
            timeout: float) -> None:
    profile = OUT / "localonly.sb"
    profile.write_text(SANDBOX_PROFILE)
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execve("/usr/bin/sandbox-exec", ["/usr/bin/sandbox-exec", "-f", str(profile), *argv], env)
    screen = open(log, "ab")

    def pump(seconds: float) -> None:
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

    def settled(quiet: float, limit: float) -> None:
        """Pump until no new request has arrived for ``quiet`` seconds, at most ``limit`` seconds."""
        end = time.time() + limit
        seen, since = len(recorder.rows), time.time()
        while time.time() < end:
            pump(0.5)
            if len(recorder.rows) != seen:
                seen, since = len(recorder.rows), time.time()
            elif time.time() - since >= quiet:
                return

    pump(8)
    for prompt in prompts:
        for ch in prompt:
            os.write(fd, ch.encode())
            pump(0.03)
        pump(0.5)
        os.write(fd, b"\r")
        settled(8, timeout)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    pump(1)
    screen.close()


def main() -> None:
    args = parse_args()
    run_dir = OUT / args.run
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    recorder = Recorder(run_dir)
    main_stub = start_stub(recorder, "base", args.reply)
    base = f"http://127.0.0.1:{main_stub.server_port}{args.prefix}"
    scratch = Path(tempfile.mkdtemp(prefix=f"d70-{args.run}-"))
    home, cfg, work = scratch / "home", scratch / "config", scratch / "work"
    for d in (home, cfg, work):
        d.mkdir()
    work = work.resolve()
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm-256color",
           "CLAUDE_CONFIG_DIR": str(cfg), "ANTHROPIC_API_KEY": KEY, "DISABLE_AUTOUPDATER": "1"}
    flag_settings: dict = {"env": {}}
    config_settings: dict = {"env": {}}
    where = {"env": env, "settings-flag": flag_settings["env"], "config-settings": config_settings["env"]}
    where[args.base_via]["ANTHROPIC_BASE_URL"] = base
    if args.hint == "zero":
        env["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] = "0"
    elif args.hint != "off":
        where[args.hint]["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] = "1"
    second = None
    if args.bedrock:
        second = start_stub(recorder, "bedrock", args.reply)
        env.update({"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_ACCESS_KEY_ID": "fake", "AWS_SECRET_ACCESS_KEY": "fake",
                    "AWS_REGION": "us-east-1",
                    "ANTHROPIC_BEDROCK_BASE_URL": f"http://127.0.0.1:{second.server_port}/bedrock_prefix"})
    if args.second:
        second = start_stub(recorder, "provider", args.reply)
    for target, pairs in ((env, args.env), (flag_settings["env"], args.flag_env), (config_settings["env"], args.cfg_env)):
        for pair in pairs:
            k, _, v = pair.partition("=")
            v = v.replace("{BASE}", base).replace("{PORT}", str(main_stub.server_port))
            target[k] = v.replace("{PORT2}", str(second.server_port)) if second else v
    claude = os.environ.get("CLAUDE") or str(Path.home() / ".local/bin/claude")
    argv = [claude]
    if args.bare:
        argv.append("--bare")
    if flag_settings["env"] or args.bare:
        path = scratch / "flag-settings.json"
        path.write_text(json.dumps(flag_settings))
        argv += ["--settings", str(path)]
    if config_settings["env"]:
        (cfg / "settings.json").write_text(json.dumps(config_settings))
    if args.model:
        argv += ["--model", args.model]
    argv += args.arg
    log = run_dir / "claude-output.log"
    setup = {"run": args.run, "claude": claude, "base_url": base, "base_via": args.base_via, "hint": args.hint,
             "bare": args.bare, "reply": args.reply, "mode": args.mode, "prompts": args.prompt,
             "env": {k: v for k, v in env.items() if k != "PATH"}, "flag_settings": flag_settings,
             "config_settings": config_settings, "scratch": str(scratch)}
    prompts = args.prompt or ["say hi"]
    if args.mode == "print":
        got_messages = (lambda: any(r["kind"] == "messages" for r in recorder.rows)) if args.reply == "401" else None
        rc = run_claude([*argv, "-p", "--output-format", "json", prompts[0]], env, work, log, args.timeout, None,
                        got_messages)
        setup["exit"] = [rc]
        for prompt in args.resume_prompt:
            rc = run_claude([*argv, "-p", "--continue", "--output-format", "json", prompt], env, work, log,
                            args.timeout, None)
            setup["exit"].append(rc)
    elif args.mode == "streamjson":
        lines = [json.dumps({"type": "user", "message": {"role": "user", "content": p}}) for p in prompts]
        rc = run_claude([*argv, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
                        env, work, log, args.timeout, lines)
        setup["exit"] = [rc]
    else:
        (cfg / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True, "theme": "dark", "numStartups": 5,
            "customApiKeyResponses": {"approved": [KEY[-20:]], "rejected": []},
            "bypassPermissionsModeAccepted": True,
            "projects": {str(work): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
        run_pty([*argv, "--dangerously-skip-permissions"], env, work, log, prompts, recorder, args.timeout)
    time.sleep(1.0)
    main_stub.shutdown()
    if second:
        second.shutdown()
    setup["argv"] = argv
    setup["requests"] = len(recorder.rows)
    (run_dir / "setup.json").write_text(json.dumps(setup, indent=1))
    paths: dict[str, int] = {}
    for r in recorder.rows:
        key = f"{r['stub']} {r['method']} {r['path']}"
        paths[key] = paths.get(key, 0) + 1
    print(f"{args.run}: {len(recorder.rows)} requests, exit {setup.get('exit')}, base {base} via {args.base_via}")
    for key, n in paths.items():
        print(f"  {n:3d}  {key}")
    shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
