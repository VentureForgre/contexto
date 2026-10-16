"""Usage meter and summary rows from a Claude Code session JSONL (for seats the proxy cannot see).

    python3 sessionlog.py status  SESSION.jsonl
    python3 sessionlog.py summary SESSION.jsonl --csv OUT.csv
    python3 sessionlog.py gate    SESSION.jsonl --next-turn-budget N [--limit 1900000] [--factor 1.5]
    python3 sessionlog.py check   SESSION.jsonl --turn K --budget N [--factor 1.5] [--reads M]

Reads only numbers and field names: message ids, models, stop reasons, usage numbers, tool
names, timestamps, and turn markers. It never copies message text, tool input, or tool output.

A call is one API response. Claude Code can write several assistant entries per response (one per
content block), so calls are deduplicated by message.id, keeping the last usage seen. A turn starts
at each user entry that is a prompt rather than a tool result. Rows use the lane 2 summary fields;
request_bytes and message_count stay empty because the session file does not record the wire
request.

Exit codes: 0 ok, 3 a stop rule fired (the STOP line says which), 2 usage error.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from extract import SUMMARY_FIELDS, total_tokens

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
EXIT_STOP = 3


def _is_prompt(entry: dict[str, Any]) -> bool:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


def read_session(path: Path) -> dict[str, Any]:
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    turn = 0
    turn_started: dict[int, str] = {}
    compactions: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        kind = entry.get("type")
        if kind == "user" and not entry.get("isSidechain"):
            if entry.get("isCompactSummary"):
                compactions.append({"ts": entry.get("timestamp"), "turn": turn, "marker": "isCompactSummary"})
            elif _is_prompt(entry) and not entry.get("isMeta"):
                turn += 1
                turn_started[turn] = entry.get("timestamp")
        elif kind == "system" and "compact" in str(entry.get("subtype", "")):
            compactions.append({"ts": entry.get("timestamp"), "turn": turn, "marker": entry.get("subtype")})
        elif kind == "assistant":
            message = entry.get("message") or {}
            key = message.get("id") or entry.get("requestRef") or entry.get("uuid")
            if key not in calls:
                order.append(key)
                calls[key] = {"turn": turn, "ts": entry.get("timestamp"), "tools": {},
                              "sidechain": bool(entry.get("isSidechain"))}
            call = calls[key]
            call["model"] = message.get("model")
            call["stop_reason"] = message.get("stop_reason") or call.get("stop_reason")
            usage = message.get("usage") or {}
            call["usage"] = {k: usage.get(k) for k in USAGE_KEYS if isinstance(usage.get(k), int)}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name"))
                    seen = call.setdefault("_tool_ids", set())
                    if block.get("id") not in seen:
                        seen.add(block.get("id"))
                        call["tools"][name] = call["tools"].get(name, 0) + 1
    rows = []
    for key in order:
        call = calls[key]
        call.pop("_tool_ids", None)
        rows.append(call)
    return {"calls": rows, "turns": turn, "turn_started": turn_started, "compactions": compactions}


def context_tokens(usage: dict[str, Any]) -> int:
    return sum(usage.get(k) or 0 for k in USAGE_KEYS[:3])


def status(session: dict[str, Any]) -> dict[str, Any]:
    calls = session["calls"]
    per_turn: dict[int, dict[str, Any]] = {}
    for call in calls:
        t = per_turn.setdefault(call["turn"], {"calls": 0, "tokens": 0, "max_context": 0, "read_calls": 0, "reads": 0})
        t["calls"] += 1
        t["tokens"] += total_tokens(call["usage"])
        t["max_context"] = max(t["max_context"], context_tokens(call["usage"]))
        reads = call["tools"].get("Read", 0)
        t["reads"] += reads
        t["read_calls"] += int(reads > 0)
    first = calls[0]["usage"] if calls else {}
    last = calls[-1] if calls else {}
    totals = {k: sum(c["usage"].get(k) or 0 for c in calls) for k in USAGE_KEYS}
    return {
        "calls": len(calls),
        "turns": session["turns"],
        "cumulative_tokens": total_tokens(totals),
        "totals": totals,
        "head_context_tokens": context_tokens(first),
        "head_usage": first,
        "last_context_tokens": context_tokens(last.get("usage") or {}),
        "last_stop_reason": last.get("stop_reason"),
        "last_ts": last.get("ts"),
        "cache_read_calls": sum(1 for c in calls if (c["usage"].get("cache_read_input_tokens") or 0) > 0),
        "compactions": session["compactions"],
        "per_turn": per_turn,
    }


def summary_rows(session: dict[str, Any], cli: str = "claude-seat") -> list[dict[str, Any]]:
    rows = []
    for call in session["calls"]:
        usage = call["usage"]
        row = {field: None for field in SUMMARY_FIELDS}
        row.update({k: usage.get(k) for k in USAGE_KEYS})
        row.update(turn=call["turn"], cli=cli, model=call.get("model"), ts=call.get("ts"), endpoint="session-jsonl")
        rows.append(row)
    return rows


def gate(st: dict[str, Any], next_turn_budget: int, limit: int, factor: float) -> tuple[bool, str]:
    projected = st["cumulative_tokens"] + factor * next_turn_budget
    ok = projected <= limit
    verdict = "GO" if ok else "STOP"
    return ok, (f"{verdict}: cumulative {st['cumulative_tokens']:,} + {factor} x next-turn budget "
                f"{next_turn_budget:,} = {int(projected):,} {'<=' if ok else '>'} limit {limit:,}")


def check_turn(st: dict[str, Any], turn: int, budget: int, factor: float, reads: int | None) -> tuple[bool, list[str]]:
    t = st["per_turn"].get(turn)
    if t is None:
        return False, [f"STOP: turn {turn} has no calls in the session file"]
    lines, ok = [], True
    if t["tokens"] > factor * budget:
        ok = False
        lines.append(f"STOP: turn {turn} cost {t['tokens']:,} > {factor} x budget {budget:,}")
    if reads and reads > 1 and t["read_calls"] > 1:
        ok = False
        lines.append(f"STOP: turn {turn} spread {t['reads']} Read calls over {t['read_calls']} requests (serial reads)")
    if st["compactions"]:
        ok = False
        lines.append(f"STOP: compaction marker(s) present: {st['compactions']}")
    if ok:
        lines.append(f"OK: turn {turn} cost {t['tokens']:,} (budget {budget:,}), {t['calls']} calls, "
                     f"{t['reads']} reads in {t['read_calls']} request(s), max context {t['max_context']:,}")
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("status", "summary", "gate", "check"):
        p = sub.add_parser(name)
        p.add_argument("session", type=Path)
        if name == "summary":
            p.add_argument("--csv", type=Path, required=True)
        if name == "gate":
            p.add_argument("--next-turn-budget", type=int, required=True)
            p.add_argument("--limit", type=int, default=1_900_000)
            p.add_argument("--factor", type=float, default=1.5)
        if name == "check":
            p.add_argument("--turn", type=int, required=True)
            p.add_argument("--budget", type=int, required=True)
            p.add_argument("--factor", type=float, default=1.5)
            p.add_argument("--reads", type=int, default=None, help="files the turn should read in parallel")
    args = parser.parse_args(argv)
    session = read_session(args.session)
    st = status(session)
    if args.cmd == "status":
        print(json.dumps(st, indent=1, sort_keys=True))
        return 0
    if args.cmd == "summary":
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
            writer.writeheader()
            writer.writerows(summary_rows(session))
        return 0
    if args.cmd == "gate":
        ok, line = gate(st, args.next_turn_budget, args.limit, args.factor)
        print(line)
        return 0 if ok else EXIT_STOP
    ok, lines = check_turn(st, args.turn, args.budget, args.factor, args.reads)
    print("\n".join(lines))
    return 0 if ok else EXIT_STOP


if __name__ == "__main__":
    sys.exit(main())
