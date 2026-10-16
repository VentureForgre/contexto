"""The full-context replay baseline: request building, the window check, and a stub round trip.

No model calls: the upstream is a local stub, and the key is a fake helper's output.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import driver, replay, run  # noqa: E402

PROBE = "Final step. Answer the questions below."


def _records(probe: str = PROBE, extra_after_probe: bool = True) -> list[dict[str, Any]]:
    shape = {"system": [{"type": "text", "text": "sys"}],
             "tools": [{"name": "Read", "input_schema": {}}, {"name": "mcp__contexto__contexto_recall", "input_schema": {}}]}
    recs: list[dict[str, Any]] = [
        {"type": "api-request-shape", "shape": shape},
        {"type": "api-request", "params": {"model": "claude-haiku-5-5", "betas": ["b1"], "max_tokens": 128000,
                                           "thinking": {"type": "adaptive"}, "context_management": {"edits": []},
                                           "output_config": {"effort": "medium"}}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "turn 1"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "t"},
                                                                          {"type": "text", "text": "OK"}]}},
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "side"}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "turn 2"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "x"}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": probe}]}},
    ]
    if extra_after_probe:  # the probe turn's own tool loop is not part of the baseline
        recs += [{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]}},
                 {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]}}]
    return recs


def test_build_request_keeps_history_strips_thinking_and_contexto_tools():
    req = replay.build_request(_records(), PROBE, 32_000)
    assert [m["role"] for m in req["messages"]] == ["user", "assistant", "user", "assistant", "user"]
    blocks = [b for m in req["messages"] for b in m["content"]]
    assert not any(b["type"] in ("thinking", "redacted_thinking", "tool_use", "tool_result") for b in blocks)
    assert req["messages"][3]["content"] == [{"type": "text", "text": "OK"}]  # emptied by stripping
    assert req["messages"][-1]["content"] == [{"type": "text", "text": PROBE}]
    assert [t["name"] for t in req["tools"]] == ["Read"] and req["tool_choice"] == {"type": "none"}
    assert "context_management" not in req and req["thinking"] == {"type": "adaptive"}
    assert req["max_tokens"] == 32_000 and req["stream"] is True and req["_betas"] == ["b1"]
    assert replay.summary(req)["messages"] == 5


def test_build_request_refuses_an_incomplete_jsonl():
    with pytest.raises(replay.ReplayError, match="api-request-shape"):
        replay.build_request(_records()[2:], PROBE, 1_000)
    with pytest.raises(replay.ReplayError, match="probe turn"):
        replay.build_request(_records(probe="something else"), PROBE, 1_000)


def test_check_fits_stops_rather_than_trimming():
    assert replay.check_fits(900_000, 32_000, 1_000_000, 1_000_000) is None
    assert "context window" in replay.check_fits(990_000, 32_000, 1_000_000, 2_000_000)
    assert "approved" in replay.check_fits(450_000, 32_000, 1_000_000, 477_000)


def test_parse_sse_collects_text_usage_and_stop_reason():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "q_1: a"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
    ]
    out = replay.parse_sse([f"data: {json.dumps(e)}".encode() for e in events])
    assert out["text"] == "q_1: a" and out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 10, "cache_read_input_tokens": 0, "output_tokens": 7}
    assert out["blocks"] == {"thinking": 1, "text": 1}


class _Stub(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []
    status = 200
    count = 1_000

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        _Stub.seen.append({"path": self.path, "key": self.headers.get("x-api-key"), "keys": sorted(body),
                           "beta": self.headers.get("anthropic-beta")})
        if _Stub.status != 200:
            data = json.dumps({"type": "error", "error": {"type": "authentication_error"}}).encode()
            self.send_response(_Stub.status)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.endswith("/count_tokens"):
            data = json.dumps({"input_tokens": _Stub.count}).encode()
            ctype = "application/json"
        else:
            events = [{"type": "message_start", "message": {"usage": {"input_tokens": _Stub.count}}},
                      {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                      {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "q_1: x"}},
                      {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}]
            data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            ctype = "text/event-stream"
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture()
def stub():
    _Stub.seen, _Stub.status, _Stub.count = [], 200, 1_000
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    s, _ = run.plan()
    jsonl = tmp_path / "cc.jsonl"
    jsonl.write_text("\n".join(json.dumps(r) for r in _records(probe=s.turns[-1])) + "\n")
    seat = tmp_path / "seat.json"
    seat.write_text(json.dumps({"apiKeyHelper": "echo test-key-not-real"}))
    return jsonl, seat


def test_replay_counts_then_sends_once_with_the_helper_key(tmp_path, stub, capsys):
    jsonl, seat = _setup(tmp_path)
    out = tmp_path / "out"
    code = run.main(["replay", "--cc-jsonl", str(jsonl), "--out", str(out), "--approved", "477681",
                     "--seat-settings", str(seat), "--upstream", stub])
    assert code == 0
    assert [x["path"] for x in _Stub.seen] == ["/v1/messages/count_tokens", "/v1/messages"]
    assert all(x["key"] == "test-key-not-real" and x["beta"] == "b1" for x in _Stub.seen)
    assert "max_tokens" not in _Stub.seen[0]["keys"] and "stream" in _Stub.seen[1]["keys"]
    result = json.loads((out / "replay-result.json").read_text())
    assert result["count_tokens"] == 1_000 and result["usage"]["output_tokens"] == 3
    assert (out / "baseline-answers.txt").read_text() == "q_1: x\n"
    assert "test-key-not-real" not in (out / "replay-result.json").read_text() + capsys.readouterr().out


def test_replay_stops_without_sending_when_the_count_does_not_fit(tmp_path, stub):
    jsonl, seat = _setup(tmp_path)
    _Stub.count = 990_000
    code = run.main(["replay", "--cc-jsonl", str(jsonl), "--out", str(tmp_path / "o"), "--approved", "5000000",
                     "--seat-settings", str(seat), "--upstream", stub])
    assert code == 1 and [x["path"] for x in _Stub.seen] == ["/v1/messages/count_tokens"]
    assert "context window" in json.loads((tmp_path / "o" / "replay-result.json").read_text())["stopped"]


def test_replay_stops_on_the_first_401(tmp_path, stub):
    jsonl, seat = _setup(tmp_path)
    _Stub.status = 401
    code = run.main(["replay", "--cc-jsonl", str(jsonl), "--out", str(tmp_path / "o"), "--approved", "477681",
                     "--seat-settings", str(seat), "--upstream", stub])
    assert code == 1 and len(_Stub.seen) == 1
    assert "401" in json.loads((tmp_path / "o" / "replay-result.json").read_text())["stopped"]


def test_replay_dry_makes_no_calls(tmp_path, stub, capsys):
    jsonl, _ = _setup(tmp_path)
    assert run.main(["replay", "--cc-jsonl", str(jsonl), "--out", str(tmp_path / "o"), "--dry"]) == 0
    assert _Stub.seen == [] and json.loads(capsys.readouterr().out)["request"]["messages"] == 5
    assert driver.MODEL == "claude-haiku-5-5"
