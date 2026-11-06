"""``contexto run claude``: run Claude Code with every model request going through the Contexto proxy.

Steps (M2 contract, Architecture):

1. Resolve the upstream Claude Code would otherwise use (``contexto.upstream``), before anything
   starts.
2. Open session S in ``contexto.paths.contexto_home()`` and start ``ContextoProxy`` on
   ``127.0.0.1:<free port>`` with the rewrite engine as its transform.
3. Write two temporary files, mode 0600, in a private temporary directory (never in the user's
   Claude configuration): a ``--settings`` file holding ``env.ANTHROPIC_BASE_URL = <proxy URL>`` and
   ``permissions.allow`` with one rule per Contexto MCP tool, by its exact name (D44:
   ``mcp__contexto__contexto_recall`` and the other four), and an ``--mcp-config`` file that makes
   Claude Code spawn ``contexto mcp --session S`` over stdio with the current interpreter.
4. Run ``claude --settings=<file> --mcp-config=<file> [user args]`` (``claude_argv``: one argument per
   flag, so the variadic ``--mcp-config`` cannot take a prompt right after ``claude``) as a child with
   the terminal passed through, wait for it, stop the proxy, delete the temporary files, and return
   claude's exit code.

The user's own settings, hooks, and MCP servers still load: a flag-level ``--settings`` only adds
the base URL and the allow rules. Its base URL outranks the user, project, and local settings files.
Its allow rules join the user's permission rules rather than replacing them, and the user's ``deny``
and ``ask`` rules still win over them (measured with zero spend on Claude Code 2.1.302: a deny takes
the tool out of the request, an ask refuses it in ``-p`` mode). Without the rules, 2.1.302 refuses
every Contexto tool in ``-p`` mode ("you haven't granted it yet") and the session loses recall.
Limit: organization-managed settings outrank ``--settings``; if they set ``ANTHROPIC_BASE_URL``,
Claude Code bypasses the proxy and Contexto cannot keep the prompt bounded. ``CLAUDE_CONFIG_DIR`` is
never set and no user configuration file is edited.

Resume (D55): the proxy records each Claude Code session id it serves (the ``X-Claude-Code-Session-Id``
header, else ``metadata.user_id``) in the session's metadata. Claude Code keeps that id across
``--resume`` and ``--continue`` (new only with ``--fork-session``; measured on 2.1.314), and names its
transcript after it. Without ``--session``, ``claude --resume <id>`` reuses the Contexto session that
served ``<id>``, and ``claude --continue`` the one that last served Claude Code in this directory; with
no match, or ``--resume`` without an id (Claude Code's picker), the launch says so on stderr and opens
a new session. After claude exits, the launcher prints the command that resumes the conversation
through Contexto, since Claude Code's own exit hint (``claude --resume <id>``) bypasses it.

Signals: Ctrl+C in a terminal reaches every process in the foreground group, so Claude Code gets
it directly; the launcher ignores SIGINT while claude runs so that an interrupt meant for claude
does not tear down the proxy under it. SIGTERM, SIGHUP and SIGQUIT sent to the launcher are forwarded
to claude; SIGUSR1, SIGUSR2 and SIGALRM, which would otherwise kill the launcher alone, end claude
with SIGTERM. Before claude starts (the version check) and after it exits, these signals unwind
through the launcher's cleanup and exit 128 + N. SIGKILL cannot be caught, so a watchdog process
(its own session, so terminal signals miss it) holds a pipe from the launcher: when the pipe closes
without the launcher's "done", it sends claude SIGTERM and removes the temporary files. What a SIGKILL
leaves: the session log has no ``exit`` event, and if the watchdog is killed too, the temporary
directory stays in ``$TMPDIR`` (the proxy URL, the tool names, the session id and the home path;
no secrets). Terminal resize (SIGWINCH) goes from the kernel to claude without the launcher's help.

Window budget (contract rule 4): the engine keeps ``W = bound - headroom - F`` tokens for the
working window, where ``F`` is the fixed overhead (system, tools, carried content, pinned text, and
page index). ``F`` is only measured after the first upstream response, so at start the launcher
warns when an assumed ``F`` leaves less than 25,000 tokens: 92,000 inside an OpenRig seat (M0
measured seat heads of 82k to 84k before pins) and 28,000 otherwise (D55: 18,641 to 27,925 measured
on Claude Code 2.1.314 with tool search on). Once the engine measures ``F``, a ``bound_too_small``
event or a window budget under 25,000 is reported once. So is a main conversation running without
Contexto's MCP tools (``contexto.toolcheck``): neither CLI reports an MCP server that fails to start,
so the proxy checks and the launcher warns. So is the first Contexto tool call Claude Code refused for
lack of a permission rule (``mcp_tools_denied``, D44). While claude runs it owns the terminal, and a
line written then lands inside its TUI (D55), so these reports are logged at once and printed after
claude exits. So is a CLI version Contexto was not tested with (``contexto.cliversions``, D38:
``<cli> --version`` at start; ``contexto run --no-version-check <cli>`` or
``CONTEXTO_NO_VERSION_CHECK=1`` skips it), before claude starts. stderr stays quiet otherwise.

Exit codes: claude's own code; 128 + N if claude died from signal N, or if the launcher got signal N
before claude started; 2 for a usage error such as a bound above the guard; 1 if the proxy cannot
start; 127 if ``claude`` is not on PATH or cannot be run; 130 for a Ctrl+C during the version check,
before claude starts.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contexto.cliversions import launch_check
from contexto.mcp import SERVER_NAME as MCP_SERVER_NAME
from contexto.mcp import TOOLS as MCP_TOOLS
from contexto.paths import contexto_home, mcp_env, session_dir
from contexto.private import affinity_salt
from contexto.proxy import ContextoProxy, affinity_key, identity_transform, is_vendor_host
from contexto.store import LIVE_LOCK, SessionStore
from contexto.toolcheck import denied_tools_message, missing_tools_message
from contexto.upstream import Upstream, resolve_upstream, untrusted_note

DEFAULT_BOUND = 150_000
REPORT_LOCK_TIMEOUT_S = 2.0  # report_missing_tools: how long the exit waits for a request still in the engine
MIN_WINDOW_BUDGET = 25_000
DEFAULT_HEADROOM_FLOOR = 25_000
ASSUMED_OVERHEAD_OPENRIG = 92_000
ASSUMED_OVERHEAD_DEFAULT = 28_000  # D55: 18,641 to 27,925 measured on Claude Code 2.1.314, tool search on
BOUND_GUARD = 170_000  # below Claude Code's idle compaction (200,000) with room for an over-bound turn
EXIT_USAGE = 2
EXIT_PROXY = 1
EXIT_NOT_FOUND = 127
EXIT_INTERRUPTED = 130  # 128 + SIGINT: Ctrl+C before the CLI started
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
# Signals whose default action would kill the launcher alone; while claude runs they end it with SIGTERM
# (claude's runtime may give SIGUSR1 a meaning of its own, such as a debugger).
TERMINATING_SIGNALS = (signal.SIGUSR1, signal.SIGUSR2, signal.SIGALRM)
WATCHDOG_DONE = b"done\n"
WATCHDOG_EXITED = b"exited\n"  # the child was reaped: its pid may be reused
# Contexto's own ``contexto run`` options, and whether each takes a value (D55: a hint when one is put
# after the CLI's name). The codex options are added by contexto.codex.
RUN_OPTIONS = {"--bound": True, "--allow-large-bound": False, "--window-fraction": True, "--headroom-floor": True,
               "--session": True, "--upstream": True, "--observe-thinking": False, "--no-version-check": False}
# Claude Code 2.1.314's options whose value is the next argument even when it starts with "-" (``<value>``
# in ``claude --help``; variadic ones take at least that one). ``claude_options`` skips it.
CLAUDE_VALUE_OPTIONS = frozenset({
    "--add-dir", "--agent", "--agents", "--allowedTools", "--allowed-tools", "--append-system-prompt",
    "--append-system-prompt-file", "--autocompact", "--betas", "--debug-file", "--disallowedTools",
    "--disallowed-tools", "--effort", "--environment", "--fallback-model", "--file", "--input-format",
    "--json-schema", "--max-budget-usd", "--mcp-config", "--model", "-n", "--name", "--output-format",
    "--permission-mode", "--permission-prompts", "--plugin-dir", "--plugin-url",
    "--remote-control-session-name-prefix", "--session-id", "--setting-sources", "--settings",
    "--system-prompt", "--system-prompt-file", "--system-prompt-snapshot", "--tools"})
CLAUDE_SESSION_HEADER = "x-claude-code-session-id"
CLI_SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}")  # Claude Code sends a UUID
LEGACY_USER_ID_SESSION = re.compile(r"_session_([0-9A-Fa-f-]{36})")  # user_<hash>_account_<uuid>_session_<uuid>
# Claude Code expands ``${VAR}`` (and ``${VAR:-default}``) in an MCP config's command, args and env, with
# no escape (2.1.314: ``$${X}`` gives ``$`` and the value); ``$VAR`` is kept as written.
MCP_CONFIG_EXPANSION = "${"


class LaunchError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class Terminated(BaseException):
    """A terminating signal while claude is not running. A BaseException, like KeyboardInterrupt, so
    the version check's ``except Exception`` lets it through to the launcher's cleanup."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@dataclass
