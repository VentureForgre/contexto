"""The Claude Code and Codex versions Contexto was tested with, and the checks against them (D38).

Contexto recognises parts of each CLI's request by text the CLI writes: Claude Code's session-start
message by its ``# Environment`` heading and its context blocks by their first lines
(``contexto.rewrite``, D32 and D33), and Codex's session context by its section tags
(``contexto.codex``). A CLI update can change that text, and nothing fails: the message falls into
another class and may be paged. So two checks look for a CLI Contexto was not tested with:

- At launch, ``contexto run`` runs ``<cli> --version`` and, when the version is not in
  ``TESTED_VERSIONS``, prints one warning line on stderr. The command runs in its own process group
  with stdin closed; at most ``MAX_VERSION_OUTPUT`` bytes of its output are read, until a line with a
  version, end of file, or ``VERSION_TIMEOUT_S``, and then the group is killed. The launch goes on
  whatever happens; a CLI that does not answer is logged and not warned about. ``contexto run
  --no-version-check <cli>`` or ``CONTEXTO_NO_VERSION_CHECK=1`` skips the check.
- In the proxy, ``ClientVersions`` reads each request's ``User-Agent`` (measured with zero spend:
  Claude Code 2.1.302 sends ``claude-cli/2.1.302 (external, sdk-cli)``, codex-cli 0.163.0 sends
  ``codex_exec/0.163.0 (...)``), logs each version it sees once, and logs one warning when a version
  is untested. It also checks, once, that the main conversation's session start was recognised
  (``Dialect.session_start_seen``) and logs one warning when the request carries the CLI's injected
  context but no session start Contexto recognises.

Every event is content-free: CLI names, version strings, and booleans.

``TESTED_VERSIONS`` is the single list. Each tuple runs oldest to newest; the newest is the one the
measured-on constants next to each signature must name (``tests/test_cliversions.py`` checks it).
``docs/cli-updates.md`` is the routine for testing a new version and adding it here.
"""

from __future__ import annotations

import builtins
import os
import re
import selectors
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from contexto.platform_compat import (
    IS_WINDOWS,
    kill_process_group,
    new_process_group_kwargs,
    read_fd_until,
)

# Claude Code 2.1.302: session-start signature, context-block kinds, and the missing-tools notices
# (spikes/m2/scale/reminders), the permission refusal of Contexto's tools in -p mode (D44), and the
# live acceptance run on Claude Opus 5.5 (PR #221; live run #3 ran on 2.1.301). 2.1.310: the same
# zero-spend checks (docs/cli-updates.md steps 1 to 6) and a live recall check through deferred tools
# (D46); it turns tool search on by default, and its shapes are 2.1.302's with tool search on
# (docs/research/M2-claude-code-2.1.310.md). 2.1.314: zero-spend checks (steps 1 to 7), no live check;
# 2.1.318: the same zero-spend checks and full live acceptance (run #4, docs/research/M2-acceptance-run4.md);
# both keep the same request shapes and add <tools-used-before-summary> subagent stop reports (D56,
# docs/research/M2-claude-code-2.1.318.md). 2.1.328: verified with live real-CLI tests, Vertex/Foundry
# probes, and hint-header / session-start checks. codex-cli 0.154.0: the Responses shapes and acceptance
# (D30); 0.163.0: the MCP launch and missing-tools check (v0.2.8), and the same context-section
# openings, rechecked with zero spend for D38; 0.166.0: the same openings and shapes, rechecked with
# zero spend for D56; 0.170.0: the same lead and context-section openings, rechecked with zero spend.
TESTED_VERSIONS: dict[str, tuple[str, ...]] = {
    "claude": ("2.1.302", "2.1.310", "2.1.314", "2.1.318", "2.1.328"),
    "codex": ("0.154.0", "0.163.0", "0.166.0", "0.170.0"),
}
# Semver compatibility ranges: (min_inclusive, max_exclusive) as (major, minor, patch) tuples.
# Patch releases within the supported major/minor family (Claude Code 2.1.302+ in 2.1.x, Codex 0.154.0+
# in 0.x) are treated as compatible without spamming exact-patch warnings; runtime structural health
# checks (gateway hint headers, session_start_seen, hint_mismatch) detect actual behavioral drift.
SUPPORTED_RANGES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "claude": ((2, 1, 302), (2, 2, 0)),
    "codex": ((0, 154, 0), (1, 0, 0)),
}
# D40: the Claude Code versions whose subagent shapes (the Agent call's launch receipt and hand-back,
# the task-notification notices, the subagent's first prompt) were explicitly measured
# (spikes/m2/subagents/). Early side-room closing uses ``subagent_shapes_supported`` (semver
# compatibility within 2.1.x >= 2.1.302 gated by runtime structural health signals, rather than
# silently disabling on every weekly patch). 2.1.314+ put the tools the agent used before the
# hand-back and the idle notice (D56); the engine reads both forms.
SUBAGENT_SHAPES_TESTED: tuple[str, ...] = ("2.1.302", "2.1.310", "2.1.314", "2.1.318", "2.1.328")
CLI_NAMES = {"claude": "Claude Code", "codex": "Codex"}
AT_RISK = {
    "claude": "it may misread the session start and reminders and page text it should carry",
    "codex": "it may misread Codex's session context and page it as a turn",
}
DIALECT_CLI = {"anthropic": "claude", "responses": "codex"}
REPORT_URL = "https://github.com/VentureForgre/contexto/issues"
NO_CHECK_ENV = "CONTEXTO_NO_VERSION_CHECK"
VERSION_TIMEOUT_S = 3.0
MAX_VERSION_OUTPUT = 4096  # bytes of --version output read at most; the real ones are under 30
MAX_SEEN = 8  # distinct client versions logged per engine; a bound, not an expected number
# Requests with a tool list but no injected context that the session-start check looks at before it
# gives up (a client other than the CLIs sends none); bounds the scan for such clients.
MAX_UNJUDGED = 8

