"""Upstream affinity header (D36): one stable, salted key per session, sent to routing proxies and
never to vendor API hosts, with the CLI's own session signals forwarded unchanged."""

from __future__ import annotations

import json
import os

import pytest

from contexto import launcher
from contexto.private import AFFINITY_SALT_FILE, affinity_salt
from contexto.proxy import AFFINITY_HEADER, ContextoProxy, affinity_key, is_vendor_host
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from test_proxy_server import (  # noqa: F401
    AUTH, REQUEST, REQUEST_BYTES, SECRET, MemoryLog, StubEngine, Upstream, call, make_proxy, upstream,
)

SALT = bytes(range(32))
KEY = affinity_key("s1", SALT)
CLAUDE_SESSION = "137f36d2-a2ef-4bc9-bbf1-b2a5ba2bac78"
# Claude Code 2.1.302 sends both on every Messages request (measured against a local stand-in, D36).
CLAUDE_HEADERS = dict(AUTH, **{"X-Claude-Code-Session-Id": CLAUDE_SESSION})
CLAUDE_BODY = json.dumps(dict(REQUEST, metadata={"user_id": json.dumps(
    {"device_id": "d", "account_uuid": "a", "session_id": CLAUDE_SESSION})}),
    ensure_ascii=False, separators=(",", ":")).encode()


def received_headers(index: int = 0) -> dict[str, str]:
    return {k.lower(): v for k, v in Upstream.received[index][2].items()}


def test_affinity_key_is_stable_per_session_and_salt_and_hides_the_id():
    assert affinity_key("s1", SALT) == affinity_key("s1", SALT) == "contexto-a476dbd7fb14a22ce20d6cade07c7017"
    assert affinity_key("s1", SALT) != affinity_key("s2", SALT)
    assert affinity_key("s1", SALT) != affinity_key("s1", bytes(32))  # another install, another key
    named = affinity_key("customer-acme-migration", SALT)
    assert "acme" not in named and named.startswith("contexto-") and len(named) == len("contexto-") + 32


def test_the_salt_is_created_once_private_and_reused(tmp_path):
    first = affinity_salt(tmp_path)
    path = tmp_path / AFFINITY_SALT_FILE
    assert len(first) == 32 and os.stat(path).st_mode & 0o777 == 0o600
    assert affinity_salt(tmp_path) == first  # every later session and resume
    path.write_text("not hex")
    with pytest.raises(ValueError):
        affinity_salt(tmp_path)


@pytest.mark.parametrize("host, vendor", [
    ("api.anthropic.com", True), ("API.Anthropic.com.", True), ("eu.api.anthropic.com", True),
    ("api.openai.com", True), ("chatgpt.com", True), ("my-resource.openai.azure.com", True),
    ("myres.services.ai.azure.com", True), ("api.anthropic.com\u3002", True),  # ideographic trailing dot
    ("api.anthro\xadpic.com", True),  # the soft hyphen maps away under IDNA, as when connecting
    ("cpa.example.com", False), ("127.0.0.1", False), ("localhost", False),
    ("notanthropic.com", False), ("anthropic.com.example.net", False), ("azure.com", False),
    ("b\xfccher.example", False),  # an IDNA name that is not a vendor's
    ("x" * 64 + ".example", True),  # cannot be IDNA-encoded: no header
])
def test_vendor_hosts_are_matched_by_normalized_name(host, vendor):
    assert is_vendor_host(host) is vendor


def test_vendor_upstreams_never_get_the_header():
    for url in ("https://api.anthropic.com", "https://api.openai.com/v1", "https://chatgpt.com/backend-api/codex",
                "https://api.anthro\xadpic.com", "https://r.openai.azure.com/openai/v1"):
        assert ContextoProxy(url, MemoryLog(), affinity_key=KEY).affinity_header is None, url
    assert ContextoProxy("https://cpa.example.com", MemoryLog(), affinity_key=KEY).affinity_header == KEY


def test_every_request_to_a_routing_proxy_carries_the_key_and_nothing_else_changes(make_proxy):
    proxy, _ = make_proxy(affinity_key=KEY)
    response, _ = call(proxy, "POST", "/v1/messages?beta=true", CLAUDE_BODY, CLAUDE_HEADERS)
    assert response.status == 200
    call(proxy, "POST", "/v1/messages/count_tokens", CLAUDE_BODY, CLAUDE_HEADERS)
    call(proxy, "GET", "/v1/models", headers=AUTH)
    assert len(Upstream.received) == 3
    for index in range(3):
        assert received_headers(index)[AFFINITY_HEADER.lower()] == KEY
    method, path, _, body = Upstream.received[0]
    assert (method, path, body) == ("POST", "/base/v1/messages?beta=true", CLAUDE_BODY)
    headers = received_headers(0)
    assert headers["x-claude-code-session-id"] == CLAUDE_SESSION
    assert headers["x-api-key"] == AUTH["x-api-key"] and headers["anthropic-beta"] == AUTH["anthropic-beta"]


