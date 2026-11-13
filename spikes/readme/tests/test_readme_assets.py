"""The README's assets under docs/assets match the scripts in spikes/readme that make them."""

from __future__ import annotations

import json
import re
import socket
import sys
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
    assert max(series.plain) == 493_902
    assert series.first_probe == 19
    assert max(series.contexto) < make_chart.BOUND < max(series.plain)
    record = (ROOT / "docs" / "research" / "M2-usage-savings-run3.md").read_text(encoding="utf-8")
    assert "109,868" in record and "493,902" in record


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
    for label in ("109,868", "493,902", "Bound 150,000", make_chart.LABEL_CONTEXTO, make_chart.LABEL_PLAIN,
                  make_chart.NOTE, "Content turns 1 to 18", "Probe turns 19 to 24", "500K"):
        assert label in labels
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
    assert 20 <= loop <= 40
    shown = "\n".join("".join(t.itertext()).replace(" ", " ") for t in root.iter(f"{SVG}text"))
    for line in (ASSETS / "demo.txt").read_text(encoding="utf-8").splitlines():
        for start in range(0, len(line), termsvg.COLS):  # a long line wraps, as in a terminal
            assert line[start:start + termsvg.COLS].strip() in shown
    assert max(len(line) for line in shown.splitlines()) <= termsvg.COLS


def check_story(text: str) -> None:
    """What the recording is for, read from a transcript: growth past the bound, a rotation, a recall."""
    lines = text.splitlines()
    session = next(i for i, line in enumerate(lines) if line.startswith("contexto: session "))
    assert f"(bound {record_demo.BOUND:,}, upstream flag)" in lines[session]
    assert lines[session - 2].startswith(f"$ contexto run --bound {record_demo.BOUND} ")
    turns = [(i, number(m.group(1)), number(m.group(2))) for i, line in enumerate(lines)
             if (m := TURN_LINE.search(line))]
    assert len(turns) == demo_script.GROWTH_TURNS + 2 and turns[0][0] > session
    assert [history for _, history, _ in turns] == sorted(history for _, history, _ in turns)
    assert max(history for _, history, _ in turns) > 1.5 * record_demo.BOUND
    assert all(received < record_demo.BOUND for _, _, received in turns)
    assert turns[0][1] == turns[0][2], "before the first rotation the upstream receives the whole history"
    show = next(i for i, line in enumerate(lines) if "runs: contexto sessions show" in line)
    assert show > turns[demo_script.GROWTH_TURNS - 1][0]
    assert number(re.search(r"^Pages: (\d+)$", text, re.M).group(1)) > 0
    assert "Over-bound turns: 0" in lines[show:]
    asked = next(i for i, line in enumerate(lines) if demo_script.QUESTION in line)
    hit = next(i for i, line in enumerate(lines) if line.startswith("> ") and demo_script.PLANT_FACT in line)
    assert show < asked < hit
    page = re.fullmatch(r"(pg_[0-9a-f]{16}) \(turn (\d+)\)", lines[hit - 2])
    assert page and any(line.startswith(f"  {page.group(1)} turn {page.group(2)}:") for line in lines[show:asked]), (
        "the hit is on a page the session had paged out")
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
    # Apart from ids, times and the port, a new run prints what the committed one printed.
    volatile = re.compile(r"[0-9a-f]{32}|pg_[0-9a-f]{16}|\d{4}-\d\d-\d\dT[\d:]+Z|127\.0\.0\.1:\d+")
    committed = (ASSETS / "demo.txt").read_text(encoding="utf-8")
    assert volatile.sub("*", files["demo.txt"]) == volatile.sub("*", committed)


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
    assert demo_script.PLANT_FACT not in demo_script.prompt(1)
    assert demo_script.PLANT not in demo_script.prompt(demo_script.PLANT_TURN)[:1000], "not in the page index"
    assert demo_script.tokens(7) == 2 and demo_script.tokens(35) == 10


def test_fake_upstream_follows_its_three_rules():
    def user(*blocks):
        return {"messages": [{"role": "user", "content": list(blocks)}]}

    assert demo_upstream.scripted_reply(user({"type": "text", "text": "anything"})) == {"text": "OK"}
    assert demo_upstream.scripted_reply({"messages": [{"role": "user", "content": demo_script.QUESTION}]}) == {
        "tool": demo_script.SEARCH_TOOL, "input": {"query": demo_script.QUERY}}
    result = {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": "> 5: the token is named x-1."}]}
    assert demo_upstream.scripted_reply(user(result)) == {"text": "The search result names it: x-1."}
    empty = {"type": "tool_result", "tool_use_id": "t", "content": "No paged-out turn matches."}
    assert demo_upstream.scripted_reply(user(empty)) == {"text": "The search result does not name it."}


def test_stub_reads_the_stream_the_fake_upstream_writes():
    raw = b"".join(demo_upstream.reply_events({"tool": demo_script.SEARCH_TOOL, "input": {"query": "a b"}}, 12, 1))
    content, stop = stub_claude.parse_stream(raw)
    assert stop == "tool_use"
    assert content == [{"type": "tool_use", "id": "toolu_fake_0001", "name": demo_script.SEARCH_TOOL,
                        "input": {"query": "a b"}}]
    content, stop = stub_claude.parse_stream(b"".join(demo_upstream.reply_events({"text": "OK"}, 12, 2)))
    assert (content, stop) == ([{"type": "text", "text": "OK"}], "end_turn")
    tools = stub_claude.tool_definitions([{"name": "contexto_search", "description": "d", "inputSchema": {"type": "object"}}])
    assert tools == [{"name": demo_script.SEARCH_TOOL, "description": "d", "input_schema": {"type": "object"}}]
    assert stub_claude.main(["--bogus"]) == 2
