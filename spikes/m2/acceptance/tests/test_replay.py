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


def _records(probe: str = PROBE) -> list[dict[str, Any]]:
    """Claude Code 2.1.301's shape: one assistant record per content block under one message id, and
    each api-request a delta (base request, messages kept, hashes of new messages)."""
    H = replay.message_hash
    session_start = {"role": "user", "content": [{"type": "text", "text": "<system-reminder>ctx</system-reminder>"},
                                                 {"type": "text", "text": "turn 1"}]}
    sys_msg = {"role": "system", "content": [{"type": "text", "text": "session start"}]}
    reminder = {"role": "system", "content": [{"type": "text", "text": "reminder"}]}
    a1 = {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "s"},
                                           {"type": "text", "text": "OK"}]}
    u2 = {"role": "user", "content": [{"type": "text", "text": "turn 2"}]}
    a2 = {"role": "assistant", "content": [{"type": "text", "text": "OK."}]}
    u3 = {"role": "user", "content": [{"type": "text", "text": probe}]}
    shape = {"system": [{"type": "text", "text": "sys"}],
             "tools": [{"name": "Read", "input_schema": {}}, {"name": "mcp__contexto__contexto_recall", "input_schema": {}}]}
    params = {"model": "claude-haiku-5-5", "betas": ["b1", "claude-code-20250219"], "max_tokens": 128000,
              "thinking": {"type": "adaptive"},
              "context_management": {"edits": []}, "output_config": {"effort": "medium"}}
    return [
        {"type": "api-request-shape", "shapeHash": "sh", "shape": shape},
        {"type": "api-request-blob", "hash": H(session_start), "message": session_start},
        {"type": "api-request-blob", "hash": H(sys_msg), "message": sys_msg},
        {"type": "api-request-blob", "hash": H(reminder), "message": reminder},
        {"type": "api-request", "id": "r1", "shapeHash": "sh", "params": params, "keep": 0,
         "tail": [H(session_start), H(sys_msg)]},
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": a1["content"][:1]}},
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": a1["content"][1:]}},
        {"type": "user", "message": u2},
        {"type": "api-request", "id": "r2", "shapeHash": "sh", "params": params, "base": "r1", "keep": 2,
         "tail": [H(a1), H(u2), H(reminder)]},
        {"type": "assistant", "message": {"id": "m2", "role": "assistant", "content": a2["content"]}},
        {"type": "user", "message": u3},
        {"type": "api-request", "id": "r3", "shapeHash": "sh", "params": params, "base": "r2", "keep": 5,
         "tail": [H(a2), H(u3), H(reminder)]},
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "side"}},
    ]


def test_build_request_is_the_recorded_probe_request_without_thinking():
    req = replay.build_request(_records(), PROBE, 32_000)
    assert [m["role"] for m in req["messages"]] == ["user", "system", "assistant", "user", "system", "assistant",
                                                    "user", "system"]
    assert req["messages"][1]["content"][0]["text"] == "session start"  # what H1 actually sent
    assert req["messages"][2]["content"] == [{"type": "text", "text": "OK"}]  # merged, then thinking stripped
    assert req["messages"][5]["content"] == [{"type": "text", "text": "OK."}]  # nothing synthesized
    assert req["messages"][6]["content"] == [{"type": "text", "text": PROBE}]
    assert [t["name"] for t in req["tools"]] == ["Read"] and req["tool_choice"] == {"type": "none"}
    assert "context_management" not in req and req["thinking"] == {"type": "adaptive"}
    assert req["max_tokens"] == 32_000 and req["stream"] is True and req["_betas"] == ["b1"]
    assert replay.summary(req)["messages"] == 8


def test_build_request_refuses_an_incomplete_jsonl():
    recs = _records()
    with pytest.raises(replay.ReplayError, match="api-request-shape"):
        replay.build_request([r for r in recs if r["type"] != "api-request-shape"], PROBE, 1_000)
    with pytest.raises(replay.ReplayError, match="probe turn"):
        replay.build_request(_records(probe="something else"), PROBE, 1_000)
    with pytest.raises(replay.ReplayError, match="does not hold"):  # an unresolvable hash is not guessed
        replay.build_request([r for r in recs if r.get("message", {}).get("id") != "m2"], PROBE, 1_000)
    thinking_only = [r for r in recs if not (r["type"] == "assistant" and r["message"]["content"][0]["type"] == "text"
                                             and r["message"]["id"] == "m1")]
    with pytest.raises(replay.ReplayError):
        replay.build_request(thinking_only, PROBE, 1_000)


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
                           "bearer": self.headers.get("authorization"),
                           "beta": self.headers.get("anthropic-beta"), "ua": self.headers.get("user-agent")})
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
    jsonl.write_text("\n".join(json.dumps(r) for r in _records(probe=s.probe_texts[0])) + "\n")
    seat = tmp_path / "seat.json"
    seat.write_text(json.dumps({"apiKeyHelper": "echo test-key-not-real"}))
    return jsonl, seat


