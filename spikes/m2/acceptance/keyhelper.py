"""apiKeyHelper shim for the acceptance run: run the seat's existing helper, read-only, at call time.

The isolated Claude Code config directory's settings.json names this script as its apiKeyHelper,
with the path of the seat's settings file as the only argument. The script reads that file's
``apiKeyHelper`` command and runs it, passing its stdout through. No key, and no copy of the
helper command, is ever written to the scratch directory, a log, or a commit.

Usage (as an apiKeyHelper): python3 keyhelper.py /path/to/seat/.claude/settings.local.json
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: keyhelper.py <seat settings file>", file=sys.stderr)
        return 2
    try:
        helper = json.loads(Path(argv[0]).read_text(encoding="utf-8")).get("apiKeyHelper")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"keyhelper: cannot read the seat settings file: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not isinstance(helper, str) or not helper:
        print("keyhelper: the seat settings file has no apiKeyHelper", file=sys.stderr)
        return 1
    proc = subprocess.run(helper, shell=True, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout.strip():
        print(f"keyhelper: the seat's apiKeyHelper failed (exit {proc.returncode}); output not shown", file=sys.stderr)
        return 1
    sys.stdout.write(proc.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
