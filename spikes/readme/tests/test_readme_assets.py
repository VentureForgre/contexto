"""The README's assets under docs/assets match the scripts in spikes/readme that make them."""

from __future__ import annotations

import csv
import json
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
ASSETS = ROOT / "docs" / "assets"
sys.path.insert(0, str(HERE))

import demo_script  # noqa: E402
import demo_upstream  # noqa: E402
import make_chart  # noqa: E402
import record_demo  # noqa: E402
import stub_claude  # noqa: E402
import termsvg  # noqa: E402

SVG = "{http://www.w3.org/2000/svg}"
HOME_PATH = re.compile(r"/Users/|/home/|/root\b|/var/folders/|/private/|/tmp/|~/")
TURN_LINE = re.compile(r"history ([\d,]+) tokens -> \[fake upstream\] received ([\d,]+) tokens")


def luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG 2 contrast ratio of two ``#rrggbb`` colours."""
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def parse_svg(text: str) -> ET.Element:
    root = ET.fromstring(text)
    assert root.tag == f"{SVG}svg"
    assert "<script" not in text.lower() and not list(root.iter(f"{SVG}script"))
    assert not re.search(r"\son\w+=", text), "no event handler attributes"
    assert "href=" not in text, "no links or external resources"
    return root


def number(text: str) -> int:
    return int(text.replace(",", ""))


# The chart.


def test_charts_regenerate_byte_identical(tmp_path):
    written = make_chart.write_charts(tmp_path)
    assert sorted(p.name for p in written) == ["request-size-dark.svg", "request-size-light.svg"]
    for path in written:
        assert path.read_bytes() == (ASSETS / path.name).read_bytes(), f"{path.name}: run spikes/readme/make_chart.py"


def test_chart_maxima_match_the_csv_and_the_research_record():
    series = make_chart.read_series()
    assert series.turns == list(range(1, 25))
    assert max(series.contexto) == 109_868
    assert max(series.plain) == series.plain[-1] == 438_406
    assert series.first_probe == 19 and series.measured == list(range(19, 25))
    assert max(series.contexto) < make_chart.BOUND < max(series.plain)
    record = make_chart.RECORD_PATH.read_text(encoding="utf-8")
    assert "109,868" in record and "438,406" in record


def test_chart_full_history_is_the_plain_run_shape_of_the_record():
    """Turns 1 to 18 are the CSV's modeled column; turns 19 to 24 are the baseline arm's measured
    counts, which the record lists on the line the constant cites."""
    series = make_chart.read_series()
    with make_chart.CSV_PATH.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    modeled = {int(r["turn"]): int(r["plain_input_modeled"]) for r in rows if r["kind"] == "content"}
    assert len(modeled) == 18 == sum(r["kind"] == "content" for r in rows), "one request per content turn"
    assert series.plain[:18] == [modeled[t] for t in range(1, 19)] and series.plain[17] == 433_633
    assert series.plain[18:] == list(make_chart.BASELINE_PROBE_TOKENS)
    assert make_chart.BASELINE_PROBE_TOKENS == (435_590, 436_155, 436_710, 437_280, 437_846, 438_406)
    listed = " / ".join(f"{v:,}" for v in make_chart.BASELINE_PROBE_TOKENS)
    record = make_chart.RECORD_PATH.read_text(encoding="utf-8")
    assert f"`count_tokens` per request: {listed};" in record, "the record lists the six counts"
    assert "overstates plain" in record, "why the CSV's probe-turn column is not plotted"
    # The same-request-points peak of the CSV is not in the chart.
    assert 493_902 not in series.plain
    with pytest.raises(ValueError, match="probe turns"):
        make_chart.read_series(probe_tokens=(1, 2))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_chart_plots_the_maxima_and_labels_them(theme):
    series = make_chart.read_series()
    text = (ASSETS / f"request-size-{theme}.svg").read_text(encoding="utf-8")
    root = parse_svg(text)
    assert len(text.encode("utf-8")) < 100_000
    assert not list(root.iter(f"{SVG}rect")), "the background stays transparent"
    paths = {p.get("class"): p.get("d") for p in root.iter(f"{SVG}path") if "L" in (p.get("d") or "")}
    for cls, values in (("series contexto", series.contexto), ("series plain", series.plain)):
        points = [tuple(float(v) for v in pair.split()) for pair in paths[cls][1:].split("L")]
        assert len(points) == 24
        # The highest plotted point is the series' maximum, at its turn.
        top = min(points, key=lambda p: p[1])
        turn = series.turns[values.index(max(values))]
        assert top == (round(make_chart.x_of(turn, series), 1), round(make_chart.y_of(max(values)), 1))
        assert all(y >= make_chart.TOP for _, y in points)
    labels = [t.text for t in root.iter(f"{SVG}text")]
    for label in ("109,868", "438,406", "Bound: 150,000 tokens", make_chart.LABEL_CONTEXTO, make_chart.LABEL_PLAIN,
                  make_chart.NOTE, make_chart.LABEL_DROP, "Content turns 1 to 18", "Recall questions, turns 19 to 24",
                  "500K"):
        assert label in labels
    assert "turns 1 to 18 modeled, 19 to 24 measured" in make_chart.LABEL_PLAIN
    assert "modeled for turns 1 to 18, measured for 19 to 24" in make_chart.NOTE
    assert "grows to 438,406 (modeled for turns 1 to 18, measured for turns 19 to 24)" in root.find(f"{SVG}desc").text
    assert "493,902" not in text and "#3" not in make_chart.NOTE
    # Each measured turn of the full history carries a dot; the label's sample has one too.
    dots = [(c.get("cx"), c.get("cy")) for c in root.iter(f"{SVG}circle") if c.get("fill") == make_chart.THEMES[theme]["plain"]]
    measured = [(make_chart.n(make_chart.x_of(t, series)), make_chart.n(make_chart.y_of(series.plain[t - 1])))
                for t in series.measured]
    assert dots[:6] == measured and len(dots) == 7
    # Every Contexto point is under the bound line; the full history crosses it.
    bound_y = make_chart.y_of(make_chart.BOUND)
    assert all(make_chart.y_of(v) > bound_y for v in series.contexto)


