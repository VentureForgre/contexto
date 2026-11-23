"""``contexto run claude``: run Claude Code with every model request going through the Contexto proxy.

Steps (M2 contract, Architecture):

1. Resolve the upstream Claude Code would otherwise use (``contexto.upstream``), before anything
   starts.
2. Open session S in ``contexto.paths.contexto_home()`` and start ``ContextoProxy`` on
   ``127.0.0.1:<free port>`` with the rewrite engine as its transform.
3. Write two temporary files, mode 0600, in a private temporary directory (never in the user's
   Claude configuration): a ``--settings`` file holding ``env.ANTHROPIC_BASE_URL = <proxy URL>``,
   ``env.CLAUDE_CODE_GATEWAY_HINT_HEADERS = 1`` and ``env.CONTEXTO_HINT_HEADERS`` (D70, below) and
   ``permissions.allow`` with one rule per Contexto MCP tool, by its exact name (D44:
   ``mcp__contexto__contexto_recall`` and the other four), and an ``--mcp-config`` file that makes
   Claude Code spawn ``contexto mcp --session S`` over stdio with the current interpreter.
4. Run ``claude --settings=<file> --mcp-config=<file> [user args]`` (``claude_argv``: one argument per
   flag, so the variadic ``--mcp-config`` cannot take a prompt right after ``claude``) as a child with
   the terminal passed through, wait for it, stop the proxy, delete the temporary files, and return
   claude's exit code.

The user's own settings, hooks, and MCP servers still load: a flag-level ``--settings`` only adds
the base URL and the allow rules, and a ``statusLine`` when no settings file Claude Code would read has
one (D64, ``contexto.statusline``: Claude Code replaces that key, so one the user has is never
overridden). Its base URL outranks the user, project, and local settings files.
Its allow rules join the user's permission rules rather than replacing them, and the user's ``deny``
and ``ask`` rules still win over them (measured with zero spend on Claude Code 2.1.302: a deny takes
the tool out of the request, an ask refuses it in ``-p`` mode). Without the rules, 2.1.302 refuses
every Contexto tool in ``-p`` mode ("you haven't granted it yet") and the session loses recall.
Limit: organization-managed settings outrank ``--settings``; if they set ``ANTHROPIC_BASE_URL``,
Claude Code bypasses the proxy and Contexto cannot keep the prompt bounded. ``CLAUDE_CONFIG_DIR`` is
never set and no user configuration file is edited.

Hint headers (D70): the ``--settings`` file asks Claude Code for its gateway hint headers
(``contexto.hints``), which say what kind of request each one is; the rewrite engine reads them in
place of signatures that change between Claude Code versions. A launch is in one of three states
(``contexto.hints.hint_headers_state``), logged in the launch event as ``hint_headers``:

- ``stripped``: the proxy leaves the hints out of what it forwards, so the upstream gets the headers
  it got before.
- ``forwarded``: the user had turned them on themselves (the process environment, a settings file
  ``env`` block, or the managed settings file), so they pass through as they did.
- ``off``: the user set ``CLAUDE_CODE_GATEWAY_HINT_HEADERS`` to ``0``, ``false``, ``no`` or ``off`` in
  the process environment, their user settings file or the managed settings file. The launcher then
  leaves the variable out of its ``env`` block, Claude Code sends no hint header, and the engine reads
  its signatures.

Claude Code applies the ``env`` block to its own environment, so every process the session starts (a
tool command, a hook, an MCP server) inherits it. The block therefore also holds
``CONTEXTO_HINT_HEADERS`` with the launch's state, and a nested ``contexto run claude`` that finds
``stripped`` there does not take the inherited ``1`` for the user's. Limit: a nested plain ``claude``
pointed at another base URL inherits the ``1`` too and sends the hints to that URL, where no Contexto
proxy removes them. The explicit off above is the way out: Contexto then sets nothing to inherit.

Cloud providers (D70): Google Vertex AI (``CLAUDE_CODE_USE_VERTEX``) and Microsoft Foundry
(``CLAUDE_CODE_USE_FOUNDRY``) are intercepted via ``ANTHROPIC_VERTEX_BASE_URL`` and
``ANTHROPIC_FOUNDRY_BASE_URL`` (clearing ``ANTHROPIC_FOUNDRY_RESOURCE`` so the Foundry SDK accepts
``baseURL``). Unsupported switches (``CLAUDE_CODE_USE_BEDROCK``, ``CLAUDE_CODE_USE_MANTLE``,
``CLAUDE_CODE_USE_ANTHROPIC_AWS``, ``CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD``), dual Vertex+Foundry,
conflicting precedence/trust overrides, and unreadable settings files that may name a switch are
refused before a session is opened (``check_cloud_provider``).

Claude Code's settings files are read once per launch, with a bound (``claude_launch_settings``).

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
directory stays in ``$TMPDIR`` (mode ``0700`` directory with ``0600`` settings/MCP files, holding
the proxy URL, tool names, session id, home path, and any merged user ``--settings`` content).
Terminal resize (SIGWINCH) goes from the kernel to claude without the launcher's help.

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

Project memory (D69): a session belongs to the project of the directory it was started in
(``contexto.store.project``) and carries what the main conversation pinned in that project's earlier
sessions. ``contexto run --no-project-memory <cli>``, or ``CONTEXTO_PROJECT_MEMORY`` set to ``0``,
``false``, ``off`` or ``no``, keeps the launch out of it: the session records ``project_memory: false``
before the proxy starts and before the CLI can start the MCP server (both read it from the store), and a
later resume of that session stays off without the option. A launch with memory on records nothing, so
it never turns a session back on. With the other start notices, after the session line and before the
CLI starts (the terminal is still the launcher's then), one line says how many records the session
carries, from how many of the project's earlier sessions, and how to list them or turn this off; there
is no line when the project has no earlier session or memory is off. The line names the project's
directory; the session log gets one ``project_memory`` event of counts, never the path.

``claude --version`` and ``--help`` (alone) run claude directly, with no session or proxy.

Exit codes: claude's own code; 128 + N if claude died from signal N, or if the launcher got signal N
before claude started; 2 for a usage error such as a bound above the guard, an invalid session id, a
cloud-provider switch that is on (D70), or a ``CONTEXTO_HOME`` that is not a directory; 1 if the proxy
or the session store cannot start (for example ``CONTEXTO_HOME`` is not writable); 127 if ``claude`` is
not on PATH or cannot be run; 130 for a Ctrl+C during the version check, before claude starts.
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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contexto.canonical import JSONTooDeepError, has_lone_surrogate, loads_bounded
from contexto.cliversions import check_disabled as version_check_disabled
from contexto.cliversions import launch_check
from contexto.config import FileConfig
from contexto.hints import HINT_HEADERS_KEY, HINT_MARKER_KEY, HINT_STATES, OFF, STRIPPED, hint_headers_state
from contexto.mcp import SERVER_NAME as MCP_SERVER_NAME
from contexto.mcp import TOOLS as MCP_TOOLS
from contexto.paths import PROJECT_MEMORY_ENV, contexto_home, mcp_env, project_memory_env, session_dir
from contexto.private import affinity_salt, open_private, private_dir
from contexto.proxy import ContextoProxy, affinity_key, identity_transform, is_vendor_host
from contexto.stats import QUIET_ENV, fault_notices, log_unwritable_message, print_exit_summary, quiet_from_env
from contexto.statusline import NO_STATUSLINE_ENV, launch_status_line
from contexto.statusline import opted_out as statusline_opted_out
from contexto.statusline import skip_notice as statusline_skip_notice
from contexto.store import LIVE_LOCK, SessionStore
from contexto.store.pinned import render_carried
from contexto.store.project import project_root, read_meta, recorded_root
from contexto.store.prune import discard_new_session
from contexto.store.sessions import named, starting_with
from contexto.toolcheck import denied_tools_message, missing_tools_message
from contexto.upstream import (
    BASE_URL_KEY,
    FOUNDRY_BASE_URL_KEY,
    FOUNDRY_RESOURCE_KEY,
    INTERCEPTED_CLOUD_PROVIDERS,
    SETTINGS_FILE_LIMIT,
    SettingsFiles,
    Upstream,
    _has_nonfinite,
    _json_constant,
    active_cloud_provider,
    checked_provider_places,
    claude_env_value,
    claude_key_helper,
    claude_oauth_detected,
    cloud_provider,
    cloud_provider_note,
    read_settings_file,
    resolve_provider_upstream,
    resolve_upstream,
    untrusted_note,
)

DEFAULT_BOUND = 150_000
REPORT_LOCK_TIMEOUT_S = 2.0  # report_missing_tools: how long the exit waits for a request still in the engine
MIN_WINDOW_BUDGET = 25_000
DEFAULT_HEADROOM_FLOOR = 25_000
DEFAULT_PIN_BUDGET = 6_000  # rewrite.Config.pin_budget, for a launch without the engine
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
# after the CLI's name). The codex options are not listed: they only apply before 'codex'.
RUN_OPTIONS = {"--bound": True, "--allow-large-bound": False, "--window-fraction": True, "--headroom-floor": True,
               "--session": True, "--upstream": True, "--observe-thinking": False, "--no-version-check": False,
               "--version-check": False, "--quiet": False, "--no-quiet": False, "--no-statusline": False,
               "--statusline": False, "--no-project-memory": False, "--project-memory": False,
               "--no-in-turn-paging": False, "--in-turn-paging": False}
ENV_VALUE_SHOWN = 40  # characters of an unrecognised variable's value quoted on stderr
# D63: set to 0, false, off or no to turn in-turn paging off, like --no-in-turn-paging.
IN_TURN_PAGING_ENV = "CONTEXTO_IN_TURN_PAGING"
# Claude Code's long options whose value is the next argument even when it starts with "-": every main-command
# option declared with a required ``<value>`` in 2.1.314-2.1.328 binaries, shown in ``--help`` or
# hidden (variadic ones take at least that one). ``claude_options`` skips that value.
CLAUDE_VALUE_OPTIONS = frozenset({
    "--add-dir", "--agent", "--agents", "--allowedTools", "--allowed-tools", "--append-system-prompt",
    "--append-system-prompt-file", "--autocompact", "--betas", "--channels", "--dangerously-load-development-channels",
    "--debug-file", "--disallowedTools", "--disallowed-tools", "--effort", "--environment", "--fallback-model",
    "--file", "--forward-home-settings", "--input-format", "--json-schema", "--max-budget-usd", "--mcp-config",
    "--model", "--name", "--output-format", "--permission-mode", "--permission-prompts", "--plugin-dir",
    "--plugin-url", "--remote-control-session-name-prefix", "--session-id", "--setting-sources", "--settings",
    "--system-prompt", "--system-prompt-file", "--system-prompt-snapshot", "--tools",
    # Hidden (in the 2.1.314-2.1.328 binaries, not in --help):
    "--advisor", "--agent-color", "--agent-id", "--agent-name", "--agent-type", "--append-subagent-system-prompt",
    "--append-subagent-system-prompt-file", "--attach-serve", "--client-data-url", "--correlation-id",
    "--deep-link-last-fetch", "--deep-link-repo", "--fast-mode", "--inherit-permission-mode", "--managed-settings",
    "--max-thinking-tokens", "--max-turns", "--messaging-socket-path", "--on-branch", "--parent-session-id",
    "--permission-prompt-tool", "--plan-mode-instructions", "--plugin-dir-no-mcp", "--pool", "--prefill",
    "--proactivity", "--project-config-root", "--ref", "--resume-drops-turn", "--resume-session-at",
    "--rewind-files", "--runner-declares", "--sdk-url", "--session-began-as-copy", "--task-budget", "--team-name",
    "--teammate-mode", "--thinking", "--thinking-display", "--watch-artifact", "--watch-artifact-no-autoreact",
    "--workload"})
# Claude Code's long options with an optional value (``[value]`` in 2.1.328 --help): when followed by a
# non-option argument (or bare ``-``), that argument is its value; when followed by another ``--flag``,
# the option takes no value and the flag is still parsed.
CLAUDE_OPTIONAL_VALUE_OPTIONS = frozenset({
    "--cloud", "--debug", "--from-pr", "--prompt-suggestions", "--remote-control", "--resume", "--teleport",
    "--worktree",
})
# Claude Code's short options (2.1.314 and 2.1.318, the same): True for a flag, False when it takes a value.
# Commander reads a cluster such as ``-pc`` or ``-rabc123`` one letter at a time: a flag is consumed and the
# rest is read again (``-pc`` is ``-p -c``); an option that takes a value, required (``-n``) or optional
# (``-r``, ``-d``, ``-w``), takes the rest of the argument as its value (``-rabc123`` is ``--resume abc123``,
# and ``-rc`` resumes "c"). Measured on 2.1.318: ``claude -prabc123`` and ``claude -rc`` both fail on the
# resume value ("abc123", "c"). ``-h`` is not listed: it is Commander's help option, which a cluster does not
# look up, so an ``h`` ends the cluster (``claude -hc`` is an unknown option; ``-ch`` prints help).
CLAUDE_SHORT_OPTIONS = {"c": True, "p": True, "v": True, "d": False, "n": False, "r": False, "w": False}
CLAUDE_SHORT_REQUIRED = frozenset({"n"})  # the short option whose value is the next argument even with a "-"
CHOOSE_LOCK = "choose.lock"  # under $CONTEXTO_HOME: one launch at a time picks a session automatically
CHOOSE_LOCK_WAIT_S = 5.0
CHOOSE_LOCK_NOTICE_S = 1.0  # a wait this long is said on stderr (D59)
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
    quiet: bool = False  # D64: no summary at exit; warnings are still printed
    statusline: bool = True  # D64: add Contexto's status line when the user has none (contexto.statusline)
    project_memory: bool = True  # False: --no-project-memory (D69)
    in_turn_paging: bool = True  # D63: page inside a running turn (rule 5)
    config_upstream: str | None = None  # optional upstream default from config.toml
    statusline_source: str | None = None  # "flag", "env", "config", "cli", or None
    project_memory_explicit: bool = False  # True when CLI explicitly set --project-memory / --no-project-memory
    project_memory_source: str | None = None  # "cli", "env", "config", or None
    version_check_explicit: bool = False  # True when CLI explicitly set --version-check / --no-version-check


def _validate_settings_object(data: Any, label: str) -> dict[str, Any]:
    """Validate that a user ``--settings`` JSON payload is an object with valid ``env`` and
    ``permissions`` shapes."""
    if not isinstance(data, dict):
        raise LaunchError(f"{label} must be a JSON object", EXIT_USAGE)
    if "env" in data and not isinstance(data["env"], dict):
        raise LaunchError(f"{label} has an 'env' field that is not a JSON object", EXIT_USAGE)
    if "permissions" in data:
        perms = data["permissions"]
        if not isinstance(perms, dict):
            raise LaunchError(f"{label} has a 'permissions' field that is not a JSON object", EXIT_USAGE)
        for field_name in ("allow", "deny", "ask"):
            if field_name in perms and not isinstance(perms[field_name], list):
                raise LaunchError(f"{label} has a 'permissions.{field_name}' field that is not a JSON list",
                                  EXIT_USAGE)
    return data


def _load_user_settings_arg(raw: str) -> dict[str, Any]:
    """Parse one user ``--settings <file-or-json>`` value safely (inline JSON object or regular JSON
    file within ``SETTINGS_FILE_LIMIT``, via a single non-blocking bounded descriptor read). Never
    leaks raw JSON text or file contents in errors."""
    if not raw or not raw.strip():
        raise LaunchError("--settings requires a non-empty file path or JSON object", EXIT_USAGE)
    stripped = raw.lstrip()
    if stripped.startswith(("{", "[")):
        if len(raw.encode("utf-8", "surrogatepass")) > SETTINGS_FILE_LIMIT:
            raise LaunchError("--settings JSON exceeds the 4 MiB limit", EXIT_USAGE)
        try:
            data = loads_bounded(raw, parse_constant=_json_constant)
        except JSONTooDeepError:
            raise LaunchError("--settings JSON is nested too deeply", EXIT_USAGE) from None
        except (ValueError, RecursionError):
            raise LaunchError("--settings is not valid JSON", EXIT_USAGE) from None
        if _has_nonfinite(data):
            raise LaunchError("--settings is not valid JSON", EXIT_USAGE)
        return _validate_settings_object(data, "--settings JSON")
    path = Path(raw)
    found = read_settings_file(path, require_object=True, reject_huge_int=True)
    if found.problem:
        raise LaunchError(f"--settings file {found.problem}", EXIT_USAGE)
    if found.data is None:
        raise LaunchError("--settings file does not exist or cannot be opened", EXIT_USAGE)
    return _validate_settings_object(found.data, "--settings file")


def _merge_user_settings_dicts(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge two user ``--settings`` dicts in order (later keys override; ``env`` merges shallowly
    and ``permissions`` merges ``allow``/``deny``/``ask`` lists without duplicates)."""
    merged = dict(base)
    for key, value in overlay.items():
        if key == "env" and isinstance(merged.get("env"), dict) and isinstance(value, dict):
            merged["env"] = {**merged["env"], **value}
        elif key == "permissions" and isinstance(merged.get("permissions"), dict) and isinstance(value, dict):
            perms = {**merged["permissions"], **value}
            for field_name in ("allow", "deny", "ask"):
                base_list = merged["permissions"].get(field_name)
                over_list = value.get(field_name)
                if isinstance(base_list, list) and isinstance(over_list, list):
                    combined = list(base_list)
                    for rule in over_list:
                        if rule not in combined:
                            combined.append(rule)
                    perms[field_name] = combined
            merged["permissions"] = perms
        else:
            merged[key] = value
    return merged


