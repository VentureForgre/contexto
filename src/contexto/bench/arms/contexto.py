"""The ``contexto`` arm: headless Claude Code under ``contexto run``, one process for the session.

The command is ``contexto run --bound B --upstream <meter> claude -p --input-format stream-json
--output-format stream-json --verbose --model M``, run with the interpreter that runs the benchmark
(``python -m contexto``), so the Contexto measured is the one installed with this command and not
whichever is first on PATH. ``--upstream`` is the unit's meter, which forwards to the real upstream
(``contexto.bench.meter`` says what that changes). The session has its own ``CONTEXTO_HOME`` under
the unit directory. A bound above the launcher's guard gets ``--allow-large-bound``, as a user would
have to pass.
"""

from __future__ import annotations

import sys

from contexto.bench.arms import Unit, UnitResult
from contexto.bench.arms.cli import CLAUDE_ARGS, child_env, isolated_config, run_session


def command(unit: Unit) -> list[str]:
    from contexto.launcher import BOUND_GUARD

    large = ["--allow-large-bound"] if unit.bound > BOUND_GUARD else []
    return [sys.executable, "-P", "-m", "contexto", "run", "--bound", str(unit.bound), *large,
            "--upstream", unit.upstream, "claude", *CLAUDE_ARGS, "--model", unit.model]


def run(unit: Unit) -> UnitResult:
    env = child_env(unit, isolated_config(unit))
    home = unit.directory / "contexto-home"
    home.mkdir(parents=True, exist_ok=True)
    env["CONTEXTO_HOME"] = str(home)
    return run_session(unit, command(unit), env)
