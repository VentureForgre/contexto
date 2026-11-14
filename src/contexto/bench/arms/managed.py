"""Organization-managed Claude Code settings that would change what a Claude Code arm runs.

Managed settings outrank every other settings source, the ``--settings`` file ``contexto run`` passes
included (D52; ``contexto.upstream`` says so and does not read them). A managed setting can send a
Claude Code arm around the meter (a base URL, a key helper, a login method), so its spend would not
be counted or guarded, or change the arm without the manifest saying so: turn its compaction off,
deny the file-read tool, pick another model, block or add MCP servers (Contexto's is one), add or
turn off hooks, or change the system prompt. The live run refuses to start when any setting in
``REFUSED``, or an ``env`` entry that configures Claude Code, is set; a unit about to start checks again.
A managed MCP configuration (``managed-mcp.json``) takes control of MCP servers and is refused too.

Where Claude Code 2.1.322 reads them (read in its bundle): ``managed-settings.json`` and the
``managed-settings.d/*.json`` drop-ins in ``/Library/Application Support/ClaudeCode`` (macOS),
``/etc/claude-code`` (Linux) or ``C:\\Program Files\\ClaudeCode`` (Windows), and on macOS the managed
preferences ``/Library/Managed Preferences/<user>/com.anthropic.claudecode.plist`` and
``/Library/Managed Preferences/com.anthropic.claudecode.plist``. Not read here: the Windows
registry policies and settings an organization's server sends. Those are why every Claude Code turn
must also have at least one metered request (the ``unmetered`` stop).

What is reported names the source by its kind ("the managed settings file", "a managed settings
drop-in") and the setting by its key, never a value, a file name an organization chose, a user name
or a home directory.
"""

from __future__ import annotations

import getpass
import json
import plistlib
import sys
from pathlib import Path
from typing import Any

