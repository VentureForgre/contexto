"""``contexto doctor [--json]``: check that ``contexto run`` can work from this directory (D64).

One line per check, marked ``[ok]``, ``[warn]``, ``[FAIL]`` or ``[skip]``, printed when that check is
done; a warning or a failure is followed by one line saying what to do. The checks, in order:

- ``python``: this interpreter is 3.12 or later.
- ``claude``, ``codex``: the CLI is on PATH, its ``--version`` (read as ``contexto run`` reads it,
  ``contexto.cliversions.installed_version``), and whether that version is in ``TESTED_VERSIONS``.
- ``settings``: the Claude Code settings files ``contexto run`` reads from this directory, and Claude
  Code's managed settings file, each read by ``contexto.upstream.read_settings_file`` as the launcher
  reads it. One that is there and cannot be used (``SettingsFile.problem``: not a regular file, too
  large, not valid JSON, an ``env`` that is not an object) is named by its scope with what is wrong:
  nothing is taken from it, and it can make the ``provider`` check fail.
- ``claude-upstream``, ``codex-upstream``: the upstream ``contexto run`` would use from this directory
  and what set it (``contexto.upstream.resolve_upstream``; for Codex
  ``contexto.codex.codex_upstream_with_source`` in API-key mode, its default). Only the URL's origin is
  shown: scheme, host and port, never user info, a path or a query, any of which can hold a credential.
  The host is shown in ASCII, as it goes on the wire.
- ``claude-key``, ``codex-key``: whether a key or key helper is configured, by name and place. For
  Claude Code, which sends its own credential through the proxy, that is ``ANTHROPIC_API_KEY``,
  ``ANTHROPIC_AUTH_TOKEN`` or ``CLAUDE_CODE_OAUTH_TOKEN`` (environment or a settings ``env`` block) or
  an ``apiKeyHelper``; a login Claude Code holds is not looked for, so none found is a warning. For
  Codex it is the helper ``contexto run codex`` would hand it: ``CONTEXTO_CODEX_KEY_HELPER``, else
  Claude Code's ``apiKeyHelper``, a project's only in a folder Claude Code trusts (D52).
- ``store``: the store home is one ``contexto run`` can use: it can be written or created, sessions can
  be made in it, and ``contexto run claude`` does not refuse its path (``check_store``).
- ``mcp``: the MCP server starts as the launcher starts it and lists Contexto's tools.
- ``provider``: ``contexto run claude`` would not refuse to launch because Claude Code may send its
  model requests to a cloud provider, past the proxy (D70): the launcher's own check, with the same
  arguments (``contexto.launcher.check_cloud_provider``, ``contexto.upstream.cloud_provider``).

What doctor never does:

- It makes no network request. It reports the upstream and does not contact it. The only processes
  it starts are ``claude --version``, ``codex --version`` and Contexto's own MCP server on stdio.
- It never runs a key helper. A helper is the user's command, with whatever side effects it has, and
  Codex's rule for a project's helper (D52, D54) exists so that one from an untrusted folder is not
  run unasked. It says where a helper is configured and that it was not run.
- It never prints a key, a part or the length of one, a helper's command (which can hold a key), or
  a helper's output.
- It leaves the store home as it found it: an existing home is probed with a file that has no name
  (``tempfile.TemporaryFile``), a missing one is not created, and no mode is changed. The MCP check
  uses a temporary home, removed afterwards, so the user's store gets no session from it.

Exit code 0 when no check failed, warnings or not; 1 when one did; 130 on Ctrl+C, and 128 plus the
signal's number on SIGTERM or SIGHUP, after the same cleanup. These fail: Python below 3.12, neither
CLI usable, an upstream that is not an http(s) URL (``contexto run`` refuses to start with it), a store
home ``contexto run`` cannot use, an MCP server that does not start and list exactly Contexto's tools,
a cloud-provider switch that is on or a settings file that may turn one on (``contexto run claude``
refuses to launch, exit 2), and a check that cannot be made at all (no working directory, no home
directory, no temporary directory: ``Fault``). Everything else is a warning: an untested or unreadable
CLI version, one of the two CLIs missing, a settings file that cannot be used, no key found (a login
the CLI holds also works), no upstream or key helper for Codex (a flag to ``contexto run`` can supply
either), a setting skipped as untrusted, and a store home that is read-only while its ``sessions``
directory can be used. A check that does not apply because its CLI is missing is skipped.

The text form escapes every character that does not print (``printable``): a host or a path can come
from a project's files, and must not be able to rewrite the terminal's lines.

``--json`` prints ``{"schema": 1, "contexto": <version>, "ok": <bool>, "summary": {"pass", "warn",
"fail", "skip"}, "checks": [{"id", "status", "summary", "action", "details"}]}`` once every check is
done, and nothing when the run is stopped first; docs/doctor.md lists each check's ``details``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contexto import __version__
from contexto.cliversions import CLI_NAMES, REPORT_URL, TESTED_VERSIONS, installed_version, is_tested, tested_list
from contexto.cliversions import _kill_group as kill_group
from contexto.cliversions import start_failure as cannot_start
from contexto.launcher import (
    CLI_INSTALL,
    LaunchError,
    Terminated,
    check_cloud_provider,
    check_mcp_values,
    check_upstream,
    cli_not_found,
    mcp_payload,
)
from contexto.mcp import LEGACY_VERSIONS as MCP_PROTOCOL_VERSIONS
from contexto.mcp import SERVER_NAME, TOOLS
from contexto.paths import contexto_home
from contexto.store import SessionStore
from contexto.upstream import (
    BASE_URL_KEY,
    CLOUD_PROVIDER_KEYS,
    CONFIG_DIR_KEY,
    KEY_HELPER_KEY,
    KIND_FILES,
    MANAGED_KIND,
    SettingsFiles,
    claude_env_value,
    claude_key_helper,
    claude_settings_order,
    cloud_provider,
    cloud_provider_note,
    managed_settings_path,
    resolve_upstream,
    untrusted_note,
)

SCHEMA = 1
PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
MARKS = {PASS: "[ok]", WARN: "[warn]", FAIL: "[FAIL]", SKIP: "[skip]"}
MARK_WIDTH = 7
EXIT_OK, EXIT_FAILED, EXIT_INTERRUPTED = 0, 1, 130
MINIMUM_PYTHON = (3, 12)
# How Claude Code can be given a credential without a login: read by name only, never shown.
CLAUDE_KEY_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
MCP_TIMEOUT_S = 10.0  # the whole exchange; the real server answers in a fraction of a second
MCP_SESSION = "doctor"
MCP_ERROR_CHARS = 200  # of the server's last stderr line, shown when it fails


@dataclass
class Check:
    id: str
    status: str
    summary: str  # one line
    action: str | None = None  # what to do next, one line; set for a warning or a failure
    details: dict[str, Any] = field(default_factory=dict)


class Fault(Exception):
    """Something a check cannot work without and the user can put right (no working directory, no home
    directory, nowhere to make a temporary directory), as opposed to a bug in the check. ``guarded``
    reports it with its own summary and next step; any other exception is reported by its type, with
    the issue tracker as the next step."""

    def __init__(self, error: str, summary: str, action: str) -> None:
        super().__init__(summary)
        self.error, self.summary, self.action = error, summary, action


class Context:
    """What the checks read: the environment, the working directory and the home directory of the
    ``contexto run`` being diagnosed. The two directories are looked up when a check first asks, inside
    that check's guard: a working directory that was deleted, or a user with no home directory, is then
    a failed line that names the fault, not a traceback. ``files`` reads each settings file once for
    every check, as one launch does (``contexto.upstream.SettingsFiles``)."""

    def __init__(self, env: Mapping[str, str], cwd: Path | None = None, home: Path | None = None) -> None:
        self.env, self._cwd, self._home = env, cwd, home
        self.files = SettingsFiles()

    @property
    def cwd(self) -> Path:
        if self._cwd is None:
            try:
                self._cwd = Path.cwd()
            except OSError as exc:
                raise Fault("no_working_directory", "the working directory cannot be read (it may have been deleted)",
                            "cd to your project directory and run contexto doctor again") from exc
        return self._cwd

    @property
    def home(self) -> Path:
        if self._home is None:
            try:
                self._home = Path.home()
            except RuntimeError as exc:
                raise Fault("no_home_directory", "the home directory cannot be found (HOME is not set and this "
                            "user has no entry in the password database)",
                            "Set HOME to your home directory and run contexto doctor again") from exc
        return self._home


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "doctor", help="check that Contexto can run here: the CLIs, the upstream, keys, the store, the MCP server",
        description="Check what `contexto run` needs from this directory: Python, the claude and codex CLIs and "
                    "their versions, the Claude Code settings files, the upstream and where it comes from, "
                    "whether a key or key helper is configured, the store home, the MCP server, and variables "
                    "that would make Claude Code bypass the proxy. Makes no network request, runs no key "
                    "helper, and prints no key.",
        epilog="Exit codes: 0 no check failed (warnings allowed); 1 a check failed; 130 Ctrl+C; 143 or 129 "
               "SIGTERM or SIGHUP. Details: docs/doctor.md.")
    parser.add_argument("--json", action="store_true", help="print the results as JSON (shape in docs/doctor.md)")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Text: each check's line is printed when the check is done, so a slow one (a ``--version`` that
    hangs for its 3 s, an MCP server for its 10 s) is not 16 s of silence. JSON: one object, after the
    last check, and none when the run is stopped."""
    checks: list[Check] = []
    try:
        with termination_raises():
            if not args.json:
                print(f"{header()}\n", flush=True)
            for check in iter_checks():
                checks.append(check)
                if not args.json:
                    print(render_check(check), flush=True)
            print(json.dumps(report(checks), indent=2) if args.json else f"\n{verdict(checks)}", flush=True)
    except BrokenPipeError:
        # The reader left (`contexto doctor | head -1`). Nothing more can be printed, and Python's flush
        # of stdout at exit would raise again, so stdout is pointed away first. Not a pass: the run did
        # not finish.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):  # no file descriptor behind stdout: nothing left to flush
            pass
        return EXIT_FAILED
    except KeyboardInterrupt:  # each check stops what it started on the way out
        print("contexto: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Terminated as stop:  # SIGTERM or SIGHUP: the same cleanup, and the shell's code for the signal
        return 128 + stop.signum
    return EXIT_FAILED if any(c.status == FAIL for c in checks) else EXIT_OK


@contextmanager
def termination_raises() -> Iterator[None]:
    """While the checks run, SIGTERM and SIGHUP (a closed terminal) raise ``launcher.Terminated``, so a
    check's ``finally`` kills the process it started and removes its temporary home, as Ctrl+C already
    did; before, the ``contexto-doctor-*`` directory stayed and a hung MCP server was orphaned. After the
    first signal both are dropped until the handlers are put back, so a second one cannot cut the cleanup
    short. They are dropped by a handler that does nothing, not by SIG_IGN: a second signal that arrived
    before the switch is still delivered to Python, which reports "ignored due to race condition" with a
    traceback when it finds SIG_IGN. A signal that is ignored already (``nohup``) stays ignored. What the
    standard library cannot cover: SIGKILL, and a caller that is not in the main thread, where no
    handler can be set."""
    def dropped(signum: int, frame: Any) -> None:
        return None

    def stop(signum: int, frame: Any) -> None:
        for other in previous:
            signal.signal(other, dropped)
        raise Terminated(signum)

    previous: dict[int, Any] = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            if signal.getsignal(signum) is not signal.SIG_IGN:
                previous[signum] = signal.signal(signum, stop)
    except ValueError:  # not the main thread
        pass
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def signals_held() -> Iterator[None]:
    """Hold SIGINT, SIGTERM and SIGHUP for the few statements that make something a ``finally`` has to
    remove, and act on a held one right after. A signal raises where it lands: inside
    ``subprocess.Popen`` while it is still returning, the caller never gets the process and cannot kill
    it (seen with SIGHUP on a busy machine: the temporary home was removed and the server lived on).
    Held by a handler that only records, not by a signal mask, which the new process would inherit."""
    held: list[int] = []
    previous: dict[int, Any] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            if signal.getsignal(signum) is not None:  # None: set from C, and cannot be put back from here
                previous[signum] = signal.signal(signum, lambda number, frame: held.append(number))
    except ValueError:  # not the main thread, where no signal handler runs anyway
        pass
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if held:
            signal.raise_signal(held[0])  # now it meets the handler it was sent to


def read_version(path: str, env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """``installed_version`` with SIGINT, SIGTERM and SIGHUP held while the command starts, as for the
    MCP server: one that landed inside ``subprocess.Popen`` left a hanging ``--version`` running (PR
    #258's re-review: 27 of 150 timed runs)."""
    return installed_version(path, env=env, spawn_guard=signals_held)


def run_checks(context: Context | None = None) -> list[Check]:
    """Every check, in the documented order (``iter_checks``)."""
    return list(iter_checks(context))


def iter_checks(context: Context | None = None) -> Iterator[Check]:
    """Each check as it finishes, in the documented order. A check that raises is reported as failed
    (``guarded``): doctor must not die of the fault it is there to find. A CLI's line is held back only
    while it could still change: with neither CLI usable both fail, so an unusable ``claude`` waits for
    ``codex``."""
    ctx = context or Context(os.environ)
    yield guarded("python", "Python", check_python)
    clis: list[Check] = []
    usable: dict[str, bool] = {}
    on_path: dict[str, bool] = {}
    shown = 0
    for cli in ("claude", "codex"):
        check = guarded(cli, CLI_NAMES[cli], lambda cli=cli: check_cli(cli, ctx.env))
        usable[cli] = bool(check.details.pop("usable", False))
        if cli == "codex" and usable[cli] and not codex_support():
            check, usable[cli] = without_codex_support(check), False
        on_path[cli] = check.details.get("path") is not None
        clis.append(check)
        if any(usable.values()):  # one CLI is enough, so no line changes from here on
            yield from clis[shown:]
            shown = len(clis)
    if not any(usable.values()):
        require_one_cli(clis)
    yield from clis[shown:]
    yield guarded("settings", "Claude Code settings", lambda: check_settings(ctx))
    per_cli: list[tuple[str, str, str, Callable[[], Check]]] = [
        ("claude-upstream", "claude", "Claude Code upstream", lambda: check_claude_upstream(ctx)),
        ("codex-upstream", "codex", "Codex upstream", lambda: check_codex_upstream(ctx)),
        ("claude-key", "claude", "Claude Code key", lambda: check_claude_key(ctx)),
        ("codex-key", "codex", "Codex key helper", lambda: check_codex_key(ctx)),
    ]
    for check_id, cli, label, check in per_cli:
        if usable[cli]:
            yield guarded(check_id, label, check)
        elif cli == "codex" and on_path[cli] and not codex_support():
            yield skipped(check_id, label, CODEX_SUPPORT_MISSING)
        else:
            yield skipped(check_id, label, f"{cli} is not on PATH or cannot be run")
    yield guarded("store", "Store home", lambda: check_store(ctx.env, claude=usable["claude"]))
    yield guarded("mcp", "MCP server", lambda: check_mcp(ctx.env, ctx.cwd))
    # The launcher stops before this check only when `shutil.which` finds no `claude` (missing, not
    # executable, a broken symlink or a directory); one that is there but cannot start still meets the
    # refusal (reviewer A, PR #258 round 5), so only the first kind skips it, in words that agree with the
    # `claude` line above (PR #258 round 6).
    yield (guarded("provider", "Cloud provider", lambda: check_provider(ctx)) if on_path["claude"]
           else skipped("provider", "Cloud provider", "claude is not on PATH or not executable"))


def guarded(check_id: str, label: str, check: Callable[[], Check]) -> Check:
    """``check()``, or a failed line when it raises: a ``Fault`` by what it says, anything else by the
    error's type only (its text could hold a URL or a path) and as a bug to report."""
    try:
        return check()
    except Fault as fault:
        return Check(check_id, FAIL, f"{label}: {fault.summary}", fault.action, {"error": fault.error})
    except Exception as exc:
        return Check(check_id, FAIL, f"{label}: the check itself failed ({type(exc).__name__})",
                     f"Report it at {REPORT_URL}", {"error": type(exc).__name__})


def skipped(check_id: str, label: str, why: str) -> Check:
    return Check(check_id, SKIP, f"{label}: not checked, {why}")


# What `contexto run codex` says, and exits 2 on, when contexto.codex cannot be imported (commands/run.py).
CODEX_SUPPORT_MISSING = "codex support is not installed in this build"


def codex_support() -> bool:
    """Whether ``contexto.codex`` imports. Without it `contexto run codex` exits 2, so doctor says so in
    those words rather than failing its Codex checks as a bug to report (PR #283's review)."""
    try:
        import contexto.codex  # noqa: F401
    except ImportError:
        return False
    return True


def without_codex_support(check: Check) -> Check:
    """The ``codex`` line for a CLI that starts while Contexto's Codex support is missing: a warning, as a
    missing CLI is, which ``require_one_cli`` makes a failure when ``claude`` is unusable too."""
    return Check("codex", WARN, f"{check.summary}, but {CODEX_SUPPORT_MISSING}",
                 "Reinstall Contexto to use `contexto run codex`; until then it exits 2",
                 {**check.details, "error": "codex_support_missing"})


def report(checks: list[Check]) -> dict[str, Any]:
    """The ``--json`` document (the module docstring gives its shape)."""
    return {"schema": SCHEMA, "contexto": __version__, "ok": not any(c.status == FAIL for c in checks),
            "summary": {status: sum(1 for c in checks if c.status == status) for status in (PASS, WARN, FAIL, SKIP)},
            "checks": [{"id": c.id, "status": c.status, "summary": c.summary, "action": c.action,
                        "details": c.details} for c in checks]}


def header() -> str:
    return f"contexto doctor (contexto {__version__})"


def printable(text: str) -> str:
    """``text`` with every character that does not print written as a backslash escape: control
    characters (a backspace or an escape sequence in a host name from a project's settings file would
    otherwise rewrite the line on the terminal), the bidirectional overrides and other format
    characters, and the surrogates of a path that is not UTF-8. ``--json`` escapes them by itself."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text)


def render_check(check: Check) -> str:
    """One check as the text form prints it: its line, and the next step under it when there is one."""
    line = f"{MARKS[check.status]:<{MARK_WIDTH}}{printable(check.summary)}"
    return line if not check.action else f"{line}\n{'':<{MARK_WIDTH}}-> {printable(check.action)}"


def render(checks: list[Check]) -> str:
    """The whole text form, as ``run`` prints it piece by piece."""
    return "\n".join([header(), "", *(render_check(check) for check in checks), "", verdict(checks)])


def verdict(checks: list[Check]) -> str:
    count = {status: sum(1 for c in checks if c.status == status) for status in (PASS, WARN, FAIL, SKIP)}
    parts = [f"{count[FAIL]} failed"] if count[FAIL] else []
    parts += [f"{count[PASS]} passed"]
    parts += [f"{count[WARN]} warning{'' if count[WARN] == 1 else 's'}"] if count[WARN] else []
    parts += [f"{count[SKIP]} skipped"] if count[SKIP] else []
    return f"{'Not ready' if count[FAIL] else 'Ready'}: {', '.join(parts)}."


# Python and the CLIs ----------------------------------------------------------------------------------


def check_python(version: tuple[int, ...] | None = None) -> Check:
    version = tuple(sys.version_info[:3]) if version is None else version
    shown = ".".join(str(part) for part in version)
    needed = ".".join(str(part) for part in MINIMUM_PYTHON)
    details = {"version": shown, "executable": sys.executable}
    if version[:2] >= MINIMUM_PYTHON:
        return Check("python", PASS, f"Python {shown} ({needed} or later)", details=details)
    return Check("python", FAIL, f"Python {shown} is older than {needed}",
                 f"Install Contexto under Python {needed} or later", details)


def check_cli(cli: str, env: Mapping[str, str]) -> Check:
    """One CLI: found on PATH, its version, and whether Contexto was tested with it. ``details["usable"]``
    (removed by ``iter_checks``) says whether it can be started at all; a missing or unstartable CLI is a
    warning here, and ``require_one_cli`` makes it a failure when the other one is unusable too."""
    name = CLI_NAMES[cli]
    details: dict[str, Any] = {"cli": cli, "path": None, "version": None, "tested": None,
                               "tested_versions": list(TESTED_VERSIONS[cli]), "usable": False}
    install = f"Install {name} and put it on PATH to use `contexto run {cli}`"
    path = shutil.which(cli, path=env.get("PATH"))
    if path is None:
        details["error"] = "not_found"
        why = cli_not_found(cli, env.get("PATH"))
        # Since #257 a CLI not on PATH at all gets its install command in the line itself; the next step
        # then only says what needs it, so the install step is given once (PR #258 review, both).
        action = f"Only `contexto run {cli}` needs it" if CLI_INSTALL[cli][1] in why else install
        return Check(cli, WARN, f"{name}: {why}", action, details)
    details["path"] = path
    version, error = read_version(path, env)
    if cannot_start(error):  # on PATH and executable, but it does not start: a missing #! interpreter, say
        details["error"] = error
        return Check(cli, WARN, f"{name}: '{cli}' at {path} cannot be run ({error})",
                     f"Check its #! line or reinstall {name}", details)
    details["usable"] = True
    if version is None:
        details["error"] = error
        return Check(cli, WARN, f"{name} at {path}: its version could not be read ({error})",
                     f"Run `{cli} --version` yourself: Contexto cannot tell whether this version was tested", details)
    details.update(version=version, tested=is_tested(cli, version))
    if details["tested"]:
        return Check(cli, PASS, f"{name} {version} at {path} (a tested version)", details=details)
    return Check(cli, WARN, f"{name} {version} at {path} is not a version Contexto was tested with "
                 f"(tested: {tested_list(cli)})", f"Check for a newer Contexto, or report it at {REPORT_URL}", details)


def require_one_cli(clis: list[Check]) -> None:
    """Contexto needs one of the two CLIs: called with neither usable, it makes both checks fail."""
    codex_missing = any(check.details.get("error") == "codex_support_missing" for check in clis)
    for check in clis:
        if check.status == WARN:
            check.status = FAIL
            if check.details.get("error") == "codex_support_missing":  # installed: its own step stands
                continue
            check.action = ("Install Claude Code and put it on PATH: Contexto needs at least one of the two CLIs, "
                            "and this build has no Codex support" if codex_missing else
                            "Install Claude Code or Codex and put it on PATH: Contexto needs at least one of them")


# Settings files ---------------------------------------------------------------------------------------


def settings_file(kind: str, env: Mapping[str, str] | None = None) -> str:
    """A settings file by its name, for a person, never a path: the user's file is
    ``~/.claude/settings.json``, or ``$CLAUDE_CONFIG_DIR/settings.json`` when ``env`` sets that
    variable (``contexto.upstream.user_settings_path``)."""
    if kind == MANAGED_KIND:
        return "Claude Code's managed settings file (managed-settings.json)"
    if kind != "user":
        return KIND_FILES.get(kind, kind)
    moved = ((env or {}).get(CONFIG_DIR_KEY) or "").strip()
    return f"${CONFIG_DIR_KEY}/settings.json" if moved else "~/.claude/settings.json"


def source_text(source: str, env: Mapping[str, str] | None = None) -> str:
    """Where a variable is set, for a person: the environment, or the ``env`` block of a settings file
    (``settings_file``)."""
    if source == "env":
        return "the environment"
    return f"the env block of {settings_file(source.removeprefix('settings:'), env)}"


def check_settings(ctx: Context) -> Check:
    """Each settings file ``contexto run claude`` reads from this directory: those of
    ``claude_settings_order``, then the managed settings file, which the cloud-provider check also reads
    (D70). Each is read, absent, or there and unusable, as ``contexto.upstream.read_settings_file``
    finds it, through the same ``SettingsFiles`` as the checks below. An unusable one is a warning that
    names it by scope and says what is wrong (``SettingsFile.problem``): nothing is taken from it, and
    one that may turn on a cloud provider makes the ``provider`` check fail. A file that does not exist,
    a directory, and one this user cannot open are absent: Claude Code cannot read them either."""
    files: list[dict[str, str]] = []
    wrong: list[str] = []
    may_switch = False
    read: list[str] = []
    paths = list(claude_settings_order(ctx.cwd, ctx.home, ctx.env))
    managed = managed_settings_path()
    if managed is not None:
        paths.append((MANAGED_KIND, managed))
    for kind, path in paths:
        name = settings_file(kind, ctx.env)
        found = ctx.files.read(path)
        if found.problem:
            files.append({"kind": kind, "state": "unusable", "problem": found.problem})
            wrong.append(f"{name} {found.problem}")
            may_switch = may_switch or found.may_name_provider
        elif found.data is not None:
            files.append({"kind": kind, "state": "read"})
            read.append(name)
        else:
            files.append({"kind": kind, "state": "absent"})
    details = {"files": files}
    if wrong:
        step = "Fix or remove it: Contexto takes nothing from it (an upstream, a key helper)"
        if may_switch:
            step += (", and `contexto run claude` refuses to launch while it may turn on a cloud provider "
                     "(see the provider check)")
        return Check("settings", WARN, f"Claude Code settings: {'; '.join(wrong)}", step, details)
    if read:
        return Check("settings", PASS, f"Claude Code settings: read {' and '.join(read)}", details=details)
    return Check("settings", PASS, "Claude Code settings: no settings file here or for this user", details=details)


# Upstreams --------------------------------------------------------------------------------------------


def host_text(host: str) -> str:
    """A host as it goes on the wire: its IDNA form, so a name with a look-alike letter from another
    alphabet shows as ``xn--...`` and not as the name it imitates. A host the codec refuses (an
    over-long label, a character no name may hold) is shown with what is not ASCII escaped."""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host.encode("ascii", "backslashreplace").decode("ascii")


def url_origin(url: str) -> str | None:
    """``scheme://host[:port]`` of an http(s) URL, or None when it is not one. User info, the path and the
    query are left out: each can hold a credential. The host is in ASCII (``host_text``)."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    host = host_text(host)
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}" + ("" if port is None else f":{port}")


def base_url_place(source: str, env: Mapping[str, str]) -> str:
    """Where Claude Code's upstream comes from, by ``contexto.upstream.Upstream.source``."""
    if source == "default":
        return f"the default: nothing sets {BASE_URL_KEY}"
    return f"{BASE_URL_KEY} in {source_text(source, env)}"


def check_claude_upstream(ctx: Context) -> Check:
    """The upstream ``contexto run claude`` would use, resolved as ``launcher._run`` resolves it."""
    upstream = resolve_upstream(None, env=ctx.env, cwd=ctx.cwd, home=ctx.home, files=ctx.files)
    place = base_url_place(upstream.source, ctx.env)
    details: dict[str, Any] = {"origin": url_origin(upstream.url), "source": upstream.source}
    if upstream.ignored:
        details["ignored"] = list(upstream.ignored)
    try:
        check_upstream(upstream)
    except LaunchError:  # its message quotes the URL, which is not repeated here
        return Check("claude-upstream", FAIL, f"Claude Code upstream from {place} is not an http(s) base URL: "
                     "`contexto run claude` refuses to start with it",
                     f"Fix {BASE_URL_KEY} there, or pass --upstream to contexto run", details)
    summary = f"Claude Code upstream: {details['origin']} (from {place})"
    if upstream.ignored:  # CLAUDE_CODE_IGNORE_PERSISTED_TRUST: a project file's base URL was passed over
        return Check("claude-upstream", WARN, summary,
                     untrusted_note(f"env.{BASE_URL_KEY}", upstream.ignored, "Pass --upstream", ctx.env), details)
    return Check("claude-upstream", PASS, summary, details=details)


def codex_upstream_place(source: str, claude_source: str, env: Mapping[str, str]) -> str:
    from contexto.codex import UPSTREAM_ENV  # here, not at import: see check_codex_upstream
    if source == "codex-env":
        return f"{UPSTREAM_ENV} in the environment"
    if source.startswith("claude-"):
        return f"Claude Code's upstream plus /v1; {base_url_place(claude_source, env)}"
    return source


def check_codex_upstream(ctx: Context) -> Check:
    """The upstream ``contexto run codex`` would use in API-key mode with no ``--codex-upstream``, resolved
    as ``codex.run_codex`` resolves it: a project's base URL only from a folder Claude Code trusts."""
    # Imported here: the CLI imports every command module to build its parser, so a top-level import made
    # every command fail when contexto.codex is missing, where `contexto run codex` must exit 2 and say so.
    from contexto.codex import API_KEY_MODE, UPSTREAM_ENV, CodexLaunchError, codex_upstream_with_source

    claude = resolve_upstream(None, env=ctx.env, cwd=ctx.cwd, home=ctx.home, trusted_only=True, files=ctx.files)
    untrusted = (untrusted_note(f"env.{BASE_URL_KEY}", claude.ignored, "Pass --codex-upstream", ctx.env)
                 if claude.ignored else None)
    details: dict[str, Any] = {"origin": None, "source": None}
    if claude.ignored:
        details["ignored"] = list(claude.ignored)
    try:
        target, source = codex_upstream_with_source(API_KEY_MODE, None, dict(ctx.env), claude.url, claude.source)
    except CodexLaunchError as exc:  # names a host at most, never the URL
        summary, _, remedy = str(exc).partition(". ")
        summary = summary.removeprefix("no upstream for Codex: ")
        return Check("codex-upstream", WARN, f"Codex upstream: none ({summary})",
                     untrusted or remedy or f"Pass --codex-upstream to contexto run, or set {UPSTREAM_ENV}", details)
    details.update(origin=url_origin(target), source=source)
    place = codex_upstream_place(source, claude.source, ctx.env)
    if details["origin"] is None:
        return Check("codex-upstream", FAIL, f"Codex upstream from {place} is not an http(s) URL: "
                     "`contexto run codex` refuses to start with it",
                     f"Fix it there, or pass --codex-upstream to contexto run (or set {UPSTREAM_ENV})", details)
    summary = f"Codex upstream: {details['origin']} (from {place})"
    if untrusted and source.startswith("claude-"):
        return Check("codex-upstream", WARN, summary, untrusted, details)
    return Check("codex-upstream", PASS, summary, details=details)


# Keys -------------------------------------------------------------------------------------------------


def check_claude_key(ctx: Context) -> Check:
    """Whether Claude Code is given a credential by a variable or a helper. Only that a value is there
    is read; the value is never kept, and a helper is not run."""
    found: list[dict[str, str]] = []
    shown: list[str] = []
    for name in CLAUDE_KEY_VARIABLES:
        value = claude_env_value(name, env=ctx.env, cwd=ctx.cwd, home=ctx.home, files=ctx.files)
        if value.value and value.value.strip() and value.source:
            found.append({"name": name, "source": value.source})
            shown.append(f"{name} in {source_text(value.source, ctx.env)}")
    helper = claude_key_helper(ctx.cwd, ctx.home, ctx.env, trusted_only=False)  # as Claude Code -p reads it
    if helper.value:
        found.append({"name": KEY_HELPER_KEY, "source": f"settings:{helper.source}"})
        shown.append(f"{KEY_HELPER_KEY} in {settings_file(helper.source or '', ctx.env)} (not run)")
    details = {"found": found}
    if found:
        return Check("claude-key", PASS, f"Claude Code key: {' and '.join(shown)}; no value is read or shown",
                     details=details)
    names = ", ".join(CLAUDE_KEY_VARIABLES)
    return Check("claude-key", WARN, f"Claude Code key: none of {names} or {KEY_HELPER_KEY} is set",
                 "Nothing to do if Claude Code holds a login (doctor does not read stored credentials); otherwise "
                 f"set ANTHROPIC_API_KEY or an {KEY_HELPER_KEY}", details)


def check_codex_key(ctx: Context) -> Check:
    """The key helper ``contexto run codex`` would hand Codex in API-key mode, in ``codex.plan_codex``'s
    order after ``--codex-key-helper``. Named by where it is configured; never run, and its command is
    not shown. The variable is read as the launcher reads it: any value that is not empty is the helper,
    and one that is only spaces is then refused ("the key helper command is empty"), where reading on
    to ``apiKeyHelper`` would report a helper the launch never uses."""
    from contexto.codex import KEY_HELPER_ENV  # here, not at import: see check_codex_upstream

    if ctx.env.get(KEY_HELPER_ENV):
        details = {"name": KEY_HELPER_ENV, "source": "env"}
        if ctx.env[KEY_HELPER_ENV].strip():
            return Check("codex-key", PASS, f"Codex key helper: {KEY_HELPER_ENV} in the environment (not run)",
                         details=details)
        return Check("codex-key", WARN, f"Codex key helper: {KEY_HELPER_ENV} is set and blank, and `contexto run "
                     "codex` refuses a blank helper", f"Unset {KEY_HELPER_ENV} or set it to a command, or pass "
                     "--codex-key-helper to contexto run", {**details, "error": "blank"})
    helper = claude_key_helper(ctx.cwd, ctx.home, ctx.env)  # a project's only from a trusted folder (D52)
    if helper.value:
        place = settings_file(helper.source or "", ctx.env)
        return Check("codex-key", PASS, f"Codex key helper: Claude Code's {KEY_HELPER_KEY} in {place} (not run)",
                     details={"name": KEY_HELPER_KEY, "source": f"settings:{helper.source}"})
    details: dict[str, Any] = {"name": None, "source": None}
    if helper.ignored:
        details["ignored"] = list(helper.ignored)
        action = untrusted_note(KEY_HELPER_KEY, helper.ignored, f"Pass --codex-key-helper (or set {KEY_HELPER_ENV})",
                                ctx.env)
    else:
        action = (f"Pass --codex-key-helper to contexto run, set {KEY_HELPER_ENV}, or set {KEY_HELPER_KEY} in Claude "
                  "Code settings; --codex-auth chatgpt uses Codex's own login instead")
    return Check("codex-key", WARN, "Codex key helper: none configured, and `contexto run codex` needs one in "
                 "API-key mode", action, details)


# The store home ---------------------------------------------------------------------------------------


def write_problem(directory: Path) -> str | None:
    """Why a file cannot be created in ``directory``, or None when one can. The probe is a file without
    a name (``tempfile.TemporaryFile``: unlinked at once, or never linked where the system allows),
    so nothing is left behind even if doctor is killed, and no mode is touched."""
    try:
        with tempfile.TemporaryFile(dir=directory):
            return None
    except OSError as exc:
        return exc.strerror or type(exc).__name__


def check_store(env: Mapping[str, str], *, claude: bool = True) -> Check:
    """The store home ``contexto run`` would use, judged as the launcher treats it, with nothing created
    and no mode changed:

    - A home that does not exist is judged by permission on its nearest existing parent: ``contexto run``
      makes it, 0700.
    - ``contexto run claude`` refuses a home whose path holds ``${`` (``launcher.check_mcp_values``, the
      launcher's own check, asked here when ``claude`` is usable; ``contexto run codex`` takes such a home).
    - Sessions are made in ``sessions``. The store sets that directory to 0700 itself, which its owner
      can always do, but only after it has looked in it: without read permission no launch gets that
      far, and without search permission it depends on the Python (``_launcher_can_look_in``). One that
      belongs to another user stops the launch whatever its mode. Without it the home must be writable.
    - A home that cannot be written, with a ``sessions`` that can be used, is a warning: the launch
      works, and Contexto cannot make its lock and salt files there."""
    try:
        home = contexto_home(env)
    except (RuntimeError, OSError) as exc:
        if isinstance(exc, OSError):
            why = "CONTEXTO_HOME is a relative path and the working directory cannot be read"
        elif env.get("CONTEXTO_HOME"):
            why = "CONTEXTO_HOME starts with ~ and names a user whose home directory cannot be found"
        else:
            why = "CONTEXTO_HOME is not set and the home directory cannot be found (HOME is not set)"
        raise Fault("no_store_home", f"it cannot be located: {why}",
                    "Set CONTEXTO_HOME to the full path of a directory you can write") from exc
    exists = os.path.lexists(home)
    details = {"path": str(home), "exists": exists}
    fix = "Make it a directory you can write, or set CONTEXTO_HOME to one"

    def failed(problem: str, action: str = fix) -> Check:
        return Check("store", FAIL, f"Store home {home} {problem}", action, details)

    if exists and not home.is_dir():
        return failed("is not a directory")
    if claude:
        try:
            check_mcp_values(MCP_SESSION, home, "")
        except LaunchError:  # its message is the launcher's usage error; the same fact in doctor's two lines
            return failed("is refused by `contexto run claude`: its path contains '${', which Claude Code would "
                          "expand in the MCP config", "Set CONTEXTO_HOME to a path without '${' (`contexto run "
                          "codex` accepts this one)")
    if not exists:
        parent = next(p for p in home.parents if os.path.lexists(p))
        if not parent.is_dir():
            return failed(f"cannot be created: {parent} is not a directory")
        if not os.access(parent, os.W_OK | os.X_OK):
            return failed(f"cannot be created: {parent} is not writable")
        return Check("store", PASS, f"Store home {home} does not exist yet and can be created", details=details)
    home_problem = write_problem(home)
    sessions = home / "sessions"
    if not os.path.lexists(sessions):
        if home_problem:
            return failed(f"cannot be written ({home_problem})")
        return Check("store", PASS, f"Store home {home} is writable", details=details)
    note = ""
    try:
        if not sessions.is_dir():
            return failed("cannot be used: its sessions entry is not a directory")
        if sessions.stat().st_uid != os.geteuid() and os.geteuid() != 0:
            return failed("cannot be used: its sessions directory belongs to another user, so `contexto run` cannot "
                          "set its mode")
        if not os.access(sessions, os.R_OK) or not _launcher_can_look_in(sessions):
            # The store sets the mode only after it has looked in the directory. Without read permission
            # every launch fails there. Without search permission it depends on the Python: before 3.14
            # the launcher's first look (``Path.exists`` on a session inside) raises, and from 3.14 it
            # returns False and the launch goes on (PR #258's round-3 review). Doctor asks this Python.
            return failed("cannot be used: its sessions directory cannot be read and entered, and `contexto run` "
                          "fails there before it can set the mode", f"Run: chmod u+rwx {shlex.quote(str(sessions))}")
        if os.access(sessions, os.W_OK | os.X_OK):  # else the store sets the mode before it writes
            problem = write_problem(sessions)
            if problem:
                return failed(f"cannot be written in its sessions directory ({problem})")
        else:
            note = " (its sessions directory cannot be written into now; `contexto run` sets it to mode 0700)"
    except OSError as exc:
        return failed(f"cannot be used: its sessions directory cannot be read ({exc.strerror or type(exc).__name__})")
    if home_problem:
        return Check("store", WARN, f"Store home {home} cannot be written ({home_problem}); `contexto run` still "
                     f"starts, because its sessions directory can be used{note}",
                     "Make the home writable to you: Contexto keeps a lock file and a salt file there, and goes "
                     "without them when it cannot create them", details)
    return Check("store", PASS, f"Store home {home} is writable{note}", details=details)


def _launcher_can_look_in(sessions: Path) -> bool:
    """Whether ``contexto run``'s first look in the store, ``session_dir(...).exists()`` in
    ``launcher.pick_session``, gets an answer on this Python, for a session that is not there."""
    try:
        (sessions / MCP_SESSION).exists()
    except OSError:
        return False
    return True


# The MCP server ---------------------------------------------------------------------------------------


def mcp_requests() -> bytes:
    """What a client sends first, one JSON-RPC message per line: ``initialize``, its notification, and
    ``tools/list``."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": MCP_PROTOCOL_VERSIONS[0], "capabilities": {},
                    "clientInfo": {"name": "contexto-doctor", "version": __version__}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    return "".join(json.dumps(message) + "\n" for message in messages).encode()


def mcp_results(output: bytes) -> dict[Any, Any]:
    """``{id: result}`` for each JSON-RPC result line in the server's output; anything else is skipped."""
    results: dict[Any, Any] = {}
    for line in output.decode("utf-8", "replace").splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and isinstance(message.get("result"), dict):
            try:
                results[message.get("id")] = message["result"]
            except TypeError:  # an id that cannot be a key
                continue
    return results


def check_mcp(env: Mapping[str, str], cwd: Path, *, command: list[str] | None = None,
              timeout: float = MCP_TIMEOUT_S) -> Check:
    """Start the MCP server as ``contexto run`` has the CLI start it (``launcher.mcp_payload``: this
    Python by absolute path, ``-P -m contexto mcp``, in the working directory), send ``initialize`` and
    ``tools/list`` on stdin, and compare the listed tools with Contexto's. It runs against a temporary
    home with one empty session, removed afterwards. The requests are written, stdin is closed (the
    server's shutdown signal) and the answers read, all within ``timeout``; a server still running then
    is killed. The server leads its own process group, so the kill reaches a child it started, and its
    pipes are closed, not read to the end, so a process that outlives the kill and holds a pipe cannot
    keep the check past ``timeout``. The cleanup runs on any way out, Ctrl+C and ``Terminated`` (SIGTERM,
    SIGHUP) included. ``command`` replaces the server's command line (tests)."""
    expected = sorted(tool["name"] for tool in TOOLS)
    fix = (f"Check that `{sys.executable} -P -m contexto --version` works: the CLIs start the server that way, so "
           "Contexto must be installed in this Python (a source directory on the path is not enough)")
    details: dict[str, Any] = {"tools": [], "missing": expected, "unexpected": [], "seconds": 0.0}
    started = time.monotonic()

    def failed(problem: str) -> Check:
        details["seconds"] = round(time.monotonic() - started, 3)
        return Check("mcp", FAIL, f"MCP server {problem}", fix, details)

    tmp: Path | None = None
    store = child = None
    try:
        try:
            with signals_held():  # no signal between the directory being made and its name being kept
                tmp = Path(tempfile.mkdtemp(prefix="contexto-doctor-"))
        except OSError as exc:  # FileNotFoundError when Python finds no usable temporary directory at all
            raise Fault("no_temp_directory", "the check needs a temporary directory and none can be made "
                        f"({exc.strerror or type(exc).__name__})", "Set TMPDIR to a directory you can write") from exc
        store = SessionStore(tmp, MCP_SESSION)  # `contexto mcp` serves an existing session only (D57)
        server = mcp_payload(MCP_SESSION, tmp)["mcpServers"][SERVER_NAME]
        argv = command or [server["command"], *server["args"]]
        try:
            with signals_held():  # nor between the server starting and this having it to stop
                child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env={**env, **server["env"]}, cwd=cwd,
                                         start_new_session=True)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return failed(f"cannot be started ({getattr(exc, 'strerror', None) or type(exc).__name__})")
        try:
            output, errors = child.communicate(mcp_requests(), timeout=timeout)
        except subprocess.TimeoutExpired:
            return failed(f"did not answer within {timeout:g} s")
        listed = mcp_results(output).get(2, {}).get("tools")
        if not isinstance(listed, list):
            last = (errors.decode("utf-8", "replace").strip().splitlines() or [""])[-1][:MCP_ERROR_CHARS]
            if child.returncode:
                return failed(f"exited with code {child.returncode} before listing its tools"
                              + (f": {last}" if last else ""))
            return failed("did not list its tools")
        names = sorted(str(tool.get("name")) for tool in listed if isinstance(tool, dict))
        details.update(tools=names, missing=sorted(set(expected) - set(names)),
                       unexpected=sorted(set(names) - set(expected)))
        if details["missing"] or details["unexpected"]:
            wrong = [f"{what}: {', '.join(details[what])}" for what in ("missing", "unexpected") if details[what]]
            return failed(f"listed other tools than Contexto's {len(expected)} ({'; '.join(wrong)})")
        details["seconds"] = round(time.monotonic() - started, 3)
        return Check("mcp", PASS, f"MCP server started and listed its {len(expected)} tools", details=details)
    finally:
        if child is not None:
            stop_server(child)
        if store is not None:
            store.release()  # the session's live lock, before its directory goes
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def stop_server(child: subprocess.Popen) -> None:
    """Kill the server's process group, reap the server, and close this end of its pipes. Nothing is read
    here: ``communicate`` after the kill waited for end of file, which a process that had left the group
    with a pipe still open put off for as long as it lived (8 s against a 1 s timeout, reviewer B, PR
    #258)."""
    kill_group(child)  # kills the group, reaps the child (waiting 1 s at most), closes stdout
    for pipe in (child.stdin, child.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:  # stdin's unwritten requests, flushed into a pipe nobody reads any more
            pass


# Cloud providers --------------------------------------------------------------------------------------


def check_provider(ctx: Context) -> Check:
    """Whether ``contexto run claude`` would refuse to launch from this directory because Claude Code may
    send its requests to a cloud provider, past the proxy (D70). The launcher's own check, with the
    arguments it uses (``contexto.upstream.cloud_provider``; ``contexto.launcher.check_cloud_provider``
    for the message): a switch on in the environment or in any settings file, or a settings file that
    is there, could not be read and may turn one on. A failure: the launch exits 2. The summary is
    ``cloud_provider_note`` and the next step is the rest of the launcher's message, word for word."""
    found = cloud_provider(env=ctx.env, cwd=ctx.cwd, home=ctx.home, files=ctx.files)
    if found is None:
        return Check("provider", PASS, "Cloud provider: no switch (CLAUDE_CODE_USE_BEDROCK and the like) is on, so "
                     "Claude Code sends its requests through the proxy", details={"variables": [], "unread": []})
    note = cloud_provider_note(found)
    try:
        check_cloud_provider(ctx.cwd, ctx.env, home=ctx.home, files=ctx.files)  # the same files, already read
        message = ""
    except LaunchError as exc:
        message = str(exc)
    details = {"variables": [{"variable": name, "provider": CLOUD_PROVIDER_KEYS.get(name), "sources": list(places)}
                             for name, places in found.on],
               "unread": [{"source": place, "problem": problem} for place, problem in found.unread]}
    return Check("provider", FAIL, f"{note}: `contexto run claude` refuses to launch (exit 2)",
                 message.removeprefix(f"{note}. ") or None, details)
