"""SSE stream hold (contract PR #74 at a64f70b; packet A9): no tool call reaches the CLI before the
real-usage room is published, and the event order is preserved exactly."""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from contexto.proxy import ContextoProxy, StreamHold, sse_event_type
from contexto.store.memory import MemoryStore


def ev(payload: dict, crlf: bool = False) -> bytes:
    sep = b"\r\n" if crlf else b"\n"
    return b"event: " + payload["type"].encode() + sep + b"data: " + json.dumps(payload).encode() + sep + sep


START = ev({"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}})
PING = ev({"type": "ping"})
DELTA = ev({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}})
STOP = ev({"type": "message_stop"})


def text_block(i: int) -> list[bytes]:
    return [ev({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}),
            ev({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": f"t{i}"}}),
            ev({"type": "content_block_stop", "index": i})]


def tool_block(i: int) -> list[bytes]:
    return [ev({"type": "content_block_start", "index": i,
                "content_block": {"type": "tool_use", "id": f"toolu_{i}", "name": "mcp__contexto__contexto_search",
                                  "input": {}}}),
            ev({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
            ev({"type": "content_block_stop", "index": i})]


def run_hold(api: str, events: list[bytes], eof: bool = True):
    calls, out = [], []
    hold = StreamHold(api, lambda: calls.append(len(out)))
    for raw in events:
        out += hold.feed(raw)
    if eof:
        out += hold.eof()
    return out, calls, hold


def test_text_streams_live_until_the_first_tool_use_then_everything_holds():
    stream = [START, *text_block(0), *tool_block(1), *text_block(2), *tool_block(3), DELTA, STOP]
    hold = StreamHold("messages", lambda: None)
    live = []
    for raw in stream[:4]:  # message_start and the whole first text block
        live += hold.feed(raw)
    assert live == stream[:4]
    for raw in stream[4:13]:  # the tool_use and everything after it, before message_delta
        assert hold.feed(raw) == []
    out, calls, _ = run_hold("messages", stream)
    assert out == stream  # exact original order after release


def test_the_publish_runs_before_any_tool_use_is_released():
    stream = [START, *tool_block(0), *text_block(1), DELTA, STOP]
    out, calls, hold = run_hold("messages", stream)
    assert calls == [1]  # triggered once, when only message_start had been written
    assert out == stream and hold.triggered and hold.held_events == 6


def test_fifteen_parallel_tool_uses_are_all_held_and_released_after_one_publish():
    blocks = [raw for i in range(15) for raw in tool_block(i)]
    stream = [START, *blocks, DELTA, STOP]
    out, calls, hold = run_hold("messages", stream)
    assert calls == [1] and hold.held_events == 45 and out == stream


def test_pings_pass_during_the_hold():
    stream = [START, *tool_block(0), PING, *text_block(1), DELTA, STOP]
    hold = StreamHold("messages", lambda: None)
    written = []
    for raw in stream:
        written.append((raw, hold.feed(raw)))
    assert dict((r, w) for r, w in written)[PING] == [PING]
    released = [raw for _, w in written for raw in w]
    content = [raw for raw in released if raw is not PING]
    assert content == [raw for raw in stream if raw is not PING]  # content order unchanged


@pytest.mark.parametrize("error_type", ["error", "response.error"])
def test_an_error_event_releases_the_hold_in_order(error_type):
    error = ev({"type": error_type, "error": {"type": "overloaded_error"}})
    stream = [START, *tool_block(0), *text_block(1), error]
    out, calls, _ = run_hold("messages", stream, eof=False)
    assert out == stream and calls == []  # no usage publish without message_delta


def test_end_of_stream_releases_the_hold_in_order():
    stream = [START, *tool_block(0), *text_block(1)]
    out, calls, _ = run_hold("messages", stream)
    assert out == stream and calls == []


def test_streams_without_tool_use_trigger_the_publish_and_never_hold():
    stream = [START, *text_block(0), DELTA, STOP]
    hold = StreamHold("messages", lambda: None)
    assert all(hold.feed(raw) == [raw] for raw in stream) and hold.triggered and hold.held_events == 0


@pytest.mark.parametrize("call_type", ["custom_tool_call", "function_call", "local_shell_call"])
def test_responses_hold_from_any_tool_call_item_until_the_terminal_event(call_type):
    created = ev({"type": "response.created", "response": {"status": "in_progress"}})
    message = ev({"type": "response.output_item.added", "item": {"type": "message", "role": "assistant"}})
    text = ev({"type": "response.output_text.delta", "delta": "x"})
    call = ev({"type": "response.output_item.added", "item": {"type": call_type, "name": "shell"}})
    args = ev({"type": "response.custom_tool_call_input.delta", "delta": "{}"})
    done = ev({"type": "response.output_item.done", "item": {"type": call_type}})
    completed = ev({"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 5}}})
    hold = StreamHold("responses", lambda: None)
    assert [hold.feed(r) for r in (created, message, text)] == [[created], [message], [text]]
    assert [hold.feed(r) for r in (call, args, done)] == [[], [], []]
    assert hold.feed(completed) == [call, args, done, completed] and hold.triggered


def test_sse_event_type_reads_data_or_event_line_and_crlf():
    assert sse_event_type(ev({"type": "message_delta"}, crlf=True)) == "message_delta"
    assert sse_event_type(b"event: ping\n\n") == "ping"


# End to end through the proxy


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    stream: list[bytes] = []
    pause_before: int = -1
    pause_s = 0.0

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i, raw in enumerate(Upstream.stream):
            if i == Upstream.pause_before:
                time.sleep(Upstream.pause_s)
            self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def proxied():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    published = []

    def observe(endpoint, usage, request):
        published.append(time.monotonic())
        return []

    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", MemoryStore(),
                          lambda r, h, e: (r, h, [{"epoch": 0}]), observe_usage=observe)
    proxy.start()
    yield proxy, published
    proxy.stop()
    server.shutdown()
    server.server_close()


def read_with_times(proxy):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=20)
    body = json.dumps({"model": "m", "stream": True, "messages": [{"role": "user", "content": "q"}]}).encode()
    conn.request("POST", "/v1/messages", body=body, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    chunks = []
    while chunk := response.read1(65536):
        chunks.append((time.monotonic(), chunk))
    conn.close()
    return chunks


def test_no_tool_use_reaches_the_client_before_the_publish(proxied):
    proxy, published = proxied
    blocks = [raw for i in range(15) for raw in tool_block(i + 1)]
    Upstream.stream = [START, *text_block(0), *blocks, DELTA, STOP]
    Upstream.pause_before, Upstream.pause_s = len(Upstream.stream) - 2, 0.5  # hold message_delta back
    chunks = read_with_times(proxy)
    body = b"".join(c for _, c in chunks)
    assert body == b"".join(Upstream.stream)  # byte-identical, original order
    first_tool = next(t for t, c in chunks if b'"tool_use"' in c)
    assert published and published[0] <= first_tool
    text_seen = next(t for t, c in chunks if b"t0" in c)
    assert text_seen < published[0] - 0.3  # the leading text streamed live during the pause


def test_upstream_ending_mid_hold_releases_everything_in_order(proxied):
    proxy, published = proxied
    Upstream.stream = [START, *tool_block(0), *text_block(1)]  # no message_delta: EOF ends the hold
    Upstream.pause_before = -1
    body = b"".join(c for _, c in read_with_times(proxy))
    assert body == b"".join(Upstream.stream)