def test_the_forward_original_fallback_carries_the_key(make_proxy, monkeypatch):
    def broken(headers, beta):
        raise RuntimeError("boom")

    Upstream.mode = "json"
    monkeypatch.setattr("contexto.proxy.add_beta", broken)
    proxy, log = make_proxy(transform=StubEngine("drop-first"), observe_thinking=True, affinity_key=KEY)
    response, _ = call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert response.status == 200 and Upstream.received[0][3] == REQUEST_BYTES  # the CLI's own bytes
    assert log.requests()[0]["fallback"] is True
    assert received_headers()[AFFINITY_HEADER.lower()] == KEY and received_headers()["x-api-key"] == SECRET


def test_a_failed_transform_still_sends_the_key(make_proxy):
    def failing(request, headers, endpoint):
        raise RuntimeError("engine bug")

    proxy, _ = make_proxy(transform=failing, affinity_key=KEY)
    call(proxy, "POST", "/v1/messages", REQUEST_BYTES, AUTH)
    assert Upstream.received[0][3] == REQUEST_BYTES and received_headers()[AFFINITY_HEADER.lower()] == KEY


def test_codex_session_signals_pass_through_with_the_key(make_proxy):
    proxy, _ = make_proxy(affinity_key=KEY)
    body = json.dumps({"model": "gpt-6-astra", "prompt_cache_key": "thread-1", "stream": True,
                       "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]},
                      separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer k", "session-id": "thread-1"}
    call(proxy, "POST", "/responses", body, headers)
    method, path, _, sent = Upstream.received[0]
    assert (method, path) == ("POST", "/base/responses")
    assert json.loads(sent)["prompt_cache_key"] == "thread-1"
    assert received_headers()["session-id"] == "thread-1"
    assert received_headers()[AFFINITY_HEADER.lower()] == KEY


def test_a_key_the_cli_sent_is_kept(make_proxy):
    proxy, _ = make_proxy(affinity_key=KEY)
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


def test_the_salt_is_never_read_through_a_symlink(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text(bytes(range(32)).hex())
    (tmp_path / AFFINITY_SALT_FILE).symlink_to(target)
    with pytest.raises(OSError):
        affinity_salt(tmp_path)
    (tmp_path / AFFINITY_SALT_FILE).unlink()
    (tmp_path / AFFINITY_SALT_FILE).mkdir()
    with pytest.raises((OSError, ValueError)):
        affinity_salt(tmp_path)


def test_a_symlinked_salt_costs_only_the_header(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text(bytes(range(32)).hex())
    (tmp_path / AFFINITY_SALT_FILE).symlink_to(target)
    store = SessionStore(tmp_path, "s1")
    assert launcher.session_affinity_key(store, "http://127.0.0.1:9") is None
    events = [json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
    assert any(e["event"] == "affinity_salt_unavailable" for e in events)


def test_no_salt_is_created_for_a_vendor_upstream(tmp_path):
    store = SessionStore(tmp_path, "s1")
    for url in ("https://api.anthropic.com", "https://chatgpt.com/backend-api/codex", "https://x.services.ai.azure.com"):
        assert launcher.session_affinity_key(store, url) is None
    assert not (tmp_path / AFFINITY_SALT_FILE).exists()  # not created, so nothing to fail on either
    assert launcher.session_affinity_key(store, "https://cpa.example.com") == affinity_key("s1", affinity_salt(tmp_path))


def test_start_proxy_keys_the_header_by_session_and_install_salt(tmp_path):
    def started_key(store, url="http://127.0.0.1:9"):
        proxy, _ = launcher.start_proxy(store, url, "flag", {"bound": 150_000}, False)
        try:
            return proxy.affinity_header
        finally:
            proxy.stop()

    key = started_key(SessionStore(tmp_path, "s1"))
    assert key == affinity_key("s1", affinity_salt(tmp_path))
    assert started_key(SessionStore.open_existing(tmp_path, "s1")) == key  # a resume keeps the key
    assert started_key(SessionStore(tmp_path, "s2")) not in (None, key)
    assert started_key(SessionStore(tmp_path, "s1"), "https://api.anthropic.com") is None
    assert started_key(MemoryStore(session_id="s1")) is None  # no home, no salt


def test_an_unreadable_salt_costs_only_the_header(tmp_path):
    (tmp_path / AFFINITY_SALT_FILE).write_text("short")
    store = SessionStore(tmp_path, "s1")
    proxy, _ = launcher.start_proxy(store, "http://127.0.0.1:9", "flag", {"bound": 150_000}, False)
    try:
        assert proxy.affinity_header is None
    finally:
        proxy.stop()
    events = [json.loads(line) for line in (store.dir / "log.jsonl").read_text().splitlines()]
    assert any(e["event"] == "affinity_salt_unavailable" and e["error"] == "ValueError" for e in events)
