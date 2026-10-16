"""Tests for the M0 measuring proxy. Synthetic fixtures only; no real captures or network."""

from __future__ import annotations

import copy
import gzip
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from extract import (
    SUMMARY_FIELDS,
    StreamDecoder,
    header_facts,
    UsageCollector,
    normalize_usage,
    request_metrics,
    summary_row,
)
from proxy import SpikeProxy

KEY = b"k" * 32
SECRET = "sk-test-SECRET-VALUE-0123456789"
CONTENT = "PRIVATE-PROMPT-TEXT"

ANTHROPIC_REQUEST = {
    "model": "claude-haiku-5-5",
    "stream": True,
    "system": [{"type": "text", "text": CONTENT}, {"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}}],
    "tools": [{"name": "Read", "input_schema": {}}, {"name": "Bash", "input_schema": {}, "cache_control": {"type": "ephemeral"}}],
    "thinking": {"type": "enabled", "budget_tokens": 1024},
    "messages": [
        {"role": "user", "content": CONTENT},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": CONTENT, "signature": "sig"},
                {"type": "text", "text": CONTENT},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": CONTENT, "cache_control": {"type": "ephemeral"}},
                {"type": "image", "source": {}},
            ],
        },
    ],
}

RESPONSES_REQUEST = {
    "model": "gpt-6-luna",
    "instructions": CONTENT,
    "tools": [{"type": "function", "name": "shell"}],
    "stream": True,
    "store": False,
    "prompt_cache_key": "abc",
    "reasoning": {"effort": "low"},
    "input": [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": CONTENT}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": CONTENT}]},
        {"type": "reasoning", "encrypted_content": CONTENT},
        {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": CONTENT},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": CONTENT}]},
    ],
}


def as_bytes(obj):
    return json.dumps(obj).encode()


def test_anthropic_request_metrics():
    m = request_metrics("/v1/messages?beta=true", as_bytes(ANTHROPIC_REQUEST), None, KEY)
    assert m["endpoint"] == "/v1/messages"
    assert m["api"] == "anthropic-messages"
    assert m["message_count"] == 3
    assert m["request_bytes"] == len(as_bytes(ANTHROPIC_REQUEST))
    assert m["block_classes"] == {
        "user": {"text": 1, "tool_result": 1, "image": 1},
        "assistant": {"thinking": 1, "text": 1, "tool_use": 1},
    }
    assert m["cache_control"] == ["system[1]", "tools[1]", "messages[2].content[0]"]
    assert m["tool_count"] == 2 and m["tools_bytes"] > 0 and m["system_bytes"] > 0
    assert m["thinking"] == "enabled"
    assert set(m["class_bytes"]) == {"text", "thinking", "tool_use", "tool_result", "image"}
    assert m["class_bytes"]["tool_result"] > len(CONTENT)
    assert m["first_message_bytes"] == len(json.dumps(ANTHROPIC_REQUEST["messages"][0], separators=(",", ":")))
    assert CONTENT not in json.dumps(m)


def test_responses_request_metrics():
    m = request_metrics("/responses", as_bytes(RESPONSES_REQUEST), None, KEY)
    assert m["api"] == "openai-responses"
    assert m["message_count"] == 6
    assert m["block_classes"] == {
        "developer": {"instructions": 1},
        "user": {"text": 1},
        "item": {"thinking": 1, "tool_use": 1, "tool_result": 1},
        "assistant": {"text": 1},
    }
    assert m["store"] is False and m["prompt_cache_key_present"] and not m["previous_response_id_present"]
    assert m["reasoning_effort"] == "low"
    assert m["class_bytes"]["tool_result"] > len(CONTENT) and m["first_message_bytes"] > 0
    # Codex 0.154 shape: tool definitions as an input item, which can carry a developer role.
    slim = {"model": "m", "input": [{"type": "additional_tools", "role": "developer", "tools": [{"name": "x" * 50}]},
                                    *RESPONSES_REQUEST["input"]]}
    s = request_metrics("/responses", as_bytes(slim), None, KEY)
    assert s["tools_bytes"] == len(json.dumps([slim["input"][0]], separators=(",", ":")))
    assert s["system_bytes"] == len(json.dumps([RESPONSES_REQUEST["input"][0]], separators=(",", ":")))
    assert s["system_bytes"] + s["tools_bytes"] < s["request_bytes"]
    assert CONTENT not in json.dumps(m)


