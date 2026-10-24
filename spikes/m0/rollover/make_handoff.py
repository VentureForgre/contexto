#!/usr/bin/env python3
"""Write a synthetic handoff of <size_unit> * 4 bytes.

Usage: make_handoff.py <size_unit> <out.md>
The size unit was meant as tokens at 4 bytes per token, but this line format measured about
1.8 bytes per token on claude-haiku-5-5 (M0-rollover.md section 3), so the token cost is about
2.2 times the argument. Use the measured handoff_tokens_delta, not the argument.
Lines look like "fact-0001: key-0001 = alder-cobalt-0001-17"; values are deterministic,
so a recall question has a known answer. Content is synthetic only.
"""
import sys

WORDS = ["alder", "birch", "cedar", "maple", "rowan", "willow", "aspen", "larch"]
COLORS = ["cobalt", "amber", "teal", "ochre", "slate", "coral", "olive", "umber"]


def value(i: int) -> str:
    return f"{WORDS[i % 8]}-{COLORS[(i * 3) % 8]}-{i:04d}-{(i * 7) % 97}"


def main() -> None:
    unit, out = int(sys.argv[1]), sys.argv[2]
    lines = ["# Synthetic handoff (test data)", ""]
    i = 0
    while sum(len(line) + 1 for line in lines) < unit * 4:
        i += 1
        lines.append(f"fact-{i:04d}: key-{i:04d} = {value(i)}")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{out}: {i} facts, {sum(len(line) + 1 for line in lines)} bytes")


if __name__ == "__main__":
    main()
