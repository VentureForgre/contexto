"""Zero-spend check that the detectors read the session log the real proxy and engine write.

The real ContextoProxy runs the real RewriteEngine over a real SessionStore and forwards to a local
stub upstream that answers every request with a small JSON message and fake usage; nothing leaves
the machine. A simulated CLI sends a growing conversation with a small bound, so the engine rotates
and pages. The detectors then run on the actual log.jsonl and pages/ the session wrote.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

proxy_mod = pytest.importorskip("contexto.proxy")
rewrite = pytest.importorskip("contexto.rewrite")
from acceptance import detectors  # noqa: E402

from contexto.store import SessionStore  # noqa: E402

PLANT = "cfg billing-core.retention_days=amber-1234"


class _Upstream(BaseHTTPRequestHandler):
    """Answers like the Messages API: JSON, or SSE when the request asks to stream."""

    calls = 0

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        stream = bool(json.loads(body or b"{}").get("stream"))
        _Upstream.calls += 1
        tokens = len(body) // 4
        cached = tokens // 2 if _Upstream.calls > 1 else 0
        usage = {"input_tokens": tokens - cached, "cache_creation_input_tokens": 0, "cache_read_input_tokens": cached}
        message = {"id": f"msg_{_Upstream.calls}", "type": "message", "role": "assistant", "model": "claude-haiku-5-5"}
        if not stream:
            reply = {**message, "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
                     "usage": {**usage, "output_tokens": 2}}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        events = [
            ("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None,
                                                                   "usage": {**usage, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        data = "".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


def _sse_text(raw: bytes) -> list[dict]:
    """The content blocks a client would assemble from an SSE response (text deltas only)."""
    text = ""
    for line in raw.decode().splitlines():
        if line.startswith("data: "):
            event = json.loads(line[6:])
            if event.get("type") == "content_block_delta":
                text += event["delta"].get("text", "")
    return [{"type": "text", "text": text}]


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_detectors_on_a_real_proxy_and_engine_session(tmp_path, stream):
    _Upstream.calls = 0
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    store = SessionStore(tmp_path, "engine-log")
    engine = rewrite.RewriteEngine(store=store, config=rewrite.Config(bound=7_000, headroom_floor=1_000))
    proxy = proxy_mod.ContextoProxy(f"http://127.0.0.1:{upstream.server_port}", store, engine,
                                    observe_usage=engine.observe_usage)
    port = proxy.start()
    try:
        history: list[dict] = []
        filler = "\n".join(f"line {i:04d} latency_ms={i * 7 % 900} shard={i % 64} ok" for i in range(90))
        for turn in range(12):
            text = (PLANT + "\n" if turn == 0 else "") + f"Turn {turn} log:\n{filler}\nReply with exactly: OK"
            history.append({"role": "user", "content": [{"type": "text", "text": text}]})
            body = json.dumps({"model": "claude-haiku-5-5", "max_tokens": 64, "system": "You are a test.",
                               "tools": [], "messages": history, "stream": stream}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=body, method="POST",
                                         headers={"content-type": "application/json", "x-api-key": "test-key-not-real"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            content = _sse_text(raw) if stream else json.loads(raw)["content"]
            assert content == [{"type": "text", "text": "OK"}]  # the relay delivered the reply intact
            history.append({"role": "assistant", "content": content})
    finally:
        proxy.stop()
        upstream.shutdown()

    session_dir = tmp_path / "sessions" / "engine-log"
    log = detectors.read_jsonl(session_dir / "log.jsonl")
    report = detectors.analyze(log, [], bound=7_000)
    assert report.requests == 12 and report.upstream_4xx == 0
    assert report.unverified_requests == []  # usage was read from the JSON body or the SSE events
    assert report.rotations >= 1 and report.engine_events.get("rotation", 0) == report.rotations
    assert report.epochs >= 2 and sum(report.requests_per_epoch.values()) == 12
    assert report.cache_eligible >= 1  # later requests inside an epoch
    assert "page_out" in report.engine_events  # the engine's own paging event
    assert detectors.paged_statements(session_dir, [PLANT]) == {PLANT: True}  # turn 0 was really paged
    first_requests = [e for e in log if e.get("event") == "request" and e.get("path") == "/v1/messages"]
    assert all({"status", "usage", "epoch", "transform_events"} <= set(e) for e in first_requests)
