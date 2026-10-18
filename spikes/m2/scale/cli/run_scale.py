#!/usr/bin/env python3
"""Resume synthetic sessions of increasing size in an isolated Claude Code and measure it.

For each size: write a session with gen_session.py into a fresh temp CLAUDE_CONFIG_DIR, start
the fake upstream, run `claude -p <prompt> --resume <id>`, and record whether a request was
sent, launch-to-first-request time, request body bytes, request and compaction counts,
count_tokens calls, peak RSS of the process tree, exit status, errors, session file size before
and after, inet sockets seen, refused proxy attempts, and the 1-minute load average.

Usage: run_scale.py <work_root> <csv> --mode contexto|honest --sizes 500000,2000000,...
       [--model claude-opus-5-5] [--tool-loop N] [--timeout S] [--keep] [--label L]
       [--window W] [--delay S] [--env K=V]
Prints one summary line per size; the full row goes to the CSV and <run>/result.json.
"""
import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness  # noqa: E402

FIELDS = ["label", "model", "mode", "tool_loop", "target_tokens", "session_tokens", "planted_usage", "turns",
          "session_messages", "session_file_mb_before", "session_file_mb_after", "load1_before",
          "sent_request", "launch_to_first_req_s", "first_req_upload_s", "first_req_body_mb",
          "first_req_messages", "first_req_honest_tokens", "max_req_body_mb", "messages_requests",
          "count_tokens_calls", "compaction_requests", "other_requests", "wall_s", "exit",
          "timed_out", "is_error", "cli_maxrss_mb", "peak_tree_rss_mb", "peak_root_rss_mb", "compact_boundary_after",
          "non_loopback", "refused_proxy", "lsof_samples", "sockets_seen", "appended_kb",
          "result_head", "stderr_tail"]


def load1() -> float:
    out = subprocess.run(["uptime"], capture_output=True, text=True).stdout
    m = re.search(r"load averages?: ([\d.]+)", out)
    return float(m.group(1)) if m else -1.0


