"""The variables that make Claude Code send model requests to another provider, past the proxy (D64).

``contexto run claude`` routes Claude Code through the proxy by setting ``ANTHROPIC_BASE_URL``. Claude
Code reads that variable only for Anthropic's own API. When one of ``PROVIDER_VARIABLES`` is on, it
talks to that provider's endpoint instead (Amazon Bedrock, Google Vertex AI, and so on), the proxy
sees no model request, and nothing is bounded or paged. Nothing fails, so the launcher and
``contexto doctor`` look for these variables and say so.

What is read from Claude Code itself (the 2.1.318 and 2.1.322 binaries, 2026-11-12; the documentation
pages env-vars, amazon-bedrock and llm-gateway on code.claude.com name five of the six):

- The provider is the first variable that is on, in the order of ``PROVIDER_VARIABLES``; none on
  means Anthropic's API. ``CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD`` is in both binaries and not on
  those pages.
- A variable is on when its value, lowercased and trimmed, is ``1``, ``true``, ``yes`` or ``on``,
  the rule ``contexto.upstream.ignores_persisted_trust`` already follows. An empty value, ``0`` and
  any other text leave it off.

A variable can come from the environment or from the ``env`` block of a settings file. The files are
the ones the upstream resolution reads, in the same order and under the same trust rule
(``contexto.upstream.claude_settings_order``, D52). Claude Code merges the ``env`` blocks key by key,
the higher file winning, and applies the result over the environment it started with (M0 finding 1,
measured for ``ANTHROPIC_BASE_URL``), so the highest file that sets a variable decides it, and the
environment only when no file does.

Measured with zero spend on Claude Code 2.1.318 in ``-p`` mode (spikes/m2/providers, 20 cases against
loopback stubs): each of the six variables, set in the environment or in the project's
``.claude/settings.json`` ``env`` block, sent every model request to the provider's endpoint and none
to the proxy; ``1``, ``TRUE`` and a JSON ``true`` were on, ``0``, an empty value and ``enabled`` off;
and ``"0"`` or ``""`` in the settings file turned off a ``1`` in the environment.

Limits: organization-managed settings are not read (as in ``contexto.upstream``). Claude Code also
has a "gateway" provider chosen by a login it holds, not by a variable; that is not looked for.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contexto.upstream import (
    CONFIG_DIR_KEY,
    JS_WHITESPACE,
    KIND_FILES,
    PROJECT_KINDS,
    TRUTHY,
    claude_settings_order,
    ignores_persisted_trust,
)

# Variable -> the provider's name as Claude Code shows it, in the order Claude Code tests them.
PROVIDER_VARIABLES: dict[str, str] = {
    "CLAUDE_CODE_USE_BEDROCK": "Amazon Bedrock",
    "CLAUDE_CODE_USE_FOUNDRY": "Microsoft Foundry",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS": "Claude Platform on AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD": "Claude Platform on Google Cloud",
    "CLAUDE_CODE_USE_MANTLE": "Amazon Bedrock (Mantle)",
    "CLAUDE_CODE_USE_VERTEX": "Google Vertex AI",
}


@dataclass(frozen=True)
class ProviderOverride:
    variable: str
    provider: str
    source: str  # "env", or "settings:<kind>" as ``contexto.upstream.Upstream.source`` names a file


def is_on(value: str) -> bool:
    """Whether Claude Code reads ``value`` as on (see the module docstring)."""
    return value.lower().strip(JS_WHITESPACE) in TRUTHY


def _as_string(value: Any) -> str | None:
    """A settings ``env`` value as the string Claude Code makes of it: a string as it is, a JSON
    boolean or number as JavaScript prints it (``true``, ``1``). None for anything else, which then
    decides nothing: whether Claude Code keeps a file holding such a value was not measured, and
    passing over it errs toward the warning."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return None


