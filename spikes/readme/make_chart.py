"""Draw the README chart: the largest request of each turn against the bound, live acceptance run 3.

    python spikes/readme/make_chart.py          # writes docs/assets/request-size-{light,dark}.svg

Standard library only, and deterministic: the same CSV gives the same bytes.

Source: ``docs/research/data/M2-usage-savings-run3.csv``, 44 requests over 24 turns (Claude Code
2.1.301, claude-haiku-5-5, bound 150,000), and its record ``docs/research/M2-usage-savings-run3.md``.
Turns 1 to 18 are content turns and 19 to 24 are probe turns (the CSV's ``kind``). The chart plots two
lines:

- Contexto: for each turn, the largest ``contexto_input_total`` of its requests, the input tokens
  Contexto sent, measured from the API's usage.
- The full history without Contexto, in the record's "plain-run shape", one request a turn. Turns 1 to
  18 are the CSV's ``plain_input_modeled``, modeled from the bytes Claude Code sent to the proxy. Turns
  19 to 24 are ``BASELINE_PROBE_TOKENS``, the baseline arm's measured ``count_tokens``.

The CSV's ``plain_input_modeled`` for turns 19 to 24 is not plotted. It is the history at the points
where Contexto sent a request, tool loops included, and the record says it overstates a plain run.

The two files have the same geometry and differ only in colour: one for GitHub's light page
(``#ffffff``), one for its dark page (``#0d1117``), both with a transparent background. The two lines
differ in colour and in dash, the measured turns of the dashed line carry a dot, and each line has its
label next to a sample of it. Text is at least 4.5:1 against its page and the lines and the bound line
at least 3:1 (``tests/test_readme_assets.py`` checks both).
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = ROOT / "docs" / "research" / "data" / "M2-usage-savings-run3.csv"
ASSETS = ROOT / "docs" / "assets"
RECORD_PATH = ROOT / "docs" / "research" / "M2-usage-savings-run3.md"
BOUND = 150_000
# docs/research/M2-usage-savings-run3.md, "Reference: the baseline arm", line 170: "`count_tokens` per
# request: 435,590 / 436,155 / 436,710 / 437,280 / 437,846 / 438,406". Measured: one full-context request
# per probe turn, without Contexto. A test reads the record and fails if this list is not in it.
BASELINE_PROBE_TOKENS = (435_590, 436_155, 436_710, 437_280, 437_846, 438_406)
Y_MAX = 500_000
Y_STEP = 100_000
X_TICKS = (1, 4, 8, 12, 16, 20, 24)

WIDTH, HEIGHT = 800, 418
LEFT, RIGHT, TOP, BOTTOM = 52, 772, 62, 322  # the plot area
FONT = '-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif'

TITLE = "Largest request in each turn"
SUBTITLE = "Input tokens"
NOTE = ("Acceptance run 3, one scripted session. Contexto measured; full history modeled for turns 1 to 18, "
        "measured for 19 to 24.")
LABEL_CONTEXTO = "Contexto (measured)"
LABEL_PLAIN = "Full history without Contexto (turns 1 to 18 modeled, 19 to 24 measured)"
LABEL_BOUND = f"Bound: {BOUND:,} tokens"
LABEL_DROP = "old turns moved to disk"

# A blue for Contexto and an orange for the full history, a lighter blue and a darker orange on the dark
# page. WCAG contrast against the page: light (#ffffff) blue 4.42, orange 3.20, ink 15.80, muted 6.11;
# dark (#0d1117) blue 5.20, orange 4.87, ink 17.39, muted 6.50. Text and grid are GitHub's own foreground
# and border tones. The grid is under 3:1 on purpose: it sits behind the data.
THEMES = {
    "light": {"page": "#ffffff", "ink": "#1f2328", "muted": "#59636e", "grid": "#d1d9e0",
              "contexto": "#2a78d6", "plain": "#eb6834"},
    "dark": {"page": "#0d1117", "ink": "#f0f6fc", "muted": "#9198a1", "grid": "#3d444d",
             "contexto": "#3987e5", "plain": "#d95926"},
}


@dataclass(frozen=True)
class Series:
    turns: list[int]
    contexto: list[int]  # largest measured Contexto request of each turn
    plain: list[int]  # the full history without Contexto: modeled on content turns, measured on probe turns
    first_probe: int | None  # the first turn whose kind is "probe"
    measured: list[int]  # the turns whose ``plain`` value is measured


def read_series(path: Path = CSV_PATH, probe_tokens: tuple[int, ...] = BASELINE_PROBE_TOKENS) -> Series:
    """The two lines. ``probe_tokens`` are the measured full-history sizes of the probe turns, in order."""
    contexto: dict[int, int] = {}
    plain: dict[int, int] = {}
    probes: set[int] = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            turn = int(row["turn"])
            contexto[turn] = max(contexto.get(turn, 0), int(row["contexto_input_total"]))
            plain[turn] = max(plain.get(turn, 0), int(row["plain_input_modeled"]))
            if row["kind"] == "probe":
                probes.add(turn)
    turns = sorted(contexto)
    if not turns:
        raise ValueError(f"{path} has no rows")
    measured = sorted(probes)
    if len(measured) != len(probe_tokens):
        raise ValueError(f"{path} has {len(measured)} probe turns and there are {len(probe_tokens)} measured sizes")
    plain.update(zip(measured, probe_tokens))
    return Series(turns, [contexto[t] for t in turns], [plain[t] for t in turns], measured[0] if measured else None,
                  measured)


def n(value: float) -> str:
    """A coordinate with at most one decimal, so the output does not depend on float printing."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def x_of(turn: float, series: Series) -> float:
    first, last = series.turns[0], series.turns[-1]
    return LEFT + (turn - first) / (last - first) * (RIGHT - LEFT)


