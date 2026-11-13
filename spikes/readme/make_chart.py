"""Draw the README chart: the largest request of each turn against the bound, live acceptance run #3.

    python spikes/readme/make_chart.py          # writes docs/assets/request-size-{light,dark}.svg

Standard library only, and deterministic: the same CSV gives the same bytes.

Source: ``docs/research/data/M2-usage-savings-run3.csv``, 44 requests over 24 turns (Claude Code
2.1.301, claude-haiku-5-5, bound 150,000; ``docs/research/M2-usage-savings-run3.md``). For each turn the
chart plots the largest request:

- ``contexto_input_total``: the input tokens Contexto sent, measured from the API's usage;
- ``plain_input_modeled``: the full history plain Claude Code would have sent at the same request,
  modeled from the bytes Claude Code sent to the proxy.

Turns 1 to 18 are content turns and 19 to 24 are probe turns (the CSV's ``kind``).

The two files have the same geometry and differ only in colour: one for GitHub's light page
(``#ffffff``), one for its dark page (``#0d1117``), both with a transparent background. The two lines
differ in colour and in dash, and each has its label next to it. Text is at least 4.5:1 against its
page and the lines and the bound line at least 3:1 (``tests/test_readme_assets.py`` checks both).
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
BOUND = 150_000
Y_MAX = 500_000
Y_STEP = 100_000
X_TICKS = (1, 4, 8, 12, 16, 20, 24)

WIDTH, HEIGHT = 800, 418
LEFT, RIGHT, TOP, BOTTOM = 52, 772, 62, 322  # the plot area
FONT = '-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif'

TITLE = "Largest request in each turn"
SUBTITLE = "Input tokens"
NOTE = "Acceptance run #3, one scripted session. Contexto measured; full history modeled."
LABEL_CONTEXTO = "Contexto (measured)"
LABEL_PLAIN = "Full history without Contexto (modeled)"

# Series colours are slots 1 and 2 of the dataviz reference palette, validated on each page colour for
# colour-blind separation and 3:1 contrast. Text and grid are GitHub's own foreground and border tones.
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
    plain: list[int]  # largest modeled full-history request of each turn
    first_probe: int | None  # the first turn whose kind is "probe"


def read_series(path: Path = CSV_PATH) -> Series:
    contexto: dict[int, int] = {}
    plain: dict[int, int] = {}
    probes: list[int] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            turn = int(row["turn"])
            contexto[turn] = max(contexto.get(turn, 0), int(row["contexto_input_total"]))
            plain[turn] = max(plain.get(turn, 0), int(row["plain_input_modeled"]))
            if row["kind"] == "probe":
                probes.append(turn)
    turns = sorted(contexto)
    if not turns:
        raise ValueError(f"{path} has no rows")
    return Series(turns, [contexto[t] for t in turns], [plain[t] for t in turns], min(probes) if probes else None)


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


def render(series: Series, theme: str) -> str:
    c = THEMES[theme]
    peak_c = max(range(len(series.turns)), key=lambda i: series.contexto[i])
    peak_p = max(range(len(series.turns)), key=lambda i: series.plain[i])
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}" '
        f'role="img" aria-labelledby="t d">',
        f'<title id="t">{TITLE}: Contexto against the full history, with a bound of {BOUND:,} tokens</title>',
        f'<desc id="d">Over {len(series.turns)} turns the largest Contexto request is {max(series.contexto):,} input '
        f"tokens (measured), under the bound of {BOUND:,}. The full history without Contexto grows to "
        f"{max(series.plain):,} (modeled). {NOTE}</desc>",
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
                 (split + 0.15, last, f"Probe turns {series.first_probe} to {last}")]
    for start, end, label in spans:
        x0, x1 = x_of(start, series), x_of(end, series)
        out.append(f'<path class="span" d="M{n(x0)} {span_y - 4}V{span_y}H{n(x1)}V{span_y - 4}"/>')
        out.append(f'<text class="mid" x="{n((x0 + x1) / 2)}" y="{span_y + 17}">{label}</text>')
    # The bound, then the two series: the modeled one dashed, the measured one solid and on top.
    out.append(f'<path class="bound" d="M{LEFT} {n(y_of(BOUND))}H{RIGHT}"/>')
    out.append(f'<text class="ink label end" x="{RIGHT}" y="{n(y_of(BOUND) - 7)}">Bound {BOUND:,}</text>')
    out.append(f'<path class="series plain" d="{path_of(series, series.plain)}"/>')
    out.append(f'<path class="series contexto" d="{path_of(series, series.contexto)}"/>')
    # The two peaks, each a dot with a ring in the page colour and its value.
    for index, values, key, anchor, dx in ((peak_p, series.plain, "plain", "end", 5),
                                           (peak_c, series.contexto, "contexto", "mid", 0)):
        x, y = x_of(series.turns[index], series), y_of(values[index])
        out.append(f'<circle cx="{n(x)}" cy="{n(y)}" r="4.5" fill="{c[key]}" stroke="{c["page"]}" stroke-width="2"/>')
        out.append(f'<text class="ink {anchor}" x="{n(x + dx)}" y="{n(y - 10)}">{values[index]:,}</text>')
    # Direct labels, each with a sample of its line: the full history above its line, Contexto under its own.
    plain_x, plain_y = x_of(6.6, series), y_of(406_000)
    out.append(f'<path class="series plain" d="M{n(plain_x)} {n(plain_y - 4.5)}h26"/>')
    out.append(f'<text class="ink label" x="{n(plain_x + 34)}" y="{n(plain_y)}">{LABEL_PLAIN}</text>')
    contexto_x, contexto_y = x_of(8, series), y_of(13_000)
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
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help="the per-request CSV of run #3")
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
