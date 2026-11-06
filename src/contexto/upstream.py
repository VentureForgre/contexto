"""Resolve the upstream base URL Claude Code would use if Contexto were not in the way.

Order per the M2 contract, "Upstream resolution" (PR #20 head bf1c7ad, D52), first match wins:

1. ``--upstream`` given explicitly.
2. ``env.ANTHROPIC_BASE_URL`` from the settings files Claude Code loads, highest precedence first
   (measured with Claude Code 2.1.314 in 27 directory layouts, D52): the main checkout's
   ``.claude/settings.local.json`` (the parent of the git common dir: the repository root, or the
   main checkout from a linked worktree, M0 finding 1a; a submodule's own root), then the working
   directory's own ``.claude/settings.local.json`` and ``.claude/settings.json``, and the user's
   ``~/.claude/settings.json``, or ``$CLAUDE_CONFIG_DIR/settings.json`` when ``CLAUDE_CONFIG_DIR``
   is set, because Claude Code then reads its user settings from that directory (D35). From a
   subdirectory Claude Code does not read the repository root's ``settings.json``, and outside git
   it reads no parent directory's files, so neither does this.
3. ``ANTHROPIC_BASE_URL`` in the process environment. M0 measured that a settings-file value
   overrides the process environment (finding 1), so the environment only counts when no
   settings file sets one.
4. ``https://api.anthropic.com``.

The project files (every file above except the user's) come from whatever repository the user
runs in. Claude Code itself uses them in ``-p`` mode with no trust check, and interactively only
after its "do you trust this folder" dialog, so ``contexto run claude`` reads them as Claude Code
does. ``contexto run codex`` passes ``trusted_only``: plain Codex ignores ``.claude/``, so a project
file's base URL or ``apiKeyHelper`` counts only when Claude Code has recorded the folder as trusted
(``folder_trusted``, D52).

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_UPSTREAM = "https://api.anthropic.com"
BASE_URL_KEY = "ANTHROPIC_BASE_URL"
CONFIG_DIR_KEY = "CLAUDE_CONFIG_DIR"
KEY_HELPER_KEY = "apiKeyHelper"
CUSTOM_OAUTH_KEY = "CLAUDE_CODE_CUSTOM_OAUTH_URL"
PROJECT_KINDS = frozenset({"main-checkout-local", "local", "project"})  # files that come with the repository
KIND_FILES = {"main-checkout-local": "the main checkout's .claude/settings.local.json",
              "local": ".claude/settings.local.json", "project": ".claude/settings.json"}


@dataclass(frozen=True)
class Upstream:
    url: str
    source: str  # "flag", "settings:<kind>", "env", or "default"
    ignored: tuple[str, ...] = ()  # project kinds that set a base URL, skipped as untrusted (trusted_only)


@dataclass(frozen=True)
class Setting:
    value: str | None
    source: str | None  # the settings kind it came from, None when no file sets it
    ignored: tuple[str, ...] = ()  # project kinds that set it, skipped as untrusted (trusted_only)


def _git_layout(directory: Path) -> tuple[Path, Path] | None:
    """(main checkout, toplevel) of the git work tree containing ``directory``, or None outside git.
    The main checkout is the parent of the git common dir: the repository itself, or its main
    checkout from a linked worktree. When the common dir is not a ``.git`` folder (a submodule's
    lives under the superproject's ``.git/modules``, measured: Claude Code then reads the
    submodule's own root) it is the toplevel."""
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
    return (common_dir.parent if common_dir.name == ".git" else toplevel), toplevel


def main_checkout(directory: Path) -> Path | None:
    """The main checkout of the git repository containing ``directory`` (see ``_git_layout``), or
    None outside git."""
    layout = _git_layout(directory)
    return layout[0] if layout is not None else None


def user_settings_path(home: Path, env: Mapping[str, str] | None = None) -> Path:
    """Claude Code's user settings file: ``$CLAUDE_CONFIG_DIR/settings.json`` when ``env`` sets
    ``CLAUDE_CONFIG_DIR``, else ``~/.claude/settings.json``. ``env`` None means no override (callers
    pass the process environment explicitly, so a test's ``home`` is never redirected).

    The value is used as given, like Claude Code does: no ``~`` expansion, so a literal ``~/cfg``
    names ``./~/cfg`` for both, and a ``~user`` that does not exist cannot raise. A relative value
    resolves against the working directory, which the CLI inherits. An empty or whitespace-only
    value is treated as unset (Claude Code is inconsistent there; falling back to ``~/.claude`` is
    the reading that cannot point at a stray ``./settings.json``)."""
    config_dir = (env or {}).get(CONFIG_DIR_KEY) or ""
    if not config_dir.strip():
        return home / ".claude" / "settings.json"
    return Path(config_dir) / "settings.json"


def claude_settings_order(cwd: Path, home: Path, env: Mapping[str, str] | None = None) -> list[tuple[str, Path]]:
    """(kind, path) for each settings file that can set the base URL or ``apiKeyHelper``, highest
    precedence first, as Claude Code reads them from ``cwd`` (see the module docstring). A project
    file that is the user settings file too (``cwd`` is the home directory) is listed once, as
    ``user``."""
    user = user_settings_path(home, env)
    order: list[tuple[str, Path]] = []
    main = main_checkout(cwd)
    if main is not None and not _same_path(main, cwd):
        order.append(("main-checkout-local", main / ".claude" / "settings.local.json"))
    order += [("local", cwd / ".claude" / "settings.local.json"), ("project", cwd / ".claude" / "settings.json")]
    return [(kind, path) for kind, path in order if not _same_path(path, user)] + [("user", user)]


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except (OSError, RuntimeError):  # a symlink loop or an unreadable parent: compare as written
        return a == b


def claude_global_config_path(home: Path, env: Mapping[str, str] | None = None) -> Path:
    """The global config file where Claude Code records folder trust: a legacy ``.config.json`` in
    its config directory when that is a non-empty file, else ``.claude.json`` in
    ``$CLAUDE_CONFIG_DIR`` or the home directory (``.claude-custom-oauth.json`` when
    ``CLAUDE_CODE_CUSTOM_OAUTH_URL`` is set). Read from Claude Code 2.1.314 and measured (D52)."""
    env = env or {}
    config_dir = env.get(CONFIG_DIR_KEY) or ""
    config_dir = Path(config_dir) if config_dir.strip() else None
    legacy = (config_dir or home / ".claude") / ".config.json"
    try:
        if legacy.is_file() and legacy.stat().st_size > 0:
            return legacy
    except OSError:
        pass
    suffix = "-custom-oauth" if env.get(CUSTOM_OAUTH_KEY) else ""
    return (config_dir or home) / f".claude{suffix}.json"


def folder_trusted(cwd: Path, home: Path, env: Mapping[str, str] | None = None) -> bool:
    """Whether Claude Code has recorded ``cwd`` as trusted: ``projects[<path>].hasTrustDialogAccepted``
    is ``true`` in its global config for one of these real paths (measured with Claude Code 2.1.314,
    D52): in a git work tree, ``cwd`` or a parent of it up to the toplevel, or the main checkout (a
    linked worktree counts as trusted when its main checkout is; accepting the dialog records the
    main checkout, even from a subdirectory); outside git, ``cwd`` or any parent. A folder above the
    toplevel does not count, and neither does a path recorded unresolved (``/tmp`` for
    ``/private/tmp``) or with a trailing slash. An unreadable config means not trusted."""
    try:
        data = json.loads(claude_global_config_path(home, env).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return False
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return False
    try:
        here = cwd.resolve()
    except (OSError, RuntimeError):
        return False
    layout = _git_layout(cwd)
    candidates = [here, *here.parents]
    if layout is not None:
        main, toplevel = (p.resolve() for p in layout)
        if toplevel in candidates:
            candidates = candidates[:candidates.index(toplevel) + 1]
        candidates.append(main)
    for path in candidates:
        entry = projects.get(str(path))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


def untrusted_note(what: str, kinds: tuple[str, ...], remedy: str) -> str:
    """The warning for a setting skipped because the folder is not trusted; names the files, never paths."""
    files = " and ".join(KIND_FILES.get(kind, kind) for kind in kinds)
    return (f"ignored {what} from {files}: Claude Code has not recorded this folder as trusted. {remedy}, "
            f"or run claude here once and accept the trust dialog")


def _settings_string(path: Path, pick: Callable[[dict], object]) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):  # unreadable or not JSON: as if the file were absent
        return None
    value = pick(data) if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _settings_base_url(path: Path) -> str | None:
    def pick(data: dict) -> object:
        env = data.get("env")
        return env.get(BASE_URL_KEY) if isinstance(env, dict) else None
    return _settings_string(path, pick)


def claude_setting(read: Callable[[Path], str | None], cwd: Path, home: Path, env: Mapping[str, str] | None = None,
                   *, trusted_only: bool = False) -> Setting:
    """The first value ``read`` finds in ``claude_settings_order``. With ``trusted_only``, a project
    file counts only when ``folder_trusted``; the project kinds skipped that way are reported."""
    ignored: list[str] = []
    trusted: bool | None = None
    for kind, path in claude_settings_order(cwd, home, env):
        value = read(path)
        if not value:
            continue
        if trusted_only and kind in PROJECT_KINDS:
            if trusted is None:
                trusted = folder_trusted(cwd, home, env)
            if not trusted:
                ignored.append(kind)
                continue
        return Setting(value, kind, tuple(ignored))
    return Setting(None, None, tuple(ignored))


def claude_key_helper(cwd: Path, home: Path, env: Mapping[str, str] | None = None, *,
                      trusted_only: bool = True) -> Setting:
    """The ``apiKeyHelper`` Claude Code would use from ``cwd``. Trusted files only by default: the
    helper is a command, and its only caller hands it to Codex, which runs it without asking."""
    return claude_setting(lambda path: _settings_string(path, lambda data: data.get(KEY_HELPER_KEY)),
                          cwd, home, env, trusted_only=trusted_only)


def resolve_upstream(explicit: str | None = None, *, env: Mapping[str, str] | None = None,
                     cwd: Path | None = None, home: Path | None = None, trusted_only: bool = False) -> Upstream:
    """The upstream per the module docstring. ``trusted_only`` skips project files in a folder Claude
    Code has not trusted (``contexto run codex``); ``Upstream.ignored`` names the skipped kinds."""
    if explicit:
        return Upstream(explicit, "flag")
    env = os.environ if env is None else env
    found = claude_setting(_settings_base_url, cwd or Path.cwd(), home or Path.home(), env, trusted_only=trusted_only)
    if found.value:
        return Upstream(found.value, f"settings:{found.source}", found.ignored)
    value = env.get(BASE_URL_KEY, "").strip()
    if value:
        return Upstream(value, "env", found.ignored)
    return Upstream(DEFAULT_UPSTREAM, "default", found.ignored)
