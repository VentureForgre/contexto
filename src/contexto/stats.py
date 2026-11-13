"""What a session did, as numbers: read from its ``log.jsonl`` and its store, never written (D64).

Contexto's value is in each session's log (how large the prompt got against the bound, how many turns
were paged, how often the model read them back, how much of the input came from cache), and until D64
nothing showed it: ``contexto sessions show`` prints event counts. ``session_stats`` turns one session
into a plain dict, which ``contexto stats`` prints, ``contexto run`` summarises at exit, and a status
line can call after every assistant message. docs/stats.md defines every number.

Rules this module keeps:

- It only reads: ``log.jsonl``, the ledger, and ``pages/``. Nothing is created or changed (the ledger
  is read with ``Ledger.peek``, which makes no lock file), nothing is waited for (a writer that holds
  the ledger's lock does not hold a reader up: the launcher calls this at exit, with signals ignored),
  and no network call is made.
- A page file is read at its first and last bytes only (the id is the second key of a page's
  canonical JSON and the token estimate the last but one), so a store of thousands of pages costs two
  small reads each, and counting the pages opens none.
- A damaged log is read as far as it goes. A line cut short by a full disk, a line that is not a JSON
  object, and a value that cannot be what its field holds (a string or a negative number where a count
  belongs) are skipped, each on its own, and counted in ``log``; a missing log gives empty numbers.
  Nothing here raises for what a log line holds.
- The log is read in blocks of ``LOG_BLOCK_BYTES``, so memory does not grow with the session.
- A number is reported only when the log or the store supports it, and each says whether the API
  measured it (``usage``) or Contexto estimated it. What the log cannot show is ``null`` and has no
  line: how many pages a run added when its log has no page count, a cache share when no response
  reported a cache field, "no compaction" when the requests could not have shown one.

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
from contexto.private import open_regular

STATS_VERSION = 1  # the ``--json`` shape; raised only when a documented key changes meaning or goes

MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
MODEL_PATHS = frozenset({MESSAGES_PATH, "/responses", "/v1/responses"})  # a prompt sent for a completion
TOOL_NAMES = ("recall", "search", "state", "pin", "close")  # Contexto's MCP tools, without ``contexto_``

# The Claude Code versions on which the compaction summary's wording was measured (the note above
# ``rewrite.CLAUDE_CODE_COMPACTION_SUMMARY``; tests/test_stats.py checks the two agree). "Claude Code did
# not compact" is claimed only for requests from one of these, with a Claude Code session id on each.
COMPACTION_MEASURED_ON = ("2.1.314", "2.1.318")
COMPACTION_REASONS = ("no_session_id", "cli_version_unknown", "cli_version_not_measured")

# The log is read in blocks of this size, each parsed on its own, so the memory a call takes does not
# grow with the log. 4 MiB holds a whole 6-hour session's log (2.9 MB at 4,000 requests), which is then
# parsed in one call as before; a 200 MB log takes under 90 MB resident, where reading it whole took
# 1.3 GB. docs/stats.md has the measurements.
LOG_BLOCK_BYTES = 4 << 20

# Events that mean Contexto left its designed path: the contract's acceptance list (M2, check 1) without
# ``turn_room_low``, which a small bound logs once per epoch in normal use, and with the events for
# damage, bugs, refused requests and lost stored data that are logged outside the engine. Each maps to
# what it means; docs/stats.md has the reasons. Every other event the source writes is in
# ``NOT_FAULT_EVENTS``: tests/test_stats.py fails when the source gains a name that is in neither.
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
    "encoded_body_rejected": "a request was answered 415 because its body could not be decoded",
    "request_refused": "a request was answered 403 because it did not come from the local CLI",
    "ledger_repaired": "the ledger of pins ended in a damaged record, which was moved aside",
    "pruned_while_opening": "the session was deleted while it was being opened, so it started empty",
    "search_cache_invalid": "CONTEXTO_SEARCH_CACHE_MB was not a valid value, so the default was used",
    "watchdog_unavailable": "the watchdog that cleans up after a killed launch could not start",
    "affinity_salt_unavailable": "the cache-affinity key could not be read, so requests carried no affinity header",
}
# Deliberately not faults. The first group are the log's ordinary records, most with a number of their
# own; the second happen in normal use; docs/stats.md gives the reason for each.
NOT_FAULT_EVENTS = frozenset({
    "request", "launch", "exit", "proxy_start", "proxy_stop", "mcp_call", "count_tokens", "rotation", "page_out",
    "carried_paged", "carried_reclassified", "over_bound", "main_changed", "degraded_leave", "epoch_resumed",
    "sizing_restored", "side_room_closed", "mcp_tools_found", "cli_version", "cli_version_check",
    "turn_room_low", "upstream_4xx", "usage_missing", "usage_unattributed", "resync", "rotation_noop",
    "epoch_not_resumed", "head_changed", "window_minimal", "side_room_expired", "hold_skipped", "upstream_retry",
    "stale_temp_removed", "estimate_error", "pdf_allowance", "pdf_pages_uncertain", "cli_version_untested",
    "proxy_stop_incomplete", "carried_text_growing", "sizing_not_restored", "window_budget_warning",
})

DISK_FULL = frozenset({"ENOSPC", "EDQUOT"})
NO_ACCESS = frozenset({"EACCES", "EPERM", "EROFS"})
UNPAGED = "requests go out unpaged and can exceed the bound"
UNPAGED_PAST = "requests went out unpaged and could exceed the bound"
HOME_HINT = "$CONTEXTO_HOME (default ~/.contexto)"

# The two ends of a page file: canonical JSON sorts a page's keys, so ``bytes`` and ``id`` come first
# and ``tokens`` and ``turn`` last.
_PAGE_HEAD = re.compile(rb'\{"bytes":\d+,"id":"(pg_[0-9a-f]{16})","messages":\[')
_PAGE_HEAD_BYTES = 64
_PAGE_TAIL = re.compile(rb'[,{]"tokens":(\d+),"turn":-?\d+\}\s*\Z')
_PAGE_TAIL_BYTES = 96


def _nat(value: Any) -> int | None:
    """``value`` when it can be a count (a whole number, not a bool, not negative), else None: a log
    line or a page file can hold anything."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _when(value: Any) -> datetime | None:
    """A log line's ``ts`` as a time, or None when it is not one."""
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _events(value: Any) -> Iterator[dict[str, Any]]:
    """The events nested in a request line (``transform_events`` or ``usage_events``) that are objects."""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _parse_lines(lines: list[bytes]) -> list[Any]:
    """Each line's JSON value, None for a line that is not JSON. Healthy lines parse in one call, about
    twice as fast as a call per line (docs/stats.md); any damaged line fails that call, or changes the
    number of values, and these lines are then parsed one by one."""
    try:
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
    return parsed


