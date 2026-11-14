"""``contexto project show`` and ``contexto project forget`` (D69): what later sessions of a project
carry from its earlier ones, for a person to see and to take out.

The project is the one of ``--dir`` (default the current directory), found as a launch finds it
(``contexto.store.project.project_root``): the repository a directory is in, else the directory.

``show`` reads and changes nothing: the sessions are read with the lock-free readers of
``contexto.store.project``, whether one is in use is the probe ``sessions prune`` makes, and a home
that does not exist is not created. It lists every session of the project, newest first, and the
records a session started now would carry, in the order they would be carried.

``forget`` takes records (``--record``) or whole sessions (``--session``, ``--all``) out of project
memory. Every change is one ``contexto.store.project.forget`` call, which writes the session's
``meta.json`` and nothing else, so no file is ever deleted here: the text stays on disk until
``contexto sessions prune``. It says what it will do before it does it and asks, unless ``--yes``;
with no terminal to ask on it refuses rather than guess. The question goes to stderr, so it is seen
when stdout is redirected. What cannot be done is reported in one line
each on stderr, the rest is still done, and the exit code is 1 when anything asked for was not done.

Text a record holds is printed on one line, whole, with every character that is not printable shown
as its escape (``_safe``), so a pinned body cannot start a line of its own, forge a line of the
listing or the question, or move the cursor. ``--json`` gives the text itself, escaped as JSON
escapes it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from contexto.commands._common import EXIT_ERROR, EXIT_OK, EXIT_USAGE, fail
from contexto.commands._common import safe_text as _safe
from contexto.commands.sessions import _iso, _table
from contexto.paths import contexto_home, session_dir
from contexto.store import SessionNotFoundError
from contexto.store.ledger import LedgerCorruptError
from contexto.store.model import _RECORD_ID
from contexto.store.project import Carried, Member, Project, forget, project_root, read_records
from contexto.store.sessions import check_home, in_use, last_activity
from contexto.store.sessions import named as name_sessions

EXIT_INTERRUPTED = 130  # Ctrl+C at the question, as a shell reports it
CARRIED_KINDS = ("obligation", "decision")
GIVEN_SHOWN = 40  # characters of a mistyped id an error repeats
DIR_HELP = "a directory of the project (default: the current directory)"
IN_USE_NOTE = ("in use: it keeps running, stops contributing to later sessions, and stops sending what it carried "
               "from its next turn start")
ON_DISK = "Nothing was deleted: the text of these sessions is still on disk. To delete it:"


class _Refused(Exception):
    """What was asked cannot be done; the message is the one line that says why."""


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "project", help="show and forget what a project's later sessions carry",
        description="Show and forget project memory: the open obligations and decisions that a new session "
                    "carries from the earlier sessions started in the same repository or directory.")
    sub = parser.add_subparsers(title="project commands", metavar="<show|forget>", required=True)
    show = sub.add_parser(
        "show", help="list the project's sessions and the records a new session would carry",
        description="Print the project's root and id, each of its sessions newest first, and the records a "
                    "session started now would carry, in the order they are carried. Changes nothing.")
    show.add_argument("--dir", metavar="PATH", help=DIR_HELP)
    show.add_argument("--json", action="store_true", help="print JSON")
    show.set_defaults(func=run_show)
    forgetting = sub.add_parser(
        "forget", help="take records or sessions out of project memory (asks first unless --yes)",
        description="Take records, or whole sessions, out of project memory, so that later sessions of the "
                    "project do not carry them. Nothing is deleted: the text stays on disk until `contexto "
                    "sessions prune`. No command undoes it.")
    forgetting.add_argument("--dir", metavar="PATH", help=DIR_HELP)
    what = forgetting.add_mutually_exclusive_group(required=True)
    what.add_argument("--record", nargs="+", action="extend", metavar="REC_ID",
                      help="forget these records, each one that `contexto project show` lists as carried")
    what.add_argument("--session", nargs="+", action="extend", metavar="ID",
                      help="take these sessions of the project out of it, each by its full session id")
    what.add_argument("--all", action="store_true", help="take every session of the project out of it")
    forgetting.add_argument("--yes", action="store_true", help="do not ask before forgetting")
    forgetting.set_defaults(func=run_forget)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _view(args: argparse.Namespace) -> tuple[Path, Project]:
    """(home, the view a session started now in ``--dir`` would have). ``_Refused`` for a home that is
    not a directory and for a ``--dir`` that is not one. Creates nothing."""
    home = contexto_home()
    try:
        check_home(home)
    except SessionNotFoundError as e:
        raise _Refused(str(e)) from e
    given = args.dir
    named = "the current directory" if given is None else f"--dir {given!r}"
    try:
        directory = Path.cwd() if given is None else Path(given)
        # A directory that is not there has no project of its own: its root would be a guess from its
        # parents, and a mistyped path would read as a project with no sessions.
        if given == "" or not directory.is_dir():
            raise _Refused(f"{named} is not a directory")
        root = project_root(directory)
    except (OSError, RuntimeError, ValueError) as e:  # a deleted current directory, a loop of symlinks
        raise _Refused(f"{named} cannot be resolved: {e}") from e
    return home, Project(home, root)


@dataclass(frozen=True)
class _Row:
    """What is shown of one session of the project. None where a part of the session cannot be read."""

    member: Member
    last_activity: float | None
    pages: int | None
    records: int | None
    carried: int  # of its records, how many a session started now would carry
    skipped: str | None  # the error class when its ledger cannot be read, so nothing of it is carried
    in_use: bool

    def cells(self) -> tuple[str, ...]:
        return (_safe(self.member.id), _iso(self.member.created),
                "?" if self.last_activity is None else _iso(self.last_activity),
                "?" if self.pages is None else str(self.pages),
                "?" if self.records is None else str(self.records), str(self.carried))

    def notes(self, in_use: str) -> str:
        return "; ".join(([f"skipped as damaged ({self.skipped})"] if self.skipped else [])
                         + ([in_use] if self.in_use else []))

    def as_json(self) -> dict:
        return {"id": self.member.id, "created": _iso(self.member.created),
                "last_activity": None if self.last_activity is None else _iso(self.last_activity),
                "pages": self.pages, "records": self.records, "carried": self.carried,
                "project_memory": self.member.memory, "skipped": self.skipped is not None,
                "skipped_error": self.skipped, "in_use": self.in_use}


def _page_count(pages_dir: Path) -> int | None:
    """Page files in ``pages_dir``, none of them read; None when the directory cannot be listed."""
    try:
        with os.scandir(pages_dir) as entries:
            return sum(1 for e in entries if e.name.startswith("pg_") and e.name.endswith(".json")
                       and e.is_file(follow_symlinks=False))
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def _rows(home: Path, view: Project, carried: Sequence[Carried], members: Sequence[Member]) -> list[_Row]:
    """The rows of ``members``, each read without changing the session (a session deleted meanwhile
    reads as empty or unknown, never as an error)."""
    counts = Counter(item.session for item in carried)
    skipped = {s.session: s.error for s in view.skipped}
    rows = []
    for member in members:
        try:
            active: float | None = last_activity(member.dir)
        except OSError:
            active = None
        records: int | None = None
        error = skipped.get(member.id)
        if error is None:
            try:
                records = len(read_records(member.dir))
            except (OSError, LedgerCorruptError) as e:  # of a session whose memory is off: not read for the carry
                error = type(e).__name__ if member.memory else None
        rows.append(_Row(member, active, _page_count(member.pages_dir), records, counts[member.id], error,
                         in_use(home, member.id)))
    return rows


def _carried_line(item: Carried) -> str:
    record = item.record
    if record.kind == "obligation":
        head = f"{record.id} obligation, owner {_safe(record.owner or '')}"
    else:
        head = f"{record.id} {record.kind}" + (f" [{_safe(record.topic)}]" if record.topic else "")
    return f"  {head} (session {_safe(item.label)}, {item.date}): {_safe(record.body)}"


def _carried_json(item: Carried) -> dict:
    record = item.record
    return {"id": record.id, "kind": record.kind, "owner": record.owner, "topic": record.topic,
            "session": item.session, "label": item.label, "date": item.date, "created_at": record.created_at,
            "text": record.body}


def _unreadable_line(home: Path, n: int) -> str:
    return (f"{n} session director{'y' if n == 1 else 'ies'} under {_safe(str(home / 'sessions'))} could not be "
            f"read and {'is' if n == 1 else 'are'} in no project.")


def _no_sessions_line(home: Path, view: Project) -> str:
    line = f"No sessions of project {_safe(str(view.root))} (id {view.id}) under {_safe(str(home / 'sessions'))}."
    return line + (f" {_unreadable_line(home, view.unreadable)}" if view.unreadable else "")


def _print_project(view: Project) -> None:
    print(f"Project: {_safe(str(view.root))}")
    print(f"Project id: {view.id}")


def run_show(args: argparse.Namespace) -> int:
    try:
        home, view = _view(args)
    except _Refused as e:
        return fail(str(e))
    carried = view.carried()
    rows = _rows(home, view, carried, view.sessions)
    if args.json:
        print(json.dumps({"root": str(view.root), "id": view.id, "sessions": [row.as_json() for row in rows],
                          "carried": [_carried_json(item) for item in carried],
                          "unreadable_sessions": view.unreadable}, indent=2, sort_keys=True))
        return EXIT_OK
    if not rows:
        print(_no_sessions_line(home, view))
        return EXIT_OK
    _print_project(view)
    print("Sessions, newest first:")
    table = [("ID", "CREATED", "LAST ACTIVITY", "PAGES", "RECORDS", "CARRIED", "MEMORY", "NOTES")]
    table += [(*row.cells(), "on" if row.member.memory else "off", row.notes("in use")) for row in rows]
    _table(table, indent="  ")
    if carried:
        print(f"Records a session started now would carry ({len(carried)}), in the order they are carried:")
        for item in carried:
            print(_carried_line(item))
    else:
        print("A session started now would carry no records.")
    if view.unreadable:
        print(_unreadable_line(home, view.unreadable))
    return EXIT_OK


def _confirm(question: str, yes: bool) -> int | None:
    """None to go on: ``--yes``, or a person answered ``y`` or ``yes``. Else the exit code, with nothing
    forgotten. Only a terminal is asked: input that is closed, a file or a pipe is nobody's answer.
    The question is written to stderr, after what was printed so far, so it is seen when stdout is
    redirected."""
    if yes:
        return None
    stdin = sys.stdin
    try:
        interactive = stdin is not None and stdin.isatty()
    except (OSError, ValueError):  # a closed stream
        interactive = False
    if not interactive:
        return fail("nothing was forgotten: there is no terminal to confirm on; rerun with --yes")
    sys.stdout.flush()
    print(f"{question} [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = stdin.readline()
    except KeyboardInterrupt:
        answer, code = "", EXIT_INTERRUPTED
    except (OSError, ValueError):
        answer, code = "", EXIT_ERROR
    else:
        if answer.strip().lower() in ("y", "yes"):
            return None
        code = EXIT_ERROR
    if not answer.endswith("\n"):  # no Enter was typed: end the question's line
        print(file=sys.stderr, flush=True)
    print("Nothing was forgotten.")
    return code


def _print_on_disk(session_ids: Sequence[str]) -> None:
    """The closing lines: nothing was deleted, and the command that deletes each session's text."""
    if session_ids:
        print(ON_DISK)
        for session_id in dict.fromkeys(session_ids):
            print(f"  contexto sessions prune --session {_safe(shlex.quote(session_id))} --yes")


