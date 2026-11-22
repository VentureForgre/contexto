"""RFC 6455 WebSocket framing and Responses WebSocket proxy relay tests."""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
from typing import Any

import pytest

from contexto.codex import RESPONSES, api_key_config, chatgpt_overrides, codex_websocket, plan_codex
from contexto.proxy import ContextoProxy
from contexto.rewrite import Config, RewriteEngine
from contexto.store.memory import MemoryStore
from contexto.ws import (
    OP_BINARY,
    OP_CLOSE,
    OP_CONT,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    WSProtocolError,
    _ByteReader,
    accept_key,
    apply_mask,
    close_payload,
    encode_frame,
    parse_close_code,
    read_frame,
    read_message,
    valid_client_key,
)

# Pure RFC 6455 framing tests -------------------------------------------------------------------------


def test_accept_key_matches_rfc6455_vector():
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_valid_client_key():
    assert valid_client_key("dGhlIHNhbXBsZSBub25jZQ==")
    assert valid_client_key(base64.b64encode(os.urandom(16)).decode("ascii"))
    for bad in (None, "", "not-base64!", base64.b64encode(b"short").decode("ascii"), "é" * 24):
        assert not valid_client_key(bad)


@pytest.mark.parametrize("payload", [b"", b"\x00", b"\x00\x00\x01\x02\x03", b"hello world", os.urandom(70_000)])
def test_apply_mask_round_trip(payload):
    mask = b"\x37\xfa\x21\x3d"
    masked = apply_mask(payload, mask)
    assert len(masked) == len(payload)
    assert apply_mask(masked, mask) == payload


@pytest.mark.parametrize("size", [0, 1, 125, 126, 200, 65_535, 65_536, 70_000])
@pytest.mark.parametrize("masked", [False, True])
def test_frame_round_trip_all_length_encodings(size, masked):
    payload = b"\x00" + b"a" * max(0, size - 1) if size else b""
    mask = b"\x01\x02\x03\x04" if masked else None
    wire = encode_frame(OP_TEXT, payload, fin=True, mask=mask)
    pos = 0

    def recv(n: int) -> bytes:
        nonlocal pos
        chunk = wire[pos:pos + min(n, 137)]
        pos += len(chunk)
        return chunk

    reader = _ByteReader(recv)
    fin, op, got = read_frame(reader, expect_masked=masked)
    assert (fin, op, got) == (True, OP_TEXT, payload)
    assert read_frame(reader, expect_masked=masked) is None


def test_fragmented_message_with_interleaved_ping():
    wire = (encode_frame(OP_TEXT, b'{"a":', fin=False)
            + encode_frame(OP_PING, b"hb", fin=True)
            + encode_frame(OP_CONT, b"123}", fin=True))
    reader = _ByteReader(lambda n: b"", initial=wire)
    controls: list[tuple[int, bytes]] = []
    msg = read_message(reader, expect_masked=False, on_control=lambda op, p: controls.append((op, p)))
    assert msg == (OP_TEXT, b'{"a":123}')
    assert controls == [(OP_PING, b"hb")]


@pytest.mark.parametrize("wire,expect_masked", [
    # Non-zero RSV1 bit
    (bytes([0xC1, 0x00]), False),
    # Fragmented ping (control frame with FIN=0)
    (bytes([OP_PING, 0x00]), False),
    # Control frame > 125 bytes
    (bytes([0x80 | OP_PING, 126, 0, 126]) + b"x" * 126, False),
    # Unmasked frame when masked required
    (encode_frame(OP_TEXT, b"hi", mask=None), True),
    # Masked frame when unmasked required
    (encode_frame(OP_TEXT, b"hi", mask=b"1234"), False),
    # Unexpected continuation
    (encode_frame(OP_CONT, b"orphan"), False),
])
def test_protocol_violations_raise(wire, expect_masked):
    reader = _ByteReader(lambda n: b"", initial=wire)
    with pytest.raises(WSProtocolError):
        read_message(reader, expect_masked=expect_masked)


def test_frame_size_limit_enforced():
    wire = encode_frame(OP_BINARY, b"x" * 200)
    reader = _ByteReader(lambda n: b"", initial=wire)
    with pytest.raises(WSProtocolError) as exc:
        read_frame(reader, max_bytes=100)
    assert exc.value.code == 1009


