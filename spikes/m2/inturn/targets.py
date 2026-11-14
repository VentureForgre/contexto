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

from inturn.harness import LOOP_START, TRUTH_SHARE, Run

SHOW_SHARE = 0.8  # of bound - headroom_floor - min_turn_room: under it, a new result has to be shown once
PRESSURE_SHARE = 0.6  # of the same line: a loop rewrite is allowed only when the request would pass it
REWRITE_BATCH = 6  # a loop may rewrite its prefix twice, and once more per rewrite_batch tokens of its own items,
REWRITE_FLOOR = 30  # which is bound / REWRITE_BATCH, or less under a large head, but never under bound / REWRITE_FLOOR
HEAD_SHARE = 0.5  # of what sits between the head and the room line: what one batch can be expected to free
ESTIMATE_MARGIN = 0.02  # of the bound: how far under the room line the engine's estimate may be for a second rewrite
ESTIMATE_TRUST = 102  # percent: the engine's estimate counts at most 1.02 x the upstream's count of the same request
ROOM_PERCENT = 95  # of a loop's late requests: the response-time room is at least half of min_turn_room


def room_line(run: Run) -> int:
    """``bound - headroom_floor - min_turn_room`` (113,000 at the defaults): a request over it leaves the
    turn less than ``min_turn_room``."""
    config = run.config
    return config.bound - config.headroom_floor - config.min_turn_room


def show_ceiling(run: Run) -> int:
    """Head, prompt and new results under this many tokens fit in a request, so the results are shown."""
    return int(SHOW_SHARE * room_line(run))


def pressure_line(run: Run) -> int:
    """Under this many tokens (67,800 at the defaults) a loop request may not rewrite its prefix."""
    return int(PRESSURE_SHARE * room_line(run))


def rewrite_batch(run: Run, conversation: str) -> float:
    """The loop tokens that buy one more prefix rewrite: ``min(bound / 6, max(bound / 30, 0.5 x (room_line
    - head)))``, with the head the conversation's first request carried (``Run.heads``). At the scripted
    heads (15,101 for Claude Code's main, 10,654 for the subagent, 13,538 for Codex) it is 25,000, bound /
    6; it is under that only once the head passes 63,000, and with a 92,000-token head it is 10,500."""
    config = run.config
    room = room_line(run) - run.heads.get(conversation, 0)
    return min(config.bound / REWRITE_BATCH, max(config.bound / REWRITE_FLOOR, HEAD_SHARE * room))


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
    """Every result accounted for, and every stub leads to its originals (rules 3 and 4).

    A page is referenced by a request when ``stubs.find_stubs`` returns a ``StubRef`` naming it. A call
    is reachable from a page when the page holds the call's original byte for byte, or holds a stub or
    fold reference whose page leads to it, within 8 pages (``harness.Stubs``).

    On every request, each result the CLI has delivered so far in that conversation is whole in the
    forwarded request, or is accounted for there:

    - a ``StubRef`` in that request names its call and its page reaches it, or the ``StubRef`` has
      empty ``call_ids`` (it references a page as a whole, as a fold note naming a rounds page does) and
      its page reaches it; a page that reaches nothing that is missing covers nothing;
    - or, for a result of an earlier turn only, its call is gone from the request too, and the text the
      model reads (text parts of messages, not tool results or other fields) names where it went: a page
      id there reaches it, or the whole turn left (no 40-character run of its prompt or of any of its
      answers that is found in no other turn's text, with whitespace, format characters and combining
      marks removed, is anywhere in that text; a shorter text counts only as a whole text part), that
      text carries the index's "(N older pages omitted; use contexto_search"
      line with N at least 1 and at most the store pages it does not name, and the result is stored byte
      for byte.

    Anything else fails. A result that is not whole in the request is also stored byte for byte in
    some page (a result of Contexto's own search, recall or state calls may be named by a reference with
    no page instead). Every stub found leads to its originals, and at least one is found."""
    problems = run.check_stubs()
    unnamed = sum(1 for s in run.sent if s.unnamed)
    if unnamed:
        worst = max(s.unnamed for s in run.sent)
        problems.append(f"{unnamed} requests left out a tool result that no stub, fold note or page id in that request "
                        f"accounts for (up to {worst} results at once)")
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
    """Paging in a loop is paid for in prompt cache, so it is bounded four ways:

    - Pressure: a loop request rewrites its prefix only when the previous forwarded request plus the
      items added since is over ``pressure_line`` (0.6 of ``room_line``), by the upstream's count.
    - Count: a loop has at most 2 prefix rewrites plus one per ``rewrite_batch`` tokens of its own items
      (25,000, bound / 6, at every scripted head; less under a head over 63,000).
    - Cost: the tokens a loop's rewrites re-send (the headline figure: on Messages by breakpoints) are
      at most the loop's own tokens or the bound, whichever is larger: one extra send of the turn.
    - In a row: a loop request is not a rewrite right after another rewrite, unless the engine's own
      pre-send estimate of it without the rewrite (the ``estimate`` of its ``request`` event) is over
      ``room_line`` less 2% of the bound, so the room rule would fail without it. That estimate counts
      at most 1.02 x the upstream's count of the same request (the previous request plus what was added
      since); v0.5.24's estimate ran at most 1.0009 x on loop requests past 50,000 tokens.
    """
    problems = []
    config = run.config
    line, pressure = room_line(run), pressure_line(run)
    forced = line - ESTIMATE_MARGIN * config.bound
    spent = run.loop_resent()
    for (conversation, turn), sent in run.loops().items():
        rewrites = sum(1 for s in sent if s.in_loop and s.rewrite)
        own = max(s.loop_tokens for s in sent)
        allowed = 2 + own / rewrite_batch(run, conversation)
        if rewrites > allowed:
            problems.append(f"{conversation}, turn {turn + 1}: {rewrites} prefix rewrites in a loop of "
                            f"{own:,} tokens; at most {int(allowed)}")
        resent, _ = spent[conversation, turn]
        if resent > max(own, config.bound):
            problems.append(f"{conversation}, turn {turn + 1}: its rewrites re-sent {resent:,} tokens, more than "
                            f"the loop's own {own:,} tokens or the bound")
    for conversation in run.scenario.conversations:
        sent = [s for s in run.sent if s.conversation == conversation.name]
        early = [a.tokens + b.appended_tokens for a, b in zip(sent, sent[1:])
                 if b.rewrite and b.in_loop and a.tokens + b.appended_tokens <= pressure]
        if early:
            problems.append(f"{conversation.name}: {len(early)} loop requests rewrote the prefix though without the "
                            f"rewrite they came to at most {max(early):,} tokens, not over {pressure:,}")
        twice = [b for a, b in zip(sent, sent[1:]) if a.rewrite and b.rewrite and b.in_loop
                 and _estimate_before(a, b) <= forced]
        if twice:
            problems.append(f"{conversation.name}: {len(twice)} requests rewrote the prefix right after a rewrite, "
                            "though the request would have kept min_turn_room without it")
    return problems


