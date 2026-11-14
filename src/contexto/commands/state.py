"""``contexto state``: the session's full pinned state, without the budget cap.

By default for a person (D57): the pinned records by group, or "No pinned records.", then how many
turns are paged out and the commands that list and search them. Records carried from earlier
sessions of the project (D69) follow the session's own, each with its session and date. ``--raw``
prints the text the MCP ``contexto_state`` tool gives the model, whose opening lines and carried
heading are addressed to the model. A character of a stored record that could move the cursor, end the
line, or reorder or hide text is shown as its escape, in both forms, as ``contexto project show`` shows
it (``safe_text``); joiners, variation selectors and other format characters print as they are, so
``--raw`` differs from the model's text only there. ``--json`` prints the same records,
the carried ones under ``carried``, and the count as one JSON object for a script (D64).
"""

from __future__ import annotations

import argparse
import json
import os

from contexto.commands._common import EXIT_OK, add_session_option, fail, safe_text, say_session
from contexto.paths import contexto_home
from contexto.stats import session_faults
from contexto.store import SessionNotFoundError, SessionStore
from contexto.store.ledger import LedgerError
from contexto.store.pinned import GROUPS, READABLE_CARRIED_HEADING, pinned_items, render_state
from contexto.store.sessions import select_session


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "state", help="print the full pinned state",
        description="Print the decisions, open obligations, and verified facts the model pinned in a "
                    "session, with no budget cap, and how many turns are paged out. With --json, one "
                    "object: session, pinned (open_obligations, active_decisions and verified_facts, each a "
                    "list of records, newest first, with id, kind, body, created_at, owner, topic, stale and "
                    "conflict), conflicts (topic and record_ids), staleness_checked, and pages (the number "
                    "of turns paged out; null, with the error on stderr and exit 1, when they cannot be "
                    "listed), and faults (the store faults of the latest run, the object `contexto stats --json` "
                    "gives under that key, docs/stats.md). stale is null when staleness_checked is false. "
                    "carried lists the records carried from earlier sessions of the project (D69), in the order "
                    "they are carried, with id, kind, body, created_at, owner, topic, conflict, session (its full "
                    "id), label and date.")
    add_session_option(parser)
    form = parser.add_mutually_exclusive_group()
    form.add_argument("--raw", action="store_true",
                      help="print what the MCP contexto_state tool returns to the model, including its "
                           "instructions to the model; a character that could move the cursor or reorder or "
                           "hide text is shown as its escape")
    form.add_argument("--json", action="store_true", help="print JSON; an error is still one line on stderr")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    home = contexto_home()
    try:
        session_id, how = select_session(home, args.session)
    except SessionNotFoundError as e:
        return fail(str(e))
    say_session(session_id, how)
    store = SessionStore.open_existing(home, session_id)
    if args.json:
        return run_json(store, home)
    try:
        if args.raw:
            print(terminal(store.state_text()))
            return EXIT_OK
        root = store.workdir()
        state, carried = store.pinned_state(root)
        pinned = render_state(state, staleness_checked=root is not None, opening=(),
                              carried=carried, carried_heading=READABLE_CARRIED_HEADING, quoted=False)
    except LedgerError as e:
        return fail(f"cannot read the ledger of session {session_id}: {e}")
    print(terminal(pinned) or "No pinned records.")
    print()
    try:
        pages = page_count(store)
    except OSError as e:  # the pins stand; the page count is reported in one line
        return fail(f"the pages of session {session_id} cannot be read: {e}")
    if pages:
        print(f"{pages} turn{'' if pages == 1 else 's'} paged out: list them with "
              f"`contexto sessions show {session_id}`, search them with "
              f"`contexto search --session {session_id} WORDS`.")
    else:
        print("No turns paged out yet.")
    faults = session_faults(home, session_id)  # D64: a store that stopped paging in the latest run, and what to do
    for message in faults["messages"]:
        print(f"\n{'Warning' if faults['degraded']['now'] else 'Note'}: {message[0].upper()}{message[1:]}")
    return EXIT_OK


def terminal(text: str) -> str:
    """``text`` with each line's characters that ``safe_text`` escapes shown as escapes: a
    stored record's text cannot move the cursor, erase a line or reorder what is shown (D69)."""
    return "\n".join(safe_text(line) for line in text.split("\n"))


def page_count(store: SessionStore) -> int:
    """Page files in the store (no page is read); OSError when the pages directory cannot be listed."""
    try:
        names = os.listdir(store.pages_dir)
    except FileNotFoundError:
        return 0
    return sum(1 for n in names if n.startswith("pg_") and n.endswith(".json") and (store.pages_dir / n).is_file())


JSON_GROUPS = dict(zip(GROUPS, ("open_obligations", "active_decisions", "verified_facts"), strict=True))


def run_json(store: SessionStore, home) -> int:
    """``state --json`` (D64): the records the text view lists, in its groups and order, with each record's
    text as it was pinned, and the page count. A ledger that cannot be read prints nothing on stdout; a
    pages directory that cannot be listed gives ``"pages": null``, the error line and exit 1, as
    ``sessions show --json`` reports its page list. ``faults`` is the object ``contexto stats --json`` gives
    under the same key (``contexto.stats.session_faults``): what made the store stop paging in the latest run,
    whether it still is, and what to do."""
    session_id = store.session_id
    try:
        root = store.workdir()
        state, carried = store.pinned_state(root)
    except LedgerError as e:
        return fail(f"cannot read the ledger of session {session_id}: {e}")
    conflicted = {rid for c in state.conflicts for rid in c.record_ids}
    pinned: dict[str, list[dict]] = {key: [] for key in JSON_GROUPS.values()}
    for heading, r in pinned_items(state):
        pinned[JSON_GROUPS[heading]].append({
            "id": r.id, "kind": r.kind, "body": r.body, "created_at": r.created_at, "owner": r.owner,
            "topic": r.topic, "stale": r.id in state.stale if root is not None else None,
            "conflict": r.id in conflicted})
    try:
        pages, unreadable = page_count(store), None
    except OSError as e:
        pages, unreadable = None, f"the pages of session {session_id} cannot be read: {e}"
    conflicts = [{"topic": c.topic, "record_ids": list(c.record_ids)} for c in state.conflicts]
    carried_out = [{"id": c.record.id, "kind": c.record.kind, "body": c.record.body,
                    "created_at": c.record.created_at, "owner": c.record.owner, "topic": c.record.topic,
                    "conflict": c.conflict, "session": c.session, "label": c.label, "date": c.date}
                   for c in carried]
    print(json.dumps({"session": session_id, "pinned": pinned, "conflicts": conflicts, "carried": carried_out,
                      "staleness_checked": root is not None, "pages": pages,
                      "faults": session_faults(home, session_id)}, indent=2, sort_keys=True))
    return fail(unreadable) if unreadable else EXIT_OK