def test_prefix_hmac_tracks_prefix_and_depends_on_key():
    a = request_metrics("/v1/messages", as_bytes(ANTHROPIC_REQUEST), None, KEY)
    more_messages = dict(ANTHROPIC_REQUEST, messages=ANTHROPIC_REQUEST["messages"][:1])
    b = request_metrics("/v1/messages", as_bytes(more_messages), None, KEY)
    changed_tools = dict(ANTHROPIC_REQUEST, tools=ANTHROPIC_REQUEST["tools"][:1])
    c = request_metrics("/v1/messages", as_bytes(changed_tools), None, KEY)
    other_key = request_metrics("/v1/messages", as_bytes(ANTHROPIC_REQUEST), None, b"x" * 32)
    assert a["prefix_hmac"] == b["prefix_hmac"]
    assert a["prefix_hmac"] != c["prefix_hmac"]
    assert a["prefix_hmac"] != other_key["prefix_hmac"]


def test_compressed_request_body_is_measured_decoded():
    raw = as_bytes(RESPONSES_REQUEST)
    m = request_metrics("/responses", gzip.compress(raw), "gzip", KEY)
    assert m["request_bytes"] == len(raw) and m["wire_bytes"] < len(raw)
    assert m["message_count"] == 6


def test_undecodable_body_reports_sizes_only():
    m = request_metrics("/responses", b"\x00\x01", "br", KEY)
    assert m["wire_bytes"] == 2 and "request_bytes" not in m and m["decode_error"]


def sse(*events):
    return b"".join(b"event: x\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)


def test_anthropic_sse_usage_merges_start_and_delta():
    stream = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_creation_input_tokens": 200,
                                                         "cache_read_input_tokens": 3000, "output_tokens": 1}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": CONTENT}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    )
    c = UsageCollector("text/event-stream; charset=utf-8")
    for i in range(0, len(stream), 7):  # split events across arbitrary chunk boundaries
        c.feed(stream[i : i + 7].replace(b"\n", b"\r\n"))
    c.finish()
    assert c.normalized() == {"input_tokens": 10, "cache_creation_input_tokens": 200,
                              "cache_read_input_tokens": 3000, "output_tokens": 42}
    assert c.stop_reason == "tool_use"
    assert c.events["message_start"] == 1


def test_responses_sse_usage_is_normalized():
    stream = sse(
        {"type": "response.created", "response": {}},
        {"type": "response.completed", "response": {"status": "completed", "usage": {
            "input_tokens": 5000, "input_tokens_details": {"cached_tokens": 4096},
            "output_tokens": 300, "output_tokens_details": {"reasoning_tokens": 120}}}},
    )
    c = UsageCollector("text/event-stream")
    c.feed(stream)
    c.finish()
    assert c.normalized() == {"input_tokens": 904, "cache_creation_input_tokens": None,
                              "cache_read_input_tokens": 4096, "output_tokens": 300,
                              "reasoning_output_tokens": 120, "raw_input_tokens": 5000}


def test_json_response_usage_and_count_tokens():
    c = UsageCollector("application/json")
    c.feed(json.dumps({"usage": {"input_tokens": 7, "output_tokens": 3}, "stop_reason": "end_turn"}).encode())
    c.finish()
    assert c.normalized()["input_tokens"] == 7 and c.stop_reason == "end_turn"
    counted = UsageCollector("application/json")
    counted.feed(b'{"input_tokens": 99}')
    counted.finish()
    assert counted.normalized() == {} and counted.events == {"count_tokens": 1}


def test_stream_decoder_gzip_in_pieces_and_unsupported():
    payload = sse({"type": "message_delta", "usage": {"output_tokens": 5}})
    blob = gzip.compress(payload)
    d = StreamDecoder("gzip")
    assert b"".join(d.decode(blob[i : i + 5]) for i in range(0, len(blob), 5)) == payload
    assert not StreamDecoder("br").supported


