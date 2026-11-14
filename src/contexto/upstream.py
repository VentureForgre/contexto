"""Resolve the upstream base URL Claude Code would use if Contexto were not in the way.

Order per the M2 contract, "Upstream resolution" (PR #20 head bf1c7ad, D52), first match wins:

1. ``--upstream`` given explicitly.
2. ``env.ANTHROPIC_BASE_URL`` from the settings files Claude Code loads, highest precedence first
   (measured with Claude Code 2.1.314 in 27 directory layouts, D52): the main checkout's
   ``.claude/settings.local.json`` (the repository root, also from a subdirectory, or the main
   checkout from a genuine linked worktree, M0 finding 1a; a submodule's own root; ``git_layout``),
   then the working directory's own ``.claude/settings.local.json`` and ``.claude/settings.json``,
   and the user's
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
does, except that no project file counts while ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST`` is set
(``claude_setting``). ``contexto run codex`` passes ``trusted_only``: plain Codex ignores ``.claude/``, so a project
file's base URL or ``apiKeyHelper`` counts only when Claude Code has recorded the folder as trusted
(``folder_trusted``, D52).

Limit: organization-managed settings outrank everything here, including the ``--settings`` file
the launcher passes, so on a machine with managed settings that set a base URL the proxy may be
bypassed. The base URL is not read from managed settings. The cloud-provider guard and the hint
header decision (D70, below) do read the managed settings file, and only the file: managed settings
that reach Claude Code another way (a device-management profile, a server) are not seen.

Every settings file is read through one reader (``read_settings_file``): a regular file only, at most
``SETTINGS_FILE_LIMIT`` bytes, read exactly when Claude Code 2.1.322 reads it (D70): UTF-8 with or
without a byte order mark, and UTF-16LE after its byte order mark, malformed ones included, decoded as
Claude Code decodes them. A settings file Claude Code does not load (one holding ``NaN`` or
``Infinity``, a comment, a syntax error) is not read for any setting. A caller that needs several
values passes one ``SettingsFiles``, which reads each file once.

The result carries the winning source as a short label for the session log; the URL itself and
any file path stay out of logs.
"""

from __future__ import annotations

import json
import math
import os
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from contexto.canonical import loads_bounded

DEFAULT_UPSTREAM = "https://api.anthropic.com"
BASE_URL_KEY = "ANTHROPIC_BASE_URL"
CONFIG_DIR_KEY = "CLAUDE_CONFIG_DIR"
KEY_HELPER_KEY = "apiKeyHelper"
CUSTOM_OAUTH_KEY = "CLAUDE_CODE_CUSTOM_OAUTH_URL"
PROJECT_KINDS = frozenset({"main-checkout-local", "local", "project"})  # files that come with the repository
KIND_FILES = {"main-checkout-local": "the main checkout's .claude/settings.local.json",
              "local": ".claude/settings.local.json", "project": ".claude/settings.json"}
MANAGED_KIND = "managed"
# Claude Code's file-based managed settings, by ``sys.platform``. Always this path: no variable moves
# it, since a variable could be inherited from a project's settings ``env`` block through a session
# and hide the real file from a nested launch. The test suite patches this table in its own process.
MANAGED_SETTINGS_FILES = {"darwin": "/Library/Application Support/ClaudeCode/managed-settings.json",
                          "linux": "/etc/claude-code/managed-settings.json"}
PROVIDER_PREFIX = "CLAUDE_CODE_USE_"  # every cloud-provider switch (CLOUD_PROVIDER_KEYS) starts with it
SETTINGS_FILE_LIMIT = 4 * 1024 * 1024  # bytes read from one settings file; real ones are a few kilobytes


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


@dataclass(frozen=True)
class GitLayout:
    """Where Claude Code's project files and trust records sit for a directory inside git."""
    toplevel: Path  # the nearest directory, ``cwd`` or a parent, with a ``.git`` entry
    main: Path  # the main checkout of a genuine linked worktree, else ``toplevel``


GIT_FILE_LIMIT = 4096  # bytes read from a .git file, commondir, or gitdir; real ones are one short line