def _forgotten_in(view: Project, given: str) -> str:
    """The session whose forgotten list already holds the record ``given``, which is not carried.
    ``_Refused``, naming the id, for anything else that is not carried: only a carried record can be
    forgotten, and a record of another project reads as one that does not exist."""
    if not _RECORD_ID.fullmatch(given):
        raise _Refused(f"{given[:GIVEN_SHOWN]!r}{'...' if len(given) > GIVEN_SHOWN else ''} is not a record id")
    found = []  # oldest session first; more than one only when a session directory was copied
    for member in reversed(view.sessions):
        try:
            found += [(member, record) for record in read_records(member.dir) if record.id == given]
        except (OSError, LedgerCorruptError):  # a ledger that cannot be read holds no record anyone carries
            continue
    if not found:
        raise _Refused(f"record {given} was not found in this project")
    for member, _ in found:
        if given in member.forgotten:
            return member.id
    member, record = next((pair for pair in found if pair[0].memory), found[0])
    state = view.state()
    if not member.memory:
        why = f"its session {_safe(member.id)} is out of project memory"
    elif record.kind not in CARRIED_KINDS:
        why = f"{'an' if record.kind[0] in 'aeiou' else 'a'} {record.kind} is never carried"
    elif given in state.superseded_by:
        why = "it was superseded"
    elif record.kind == "obligation" and all(r.id != given for r in state.open_obligations):
        why = "it is closed"
    elif given not in member.main_records:
        why = "it was not pinned in a main conversation"
    else:
        why = "a session started now would not carry it"
    raise _Refused(f"record {given} is not carried: {why}")


