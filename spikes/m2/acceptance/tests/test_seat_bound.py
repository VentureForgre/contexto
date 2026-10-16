"""M2 A32: seat_bound on short synthetic seat sessions (zero spend; the real engine, a temporary store)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import seat_bound as sb  # noqa: E402
from contexto.rewrite import Config  # noqa: E402


def test_the_synthetic_session_has_the_measured_head_and_one_turn_start_per_turn():
    reqs = sb.session(84_000, 7)
    head = sb.tokens_of({"tools": reqs[0]["body"]["tools"], "system": reqs[0]["body"]["system"]})
    assert abs(head - 84_000) < 50
    assert sum(r["start"] for r in reqs) == 7 and [r["turn"] for r in reqs if r["start"]] == list(range(1, 8))
    # Each request holds everything before it: the history only grows.
    sizes = [sb.tokens_of(r["body"]) for r in reqs]
    assert sizes == sorted(sizes) and sizes[-1] - sizes[0] > 20_000


def test_a_large_read_turn_carries_the_measured_growth():
    reqs = sb.session(84_000, 4, big_read_at=2)
    growth = [sb.tokens_of(b["body"]) - sb.tokens_of(a["body"]) for a, b in zip(reqs, reqs[1:])]
    assert max(growth) >= sb.BIG_READ


def test_the_engine_rotates_for_room_and_keeps_every_turn_start_above_min_turn_room():
    config = Config(bound=150_000)
    reqs = sb.session(84_000, 14)
    rows = sb.replay(reqs, config)
    s = sb.summarize(rows, config)
    assert s["rotations"] >= 1 and set(s["reasons"]) <= {"min_turn_room", "bound"}
    assert s["turn_start_room"]["min"] >= config.min_turn_room and s["window_minimal"] == 0
    # A rotation reads the head alone from cache (A28's checkpoint model); the next request reads it whole.
    head = sb.tokens_of({"tools": reqs[0]["body"]["tools"], "system": reqs[0]["body"]["system"]})
    rotated = [i for i, r in enumerate(rows) if r["rotation"]]
    assert all(rows[i]["cached"] == head for i in rotated)
    assert all(rows[i + 1]["cached"] == rows[i]["tokens"] for i in rotated if i + 1 < len(rows))
    assert s["window_budget"][0] == 150_000 - config.headroom_floor - s["fixed_overhead"][1]


def test_rule_16_resume_matches_no_restart_and_no_resume_rotates_again():
    config = Config(bound=150_000)
    reqs = sb.session(84_000, 21)
    starts = [i for i, r in enumerate(reqs) if r["start"]]
    restarts = {starts[10]}
    plain = sb.replay(reqs, config)
    resumed = sb.replay(reqs, config, restarts, resume=True)
    fresh = sb.replay(reqs, config, restarts, resume=False)
    assert [r["tokens"] for r in resumed] == [r["tokens"] for r in plain]
    assert fresh[starts[10]]["rotation"] is not None and plain[starts[10]]["rotation"] is None


def test_one_large_read_keeps_the_window_budget_negative_afterwards():
    config = Config(bound=150_000)
    s = sb.summarize(sb.replay(sb.session(84_000, 8, big_read_at=3), config), config)
    assert s["window_budget"][0] < 0 and s["window_minimal"] >= 3 and s["bound_too_small"] >= 3
    assert s["margin_to_idle_compaction"] < 0


def test_main_writes_numbers_only(tmp_path, capsys):
    out_json, out_csv = tmp_path / "r.json", tmp_path / "r.csv"
    assert sb.main(["--turns", "7", "--restart-every", "3", "--json", str(out_json), "--csv", str(out_csv)]) == 0
    result = json.loads(out_json.read_text())
    assert result["turns"] == 7 and "head 92,000 bound 170,000 restarts no-resume" in result["scenarios"]
    lines = out_csv.read_text().splitlines()
    assert lines[0] == ",".join(sb.CSV_COLUMNS) and len(lines) > 1
    capsys.readouterr()
