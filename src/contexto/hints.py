"""Claude Code's gateway hint headers (D70): their names, their fixed vocabularies, and who gets them.

Claude Code documents routing hints for a gateway (``llm-gateway-protocol``, "Gateway hint headers";
Claude Code 2.1.273 or later, ``x-claude-code-prompt-id`` from 2.1.283). With a custom base URL they
are off unless ``CLAUDE_CODE_GATEWAY_HINT_HEADERS`` is on. Measured on Claude Code 2.1.322 against a
loopback stub (docs/research/D70-serve-probes.md):

- ``x-claude-code-request-class``: on every Messages and ``count_tokens`` request. ``main`` for a turn
  of the main conversation, ``subagent`` for a subagent's turn, ``compaction`` for the request that
  summarizes a conversation, ``auxiliary`` for the session title and for ``count_tokens``. The docs
  add ``workflow`` (an agent running inside a workflow), which was not provoked.
- ``x-claude-code-compaction``: on the summarization request, with what triggered it (``manual``
  measured; ``auto`` and ``reactive`` from the docs).
- ``x-claude-code-context-compacted``: once, on the first main request after a compaction, with the
  same values.
- ``x-claude-code-prompt-id`` (a random UUID per user prompt), ``x-claude-code-agent-type`` (on a
  subagent's own turns) and ``x-claude-code-prev-tool-durations`` (``Bash=742;Read=9``).

Without the variable, or with it ``0``, none of the six is sent. ``X-Claude-Code-Session-Id``,
``x-claude-code-agent-id`` and ``x-claude-code-parent-agent-id`` are sent either way and are not hint
headers here.

``contexto run claude`` turns the hints on in the ``--settings`` file it passes, for the rewrite
engine (``contexto.rewrite.RewriteEngine._classify``). The docs give the reason they are off for a
custom base URL: "a proxy that rejects unknown headers would fail the request". So the proxy removes
the six before it forwards (``ContextoProxy(strip_hint_headers=True)``), and the upstream gets the
headers it got before Contexto asked for them. A launch is in one of three states
(``hint_headers_state``): ``stripped`` as just described; ``forwarded`` when the user turned the hints
on themselves, since their gateway was already getting them; ``off`` when the user turned them off,
in which case Contexto does not ask for them and the engine reads its signatures.

Limit: Claude Code applies the ``env`` block of the ``--settings`` file to its own environment, so
every process the session starts inherits ``CLAUDE_CODE_GATEWAY_HINT_HEADERS=1``. A nested
``contexto run claude`` tells that value from the user's by the marker written beside it
(``HINT_MARKER_KEY``). A nested plain ``claude`` that is pointed at another base URL has no Contexto
proxy in its path, so it sends the hints to that URL. The way out is the explicit off: with
``CLAUDE_CODE_GATEWAY_HINT_HEADERS=0`` in the environment or the user settings file Contexto sets
nothing.

Only two of the values are ever logged, and only after they were matched against the documented set:
the class and the compaction trigger. A prompt id, an agent type or a tool name never reaches a log.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from contexto.upstream import (
    SettingsFiles,
    claude_env_value,
    env_falsy,
    env_truthy,
    managed_settings_path,
    settings_file_env,
    user_settings_path,
)

HINT_HEADERS_KEY = "CLAUDE_CODE_GATEWAY_HINT_HEADERS"
# Written beside HINT_HEADERS_KEY in the launcher's ``--settings`` ``env`` block: what that launch
# decided (one of HINT_STATES). Claude Code applies the block to its own environment, so a nested
# ``contexto run claude`` inherits both and can tell Contexto's ``1`` from the user's.
HINT_MARKER_KEY = "CONTEXTO_HINT_HEADERS"
STRIPPED, FORWARDED, OFF = "stripped", "forwarded", "off"
HINT_STATES = (STRIPPED, FORWARDED, OFF)
REQUEST_CLASS_HEADER = "x-claude-code-request-class"
CONTEXT_COMPACTED_HEADER = "x-claude-code-context-compacted"
COMPACTION_HEADER = "x-claude-code-compaction"
PROMPT_ID_HEADER = "x-claude-code-prompt-id"
AGENT_TYPE_HEADER = "x-claude-code-agent-type"
PREV_TOOL_DURATIONS_HEADER = "x-claude-code-prev-tool-durations"
# The six headers CLAUDE_CODE_GATEWAY_HINT_HEADERS turns on, in lower case.
GATED_HINT_HEADERS = frozenset({REQUEST_CLASS_HEADER, CONTEXT_COMPACTED_HEADER, COMPACTION_HEADER, PROMPT_ID_HEADER,
                                AGENT_TYPE_HEADER, PREV_TOOL_DURATIONS_HEADER})

# x-claude-code-request-class. MAIN_CLASSES are requests of the main conversation itself; SIDE_CLASSES
# are every other request Claude Code sends for a session.
MAIN_CLASSES = frozenset({"main", "compaction"})
SIDE_CLASSES = frozenset({"subagent", "workflow", "auxiliary"})
AGENT_CLASSES = frozenset({"subagent", "workflow"})
REQUEST_CLASSES = MAIN_CLASSES | SIDE_CLASSES
# x-claude-code-compaction and x-claude-code-context-compacted.
COMPACTION_TRIGGERS = frozenset({"auto", "manual", "reactive"})
OTHER_TRIGGER = "other"  # the header is there with a value outside COMPACTION_TRIGGERS


class GatewayHints(NamedTuple):
    """What a request's hint headers say, reduced to the documented vocabularies."""

    request_class: str | None  # one of REQUEST_CLASSES, or None (absent, or a value Contexto does not know)
    context_compacted: str | None  # one of COMPACTION_TRIGGERS, OTHER_TRIGGER, or None when absent
    compaction: str | None  # the same, for the summarization request