class LaunchOptions:
    claude_args: list[str] = field(default_factory=list)
    bound: int = DEFAULT_BOUND
    session_id: str | None = None
    upstream: str | None = None
    observe_thinking: bool = False
    allow_large_bound: bool = False
    window_fraction: float | None = None
    headroom_floor: int | None = None
    claude_bin: str = "claude"
    version_check: bool = True


def check_user_args(claude_args: list[str]) -> None:
    """Fail closed on a user ``--settings``: Claude Code takes a single ``--settings``, so a second one
    could replace ours and silently send requests around the proxy. ``--mcp-config`` is variadic and
    allowed. Every argument is checked, after a ``--`` too: a ``--`` can be another option's value
    (``--append-system-prompt -- --settings x.json``), and Claude Code 2.1.302 and 2.1.310 then still
    read the ``--settings`` after it. So a prompt that is exactly ``--settings`` or starts with
    ``--settings=`` is refused as well."""
    for arg in claude_args:
        if arg == "--settings" or arg.startswith("--settings="):
            raise LaunchError(
                "--settings in the claude arguments is not supported: Claude Code accepts one --settings, "
                "and contexto uses it to route requests through its proxy, so a second one could bypass "
                "the proxy. Put those settings in your Claude settings files instead "
                "(see README, 'contexto run'). Every argument is checked, including those after --, so a "
                "prompt that is exactly --settings or starts with --settings= is refused too.", EXIT_USAGE)


def check_misplaced_options(cli: str, cli_args: list[str]) -> None:
    """D55: Contexto's own options right after the CLI's name go to the CLI, which refuses them with its
    own error (Claude Code 2.1.314: "unknown option '--session' (Did you mean --version?)"). Refuse them
    first, naming the command with those options moved before the CLI's name. Only the leading
    arguments are read: later ones can be a prompt or another option's value."""
    moved: list[str] = []
    rest = list(cli_args)
    while rest:
        name = rest[0].split("=", 1)[0]
        if name not in RUN_OPTIONS:
            break
        takes_value = RUN_OPTIONS[name] and "=" not in rest[0]
        moved += rest[:2] if takes_value else rest[:1]
        rest = rest[2:] if takes_value else rest[1:]
    if moved:
        first = moved[0].split("=", 1)[0]
        raise LaunchError(
            f"{first} after '{cli}' is a Contexto option, and everything after '{cli}' goes to the CLI "
            f"unchanged. Put Contexto's options before '{cli}': {shlex.join(['contexto', 'run', *moved, cli, *rest])}",
            EXIT_USAGE)