def _blocks(fd: int) -> Iterator[bytes]:
    """The file in pieces of about ``LOG_BLOCK_BYTES``. It is read 1 MiB at a time, as ``read_regular``
    reads: asking the OS for a whole block at once made each call allocate, and on macOS keep, a
    4 MiB buffer, and the process's memory grew with every call. A read that fails ends the log there
    (the disk failed under it)."""
    chunks: list[bytes] = []
    size = 0
    while True:
        try:
            chunk = os.read(fd, min(1 << 20, LOG_BLOCK_BYTES))
        except OSError:
            chunk = b""
        if chunk:
            chunks.append(chunk)
            size += len(chunk)
        if chunks and (not chunk or size >= LOG_BLOCK_BYTES):
            yield b"".join(chunks)
            chunks, size = [], 0
        if not chunk:
            return


def _whole_lines(fd: int, start: int = 0) -> Iterator[tuple[int, bytes | None]]:
    """(file offset, bytes) for each stretch of whole lines from ``start`` on, about a block at a time;
    the last may lack its newline. A line longer than ``LOG_BLOCK_BYTES`` is not a log line (they are
    content-free and about 1 KB) and is unreadable wherever the blocks' edges fall: one that no block
    ends gives ``(offset, None)`` once and is never kept in memory, and the readers drop one that a
    stretch still holds (``_too_long``)."""
    if start:
        os.lseek(fd, start, os.SEEK_SET)
    rest, skipping, at = b"", False, start  # ``at`` is the file offset of ``rest``, or of the next block
    for block in _blocks(fd):
        if skipping:  # inside a line longer than a block, already reported
            end = block.find(b"\n")
            if end < 0:
                at += len(block)
                continue
            at += end + 1
            block, skipping = block[end + 1:], False
        data = rest + block if rest else block
        cut = data.rfind(b"\n")
        if cut < 0:
            if len(data) > LOG_BLOCK_BYTES:
                yield at, None
                at += len(data)
                rest, skipping = b"", True
            else:
                rest = data
            continue
        rest = data[cut + 1:]
        yield at, data[:cut]
        at += cut + 1
    if rest.strip():
        yield at, rest


def _too_long(line: bytes) -> bool:
    return len(line) > LOG_BLOCK_BYTES


def log_values(directory: Path, start: int = 0) -> Iterator[Any]:
    """The JSON value of each non-empty line of the session's log from byte ``start`` on, in order;
    None for a line that cannot be read. The file is opened as ``open_regular`` opens it (a FIFO,
    directory or symlink in its place is never waited on or followed) and OSError is raised, before the
    first value, when there is no readable log.

    Each block's whole lines are parsed together, so the memory in use is a few blocks' worth however
    large the log is."""
    fd = open_regular(Path(directory) / "log.jsonl")
    try:
        for _, data in _whole_lines(fd, start):
            if data is None:
                yield None
                continue
            lines = [b"\0" if _too_long(line) else line for line in data.split(b"\n") if line.strip()]
            del data
            yield from _parse_lines(lines)  # a NUL is not JSON: the line is read as unreadable
    finally:
        os.close(fd)


HELD_LINES = 64  # lines kept between one run's exit and the next proxy start; a launch writes a few
_RUN_MARK = re.compile(rb'"event":\s*"(?:proxy_start|launch)"')
_LOOK_BACK_BYTES = 1 << 16


def _line_value(line: bytes) -> Any:
    try:
        return json.loads(line)
    except (ValueError, RecursionError):
        return None


def _run_begins(fd: int, start_line: int) -> int:
    """The offset of the first line of the run whose proxy start line is at ``start_line``: the lines
    written since the last ``exit`` before it, which are its launch's (see ``session_stats``). The run
    begins at its proxy start when the lines before it follow another proxy start (a run that was
    killed), or are more than ``HELD_LINES``: those are not a launch's."""
    begin, held, pos, tail = start_line, 0, start_line, b""
    while pos > 0:
        size = min(pos, _LOOK_BACK_BYTES)
        data = os.pread(fd, size, pos - size) + tail
        pos -= size
        first = 0 if pos == 0 else data.find(b"\n") + 1  # the first line that starts in this piece
        if pos and not first:  # no line starts here: it is part of a longer one
            if len(data) > LOG_BLOCK_BYTES:
                return start_line
            tail = data
            continue
        tail = data[:first]
        lines, at = [], pos + first
        for line in data[first:].split(b"\n"):
            lines.append((at, line))
            at += len(line) + 1
        for at, line in reversed(lines):
            if not line.strip():
                continue
            value = None if _too_long(line) else _line_value(line)
            if isinstance(value, dict):
                if value.get("event") == "exit":
                    return begin
                held += 1
                if value.get("event") == "proxy_start" or held > HELD_LINES:
                    return start_line
            begin = at
    return begin


def _last_run_tail(directory: Path) -> tuple[int, int, dict[str, Any] | None]:
    """Where the latest run begins in the session's log, found without parsing the runs before it:
    (the byte offset of its first line, the proxy starts before it, the last ``launch`` line before it).
    ``(0, 0, None)`` for a log with no proxy start, and for one that cannot be read.

    A resumed session's log holds every run, and the exit summary and ``contexto state`` want the
    latest. The file is still read once, but only searched: a line is parsed when it holds a proxy
    start's or a launch's name, which is two lines a run."""
    try:
        fd = open_regular(Path(directory) / "log.jsonl")
    except OSError:
        return 0, 0, None
    try:
        runs, last, launch, carried = 0, None, None, None
        for base, data in _whole_lines(fd):
            if data is None:
                continue
            done = 0
            for mark in _RUN_MARK.finditer(data):
                if mark.start() < done:
                    continue  # a second name on a line already read
                begin = data.rfind(b"\n", 0, mark.start()) + 1
                end = data.find(b"\n", mark.end())
                done = len(data) if end < 0 else end
                value = None if done - begin > LOG_BLOCK_BYTES else _line_value(data[begin:done])
                name = value.get("event") if isinstance(value, dict) else None
                if name == "proxy_start":
                    runs, last, carried = runs + 1, base + begin, launch
                elif name == "launch":
                    launch = value
        if last is None:
            return 0, 0, None
        return _run_begins(fd, last), runs - 1, carried
    except OSError:
        return 0, 0, None
    finally:
        os.close(fd)


def store_fault_cause(failure: dict[str, Any]) -> str:
    """What a ``store_error`` or ``page_conflict`` event means for the user: ``disk_full``, ``no_access``
    (permissions or a read-only filesystem), ``page_conflict``, or ``store`` (anything else)."""
    if failure.get("event") == "page_conflict":
        return "page_conflict"
    code = failure.get("errno") if isinstance(failure.get("errno"), str) else None
    if code in DISK_FULL:
        return "disk_full"
    return "no_access" if code in NO_ACCESS else "store"


