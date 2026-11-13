"""What a session did, as numbers: read from its ``log.jsonl`` and its store, never written (D64).

Contexto's value is in each session's log (how large the prompt got against the bound, how many turns
were paged, how often the model read them back, how much of the input came from cache), and until D64
nothing showed it: ``contexto sessions show`` prints event counts. ``session_stats`` turns one session
into a plain dict, which ``contexto stats`` prints, ``contexto run`` summarises at exit, and a status
line can call after every assistant message. docs/stats.md defines every number.

Rules this module keeps:

- It only reads: ``log.jsonl``, the ledger, and ``pages/``. Nothing is created, and no network call is
  made. A page file is read at its last bytes only (the token estimate is the last-but-one key of a
  page's canonical JSON), so a store of thousands of pages costs one small read each.
- A damaged log is read as far as it goes. A line cut short by a full disk, a line that is not a JSON
  object, and a field of the wrong type are skipped, each on its own, and counted in ``log``; a
  missing log gives empty numbers. Nothing here raises for what a log line holds.
- A number is reported only when the log or the store supports it, and each says whether the API
  measured it (``usage``) or Contexto estimated it.

The paths and the event names are the proxy's and the engine's. They are repeated here rather than
imported so that a status line does not load the proxy to read a log; tests/test_stats.py checks that
they agree.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from contexto.paths import session_dir
from contexto.private import open_regular, read_regular

STATS_VERSION = 1  # the ``--json`` shape; raised only when a documented key changes meaning or goes

MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
MODEL_PATHS = frozenset({MESSAGES_PATH, "/responses", "/v1/responses"})  # a prompt sent for a completion
TOOL_NAMES = ("recall", "search", "state", "pin", "close")  # Contexto's MCP tools, without ``contexto_``

# Events that mean Contexto left its designed path: the contract's acceptance list (M2, check 1) without
# ``turn_room_low``, which a small bound logs once per epoch in normal use, and with the events for
# damage and bugs that are logged outside the engine. Each maps to what it means; docs/stats.md has the
# reasons, and the events that are deliberately not counted.
FAULT_EVENTS = {
    "degraded_enter": "paging stopped after a store failure",
    "store_error": "the store could not be read or written",
    "page_conflict": "a stored page did not match the turn it should hold",
    "fallback_failed": "a request was forwarded as the CLI sent it, unbounded",
    "transform_error": "a Contexto bug: a request was forwarded as the CLI sent it",
    "proxy_error": "the proxy failed while handling a request",
    "request_too_deep": "a request nested too deeply was forwarded as the CLI sent it",
    "engine_unavailable": "the rewrite engine was missing, so nothing was bounded",
    "websocket_rejected": "the CLI tried a WebSocket, which Contexto refuses",
    "bound_too_small": "the bound could not hold one turn, so requests exceeded it",
    "carried_over_budget": "content Contexto cannot page grew past its budget",
    "unclassified_system_message": "the CLI sent a system message of a kind Contexto does not know",
    "session_start_unrecognised": "the CLI's session start was not recognised",
    "page_quarantined": "a damaged page was moved to quarantine",
    "page_unreadable": "a page file could not be read",
    "ledger_damaged": "the ledger of pins is damaged",
    "retained_stale": "the snapshot of the conversation in view could not be written",
    "epoch_record_stale": "the record a restart resumes from could not be written",
    "side_room_close_failed": "a subagent's recall room could not be closed",
    "mcp_tools_missing": "the model had no Contexto tools",
    "mcp_tools_denied": "the CLI refused a Contexto tool",
    "turn_room_missing": "a Contexto tool found no recall room for its turn",
    "toolcheck_error": "the check for Contexto's tools failed",
    "cli_version_error": "the check of the CLI's version failed",
    "observe_usage_error": "reading a response's usage failed",
}

DISK_FULL = frozenset({"ENOSPC", "EDQUOT"})
NO_ACCESS = frozenset({"EACCES", "EPERM", "EROFS"})
UNPAGED = "requests go out unpaged and can exceed the bound"
HOME_HINT = "$CONTEXTO_HOME (default ~/.contexto)"

# The end of a page file: canonical JSON sorts a page's keys, so ``tokens`` and ``turn`` come last.
_PAGE_TAIL = re.compile(rb'[,{]"tokens":(\d+),"turn":-?\d+\}\s*\Z')
_PAGE_TAIL_BYTES = 96


def _int(value: Any) -> int | None:
    """``value`` when it is a whole number that is not a bool, else None: a log line can hold anything."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(value: Any) -> int:
    number = _int(value)
    return number if number is not None and number > 0 else 0


