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
# Why a unit stopped. "budget", "auth" and "managed_settings" end the whole run; after any other the next
# unit runs. "runaway": the unit passed a per-turn request cap or its token cap (contexto.bench.meter).
# "api_error": the full arm's request kept failing, or a Claude Code turn passed the meter's cap on failed
# responses.
# "unmetered": a Claude Code turn ended with no request through the meter, so something sent Claude Code
# around it. "desync": Claude Code printed a result that answered no turn, so its replies could not be matched
# to the turns. "interrupted": Ctrl+C, SIGTERM, or (found on resume) a run killed while the unit ran.
STOPS = ("budget", "auth", "cli_exit", "cli_error", "thrashing", "timeout", "api_error", "interrupted", "error",
         "runaway", "unmetered", "desync", "managed_settings")
RUN_STOPS = ("budget", "auth", "managed_settings")


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
    toolloop_reads: int | None = None  # distinct files of the tool-loop turn's list that were read
    toolloop_tool_calls: int | None = None  # every tool call the assistant made in that turn
    probe_turns_completed: int = 0
    turns_completed: int = 0
    # probe turn index -> every text the assistant wrote in that turn, in order; never written whole
    answers: dict[int, str] = field(default_factory=dict)

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
    """The stop the meter holds, as a ``Stop``, or None: the run-wide one of its ledger first, then the
    unit's own (a runaway cap)."""
    ledger = unit.meter.ledger
    if ledger.stop == "budget":
        return Stop("budget", f"the token budget would have been passed ({ledger.spent:,} of {ledger.budget:,} "
                              "tokens spent when the next request was refused)")
    if ledger.stop == "auth":
        return Stop("auth", f"the upstream answered {unit.meter.auth_status or 401}: the credential was refused, and "
                            "the run stops at the first such answer")
    if (code := getattr(unit.meter, "unit_stop", None)) in ("runaway", "api_error"):
        return Stop(code, unit.meter.unit_stop_reason or "the unit passed a runaway cap")
    return None


def stop_pending(unit: Unit) -> bool:
    """Whether the meter has a stop for this unit (cheap: two event flags)."""
    stopped = getattr(unit.meter, "stopped", None)
    return unit.meter.ledger.stopped.is_set() or (stopped is not None and stopped.is_set())
