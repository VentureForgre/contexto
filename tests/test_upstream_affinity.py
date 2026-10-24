"""Upstream affinity header (D36): one stable, content-free key per session, sent to routing
proxies and never to vendor API hosts, with the CLI's own session signals forwarded unchanged."""

from __future__ import annotations

import json

import pytest

from contexto import launcher
from contexto.proxy import AFFINITY_HEADER, ContextoProxy, affinity_key, is_vendor_host
from contexto.store.memory import MemoryStore
from test_proxy_server import AUTH, REQUEST, REQUEST_BYTES, MemoryLog, Upstream, call, make_proxy, upstream  # noqa: F401

CLAUDE_SESSION = "137f36d2-a2ef-4bc9-bbf1-b2a5ba2bac78"
# Claude Code 2.1.302 sends both on every Messages request (measured against a local stand-in, D36).
CLAUDE_HEADERS = dict(AUTH, **{"X-Claude-Code-Session-Id": CLAUDE_SESSION})
CLAUDE_BODY = json.dumps(dict(REQUEST, metadata={"user_id": json.dumps(
    {"device_id": "d", "account_uuid": "a", "session_id": CLAUDE_SESSION})}),
    ensure_ascii=False, separators=(",", ":")).encode()


def received_headers(index: int = 0) -> dict[str, str]:
    return {k.lower(): v for k, v in Upstream.received[index][2].items()}


def test_affinity_key_is_stable_per_session_and_hides_the_id():
    assert affinity_key("s1") == affinity_key("s1") == "contexto-efded64923618d4637630626180bfed6"
    assert affinity_key("s1") != affinity_key("s2")
    named = affinity_key("customer-acme-migration")
    assert "acme" not in named and named.startswith("contexto-") and len(named) == len("contexto-") + 32


@pytest.mark.parametrize("host, vendor", [
    ("api.anthropic.com", True), ("API.Anthropic.com.", True), ("eu.api.anthropic.com", True),
    ("api.openai.com", True), ("chatgpt.com", True),
    ("cliproxy.sullylab.de", False), ("127.0.0.1", False), ("localhost", False),
    ("notanthropic.com", False), ("anthropic.com.example.net", False),
])
def test_vendor_hosts_are_matched_by_domain(host, vendor):
    assert is_vendor_host(host) is vendor


def test_vendor_upstreams_never_get_the_header():
    for url in ("https://api.anthropic.com", "https://api.openai.com/v1", "https://chatgpt.com/backend-api/codex"):
        assert ContextoProxy(url, MemoryLog(), affinity_key=affinity_key("s1")).affinity_header is None
    assert ContextoProxy("https://cliproxy.sullylab.de", MemoryLog(),
                         affinity_key=affinity_key("s1")).affinity_header == affinity_key("s1")


def test_every_request_to_a_routing_proxy_carries_the_key_and_nothing_else_changes(make_proxy):
    key = affinity_key("s1")
    proxy, _ = make_proxy(affinity_key=key)
    response, _ = call(proxy, "POST", "/v1/messages?beta=true", CLAUDE_BODY, CLAUDE_HEADERS)
    assert response.status == 200
    call(proxy, "POST", "/v1/messages/count_tokens", CLAUDE_BODY, CLAUDE_HEADERS)
    call(proxy, "GET", "/v1/models", headers=AUTH)
    assert len(Upstream.received) == 3
    for index in range(3):
        assert received_headers(index)[AFFINITY_HEADER.lower()] == key
    method, path, _, body = Upstream.received[0]
    assert (method, path, body) == ("POST", "/base/v1/messages?beta=true", CLAUDE_BODY)
    headers = received_headers(0)
    assert headers["x-claude-code-session-id"] == CLAUDE_SESSION
    assert headers["x-api-key"] == AUTH["x-api-key"] and headers["anthropic-beta"] == AUTH["anthropic-beta"]


def test_codex_session_signals_pass_through_with_the_key(make_proxy):
    proxy, _ = make_proxy(affinity_key=affinity_key("s1"))
    body = json.dumps({"model": "gpt-6-astra", "prompt_cache_key": "thread-1", "stream": True,
                       "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]},
                      separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer k", "session-id": "thread-1"}
    call(proxy, "POST", "/responses", body, headers)
    method, path, _, sent = Upstream.received[0]
    assert (method, path) == ("POST", "/base/responses")
    assert json.loads(sent)["prompt_cache_key"] == "thread-1"
    assert received_headers()["session-id"] == "thread-1"
    assert received_headers()[AFFINITY_HEADER.lower()] == affinity_key("s1")


def test_a_key_the_cli_sent_is_kept(make_proxy):
    proxy, _ = make_proxy(affinity_key=affinity_key("s1"))
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, dict(AUTH, **{"x-session-affinity": "client-own"}))
    sent = [(k, v) for k, v in Upstream.received[0][2].items() if k.lower() == AFFINITY_HEADER.lower()]
    assert sent == [("x-session-affinity", "client-own")]


def test_without_a_key_no_header_is_added(make_proxy):
    proxy, _ = make_proxy()
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert AFFINITY_HEADER.lower() not in received_headers()


def test_the_rewrite_engine_keeps_the_session_fields():
    transform, _, engine_name = launcher.build_engine(MemoryStore(session_id="s1"), {"bound": 150_000})
    assert engine_name == "rewrite"
    claude = json.loads(CLAUDE_BODY)
    sent, headers, _ = transform(json.loads(CLAUDE_BODY), dict(CLAUDE_HEADERS), "/v1/messages")
    assert sent["metadata"] == claude["metadata"] and headers["X-Claude-Code-Session-Id"] == CLAUDE_SESSION


def test_start_proxy_keys_the_header_by_the_store_session():
    store = MemoryStore(session_id="s1")
    proxy, _ = launcher.start_proxy(store, "http://127.0.0.1:9", "flag", {"bound": 150_000}, False)
    try:
        assert proxy.affinity_header == affinity_key("s1")
    finally:
        proxy.stop()
    proxy, _ = launcher.start_proxy(MemoryStore(session_id="s1"), "https://api.anthropic.com", "default",
                                    {"bound": 150_000}, False)
    try:
        assert proxy.affinity_header is None
    finally:
        proxy.stop()