def extract_user_settings(claude_args: list[str]) -> tuple[list[str], dict[str, Any] | None]:
    """Extract and parse any user ``--settings <file-or-json>`` flags before ``--`` so Contexto can
    merge them into the temporary settings file it passes to Claude Code.

    Scanning stops at the end-of-options ``--`` marker (while respecting ``CLAUDE_VALUE_OPTIONS``, so
    ``--append-system-prompt -- --settings x.json`` still sees ``--settings`` and ``-p -- --settings``
    treats ``--settings`` as a literal prompt). Returns ``(remaining_args, merged_user_settings)``."""
    drop: set[int] = set()
    raw_values: list[str] = []
    for i, name, attached in claude_options(claude_args):
        if name != "--settings":
            continue
        if attached is not None:
            drop.add(i)
            raw_values.append(attached)
        else:
            if i + 1 >= len(claude_args):
                raise LaunchError("--settings requires a file path or JSON object", EXIT_USAGE)
            drop.add(i)
            drop.add(i + 1)
            raw_values.append(claude_args[i + 1])
    if not raw_values:
        return list(claude_args), None
    merged: dict[str, Any] = {}
    for raw in raw_values:
        merged = _merge_user_settings_dicts(merged, _load_user_settings_arg(raw))
    remaining = [arg for idx, arg in enumerate(claude_args) if idx not in drop]
    return remaining, merged


