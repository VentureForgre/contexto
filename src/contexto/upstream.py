"""Resolve the upstream base URL Claude Code would use if Contexto were not in the way.

Order per the M2 contract, "Upstream resolution" (PR #20 head bf1c7ad), first match wins:

1. ``--upstream`` given explicitly.
2. ``env.ANTHROPIC_BASE_URL`` from the settings files Claude Code loads, highest precedence first:
   in a linked git worktree the MAIN checkout's ``.claude/settings.local.json`` (measured in M0,
   docs/research/M0-proxy-measurements.md finding 1a: it is loaded and its value wins), then the
   project's ``.claude/settings.local.json``, ``.claude/settings.json``, and the user's
   ``~/.claude/settings.json``.
3. ``ANTHROPIC_BASE_URL`` in the process environment. M0 measured that a settings-file value
   overrides the process environment (finding 1), so the environment only counts when no
   settings file sets one.
4. ``https://api.anthropic.com``.

Limit: organization-managed settings outrank everything here, including the ``--settings`` file
the launcher passes, so on a machine with managed settings that set a base URL the proxy may be
bypassed. This module does not read managed settings.

The result carries the winning source as a short label for the session log; the URL itself and
any file path stay out of logs.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_UPSTREAM = "https://api.anthropic.com"
BASE_URL_KEY = "ANTHROPIC_BASE_URL"


@dataclass(frozen=True)
class Upstream:
    url: str
    source: str  # "flag", "settings:<kind>", "env", or "default"


def main_checkout(directory: Path) -> Path | None:
    """The main checkout when ``directory`` is inside a linked git worktree, else None."""
    try:
        out = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.stdout.splitlines()
    if len(lines) < 2:
        return None
    common_dir, toplevel = Path(lines[0]), Path(lines[1])
    if common_dir.name != ".git":
        return None
    main = common_dir.parent
    return main if main.resolve() != toplevel.resolve() else None


def project_root(directory: Path) -> Path:
    """The git toplevel containing ``directory``, or ``directory`` itself outside git."""
    try:
        out = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return directory
    return Path(out.stdout.strip() or directory)


def claude_settings_order(cwd: Path, home: Path) -> list[tuple[str, Path]]:
    """(kind, path) for each settings file that can set the base URL, highest precedence first."""
    order: list[tuple[str, Path]] = []
    main = main_checkout(cwd)
    if main is not None:
        order.append(("main-checkout-local", main / ".claude" / "settings.local.json"))
    root = project_root(cwd)
    order += [
        ("local", root / ".claude" / "settings.local.json"),
        ("project", root / ".claude" / "settings.json"),
        ("user", home / ".claude" / "settings.json"),
    ]
    return order


def _settings_base_url(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    env = data.get("env") if isinstance(data, dict) else None
    value = env.get(BASE_URL_KEY) if isinstance(env, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def resolve_upstream(explicit: str | None = None, *, env: Mapping[str, str] | None = None,
                     cwd: Path | None = None, home: Path | None = None) -> Upstream:
    if explicit:
        return Upstream(explicit, "flag")
    for kind, path in claude_settings_order(cwd or Path.cwd(), home or Path.home()):
        value = _settings_base_url(path)
        if value:
            return Upstream(value, f"settings:{kind}")
    value = (os.environ if env is None else env).get(BASE_URL_KEY, "").strip()
    if value:
        return Upstream(value, "env")
    return Upstream(DEFAULT_UPSTREAM, "default")