def test_chart_themes_share_geometry_and_differ_only_in_colour():
    series = make_chart.read_series()
    light, dark = make_chart.render(series, "light"), make_chart.render(series, "dark")
    colour = re.compile(r"#[0-9a-f]{6}")
    assert colour.sub("#", light) == colour.sub("#", dark)
    assert light != dark


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_chart_colours_read_on_the_page(theme):
    c = make_chart.THEMES[theme]
    assert c["page"] == {"light": "#ffffff", "dark": "#0d1117"}[theme]
    for text in ("ink", "muted"):
        assert contrast(c[text], c["page"]) >= 4.5, text
    for line in ("contexto", "plain", "muted"):  # muted is also the bound line
        assert contrast(c[line], c["page"]) >= 3, line
    text = make_chart.render(make_chart.read_series(), theme)
    assert "stroke-dasharray" in text.split(".plain{")[1].split("}")[0], "the lines differ by dash, not only hue"
    assert "stroke-dasharray" not in text.split(".contexto{")[1].split("}")[0]


def test_chart_reads_a_missing_csv_as_an_error(tmp_path, capsys):
    assert make_chart.main(["--csv", str(tmp_path / "none.csv"), "--out", str(tmp_path)]) == 1
    assert "make_chart:" in capsys.readouterr().err


# The renderer.

SMALL = [(0.0, "# a note\n"), (0.5, "$ tool run \\\n    --flag\n"), (1.0, "[stub claude] turn  1: history 10 tokens -> "),
         (1.01, "[fake upstream] received 10 tokens\n"), (2.5, "tool: <done> & \"quoted\"\n"),
         (3.0, "x" * 100 + "\n"), (9.0, "the fact is amber-kestrel-17\n")]


def test_renderer_emits_wellformed_svg_without_scripts():
    text = termsvg.render(SMALL, title="a <small> capture", highlight="amber-kestrel-17")
    root = parse_svg(text)
    assert len(text.encode("utf-8")) < record_demo.MAX_SVG_BYTES
    assert "@keyframes" in text and "prefers-reduced-motion" in text
    rows = ["".join(t.itertext()).replace(" ", " ") for t in root.iter(f"{SVG}text")]
    assert rows[0] == "a <small> capture"  # the bar
    assert rows[1:] == ["# a note", "$ tool run \\", "--flag",
                        "[stub claude] turn  1: history 10 tokens -> [fake upstream] received 10 tokens",
                        'tool: <done> & "quoted"', "x" * 90, "x" * 10, "the fact is amber-kestrel-17"]
    assert text == termsvg.render(SMALL, title="a <small> capture", highlight="amber-kestrel-17")