def one(work: Path, a, size: int) -> dict:
    label = f"{a.label}-{a.mode}-{a.model.replace('claude-', '')}-tl{a.tool_loop}-{size}"
    root = work / label
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    cwd = root / "cwd"
    cwd.mkdir()
    cwd = cwd.resolve()
    read_file = cwd / "notes.txt"
    read_file.write_text("".join(f"line {i} of a harmless temp file\n" for i in range(200)))
    port = harness.free_port()
    env = harness.claude_env(root, port, a.extra_env)
    gen = subprocess.run(["python3", "-I", str(HERE / "gen_session.py"), str(root / "config/projects"), str(cwd),
                          str(size), "--usage", a.mode], capture_output=True, text=True, check=True)
    g = json.loads(gen.stdout)
    spath = Path(g["path"])
    before = spath.stat().st_size
    log = root / "upstream.jsonl"
    up_args = ["--mode", a.mode, "--tool-loop", str(a.tool_loop), "--read-path", str(read_file)]
    if a.window:
        up_args += ["--window", str(a.window)]
    if a.delay:
        up_args += ["--delay", str(a.delay)]
    up = harness.start_upstream(HERE, port, log, *up_args)
    l1 = load1()
    try:
        args = ["-p", a.prompt, "--resume", g["session_id"], "--model", a.model, "--output-format", "json",
                "--allowedTools", "Read"]
        res = harness.run_claude(args, cwd, env, root / "claude", timeout=a.timeout)
    finally:
        up.terminate()
        up.wait()
    recs = harness.read_log(log)
    msgs = [r for r in recs if r.get("path", "").endswith("/v1/messages") and r.get("method") == "POST"]
    ct = [r for r in recs if r.get("path", "").endswith("/count_tokens")]
    refused = [r for r in recs if r.get("refused")]
    other = [r for r in recs if r not in msgs and r not in ct and r not in refused]
    first = min(msgs, key=lambda r: r["t_start"]) if msgs else None
    after = spath.stat().st_size if spath.exists() else 0
    cb = 0
    appended: dict[str, int] = {}  # bytes the CLI appended to the session file, by record type
    if spath.exists():
        with open(spath, "rb") as f:
            for line in f:
                if b"compact_boundary" in line or b"isCompactSummary" in line:
                    cb += 1
            f.seek(before)
            for line in f:
                try:
                    t = json.loads(line).get("type", "?")
                except ValueError:
                    t = "unparsed"
                appended[t] = appended.get(t, 0) + len(line)
    out = (root / "claude.stdout").read_text(errors="replace")
    err = (root / "claude.stderr").read_text(errors="replace")
    err = re.sub(r"\s*[\d.]+ real\s+[\d.]+ user.*", "", err, flags=re.S)  # drop /usr/bin/time report
    is_error, head = "", out.strip()[:160]
    try:
        j = json.loads(out)
        is_error = j.get("is_error")
        head = str(j.get("result") or j.get("subtype") or "")[:160]
    except ValueError:
        pass
    row = {
        "label": label, "model": a.model, "mode": a.mode, "tool_loop": a.tool_loop, "target_tokens": size,
        "session_tokens": g["tokens"], "planted_usage": g["planted_usage"], "turns": g["turns"], "session_messages": g["messages"],
        "session_file_mb_before": round(before / 2**20, 1), "session_file_mb_after": round(after / 2**20, 1),
        "load1_before": l1, "sent_request": bool(msgs),
        "launch_to_first_req_s": round(first["t_start"] - res["t_launch"], 2) if first else "",
        "first_req_upload_s": round(first["t"] - first["t_start"], 2) if first else "",
        "first_req_body_mb": round(first["body_bytes"] / 2**20, 2) if first else "",
        "first_req_messages": first["messages"] if first else "",
        "first_req_honest_tokens": first["honest_tokens"] if first else "",
        "max_req_body_mb": round(max(r["body_bytes"] for r in msgs) / 2**20, 2) if msgs else "",
        "messages_requests": len(msgs), "count_tokens_calls": len(ct),
        "compaction_requests": sum(1 for r in msgs if r.get("compaction")), "other_requests": len(other),
        "wall_s": res["wall_s"], "exit": res["exit"], "timed_out": res["timed_out"] or ("rss_cap" if res["rss_capped"] else False), "is_error": is_error,
        "cli_maxrss_mb": res["maxrss_mb"], "peak_tree_rss_mb": res["peak_tree_rss_mb"], "peak_root_rss_mb": res["peak_root_rss_mb"],
        "compact_boundary_after": cb, "non_loopback": len(res["non_loopback"]), "refused_proxy": len(refused),
        "lsof_samples": res["lsof_samples"], "sockets_seen": len(res["sockets"]),
        "appended_kb": json.dumps({k: v // 1024 for k, v in sorted(appended.items())}),
        "result_head": head.replace("\n", " "), "stderr_tail": err.strip()[-200:].replace("\n", " "),
    }
    (root / "result.json").write_text(json.dumps({"row": row, "requests": [
        {k: r.get(k) for k in ("t_start", "t", "path", "body_bytes", "messages", "model", "max_tokens",
                               "honest_tokens", "reported_input", "compaction", "reply", "status", "tools")}
        for r in msgs + ct], "refused": refused, "monitor": res}, indent=1))
    if not a.keep:
        spath.unlink(missing_ok=True)
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("csv")
    ap.add_argument("--mode", choices=("contexto", "honest"), required=True)
    ap.add_argument("--sizes", required=True)
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--tool-loop", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--window", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.5,
                    help="fake upstream holds each reply this long so lsof sees the socket")
    ap.add_argument("--prompt", default="continue")
    ap.add_argument("--label", default="r")
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE added to the CLI env")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    a.extra_env = dict(e.split("=", 1) for e in a.env)
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    csv_path = Path(a.csv)
    new = not csv_path.exists()
    for size in [int(float(s)) for s in a.sizes.split(",")]:
        row = one(work, a, size)
        with open(csv_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
                new = False
            w.writerow(row)
        print(json.dumps({k: row[k] for k in ("label", "sent_request", "launch_to_first_req_s", "first_req_body_mb",
                                              "messages_requests", "compaction_requests", "count_tokens_calls",
                                              "wall_s", "exit", "cli_maxrss_mb", "peak_tree_rss_mb", "non_loopback", "sockets_seen",
                                              "refused_proxy", "result_head")}), flush=True)
        if row["non_loopback"]:
            print("STOP: non-loopback connection seen", flush=True)
            sys.exit(3)


if __name__ == "__main__":
    main()
