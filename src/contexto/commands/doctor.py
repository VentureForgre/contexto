"""``contexto doctor [--json]``: check that ``contexto run`` can work from this directory (D64).

One line per check, marked ``[ok]``, ``[warn]``, ``[FAIL]`` or ``[skip]``; a warning or a failure is
followed by one line saying what to do. The checks, in order:

- ``python``: this interpreter is 3.12 or later.
- ``claude``, ``codex``: the CLI is on PATH, its ``--version`` (read as ``contexto run`` reads it,
  ``contexto.cliversions.installed_version``), and whether that version is in ``TESTED_VERSIONS``.
- ``claude-upstream``, ``codex-upstream``: the upstream ``contexto run`` would use from this directory
  and what set it (``contexto.upstream.resolve_upstream``; for Codex
  ``contexto.codex.codex_upstream_with_source`` in API-key mode, its default). Only the URL's origin is
  shown: scheme, host and port, never user info, a path or a query, any of which can hold a credential.
- ``claude-key``, ``codex-key``: whether a key or key helper is configured, by name and place. For
  Claude Code, which sends its own credential through the proxy, that is ``ANTHROPIC_API_KEY``,
  ``ANTHROPIC_AUTH_TOKEN`` or ``CLAUDE_CODE_OAUTH_TOKEN`` (environment or a settings ``env`` block) or
  an ``apiKeyHelper``; a login Claude Code holds is not looked for, so none found is a warning. For
  Codex it is the helper ``contexto run codex`` would hand it: ``CONTEXTO_CODEX_KEY_HELPER``, else
  Claude Code's ``apiKeyHelper``, a project's only in a folder Claude Code trusts (D52).
- ``store``: the store home can be written, or created.
- ``mcp``: the MCP server starts as the launcher starts it and lists Contexto's tools.
- ``provider``: no variable sends Claude Code's model requests to another provider, past the proxy
  (``contexto.providers``).

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

Exit code 0 when no check failed, warnings or not; 1 when one did. These fail: Python below 3.12,
neither CLI usable, an upstream that is not an http(s) URL (``contexto run`` refuses to start with
it), a store home that cannot be written or created, and an MCP server that does not start and list
exactly Contexto's tools. Everything else is a warning: an untested or unreadable CLI version, one of
the two CLIs missing, no key found (a login the CLI holds also works), no upstream or key helper for
Codex (a flag to ``contexto run`` can supply either), a setting skipped as untrusted, and a provider
variable. A check that does not apply because its CLI is missing is skipped.

``--json`` prints ``{"schema": 1, "contexto": <version>, "ok": <bool>, "summary": {"pass", "warn",
"fail", "skip"}, "checks": [{"id", "status", "summary", "action", "details"}]}``; docs/doctor.md
lists each check's ``details``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contexto import __version__, providers
from contexto.cliversions import CLI_NAMES, REPORT_URL, TESTED_VERSIONS, installed_version, is_tested, tested_list
from contexto.codex import API_KEY_MODE, KEY_HELPER_ENV, UPSTREAM_ENV, CodexLaunchError, codex_upstream_with_source
from contexto.codex import _start_failure as cannot_start
from contexto.launcher import LaunchError, check_upstream, cli_not_found, mcp_payload
from contexto.mcp import LEGACY_VERSIONS as MCP_PROTOCOL_VERSIONS
from contexto.mcp import SERVER_NAME, TOOLS
from contexto.paths import contexto_home
from contexto.store import SessionStore
from contexto.upstream import BASE_URL_KEY, KEY_HELPER_KEY, claude_key_helper, resolve_upstream, untrusted_note

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


@dataclass(frozen=True)
class Context:
    """What the checks read: the environment, the working directory and the home directory of the
    ``contexto run`` being diagnosed."""
    env: Mapping[str, str]
    cwd: Path
    home: Path


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "doctor", help="check that Contexto can run here: the CLIs, the upstream, keys, the store, the MCP server",
        description="Check what `contexto run` needs from this directory: Python, the claude and codex CLIs and "
                    "their versions, the upstream and where it comes from, whether a key or key helper is "
                    "configured, the store home, the MCP server, and variables that would make Claude Code "
                    "bypass the proxy. Makes no network request, runs no key helper, and prints no key.",
        epilog="Exit codes: 0 no check failed (warnings allowed); 1 a check failed; 130 Ctrl+C. "
               "Details: docs/doctor.md.")
    parser.add_argument("--json", action="store_true", help="print the results as JSON (shape in docs/doctor.md)")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        checks = run_checks()
    except KeyboardInterrupt:  # each check stops what it started on the way out
        print("contexto: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    print(json.dumps(report(checks), indent=2) if args.json else render(checks))
    return EXIT_FAILED if any(c.status == FAIL for c in checks) else EXIT_OK


def run_checks(context: Context | None = None) -> list[Check]:
    """Every check, in the documented order. A check that raises is reported as failed, by the error's
    type only (its text could hold a URL or a path): doctor must not die of the fault it is there to
    find."""
    ctx = context or Context(os.environ, Path.cwd(), Path.home())
    checks = [guarded("python", "Python", check_python)]
    clis = [guarded(cli, CLI_NAMES[cli], lambda cli=cli: check_cli(cli, ctx.env)) for cli in ("claude", "codex")]
    require_one_cli(clis)
    checks += clis
    usable = {c.id: bool(c.details.get("usable")) for c in clis}
    for check in clis:
        check.details.pop("usable", None)
    per_cli: list[tuple[str, str, str, Callable[[], Check]]] = [
        ("claude-upstream", "claude", "Claude Code upstream", lambda: check_claude_upstream(ctx)),
        ("codex-upstream", "codex", "Codex upstream", lambda: check_codex_upstream(ctx)),
        ("claude-key", "claude", "Claude Code key", lambda: check_claude_key(ctx)),
        ("codex-key", "codex", "Codex key helper", lambda: check_codex_key(ctx)),
    ]
    for check_id, cli, label, check in per_cli:
        checks.append(guarded(check_id, label, check) if usable[cli] else skipped(check_id, label, cli))
    checks.append(guarded("store", "Store home", lambda: check_store(ctx.env)))
    checks.append(guarded("mcp", "MCP server", lambda: check_mcp(ctx.env, ctx.cwd)))
    checks.append(guarded("provider", "Provider variables", lambda: check_provider(ctx)) if usable["claude"]
                  else skipped("provider", "Provider variables", "claude"))
    return checks


def guarded(check_id: str, label: str, check: Callable[[], Check]) -> Check:
    try:
        return check()
    except Exception as exc:
        return Check(check_id, FAIL, f"{label}: the check itself failed ({type(exc).__name__})",
                     f"Report it at {REPORT_URL}", {"error": type(exc).__name__})


def skipped(check_id: str, label: str, cli: str) -> Check:
    return Check(check_id, SKIP, f"{label}: not checked, {cli} is not on PATH or cannot be run")


def report(checks: list[Check]) -> dict[str, Any]:
    """The ``--json`` document (the module docstring gives its shape)."""
    return {"schema": SCHEMA, "contexto": __version__, "ok": not any(c.status == FAIL for c in checks),
            "summary": {status: sum(1 for c in checks if c.status == status) for status in (PASS, WARN, FAIL, SKIP)},
            "checks": [{"id": c.id, "status": c.status, "summary": c.summary, "action": c.action,
                        "details": c.details} for c in checks]}


def render(checks: list[Check]) -> str:
    lines = [f"contexto doctor (contexto {__version__})", ""]
    for check in checks:
        lines.append(f"{MARKS[check.status]:<{MARK_WIDTH}}{check.summary}")
        if check.action:
            lines.append(f"{'':<{MARK_WIDTH}}-> {check.action}")
    return "\n".join([*lines, "", verdict(checks)])


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
    (removed by ``run_checks``) says whether it can be started at all; a missing or unstartable CLI is a
    warning here, and ``require_one_cli`` makes it a failure when the other one is unusable too."""
    name = CLI_NAMES[cli]
    details: dict[str, Any] = {"cli": cli, "path": None, "version": None, "tested": None,
                               "tested_versions": list(TESTED_VERSIONS[cli]), "usable": False}
    install = f"Install {name} and put it on PATH to use `contexto run {cli}`"
    path = shutil.which(cli, path=env.get("PATH"))
    if path is None:
        details["error"] = "not_found"
        return Check(cli, WARN, f"{name}: {cli_not_found(cli, env.get('PATH'))}", install, details)
    details["path"] = path
    version, error = installed_version(path, env=env)
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
    """Contexto needs one of the two CLIs: with neither usable, both checks fail."""
    if any(c.details.get("usable") for c in clis):
        return
    for check in clis:
        if check.status == WARN:
            check.status = FAIL
            check.action = "Install Claude Code or Codex and put it on PATH: Contexto needs at least one of them"