_VERSION = re.compile(r"(?<![\w.])(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)(?![\w.])")
_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")
_USER_AGENT = re.compile(
    r"\s*([A-Za-z][A-Za-z0-9_.-]*)/(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)(?![\w.])"
)


def parse_version(text: str) -> str | None:
    """The first ``X.Y.Z`` version in a ``--version`` output (``2.1.302 (Claude Code)``,
    ``codex-cli 0.163.0``), or None."""
    match = _VERSION.search(text or "")
    return match.group(1) if match else None


def parse_semver(version: str) -> tuple[int, int, int, bool, str] | None:
    """Parse ``X.Y.Z[-pre]`` into ``(major, minor, patch, is_release, pre)`` so pre-releases sort
    before their matching release; None if ``version`` is not a valid semver string."""
    match = _SEMVER.fullmatch(version or "")
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return int(major), int(minor), int(patch), pre is None, pre or ""


def supported_range(cli: str) -> str:
    """Human-readable supported semver range for ``cli`` (e.g. ``>=2.1.302, <2.2.0``), or ``none``."""
    bounds = SUPPORTED_RANGES.get(cli)
    if not bounds:
        return "none"
    low, high = bounds
    return f">={low[0]}.{low[1]}.{low[2]}, <{high[0]}.{high[1]}.{high[2]}"


def is_compatible(cli: str, version: str) -> bool:
    """Whether ``version`` is within ``cli``'s supported semver range (or explicitly listed in
    ``TESTED_VERSIONS``). Pre-releases of the minimum version are excluded unless explicitly listed."""
    if version in TESTED_VERSIONS.get(cli, ()):
        return True
    bounds = SUPPORTED_RANGES.get(cli)
    parsed = parse_semver(version)
    if bounds is None or parsed is None:
        return False
    low, high = bounds
    major, minor, patch, is_release, _ = parsed
    triple = (major, minor, patch)
    if triple < low or triple >= high:
        return False
    return is_release or triple > low


def is_tested(cli: str, version: str) -> bool:
    """Whether ``version`` is supported for ``cli`` (semver compatibility range within the tested
    family, replacing exact-patch allowlisting)."""
    return is_compatible(cli, version)


def subagent_shapes_supported(version: str | None, *, session_start_ok: bool | None = None,
                              hint_mismatch: bool = False) -> bool:
    """Whether early side-room closing is enabled for Claude Code ``version``.

    Enabled for any Claude Code version in the supported semver range (``>=2.1.302, <2.2.0``):
    - Versions explicitly listed in ``SUBAGENT_SHAPES_TESTED`` stay enabled unless a ``hint_mismatch``
      is observed.
    - Newer patch releases in the compatible range stay enabled unless runtime structural health
      checks report drift (``session_start_ok is False`` or ``hint_mismatch``).
    Returns False when no Claude Code version has been seen or the version is outside the range."""
    if not version or not is_compatible("claude", version):
        return False
    if hint_mismatch:
        return False
    if version not in SUBAGENT_SHAPES_TESTED and session_start_ok is False:
        return False
    return True


def tested_list(cli: str) -> str:
    return ", ".join(TESTED_VERSIONS.get(cli, ())) or "none"


def untested_warning(cli: str, version: str) -> str:
    """The one-line launch warning for a version outside the supported range (without the ``contexto: `` prefix)."""
    name = CLI_NAMES.get(cli, cli)
    return (f"warning: {name} {version} is outside the version range Contexto supports "
            f"(supported: {supported_range(cli)}; tested: {tested_list(cli)}); "
            f"{AT_RISK.get(cli, 'it may misread what the CLI sends')}. Check for a newer Contexto or report it at "
            f"{REPORT_URL} (silence: contexto run --no-version-check {cli}, or {NO_CHECK_ENV}=1).")


