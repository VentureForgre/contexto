"""Find planted values a test seat restated in its own replies (L2d contamination check).

    python3 restate_check.py --session-file S.jsonl --truth truth.jsonl --run-log turns.jsonl

Reads the seat's assistant text for each sent turn (session turn numbers come from the
drive_l2d.py run log) and checks it, in memory only, for each plant's current answer and its
older values. Prints and returns only plant ids, turn ids, and which value kind matched. No text
or value is printed or written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def replies_by_session_turn(session_file: Path, kind: str = "text") -> dict[int, str]:
    """Assistant text (kind "text") or thinking (kind "thinking") per session turn."""
    field = "text" if kind == "text" else "thinking"
    texts: dict[int, list[str]] = {}
    turn = 0
    for line in session_file.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "user" and not entry.get("isSidechain"):
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, str) or (isinstance(content, list) and not any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content)):
                turn += 1
        elif entry.get("type") == "assistant":
            for block in (entry.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == kind:
                    texts.setdefault(turn, []).append(block.get(field) or "")
    return {t: "\n".join(parts) for t, parts in texts.items()}


def restated(text: str, plants: list[dict[str, Any]]) -> list[dict[str, str]]:
    hits = []
    for plant in plants:
        if plant["answer"] and plant["answer"] in text:
            hits.append({"plant": plant["id"], "matched": "answer"})
        elif any(old and old in text for old in plant.get("older") or []):
            hits.append({"plant": plant["id"], "matched": "older"})
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--run-log", type=Path, required=True, help="drive_l2d.py turns.jsonl")
    args = parser.parse_args(argv)
    plants = [json.loads(line) for line in args.truth.read_text().splitlines()]
    replies = replies_by_session_turn(args.session_file)
    thinking = replies_by_session_turn(args.session_file, "thinking")
    sent = {}
    for line in args.run_log.read_text().splitlines():
        record = json.loads(line)
        if record.get("phase") == "result":
            sent[record["turn_id"]] = record["session_turn"]
    report = {turn_id: {"reply": restated(replies.get(session_turn, ""), plants),
                        "thinking": restated(thinking.get(session_turn, ""), plants),
                        "thinking_chars": len(thinking.get(session_turn, ""))}
              for turn_id, session_turn in sent.items()}
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