def test_anthropic_usage_with_output_details_stays_anthropic():
    usage = {"input_tokens": 4, "cache_creation_input_tokens": 166, "cache_read_input_tokens": 1495,
             "output_tokens": 76, "output_tokens_details": {"thinking_tokens": 0}}
    assert normalize_usage(usage) == {"input_tokens": 4, "cache_creation_input_tokens": 166,
                                      "cache_read_input_tokens": 1495, "output_tokens": 76}


def test_header_facts_never_include_secret_values():
    facts = header_facts([("Authorization", f"Bearer {SECRET}"), ("x-api-key", SECRET), ("Cookie", SECRET),
                          ("anthropic-beta", "a-1, b-2"), ("Content-Type", "application/json")])
    assert facts["auth_scheme"] == "x-api-key" or facts["auth_scheme"] == "bearer"
    assert facts["anthropic_beta"] == ["a-1", "b-2"]
    assert facts["header_names"] == ["anthropic-beta", "authorization", "content-type", "cookie", "x-api-key"]
    assert SECRET not in json.dumps(facts)


def test_normalize_usage_passes_anthropic_fields():
    assert normalize_usage({"input_tokens": 1, "output_tokens": 2})["cache_read_input_tokens"] is None


def test_summary_row_has_exactly_lane2_fields():
    row = summary_row({"turn": 3, "usage": {"input_tokens": 1}, "request_bytes": 9, "message_count": 2,
                       "cli": "claude", "model": "m", "ts": "t", "endpoint": "/v1/messages", "extra": CONTENT})
    assert tuple(row) == SUMMARY_FIELDS
    assert row["turn"] == 3 and row["input_tokens"] == 1 and row["output_tokens"] is None


# End to end: a fake upstream that records what it received and streams SSE slowly.

class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeUpstream.received.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 11, "cache_read_input_tokens": 5,
                                                             "cache_creation_input_tokens": 0, "output_tokens": 1}}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 8}},
        ]
        for event in events:
            chunk = sse(event)
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
            time.sleep(0.3)
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def upstream():
    FakeUpstream.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/base"
    server.shutdown()
    server.server_close()


def test_proxy_end_to_end_is_transparent_and_content_free(upstream, tmp_path):
    proxy = SpikeProxy(upstream, tmp_path, "test")
    url = proxy.start()
    assert url.startswith("http://127.0.0.1:")
    proxy.turn = 4
    body = as_bytes(ANTHROPIC_REQUEST)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=10)
        conn.request("POST", "/v1/messages?beta=true", body=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {SECRET}",
            "x-api-key": SECRET, "Cookie": f"session={SECRET}", "anthropic-version": "2023-06-01"})
        response = conn.getresponse()
        started = time.monotonic()
        first = response.read1(65536)
        first_at = time.monotonic() - started
        rest = response.read()
        conn.close()
    finally:
        proxy.stop()

    # Request reached the upstream unchanged, under the upstream base path, with auth forwarded.
    path, headers, received = FakeUpstream.received[0]
    assert path == "/base/v1/messages?beta=true"
    assert received == body
    assert headers["Authorization"] == f"Bearer {SECRET}" and headers["x-api-key"] == SECRET
    assert headers["anthropic-version"] == "2023-06-01"
    # Streamed, not buffered: the first event arrives before the upstream finished sleeping.
    assert b"message_start" in first and first_at < 0.25
    assert b"message_delta" in first + rest

    record = json.loads((tmp_path / "requests.jsonl").read_text())
    assert record["turn"] == 4 and record["status"] == 200 and record["message_count"] == 3
    assert "authorization" in record["header_names"] and record["auth_scheme"] in ("bearer", "x-api-key")
    assert record["usage"] == {"input_tokens": 11, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 5, "output_tokens": 8}
    summary = json.loads((tmp_path / "summary.jsonl").read_text())
    assert tuple(summary) == SUMMARY_FIELDS and summary["output_tokens"] == 8
    for name in ("requests.jsonl", "summary.jsonl"):
        text = (tmp_path / name).read_text()
        assert SECRET not in text and CONTENT not in text


def test_proxy_reports_unreachable_upstream(tmp_path):
    proxy = SpikeProxy("http://127.0.0.1:9", tmp_path, "test")
    url = proxy.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=10)
        conn.request("POST", "/v1/messages", body=b"{}", headers={"Content-Type": "application/json"})
        assert conn.getresponse().status == 502
    finally:
        proxy.stop()
    assert json.loads((tmp_path / "requests.jsonl").read_text())["status"] == 502


