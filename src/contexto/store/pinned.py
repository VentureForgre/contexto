"""Pinned text and the full pinned state, rendered from derived ledger state (M2 contract, Store).

Pinned text = the mandatory first line, then open obligations, active decisions, then verified
facts, newest first within each group, each with its record id, capped at a token budget. Over
budget it lists what fits and then the number of omitted items and a pointer to ``contexto_state``.
The full state is the same listing with no cap, plus any decision conflicts.
"""

from __future__ import annotations

from contexto.store.model import Record
from contexto.store.pages import PINNED_FIRST_LINE
from contexto.store.state import State
from contexto.tokens import estimate_tokens

GROUPS = ("Open obligations:", "Active decisions:", "Verified facts:")
_FACT_KINDS = ("event", "artifact", "reference")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def pinned_items(state: State) -> list[tuple[str, Record]]:
    """(group heading, record) in render order: by group, newest first inside each."""
    newest = list(reversed(state.active))
    conflicted = {rid for c in state.conflicts for rid in c.record_ids}
    open_ids = {r.id for r in state.open_obligations}
    items = [(GROUPS[0], r) for r in newest if r.id in open_ids]
    items += [(GROUPS[1], r) for r in newest if r.kind == "decision"]
    items += [(GROUPS[2], r) for r in newest if r.kind in _FACT_KINDS and r.epistemic == "verified"]
    return [(g, r) for g, r in items if r.id not in conflicted or g == GROUPS[1]]


def item_line(record: Record, state: State) -> str:
    conflicted = {rid for c in state.conflicts for rid in c.record_ids}
    if record.kind == "obligation":
        head = f"{record.id} obligation, owner {record.owner}"
    elif record.kind == "decision":
        head = f"{record.id} decision" + (f" [{record.topic}]" if record.topic else "")
        if record.id in conflicted:
            head += " CONFLICT"
    else:
        head = f"{record.id} {record.kind}, verified"
    if record.id in state.stale:
        head += " STALE"
    return f"- {head}: {_one_line(record.body)}"


UNCHECKED_LINE = ("(File sources were not checked for staleness: this session's working directory "
                  "is not recorded.)")


def _has_file_sources(state: State) -> bool:
    return any(s.type == "file" for r in state.active for s in r.provenance)


def _omitted_line(n: int) -> str:
    return f"({n} more pinned items omitted; call contexto_state for the full list)"


def render_pinned(state: State, budget_tokens: int, staleness_checked: bool = True) -> str:
    """Pinned text within ``budget_tokens`` (the first line, and the unchecked marker, always included)."""
    items = pinned_items(state)
    lines = [PINNED_FIRST_LINE]
    if not staleness_checked and _has_file_sources(state):
        lines.append(UNCHECKED_LINE)
    used = sum(estimate_tokens(x + "\n") for x in lines)
    group = None
    for i, (heading, record) in enumerate(items):
        new = ([heading] if heading != group else []) + [item_line(record, state)]
        cost = sum(estimate_tokens(x + "\n") for x in new)
        rest = len(items) - i - 1
        reserve = estimate_tokens(_omitted_line(rest)) if rest else 0
        if used + cost + reserve > budget_tokens:
            lines.append(_omitted_line(len(items) - i))
            break
        lines += new
        used += cost
        group = heading
    return "\n".join(lines)


def render_state(state: State, staleness_checked: bool = True) -> str:
    """The full pinned state with no cap, plus decision conflicts."""
    lines = [PINNED_FIRST_LINE]
    if not staleness_checked and _has_file_sources(state):
        lines.append(UNCHECKED_LINE)
    group = None
    for heading, record in pinned_items(state):
        if heading != group:
            lines.append(heading)
            group = heading
        lines.append(item_line(record, state))
    for c in state.conflicts:
        lines.append(f"Conflict on topic {c.topic}: {', '.join(c.record_ids)}")
    return "\n".join(lines)