FILE_NAME = "managed-settings.json"
MCP_FILE_NAME = "managed-mcp.json"
DROP_IN_DIR = "managed-settings.d"
PLIST_ID = "com.anthropic.claudecode"
# Every top-level managed setting the live run refuses when it is set (to anything but an empty value),
# by what it would change. Each would make a Claude Code arm differ from what the manifest records
# (cli_environment, plain_compaction, arm_differences), or send it around the meter. Keys are from
# Claude Code's settings reference (read 2026-11-13). A setting not listed here is allowed: it
# applies to both Claude Code arms alike and does not touch what this benchmark measures.
REFUSED = (
    ("where requests go and with what credential",
     ("apiKeyHelper", "forceLoginMethod", "forceLoginOrgUUID", "awsAuthRefresh", "awsCredentialExport")),
    # The plain arm's compaction is CLAUDE_CODE_AUTO_COMPACT_WINDOW; these could turn it off or move it.
    ("compaction", ("autoCompactEnabled", "autoCompactWindow", "modelSettings")),
    # Both arms rely on permissions.allow ["Read"] in their own settings file and on --settings rules.
    ("permissions", ("permissions", "allowManagedPermissionRulesOnly", "autoMode", "disableAutoMode")),
    # --model names the model; these could replace, restrict or tune it.
    ("the model", ("model", "availableModels", "availableModelsMatch", "deniedModels", "enforceAvailableModels",
                   "fallbackModel", "modelOverrides", "effortLevel", "maxEffortLevel", "alwaysThinkingEnabled",
                   "fastMode", "advisorModel", "agent", "promptCacheTtl", "subagentPromptCacheTtl")),
    # The contexto arm's MCP server comes from --mcp-config; these could block it or add others.
    ("MCP servers", ("allowedMcpServers", "deniedMcpServers", "allowManagedMcpServersOnly", "managedMcpServers",
                     "enableAllProjectMcpServers", "enabledMcpjsonServers", "disabledMcpjsonServers",
                     "strictPluginOnlyCustomization", "disableSideloadFlags")),
    ("hooks", ("hooks", "disableAllHooks", "allowManagedHooksOnly")),
    ("the system prompt", ("outputStyle", "claudeMd", "language", "includeGitInstructions")),
)
REFUSED_KEYS = tuple(key for _what, keys in REFUSED for key in keys)
# Environment variables a managed ``env`` may not set: every one that configures Claude Code, a
# credential or a route (the arms strip or set these), and the proxy variables.
ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "CLAUDECODE", "AWS_", "GOOGLE_", "VERTEX_", "CLOUD_ML_")
ENV_NAMES = frozenset({"DISABLE_AUTO_COMPACT", "DISABLE_COMPACT", "DISABLE_AUTOUPDATER", "DISABLE_PROMPT_CACHING",
                       "FORCE_PROMPT_CACHING_5M", "MAX_THINKING_TOKENS", "ENABLE_TOOL_SEARCH", "HTTP_PROXY",
                       "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})


def managed_directory(platform: str | None = None) -> Path:
    platform = platform or sys.platform
    if platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if platform.startswith("win"):
        return Path("C:\\Program Files\\ClaudeCode")
    return Path("/etc/claude-code")


def sources(platform: str | None = None) -> list[tuple[str, Path]]:
    """Every managed-settings source this module reads, as (what it is, path). The label never holds a
    user name."""
    platform = platform or sys.platform
    directory = managed_directory(platform)
    out = [("the managed settings file", directory / FILE_NAME)]
    try:
        drop_ins = sorted(p for p in (directory / DROP_IN_DIR).glob("*.json") if p.is_file())
    except OSError:
        drop_ins = []
    out += [("a managed settings drop-in", p) for p in drop_ins]  # an organization names these files
    out.append(("the managed MCP configuration", directory / MCP_FILE_NAME))
    if platform == "darwin":
        try:
            user = getpass.getuser()
        except (OSError, KeyError, ImportError):
            user = ""
        if user:
            out.append(("the per-user managed preferences",
                        Path("/Library/Managed Preferences") / user / f"{PLIST_ID}.plist"))
        out.append(("the device-level managed preferences", Path("/Library/Managed Preferences") / f"{PLIST_ID}.plist"))
    return out


def read(path: Path) -> dict[str, Any] | None:
    """A source's settings, or None when it does not exist or cannot be read as settings."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if path.suffix == ".plist":
        try:
            settings: Any = plistlib.loads(data)
        except (plistlib.InvalidFileException, ValueError, OverflowError):
            return None
        nested = settings.get("Settings") if isinstance(settings, dict) else None
        if isinstance(nested, str):
            try:
                nested = json.loads(nested)
            except ValueError:
                nested = None
        if isinstance(nested, dict):
            settings = {**settings, **nested}
    else:
        try:
            settings = json.loads(data)
        except ValueError:
            return None
    return settings if isinstance(settings, dict) else None


def env_overrides(env: Any) -> list[str]:
    if not isinstance(env, dict):
        return []
    return sorted(name for name in env if isinstance(name, str)
                  and (name.upper().startswith(ENV_PREFIXES) or name.upper() in ENV_NAMES))


def problems(found: list[tuple[str, Path]] | None = None) -> list[str]:
    """What the managed settings on this machine set that ``REFUSED`` or ``env_overrides`` names, one
    sentence per source, by key only; empty when nothing does."""
    out = []
    for label, path in sources() if found is None else found:
        settings = read(path)
        if not settings:
            continue
        keys = [key for key in REFUSED_KEYS if settings.get(key) not in (None, "", [], {})]
        if path.name == MCP_FILE_NAME and settings.get("mcpServers") not in (None, {}):
            keys.append("mcpServers")
        names = env_overrides(settings.get("env"))
        if keys or names:
            parts = ([", ".join(keys)] if keys else []) + ([f"env {', '.join(names)}"] if names else [])
            out.append(f"{label} sets {'; '.join(parts)}")
    return out


def refusal(found: list[str]) -> str:
    return ("organization-managed Claude Code settings on this machine would change what the contexto and plain "
            "arms run, or send their requests around the benchmark's meter: " + "; ".join(found)
            + ". Run the benchmark on a machine without them, or with --arms full")