def settings_env(path: Path) -> dict[str, Any]:
    """The ``env`` block of a settings file; empty when the file is absent, unreadable, not JSON, or has
    no such block (as ``contexto.upstream`` reads a file it cannot use: as if it were absent)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    block = data.get("env") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def claude_env(names: Iterable[str], cwd: Path, home: Path, env: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """``{name: (value, source)}`` for each of ``names`` a Claude Code started in ``cwd`` would have set:
    the highest settings file whose ``env`` block holds it (``settings:<kind>``), else the environment
    (``env``). No project file counts while ``env`` sets ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST``, as for
    the upstream (D52). A value may be empty: a file that sets a variable to nothing unsets it."""
    names = list(names)
    decided: dict[str, tuple[str, str]] = {}
    no_trust = ignores_persisted_trust(env)
    for kind, path in claude_settings_order(cwd, home, env):
        if kind in PROJECT_KINDS and no_trust:
            continue
        block = settings_env(path)
        for name in names:
            value = _as_string(block.get(name))
            if value is not None and name not in decided:
                decided[name] = (value, f"settings:{kind}")
    for name in names:
        if name not in decided and name in env:
            decided[name] = (env[name], "env")
    return decided


def provider_overrides(cwd: Path, home: Path, env: Mapping[str, str]) -> list[ProviderOverride]:
    """Every provider variable that is on for a Claude Code started in ``cwd``, in Claude Code's order
    (the first is the provider it uses), each with where its deciding value is set."""
    decided = claude_env(PROVIDER_VARIABLES, cwd, home, env)
    return [ProviderOverride(variable, provider, decided[variable][1])
            for variable, provider in PROVIDER_VARIABLES.items()
            if variable in decided and is_on(decided[variable][0])]


def settings_file(kind: str, env: Mapping[str, str] | None = None) -> str:
    """A settings file by its name, for a person, never a path: the user's file is
    ``~/.claude/settings.json``, or ``$CLAUDE_CONFIG_DIR/settings.json`` when ``env`` sets that
    variable (``contexto.upstream.user_settings_path``)."""
    if kind != "user":
        return KIND_FILES.get(kind, kind)
    moved = ((env or {}).get(CONFIG_DIR_KEY) or "").strip()
    return f"${CONFIG_DIR_KEY}/settings.json" if moved else "~/.claude/settings.json"


def source_text(source: str, env: Mapping[str, str] | None = None) -> str:
    """Where a variable is set, for a person: the environment, or the ``env`` block of a settings file
    (``settings_file``)."""
    if source == "env":
        return "the environment"
    return f"the env block of {settings_file(source.removeprefix('settings:'), env)}"


def bypass_places(overrides: list[ProviderOverride], env: Mapping[str, str] | None = None) -> str:
    """``A is set in <place> and B is set in <place>`` for a non-empty ``provider_overrides`` result."""
    return " and ".join(f"{o.variable} is set in {source_text(o.source, env)}" for o in overrides)


def bypass_warning(overrides: list[ProviderOverride], env: Mapping[str, str] | None = None) -> str:
    """One line for ``contexto run claude`` and ``contexto doctor`` (without the ``contexto: `` prefix):
    which variables are on and where, the provider Claude Code then uses, what that costs, and what to
    do. ``overrides`` is a non-empty ``provider_overrides`` result."""
    return (f"warning: {bypass_places(overrides, env)}, so Claude Code sends model requests to "
            f"{overrides[0].provider} and not through Contexto: the proxy sees none of them and this session is "
            f"not kept under the bound. {bypass_remedy(overrides)}")


def bypass_remedy(overrides: list[ProviderOverride]) -> str:
    """What to do about ``overrides``, in one sentence."""
    names = ", ".join(o.variable for o in overrides)
    if all(o.source == "env" for o in overrides):
        how = f"Unset {names}"
    elif len(overrides) == 1:
        how = f"Remove {names} from that env block"
    else:
        how = f"Unset or remove {names}"
    return (f"{how} to send requests through Contexto, or run claude without Contexto to use "
            f"{overrides[0].provider}.")


def bypass_event(overrides: list[ProviderOverride]) -> dict[str, Any]:
    """The session log's record of the same fact: names and source labels, no values and no paths."""
    return {"event": "provider_bypass", "provider": overrides[0].provider,
            "variables": [{"variable": o.variable, "source": o.source} for o in overrides]}
