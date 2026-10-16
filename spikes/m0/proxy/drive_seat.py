"""Drive an OpenRig test seat through the measuring proxy, one synthetic turn at a time.

    python3 drive_seat.py --session lab-subject@contexto --proxy http://127.0.0.1:18451

The seat's model routing points at a long-lived proxy.py (set up by the manager, not by this
script). Each turn: label it on the proxy's control endpoint, deliver the prompt with
`rig send`, then wait on the proxy's own status until the seat's last model call ends the turn
and traffic stays quiet. Results come from the proxy log, never from screen captures.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from run_session import PROMPTS

SCRATCH = ".contexto/spikes/L1-a-lab"
SEED_SCRIPT = Path(__file__).resolve().parent / "run_session.py"


def control(proxy: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(proxy + "/__m0proxy/" + path, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def last_stop_reason(requests_log: Path) -> str | None:
    try:
        lines = requests_log.read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        record = json.loads(line)
        if record.get("endpoint", "").endswith("/messages"):
            return record.get("stop_reason")
    return None


def wait_turn_end(proxy: str, requests_log: Path, requests_before: int, quiet_s: float,
                  timeout_s: float) -> dict[str, Any]:
    """Done when at least one new request finished with end_turn and nothing moved for quiet_s."""
    deadline = time.monotonic() + timeout_s
    last_change, last_seen = time.monotonic(), None
    while time.monotonic() < deadline:
        status = control(proxy, "status")
        marker = (status["requests"], status["inflight"], status["spent"])
        if marker != last_seen:
            last_seen, last_change = marker, time.monotonic()
        settled = time.monotonic() - last_change >= quiet_s
        if (settled and status["inflight"] == 0 and status["requests"] > requests_before
                and last_stop_reason(requests_log) == "end_turn"):
            return status
        if status["refused"]:
            return status
        time.sleep(2)
    return dict(control(proxy, "status"), timed_out=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session", required=True)
    parser.add_argument("--proxy", default="http://127.0.0.1:18451")
    parser.add_argument("--turns", type=int, default=len(PROMPTS))
    parser.add_argument("--first", type=int, default=0, help="turn to start from; 0 runs the seeding turn")
    parser.add_argument("--budget", type=int, default=450_000)
    parser.add_argument("--quiet", type=float, default=12.0, help="seconds without traffic that end a turn")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--proxy-dir", type=Path, required=True, help="the running proxy's --out directory")
    args = parser.parse_args(argv)

    prompts = [(0, f"[M0 synthetic setup] Run `python3 {SEED_SCRIPT} seed {SCRATCH}` from your working "
                   f"directory and reply with its one-line output. Do not commit anything.")]
    prompts += [(i, f"[M0 synthetic task {i}; work only inside {SCRATCH}; never commit] {p}")
                for i, p in enumerate(PROMPTS[: args.turns], start=1)]
    turn_sizes: list[tuple[int, int]] = []  # (tokens, requests) per finished turn
    for turn, prompt in prompts:
        if turn < args.first:
            continue
        before = control(args.proxy, "status")
        if turn_sizes:
            per_request = turn_sizes[-1][0] / max(turn_sizes[-1][1], 1)
            avg_requests = sum(r for _, r in turn_sizes) / len(turn_sizes)
            projected = before["spent"] + per_request * 1.3 * avg_requests
            if projected > args.budget:
                note(args.proxy_dir / "turns.jsonl", {"turn": turn, "stopped": "budget", "spent": before["spent"], "projected": int(projected)})
                break
        control(args.proxy, "turn", {"turn": turn})
        sent = subprocess.run(["rig", "send", args.session, prompt], capture_output=True, text=True)
        if sent.returncode != 0:
            note(args.proxy_dir / "turns.jsonl", {"turn": turn, "stopped": "rig send failed", "returncode": sent.returncode})
            break
        after = wait_turn_end(args.proxy, args.proxy_dir / "requests.jsonl", before["requests"], args.quiet, args.timeout)
        tokens, requests = after["spent"] - before["spent"], after["requests"] - before["requests"]
        turn_sizes.append((tokens, requests))
        line = {"turn": turn, "requests": requests, "turn_tokens": tokens, "cumulative_tokens": after["spent"],
                "timed_out": after.get("timed_out", False), "refused": after["refused"]}
        note(args.proxy_dir / "turns.jsonl", line)
        if line["timed_out"] or after["refused"]:
            break
    control(args.proxy, "turn", {"turn": None})
    return 0


def note(path: Path, line: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, sort_keys=True) + "\n")
    print(json.dumps(line, sort_keys=True), flush=True)


if __name__ == "__main__":
    sys.exit(main())
