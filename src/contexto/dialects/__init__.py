"""Pluggable API dialect registry for the rewrite engine, proxy, and store renderer.

Every model API Contexto supports registers a single ``Dialect`` instance here:

- ``anthropic``: Anthropic Messages API (``/v1/messages``, ``/v1/messages/count_tokens``)
- ``responses``: OpenAI Responses API (``/responses``, ``/v1/responses``)
- ``chat_completions``: OpenAI Chat Completions API (``/v1/chat/completions``, ``/chat/completions``)
- ``gemini``: Google Gemini API (``/v1beta/models/*:generateContent``, ``:streamGenerateContent``, ``:countTokens``)

Callers look up dialects by endpoint path (``dialect_for``), by name (``get``), or list every
registered dialect (``all_dialects``, ``dialect_names``, ``history_fields``) without hard-coding
two-way ``anthropic``/``responses`` branches.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from contexto.rewrite import Dialect

_LOCK = threading.Lock()
_REGISTRY: dict[str, Dialect] = {}
_BUILTINS_LOADED = False


def _ensure_builtins() -> None:
    """Register the built-in dialects lazily, avoiding import cycles with ``rewrite`` and ``codex``."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    with _LOCK:
        if _BUILTINS_LOADED:
            return
        from contexto.codex import RESPONSES
        from contexto.dialects.chat_completions import CHAT_COMPLETIONS
        from contexto.dialects.gemini import GEMINI
        from contexto.rewrite import ANTHROPIC

        for dialect in (ANTHROPIC, RESPONSES, CHAT_COMPLETIONS, GEMINI):
            _REGISTRY.setdefault(dialect.name, dialect)
        _BUILTINS_LOADED = True


def register(dialect: Dialect) -> Dialect:
    """Register ``dialect`` under ``dialect.name`` and return it."""
    _ensure_builtins()
    with _LOCK:
        _REGISTRY[dialect.name] = dialect
    return dialect


def get(name: str) -> Dialect | None:
    """Return the registered dialect named ``name``, or None."""
    _ensure_builtins()
    with _LOCK:
        return _REGISTRY.get(name)


def all_dialects() -> tuple[Dialect, ...]:
    """Every registered dialect, in registration order."""
    _ensure_builtins()
    with _LOCK:
        return tuple(_REGISTRY.values())


def dialect_names() -> tuple[str, ...]:
    """The names of every registered dialect."""
    _ensure_builtins()
    with _LOCK:
        return tuple(_REGISTRY)


def history_fields() -> tuple[str, ...]:
    """Every distinct request history key (``Dialect.field``) across registered dialects."""
    seen: dict[str, None] = {}
    for dialect in all_dialects():
        seen[dialect.field] = None
    return tuple(seen)


def dialect_for(endpoint: str) -> Dialect | None:
    """The registered dialect that handles ``endpoint``, or None when passed through unchanged."""
    path = endpoint.split("?", 1)[0]
    for dialect in all_dialects():
        if dialect.matches_endpoint(path):
            return dialect
    return None


def api_for(endpoint: str) -> str | None:
    """The proxy log ``api`` label for ``endpoint``, or None when not a transformed endpoint."""
    dialect = dialect_for(endpoint)
    return dialect.api_name if dialect is not None else None


def is_transformed_path(endpoint: str) -> bool:
    """Whether ``endpoint`` is handled by a registered dialect."""
    return dialect_for(endpoint) is not None


def is_count_tokens_path(endpoint: str) -> bool:
    """Whether ``endpoint`` is a token-counting endpoint of any registered dialect."""
    path = endpoint.split("?", 1)[0]
    dialect = dialect_for(path)
    return dialect is not None and dialect.is_count_endpoint(path)


def is_usage_feedback_path(endpoint: str) -> bool:
    """Whether ``endpoint`` is a generation endpoint (transformed and not a token-count endpoint)."""
    path = endpoint.split("?", 1)[0]
    dialect = dialect_for(path)
    return dialect is not None and not dialect.is_count_endpoint(path)


def model_from_endpoint(endpoint: str) -> str | None:
    """Extract the model name from an endpoint path that embeds it (such as Gemini), or None."""
    from contexto.dialects.gemini import gemini_model_from_path

    return gemini_model_from_path(endpoint.split("?", 1)[0])
