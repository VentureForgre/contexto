"""What D63 (in-turn paging) must deliver on every scenario, as checks over a finished run.

At v0.5.24 every scenario fails: the engine pages only at a turn start, so a tool loop goes over the
bound. The target tests are marked xfail until the engine pull request removes the marks.

Each rule is a function from a ``Run`` to the sentences that say how the run misses it. ``unmet``
joins them; ``unmet_by_rule`` keeps them apart, so a test can say which rule an engine fails.

Terms. A loop is one turn of one conversation after its first request. A prefix rewrite is a request
whose head or forwarded items differ from the conversation's previous request at a position both
have. The turn's own items are the CLI's items since the turn's prompt, in the upstream's tokens.
"""

from __future__ import annotations

from collections.abc import Callable

from inturn.harness import LOOP_START, Run

SHOW_SHARE = 0.8  # of bound - headroom_floor - min_turn_room: under it, a new result has to be shown once
REWRITE_BATCH = 6  # a loop may rewrite its prefix twice, and once more per bound / REWRITE_BATCH of its own items
ROOM_PERCENT = 95  # of a loop's late requests: the response-time room is at least half of min_turn_room


def show_ceiling(run: Run) -> int:
    """Head, prompt and new results under this many tokens fit in a request, so the results are shown."""
    config = run.config
    return int(SHOW_SHARE * (config.bound - config.headroom_floor - config.min_turn_room))


def bound(run: Run) -> list[str]:
    """No request is over the bound: no ``over_bound`` event from the estimate or from real usage, and
    the largest forwarded request is at or under the bound by the estimate and by the upstream's count."""
    m = run.metrics()
    problems = []
    over = m["over_bound"]
    if over["estimate"] or over["real"]:
        problems.append(f"over_bound: {over['estimate']} from the estimate, {over['real']} from real usage")
    largest = m["largest"]
    if largest["tokens"] > m["bound"] or (largest["estimate"] or 0) > m["bound"]:
        problems.append(f"largest forwarded request: {largest['tokens']:,} tokens, estimate "
                        f"{largest['estimate']}, bound {m['bound']:,}")
    return problems


def violations(run: Run) -> list[str]:
    """The upstream counts no violation of any kind (``upstream.py`` lists them)."""
    if not run.violations:
        return []
    return [f"violations: {dict(sorted(run.violations.items()))} in {run.rejected} requests"]


def storage(run: Run) -> list[str]:
    """On every request, each result delivered so far is whole in the forwarded request or in a store
    page byte for byte, and every stub leads to its originals (``stubs.py``)."""
    problems = run.check_stubs()
    lost = sum(1 for s in run.sent if s.lost)
    if lost:
        worst = max(s.lost for s in run.sent)
        problems.append(f"{lost} requests left a tool result neither whole in the request nor byte for byte in a "
                        f"store page (up to {worst} results at once)")
    return problems


def thinking(run: Run) -> list[str]:
    """In a loop, the newest response comes back without its thinking on no more requests than the
    loop has prefix rewrites. (Thinking missing where no rewrite covers it is a violation.)"""
    problems = []
    for (conversation, turn), sent in run.loops().items():
        missing = sum(1 for s in sent if s.latest_thinking_missing)
        rewrites = sum(1 for s in sent if s.in_loop and s.rewrite)
        if missing > rewrites:
            problems.append(f"{conversation}, turn {turn + 1}: the newest response's thinking was missing on "
                            f"{missing} requests, and the loop has {rewrites} prefix rewrites")
    return problems


def seen_first(run: Run) -> list[str]:
    """A result is whole in the request that first carries it, unless the head, the turn's prompt and
    that request's new results together are at least ``show_ceiling`` tokens."""
    ceiling = show_ceiling(run)
    early = [s for s in run.sent if s.stubbed_on_arrival and s.unseen_tokens < ceiling]
    if not early:
        return []
    return [f"{sum(s.stubbed_on_arrival for s in early)} results in {len(early)} requests were stubbed in the "
            f"request that first carried them, though head, prompt and new results came to under {ceiling:,} tokens "
            f"(the largest: {max(s.unseen_tokens for s in early):,})"]


def cache(run: Run) -> list[str]:
    """A loop has at most 2 prefix rewrites plus one per ``bound / 6`` tokens of its own items, and no
    request in a loop is a rewrite right after another, unless it would be over the bound without one."""
    problems = []
    batch = run.config.bound / REWRITE_BATCH
    for (conversation, turn), sent in run.loops().items():
        rewrites = sum(1 for s in sent if s.in_loop and s.rewrite)
        allowed = 2 + sent[-1].loop_tokens / batch
        if rewrites > allowed:
            problems.append(f"{conversation}, turn {turn + 1}: {rewrites} prefix rewrites in a loop of "
                            f"{sent[-1].loop_tokens:,} tokens; at most {int(allowed)}")
    for conversation in run.scenario.conversations:
        sent = [s for s in run.sent if s.conversation == conversation.name]
        twice = [b for a, b in zip(sent, sent[1:]) if a.rewrite and b.rewrite and b.in_loop
                 and a.tokens + b.appended_tokens <= run.config.bound]
        if twice:
            problems.append(f"{conversation.name}: {len(twice)} requests rewrote the prefix right after a rewrite, "
                            "though the request would have been under the bound without it")
    return problems