def _assert_key_absent(tmp_path: Path, capsys) -> None:
    """The key never reaches stdout, stderr, or any file the replay writes (only the seat settings
    file, which names the helper command, is excluded)."""
    captured = capsys.readouterr()
    assert "test-key-not-real" not in captured.out + captured.err
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "seat.json":
            assert "test-key-not-real" not in path.read_text(errors="replace"), path.name


def test_replay_counts_then_sends_once_with_the_helper_key(tmp_path, stub, capsys):
    jsonl, seat = _setup(tmp_path)
    out = tmp_path / "out"
    code = run.main(["replay", "--cc-jsonl", str(jsonl), "--out", str(out), "--approved", "477681",
                     "--seat-settings", str(seat), "--upstream", stub])
    assert code == 0
    assert [x["path"] for x in _Stub.seen] == ["/v1/messages/count_tokens", "/v1/messages"] * 6  # D27: 6 turns
    assert all(x["key"] == "test-key-not-real" and x["beta"] == "b1" for x in _Stub.seen)  # claude-code-* dropped
    assert all(x["bearer"] == "Bearer test-key-not-real" for x in _Stub.seen)  # the approved header set
    assert all(x["ua"] == replay.USER_AGENT and "claude" not in x["ua"].lower() for x in _Stub.seen)
    assert "max_tokens" not in _Stub.seen[0]["keys"] and "stream" in _Stub.seen[1]["keys"]
    result = json.loads((out / "replay-result.json").read_text())
    assert [t["count_tokens"] for t in result["turns"]] == [1_000] * 6 and result["spent"] == 6 * 1_003
    assert (out / "baseline-answers.txt").read_text() == "q_1: x\n" * 6
    assert result["completed"] == 6 and result["answers_file"] == "baseline-answers.txt"
    assert not (out / "baseline-answers.partial.txt").exists()
    _assert_key_absent(tmp_path, capsys)


def test_replay_stops_without_sending_when_the_count_does_not_fit(tmp_path, stub):
    jsonl, seat = _setup(tmp_path)
    _Stub.count = 990_000
    code = run.main(["replay", "--cc-jsonl", str(jsonl), "--out", str(tmp_path / "o"), "--approved", "5000000",
                     "--seat-settings", str(seat), "--upstream", stub])
    assert code == 1 and [x["path"] for x in _Stub.seen] == ["/v1/messages/count_tokens"]
    result = json.loads((tmp_path / "o" / "replay-result.json").read_text())
    assert "context window" in result["stopped"] and result["completed"] == 0
    # A stopped replay never writes the full-run name (reviewer-2 on PR #123)
    assert result["answers_file"] == "baseline-answers.partial.txt"
    assert (tmp_path / "o" / "baseline-answers.partial.txt").exists()
    assert not (tmp_path / "o" / "baseline-answers.txt").exists()


def test_replay_stops_on_the_first_401(tmp_path, stub, capsys):
    jsonl, seat = _setup(tmp_path)
    _Stub.status = 401
    code = run.main(["replay", "--cc-jsonl", str(jsonl), "--out", str(tmp_path / "o"), "--approved", "477681",
                     "--seat-settings", str(seat), "--upstream", stub])
    assert code == 1 and len(_Stub.seen) == 1
    assert "401" in json.loads((tmp_path / "o" / "replay-result.json").read_text())["stopped"]
    _assert_key_absent(tmp_path, capsys)


def test_replay_dry_makes_no_calls(tmp_path, stub, capsys):
    jsonl, _ = _setup(tmp_path)
    assert run.main(["replay", "--cc-jsonl", str(jsonl), "--out", str(tmp_path / "o"), "--dry"]) == 0
    assert _Stub.seen == [] and json.loads(capsys.readouterr().out)["first_request"]["messages"] == 8
    assert driver.MODEL == "claude-haiku-5-5"


def test_parse_sse_collects_tool_calls_with_their_input():
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1",
                                                                      "name": "mcp__contexto__contexto_search", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"query": "chec'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'kout-api"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}},
    ]
    out = replay.parse_sse([f"data: {json.dumps(e)}" for e in events])
    assert out["tool_uses"] == [{"name": "mcp__contexto__contexto_search", "input": {"query": "checkout-api"}}]
    assert out["stop_reason"] == "tool_use" and out["text"] == ""


def test_auth_headers_carry_the_key_only_in_credential_headers():
    # The approved default (intake 03:24Z): the key in x-api-key and Authorization: Bearer.
    assert replay.AUTH_HEADER == "both"
    assert replay.auth_headers("k") == {"x-api-key": "k", "authorization": "Bearer k"}
    assert replay.auth_headers("k", "x-api-key") == {"x-api-key": "k"}
    assert replay.auth_headers("k", "bearer") == {"authorization": "Bearer k"}