def check_upstream(upstream: Upstream) -> None:
    """A malformed upstream URL is a usage error naming where it came from, before any session is opened
    (``http://[::1`` raised ValueError from ``urlsplit`` with a traceback and left an empty session)."""
    try:
        parts = urlsplit(upstream.url)
        _ = parts.port  # parsed on access; raises for a bad one
        ok = parts.scheme in ("http", "https") and bool(parts.hostname)
        problem = "upstream must be an http(s) base URL"
    except ValueError as exc:
        ok, problem = False, str(exc)
    if not ok:
        raise LaunchError(f"invalid upstream {upstream.url!r} (from {upstream.source}): {problem}", EXIT_USAGE)


def check_home(home: Path) -> None:
    """``$CONTEXTO_HOME`` must be a directory or not exist yet; it is never chmodded here (a home the user
    made is kept as it is; one Contexto creates is 0700)."""
    if os.path.lexists(home) and not home.is_dir():
        raise LaunchError(f"CONTEXTO_HOME {str(home)!r} is not a directory", EXIT_USAGE)


def check_mcp_values(session_id: str, home: Path, executable: str) -> None:
    """D55: Claude Code expands ``${VAR}`` in the MCP config it is given, so a home, session id or
    interpreter path holding ``${`` would make the MCP server open another store (or run another
    program), with no warning. There is no escape, so such a value is refused."""
    for what, value in (("CONTEXTO_HOME", str(home)), ("the session id", session_id),
                        ("the Python interpreter path", executable)):
        if MCP_CONFIG_EXPANSION in value:
            raise LaunchError(f"{what} {value!r} contains '${{': Claude Code would expand it in the MCP config, "
                              f"and Contexto's MCP server would read another store. Use a value without '${{'.",
                              EXIT_USAGE)


def cli_not_found(name: str, path: str | None = None) -> str:
    """Why ``shutil.which`` found no usable ``name``: a non-executable file, a broken symlink or a
    directory on PATH is named with what is wrong; otherwise "not found on PATH"."""
    candidates = [Path(name)] if os.sep in name else [
        Path(d or os.curdir) / name for d in (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)]
    for candidate in candidates:
        if candidate.is_symlink() and not candidate.exists():
            return f"'{name}' at {candidate} is a broken symlink (to {os.readlink(candidate)})"
        if candidate.is_dir():
            return f"'{name}' at {candidate} is a directory, not a program"
        if candidate.is_file() and not os.access(candidate, os.X_OK):
            return f"'{name}' at {candidate} is not executable (chmod +x it, or fix PATH)"
    return f"'{name}' not found on PATH"


def check_bound(bound: int, allow_large_bound: bool) -> None:
    if bound <= 0:
        raise LaunchError(f"--bound must be positive, got {bound}", EXIT_USAGE)
    if bound > BOUND_GUARD and not allow_large_bound:
        raise LaunchError(
            f"--bound {bound} is above {BOUND_GUARD:,}: Claude Code may compact on its own (idle compaction "
            f"starts at 200,000 by default). Pass --allow-large-bound to override.", EXIT_USAGE)


def engine_config(options: LaunchOptions) -> dict[str, int | float]:
    """Keyword arguments for ``contexto.rewrite.Config``; unset values keep the engine's defaults."""
    config: dict[str, int | float] = {"bound": options.bound}
    if options.window_fraction is not None:
        config["window_fraction"] = options.window_fraction
    if options.headroom_floor is not None:
        config["headroom_floor"] = options.headroom_floor
    return config


