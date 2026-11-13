"""The two charts of a bench results file, as SVG text. Standard library only.

``accuracy_svg(results)``
    Accuracy by arm with Wilson 95% intervals, overall and by probe class. A point with its
    interval, not a bar from zero, so that differences of a few points stay visible. It reads
    ``arms`` and so exists only for a live results file.
``request_size_svg(results)``
    Request size against history length for each series of ``request_series``, with the bound as
    a labelled line. Each series' ``basis`` (measured, estimated, modelled) is in the legend and
    the file's ``notes`` are printed as footnotes. It works for a live and for a replay file.

The output is deterministic: no timestamp, no generated id, coordinates printed to one decimal.
The same results file gives the same bytes, so a committed chart can be regenerated and compared.

Reading without colour: each arm has its own marker shape, and its own dash pattern in the line
chart, and the legend shows both. The background is drawn explicitly, so the chart looks the same
on a light and on a dark page. The colours are the first three of a categorical palette checked
for separation under the common colour-vision deficiencies.

Geometry, for anyone checking a plotted point. Both charts are ``WIDTH`` wide. In the request-size
chart a point ``(history, size)`` is drawn at ``x = LINE_LEFT + history / x_top * LINE_PLOT_WIDTH``
and ``y = plot_bottom - size / y_top * LINE_PLOT_HEIGHT``, where ``x_top`` and ``y_top`` are the
last tick of each axis (``axis_top``). In the accuracy chart an accuracy ``p`` is drawn at
``x = ACC_LEFT + (p - low) / (1 - low) * ACC_PLOT_WIDTH``, where ``low`` is the first tick.
"""

from __future__ import annotations

import math
import re
import textwrap
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

WIDTH = 800
MARGIN = 24
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
GRID = "#e4e3de"
AXIS = "#a9a8a1"
FONT = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
# Arm: colour, marker shape, dash pattern of its line. The colour follows the arm, whichever arms
# a file has, so an arm looks the same in every chart.
STYLES = {
    "contexto": ("#2a78d6", "circle", ""),
    "full": ("#eb6834", "square", "7 4"),
    "plain": ("#1baf7a", "triangle", "2 4"),
}
OTHER_STYLE = ("#52514e", "diamond", "10 3 2 3")
BASIS_WORDS = {"measured": "measured", "estimated": "estimated", "modelled": "modelled"}
LABELS = {"contexto": "Contexto", "full": "Full context", "plain": "Plain Claude Code"}

LINE_LEFT = 76
LINE_PLOT_WIDTH = WIDTH - LINE_LEFT - MARGIN
LINE_PLOT_HEIGHT = 300
ACC_LEFT = 200
ACC_RIGHT = 64
ACC_PLOT_WIDTH = WIDTH - ACC_LEFT - ACC_RIGHT
ACC_SLOT = 16  # vertical space of one arm inside a row
ACC_ROW_PAD = 14
MAX_MARKERS = 40
NOTE_WRAP = 118
CHAR_WIDTH = 6.7  # an estimate for 12px text; there are no font metrics here

ACCURACY = "accuracy"
REQUEST_SIZE = "request-size"
TITLES = {ACCURACY: "Accuracy by arm", REQUEST_SIZE: "Request size against history length"}


class ChartError(ValueError):
    """A results file a chart cannot be drawn from. The message says what is missing."""


# SVG text


def _n(value: float) -> str:
    return f"{value:.1f}"


def _el(tag: str, text: str | None = None, children: Sequence[str] = (), **attrs: Any) -> str:
    parts = "".join(f" {name.rstrip('_').replace('_', '-')}={quoteattr(str(value))}" for name, value in attrs.items())
    if text is None and not children:
        return f"<{tag}{parts}/>"
    return f"<{tag}{parts}>{escape(text) if text is not None else ''}{''.join(children)}</{tag}>"


def _text(x: float, y: float, text: str, size: int = 12, fill: str = INK_SECONDARY, anchor: str = "start",
          **attrs: Any) -> str:
    return _el("text", text, x=_n(x), y=_n(y), font_size=size, fill=fill, text_anchor=anchor, **attrs)