def test_renderer_rows_times_and_wrapping():
    rows = termsvg.rows_of(SMALL, cols=90)
    assert [r.kind for r in rows] == ["note", "command", "command", "output", "output", "output", "output", "output"]
    assert rows[3].at == 1.01, "a line appears when its last chunk arrived"
    assert [len(r.text) for r in rows[5:7]] == [90, 10]
    play, total = termsvg.schedule([r.at for r in rows])
    assert play == sorted(play) and play[0] == termsvg.LEAD
    assert play[-1] - play[-2] == pytest.approx(termsvg.MAX_GAP), "a long pause is cut"
    assert total == pytest.approx(play[-1] + termsvg.HOLD)
    assert termsvg.rows_of([(0.0, "no newline")])[0].text == "no newline"


def test_renderer_marks_who_printed_what():
    row = termsvg.Row("[stub claude] turn 2 -> [fake upstream] got amber-kestrel-17", 0.0, "output")
    assert termsvg.spans(row, "amber-kestrel-17") == [
        ("s", "[stub claude]"), ("d", " turn 2 -> "), ("u", "[fake upstream]"), ("d", " got "),
        ("h", "amber-kestrel-17")]
    assert termsvg.spans(termsvg.Row("Pages: 6", 0.0, "output"), None) == [("", "Pages: 6")]


def test_renderer_shows_the_two_sizes_of_a_turn_line_bright():
    row = termsvg.Row("[stub claude] turn 3: history 50,170 tokens -> [fake upstream] received 13,806 tokens", 0.0,
                      "output")
    assert termsvg.spans(row, None) == [
        ("s", "[stub claude]"), ("d", " turn 3: history "), ("", "50,170"), ("d", " tokens -> "),
        ("u", "[fake upstream]"), ("d", " received "), ("", "13,806"), ("d", " tokens")]
    # Only a stand-in's sizes: the tool's own lines and other numbers are left as they are.
    other = termsvg.Row("[fake upstream] listening on 127.0.0.1:8399, counting 1 token per 3.5 bytes received", 0.0,
                        "output")
    assert [cls for cls, _ in termsvg.spans(other, None)] == ["u", "d"]


def test_renderer_shows_the_first_line_from_the_start():
    """A viewer that stops at time 0 shows the note line, not an empty terminal; without animation
    every line is shown."""
    root = parse_svg(termsvg.render(SMALL, title="t"))
    rows = [t for t in root.iter(f"{SVG}text") if t.get("class") != "bar"]
    assert "".join(rows[0].itertext()) == "# a note" and rows[0].get("class") is None
    assert all(t.get("class") for t in rows[1:])
    still = parse_svg(termsvg.render(SMALL, title="t", animate=False))
    assert not any(t.get("class") for t in still.iter(f"{SVG}text") if t.get("class") != "bar")


def test_renderer_still_frame_and_scrolling():
    events = [(float(i), f"line {i}\n") for i in range(40)]
    text = termsvg.render(events, title="t", rows=10)
    parse_svg(text)
    assert "@keyframes scroll" in text
    assert f'transform="translate(0 {-30 * termsvg.LINE_H})"' in text, "without animation the last frame shows"
    still = termsvg.render(termsvg.frame_at(events, 4.0), title="t", rows=10, animate=False)
    assert "@keyframes" not in still and "line 4" in still and "line 5" not in still


def test_terminal_colours_read_on_the_panel():
    for name in ("fg", "d", "s", "u", "p", "n", "h"):
        assert contrast(termsvg.PALETTE[name], termsvg.PALETTE["panel"]) >= 4.5, name
    assert contrast(termsvg.PALETTE["d"], termsvg.PALETTE["bar"]) >= 4.5
    for page in ("#ffffff", "#0d1117"):  # the panel's edge shows on both of GitHub's pages
        assert max(contrast(termsvg.PALETTE["panel"], page), contrast(termsvg.PALETTE["border"], page)) >= 1.1


# The committed recording.


