"""Render a captured terminal session as an animated SVG. Standard library only, no scripts in the output.

The input is a capture: a list of ``(seconds, text)`` chunks in the order a terminal received them.
The output is one SVG: a dark panel, one ``<text>`` element per terminal row, shown at its time by a
CSS animation, with the rows scrolling up once the screen is full. GitHub shows an SVG in a README
through ``<img>``, where scripts do not run and CSS animations do.

What the renderer changes, and what it does not:

- It never adds, drops or rewrites text. A line longer than the screen wraps, as in a terminal.
- The first line is there from the start of the loop, so a viewer that stops at time 0 does not show
  an empty terminal. Every later line appears when its last chunk arrived. Pauses longer than
  ``MAX_GAP`` seconds are cut to ``MAX_GAP``, and the last frame is held for ``HOLD`` seconds before
  the loop restarts.
- Colour only marks who printed what: the prompt and command, the ``[stub claude]`` and
  ``[fake upstream]`` lines (dim, with the two sizes of a turn line bright), and the planted fact.
  Unlabelled lines are the tool's own output.

Without animation support (a static renderer, or ``prefers-reduced-motion``) the SVG shows its last
frame. ``frame_at`` gives the capture up to a time, to render an earlier frame as a still.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.sax.saxutils import escape

Events = list[tuple[float, str]]

COLS = 90
ROWS = 33  # the recording is 37 rows: the last frame runs from the `contexto: session` line to the last reply
FONT_SIZE = 14
CHAR_W = 8.4  # 0.6 em, the advance of the common monospace fonts; textLength pins it for the others
LINE_H = 19
PAD_X = 22
PAD_Y = 14
BAR_H = 34
LEAD = 0.8  # seconds the first line is shown alone when the capture has no pause after it
MAX_GAP = 4.0
HOLD = 8.0
FONT = 'ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono","DejaVu Sans Mono",monospace'

LABELS = {"[stub claude]": "s", "[fake upstream]": "u"}
# The two sizes of a turn line: what the stub sent and what the fake upstream received.
FIGURES = re.compile(r"(?<=history )[\d,]*\d(?= tokens)|(?<=received )[\d,]*\d(?= tokens)")
# Each colour is at least 4.5:1 against the panel (#161b22); see test_readme_assets.py.
PALETTE = {
    "panel": "#161b22", "bar": "#21262d", "border": "#30363d",
    "fg": "#e6edf3",  # the tool's own output
    "d": "#9198a1",  # text after a stub label
    "s": "#a5d6ff", "u": "#d2a8ff",  # the two stub labels
    "p": "#3fb950", "n": "#e3b341", "h": "#e3b341",  # prompt, note line, the planted fact
}


@dataclass(frozen=True)
class Row:
    text: str
    at: float  # capture time of the line's last chunk
    kind: str  # "command", "note" or "output"


def rows_of(events: Events, cols: int = COLS) -> list[Row]:
    """The terminal rows of a capture, in order: one per line, more when a line is wider than ``cols``."""
    rows: list[Row] = []
    pending = ""
    last = 0.0
    continued = False  # the previous line was a command ending in a backslash

    def close(line: str, at: float) -> None:
        nonlocal continued
        if line.startswith("$ ") or continued:
            kind = "command"
        elif line.startswith("# "):
            kind = "note"
        else:
            kind = "output"
        continued = kind == "command" and line.endswith("\\")
        for start in range(0, max(len(line), 1), cols):
            rows.append(Row(line[start:start + cols], at, kind))

    for at, text in events:
        pending += text.replace("\r", "")
        last = at
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            close(line, at)
    if pending:
        close(pending, last)
    return rows


def frame_at(events: Events, seconds: float) -> Events:
    """The capture up to ``seconds``, for a still of an earlier frame."""
    return [(at, text) for at, text in events if at <= seconds]


def schedule(times: list[float]) -> tuple[list[float], float]:
    """Playback times for capture times, and the loop length. Gaps above ``MAX_GAP`` are cut to it."""
    play: list[float] = []
    clock = LEAD
    previous = times[0] if times else 0.0
    for at in times:
        clock += min(max(at - previous, 0.0), MAX_GAP)
        previous = at
        play.append(round(clock, 2))
    return play, round((play[-1] if play else LEAD) + HOLD, 2)


def spans(row: Row, highlight: str | None) -> list[tuple[str, str]]:
    """``(class, text)`` pieces of a row. The class "" is the tool's own output."""
    text = row.text
    if row.kind == "note":
        return [("n", text)]
    if row.kind == "command":
        return [("p", "$"), ("", text[1:])] if text.startswith("$ ") else [("", text)]
    pieces: list[tuple[str, str]] = []
    current = ""
    pattern = "|".join(re.escape(label) for label in LABELS)
    position = 0
    for match in re.finditer(pattern, text):
        if match.start() > position:
            pieces.append((current, text[position:match.start()]))
        pieces.append((LABELS[match.group()], match.group()))
        current = "d"
        position = match.end()
    if position < len(text):
        pieces.append((current, text[position:]))
    pieces = [part for cls, piece in pieces for part in (_figures(piece) if cls == "d" else [(cls, piece)])]
    if not highlight:
        return pieces
    out: list[tuple[str, str]] = []
    for cls, piece in pieces:
        parts = piece.split(highlight)
        for i, part in enumerate(parts):
            if i:
                out.append(("h", highlight))
            if part:
                out.append((cls, part))
    return out


