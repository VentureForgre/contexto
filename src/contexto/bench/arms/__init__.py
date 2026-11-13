"""The three arms of the live benchmark. Each is a module with ``run(unit) -> UnitResult``.

A unit is one arm on one script: ``Unit`` carries the script, the model, the bound, the credentials,
the upstream (the unit's meter, which forwards to the real one) and the unit's own directory.

- ``contexto``: headless Claude Code under ``contexto run``.
- ``plain``: the same headless Claude Code with no Contexto and its auto-compaction set from the bound.
- ``full``: direct Messages API requests carrying the whole script.

``run`` returns a ``UnitResult`` and raises nothing for a stop it understands (the budget, a refused
credential, Claude Code exiting, a turn with no result): those are ``status="stopped"`` with a
``stop`` code and a ``reason`` in plain words that holds no private detail. Ctrl+C passes through as
``KeyboardInterrupt`` after the arm has stopped its child processes.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ARMS = ("contexto", "full", "plain")
CLI_ARMS = ("contexto", "plain")
# Why a unit stopped. "budget" and "auth" end the whole run; after any other the next unit runs.
STOPS = ("budget", "auth", "cli_exit", "cli_error", "thrashing", "timeout", "api_error", "interrupted", "error")


@dataclass
class Unit:
    arm: str
    script: Any  # contexto.bench.model.Script
    model: str
    bound: int
    credentials: Any  # contexto.bench.credentials.Credentials
    upstream: str  # the unit's meter
    directory: Path  # the unit's own directory: fresh, outside any git work tree
    meter: Any  # contexto.bench.meter.Meter: the arm sets meter.turn before each turn
    cache_ttl: str = "5m"
    turn_timeout: float = 1_800.0
    context_window: int | None = None
    claude: str = "claude"  # the Claude Code executable


@dataclass
class UnitResult:
    status: str = "complete"  # "complete", "stopped" or "not_run"
    stop: str | None = None  # one of STOPS when stopped
    reason: str | None = None
    cli_exit: int | None = None
    compactions: int | None = None
    compaction_pre_tokens: list[int] = field(default_factory=list)
    concluded_ok: dict[str, bool] = field(default_factory=dict)
    toolloop_reads: int | None = None
    probe_turns_completed: int = 0
    turns_completed: int = 0
    answers: dict[int, str] = field(default_factory=dict)  # probe turn index -> the reply; never written whole

    def stopped(self, stop: str, reason: str) -> UnitResult:
        self.status, self.stop, self.reason = "stopped", stop, reason
        return self


class Stop(Exception):
    """An arm's unit cannot go on. ``code`` is one of ``STOPS``."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


def runner(arm: str) -> Callable[[Unit], UnitResult]:
    """The ``run`` function of an arm, imported when first asked for."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}: the arms are {', '.join(ARMS)}")
    return importlib.import_module(f"{__name__}.{arm}").run


def ledger_stop(unit: Unit) -> Stop | None:
    """The run-wide stop the meter's ledger holds, as a ``Stop``, or None."""
    ledger = unit.meter.ledger
    if ledger.stop == "budget":
        return Stop("budget", f"the token budget would have been passed ({ledger.spent:,} of {ledger.budget:,} "
                              "tokens spent when the next request was refused)")
    if ledger.stop == "auth":
        return Stop("auth", f"the upstream answered {unit.meter.auth_status or 401}: the credential was refused, and "
                            "the run stops at the first such answer")
    return None