def merge_settings(user_settings: dict[str, Any] | None, base_payload: dict[str, Any],
                   status_line: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Merge a user's ``--settings`` payload with Contexto's required ``base_payload`` (``env`` and
    ``permissions.allow``) and optional ``status_line``.

    Contexto's ``env`` entries win so requests always reach the proxy; user ``env`` keys are kept.
    User ``permissions`` (``deny``, ``ask``, ``allow``, etc.) are preserved with Contexto's tool allow
    rules appended without duplicates. If the user's ``--settings`` defines ``statusLine``, it is
    preserved untouched and ``user_had_statusline`` is returned True."""
    if not user_settings:
        return {**base_payload, **status_line}, False
    merged = dict(user_settings)
    user_env = merged.get("env") if isinstance(merged.get("env"), dict) else {}
    merged["env"] = {**user_env, **base_payload["env"]}
    user_perms = dict(merged["permissions"]) if isinstance(merged.get("permissions"), dict) else {}
    allow = list(user_perms.get("allow")) if isinstance(user_perms.get("allow"), list) else []
    for rule in base_payload["permissions"]["allow"]:
        if rule not in allow:
            allow.append(rule)
    user_perms["allow"] = allow
    merged["permissions"] = user_perms
    user_had_statusline = "statusLine" in merged
    if not user_had_statusline and status_line:
        merged.update(status_line)
    return merged, user_had_statusline


def check_user_args(claude_args: list[str]) -> None:
    """Validate user arguments before ``--`` (such as ``--settings <file-or-json>``). Stops scanning
    after ``--`` so a literal prompt like ``--settings`` is never refused; fails safely only when a
    ``--settings`` flag has a missing, unreadable, or invalid JSON payload."""
    extract_user_settings(claude_args)


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
    (``http://[::1`` raised ValueError from ``urlsplit`` with a traceback and left an empty session).
    A URL with a lone surrogate, which a settings file can hold, cannot go into a request either. The
    message names the source and the problem, never the URL."""
    try:
        parts = urlsplit(upstream.url)
        _ = parts.port  # parsed on access; raises for a bad one
        ok = parts.scheme in ("http", "https") and bool(parts.hostname)
        problem = "upstream must be an http(s) base URL"
        if ok and has_lone_surrogate(upstream.url):
            ok, problem = False, "upstream holds a lone surrogate"
    except ValueError as exc:
        ok, problem = False, str(exc)
    if not ok:
        # Never the URL: it can hold a password or a secret path segment, and this lands in logs.
        raise LaunchError(f"invalid upstream (from {upstream.source}): {problem}", EXIT_USAGE)


def check_not_service(upstream: Upstream, home: Path, command: str) -> None:
    """Refuse an upstream that is the address of this home's ``contexto serve`` (a loopback host whose
    first path segment is the service's token; ``contexto.servecontract.names_service``). That is the
    base URL a client of the service is given, so it is what the default upstream resolves to once it
    is in the user's Claude Code settings, and requests would then pass two engines that open the
    same store. The URL is never printed: it holds the token."""
    from contexto.servecontract import names_service

    if names_service(upstream.url, home):
        raise LaunchError(
            f"the upstream (from {upstream.source}) is this Contexto home's `contexto serve`, which bounds the "
            f"requests it is sent itself. Pass --upstream URL (for example --upstream https://api.anthropic.com), "
            f"or use the client through `contexto serve` instead of `{command}`.", EXIT_USAGE)


def check_cloud_provider(cwd: Path, env: Mapping[str, str] | None = None, *, home: Path | None = None,
                         files: SettingsFiles | None = None, managed: Path | None = None,
                         extra_settings: dict[str, Any] | None = None, allow_intercepted: bool = True) -> None:
    """D70: refuse a launch that Claude Code may send to an unsupported cloud provider (such as
    SigV4-signed Amazon Bedrock, Mantle, Claude Platform on AWS, or Claude Platform on Google Cloud),
    or when both Vertex AI and Microsoft Foundry are enabled simultaneously, or when a settings file
    that may name a provider switch cannot be read.

    Vertex AI (``CLAUDE_CODE_USE_VERTEX``) and Microsoft Foundry (``CLAUDE_CODE_USE_FOUNDRY``) are
    intercepted directly via ``ANTHROPIC_VERTEX_BASE_URL`` and ``ANTHROPIC_FOUNDRY_BASE_URL`` when
    ``allow_intercepted=True`` (the default for ``contexto run claude``). The error message explicitly
    lists every checked environment and settings location."""
    found = cloud_provider(env=env, cwd=cwd, home=home, files=files, managed=managed,
                           extra_settings=extra_settings, allow_intercepted=allow_intercepted)
    if found is None:
        return
    steps = []
    for name, places in found.on:
        if "env" in places:
            steps.append(f"unset {name} for this command (env -u {name} contexto run ...)")
        if "flag:--settings" in places:
            steps.append(f"remove {name} from --settings, or set it to 0 there")
        blocks = len([place for place in places if place not in ("env", "flag:--settings")])
        if blocks:
            where = "that env block" if blocks == 1 else "those env blocks"
            steps.append(f"remove {name} from {where}, or set it to 0 there")
    for name, places, _ in found.conflicts:
        if "env" in places:
            steps.append(f"unset {name} for this command (env -u {name} contexto run ...)")
        if "flag:--settings" in places:
            steps.append(f"remove or align {name} in --settings")
        blocks = len([place for place in places if place not in ("env", "flag:--settings")])
        if blocks:
            where = "that env block" if blocks == 1 else "those env blocks"
            steps.append(f"remove or align {name} in {where}")
    if found.unread:
        steps.append("fix or remove the " + ("file" if len(found.unread) == 1 else "files")
                     + " that could not be read")
    remedy = "; ".join(steps)
    checked = checked_provider_places()
    if found.conflicts:
        why = (f"Contexto cannot tell whether Claude Code will route to that cloud provider or directly to "
               f"Anthropic (checked: {checked}), so the launch is refused")
        how = f"Make the provider switch consistent: {remedy}."
    elif found.on and all(name in INTERCEPTED_CLOUD_PROVIDERS for name, _ in found.on):
        why = (f"Both Google Vertex AI and Microsoft Foundry switches are on at once (checked: {checked}); "
               f"Contexto cannot tell which provider endpoint to intercept, whichever of them Claude Code "
               f"would apply")
        how = f"To run through Contexto, keep only one provider switch on: {remedy}."
    elif found.on:
        why = (f"Contexto's proxy would see no request and the session would run with no bound, so the launch "
               f"is refused. Contexto refuses when an unsupported provider switch is on in any of: {checked}, "
               f"whichever of them Claude Code would apply")
        how = (f"To run through Contexto: {remedy}; and give Contexto an upstream that speaks the Anthropic "
               f"Messages API (--upstream or ANTHROPIC_BASE_URL), or use Vertex AI / Microsoft Foundry. "
               f"To keep using this provider, run claude without contexto.")
    else:
        why = (f"With an unsupported provider on, Contexto's proxy would see no request and the session would "
               f"run with no bound, so the launch is refused (checked: {checked})")
        how = f"{remedy[0].upper()}{remedy[1:]}, then run again."
    raise LaunchError(f"{cloud_provider_note(found)}. {why}. {how}", EXIT_USAGE)


def claude_launch_settings(explicit_upstream: str | None, cwd: Path, env: Mapping[str, str] | None = None, *,
                           home: Path | None = None, managed: Path | None = None,
                           config_upstream: str | None = None,
                           extra_settings: dict[str, Any] | None = None,
                           files: SettingsFiles | None = None) -> tuple[Upstream, str]:
    """Everything ``contexto run claude`` takes from Claude Code's settings files (and any user
    ``--settings`` payload), each file read once and with a bound (``contexto.upstream.SettingsFiles``):
    the cloud-provider check, which refuses unsupported providers before anything else is decided; the
    upstream (``resolve_provider_upstream`` for Vertex AI / Microsoft Foundry, else ``resolve_upstream``);
    and the hint header state (``contexto.hints.hint_headers_state``). Nothing is created on disk."""
    files = files or SettingsFiles()
    check_cloud_provider(cwd, env, home=home, files=files, managed=managed, extra_settings=extra_settings)
    provider_key, _ = active_cloud_provider(env=env, cwd=cwd, home=home, files=files, managed=managed,
                                            extra_settings=extra_settings)
    if provider_key is not None:
        try:
            upstream = resolve_provider_upstream(provider_key, explicit_upstream, env=env, cwd=cwd, home=home,
                                                 files=files, managed=managed, extra_settings=extra_settings)
        except ValueError as exc:
            raise LaunchError(str(exc), EXIT_USAGE) from None
    else:
        upstream = resolve_upstream(explicit_upstream, env=env, cwd=cwd, home=home, files=files,
                                    config_upstream=config_upstream, extra_settings=extra_settings)
    return upstream, hint_headers_state(env=env, cwd=cwd, home=home, files=files, managed=managed,
                                        extra_settings=extra_settings)


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


# How to install each CLI (D64): its name, the first command its own documentation gives for macOS and
# Linux, and that page (read 2026-11-12: Claude Code's "Advanced setup", Codex's "Codex CLI", at the
# address developers.openai.com/codex/cli permanently redirects to).
CLI_INSTALL = {
    "claude": ("Claude Code", "curl -fsSL https://claude.ai/install.sh | bash",
               "https://code.claude.com/docs/en/setup"),
    "codex": ("Codex", "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
              "https://learn.chatgpt.com/docs/codex/cli"),
}


def cli_not_found(name: str, path: str | None = None) -> str:
    """Why ``shutil.which`` found no usable ``name``: a non-executable file, a broken symlink or a
    directory on PATH is named with what is wrong; otherwise "not found on PATH", and for ``claude`` and
    ``codex`` how to install it (D64). The CLI may be installed outside PATH, which is not searched for
    here, so the message names that case too."""
    candidates = [Path(name)] if os.sep in name else [
        Path(d or os.curdir) / name for d in (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)]
    for candidate in candidates:
        if candidate.is_symlink() and not candidate.exists():
            return f"'{name}' at {candidate} is a broken symlink (to {os.readlink(candidate)})"
        if candidate.is_dir():
            return f"'{name}' at {candidate} is a directory, not a program"
        if candidate.is_file() and not os.access(candidate, os.X_OK):
            return f"'{name}' at {candidate} is not executable (chmod +x it, or fix PATH)"
    if name not in CLI_INSTALL:
        return f"'{name}' not found on PATH"
    product, command, page = CLI_INSTALL[name]
    return (f"'{name}' not found on PATH. Install {product}: {command} (other ways: {page}). If it is installed, "
            "add its directory to PATH.")


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
    if not options.in_turn_paging:
        config["in_turn_paging"] = False
    return config


def in_turn_paging(flag_off: bool, env: dict[str, str] | None = None) -> bool:
    """D63: in-turn paging is on unless ``--no-in-turn-paging`` is given or ``CONTEXTO_IN_TURN_PAGING``
    is 0, false, off or no."""
    env = os.environ if env is None else env
    return not flag_off and env.get(IN_TURN_PAGING_ENV, "").strip().lower() not in ("0", "false", "off", "no")


def assumed_overhead(env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    return ASSUMED_OVERHEAD_OPENRIG if env.get("OPENRIG_SESSION_NAME") else ASSUMED_OVERHEAD_DEFAULT


# The measured fixed overheads the warning quotes: Claude Code 2.1.314 ran as low as 13,842 with a fresh
# config and up to 27,925 with tool search on (D55, D61).
MEASURED_OVERHEADS = "13,842 (a fresh config) to about 28,000"


def start_warning(bound: int, headroom_floor: int | None, overhead: int,
                  measured: str = MEASURED_OVERHEADS) -> str | None:
    """A one-line warning when the assumed window budget is below MIN_WINDOW_BUDGET, else None. It says
    what that costs: at bound 60,000 on 2.1.314 every epoch logged ``turn_room_low`` and a turn ran out of
    recall room after 5 searches (QA 2026-11-05), so "rotations will page often" undersold it. ``measured``
    is the CLI's measured overhead range, quoted when the budget may be 0 or less."""
    headroom = headroom_floor if headroom_floor is not None else DEFAULT_HEADROOM_FLOOR
    budget = bound - headroom - overhead
    if budget >= MIN_WINDOW_BUDGET:
        return None
    suggested = headroom + overhead + 2 * MIN_WINDOW_BUDGET
    sizing = f"bound {bound:,} - headroom {headroom:,} - typical fixed overhead {overhead:,}"
    if budget <= 0:
        # The overhead is the top of the measured range, and measured overheads run lower, so this is a
        # "may", not a "will".
        return (f"bound {bound:,} may leave no window budget ({sizing} = {budget:,} tokens; measured overheads "
                f"run from {measured}): the engine may keep only the new turn, requests may exceed "
                f"the bound, and recall may not fit at all. Use --bound {suggested:,} or more.")
    return (f"window budget about {budget:,} tokens ({sizing}) is below {MIN_WINDOW_BUDGET:,}: turns page out "
            f"almost at once and recalled pages will barely fit, if at all. Consider --bound {suggested:,} or more.")


class NoticeLog:
    """Session log wrapper: everything goes to the store; the engine's first bound_too_small event,
    first ``pdf_allowance`` and ``pdf_pages_uncertain`` (D60),
    first window budget under MIN_WINDOW_BUDGET, first ``mcp_tools_denied`` (D44), and first
    ``mcp_tools_missing`` (from a request, or logged at exit by ``report_missing_tools``) are also shown
    on stderr, once each; an exit-time ``mcp_tools_missing`` that repeats a mid-run one is shown once
    more, after the CLI exited. A store failure (``store_error``, ``page_conflict``) is shown once per cause
    with what to do about it, and so is a session log that cannot be written (D64).

    Between ``hold`` and ``release`` (while the CLI owns the terminal) the lines are kept and printed at
    ``release``, in order: a line written mid-run landed in Claude Code's input box and garbled the next
    redraw (D55). A repeat of a warning still held is not added."""

    def __init__(self, store: Any, stderr) -> None:
        self.store = store
        self.stderr = stderr
        self.shown: set[str] = set()
        self.held: list[str] | None = None
        self.held_keys: set[str] = set()
        self.start_text_tokens: int | None = None  # D69: the carried records the engine sent at the start

    def hold(self) -> None:
        if self.held is None:
            self.held, self.held_keys = [], set()

    def release(self) -> None:
        held, self.held = self.held or [], None
        for message in held:
            print(f"contexto: {message}", file=self.stderr, flush=True)

    def log(self, event: dict[str, Any]) -> None:
        # D64: store failures first, because on a full disk the write below fails as well.
        for key, message in fault_notices(event, getattr(self.store, "session_id", None)):
            self._once(key, "warning: " + message)
        # D69: read before the write below, so a failed write keeps the hint a later bound notice gives
        # (review B round 9, N23).
        inners = [event, *(event.get("transform_events") or [])]
        for inner in inners:
            if isinstance(inner, dict) and inner.get("event") == "start_text" and isinstance(inner.get("tokens"), int):
                self.start_text_tokens = inner["tokens"]
        try:
            self.store.log(event)
        except OSError as exc:
            self._once("log_unwritable", "warning: " + log_unwritable_message(exc))
            raise
        for inner in inners:
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
            if inner.get("event") == "pdf_pages_uncertain":
                self._once("pdf_pages_uncertain", "a PDF could not be read fully, so it is counted at the "
                           "maximum, 600 pages; earlier messages may be paged out for it, and stay recallable.")
            elif inner.get("event") == "pdf_allowance":
                # D60: the turn fits at a typical page cost; only the PDF's allowance does not. Dense pages
                # can cost more than typical ones, so this is not a promise that the request fits.
                self._once("pdf_allowance", "a large PDF is estimated generously (4,568 tokens a page, more than "
                           "most pages cost), so this turn kept only its own messages; the earlier ones are "
                           "recallable. With typical pages the turn may fit the bound as it is.")
            elif inner.get("event") == "bound_too_small" and inner.get("pdf_uncertain"):
                # D60: an uncertain PDF counts 600 pages; its real size, and so the bound it needs, is unknown.
                self._once("bound_too_small_pdf", "a PDF in this turn could not be read fully, so it is counted "
                           "at the maximum, 600 pages, and the turn does not fit the window; the engine keeps "
                           "only the new turn, and requests may exceed the bound. A larger --bound helps only if "
                           "the PDF is long.")
            elif inner.get("event") == "bound_too_small":
                # Rule 4 window_minimal (contract PR #50): the engine still rotates, to a window of
                # only the new turn, which can still put the request over the bound.
                bound = inner.get("bound")
                # D69: carried records sent at the session's start take part of the bound from the first
                # request; the notice says so, and how to turn them off.
                carried = ("" if not self.start_text_tokens else f" Records carried by project memory take about "
                           f"{self.start_text_tokens:,} tokens of the bound; --no-project-memory turns them off.")
                self._once("bound_too_small", (f"bound {bound:,} is too small: fixed overhead {overhead:,} leaves a "
                           f"window budget of {budget:,}; the engine keeps only the new turn, and requests may "
                           f"exceed the bound. Restart with a larger --bound."
                           if all(isinstance(v, int) for v in (bound, overhead, budget))
                           else "bound is too small for the measured fixed overhead; the engine keeps only the new "
                           "turn, and requests may exceed the bound.") + carried)
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


def claude_options(cli_args: list[str]) -> Iterator[tuple[int, str, str | None]]:
    """(index, option, attached value) of each option Claude Code reads, up to ``--``, as its Commander
    parser reads them. A long option's attached value is what follows ``=``; the value of one in
    ``CLAUDE_VALUE_OPTIONS`` given as the next argument is skipped even when it starts with ``-``
    (``--append-system-prompt --continue`` is a prompt, not a continue). Optional-value long options
    (``CLAUDE_OPTIONAL_VALUE_OPTIONS``) consume the next argument only when it is not another flag.
    A short-option cluster is split per ``CLAUDE_SHORT_OPTIONS`` (``-pc`` yields ``-p`` and ``-c``;
    ``-rabc`` yields ``-r`` with ``abc`` attached); an unknown letter ends it, as Claude Code then
    stops with an error."""
    i = 0
    while i < len(cli_args):
        arg = cli_args[i]
        if arg == "--":
            return
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            yield i, name, value if eq else None
            if not eq and name in CLAUDE_VALUE_OPTIONS:
                i += 1
            elif not eq and name in CLAUDE_OPTIONAL_VALUE_OPTIONS:
                if i + 1 < len(cli_args) and (cli_args[i + 1] == "-" or not cli_args[i + 1].startswith("-")):
                    i += 1
        elif arg.startswith("-") and len(arg) > 1:
            letters = arg[1:]
            while letters:
                letter, rest = letters[0], letters[1:]
                flag = CLAUDE_SHORT_OPTIONS.get(letter)
                if flag is None:
                    yield i, f"-{letters}", None
                    break
                if flag:
                    yield i, f"-{letter}", None
                    letters = rest
                    continue
                yield i, f"-{letter}", rest or None
                if not rest and letter in CLAUDE_SHORT_REQUIRED:
                    i += 1
                break
        i += 1


def resume_request(cli_args: list[str]) -> tuple[str, str | None] | None:
    """What Claude Code will resume: ``("continue", None)`` for ``-c``/``--continue`` (also in a cluster
    such as ``-pc``), ``("resume", id)`` for ``-r``/``--resume <id>``, ``--resume=<id>`` or ``-r<id>``,
    ``("resume", None)`` for ``--resume`` without a value (Claude Code's picker), else None. Only options
    ``claude_options`` names are read."""
    for i, name, attached in claude_options(cli_args):
        if name in ("-c", "--continue"):
            return "continue", None
        if name in ("-r", "--resume"):
            if attached is not None:
                return "resume", attached or None
            value = cli_args[i + 1] if i + 1 < len(cli_args) else None
            # Commander's dash-like test is a "-" and more, so a lone "-" is a session name (D59).
            return "resume", value if value and (value == "-" or not value.startswith("-")) else None
    return None


def forks(cli_args: list[str]) -> bool:
    """``--fork-session``: Claude Code gives the resumed or continued conversation a new id."""
    return any(name == "--fork-session" for _, name, _ in claude_options(cli_args))


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
    created: bool = False  # this launch created the session (removed again if the proxy cannot start, D57)


def choose_session(options: LaunchOptions, home: Path, cwd: Path, stderr) -> SessionChoice:
    """The session for this launch. ``--session`` wins and is never questioned. Without it, ``--resume
    <id>`` takes the session that served ``<id>`` (naming any other that served it too) and
    ``--continue`` the one that last served Claude Code in ``cwd``, unless that session is in use by
    another launch: two launches on one store share its epoch record and its recall room, which the
    contract leaves out of scope, so a new session opens instead. Every other resume gets a new session
    with a line on stderr saying why."""
    if options.session_id is not None:  # '' too: it is refused as an invalid id when opened (D57)
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
    if forks(options.claude_args):
        # Kept on purpose: the fork's history up to the fork point is that session's, and so are the pins.
        say(f"with --fork-session the fork shares Contexto session {session} with the conversation it forks "
            f"(pages, pins and recall room; switching between the two re-rotates). Pass --session NEW before "
            f"'claude' to give the fork its own store.")
    return SessionChoice(session, {s[2] for s in served if s[1] == session} if kind == "continue" else None)


@contextmanager
def choosing(home: Path, stderr=None) -> Iterator[None]:
    """One launch at a time picks a session automatically (reviewer A, PR #240 round 2): an exclusive
    ``flock`` on ``$CONTEXTO_HOME/choose.lock`` is held from the busy-session probe until the chosen
    session is open, with its shared ``live.lock`` taken. Two ``contexto run claude -c`` started at the
    same moment otherwise both probed the session before either held it, and shared it in 6 of 10 tries.
    The wait is bounded (``CHOOSE_LOCK_WAIT_S``); a lock that cannot be opened or taken in time only
    costs the serialization, never the launch. A wait past ``CHOOSE_LOCK_NOTICE_S``, and its timeout, are
    said on stderr, and Ctrl+C during it exits 130 like the version check, without a traceback (D59)."""
    out = stderr or sys.stderr
    fd = None
    try:
        try:
            private_dir(home, narrow=False)
            fd = open_private(home / CHOOSE_LOCK, os.O_RDWR | os.O_NOFOLLOW)
            started = time.monotonic()
            noticed = False
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    waited = time.monotonic() - started
                    if waited >= CHOOSE_LOCK_WAIT_S:
                        print(f"contexto: another contexto launch has held {CHOOSE_LOCK} for "
                              f"{CHOOSE_LOCK_WAIT_S:g} s; choosing without it, so two launches may pick the "
                              f"same session", file=out, flush=True)
                        break
                    if not noticed and waited >= CHOOSE_LOCK_NOTICE_S:
                        noticed = True
                        print("contexto: waiting for another contexto launch to choose its session", file=out,
                              flush=True)
                    time.sleep(0.01)
        except OSError:
            pass
        except KeyboardInterrupt:
            print("contexto: interrupted", file=out, flush=True)
            raise Terminated(signal.SIGINT) from None  # run_claude exits 128 + SIGINT
        yield
    finally:
        if fd is not None:
            os.close(fd)  # releases the lock


def pick_session(options: LaunchOptions, home: Path, cwd: Path, stderr) -> tuple[SessionChoice, Any]:
    """Choose the session (``choose_session``) and open it. A choice made automatically (a resume
    without ``--session``) is made under ``choosing``, so a concurrent launch sees this one's live lock."""
    automatic = options.session_id is None and resume_request(options.claude_args) is not None
    with choosing(home, stderr) if automatic else nullcontext():
        choice = choose_session(options, home, cwd, stderr)
        check_mcp_values(choice.session_id, home, sys.executable)
        try:
            choice.created = not session_dir(home, choice.session_id).exists()
            store = open_session(home, choice.session_id, cwd)
        except ValueError as exc:  # an id no session can have: a usage error
            raise LaunchError(f"cannot open session {choice.session_id[:40]!r}: {exc}", EXIT_USAGE) from exc
        except OSError as exc:  # the store's filesystem: Contexto cannot start (D57)
            raise LaunchError(f"cannot open session {choice.session_id!r}: {exc}", EXIT_PROXY) from exc
    return choice, store


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
    if options.quiet:
        args.append("--quiet")
    if not options.statusline:
        args.append("--no-statusline")
    if not options.project_memory:
        args.append("--no-project-memory")
    if not options.in_turn_paging:
        args.append("--no-in-turn-paging")
    return shlex.join([*args, "claude", "--resume", cli_session])


def open_session(home: Path, session_id: str, workdir: Path) -> Any:
    """Create the session and record the directory claude runs in, before claude starts (lane B
    checks file sources against it for staleness). An id that is not a session but starts existing ones
    is refused (D57): the readers take a prefix, so a new session under it would hide the one meant. A
    ``CONTEXTO_HOME`` that is not a directory is refused here too, so ``run codex`` exits 2 like claude."""
    check_home(home)
    starts = [] if session_dir(home, session_id).exists() else starting_with(home, session_id)
    if starts:
        raise LaunchError(f"--session {session_id!r} is not a session but the start of {named(starts)}; give the "
                          "full id to resume one, or a different id for a new session", EXIT_USAGE)
    store = SessionStore(home, session_id)
    store.set_workdir(workdir)
    return store


def project_memory_wanted(options: LaunchOptions, stderr=None, env: dict[str, str] | None = None) -> bool:
    """Whether this launch asks for project memory (D69): explicit CLI flag wins, else
    ``CONTEXTO_PROJECT_MEMORY`` (``contexto.paths.project_memory_env``), else ``config.toml``, else on.
    A value of the variable Contexto does not know gets one line on stderr naming the ones it does, and
    changes nothing. Shared by claude and codex."""
    if options.project_memory_explicit or options.project_memory_source == "cli":
        return options.project_memory
    on, unknown = project_memory_env(env)
    if unknown is not None:
        shown = unknown if len(unknown) <= ENV_VALUE_SHOWN else unknown[:ENV_VALUE_SHOWN] + "..."
        print(f"contexto: {PROJECT_MEMORY_ENV}={shown!r} is not a value Contexto knows and is ignored: 0, false, "
              f"off or no turns project memory off; 1, true, on, yes or unset leaves it on",
              file=stderr or sys.stderr, flush=True)
        return options.project_memory
    active = env if env is not None else os.environ
    if options.project_memory_source == "config" and (active.get(PROJECT_MEMORY_ENV) or "").strip():
        return on
    return options.project_memory and on


def record_project_memory(store: Any, wanted: bool) -> None:
    """Record a launch that turned project memory off in the session (``project_memory: false``), before
    the proxy starts and before the CLI can start the MCP server: both read it from the store. A launch
    that wants memory records nothing, so a session that never recorded it stays as it is and a resumed
    session that recorded ``false`` stays off (``true`` is never written over it).

    A session that cannot record ``false`` would run with memory on against what the user asked, so that
    is a ``LaunchError`` and the CLI is not started. A store that has no project memory to turn off (a
    test's stand-in) is left alone. Shared by claude and codex."""
    turn = getattr(store, "set_project_memory", None)
    if wanted or not callable(turn):
        return
    try:
        turn(False)
    except Exception as exc:
        raise LaunchError(f"cannot record that project memory is off for this session: {exc}", EXIT_PROXY) from exc


def project_memory_line(root: Path, earlier: int, carried: int, sources: int, skipped: int,
                        sent: int | None = None) -> str | None:
    """The start line about project memory, None when the project has no earlier session: how many records
    this session carries and from how many of the ``earlier`` sessions (``sources``), or that it carries
    none and that the earlier sessions can be searched; when only ``sent`` of the ``carried`` records fit
    the pin budget, that the rest did not; how many
    of the project's sessions were ``skipped`` as damaged or unreadable; the project's directory; and how
    to list the records or turn this off. A path that is not printable as it is (a line break, an escape
    sequence) is quoted, so it cannot forge a line or drive the terminal."""
    if earlier <= 0:
        return None
    path = str(root)
    where = path if path.isprintable() else repr(path)
    sessions = f"{earlier} earlier session{'' if earlier == 1 else 's'}"
    records = f"{carried} record{'' if carried == 1 else 's'} carried"
    if sent is not None and sent < carried:
        records = (f"{sent} of {carried} records carried ({carried - sent} did not fit the pin budget; contexto state "
                   "lists them all)")
    if carried:
        text = f"{records} from {sources} of {sessions} in {where}"
    else:  # nothing carried: the earlier sessions are still there to search (D69)
        text = f"{records} from {sessions} in {where}; {'it' if earlier == 1 else 'they'} can be searched"
    if skipped:
        text += f"; {skipped} session{'' if skipped == 1 else 's'} skipped as damaged or unreadable"
    return f"project memory: {text} (list: contexto project show; off: --no-project-memory)"


def engine_pin_budget(config: dict[str, int | float] | None = None) -> int:
    """The pin budget of the engine ``config`` builds (``contexto.rewrite.Config``); 6,000 without the engine."""
    try:
        from contexto.rewrite import Config
    except ImportError:
        return DEFAULT_PIN_BUDGET
    return Config(**(config or {})).pin_budget


def project_memory_notice(store: Any, stderr=None, pin_budget: int | None = None) -> None:
    """At launch (D69): log one content-free ``project_memory`` event (whether memory is on for the
    session, how many earlier sessions its project has, how many records it carries and from how many
    sessions, how many of the project's sessions were skipped as damaged and how many session directories
    of any project were left out as unreadable) and, when the project has an earlier session, print
    ``project_memory_line``. Its record count is what the first request sends: the carried section the
    engine renders within ``pin_budget`` (default the engine's, ``engine_pin_budget``), counted from that
    render, with the total beside it when the budget cuts it.

    It runs with the other start notices, before the CLI starts: the terminal is still the launcher's
    then, and a line written while the CLI runs lands inside its TUI (D55). It never fails a launch:
    any error is that event with the error's class in place of the counts, and no line. The line names
    the project's directory, which is the user's own, on their terminal; the log never does. Reading
    the project here also fills this process's cache of the earlier sessions, which the engine in the
    same process reads again at the first request. A store with no ``project`` (a test's stand-in) gets
    neither. Shared by claude and codex."""
    view = getattr(store, "project", None)
    if not callable(view):
        return
    line = moved = None
    try:
        directory = getattr(store, "dir", None)
        recorded = read_meta(directory) if directory is not None else None
        on = (recorded or {}).get("project_memory") is not False  # as the store reads it
        moved = moved_line(store, recorded) if on else None
        event: dict[str, Any] = {"event": "project_memory", "on": on, "earlier": 0, "carried": 0,
                                 "carried_sessions": 0, "skipped": 0, "unreadable": 0}
        project = view() if on else None
        if not on and was_carrying(store):
            line = ("project memory: off for this session; the records it carried are not sent again from its next "
                    "request")
        if project is not None:
            carried = project.carried()
            event.update(earlier=len(project.earlier), carried=len(carried),
                         carried_sessions=len({item.session for item in carried}),
                         skipped=len(project.skipped), unreadable=project.unreadable)
            sent = None
            if carried and event["earlier"]:
                budget = engine_pin_budget() if pin_budget is None else pin_budget
                text = render_carried(carried, budget)  # what the store's carried_text gives the engine
                sent = sum(1 for part in text.split("\n") if part.startswith("- "))
            line = project_memory_line(project.root, event["earlier"], event["carried"], event["carried_sessions"],
                                       event["skipped"], sent)
    except Exception as exc:
        event, line = {"event": "project_memory", "error": type(exc).__name__}, None
    try:
        store.log(event)
    except Exception:  # a diagnostic record; never stop a launch for it
        pass
    for text in (moved, line):
        if text:
            print(f"contexto: {text}", file=stderr or sys.stderr, flush=True)


def was_carrying(store: Any) -> bool:
    """Whether the session's epoch record holds a start text (D69): a resumed session that was sending
    carried records, which the engine stops sending from the resume on (rule 19)."""
    reader = getattr(store, "epoch_record", None)
    try:
        record = reader() if callable(reader) else None
    except Exception:  # an unreadable record resumes nothing, so nothing carried is sent from it
        return False
    return isinstance(record, dict) and isinstance(record.get("start_text"), str) and bool(record["start_text"])


def moved_line(store: Any, meta: dict[str, Any] | None) -> str | None:
    """D69: the line for a session resumed from a directory of another project: it stays in the project
    it was started in (``SessionStore.set_workdir`` never moves it), which the line names. None otherwise."""
    workdir = store.workdir() if callable(getattr(store, "workdir", None)) else None
    root = recorded_root(meta or {})
    if workdir is None or root is None:
        return None
    here = project_root(workdir)
    if str(here) == str(root):
        return None
    shown = [str(path) if str(path).isprintable() else repr(str(path)) for path in (root, here)]
    session = getattr(store, "session_id", "")
    name = session if isinstance(session, str) and session.isprintable() else repr(session)
    return (f"project memory: session {name} stays in the project it was started in, {shown[0]}; it neither carries "
            f"from nor gives to {shown[1]}")


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
        json.dump(payload, f, allow_nan=False)
    return path


def contexto_tool_rules() -> list[str]:
    """D44: one permission rule per Contexto MCP tool, by its exact name (``mcp__contexto__contexto_recall``
    and the other four). They cover only the server ``mcp_payload`` names, and only the tools it serves."""
    return [f"mcp__{MCP_SERVER_NAME}__{tool['name']}" for tool in MCP_TOOLS]


def settings_payload(proxy_url: str, hint_headers: str = STRIPPED, *,
                     base_url_key: str = BASE_URL_KEY) -> dict[str, Any]:
    """The flag-level ``--settings`` content: the proxy's base URL (``ANTHROPIC_BASE_URL``, plus
    ``ANTHROPIC_VERTEX_BASE_URL`` or ``ANTHROPIC_FOUNDRY_BASE_URL`` when intercepting Vertex AI or
    Microsoft Foundry), the request for Claude Code's gateway hint headers (D70), and an allow rule
    for each of Contexto's own MCP tools (D44).

    ``hint_headers`` is the launch's ``contexto.hints.hint_headers_state``. With ``off`` the hints are
    not asked for. The state is written beside the request as ``CONTEXTO_HINT_HEADERS``: Claude Code
    applies this ``env`` block to its own environment, every process the session starts inherits it,
    and a nested ``contexto run claude`` reads the marker to tell Contexto's ``1`` from the user's."""
    if hint_headers not in HINT_STATES:
        raise ValueError(f"unknown hint header state {hint_headers!r}")
    env = {BASE_URL_KEY: proxy_url}
    if base_url_key != BASE_URL_KEY:
        env[base_url_key] = proxy_url
        if base_url_key == FOUNDRY_BASE_URL_KEY:
            # Claude Code's Foundry SDK rejects having both ANTHROPIC_FOUNDRY_BASE_URL and
            # ANTHROPIC_FOUNDRY_RESOURCE set ("baseURL and resource are mutually exclusive",
            # verified on 2.1.328); clearing the resource in --settings env routes cleanly through
            # Contexto's proxy to the resolved resource upstream.
            env[FOUNDRY_RESOURCE_KEY] = ""
    if hint_headers != OFF:
        env[HINT_HEADERS_KEY] = "1"
    env[HINT_MARKER_KEY] = hint_headers
    return {"env": env, "permissions": {"allow": contexto_tool_rules()}}


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


def session_env(session_id: str, env: dict[str, str] | None = None) -> dict[str, str]:
    """``env`` (default the process environment) with ``CONTEXTO_SESSION`` set to the launched session."""
    return {**(os.environ if env is None else env), "CONTEXTO_SESSION": session_id}


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
            print(f"contexto: {exc}", file=stderr)  # one prefix for claude and codex (D64)
            return exc.code
        except Terminated as exc:
            return 128 + exc.signum


def _cli_bool(args: Any, true_attr: str, false_attr: str) -> bool | None:
    """Explicit CLI boolean choice from paired flags (None when neither flag was passed)."""
    if getattr(args, true_attr, False):
        return True
    if getattr(args, false_attr, False):
        return False
    return None


def _resolve_bool(cli_value: bool | None, env_set: bool, env_value: bool, config_value: bool | None,
                  default: bool) -> bool:
    """Precedence: explicit CLI > explicit env > config.toml > built-in default."""
    if cli_value is not None:
        return cli_value
    if env_set:
        return env_value
    if config_value is not None:
        return config_value
    return default


def options_from_args(args: Any, *, config: FileConfig | None = None,
                      env: Mapping[str, str] | None = None) -> LaunchOptions:
    """LaunchOptions from the ``contexto run`` argparse namespace (also used by contexto.codex).
    Precedence: explicit CLI flags > environment variables > ``config.toml`` > built-in defaults."""
    active_env = os.environ if env is None else env
    cfg = config if config is not None else FileConfig()

    bound_arg = getattr(args, "bound", None)
    bound = bound_arg if bound_arg is not None else (cfg.bound if cfg.bound is not None else DEFAULT_BOUND)

    allow_large = bool(getattr(args, "allow_large_bound", False)) or bool(cfg.allow_large_bound or False)

    wf_arg = getattr(args, "window_fraction", None)
    window_fraction = wf_arg if wf_arg is not None else cfg.window_fraction

    hf_arg = getattr(args, "headroom_floor", None)
    headroom_floor = hf_arg if hf_arg is not None else cfg.headroom_floor

    observe_thinking = bool(getattr(args, "observe_thinking", False)) or bool(cfg.observe_thinking or False)

    vc_cli = _cli_bool(args, "version_check_on", "no_version_check")
    vc_env_set = bool((active_env.get("CONTEXTO_NO_VERSION_CHECK") or "").strip())
    version_check = _resolve_bool(vc_cli, vc_env_set, not version_check_disabled(active_env), cfg.version_check, True)

    q_cli = _cli_bool(args, "quiet", "quiet_off")
    q_env_set = bool((active_env.get(QUIET_ENV) or "").strip())
    quiet = _resolve_bool(q_cli, q_env_set, quiet_from_env(active_env), cfg.quiet, False)

    sl_cli = _cli_bool(args, "statusline_on", "no_statusline")
    sl_env_set = bool((active_env.get(NO_STATUSLINE_ENV) or "").strip())
    if sl_cli is not None:
        statusline = sl_cli
        statusline_source = "flag" if not statusline else "cli"
    elif sl_env_set:
        statusline = not statusline_opted_out(active_env)
        statusline_source = "env" if not statusline else None
    elif cfg.statusline is not None:
        statusline = cfg.statusline
        statusline_source = "config" if not statusline else None
    else:
        statusline = True
        statusline_source = None

    pm_cli = _cli_bool(args, "project_memory_on", "no_project_memory")
    pm_env_set = bool((active_env.get(PROJECT_MEMORY_ENV) or "").strip())
    pm_env_on, pm_env_unknown = project_memory_env(dict(active_env)) if pm_env_set else (True, None)
    if pm_cli is not None:
        project_memory, pm_source = pm_cli, "cli"
    elif pm_env_set and pm_env_unknown is None:
        project_memory, pm_source = pm_env_on, "env"
    elif cfg.project_memory is not None:
        project_memory, pm_source = cfg.project_memory, "config"
    else:
        project_memory, pm_source = True, None

    itp_cli = _cli_bool(args, "in_turn_paging_on", "no_in_turn_paging")
    itp_env_set = bool((active_env.get(IN_TURN_PAGING_ENV) or "").strip())
    in_turn = _resolve_bool(itp_cli, itp_env_set, in_turn_paging(False, dict(active_env)), cfg.in_turn_paging, True)

    return LaunchOptions(
        claude_args=list(getattr(args, "cli_args", None) or []), bound=bound, session_id=args.session,
        upstream=args.upstream, observe_thinking=observe_thinking, allow_large_bound=allow_large,
        window_fraction=window_fraction, headroom_floor=headroom_floor,
        version_check=version_check, quiet=quiet, statusline=statusline,
        project_memory=project_memory, in_turn_paging=in_turn,
        config_upstream=cfg.upstream, statusline_source=statusline_source,
        project_memory_explicit=pm_cli is not None, project_memory_source=pm_source,
        version_check_explicit=vc_cli is not None)


def check_options(options: LaunchOptions) -> None:
    check_bound(options.bound, options.allow_large_bound)
    if options.window_fraction is not None and not 0 < options.window_fraction < 1:
        raise LaunchError(f"--window-fraction must be between 0 and 1, got {options.window_fraction}", EXIT_USAGE)
    # The default floor is checked like an explicit one (D57): the engine's headroom is never below it.
    if options.headroom_floor is None:
        if options.bound <= DEFAULT_HEADROOM_FLOOR:
            raise LaunchError(
                f"--bound {options.bound:,} is not above the headroom floor ({DEFAULT_HEADROOM_FLOOR:,}, the "
                f"default), so no turn fits: use a larger --bound, or a smaller --headroom-floor", EXIT_USAGE)
    elif not 0 < options.headroom_floor < options.bound:
        raise LaunchError(f"--headroom-floor must be above 0 and below the bound ({options.bound:,}), "
                          f"got {options.headroom_floor:,}", EXIT_USAGE)


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
                observe_thinking: bool, *, stderr=None, cli: str | None = None,
                strip_hint_headers: bool = False) -> tuple[ContextoProxy, str]:
    """Build the rewrite engine for ``store`` and start the proxy on 127.0.0.1 with a free port.
    Returns ``(proxy, engine_name)``; the caller stops the proxy. Shared by claude and codex. With
    ``cli="claude"`` the engine is wrapped in a ``SessionRecorder`` (``proxy.transform``), so the
    session records which Claude Code sessions it served (D55). ``strip_hint_headers`` is the proxy's
    (D70); Codex sends no such headers and leaves it off."""
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
                              upstream_source=upstream_source, affinity_key=key,
                              strip_hint_headers=strip_hint_headers)
    except ValueError as exc:
        raise LaunchError(f"invalid upstream (from {upstream_source}): {exc}", EXIT_USAGE) from exc
    try:
        proxy.start(0)
    except OSError as exc:
        raise LaunchError(f"cannot start the proxy: {exc}", EXIT_PROXY) from exc
    if engine_name == "identity":
        store.log({"event": "engine_unavailable", "detail": "contexto.rewrite not installed; identity transform"})
    return proxy, engine_name


