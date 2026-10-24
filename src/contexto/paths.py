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


def session_dir(home: Path, session_id: str) -> Path:
    """``home/sessions/<session_id>``; the id must be a single safe path segment."""
    if (not session_id or session_id in (".", "..") or any(c in session_id for c in "/\\\0")):
        raise ValueError(f"invalid session id: {session_id!r}")
    return Path(home) / "sessions" / session_id