def post(url, path, body=b"{}"):
    conn = http.client.HTTPConnection("127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=10)
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def test_control_turn_and_cap_refusal(upstream, tmp_path):
    proxy = SpikeProxy(upstream, tmp_path, "test", cap_tokens=20)
    url = proxy.start()
    try:
        status, data = post(url, "/__m0proxy/turn", b'{"turn": 7}')
        assert status == 200 and json.loads(data)["turn"] == 7
        assert post(url, "/v1/messages", as_bytes(ANTHROPIC_REQUEST))[0] == 200  # 24 tokens: crosses the cap
        status, data = post(url, "/v1/messages", as_bytes(ANTHROPIC_REQUEST))
        assert status == 400 and b"token cap reached" in data
        assert post(url, "/__m0proxy/turn", b'{"turn": "x"}')[0] == 400
    finally:
        proxy.stop()
    assert len(FakeUpstream.received) == 1  # the refused request never left the proxy
    assert proxy.status()["refused"] == 1 and proxy.status()["spent"] == 24
    assert json.loads((tmp_path / "requests.jsonl").read_text())["turn"] == 7


# WebSocket tunnel: fake upstream speaking RFC 6455 with permessage-deflate.

import socket
import struct
import zlib

from extract import WsFrameParser, permessage_deflate


def deflate(message):
    c = zlib.compressobj(wbits=-15)
    return (c.compress(message) + c.flush(zlib.Z_SYNC_FLUSH))[:-4]


def frame(payload, opcode=1, mask=None, compressed=False):
    b0 = 0x80 | opcode | (0x40 if compressed else 0)
    n = len(payload)
    head = bytes([b0]) + (bytes([n | (0x80 if mask else 0)]) if n < 126 else
                          bytes([126 | (0x80 if mask else 0)]) + struct.pack(">H", n) if n < 65536 else
                          bytes([127 | (0x80 if mask else 0)]) + struct.pack(">Q", n))
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return head + mask + payload
    return head + payload


def recv_until(sock, marker):
    data = b""
    while marker not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


SERVER_EVENTS = [
    {"type": "response.created", "response": {}},
    {"type": "response.completed", "response": {"status": "completed", "usage": {
        "input_tokens": 1000, "input_tokens_details": {"cached_tokens": 600}, "output_tokens": 20}}},
]


def test_ws_frame_parser_fragments_and_deflate():
    message = json.dumps(RESPONSES_REQUEST).encode()
    compressed = deflate(message)
    half = len(compressed) // 2
    wire = frame(compressed[:half], opcode=1, mask=b"abcd", compressed=True)
    wire = bytes([wire[0] & 0x7F]) + wire[1:]  # first fragment: FIN cleared
    wire += frame(compressed[half:], opcode=0, mask=b"wxyz")
    parser = WsFrameParser(*permessage_deflate("permessage-deflate; client_max_window_bits")[:2])
    out = []
    for i in range(0, len(wire), 3):
        out += parser.feed(wire[i : i + 3])
    assert out == [(1, message, len(compressed))]


def test_proxy_tunnels_websocket_byte_for_byte(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    upstream_port = listener.getsockname()[1]
    seen = {}

    def serve():
        conn, _ = listener.accept()
        head = recv_until(conn, b"\r\n\r\n")
        seen["head"] = head
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: x\r\nSec-WebSocket-Extensions: permessage-deflate\r\n\r\n")
        parser = WsFrameParser(True)
        messages = []
        while not messages:
            messages = parser.feed(conn.recv(65536))
        seen["message"] = messages[0][1]
        seen["sent"] = b"".join(frame(deflate(json.dumps(e).encode()), compressed=True) for e in SERVER_EVENTS)
        conn.sendall(seen["sent"])
        time.sleep(0.2)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    proxy = SpikeProxy(f"http://127.0.0.1:{upstream_port}/backend", tmp_path, "codex")
    url = proxy.start()
    proxy.turn = 2
    try:
        client = socket.create_connection(("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=10)
        client.sendall(b"GET /responses HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       b"Sec-WebSocket-Key: k\r\nSec-WebSocket-Version: 13\r\n"
                       b"Sec-WebSocket-Extensions: permessage-deflate\r\nAuthorization: Bearer " + SECRET.encode() + b"\r\n\r\n")
        assert recv_until(client, b"\r\n\r\n").startswith(b"HTTP/1.1 101")
        body = json.dumps(dict(RESPONSES_REQUEST, type="response.create")).encode()
        client.sendall(frame(deflate(body), mask=b"mask", compressed=True))
        received = b""
        while len(received) < len(seen.get("sent", b"x" * 10**9)):
            chunk = client.recv(65536)
            if not chunk:
                break
            received += chunk
        client.close()
        assert proxy.wait_idle(5)
    finally:
        proxy.stop()
        listener.close()

    assert seen["head"].startswith(b"GET /backend/responses HTTP/1.1")
    assert b"Upgrade: websocket" in seen["head"] and b"Bearer " + SECRET.encode() in seen["head"]
    assert seen["message"] == body
    assert received == seen["sent"]
    records = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    handshake, message = records
    assert handshake["transport"] == "websocket-handshake" and handshake["status"] == 101
    assert message["transport"] == "websocket" and message["message_type"] == "response.create"
    assert message["turn"] == 2 and message["message_count"] == 6 and message["request_bytes"] == len(body)
    assert message["usage"]["cache_read_input_tokens"] == 600 and message["usage"]["input_tokens"] == 400
    assert message["stop_reason"] == "completed"
    log = (tmp_path / "requests.jsonl").read_text()
    assert SECRET not in log and CONTENT not in log


def test_block_hmacs_locate_first_changed_block():
    from extract import block_hmacs, first_changed_block

    later = copy.deepcopy(ANTHROPIC_REQUEST)
    later["messages"][0]["content"] = CONTENT + " changed"
    a, b = block_hmacs(ANTHROPIC_REQUEST, KEY), block_hmacs(later, KEY)
    assert [x[0] for x in a][:4] == ["system[0]", "system[1]", "tools[0]", "tools[1]"]
    assert a[1][3] is True and a[0][3] is False  # cache_control marker noted, not hashed
    assert first_changed_block(a, a) is None
    assert first_changed_block(a, b) == {"index": 4, "position": "messages[0](user).content[0]", "type": "text",
                                         "previous_position": "messages[0](user).content[0]"}
    moved = copy.deepcopy(ANTHROPIC_REQUEST)
    moved["system"][1].pop("cache_control")
    assert first_changed_block(a, block_hmacs(moved, KEY)) is None  # moving a marker is not a content change
    assert CONTENT not in json.dumps(a)


def test_cap_holds_when_accounting_is_slow(upstream, tmp_path, monkeypatch):
    """Regression for the flaky cap test: the spend from a response must be counted before the
    client can see that response complete, or a request sent right after it slips past the cap.
    Slowing the record write widens that window to make the race deterministic."""
    original = SpikeProxy.write

    def slow_write(self, record):
        time.sleep(0.3)
        original(self, record)

    monkeypatch.setattr(SpikeProxy, "write", slow_write)
    proxy = SpikeProxy(upstream, tmp_path, "test", cap_tokens=20)
    url = proxy.start()
    try:
        assert post(url, "/v1/messages", as_bytes(ANTHROPIC_REQUEST))[0] == 200  # 24 tokens: crosses the cap
        status, data = post(url, "/v1/messages", as_bytes(ANTHROPIC_REQUEST))
    finally:
        proxy.stop()
    assert status == 400 and b"token cap reached" in data
    assert len(FakeUpstream.received) == 1


def test_unreachable_upstream_is_logged_before_the_502(tmp_path, monkeypatch):
    """Regression for reviewer-2's flake: the record must be on disk before the client gets the 502."""
    original = SpikeProxy.write

    def slow_write(self, record):
        time.sleep(0.3)
        original(self, record)

    monkeypatch.setattr(SpikeProxy, "write", slow_write)
    proxy = SpikeProxy("http://127.0.0.1:9", tmp_path, "test")
    url = proxy.start()
    try:
        assert post(url, "/v1/messages")[0] == 502
        record = json.loads((tmp_path / "requests.jsonl").read_text())  # read at once, no waiting
    finally:
        proxy.stop()
    assert record["status"] == 502