def _events(value: Any) -> Iterator[dict[str, Any]]:
    """The events nested in a request line (``transform_events`` or ``usage_events``) that are objects."""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def log_lines(directory: Path) -> tuple[list[dict[str, Any]], int, bool]:
    """(the log's JSON objects in order, lines that could not be read, whether there is no readable
    log). The file is read as ``read_regular`` reads it: a FIFO, directory or symlink in its place is
    never waited on or followed, and counts as no log."""
    try:
        data = read_regular(Path(directory) / "log.jsonl")
    except OSError:
        return [], 0, True
    lines = [line for line in data.split(b"\n") if line.strip()]
    try:
        # A healthy log parses in one call, about twice as fast as a call per line (docs/stats.md). Any
        # damaged line fails it, or changes the number of values, and the log is then read line by line.
        parsed = json.loads(b"[" + b",".join(lines) + b"]")
    except (ValueError, RecursionError):
        parsed = None
    if parsed is None or len(parsed) != len(lines):
        parsed = []
        for line in lines:
            try:
                parsed.append(json.loads(line))
            except (ValueError, RecursionError):  # cut short by a full disk, not UTF-8, or nested too deep
                parsed.append(None)
    events = [event for event in parsed if isinstance(event, dict)]
    return events, len(parsed) - len(events), False


def store_fault_cause(failure: dict[str, Any]) -> str:
    """What a ``store_error`` or ``page_conflict`` event means for the user: ``disk_full``, ``no_access``
    (permissions or a read-only filesystem), ``page_conflict``, or ``store`` (anything else)."""
    if failure.get("event") == "page_conflict":
        return "page_conflict"
    code = failure.get("errno") if isinstance(failure.get("errno"), str) else None
    if code in DISK_FULL:
        return "disk_full"
    return "no_access" if code in NO_ACCESS else "store"


def store_fault_message(failure: dict[str, Any], session_id: str | None = None) -> str:
    """One paragraph for a store failure: what happened, what it costs, and what to do next. Built only
    from the event's content-free fields (an errno name, an operation, an exception class, a page id)."""
    cause = store_fault_cause(failure)
    operation = failure.get("operation") if isinstance(failure.get("operation"), str) else "a store call"
    error = failure.get("error") if isinstance(failure.get("error"), str) else "an error"
    code = failure.get("errno") if isinstance(failure.get("errno"), str) else None
    detail = f"{code} during {operation}" if code else f"{error} during {operation}"
    retry = "Contexto retries at each turn start and pages again once the store works; turns already paged are kept."
    if cause == "disk_full":
        return (f"the disk that holds Contexto's store is full ({detail}), so paging has stopped: {UNPAGED}. "
                f"Free space on the disk that holds {HOME_HINT}. {retry}")
    if cause == "no_access":
        return (f"Contexto cannot write its store ({detail}), so paging has stopped: {UNPAGED}. Check the owner "
                f"and permissions of the session directory under {HOME_HINT}, and that its filesystem is not "
                f"read-only. {retry}")
    if cause == "page_conflict":
        page = failure.get("page_id") if isinstance(failure.get("page_id"), str) else "a page"
        return (f"a stored page ({page}) does not match the turn it should hold, so paging has stopped: {UNPAGED}. "
                f"At the next turn start Contexto moves that file to the session's quarantine folder (nothing is "
                f"deleted) and pages the turn again. If it happens again, check the disk under {HOME_HINT}.")
    show = f"contexto sessions show {session_id}" if session_id else "contexto sessions show ID"
    return (f"Contexto's store failed ({detail}), so paging has stopped: {UNPAGED}. {retry} Run `{show}` to check "
            f"the session's ledger and pages.")


def fault_notices(event: Any, session_id: str | None = None) -> list[tuple[str, str]]:
    """(key, message) for each store failure in a log event, for the launcher's ``NoticeLog``: the event
    itself, or the ones nested in a request line. One key per cause, so a disk that stays full is said
    once and a second cause is still said. Never raises: it runs on every event the proxy logs."""
    if not isinstance(event, dict):
        return []
    found = [event] if event.get("event") != "request" else [
        *_events(event.get("transform_events")), *_events(event.get("usage_events"))]
    return [(f"store_fault_{store_fault_cause(e)}", store_fault_message(e, session_id))
            for e in found if e.get("event") in ("store_error", "page_conflict")]


