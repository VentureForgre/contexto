"""Tests for the session-JSONL meter. Synthetic entries only."""

from __future__ import annotations

import json

from sessionlog import EXIT_STOP, check_turn, gate, main, read_session, status, summary_rows

SECRET_TEXT = "PRIVATE-PROMPT-TEXT"


def usage(i, cw, cr, out):
    return {"input_tokens": i, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr, "output_tokens": out,
            "service_tier": "standard", "cache_creation": {"ephemeral_5m_input_tokens": cw}}


def assistant(mid, u, blocks, stop=None):
    return {"type": "assistant", "timestamp": "t", "message": {"id": mid, "model": "claude-haiku-5-5",
            "stop_reason": stop, "usage": u, "content": blocks}}


def prompt(text=SECRET_TEXT):
    return {"type": "user", "timestamp": "t", "message": {"role": "user", "content": text}}


def tool_results(*ids):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": i, "content": SECRET_TEXT} for i in ids]}}


def read(i):
    return {"type": "tool_use", "id": i, "name": "Read", "input": {"file_path": SECRET_TEXT}}


def write_session(tmp_path, entries):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n")
    return path


ENTRIES = [
    {"type": "queue-operation", "content": SECRET_TEXT},
    prompt(),
    # One API response written as two entries (one per content block): counted once.
    assistant("m1", usage(2, 80000, 0, 50), [read("r1")]),
    assistant("m1", usage(2, 80000, 0, 60), [read("r2")], stop="tool_use"),
    tool_results("r1", "r2"),
    assistant("m2", usage(4, 3000, 80000, 10), [{"type": "text", "text": SECRET_TEXT}], stop="end_turn"),
    prompt(),
    # Serial reads: one Read per request.
    assistant("m3", usage(2, 100, 83000, 5), [read("r3")], stop="tool_use"),
    tool_results("r3"),
    assistant("m4", usage(2, 9000, 83100, 5), [read("r4")], stop="tool_use"),
    tool_results("r4"),
    assistant("m5", usage(2, 50, 92100, 5), [{"type": "text", "text": "DONE"}], stop="end_turn"),
]


def test_status_dedupes_calls_and_counts_turns(tmp_path):
    st = status(read_session(write_session(tmp_path, ENTRIES)))
    assert st["calls"] == 5 and st["turns"] == 2
    assert st["head_context_tokens"] == 80002
    assert st["per_turn"][1] == {"calls": 2, "tokens": 80062 + 83014, "max_context": 83004, "read_calls": 1, "reads": 2}
    assert st["per_turn"][2]["read_calls"] == 2 and st["per_turn"][2]["reads"] == 2
    assert st["last_stop_reason"] == "end_turn" and st["compactions"] == []
    assert SECRET_TEXT not in json.dumps(st)


def test_gate_and_turn_checks(tmp_path):
    st = status(read_session(write_session(tmp_path, ENTRIES)))
    assert gate(st, 100_000, 1_900_000, 1.5)[0] is True
    ok, line = gate(st, 1_200_000, 1_900_000, 1.5)
    assert not ok and line.startswith("STOP")
    assert check_turn(st, 1, 200_000, 1.5, reads=2)[0] is True  # two reads in one request: parallel
    ok, lines = check_turn(st, 2, 400_000, 1.5, reads=2)
    assert not ok and "serial reads" in lines[0]
    ok, lines = check_turn(st, 1, 100_000, 1.5, reads=None)
    assert not ok and "> 1.5 x budget" in lines[0]


def test_compaction_marker_stops(tmp_path):
    entries = ENTRIES + [{"type": "system", "subtype": "compact_boundary", "timestamp": "t"}]
    st = status(read_session(write_session(tmp_path, entries)))
    ok, lines = check_turn(st, 2, 10**9, 1.5, reads=None)
    assert not ok and "compaction" in lines[0]


def test_summary_rows_and_cli_exit_codes(tmp_path, capsys):
    path = write_session(tmp_path, ENTRIES)
    rows = summary_rows(read_session(path))
    assert rows[0]["request_bytes"] is None and rows[0]["endpoint"] == "session-jsonl" and rows[-1]["turn"] == 2
    assert main(["gate", str(path), "--next-turn-budget", "1000"]) == 0
    assert main(["gate", str(path), "--next-turn-budget", "2000000"]) == EXIT_STOP
    out = tmp_path / "rows.csv"
    assert main(["summary", str(path), "--csv", str(out)]) == 0
    assert SECRET_TEXT not in out.read_text() + capsys.readouterr().out