def test_close_payload_round_trip():
    p = close_payload(1000, "bye")
    assert parse_close_code(p) == 1000
    assert parse_close_code(b"") is None


# End-to-end WebSocket proxy tests --------------------------------------------------------------------


class _ListLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class _FakeWSUpstream:
    """Loopback RFC 6455 server for testing the WebSocket relay end to end."""

    def __init__(self, handler_fn) -> None:
        self._handler_fn = handler_fn
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(16)
        self._stop = threading.Event()
        self.received_headers: list[dict[str, str]] = []
        self.received_frames: list[tuple[int, bytes]] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            reader = _ByteReader(conn.recv)
            raw = reader.read_until(b"\r\n\r\n", 65536)
            lines = raw.decode("iso-8859-1").split("\r\n")
            req_line = lines[0]
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            self.received_headers.append(headers)
            self._handler_fn(conn, reader, req_line, headers, self.received_frames)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


def _client_ws_connect(port: int, path: str = "/responses",
                       extra_headers: dict[str, str] | None = None) -> tuple[socket.socket, _ByteReader, bytes]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    headers = {
        "Host": f"127.0.0.1:{port}",
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": key,
        "Sec-WebSocket-Version": "13",
        **(extra_headers or {}),
    }
    lines = [f"GET {path} HTTP/1.1", *(f"{k}: {v}" for k, v in headers.items()), "", ""]
    sock.sendall("\r\n".join(lines).encode("iso-8859-1"))
    reader = _ByteReader(sock.recv)
    head = reader.read_until(b"\r\n\r\n", 65536)
    return sock, reader, head


