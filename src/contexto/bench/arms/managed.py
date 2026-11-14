"""Organization-managed Claude Code settings that would send a Claude Code arm around the meter.

Managed settings outrank every other settings source, the ``--settings`` file ``contexto run`` passes
included (D52; ``contexto.upstream`` says so and does not read them). If they set the base URL, a key
helper, a login method or an environment variable that configures Claude Code, a Claude Code arm
could reach the vendor without passing the meter, so its spend would not be counted or guarded, or
its compaction would not be what the manifest says. The live run refuses to start in that case.

Where Claude Code 2.1.322 reads them (read in its bundle): ``managed-settings.json`` and the
``managed-settings.d/*.json`` drop-ins in ``/Library/Application Support/ClaudeCode`` (macOS),
``/etc/claude-code`` (Linux) or ``C:\\Program Files\\ClaudeCode`` (Windows), and on macOS the managed
preferences ``/Library/Managed Preferences/<user>/com.anthropic.claudecode.plist`` and
``/Library/Managed Preferences/com.anthropic.claudecode.plist``. Not read here: the Windows
registry policies and settings an organization's server sends. Those are why every Claude Code turn
must also have at least one metered request (the ``unmetered`` stop).

What is reported names the source by its kind and the setting by its key, never a value, a user
name or a home directory.
"""

from __future__ import annotations

import getpass
import json
import plistlib
import sys
from pathlib import Path
from typing import Any

FILE_NAME = "managed-settings.json"
DROP_IN_DIR = "managed-settings.d"
PLIST_ID = "com.anthropic.claudecode"
# Top-level settings that change where Claude Code sends requests or with what credential.
ROUTING_KEYS = ("apiKeyHelper", "forceLoginMethod", "forceLoginOrgUUID", "awsAuthRefresh", "awsCredentialExport")
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
    out += [(f"the managed settings drop-in {p.name}", p) for p in drop_ins]
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
    """What the managed settings on this machine set that would override what the Claude Code arms set,
    one sentence per source; empty when nothing does."""
    out = []
    for label, path in sources() if found is None else found:
        settings = read(path)
        if not settings:
            continue
        keys = [key for key in ROUTING_KEYS if settings.get(key) not in (None, "", [], {})]
        names = env_overrides(settings.get("env"))
        if keys or names:
            parts = ([", ".join(keys)] if keys else []) + ([f"env {', '.join(names)}"] if names else [])
            out.append(f"{label} sets {'; '.join(parts)}")
    return out


def refusal(found: list[str]) -> str:
    return ("organization-managed Claude Code settings on this machine would override what the contexto and plain "
            "arms set, so their requests could go around the benchmark's meter: " + "; ".join(found)
            + ". Run the benchmark on a machine without them, or with --arms full")