def check_disabled(env: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if env is None else env).get(NO_CHECK_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no")


def start_failure(error: str | None) -> bool:
    """Whether ``installed_version``'s error means the CLI could not be started at all: the name of an
    ``OSError`` from starting it (FileNotFoundError for a missing #! interpreter, PermissionError,
    OSError for an exec format error), not a timeout, an exit code, or unparsed output."""
    cls = getattr(builtins, error or "", None)
    return isinstance(cls, type) and issubclass(cls, OSError) and not issubclass(cls, TimeoutError)


def installed_version(binary: str, *, timeout: float = VERSION_TIMEOUT_S,
                      env: Mapping[str, str] | None = None,
                      spawn_guard: Callable[[], AbstractContextManager[Any]] | None = None
                      ) -> tuple[str | None, str | None]:
    """``(version, None)`` from ``<binary> --version``, or ``(None, reason)`` when it cannot be read:
    ``not_found``, ``timeout``, ``exit_<code>``, ``unparsed``, or the exception's type name.

    Bounded whatever the command does: it runs in its own process group (so a Ctrl+C at the terminal
    does not reach it), stdin closed, stdout and stderr on one pipe. At most ``MAX_VERSION_OUTPUT``
    bytes are read, until a complete line holding a version, end of file, or ``timeout``; a command
    that printed a version and exited is not waited on for a child that keeps the pipe open. The
    process group is killed before returning. ``KeyboardInterrupt`` kills it too and propagates.

    ``spawn_guard`` wraps the start of the command (``contexto doctor`` holds SIGINT, SIGTERM and SIGHUP
    there): a signal that lands inside ``subprocess.Popen`` while it is still returning leaves a process
    the caller never got and cannot kill. A held signal is raised as the guard exits, so the command is
    killed on that path too."""
    path = shutil.which(binary) if os.sep not in binary else binary
    if path is None:
        return None, "not_found"
    started = time.monotonic()
    child: subprocess.Popen | None = None
    try:
        with spawn_guard() if spawn_guard is not None else nullcontext():
            child = subprocess.Popen([path, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=None if env is None else dict(env),
                                     **new_process_group_kwargs())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, type(exc).__name__
    except BaseException:  # a signal the guard held, raised as it exits: the command must not outlive it
        if child is not None:
            _kill_group(child)
        raise
    try:
        output, timed_out = _read_version_output(child, started + timeout)
        code = child.poll()
        while code is None and not timed_out and time.monotonic() < started + timeout:
            time.sleep(0.01)  # it printed and closed or finished its output: give it the deadline to exit
            code = child.poll()
    finally:
        _kill_group(child)
    version = parse_version(output.decode("utf-8", "replace"))
    if code not in (0, None):
        return None, f"exit_{code}"
    if version:
        return version, None  # code None: it printed a version and kept running; the version is enough
    return (None, "timeout") if timed_out else (None, "unparsed")


def _read_version_output(child: subprocess.Popen, deadline: float) -> tuple[bytes, bool]:
    """Read ``child``'s output until a complete line with a version, end of file, ``MAX_VERSION_OUTPUT``
    bytes, or ``deadline`` (a ``time.monotonic`` value); returns ``(output, timed_out)``."""
    assert child.stdout is not None
    fd = child.stdout.fileno()

    def _has_version_line(buf: bytes) -> bool:
        *complete, _ = buf.split(b"\n")
        return any(parse_version(line.decode("utf-8", "replace")) for line in complete)

    if IS_WINDOWS:
        return read_fd_until(fd, max_bytes=MAX_VERSION_OUTPUT, deadline=deadline,
                             stop=_has_version_line, read_fn=os.read)
    output = b""
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while len(output) < MAX_VERSION_OUTPUT:
            left = deadline - time.monotonic()
            if left <= 0:
                return output, True
            if not selector.select(left):
                continue
            chunk = os.read(fd, MAX_VERSION_OUTPUT - len(output))
            if not chunk:
                return output, False  # end of file
            output += chunk
            if _has_version_line(output):
                return output, False  # a complete version line: no need to wait for end of file
    return output, False


def _kill_group(child: subprocess.Popen) -> None:
    """Kill the command's process group (and so anything it left holding the pipe), then reap it."""
    kill_process_group(child, timeout=1.0)
    if child.stdout is not None:
        child.stdout.close()


def launch_check(cli: str, binary: str, *, enabled: bool = True, env: Mapping[str, str] | None = None,
                 timeout: float = VERSION_TIMEOUT_S,
                 installed: tuple[str | None, str | None] | None = None) -> tuple[dict[str, Any], str | None]:
    """The launcher's check: ``(cli_version_check event, warning line or None)``. Never raises.
    ``installed`` is an ``installed_version`` result the caller already has, so ``--version`` is not run
    again (D59: a hanging one cost a Codex launch its timeout twice)."""
    if not enabled or check_disabled(env):
        return {"event": "cli_version_check", "cli": cli, "skipped": True}, None
    try:
        version, error = installed if installed is not None else installed_version(binary, timeout=timeout, env=env)
    except Exception as exc:  # a diagnostic; never stop a launch for it
        version, error = None, type(exc).__name__
    event: dict[str, Any] = {"event": "cli_version_check", "cli": cli, "version": version,
                             "tested": is_tested(cli, version) if version else None,
                             "tested_versions": tested_list(cli)}
    if error:
        event["error"] = error
    warning = untested_warning(cli, version) if version and not is_tested(cli, version) else None
    return event, warning


def client_version(headers: Mapping[str, str]) -> tuple[str, str, str] | None:
    """``(cli, client, version)`` from the request's ``User-Agent`` when it names Claude Code
    (``claude-cli/<v>``) or Codex (``codex_<surface>/<v>``; ``codex_exec`` is the one measured, other
    surfaces are matched by the prefix); None for any other client."""
    agent = next((v for k, v in headers.items() if k.lower() == "user-agent"), None)
    match = _USER_AGENT.match(agent) if isinstance(agent, str) else None
    if not match:
        return None
    client, version = match.group(1), match.group(2)
    if client == "claude-cli":
        return "claude", client, version
    if client.startswith("codex_") or client == "codex":
        return "codex", client, version
    return None


class ClientVersions:
    """Runtime checks for the rewrite engine. ``observe`` returns content-free events for the request's
    log entry: ``cli_version`` once per distinct client version, ``cli_version_untested`` once per
    engine, and ``session_start_unrecognised`` at most once per engine.

    The session start is judged once, on the first request that the dialect judges
    (``Dialect.judges_session_start``; for Claude Code, one with a tool list, or with the hint headers
    on, one whose request class is ``main`` or ``compaction``, D70) and that carries the CLI's
    injected context (``Dialect.session_start_seen`` returns True or False, not None). That is the main
    conversation's first request: Claude Code's title, topic, and quota side requests have no tool
    list, and a subagent's requests start from a tool call of a main turn, so after it. The engine's
    ``main`` (``RewriteEngine.publishes``) is not used: a tool-less side request that arrives first is
    main until the real conversation's first request (D50). Requests that cannot tell (None) leave
    the check open, for at most ``MAX_UNJUDGED`` of them."""

    def __init__(self) -> None:
        self.seen: dict[tuple[str, str], str] = {}  # (cli, version) -> client
        self.latest: dict[str, str] = {}  # cli -> latest version seen
        self.untested_logged = False
        self.session_start_judged = False
        self.session_start_ok: bool | None = None
        self.hint_mismatch_seen = False
        self.unjudged = 0  # judged requests that could not tell

    def note_hint_mismatch(self) -> None:
        """Record that a gateway hint header disagreed with a structural signature."""
        self.hint_mismatch_seen = True

    def observe(self, request: dict, headers: Mapping[str, str], dialect: Any, *,
                judged: bool | None = None) -> list[dict]:
        """``judged`` is the engine's answer to "is this a request the dialect judges?" when it has one
        from Claude Code's request class (D70, ``RewriteEngine._classify``); None asks the dialect."""
        events: list[dict] = []
        found = client_version(headers)
        if found:
            cli, client, version = found
            self.latest[cli] = version
            if (cli, version) not in self.seen and len(self.seen) < MAX_SEEN:
                self.seen[(cli, version)] = client
                tested = is_tested(cli, version)
                events.append({"event": "cli_version", "cli": cli, "client": client, "version": version,
                               "tested": tested, "tested_versions": tested_list(cli)})
                if not tested and not self.untested_logged:
                    self.untested_logged = True
                    events.append({"event": "cli_version_untested", "cli": cli, "version": version,
                                   "tested_versions": tested_list(cli)})
        if judged is None:
            judged = dialect.judges_session_start(request)
        if not self.session_start_judged and judged:
            seen = dialect.session_start_seen(request.get(dialect.field) or [])
            if seen is None:
                self.unjudged += 1
                self.session_start_judged = self.unjudged >= MAX_UNJUDGED  # not a CLI's requests: give up
            else:
                self.session_start_judged = True
                self.session_start_ok = seen
                if not seen:
                    cli = DIALECT_CLI.get(dialect.name, dialect.name)
                    events.append({"event": "session_start_unrecognised", "dialect": dialect.name,
                                   "signature_version": dialect.session_start_version,
                                   "cli_version": self.latest.get(cli), "tested_versions": tested_list(cli)})
        return events