def assumed_overhead(env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    return ASSUMED_OVERHEAD_OPENRIG if env.get("OPENRIG_SESSION_NAME") else ASSUMED_OVERHEAD_DEFAULT


def start_warning(bound: int, headroom_floor: int | None, overhead: int) -> str | None:
    """A one-line warning when the assumed window budget is below MIN_WINDOW_BUDGET, else None. It says
    what that costs: at bound 60,000 on 2.1.314 every epoch logged ``turn_room_low`` and a turn ran out of
    recall room after 5 searches (QA 2026-11-05), so "rotations will page often" undersold it."""
    headroom = headroom_floor if headroom_floor is not None else DEFAULT_HEADROOM_FLOOR
    budget = bound - headroom - overhead
    if budget >= MIN_WINDOW_BUDGET:
        return None
    suggested = headroom + overhead + 2 * MIN_WINDOW_BUDGET
    sizing = f"bound {bound:,} - headroom {headroom:,} - typical fixed overhead {overhead:,}"
    if budget <= 0:
        return (f"bound {bound:,} leaves no window budget ({sizing} = {budget:,} tokens): the engine keeps only "
                f"the new turn, requests may exceed the bound, and recall will not fit. "
                f"Use --bound {suggested:,} or more.")
    return (f"window budget about {budget:,} tokens ({sizing}) is below {MIN_WINDOW_BUDGET:,}: turns page out "
            f"almost at once and recalled pages will barely fit, if at all. Consider --bound {suggested:,} or more.")


class NoticeLog:
    """Session log wrapper: everything goes to the store; the engine's first bound_too_small event,
    first window budget under MIN_WINDOW_BUDGET, first ``mcp_tools_denied`` (D44), and first
    ``mcp_tools_missing`` (from a request, or logged at exit by ``report_missing_tools``) are also shown
    on stderr, once each; an exit-time ``mcp_tools_missing`` that repeats a mid-run one is shown once
    more, after the CLI exited.

    Between ``hold`` and ``release`` (while the CLI owns the terminal) the lines are kept and printed at
    ``release``, in order: a line written mid-run landed in Claude Code's input box and garbled the next
    redraw (D55). A repeat of a warning still held is not added."""

    def __init__(self, store: Any, stderr) -> None:
        self.store = store
        self.stderr = stderr
        self.shown: set[str] = set()
        self.held: list[str] | None = None
        self.held_keys: set[str] = set()

    def hold(self) -> None:
        if self.held is None:
            self.held, self.held_keys = [], set()

    def release(self) -> None:
        held, self.held = self.held or [], None
        for message in held:
            print(f"contexto: {message}", file=self.stderr, flush=True)

    def log(self, event: dict[str, Any]) -> None:
        self.store.log(event)
        for inner in [event, *(event.get("transform_events") or [])]:
            if not isinstance(inner, dict):
                continue
            if inner.get("event") == "mcp_tools_missing":
                # Once during the run, and once more after the CLI exits if the tools were still missing
                # then: a warning printed mid-run can be drawn over by the CLI's TUI.
                repeated = inner.get("at") == "exit" and inner.get("repeated") is True
                message = missing_tools_message(inner.get("dialect"), inner.get("server_listed"))
                self._once("mcp_tools_missing_exit" if repeated else "mcp_tools_missing",
                           ("warning (repeated from the session): " if repeated else "warning: ") + message)
                continue
            if inner.get("event") == "mcp_tools_denied":
                self._once("mcp_tools_denied", "warning: " + denied_tools_message(inner.get("tools")))
                continue
            if inner is event:
                continue
            budget, overhead = inner.get("window_budget"), inner.get("fixed_overhead")
            if inner.get("event") == "bound_too_small":
                # Rule 4 window_minimal (contract PR #50): the engine still rotates, to a window of
                # only the new turn, which can still put the request over the bound.
                bound = inner.get("bound")
                self._once("bound_too_small", f"bound {bound:,} is too small: fixed overhead {overhead:,} leaves a "
                           f"window budget of {budget:,}; the engine keeps only the new turn, and requests may "
                           f"exceed the bound. Restart with a larger --bound."
                           if all(isinstance(v, int) for v in (bound, overhead, budget))
                           else "bound is too small for the measured fixed overhead; the engine keeps only the new "
                           "turn, and requests may exceed the bound.")
            elif isinstance(budget, int) and budget < MIN_WINDOW_BUDGET:
                self._once("small_window", f"measured window budget {budget:,} is below {MIN_WINDOW_BUDGET:,} "
                           f"(fixed overhead {overhead:,}); consider a larger --bound."
                           if isinstance(overhead, int) else f"measured window budget {budget:,} is small.")

    def _once(self, key: str, message: str) -> None:
        if key in self.shown:
            return
        self.shown.add(key)
        if self.held is None:
            print(f"contexto: {message}", file=self.stderr, flush=True)
        elif not (key == "mcp_tools_missing_exit" and "mcp_tools_missing" in self.held_keys):
            self.held.append(message)
            self.held_keys.add(key)


def claude_session(headers: dict[str, str], request: Any) -> str | None:
    """Claude Code's session id for a request: the ``X-Claude-Code-Session-Id`` header, else
    ``metadata.user_id`` (a JSON object with ``session_id`` on 2.1.314, ``..._session_<uuid>`` before);
    None when absent or not a plain id. The same id names Claude Code's transcript and is what
    ``claude --resume`` takes (measured on 2.1.314)."""
    for name, value in headers.items():
        if name.lower() == CLAUDE_SESSION_HEADER:
            return value if isinstance(value, str) and CLI_SESSION_ID.fullmatch(value) else None
    metadata = request.get("metadata") if isinstance(request, dict) else None
    user_id = metadata.get("user_id") if isinstance(metadata, dict) else None
    if not isinstance(user_id, str):
        return None
    if user_id.startswith("{"):
        try:
            value = json.loads(user_id).get("session_id")
        except (ValueError, AttributeError):
            return None
    else:
        match = LEGACY_USER_ID_SESSION.search(user_id)
        value = match.group(1) if match else None
    return value if isinstance(value, str) and CLI_SESSION_ID.fullmatch(value) else None


class SessionRecorder:
    """The engine with one addition (D55): each request's Claude Code session id is recorded in the
    session's metadata, at its first request in this launch (so a launch killed later still has it) and
    with its last-seen time at exit (``flush``), so ``--continue`` can pick the session that served
    Claude Code last. Recording never fails or changes a request. Every other attribute is the engine's
    (``tools_verdict``, ``preserves_input``)."""

    def __init__(self, transform: Callable[..., Any], store: Any, cli: str = "claude") -> None:
        self.transform = transform
        self.store = store
        self.cli = cli
        self.seen: dict[str, float] = {}  # Claude Code session id -> last seen, wall-clock seconds
        self.last: str | None = None

    def __call__(self, request: dict[str, Any], headers: dict[str, str], endpoint: str) -> Any:
        try:
            self._note(claude_session(headers, request))
        except Exception:  # bookkeeping for the next launch; never fail a request for it
            pass
        return self.transform(request, headers, endpoint)

    def _note(self, cli_session: str | None) -> None:
        if cli_session is None:
            return
        first = cli_session not in self.seen
        self.seen[cli_session] = time.time()
        self.last = cli_session
        if first:
            self.store.record_cli_session(self.cli, cli_session, self.seen[cli_session])

    def flush(self) -> None:
        """Record each id's last-seen time; runs in the cleanup, so it never raises. A copy is read: a
        request still in a handler after ``proxy_stop_incomplete`` can add an id meanwhile."""
        try:
            for cli_session, seen_at in list(self.seen.items()):
                self.store.record_cli_session(self.cli, cli_session, seen_at)
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        transform = self.__dict__.get("transform")
        if transform is None:
            raise AttributeError(name)
        return getattr(transform, name)


def claude_options(cli_args: list[str]) -> Iterator[tuple[int, str]]:
    """(index, argument) of each argument Claude Code reads as an option, up to ``--``. The value of an
    option in ``CLAUDE_VALUE_OPTIONS`` is skipped even when it starts with ``-`` (``--append-system-prompt
    --continue`` is a prompt, not a continue)."""
    i = 0
    while i < len(cli_args):
        arg = cli_args[i]
        if arg == "--":
            return
        if arg.startswith("-"):
            yield i, arg
            if arg in CLAUDE_VALUE_OPTIONS:
                i += 1
        i += 1


def resume_request(cli_args: list[str]) -> tuple[str, str | None] | None:
    """What Claude Code will resume: ``("continue", None)`` for ``-c``/``--continue``, ``("resume", id)``
    for ``-r``/``--resume <id>`` or ``--resume=<id>``, ``("resume", None)`` for ``--resume`` without a
    value (Claude Code's picker), else None. Only arguments ``claude_options`` names are read."""
    for i, arg in claude_options(cli_args):
        if arg in ("-c", "--continue"):
            return "continue", None
        if arg in ("-r", "--resume"):
            value = cli_args[i + 1] if i + 1 < len(cli_args) else None
            return "resume", value if value and not value.startswith("-") else None
        if arg.startswith("--resume="):
            return "resume", arg.split("=", 1)[1] or None
    return None


def forks(cli_args: list[str]) -> bool:
    """``--fork-session``: Claude Code gives the resumed or continued conversation a new id."""
    return any(arg == "--fork-session" for _, arg in claude_options(cli_args))


def served_sessions(home: Path) -> list[tuple[float, str, str, Path | None]]:
    """Every Claude Code session a Contexto session under ``home`` served: (last seen, Contexto session
    id, Claude Code session id, the Contexto session's workdir). A session that cannot be read is
    skipped: the lookup must never stop a launch."""
    try:
        names = [entry.name for entry in (Path(home) / "sessions").iterdir()]
    except OSError:
        return []
    found = []
    for name in names:
        try:
            store = SessionStore.open_existing(home, name)
            served, workdir = store.cli_sessions("claude"), store.workdir()
        except Exception:
            continue
        found += [(seen_at, name, cli_session, workdir) for cli_session, seen_at in served.items()]
    return found


def session_in_use(home: Path, session_id: str) -> bool:
    """Whether another process has the session open: the proxy, launcher and MCP server each hold a
    shared lock on its ``live.lock`` (D37), so a non-blocking exclusive lock fails. False when the lock
    cannot be opened (the open that follows reports a real problem)."""
    try:
        fd = os.open(session_dir(home, session_id) / LIVE_LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)  # releases the probe's lock
    return False


@dataclass
class SessionChoice:
    session_id: str
    continued: set[str] | None = None  # --continue: the Claude Code sessions the chosen session had served
    unmatched: bool = False  # a resume was asked for, and this launch got a new session


def choose_session(options: LaunchOptions, home: Path, cwd: Path, stderr) -> SessionChoice:
    """The session for this launch. ``--session`` wins and is never questioned. Without it, ``--resume
    <id>`` takes the session that served ``<id>`` (naming any other that served it too) and
    ``--continue`` the one that last served Claude Code in ``cwd``, unless that session is in use by
    another launch: two launches on one store share its epoch record and its recall room, which the
    contract leaves out of scope, so a new session opens instead. Every other resume gets a new session
    with a line on stderr saying why."""
    if options.session_id:
        return SessionChoice(options.session_id)
    request = resume_request(options.claude_args)
    new = uuid.uuid4().hex
    if request is None:
        return SessionChoice(new)

    def say(message: str) -> None:
        print(f"contexto: {message}", file=stderr, flush=True)

    kind, wanted = request
    if kind == "resume" and wanted is None:
        say("--resume without a session id opens Claude Code's picker, and Contexto cannot tell which "
            "conversation you will pick, so this launch uses a new Contexto session. To keep the "
            "conversation's store, run: contexto run claude --resume <id>")
        return SessionChoice(new, unmatched=True)
    served = served_sessions(home)
    if kind == "resume":
        shown = wanted if CLI_SESSION_ID.fullmatch(wanted) else repr(wanted)  # a search term, not an id
        matches = [s for s in served if s[2] == wanted]
        found = f"Claude Code session {shown} was served by Contexto session "
        missing = f"no Contexto session has served Claude Code session {shown}"
    else:
        here = cwd.resolve()
        matches = [s for s in served if s[3] == here]
        found = "the last Contexto session to serve Claude Code in this directory is "
        missing = "no Contexto session has served Claude Code in this directory"
    if not matches:
        say(f"{missing}; starting a new session (pass --session ID before 'claude' to use a given one)")
        return SessionChoice(new, unmatched=True)
    session = max(matches, key=lambda s: s[0])[1]
    if session_in_use(home, session):
        say(f"{found}{session}, which another launch has open; starting a new session (pass --session "
            f"{session} before 'claude' to share it)")
        return SessionChoice(new, unmatched=True)
    others = sorted({s[1] for s in matches if s[1] != session}) if kind == "resume" else []
    also = f" (newest; also served by {', '.join(others)}: pass --session ID to use one of those)" if others else ""
    say(f"{found}{session}{also}; {'resuming' if kind == 'resume' else 'continuing'} it")
    return SessionChoice(session, {s[2] for s in served if s[1] == session} if kind == "continue" else None)


def resume_command(options: LaunchOptions, session_id: str, cli_session: str) -> str:
    """The command that resumes Claude Code session ``cli_session`` through Contexto session
    ``session_id`` with this launch's Contexto options."""
    args = ["contexto", "run", "--session", session_id]
    if options.bound != DEFAULT_BOUND:
        args += ["--bound", str(options.bound)]
    if options.allow_large_bound:
        args.append("--allow-large-bound")
    if options.window_fraction is not None:
        args += ["--window-fraction", str(options.window_fraction)]
    if options.headroom_floor is not None:
        args += ["--headroom-floor", str(options.headroom_floor)]
    if options.upstream:
        args += ["--upstream", options.upstream]
    if options.observe_thinking:
        args.append("--observe-thinking")
    if not options.version_check:
        args.append("--no-version-check")
    return shlex.join([*args, "claude", "--resume", cli_session])


def open_session(home: Path, session_id: str, workdir: Path) -> Any:
    """Create the session and record the directory claude runs in, before claude starts (lane B
    checks file sources against it for staleness)."""
    store = SessionStore(home, session_id)
    store.set_workdir(workdir)
    return store


def build_engine(store: Any,
                 config: dict[str, int | float]) -> tuple[Callable[..., Any], Callable[..., Any] | None, str]:
    """The A2 rewrite engine as (transform, observe_usage, name); identity when it is not installed."""
    try:
        from contexto.rewrite import Config, RewriteEngine
    except ImportError:
        return identity_transform, None, "identity"
    engine = RewriteEngine(store, config=Config(**config))
    return engine, getattr(engine, "observe_usage", None), "rewrite"


def write_private_json(directory: Path, name: str, payload: dict[str, Any]) -> Path:
    """Create ``directory/name`` with mode 0600 from the first byte, holding ``payload``."""
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def contexto_tool_rules() -> list[str]:
    """D44: one permission rule per Contexto MCP tool, by its exact name (``mcp__contexto__contexto_recall``
    and the other four). They cover only the server ``mcp_payload`` names, and only the tools it serves."""
    return [f"mcp__{MCP_SERVER_NAME}__{tool['name']}" for tool in MCP_TOOLS]


def settings_payload(proxy_url: str) -> dict[str, Any]:
    """The flag-level ``--settings`` content: the proxy's base URL, and an allow rule for each of Contexto's
    own MCP tools (D44). Claude Code 2.1.302 refuses an MCP tool in ``-p`` mode unless a rule allows it
    ("you haven't granted it yet"), so without these rules ``contexto run claude -p`` loses recall."""
    return {"env": {"ANTHROPIC_BASE_URL": proxy_url}, "permissions": {"allow": contexto_tool_rules()}}


def mcp_payload(session_id: str, home: Path) -> dict[str, Any]:
    # Claude Code starts the server in the user's project; -P keeps that directory off sys.path,
    # so a project's own contexto package cannot run in place of the installed server. One
    # --session=<id> token, so an id starting with "-" is not read as an option.
    return {"mcpServers": {MCP_SERVER_NAME: {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-P", "-m", "contexto", "mcp", f"--session={session_id}"],
        "env": mcp_env(home),
    }}}


def claude_argv(claude: str, settings: Path, mcp: Path, claude_args: list[str]) -> list[str]:
    """The Claude Code command line. Each launcher flag and its file are one ``--flag=<file>`` token:
    ``--mcp-config`` takes several files, so in the two-token form Claude Code (2.1.302, 2.1.310) also
    reads the next argument as a config file, and ``contexto run claude "prompt"`` failed with "MCP
    config file not found". The ``=`` form ends the option at its own token."""
    return [claude, f"--settings={settings}", f"--mcp-config={mcp}", *claude_args]


def exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


class LaunchSignals:
    """The launcher's terminating signals while claude is not running (D55): SIGTERM, SIGHUP, SIGQUIT,
    SIGUSR1, SIGUSR2 and SIGALRM raise ``Terminated``, which unwinds through the cleanup (the version
    check kills its ``--version`` process group on the way). Without this, a SIGTERM or SIGHUP during the
    version check killed the launcher outright and left the temporary files. ``disarm`` makes them
    no-ops once the cleanup starts, so a second signal cannot cut it short; ``run_child`` installs its
    own forwarding handlers while claude runs. Outside the main thread nothing is installed."""

    def __init__(self) -> None:
        self.armed = True
        self.previous: dict[int, Any] = {}

    def __enter__(self) -> LaunchSignals:
        for signum in (*FORWARDED_SIGNALS, *TERMINATING_SIGNALS):
            try:
                self.previous[signum] = signal.signal(signum, self._raise)
            except ValueError:  # not the main thread
                break
        return self

    def _raise(self, signum: int, frame: Any) -> None:
        if self.armed:
            raise Terminated(signum)

    def disarm(self) -> None:
        self.armed = False

    def __exit__(self, *exc: Any) -> None:
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)