def git_layout(directory: Path) -> GitLayout | None:
    """The git layout around ``directory``, from the file system alone, or None when no directory
    from ``directory`` up has a ``.git`` entry. Git itself is not asked: a folder that does not come
    from ``git clone`` (an archive) writes its own ``.git``, so git's answer is the folder's. Any
    ``.git`` entry, file or directory, valid or not, ends the walk (reviewer A, PR #237: a garbage
    ``.git`` file, an empty ``.git`` directory, or ``core.worktree`` set elsewhere must not make a
    trusted parent count; real Claude Code 2.1.314 showed its dialog in all three). The main
    checkout differs from the toplevel only for a linked worktree that its main checkout's own
    ``.git/worktrees/<name>/gitdir`` points back to (see ``_linked_main``); a submodule's or any
    other ``.git`` file names no main checkout (measured: Claude Code reads a submodule's own root)."""
    try:
        here = directory.resolve()
    except (OSError, RuntimeError):
        return None
    for top in (here, *here.parents):
        dot_git = top / ".git"
        if os.path.lexists(dot_git):
            return GitLayout(top, _linked_main(dot_git) or top)
    return None


def _read_git_file(path: Path) -> str | None:
    """A small regular file's first line (a symlink to a FIFO or device is never opened), or None."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as f:
            data = f.read(GIT_FILE_LIMIT)
    except OSError:
        return None
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[0].strip() if lines else None


def _linked_main(dot_git: Path) -> Path | None:
    """``M`` when ``dot_git`` is a genuine linked worktree's ``.git`` file: ``gitdir: G`` with ``G``
    at ``M/.git/worktrees/<name>``, ``G/commondir`` naming ``M/.git``, and ``G/gitdir`` naming this
    very ``.git`` file. A hostile folder can write the first two, not the back-pointer inside
    ``M/.git`` (reviewer A, PR #237: a ``.git`` file or ``commondir`` aimed at a trusted repository,
    a fake worktree, or another worktree's entry all made Contexto read the trusted repository's
    files; Claude Code 2.1.314 read the folder's own and showed its dialog). Relative paths resolve
    against the file's own directory, as git writes them with ``worktree.useRelativePaths``."""
    if dot_git.is_symlink():
        return None
    line = _read_git_file(dot_git)
    if not line or not line.startswith("gitdir:"):
        return None
    try:
        gitdir = (dot_git.parent / line.removeprefix("gitdir:").strip()).resolve()
        common = gitdir.parent.parent
        if gitdir.parent.name != "worktrees" or common.name != ".git":
            return None
        commondir, back = _read_git_file(gitdir / "commondir"), _read_git_file(gitdir / "gitdir")
        if not commondir or not back:
            return None
        if (gitdir / commondir).resolve() != common or (gitdir / back).resolve() != dot_git.resolve():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return common.parent


def main_checkout(directory: Path) -> Path | None:
    """The main checkout of the git repository containing ``directory`` (see ``git_layout``), or
    None outside git."""
    layout = git_layout(directory)
    return layout.main if layout is not None else None


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


def claude_settings_order(cwd: Path, home: Path, env: Mapping[str, str] | None = None, *,
                          layout: GitLayout | None = None) -> list[tuple[str, Path]]:
    """(kind, path) for each settings file that can set the base URL or ``apiKeyHelper``, highest
    precedence first, as Claude Code reads them from ``cwd`` (see the module docstring). A project
    file that is the user settings file too (``cwd`` is the home directory) is listed once, as
    ``user``. ``layout`` is ``git_layout(cwd)`` when the caller already has it."""
    user = user_settings_path(home, env)
    order: list[tuple[str, Path]] = []
    layout = layout or git_layout(cwd)
    if layout is not None and not _same_path(layout.main, cwd):
        order.append(("main-checkout-local", layout.main / ".claude" / "settings.local.json"))
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


# Claude Code ignores every recorded trust, and shows its dialog in every folder, while this is truthy
# (reviewer A, PR #237 round 2); in -p mode it then also drops a project file's env and apiKeyHelper
# (round 3). Only the environment it starts with counts, never a settings file's env.
IGNORE_TRUST_KEY = "CLAUDE_CODE_IGNORE_PERSISTED_TRUST"
TRUTHY = frozenset({"1", "true", "yes", "on"})
# What JavaScript's String.prototype.trim() strips (Claude Code parses the variable with it): Unicode
# space separators, the line terminators, and U+FEFF, which Python's str.strip() keeps (round 3).
JS_WHITESPACE = ("\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
                 "\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")


def env_truthy(value: str | None) -> bool:
    """Whether Claude Code reads an environment value as on:
    ``["1", "true", "yes", "on"].includes(String(v).toLowerCase().trim())`` (read from 2.1.314 and
    2.1.318). Measured on 2.1.322 for ``CLAUDE_CODE_USE_BEDROCK`` (D70; requests went to the Bedrock
    stub and none to ``ANTHROPIC_BASE_URL``): on for ``1``, ``true``, ``yes``, ``on``, ``TRUE`` and
    ``" 1 "``; off for ``0``, ``false``, ``no``, ``2``, ``enabled`` and the empty string. The same for
    ``CLAUDE_CODE_GATEWAY_HINT_HEADERS`` (``true``, ``yes``, ``on``, ``TRUE`` on; ``false``, ``2`` off)."""
    return (value or "").lower().strip(JS_WHITESPACE) in TRUTHY


FALSY = frozenset({"0", "false", "no", "off"})


def env_falsy(value: str | None) -> bool:
    """Whether ``value`` is one of the words that say off outright (``0``, ``false``, ``no``, ``off``; any
    case, surrounding whitespace ignored), as against a value Claude Code merely does not read as on
    (the empty string, ``2``)."""
    return (value or "").lower().strip(JS_WHITESPACE) in FALSY


def ignores_persisted_trust(env: Mapping[str, str] | None) -> bool:
    """Whether ``env`` sets ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST`` the way Claude Code reads it
    (``env_truthy``)."""
    return env_truthy((env or {}).get(IGNORE_TRUST_KEY, ""))


def folder_trusted(cwd: Path, home: Path, env: Mapping[str, str] | None = None, *,
                   layout: GitLayout | None = None) -> bool:
    """Whether Claude Code has recorded ``cwd`` as trusted: ``projects[<path>].hasTrustDialogAccepted``
    is ``true`` in its global config for one of these real paths (measured with Claude Code 2.1.314,
    D52): in git (``git_layout``), ``cwd`` or a parent of it up to the toplevel, or the main
    checkout of a genuine linked worktree (accepting the dialog records the main checkout, even
    from a subdirectory); outside git, ``cwd`` or any parent. A folder above the toplevel does not
    count, and neither does a path recorded unresolved (``/tmp`` for ``/private/tmp``) or with a
    trailing slash. An unreadable config means not trusted, and so does any folder while ``env`` sets
    ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST``. ``layout`` is ``git_layout(cwd)`` when the caller already
    has it."""
    if ignores_persisted_trust(env):
        return False
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
    layout = layout or git_layout(cwd)
    candidates = [here, *here.parents]
    if layout is not None:  # git_layout's toplevel is always here or one of its parents
        candidates = [*candidates[:candidates.index(layout.toplevel) + 1], layout.main]
    for path in candidates:
        entry = projects.get(str(path))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


def untrusted_note(what: str, kinds: tuple[str, ...], remedy: str, env: Mapping[str, str] | None = None) -> str:
    """The warning for a setting skipped because the folder is not trusted, or because ``env`` sets
    ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST`` (then no record helps, so the dialog is not offered as a
    way on); names the files, never paths."""
    files = " and ".join(KIND_FILES.get(kind, kind) for kind in kinds)
    if ignores_persisted_trust(env):
        return (f"ignored {what} from {files}: {IGNORE_TRUST_KEY} is set, so no folder counts as trusted. "
                f"{remedy}, or unset {IGNORE_TRUST_KEY}")
    return (f"ignored {what} from {files}: Claude Code has not recorded this folder as trusted. {remedy}, "
            f"or run claude here once and accept the trust dialog")


class SettingsFile(NamedTuple):
    """One settings file as ``read_settings_file`` found it."""

    data: dict | None  # its JSON object; None when the file is missing or could not be read
    problem: str | None  # why a file that is there could not be read, as "is ..."; else None
    # For a file with a ``problem``: whether it might set a cloud-provider switch. False only for a file
    # that was read whole but is not valid JSON, and whose text neither names ``CLAUDE_CODE_USE_`` nor
    # holds a NUL byte (a wide encoding hides the name) or a ``\u`` escape that could spell it.
    may_name_provider: bool = True


def read_settings_file(path: Path) -> SettingsFile:
    """One Claude Code settings file, read with a bound.

    Opened without blocking and checked to be a regular file before a byte is read, so a FIFO or a
    device at that path is never read; at most ``SETTINGS_FILE_LIMIT`` bytes. Decoded as Claude Code
    2.1.322 decodes it (measured, D70): UTF-16LE when it starts with that byte order mark, a final odd
    byte dropped and a lone surrogate kept; otherwise UTF-8, a byte order mark dropped and a byte that
    is not UTF-8 read as U+FFFD. Parsed with ``loads_settings``, which refuses ``NaN`` and ``Infinity``
    as Claude Code does.

    ``data`` is the file's JSON object. An empty or blank file, and a document that is not an object,
    give an empty object: nothing can be set there. A file that does not exist, a directory, and a
    file this user cannot open give ``SettingsFile(None, None)``: Claude Code runs as the same user
    and cannot read them either. A file that is there and that Contexto did not read gives
    ``problem``: it is not a regular file, it is larger than the limit, it cannot be read, or it is
    not valid JSON (a syntax error, ``NaN``, a wide encoding Claude Code does not load, nested too
    deep). Such a file is not evidence that a setting is off, since Claude Code may read it
    differently; the cloud-provider
    guard refuses on it (``cloud_provider``) unless it was read whole, is not valid JSON, and its text
    could not name a switch (``may_name_provider``): Claude Code does not load a file with a comment,
    a trailing comma, ``NaN`` or another syntax error (measured on 2.1.322, D70), so such a file turns
    no switch on. The NUL byte in that rule is the backstop for wide files that still do not parse:
    UTF-16BE, UTF-16LE without the mark, UTF-32, and UTF-16LE with the mark and a syntax error."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except (OSError, ValueError):  # missing, not permitted, a symlink loop, a name the system rejects
        return SettingsFile(None, None)
    try:
        mode = os.fstat(fd)
        if stat.S_ISDIR(mode.st_mode):
            return SettingsFile(None, None)
        if not stat.S_ISREG(mode.st_mode):
            return SettingsFile(None, "is not a regular file")
        chunks, size = [], mode.st_size
        while size <= SETTINGS_FILE_LIMIT:  # one byte past the limit shows a file that grew since fstat
            chunk = os.read(fd, min(1 << 20, SETTINGS_FILE_LIMIT + 1 - sum(map(len, chunks))))
            if not chunk:
                break
            chunks.append(chunk)
            size = max(size, sum(map(len, chunks)))
    except OSError:
        return SettingsFile(None, "cannot be read")
    finally:
        os.close(fd)
    if size > SETTINGS_FILE_LIMIT:
        return SettingsFile(None, f"is larger than {SETTINGS_FILE_LIMIT // (1024 * 1024)} MiB")
    raw = b"".join(chunks)
    try:
        # Claude Code 2.1.322 loads UTF-8, with or without a byte order mark, and UTF-16LE that starts
        # with one (measured, D70); it does not load UTF-16BE, UTF-16LE without the mark, or UTF-32. It
        # also loads UTF-16LE with the mark that holds a lone surrogate or ends in one odd byte, and
        # UTF-8 with a byte that is not UTF-8 (all measured), so they are decoded the same way: the odd
        # byte dropped, lone surrogates kept, a bad UTF-8 byte read as U+FFFD.
        if raw.startswith(b"\xff\xfe"):
            text = raw[2:len(raw) - len(raw) % 2].decode("utf-16-le", "surrogatepass")
        else:
            text = raw.decode("utf-8-sig", "replace")
        data = loads_settings(text) if text.strip() else {}
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONTooDeepError are ValueErrors
        # The backstop for a file that still does not parse: a NUL byte means a wide encoding, in which
        # the switch's name is not the UTF-8 bytes looked for (UTF-16BE, UTF-16LE without the mark,
        # UTF-32, or UTF-16LE with the mark and a syntax error).
        return SettingsFile(None, "is not valid JSON",
                            PROVIDER_PREFIX.encode() in raw or b"\\u" in raw or b"\x00" in raw)
    return SettingsFile(data if isinstance(data, dict) else {}, None)


def _not_json(constant: str) -> object:
    raise ValueError(f"{constant} is not JSON")


def loads_settings(text: str) -> object:
    """A settings file's text parsed as Claude Code parses it: ``loads_bounded``, except that ``NaN``,
    ``Infinity`` and ``-Infinity``, which Python's parser accepts, make the text not valid JSON.
    Claude Code does not load a settings file holding one (measured on 2.1.322, D70). The proxy's
    own parsing of requests and responses is not changed. A lone surrogate in a string, from a
    ``\\ud800`` escape or from a malformed UTF-16LE file, is kept, as JavaScript keeps it."""
    return loads_bounded(text, parse_constant=_not_json)


class SettingsFiles:
    """The settings files of one launch or command, each read at most once (``read_settings_file``)."""

    def __init__(self) -> None:
        self._read: dict[Path, SettingsFile] = {}

    def read(self, path: Path) -> SettingsFile:
        found = self._read.get(path)
        if found is None:
            found = self._read[path] = read_settings_file(path)
        return found


def managed_settings_path() -> Path | None:
    """Claude Code's managed settings file on this platform (``MANAGED_SETTINGS_FILES``); None on a
    platform with no known location. File-based managed settings only."""
    known = MANAGED_SETTINGS_FILES.get(sys.platform)
    return Path(known) if known else None


def _settings_string(data: dict, pick: Callable[[dict], object]) -> str | None:
    value = pick(data)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _settings_base_url(data: dict) -> str | None:
    def pick(data: dict) -> object:
        env = data.get("env")
        return env.get(BASE_URL_KEY) if isinstance(env, dict) else None
    return _settings_string(data, pick)


def claude_setting(read: Callable[[dict], str | None], cwd: Path, home: Path, env: Mapping[str, str] | None = None,
                   *, trusted_only: bool = False, files: SettingsFiles | None = None) -> Setting:
    """The first value ``read`` finds in the JSON object of a file in ``claude_settings_order``; a
    file that is missing or cannot be read (``read_settings_file``) sets nothing. With
    ``trusted_only``, a project file counts only when ``folder_trusted``. While ``env`` sets
    ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST``
    no project file counts for any caller: Claude Code in ``-p`` mode then drops a project file's
    ``env`` and ``apiKeyHelper`` (measured on 2.1.314 and 2.1.318, D52), so the proxy must not
    forward to its base URL. Interactively it shows its dialog and, once accepted, uses the
    project's base URL (measured); skipping there too is the stricter side, and ``--upstream``
    overrides. The project kinds skipped either way are reported. ``files`` is the caller's
    ``SettingsFiles`` when it reads more than one value."""
    ignored: list[str] = []
    trusted: bool | None = None
    no_trust = ignores_persisted_trust(env)
    files = files or SettingsFiles()
    layout = git_layout(cwd)  # one walk for the order and the trust check (reviewer A's nit, PR #237)
    for kind, path in claude_settings_order(cwd, home, env, layout=layout):
        data = files.read(path).data
        value = read(data) if data is not None else None
        if value is None:  # the file does not set it; an empty value (``_settings_env``) is still a value
            continue
        if kind in PROJECT_KINDS and (trusted_only or no_trust):
            if trusted is None:
                trusted = not no_trust and folder_trusted(cwd, home, env, layout=layout)
            if not trusted:
                ignored.append(kind)
                continue
        return Setting(value, kind, tuple(ignored))
    return Setting(None, None, tuple(ignored))


def _js_number(value: int | float) -> str:
    """JavaScript's ``String()`` of a JSON number. Exact for integers, which is all that matters here
    (only ``1`` is ever on); other numbers give Python's shortest form."""
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value.is_integer() and abs(value) < 2 ** 53:
            return str(int(value))
        return repr(value)
    if abs(value) < 2 ** 53:
        return str(value)
    try:
        return repr(float(value))
    except OverflowError:  # past the largest double: JavaScript reads the number as Infinity
        return "Infinity" if value > 0 else "-Infinity"


def js_string(value: object) -> str:
    """JavaScript's ``String(value)`` for a parsed JSON value, which is how Claude Code turns a
    settings ``env`` entry into an environment value. Measured on 2.1.322 with
    ``CLAUDE_CODE_USE_BEDROCK`` in a settings file (D70): on for ``true``, ``1``, ``1.0``, ``1e0``,
    ``["1"]``, ``[1]``, ``[["1"]]``, ``[true]`` and ``" On "``; off for ``false``, ``1.5``, ``[]``,
    ``["1","1"]`` (the text ``1,1``), ``null``, an object (``[object Object]``) and ``"1.0"``.

    A string is itself, ``true`` and ``false`` their names, a number its shortest form (``1.0`` and
    ``1e0`` give ``1``), an object ``[object Object]``, and a list its items joined with commas,
    lists inside it flattened the same way and ``null`` items empty. No recursion."""
    if not isinstance(value, list):
        return _js_scalar(value)
    leaves: list[str] = []
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, list) and item:
            pending.extend(reversed(item))
        else:
            leaves.append("" if item is None or isinstance(item, list) else _js_scalar(item))
    return ",".join(leaves)


