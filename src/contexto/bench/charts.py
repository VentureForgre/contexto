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
on a light and on a dark page. Every series colour has a contrast of 3 to 1 or more against that
background, and the three known arms differ in lightness as well as hue. An arm this module does
not know gets a marker, a dash pattern and a colour of its own from a second list (``_styles``),
by the order of the names, so it looks the same in both charts.

Text. A manifest ``label`` is added to the title of every chart. A title, subtitle or footnote
too long for one line is wrapped to the chart's width; a legend entry or a row label too long for
its place is cut with three dots and keeps its full text as a tooltip (``<title>``). A legend
entry is cut in its label, so the series' basis after it ("(estimated)") is always drawn whole. A
series with no points is not drawn and a footnote says so. Widths are estimated (``text_width``):
there are no font metrics here. A character XML 1.0 does not allow is replaced by U+FFFD, so the
file is always well-formed.

The same results file gives the same bytes on every Python version from 3.12 to 3.15: which
characters count as wide comes from a table in this module (``WIDE``, East Asian Width W or F in
Unicode 17.0), not from the running Python's Unicode tables, which differ between versions for
recently assigned characters. Words are split only at a space, tab, carriage return or line feed.

Geometry, for anyone checking a plotted point. Both charts are ``WIDTH`` wide. In the request-size
chart a point ``(history, size)`` is drawn at ``x = LINE_LEFT + history / x_top * LINE_PLOT_WIDTH``
and ``y = plot_bottom - size / y_top * LINE_PLOT_HEIGHT``, where ``x_top`` and ``y_top`` are the
last tick of each axis (``axis_top``). In the accuracy chart an accuracy ``p`` is drawn at
``x = ACC_LEFT + (p - low) / (1 - low) * ACC_PLOT_WIDTH``, where ``low`` is the first tick.
"""

from __future__ import annotations

import bisect
import math
import re
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
    "plain": ("#0a6b47", "triangle", "2 4"),
}
# For arms outside the three: the k-th such name, in sorted order, takes the k-th of each list.
# The lists have 4, 4 and 5 entries, so 20 arms get 20 different pairs of marker and dash.
OTHER_COLOURS = ("#8250c4", "#a8326e", "#6b5a12", "#3d6a78")
OTHER_SHAPES = ("diamond", "pentagon", "plus", "hexagon")
OTHER_DASHES = ("10 3 2 3", "12 4", "4 2", "1 3 6 3", "14 3 2 3 2 3")
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
CHAR_WIDTH = 6.7  # an estimate for 12px text; there are no font metrics here
TITLE_SIZE = 16
CUT = "..."
REPLACEMENT = "\ufffd"
# What XML 1.0 allows in a document: tab, line feed, carriage return and the ranges below.
_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_SPACE = re.compile("[ \t\r\n]+")
# The code points of East Asian Width W or F in Unicode 17.0, as inclusive ranges: the characters
# ``text_width`` counts twice. Fixed here so that a chart does not change with the Python version.
WIDE = (
    (0x1100, 0x115F), (0x231A, 0x231B), (0x2329, 0x232A), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3),
    (0x25FD, 0x25FE), (0x2614, 0x2615), (0x2630, 0x2637), (0x2648, 0x2653), (0x267F, 0x267F), (0x268A, 0x268F),
    (0x2693, 0x2693), (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE), (0x26C4, 0x26C5), (0x26CE, 0x26CE),
    (0x26D4, 0x26D4), (0x26EA, 0x26EA), (0x26F2, 0x26F3), (0x26F5, 0x26F5), (0x26FA, 0x26FA), (0x26FD, 0x26FD),
    (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755),
    (0x2757, 0x2757), (0x2795, 0x2797), (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50),
    (0x2B55, 0x2B55), (0x2E80, 0x2E99), (0x2E9B, 0x2EF3), (0x2F00, 0x2FD5), (0x2FF0, 0x303E), (0x3041, 0x3096),
    (0x3099, 0x30FF), (0x3105, 0x312F), (0x3131, 0x318E), (0x3190, 0x31E5), (0x31EF, 0x321E), (0x3220, 0x3247),
    (0x3250, 0xA48C), (0xA490, 0xA4C6), (0xA960, 0xA97C), (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFE10, 0xFE19),
    (0xFE30, 0xFE52), (0xFE54, 0xFE66), (0xFE68, 0xFE6B), (0xFF01, 0xFF60), (0xFFE0, 0xFFE6), (0x16FE0, 0x16FE4),
    (0x16FF0, 0x16FF6), (0x17000, 0x18CD5), (0x18CFF, 0x18D1E), (0x18D80, 0x18DF2), (0x1AFF0, 0x1AFF3),
    (0x1AFF5, 0x1AFFB), (0x1AFFD, 0x1AFFE), (0x1B000, 0x1B122), (0x1B132, 0x1B132), (0x1B150, 0x1B152),
    (0x1B155, 0x1B155), (0x1B164, 0x1B167), (0x1B170, 0x1B2FB), (0x1D300, 0x1D356), (0x1D360, 0x1D376),
    (0x1F004, 0x1F004), (0x1F0CF, 0x1F0CF), (0x1F18E, 0x1F18E), (0x1F191, 0x1F19A), (0x1F200, 0x1F202),
    (0x1F210, 0x1F23B), (0x1F240, 0x1F248), (0x1F250, 0x1F251), (0x1F260, 0x1F265), (0x1F300, 0x1F320),
    (0x1F32D, 0x1F335), (0x1F337, 0x1F37C), (0x1F37E, 0x1F393), (0x1F3A0, 0x1F3CA), (0x1F3CF, 0x1F3D3),
    (0x1F3E0, 0x1F3F0), (0x1F3F4, 0x1F3F4), (0x1F3F8, 0x1F43E), (0x1F440, 0x1F440), (0x1F442, 0x1F4FC),
    (0x1F4FF, 0x1F53D), (0x1F54B, 0x1F54E), (0x1F550, 0x1F567), (0x1F57A, 0x1F57A), (0x1F595, 0x1F596),
    (0x1F5A4, 0x1F5A4), (0x1F5FB, 0x1F64F), (0x1F680, 0x1F6C5), (0x1F6CC, 0x1F6CC), (0x1F6D0, 0x1F6D2),
    (0x1F6D5, 0x1F6D8), (0x1F6DC, 0x1F6DF), (0x1F6EB, 0x1F6EC), (0x1F6F4, 0x1F6FC), (0x1F7E0, 0x1F7EB),
    (0x1F7F0, 0x1F7F0), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945), (0x1F947, 0x1F9FF), (0x1FA70, 0x1FA7C),
    (0x1FA80, 0x1FA8A), (0x1FA8E, 0x1FAC6), (0x1FAC8, 0x1FAC8), (0x1FACD, 0x1FADC), (0x1FADF, 0x1FAEA),
    (0x1FAEF, 0x1FAF8), (0x20000, 0x2FFFD), (0x30000, 0x3FFFD),)
_WIDE_STARTS = tuple(start for start, _ in WIDE)

ACCURACY = "accuracy"
REQUEST_SIZE = "request-size"
TITLES = {ACCURACY: "Accuracy by arm", REQUEST_SIZE: "Request size against history length"}


class ChartError(ValueError):
    """A results file a chart cannot be drawn from. The message says what is missing."""


# SVG text


def _n(value: float) -> str:
    return f"{value:.1f}"


def _clean(text: Any) -> str:
    """The text with every character XML 1.0 does not allow replaced by U+FFFD."""
    return _NOT_XML.sub(REPLACEMENT, str(text))


def _el(tag: str, text: str | None = None, children: Sequence[str] = (), **attrs: Any) -> str:
    """One element. ``children`` are finished elements and come before the text, so that a
    ``<title>`` is the first child. The text and every attribute value are cleaned and escaped."""
    parts = "".join(f" {name.rstrip('_').replace('_', '-')}={quoteattr(_clean(value))}"
                    for name, value in attrs.items())
    if text is None and not children:
        return f"<{tag}{parts}/>"
    return f"<{tag}{parts}>{''.join(children)}{escape(_clean(text)) if text is not None else ''}</{tag}>"


def _units(ch: str) -> int:
    """2 for a wide character (``WIDE``), else 1."""
    code = ord(ch)
    i = bisect.bisect_right(_WIDE_STARTS, code) - 1
    return 2 if i >= 0 and code <= WIDE[i][1] else 1


def text_width(text: str, size: int = 12) -> float:
    """An estimate of the width of ``text`` in pixels at a font size: ``CHAR_WIDTH`` per character
    at 12px, in proportion at other sizes, and twice that for a wide (East Asian) character."""
    return sum(_units(ch) for ch in text) * CHAR_WIDTH * size / 12


def _shorten(text: str, size: int, limit: float) -> str:
    """``text`` if it is no wider than ``limit`` pixels, else its start followed by three dots."""
    if text_width(text, size) <= limit:
        return text
    kept = text
    while kept and text_width(kept.rstrip() + CUT, size) > limit:
        kept = kept[:-1]
    return kept.rstrip() + CUT


def _wrap(text: str, size: int, limit: float) -> list[str]:
    """``text`` broken at spaces into lines no wider than ``limit`` pixels, a word wider than a line
    broken where it must be (text with no spaces, as in Chinese or Japanese, is such a word)."""
    room = limit * 12 / (CHAR_WIDTH * size)  # in units of one narrow character
    lines: list[str] = []
    line, used = "", 0
    for word in _SPACE.split(text.strip()):
        width = sum(_units(ch) for ch in word)
        if line and used + 1 + width <= room:
            line, used = f"{line} {word}", used + 1 + width
            continue
        if line:
            lines.append(line)
        line, used = "", 0
        for ch in word:
            if line and used + _units(ch) > room:
                lines.append(line)
                line, used = "", 0
            line, used = line + ch, used + _units(ch)
    if line:
        lines.append(line)
    return lines or [""]


def _text(x: float, y: float, text: str, size: int = 12, fill: str = INK_SECONDARY, anchor: str = "start",
          limit: float | None = None, keep: str = "", **attrs: Any) -> str:
    """A line of text, ``text`` followed by ``keep``. With ``limit`` (pixels) a text too wide is cut
    with three dots in ``text``, so ``keep`` is drawn whole, and carries its full text as a tooltip."""
    whole = _clean(text + keep)
    drawn = whole
    if limit is not None and text_width(whole, size) > limit:
        tail = _clean(keep)
        room = limit - text_width(tail, size)
        drawn = (_shorten(_clean(text), size, room) + tail) if tail and room > text_width(CUT, size) else (
            _shorten(whole, size, limit))
    text = whole
    children = [_el("title", text)] if drawn != text else []
    return _el("text", drawn, children, x=_n(x), y=_n(y), font_size=size, fill=fill, text_anchor=anchor, **attrs)


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
        corners = [(x, y - r - 0.6), (x + r + 0.4, y + r - 0.4), (x - r - 0.4, y + r - 0.4)]
    elif shape == "plus":
        arm, half = r + 0.6, 0.4 * r
        corners = [(x - half, y - arm), (x + half, y - arm), (x + half, y - half), (x + arm, y - half),
                   (x + arm, y + half), (x + half, y + half), (x + half, y + arm), (x - half, y + arm),
                   (x - half, y + half), (x - arm, y + half), (x - arm, y - half), (x - half, y - half)]
    else:  # a regular polygon with one corner at the top: diamond, pentagon, hexagon
        sides = {"diamond": 4, "pentagon": 5, "hexagon": 6}[shape]
        d = r + 0.8
        corners = [(x + d * math.sin(2 * math.pi * i / sides), y - d * math.cos(2 * math.pi * i / sides))
                   for i in range(sides)]
    return _el("polygon", children=children, points=" ".join(f"{_n(px)},{_n(py)}" for px, py in corners), **common)


def _styles(results: Mapping[str, Any]) -> dict[str, tuple[str, str, str]]:
    """The colour, marker shape and dash pattern of every arm and series of a results file. A
    known arm has its own (``STYLES``); the others take theirs from the second lists by the sorted
    order of their names, so an arm looks the same in both charts."""
    names = [*(results.get("arms") or {}), *(results.get("request_series") or {})]
    others = sorted({name for name in names if name not in STYLES})
    styles = dict(STYLES)
    for k, name in enumerate(others):
        styles[name] = (OTHER_COLOURS[k % len(OTHER_COLOURS)], OTHER_SHAPES[k % len(OTHER_SHAPES)],
                        OTHER_DASHES[k % len(OTHER_DASHES)])
    return styles


def _document(height: float, title: str, description: str, body: Sequence[str]) -> str:
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height:.0f}" width="{WIDTH}" '
            f'height="{height:.0f}" role="img" font-family={quoteattr(FONT)}>')
    lines = [head, _el("title", title), _el("desc", description),
             _el("rect", x=0, y=0, width=WIDTH, height=f"{height:.0f}", fill=SURFACE), *body, "</svg>"]
    return "\n".join(lines) + "\n"


def _legend(entries: Sequence[tuple[str, str, str, bool]], y: float,
            styles: Mapping[str, tuple[str, str, str]]) -> tuple[list[str], float]:
    """A row of legend entries ``(series name, label, basis words, with line)``, wrapped to the
    width; an entry wider than the chart is cut in its label, so its basis stays whole. Returns the
    elements and the y below the last row."""
    out: list[str] = []
    x = float(MARGIN)
    for name, words, basis, with_line in entries:
        colour, shape, dash = styles[name]
        key = 34 if with_line else 14
        room = WIDTH - 2 * MARGIN - key - 6
        width = key + 6 + min(text_width(_clean(words + basis)), room) + 22
        if x > MARGIN and x + width > WIDTH - MARGIN:
            x, y = float(MARGIN), y + 20
        if with_line:
            line = {"x1": _n(x), "y1": _n(y - 4), "x2": _n(x + key), "y2": _n(y - 4), "stroke": colour,
                    "stroke_width": 2, "stroke_linecap": "round"}
            if dash:
                line["stroke_dasharray"] = dash
            out.append(_el("line", **line))
        out.append(_marker(shape, x + key / 2, y - 4, colour))
        out.append(_text(x + key + 6, y, words, fill=INK, limit=room, keep=basis, data_legend=name))
        x += width
    return out, y + 12


def _labelled(title: str, results: Mapping[str, Any]) -> str:
    """The title with the manifest's ``label`` after it, when the run has one."""
    label = (results.get("manifest") or {}).get("label")
    return f"{title}: {label}" if label else title