def test_recording_files_come_from_the_committed_capture():
    files = record_demo.outputs(record_demo.load_cast(ASSETS / "demo.cast.json"))
    for name, text in files.items():
        assert (ASSETS / name).read_text(encoding="utf-8") == text, f"{name}: run record_demo.py --render-only"


def test_recording_is_labelled_synthetic_and_holds_no_home_path():
    text = (ASSETS / "demo.txt").read_text(encoding="utf-8")
    assert text.splitlines()[0] == f"# {demo_script.SYNTHETIC_LABEL}"
    assert "No model" in demo_script.SYNTHETIC_LABEL and "stub" in demo_script.SYNTHETIC_LABEL
    svg = (ASSETS / "demo.svg").read_text(encoding="utf-8")
    assert "synthetic session" in record_demo.TITLE and record_demo.TITLE in svg
    for name in ("demo.txt", "demo.svg", "demo.cast.json", "request-size-light.svg", "request-size-dark.svg"):
        assert not HOME_PATH.search((ASSETS / name).read_text(encoding="utf-8")), name
    record_demo.check_capture(text)


def test_recording_svg_is_small_animated_and_shows_every_captured_line():
    svg = (ASSETS / "demo.svg").read_text(encoding="utf-8")
    root = parse_svg(svg)
    assert len(svg.encode("utf-8")) < 300_000
    assert "@keyframes" in svg and "infinite" in svg
    loop = float(re.search(r"scroll ([\d.]+)s", svg).group(1))
    assert 20 <= loop <= 45
    shown = "\n".join("".join(t.itertext()).replace(" ", " ") for t in root.iter(f"{SVG}text"))
    for line in (ASSETS / "demo.txt").read_text(encoding="utf-8").splitlines():
        for start in range(0, len(line), termsvg.COLS):  # a long line wraps, as in a terminal
            assert line[start:start + termsvg.COLS].strip() in shown
    assert max(len(line) for line in shown.splitlines()) <= termsvg.COLS


def test_recording_frames_first_drop_and_last():
    """What the image shows at three moments: the note line alone at time 0, a hold of about 2 s on the
    first turn where the upstream receives less than was sent, and a last frame (also the frame a viewer
    without animation shows) that runs from the session line to the last reply."""
    events = record_demo.load_cast(ASSETS / "demo.cast.json")
    svg = (ASSETS / "demo.svg").read_text(encoding="utf-8")
    rows = termsvg.rows_of(events)
    play, total = termsvg.schedule([row.at for row in rows])
    texts = [t for t in parse_svg(svg).iter(f"{SVG}text") if t.get("class") != "bar"]
    assert "".join(texts[0].itertext()) == f"# {demo_script.SYNTHETIC_LABEL}" and texts[0].get("class") is None
    assert all(t.get("class") for t in texts[1:]), "every later line appears at its time"
    assert json.loads((ASSETS / "demo.cast.json").read_text(encoding="utf-8"))["rows"] == termsvg.ROWS
    last = rows[-termsvg.ROWS:]
    assert last[0].text.startswith("contexto: session ") and f"(bound {record_demo.BOUND:,}," in last[0].text
    assert demo_script.PLANT_FACT in last[-1].text and len(rows) - termsvg.ROWS <= 4
    assert f'<g class="scroll" transform="translate(0 {-(len(rows) - termsvg.ROWS) * termsvg.LINE_H})">' in svg
    assert "prefers-reduced-motion:reduce){*{animation:none!important}}" in svg
    drop = next(i for i, row in enumerate(rows)
                if (m := TURN_LINE.search(row.text)) and number(m.group(2)) < number(m.group(1)))
    assert 1.8 <= play[drop + 1] - play[drop] <= 3.0, "the first drop is held for about 2 s"
    hit = next(i for i, row in enumerate(rows) if row.text.startswith("> "))
    after = next(at for at in play[hit:] if at > play[hit])
    assert after - play[hit] >= 2.5, "the recalled line is held"
    assert total - play[-1] == pytest.approx(termsvg.HOLD) and termsvg.HOLD >= 6
    # The two sizes of the first-drop line are bright: no class, so the text's own fill.
    line = texts[drop]
    assert [(span.get("class"), span.text) for span in line][:2] == [("s", demo_script.STUB_LABEL),
                                                                    ("d", " turn 3: history ")]
    sizes = TURN_LINE.search(rows[drop].text).groups()
    assert [span.text for span in line if span.get("class") is None] == list(sizes)
    # WebKit stretches bare text next to a <tspan> to the whole textLength, so a row has none.
    for text in texts:
        assert not (text.text or "").strip() and len(text), "a row is made of <tspan> pieces only"
        assert not any((span.tail or "").strip() for span in text), "no bare text between <tspan> pieces"


