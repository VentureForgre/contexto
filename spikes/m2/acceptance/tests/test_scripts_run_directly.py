"""The acceptance scripts run the way docs/research/M2-acceptance-harness.md runs them
(``python spikes/m2/acceptance/run.py ...``). Each imports ``acceptance.script`` before ``fidelity``,
because ``script`` puts ``spikes/m0`` on ``sys.path``; an import sort that moved ``fidelity`` first
broke all three (PR #213). The tests in this directory import ``acceptance.script`` early, so only a
fresh interpreter shows the order."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ACCEPTANCE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["run.py", "classa.py", "codex_dryrun.py"])
def test_script_runs_directly(name):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, str(ACCEPTANCE / name), "--help"], capture_output=True, text=True,
                          env=env, cwd=ACCEPTANCE.parents[2], timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(f"usage: {name}")
