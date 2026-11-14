"""Write the sample report that test_bench_report.py compares byte for byte (D66).

Input: nothing but the code. The output directory is the fixture ``test_bench_records.make_run``
builds (real generated scripts for seeds 11 to 16, the first seed three times, answers and request
records invented by a seeded generator). ``contexto.bench.report.write`` turns it into
``results.md`` and the two charts, which are copied into
``tests/fixtures/bench_report_invented_figures/``. Zero spend: no arm is run and nothing is sent
anywhere.

The figures are invented; the files show what a report looks like, not how any arm performs. So
that a file met on its own cannot be taken for a result, the fixture's manifest carries a
``label`` saying so, which the report prints in the first line of ``results.md`` and in the title
of each chart, its model is named ``invented-figures-no-model-was-run``, and the directory and the
Markdown file have "invented" in their names. The charts keep the names and the place the report
gives them (``charts/accuracy.svg`` and ``charts/request-size.svg`` beside the Markdown), so the
links in ``invented-results.md`` resolve in the committed directory and every file is compared
byte for byte with what the report writes; their titles carry the label.

Run it again after a deliberate change to the report's wording or to a chart:

    uv run --with pytest python tests/fixtures/make_bench_report.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(__file__).with_name("bench_report_invented_figures")
# What the report writes, and the name of its committed copy.
FILES = {"results.md": "invented-results.md", "charts/accuracy.svg": "charts/accuracy.svg",
         "charts/request-size.svg": "charts/request-size.svg"}
SEEDS = (11, 12, 13, 14, 15, 16)
REPEATS = 3
LABEL = "Figures invented by a test generator: no model was run"
MODEL = "invented-figures-no-model-was-run"


def build(directory: Path) -> Path:
    """Write the fixture run and its report into ``directory`` and return the run's directory."""
    from contexto.bench import report
    from test_bench_records import make_run

    run = make_run(directory / "run", seeds=SEEDS, repeats=REPEATS, model=MODEL, label=LABEL)
    report.write(run)
    return run


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        run = build(Path(tmp))
        for name, committed in FILES.items():
            (OUT / committed).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(run / name, OUT / committed)
            print(f"written: {(OUT / committed).relative_to(OUT.parent.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