def _head(title: str, subtitle: str) -> tuple[list[str], float]:
    """The title and the subtitle, each wrapped to the width. Returns the elements and the y of
    the legend row below them. A one-line title puts the subtitle at y = 48."""
    room = WIDTH - 2 * MARGIN
    out = []
    y = 28.0
    for line in _wrap(_clean(title), TITLE_SIZE, room):
        out.append(_text(MARGIN, y, line, size=TITLE_SIZE, fill=INK, font_weight=600, data_head="title"))
        y += 20
    if subtitle:
        for line in _wrap(_clean(subtitle), 12, room):
            out.append(_text(MARGIN, y, line, data_head="subtitle"))
            y += 16
        y += 8
    return out, y


def _footnotes(notes: Sequence[str], y: float) -> tuple[list[str], float]:
    out = []
    for note in notes:
        for i, line in enumerate(_wrap(_clean(note), 11, WIDTH - 2 * MARGIN)):
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


def _series_label(name: str, series: Mapping[str, Any]) -> tuple[str, str]:
    """A series' label and, after it, its basis in words: ``("Contexto", " (measured)")``."""
    basis = series.get("basis")
    return str(series.get("label") or LABELS.get(name, name)), f" ({BASIS_WORDS.get(basis, basis)})"


def _meets(box: tuple[float, float, float, float], points: Sequence[tuple[float, float]]) -> bool:
    """Whether a line through ``points`` (in pixels) enters ``box`` (left, top, right, bottom): each
    segment is clipped to the box (Liang and Barsky); a single point is a segment of length 0."""
    left, top, right, bottom = box
    for (ax, ay), (bx, by) in zip(points, points[1:]) if len(points) > 1 else [(p, p) for p in points]:
        low, high = 0.0, 1.0
        for p, q in ((ax - bx, ax - left), (bx - ax, right - ax), (ay - by, ay - top), (by - ay, bottom - ay)):
            if p == 0:
                if q < 0:
                    break
            elif p < 0:
                low = max(low, q / p)
            else:
                high = min(high, q / p)
            if low > high:
                break
        else:
            return True
    return False