def y_of(tokens: float) -> float:
    return BOTTOM - tokens / Y_MAX * (BOTTOM - TOP)


def path_of(series: Series, values: list[int]) -> str:
    return "M" + "L".join(f"{n(x_of(t, series))} {n(y_of(v))}" for t, v in zip(series.turns, values))


def dot(x: float, y: float, colour: str, page: str, r: float = 4.5) -> str:
    """A point marker with a ring in the page colour, so it reads on top of a line."""
    return f'<circle cx="{n(x)}" cy="{n(y)}" r="{n(r)}" fill="{colour}" stroke="{page}" stroke-width="2"/>'


def render(series: Series, theme: str) -> str:
    c = THEMES[theme]
    peak_c = max(range(len(series.turns)), key=lambda i: series.contexto[i])
    end_p = len(series.turns) - 1
    modeled = [t for t in series.turns if t not in series.measured]
    parts = (f"modeled for turns {modeled[0]} to {modeled[-1]}, measured for turns {series.measured[0]} to "
             f"{series.measured[-1]}" if modeled and series.measured else "modeled")
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}" '
        f'role="img" aria-labelledby="t d">',
        f'<title id="t">{TITLE}: Contexto against the full history, with a bound of {BOUND:,} tokens</title>',
        f'<desc id="d">Over {len(series.turns)} turns the largest Contexto request is {max(series.contexto):,} input '
        f"tokens (measured), under the bound of {BOUND:,}. The full history without Contexto grows to "
        f"{series.plain[end_p]:,} ({parts}). {NOTE}</desc>",
        f"<style>text{{font-family:{FONT};font-size:12px;fill:{c['muted']}}}.ink{{fill:{c['ink']}}}"
        f".title{{font-size:15px;font-weight:600}}.label{{font-size:13px}}.end{{text-anchor:end}}"
        f".mid{{text-anchor:middle}}.grid{{stroke:{c['grid']};stroke-width:1}}"
        f".series{{fill:none;stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}}"
        f".contexto{{stroke:{c['contexto']}}}.plain{{stroke:{c['plain']};stroke-dasharray:7 5}}"
        f".bound{{stroke:{c['muted']};stroke-width:1.5}}.span{{stroke:{c['muted']};stroke-width:1;fill:none}}</style>",
        f'<text class="ink title" x="{LEFT}" y="22">{TITLE}</text>',
        f'<text x="{LEFT}" y="42">{SUBTITLE}</text>',
    ]
    # Grid and y axis, in thousands of tokens.
    for tokens in range(0, Y_MAX + 1, Y_STEP):
        y = n(y_of(tokens))
        out.append(f'<path class="grid" d="M{LEFT} {y}H{RIGHT}"/>')
        out.append(f'<text class="end" x="{LEFT - 8}" y="{n(y_of(tokens) + 4)}">{f"{tokens // 1000}K" if tokens else "0"}</text>')
    for turn in X_TICKS:
        out.append(f'<text class="mid" x="{n(x_of(turn, series))}" y="{BOTTOM + 18}">{turn}</text>')
    # Content and probe turns: a hairline in the plot and a labelled span under the axis.
    span_y = BOTTOM + 30
    first, last = series.turns[0], series.turns[-1]
    spans = [(first, last, f"Turns {first} to {last}")]
    if series.first_probe is not None and first < series.first_probe <= last:
        split = series.first_probe - 0.5
        out.append(f'<path class="grid" d="M{n(x_of(split, series))} {TOP}V{BOTTOM}"/>')
        spans = [(first, split - 0.15, f"Content turns {first} to {series.first_probe - 1}"),
                 (split + 0.15, last, f"Recall questions, turns {series.first_probe} to {last}")]
    for start, end, label in spans:
        x0, x1 = x_of(start, series), x_of(end, series)
        out.append(f'<path class="span" d="M{n(x0)} {span_y - 4}V{span_y}H{n(x1)}V{span_y - 4}"/>')
        out.append(f'<text class="mid" x="{n((x0 + x1) / 2)}" y="{span_y + 17}">{label}</text>')
    # The bound, then the two series: the full history dashed, Contexto solid and on top.
    out.append(f'<path class="bound" d="M{LEFT} {n(y_of(BOUND))}H{RIGHT}"/>')
    out.append(f'<text class="ink label end" x="{RIGHT}" y="{n(y_of(BOUND) - 7)}">{LABEL_BOUND}</text>')
    out.append(f'<path class="series plain" d="{path_of(series, series.plain)}"/>')
    out.append(f'<path class="series contexto" d="{path_of(series, series.contexto)}"/>')
    # A dot on each measured turn of the full history, and its last value.
    for turn in series.measured:
        out.append(dot(x_of(turn, series), y_of(series.plain[series.turns.index(turn)]), c["plain"], c["page"], 4))
    x, y = x_of(series.turns[end_p], series), y_of(series.plain[end_p])
    out.append(f'<text class="ink end" x="{n(x + 5)}" y="{n(y - 11)}">{series.plain[end_p]:,}</text>')
    # Contexto's largest request: a dot and its value.
    x, y = x_of(series.turns[peak_c], series), y_of(series.contexto[peak_c])
    out.append(dot(x, y, c["contexto"], c["page"]))
    out.append(f'<text class="ink mid" x="{n(x)}" y="{n(y - 10)}">{series.contexto[peak_c]:,}</text>')
    # The first turn where Contexto's request is smaller than the turn before: a tick under it and a note.
    drop = next((i for i in range(1, len(series.turns)) if series.contexto[i] < series.contexto[i - 1]), None)
    if drop is not None:
        x, y = x_of(series.turns[drop], series), y_of(series.contexto[drop])
        out.append(f'<path class="span" d="M{n(x)} {n(y + 4)}V{n(y + 9)}"/>')
        out.append(f'<text x="{n(x - 4)}" y="{n(y + 20)}">{LABEL_DROP}</text>')
    # Direct labels, each with a sample of its line: the full history above its line, Contexto under its own.
    plain_x, plain_y = LEFT + 14, y_of(462_000)
    out.append(f'<path class="series plain" d="M{n(plain_x)} {n(plain_y - 4.5)}h26"/>')
    if series.measured:
        out.append(dot(plain_x + 36, plain_y - 4.5, c["plain"], c["page"], 4))
    out.append(f'<text class="ink label" x="{n(plain_x + 48)}" y="{n(plain_y)}">{LABEL_PLAIN}</text>')
    contexto_x, contexto_y = x_of(11, series), y_of(13_000)
    out.append(f'<path class="series contexto" d="M{n(contexto_x)} {n(contexto_y - 4.5)}h26"/>')
    out.append(f'<text class="ink label" x="{n(contexto_x + 34)}" y="{n(contexto_y)}">{LABEL_CONTEXTO}</text>')
    out.append(f'<text x="{LEFT}" y="{HEIGHT - 10}">{NOTE}</text>')
    out += ["</svg>", ""]
    return "\n".join(out)


def write_charts(out_dir: Path = ASSETS, csv_path: Path = CSV_PATH) -> list[Path]:
    series = read_series(csv_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for theme in THEMES:
        path = out_dir / f"request-size-{theme}.svg"
        path.write_text(render(series, theme), encoding="utf-8")
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ASSETS, help="directory for the two SVGs (default docs/assets)")
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help="the per-request CSV of run 3")
    args = parser.parse_args(argv)
    try:
        for path in write_charts(args.out, args.csv):
            print(f"wrote {path} ({path.stat().st_size:,} bytes)")
    except (OSError, ValueError, KeyError) as exc:
        print(f"make_chart: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