def room(run: Run) -> list[str]:
    """Every main request in a loop publishes a room before the send and after the response, and every
    subagent response with a tool call a side room. Once the turn's own items pass 50,000 tokens the
    request-time room is never under ``min_turn_room``, and the response-time room is never 0 and is at
    least half of ``min_turn_room`` on 95% of those requests."""
    problems = []
    low = run.config.min_turn_room
    for conversation in run.scenario.conversations:
        name = conversation.name
        loop = [s for s in run.sent if s.conversation == name and s.in_loop]
        if conversation.agent:
            unpublished = sum(1 for s in loop if s.tool_calls and s.room is None)
            if unpublished:
                problems.append(f"{name}: {unpublished} responses with a tool call published no side room")
        else:
            before = sum(1 for s in loop if s.room_at_request is None)
            after = sum(1 for s in loop if s.room is None)
            if before or after:
                problems.append(f"{name}: no room was published before {before} loop requests and after {after}")
        late = [s for s in loop if s.loop_tokens > LOOP_START]
        under = sum(1 for s in late if s.room_at_request is not None and s.room_at_request < low)
        if under:
            problems.append(f"{name}: the request-time room was under {low:,} on {under} requests after the turn "
                            f"passed {LOOP_START:,} tokens")
        zero = sum(1 for s in late if s.room == 0)
        if zero:
            problems.append(f"{name}: the response-time room was 0 on {zero} requests after the turn passed "
                            f"{LOOP_START:,} tokens")
        thin = sum(1 for s in late if s.room is not None and s.room < low // 2)
        if thin * 100 > len(late) * (100 - ROOM_PERCENT):
            problems.append(f"{name}: the response-time room was under {low // 2:,} on {thin} of {len(late)} "
                            f"requests after the turn passed {LOOP_START:,} tokens")
    return problems


def recall(run: Run) -> list[str]:
    """Late in a loop, a search for a line of an early result returns that line, from the request in
    view or from a page; when the result is no longer whole in the request, the search names a page,
    and recalling it at the offset given returns the line and the two lines after it as the original
    has them. One case is let through: a subagent whose result is still whole in its request may be
    told that no page holds the line, because search reads the main conversation's view only."""
    problems = []
    if not any(p["kind"] == "search" for p in run.probes):
        problems.append("the scenario made no search")
    for conversation in run.scenario.conversations:
        mine = [p for p in run.probes if p["conversation"] == conversation.name]
        for search, after in zip(mine[::2], mine[1::2]):  # a search, then what the model did with its reply
            where = f"{conversation.name}, request {search['request']}"
            if conversation.agent and search["target_in_view"] and search["outcome"] == "nothing":
                continue
            if search["outcome"] not in ("in_view", "paged"):
                problems.append(f"{where}: the search did not return the line ({search['outcome']})")
            elif not search["target_in_view"] and search["outcome"] != "paged":
                problems.append(f"{where}: the result is not whole in the request, and the search named no page")
            elif search["outcome"] == "paged" and after["outcome"] != "returned":
                problems.append(f"{where}: recalling the page the search named did not return the original "
                                f"({after['outcome']})")
    return problems


def count_tokens(run: Run) -> list[str]:
    """A ``count_tokens`` request with the same history gets the transform that was just forwarded, and
    publishes no room (contract rule 11)."""
    counted = run.count_tokens
    problems = []
    if counted["different"]:
        problems.append(f"{counted['different']} of {counted['sent']} count_tokens requests got another transform "
                        "than the request just forwarded")
    if counted["published"]:
        problems.append(f"{counted['published']} count_tokens requests published a room")
    return problems


RULES: dict[str, Callable[[Run], list[str]]] = {
    "bound": bound, "violations": violations, "storage": storage, "thinking": thinking, "seen_first": seen_first,
    "cache": cache, "room": room, "recall": recall, "count_tokens": count_tokens,
}


def unmet_by_rule(run: Run) -> dict[str, list[str]]:
    """The rules ``run`` misses, each with its sentences; empty when it meets them all."""
    found = {name: rule(run) for name, rule in RULES.items()}
    return {name: problems for name, problems in found.items() if problems}


def unmet(run: Run) -> list[str]:
    """The targets ``run`` misses, one sentence each; empty when it meets them all."""
    return [problem for problems in unmet_by_rule(run).values() for problem in problems]