def check_cli_version(cli: str, binary: str, store: Any, *, enabled: bool = True, stderr=None,
                      installed: tuple[str | None, str | None] | None = None,
                      explicit: bool = False) -> bool:
    """D38: log the installed CLI's version (``<binary> --version``, a few seconds at most) and print one
    warning line when Contexto was not tested with it. Never raises; a CLI whose version cannot be read
    is logged, not warned about. Returns False only when the user pressed Ctrl+C during the check: the
    caller then exits ``EXIT_INTERRUPTED`` through its normal cleanup, without starting the CLI and
    without a traceback. Shared by claude and codex; ``installed`` is a version already read (D59)."""
    out = stderr or sys.stderr
    try:
        env = {k: v for k, v in os.environ.items() if k != "CONTEXTO_NO_VERSION_CHECK"} if explicit else None
        event, warning = launch_check(cli, binary, enabled=enabled, env=env, installed=installed)
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
    while the child starts is held and forwarded once it exists, not lost or raised mid-spawn; if the
    spawn fails, such a signal decides the exit code (128 + N) over the 127.
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
            return EXIT_NOT_FOUND if forwarding.received is None else 128 + forwarding.received
        if watchdog is not None:
            watchdog.watch(child.pid)
        forwarding.attach(child)
        return exit_code(child.wait())
    finally:
        # First the child: reaped, then forgotten by the watchdog at once (its pid may be reused), then
        # the handlers restored.
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
        for signum, handler in previous.items():
            signal.signal(signum, handler)