def check_story(text: str) -> None:
    """What the recording is for, read from a transcript: growth past the bound, a rotation, a recall."""
    lines = text.splitlines()
    stub, upstream = demo_script.STUB_LABEL, demo_script.UPSTREAM_LABEL
    session = next(i for i, line in enumerate(lines) if line.startswith("contexto: session "))
    assert f"(bound {record_demo.BOUND:,}, upstream flag)" in lines[session]
    assert lines[session - 2].startswith(f"$ contexto run --bound {record_demo.BOUND} ")
    assert lines[session - 3].startswith(f"{upstream} listening on 127.0.0.1:"), "the upstream listens first"
    turns = [(i, number(m.group(1)), number(m.group(2))) for i, line in enumerate(lines)
             if (m := TURN_LINE.search(line))]
    assert len(turns) == demo_script.GROWTH_TURNS + 2 and turns[0][0] > session
    assert [history for _, history, _ in turns] == sorted(history for _, history, _ in turns)
    assert max(history for _, history, _ in turns) > 1.5 * record_demo.BOUND
    assert all(received < record_demo.BOUND for _, _, received in turns)
    assert turns[0][1] == turns[0][2], "before the first rotation the upstream receives the whole history"
    # Turns are counted from 0, as the tool counts them.
    for n, (i, _, _) in enumerate(turns[:demo_script.GROWTH_TURNS]):
        assert lines[i].startswith(f"{stub} turn {n}: history ")
    assert all(lines[i].startswith(f"{stub} turn {demo_script.GROWTH_TURNS}: ") for i, _, _ in turns[-2:])
    planted = lines.index(f'{stub} turn {demo_script.PLANT_TURN} ends with: "{demo_script.PLANT}"')
    assert planted == turns[demo_script.PLANT_TURN][0] - 1
    drop = next(n for n, (_, history, received) in enumerate(turns) if received < history)
    assert 0 < drop < demo_script.GROWTH_TURNS and turns[drop][2] < turns[drop][1] / 2
    # The shell command is shown as it is run, one labelled row per line, and its output is the filter's.
    command = stub_claude.SHOW_COMMAND.split("\n")
    show = lines.index(f"{stub} runs ({stub_claude.SHOW_NOTE}): {command[0]}")
    assert lines[show + 1:show + len(command)] == [f"{stub} {row}" for row in command[1:]]
    assert show > turns[demo_script.GROWTH_TURNS - 1][0]
    asked = lines.index(f'{stub} turn {demo_script.GROWTH_TURNS} asks: "{demo_script.QUESTION}"')
    shown = lines[show + len(command):asked]
    pages = [re.fullmatch(r"  (pg_[0-9a-f]{16}) turn (\d+): Build log for step (\d+)\. Keep it for reference\.", line)
             for line in shown[2:]]
    assert shown[:2] == [f"Pages: {len(pages)}", "Over-bound turns: 0"] and pages and all(pages)
    assert all(m.group(2) == m.group(3) for m in pages), "the page list and the scripted text name the same turn"
    assert [int(m.group(2)) for m in pages] == sorted((int(m.group(2)) for m in pages), reverse=True)
    assert all(len(line) <= stub_claude.SHOW_COLUMNS for line in shown)
    hit = next(i for i, line in enumerate(lines) if line.startswith("> ") and demo_script.PLANT_FACT in line)
    assert show < asked < hit and lines[hit].endswith(f": {demo_script.PLANT}")
    page = re.fullmatch(r"(pg_[0-9a-f]{16}) \(turn (\d+)\)", lines[hit - 2])
    assert page and (page.group(1), page.group(2)) in [(m.group(1), m.group(2)) for m in pages], (
        "the hit is on a page the session had paged out")
    assert int(page.group(2)) == demo_script.PLANT_TURN, "the search names the turn the fact was planted in"
    assert f'{upstream} scripted reply: call contexto_search {{"query": "{demo_script.QUERY}"}}' in lines[asked:hit]
    assert not re.search(r"Turn \d|deploy|Log events", text)
    assert demo_script.PLANT_FACT in lines[-1] and lines[-1].startswith(demo_script.STUB_LABEL)
    for line in lines[session + 1:]:
        labelled = line.startswith((demo_script.STUB_LABEL, demo_script.UPSTREAM_LABEL))
        if "history" in line or "scripted" in line:
            assert labelled, f"a stub line without its label: {line}"


