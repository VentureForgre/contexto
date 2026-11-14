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
and ``"0"`` or ``""`` in the settings file turned off a ``1`` in the environment. On 2.1.322 (the same
spike, review of PR #258): a settings file that starts with a UTF-8 byte order mark is read like any
other, so ``settings_env`` reads through the mark.

``CLAUDE_CODE_IGNORE_PERSISTED_TRUST``: in ``-p`` mode Claude Code then drops a project file's ``env``
(D52), this variable included (measured on 2.1.322: every request went through the proxy), so only the
user's file and the environment decide. Interactively it shows its trust dialog and applies the
project's ``env`` once the dialog is accepted (D52, measured for ``ANTHROPIC_BASE_URL``; not measured
for a provider variable). So a provider variable that only a project file turns on is still reported,
marked ``after_trust``, and the warning says it may apply, not that it does.

Not a bypass: ``CLAUDE_CODE_USE_GATEWAY``, a seventh ``CLAUDE_CODE_USE_`` variable in the 2.1.322
program, which needs ``ANTHROPIC_BASE_URL`` and ``ANTHROPIC_AUTH_TOKEN``. Measured on 2.1.322 in ``-p``
mode with both set, the variable in the environment and then in the project's ``env`` block: every model
request went through the proxy and none to the environment's own base URL. It is not in
``PROVIDER_VARIABLES``.

