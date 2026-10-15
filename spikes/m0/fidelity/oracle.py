"""Simulated reader for the offline conditions. Not a model.

The reader answers a probe only from text present in the request it is given, plus pages it
recalls through the store when the condition offers the recall tool. It resolves conflicting
statements by turn order (newest wins) and picks pages to recall by matching the probe's hint
word against stub headlines, never by similarity search.

What this measures is answer availability under a policy: whether the current answer is visible
or reachable in at most ``max_recalls`` calls. It is an upper bound on what a careful reader could
answer from the request, not a measurement of any model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fidelity.policy import RECALL_MAX_CALLS_PER_TURN, RECALL_TOOL_NAME, RecallError, Request, Store
from fidelity.session import Planted

_STUB = re.compile(r"\[paged (pg_[0-9a-f]{16}) \|")
_PINNED_OPEN_HEADER = "OPEN OBLIGATIONS as of turn "


@dataclass(frozen=True)
class Answer:
    text: str | None
    recalls: int


Claim = tuple[int, int, int, str]  # (turn, segment position, order inside segment, value)
_TURN_PREFIX = re.compile(r"^\[turn (\d+)\] ")


def _claims(planted: Planted, segments: list[tuple[int, str]]) -> list[Claim]:
    """Every statement about ``planted`` in ``segments``, ordered by (turn, position, order)."""
    found: list[Claim] = []
    if planted.category == "obligation":
        oid = re.escape(planted.subject)
        opened = re.compile(rf"Action item {oid}: |^- {oid}: ")
        closed = re.compile(rf"Action item {oid} is done")
        for pos, (turn, text) in enumerate(segments):
            for order, line in enumerate(text.split("\n")):
                if closed.search(line):
                    found.append((turn, pos, order, "closed"))
                elif opened.search(line):
                    found.append((turn, pos, order, "open"))
        return sorted(found)
    pattern = re.compile(rf"the {re.escape(planted.subject)} is (?:now )?([a-z]+-\d+)")
    for pos, (turn, text) in enumerate(segments):
        for order, match in enumerate(pattern.finditer(text)):
            found.append((turn, pos, order, match.group(1)))
    return sorted(found)


def _newest(planted: Planted, segments: list[tuple[int, str]], request: Request) -> Claim | None:
    claims = _claims(planted, segments)
    pinned = segments[: request.pinned_count]
    if planted.category == "obligation" and any(text.startswith(_PINNED_OPEN_HEADER) for _, text in pinned):
        # The list is complete as of the turn its header names (the window start minus one), so an
        # item it omits was closed by then; later turns in the window may still reopen it.
        if not any(text.startswith(f"- {planted.subject}: ") for _, text in pinned):
            claims.append((request.window_first_turn - 1, 0, 0, "closed"))
    return max(claims) if claims else None


def _page_segments(page_text: str, first_turn: int) -> list[tuple[int, str]]:
    """Split recalled turn pages back into lines carrying their own turn numbers."""
    out = []
    turn = first_turn
    for line in page_text.split("\n"):
        match = _TURN_PREFIX.match(line)
        if match:
            turn = int(match.group(1))
        out.append((turn, line))
    return out


def answer(planted: Planted, request: Request, store: Store, max_recalls: int = 3) -> Answer:
    segments = list(request.segments)
    newest = _newest(planted, segments, request)
    current = newest[3] if newest else None
    if not any(t.get("name") == RECALL_TOOL_NAME for t in request.tools):
        return Answer(current, 0)
    # Recall only when nothing is known, or when the newest evidence predates the window and is
    # not pinned state. Pinned state is current as of the window start, so it needs no recall.
    if newest is not None:
        in_window = newest[0] >= request.window_first_turn
        pinned = newest[1] < request.pinned_count or (
            planted.category == "obligation" and request.pinned_count > 0
        )
        if in_window or pinned:
            return Answer(current, 0)

    budget = min(max_recalls, RECALL_MAX_CALLS_PER_TURN)
    candidates: set[tuple[int, str]] = set()
    for turn, text in segments:
        for line in text.split("\n"):
            match = _STUB.search(line)
            if match and planted.hint.casefold() in line.casefold():
                candidates.add((turn, match.group(1)))
    recalls = 0
    for _, pid in sorted(candidates, reverse=True):  # newest pages first
        offset: int | None = 0
        text = ""
        while offset is not None and recalls < budget:
            try:
                got = store.recall(pid, offset)
            except RecallError:
                break
            recalls += 1
            text += got["text"]
            offset = got["next_offset"]
        segments.extend(_page_segments(text, store.pages[pid].first_turn))
        if recalls >= budget:
            break
    newest = _newest(planted, segments, request)
    return Answer(newest[3] if newest else None, recalls)
