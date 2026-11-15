"""Contexto: durable, recoverable context for very long-running AI sessions."""

import json
import re
from typing import Any

# The distribution name, on PyPI and in installed metadata. The import name and the command are "contexto".
DISTRIBUTION = "contexto-proxy"

# What ``version_line`` prints from ``direct_url.json``. The file is written by the installer, so both
# values are checked: a revision is a tag or branch name of ordinary characters, a commit id is hex.
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+-]{0,63}")
_COMMIT = re.compile(r"[0-9a-fA-F]{8,64}")
# Characters; the real file is a few hundred. A longer one is not parsed. It is still read whole:
# importlib.metadata has no bounded read, and the file is only read for --version.
_MAX_DIRECT_URL = 65_536


def __getattr__(name: str) -> Any:
    """``__version__`` and ``distribution``, loaded when they are first asked for (D64). Both need
    ``importlib.metadata``, which takes longer to import than everything else ``contexto statusline``
    does, and Claude Code runs that command after every assistant message. Each is kept as a module
    attribute once loaded, so ``contexto.__version__`` and a test's ``monkeypatch`` of ``distribution``
    work as for ordinary attributes."""
    if name == "distribution":
        from importlib.metadata import distribution

        globals()["distribution"] = distribution
        return distribution
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            # Installed metadata carries the version the release workflow stamped.
            found = version(DISTRIBUTION)
        except PackageNotFoundError:  # a source tree that was never installed
            found = "0.0.0+unknown"
        globals()["__version__"] = found
        return found
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _loaded(name: str) -> Any:
    """A lazy attribute from inside this module, where a bare name does not reach ``__getattr__``."""
    return globals()[name] if name in globals() else __getattr__(name)


def _names_commit(revision: str, commit: str) -> bool:
    """True when the requested ``revision`` is ``commit`` itself, in full or abbreviated (7 or more
    characters, git's own shortest default), so it is not printed a second time."""
    return len(revision) >= 7 and commit.lower().startswith(revision.lower())


def _git_install() -> str:
    """`` (git <revision>, commit <8 hex>)`` or `` (git commit <8 hex>)`` when this installation was made
    from a git URL, else ``""``. Installers record that in the distribution's ``direct_url.json`` (PEP 610).
    Only that file is read: git is not run and nothing is fetched."""
    try:
        text = _loaded("distribution")(DISTRIBUTION).read_text("direct_url.json")
        info = json.loads(text)["vcs_info"] if text and len(text) <= _MAX_DIRECT_URL else None
        if not isinstance(info, dict) or info.get("vcs") != "git":
            return ""  # an index, wheel, archive or local directory install, or another VCS
        commit, revision = info.get("commit_id"), info.get("requested_revision")
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            return ""
        if isinstance(revision, str) and _REVISION.fullmatch(revision) and not _names_commit(revision, commit):
            return f" (git {revision}, commit {commit[:8].lower()})"
        return f" (git commit {commit[:8].lower()})"
    except Exception:  # unreadable or malformed metadata must never break `contexto --version`
        return ""


def version_line() -> str:
    """What ``contexto --version`` prints: ``contexto <version>``, followed by the git revision when the
    installation came from a git URL. A git install carries the placeholder version, so the revision is
    what tells two of them apart. ``__version__`` is the version alone."""
    return f"contexto {_loaded('__version__')}{_git_install()}"
