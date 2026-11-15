"""The ``plain`` arm: the same headless Claude Code with no Contexto, compacting on its own.

Claude Code has no setting for the token count at which it compacts. Every documented control sets
the auto-compact window W, and 2.1.322 compacts when its count reaches W minus 33,000 (20,000 for
the summary's output and 13,000 more; read in the 2.1.322 bundle, not documented, and not confirmed
by a live run: the only documented figure is that a 1,000,000 window compacts "at about 967K"). So
for a bound B the arm sets ``CLAUDE_CODE_AUTO_COMPACT_WINDOW = B + 33000``, which outranks the flag
and the settings. W must be 100,000 to 1,000,000, so this works for bounds from 67,000 to 967,000;
outside that range the trigger cannot be put at the bound, the nearest window is set and the
manifest says so. The count Claude Code compares is the last response's usage plus its own estimate
of what was added since, so a large tool result can carry a session past the trigger before the
check runs; each compaction's ``pre_tokens`` is recorded so a reader can see how far.

Everything else is as in the ``contexto`` arm (``contexto.bench.arms.cli``); the base URL is the
unit's meter.
"""

from __future__ import annotations

from typing import Any

from contexto.bench.arms import Unit, UnitResult
from contexto.bench.arms.cli import CLAUDE_ARGS, child_env, isolated_config, run_session

VARIABLE = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
RESERVE = 33_000
MIN_WINDOW, MAX_WINDOW = 100_000, 1_000_000
MIN_BOUND, MAX_BOUND = MIN_WINDOW - RESERVE, MAX_WINDOW - RESERVE


def compaction_setting(bound: int) -> dict[str, Any]:
    """What the plain arm sets for a bound: the manifest's ``plain_compaction``."""
    window = min(MAX_WINDOW, max(MIN_WINDOW, bound + RESERVE))
    forced = MIN_BOUND <= bound <= MAX_BOUND
    basis = (f"Claude Code compacts at its auto-compact window minus {RESERVE:,} tokens (read in the 2.1.322 bundle, "
             "not documented, unconfirmed by a live run)")
    if forced:
        note = f"{basis}, so a window of {window:,} is intended to compact at the bound, {bound:,}."
    else:
        note = (f"The threshold cannot be forced to the bound, {bound:,}: {basis}, and the window must be "
                f"{MIN_WINDOW:,} to {MAX_WINDOW:,} tokens, so only bounds from {MIN_BOUND:,} to {MAX_BOUND:,} can be "
                f"matched. The nearest window, {window:,}, was set; it is intended to compact at {window - RESERVE:,}.")
    return {"variable": VARIABLE, "value": window, "intended_trigger": window - RESERVE, "forced": forced, "note": note}


def command(unit: Unit) -> list[str]:
    return [unit.claude, *CLAUDE_ARGS, "--model", unit.model]


def run(unit: Unit) -> UnitResult:
    env = child_env(unit, isolated_config(unit))
    env["ANTHROPIC_BASE_URL"] = unit.upstream
    env[VARIABLE] = str(compaction_setting(unit.bound)["value"])
    return run_session(unit, command(unit), env)