def log_unwritable_message(error: BaseException) -> str:
    """The notice for a session log that cannot be appended to: stats and the exit summary read that log,
    so they undercount from here on. Names the errno or the class, never the exception's text (paths)."""
    code = errno.errorcode.get(getattr(error, "errno", None) or 0, type(error).__name__)
    advice = (f"Free space on the disk that holds {HOME_HINT}." if code in DISK_FULL
              else f"Check the session directory under {HOME_HINT}.")
    return (f"the session log could not be written ({code}), so `contexto stats` and the summary below miss "
            f"what happened from here on. {advice}")


class _Tally:
    """The numbers of one scope (the whole log, or its last run), filled one log line at a time."""

    def __init__(self) -> None:
        self.cli: str | None = None
        self.bound: int | None = None
        self.first_ts: str | None = None
        self.last_ts: str | None = None
        self.requests = {"model": 0, "main": 0, "side": 0, "with_usage": 0, "failed": 0, "status": {},
                         "aborted": 0, "count_tokens": 0, "other": 0}
        self.peak: tuple[int, str] | None = None  # the largest prompt the API counted
        self.peak_estimate: int | None = None  # the largest the engine estimated before sending
        self.last: tuple[int, str] | None = None  # the main conversation's most recent prompt
        self.over = {"requests": 0, "measured": 0, "estimate_only": 0, "in_tool_loop": 0, "at_turn_start": 0,
                     "degraded": 0}
        self.paging = {"rotations": 0, "turns_paged": 0, "pages_written": 0, "carried_items_paged": 0}
        self.tools = {name: {"calls": 0, "failed": 0, "charged_tokens": 0} for name in TOOL_NAMES}
        self.out_of_room = 0
        self.cache = {"requests": 0, "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
        self.bytes = {"cli_sent": 0, "forwarded": 0, "largest_cli_request": 0, "largest_forwarded_request": 0}
        self.messages_api = False  # a Messages request was seen: the compaction check applies
        self.compactions = 0
        self.fault_events: dict[str, int] = {}
        self.entered = self.left = self.unpaged = 0
        self.largest_unpaged: int | None = None
        self.since: str | None = None
        self.store_errors: dict[tuple, dict[str, Any]] = {}
        self.degraded = {True: False, False: False}  # by whether the conversation is the main one
        self.seen_main = False
        self.ended_degraded = False  # an earlier run in this scope ended with paging stopped

    def new_engine(self) -> None:
        """A proxy start: the engine's degraded state is in memory, so a new one starts paging again."""
        self.ended_degraded = self.ended_degraded or self.degraded_now()
        self.degraded = {True: False, False: False}
        self.seen_main = False
        self.since = None

    def line(self, event: dict[str, Any]) -> None:
        when = event.get("ts")
        if isinstance(when, str):
            self.first_ts = self.first_ts or when
            self.last_ts = when
        name = event.get("event")
        if name == "request":
            self._request(event, when if isinstance(when, str) else None)
        elif name == "mcp_call":
            self._tool(event)
        elif name == "launch":
            if isinstance(event.get("cli"), str):
                self.cli = event["cli"]
            config = event.get("config")
            bound = _int(config.get("bound")) if isinstance(config, dict) else None
            if bound is not None:
                self.bound = bound
        elif name == "mcp_tools_missing" and event.get("repeated") is True:
            pass  # the exit-time repeat of a warning the run already counted
        elif isinstance(name, str) and name in FAULT_EVENTS:
            self._fault(event, name, when if isinstance(when, str) else None)

    def _fault(self, event: dict[str, Any], name: str, when: str | None) -> None:
        self.fault_events[name] = self.fault_events.get(name, 0) + 1
        if name not in ("store_error", "page_conflict"):
            return
        key = tuple(event.get(k) if isinstance(event.get(k), str) else None
                    for k in ("event", "operation", "error", "errno"))
        entry = self.store_errors.setdefault(key, {
            "event": key[0], "operation": key[1], "error": key[2], "errno": key[3],
            "cause": store_fault_cause(event), "count": 0, "first": when, "last": when,
            "page_id": event.get("page_id") if isinstance(event.get("page_id"), str) else None})
        entry["count"] += 1
        entry["last"] = when or entry["last"]

    def _tool(self, event: dict[str, Any]) -> None:
        tool = event.get("tool")
        name = tool.removeprefix("contexto_") if isinstance(tool, str) else None
        if name not in self.tools:
            return
        counts = self.tools[name]
        counts["calls" if event.get("ok") is True else "failed"] += 1
        counts["charged_tokens"] += _count(event.get("charged_tokens"))
        if event.get("turn_budget") in ("spent", "nearly_spent"):
            self.out_of_room += 1

    def _request(self, event: dict[str, Any], when: str | None) -> None:
        path = event.get("path")
        model = event.get("method") == "POST" and isinstance(path, str) and path in MODEL_PATHS
        if not model:
            self.requests["count_tokens" if path == COUNT_TOKENS_PATH else "other"] += 1
            return
        self.requests["model"] += 1
        self.messages_api = self.messages_api or path == MESSAGES_PATH
        status = _int(event.get("status"))
        if status is not None and status >= 400:
            self.requests["failed"] += 1
            self.requests["status"][str(status)] = self.requests["status"].get(str(status), 0) + 1
        if event.get("client_aborted") is True:
            self.requests["aborted"] += 1
        sent, original = _int(event.get("request_bytes")), _int(event.get("original_request_bytes"))
        if sent is not None and original is not None:
            self.bytes["cli_sent"] += original
            self.bytes["forwarded"] += sent
            self.bytes["largest_cli_request"] = max(self.bytes["largest_cli_request"], original)
            self.bytes["largest_forwarded_request"] = max(self.bytes["largest_forwarded_request"], sent)

        # The engine's own events, in the order it made them; its ``request`` event is the last and says
        # which conversation this was and whether it went out unpaged.
        engine: dict[str, Any] = {}
        flagged = real = False
        in_loop: bool | None = None
        nested = list(_events(event.get("transform_events")))
        for inner in nested:
            if inner.get("event") == "request":
                engine = inner
        main = engine.get("main") is not False
        usage_events = list(_events(event.get("usage_events")))
        for inner in [*nested, *usage_events]:
            kind = inner.get("event")
            if kind == "request":  # the engine's own word on this request, should an enter or leave line be lost
                self.degraded[main] = inner.get("degraded") is True
                if main:
                    self.since = (self.since or when) if self.degraded[main] else None
                continue
            if kind == "over_bound":
                flagged = True
                if inner.get("source") == "real":
                    real = True
                elif in_loop is None:
                    in_loop = inner.get("in_tool_loop") is True
            elif kind == "rotation":
                self.paging["rotations"] += 1
            elif kind == "page_out":
                self.paging["turns_paged"] += _count(inner.get("turns"))
                self.paging["pages_written"] += _count(inner.get("pages"))
            elif kind == "carried_paged":
                self.paging["carried_items_paged"] += _count(inner.get("count"))
            elif kind == "main_changed" and inner.get("reason") == "compacted":
                self.compactions += 1
            elif kind == "degraded_enter":
                self.entered += 1
                self.degraded[main] = True
                if main:
                    self.since = self.since or when
            elif kind == "degraded_leave":
                self.left += 1
                self.degraded[main] = False
                if main:
                    self.since = None
            if self.bound is None and kind in ("over_bound", "bound_too_small"):
                self.bound = _int(inner.get("bound"))  # a log without its launch line still knows the bound
            if isinstance(kind, str) and kind in FAULT_EVENTS:
                self._fault(inner, kind, when)
        self.seen_main = self.seen_main or (main and bool(engine))
        self.requests["main" if main else "side"] += 1

        unpaged = engine.get("degraded") is True
        if unpaged:
            self.unpaged += 1
            if sent is not None:
                self.largest_unpaged = max(self.largest_unpaged or 0, sent)
        if flagged or event.get("over_bound") is True:
            self.over["requests"] += 1
            self.over["measured" if real else "estimate_only"] += 1
            if in_loop is not None:
                self.over["in_tool_loop" if in_loop else "at_turn_start"] += 1
            if unpaged:
                self.over["degraded"] += 1

        prompt = self._usage(event.get("usage"), event.get("api"))
        estimate = _int(engine.get("sent_estimate"))
        estimate = estimate if estimate is not None else _int(engine.get("estimate"))
        if prompt is not None:
            if self.peak is None or prompt > self.peak[0]:
                self.peak = (prompt, "usage")
        if estimate is not None:
            self.peak_estimate = max(self.peak_estimate or 0, estimate)
        if main and (prompt is not None or estimate is not None):
            self.last = (prompt, "usage") if prompt is not None else (estimate, "estimate")

    def _usage(self, usage: Any, api: Any) -> int | None:
        """The prompt tokens the API counted for one request, or None; also adds them to the cache
        numbers. Messages usage reports the cached parts apart from ``input_tokens``; Responses usage
        reports ``cached_tokens`` as a part of it."""
        if not isinstance(usage, dict):
            return None
        fresh = _int(usage.get("input_tokens"))
        if api == "responses":
            details = usage.get("input_tokens_details")
            read = _count(details.get("cached_tokens")) if isinstance(details, dict) else 0
            write, prompt = 0, fresh
        else:
            read, write = _count(usage.get("cache_read_input_tokens")), _count(usage.get("cache_creation_input_tokens"))
            prompt = (fresh or 0) + read + write if fresh is not None or read or write else None
        if prompt is None or prompt <= 0:
            return None
        self.requests["with_usage"] += 1
        self.cache["requests"] += 1
        self.cache["input_tokens"] += prompt
        self.cache["cache_read_tokens"] += min(read, prompt)
        self.cache["cache_write_tokens"] += write
        return prompt

    def degraded_now(self) -> bool:
        """Whether paging is stopped at the end of the scope: the main conversation's state, or any
        conversation's when the scope holds no request of the main one."""
        return self.degraded[True] if self.seen_main else any(self.degraded.values())


def _seconds(first: str | None, last: str | None) -> int | None:
    try:
        return max(0, round((datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds()))
    except (TypeError, ValueError):
        return None


def _page_tokens(path: Path, size: int) -> int | None:
    """The token estimate stored in one page file, from its last bytes; the whole file is parsed only when
    they are not a canonical page's end. None when the file is not a readable page."""
    try:
        fd = open_regular(path)
    except OSError:
        return None
    try:
        match = _PAGE_TAIL.search(os.pread(fd, _PAGE_TAIL_BYTES, max(0, size - _PAGE_TAIL_BYTES)))
        if match:
            return int(match.group(1))
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        page = json.loads(b"".join(chunks))
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        os.close(fd)
    return _int(page.get("tokens")) if isinstance(page, dict) else None


def page_totals(pages_dir: Path, *, tokens: bool = True) -> dict[str, Any]:
    """The pages a session holds on disk: how many, the bytes their files take, and (with ``tokens``)
    the sum of the token estimates made when each turn was paged. Only regular ``pg_*.json`` files
    count; one that cannot be read is counted in ``unreadable`` and adds no tokens. Every value is
    None when the directory cannot be listed."""
    out: dict[str, Any] = {"count": 0, "tokens_estimate": 0 if tokens else None, "file_bytes": 0, "unreadable": 0}
    try:
        entries = list(os.scandir(pages_dir))
    except FileNotFoundError:
        return out
    except OSError:
        return dict.fromkeys(out)
    for entry in entries:
        if not (entry.name.startswith("pg_") and entry.name.endswith(".json")):
            continue
        try:
            status = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(status.st_mode):
            continue
        out["count"] += 1
        out["file_bytes"] += status.st_size
        if tokens:
            found = _page_tokens(Path(entry.path), status.st_size)
            if found is None:
                out["unreadable"] += 1
            else:
                out["tokens_estimate"] += found
    return out


def _pins(directory: Path) -> dict[str, Any]:
    """Active ledger records and open obligations; ``readable`` is False for a damaged ledger."""
    from contexto.store.ledger import Ledger, LedgerError  # lazily: the store is not needed to read a log
    from contexto.store.state import derive

    try:
        state = derive(Ledger(directory).records())
    except (LedgerError, OSError, ValueError):
        return {"readable": False, "active_records": None, "open_obligations": None}
    return {"readable": True, "active_records": len(state.active), "open_obligations": len(state.open_obligations)}


def _fault_report(tally: _Tally, session_id: str, earlier_run: bool) -> dict[str, Any]:
    now = tally.degraded_now()
    store_errors = [{k: v for k, v in entry.items() if k != "page_id"} for entry in tally.store_errors.values()]
    messages = [store_fault_message(entry, session_id) for entry in tally.store_errors.values()]
    if messages or tally.entered:
        count = f"{tally.unpaged:,} request{'' if tally.unpaged == 1 else 's'} went out unpaged"
        if tally.largest_unpaged:
            count += f", the largest {size_text(tally.largest_unpaged)}"
        if now:
            status = f"Paging has not resumed{f' since {tally.since}' if tally.since else ''}: {count}."
        elif earlier_run:
            status = f"That was in an earlier run, which ended with paging stopped ({count}); a new run pages again."
        else:
            status = f"Paging has resumed; {count} meanwhile."
        messages = [*messages[:-1], f"{messages[-1]} {status}"] if messages else [status]
    return {
        "total": sum(tally.fault_events.values()),
        "events": dict(tally.fault_events),
        "degraded": {"entered": tally.entered, "left": tally.left, "now": now, "since": tally.since if now else None,
                     "unpaged_requests": tally.unpaged, "largest_unpaged_request_bytes": tally.largest_unpaged},
        "store_errors": store_errors,
        "messages": messages,
    }


def session_stats(home: Path, session_id: str, *, last_run: bool = False, page_tokens: bool = True,
                  store: bool = True) -> dict[str, Any]:
    """One session's numbers as a plain dict of JSON values (the ``contexto stats --json`` shape,
    ``STATS_VERSION``; docs/stats.md defines each key).

    ``last_run`` counts only the log from the most recent proxy start on (one ``contexto run``); the
    pages and pins are the store's as it stands either way. ``page_tokens=False`` counts the page files
    without opening them, and ``store=False`` skips the ledger and the pages altogether, for a caller
    that wants only the log's numbers. Raises ``SessionNotFoundError`` when the session directory does
    not exist; nothing else is raised for a damaged session."""
    directory = session_dir(home, session_id)
    if directory.is_symlink() or not directory.is_dir():
        from contexto.store import SessionNotFoundError

        raise SessionNotFoundError(f"no session {session_id!r} under {Path(home) / 'sessions'}")
    events, unreadable, missing = log_lines(directory)
    tally, runs = _Tally(), 0
    cli, bound = None, None
    for event in events:
        if event.get("event") == "proxy_start":
            runs += 1
            cli, bound = tally.cli or cli, tally.bound if tally.bound is not None else bound
            if last_run:
                tally = _Tally()
            else:
                tally.new_engine()
        tally.line(event)
    cli, bound = tally.cli or cli, tally.bound if tally.bound is not None else bound
    peak = tally.peak or ((tally.peak_estimate, "estimate") if tally.peak_estimate is not None else None)
    cache, sizes = tally.cache, tally.bytes
    if store:
        pages, pins = page_totals(directory / "pages", tokens=page_tokens), _pins(directory)
    else:
        pages = {"count": None, "tokens_estimate": None, "file_bytes": None, "unreadable": None}
        pins = {"readable": False, "active_records": None, "open_obligations": None}
    return {
        "version": STATS_VERSION,
        "session": session_id,
        "scope": "last_run" if last_run else "session",
        "runs": runs,
        "cli": cli,
        "bound": bound,
        "started": tally.first_ts,
        "ended": tally.last_ts,
        "span_seconds": _seconds(tally.first_ts, tally.last_ts),
        "log": {"missing": missing, "lines": len(events) + unreadable, "unreadable_lines": unreadable},
        "requests": tally.requests,
        "prompt": {"peak_tokens": peak[0] if peak else None, "peak_source": peak[1] if peak else None,
                   "last_tokens": tally.last[0] if tally.last else None,
                   "last_source": tally.last[1] if tally.last else None, "bound": bound},
        "over_bound": tally.over,
        "paging": tally.paging,
        "pages": pages,
        "tools": {**tally.tools, "out_of_room": tally.out_of_room},
        "pins": pins,
        "cache": {**cache, "read_share": (round(cache["cache_read_tokens"] / cache["input_tokens"], 4)
                                          if cache["input_tokens"] else None)},
        "bytes": {**sizes, "forwarded_share": (round(sizes["forwarded"] / sizes["cli_sent"], 4)
                                               if sizes["cli_sent"] else None)},
        "compaction": {"seen": tally.compactions} if tally.messages_api else None,
        "faults": _fault_report(tally, session_id, earlier_run=tally.ended_degraded and not last_run),
    }


def session_faults(home: Path, session_id: str) -> dict[str, Any]:
    """The ``faults`` part of the latest run's stats, for ``contexto state``: whether paging is stopped
    now, why, and what to do (``messages``). Only the log is read."""
    return session_stats(home, session_id, last_run=True, store=False)["faults"]


# Text ------------------------------------------------------------------------------------------------

def size_text(n: int) -> str:
    """``n`` bytes in binary units, as ``contexto sessions list`` prints sizes."""
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024
        if size < 1024 or unit == "GiB":
            break
    return f"{size:.1f} {unit}"


def duration_text(seconds: float) -> str:
    """``42s``, ``12m05s``, ``6h12m``."""
    whole = max(0, int(seconds))
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def _about(tokens: int) -> str:
    """A large token count to two or three figures: ``1.9M``, ``85k``, ``950``."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    return f"{tokens / 1_000:.0f}k" if tokens >= 10_000 else f"{tokens:,}"


def _n(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count:,} {noun if count == 1 else plural or noun + 's'}"


def render(out: dict[str, Any], *, brief: bool = False) -> list[str]:
    """The stats as indented text lines, one per subject, each only when its data exists. ``brief``
    is the exit summary's: the byte line, and the request line unless a request failed, are left to
    ``contexto stats``."""
    lines: list[str] = []
    requests, prompt, over = out["requests"], out["prompt"], out["over_bound"]
    if not requests["model"]:
        lines.append("  requests    no model request in the log")
    elif not brief or requests["failed"]:
        text = f"{_n(requests['model'], 'model request')} ({requests['main']:,} main, {requests['side']:,} side)"
        if requests["failed"]:
            codes = ", ".join(f"{code} x{count}" for code, count in sorted(requests["status"].items()))
            text += f"; {requests['failed']:,} failed ({codes})"
        lines.append(f"  requests    {text}")
    if prompt["peak_tokens"] is not None:
        source = ("measured" if prompt["peak_source"] == "usage"
                  else "Contexto's estimate; the API reported no usage")
        against = f" of {prompt['bound']:,}" if prompt["bound"] else ""
        text = (f"peak {prompt['peak_tokens']:,}{against} tokens ({source}); "
                f"over the bound: {_n(over['requests'], 'request')}")
        if over["requests"]:
            parts = [f"{over['measured']:,} measured by the API", f"{over['estimate_only']:,} by estimate only",
                     f"{over['in_tool_loop']:,} inside a tool loop", f"{over['at_turn_start']:,} at a turn start"]
            if over["degraded"]:
                parts.append(f"{over['degraded']:,} while paging was stopped")
            text += f" ({'; '.join(parts)})"
        lines.append(f"  prompt      {text}")
    paging, pages = out["paging"], out["pages"]
    if paging["rotations"] or pages["count"]:
        parts = []
        if paging["rotations"]:
            parts.append(f"{_n(paging['turns_paged'], 'turn')} in {_n(paging['rotations'], 'rotation')}")
        if pages["count"]:
            text = f"{_n(pages['count'], 'page')} on disk"
            if pages["tokens_estimate"]:
                text += f", about {_about(pages['tokens_estimate'])} tokens (an estimate made when each was paged)"
            if pages["unreadable"]:
                text += f"; {_n(pages['unreadable'], 'page')} could not be read"
            parts.append(text)
        lines.append(f"  paged out   {'; '.join(parts)}")
    tools, pins = out["tools"], out["pins"]
    calls = [f"{tools[name]['calls']:,} {name}" for name in TOOL_NAMES if tools[name]["calls"]]
    if calls or pins["open_obligations"]:
        one = sum(tools[name]["calls"] for name in TOOL_NAMES) == 1
        parts = [f"{', '.join(calls)} {'call' if one else 'calls'}"] if calls else []
        charged = sum(tools[name]["charged_tokens"] for name in TOOL_NAMES)
        if charged:
            parts.append(f"about {_about(charged)} tokens read back (an estimate)")
        if tools["out_of_room"]:
            parts.append(f"{_n(tools['out_of_room'], 'call')} found the turn's recall room used up")
        if pins["open_obligations"]:
            parts.append(f"{_n(pins['open_obligations'], 'obligation')} open")
        lines.append(f"  recall      {'; '.join(parts)}")
    cache = out["cache"]
    if cache["read_share"] is not None:
        lines.append(f"  cache       {cache['read_share']:.0%} of input tokens read from cache (measured)")
    sizes = out["bytes"]
    if sizes["cli_sent"] and not brief:
        lines.append(f"  bytes       the CLI sent {size_text(sizes['cli_sent'])}, Contexto forwarded "
                     f"{size_text(sizes['forwarded'])} ({sizes['forwarded_share']:.0%})")
    compaction = out["compaction"]
    if compaction is not None and requests["model"]:
        seen = compaction["seen"]
        lines.append("  compaction  none seen: Claude Code did not compact the main conversation" if not seen else
                     f"  compaction  Claude Code compacted the main conversation {_n(seen, 'time')}")
    if not brief:
        lines += fault_lines(out)
        if out["log"]["unreadable_lines"]:
            lines.append(f"  log         {_n(out['log']['unreadable_lines'], 'line')} of the log could not be read "
                         "(cut short or damaged), so the numbers above may be low")
    return lines


def fault_summary(faults: dict[str, Any]) -> str:
    """``3 events (store_error 2, degraded_enter 1)``, the most frequent first."""
    names = sorted(faults["events"].items(), key=lambda item: (-item[1], item[0]))
    return f"{_n(faults['total'], 'event')} ({', '.join(f'{name} {count}' for name, count in names)})"


def fault_lines(out: dict[str, Any]) -> list[str]:
    faults = out["faults"]
    if not faults["total"]:
        return ["  faults      none"]
    return [f"  faults      {fault_summary(faults)}", *(f"    {message}" for message in faults["messages"])]


# The exit summary of ``contexto run`` ---------------------------------------------------------------

INFO_ARGS = (["--version"], ["-v"], ["-V"], ["--help"], ["-h"])  # the CLI prints and exits: no summary
# The fault events the launcher's NoticeLog already puts on stderr in its own words (a store failure is
# said with ``degraded_enter``'s cause), so the exit summary does not warn about them a second time.
NOTICED_EVENTS = frozenset({"store_error", "page_conflict", "degraded_enter", "mcp_tools_missing",
                            "mcp_tools_denied", "bound_too_small"})
QUIET_ENV = "CONTEXTO_QUIET"


def quiet_from_env(env: Any = None) -> bool:
    """``CONTEXTO_QUIET`` set to anything but an empty value, 0, false or no, as the version check reads
    its own variable."""
    value = (os.environ if env is None else env).get(QUIET_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no")


def exit_summary(home: Path, session_id: str, cli: str, elapsed_s: float, *, quiet: bool = False,
                 log_incomplete: bool = False) -> list[str]:
    """The lines ``contexto run`` prints on stderr once the CLI has exited: what this run did (the log
    from its proxy start on, not the whole of a resumed session), with a ``faults`` line when it logged
    any, then one warning line for the faults no earlier warning described (``NOTICED_EVENTS``).
    ``quiet`` drops the summary and keeps the warning."""
    out = session_stats(home, session_id, last_run=True)
    # Not "contexto: session ...": that is how the one line printed at start begins, and scripts read it.
    head = f"contexto: run of session {session_id} ended after {duration_text(elapsed_s)}"
    requests = out["requests"]
    others = requests["other"] + requests["count_tokens"]
    lines: list[str] = []
    if quiet:
        pass
    elif requests["model"]:
        more = f" (`contexto stats {session_id}` shows all {out['runs']} runs)" if out["runs"] > 1 else ""
        lines += [head + more, *render(out, brief=True)]
        if log_incomplete:
            lines.append("  (the session log could not be written at times, so these numbers are low)")
    elif log_incomplete:  # no line of a request does not show that there was none
        lines.append(f"{head}; its log could not be written, so this run has no numbers")
    else:
        reached = (f"the CLI reached the proxy for {_n(others, 'other request')}" if others
                   else "nothing reached the proxy")
        lines.append(f"{head} with no model request through Contexto ({reached}). If you sent {cli} a prompt, it "
                     f"bypassed Contexto and nothing was bounded or paged: see docs/stats.md, 'No model request'.")
    faults = out["faults"]
    if faults["total"] and lines and requests["model"]:
        degraded = faults["degraded"]
        state = ""
        if degraded["entered"] or degraded["unpaged_requests"]:
            state = (f": {_n(degraded['unpaged_requests'], 'request')} went out unpaged, and paging "
                     f"{'had not resumed at exit' if degraded['now'] else 'resumed before exit'}")
        lines.append(f"  faults      {fault_summary(faults)}{state}; details: contexto stats {session_id}")
    unsaid = {name: count for name, count in faults["events"].items() if name not in NOTICED_EVENTS}
    if unsaid:
        counted = fault_summary({"total": sum(unsaid.values()), "events": unsaid})
        lines.append(f"contexto: warning: this run logged {counted} that Contexto left its designed path. "
                     f"Details: contexto stats {session_id}")
    return lines


def print_exit_summary(store: Any, cli: str, started: float | None, *, cli_args: Any = (), quiet: bool = False,
                       log_incomplete: bool = False, stderr: Any = None) -> None:
    """Print ``exit_summary`` for the session ``store`` holds; ``started`` is ``time.monotonic()`` when
    the CLI was started. It runs in the launcher's cleanup, after the CLI has released the terminal, so
    it never raises and never changes the exit code: any failure prints nothing. Nothing is printed
    when the CLI never started (``started`` is None) or was only asked for its version or help."""
    try:
        if started is None or list(cli_args) in INFO_ARGS:
            return
        for line in exit_summary(store.home, store.session_id, cli, time.monotonic() - started, quiet=quiet,
                                 log_incomplete=log_incomplete):
            print(line, file=stderr, flush=True)
    except Exception:  # a summary is a courtesy; the CLI's exit code and the resume line come first
        pass