def test_no_claude_code_beta_reaches_a_direct_request():
    # Intake (2026-10-16): no Claude Code client marker on any direct harness send; other betas stay.
    assert replay.harness_betas(["b1", "claude-code-20250219", "claude-code-x", 3]) == ["b1"]
    req = replay.build_request(_records(), PROBE, 1_000)
    assert req["_betas"] == ["b1"]
    assert replay.beta_headers({"_betas": ["claude-code-20250219"]}) == {}
    assert replay.beta_headers({"_betas": ["claude-code-20250219", "b2"]}) == {"anthropic-beta": "b2"}


def test_decoding_stops_at_the_probe_request_so_a_later_unresolvable_record_is_harmless():
    # Live H1 rerun (2026-10-16): the probe turn's own tool-loop requests named assistant messages
    # the JSONL did not hold; the baseline only needs the first request holding the probe.
    later = {"type": "api-request", "id": "r4", "shapeHash": "sh", "params": _records()[4]["params"], "base": "r3",
             "keep": 8, "tail": ["not-a-known-hash"]}
    req = replay.build_request(_records() + [later], PROBE, 32_000)
    assert req["messages"][-2]["content"] == [{"type": "text", "text": PROBE}]
    early = [r for r in _records() if r.get("id") != "r3"] + [later]  # no probe before the broken record
    with pytest.raises(replay.ReplayError):
        replay.build_request(early, PROBE, 32_000)


def test_a_display_count_tokens_rejects_becomes_omitted_and_is_labeled():
    recs = _records()
    for r in recs:
        if r.get("type") == "api-request":
            r["params"]["thinking"] = {"type": "adaptive", "display": "updates"}
    req = replay.build_request(recs, PROBE, 32_000)
    assert req["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert req["_changes"] == {"thinking.display": ["updates", "omitted"]}
    assert replay.build_request(_records(), PROBE, 32_000)["_changes"] == {}  # nothing to change


def test_baseline_request_k_carries_only_the_baselines_own_earlier_answers():
    # Intake D27 (05:42Z): request k = history + probe turns 1..k-1 with the BASELINE's answers + turn k.
    probes = [PROBE, "second probe turn", "third probe turn"]
    h1_answer = {"type": "assistant", "message": {"id": "h1", "role": "assistant",
                                                  "content": [{"type": "text", "text": "H1 ANSWER"}]}}
    parts = replay.turn_requests(_records() + [h1_answer], probes, 32_000)
    assert [m["role"] for m in parts["history"]] == ["user", "system", "assistant", "user", "system", "assistant"]
    assert [m["role"] for m in parts["tail"]] == ["system"]  # the per-turn reminder after the probe
    r1 = replay.baseline_request(parts, [], 1)
    r3 = replay.baseline_request(parts, ["B1", "B2"], 3)
    tail = [(m["role"], m["content"][0]["text"]) for m in r3["messages"][len(parts["history"]):]]
    assert tail == [("user", PROBE), ("system", "reminder"), ("assistant", "B1"),
                    ("user", "second probe turn"), ("system", "reminder"), ("assistant", "B2"),
                    ("user", "third probe turn"), ("system", "reminder")]
    assert "H1 ANSWER" not in json.dumps(r3) and "H1 ANSWER" not in json.dumps(r1)
    assert r1["messages"] == parts["history"] + [{"role": "user", "content": [{"type": "text", "text": PROBE}]},
                                                 *parts["tail"]]
    with pytest.raises(replay.ReplayError):
        replay.baseline_request(parts, ["B1"], 3)  # turn 3 needs both earlier answers


def test_the_baseline_uses_the_same_assignment_as_h1():
    s, _ = run.plan()
    jsonl_probe = s.probe_texts[0]
    assert all(any(f"{i}:" in text for i in ids) for ids, text in zip(s.probe_turn_ids, s.probe_texts))
    assert jsonl_probe == s.probe_texts[0]  # the replay keys its first request on H1's first probe turn


def test_the_replay_approval_is_the_combined_approval_minus_the_recorded_h1_spend(tmp_path, stub):
    # Reviewer-2 on PR #112: tie the replay's --approved to the live run's own record.
    jsonl, seat = _setup(tmp_path)
    record = tmp_path / "acceptance-x-h1.json"
    record.write_text(json.dumps({"spent": 4_990_000, "session": "x"}))
    code = run.main(["replay", "--cc-jsonl", str(jsonl), "--out", str(tmp_path / "o"), "--approved", "5000000",
                     "--h1-record", str(record), "--seat-settings", str(seat), "--upstream", stub])
    result = json.loads((tmp_path / "o" / "replay-result.json").read_text())
    assert result["approval"] == {"combined": 5_000_000, "h1_spent": 4_990_000, "replay": 10_000}
    assert result["h1_session"] == "x"  # score ties this baseline to that H1 run
    assert code == 1 and "approved 10,000" in result["stopped"]  # 1,000 + 32,000 output does not fit 10,000
    assert [x["path"] for x in _Stub.seen] == ["/v1/messages/count_tokens"]
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert run.main(["replay", "--cc-jsonl", str(jsonl), "--out", str(tmp_path / "o2"), "--approved", "1",
                     "--h1-record", str(bad), "--seat-settings", str(seat), "--upstream", stub]) == 2
