"""The short scale run, once per test session, and its numbers at the end of the pytest output.

The run's measurements are printed whether its tests pass or fail, so a CI log always holds the numbers the
budgets in ``short.BUDGETS`` were compared against on that runner. When the run itself raises (the fixture
errors), the step it failed in and what it had measured by then are printed instead.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scale.proxy import short  # noqa: E402

# Set to a file path to append each run's measurements to it, one JSON object per line (the flake check
# in README.md, "The short run", reads the spread from it).
OUT_ENV = "CONTEXTO_SCALE_SHORT_OUT"
REPORTS: list[str] = []


@pytest.fixture(scope="session")
def short_run(tmp_path_factory):
    progress: dict = {}
    try:
        measured = short.measure(home=tmp_path_factory.mktemp("scale-short"), progress=progress)
    except BaseException as exc:  # the fixture errors: still print the step and what was measured by then
        REPORTS.append(short.failure_report(exc, progress))
        raise
    REPORTS.append(short.report(measured))
    out = os.environ.get(OUT_ENV)
    if out:
        with open(out, "a") as f:
            f.write(json.dumps({k: v for k, v in measured.items() if k != "rows"}) + "\n")
    return measured


def pytest_terminal_summary(terminalreporter):
    for text in REPORTS:
        terminalreporter.section("scale harness, short run")
        terminalreporter.write_line(text)
