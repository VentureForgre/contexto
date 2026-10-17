"""SSE stream hold (contract PR #74 at a64f70b; packet A9): no tool call reaches the CLI before the
real-usage room is published, and the event order is preserved exactly."""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from contexto.proxy import ContextoProxy, SSEEventSplitter, StreamHold, UsageReader, sse_event_type
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


@pytest.mark.parametrize("ending", ["eof", "error"])
def test_a_hold_ended_without_its_trigger_calls_before_release_first(ending):
    # The proxy passes its usage observe here, so the usage already read is published before the CLI
    # sees a held tool call when the hold ends at an error event, end of stream, or upstream failure.
    out, released_at = [], []
    hold = StreamHold("messages", lambda: pytest.fail("no trigger in this stream"),
                      before_release=lambda: released_at.append(len(out)))
    stream = [START, *tool_block(0)]
    for raw in stream:
        out += hold.feed(raw)
    if ending == "eof":
        out += hold.eof()
    else:
        out += hold.feed(ev({"type": "error", "error": {"type": "overloaded_error"}}))
    assert released_at == [1] and out[:4] == stream  # called once, while only message_start was out
    assert hold.eof() == [] and released_at == [1]  # nothing held: not called again


def test_before_release_is_not_called_when_nothing_is_held():
    calls = []
    hold = StreamHold("messages", lambda: None, before_release=lambda: calls.append(1))
    for raw in [START, *text_block(0)]:
        hold.feed(raw)
    hold.feed(ev({"type": "error", "error": {"type": "overloaded_error"}}))
    assert hold.eof() == [] and calls == []


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


# One SSE event grammar (WHATWG HTML, server-sent events): a line ends at CRLF, LF, or CR, and an
# event ends at a blank line. The splitter that feeds StreamHold and the usage reader share it.

TERMINATORS = {"lf": b"\n", "crlf": b"\r\n", "cr": b"\r"}


def ev_with(payload: dict, sep: bytes) -> bytes:
    return b"event: " + payload["type"].encode() + sep + b"data: " + json.dumps(payload).encode() + sep + sep


def split_all(data: bytes, step: int) -> tuple[list[bytes], bytes]:
    splitter, events = SSEEventSplitter(), []
    for i in range(0, len(data), step):
        events += splitter.feed(data[i:i + step])
    return events, splitter.flush()


@pytest.mark.parametrize("step", [1, 2, 3, 7, 1 << 16])
@pytest.mark.parametrize("name", TERMINATORS)
def test_the_splitter_ends_events_at_a_blank_line_in_every_line_ending(name, step):
    sep = TERMINATORS[name]
    stream = [ev_with(e, sep) for e in (
        {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 40}},
        {"type": "message_stop"})]
    events, tail = split_all(b"".join(stream), step)
    if name == "cr":
        # The last CR of the stream ends the read, so it may still be the first half of a CRLF:
        # that event waits for the next byte, or for end of stream, where flush returns it.
        assert events == stream[:-1] and tail == stream[-1]
    else:
        assert events == stream and tail == b""


@pytest.mark.parametrize("step", [1, 2, 1 << 16])
def test_crlf_split_across_reads_is_one_line_ending_not_two(step):
    one = b"event: ping\r\ndata: {\"type\": \"ping\"}\r\n\r\n"
    events, tail = split_all(one + one, step)
    assert events == [one, one] and tail == b""


def test_a_cr_ending_a_read_waits_for_the_next_byte_then_dispatches():
    splitter = SSEEventSplitter()
    assert splitter.feed(b"data: {}\r\n\r") == []  # an LF may follow: CRLF CRLF, not CRLF CR
    assert splitter.feed(b"\ndata: {}\r") == [b"data: {}\r\n\r\n"]
    assert splitter.feed(b"\rdata") == [b"data: {}\r\r"]  # the next byte is not LF: CR CR ends it
    assert splitter.flush() == b"data" and splitter.flush() == b""


def test_mixed_line_endings_and_a_final_event_without_its_blank_line():
    data = b"data: {\"type\":\"a\"}\r\n\ndata: {\"type\":\"b\"}\n\r\ndata: {\"type\":\"c\"}\r\r\ndata: {\"type\":\"d\"}\n"
    events, tail = split_all(data, 1 << 16)
    assert [sse_event_type(e) for e in events] == ["a", "b", "c"]
    assert sse_event_type(tail) == "d" and b"".join(events) + tail == data


