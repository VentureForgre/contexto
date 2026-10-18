#!/usr/bin/env python3
"""Drive a long, feature-heavy Claude Code session against scripted_model.py. Zero model spend.

Two modes:
  resume  one `claude -p` process per turn: turn 1 `--session-id`, later turns `--resume <id>`,
          every fifth turn `--continue`; plan mode and effort set with per-launch flags.
  stream  one `claude -p --input-format stream-json` process for the whole session; plan mode
          toggled with the `set_permission_mode` control request and effort with
          `apply_flag_settings {effortLevel}`.

Each turn's prompt carries a STEP[...] script (see scripted_model.py) so the fake model makes the
CLI use TodoWrite, Read, Edit, Write, Bash, EnterPlanMode/ExitPlanMode and an MCP tool (Contexto's
own `contexto mcp`, temp home). Between some turns the harness edits files the session has read or
written (file-modified reminders). The project has a CLAUDE.md, a nested sub/CLAUDE.md and a
project skill.

Isolation: spikes/m2/scale/cli/harness.py (scrubbed env, temp HOME and CLAUDE_CONFIG_DIR, fake key,
ANTHROPIC_BASE_URL on 127.0.0.1, nonessential traffic and telemetry off, HTTPS_PROXY to the fake
server which refuses CONNECT). lsof samples every process in the CLI's tree for the whole run; any
non-loopback socket kills the run and is reported.

Usage: run_session.py --mode resume|stream --turns 48 --root /tmp/rem-xxx
Writes <root>/model.jsonl, <root>/bodies/NNNNN.json, <root>/turns.jsonl, <root>/monitor.json.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(HERE.parent / "cli"))
from harness import (CLAUDE_BIN, claude_env, free_port, inet_sockets, non_loopback,  # noqa: E402
                     run_claude, tree_pids)

MODEL = "claude-opus-5-5"  # overridden by --model
# Task tools (TaskCreate/TaskUpdate/TaskList in 2.1.302; TodoWrite is disabled on every model measured,
# so its action is skipped): created on turn 1, updated on 7 and 13, one more created on 19, then left
# stale with two open tasks to the end. On models without task tools the actions are skipped.
TASKS = {1: ["taskcreate=1", "taskcreate=2", "taskcreate=3", "taskupdate=1:in_progress", "todo=ip,p,p"],
         7: ["taskupdate=1:completed", "taskupdate=2:in_progress"],
         13: ["taskupdate=2:completed", "taskupdate=3:in_progress"],
         19: ["taskcreate=4", "tasklist"]}


PLAIN = False  # --plain: every turn is a bare prompt (no tools, no file changes, no mode or effort change)


def plan(t: int) -> dict:
    """The scripted content of turn t (1-based): a 12-turn cycle of features."""
    if PLAIN:
        return {"turn": t, "acts": [], "mode": "bypassPermissions", "effort": None, "ext": [],
                "prompt": f"Turn {t}. Synthetic user request number {t}."}
    c = (t - 1) % 12
    cyc = (t - 1) // 12
    acts, mode, effort, ext = [], "bypassPermissions", None, []
    todo = TASKS.get(t, [])
    if c == 0:
        acts = todo + ["read=data.txt", "read=notes.md"]
    elif c == 1:
        acts = ["edit=notes.md"]
    elif c == 2:
        acts, ext = ["read=sub/inner.txt"], ["data.txt"]
    elif c == 3:
        acts = [f"write=out/f{t}.txt"]
    elif c == 5:
        acts, effort = ["bash=ls"], "high"
    elif c == 6:
        acts, ext = todo + ["edit=data.txt"], ["notes.md"]
    elif c == 7:
        effort = "low"
    elif c == 8:
        acts, mode = (["enterplan"] if cyc % 2 else []) + ["read=data.txt"], "plan"
    elif c == 9:
        acts, mode = ["exitplan"], "plan"
    elif c == 10:
        acts, ext = ["tool=mcp__contexto__contexto_state"], ["data.txt", "sub/inner.txt", f"out/f{t - 7}.txt"]
    elif c == 11:
        acts = [f"read=out/f{t - 8}.txt"]
    step = f" STEP[{'|'.join(acts)}]" if acts else ""
    return {"turn": t, "acts": acts, "mode": mode, "effort": effort, "ext": ext,
            "prompt": f"Turn {t}. Synthetic user request number {t}.{step}"}


def setup_project(root: Path) -> Path:
    proj = root / "project"
    (proj / "sub").mkdir(parents=True, exist_ok=True)
    (proj / "out").mkdir(exist_ok=True)
    (proj / ".claude/skills/synthetic-notes").mkdir(parents=True, exist_ok=True)
    (proj / "CLAUDE.md").write_text("# Synthetic project rules\n\n" + "".join(
        f"- Synthetic rule {i}: keep files short and end them with END_OF_FILE.\n" for i in range(12)))
    (proj / "sub/CLAUDE.md").write_text("# Nested rules for sub/\n\n- Files in sub/ are synthetic fixtures.\n")
    (proj / ".claude/skills/synthetic-notes/SKILL.md").write_text(
        "---\nname: synthetic-notes\ndescription: Use when editing notes.md in the synthetic project.\n---\n\n"
        "Keep notes.md short.\n")
    for rel in ("data.txt", "notes.md", "sub/inner.txt"):
        (proj / rel).write_text(f"synthetic {rel} line 1\nEND_OF_FILE\n")
    for t in range(1, 60):
        (proj / f"out/f{t}.txt").write_text(f"placeholder {t}\nEND_OF_FILE\n")
    return proj


def external_edit(proj: Path, rel: str, t: int) -> None:
    p = proj / rel
    old = p.read_text() if p.exists() else "END_OF_FILE\n"
    p.write_text(f"external change before turn {t}\n" + old)


def mcp_config(root: Path) -> Path:
    cfg = {"mcpServers": {"contexto": {"command": str(REPO / ".venv/bin/contexto"),
                                       "args": ["mcp", "--session", "spike-reminders",
                                                "--home", str(root / "contexto_home")]}}}
    p = root / "mcp.json"
    p.write_text(json.dumps(cfg))
    return p


def start_model(root: Path, proj: Path, port: int) -> subprocess.Popen:
    err = open(root / "model.err", "w")
    p = subprocess.Popen(["python3", "-I", str(HERE / "scripted_model.py"), str(port), str(root / "model.jsonl"),
                          str(root / "bodies"), str(proj)], stdout=err, stderr=err)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.05)
    p.kill()
    raise RuntimeError("scripted model did not start")


def run_resume(root: Path, proj: Path, env: dict, mcp: Path, turns: int) -> list[dict]:
    sid = str(uuid.uuid4())
    out = []
    (root / "logs").mkdir(exist_ok=True)
    for t in range(1, turns + 1):
        p = plan(t)
        for rel in p["ext"]:
            external_edit(proj, rel, t)
        how = "session-id" if t == 1 else ("continue" if t % 5 == 0 else "resume")
        args = ["-p", "--model", MODEL, "--permission-mode", p["mode"], "--mcp-config", str(mcp),
                "--strict-mcp-config"]
        args += ["--session-id", sid] if how == "session-id" else (["--continue"] if how == "continue"
                                                                    else ["--resume", sid])
        if p["effort"]:
            args += ["--effort", p["effort"]]
        args.append(p["prompt"])
        t0 = time.time()
        res = run_claude(args, proj, env, root / "logs" / f"turn{t:03d}", timeout=180)
        rec = {**p, "how": how, "t0": t0, "t1": time.time(), "exit": res["exit"],
               "non_loopback": res["non_loopback"], "sockets": res["sockets"], "lsof_samples": res["lsof_samples"]}
        out.append(rec)
        print(f"turn {t} {how} mode={p['mode']} effort={p['effort']} exit={res['exit']} "
              f"lsof={res['lsof_samples']} bad={len(res['non_loopback'])}", flush=True)
        if res["non_loopback"]:
            raise SystemExit(f"non-loopback socket seen on turn {t}: stopping")
    return out


def run_stream(root: Path, proj: Path, env: dict, mcp: Path, turns: int) -> list[dict]:
    sid = str(uuid.uuid4())
    args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--model", MODEL, "--permission-mode", "bypassPermissions", "--mcp-config", str(mcp),
            "--strict-mcp-config", "--session-id", sid]
    (root / "logs").mkdir(exist_ok=True)
    proc = subprocess.Popen([CLAUDE_BIN, *args], cwd=proj, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=open(root / "logs/stream.stderr", "w"),
                            start_new_session=True, text=True, bufsize=1)
    events: list[dict] = []
    cond = threading.Condition()
    sockets: set[str] = set()
    bad: list[str] = []
    samples = [0]

    def reader():
        with open(root / "logs/stream.stdout", "w") as f:
            for line in proc.stdout:
                f.write(line)
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                resp = ev.get("response") if isinstance(ev.get("response"), dict) else {}
                with cond:
                    events.append({"type": ev.get("type"), "subtype": resp.get("subtype") or ev.get("subtype"),
                                   "request_id": resp.get("request_id"), "error": resp.get("error")})
                    cond.notify_all()

    def sock_loop():
        while proc.poll() is None:
            lines = inet_sockets(tree_pids(proc.pid))
            samples[0] += 1
            for ln in lines:
                f = ln.split()
                sockets.add(" ".join(f[7:]) if len(f) > 7 else ln)
            b = non_loopback(lines)
            if b:
                bad.extend(b)
                os.killpg(proc.pid, signal.SIGKILL)
                return

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=sock_loop, daemon=True).start()

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def wait_until(pred, timeout) -> bool:
        end = time.time() + timeout
        with cond:
            while not pred():
                left = end - time.time()
                if left <= 0 or proc.poll() is not None:
                    return False
                cond.wait(min(left, 1))
        return True

    def control(req: dict) -> str:
        rid = f"ctl-{uuid.uuid4().hex[:8]}"
        send({"type": "control_request", "request_id": rid, "request": req})
        got = lambda: [e for e in events if e["type"] == "control_response" and e["request_id"] == rid]  # noqa: E731
        if not wait_until(lambda: bool(got()), 20):
            return "no response"
        e = got()[0]
        return e["subtype"] + (f": {e['error']}" if e.get("error") else "")

    out, mode, effort = [], "bypassPermissions", "medium"
    for t in range(1, turns + 1):
        p = plan(t)
        for rel in p["ext"]:
            external_edit(proj, rel, t)
        ctl = []
        if p["mode"] != mode:
            ctl.append(("set_permission_mode " + p["mode"],
                        control({"subtype": "set_permission_mode", "mode": p["mode"]})))
            mode = p["mode"]
        want = p["effort"] or "medium"
        if want != effort:
            ctl.append(("effortLevel " + want,
                        control({"subtype": "apply_flag_settings", "settings": {"effortLevel": want}})))
            effort = want
        n_results = lambda: sum(1 for e in events if e["type"] == "result")  # noqa: E731
        with cond:
            before = n_results()
        t0 = time.time()
        send({"type": "user", "message": {"role": "user", "content": p["prompt"]}, "parent_tool_use_id": None,
              "session_id": sid})
        ok = wait_until(lambda: n_results() > before, 180)
        out.append({**p, "how": "stream", "t0": t0, "t1": time.time(), "ok": ok, "controls": ctl})
        print(f"turn {t} stream mode={mode} effort={effort} ok={ok} ctl={ctl} lsof={samples[0]} bad={len(bad)}",
              flush=True)
        if bad:
            raise SystemExit(f"non-loopback socket seen on turn {t}: stopping")
        if not ok:
            break
    proc.stdin.close()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(10)
    (root / "monitor.json").write_text(json.dumps({"sockets": sorted(sockets), "non_loopback": bad,
                                                   "lsof_samples": samples[0], "exit": proc.returncode}, indent=1))
    return out


def main() -> None:
    global MODEL, PLAIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("resume", "stream"), required=True)
    ap.add_argument("--turns", type=int, default=48)
    ap.add_argument("--root", required=True)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE added to the CLI env")
    ap.add_argument("--plain", action="store_true", help="bare prompts only: the per-turn floor")
    a = ap.parse_args()
    MODEL, PLAIN = a.model, a.plain
    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    proj = setup_project(root)
    port = free_port()
    srv = start_model(root, proj, port)
    turns: list[dict] = []
    try:
        env = claude_env(root, port, dict(kv.split("=", 1) for kv in a.env))
        mcp = mcp_config(root)
        turns = (run_resume if a.mode == "resume" else run_stream)(root, proj, env, mcp, a.turns)
    finally:
        srv.terminate()
        srv.wait(5)
        with open(root / "turns.jsonl", "w") as f:
            for r in turns:
                f.write(json.dumps(r) + "\n")
    if a.mode == "resume":
        (root / "monitor.json").write_text(json.dumps({
            "sockets": sorted({s for r in turns for s in r["sockets"]}),
            "non_loopback": [b for r in turns for b in r["non_loopback"]],
            "lsof_samples": sum(r["lsof_samples"] for r in turns)}, indent=1))


if __name__ == "__main__":
    main()