Limits: organization-managed settings are not read (as in ``contexto.upstream``). Claude Code can also
be set up for a gateway by a login it holds ("because you are signed in to one", in the 2.1.322
program); that is not looked for, and what it does to the proxy's base URL was not measured.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contexto.cliversions import REPORT_URL
from contexto.upstream import (
    CONFIG_DIR_KEY,
    IGNORE_TRUST_KEY,
    JS_WHITESPACE,
    KIND_FILES,
    PROJECT_KINDS,
    TRUTHY,
    claude_settings_order,
    ignores_persisted_trust,
    read_settings,
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
    # Set by a project file while CLAUDE_CODE_IGNORE_PERSISTED_TRUST is on: Claude Code -p drops it, and
    # interactive Claude Code applies it once its trust dialog is accepted (the module docstring).
    after_trust: bool = False


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
        # Only the string matters, and of the numbers only 1 (1, 1.0, 1e0) prints as one of Claude Code's
        # "on" words; any other prints as something that is not, whatever its digits (PR #258's
        # re-review: rounding to six digits read 1.0000000001 as on).
        return "1" if value == 1 else repr(value)
    return None


def settings_env(path: Path) -> dict[str, Any]:
    """The ``env`` block of a settings file; empty when the file is absent, unreadable, not JSON, or has
    no such block (as ``contexto.upstream`` reads a file it cannot use: as if it were absent). A UTF-8
    byte order mark at the start is skipped, as Claude Code skips it (the module docstring): read as
    plain UTF-8 the file was not JSON, and a variable it turned on went unreported. ``read_settings``
    also skips a file that is not a regular file: a named pipe at any settings path blocked the launch."""
    data = read_settings(path)
    block = data.get("env") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def claude_env(names: Iterable[str], cwd: Path, home: Path, env: Mapping[str, str], *,
               after_trust: bool = False) -> dict[str, tuple[str, str]]:
    """``{name: (value, source)}`` for each of ``names`` a Claude Code started in ``cwd`` would have set:
    the highest settings file whose ``env`` block holds it (``settings:<kind>``), else the environment
    (``env``). No project file counts while ``env`` sets ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST``, as for
    the upstream (D52), unless ``after_trust``: then the project files count as they do once Claude
    Code's trust dialog is accepted. A value may be empty: a file that sets a variable to nothing unsets
    it."""
    names = list(names)
    decided: dict[str, tuple[str, str]] = {}
    no_trust = ignores_persisted_trust(env) and not after_trust
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
    (the first is the provider it uses), each with where its deciding value is set. While ``env`` sets
    ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST``, the variables that only a project file turns on follow,
    marked ``after_trust`` (the module docstring): leaving them out would say nothing about a bypass
    that interactive Claude Code may make."""
    def on(decided: dict[str, tuple[str, str]], after_trust: bool = False) -> list[ProviderOverride]:
        return [ProviderOverride(variable, provider, decided[variable][1], after_trust)
                for variable, provider in PROVIDER_VARIABLES.items()
                if variable in decided and is_on(decided[variable][0])]

    found = on(claude_env(PROVIDER_VARIABLES, cwd, home, env))
    if ignores_persisted_trust(env):
        now = {o.variable for o in found}
        later = on(claude_env(PROVIDER_VARIABLES, cwd, home, env, after_trust=True), after_trust=True)
        found += [o for o in later if o.variable not in now]  # on only because a project file says so
    return found


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
    """``A is set in <place> and B is set in <place>`` for a non-empty list of overrides."""
    return " and ".join(f"{o.variable} is set in {source_text(o.source, env)}" for o in overrides)


def bypass_warning(overrides: list[ProviderOverride], env: Mapping[str, str] | None = None) -> str:
    """One line for ``contexto run claude`` (without the ``contexto: `` prefix): which variables are on
    and where, the provider Claude Code then uses, what that costs, and what to do. ``overrides`` is a
    non-empty ``provider_overrides`` result."""
    return f"warning: {bypass_summary(overrides, env)}. {bypass_remedy(overrides)}"


def bypass_summary(overrides: list[ProviderOverride], env: Mapping[str, str] | None = None) -> str:
    """What ``overrides`` mean, without the remedy: a fact for the variables Claude Code reads now, and
    "may" for those a project file sets while ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST`` is on."""
    now = [o for o in overrides if not o.after_trust]
    later = [o for o in overrides if o.after_trust]
    cost = "the proxy sees none of them and this session is not kept under the bound"
    parts = []
    if now:
        parts.append(f"{bypass_places(now, env)}, so Claude Code sends model requests to {now[0].provider} and not "
                     f"through Contexto: {cost}")
    if later:
        parts.append(f"{bypass_places(later, env)}. {IGNORE_TRUST_KEY} is set, so Claude Code -p ignores that env "
                     "block, but interactive Claude Code may apply it once its trust dialog is accepted"
                     + ("" if now else f", and then send model requests to {later[0].provider} and not through "
                                       f"Contexto: {cost}"))
    return ". Also, ".join(parts)


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
    """The session log's record of the same fact: names and source labels, no values and no paths. A
    variable that may apply only after the trust dialog carries ``after_trust_dialog``."""
    return {"event": "provider_bypass", "provider": overrides[0].provider,
            "variables": [override_record(o) for o in overrides]}


def override_record(override: ProviderOverride) -> dict[str, Any]:
    record: dict[str, Any] = {"variable": override.variable, "source": override.source}
    if override.after_trust:
        record["after_trust_dialog"] = True
    return record


def launch_check(env: Mapping[str, str]) -> tuple[str | None, dict[str, Any] | None]:
    """For ``contexto run claude``, from the working directory: the warning line and the session log
    event, or ``(None, None)`` when no provider variable is on. It never raises: this is a diagnostic,
    and a settings value must not be able to stop a launch (a 400-digit JSON integer once did, with an
    ``OverflowError``; reviewers A and B, PR #258). When the lookup itself fails the line says so, by the
    error's type only (its text can hold a path): saying nothing would hide a bypass that is really there."""
    try:
        found = provider_overrides(Path.cwd(), Path.home(), env)
        return (bypass_warning(found, env), bypass_event(found)) if found else (None, None)
    except Exception as exc:
        name = type(exc).__name__
        return (f"warning: could not check whether a provider variable ({next(iter(PROVIDER_VARIABLES))} and the "
                f"like) sends Claude Code's requests past the proxy ({name}). The launch continues. Report it at "
                f"{REPORT_URL}", {"event": "provider_bypass_check_failed", "error": name})
