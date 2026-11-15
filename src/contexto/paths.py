"""Where Contexto keeps its data: ``$CONTEXTO_HOME``, else ``~/.contexto``, one directory per session."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def absolute_home(value: str | os.PathLike[str]) -> Path:
    """A data directory as the user named it, with ``~`` expanded and made absolute. One rule for every
    way a home is given ($CONTEXTO_HOME or ``--home``): a client that writes ``~/x`` without a shell must
    not open a literal ``./~/x`` (B24). ``absolute()``, not ``resolve()``: symlinks are kept as named."""
    return Path(value).expanduser().absolute()


def contexto_home(env: Mapping[str, str] | None = None) -> Path:
    """``$CONTEXTO_HOME`` (from ``env``, default the process environment), else ``~/.contexto``, as an
    absolute path. Absolute because it is handed to child processes that start elsewhere: Codex starts
    its MCP servers in its own working directory, so a relative home would open a second store
    (packet A18)."""
    home = (os.environ if env is None else env).get("CONTEXTO_HOME")
    return absolute_home(home) if home else absolute_home(Path.home() / ".contexto")


# Passed on to the MCP server when set: Codex starts MCP servers with a restricted environment (D39).
SEARCH_CACHE_ENV = "CONTEXTO_SEARCH_CACHE_MB"


def mcp_env(home: Path, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment ``contexto run`` gives the MCP server: ``CONTEXTO_HOME``, always (packet A18), and
    ``CONTEXTO_SEARCH_CACHE_MB`` when ``env`` (default the process environment) sets it."""
    out = {"CONTEXTO_HOME": str(home)}
    value = (os.environ if env is None else env).get(SEARCH_CACHE_ENV)
    if value is not None:
        out[SEARCH_CACHE_ENV] = value
    return out


# D69: ``contexto run`` turns project memory off for the launch when this is one of the off values.
PROJECT_MEMORY_ENV = "CONTEXTO_PROJECT_MEMORY"
PROJECT_MEMORY_OFF = ("0", "false", "off", "no")
PROJECT_MEMORY_ON = ("1", "true", "on", "yes")


def project_memory_env(env: Mapping[str, str] | None = None) -> tuple[bool, str | None]:
    """What ``CONTEXTO_PROJECT_MEMORY`` (from ``env``, default the process environment) asks of a launch:
    ``(on, unrecognised)``. Off only for ``0``, ``false``, ``off`` or ``no``, in any case and with the
    space around it ignored. Unset, empty, or ``1``, ``true``, ``on``, ``yes`` is on. Any other value is
    on as well, so a typing error never turns memory off unasked, and is returned (stripped) as
    ``unrecognised`` for the launcher to name."""
    value = ((os.environ if env is None else env).get(PROJECT_MEMORY_ENV) or "").strip()
    word = value.lower()
    if word in PROJECT_MEMORY_OFF:
        return False, None
    return True, None if not word or word in PROJECT_MEMORY_ON else value


SESSION_ID_MAX_BYTES = 255  # NAME_MAX: a longer directory name fails in the filesystem, not here


def session_dir(home: Path, session_id: str) -> Path:
    """``home/sessions/<session_id>``; the id must be a single safe path segment the filesystem can name."""
    if (not session_id or session_id in (".", "..") or any(c in session_id for c in "/\\\0")):
        raise ValueError(f"invalid session id: {session_id!r}")
    try:
        size = len(os.fsencode(session_id))
    except UnicodeEncodeError:  # a lone surrogate no file name can hold
        raise ValueError(f"invalid session id: {session_id[:40]!r}") from None
    if size > SESSION_ID_MAX_BYTES:
        raise ValueError(f"invalid session id {session_id[:40]!r}...: longer than {SESSION_ID_MAX_BYTES} bytes")
    return Path(home) / "sessions" / session_id
