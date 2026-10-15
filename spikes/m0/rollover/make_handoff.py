#!/usr/bin/env python3
"""Write a synthetic handoff of roughly <tokens> tokens (about 4 bytes per token).

Usage: make_handoff.py <approx_tokens> <out.md>
Lines look like "fact-0001: key-0001 = alder-cobalt-0001-17"; values are deterministic,
so a recall question has a known answer. Content is synthetic only.
"""
import sys

WORDS = ["alder", "birch", "cedar", "maple", "rowan", "willow", "aspen", "larch"]
COLORS = ["cobalt", "amber", "teal", "ochre", "slate", "coral", "olive", "umber"]


def value(i: int) -> str:
    return f"{WORDS[i % 8]}-{COLORS[(i * 3) % 8]}-{i:04d}-{(i * 7) % 97}"


def main() -> None:
    tokens, out = int(sys.argv[1]), sys.argv[2]
    lines = ["# Synthetic handoff (test data)", ""]
    i = 0
    while sum(len(l) + 1 for l in lines) < tokens * 4:
        i += 1
        lines.append(f"fact-{i:04d}: key-{i:04d} = {value(i)}")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{out}: {i} facts, {sum(len(l) + 1 for l in lines)} bytes")


if __name__ == "__main__":
    main()