def store_fault_message(failure: dict[str, Any], session_id: str | None = None, *, past: bool = False) -> str:
    """One paragraph for a store failure: what happened, what it costs, and what to do next. Built only
    from the event's content-free fields (an errno name, an operation, an exception class, a page id).
    ``past`` tells it as over, for a reader that knows paging resumed or that the run has ended; the
    warning raised when the failure happens cannot know, and says it in the present."""
    cause = store_fault_cause(failure)
    operation = failure.get("operation") if isinstance(failure.get("operation"), str) else "a store call"
    error = failure.get("error") if isinstance(failure.get("error"), str) else "an error"
    code = failure.get("errno") if isinstance(failure.get("errno"), str) else None
    detail = f"{code} during {operation}" if code else f"{error} during {operation}"
    stopped = (f"so paging stopped: {UNPAGED_PAST}" if past else f"so paging has stopped: {UNPAGED}")
    retry = "Contexto retries at each turn start and pages again once the store works; turns already paged are kept."

    def advice(text: str) -> str:
        return f"If it happens again, {text[0].lower()}{text[1:]}" if past else f"{text} {retry}"

    if cause == "disk_full":
        return (f"the disk that holds Contexto's store {'was' if past else 'is'} full ({detail}), {stopped}. "
                + advice(f"Free space on the disk that holds {HOME_HINT}."))
    if cause == "no_access":  # a read can fail this way as well as a write (EACCES during pinned_text)
        return (f"Contexto {'could not' if past else 'cannot'} read or write its store ({detail}), {stopped}. "
                + advice(f"Check the owner and permissions of the session directory under {HOME_HINT}, and that "
                         f"its filesystem is not read-only."))
    if cause == "page_conflict":
        page = failure.get("page_id") if isinstance(failure.get("page_id"), str) else "a page"
        return (f"a stored page ({page}) {'did' if past else 'does'} not match the turn it should hold, {stopped}. "
                f"At the next turn start Contexto moves that file to the session's quarantine folder (nothing is "
                f"deleted) and pages the turn again. If it happens again, check the disk under {HOME_HINT}.")
    show = f"contexto sessions show {session_id}" if session_id else "contexto sessions show ID"
    return (f"Contexto's store failed ({detail}), {stopped}. {'' if past else retry + ' '}Run `{show}` to check "
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
        self.first_ts: str | None = None  # as logged; only a ``ts`` that is a time counts
        self.last_ts: str | None = None
        self.first_at: datetime | None = None
        self.last_at: datetime | None = None
        self.ignored = 0  # values that could not be what their field holds, each left out of its number
        self.requests = {"model": 0, "main": 0, "side": 0, "with_usage": 0, "failed": 0, "status": {},
                         "aborted": 0, "cut_short": 0, "not_transformed": 0, "rejected_encoding": 0,
                         "in_flight_at_stop": 0, "count_tokens": 0, "other": 0}
        self.peak: int | None = None  # the largest prompt the API counted
        self.peak_unmeasured: int | None = None  # the largest estimate among requests with no usage
        self.last: tuple[int, str] | None = None  # the main conversation's most recent prompt
        self.over = {"requests": 0, "measured": 0, "estimate_only": 0, "in_tool_loop": 0, "at_turn_start": 0,
                     "degraded": 0}
        self.paging = {"rotations": 0, "carried_items_paged": 0}
        self.tools = {name: {"calls": 0, "failed": 0, "charged_tokens": 0} for name in TOOL_NAMES}
        self.out_of_room = 0
        self.cache = {"requests": 0, "unreported_requests": 0, "input_tokens": 0, "cache_read_tokens": 0,
                      "cache_write_tokens": 0}
        self.bytes = {"cli_sent": 0, "forwarded": 0, "largest_cli_request": 0, "largest_forwarded_request": 0}
        self.fault_events: dict[str, int] = {}
        self.entered = self.left = self.unpaged = 0
        self.largest_unpaged: int | None = None
        self.since: str | None = None
        self.store_errors: dict[tuple, dict[str, Any]] = {}
        self.degraded = {True: False, False: False}  # by whether the conversation is the main one
        self.seen_main = False
        self.ended_degraded = False  # an earlier run in this scope ended with paging stopped
        # Runs. A run is open from its proxy start to its ``exit`` line, or to the next proxy start when
        # it was killed or was a bare ``contexto proxy``, which writes no ``exit``.
        self.run_open = False
        self.run_seconds = 0
        self._run_first: datetime | None = None
        self._run_last: datetime | None = None
        self.gains: list[int | None] = []  # per run, the pages the store gained; None when the log cannot say
        self._pages_start: int | None = None
        self._pages_stop: int | None = None
        self._stopped = False  # this run's proxy_stop line was seen
        # Compaction (Claude Code only). ``undetectable`` holds why the log could not have shown one.
        self.messages_api = False  # a Messages request was seen: the compaction check applies
        self.compactions = 0
        self.undetectable: set[str] = set()
        self.cli_versions: list[str] = []  # the Claude Code versions the engine logged, in order
        self._engine_messages = False  # this engine handled a Messages request
        self._engine_versions: set[str] = set()

    def _number(self, value: Any) -> int | None:
        """``value`` as a count, or None; a value that is there and cannot be a count is counted. The
        test is ``_nat``'s, written out: this runs eight times per request line."""
        if type(value) is int and value >= 0:
            return value
        if value is not None:
            self.ignored += 1
        return None

    def _text(self, value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if value is not None:
            self.ignored += 1
        return None

    def start_run(self, event: dict[str, Any]) -> None:
        """A proxy start. The engine's degraded state is in memory, so a new one starts paging again;
        a run still open (killed, or a bare proxy) ends where this one starts."""
        self._close_run(_nat(event.get("pages")))
        self._judge_compaction()
        self.ended_degraded = self.ended_degraded or self.degraded_now()
        self.degraded = {True: False, False: False}
        self.seen_main = False
        self.since = None
        self.run_open = True
        self._pages_start = self._number(event.get("pages"))

    def _close_run(self, pages_after: int | None) -> None:
        """End the open run: its time, and the pages the store gained in it. The gain is the proxy's own
        count at its stop less the one at its start; without a stop line (a run in progress or killed)
        the count ``pages_after`` stands in; a log from before the proxy recorded the count gives None.
        Pages moved to quarantine during the run lower it, never below 0."""
        if not self.run_open:
            return
        self.run_open = False
        if self._run_first is not None and self._run_last is not None:
            self.run_seconds += max(0, round((self._run_last - self._run_first).total_seconds()))
        end = self._pages_stop if self._stopped else pages_after
        known = self._pages_start is not None and end is not None
        self.gains.append(max(0, end - self._pages_start) if known else None)
        self._run_first = self._run_last = self._pages_start = self._pages_stop = None
        self._stopped = False

    def _judge_compaction(self) -> None:
        """Whether the engine now ending could have shown a compaction: the wording of Claude Code's
        summary is known only for ``COMPACTION_MEASURED_ON``, and the engine logs the version it reads
        from the ``claude-cli`` User-Agent once per engine."""
        if self._engine_messages:
            if not self._engine_versions:
                self.undetectable.add("cli_version_unknown")
            elif not self._engine_versions <= set(COMPACTION_MEASURED_ON):
                self.undetectable.add("cli_version_not_measured")
        self._engine_messages = False
        self._engine_versions = set()

    def finish(self, pages_now: int | None) -> None:
        """The end of the log; ``pages_now`` is the store's page count as it stands, for a run that has
        not stopped."""
        self._close_run(pages_now)
        self._judge_compaction()

    def line(self, event: dict[str, Any]) -> None:
        stamp = event.get("ts")
        when: str | None = None
        if stamp is not None:
            at = _when(stamp)
            if at is None:
                self.ignored += 1
            else:
                when = stamp
                if self.first_at is None:
                    self.first_ts, self.first_at = when, at
                self.last_ts, self.last_at = when, at
                if self.run_open:
                    self._run_first = self._run_first or at
                    self._run_last = at
        name = self._text(event.get("event"))
        if name == "request":
            self._request(event, when)
        elif name == "mcp_call":
            self._tool(event)
        elif name == "launch":
            self.cli = self._text(event.get("cli")) or self.cli
            config = event.get("config")
            if isinstance(config, dict):
                bound = self._number(config.get("bound"))
                self.bound = bound if bound is not None else self.bound
            elif config is not None:
                self.ignored += 1
        elif name == "proxy_stop":
            self._stopped, self._pages_stop = True, self._number(event.get("pages"))
        elif name == "exit":
            self._close_run(None)
        elif name == "proxy_stop_incomplete":  # requests with no line of their own: said, not a fault
            self.requests["in_flight_at_stop"] += self._number(event.get("in_flight")) or 0
        elif name == "mcp_tools_missing" and event.get("repeated") is True:
            pass  # the exit-time repeat of a warning the run already counted
        elif name in FAULT_EVENTS:
            self._fault(event, name, when)
            if name == "encoded_body_rejected" and event.get("endpoint") in MODEL_PATHS:
                self.requests["rejected_encoding"] += 1  # answered 415: it has no request line

    def _fault(self, event: dict[str, Any], name: str, when: str | None) -> None:
        self.fault_events[name] = self.fault_events.get(name, 0) + 1
        if name not in ("store_error", "page_conflict"):
            return
        names = ("event", "operation", "error", "errno")
        key = tuple(event.get(k) if isinstance(event.get(k), str) else None for k in names)
        entry = self.store_errors.setdefault(key, {
            **dict(zip(names, key)), "cause": store_fault_cause(event), "count": 0, "first": when, "last": when,
            "page_id": event.get("page_id") if isinstance(event.get("page_id"), str) else None})
        entry["count"] += 1
        entry["last"] = when or entry["last"]

    def _tool(self, event: dict[str, Any]) -> None:
        tool = self._text(event.get("tool"))
        name = tool.removeprefix("contexto_") if tool else None
        if name not in self.tools:
            return
        counts = self.tools[name]
        ok = event.get("ok") is True
        counts["calls" if ok else "failed"] += 1
        counts["charged_tokens"] += self._number(event.get("charged_tokens")) or 0
        if ok and event.get("turn_budget") in ("spent", "nearly_spent"):
            self.out_of_room += 1

    def _request(self, event: dict[str, Any], when: str | None) -> None:
        path = event.get("path")
        model = event.get("method") == "POST" and isinstance(path, str) and path in MODEL_PATHS
        if not model:
            self.requests["count_tokens" if path == COUNT_TOKENS_PATH else "other"] += 1
            return
        self.requests["model"] += 1
        status = self._number(event.get("status"))
        if status is not None and status >= 400:
            self.requests["failed"] += 1
            self.requests["status"][str(status)] = self.requests["status"].get(str(status), 0) + 1
        if event.get("client_aborted") is True:
            self.requests["aborted"] += 1
        error = event.get("error")
        if isinstance(error, str) and error.startswith("relay:"):
            self.requests["cut_short"] += 1  # upstream ended the response early, or the relay failed
        if event.get("transform_skipped"):
            self.requests["not_transformed"] += 1  # a body the engine could not read, forwarded as it came
        sent, original = self._number(event.get("request_bytes")), self._number(event.get("original_request_bytes"))
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
        if path == MESSAGES_PATH:
            self.messages_api = True
            if engine:
                self._engine_messages = True
                if engine.get("cli_session") is not True:  # absent in a log from before the engine wrote it
                    self.undetectable.add("no_session_id")
        usage_events = list(_events(event.get("usage_events")))
        moved = False  # this line holds a degraded_enter or degraded_leave
        for inner in [*nested, *usage_events]:
            kind = inner.get("event")
            if kind == "request":
                continue
            if kind == "over_bound":
                flagged = True
                if inner.get("source") == "real":
                    real = True
                elif in_loop is None:
                    in_loop = inner.get("in_tool_loop") is True
            elif kind == "rotation":
                self.paging["rotations"] += 1
            elif kind == "carried_paged":
                self.paging["carried_items_paged"] += self._number(inner.get("count")) or 0
            elif kind == "main_changed" and inner.get("reason") == "compacted":
                self.compactions += 1
            elif kind == "cli_version" and inner.get("cli") == "claude" and isinstance(inner.get("version"), str):
                self._engine_versions.add(inner["version"])
                if inner["version"] not in self.cli_versions:
                    self.cli_versions.append(inner["version"])
            elif kind == "degraded_enter":
                self.entered += 1
                self.degraded[main], moved = True, True
                if main:
                    self.since = self.since or when
            elif kind == "degraded_leave":
                self.left += 1
                self.degraded[main], moved = False, True
                if main:
                    self.since = None
            if self.bound is None and kind in ("over_bound", "bound_too_small"):
                self.bound = self._number(inner.get("bound"))  # a log without its launch line still knows the bound
            if isinstance(kind, str) and kind in FAULT_EVENTS:
                self._fault(inner, kind, when)
        if engine and not moved:
            # The engine's own word on this request, should an enter or leave line be lost. Only then: a
            # store that fails after the request was built (the publish of its recall room) enters
            # degraded mode on a line whose request event has no flag, and that entry stands.
            self.degraded[main] = engine.get("degraded") is True
            if main:
                self.since = (self.since or when) if self.degraded[main] else None
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
        estimate = self._number(engine.get("sent_estimate"))
        estimate = estimate if estimate is not None else self._number(engine.get("estimate"))
        if prompt is not None:
            self.peak = max(self.peak or 0, prompt)
        elif estimate is not None:
            # The largest requests are the ones most likely to have no usage (the API refuses a prompt
            # that is too long), so they are kept beside the measured peak, never inside it.
            self.peak_unmeasured = max(self.peak_unmeasured or 0, estimate)
        if main and (prompt is not None or estimate is not None):
            self.last = (prompt, "usage") if prompt is not None else (estimate, "estimate")

    def _usage(self, usage: Any, api: Any) -> int | None:
        """The prompt tokens the API counted for one request, or None; also adds them to the cache
        numbers when the response said how much was read from cache. Messages usage reports the cached
        parts apart from ``input_tokens``; Responses usage reports ``cached_tokens`` as a part of it.
        The proxy logs ``null`` for a cache field the API left out (an Anthropic-compatible gateway
        may), and that is not a measured 0: such a request is counted in ``unreported_requests``."""
        if not isinstance(usage, dict):
            return None
        fresh = self._number(usage.get("input_tokens"))
        if api == "responses":
            details = usage.get("input_tokens_details")
            read = self._number(details.get("cached_tokens")) if isinstance(details, dict) else None
            write, prompt = None, fresh
        else:
            read = self._number(usage.get("cache_read_input_tokens"))
            write = self._number(usage.get("cache_creation_input_tokens"))
            prompt = (fresh or 0) + (read or 0) + (write or 0)
        if not prompt:
            return None
        self.requests["with_usage"] += 1
        if read is None:
            self.cache["unreported_requests"] += 1
        else:
            self.cache["requests"] += 1
            self.cache["input_tokens"] += prompt
            self.cache["cache_read_tokens"] += min(read, prompt)
            self.cache["cache_write_tokens"] += write or 0
        return prompt

    def degraded_now(self) -> bool:
        """Whether paging is stopped at the end of the scope: the main conversation's state, or any
        conversation's when the scope holds no request of the main one."""
        return self.degraded[True] if self.seen_main else any(self.degraded.values())


def _seconds(first: datetime | None, last: datetime | None) -> int | None:
    if first is None or last is None:
        return None
    try:
        return max(0, round((last - first).total_seconds()))
    except TypeError:  # one timestamp with a UTC offset and one without
        return None


def _page_tokens(path: Path, size: int) -> int | None:
    """The token estimate stored in one page file, from its first and last bytes; the whole file is
    parsed only when they are not a canonical page's. None when the file is not a readable page named
    by its own id: one with a damaged start and an intact end is not a page the store can read, so its
    estimate is not added."""
    try:
        fd = open_regular(path)
    except OSError:
        return None
    try:
        head = _PAGE_HEAD.match(os.pread(fd, _PAGE_HEAD_BYTES, 0))
        tail = _PAGE_TAIL.search(os.pread(fd, _PAGE_TAIL_BYTES, max(0, size - _PAGE_TAIL_BYTES)))
        if head and tail and head.group(1).decode() == path.stem:
            return int(tail.group(1))
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        page = json.loads(b"".join(chunks))
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        os.close(fd)
    if not isinstance(page, dict) or page.get("id") != path.stem or not isinstance(page.get("messages"), list):
        return None
    return _nat(page.get("tokens"))


def _is_page_name(name: str) -> bool:
    return name.startswith("pg_") and name.endswith(".json")


def count_pages(pages_dir: Path) -> int | None:
    """How many page files ``pages_dir`` holds: regular ``pg_*.json`` files, counted from the directory
    listing alone. No file is opened or parsed, so the proxy can record the count when it starts and
    stops. 0 for a directory that does not exist yet; None when it cannot be listed."""
    try:
        with os.scandir(pages_dir) as entries:
            return sum(1 for entry in entries if _is_page_name(entry.name) and entry.is_file(follow_symlinks=False))
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def page_totals(pages_dir: Path, *, tokens: bool = True) -> dict[str, Any]:
    """The pages a session holds on disk: how many, the bytes their files take, and (with ``tokens``)
    the sum of the token estimates made when each was paged. Only regular ``pg_*.json`` files count;
    one that cannot be read is counted in ``unreadable`` and adds no tokens. Every value is None when
    the directory cannot be listed."""
    out: dict[str, Any] = {"count": 0, "tokens_estimate": 0 if tokens else None, "file_bytes": 0, "unreadable": 0}
    try:
        entries = list(os.scandir(pages_dir))
    except FileNotFoundError:
        return out
    except OSError:
        return dict.fromkeys(out)
    for entry in entries:
        if not _is_page_name(entry.name):
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
    """Active ledger records and open obligations; ``readable`` is False for a damaged ledger. Never
    waits for the ledger's lock and creates nothing (``Ledger.peek``)."""
    from contexto.store.ledger import Ledger, LedgerError  # lazily: the store is not needed to read a log
    from contexto.store.state import derive

    try:
        state = derive(Ledger(directory).peek())
    except (LedgerError, OSError, ValueError):
        return {"readable": False, "active_records": None, "open_obligations": None}
    return {"readable": True, "active_records": len(state.active), "open_obligations": len(state.open_obligations)}


def _fault_report(tally: _Tally, session_id: str, earlier_run: bool) -> dict[str, Any]:
    now = tally.degraded_now()
    store_errors = [{k: v for k, v in entry.items() if k != "page_id"} for entry in tally.store_errors.values()]
    messages = [store_fault_message(entry, session_id, past=not now) for entry in tally.store_errors.values()]
    if messages or tally.entered:
        count = f"{tally.unpaged:,} request{'' if tally.unpaged == 1 else 's'} went out unpaged"
        if tally.largest_unpaged:
            count += f", the largest {size_text(tally.largest_unpaged)}"
        if now:
            status = f"Paging has not resumed{f' since {tally.since}' if tally.since else ''}: {count}."
        elif earlier_run:
            status = f"That was in an earlier run, which ended with paging stopped ({count}); a new run pages again."
        else:
            status = f"Paging has resumed; meanwhile {count}."
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

    ``last_run`` counts only the log of the most recent run (one ``contexto run``): from the lines its
    launch wrote while opening the session, through its proxy start, to the end. Only that part of the
    log is parsed (``_last_run_tail``), so the cost does not grow with the runs before it. The pages and
    pins are the store's as it stands either way. ``page_tokens=False`` counts the page files without opening
    them, and ``store=False`` skips the ledger and the pages altogether, for a caller that wants only
    the log's numbers. Raises ``SessionNotFoundError`` when the session directory does not exist;
    nothing else is raised for a damaged session."""
    directory = session_dir(home, session_id)
    if directory.is_symlink() or not directory.is_dir():
        from contexto.store import SessionNotFoundError

        raise SessionNotFoundError(f"no session {session_id!r} under {Path(home) / 'sessions'}")
    tally, lines, unreadable, missing = _Tally(), 0, 0, False
    start, runs, launch = _last_run_tail(directory) if last_run else (0, 0, None)
    cli, bound = None, None
    if launch is not None:  # the launch a run without its own line (a bare proxy) inherits from
        config = launch.get("config")
        cli = launch.get("cli") if isinstance(launch.get("cli"), str) else None
        bound = _nat(config.get("bound")) if isinstance(config, dict) else None
    # A launch opens the session before it starts the proxy, and what that finds (a ledger repaired, a
    # session pruned meanwhile) is logged then. So the lines after a run's ``exit`` are held: they
    # belong to the next run if one starts, and to no run otherwise.
    held: list[dict[str, Any]] = []
    between = True
    try:
        for event in log_values(directory, start):
            lines += 1
            if not isinstance(event, dict):
                unreadable += 1
                continue
            name = event.get("event")
            if name == "proxy_start":
                runs += 1
                cli, bound = tally.cli or cli, tally.bound if tally.bound is not None else bound
                if last_run:
                    tally = _Tally()
                tally.start_run(event)
                for earlier in held:
                    tally.line(earlier)
                held, between = [], False
                tally.line(event)
            elif between:
                held.append(event)
                if name == "exit" or len(held) > HELD_LINES:
                    # Not a launch's few lines (an exit with no proxy start before it, or too many): they
                    # stay with what came before. After an exit, the lines that follow are held again.
                    for earlier in held:
                        tally.line(earlier)
                    held, between = [], name == "exit"
            else:
                tally.line(event)
                between = name == "exit"
    except OSError:
        missing = True
    if not (last_run and runs):
        for earlier in held:
            tally.line(earlier)
    cli, bound = tally.cli or cli, tally.bound if tally.bound is not None else bound
    if store:
        pages, pins = page_totals(directory / "pages", tokens=page_tokens), _pins(directory)
    else:
        pages = {"count": None, "tokens_estimate": None, "file_bytes": None, "unreadable": None}
        pins = {"readable": False, "active_records": None, "open_obligations": None}
    tally.finish(pages["count"])
    measured = tally.peak is not None
    peak = tally.peak if measured else tally.peak_unmeasured
    cache, sizes, gains = tally.cache, tally.bytes, tally.gains
    reasons = [reason for reason in COMPACTION_REASONS if reason in tally.undetectable]
    return {
        "version": STATS_VERSION,
        "session": session_id,
        "scope": "last_run" if last_run else "session",
        "runs": runs,
        "cli": cli,
        "bound": bound,
        "started": tally.first_ts,
        "ended": tally.last_ts,
        "span_seconds": _seconds(tally.first_at, tally.last_at),
        "run_seconds": tally.run_seconds if runs else None,
        "log": {"missing": missing, "lines": lines, "unreadable_lines": unreadable, "ignored_values": tally.ignored},
        "requests": tally.requests,
        "prompt": {"peak_tokens": peak,
                   "peak_source": ("usage" if measured else "estimate") if peak is not None else None,
                   "peak_estimate_without_usage": tally.peak_unmeasured,
                   "last_tokens": tally.last[0] if tally.last else None,
                   "last_source": tally.last[1] if tally.last else None, "bound": bound},
        "over_bound": tally.over,
        "paging": {"rotations": tally.paging["rotations"],
                   "pages_added": sum(gains) if gains and None not in gains else None,
                   "carried_items_paged": tally.paging["carried_items_paged"]},
        "pages": pages,
        "tools": {**tally.tools, "out_of_room": tally.out_of_room},
        "pins": pins,
        "cache": {**cache, "read_share": (round(cache["cache_read_tokens"] / cache["input_tokens"], 4)
                                          if cache["input_tokens"] else None)},
        "bytes": {**sizes, "forwarded_share": (round(sizes["forwarded"] / sizes["cli_sent"], 4)
                                               if sizes["cli_sent"] else None)},
        "compaction": ({"seen": tally.compactions, "detectable": not reasons, "reasons": reasons,
                        "cli_versions": tally.cli_versions} if tally.messages_api else None),
        "faults": _fault_report(tally, session_id, earlier_run=tally.ended_degraded and not last_run),
    }


def session_faults(home: Path, session_id: str) -> dict[str, Any]:
    """The ``faults`` part of the latest run's stats, for ``contexto state``: whether paging is stopped
    now, why, and what to do (``messages``). Only the log is read."""
    return session_stats(home, session_id, last_run=True, store=False)["faults"]


# Text ------------------------------------------------------------------------------------------------

def size_text(n: int) -> str:
    """``n`` bytes in binary units, as ``contexto sessions list`` prints sizes. The unit is the one in
    which the rounded figure is under 1024, so 1,048,575 bytes is ``1.0 MiB`` and never ``1024.0 KiB``."""
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024
        if round(size, 1) < 1024 or unit == "TiB":
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


def percent_text(share: float) -> str:
    """A share as a whole percent. ``0%`` and ``100%`` are printed only for exactly none and exactly
    all: a share that rounds to them reads ``under 1%`` or ``over 99%``."""
    text = f"{share:.0%}"
    if text == "0%" and share > 0:
        return "under 1%"
    return "over 99%" if text == "100%" and share < 1 else text


def _about(tokens: int) -> str:
    """A large token count to two or three figures: ``1.9M``, ``85k``, ``950``. From 999,500 up it is
    in millions, so it never reads ``1000k``."""
    if tokens >= 999_500:
        return f"{tokens / 1_000_000:.1f}M"
    return f"{tokens / 1_000:.0f}k" if tokens >= 10_000 else f"{tokens:,}"


def _n(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count:,} {noun if count == 1 else plural or noun + 's'}"


def _per_tool(tools: dict[str, Any], key: str) -> str:
    return ", ".join(f"{tools[name][key]:,} {name}" for name in TOOL_NAMES if tools[name][key])


def _request_text(requests: dict[str, Any]) -> str:
    text = f"{_n(requests['model'], 'model request')} ({requests['main']:,} main, {requests['side']:,} side)"
    if requests["failed"]:
        codes = ", ".join(f"{code} x{count}" for code, count in sorted(requests["status"].items()))
        text += f"; {requests['failed']:,} failed ({codes})"
    if requests["cut_short"]:
        text += f"; {_n(requests['cut_short'], 'response')} cut short"
    if requests["not_transformed"]:
        text += f"; {requests['not_transformed']:,} forwarded as the CLI sent it, unread"
    if requests["rejected_encoding"]:
        text += f"; {requests['rejected_encoding']:,} more refused with 415 before they were read"
    if requests["in_flight_at_stop"]:
        text += f"; {requests['in_flight_at_stop']:,} still in flight when the proxy stopped, so not in these numbers"
    return text


def _over_text(over: dict[str, Any], with_usage: int) -> str:
    """The headline counts what the API measured; requests only the estimate flagged, and where the
    flagged ones were, follow as their own clauses, because they split the requests two ways."""
    if not over["requests"]:
        return "over the bound: 0 requests"
    if with_usage:
        parts = [f"over the bound: {_n(over['measured'], 'request')} (measured)"]
        if over["estimate_only"]:
            parts.append(f"{over['estimate_only']:,} more by Contexto's estimate only")
    else:
        parts = [f"over the bound: {_n(over['estimate_only'], 'request')} by Contexto's estimate (the API reported "
                 "no usage)"]
    flagged = over["in_tool_loop"] + over["at_turn_start"]
    if flagged:
        parts.append(f"of the {flagged:,} flagged before sending, {over['in_tool_loop']:,} inside a tool loop and "
                     f"{over['at_turn_start']:,} at a turn start")
    if over["degraded"]:
        parts.append(f"{over['degraded']:,} while paging was stopped")
    return "; ".join(parts)


def _paged_text(out: dict[str, Any]) -> str | None:
    """What is paged out. The store is the truth for how much: the pages on disk, which are the
    session's in every scope. A run's own part is the pages the store gained while it ran, said apart
    so the session's pages never read as the run's. The noun is "pages": most hold one turn, and one
    written by the bounded fallback holds one carried message (docs/stats.md)."""
    paging, pages = out["paging"], out["pages"]
    rotations, added, count = paging["rotations"], paging["pages_added"], pages["count"]
    if not rotations and not count and not added:
        return None
    held = None if count is None else "no page on disk"
    if count:
        held = f"{_n(count, 'page')} on disk"
        if pages["tokens_estimate"]:
            held += f", about {_about(pages['tokens_estimate'])} tokens (an estimate made when each was paged)"
        if pages["unreadable"]:
            held += f"; {_n(pages['unreadable'], 'page')} could not be read"
    turned = _n(rotations, "rotation")
    if out["scope"] != "last_run":
        return "; ".join(part for part in (held, turned if rotations else None) if part)
    if added is None:
        did = f"{turned} this run" if rotations else None
    elif added:
        did = f"{_n(added, 'page')} in {turned} this run"
    else:
        did = f"no new page in {turned} this run" if rotations else "nothing this run"
    if held is None:
        return did
    if did is None:
        return f"the session has {held} (this run's log does not say how many it added)"
    return f"{did}; the session has {held}"


COMPACTION_WHY = {
    "no_session_id": "the log does not show a Claude Code session id on every request",
    "cli_version_unknown": "the log does not name Claude Code's version",
    "cli_version_not_measured": "Contexto's check was not measured on Claude Code {versions}",
}


def _compaction_text(compaction: dict[str, Any]) -> str:
    """"Claude Code did not compact" is claimed only when the log could have shown a compaction."""
    seen = compaction["seen"]
    if compaction["detectable"]:
        return (f"Claude Code compacted the main conversation {_n(seen, 'time')}" if seen
                else "none seen: Claude Code did not compact the main conversation")
    others = ", ".join(v for v in compaction["cli_versions"] if v not in COMPACTION_MEASURED_ON)
    why = "; ".join(COMPACTION_WHY[reason].format(versions=others) for reason in compaction["reasons"])
    if seen:
        return (f"Claude Code compacted the main conversation at least {_n(seen, 'time')}; others cannot be told "
                f"from this log ({why})")
    return f"none seen; whether Claude Code compacted cannot be told from this log ({why})"


def render(out: dict[str, Any], *, brief: bool = False) -> list[str]:
    """The stats as indented text lines, one per subject, each only when its data exists. ``brief``
    is the exit summary's: the byte line, and the request line unless some request failed or is in no
    other number, are left to ``contexto stats``."""
    lines: list[str] = []
    requests, prompt, over = out["requests"], out["prompt"], out["over_bound"]
    odd = any(requests[key] for key in ("failed", "cut_short", "not_transformed", "rejected_encoding",
                                        "in_flight_at_stop"))
    if not requests["model"] and not odd:
        lines.append("  requests    no model request in the log")
    elif not brief or odd:
        lines.append(f"  requests    {_request_text(requests)}")
    if prompt["peak_tokens"] is not None:
        measured = prompt["peak_source"] == "usage"
        against = f" of {prompt['bound']:,}" if prompt["bound"] else ""
        source = "measured" if measured else "Contexto's estimate; the API reported no usage"
        text = f"peak {prompt['peak_tokens']:,}{against} tokens ({source})"
        larger = prompt["peak_estimate_without_usage"]
        if measured and larger is not None and larger > prompt["peak_tokens"]:
            text += f"; a request the API reported no usage for was estimated larger, at {larger:,}"
        lines.append(f"  prompt      {text}; {_over_text(over, requests['with_usage'])}")
    paged = _paged_text(out)
    if paged:
        lines.append(f"  paged out   {paged}")
    tools, pins = out["tools"], out["pins"]
    ok = sum(tools[name]["calls"] for name in TOOL_NAMES)
    failed = sum(tools[name]["failed"] for name in TOOL_NAMES)
    if ok or failed or pins["open_obligations"]:
        parts = []
        if ok:
            text = f"{_n(ok, 'call')} ({_per_tool(tools, 'calls')})"
            if tools["out_of_room"]:
                text += (f", {tools['out_of_room']:,} of which found the turn's recall room used up and returned "
                         "little or nothing")
            parts.append(text)
        elif failed:
            parts.append("no call succeeded")
        if failed:
            parts.append(f"{failed:,} failed ({_per_tool(tools, 'failed')})")
        charged = sum(tools[name]["charged_tokens"] for name in TOOL_NAMES)
        if charged:
            # Not "tokens read back": the charge is the reply's characters at 1.5 a token or fewer, more
            # than twice the rate of the pages' estimate on the line above, so the two do not compare.
            parts.append(f"about {_about(charged)} tokens charged to the recall room (a budget charge that runs "
                         "high, not a count of what was read)")
        if pins["open_obligations"]:
            parts.append(f"{_n(pins['open_obligations'], 'obligation')} open")
        lines.append(f"  recall      {'; '.join(parts)}")
    cache = out["cache"]
    if cache["read_share"] is not None:
        over_which = ""
        if cache["unreported_requests"]:
            over_which = (f", over the {cache['requests']:,} of {cache['requests'] + cache['unreported_requests']:,} "
                          "requests with usage that reported a cache field")
        lines.append(f"  cache       {percent_text(cache['read_share'])} of input tokens read from cache "
                     f"(measured{over_which})")
    sizes = out["bytes"]
    if sizes["cli_sent"] and not brief:
        lines.append(f"  bytes       the CLI sent {size_text(sizes['cli_sent'])}, Contexto forwarded "
                     f"{size_text(sizes['forwarded'])} ({percent_text(sizes['forwarded_share'])})")
    compaction = out["compaction"]
    if compaction is not None and requests["model"]:
        lines.append(f"  compaction  {_compaction_text(compaction)}")
    if not brief:
        lines += fault_lines(out)
        log = out["log"]
        if log["unreadable_lines"]:
            lines.append(f"  log         {_n(log['unreadable_lines'], 'line')} of the log could not be read "
                         "(cut short or damaged), so the numbers above may be low")
        if log["ignored_values"]:
            lines.append(f"  log         {_n(log['ignored_values'], 'value')} in the log were not what their field "
                         "holds (damage) and were left out, so the numbers above may be low")
    return lines


def _by_count(events: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(events.items(), key=lambda item: (-item[1], item[0]))


def fault_summary(faults: dict[str, Any]) -> str:
    """``3 events (store_error 2, degraded_enter 1)``, the most frequent first."""
    names = ", ".join(f"{name} {count}" for name, count in _by_count(faults["events"]))
    return f"{_n(faults['total'], 'event')} ({names})"


def fault_lines(out: dict[str, Any]) -> list[str]:
    """The faults, what each event means (the exit warning points here for details, and an event's name
    alone is not one), and for a store failure what to do."""
    faults = out["faults"]
    if not faults["total"]:
        return ["  faults      none"]
    meanings = [f"    {name} x{count}: {FAULT_EVENTS.get(name, 'an event this release does not know')}"
                for name, count in _by_count(faults["events"])]
    return [f"  faults      {fault_summary(faults)}", *meanings, *(f"    {message}" for message in faults["messages"])]


# The exit summary of ``contexto run`` ---------------------------------------------------------------

INFO_ARGS = (["--version"], ["-v"], ["-V"], ["--help"], ["-h"])  # the CLI prints and exits: no summary
# The fault events already put on stderr in their own words, by the launcher's NoticeLog (a store
# failure is said with ``degraded_enter``'s cause, and the page-conflict warning says the page will be
# moved to quarantine) or by the store when it opens a session that was pruned meanwhile, so the exit
# summary does not warn about them a second time.
NOTICED_EVENTS = frozenset({"store_error", "page_conflict", "degraded_enter", "page_quarantined",
                            "mcp_tools_missing", "mcp_tools_denied", "bound_too_small", "pruned_while_opening"})
HELP_ARGS = frozenset({"--help", "-h", "--version"})  # anywhere in the CLI's arguments: it printed and exited
CLI_NOT_STARTED = 127  # the launcher's EXIT_NOT_FOUND: it has already said that the CLI cannot be started
QUIET_ENV = "CONTEXTO_QUIET"


def quiet_from_env(env: Any = None) -> bool:
    """``CONTEXTO_QUIET`` set to anything but an empty value, 0, false or no, as the version check reads
    its own variable."""
    value = (os.environ if env is None else env).get(QUIET_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no")


def exit_summary(home: Path, session_id: str, cli: str, elapsed_s: float, *, quiet: bool = False,
                 log_incomplete: bool = False, cli_args: Any = (), exit_code: int | None = None) -> list[str]:
    """The lines ``contexto run`` prints on stderr once the CLI has exited: what this run did (the log
    from its proxy start on, not the whole of a resumed session), with a ``faults`` line when it logged
    any, then one warning line for the faults no earlier warning described (``NOTICED_EVENTS``).
    ``quiet`` drops the summary and keeps the warning. A run with no model request gets one line that
    says so, unless the CLI could not be started (``exit_code``; the launcher has said that) or was
    asked for help (``cli_args``)."""
    out = session_stats(home, session_id, last_run=True)
    # Not "contexto: session ...": that is how the one line printed at start begins, and scripts read it.
    head = f"contexto: run of session {session_id} ended after {duration_text(elapsed_s)}"
    requests = out["requests"]
    others = requests["other"] + requests["count_tokens"]
    faults = out["faults"]
    degraded = faults["degraded"]
    lines: list[str] = []
    if quiet:
        if degraded["entered"] and not degraded["now"]:
            # The store warning was raised while the store was failing and held until now, so it reads
            # as if paging were still stopped. The summary's faults line says it resumed; without the
            # summary this line does.
            lines.append(f"contexto: paging resumed before {cli} exited; meanwhile "
                         f"{_n(degraded['unpaged_requests'], 'request')} went out unpaged.")
    elif requests["model"]:
        more = f" (`contexto stats {session_id}` shows all {out['runs']} runs)" if out["runs"] > 1 else ""
        lines += [head + more, *render(out, brief=True)]
        if log_incomplete:
            lines.append("  (the session log could not be written at times, so these numbers are low)")
    elif log_incomplete:  # no line of a request does not show that there was none
        lines.append(f"{head}; its log could not be written, so this run has no numbers")
    elif (exit_code == CLI_NOT_STARTED and not others) or any(arg in HELP_ARGS for arg in cli_args):
        pass  # nothing ran that could have sent a prompt
    else:
        reached = (f"the CLI reached the proxy for {_n(others, 'other request')}" if others
                   else "nothing reached the proxy")
        lines.append(f"{head} with no model request through Contexto ({reached}). If you sent {cli} a prompt, it "
                     f"bypassed Contexto and nothing was bounded or paged: see docs/stats.md, 'No model request'.")
    if faults["total"] and lines and requests["model"] and not quiet:
        state = ""
        if degraded["entered"] or degraded["unpaged_requests"]:
            state = (f": {_n(degraded['unpaged_requests'], 'request')} went out unpaged, and paging "
                     f"{'had not resumed at exit' if degraded['now'] else 'resumed before exit'}")
        lines.append(f"  faults      {fault_summary(faults)}{state}; details: contexto stats {session_id}")
    unsaid = {name: count for name, count in faults["events"].items() if name not in NOTICED_EVENTS}
    if unsaid:
        total = sum(unsaid.values())
        names = ", ".join(f"{name} {count}" for name, count in _by_count(unsaid))
        lines.append(f"contexto: warning: this run logged {_n(total, 'event')} that mean{'s' if total == 1 else ''} "
                     f"Contexto left its designed path ({names}). Details: contexto stats {session_id}")
    return lines


def print_exit_summary(store: Any, cli: str, started: float | None, *, cli_args: Any = (), quiet: bool = False,
                       log_incomplete: bool = False, stderr: Any = None, code: int | None = None) -> None:
    """Print ``exit_summary`` for the session ``store`` holds; ``started`` is ``time.monotonic()`` when
    the CLI was started and ``code`` its exit code. It runs in the launcher's cleanup, after the CLI has
    released the terminal, so it never raises and never changes the exit code: any failure prints
    nothing. That includes Ctrl+C: SIGINT has its default handler again by then, and pressing it more
    than once is how people leave Claude Code. Nothing is printed when the CLI never started
    (``started`` is None) or was only asked for its version or help."""
    try:
        if started is None or list(cli_args) in INFO_ARGS:
            return
        for line in exit_summary(store.home, store.session_id, cli, time.monotonic() - started, quiet=quiet,
                                 log_incomplete=log_incomplete, cli_args=list(cli_args), exit_code=code):
            print(line, file=stderr, flush=True)
    except (Exception, KeyboardInterrupt):  # a courtesy; the CLI's exit code and the resume line come first
        pass