INFO_ARGS = (["--version"], ["-v"], ["--help"], ["-h"])  # claude prints and exits: no model request


def _oauth_guidance_line(upstream: Upstream, cwd: Path, extra_settings: dict[str, Any] | None, *,
                         files: SettingsFiles | None = None, env: Mapping[str, str] | None = None,
                         home: Path | None = None) -> str | None:
    """When Claude Pro/Max OAuth auth is detected (non-invasively, without reading secrets) and no
    explicit API key or key helper overrides it, return a concise note only when the upstream is a
    non-Anthropic host (not loopback), explaining transparent ``Authorization: Bearer`` passthrough."""
    if upstream.provider is not None:
        return None
    env = os.environ if env is None else env
    home = home or Path.home()
    files = files or SettingsFiles()
    oauth = claude_oauth_detected(env=env, cwd=cwd, home=home, files=files, extra_settings=extra_settings)
    if oauth is None:
        return None
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        val = claude_env_value(key, env=env, cwd=cwd, home=home, files=files, extra_settings=extra_settings)
        if val.value and val.value.strip():
            return None
    if claude_key_helper(cwd, home, env, trusted_only=False, files=files, extra_settings=extra_settings).value:
        return None
    host = (urlsplit(upstream.url).hostname or "").lower()
    if not host or host in ("127.0.0.1", "localhost", "::1") or is_vendor_host(host):
        return None
    return (f"Claude OAuth detected ({oauth}) with a custom upstream: Contexto forwards Authorization: Bearer "
            "and anthropic-beta headers transparently (use --upstream https://api.anthropic.com for direct "
            "Claude Pro/Max OAuth, or set ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN for a custom gateway).")