# Upstreams --------------------------------------------------------------------------------------------


def url_origin(url: str) -> str | None:
    """``scheme://host[:port]`` of an http(s) URL, or None when it is not one. User info, the path and the
    query are left out: each can hold a credential."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}" + ("" if port is None else f":{port}")


def base_url_place(source: str, env: Mapping[str, str]) -> str:
    """Where Claude Code's upstream comes from, by ``contexto.upstream.Upstream.source``."""
    if source == "default":
        return f"the default: nothing sets {BASE_URL_KEY}"
    return f"{BASE_URL_KEY} in {providers.source_text(source, env)}"


def check_claude_upstream(ctx: Context) -> Check:
    """The upstream ``contexto run claude`` would use, resolved as ``launcher._run`` resolves it."""
    upstream = resolve_upstream(None, env=ctx.env, cwd=ctx.cwd, home=ctx.home)
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
    if source == "codex-env":
        return f"{UPSTREAM_ENV} in the environment"
    if source.startswith("claude-"):
        return f"Claude Code's upstream plus /v1; {base_url_place(claude_source, env)}"
    return source


def check_codex_upstream(ctx: Context) -> Check:
    """The upstream ``contexto run codex`` would use in API-key mode with no ``--codex-upstream``, resolved
    as ``codex.run_codex`` resolves it: a project's base URL only from a folder Claude Code trusts."""
    claude = resolve_upstream(None, env=ctx.env, cwd=ctx.cwd, home=ctx.home, trusted_only=True)
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
    set_by = providers.claude_env(CLAUDE_KEY_VARIABLES, ctx.cwd, ctx.home, ctx.env)
    for name in CLAUDE_KEY_VARIABLES:
        value, source = set_by.get(name, ("", ""))
        if value.strip():
            found.append({"name": name, "source": source})
            shown.append(f"{name} in {providers.source_text(source, ctx.env)}")
    helper = claude_key_helper(ctx.cwd, ctx.home, ctx.env, trusted_only=False)  # as Claude Code -p reads it
    if helper.value:
        found.append({"name": KEY_HELPER_KEY, "source": f"settings:{helper.source}"})
        shown.append(f"{KEY_HELPER_KEY} in {providers.settings_file(helper.source or '', ctx.env)} (not run)")
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
    not shown."""
    if (ctx.env.get(KEY_HELPER_ENV) or "").strip():
        return Check("codex-key", PASS, f"Codex key helper: {KEY_HELPER_ENV} in the environment (not run)",
                     details={"name": KEY_HELPER_ENV, "source": "env"})
    helper = claude_key_helper(ctx.cwd, ctx.home, ctx.env)  # a project's only from a trusted folder (D52)
    if helper.value:
        place = providers.settings_file(helper.source or "", ctx.env)
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


def check_store(env: Mapping[str, str]) -> Check:
    """The store home ``contexto run`` would use: writable when it exists (and its ``sessions``
    directory, where sessions are made), else creatable. A home that does not exist is judged by
    permission on its nearest existing parent and is not created: ``contexto run`` makes it, 0700."""
    home = contexto_home(env)
    exists = os.path.lexists(home)
    details = {"path": str(home), "exists": exists}
    fix = "Make it a directory you can write, or set CONTEXTO_HOME to one"

    def failed(problem: str) -> Check:
        return Check("store", FAIL, f"Store home {home} {problem}", fix, details)

    if not exists:
        parent = next(p for p in home.parents if os.path.lexists(p))
        if not parent.is_dir():
            return failed(f"cannot be created: {parent} is not a directory")
        if not os.access(parent, os.W_OK | os.X_OK):
            return failed(f"cannot be created: {parent} is not writable")
        return Check("store", PASS, f"Store home {home} does not exist yet and can be created", details=details)
    if not home.is_dir():
        return failed("is not a directory")
    for directory in (home, home / "sessions"):
        if directory != home and not os.path.lexists(directory):
            continue
        problem = "Not a directory" if not directory.is_dir() else write_problem(directory)
        if problem:
            where = "" if directory == home else " in its sessions directory"
            return failed(f"cannot be written{where} ({problem})")
    return Check("store", PASS, f"Store home {home} is writable", details=details)


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
    is killed. ``command`` replaces the server's command line (tests)."""
    expected = sorted(tool["name"] for tool in TOOLS)
    fix = (f"Check that `{sys.executable} -P -m contexto --version` works: the CLIs start the server that way, so "
           "Contexto must be installed in this Python (a source directory on the path is not enough)")
    details: dict[str, Any] = {"tools": [], "missing": expected, "unexpected": [], "seconds": 0.0}
    started = time.monotonic()

    def failed(problem: str) -> Check:
        details["seconds"] = round(time.monotonic() - started, 3)
        return Check("mcp", FAIL, f"MCP server {problem}", fix, details)

    tmp = Path(tempfile.mkdtemp(prefix="contexto-doctor-"))
    store = child = None
    try:
        store = SessionStore(tmp, MCP_SESSION)  # `contexto mcp` serves an existing session only (D57)
        server = mcp_payload(MCP_SESSION, tmp)["mcpServers"][SERVER_NAME]
        argv = command or [server["command"], *server["args"]]
        try:
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env={**env, **server["env"]}, cwd=cwd)
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
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()  # reap it and close its pipes
        if store is not None:
            store.release()  # the session's live lock, before its directory goes
        shutil.rmtree(tmp, ignore_errors=True)


# Provider variables -----------------------------------------------------------------------------------


def check_provider(ctx: Context) -> Check:
    found = providers.provider_overrides(ctx.cwd, ctx.home, ctx.env)
    variables = [{"variable": o.variable, "source": o.source} for o in found]
    if not found:
        return Check("provider", PASS, "No provider variable (CLAUDE_CODE_USE_BEDROCK and the like) sends Claude "
                     "Code's requests past the proxy", details={"variables": variables})
    provider = found[0].provider
    return Check("provider", WARN, f"{providers.bypass_places(found, ctx.env)}: Claude Code sends model requests to "
                 f"{provider}, not through Contexto", providers.bypass_remedy(found),
                 {"provider": provider, "variables": variables})
