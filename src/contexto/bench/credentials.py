"""The credential of a live benchmark run: the user's own key, from one of three sources.

- ``ANTHROPIC_API_KEY`` in the environment;
- ``--key-helper COMMAND``: a command run through ``/bin/sh -c`` when a key is needed, whose output
  is the key, the way Claude Code runs an ``apiKeyHelper``;
- ``--settings FILE``: a Claude Code settings file whose ``apiKeyHelper`` is that command, read each
  time a key is needed (the acceptance harness's ``--seat-settings``).

A key is never a command-line value. It is held in memory, sent only in request headers, and never
written to a file, a log, an error message or the manifest: every error here names the step that
failed and nothing the helper printed.

How each arm gets it:

- The ``full`` arm asks ``Credentials.headers()`` before every request: ``X-Api-Key`` for an
  environment key, ``X-Api-Key`` and ``Authorization: Bearer`` for a helper's, which is what Claude
  Code sends for each source.
- The CLI arms get the same source, not a copy of the key. For an environment key the child's
  environment carries ``ANTHROPIC_API_KEY``, as the user's own shell does. For a helper, the
  isolated Claude Code settings name this module as their ``apiKeyHelper``
  (``python -m contexto.bench.credentials``), and the child's environment carries the helper command
  or the settings path in ``CONTEXTO_BENCH_KEY_HELPER`` or ``CONTEXTO_BENCH_KEY_SETTINGS``; Claude
  Code runs the shim when it needs a key, and the shim runs the user's helper and passes its output
  through. So the key is in no file and on no command line, and neither is the helper command or
  the settings path, except on the command line of the one ``/bin/sh -c COMMAND`` process that is
  the helper itself.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ENV_KEY = "ANTHROPIC_API_KEY"
HELPER_VAR = "CONTEXTO_BENCH_KEY_HELPER"
SETTINGS_VAR = "CONTEXTO_BENCH_KEY_SETTINGS"
HELPER_TIMEOUT_S = 60
# Variables that would outrank or replace the run's credential in a child (Claude Code's order:
# ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY, apiKeyHelper, CLAUDE_CODE_OAUTH_TOKEN).
CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


class CredentialError(Exception):
    """No key could be obtained. The message never holds a key, a helper command or its output."""


def run_helper(command: str) -> str:
    """The key a helper command prints. Raises ``CredentialError`` without showing its output."""
    try:
        proc = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, timeout=HELPER_TIMEOUT_S,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise CredentialError(f"the key helper did not finish in {HELPER_TIMEOUT_S} seconds") from None
    except OSError as exc:
        raise CredentialError(f"the key helper could not be run ({type(exc).__name__})") from None
    key = proc.stdout.strip()
    if proc.returncode != 0 or not key:
        raise CredentialError(f"the key helper failed (exit {proc.returncode}); its output is not shown")
    return key


def settings_helper(path: Path) -> str:
    """The ``apiKeyHelper`` command of a Claude Code settings file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CredentialError(f"the settings file cannot be read ({type(exc).__name__})") from None
    except ValueError:
        raise CredentialError("the settings file is not JSON") from None
    helper = data.get("apiKeyHelper") if isinstance(data, dict) else None
    if not isinstance(helper, str) or not helper.strip():
        raise CredentialError("the settings file has no apiKeyHelper")
    return helper


@dataclass(frozen=True)
class Credentials:
    """Where the run's key comes from. ``source`` is ``"env"``, ``"helper"`` or ``"settings"``."""

    source: str
    _helper: str | None = field(default=None, repr=False)
    _settings: Path | None = field(default=None, repr=False)
    _key: str | None = field(default=None, repr=False)

    def key(self) -> str:
        """The key, obtained now: a helper is run at every call, as Claude Code runs it."""
        if self.source == "env":
            if not self._key:
                raise CredentialError(f"{ENV_KEY} is not set")
            return self._key
        if self.source == "settings":
            assert self._settings is not None
            return run_helper(settings_helper(self._settings))
        assert self._helper is not None
        return run_helper(self._helper)

    def headers(self) -> dict[str, str]:
        """The credential headers of a direct request, in the form Claude Code sends for this source."""
        key = self.key()
        if self.source == "env":
            return {"x-api-key": key}
        return {"x-api-key": key, "authorization": f"Bearer {key}"}

    def child_env(self) -> dict[str, str]:
        """What a CLI child's environment needs so that Claude Code finds the same credential."""
        if self.source == "env":
            return {ENV_KEY: self.key()}
        if self.source == "settings":
            return {SETTINGS_VAR: str(self._settings)}
        return {HELPER_VAR: str(self._helper)}

    def claude_settings(self) -> dict[str, str]:
        """The keys the isolated Claude Code settings file needs: the shim as ``apiKeyHelper`` for a
        helper source, nothing for an environment key."""
        if self.source == "env":
            return {}
        return {"apiKeyHelper": f"{shlex.quote(sys.executable)} -P -m contexto.bench.credentials"}


def resolve(key_helper: str | None, settings: Path | None, environ: Mapping[str, str] | None = None) -> Credentials:
    """The run's credential from the options and the environment. An option outranks the environment
    variable, since the user gave it for this run. Raises ``CredentialError`` when there is none or two."""
    environ = os.environ if environ is None else environ
    if key_helper is not None and settings is not None:
        raise CredentialError("give --key-helper or --settings, not both")
    if key_helper is not None:
        if not key_helper.strip():
            raise CredentialError("--key-helper is empty")
        return Credentials("helper", _helper=key_helper)
    if settings is not None:
        resolved = settings.expanduser().resolve()
        settings_helper(resolved)  # fail now, not at the first request
        return Credentials("settings", _settings=resolved)
    key = (environ.get(ENV_KEY) or "").strip()
    if not key:
        raise CredentialError(f"no key: set {ENV_KEY}, or pass --key-helper COMMAND or --settings FILE")
    return Credentials("env", _key=key)


def main() -> int:
    """The ``apiKeyHelper`` shim Claude Code runs in a CLI arm: print the user's helper's key."""
    try:
        if os.environ.get(SETTINGS_VAR):
            command = settings_helper(Path(os.environ[SETTINGS_VAR]))
        elif os.environ.get(HELPER_VAR):
            command = os.environ[HELPER_VAR]
        else:
            raise CredentialError("no key helper is named in the environment")
        sys.stdout.write(run_helper(command) + "\n")
    except CredentialError as exc:
        print(f"contexto bench: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