def _forget_records(args: argparse.Namespace, home: Path, view: Project) -> int:
    carried = {item.record.id: item for item in view.carried()}
    chosen: list[Carried] = []
    already: list[tuple[str, str]] = []  # (record id, its session): forgotten before, which is not an error
    refused = failed = 0
    for given in dict.fromkeys(args.record):
        if given in carried:
            chosen.append(carried[given])
            continue
        try:
            already.append((given, _forgotten_in(view, given)))
        except _Refused as e:
            refused += 1
            fail(str(e))
    if not chosen and not already:
        return EXIT_ERROR
    _print_project(view)
    for record_id, session_id in already:
        print(f"already forgotten: {record_id} (session {_safe(session_id)})")
    forgotten_from = [session_id for _, session_id in already]
    if chosen:
        print(f"Records to forget ({len(chosen)}): later sessions of this project will not carry them.")
        for item in chosen:
            print(_carried_line(item))
        stop = _confirm(f"Forget {_count(len(chosen), 'record')}?", args.yes)
        if stop is not None:
            return stop
        by_session: dict[str, list[str]] = {}
        for item in chosen:
            by_session.setdefault(item.session, []).append(item.record.id)
        done = 0
        for session_id, record_ids in by_session.items():
            try:
                forget(home, session_id, records=record_ids)
            except (SessionNotFoundError, ValueError, OSError) as e:
                why = (f"session {_safe(session_id)} no longer exists (pruned since it was listed)"
                       if isinstance(e, SessionNotFoundError) else str(e))
                failed += len(record_ids)
                for record_id in record_ids:
                    fail(f"record {record_id} was not forgotten: {why}")
                continue
            done += len(record_ids)
            forgotten_from.append(session_id)
            for record_id in record_ids:
                print(f"forgot {record_id} (session {_safe(session_id)})")
        print(f"Forgot {_count(done, 'record')}" + (f"; {failed} not forgotten." if failed else "."))
    _print_on_disk(forgotten_from)
    return EXIT_ERROR if refused or failed else EXIT_OK


