"""Tests for the L2e replay runner, against a fake API (no network)."""

from __future__ import annotations

import pytest

from fidelity import l2d, replay


@pytest.fixture(scope="module")
def plan():
    return l2d.build()


def _fake(calls):
    def post(body):
        calls.append(body)
        n = len(calls)
        content = [{"type": "thinking", "thinking": "", "signature": f"sig{n}"}, {"type": "text", "text": "OK"}]
        return 200, {"content": content, "stop_reason": "end_turn",
                     "usage": {"input_tokens": 1_000, "output_tokens": 50}}
    return post


def test_sequence_forks_and_stripping(plan, tmp_path):
    calls = []
    runner = replay.Runner(_fake(calls), cap=10**9, estimates={})
    replay.run(plan, runner, tmp_path)
    labels = [r["label"] for r in runner.records]
    assert labels == ["RS", "RG", "RP0_preserved", "RP0_stripped", "RF", "RP1_preserved", "RP1_stripped"]
    sent = {r["label"]: r["thinking_blocks_sent"] for r in runner.records}
    assert sent["RP0_preserved"] == 2 and sent["RP0_stripped"] == 0
    assert sent["RF"] == 2  # probes never enter the shared history
    assert sent["RP1_preserved"] == 3 and sent["RP1_stripped"] == 0
    for body in calls:
        assert body["model"] == "claude-haiku-5-5" and body["thinking"] == {"type": "adaptive"}
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["user", "assistant"] * (len(roles) // 2) + ["user"]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(l2d.REPLAY_ANSWER_FILES)
    assert runner.spent == 7 * 1_050


def test_cap_stops_before_a_request_that_would_pass_it(plan, tmp_path):
    calls = []
    estimates = {r.turn: r.context for r in l2d.replay_requests(plan)}
    runner = replay.Runner(_fake(calls), cap=100_000, estimates=estimates)
    with pytest.raises(replay.CapExceeded):
        replay.run(plan, runner, tmp_path)
    assert runner.spent <= 100_000
    assert "RF" not in [r["label"] for r in runner.records]


def test_http_error_is_recorded_and_stops(plan, tmp_path):
    def post(body):
        return 400, {"error": {"type": "invalid_request_error", "message": "bad"}}
    runner = replay.Runner(post, cap=10**9, estimates={})
    with pytest.raises(replay.RequestFailed):
        replay.run(plan, runner, tmp_path)
    assert runner.records[0]["status"] == 400 and runner.records[0]["error_type"] == "invalid_request_error"


def test_continue_stripped_p1_rebuilds_history_without_thinking(plan, tmp_path):
    import json
    calls = []
    runner = replay.Runner(_fake(calls), cap=10**9, estimates={})
    (tmp_path / "answers").mkdir()
    replay.run(plan, runner, tmp_path / "answers")
    records = [r for r in runner.records if r["label"] != "RP1_stripped"]
    for r in records:
        r["text_chars"] = len(replay.EXPECTED_REPLIES.get(r["label"], "OK"))
    (tmp_path / "summary.json").write_text(json.dumps({"records": records, "total_tokens": 6 * 1_050}))
    sent = []
    summary = replay.continue_stripped_p1(plan, tmp_path, _fake(sent), cap=10**9)
    messages = sent[0]["messages"]
    assert all(b["type"] == "text" for m in messages for b in m["content"])
    assert [m["content"][0]["text"] for m in messages if m["role"] == "assistant"] == ["OK", "DONE", "OK"]
    stripped_from_run = replay.strip_thinking(calls[-1]["messages"])
    assert [m["role"] for m in messages] == [m["role"] for m in stripped_from_run]
    assert summary["total_tokens"] == 7 * 1_050 and summary["records"][-1]["reconstructed_history"]


def test_continue_refuses_when_a_reply_differs(plan, tmp_path):
    import json
    records = [{"label": label, "text_chars": 2, "blocks": ["thinking", "text"], "total_tokens": 1, "usage": {}}
               for label in ("RS", "RG", "RP0_preserved", "RP0_stripped", "RF", "RP1_preserved")]
    (tmp_path / "summary.json").write_text(json.dumps({"records": records, "total_tokens": 6}))
    with pytest.raises(SystemExit):
        replay.continue_stripped_p1(plan, tmp_path, _fake([]), cap=10**9)