def read_usage(data: bytes, step: int = 7) -> UsageReader:
    reader = UsageReader("text/event-stream")
    for i in range(0, len(data), step):
        reader.feed(data[i:i + step])
    reader.finish()
    return reader


@pytest.mark.parametrize("name", TERMINATORS)
def test_the_usage_reader_reads_message_start_and_message_delta_in_every_line_ending(name):
    sep = TERMINATORS[name]
    data = b"".join(ev_with(e, sep) for e in (
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}},
        {"type": "message_stop"}))
    reader = read_usage(data)
    assert reader.usage == {"input_tokens": 10, "output_tokens": 40}
    assert reader.ending == {"stop_reason": "tool_use"}


@pytest.mark.parametrize("name", TERMINATORS)
def test_the_usage_reader_reads_a_final_message_delta_without_its_blank_line(name):
    sep = TERMINATORS[name]
    data = START + ev_with(DELTA_PAYLOAD, sep)[: -len(sep)]  # upstream closed right after the event
    assert read_usage(data).usage == {"input_tokens": 10, "output_tokens": 40}


DELTA_PAYLOAD = {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}}


# End to end through the proxy


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    stream: list[bytes] = []
    pause_before: int = -1
    pause_s = 0.0
    gzip_anyway = False
    accept_encodings: list[tuple[str, str | None]] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Upstream.accept_encodings.append((self.path, self.headers.get("Accept-Encoding")))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Upstream.accept_encodings.append((self.path, self.headers.get("Accept-Encoding")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        if Upstream.gzip_anyway:
            import gzip as _gzip
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            data = _gzip.compress(b"".join(Upstream.stream))
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(data), data))
            return
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
    Upstream.gzip_anyway, Upstream.accept_encodings = False, []

    def observe(endpoint, usage, request):
        published.append(time.monotonic())
        return [{"event": "over_bound", "source": "real", "over_bound": True, "real_tokens": 1}]

    store = MemoryStore()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", store,
                          lambda r, h, e: (r, h, [{"epoch": 0}]), observe_usage=observe)
    proxy.start()
    proxy.test_store = store
    yield proxy, published
    proxy.stop()
    server.shutdown()
    server.server_close()


def read_with_times(proxy, path="/v1/messages", accept="gzip, br"):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=20)
    body = json.dumps({"model": "m", "stream": True, "messages": [{"role": "user", "content": "q"}]}).encode()
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json", "Accept-Encoding": accept})
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


def wait_requests(store, count=1, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [e for e in store.events if e["event"] == "request"]
        if len(found) >= count:
            return found
        time.sleep(0.01)
    return [e for e in store.events if e["event"] == "request"]


def test_usage_events_are_recorded_once_on_the_entry_inside_the_hold(proxied):
    proxy, published = proxied
    Upstream.stream = [START, *tool_block(0), DELTA, STOP]
    Upstream.pause_before = -1
    read_with_times(proxy)
    entry = wait_requests(proxy.test_store)[0]
    assert entry["usage_events"] == [{"event": "over_bound", "source": "real", "over_bound": True, "real_tokens": 1}]
    assert entry["over_bound"] is True and entry["stream_hold"]["held_events"] == 3
    assert not [e for e in proxy.test_store.events if e["event"] == "over_bound"]  # logged once, on the entry


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages/count_tokens", "/responses"])
def test_identity_encoding_is_forced_upstream_on_transformed_endpoints(proxied, path):
    proxy, _ = proxied
    Upstream.stream = [START, DELTA, STOP]
    read_with_times(proxy, path=path, accept="gzip, br")
    assert Upstream.accept_encodings[-1] == (path, "identity")


def test_other_paths_keep_the_clients_accept_encoding(proxied):
    proxy, _ = proxied
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    conn.request("GET", "/v1/models", headers={"Accept-Encoding": "gzip"})
    conn.getresponse().read()
    conn.close()
    assert Upstream.accept_encodings[-1] == ("/v1/models", "gzip")


def test_an_encoded_stream_logs_hold_skipped_instead_of_skipping_silently(proxied):
    proxy, _ = proxied
    Upstream.stream, Upstream.gzip_anyway = [START, *tool_block(0), DELTA, STOP], True
    read_with_times(proxy)
    wait_requests(proxy.test_store)
    skipped = [e for e in proxy.test_store.events if e["event"] == "hold_skipped"]
    assert skipped and skipped[0]["encoding"] == "gzip"