WATCHDOG = r"""
import os, shutil, signal, sys
pid = None
for line in iter(sys.stdin.buffer.readline, b""):
    if line == b"done\n":
        sys.exit(0)
    if line.startswith(b"pid "):
        pid = int(line[4:])
    if line == b"exited\n":
        pid = None
if pid is not None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
shutil.rmtree(sys.argv[1], ignore_errors=True)
"""


class Watchdog:
    """Ends claude and removes the temporary files if the launcher dies without its cleanup (SIGKILL;
    macOS has no PDEATHSIG). A small Python process in its own session (no terminal signal reaches it)
    reads a pipe from the launcher: ``pid <n>`` when claude starts, ``exited`` once it is reaped (its
    pid may then be reused, so it is never signalled after that), ``done`` from the cleanup. End of
    file without ``done`` means the launcher is gone: it sends claude SIGTERM if claude has not exited
    (Claude Code exits on it) and deletes the temporary directory. The session log gets no ``exit``
    event in that case."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process

    @classmethod
    def start(cls, tmp: Path, store: Any) -> Watchdog | None:
        try:
            process = subprocess.Popen([sys.executable, "-I", "-S", "-c", WATCHDOG, str(tmp)],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
        except (OSError, subprocess.SubprocessError) as exc:
            store.log({"event": "watchdog_unavailable", "error": type(exc).__name__})
            return None
        return cls(process)

    def watch(self, pid: int) -> None:
        self._send(f"pid {pid}\n".encode())

    def forget(self) -> None:
        self._send(WATCHDOG_EXITED)

    def done(self) -> None:
        self._send(WATCHDOG_DONE)
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def _send(self, data: bytes) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (OSError, ValueError):  # the watchdog is gone; the launch goes on without it
            pass


def run_claude(options: LaunchOptions, *, stderr=None) -> int:
    stderr = stderr or sys.stderr
    with LaunchSignals() as signals:
        try:
            return _run(options, stderr, signals)
        except LaunchError as exc:
            print(f"contexto run: {exc}", file=stderr)
            return exc.code
        except Terminated as exc:
            return 128 + exc.signum


def options_from_args(args: Any) -> LaunchOptions:
    """LaunchOptions from the ``contexto run`` argparse namespace (also used by contexto.codex)."""
    return LaunchOptions(
        claude_args=list(getattr(args, "cli_args", None) or []), bound=args.bound, session_id=args.session,
        upstream=args.upstream, observe_thinking=args.observe_thinking, allow_large_bound=args.allow_large_bound,
        window_fraction=args.window_fraction, headroom_floor=args.headroom_floor,
        version_check=not getattr(args, "no_version_check", False))


def check_options(options: LaunchOptions) -> None:
    check_bound(options.bound, options.allow_large_bound)
    if options.window_fraction is not None and not 0 < options.window_fraction < 1:
        raise LaunchError(f"--window-fraction must be between 0 and 1, got {options.window_fraction}", EXIT_USAGE)
    if options.headroom_floor is not None and not 0 < options.headroom_floor < options.bound:
        raise LaunchError(f"--headroom-floor must be between 0 and the bound, got {options.headroom_floor}",
                          EXIT_USAGE)


def session_affinity_key(store: Any, upstream_url: str) -> str | None:
    """The store's ``X-Session-Affinity`` value (D36), salted by the install's ``affinity.salt`` in
    the store's home. None, without touching the salt, for a vendor upstream (no header is sent
    there) or a store without a home (in memory); None when the salt cannot be read, which is logged
    and only costs the header."""
    home, session_id = getattr(store, "home", None), getattr(store, "session_id", None)
    if home is None or not isinstance(session_id, str):
        return None
    try:
        host = urlsplit(upstream_url).hostname
    except ValueError:  # malformed; the proxy refuses the URL right after this
        return None
    if not host or is_vendor_host(host):
        return None
    try:
        return affinity_key(session_id, affinity_salt(home))
    except (OSError, ValueError) as exc:
        store.log({"event": "affinity_salt_unavailable", "error": type(exc).__name__})
        return None


def start_proxy(store: Any, upstream_url: str, upstream_source: str, config: dict[str, int | float],
                observe_thinking: bool, *, stderr=None, cli: str | None = None) -> tuple[ContextoProxy, str]:
    """Build the rewrite engine for ``store`` and start the proxy on 127.0.0.1 with a free port.
    Returns ``(proxy, engine_name)``; the caller stops the proxy. Shared by claude and codex. With
    ``cli="claude"`` the engine is wrapped in a ``SessionRecorder`` (``proxy.transform``), so the
    session records which Claude Code sessions it served (D55)."""
    # The previous launch's MCP server record must not count for this one (contexto.toolcheck): a
    # back-to-back launch can start within the check's clock slack of that server's tools/list.
    clear = getattr(store, "clear_mcp_listed", None)
    try:
        if callable(clear):
            clear()
    except Exception:  # a diagnostic record; never stop a launch for it
        pass
    transform, observe_usage, engine_name = build_engine(store, config)
    if cli == "claude":
        transform = SessionRecorder(transform, store)
    key = session_affinity_key(store, upstream_url)
    try:
        proxy = ContextoProxy(upstream_url, NoticeLog(store, stderr or sys.stderr), transform,
                              observe_usage=observe_usage, observe_thinking=observe_thinking,
                              upstream_source=upstream_source, affinity_key=key)
    except ValueError as exc:
        raise LaunchError(f"invalid upstream: {exc}", EXIT_USAGE) from exc
    try:
        proxy.start(0)
    except OSError as exc:
        raise LaunchError(f"cannot start the proxy: {exc}", EXIT_PROXY) from exc
    if engine_name == "identity":
        store.log({"event": "engine_unavailable", "detail": "contexto.rewrite not installed; identity transform"})
    return proxy, engine_name


def check_cli_version(cli: str, binary: str, store: Any, *, enabled: bool = True, stderr=None) -> bool:
    """D38: log the installed CLI's version (``<binary> --version``, a few seconds at most) and print one
    warning line when Contexto was not tested with it. Never raises; a CLI whose version cannot be read
    is logged, not warned about. Returns False only when the user pressed Ctrl+C during the check: the
    caller then exits ``EXIT_INTERRUPTED`` through its normal cleanup, without starting the CLI and
    without a traceback. Shared by claude and codex."""
    out = stderr or sys.stderr
    try:
        event, warning = launch_check(cli, binary, enabled=enabled)
    except KeyboardInterrupt:  # the check killed its --version process group before this
        event, warning = {"event": "cli_version_check", "cli": cli, "error": "interrupted"}, None
    try:
        store.log(event)
    except Exception:  # a diagnostic record; never stop a launch for it
        pass
    if event.get("error") == "interrupted":
        print("contexto: interrupted", file=out, flush=True)
        return False
    if warning:
        print(f"contexto: {warning}", file=out, flush=True)
    return True


def report_missing_tools(proxy: ContextoProxy) -> None:
    """At exit, after the proxy stopped: if the main conversation's last request lacked Contexto's MCP
    tools, log the engine's exit-time ``mcp_tools_missing`` (``repeated`` if it was reported during the
    run); the proxy's NoticeLog shows the warning. Shared by claude and codex. Runs under the transform
    lock, so a request still in a handler (after ``proxy_stop_incomplete``) cannot run the check at the
    same time; a handler stuck holding it makes the exit skip the report after a moment, not hang."""
    verdict = getattr(proxy.transform, "tools_verdict", None)
    if not callable(verdict):
        return
    lock = getattr(proxy, "transform_lock", None)
    if lock is not None and not lock.acquire(timeout=REPORT_LOCK_TIMEOUT_S):
        return
    try:
        event = verdict()
        if event:
            proxy.log(event)
    except Exception:  # a diagnostic; never change the exit
        return
    finally:
        if lock is not None:
            lock.release()


def run_child(argv: list[str], env: dict[str, str] | None = None, *, stderr=None,
              watchdog: Watchdog | None = None) -> int:
    """Run a CLI with the terminal passed through and return its exit code (128 + N if it died from
    signal N, 127 if it cannot be started). Ctrl+C reaches it through the terminal while the
    launcher ignores SIGINT; SIGTERM, SIGHUP and SIGQUIT are forwarded, and SIGUSR1, SIGUSR2 and
    SIGALRM end it with SIGTERM. The handlers are installed before the spawn, so a signal that arrives
    while the child starts is held and forwarded once it exists, not lost or raised mid-spawn.
    ``watchdog`` is told the child's pid when it starts and told to forget it once it is reaped, so a
    later death of the launcher never signals a reused pid. Shared by claude and codex."""
    forwarding = _ChildSignals()
    previous = forwarding.install()
    child: subprocess.Popen | None = None
    try:
        try:
            child = subprocess.Popen(argv, env=env)
        except (FileNotFoundError, PermissionError) as exc:
            print(f"contexto: cannot start {argv[0]!r}: {exc.strerror}", file=stderr or sys.stderr)
            return EXIT_NOT_FOUND
        if watchdog is not None:
            watchdog.watch(child.pid)
        forwarding.attach(child)
        return exit_code(child.wait())
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if watchdog is not None:
                watchdog.forget()


def _run(options: LaunchOptions, stderr, signals: LaunchSignals) -> int:
    check_misplaced_options("claude", options.claude_args)
    check_user_args(options.claude_args)
    check_options(options)
    claude = shutil.which(options.claude_bin)
    if claude is None:
        raise LaunchError(cli_not_found(options.claude_bin), EXIT_NOT_FOUND)
    upstream = resolve_upstream(options.upstream, cwd=Path.cwd())
    check_upstream(upstream)
    if upstream.ignored:  # CLAUDE_CODE_IGNORE_PERSISTED_TRUST is set: as Claude Code -p, no project base URL (D52)
        print(f"contexto: {untrusted_note('env.ANTHROPIC_BASE_URL', upstream.ignored, 'Pass --upstream', os.environ)}",
              file=stderr)
    home = contexto_home()
    check_home(home)
    choice = choose_session(options, home, Path.cwd(), stderr)
    session_id = choice.session_id
    check_mcp_values(session_id, home, sys.executable)
    try:
        store = open_session(home, session_id, Path.cwd())
    except (OSError, ValueError) as exc:
        raise LaunchError(f"cannot open session {session_id!r}: {exc}", EXIT_USAGE) from exc
    config = engine_config(options)
    proxy, engine_name = start_proxy(store, upstream.url, upstream.source, config, options.observe_thinking,
                                     stderr=stderr, cli="claude")
    notices, recorder = proxy.session_log, proxy.transform
    tmp: Path | None = None
    watchdog: Watchdog | None = None
    code = EXIT_PROXY
    try:
        tmp = Path(tempfile.mkdtemp(prefix="contexto-run-"))  # mode 0700, removed on exit
        watchdog = Watchdog.start(tmp, store)
        settings = write_private_json(tmp, "settings.json", settings_payload(proxy.url))
        mcp = write_private_json(tmp, "mcp.json", mcp_payload(session_id, home))
        store.log({"event": "launch", "cli": "claude", "session": session_id, "config": config,
                   "engine": engine_name, "upstream_source": upstream.source,
                   "observe_thinking": options.observe_thinking, "user_arg_count": len(options.claude_args),
                   "affinity_header": proxy.affinity_header is not None})
        print(f"contexto: session {session_id} (bound {options.bound:,}, upstream {upstream.source})",
              file=stderr, flush=True)
        if not check_cli_version("claude", claude, store, enabled=options.version_check, stderr=stderr):
            code = EXIT_INTERRUPTED
            return code
        warning = start_warning(options.bound, options.headroom_floor, assumed_overhead())
        if warning:
            print(f"contexto: {warning}", file=stderr, flush=True)
            store.log({"event": "window_budget_warning", "bound": options.bound,
                       "assumed_overhead": assumed_overhead()})
        notices.hold()  # claude owns the terminal until it exits (D55)
        code = run_child(claude_argv(claude, settings, mcp, options.claude_args), stderr=stderr, watchdog=watchdog)
        return code
    except Terminated as exc:
        code = 128 + exc.signum
        raise
    finally:
        signals.disarm()
        proxy.stop()
        report_missing_tools(proxy)
        recorder.flush()
        notices.release()
        if watchdog is not None:
            watchdog.done()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        store.log({"event": "exit", "code": code})
        after_exit(options, home, session_id, recorder, choice, stderr)


def after_exit(options: LaunchOptions, home: Path, session_id: str, recorder: SessionRecorder,
               choice: SessionChoice, stderr) -> None:
    """The last lines, after claude exited (D55), when a request named a Claude Code session:
    - a ``--continue`` that Claude Code took to a conversation the chosen session never served (not
      with ``--fork-session``, whose new id continues the same history);
    - a resume that got a new session while another session served the same conversation (the
      picker, a search term, or a session in use): the conversation is now split across two stores;
    - the command that resumes this conversation through Contexto (Claude Code's own exit hint,
      ``claude --resume <id>``, bypasses it)."""
    last = recorder.last
    if last is None:
        return
    if choice.continued is not None and not forks(options.claude_args) and not choice.continued & set(recorder.seen):
        print(f"contexto: warning: Claude Code continued session {last}, which Contexto session {session_id} "
              f"had not served (its latest conversation here ran without Contexto?), so that session's pages "
              f"and pins come from another conversation. Resume by id to avoid this.", file=stderr, flush=True)
    if choice.unmatched:
        others = sorted({s[1] for s in served_sessions(home) if s[2] == last and s[1] != session_id})
        if others:
            print(f"contexto: Claude Code session {last} was also served by Contexto session "
                  f"{', '.join(others)}; its earlier pages and pins are there, and a later resume picks this "
                  f"session (the newest) unless you pass --session ID.", file=stderr, flush=True)
    print(f"contexto: resume this conversation through Contexto with: "
          f"{resume_command(options, session_id, last)}", file=stderr, flush=True)


class _ChildSignals:
    """``run_child``'s handlers: keep the launcher alive through the user's Ctrl+C and forward
    termination to the child, the forwarded signals as they are and the other terminating ones as
    SIGTERM. A signal that arrives before the child exists is kept and sent by ``attach``. Python
    handlers (not SIG_IGN) are used, so nothing is inherited by the child."""

    def __init__(self) -> None:
        self.child: subprocess.Popen | None = None
        self.pending: int | None = None

    def install(self) -> dict[int, Any]:
        previous: dict[int, Any] = {signal.SIGINT: signal.signal(signal.SIGINT, self._ignore)}
        for signum in (*FORWARDED_SIGNALS, *TERMINATING_SIGNALS):
            previous[signum] = signal.signal(signum, self._forward)
        return previous

    def attach(self, child: subprocess.Popen) -> None:
        self.child = child
        if self.pending is not None:
            self._send(self.pending)

    def _ignore(self, signum: int, frame: Any) -> None:
        pass

    def _forward(self, signum: int, frame: Any) -> None:
        signum = signum if signum in FORWARDED_SIGNALS else signal.SIGTERM
        if self.child is None:
            self.pending = signum
        else:
            self._send(signum)

    def _send(self, signum: int) -> None:
        if self.child is not None and self.child.poll() is None:
            self.child.send_signal(signum)
