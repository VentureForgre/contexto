"""``contexto.bench.charts`` and ``contexto bench --chart``: the two SVG charts of a results file (D66).

The coordinates checked here are computed by hand from the geometry the module documents: the
request-size plot is 700 wide from x = 76 and 300 high, the accuracy plot 536 wide from x = 200.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from contexto import cli
from contexto.bench import charts, report
from test_bench_records import ARMS, MODEL, SMALL_BOUND, make_run

SVG = "{http://www.w3.org/2000/svg}"
NOTES = ["Sizes are estimates: there is no tokenizer without a model.",
         "The full-context and plain lines are modelled from the same trace, not run."]


def parse(svg: str) -> ET.Element:
    root = ET.fromstring(svg)
    assert root.tag == f"{SVG}svg"
    return root


def texts(root: ET.Element, **attrs) -> list[str]:
    return [t.text for t in root.iter(f"{SVG}text") if all(t.get(k.replace("_", "-")) == v for k, v in attrs.items())]


def all_text(root: ET.Element) -> str:
    return " ".join(t.text or "" for t in root.iter(f"{SVG}text"))


def line_results(**fields) -> dict:
    """A results file with round numbers, so the plotted coordinates can be worked out by hand."""
    out = {"format": 1, "kind": "live", "bound": 100, "notes": list(NOTES), "request_series": {
        "contexto": {"basis": "measured", "label": "Contexto", "points": [[0, 0], [500, 50], [1000, 80]]},
        "full": {"basis": "measured", "label": "Full context", "points": [[0, 0], [500, 500], [1000, 1000]]}}}
    out.update(fields)
    return out


def cell(correct: int, probes: int, interval: list[float], **more) -> dict:
    return {"probes": probes, "correct": correct, "accuracy": correct / probes, "interval": interval, **more}


def accuracy_results() -> dict:
    return {"format": 1, "kind": "live", "bound": 100, "notes": [], "request_series": {}, "arms": {
        "contexto": cell(90, 100, [0.8, 0.95], by_class={"chain": cell(45, 50, [0.75, 0.97])}),
        "plain": cell(60, 100, [0.5, 0.7], by_class={"chain": cell(27, 45, [0.55, 0.72])})}}


# Request size against history length


def test_axis_top_is_the_next_round_tick():
    assert charts.axis_top(1000) == (1000, 200)
    assert charts.axis_top(20_711) == (25_000, 5_000)
    assert charts.axis_top(430_000) == (500_000, 100_000)
    assert charts.axis_top(7) == (8, 2)
    assert charts.axis_top(0) == (1, 1)


def test_request_size_points_are_plotted_where_the_geometry_says():
    root = parse(charts.request_size_svg(line_results()))
    lines = {p.get("data-series"): p for p in root.iter(f"{SVG}polyline")}
    assert list(lines) == ["contexto", "full"]
    # Both axes end at 1,000. x = 76 + history / 1000 * 700. With no subtitle the plot runs from
    # y = 74 to y = 374, so y = 374 - size / 1000 * 300.
    assert lines["contexto"].get("points") == "76.0,374.0 426.0,359.0 776.0,350.0"
    assert lines["full"].get("points") == "76.0,374.0 426.0,224.0 776.0,74.0"
    bound = next(e for e in root.iter(f"{SVG}line") if e.get("data-bound") == "100")
    assert (bound.get("x1"), bound.get("y1"), bound.get("x2"), bound.get("y2")) == ("76.0", "344.0", "776.0", "344.0")
    assert texts(root, data_bound_label="100") == ["bound: 100 tokens"]
    markers = {g.get("data-markers"): g for g in root.iter(f"{SVG}g") if g.get("data-markers")}
    circles = [(c.get("cx"), c.get("cy")) for c in markers["contexto"].iter(f"{SVG}circle")]
    assert circles == [("76.0", "374.0"), ("426.0", "359.0"), ("776.0", "350.0")]


def test_request_size_has_axes_with_units_ticks_legend_and_footnotes():
    root = parse(charts.request_size_svg(line_results()))
    assert texts(root, data_axis="x") == ["History length (tokens of conversation so far)"]
    assert texts(root, data_axis="y") == ["Request input (tokens)"]
    assert texts(root, data_tick="x") == ["0", "200", "400", "600", "800", "1,000"]
    assert texts(root, data_tick="y") == ["0", "200", "400", "600", "800", "1,000"]
    assert texts(root, data_legend="contexto") == ["Contexto (measured)"]
    assert texts(root, data_legend="full") == ["Full context (measured)"]
    assert texts(root, data_note="start") == NOTES  # each note fits one line here
    assert root.find(f"{SVG}title").text == "Request size against history length"


def test_long_footnotes_are_wrapped_and_none_is_lost():
    note = "This note is longer than one line of the chart. " * 6
    root = parse(charts.request_size_svg(line_results(notes=[note.strip(), "Short."])))
    assert len(texts(root, data_note="start")) == 2
    printed = " ".join(t for t in texts(root, font_size="11") if not t.replace(",", "").isdigit())
    assert note.strip() in printed and printed.endswith("Short.")


def test_a_replay_file_is_titled_synthetic_and_names_each_basis_in_words():
    results = line_results(kind="replay")
    results["request_series"] = {
        "contexto": {"basis": "estimated", "label": "Contexto", "points": [[0, 0], [1000, 80]]},
        "full": {"basis": "modelled", "label": "Full context", "points": [[0, 0], [1000, 1000]]},
        "plain": {"basis": "modelled", "label": "Plain Claude Code", "points": [[0, 0], [600, 120], [700, 20]]}}
    root = parse(charts.request_size_svg(results))
    title = "Request size against history length: synthetic session, no model"
    assert root.find(f"{SVG}title").text == title and title in all_text(root)
    assert [texts(root, data_legend=name)[0] for name in ("contexto", "full", "plain")] == [
        "Contexto (estimated)", "Full context (modelled)", "Plain Claude Code (modelled)"]
    assert "no model" not in all_text(parse(charts.request_size_svg(line_results())))


def test_series_differ_by_dash_and_marker_as_well_as_colour():
    results = line_results()
    results["request_series"]["plain"] = {"basis": "measured", "label": "Plain Claude Code",
                                          "points": [[0, 0], [600, 120], [700, 20]]}
    root = parse(charts.request_size_svg(results))
    lines = {p.get("data-series"): p for p in root.iter(f"{SVG}polyline")}
    assert len({p.get("stroke") for p in lines.values()}) == 3
    assert len({p.get("stroke-dasharray") for p in lines.values()}) == 3
    shapes = {g.get("data-markers"): {child.tag.removeprefix(SVG) for child in g}
              for g in root.iter(f"{SVG}g") if g.get("data-markers")}
    assert shapes == {"contexto": {"circle"}, "full": {"rect"}, "plain": {"polygon"}}


def test_the_background_is_explicit_and_text_is_never_drawn_in_a_series_colour():
    for svg in (charts.request_size_svg(line_results()), charts.accuracy_svg(accuracy_results())):
        root = parse(svg)
        background = root.find(f"{SVG}rect")
        assert (background.get("x"), background.get("y"), background.get("width")) == ("0", "0", "800")
        assert background.get("fill") == charts.SURFACE
        assert root.get("width") == "800" and root.get("viewBox").startswith("0 0 800 ")
        colours = {style[0] for style in charts.STYLES.values()}
        assert not {t.get("fill") for t in root.iter(f"{SVG}text")} & colours


def test_a_long_series_gets_a_marker_on_some_points_and_a_line_through_all():
    results = line_results()
    results["request_series"]["contexto"]["points"] = [[i * 10, 50] for i in range(101)]
    root = parse(charts.request_size_svg(results))
    line = next(p for p in root.iter(f"{SVG}polyline") if p.get("data-series") == "contexto")
    assert len(line.get("points").split()) == 101
    markers = next(g for g in root.iter(f"{SVG}g") if g.get("data-markers") == "contexto")
    xs = [c.get("cx") for c in markers.iter(f"{SVG}circle")]
    assert len(xs) == 35 and xs[0] == "76.0" and xs[-1] == "776.0"  # every third point, and the last


def test_a_file_without_series_cannot_be_drawn():
    with pytest.raises(charts.ChartError, match="no request_series"):
        charts.request_size_svg({"kind": "live", "bound": 100, "request_series": {}})
    with pytest.raises(charts.ChartError, match="no bound"):
        charts.request_size_svg({k: v for k, v in line_results().items() if k != "bound"})


# Accuracy by arm


def test_accuracy_points_and_intervals_are_plotted_where_the_geometry_says():
    root = parse(charts.accuracy_svg(accuracy_results()))
    # The lowest interval end is 50%, so the axis runs from 50% to 100% in steps of 10 points:
    # x = 200 + (accuracy - 0.5) / 0.5 * 536. With no subtitle the first row starts at y = 70 and
    # a row of two arms is 46 high, each arm 16 apart from y = 85.
    assert texts(root, data_tick="x") == ["50%", "60%", "70%", "80%", "90%", "100%"]
    marks = {(g.get("data-arm"), g.get("data-row")): g for g in root.iter(f"{SVG}g") if g.get("data-arm")}
    assert list(marks) == [("contexto", "All probes"), ("plain", "All probes"), ("contexto", "chain"),
                           ("plain", "chain")]
    line = marks[("contexto", "All probes")].find(f"{SVG}line")
    assert (line.get("x1"), line.get("x2"), line.get("y1"), line.get("y2")) == ("521.6", "682.4", "85.0", "85.0")
    point = marks[("contexto", "All probes")].find(f"{SVG}circle")
    assert (point.get("cx"), point.get("cy")) == ("628.8", "85.0")
    line = marks[("plain", "All probes")].find(f"{SVG}line")
    assert (line.get("x1"), line.get("x2"), line.get("y1")) == ("200.0", "414.4", "101.0")
    assert marks[("plain", "All probes")].find(f"{SVG}polygon") is not None  # a triangle, not a circle
    chain = marks[("contexto", "chain")].find(f"{SVG}circle")
    assert (chain.get("cx"), chain.get("cy")) == ("628.8", "131.0")  # 90% again, one row of 46 lower


def test_accuracy_rows_carry_n_and_the_overall_values():
    root = parse(charts.accuracy_svg(accuracy_results()))
    assert texts(root, data_row="All probes") == ["All probes"] and texts(root, data_row="chain") == ["chain"]
    assert texts(root, data_n="All probes") == ["n = 100"]
    assert texts(root, data_n="chain") == ["n = 50 / 45"]  # the arms differ, so one figure per arm
    assert texts(root, data_value="contexto") == ["90.0%"] and texts(root, data_value="plain") == ["60.0%"]
    assert texts(root, data_axis="x") == ["Probes answered correctly (the axis starts at 50%, not at 0)"]
    assert texts(root, data_legend="contexto") == ["Contexto"]
    assert texts(root, data_legend="plain") == ["Plain Claude Code"]
    hover = [t.text for t in root.iter(f"{SVG}title")]
    assert "Contexto, All probes: 90.0% (90 of 100), 95% interval 80.0% to 95.0%" in hover
    assert "Wilson 95% interval" in all_text(root)


def test_the_accuracy_axis_starts_at_zero_when_an_interval_reaches_low():
    results = accuracy_results()
    results["arms"]["plain"]["by_class"]["chain"] = cell(1, 6, [0.03, 0.56])
    root = parse(charts.accuracy_svg(results))
    assert texts(root, data_tick="x") == ["0%", "20%", "40%", "60%", "80%", "100%"]
    assert texts(root, data_axis="x") == ["Probes answered correctly"]


def test_an_arm_that_did_not_run_is_said_not_drawn():
    results = accuracy_results()
    results["arms"]["full"] = {"probes": 0, "correct": 0, "accuracy": None, "interval": None, "by_class": {}}
    root = parse(charts.accuracy_svg(results))
    assert "Full context: not run" in all_text(root)
    assert not [g for g in root.iter(f"{SVG}g") if g.get("data-arm") == "full"]


def test_a_replay_file_has_no_accuracy_chart():
    with pytest.raises(charts.ChartError, match="a replay makes no model calls"):
        charts.accuracy_svg(line_results(kind="replay"))
    with pytest.raises(charts.ChartError, match="no arms block"):
        charts.accuracy_svg(line_results())


# From a fixture directory


def test_charts_of_a_fixture_run_have_its_series_and_classes(tmp_path):
    results = report.build([make_run(tmp_path / "run")])
    root = parse(charts.request_size_svg(results))
    lines = {p.get("data-series"): p for p in root.iter(f"{SVG}polyline")}
    assert list(lines) == list(ARMS)
    for arm in ARMS:
        assert len(lines[arm].get("points").split()) == len(results["request_series"][arm]["points"])
    assert texts(root, data_bound_label=str(SMALL_BOUND)) == [f"bound: {SMALL_BOUND:,} tokens"]
    for note in results["notes"]:
        assert note.split(":")[0][:40] in all_text(root)
    assert MODEL in all_text(root) and "seed 11" in all_text(root)
    root = parse(charts.accuracy_svg(results))
    rows = [t.text for t in root.iter(f"{SVG}text") if t.get("data-row")]
    assert rows == ["All probes", "paged_only", "multihop", "chain", "obligations", "distractor", "paraphrase",
                    "concluded", "toolloop"]
    assert texts(root, data_n="All probes") == ["n = 216"]
    assert len([g for g in root.iter(f"{SVG}g") if g.get("data-arm")]) == 27


def test_the_same_results_give_the_same_bytes_with_no_ids_or_dates(tmp_path):
    results = report.build([make_run(tmp_path / "run")])
    for draw in (charts.request_size_svg, charts.accuracy_svg):
        svg = draw(results)
        assert svg == draw(json.loads(json.dumps(results)))
        assert " id=" not in svg and "2026-11" not in svg and svg.endswith("</svg>\n")
        assert all(ord(ch) < 128 for ch in svg)


def test_write_names_the_files_and_says_what_it_leaves_out(tmp_path):
    live = report.build([make_run(tmp_path / "run", repeats=1, seeds=(11,))])
    assert charts.planned(live) == [("accuracy.svg", "Accuracy by arm"),
                                    ("request-size.svg", "Request size against history length")]
    assert charts.skipped(live) == []
    paths = charts.write(live, tmp_path / "charts")
    assert [p.name for p in paths] == ["accuracy.svg", "request-size.svg"]
    assert paths[0].read_text(encoding="utf-8") == charts.accuracy_svg(live)
    replay = line_results(kind="replay")
    assert charts.planned(replay) == [("request-size.svg", "Request size against history length")]
    assert [name for name, _ in charts.skipped(replay)] == ["accuracy.svg"]
    with pytest.raises(charts.ChartError):
        charts.write({"kind": "replay", "request_series": {}}, tmp_path / "none")
    assert not (tmp_path / "none").exists()


def test_a_file_that_merges_models_gets_one_pair_of_charts_per_model(tmp_path):
    haiku = make_run(tmp_path / "haiku", repeats=1, seeds=(11,))
    opus = make_run(tmp_path / "opus", repeats=1, seeds=(11,), model="claude-opus-5-5")
    results = report.build([haiku, opus])
    assert [name for name, _ in charts.planned(results)] == [
        "accuracy-claude-haiku-5-5.svg", "request-size-claude-haiku-5-5.svg", "accuracy-claude-opus-5-5.svg",
        "request-size-claude-opus-5-5.svg"]
    assert charts.planned(results)[2][1] == "Accuracy by arm, claude-opus-5-5"
    paths = charts.write(results, tmp_path / "charts")
    assert "claude-opus-5-5, protocol v2" in all_text(parse(paths[2].read_text(encoding="utf-8")))


# contexto bench --chart


def test_chart_mode_writes_beside_the_results_file(tmp_path, capsys):
    out = make_run(tmp_path / "run", repeats=1, seeds=(11,))
    results = report.write(out, charts=False)
    assert not (out / "charts").exists()
    assert cli.main(["bench", "--chart", str(out / "results.json")]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [f"written: {out / 'charts' / 'accuracy.svg'}",
                                         f"written: {out / 'charts' / 'request-size.svg'}"]
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert (out / "charts" / "accuracy.svg").read_text(encoding="utf-8") == charts.accuracy_svg(results)
    assert (out / "charts" / "request-size.svg").read_text(encoding="utf-8") == charts.request_size_svg(results)


def test_chart_mode_writes_into_out_when_given(tmp_path, capsys):
    out = make_run(tmp_path / "run", repeats=1, seeds=(11,))
    report.write(out, charts=False)
    target = tmp_path / "site"
    assert cli.main(["bench", "--chart", str(out / "results.json"), "--out", str(target)]) == 0
    assert sorted(p.name for p in (target / "charts").iterdir()) == ["accuracy.svg", "request-size.svg"]
    assert not (out / "charts").exists()


def test_chart_mode_on_a_replay_file_says_there_is_no_accuracy_chart(tmp_path, capsys):
    path = tmp_path / "results.json"
    path.write_text(json.dumps(line_results(kind="replay")), encoding="utf-8")
    assert cli.main(["bench", "--chart", str(path)]) == 0
    out = capsys.readouterr().out
    assert f"written: {tmp_path / 'charts' / 'request-size.svg'}" in out
    assert "not written: accuracy.svg: the results file has no per-arm accuracy (a replay makes no model calls" in out
    assert [p.name for p in (tmp_path / "charts").iterdir()] == ["request-size.svg"]
    root = parse((tmp_path / "charts" / "request-size.svg").read_text(encoding="utf-8"))
    assert "synthetic session, no model" in all_text(root)


@pytest.mark.parametrize("content, words", [
    (None, "cannot read the file"),
    ("{", "not JSON"),
    ('{"format": 1}', "not a bench results file"),
    ('{"request_series": {}, "kind": "replay"}', "no request_series with points"),
])
def test_chart_mode_exits_1_for_a_file_it_cannot_draw(tmp_path, capsys, content, words):
    path = tmp_path / "results.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert cli.main(["bench", "--chart", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("contexto bench: error: ") and words in captured.err
    assert not (tmp_path / "charts").exists()