def _marker(shape: str, x: float, y: float, colour: str, title: str | None = None, r: float = 4.0) -> str:
    """A filled marker with a ring in the surface colour, so it stays legible where it crosses a line."""
    common = {"fill": colour, "stroke": SURFACE, "stroke_width": "1.5"}
    children = [_el("title", title)] if title else []
    if shape == "circle":
        return _el("circle", children=children, cx=_n(x), cy=_n(y), r=_n(r), **common)
    if shape == "square":
        side = 2 * r - 0.6
        return _el("rect", children=children, x=_n(x - side / 2), y=_n(y - side / 2), width=_n(side),
                   height=_n(side), **common)
    if shape == "triangle":
        points = f"{_n(x)},{_n(y - r - 0.6)} {_n(x + r + 0.4)},{_n(y + r - 0.4)} {_n(x - r - 0.4)},{_n(y + r - 0.4)}"
    else:  # diamond
        d = r + 0.8
        points = f"{_n(x)},{_n(y - d)} {_n(x + d)},{_n(y)} {_n(x)},{_n(y + d)} {_n(x - d)},{_n(y)}"
    return _el("polygon", children=children, points=points, **common)


def _style(name: str) -> tuple[str, str, str]:
    return STYLES.get(name, OTHER_STYLE)


def _document(height: float, title: str, description: str, body: Sequence[str]) -> str:
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height:.0f}" width="{WIDTH}" '
            f'height="{height:.0f}" role="img" font-family={quoteattr(FONT)}>')
    lines = [head, _el("title", title), _el("desc", description),
             _el("rect", x=0, y=0, width=WIDTH, height=f"{height:.0f}", fill=SURFACE), *body, "</svg>"]
    return "\n".join(lines) + "\n"


def _legend(entries: Sequence[tuple[str, str, bool]], y: float) -> tuple[list[str], float]:
    """A row of legend entries ``(series name, words, with line)``, wrapped to the width. Returns
    the elements and the y below the last row."""
    out: list[str] = []
    x = float(MARGIN)
    for name, words, with_line in entries:
        colour, shape, dash = _style(name)
        key = 34 if with_line else 14
        width = key + 6 + len(words) * CHAR_WIDTH + 22
        if x > MARGIN and x + width > WIDTH - MARGIN:
            x, y = float(MARGIN), y + 20
        if with_line:
            line = {"x1": _n(x), "y1": _n(y - 4), "x2": _n(x + key), "y2": _n(y - 4), "stroke": colour,
                    "stroke_width": 2, "stroke_linecap": "round"}
            if dash:
                line["stroke_dasharray"] = dash
            out.append(_el("line", **line))
        out.append(_marker(shape, x + key / 2, y - 4, colour))
        out.append(_text(x + key + 6, y, words, fill=INK, data_legend=name))
        x += width
    return out, y + 12


def _footnotes(notes: Sequence[str], y: float) -> tuple[list[str], float]:
    out = []
    for note in notes:
        for i, line in enumerate(textwrap.wrap(str(note), NOTE_WRAP) or [""]):
            out.append(_text(MARGIN, y, line, size=11, **({"data_note": "start"} if i == 0 else {})))
            y += 15
        y += 3
    return out, y


def axis_top(largest: float, target: int = 5) -> tuple[int, int]:
    """The last tick and the step of an axis from 0 that holds ``largest`` in about ``target`` steps
    of 1, 2 or 5 times a power of ten. Whole numbers only."""
    largest = max(1, math.ceil(largest))
    raw = max(1, math.ceil(largest / target))
    magnitude = 10 ** (len(str(raw)) - 1)
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)
    return step * math.ceil(largest / step), step


def _tokens(value: int, top: int) -> str:
    if value == 0:
        return "0"
    if top >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    if top >= 10_000:
        return f"{value / 1_000:g}K"
    return f"{value:,}"


def _subtitle(results: Mapping[str, Any], seed: bool) -> str:
    manifest = results.get("manifest") or {}
    parts = []
    model = results.get("chart_model") or manifest.get("model")
    if model:
        parts.append(str(model))
    if manifest.get("protocol"):
        parts.append(f"protocol {manifest['protocol']}")
    if seed and manifest.get("seed") is not None:
        parts.append(f"seed {manifest['seed']}")
    seeds = manifest.get("seeds") or []
    if not seed and seeds:
        parts.append(f"{len(seeds)} seed{'' if len(seeds) == 1 else 's'}")
    if manifest.get("contexto_version"):
        parts.append(f"Contexto {manifest['contexto_version']}")
    if manifest.get("claude_code_version"):
        parts.append(f"Claude Code {manifest['claude_code_version']}")
    return ", ".join(parts)


