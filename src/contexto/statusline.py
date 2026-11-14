"""``contexto statusline``: one short line for Claude Code's status line, and the rule for installing it (D64).

Claude Code runs a ``statusLine`` command after every assistant message and shows what it prints under
the prompt. This module prints ``ctx 112k/150k · 41 paged · 3 recalls · 5 searches`` for one Contexto
session (docs/statusline.md defines each part), and decides when ``contexto run claude`` may put that
command in the settings file it passes to Claude Code.

Rules this module keeps:

- **A status line the user has is never replaced.** Claude Code takes ``statusLine`` from the
  highest-ranking settings file that has it, and the launcher's ``--settings`` outranks the user's own
  files. So the launcher adds its command only when no settings file Claude Code would read has the key,
  every such file that exists could be read, and none turns hooks off (``launch_status_line``). If in
  doubt it adds nothing; the user can call ``contexto statusline`` from a status line of their own.
- **It never fails in the user's prompt area.** Claude Code blanks the status line on a non-zero exit, so
  the command prints nothing and exits 0 for any error, a missing session, or a mistyped option.
- **A wrong session's numbers are worse than none.** The session is ``--session``, else
  ``$CONTEXTO_SESSION``, else the session that is serving the Claude Code session named on stdin right
  now. When stdin names a Claude Code session, ``$CONTEXTO_SESSION`` counts only if it is open and
  serving that one, and two open sessions serving it give none. There is no "most recent session".
- **It is fast.** It runs in its own process each time, so it loads neither the proxy, the engine nor
  the store, and it reads only what the line shows: the end of the log for the current prompt and
  whether paging is stopped, and the few lines that hold a launch or a tool call (``quick_numbers``).
  Each line it does read is counted by ``contexto.stats``'s own ``_Tally``, so the numbers are the ones
  ``contexto stats`` gives; when the quick read cannot be sure of that it reads the whole log with
  ``session_stats`` instead. tests/test_statusline.py holds the two to the same answers.
- It only reads, and makes no network call.

The session records and the lock it reads are the store's and the launcher's; they are read here without
importing either (as ``contexto.stats`` reads the log), and the tests check that they agree.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from contexto import stats
from contexto.paths import absolute_home, contexto_home, session_dir
from contexto.private import open_regular, read_regular
from contexto.stats import session_stats

PREFIX = "ctx"
SEPARATOR = " · "
PLAIN_SEPARATOR = " | "  # for an output encoding that has no middle dot
STOPPED = "PAGING STOPPED"
STOPPED_HINT = "see contexto stats"
# The cause of stopped paging, in two or three words; ``contexto stats`` has the advice.
CAUSES = {"disk_full": "disk full", "no_access": "store not writable", "page_conflict": "page conflict",
          "store": "store error"}
RED, RESET = "\x1b[1;31m", "\x1b[0m"
RESTART_NOTICE = "your status line needs a restart"  # of `contexto run claude`; ``restart_line`` prints it

NO_STATUSLINE_ENV = "CONTEXTO_NO_STATUSLINE"
SESSION_ENV = "CONTEXTO_SESSION"
STDIN_WAIT_S = 0.5  # how long a pipe on stdin may stay silent before the command gives up on it
STDIN_LIMIT = 1 << 20  # Claude Code's JSON is a few kilobytes
SETTINGS_LIMIT = 8 << 20  # a settings file larger than this is not read, so it counts as unreadable
EXPANSION = "${"  # Claude Code expands ${VAR} in an MCP config (D55); ``launch_status_line`` has the rule
MANAGED_DIRS = {"darwin": "/Library/Application Support/ClaudeCode", "linux": "/etc/claude-code"}
# A log line that holds a launch, a proxy start or a tool call contains one of these words; any other
# line that happens to contain one is parsed and then ignored. They are searched for one at a time with
# ``bytes.find``: one regular expression for the three was 20 times slower over a 3 MB log.
MARKED = (b"mcp_call", b"launch", b"proxy_start")


# The line --------------------------------------------------------------------------------------------

def short_tokens(n: int) -> str:
    """``950``, ``9.4k``, ``112k``, ``1.9M``: rounded down, never up. Against the bound, ``prompt_and_bound``
    adds the decimals it takes to keep the two apart."""
    n = max(0, n)
    if n < 1_000:
        return str(n)
    if n < 10_000:
        return _decimals(n, 1_000, "k", 1)
    if n < 1_000_000:
        return f"{n // 1_000}k"
    return _decimals(n, 1_000_000, "M", 1 if n < 10_000_000 else 0)


def _decimals(n: int, unit: int, suffix: str, places: int) -> str:
    """``n`` in ``unit``s, rounded down to ``places`` decimals, without trailing zeros."""
    whole, part = divmod(n * 10 ** places // unit, 10 ** places)
    digits = f"{part:0{places}d}".rstrip("0") if places else ""
    return f"{whole}.{digits}{suffix}" if digits else f"{whole}{suffix}"


def prompt_and_bound(prompt: int, bound: int) -> tuple[str, str]:
    """The two sides of ``112k/150k``. Each is rounded down, so two numbers that differ can shorten alike:
    150,900 against a bound of 150,000 would read ``150k/150k``, as if at the bound and not over it, and
    150,200 against 150,500 as if it had reached it (PR #276's review). Then both get the decimals it
    takes to tell them apart (``150.9k/150k``, ``150.2k/150.5k``, ``1.09M/1M``). Rounding down keeps the
    order: a prompt over the bound always reads as more than it, one under as less, and only one at the
    bound reads as equal. Equal text means the same unit, so three places of ``k`` or six of ``M`` are
    whole tokens and always differ."""
    prompt, bound = max(0, prompt), max(0, bound)
    shown = short_tokens(prompt), short_tokens(bound)
    if prompt == bound or shown[0] != shown[1] or bound < 1_000:
        return shown
    unit, suffix, most = (1_000_000, "M", 6) if bound >= 1_000_000 else (1_000, "k", 3)
    for places in range(1, most + 1):
        shown = _decimals(prompt, unit, suffix, places), _decimals(bound, unit, suffix, places)
        if shown[0] != shown[1]:
            break
    return shown


def separator_for(encoding: str | None) -> str:
    """`` · `` when the output's encoding has the middle dot, else `` | ``."""
    try:
        "·".encode(encoding)  # type: ignore[arg-type]
    except (LookupError, UnicodeError, TypeError):
        return PLAIN_SEPARATOR
    return SEPARATOR


def render(numbers: Mapping[str, Any], *, color: bool = False, separator: str = SEPARATOR) -> str:
    """The line for ``numbers`` (``session_numbers``'s dict), each part only when its data exists; an
    empty string when there is nothing to show. Stopped paging comes first, in capitals, because that is
    when the user most needs to look; it replaces the recall counts with where to read more."""
    parts: list[str] = []
    stopped = numbers.get("stopped") is True
    if stopped:
        parts.append(stopped_marker(numbers, color=color))
    prompt, bound = numbers.get("prompt"), numbers.get("bound")
    bound = bound if isinstance(bound, int) and bound > 0 else None
    if isinstance(prompt, int):
        mark = "~" if numbers.get("prompt_source") == "estimate" else ""
        parts.append("{}{}/{}".format(mark, *prompt_and_bound(prompt, bound)) if bound
                     else mark + short_tokens(prompt))
    elif bound:
        parts.append(f"bound {short_tokens(bound)}")
    if numbers.get("paged"):
        parts.append(f"{numbers['paged']:,} paged")
    if stopped:
        parts.append(STOPPED_HINT)
    else:
        for count, one, many in ((numbers.get("recalls"), "recall", "recalls"),
                                 (numbers.get("searches"), "search", "searches")):
            if count:
                parts.append(f"{count:,} {one if count == 1 else many}")
    return f"{PREFIX} {separator.join(parts)}" if parts else ""


def stopped_marker(numbers: Mapping[str, Any], *, color: bool = False) -> str:
    """``PAGING STOPPED (disk full)``, in red on request."""
    cause = CAUSES.get(numbers.get("cause"))
    marker = f"{STOPPED} ({cause})" if cause else STOPPED
    return f"{RED}{marker}{RESET}" if color else marker


def restart_line(home: Path, session_id: str | None, *, color: bool = False, separator: str = SEPARATOR) -> str:
    """What the installed command prints once a settings file has a status line of the user's own:
    ``ctx: your status line needs a restart``, reason first and short enough for a 40-column row. When
    paging is stopped the marker comes first and the notice after it, because that matters more and a
    narrow row cuts from the end (PR #276's review: the notice had hidden the marker). Never raises."""
    try:
        numbers = session_numbers(home, session_id) if session_id else None
        if numbers and numbers.get("stopped") is True:
            return f"{PREFIX} {stopped_marker(numbers, color=color)}{separator}{RESTART_NOTICE}"
    except Exception:
        pass
    return f"{PREFIX}: {RESTART_NOTICE}"


def status_line(home: Path, session_id: str, *, color: bool = False, separator: str = SEPARATOR) -> str:
    """The status line for one session, or an empty string when the session does not exist or nothing
    about it can be read. Never raises: it runs in the user's prompt area."""
    try:
        numbers = session_numbers(home, session_id)
        return render(numbers, color=color, separator=separator) if numbers else ""
    except Exception:
        return ""


# The numbers -----------------------------------------------------------------------------------------

def session_numbers(home: Path, session_id: str) -> dict[str, Any] | None:
    """What the line shows for one session, or None when it does not exist: ``prompt`` (the main
    conversation's latest prompt, in tokens) and ``prompt_source`` (``usage`` or ``estimate``), ``bound``,
    ``paged`` (page files in the store), ``recalls`` and ``searches`` (tool calls that succeeded),
    ``stopped`` (paging is stopped now) and its ``cause``. A value is None when nothing supports it."""
    directory = session_dir(home, session_id)
    if directory.is_symlink() or not directory.is_dir():
        return None
    try:
        numbers = quick_numbers(directory)
    except Exception:  # the quick read is an optimisation; the whole log is always right
        numbers = None
    if numbers is None or numbers["stopped"]:
        numbers = full_numbers(home, session_id)  # stopped paging is rare, and its cause is in the whole log
    numbers["paged"] = count_pages(directory / "pages")
    return numbers


def count_pages(pages_dir: Path) -> int | None:
    """Page files in the store: ``stats.page_totals``'s ``count`` (regular ``pg_*.json`` files; None when
    the directory cannot be listed) without a ``stat`` call per file, which took 8.6 ms for the 1,200
    pages of a 6-hour session against 1.3 ms here. The directory listing already says what each entry
    is."""
    try:
        with os.scandir(pages_dir) as entries:
            return sum(1 for entry in entries if entry.name.startswith("pg_") and entry.name.endswith(".json")
                       and entry.is_file(follow_symlinks=False))
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def full_numbers(home: Path, session_id: str) -> dict[str, Any]:
    """The numbers from ``session_stats``, which reads the whole log. The cause of stopped paging is the
    newest store failure logged since paging stopped (to the second, as the log's times are written); a
    failure of an earlier run or an earlier stop is not named."""
    out = session_stats(home, session_id, store=False)
    degraded = out["faults"]["degraded"]
    cause = None
    if degraded["now"] is True:
        since = (degraded["since"] or "")[:19]
        times = [((entry.get("last") or "")[:19], entry["cause"]) for entry in out["faults"]["store_errors"]]
        current = [found for found in times if found[0] >= since]
        cause = max(current)[1] if current else None
    return {"prompt": out["prompt"]["last_tokens"], "prompt_source": out["prompt"]["last_source"],
            "bound": out["bound"], "recalls": out["tools"]["recall"]["calls"],
            "searches": out["tools"]["search"]["calls"], "stopped": degraded["now"] is True, "cause": cause}


def _event(line: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):  # cut short by a full disk, not UTF-8, or nested too deep
        return None
    return event if isinstance(event, dict) else None


def _lines_back(data: bytes) -> Iterator[tuple[int, bytes]]:
    """(offset, line) for each non-empty line of ``data``, the last line first."""
    end = len(data)
    while end > 0:
        cut = data.rfind(b"\n", 0, end)
        line = data[cut + 1:end]
        if line.strip():
            yield cut + 1, line
        end = cut


def _marked_lines(data: bytes) -> list[int]:
    """The offsets of the lines of ``data`` that contain a ``MARKED`` word, in order."""
    starts: set[int] = set()
    for word in MARKED:
        at = data.find(word)
        while at >= 0:
            starts.add(data.rfind(b"\n", 0, at) + 1)
            end = data.find(b"\n", at)
            at = data.find(word, end) if end >= 0 else -1
    return sorted(starts)


def quick_numbers(directory: Path) -> dict[str, Any] | None:
    """``full_numbers`` without parsing the whole log, or None when that cannot be done exactly.

    Three things are read. Each parsed line goes through ``stats._Tally``, the class ``session_stats``
    counts with, so what a line means is decided in one place.

    1. Every line that holds a launch (the bound), a proxy start (where the latest run begins) or a tool
       call: found by a word search over the bytes, a few hundred short lines in a long session.
    2. From the end backwards, the newest request of the main conversation that has a prompt size.
    3. From the end backwards within the latest run, the newest main-conversation request that carries
       the engine's own ``request`` event. That event states whether the request went out unpaged, so the
       engine's state at the end of the log is that line's and the later lines' doing. A run with no
       such line is counted whole.

    The bound must come from a launch line: ``session_stats`` otherwise takes it from an ``over_bound``
    event anywhere in the log, which only the whole log can answer, so None is returned.

    It assumes the log is one Contexto writes: every request line inside a run, after its proxy start. On
    request lines with no run around them (a hand-made log), whether paging is stopped can differ from
    ``contexto stats`` (PR #276's review: 64 of 3,000 random logs of that shape, none the launcher writes).
    """
    try:
        data = read_regular(directory / "log.jsonl")
    except OSError:  # no log: a FIFO, directory or symlink in its place counts as none, as in stats
        return {"prompt": None, "prompt_source": None, "bound": None, "recalls": 0, "searches": 0,
                "stopped": False, "cause": None}
    marked = stats._Tally()
    run_start = 0
    for start in _marked_lines(data):
        end = data.find(b"\n", start)
        event = _event(data[start:end if end >= 0 else len(data)])
        if event is None:
            continue
        name = event.get("event")
        if name == "proxy_start":
            run_start = start
        elif name in ("launch", "mcp_call"):
            marked.line(event)
    if marked.bound is None:
        return None

    prompt: tuple[int, str] | None = None
    tail: list[dict[str, Any]] = []  # the latest run's events back to the anchoring request, newest first
    anchored = False
    for start, line in _lines_back(data):
        in_tail = not anchored and start >= run_start
        if prompt is not None and not in_tail:
            break
        event = _event(line)
        if event is None:
            continue
        if in_tail:
            tail.append(event)
        if event.get("event") != "request":
            continue
        one = stats._Tally()
        one.line(event)
        if prompt is None and one.last is not None:
            prompt = one.last
        anchored = anchored or (in_tail and one.seen_main)
    engine = stats._Tally()
    for event in reversed(tail):
        engine.line(event)
    return {"prompt": prompt[0] if prompt else None, "prompt_source": prompt[1] if prompt else None,
            "bound": marked.bound, "recalls": marked.tools["recall"]["calls"],
            "searches": marked.tools["search"]["calls"], "stopped": engine.degraded_now(), "cause": None}


# Which session ---------------------------------------------------------------------------------------

def find_session(home: Path, given: str) -> str | None:
    """The session ``given`` names, as every reader takes one (``store.sessions.resolve_session``): that
    exact id, else the one session id that starts with it; None for no match or more than one."""
    try:
        path = session_dir(home, given)
    except ValueError:
        return None
    if path.is_symlink():
        return None
    if path.is_dir():
        return given
    try:
        names = os.listdir(Path(home) / "sessions")
    except OSError:
        return None
    matches = [name for name in names if name.startswith(given) and _is_session(home, name)]
    return matches[0] if len(matches) == 1 else None


def _is_session(home: Path, name: str) -> bool:
    try:
        path = session_dir(home, name)
    except ValueError:
        return False
    return not path.is_symlink() and path.is_dir()


def served_by(home: Path, claude_session: str) -> list[tuple[float, str]]:
    """(last seen, Contexto session id) for each session whose records say it served Claude Code session
    ``claude_session``: what the launcher's ``SessionRecorder`` writes to ``meta.json`` (D55) and its
    ``served_sessions`` reads. A session that cannot be read is skipped."""
    try:
        names = os.listdir(Path(home) / "sessions")
    except OSError:
        return []
    found = []
    for name in names:
        seen_at = served_at(home, name, claude_session)
        if seen_at is not None:
            found.append((seen_at, name))
    return found


def served_at(home: Path, session_id: str, claude_session: str) -> float | None:
    """When session ``session_id``'s records say it last served Claude Code session ``claude_session``, or
    None when they do not say it did (or cannot be read)."""
    if not _is_session(home, session_id):
        return None
    try:
        data = read_regular(session_dir(home, session_id) / "meta.json")
        meta = json.loads(data) if claude_session.encode("utf-8", "replace") in data else None
    except (OSError, ValueError, RecursionError):
        return None
    every = meta.get("cli_sessions") if isinstance(meta, dict) else None
    ids = every.get("claude") if isinstance(every, dict) else None
    seen_at = ids.get(claude_session) if isinstance(ids, dict) else None
    if isinstance(seen_at, (int, float)) and not isinstance(seen_at, bool) and math.isfinite(seen_at):
        return float(seen_at)
    return None


def session_live(home: Path, session_id: str) -> bool:
    """Whether a launch has the session open now: the proxy, the launcher and the MCP server each hold a
    shared lock on its ``live.lock`` (D37), so a non-blocking exclusive lock fails. The probe the
    launcher's ``session_in_use`` makes; its lock is dropped at once."""
    import fcntl

    try:
        fd = os.open(session_dir(home, session_id) / "live.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def run_started(home: Path, session_id: str) -> float | None:
    """When the latest run of ``session_id`` started: its newest ``proxy_start`` line's time, to the second
    (the log's times are UTC, written to the second or finer). None when the log cannot be read or has
    no such line."""
    import calendar

    try:
        data = read_regular(session_dir(home, session_id) / "log.jsonl")
    except OSError:
        return None
    for _, line in _lines_back(data):
        if b"proxy_start" not in line:
            continue
        event = _event(line)
        if event is None or event.get("event") != "proxy_start":
            continue
        try:
            return float(calendar.timegm(time.strptime(str(event.get("ts", ""))[:19], "%Y-%m-%dT%H:%M:%S")))
        except (ValueError, OverflowError):
            return None
    return None


def serves_in_this_run(home: Path, session_id: str, seen_at: float) -> bool:
    """Whether a time ``served_at`` gave falls within the launch that has the session open now. The
    launcher records a Claude Code session at its first request in each launch (``SessionRecorder``), so
    a conversation this launch serves is recorded after its proxy started. One served only in an earlier
    run is not: a session reopened for another conversation would otherwise show its numbers under a
    plain ``claude --resume`` of the old one (PR #276's review)."""
    started = run_started(home, session_id)
    return started is not None and seen_at >= started


def serving_session(home: Path, claude_session: str) -> str | None:
    """The Contexto session that is serving ``claude_session`` now: the one whose records name it and that
    a launch has open. A session that served the conversation in an earlier run is not it: a plain
    ``claude --resume`` of that conversation does not go through Contexto, and the old numbers would
    describe nothing on screen, nor is one that served it only in an earlier run of a launch now open for
    another conversation (``serves_in_this_run``). Two open launches on one conversation give None: either could be the
    window asking, and a guess showed one's numbers in the other's (PR #276's review)."""
    live = [name for seen_at, name in served_by(home, claude_session)
            if session_live(home, name) and serves_in_this_run(home, name, seen_at)]
    return live[0] if len(live) == 1 else None


def serves_now(home: Path, session_id: str, claude_session: str) -> bool:
    """Whether ``session_id`` is open and its records say it served ``claude_session`` in this run: the test a session
    from ``$CONTEXTO_SESSION`` must pass when stdin names a Claude Code session. The variable outlives the
    launch that set it in a shell where the user exported it (the readers default to it), and there it
    would show an old session's numbers under a conversation that session never served."""
    seen_at = served_at(home, session_id, claude_session)
    return seen_at is not None and session_live(home, session_id) and serves_in_this_run(home, session_id, seen_at)


def stdin_object(stream: Any) -> dict[str, Any] | None:
    """The JSON object on ``stream`` (Claude Code writes the session's data there), or None. A terminal
    is not read. A pipe is read until it holds a whole object, ends, stays silent for ``STDIN_WAIT_S``,
    or passes ``STDIN_LIMIT``, so a pipe nobody writes to or closes cannot hold the status line up."""
    import select

    try:
        fd = stream.fileno()
        if os.isatty(fd):
            return None
        deadline = time.monotonic() + STDIN_WAIT_S
        data = b""
        while True:
            ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                return None
            chunk = os.read(fd, 65536)
            data += chunk
            if len(data) > STDIN_LIMIT:
                return None
            if not chunk or data.rstrip().endswith(b"}"):
                try:
                    found = json.loads(data)
                except ValueError:
                    if not chunk:
                        return None
                    continue  # a "}" inside the object: more is coming
                return found if isinstance(found, dict) else None
    except (OSError, ValueError, AttributeError, RecursionError):  # no stdin, a closed one, or not JSON
        return None


# Settings files --------------------------------------------------------------------------------------

UNREADABLE = object()  # a settings file that exists and could not be read as a JSON object


def read_settings(path: Path) -> Any:
    """The JSON object in the settings file at ``path``; None when there is no file; ``UNREADABLE`` when
    something is there that cannot be read as one (permissions, a directory or FIFO, a dangling link,
    not JSON, not an object, too large). An empty file is an empty object: whatever Claude Code makes
    of it, it holds no status line. A symlink is followed, as Claude Code follows it; a FIFO is never
    waited on."""
    if not os.path.lexists(path):
        return None
    try:
        fd = open_regular(path, follow=True)
        try:
            chunks, size = [], 0
            while size <= SETTINGS_LIMIT and (chunk := os.read(fd, 1 << 20)):
                chunks.append(chunk)
                size += len(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if size > SETTINGS_LIMIT:
            return UNREADABLE
        found = json.loads(data) if data.strip() else {}
    except (OSError, ValueError, RecursionError):
        return UNREADABLE
    return found if isinstance(found, dict) else UNREADABLE


def own_status_line(paths: Sequence[str | os.PathLike[str]]) -> bool:
    """Whether one of these settings files now has a ``statusLine`` (a file that cannot be read does not
    count: mid-session it is no reason to hide the numbers)."""
    for path in paths:
        found = read_settings(Path(path))
        if isinstance(found, dict) and "statusLine" in found:
            return True
    return False


def managed_settings_dir(platform: str | None = None) -> Path | None:
    """The system directory Claude Code reads ``managed-settings.json`` and ``managed-settings.d/`` from
    (its documentation, 2026-11-12), or None on a platform where Contexto does not know it. Managed
    settings delivered another way (an MDM profile, the claude.ai console) are not read: they outrank the
    launcher's ``--settings``, so a status line they set is never replaced either way."""
    platform = sys.platform if platform is None else platform
    for name, directory in MANAGED_DIRS.items():
        if platform.startswith(name):
            return Path(directory)
    return None


def settings_files(cwd: Path, user_home: Path, env: Mapping[str, str] | None,
                   managed_dir: Path | None) -> list[tuple[str, Path]]:
    """(kind, path) of each settings file Claude Code would read from ``cwd``, highest precedence first:
    the managed file and its drop-ins (``*.json`` that is not hidden, in name order), then the project
    and user files in D52's order (``contexto.upstream.claude_settings_order``, which follows
    ``CLAUDE_CONFIG_DIR``). A drop-in directory that cannot be listed is returned itself, and reads as
    unreadable."""
    from contexto.upstream import claude_settings_order  # not needed to print a line

    files: list[tuple[str, Path]] = []
    if managed_dir is not None:
        files.append(("managed", managed_dir / "managed-settings.json"))
        drop_ins = managed_dir / "managed-settings.d"
        try:
            names = sorted(name for name in os.listdir(drop_ins) if name.endswith(".json") and name[:1] != ".")
        except FileNotFoundError:
            names = []
        except OSError:
            names = []
            files.append(("managed", drop_ins))
        files += [("managed", drop_ins / name) for name in names]
    return files + claude_settings_order(cwd, user_home, env)


def installed_command(session_id: str, home: Path, own_settings: Sequence[Path], *, color: bool,
                      executable: str | None = None) -> str:
    """The shell command for the launcher's ``statusLine``. Nothing in it depends on the environment
    Claude Code gives the command (2.1.318 and 2.1.322 pass the launcher's on, which is measured and
    not documented): the
    interpreter is this one by its absolute path, ``-P`` keeps the project's directory off ``sys.path``
    as it does for the MCP server, and the session, the store home and the settings files to re-check
    are arguments. Each is one ``--name=value`` token, quoted for a POSIX shell, so an id that starts
    with ``-`` or a home with spaces or quotes arrives whole."""
    import shlex

    argv = [executable or sys.executable, "-P", "-m", "contexto", "statusline", f"--session={session_id}",
            f"--home={home}", *(f"--own-settings={path}" for path in own_settings)]
    return shlex.join([*argv, "--color"] if color else argv)


def opted_out(env: Mapping[str, str]) -> bool:
    """``CONTEXTO_NO_STATUSLINE`` set to anything but an empty value, 0, false or no, as ``CONTEXTO_QUIET``
    is read."""
    return env.get(NO_STATUSLINE_ENV, "").strip().lower() not in ("", "0", "false", "no")


def launch_status_line(session_id: str, home: Path, cwd: Path, *, enabled: bool = True,
                       env: Mapping[str, str] | None = None, user_home: Path | None = None,
                       managed_dir: Path | None = None) -> tuple[dict[str, Any], str]:
    """What ``contexto run claude`` adds to its ``--settings`` for a status line, and a short note for the
    ``launch`` event: ``({"statusLine": {...}}, "added")``, or ``({}, reason)``.

    Claude Code replaces ``statusLine`` rather than merging it, and ``--settings`` outranks the project
    and user files, so adding one there would hide the user's own. It is added only when that cannot
    happen. The reasons, in the order they are checked:

    - ``off:flag``, ``off:env``: ``contexto run --no-statusline``, ``CONTEXTO_NO_STATUSLINE``.
    - ``own:<kind>``: a settings file has a ``statusLine`` key, whatever its value. ``<kind>`` is the
      highest-ranking such file: ``managed``, ``main-checkout-local``, ``local``, ``project`` or ``user``.
    - ``unreadable:<kind>``: a settings file exists and could not be read as a JSON object, so it may
      hold one.
    - ``hooks_off:<kind>``: a file sets ``disableAllHooks`` to true, which also turns a status line off.
    - ``managed_hooks_only``: managed settings allow only managed hooks, so Claude Code would not run it.
    - ``unsafe_path``: the command would hold ``${``, which Claude Code expands in an MCP config (D55).
      2.1.318 and 2.1.322 passed it to the shell as written in a status line command (measured,
      spikes/m2/statusline), but a path cannot be escaped if a later version expands it.
    - ``error``: deciding failed. Never raised: a status line must not stop a launch.

    ``managed_dir`` None means this platform's (``managed_settings_dir``)."""
    try:
        env = os.environ if env is None else env
        if not enabled:
            return {}, "off:flag"
        if opted_out(env):
            return {}, "off:env"
        files = settings_files(cwd, user_home or Path.home(), env,
                               managed_settings_dir() if managed_dir is None else managed_dir)
        own = unreadable = hooks_off = None
        managed_only = False
        for kind, path in files:
            found = read_settings(path)
            if found is None:
                continue
            if found is UNREADABLE:
                unreadable = unreadable or kind
                continue
            if "statusLine" in found:
                own = own or kind
            if found.get("disableAllHooks") is True:
                hooks_off = hooks_off or kind
            managed_only = managed_only or (kind == "managed" and found.get("allowManagedHooksOnly") is True)
        if own:
            return {}, f"own:{own}"
        if unreadable:
            return {}, f"unreadable:{unreadable}"
        if hooks_off:
            return {}, f"hooks_off:{hooks_off}"
        if managed_only:
            return {}, "managed_hooks_only"
        # The files a `/statusline` during this session could write: the command re-checks them.
        watched = [Path(cwd) / path for kind, path in files if kind != "managed"]
        command = installed_command(session_id, home, watched, color=not env.get("NO_COLOR"))
        if EXPANSION in command:
            return {}, "unsafe_path"
        return {"statusLine": {"type": "command", "command": command}}, "added"
    except Exception:
        return {}, "error"


# The command -----------------------------------------------------------------------------------------

DESCRIPTION = (
    "Print one short line about a Contexto session for Claude Code's status line, for example "
    "'ctx 112k/150k · 41 paged · 3 recalls · 5 searches': the main conversation's latest prompt against "
    "the bound; the page files kept on disk for the whole session, from every conversation in it "
    "(subagents' too), which the model can read back; and the recall and search calls the model made. "
    "'PAGING STOPPED' comes first when a store failure has stopped paging. The session is --session, else "
    "$CONTEXTO_SESSION, else the session serving the Claude Code session named by session_id in the JSON "
    "on stdin; when stdin names one, $CONTEXTO_SESSION counts only while a launch has it open and it has "
    "served that Claude Code session. With no session, or on any error, it prints nothing and exits 0, so "
    "a status line script of your own can call it and append what it prints. docs/statusline.md has the "
    "details.")


def add_arguments(parser: Any) -> None:
    parser.add_argument("--session", metavar="ID", default=None,
                        help="session id, or a unique prefix of one, shown whatever stdin names (default: "
                             "$CONTEXTO_SESSION, else the session serving the Claude Code session named on "
                             "stdin)")
    parser.add_argument("--home", metavar="DIR", default=None,
                        help="data directory (default: $CONTEXTO_HOME, else ~/.contexto)")
    parser.add_argument("--color", action="store_true",
                        help="mark stopped paging in red (ANSI); everything else stays plain")
    parser.add_argument("--own-settings", metavar="FILE", action="append", default=[],
                        help="a Claude Code settings file to check at each run: if it now has a statusLine, "
                             "print a note to restart `contexto run claude` in place of the numbers (after "
                             "PAGING STOPPED, when paging is stopped). "
                             "`contexto run claude` passes these; a script of your own does not need them")


def run(args: Any, *, stdin: Any = None, stdout: Any = None, env: Mapping[str, str] | None = None) -> int:
    """Print the line for the parsed arguments. Always returns 0, and prints nothing rather than an
    error: Claude Code blanks the status line on a non-zero exit, and never shows stderr."""
    out = sys.stdout if stdout is None else stdout
    try:
        text = _line(args, stdin, env, getattr(out, "encoding", None))
    except Exception:
        text = ""
    if text:
        try:
            print(text, file=out, flush=True)
        except (OSError, ValueError):  # Claude Code cancelled this run and closed the pipe
            if stdout is None:
                _discard_stdout()
    return 0


def _discard_stdout() -> None:
    """Point stdout at the null device, so the interpreter's own flush at exit cannot fail a second time
    and turn the exit code into 120."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


def _line(args: Any, stdin: Any, env: Mapping[str, str] | None, encoding: str | None) -> str:
    env = os.environ if env is None else env
    separator = separator_for(encoding)
    if args.own_settings and own_status_line(args.own_settings):
        try:
            home = absolute_home(args.home) if args.home else contexto_home(env)
            session = find_session(home, args.session) if args.session is not None else None
        except Exception:  # the notice is about the settings; it does not need the session
            home, session = Path(), None
        return restart_line(home, session, color=bool(args.color), separator=separator)
    home = absolute_home(args.home) if args.home else contexto_home(env)
    session = _session(args, home, stdin, env)
    if session is None:
        return ""
    return status_line(home, session, color=bool(args.color), separator=separator)


def _session(args: Any, home: Path, stdin: Any, env: Mapping[str, str]) -> str | None:
    """``--session`` as given (the user named it: stdin is never read); else ``$CONTEXTO_SESSION``, which
    must be open and serving the Claude Code session on stdin when stdin names one (``serves_now``);
    else the session serving that Claude Code session."""
    if args.session is not None:
        return find_session(home, args.session)
    given = stdin_object(sys.stdin if stdin is None else stdin)
    claude_session = given.get("session_id") if given else None
    claude_session = claude_session if isinstance(claude_session, str) and claude_session else None
    if env.get(SESSION_ENV):
        session = find_session(home, env[SESSION_ENV])
        if session is None or claude_session is None:
            return session
        return session if serves_now(home, session, claude_session) else None
    return serving_session(home, claude_session) if claude_session else None


def main(argv: Sequence[str] | None = None, *, stdin: Any = None, stdout: Any = None,
         env: Mapping[str, str] | None = None) -> int:
    """``contexto statusline`` without the rest of the CLI: ``contexto.cli`` hands the arguments straight
    here, so a status line does not load every other command to print one line. A usage error prints
    argparse's message on stderr and still exits 0."""
    given = sys.argv[1:] if argv is None else list(argv)
    args = _plain_arguments(given)
    if args is None:
        import argparse

        parser = argparse.ArgumentParser(prog="contexto statusline", description=DESCRIPTION)
        add_arguments(parser)
        try:
            args = parser.parse_args(given)
        except SystemExit:  # --help, or a usage error: argparse has printed it
            return 0
    return run(args, stdin=stdin, stdout=stdout, env=env)


def _plain_arguments(argv: Sequence[str]) -> Any:
    """The arguments when each is ``--color`` or a whole ``--name=value`` token, as ``installed_command``
    writes them (and for no arguments at all), read without loading argparse; None for any other
    spelling, which argparse then reads, or reports."""
    from types import SimpleNamespace

    args = SimpleNamespace(session=None, home=None, own_settings=[], color=False)
    for arg in argv:
        name, eq, value = arg.partition("=")
        if arg == "--color":
            args.color = True
        elif eq and name == "--session":
            args.session = value
        elif eq and name == "--home":
            args.home = value
        elif eq and name == "--own-settings":
            args.own_settings.append(value)
        else:
            return None
    return args