def _js_scalar(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _js_number(value)
    return "null" if value is None else "[object Object]"


def _settings_env(data: dict, name: str) -> str | None:
    """``env.<name>`` in a settings file's JSON object as Claude Code applies it to its environment
    (``js_string``), or None when the file does not set it (no ``env`` object, no such entry, or
    ``null``). An empty string is a value."""
    env = data.get("env")
    value = env.get(name) if isinstance(env, dict) else None
    return None if value is None else js_string(value)


def settings_file_env(files: SettingsFiles, path: Path | None, name: str) -> str | None:
    """``env.<name>`` of the settings file at ``path`` (``_settings_env``), or None when there is no
    such file, it could not be read, or it does not set the variable."""
    data = files.read(path).data if path is not None else None
    return _settings_env(data, name) if data is not None else None


@dataclass(frozen=True)
class EnvValue:
    """An environment variable as Claude Code would see it, and where the value comes from."""
    name: str
    value: str | None  # None when nothing sets it
    source: str | None  # "settings:<kind>" (see ``KIND_FILES``; or ``user``, ``managed``), "env", or None


def claude_env_value(name: str, *, env: Mapping[str, str] | None = None, cwd: Path | None = None,
                     home: Path | None = None, files: SettingsFiles | None = None, managed: Path | None = None,
                     process_env: bool = True) -> EnvValue:
    """The value Claude Code would run with for the environment variable ``name``, launched from
    ``cwd`` with the process environment ``env``, as far as Contexto can tell: the managed settings
    file's ``env`` block (``managed``, by default ``managed_settings_path``), else the first settings
    file ``env`` block that sets it, in the order and under the trust rule ``contexto run claude`` uses
    for the base URL (``claude_setting``: a project file counts as ``claude -p`` reads it, with no
    trust record needed, and no project file counts while ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST`` is
    set), else the process environment, which ``process_env=False`` leaves out. Measured on 2.1.322
    (D70) with ``CLAUDE_CODE_USE_BEDROCK`` and ``CLAUDE_CODE_GATEWAY_HINT_HEADERS``: a settings file's
    value wins over the environment's in both directions, in the user file and in a project file, and
    a project file's wins over the user file's. That managed settings come first is from Claude Code's
    documentation, not measured. A ``--settings`` file is not read. This is a prediction, good enough
    to decide what to forward; the cloud-provider guard does not rely on it (``cloud_provider``)."""
    env = os.environ if env is None else env
    files = files or SettingsFiles()
    managed = managed or managed_settings_path()
    value = settings_file_env(files, managed, name)
    if value is not None:
        return EnvValue(name, value, f"settings:{MANAGED_KIND}")
    found = claude_setting(lambda data: _settings_env(data, name), cwd or Path.cwd(), home or Path.home(), env,
                           files=files)
    if found.value is not None:
        return EnvValue(name, found.value, f"settings:{found.source}")
    value = env.get(name) if process_env else None
    return EnvValue(name, value, "env" if value is not None else None)


# D70: with one of these on, Claude Code 2.1.322 sent every request to that provider's own endpoint
# (ANTHROPIC_BEDROCK_BASE_URL, ANTHROPIC_VERTEX_BASE_URL, ANTHROPIC_FOUNDRY_BASE_URL,
# ANTHROPIC_BEDROCK_MANTLE_BASE_URL, ANTHROPIC_AWS_BASE_URL) and none to ANTHROPIC_BASE_URL, not even
# HEAD /api/hello: 0 of the requests of 5 runs with the value 1 and 4 more with true.
CLOUD_PROVIDER_KEYS = {"CLAUDE_CODE_USE_BEDROCK": "Amazon Bedrock", "CLAUDE_CODE_USE_VERTEX": "Google Vertex AI",
                       "CLAUDE_CODE_USE_FOUNDRY": "Microsoft Foundry",
                       "CLAUDE_CODE_USE_MANTLE": "Amazon Bedrock (Mantle)",
                       "CLAUDE_CODE_USE_ANTHROPIC_AWS": "Claude Platform on AWS"}
PLACE_NAMES = {"env": "the process environment",
               "settings:user": "the env block of your Claude Code user settings "
                                "(~/.claude/settings.json, or settings.json in $CLAUDE_CONFIG_DIR)",
               f"settings:{MANAGED_KIND}": "the env block of Claude Code's managed settings file "
                                           "(managed-settings.json)"}


@dataclass(frozen=True)
class CloudProvider:
    """Why ``cloud_provider`` would not let a launch through."""
    on: tuple[tuple[str, tuple[str, ...]], ...]  # each switch that is on, with every place it is on
    unread: tuple[tuple[str, str], ...] = ()  # (place, problem) for each settings file that could not be read


def cloud_provider(*, env: Mapping[str, str] | None = None, cwd: Path | None = None, home: Path | None = None,
                   files: SettingsFiles | None = None, managed: Path | None = None) -> CloudProvider | None:
    """Whether a Claude Code launched from ``cwd`` may send its requests to a cloud provider, or None
    (D70). With one of ``CLOUD_PROVIDER_KEYS`` on, Claude Code sends nothing to ``ANTHROPIC_BASE_URL``,
    so a proxy it was pointed at sees no request and bounds nothing.

    The check fails closed. A switch counts when it is on (``env_truthy``) in the process environment
    or in the ``env`` block (``js_string``) of any settings file Contexto can find: every file of
    ``claude_settings_order`` (the main checkout's and the folder's project files, and the user's) and
    the managed settings file (``managed``, by default ``managed_settings_path``; file-based managed
    settings only). Precedence between them is not applied and neither is the folder's trust state:
    Claude Code does not apply every file Contexto can read (measured on 2.1.322: a file holding
    ``NaN`` or a schema error, any project file under ``--setting-sources user``, a project file's
    ``env`` in ``-p`` mode while ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST`` is set), so a file that says
    off cannot be trusted to win, and a project file can only add a refusal. The accepted cost is a
    refusal Claude Code would not have needed: the environment says on and a file Claude Code does
    apply says off, or a file it ignores says on.

    A settings file that is there and that Contexto could not read (``read_settings_file``: not a
    regular file, over the size limit, not valid JSON) is reported in ``unread`` and also
    refuses the launch. It is not evidence that a switch is off: Claude Code has no such limit and
    may parse the file differently. A file that does not exist, and one this user cannot open, pass:
    Claude Code cannot read them either.

    Not covered: managed settings that are not in that file, and ``CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST``
    (the docs say Claude Code then ignores these switches in settings files; not measured, so a switch
    found there still counts)."""
    env = os.environ if env is None else env
    files = files or SettingsFiles()
    managed = managed or managed_settings_path()
    order = [(f"settings:{kind}", path)
             for kind, path in claude_settings_order(cwd or Path.cwd(), home or Path.home(), env)]
    if managed is not None:
        order.append((f"settings:{MANAGED_KIND}", managed))
    read = [(place, files.read(path)) for place, path in order]
    on = []
    for name in CLOUD_PROVIDER_KEYS:
        places = ["env"] if env_truthy(env.get(name)) else []
        places += [place for place, found in read
                   if found.data is not None and env_truthy(_settings_env(found.data, name))]
        if places:
            on.append((name, tuple(places)))
    unread = tuple((place, found.problem) for place, found in read if found.problem and found.may_name_provider)
    return CloudProvider(tuple(on), unread) if on or unread else None


def _place(place: str) -> str:
    kind = place.removeprefix("settings:")
    return PLACE_NAMES.get(place) or f"the env block of {KIND_FILES.get(kind, kind)}"


def _file(place: str) -> str:
    return _place(place).removeprefix("the env block of ")


def _listed(items: list[str], word: str = "in ") -> str:
    """``a``, ``a and in b``, ``a, in b and in c``."""
    if len(items) < 2:
        return "".join(items)
    return f", {word}".join(items[:-1]) + f" and {word}" + items[-1]


def cloud_provider_note(found: CloudProvider) -> str:
    """What ``cloud_provider`` found, as sentences without the final full stop: each switch and every
    place it is on, then each settings file that could not be read. No value and no path of the
    user's."""
    parts = []
    for name, places in found.on:
        provider = CLOUD_PROVIDER_KEYS.get(name, "a cloud provider")
        parts.append(f"{name} is set in {_listed([_place(place) for place in places])}. With it on, Claude Code "
                     f"sends its requests to {provider} and none to ANTHROPIC_BASE_URL")
    for place, problem in found.unread:
        parts.append(f"Contexto could not read {_file(place)} (it {problem}), so it cannot tell whether that file "
                     f"turns on a cloud provider")
    return ". ".join(parts)


def claude_key_helper(cwd: Path, home: Path, env: Mapping[str, str] | None = None, *,
                      trusted_only: bool = True) -> Setting:
    """The ``apiKeyHelper`` Claude Code would use from ``cwd``. Trusted files only by default: the
    helper is a command, and its only caller hands it to Codex, which runs it without asking."""
    return claude_setting(lambda data: _settings_string(data, lambda data: data.get(KEY_HELPER_KEY)),
                          cwd, home, env, trusted_only=trusted_only)


def resolve_upstream(explicit: str | None = None, *, env: Mapping[str, str] | None = None,
                     cwd: Path | None = None, home: Path | None = None, trusted_only: bool = False,
                     files: SettingsFiles | None = None) -> Upstream:
    """The upstream per the module docstring. ``trusted_only`` skips project files in a folder Claude
    Code has not trusted (``contexto run codex``); ``Upstream.ignored`` names the skipped kinds.
    ``files`` is the caller's ``SettingsFiles`` when it reads more than the base URL."""
    if explicit:
        return Upstream(explicit, "flag")
    env = os.environ if env is None else env
    found = claude_setting(_settings_base_url, cwd or Path.cwd(), home or Path.home(), env, trusted_only=trusted_only,
                           files=files)
    if found.value:
        return Upstream(found.value, f"settings:{found.source}", found.ignored)
    value = env.get(BASE_URL_KEY, "").strip()
    if value:
        return Upstream(value, "env", found.ignored)
    return Upstream(DEFAULT_UPSTREAM, "default", found.ignored)
