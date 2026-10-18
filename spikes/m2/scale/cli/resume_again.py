#!/usr/bin/env python3
"""Resume a kept run_scale.py session N more times (contexto-like usage) and report how the
session file and the request change each time. Needs a run made with --keep.

Usage: resume_again.py <run_dir> [times=2]
"""
import glob
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness  # noqa: E402

root = Path(sys.argv[1]).resolve()
times = int(sys.argv[2]) if len(sys.argv) > 2 else 2
f = Path(glob.glob(str(root / "config/projects/*/*.jsonl"))[0])
for i in range(2, 2 + times):
    before = f.stat().st_size
    port = harness.free_port()
    log = root / f"upstream-resume{i}.jsonl"
    log.unlink(missing_ok=True)
    up = harness.start_upstream(HERE, port, log, "--mode", "contexto", "--delay", "1.5")
    try:
        res = harness.run_claude(["-p", "continue", "--resume", f.stem, "--model", "claude-opus-5-5",
                                  "--output-format", "json"],
                                 root / "cwd", harness.claude_env(root, port), root / f"claude-resume{i}", timeout=600)
    finally:
        up.terminate()
        up.wait()
    reqs = [r for r in harness.read_log(log) if r.get("path", "").endswith("/v1/messages")]
    t0 = res["t_launch"]
    print(json.dumps({"resume": i, "file_mb_before": round(before / 2**20, 1),
                      "file_mb_after": round(f.stat().st_size / 2**20, 1),
                      "requests": [{"messages": r["messages"], "body_mb": round(r["body_bytes"] / 2**20, 2),
                                    "compaction": r["compaction"], "t": round(r["t_start"] - t0, 2)} for r in reqs],
                      "exit": res["exit"], "maxrss_mb": res["maxrss_mb"],
                      "non_loopback": len(res["non_loopback"]), "sockets": res["sockets"]}), flush=True)