def _session_named(home: Path, view: Project, given: str) -> Member:
    """The session of this project whose id is ``given``. Only this project's sessions are looked at, so a
    session of another project is refused exactly as one that does not exist. A prefix never names one:
    ``_starts`` refuses it first, since forgetting cannot be undone (D57, as ``sessions prune``)."""
    try:
        session_dir(home, given)
    except ValueError as e:
        raise _Refused(str(e)) from e
    exact = [m for m in view.sessions if m.id == given]
    if exact:
        return exact[0]
    raise _Refused(f"{given!r} is not a session of this project")


def _starts(view: Project, given: str) -> str | None:
    """The refusal for ``given`` when it is not a session id of this project but starts one or more,
    worded as ``contexto sessions prune --session`` words it; None otherwise."""
    if any(m.id == given for m in view.sessions):
        return None
    ids = sorted(_safe(m.id) for m in view.sessions if given and m.id.startswith(given))
    return (f"--session {given!r} is not a session but the start of {name_sessions(ids)}; forget needs the full id"
            if ids else None)


def _forget_sessions(args: argparse.Namespace, home: Path, view: Project) -> int:
    chosen: list[Member] = []
    already: list[Member] = []  # out of project memory before, which is not an error
    refused = failed = 0
    if args.all:
        chosen = [m for m in view.sessions if m.memory]
        if not chosen:
            print(f"Every session of this project is already out of project memory "
                  f"({_count(len(view.sessions), 'session')})." if view.sessions else _no_sessions_line(home, view))
            return EXIT_OK
    else:
        starts = [message for given in dict.fromkeys(args.session) if (message := _starts(view, given))]
        if starts:  # a usage error: nothing is forgotten
            for message in starts:
                fail(message)
            return EXIT_USAGE
        seen: set[str] = set()
        for given in dict.fromkeys(args.session):
            try:
                member = _session_named(home, view, given)
            except _Refused as e:
                refused += 1
                fail(str(e))
                continue
            if member.id not in seen:
                seen.add(member.id)
                (chosen if member.memory else already).append(member)
        if not chosen and not already:
            return EXIT_ERROR
    _print_project(view)
    for member in already:
        print(f"already out of project memory: session {_safe(member.id)}")
    forgotten = [member.id for member in already]
    if chosen:
        print(f"Sessions to take out of project memory ({len(chosen)}): later sessions of this project will "
              "carry nothing from them; what they closed or replaced stays closed or replaced.")
        table = [("ID", "CREATED", "LAST ACTIVITY", "PAGES", "RECORDS", "CARRIED", "NOTES")]
        table += [(*row.cells(), row.notes(IN_USE_NOTE)) for row in _rows(home, view, view.carried(), chosen)]
        _table(table, indent="  ")
        stop = _confirm(f"Forget {_count(len(chosen), 'session')}?", args.yes)
        if stop is not None:
            return stop
        for member in chosen:
            try:
                forget(home, member.id, leave=True)
            except (SessionNotFoundError, ValueError, OSError) as e:
                why = ("it no longer exists (pruned since it was listed)" if isinstance(e, SessionNotFoundError)
                       else str(e))
                failed += 1
                fail(f"session {_safe(member.id)} was not forgotten: {why}")
                continue
            forgotten.append(member.id)
            print(f"forgot session {_safe(member.id)}")
        done = len(chosen) - failed
        print(f"Forgot {_count(done, 'session')}" + (f"; {failed} not forgotten." if failed else "."))
    _print_on_disk(forgotten)
    return EXIT_ERROR if refused or failed else EXIT_OK


def run_forget(args: argparse.Namespace) -> int:
    try:
        home, view = _view(args)
    except _Refused as e:
        return fail(str(e))
    if args.record is not None:
        return _forget_records(args, home, view)
    return _forget_sessions(args, home, view)
