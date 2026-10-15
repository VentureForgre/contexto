"""Send one L2d turn to the test seat, wait for it in the seat's session JSONL, then check it.

    python3 drive_l2d.py TURN_ID --session-file S.jsonl --plan DIR --log RUN.jsonl

DIR holds builder-4's turns.jsonl and budget.md. One invocation sends one turn, so the operator
acts between turns (gates, moving growth files out before a probe). The turn text is sent exactly
as given. Only numbers, tool names, and stop lines are written to RUN.jsonl.

Before sending: STOP unless cumulative + factor x this turn's budget <= limit, and for G2 also
cumulative + g2_reserve < limit. After the turn: STOP on cost over factor x budget, serial reads
in a growth turn, any tool other than one Write in a probe turn, a compaction marker, or a sharp
context drop. A reply over 100 output tokens is noted, not stopped.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sessionlog import EXIT_STOP, check_turn, context_tokens, gate, read_session, status

POLL_S = 3.0


def turn_budgets(budget_md: Path) -> dict[str, int]:
    """Sum context + output per turn name from the budget sheet's main table."""
    budgets: dict[str, int] = {}
    for line in budget_md.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 5 and cells[0].isdigit() and re.fullmatch(r"[A-Z]\d", cells[1]):
            budgets[cells[1]] = budgets.get(cells[1], 0) + int(cells[2].replace(",", "")) + int(cells[3].replace(",", ""))
    return budgets


def wait_for_turn(session_file: Path, turns_before: int, quiet_s: float, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_size, last_change = -1, time.monotonic()
    while time.monotonic() < deadline:
        size = session_file.stat().st_size
        if size != last_size:
            last_size, last_change = size, time.monotonic()
        session = read_session(session_file)
        calls = session["calls"]
        if (session["turns"] > turns_before and calls and calls[-1]["turn"] == session["turns"]
                and calls[-1].get("stop_reason") == "end_turn" and time.monotonic() - last_change >= quiet_s):
            return session
        time.sleep(POLL_S)
    raise TimeoutError(f"turn did not finish within {timeout_s:.0f} s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("turn_id")
    parser.add_argument("--session", default="lab-subject@contexto")
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=1_900_000)
    parser.add_argument("--factor", type=float, default=1.5)
    parser.add_argument("--g2-reserve", type=int, default=822_000)
    parser.add_argument("--quiet", type=float, default=10.0)
    parser.add_argument("--raw", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--probe-calls", type=int, default=2, help="measured calls per probe turn (from P1)")
    parser.add_argument("--timeout", type=float, default=1500.0)
    args = parser.parse_args(argv)

    turns = {t["id"]: t for t in map(json.loads, (args.plan / "turns.jsonl").read_text().splitlines())}
    turn = turns[args.turn_id]
    budget = turn_budgets(args.plan / "budget.md")[args.turn_id]
    files = turn["files"] if isinstance(turn["files"], list) else []

    def log(line: dict[str, Any]) -> None:
        line = {"turn_id": args.turn_id, **line}
        with args.log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
        print(json.dumps(line, sort_keys=True), flush=True)

    before = status(read_session(args.session_file))
    if args.turn_id == "P2":
        # Agreed with manager-1 and manager-2: P2 is gated on measured size, not the plan:
        # context after G2 x measured calls per probe turn x 1.1.
        measured = before["last_context_tokens"] * args.probe_calls
        ok, line = gate(before, measured, args.limit, 1.1)
        line += f" [P2 gate: context {before['last_context_tokens']:,} x {args.probe_calls} calls]"
    else:
        ok, line = gate(before, budget, args.limit, args.factor)
    if ok and args.turn_id == "G2" and before["cumulative_tokens"] + args.g2_reserve >= args.limit:
        ok, line = False, (f"STOP: G2 rule: cumulative {before['cumulative_tokens']:,} + {args.g2_reserve:,} "
                           f">= {args.limit:,}")
    log({"phase": "gate", "ok": ok, "line": line, "cumulative_before": before["cumulative_tokens"], "budget": budget})
    if not ok:
        return EXIT_STOP

    # --raw: no From/To header or reply footer, so the seat answers in its own terminal instead of
    # spending an extra call on a rig send reply (measured on S0: one extra Bash call per turn).
    send = ["rig", "send", "--raw", args.session, turn["text"]] if args.raw else ["rig", "send", args.session, turn["text"]]
    sent = subprocess.run(send, capture_output=True, text=True)
    if sent.returncode != 0:
        log({"phase": "send", "ok": False, "returncode": sent.returncode})
        return 1
    try:
        session = wait_for_turn(args.session_file, before["turns"], args.quiet, args.timeout)
    except TimeoutError as exc:
        log({"phase": "wait", "ok": False, "line": f"STOP: {exc}"})
        return EXIT_STOP

    after = status(session)
    index = session["turns"]
    turn_calls = [c for c in session["calls"] if c["turn"] == index]
    tools: dict[str, int] = {}
    for call in turn_calls:
        for name, count in call["tools"].items():
            tools[name] = tools.get(name, 0) + count
    ok, lines = check_turn(after, index, budget, args.factor, len(files) if turn["kind"] == "growth" else None)
    if args.raw and turn["kind"] in ("rules", "opening") and len(turn_calls) != 1:
        ok = False
        lines.append(f"STOP: {len(turn_calls)} calls for a short turn, expected 1 (tools {tools})")
    if turn["kind"] == "probe" and tools != {"Write": 1}:
        ok = False
        lines.append(f"STOP: probe turn tools {tools}, expected exactly one Write (checkpoint contaminated)")
    previous_context = context_tokens(before.get("head_usage") or {}) if not before["calls"] else before["last_context_tokens"]
    for call in turn_calls:
        current = context_tokens(call["usage"])
        if previous_context and current < 0.7 * previous_context:
            ok = False
            lines.append(f"STOP: context dropped sharply {previous_context:,} -> {current:,}")
        previous_context = current
    last_output = turn_calls[-1]["usage"].get("output_tokens") if turn_calls else None
    notes = ["NOTE: reply over 100 output tokens"] if last_output and last_output > 100 and turn["kind"] != "probe" else []
    log({
        "phase": "result", "ok": ok, "lines": lines + notes, "session_turn": index,
        "turn_tokens": after["per_turn"].get(index, {}).get("tokens"), "budget": budget,
        "cumulative_after": after["cumulative_tokens"], "tools": tools,
        "calls": [{"context": context_tokens(c["usage"]), **c["usage"], "stop_reason": c.get("stop_reason"),
                   "tools": c["tools"]} for c in turn_calls],
    })
    return 0 if ok else EXIT_STOP


if __name__ == "__main__":
    sys.exit(main())