def test_committed_transcript_shows_the_rotation_and_the_recall():
    check_story((ASSETS / "demo.txt").read_text(encoding="utf-8"))


def test_a_fresh_recording_shows_the_same_story():
    """The whole recording, with no pauses: the real launcher, proxy, store and MCP server of this
    checkout, driven by the stub CLI against the fake upstream. About two seconds."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    events, extra = record_demo.record(port=port, pace=0)
    files = record_demo.outputs(events)
    record_demo.check_capture(files["demo.txt"])
    record_demo.check_private(files, extra)
    check_story(files["demo.txt"])
    parse_svg(files["demo.svg"])
    cast = json.loads(files["demo.cast.json"])
    assert "".join(text for _, text in cast["events"]) == files["demo.txt"]
    # Apart from the session id, the page ids and the port, a new run prints what the committed one printed.
    volatile = re.compile(r"[0-9a-f]{32}|pg_[0-9a-f]{16}|127\.0\.0\.1:\d+")
    committed = (ASSETS / "demo.txt").read_text(encoding="utf-8")
    assert volatile.sub("*", files["demo.txt"]) == volatile.sub("*", committed)


LISTENING = "import sys, time; time.sleep({delay}); print('[fake upstream] listening on 127.0.0.1:{port}, x'); " \
            "sys.stdout.flush(); time.sleep(30)"


def test_recorder_waits_for_the_upstream_listening_line():
    """The session starts only after the capture holds the upstream's own listening line, so the command
    line cannot be recorded before it."""
    capture = record_demo.Capture(90, 10)
    late = subprocess.Popen([sys.executable, "-c", LISTENING.format(delay=0.4, port=4567)],
                            stdout=capture.slave, stderr=capture.slave)
    try:
        record_demo.wait_for_listening(capture, 4567, late)
        capture.add("$ next\n")
        assert capture.text() == "[fake upstream] listening on 127.0.0.1:4567, x\n$ next\n"
        # The line of another port is not this upstream's line.
        with pytest.raises(RuntimeError, match=r"did not print its listening line for 127\.0\.0\.1:456 within"):
            record_demo.wait_for_listening(capture, 456, late, timeout=0.2)
    finally:
        late.kill()
        late.wait()
    gone = subprocess.Popen([sys.executable, "-c", "import sys; print('no room'); sys.exit(3)"],
                            stdout=capture.slave, stderr=capture.slave)
    try:
        with pytest.raises(RuntimeError, match=r"exited with code 3 before it listened(.|\n)*no room"):
            record_demo.wait_for_listening(capture, 4567 + 1, gone)
    finally:
        gone.wait()
        capture.close()


def test_recorder_refuses_a_port_another_server_holds():
    """With another server on the port the fake upstream cannot bind and exits; the recorder stops there
    and sends nothing to the other server."""
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        other.settimeout(0.5)
        with pytest.raises(RuntimeError, match="exited with code 1 before it listened"):
            record_demo.record(port=other.getsockname()[1], pace=0)
        with pytest.raises(TimeoutError):
            other.accept()


def test_recorder_ends_a_session_that_runs_too_long():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(RuntimeError, match=r"did not finish within 0\.05 s and was killed(.|\n)*\$ contexto run"):
        record_demo.record(port=port, pace=0, timeout=0.05)


def test_recorder_ends_what_a_finished_session_left_running():
    """A child that outlives the session (a shell command the stub gave up on) holds the terminal, and
    the capture's close would wait for it. The recorder ends the session's whole process group."""
    capture = record_demo.Capture(90, 10)
    std = {"stdin": subprocess.DEVNULL, "stdout": capture.slave, "stderr": capture.slave}
    started = time.monotonic()
    assert record_demo.run_session("sleep 60 & echo left-behind", capture, 20, **std) == 0
    events = capture.close()
    assert time.monotonic() - started < 15, "the sleep was ended, not waited for"
    assert "left-behind" in "".join(text for _, text in events)


def test_stub_gives_up_on_a_silent_mcp_server():
    silent = {"command": sys.executable, "args": ["-c", "import time; time.sleep(30)"]}
    with pytest.raises(RuntimeError, match=r"did not answer initialize within 0\.2 s"):
        stub_claude.McpServer(silent, timeout=0.2)
    closed = {"command": sys.executable, "args": ["-c", "pass"]}
    with pytest.raises((RuntimeError, OSError)):
        stub_claude.McpServer(closed, timeout=5)


# The stand-ins.


def test_private_check_refuses_identifying_text():
    with pytest.raises(RuntimeError, match="identifies"):
        record_demo.check_private({"demo.txt": "cwd is /srv/Secret-Dir/x"}, ["/srv/secret-dir"])
    record_demo.check_private({"demo.txt": "nothing here"}, ["/srv/secret-dir"])
    with pytest.raises(RuntimeError, match="nothing was written"):
        record_demo.check_capture("no label")


def test_build_log_never_matches_the_search():
    log = demo_script.build_log(3, 5_000).lower()
    assert not any(word in log for word in demo_script.QUERY.split())
    assert max(len(line) for line in log.splitlines()) <= 60
    assert demo_script.prompt(demo_script.PLANT_TURN).endswith(demo_script.PLANT)
    assert demo_script.PLANT_TURN == 1 and demo_script.PLANT_FACT not in demo_script.prompt(0)
    assert demo_script.prompt(5).startswith("Build log for step 5. Keep it for reference.\nbuild 05.0001 ")
    assert not any(word in demo_script.PLANT.lower() for word in ("token", "key", "secret", "password"))
    assert demo_script.PLANT not in demo_script.prompt(demo_script.PLANT_TURN)[:1000], "not in the page index"
    assert demo_script.tokens(7) == 2 and demo_script.tokens(35) == 10


def test_fake_upstream_follows_its_three_rules():
    def user(*blocks):
        return {"messages": [{"role": "user", "content": list(blocks)}]}

    assert demo_upstream.scripted_reply(user({"type": "text", "text": "anything"})) == {"text": "OK"}
    assert demo_upstream.scripted_reply({"messages": [{"role": "user", "content": demo_script.QUESTION}]}) == {
        "tool": demo_script.SEARCH_TOOL, "input": {"query": demo_script.QUERY}}
    result = {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": f"> 5: {demo_script.PLANT}"}]}
    assert demo_upstream.scripted_reply(user(result)) == {"text": f"The search result names it: {demo_script.PLANT_FACT}."}
    other = {"type": "tool_result", "tool_use_id": "t", "content": "> 5: The release codename is x-1."}
    assert demo_upstream.scripted_reply(user(other)) == {"text": "The search result names it: x-1."}
    empty = {"type": "tool_result", "tool_use_id": "t", "content": "No paged-out turn matches."}
    assert demo_upstream.scripted_reply(user(empty)) == {"text": "The search result does not name it."}


def test_stub_reads_the_stream_the_fake_upstream_writes():
    raw = b"".join(demo_upstream.reply_events({"tool": demo_script.SEARCH_TOOL, "input": {"query": "a b"}}, 12, 1))
    content, stop, received = stub_claude.parse_stream(raw)
    assert stop == "tool_use" and received == 12, "the stub reads what the upstream received from the usage"
    assert content == [{"type": "tool_use", "id": "toolu_fake_0001", "name": demo_script.SEARCH_TOOL,
                        "input": {"query": "a b"}}]
    content, stop, received = stub_claude.parse_stream(b"".join(demo_upstream.reply_events({"text": "OK"}, 34, 2)))
    assert (content, stop, received) == ([{"type": "text", "text": "OK"}], "end_turn", 34)
    tools = stub_claude.tool_definitions([{"name": "contexto_search", "description": "d", "inputSchema": {"type": "object"}}])
    assert tools == [{"name": demo_script.SEARCH_TOOL, "description": "d", "input_schema": {"type": "object"}}]
    assert stub_claude.main(["--bogus"]) == 2
