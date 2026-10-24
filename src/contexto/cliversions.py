"""The Claude Code and Codex versions Contexto was tested with, and the checks against them (D38).

Contexto recognises parts of each CLI's request by text the CLI writes: Claude Code's session-start
message by its ``# Environment`` heading and its context blocks by their first lines
(``contexto.rewrite``, D32 and D33), and Codex's session context by its section tags
(``contexto.codex``). A CLI update can change that text, and nothing fails: the message falls into
another class and may be paged. So two checks look for a CLI Contexto was not tested with:

- At launch, ``contexto run`` runs ``<cli> --version`` (``VERSION_TIMEOUT_S``, stdin closed) and, when
  the version is not in ``TESTED_VERSIONS``, prints one warning line on stderr. The launch goes on
  whatever happens; a CLI that does not answer is logged and not warned about. ``--no-version-check``
  or ``CONTEXTO_NO_VERSION_CHECK=1`` skips the check.
- In the proxy, ``ClientVersions`` reads each request's ``User-Agent`` (measured with zero spend:
  Claude Code 2.1.302 sends ``claude-cli/2.1.302 (external, sdk-cli)``, codex-cli 0.163.0 sends
  ``codex_exec/0.163.0 (...)``), logs each version it sees once, and logs one warning when a version
  is untested. On the main conversation's first request it also checks that the session start was
  recognised (``Dialect.session_start_seen``) and logs one warning when the request carries the
  CLI's injected context but no session start Contexto recognises.

Every event is content-free: CLI names, version strings, and booleans.

``TESTED_VERSIONS`` is the single list. Each tuple runs oldest to newest; the newest is the one the
measured-on constants next to each signature must name (``tests/test_cliversions.py`` checks it).
``docs/cli-updates.md`` is the routine for testing a new version and adding it here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Mapping

# Claude Code 2.1.302: session-start signature, context-block kinds, and the missing-tools notices
# (spikes/m2/scale/reminders, live run #3). codex-cli 0.154.0: the Responses shapes and acceptance
# (D30); 0.163.0: the MCP launch and missing-tools check (v0.2.8), and the same context-section
# openings, rechecked with zero spend for D38.
TESTED_VERSIONS: dict[str, tuple[str, ...]] = {
    "claude": ("2.1.302",),
    "codex": ("0.154.0", "0.163.0"),
}
CLI_NAMES = {"claude": "Claude Code", "codex": "Codex"}
AT_RISK = {
    "claude": "it may misread the session start and reminders and page text it should carry",
    "codex": "it may misread Codex's session context and page it as a turn",
}
DIALECT_CLI = {"anthropic": "claude", "responses": "codex"}
REPORT_URL = "https://github.com/VentureForgre/contexto/issues"
NO_CHECK_ENV = "CONTEXTO_NO_VERSION_CHECK"
VERSION_TIMEOUT_S = 3.0
MAX_SEEN = 8  # distinct client versions logged per engine; a bound, not an expected number

_VERSION = re.compile(r"(?<![\w.])(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)(?![\w.])")
_USER_AGENT = re.compile(r"\s*([A-Za-z][A-Za-z0-9_.-]*)/(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)(?![\w.])")


def parse_version(text: str) -> str | None:
    """The first ``X.Y.Z`` version in a ``--version`` output (``2.1.302 (Claude Code)``,
    ``codex-cli 0.163.0``), or None."""
    match = _VERSION.search(text or "")
    return match.group(1) if match else None


def is_tested(cli: str, version: str) -> bool:
    return version in TESTED_VERSIONS.get(cli, ())


def tested_list(cli: str) -> str:
    return ", ".join(TESTED_VERSIONS.get(cli, ())) or "none"


def untested_warning(cli: str, version: str) -> str:
    """The one-line launch warning for an untested version (without the ``contexto: `` prefix)."""
    name = CLI_NAMES.get(cli, cli)
    return (f"warning: {name} {version} is not a version Contexto was tested with (tested: {tested_list(cli)}); "
            f"{AT_RISK.get(cli, 'it may misread what the CLI sends')}. Check for a newer Contexto or report it at "
            f"{REPORT_URL} (silence: --no-version-check or {NO_CHECK_ENV}=1).")


def check_disabled(env: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if env is None else env).get(NO_CHECK_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no")


def installed_version(binary: str, *, timeout: float = VERSION_TIMEOUT_S,
                      env: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """``(version, None)`` from ``<binary> --version``, or ``(None, reason)`` when it cannot be read:
    ``not_found``, ``timeout``, ``exit_<code>``, ``unparsed``, or the exception's type name."""
    path = shutil.which(binary) if os.sep not in binary else binary
    if path is None:
        return None, "not_found"
    try:
        done = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors="replace", timeout=timeout, env=None if env is None else dict(env))
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, type(exc).__name__
    if done.returncode != 0:
        return None, f"exit_{done.returncode}"
    version = parse_version(done.stdout) or parse_version(done.stderr)
    return (version, None) if version else (None, "unparsed")


def launch_check(cli: str, binary: str, *, enabled: bool = True, env: Mapping[str, str] | None = None,
                 timeout: float = VERSION_TIMEOUT_S) -> tuple[dict[str, Any], str | None]:
    """The launcher's check: ``(cli_version_check event, warning line or None)``. Never raises."""
    if not enabled or check_disabled(env):
        return {"event": "cli_version_check", "cli": cli, "skipped": True}, None
    try:
        version, error = installed_version(binary, timeout=timeout, env=env)
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
    (``claude-cli/<v>``) or Codex (``codex_<surface>/<v>``, such as ``codex_exec`` or ``codex_cli_rs``);
    None for any other client."""
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
    engine, and ``session_start_unrecognised`` at most once per engine, when the first main request the
    dialect judges (``Dialect.judges_session_start``) carries the CLI's injected context but no session
    start the dialect recognises (``Dialect.session_start_seen`` returns False; None means it cannot
    tell)."""

    def __init__(self) -> None:
        self.seen: dict[tuple[str, str], str] = {}  # (cli, version) -> client
        self.latest: dict[str, str] = {}  # cli -> latest version seen
        self.untested_logged = False
        self.session_start_judged = False

    def observe(self, request: dict, headers: Mapping[str, str], dialect: Any, *, main: bool) -> list[dict]:
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
        # Judged once, on the first main request the dialect judges (Claude Code: one with a tool list; its
        # title, topic, and quota side requests have none and carry no session start), so the scan stays
        # off every later request.
        if main and not self.session_start_judged and dialect.judges_session_start(request):
            self.session_start_judged = True
            seen = dialect.session_start_seen(request.get(dialect.field) or [])
            if seen is False:
                cli = DIALECT_CLI.get(dialect.name, dialect.name)
                events.append({"event": "session_start_unrecognised", "dialect": dialect.name,
                               "signature_version": dialect.session_start_version,
                               "cli_version": self.latest.get(cli), "tested_versions": tested_list(cli)})
        return events