# Request size against history length


def _series_of(results: Mapping[str, Any]) -> dict[str, Any]:
    series = results.get("request_series")
    if not isinstance(series, dict) or not any(s.get("points") for s in series.values() if isinstance(s, dict)):
        raise ChartError("the results file has no request_series with points, which the request-size chart draws")
    if not isinstance(results.get("bound"), int):
        raise ChartError("the results file has no bound, which the request-size chart draws as a line")
    return {name: s for name, s in series.items() if isinstance(s, dict) and s.get("points")}


def request_size_svg(results: Mapping[str, Any]) -> str:
    """The request-size chart of a results file (live or replay), as SVG text."""
    series = _series_of(results)
    bound = results["bound"]
    replay = results.get("kind") == "replay"
    title = TITLES[REQUEST_SIZE] + (": synthetic session, no model" if replay else "")
    x_top, x_step = axis_top(max(p[0] for s in series.values() for p in s["points"]))
    y_top, y_step = axis_top(max(bound, max(p[1] for s in series.values() for p in s["points"])))
    body = [_text(MARGIN, 28, title, size=16, fill=INK, font_weight=600)]
    subtitle = _subtitle(results, seed=True)
    y = 48.0
    if subtitle:
        body.append(_text(MARGIN, y, subtitle))
        y += 24
    entries = [(name, f"{s.get('label') or LABELS.get(name, name)} ({BASIS_WORDS.get(s.get('basis'), s.get('basis'))})",
                True) for name, s in series.items()]
    legend, y = _legend(entries, y)
    body += legend
    top = y + 14
    bottom = top + LINE_PLOT_HEIGHT

    def px(history: float) -> float:
        return LINE_LEFT + history / x_top * LINE_PLOT_WIDTH

    def py(size: float) -> float:
        return bottom - size / y_top * LINE_PLOT_HEIGHT

    for tick in range(0, y_top + 1, y_step):
        body.append(_el("line", x1=_n(LINE_LEFT), y1=_n(py(tick)), x2=_n(LINE_LEFT + LINE_PLOT_WIDTH), y2=_n(py(tick)),
                        stroke=AXIS if tick == 0 else GRID, stroke_width=1))
        body.append(_text(LINE_LEFT - 8, py(tick) + 4, _tokens(tick, y_top), size=11, anchor="end", data_tick="y"))
    for tick in range(0, x_top + 1, x_step):
        body.append(_el("line", x1=_n(px(tick)), y1=_n(bottom), x2=_n(px(tick)), y2=_n(bottom + 4), stroke=AXIS,
                        stroke_width=1))
        body.append(_text(px(tick), bottom + 18, _tokens(tick, x_top), size=11, anchor="middle", data_tick="x"))
    body.append(_text(LINE_LEFT + LINE_PLOT_WIDTH / 2, bottom + 40, "History length (tokens of conversation so far)",
                      anchor="middle", data_axis="x"))
    middle = (top + bottom) / 2
    body.append(_text(18, middle, "Request input (tokens)", anchor="middle", data_axis="y",
                      transform=f"rotate(-90 18 {_n(middle)})"))
    body.append(_el("line", x1=_n(LINE_LEFT), y1=_n(py(bound)), x2=_n(LINE_LEFT + LINE_PLOT_WIDTH), y2=_n(py(bound)),
                    stroke=INK, stroke_width=1, stroke_dasharray="1 3", data_bound=bound))
    for name, s in series.items():
        colour, shape, dash = _style(name)
        points = s["points"]
        words = f"{s.get('label') or LABELS.get(name, name)} ({BASIS_WORDS.get(s.get('basis'), s.get('basis'))})"
        line = {"points": " ".join(f"{_n(px(h))},{_n(py(size))}" for h, size in points), "fill": "none",
                "stroke": colour, "stroke_width": 2, "stroke_linejoin": "round", "stroke_linecap": "round",
                "data_series": name}
        if dash:
            line["stroke_dasharray"] = dash
        summary = f"{words}: {len(points)} requests, largest {max(p[1] for p in points):,} tokens"
        body.append(_el("polyline", children=[_el("title", summary)], **line))
        every = max(1, math.ceil(len(points) / MAX_MARKERS))
        marked = [p for i, p in enumerate(points) if i % every == 0 or i == len(points) - 1]
        body.append(_el("g", children=[
            _marker(shape, px(h), py(size), colour, f"{words}: request of {size:,} tokens at {h:,} tokens of history")
            for h, size in marked], data_markers=name))
    body.append(_el("text", f"bound: {bound:,} tokens", x=_n(LINE_LEFT + 8), y=_n(py(bound) - 7), font_size=12,
                    fill=INK, stroke=SURFACE, stroke_width=4, paint_order="stroke", stroke_linejoin="round",
                    data_bound_label=bound))
    notes, y = _footnotes(list(results.get("notes") or []), bottom + 66)
    body += notes
    description = (f"{title}. The bound is {bound:,} tokens. Series: "
                   + "; ".join(words for _, words, _ in entries) + ".")
    return _document(y + 4, title, description, body)


