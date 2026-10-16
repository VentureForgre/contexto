"""M2 A27: codex_usage_vs_plain on synthetic, content-free session logs shaped like the proxy's
Responses entries (byte sizes, usage, stream_hold, rotation events). Zero spend."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import codex_usage_vs_plain as cu  # noqa: E402


def req(orig: int, sent: int, inp: int, cached: int = 0, out: int = 5, held: int = 0, epoch: int = 1,
        rotated: bool = False) -> dict:
    events = [{"event": "rotation", "reason": "bound"}] if rotated else []
    return {"event": "request", "method": "POST", "path": "/responses", "status": 200, "epoch": epoch,
            "original_request_bytes": orig, "request_bytes": sent, "transform_events": events,
            "stream_hold": {"held_events": held},
            "usage": {"input_tokens": inp, "input_tokens_details": {"cached_tokens": cached}, "output_tokens": out}}


# Two content turns passed through (3 bytes per token), one content turn rotated (2,000 bytes of history
# paged, net of the preamble), then a probe turn with one tool round (400 bytes, 100 tokens: 4 per token)
# and a second probe turn whose first request has the first probe turn's tool loop paged.
LOG = [
    req(3_000, 3_000, 1_000, epoch=0),
    req(6_000, 6_000, 2_000, cached=1_000, epoch=0),
    req(9_000, 7_000, 2_300, cached=900, rotated=True),
    req(9_600, 7_600, 2_500, cached=900, held=3, rotated=True),
    req(10_000, 8_000, 2_600, cached=2_400),
    req(10_600, 7_700, 2_400, cached=900, rotated=True),
]


def test_rates_come_from_pass_through_and_append_only_requests():
    reqs = cu.requests(LOG)
    assert [r["turn"] for r in reqs] == [1, 2, 3, 4, 4, 5]
    assert cu.text_rates(reqs) == [3.0]
    assert cu.json_pairs(reqs) == [(5, 400, 100)]


def test_a_rewritten_epoch_0_request_or_a_non_append_only_pair_is_refused():
    with pytest.raises(ValueError, match="pass-through"):
        cu.text_rates(cu.requests([req(3_000, 2_900, 1_000, epoch=0)]))
    bad = [req(9_600, 7_600, 2_500, held=3), req(10_100, 8_000, 2_600)]
    with pytest.raises(ValueError, match="not append-only"):
        cu.json_pairs(cu.requests(bad))


def test_plain_converts_paged_text_and_paged_tool_json_at_their_own_rates():
    reqs = cu.requests(LOG)
    same = cu.plain_same_points(reqs, 3.0, 4.0)
    assert same[:2] == [1_000, 2_000]  # passed through: nothing paged
    assert same[2] == 2_300 + 2_000 / 3.0
    # Turn 5: 2,900 bytes removed, 400 of them turn 4's tool loop.
    assert same[5] == 2_400 + 2_500 / 3.0 + 400 / 4.0


def test_the_plain_run_shape_drops_earlier_tool_loops_and_keeps_one_request_per_turn():
    reqs = cu.requests(LOG)
    same = cu.plain_same_points(reqs, 3.0, 4.0)
    shape = cu.plain_run_shape(reqs, same, 4.0, n_content=3)
    assert [r["turn"] for r in shape] == [1, 2, 3, 4, 5]
    assert shape[3]["input"] == same[3] and shape[4]["input"] == 2_400 + 2_500 / 3.0
    assert shape[3]["output"] == 5


def test_best_cache_reads_the_previous_prompt_and_the_measured_hit_fraction():
    assert cu.best_cache([100.0, 150.0, 120.0]) == [(0.0, 100.0), (100.0, 50.0), (120.0, 0.0)]
    assert cu.measured_hit_fraction(cu.requests(LOG)) == 2_400 / 2_500


def test_the_long_context_step_and_the_write_rate_apply_per_request():
    assert cu.usd(0, 1_000_000, 0, 1_000_000) == pytest.approx(12.5 * 2)  # above 272K: input x2
    assert cu.usd(0, 100_000, 0, 100_000, write=False) == pytest.approx(1.0)
    assert cu.usd(100_000, 0, 1_000, 100_000) == pytest.approx(0.1 + 0.05)


def test_main_reads_a_run_dir_and_writes_numbers_only(tmp_path, capsys):
    session = tmp_path / "store" / "sessions" / cu.SESSION
    session.mkdir(parents=True)
    (session / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in LOG))
    assert cu.main([str(tmp_path), "--csv", str(tmp_path / "rows.csv")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["contexto"]["input"] == sum(e["usage"]["input_tokens"] for e in LOG)
    assert out["plain_same_points"]["requests"] == 6 and not out["plain_same_points"]["compaction_fires"]
    assert (tmp_path / "rows.csv").read_text().splitlines()[0] == "n,turn,held,orig,sent,input,cached,output,plain"
    assert cu.main([str(tmp_path / "missing")]) == 1
