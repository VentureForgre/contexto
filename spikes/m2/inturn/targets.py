"""What D63 (in-turn paging) must deliver on every scenario, as one check over a finished run.

At v0.5.24 every scenario fails it: the engine pages only at a turn start, so a tool loop goes over
the bound. The target tests are marked xfail until the engine pull request removes the marks.
"""

from __future__ import annotations

from inturn.harness import LOOP_START, Run


def unmet(run: Run) -> list[str]:
    """The targets ``run`` misses, one sentence each; empty when it meets them all."""
    m = run.metrics()
    bound = m["bound"]
    problems = []
    over = m["over_bound"]
    if over["estimate"] or over["real"]:
        problems.append(f"over_bound: {over['estimate']} from the estimate, {over['real']} from real usage")
    largest = m["largest"]
    if largest["tokens"] > bound or (largest["estimate"] or 0) > bound:
        problems.append(f"largest forwarded request: {largest['tokens']:,} tokens, estimate "
                        f"{largest['estimate']:,}, bound {bound:,}")
    if m["violations"]:
        problems.append(f"violations: {m['violations']} in {m['rejected_requests']} requests")
    problems += run.check_stubs()
    if m["originals"]["lost"]:
        problems.append(f"{m['originals']['lost']} tool results are in neither the forwarded request nor a page")
    for conversation, room in m["room"].items():
        if room["zero_past_50k"]:
            problems.append(f"{conversation}: the published recall room was 0 on {room['zero_past_50k']} requests "
                            f"after the turn passed {LOOP_START:,} tokens")
    return problems