# Accuracy by arm


def _arms_of(results: Mapping[str, Any]) -> dict[str, Any]:
    arms = results.get("arms")
    if not isinstance(arms, dict) or not arms:
        why = ("a replay makes no model calls, so it has no answers to score"
               if results.get("kind") == "replay" else "it has no arms block")
        raise ChartError(f"the results file has no per-arm accuracy ({why}), which the accuracy chart draws")
    return arms


def _accuracy_low(arms: Mapping[str, Any], rows: Sequence[tuple[str, str | None]]) -> tuple[int, int]:
    """The first tick of the accuracy axis, in percent, and the tick step: the axis starts at the
    tick below the lowest interval end and uses at most six steps of 5, 10 or 20 points."""
    lows = [100.0]
    for arm in arms.values():
        for _, cls in rows:
            cell = arm if cls is None else arm.get("by_class", {}).get(cls)
            if cell and cell.get("interval"):
                lows.append(100 * cell["interval"][0])
    lowest = math.floor(min(lows) + 1e-9)
    for step in (5, 10, 20):
        low = min(lowest // step * step, 100 - step)
        if (100 - low) / step <= 6:
            return low, step
    return 0, 20


def accuracy_svg(results: Mapping[str, Any]) -> str:
    """The accuracy chart of a live results file, as SVG text. ``ChartError`` when the file has no
    ``arms`` (a replay file)."""
    arms = _arms_of(results)
    classes = list(dict.fromkeys(cls for arm in arms.values() for cls in arm.get("by_class", {})))
    rows: list[tuple[str, str | None]] = [("All probes", None), *((cls, cls) for cls in classes)]
    low, step = _accuracy_low(arms, rows)
    title = "Accuracy by arm, with Wilson 95% intervals"
    body = [_text(MARGIN, 28, title, size=16, fill=INK, font_weight=600)]
    subtitle = _subtitle(results, seed=False)
    y = 48.0
    if subtitle:
        body.append(_text(MARGIN, y, subtitle))
        y += 24
    legend, y = _legend([(name, LABELS.get(name, name), False) for name in arms], y)
    body += legend
    top = y + 10
    row_height = len(arms) * ACC_SLOT + ACC_ROW_PAD
    bottom = top + len(rows) * row_height

    def px(share: float) -> float:
        return ACC_LEFT + (100 * share - low) / (100 - low) * ACC_PLOT_WIDTH

    for tick in range(low, 101, step):
        x = px(tick / 100)
        body.append(_el("line", x1=_n(x), y1=_n(top), x2=_n(x), y2=_n(bottom), stroke=GRID, stroke_width=1))
        body.append(_text(x, bottom + 18, f"{tick}%", size=11, anchor="middle", data_tick="x"))
    axis = "Probes answered correctly" + (f" (the axis starts at {low}%, not at 0)" if low else "")
    body.append(_text(ACC_LEFT + ACC_PLOT_WIDTH / 2, bottom + 40, axis, anchor="middle", data_axis="x"))
    for index, (name, cls) in enumerate(rows):
        row_top = top + index * row_height
        if index:
            body.append(_el("line", x1=_n(MARGIN), y1=_n(row_top), x2=_n(WIDTH - MARGIN), y2=_n(row_top), stroke=GRID,
                            stroke_width=1))
        cells = {arm: (block if cls is None else block.get("by_class", {}).get(cls)) for arm, block in arms.items()}
        counts = [cell["probes"] if cell else 0 for cell in cells.values()]
        n = f"n = {counts[0]:,}" if len(set(counts)) == 1 else "n = " + " / ".join(f"{c:,}" for c in counts)
        centre = row_top + row_height / 2
        body.append(_text(MARGIN, centre - 2, name, fill=INK, data_row=name,
                          **({"font_weight": 600} if cls is None else {})))
        body.append(_text(MARGIN, centre + 13, n, size=11, data_n=name))
        for slot, (arm, cell) in enumerate(cells.items()):
            colour, shape, _ = _style(arm)
            cy = row_top + ACC_ROW_PAD / 2 + slot * ACC_SLOT + ACC_SLOT / 2
            if not cell or not cell.get("probes"):
                body.append(_text(ACC_LEFT + 6, cy + 4, f"{LABELS.get(arm, arm)}: not run", size=11))
                continue
            lo, hi = cell["interval"]
            words = (f"{LABELS.get(arm, arm)}, {name}: {100 * cell['accuracy']:.1f}% ({cell['correct']:,} of "
                     f"{cell['probes']:,}), 95% interval {100 * lo:.1f}% to {100 * hi:.1f}%")
            body.append(_el("g", children=[
                _el("title", words),
                _el("line", x1=_n(px(lo)), y1=_n(cy), x2=_n(px(hi)), y2=_n(cy), stroke=colour, stroke_width=2,
                    stroke_linecap="round"),
                _marker(shape, px(cell["accuracy"]), cy, colour),
            ], data_arm=arm, data_row=name))
            if cls is None:
                body.append(_text(px(hi) + 8, cy + 4, f"{100 * cell['accuracy']:.1f}%", size=11, fill=INK,
                                  data_value=arm))
    notes = ["Each point is the share of probes answered correctly and the line through it is the Wilson 95% "
             "interval; n is the number of probes (one figure per arm when the arms differ). Scoring is exact "
             "match on synthetic sessions.",
             "The figures use repeat 0 of each seed. Intervals that overlap do not by themselves show that two "
             "arms are equal; the paired comparison is in results.md."]
    footer, y = _footnotes(notes, bottom + 66)
    body += footer
    description = (f"{title}. " + "; ".join(
        f"{LABELS.get(arm, arm)} {100 * block['accuracy']:.1f}% of {block['probes']:,} probes"
        for arm, block in arms.items() if block.get("probes")) + ".")
    return _document(y + 4, title, description, body)


# Files


def _slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", model)


def views(results: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """What to draw from a results file: the file itself, or one view per model when it merges
    several models (``by_model``). Each view comes with the suffix of its file names."""
    by_model = results.get("by_model")
    if isinstance(by_model, dict) and by_model:
        return [(f"-{_slug(model)}", {**results, **block, "chart_model": model}) for model, block in by_model.items()]
    return [("", results)]


def _drawn(results: Mapping[str, Any]) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """The charts that can be drawn, as ``(file name, title, svg)``, and those that cannot, as
    ``(file name, reason)``."""
    drawn, skipped = [], []
    for suffix, view in views(results):
        model = f", {view['chart_model']}" if view.get("chart_model") else ""
        for kind, draw in ((ACCURACY, accuracy_svg), (REQUEST_SIZE, request_size_svg)):
            name = f"{kind}{suffix}.svg"
            try:
                drawn.append((name, TITLES[kind] + model, draw(view)))
            except ChartError as exc:
                skipped.append((name, str(exc)))
    return drawn, skipped


def planned(results: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The file names and titles of the charts ``write`` would make from these results."""
    return [(name, title) for name, title, _ in _drawn(results)[0]]


def skipped(results: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The charts ``write`` would not make from these results, each with the reason."""
    return _drawn(results)[1]


def write(results: Mapping[str, Any], charts_dir: Path | str) -> list[Path]:
    """Write every chart the results allow into ``charts_dir`` and return the paths. A chart the
    file cannot give (``skipped``) is left out; with nothing to draw it raises ``ChartError``."""
    drawn, left_out = _drawn(results)
    if not drawn:
        raise ChartError("; ".join(reason for _, reason in left_out) or "the results file has nothing to draw")
    charts_dir = Path(charts_dir)
    charts_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, _, svg in drawn:
        (charts_dir / name).write_text(svg, encoding="utf-8")
        paths.append(charts_dir / name)
    return paths