def _estimate_before(previous, sent) -> float:
    """The engine's pre-send estimate of ``sent`` without its rewrite, at most 1.02 x the upstream's count
    of the previous request and the items added since; that count when the engine publishes none."""
    counted = previous.tokens + sent.appended_tokens
    if sent.estimate_before is None:
        return counted
    return min(sent.estimate_before, counted * ESTIMATE_TRUST / 100)


def room(run: Run) -> list[str]:
    """Every main request in a loop publishes a room before the send and after the response, and every
    subagent response with a tool call a side room. Once the turn's own items pass 50,000 tokens the
    request-time room is never under ``min_turn_room``, and the response-time room is never 0 and is at
    least half of ``min_turn_room`` on 95% of those requests.

    Every room published tells the truth, by the upstream's count: a response-time room (main's turn
    room or a side room) is at most ``bound - (input + output) - headroom_floor`` from that response's
    usage, on every response, and a request-time room at most ``bound - headroom_floor - 0.9 x the
    request's tokens``, on every request but a conversation's first. The first is sized before any usage,
    at the estimator's seeds, which run low on a dense head (11% at v0.5.24, a limitation STATUS lists
    apart from D63)."""
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
        mine = [s for s in run.sent if s.conversation == name]
        truths = [run.truth(s) for s in mine]
        # A conversation's first request is sized before any usage, at the estimator's seeds, which run low
        # on a dense head (STATUS "Open follow-ups"; not D63's sizing), so its request-time room is not held to this.
        early = sum(1 for s, (most, _) in list(zip(mine, truths))[1:]
                    if s.room_at_request is not None and s.room_at_request > most)
        late_lies = sum(1 for s, (_, most) in zip(mine, truths) if s.room is not None and s.room > most)
        if early:
            problems.append(f"{name}: the request-time room was more than bound - headroom_floor - "
                            f"{TRUTH_SHARE} x the request's tokens on {early} requests")
        if late_lies:
            problems.append(f"{name}: the response-time room was more than bound - (input + output) - "
                            f"headroom_floor on {late_lies} requests")
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