NO_HINTS = GatewayHints(None, None, None)


def _trigger(value: str | None) -> str | None:
    if not value:
        return None
    return value if value in COMPACTION_TRIGGERS else OTHER_TRIGGER


def gateway_hints(headers: Mapping[str, str]) -> GatewayHints:
    """The hints of one request. Header names match in any case; values are compared as sent (Claude
    Code sends them in lower case). An unknown class reads as no class, so the caller falls back to its
    signatures; a compaction header with an unknown value still says a compaction happened, and reads
    as ``other``. Nothing else is taken from the headers, and no value outside the fixed sets is
    returned, so everything here is safe to log."""
    found: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in (REQUEST_CLASS_HEADER, CONTEXT_COMPACTED_HEADER, COMPACTION_HEADER) and isinstance(value, str):
            found[lower] = value
    request_class = found.get(REQUEST_CLASS_HEADER)
    return GatewayHints(request_class if request_class in REQUEST_CLASSES else None,
                        _trigger(found.get(CONTEXT_COMPACTED_HEADER)), _trigger(found.get(COMPACTION_HEADER)))


def without_hint_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """``headers`` without the six gated hint headers, in the same order. The session id, agent id
    and parent agent id headers are kept: Claude Code sends them with or without the variable."""
    return {name: value for name, value in headers.items() if name.lower() not in GATED_HINT_HEADERS}


def hint_headers_state(*, env: Mapping[str, str] | None = None, cwd: Path | None = None, home: Path | None = None,
                       files: SettingsFiles | None = None, managed: Path | None = None) -> str:
    """What ``contexto run claude`` does about the hint headers for a launch from ``cwd``:

    - ``forwarded``: the user had the hints on, so the upstream was already getting them. Contexto
      asks for them too and the proxy removes nothing.
    - ``off``: the user turned them off. Contexto does not ask for them (the variable is left out of
      its ``env`` block), Claude Code sends none, the engine reads its signatures, and the proxy
      removes nothing: the launch behaves as it did before D70.
    - ``stripped``: neither. Contexto asks for them and the proxy removes the six before it forwards.

    "On" is the value Claude Code would run with without Contexto's ``--settings`` file
    (``contexto.upstream.claude_env_value``): the managed settings file's, else the first settings
    file ``env`` block that sets ``CLAUDE_CODE_GATEWAY_HINT_HEADERS``, else the process environment's.
    Measured on 2.1.322: ``1``, ``true``, ``yes``, ``on`` and ``TRUE`` send the headers, ``0``,
    ``false`` and ``2`` do not, and a settings file's value wins over the environment's in both
    directions. A project file follows the trust rule ``contexto run claude`` has for the base URL: it
    counts as ``claude -p`` reads it, with no trust record needed, and not at all while
    ``CLAUDE_CODE_IGNORE_PERSISTED_TRUST`` is set. Interactively Claude Code applies a project file
    only after its trust dialog, so a project file that turns the hints on lets them reach the
    upstream from the first request, before the folder is trusted.

    The process environment's value does not count while ``CONTEXTO_HINT_HEADERS`` there is
    ``stripped``: the process then runs inside an outer ``contexto run claude`` session, which put the
    ``1`` there for itself and removes the hints (module docstring). With ``forwarded`` the outer
    launch found the user had them on, so the value is the user's; with ``off`` it set nothing.

    "Off" is, when the hints are not on, one of ``0``, ``false``, ``no``, ``off`` in the process
    environment (unless it is an outer launch's, as above), in the user settings file, or in the
    managed settings file, which outranks Contexto's ``--settings``. A project file's off value is not
    the user's choice and Contexto's ``--settings`` value outranks it (measured), so it leaves the
    launch ``stripped``. A settings file that cannot be read decides nothing."""
    env = os.environ if env is None else env
    home = home or Path.home()
    files = files or SettingsFiles()
    managed = managed or managed_settings_path()
    own = env.get(HINT_MARKER_KEY) != STRIPPED  # whether the environment's value is the user's own
    found = claude_env_value(HINT_HEADERS_KEY, env=env, cwd=cwd, home=home, files=files, managed=managed,
                             process_env=own)
    if env_truthy(found.value):
        return FORWARDED
    said = (env.get(HINT_HEADERS_KEY) if own else None,
            settings_file_env(files, user_settings_path(home, env), HINT_HEADERS_KEY),
            settings_file_env(files, managed, HINT_HEADERS_KEY))
    return OFF if any(env_falsy(value) for value in said) else STRIPPED


def user_enabled_hint_headers(*, env: Mapping[str, str] | None = None, cwd: Path | None = None,
                              home: Path | None = None, files: SettingsFiles | None = None,
                              managed: Path | None = None) -> bool:
    """Whether the user turned the hint headers on themselves, so the upstream was already getting them
    and the proxy must not remove them: ``hint_headers_state`` is ``forwarded``. A value an outer
    ``contexto run claude`` put in the process environment is not the user's."""
    return hint_headers_state(env=env, cwd=cwd, home=home, files=files, managed=managed) == FORWARDED