def _run(options: LaunchOptions, stderr, signals: LaunchSignals) -> int:
    check_misplaced_options("claude", options.claude_args)
    claude_args, user_settings = extract_user_settings(options.claude_args)
    check_options(options)
    claude = shutil.which(options.claude_bin)
    if claude is None:
        raise LaunchError(cli_not_found(options.claude_bin), EXIT_NOT_FOUND)
    if options.claude_args in INFO_ARGS:  # nothing to bound, so no session or proxy (D57)
        return run_child([claude, *options.claude_args], stderr=stderr)
    # D70: the provider check, the upstream, the hint header state and OAuth check, from one read of each file.
    settings_files = SettingsFiles()
    upstream, hint_headers = claude_launch_settings(options.upstream, Path.cwd(),
                                                    config_upstream=options.config_upstream,
                                                    extra_settings=user_settings,
                                                    files=settings_files)
    check_not_service(upstream, contexto_home(), "contexto run claude")
    check_upstream(upstream)
    if upstream.ignored:  # CLAUDE_CODE_IGNORE_PERSISTED_TRUST is set: as Claude Code -p, no project base URL (D52)
        note = untrusted_note(f"env.{upstream.base_url_key}", upstream.ignored, "Pass --upstream", os.environ)
        print(f"contexto: {note}", file=stderr)
    home = contexto_home()
    check_home(home)
    memory = project_memory_wanted(options, stderr)
    choice, store = pick_session(options, home, Path.cwd(), stderr)
    session_id = choice.session_id
    config = engine_config(options)
    # D70: the --settings file below turns the hint headers on for the engine, and the proxy removes
    # them, unless the user had them on already (forwarded) or turned them off (not asked for).
    try:
        record_project_memory(store, memory)  # before anything reads the store: the proxy, the MCP server
        proxy, engine_name = start_proxy(store, upstream.url, upstream.source, config, options.observe_thinking,
                                         stderr=stderr, cli="claude", strip_hint_headers=hint_headers == STRIPPED)
    except LaunchError:
        if choice.created:  # a launch that never started leaves no empty session (D57)
            discard_new_session(store)
        raise
    notices, recorder = proxy.session_log, proxy.transform
    tmp: Path | None = None
    watchdog: Watchdog | None = None
    code = EXIT_PROXY
    started: float | None = None  # when claude was started (D64: a launch that never ran it has no summary)
    try:
        tmp = Path(tempfile.mkdtemp(prefix="contexto-run-"))  # mode 0700, removed on exit
        watchdog = Watchdog.start(tmp, store)
        # D64: a statusLine only when it cannot replace one of the user's; the note says which it was.
        status_line, status_note = launch_status_line(session_id, home, Path.cwd(), enabled=options.statusline,
                                                      off_source=options.statusline_source)
        merged_settings, user_had_statusline = merge_settings(
            user_settings, settings_payload(proxy.url, hint_headers, base_url_key=upstream.base_url_key), status_line
        )
        if user_had_statusline:
            status_note = "user_flag"
        settings = write_private_json(tmp, "settings.json", merged_settings)
        mcp = write_private_json(tmp, "mcp.json", mcp_payload(session_id, home))
        store.log({"event": "launch", "cli": "claude", "session": session_id, "config": config,
                   "engine": engine_name, "upstream_source": upstream.source,
                   "observe_thinking": options.observe_thinking, "user_arg_count": len(options.claude_args),
                   "affinity_header": proxy.affinity_header is not None,
                   "hint_headers": hint_headers, "statusline": status_note})
        print(f"contexto: session {session_id} (bound {options.bound:,}, upstream {upstream.source})",
              file=stderr, flush=True)
        skip_msg = statusline_skip_notice(status_note)
        if skip_msg and (not options.quiet or status_note.startswith(("unreadable:", "error", "unsafe_path"))):
            print(f"contexto: {skip_msg}", file=stderr, flush=True)
        oauth_note = _oauth_guidance_line(upstream, Path.cwd(), user_settings, files=settings_files)
        if oauth_note:
            print(f"contexto: {oauth_note}", file=stderr, flush=True)
        project_memory_notice(store, stderr, engine_pin_budget(config))  # D69: what it carries, or nothing
        if not check_cli_version("claude", claude, store, enabled=options.version_check, stderr=stderr,
                                 explicit=options.version_check_explicit):
            code = EXIT_INTERRUPTED
            return code
        warning = start_warning(options.bound, options.headroom_floor, assumed_overhead())
        if warning:
            print(f"contexto: {warning}", file=stderr, flush=True)
            store.log({"event": "window_budget_warning", "bound": options.bound,
                       "assumed_overhead": assumed_overhead()})
        notices.hold()  # claude owns the terminal until it exits (D55)
        started = time.monotonic()
        # The CLI's shell commands (`contexto state`, `contexto search`) then name this session (D57).
        code = run_child(claude_argv(claude, settings, mcp, claude_args), session_env(session_id),
                         stderr=stderr, watchdog=watchdog)
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
        if tmp is not None:  # before "done": a SIGKILL in between still leaves the watchdog to remove it
            shutil.rmtree(tmp, ignore_errors=True)
        if watchdog is not None:
            watchdog.done()
        try:
            notices.log({"event": "exit", "code": code})
        except OSError:  # D64: a full disk, already said by the NoticeLog; claude's exit code stands
            pass
        print_exit_summary(store, "claude", started, cli_args=options.claude_args, quiet=options.quiet,
                           log_incomplete="log_unwritable" in notices.shown, stderr=stderr, code=code)
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
                  f"{', '.join(others)}; its earlier pages and pins are there, and a later resume picks whichever "
                  f"session served it last unless you pass --session ID.", file=stderr, flush=True)
    print(f"contexto: resume this conversation through Contexto with: "
          f"{resume_command(options, session_id, last)}", file=stderr, flush=True)


class _ChildSignals:
    """``run_child``'s handlers: keep the launcher alive through the user's Ctrl+C and forward
    termination to the child, the forwarded signals as they are and the other terminating ones as
    SIGTERM. A signal that arrives before the child exists is kept and sent by ``attach``. Python
    handlers (not SIG_IGN) are used, so nothing is inherited by the child."""

    def __init__(self) -> None:
        self.child: subprocess.Popen | None = None
        self.pending: int | None = None  # the signal to send once the child exists
        self.received: int | None = None  # the signal the launcher got before the child existed

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

    def _forward(self, received: int, frame: Any) -> None:
        signum = received if received in FORWARDED_SIGNALS else signal.SIGTERM
        if self.child is None:
            self.received = self.received or received
            self.pending = signum
        else:
            self._send(signum)

    def _send(self, signum: int) -> None:
        if self.child is not None and self.child.poll() is None:
            self.child.send_signal(signum)
