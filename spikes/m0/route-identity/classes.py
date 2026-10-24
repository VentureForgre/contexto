#!/usr/bin/env python3
"""Infer which requests in a Series A run shared an upstream end-user binding.

A thinking block produced by request j sits at messages[2j-1] in later requests. If request k
replays it and the API does not report it as thinking_dropped (end_user_binding_mismatch),
k and j share a binding; if it is dropped, they differ. Prints the same/different constraints,
the resulting classes, and any contradiction. Input: probe.py Series A JSONL with drop paths.

Usage: classes.py <series-A.jsonl>
"""
import json
import re
import sys


def main() -> None:
    rows = [json.loads(line) for line in open(sys.argv[1])]
    producers = {}  # message index -> request number that produced a thinking block there
    same, diff = set(), set()
    for r in rows:
        k = r["i"]
        dropped = {int(m.group(1)) for t in r["transformations"]
                   if (m := re.match(r"messages\.(\d+)\.", (t.get("shape") or {}).get("path", "")))}
        for idx, j in producers.items():
            (diff if idx in dropped else same).add((min(j, k), max(j, k)))
        if "thinking" in r["content_types"]:
            producers[2 * k - 1] = k
    parent = {r["i"]: r["i"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in same:
        parent[find(a)] = find(b)
    contradictions = [(a, b) for a, b in diff if find(a) == find(b)]
    groups = {}
    for r in rows:
        groups.setdefault(find(r["i"]), []).append(r["i"])
    constrained = {x for p in same | diff for x in p}
    print(f"same={sorted(same)}")
    print(f"different={sorted(diff)}")
    print(f"classes={sorted(sorted(g) for g in groups.values())}")
    print(f"unconstrained_requests={sorted(set(parent) - constrained)} contradictions={contradictions}")


if __name__ == "__main__":
    main()
