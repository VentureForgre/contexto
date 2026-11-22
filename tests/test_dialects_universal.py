"""Unit and proxy end-to-end tests for the pluggable dialect registry, OpenAI Chat Completions,
and Google Gemini dialects."""

from __future__ import annotations

import base64
import json
import struct
import threading
import zlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from contexto import dialects, rewrite, stats, tokens
from contexto import proxy as proxy_module
from contexto.dialects.chat_completions import CHAT_COMPLETIONS, ChatCompletionsDialect
from contexto.dialects.gemini import GEMINI, GeminiDialect, gemini_model_from_path
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.pages import build_page, build_stub, search_pages
from contexto.store.render import own_call_ids, render_page
from test_proxy_server import AUTH, Upstream, call


@contextmanager
def _sse_upstream(chunks: list[bytes]):
    """Minimal local HTTP server returning scripted SSE chunks and recording received requests."""
    received: list[tuple[str, str, dict[str, str], bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            received.append((self.command, self.path, dict(self.headers), body))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", received
    finally:
        server.shutdown()
        server.server_close()


def _tiny_png_b64(width: int = 100, height: int = 100) -> str:
    """Minimal valid PNG header bytes encoded as base64 (for image pricing tests)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    raw = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk + struct.pack(">I", zlib.crc32(chunk))
    return base64.b64encode(raw).decode("ascii")


# 1. Dialect registry -----------------------------------------------------------------------------


def test_registry_resolves_all_four_builtin_dialects():
    assert set(dialects.dialect_names()) >= {"anthropic", "responses", "chat_completions", "gemini"}
    assert dialects.dialect_for("/v1/messages") is rewrite.ANTHROPIC
    assert dialects.dialect_for("/v1/messages/count_tokens") is rewrite.ANTHROPIC
    assert dialects.dialect_for("/responses").name == "responses"
    assert dialects.dialect_for("/v1/responses").name == "responses"
    assert dialects.dialect_for("/v1/chat/completions") is CHAT_COMPLETIONS
    assert dialects.dialect_for("/chat/completions") is CHAT_COMPLETIONS
    assert dialects.dialect_for("/v1beta/models/gemini-2.5-pro:generateContent") is GEMINI
    assert dialects.dialect_for("/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse") is GEMINI
    assert dialects.dialect_for("/v1beta/models/gemini-2.5-pro:countTokens") is GEMINI
    assert dialects.dialect_for("/v1/models") is None

    assert dialects.api_for("/v1/messages") == "messages"
    assert dialects.api_for("/v1/responses") == "responses"
    assert dialects.api_for("/v1/chat/completions") == "chat_completions"
    assert dialects.api_for("/v1beta/models/gemini-2.5-pro:generateContent") == "gemini"
    assert dialects.is_count_tokens_path("/v1/messages/count_tokens") is True
    assert dialects.is_count_tokens_path("/v1beta/models/gemini-2.5-pro:countTokens") is True
    assert dialects.is_count_tokens_path("/v1beta/models/gemini-2.5-pro:generateContent") is False
    assert dialects.is_usage_feedback_path("/v1/chat/completions") is True
    assert dialects.is_usage_feedback_path("/v1beta/models/gemini-2.5-pro:streamGenerateContent") is True
    assert dialects.is_usage_feedback_path("/v1beta/models/gemini-2.5-pro:countTokens") is False
    assert set(dialects.history_fields()) >= {"messages", "input", "contents"}
    assert gemini_model_from_path("/v1beta/models/gemini-2.5-pro:generateContent") == "gemini-2.5-pro"


# 2. OpenAI Chat Completions dialect --------------------------------------------------------------


def _chat_turn(n: int, *, body_chars: int = 400) -> list[dict]:
    pad = f"detail-{n}-" + ("x" * body_chars)
    return [
        {"role": "user", "content": f"Question {n}: {pad}"},
        {"role": "assistant", "content": None, "reasoning_content": f"secret thought {n}",
         "tool_calls": [{"id": f"call_{n}", "type": "function",
                         "function": {"name": "read_file", "arguments": json.dumps({"path": f"src/mod_{n}.py"})}}]},
        {"role": "tool", "tool_call_id": f"call_{n}", "content": f"file contents for turn {n}: {pad}"},
        {"role": "assistant", "content": f"Answer {n}: done."},
    ]


def test_chat_completions_dialect_primitives_and_thinking_strip():
    d = ChatCompletionsDialect()
    sys_msg = {"role": "system", "content": "You are helpful."}
    dev_msg = {"role": "developer", "content": "Follow repo conventions."}
    user_msg = {"role": "user", "content": "Hello"}
    asst_msg = {"role": "assistant", "content": "Checking...", "reasoning_content": "internal chain",
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]}
    tool_msg = {"role": "tool", "tool_call_id": "call_1", "content": "print(1)"}

    items = [sys_msg, dev_msg, user_msg, asst_msg, tool_msg]
    assert d.lead_count(items) == 2
    assert d.is_system_like(sys_msg) and d.is_system_like(dev_msg)
    assert d.is_turn_start(user_msg) and not d.is_turn_start(tool_msg) and not d.is_turn_start(asst_msg)
    assert d.is_assistant(asst_msg) and not d.is_assistant(tool_msg)
    assert d.result_ids(tool_msg) == ["call_1"] and d.result_ids(user_msg) == []

    # strip_thinking removes reasoning_content without mutating the original message
    stripped = d.strip_thinking(asst_msg)
    assert stripped is not None and "reasoning_content" not in stripped
    assert "reasoning_content" in asst_msg
    assert stripped["tool_calls"] == asst_msg["tool_calls"]

    # merge_first prepends preamble and is idempotent on its output
    merged = d.merge_first(user_msg, [], "PREAMBLE")
    assert user_msg["content"] == "Hello"
    assert merged["content"] == [{"type": "text", "text": "PREAMBLE"}, {"type": "text", "text": "Hello"}]
    twice = d.merge_first(merged, [], "OUTER")
    assert twice["content"][0] == {"type": "text", "text": "OUTER"}

    # Usage & stream_options.include_usage
    assert d.usage_tokens({"prompt_tokens": 500, "completion_tokens": 50}) == 500
    assert d.output_tokens({"prompt_tokens": 500, "completion_tokens": 50}) == 50
    stream_req = {"model": "gpt-4o", "messages": [user_msg], "stream": True}
    prepared = d.prepare_upstream(stream_req)
    assert prepared["stream_options"] == {"include_usage": True}
    assert "stream_options" not in stream_req  # preserves input
    explicit_false = {"model": "gpt-4o", "messages": [user_msg], "stream": True,
                      "stream_options": {"include_usage": False}}
    assert d.prepare_upstream(explicit_false) is explicit_false


def test_chat_completions_paging_rendering_and_epoch_resume(tmp_path):
    store = SessionStore(tmp_path, "chat-sess")
    config = rewrite.Config(bound=1200, headroom_floor=300, window_fraction=0.5)
    engine = rewrite.RewriteEngine(store=store, config=config)

    messages: list[dict] = [{"role": "system", "content": "System prompt"}]
    rotated = False
    for turn in range(6):
        turn_msgs = _chat_turn(turn, body_chars=600)
        messages.append(turn_msgs[0])  # new user prompt triggers rotation check
        req = {"model": "gpt-4o", "messages": list(messages), "stream": False}
        out, _, events = engine(req, {}, "/v1/chat/completions")
        # Leading system message stays first in every request
        assert out["messages"][0] == {"role": "system", "content": "System prompt"}
        if any(e.get("event") == "rotation" for e in events):
            rotated = True
            # Reasoning content stripped from retained pre-epoch assistant items
            assert all("reasoning_content" not in m for m in out["messages"] if m.get("role") == "assistant")
        est_tokens = max(1, len(json.dumps(out)) // 3)
        engine.observe_usage("/v1/chat/completions", {"prompt_tokens": est_tokens, "completion_tokens": 40}, out)
        messages.extend(turn_msgs[1:])  # assistant tool call, tool result, assistant reply

    assert rotated, "expected Chat Completions conversation to rotate under small bound"
    page_ids = store.page_ids()
    assert page_ids
    pages = [p for pid in page_ids if (p := store.get_page(pid)) is not None]
    rendered_by_id = {p["id"]: render_page(p) for p in pages}
    page0 = next((p for p in pages if "Question 0:" in rendered_by_id[p["id"]]), None)
    assert page0 is not None
    rendered = rendered_by_id[page0["id"]]
    assert "Question 0:" in rendered and "tool_use read_file" in rendered and "tool_result:" in rendered
    assert "secret thought" not in rendered
    assert page0["stub"]["tools"] == ["read_file"]
    assert "src/mod_0.py" in page0["stub"]["paths"]
    assert page0["id"] in search_pages(pages, "mod_0.py")

    # Epoch record resumes cleanly across a new engine instance on the same store
    next_messages = [*messages, {"role": "user", "content": "Follow-up after restart"}]
    req2 = {"model": "gpt-4o", "messages": next_messages, "stream": False}
    engine2 = rewrite.RewriteEngine(store=store, config=config)
    _, _, events2 = engine2(req2, {}, "/v1/chat/completions")
    assert any(e.get("event") == "epoch_resumed" for e in events2), events2
    store.release()


def test_chat_completions_proxy_streaming_hold_and_usage(make_proxy, tmp_path):
    store = SessionStore(tmp_path, "chat-proxy")
    engine = rewrite.RewriteEngine(store=store, config=rewrite.Config(bound=10_000, headroom_floor=1_000))
    announced: list[tuple[str, ...]] = []
    chunks = [
        (b'data: {"id":"cmpl_1","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":'
         b'[{"index":0,"id":"call_abc","type":"function","function":{"name":"contexto_state",'
         b'"arguments":"{}"}}]},"finish_reason":null}]}\n\n'),
        b'data: {"id":"cmpl_1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        (b'data: {"id":"cmpl_1","choices":[],"usage":{"prompt_tokens":321,"completion_tokens":19,'
         b'"prompt_tokens_details":{"cached_tokens":100}}}\n\n'),
        b"data: [DONE]\n\n",
    ]
    with _sse_upstream(chunks) as (url, received):
        proxy, log = make_proxy(upstream=url, transform=engine, observe_usage=engine.observe_usage,
                                on_tool_calls=lambda ids: announced.append(ids))
        req = {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "Check state"}]}
        resp, body = call(proxy, "POST", "/v1/chat/completions", json.dumps(req).encode(), AUTH)
        assert resp.status == 200 and b"[DONE]" in body

        # stream_options.include_usage was injected automatically
        sent_body = json.loads(received[0][3])
        assert sent_body["stream_options"] == {"include_usage": True}
        assert announced == [("call_abc",)]

        [entry] = log.requests()
        assert entry["api"] == "chat_completions"
        assert entry["usage"]["prompt_tokens"] == 321
        assert entry["stop_reason"] == "tool_calls"
        assert entry["stream_hold"]["triggered"] is True
        assert entry["stream_hold"]["held_events"] >= 1

        # stats module reads chat_completions usage and cache share cleanly
        store.log(entry)
        st = stats.session_stats(tmp_path, "chat-proxy")
        assert st["requests"]["model"] == 1 and st["prompt"]["peak_tokens"] == 321
        assert st["cache"]["cache_read_tokens"] == 100
    store.release()


# 3. Google Gemini dialect ------------------------------------------------------------------------


def _gemini_turn(n: int, *, body_chars: int = 400) -> list[dict]:
    pad = f"gem-{n}-" + ("y" * body_chars)
    return [
        {"role": "user", "parts": [{"text": f"Prompt {n}: {pad}"}]},
        {"role": "model", "parts": [
            {"text": f"internal reasoning {n}", "thought": True},
            {"functionCall": {"id": f"fc_{n}", "name": "read_file", "args": {"path": f"lib/g_{n}.py"}}},
        ]},
        {"role": "user", "parts": [
            {"functionResponse": {"id": f"fc_{n}", "name": "read_file", "response": {"content": f"body {n}: {pad}"}}},
        ]},
        {"role": "model", "parts": [{"text": f"Summary {n}."}]},
    ]


def test_gemini_dialect_primitives_media_and_thinking_strip():
    d = GeminiDialect()
    user_item = {"role": "user", "parts": [{"text": "Analyze this image"},
                                           {"inlineData": {"mimeType": "image/png", "data": _tiny_png_b64()}}]}
    model_item = {"role": "model", "parts": [{"text": "deep thought", "thought": True},
                                             {"text": "Final answer"}]}
    func_resp = {"role": "user", "parts": [{"functionResponse": {"id": "fc_9", "name": "ls",
                                                                 "response": {"files": ["a.py"]}}}]}

    assert d.is_turn_start(user_item) is True
    assert d.is_turn_start(func_resp) is False
    assert d.is_assistant(model_item) is True
    assert d.result_ids(func_resp) == ["fc_9"]

    # Thought parts stripped without mutating input
    stripped = d.strip_thinking(model_item)
    assert stripped == {"role": "model", "parts": [{"text": "Final answer"}]}
    assert len(model_item["parts"]) == 2

    # Thought-only model item gets placeholder
    only_thought = {"role": "model", "parts": [{"text": "only thought", "thought": True}]}
    assert d.strip_thinking(only_thought) == {"role": "model", "parts": [{"text": rewrite.THINKING_OMITTED}]}

    # Media pricing recognises Gemini inlineData image and Chat Completions image_url
    _, found = tokens.split_media([user_item])
    assert len(found) == 1
    assert tokens.media_cost(found).tokens >= tokens.IMAGE_MIN_TOKENS
    chat_img = {"role": "user", "content": [{"type": "image_url",
                                             "image_url": {"url": f"data:image/png;base64,{_tiny_png_b64()}"}}]}
    _, chat_found = tokens.split_media([chat_img])
    assert len(chat_found) == 1 and tokens.media_cost(chat_found).tokens >= tokens.IMAGE_MIN_TOKENS
    assert tokens.is_tool_output(func_resp) is True

    # Usage & output_tokens
    usage = {"promptTokenCount": 800, "candidatesTokenCount": 40, "thoughtsTokenCount": 60,
             "cachedContentTokenCount": 200}
    assert d.usage_tokens(usage) == 800
    assert d.output_tokens(usage) == 100


def test_gemini_paging_count_tokens_rendering_and_resume(tmp_path):
    store = SessionStore(tmp_path, "gem-sess")
    config = rewrite.Config(bound=1200, headroom_floor=300, window_fraction=0.5)
    engine = rewrite.RewriteEngine(store=store, config=config)
    ep = "/v1beta/models/gemini-2.5-pro:generateContent"

    contents: list[dict] = []
    rotated = False
    for turn in range(6):
        turn_items = _gemini_turn(turn, body_chars=600)
        contents.append(turn_items[0])  # new user prompt triggers rotation check
        req = {"systemInstruction": {"parts": [{"text": "Be concise."}]}, "contents": list(contents)}
        out, _, events = engine(req, {}, ep)
        if any(e.get("event") == "rotation" for e in events):
            rotated = True
            for item in out["contents"]:
                if item.get("role") == "model":
                    assert not any(p.get("thought") is True for p in item.get("parts", []))
        est_tokens = max(1, len(json.dumps(out)) // 3)
        engine.observe_usage(ep, {"promptTokenCount": est_tokens, "candidatesTokenCount": 30}, out)
        contents.extend(turn_items[1:])

    assert rotated
    # :countTokens returns the current epoch's view with no side effects
    count_req = {"systemInstruction": {"parts": [{"text": "Be concise."}]}, "contents": list(contents)}
    counted, _, count_events = engine(count_req, {}, "/v1beta/models/gemini-2.5-pro:countTokens")
    assert len(count_events) == 1 and count_events[0]["event"] == "count_tokens" and count_events[0]["epoch"] >= 1
    assert len(counted["contents"]) < len(contents)

    # Stored pages render and search identically
    pages = [p for pid in store.page_ids() if (p := store.get_page(pid)) is not None]
    rendered_by_id = {p["id"]: render_page(p) for p in pages}
    page0 = next((p for p in pages if "Prompt 0:" in rendered_by_id[p["id"]]), None)
    assert page0 is not None
    rendered = rendered_by_id[page0["id"]]
    assert "Prompt 0:" in rendered and "tool_use read_file" in rendered and "tool_result:" in rendered
    assert "internal reasoning" not in rendered
    assert page0["stub"]["tools"] == ["read_file"]
    assert "lib/g_0.py" in page0["stub"]["paths"]

    # Epoch resume on a fresh engine
    engine2 = rewrite.RewriteEngine(store=store, config=config)
    req2 = {"systemInstruction": {"parts": [{"text": "Be concise."}]},
            "contents": [*contents, {"role": "user", "parts": [{"text": "Next turn"}]}]}
    _, _, events2 = engine2(req2, {}, ep)
    assert any(e.get("event") == "epoch_resumed" for e in events2), events2
    store.release()


def test_gemini_proxy_sse_stream_hold_and_usage(make_proxy, tmp_path):
    store = SessionStore(tmp_path, "gem-proxy")
    engine = rewrite.RewriteEngine(store=store, config=rewrite.Config(bound=10_000, headroom_floor=1_000))
    announced: list[tuple[str, ...]] = []
    chunks = [
        (b'data: {"candidates":[{"content":{"role":"model","parts":[{"functionCall":'
         b'{"id":"fc_42","name":"contexto_state","args":{}}}]}}]}\n\n'),
        (b'data: {"candidates":[{"content":{"role":"model","parts":[]},"finishReason":"STOP"}],'
         b'"usageMetadata":{"promptTokenCount":512,"candidatesTokenCount":24,"cachedContentTokenCount":256}}\n\n'),
    ]
    with _sse_upstream(chunks) as (url, _received):
        proxy, log = make_proxy(upstream=url, transform=engine, observe_usage=engine.observe_usage,
                                on_tool_calls=lambda ids: announced.append(ids))
        req = {"contents": [{"role": "user", "parts": [{"text": "Inspect state"}]}]}
        ep = "/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse"
        resp, body = call(proxy, "POST", ep, json.dumps(req).encode(), AUTH)
        assert resp.status == 200 and b"fc_42" in body
        assert announced == [("fc_42",)]

        [entry] = log.requests()
        assert entry["api"] == "gemini"
        assert entry["model"] == "gemini-2.5-pro"
        assert entry["stream"] is True
        assert entry["usage"]["promptTokenCount"] == 512
        assert entry["stop_reason"] == "STOP"
        assert entry["stream_hold"]["triggered"] is True
        assert entry["stream_hold"]["held_events"] >= 1

        store.log(entry)
        st = stats.session_stats(tmp_path, "gem-proxy")
        assert st["requests"]["model"] == 1 and st["prompt"]["peak_tokens"] == 512
        assert st["cache"]["cache_read_tokens"] == 256
    store.release()


def test_echo_tool_filtering_for_chat_completions_and_gemini():
    chat_msgs = [
        {"role": "user", "content": "Find secret"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "mcp__contexto__contexto_search", "arguments": '{"query": "alpha"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "alpha matched old turn"},
    ]
    assert own_call_ids(chat_msgs) == {"c1"}
    page = build_page(b"k" * 32, chat_msgs, 0)
    assert "alpha matched old turn" not in render_page(page, searchable=True)
    assert "alpha matched old turn" in render_page(page, searchable=False)

    gem_msgs = [
        {"role": "user", "parts": [{"text": "Find secret"}]},
        {"role": "model", "parts": [{"functionCall": {"id": "g1", "name": "mcp__contexto__contexto_recall",
                                                      "args": {"page_id": "pg_0"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"id": "g1", "name": "mcp__contexto__contexto_recall",
                                                         "response": {"text": "recalled secret text"}}}]},
    ]
    assert own_call_ids(gem_msgs) == {"g1"}
    gpage = build_page(b"k" * 32, gem_msgs, 0)
    assert "recalled secret text" not in render_page(gpage, searchable=True)
    assert "recalled secret text" in render_page(gpage, searchable=False)
    assert build_stub(gem_msgs)["tools"] == ["mcp__contexto__contexto_recall"]
    assert MemoryStore().page_ids() == []


@pytest.mark.parametrize("suffix, expected", [
    ("/a|b[0]", "/base/a|b[0]/v1/messages"),
    ("/a\\b", "/base/a/b/v1/messages"),
    ("/a/b/../c", "/base/a/c/v1/messages"),
    ("/../x", "/x/v1/messages"),
])
def test_base_path_encoding_edge_cases_match_claude_code(make_proxy, upstream, suffix, expected):
    proxy, _ = make_proxy(upstream=upstream + suffix)
    resp, _ = call(proxy, "POST", "/v1/messages",
                   json.dumps({"model": "claude-haiku-5-5", "max_tokens": 10,
                               "messages": [{"role": "user", "content": "hi"}]}).encode(),
                   AUTH)
    assert resp.status == 200
    assert Upstream.received[0][1] == expected
    assert proxy_module._normalize_base_path("/a/..") == ""