def _figures(piece: str) -> list[tuple[str, str]]:
    """A dim piece with its sizes in the bright colour of the tool's own output."""
    out: list[tuple[str, str]] = []
    position = 0
    for match in FIGURES.finditer(piece):
        if match.start() > position:
            out.append(("d", piece[position:match.start()]))
        out.append(("", match.group()))
        position = match.end()
    if position < len(piece):
        out.append(("d", piece[position:]))
    return out


def _text(piece: str) -> str:
    """XML text for a piece: runs of two or more spaces as no-break spaces, so they are not collapsed."""
    return escape(re.sub(r" {2,}", lambda m: " " * len(m.group()), piece))


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def render(events: Events, *, title: str, highlight: str | None = None, cols: int = COLS, rows: int = ROWS,
           animate: bool = True) -> str:
    """The SVG for a capture. ``title`` stays in the panel's top bar for the whole loop."""
    lines = rows_of(events, cols)
    play, total = schedule([line.at for line in lines])
    width = round(cols * CHAR_W + 2 * PAD_X)
    screen_h = rows * LINE_H
    height = BAR_H + 2 * PAD_Y + screen_h
    top = BAR_H + PAD_Y
    shown = max(0, len(lines) - rows)  # rows scrolled off at the end

    css = [f"text{{font-family:{FONT};font-size:{FONT_SIZE}px;fill:{PALETTE['fg']};white-space:pre}}",
           f".bar{{fill:{PALETTE['d']};font-size:13px}}"]
    css += [f".{cls}{{fill:{PALETTE[cls]}}}" for cls in ("d", "s", "u", "p", "n", "h")]
    classes: dict[float, str] = {}
    scroll: list[tuple[float, int]] = []
    if animate:
        for i, at in enumerate(play):
            if at == play[0]:
                continue  # the first line has no animation: it is there from 0%
            if at not in classes:
                name = f"t{len(classes)}"
                classes[at] = name
                percent = _number(at / total * 100)
                css.append(f"@keyframes {name}{{0%{{opacity:0}}{percent}%,to{{opacity:1}}}}")
                css.append(f".{name}{{animation:{name} {_number(total)}s step-end infinite}}")
            if i >= rows and (not scroll or scroll[-1][0] != at):
                scroll.append((at, i - rows + 1))
            elif i >= rows:
                scroll[-1] = (at, i - rows + 1)
        if scroll:
            stops = "".join(f"{_number(at / total * 100)}%{{transform:translateY({-n * LINE_H}px)}}"
                            for at, n in scroll)
            css.append(f"@keyframes scroll{{0%{{transform:translateY(0)}}{stops}}}")
            css.append(f".scroll{{animation:scroll {_number(total)}s step-end infinite}}")
        css.append("@media (prefers-reduced-motion:reduce){*{animation:none!important}}")

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
           f'height="{height}" role="img" aria-label="{escape(title, {chr(34): "&quot;"})}">',
           f"<title>{escape(title)}</title>",
           f"<style>{''.join(css)}</style>",
           f'<clipPath id="screen"><rect x="0" y="{top}" width="{width}" height="{screen_h}"/></clipPath>',
           f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="8" fill="{PALETTE["panel"]}" '
           f'stroke="{PALETTE["border"]}"/>',
           f'<path d="M0.5 {BAR_H}V8.5a8 8 0 0 1 8-8H{width - 8.5}a8 8 0 0 1 8 8V{BAR_H}z" fill="{PALETTE["bar"]}" '
           f'stroke="{PALETTE["border"]}"/>',
           f'<text class="bar" x="{width / 2:g}" y="{BAR_H / 2 + 4.5:g}" text-anchor="middle">{escape(title)}</text>',
           '<g clip-path="url(#screen)">',
           f'<g class="scroll" transform="translate(0 {-shown * LINE_H})">' if shown else "<g>"]
    for i, line in enumerate(lines):
        stripped = line.text.lstrip(" ")
        if not stripped:
            continue
        indent = len(line.text) - len(stripped)
        pieces = spans(Row(stripped, line.at, line.kind), highlight)
        body = "".join(f'<tspan class="{cls}">{_text(piece)}</tspan>' if cls else _text(piece)
                       for cls, piece in pieces)
        cls = f' class="{classes[play[i]]}"' if play[i] in classes else ""
        out.append(f'<text{cls} x="{_number(PAD_X + indent * CHAR_W)}" y="{top + i * LINE_H + 14}" '
                   f'textLength="{_number(len(stripped) * CHAR_W)}">{body}</text>')
    out += ["</g>", "</g>", "</svg>", ""]
    return "\n".join(out)