def _bound_label_place(label: str, baseline: float,
                       lines: Sequence[Sequence[tuple[float, float]]]) -> tuple[float, str | None]:
    """Where the bound's label is drawn, as ``(x, text-anchor)``: at the left end of the bound line, or
    at its right end when a series crosses the left place and none crosses the right one. Its halo
    hides a line under it, so a crossing series would lose a piece of its line. The place is the
    label's estimated box (12 pixels above the baseline, 3 below) widened by a marker's size."""
    width, pad = text_width(label), 5.0
    for x, anchor in ((LINE_LEFT + 8.0, None), (LINE_LEFT + LINE_PLOT_WIDTH - 8.0, "end")):
        left = x - width if anchor else x
        box = (left - pad, baseline - 12 - pad, left + width + pad, baseline + 3 + pad)
        if not any(_meets(box, line) for line in lines):
            return x, anchor
    return LINE_LEFT + 8.0, None


def request_size_svg(results: Mapping[str, Any]) -> str:
    """The request-size chart of a results file (live or replay), as SVG text."""
    series = _series_of(results)
    bound = results["bound"]
    replay = results.get("kind") == "replay"
    title = _labelled(TITLES[REQUEST_SIZE] + (": synthetic session, no model" if replay else ""), results)
    styles = _styles(results)
    x_top, x_step = axis_top(max(p[0] for s in series.values() for p in s["points"]))
    y_top, y_step = axis_top(max(bound, max(p[1] for s in series.values() for p in s["points"])))
    body, y = _head(title, _subtitle(results, seed=True))
    entries = [(name, *_series_label(name, s), True) for name, s in series.items()]
    legend, y = _legend(entries, y, styles)
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
        colour, shape, dash = styles[name]
        points = s["points"]
        words = "".join(_series_label(name, s))
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
    label = f"bound: {bound:,} tokens"
    drawn = [[(px(h), py(size)) for h, size in s["points"]] for s in series.values()]
    label_x, anchor = _bound_label_place(label, py(bound) - 7, drawn)
    body.append(_el("text", label, x=_n(label_x), y=_n(py(bound) - 7), font_size=12,
                    **({"text_anchor": anchor} if anchor else {}), fill=INK, stroke=SURFACE, stroke_width=4,
                    paint_order="stroke", stroke_linejoin="round", data_bound_label=bound))
    empty = [f"The {_series_label(name, s)[0]} series{_series_label(name, s)[1]} has no points, so it is not drawn."
             for name, s in (results.get("request_series") or {}).items()
             if isinstance(s, dict) and name not in series]
    notes, y = _footnotes([*(results.get("notes") or []), *empty], bottom + 66)
    body += notes
    description = (f"{title}. The bound is {bound:,} tokens. Series: "
                   + "; ".join(words + basis for _, words, basis, _ in entries) + ".")
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
            cell = arm if cls is None else (arm.get("by_class") or {}).get(cls)
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
    classes = list(dict.fromkeys(cls for arm in arms.values() for cls in arm.get("by_class") or {}))
    rows: list[tuple[str, str | None]] = [("All probes", None), *((cls, cls) for cls in classes)]
    low, step = _accuracy_low(arms, rows)
    title = _labelled("Accuracy by arm, with Wilson 95% intervals", results)
    styles = _styles(results)
    body, y = _head(title, _subtitle(results, seed=False))
    legend, y = _legend([(name, LABELS.get(name, name), "", False) for name in arms], y, styles)
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
        cells = {arm: (block if cls is None else (block.get("by_class") or {}).get(cls)) for arm, block in arms.items()}
        counts = [cell["probes"] if cell else 0 for cell in cells.values()]
        n = f"n = {counts[0]:,}" if len(set(counts)) == 1 else "n = " + " / ".join(f"{c:,}" for c in counts)
        centre = row_top + row_height / 2
        room = ACC_LEFT - MARGIN - 8  # a row label stops short of the plot
        body.append(_text(MARGIN, centre - 2, name, fill=INK, limit=room, data_row=name,
                          **({"font_weight": 600} if cls is None else {})))
        body.append(_text(MARGIN, centre + 13, n, size=11, limit=room, data_n=name))
        for slot, (arm, cell) in enumerate(cells.items()):
            colour, shape, _ = styles[arm]
            cy = row_top + ACC_ROW_PAD / 2 + slot * ACC_SLOT + ACC_SLOT / 2
            if not cell or not cell.get("probes"):
                body.append(_text(ACC_LEFT + 6, cy + 4, f"{LABELS.get(arm, arm)}: not run", size=11,
                                  limit=ACC_PLOT_WIDTH - 6))
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
    several models (``by_model`` is filled and the top-level ``arms`` is null). Each view comes
    with the suffix of its file names: the model's name with every character outside
    ``[A-Za-z0-9._-]`` as a hyphen, and ``-2``, ``-3`` and so on after it when an earlier model
    gave the same."""
    by_model = results.get("by_model")
    if not (isinstance(by_model, dict) and by_model and results.get("arms") is None
            and results.get("kind") != "replay"):
        return [("", results)]
    out: list[tuple[str, Mapping[str, Any]]] = []
    taken: set[str] = set()
    for model, block in by_model.items():
        suffix, number = f"-{_slug(model)}", 1
        while suffix in taken:
            number += 1
            suffix = f"-{_slug(model)}-{number}"
        taken.add(suffix)
        out.append((suffix, {**results, **block, "chart_model": model}))
    return out


Drawn = list[tuple[str, str, str]]  # file name, title, SVG text


def render(results: Mapping[str, Any]) -> tuple[Drawn, list[tuple[str, str]]]:
    """Draw every chart once. Returns the charts that can be drawn, as ``(file name, title, svg)``,
    and those that cannot, as ``(file name, reason)``. Nothing is written."""
    drawn, left_out = [], []
    for suffix, view in views(results):
        model = f", {view['chart_model']}" if view.get("chart_model") else ""
        for kind, draw in ((ACCURACY, accuracy_svg), (REQUEST_SIZE, request_size_svg)):
            name = f"{kind}{suffix}.svg"
            try:
                drawn.append((name, _clean(_labelled(TITLES[kind] + model, view)), draw(view)))
            except ChartError as exc:
                left_out.append((name, str(exc)))
    return drawn, left_out


def planned(results: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The file names and titles of the charts ``write`` would make from these results."""
    return [(name, title) for name, title, _ in render(results)[0]]


def skipped(results: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The charts ``write`` would not make from these results, each with the reason."""
    return render(results)[1]


def write(results: Mapping[str, Any] | None, charts_dir: Path | str, drawn: Drawn | None = None) -> list[Path]:
    """Write every chart the results allow into ``charts_dir`` and return the paths. A chart the
    file cannot give (``skipped``) is left out; with nothing to draw it raises ``ChartError``.
    ``drawn`` is the first value of an earlier ``render(results)``, to write without drawing again.

    All of the charts are written or none, as the report's files are: what cannot be written is an
    ``OSError``, the charts that were there before keep their content, and a ``charts_dir`` this
    call made is removed again."""
    from contexto.bench import report  # report imports this module

    if drawn is None:
        drawn, left_out = render(results or {})
        if not drawn:
            raise ChartError("; ".join(reason for _, reason in left_out) or "the results file has nothing to draw")
    charts_dir = Path(charts_dir)
    report._place(charts_dir, {name: svg.encode("utf-8") for name, _, svg in drawn})
    return [charts_dir / name for name, _, _ in drawn]