def test_ws_relay_rewrites_response_create_holds_tool_calls_and_observes_usage():
    order: list[str] = []

    def upstream_fn(conn, reader, req_line, headers, frames):
        accept = accept_key(headers["sec-websocket-key"])
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        conn.sendall(resp.encode("iso-8859-1"))
        while True:
            def on_ctrl(op, payload):
                if op == OP_PING:
                    conn.sendall(encode_frame(OP_PONG, payload))
                elif op == OP_CLOSE:
                    conn.sendall(encode_frame(OP_CLOSE, payload))

            msg = read_message(reader, expect_masked=True, on_control=on_ctrl)
            if msg is None:
                break
            op, payload = msg
            frames.append((op, payload))
            json.loads(payload)
            turn_n = len(frames)
            # Emit a client tool call followed by response.completed with usage.
            call_event = {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "call_id": f"call_ws_{turn_n}", "name": "exec", "arguments": "{}"},
            }
            done_event = {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [call_event["item"]],
                    "usage": {"input_tokens": 500 * turn_n, "output_tokens": 50},
                },
            }
            conn.sendall(encode_frame(OP_TEXT, json.dumps(call_event).encode()))
            conn.sendall(encode_frame(OP_TEXT, json.dumps(done_event).encode()))

    upstream = _FakeWSUpstream(upstream_fn)
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200))
    observed_calls: list[dict[str, Any]] = []

    def observe(endpoint, usage, original, *, tool_calls=None, tool_call_ids=()):
        order.append(f"observe:{usage.get('input_tokens')}")
        observed_calls.append({"endpoint": endpoint, "usage": usage, "tool_calls": tool_calls,
                               "tool_call_ids": tool_call_ids})
        return engine.observe_usage(endpoint, usage, original, tool_calls=tool_calls, tool_call_ids=tool_call_ids)

    log = _ListLog()
    proxy = ContextoProxy(
        f"http://127.0.0.1:{upstream.port}/v1",
        log,
        engine,
        observe_usage=observe,
        affinity_key="contexto-test-affinity",
        allow_websocket=True,
    )
    proxy.start()
    try:
        sock, reader, head = _client_ws_connect(
            proxy.port, "/responses",
            extra_headers={"Authorization": "Bearer test-tok", "Sec-WebSocket-Extensions": "permessage-deflate"},
        )
        try:
            assert b"101 Switching Protocols" in head
            # Turn 1: send a ping first to check control relay, then a response.create frame.
            sock.sendall(encode_frame(OP_PING, b"ping1", mask=os.urandom(4)))
            pongs: list[bytes] = []

            def make_lead():
                return [
                    {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
                    {"type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": "<environment_context>x</environment_context>"},
                    ]},
                ]

            turn1_items = [*make_lead(),
                           {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "hello " * 20}]}]
            sock.sendall(encode_frame(OP_TEXT, json.dumps({
                "type": "response.create", "model": "gpt-6-astra", "store": False, "input": turn1_items,
            }).encode(), mask=os.urandom(4)))

            ev1 = read_message(reader, expect_masked=False,
                               on_control=lambda op, p: pongs.append(p) if op == OP_PONG else None)
            assert ev1 is not None and json.loads(ev1[1])["type"] == "response.output_item.added"
            order.append("client_saw_call_1")
            ev2 = read_message(reader, expect_masked=False)
            assert ev2 is not None and json.loads(ev2[1])["type"] == "response.completed"
            assert pongs == [b"ping1"]

            # Turn 2 on the SAME WebSocket connection with many turns so RewriteEngine rotates!
            filler = "synthetic filler text " * 250
            turn2_items = [*turn1_items]
            for t in range(6):
                turn2_items += [
                    {"type": "reasoning", "id": f"rs_{t}", "summary": [], "encrypted_content": f"enc_{t}"},
                    {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": f"ans {t} {filler}"}]},
                    {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": f"prompt {t} {filler}"}]},
                ]
            sock.sendall(encode_frame(OP_TEXT, json.dumps({
                "type": "response.create", "model": "gpt-6-astra", "store": False, "input": turn2_items,
            }).encode(), mask=os.urandom(4)))

            ev3 = read_message(reader, expect_masked=False)
            assert ev3 is not None and json.loads(ev3[1])["type"] == "response.output_item.added"
            order.append("client_saw_call_2")
            ev4 = read_message(reader, expect_masked=False)
            assert ev4 is not None and json.loads(ev4[1])["type"] == "response.completed"

            # Close cleanly
            sock.sendall(encode_frame(OP_CLOSE, close_payload(1000, "done"), mask=os.urandom(4)))
            assert read_message(reader, expect_masked=False) is None
        finally:
            sock.close()
    finally:
        proxy.stop()
        upstream.close()

    # Verify Sec-WebSocket-Extensions was stripped and X-Session-Affinity injected upstream.
    assert "sec-websocket-extensions" not in upstream.received_headers[0]
    assert upstream.received_headers[0]["x-session-affinity"] == "contexto-test-affinity"
    assert upstream.received_headers[0]["authorization"] == "Bearer test-tok"

    # Verify observe_usage ran BEFORE the client received the held tool-call frame!
    assert order.index("observe:500") < order.index("client_saw_call_1")
    assert order.index("observe:1000") < order.index("client_saw_call_2")
    assert observed_calls[0]["tool_calls"] is True and observed_calls[0]["tool_call_ids"] == ("call_ws_1",)
    assert observed_calls[1]["tool_calls"] is True and observed_calls[1]["tool_call_ids"] == ("call_ws_2",)

    # Verify Turn 2 was rotated (rewritten input is shorter than the original 21 items) and pages written.
    rewritten_turn2 = json.loads(upstream.received_frames[1][1])
    assert rewritten_turn2["type"] == "response.create"
    assert len(rewritten_turn2["input"]) < len(turn2_items)
    assert store.pages

    # Verify session log recorded both websocket_turn events and one websocket_session event.
    turns = [e for e in log.events if e["event"] == "websocket_turn"]
    sessions = [e for e in log.events if e["event"] == "websocket_session"]
    assert len(turns) == 2 and turns[0]["end_event"] == "completed" and turns[0]["response_status"] == "completed"
    assert len(sessions) == 1 and sessions[0]["status"] == 101 and sessions[0]["turns"] == 2
    assert sessions[0]["close_code"] == 1000


def test_ws_non_101_upstream_forwards_http_response_for_fallback():
    def upstream_fn(conn, reader, req_line, headers, frames):
        body = b'{"error":{"message":"use http"}}'
        resp = (
            "HTTP/1.1 426 Upgrade Required\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("iso-8859-1") + body
        conn.sendall(resp)

    upstream = _FakeWSUpstream(upstream_fn)
    log = _ListLog()
    proxy = ContextoProxy(f"http://127.0.0.1:{upstream.port}/v1", log, allow_websocket=True)
    proxy.start()
    try:
        sock, reader, head = _client_ws_connect(proxy.port, "/responses")
        try:
            assert b"426 Upgrade Required" in head
            body = reader.read_exact(len(b'{"error":{"message":"use http"}}'))
            assert body == b'{"error":{"message":"use http"}}'
        finally:
            sock.close()
    finally:
        proxy.stop()
        upstream.close()

    sessions = [e for e in log.events if e["event"] == "websocket_session"]
    assert len(sessions) == 1 and sessions[0]["status"] == 426 and sessions[0]["turns"] == 0


def test_ws_rejects_origin_non_responses_and_invalid_handshake():
    log = _ListLog()
    proxy = ContextoProxy("http://127.0.0.1:9/v1", log, allow_websocket=True)
    proxy.start()
    try:
        # Browser Origin is still refused 403.
        sock, _, head = _client_ws_connect(proxy.port, "/responses", extra_headers={"Origin": "https://evil.invalid"})
        sock.close()
        assert b"403 " in head

        # Non-Responses path is still refused 501.
        sock, _, head = _client_ws_connect(proxy.port, "/v1/messages")
        sock.close()
        assert b"501 " in head

        # Bad Sec-WebSocket-Version is 400.
        sock, _, head = _client_ws_connect(proxy.port, "/responses", extra_headers={"Sec-WebSocket-Version": "8"})
        sock.close()
        assert b"400 " in head
    finally:
        proxy.stop()

    assert any(e["event"] == "request_refused" and e["reason"] == "origin" for e in log.events)
    assert any(e["event"] == "websocket_rejected" and e["path"] == "/v1/messages" for e in log.events)


def test_ws_transform_error_fails_open():
    def upstream_fn(conn, reader, req_line, headers, frames):
        accept = accept_key(headers["sec-websocket-key"])
        conn.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("iso-8859-1"))
        msg = read_message(reader, expect_masked=True)
        if msg is not None:
            frames.append(msg)
            conn.sendall(encode_frame(OP_TEXT, json.dumps({
                "type": "response.completed", "response": {"status": "completed", "output": [], "usage": {}},
            }).encode()))
        conn.sendall(encode_frame(OP_CLOSE, close_payload(1000)))

    def broken(req, headers, endpoint):
        raise RuntimeError("boom")

    upstream = _FakeWSUpstream(upstream_fn)
    log = _ListLog()
    proxy = ContextoProxy(f"http://127.0.0.1:{upstream.port}/v1", log, broken, allow_websocket=True)
    proxy.start()
    try:
        sock, reader, head = _client_ws_connect(proxy.port, "/responses")
        try:
            assert b"101 Switching Protocols" in head
            raw = json.dumps({"type": "response.create", "model": "gpt-6-astra", "input": []}).encode()
            sock.sendall(encode_frame(OP_TEXT, raw, mask=os.urandom(4)))
            ev = read_message(reader, expect_masked=False)
            assert ev is not None and json.loads(ev[1])["type"] == "response.completed"
        finally:
            sock.close()
    finally:
        proxy.stop()
        upstream.close()

    assert len(upstream.received_frames) == 1
    assert json.loads(upstream.received_frames[0][1])["type"] == "response.create"
    assert any(e["event"] == "transform_error" and e["stage"] == "transform" for e in log.events)


def test_codex_websocket_opt_in_config_and_env(tmp_path):
    assert not codex_websocket(False, {})
    assert codex_websocket(True, {})
    assert codex_websocket(False, {"CONTEXTO_CODEX_WEBSOCKET": "1"})
    assert codex_websocket(False, {"CONTEXTO_CODEX_WEBSOCKET": "true"})
    assert not codex_websocket(False, {"CONTEXTO_CODEX_WEBSOCKET": "0"})

    cfg_off = api_key_config("http://127.0.0.1:1", "echo k", "S1", tmp_path)
    cfg_on = api_key_config("http://127.0.0.1:1", "echo k", "S1", tmp_path, supports_websockets=True)
    assert "supports_websockets = false" in cfg_off
    assert "supports_websockets = true" in cfg_on
    assert "supports_websockets=true" in " ".join(
        chatgpt_overrides("http://127.0.0.1:1", "S1", tmp_path, supports_websockets=True))
    launch = plan_codex([], "http://127.0.0.1:1", "S1", env={"OPENAI_API_KEY": "sk-test"},
                        cwd=tmp_path, home=tmp_path, supports_websockets=True)
    try:
        assert "supports_websockets = true" in (launch.codex_home / "config.toml").read_text()
    finally:
        launch.cleanup()
    assert RESPONSES.in_turn is not None
