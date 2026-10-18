"""Calibrate the store-scale spike's distractor rates from real Claude Code transcripts (offline, read-only).

    python3 scale_calibrate.py [DIR ...] --out calibration.json

Reads ``DIR/*/*.jsonl`` (Claude Code session transcripts; default ``~/.claude/projects``), splits each
main session into turns the way the engine pages them (a turn starts at a user message that is not
a tool result), and writes numbers only: the turn count, the turn-size distribution under
contexto's own estimator, and, for every probe and latency query of ``scale_probes``, the share of
turns whose text contains the query exactly (document frequency). No transcript text is written.
Sidechain (subagent) records and subagent transcript files are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[3] / "src"))

from contexto.store.pages import _texts  # noqa: E402  (the text search reads)
from contexto.tokens import estimate_message_tokens  # noqa: E402
from scale_probes import FACTS, LATENCY_QUERIES  # noqa: E402


def turns_of(path: Path) -> Iterator[list[dict[str, Any]]]:
    """The turns of one transcript as Messages API message lists, oldest first."""
    turn: list[dict[str, Any]] = []
    try:
        lines = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with lines:
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("isSidechain") or record.get("type") not in ("user", "assistant"):
                continue
            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
                continue
            content = message.get("content")
            if message["role"] == "user" and not (isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content)):
                if turn:
                    yield turn
                turn = []
            turn.append({"role": message["role"], "content": content})
    if turn:
        yield turn


def query_set() -> list[str]:
    out: list[str] = []
    for q in [q for f in FACTS for q in f.queries] + [q for qs in LATENCY_QUERIES.values() for q in qs]:
        if q not in out:
            out.append(q)
    return out


def calibrate(dirs: list[Path]) -> dict[str, Any]:
    queries = query_set()
    df = dict.fromkeys(queries, 0)
    tokens: list[int] = []
    files = sorted(p for d in dirs for p in d.glob("*/*.jsonl"))
    for path in files:
        for turn in turns_of(path):
            text = "\n".join(_texts(turn))
            tokens.append(estimate_message_tokens(turn))
            for q in queries:
                if q in text:
                    df[q] += 1
    n = len(tokens)
    qs = statistics.quantiles(tokens, n=20) if n >= 20 else []
    return {
        "files": len(files), "turns": n,
        "turn_tokens": {"median": statistics.median(tokens) if tokens else None,
                        "mean": round(statistics.fmean(tokens)) if tokens else None,
                        "total": sum(tokens),
                        "p90": qs[17] if qs else None, "p95": qs[18] if qs else None,
                        "max": max(tokens) if tokens else None},
        "df": {q: round(c / n, 5) if n else 0.0 for q, c in df.items()},
        "df_counts": df,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dirs", nargs="*", type=Path, default=[Path(os.path.expanduser("~/.claude/projects"))])
    parser.add_argument("--out", type=Path, default=HERE / "calibration.json")
    args = parser.parse_args(argv)
    result = calibrate(args.dirs)
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{result['files']} files, {result['turns']} turns, median turn {result['turn_tokens']['median']} tokens; "
          f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
