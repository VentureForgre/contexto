"""Tests for Claude Code 2.1.328 / Codex 0.170.0 compatibility, semver version ranges,
toolcheck heartbeat resilience, merged user --settings, Vertex/Foundry interception,
and non-invasive Claude OAuth detection & Bearer passthrough.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from contexto import cliversions, launcher, proxy, rewrite, stats, toolcheck, upstream
from contexto.commands import doctor
from contexto.rewrite import Config, RewriteEngine
from contexto.store.memory import MemoryStore

CLAUDE_BIN = shutil.which("claude")
needs_claude = pytest.mark.skipif(CLAUDE_BIN is None, reason="claude is not on PATH")


# ---------------------------------------------------------------------------
# 1. Semver compatibility ranges & structural health signals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cli,version,expected", [
    ("claude", "2.1.301", False),
    ("claude", "2.1.302-rc.1", False),
    ("claude", "2.1.302", True),
    ("claude", "2.1.322", True),
    ("claude", "2.1.325", True),
    ("claude", "2.1.328", True),
    ("claude", "2.1.329-beta.1", True),
    ("claude", "2.1.999", True),
    ("claude", "2.2.0", False),
    ("claude", "3.0.0", False),
    ("codex", "0.153.9", False),
    ("codex", "0.154.0", True),
    ("codex", "0.170.0", True),
    ("codex", "0.199.5", True),
    ("codex", "1.0.0", False),
    ("claude", "not-a-version", False),
])
def test_semver_compatibility_ranges(cli, version, expected):
    assert cliversions.is_compatible(cli, version) is expected
    assert cliversions.is_tested(cli, version) is expected


def test_subagent_shapes_supported_allows_weekly_patches_and_gates_on_drift():
    # Weekly patches inside 2.1.x (>= 2.1.302) stay enabled when structurally healthy.
    for v in ("2.1.302", "2.1.322", "2.1.325", "2.1.328", "2.1.350"):
        assert cliversions.subagent_shapes_supported(v) is True
    # Out-of-range or absent versions fall back to expiry.
    for v in (None, "", "2.1.200", "2.2.0", "3.0.0"):
        assert cliversions.subagent_shapes_supported(v) is False
    # Runtime drift signals disable early closing on newer untested patches.
    assert cliversions.subagent_shapes_supported("2.1.350", session_start_ok=False) is False
    assert cliversions.subagent_shapes_supported("2.1.350", hint_mismatch=True) is False
    assert cliversions.subagent_shapes_supported("2.1.328", hint_mismatch=True) is False
    assert cliversions.subagent_shapes_supported("2.1.350", session_start_ok=True, hint_mismatch=False) is True


def test_client_versions_tracks_structural_health_signals():
    cv = cliversions.ClientVersions()
    req = {
        "model": "claude-opus-5-5",
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"},
                                         {"type": "text", "text": "hi"}]},
            {"role": "system", "content": [{"type": "text", "text": "# Environment\n..."}]},
        ],
    }
    events = cv.observe(req, {"User-Agent": "claude-cli/2.1.328 (external, cli)",
                              "x-claude-code-request-class": "main"}, rewrite.ANTHROPIC)
    assert [e["event"] for e in events] == ["cli_version"]
    assert events[0]["tested"] is True
    assert cv.session_start_ok is True
    assert cv.hint_mismatch_seen is False
    cv.note_hint_mismatch()
    assert cv.hint_mismatch_seen is True


# ---------------------------------------------------------------------------
# 2. toolcheck.py heartbeat & structural tool-list resilience
# ---------------------------------------------------------------------------


def test_toolcheck_deferred_mode_relies_on_mcp_listed_heartbeat_when_prose_changes():
    class Heartbeat:
        def __init__(self, at: float | None) -> None:
            self.at = at

        def mcp_listed_at(self) -> float | None:
            return self.at

    now = 1000.0
    # Deferred mode active (ToolSearch + defer_loading), but Claude Code changed its listing prose
    # so no mcp__contexto__* names appear verbatim in the system text.
    request = {
        "model": "claude-opus-5-5",
        "tools": [
            {"name": "Bash", "input_schema": {"type": "object"}},
            {"name": "ToolSearch", "input_schema": {"type": "object"}},
            {"name": "DeferredPlaceholder", "defer_loading": True, "input_schema": {"type": "object"}},
        ],
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": [{"type": "text", "text": "# Environment\nDeferred tools ready."}]},
        ],
    }
    # Without mcp_listed_at heartbeat -> missing after grace.
    absent = toolcheck.ToolCheck(Heartbeat(None), clock=lambda: now, wall=lambda: 50.0)
    now += toolcheck.GRACE_S
    [ev] = absent.observe(request, "anthropic", main=True)
    assert ev["event"] == "mcp_tools_missing" and ev["server_listed"] is False

    # With mcp_listed_at heartbeat during this launch -> present (no false alarm from prose tweak!).
    present = toolcheck.ToolCheck(Heartbeat(55.0), clock=lambda: now, wall=lambda: 50.0)
    assert present.observe(request, "anthropic", main=True) == []
    assert present.verdict() is None


def test_toolcheck_handles_tweaked_disconnect_and_bulleted_listings():
    offered, down, removed, listing_seen = toolcheck.contexto_notice_state([
        "Deferred tools available:\n- `mcp__contexto__contexto_search`\n- mcp__contexto__contexto_recall",
    ])
    assert (offered, down, removed, listing_seen) == (True, False, False, True)

    offered, down, removed, listing_seen = toolcheck.contexto_notice_state([
        "Deferred tools available:\n- mcp__contexto__contexto_search",
        "MCP server disconnected: contexto",
    ])
    assert down is True and removed is True and listing_seen is True


# ---------------------------------------------------------------------------
# 3. User --settings merging, -- scanning, and resilient option scanning
# ---------------------------------------------------------------------------


def test_extract_and_merge_user_settings_stops_at_double_dash_and_preserves_fields(tmp_path):
    # Literal --settings after -- is preserved as a prompt.
    rem, cfg = launcher.extract_user_settings(["-p", "--", "--settings", "--settings=bad.json"])
    assert rem == ["-p", "--", "--settings", "--settings=bad.json"] and cfg is None

    # Optional-value flags from 2.1.328 do not break --settings scanning.
    f1 = tmp_path / "s1.json"
    f1.write_text(json.dumps({
        "model": "claude-sonnet-4-6",
        "env": {"A": "1", "B": "from-file"},
        "permissions": {"allow": ["Read"], "deny": ["Bash"]},
    }), encoding="utf-8")
    args = ["--remote-control", "--settings", str(f1), "--settings={\"env\":{\"B\":\"from-inline\"}}",
            "-p", "--", "--settings"]
    rem, cfg = launcher.extract_user_settings(args)
    assert rem == ["--remote-control", "-p", "--", "--settings"]
    assert cfg == {
        "model": "claude-sonnet-4-6",
        "env": {"A": "1", "B": "from-inline"},
        "permissions": {"allow": ["Read"], "deny": ["Bash"]},
    }
    base = launcher.settings_payload("http://127.0.0.1:9999", "stripped")
    merged, had_status = launcher.merge_settings(cfg, base, {"statusLine": {"type": "command", "command": "ctx"}})
    assert had_status is False
    assert merged["env"]["A"] == "1" and merged["env"]["B"] == "from-inline"
    assert merged["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"
    assert merged["permissions"]["deny"] == ["Bash"]
    assert "Read" in merged["permissions"]["allow"]
    assert all(r in merged["permissions"]["allow"] for r in launcher.contexto_tool_rules())
    assert merged["statusLine"] == {"type": "command", "command": "ctx"}


def test_merge_settings_preserves_user_statusline():
    user = {"statusLine": {"type": "command", "command": "my-status"}}
    base = launcher.settings_payload("http://127.0.0.1:9999", "stripped")
    merged, had_status = launcher.merge_settings(user, base, {"statusLine": {"type": "command", "command": "ctx"}})
    assert had_status is True
    assert merged["statusLine"] == {"type": "command", "command": "my-status"}


def test_user_settings_invalid_shapes_fail_closed(tmp_path):
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    bad_env = tmp_path / "bad_env.json"
    bad_env.write_text('{"env": "nope"}', encoding="utf-8")
    bad_perms = tmp_path / "bad_perms.json"
    bad_perms.write_text('{"permissions": {"allow": "Read"}}', encoding="utf-8")
    for args in (
        ["--settings", str(arr)],
        ["--settings", str(bad_env)],
        ["--settings", str(bad_perms)],
        ["--settings={\"permissions\": 123}"],
        ["--settings", str(tmp_path)],  # directory
    ):
        with pytest.raises(launcher.LaunchError) as exc:
            launcher.extract_user_settings(args)
        assert exc.value.code == launcher.EXIT_USAGE


def test_user_settings_upstream_and_cloud_guard(tmp_path):
    home, cwd, managed = tmp_path / "h", tmp_path / "p", tmp_path / "m.json"
    home.mkdir()
    cwd.mkdir()
    # --settings env.ANTHROPIC_BASE_URL resolves as upstream when --upstream is not given.
    up, hints = launcher.claude_launch_settings(
        None, cwd, {}, home=home, managed=managed,
        extra_settings={"env": {"ANTHROPIC_BASE_URL": "https://flag-gw.invalid",
                                "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}},
    )
    assert (up.url, up.source, hints) == ("https://flag-gw.invalid", "flag:--settings", "forwarded")
    # Unsupported provider inside --settings is still refused.
    with pytest.raises(launcher.LaunchError) as exc:
        launcher.claude_launch_settings(None, cwd, {}, home=home, managed=managed,
                                        extra_settings={"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}})
    assert exc.value.code == launcher.EXIT_USAGE
    assert "CLAUDE_CODE_USE_BEDROCK" in str(exc.value)
    assert "--settings" in str(exc.value)


# ---------------------------------------------------------------------------
# 4. Vertex AI & Microsoft Foundry interception, path matching, and error text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,expected", [
    ("/v1/messages", ("messages", "/v1/messages")),
    ("/v1/messages/count_tokens", ("messages", "/v1/messages/count_tokens")),
    ("/anthropic/v1/messages", ("messages", "/v1/messages")),
    ("/anthropic/v1/messages/count_tokens", ("messages", "/v1/messages/count_tokens")),
    ("/projects/p1/locations/us-east5/publishers/anthropic/models/claude-sonnet-4-6:streamRawPredict",
     ("messages", "/v1/messages")),
    ("/v1/projects/p1/locations/global/publishers/anthropic/models/claude-opus-5-5:rawPredict",
     ("messages", "/v1/messages")),
    ("/projects/p1/locations/us-east5/publishers/anthropic/models/count-tokens:rawPredict",
     ("messages", "/v1/messages/count_tokens")),
    ("/v1/projects/p1/locations/us-east5/publishers/anthropic/models/count-tokens:rawPredict",
     ("messages", "/v1/messages/count_tokens")),
    ("/responses", ("responses", "/responses")),
    ("/v1/responses", ("responses", "/v1/responses")),
    ("/api/hello", (None, None)),
])
def test_proxy_classify_path_for_vertex_foundry_and_direct(path, expected):
    assert proxy.classify_path(path) == expected
    assert proxy.is_transformed_path(path) is (expected[0] is not None)


def test_vertex_and_foundry_upstream_resolution_and_dual_refusal(tmp_path):
    home, cwd, managed = tmp_path / "h", tmp_path / "p", tmp_path / "m.json"
    home.mkdir()
    cwd.mkdir()

    # Vertex via CLOUD_ML_REGION (regional and global) and explicit ANTHROPIC_VERTEX_BASE_URL.
    up, _ = launcher.claude_launch_settings(None, cwd, {"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "us-east5"},
                                            home=home, managed=managed)
    assert up.url == "https://us-east5-aiplatform.googleapis.com/v1"
    assert up.base_url_key == "ANTHROPIC_VERTEX_BASE_URL" and up.provider == "CLAUDE_CODE_USE_VERTEX"

    up_global, _ = launcher.claude_launch_settings(None, cwd,
                                                   {"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "global"},
                                                   home=home, managed=managed)
    assert up_global.url == "https://aiplatform.googleapis.com/v1"

    up_custom, _ = launcher.claude_launch_settings(
        None, cwd, {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_BASE_URL": "https://vertex-gw.invalid/v1"},
        home=home, managed=managed)
    assert up_custom.url == "https://vertex-gw.invalid/v1" and up_custom.base_url_key == "ANTHROPIC_VERTEX_BASE_URL"

    # Vertex missing both region and base URL -> clean usage error.
    with pytest.raises(launcher.LaunchError) as exc:
        launcher.claude_launch_settings(None, cwd, {"CLAUDE_CODE_USE_VERTEX": "1"}, home=home, managed=managed)
    assert exc.value.code == launcher.EXIT_USAGE and "CLOUD_ML_REGION" in str(exc.value)

    # Foundry via ANTHROPIC_FOUNDRY_RESOURCE and ANTHROPIC_FOUNDRY_BASE_URL.
    f_up, _ = launcher.claude_launch_settings(
        None, cwd, {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": "contoso"},
        home=home, managed=managed)
    assert f_up.url == "https://contoso.services.ai.azure.com/anthropic"
    assert f_up.base_url_key == "ANTHROPIC_FOUNDRY_BASE_URL" and f_up.provider == "CLAUDE_CODE_USE_FOUNDRY"

    f_custom, _ = launcher.claude_launch_settings(
        None, cwd, {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_BASE_URL": "https://foundry.invalid"},
        home=home, managed=managed)
    assert f_custom.url == "https://foundry.invalid"

    # Foundry missing both resource and base URL -> clean usage error.
    with pytest.raises(launcher.LaunchError) as exc:
        launcher.claude_launch_settings(None, cwd, {"CLAUDE_CODE_USE_FOUNDRY": "1"}, home=home, managed=managed)
    assert exc.value.code == launcher.EXIT_USAGE and "ANTHROPIC_FOUNDRY_RESOURCE" in str(exc.value)

    # Both Vertex and Foundry on simultaneously -> refused clearly.
    with pytest.raises(launcher.LaunchError) as exc:
        launcher.claude_launch_settings(None, cwd, {"CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1"},
                                        home=home, managed=managed)
    assert exc.value.code == launcher.EXIT_USAGE and "Both Google Vertex AI and Microsoft Foundry" in str(exc.value)


def test_unsupported_cloud_provider_refusal_lists_exact_checked_locations(tmp_path):
    home, cwd, managed = tmp_path / "h", tmp_path / "p", tmp_path / "m.json"
    home.mkdir()
    cwd.mkdir()
    with pytest.raises(launcher.LaunchError) as exc:
        launcher.check_cloud_provider(cwd, {"CLAUDE_CODE_USE_BEDROCK": "1"}, home=home, managed=managed)
    msg = str(exc.value)
    assert "in any of these places" not in msg
    for expected in (
        "the process environment",
        ".claude/settings.local.json",
        ".claude/settings.json",
        "~/.claude/settings.json",
        "managed-settings.json",
        "--settings",
    ):
        assert expected in msg, (expected, msg)


def test_proxy_transforms_vertex_and_foundry_and_passes_oauth_bearer_verbatim():
    seen: list[dict[str, Any]] = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            seen.append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(raw),
            })
            resp = json.dumps({
                "id": "msg_1", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 120, "output_tokens": 5},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args: Any) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    store = MemoryStore()
    engine = RewriteEngine(store, config=Config(bound=150_000))
    px = proxy.ContextoProxy(
        f"http://127.0.0.1:{srv.server_address[1]}/v1",
        store,
        engine,
        observe_usage=engine.observe_usage,
        observe_thinking=True,
        strip_hint_headers=True,
    )
    px.start(0)
    try:
        # 1. Vertex :rawPredict path + Bearer OAuth + oauth beta + hint header stripped
        vertex_path = "/projects/proj-1/locations/us-east5/publishers/anthropic/models/claude-sonnet-4-6:rawPredict"
        conn = http.client.HTTPConnection("127.0.0.1", px.port, timeout=10)
        secret_token = "sk-ant-oat01-SECRET-OAUTH-TOKEN-NEVER-LOGGED"
        payload = json.dumps({"messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}).encode()
        conn.request("POST", vertex_path, body=payload, headers={
            "Host": f"127.0.0.1:{px.port}",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "x-claude-code-request-class": "main",
        })
        res = conn.getresponse()
        assert res.status == 200
        res.read()
        conn.close()

        # 2. Foundry /anthropic/v1/messages path
        conn = http.client.HTTPConnection("127.0.0.1", px.port, timeout=10)
        conn.request("POST", "/anthropic/v1/messages?beta=true", body=payload, headers={
            "Host": f"127.0.0.1:{px.port}",
            "Content-Type": "application/json",
            "api-key": "fake-foundry-key",
        })
        res = conn.getresponse()
        assert res.status == 200
        res.read()
        conn.close()
    finally:
        px.stop()
        srv.shutdown()
        srv.server_close()

    assert seen[0]["path"] == "/v1" + vertex_path
    assert seen[0]["headers"]["authorization"] == f"Bearer {secret_token}"
    assert "oauth-2025-04-20" in seen[0]["headers"]["anthropic-beta"]
    assert proxy.THINKING_BINDING_BETA in seen[0]["headers"]["anthropic-beta"]
    assert "x-claude-code-request-class" not in seen[0]["headers"]
    assert seen[1]["path"] == "/v1/anthropic/v1/messages?beta=true"
    assert seen[1]["headers"]["api-key"] == "fake-foundry-key"

    # Verify engine observed usage on both requests and zero secrets reached the session log.
    req_events = [e for e in store.events if e.get("event") == "request"]
    assert len(req_events) == 2
    assert all(e.get("api") == "messages" and e.get("usage", {}).get("input_tokens") == 120 for e in req_events)
    assert secret_token not in json.dumps(store.events)
    assert "fake-foundry-key" not in json.dumps(store.events)

    # Vendor host check omits affinity for Vertex AI (global & regional) and Foundry, not spoofed hosts.
    assert proxy.is_vendor_host("us-east5-aiplatform.googleapis.com") is True
    assert proxy.is_vendor_host("aiplatform.googleapis.com") is True
    assert proxy.is_vendor_host("myres.services.ai.azure.com") is True
    assert proxy.is_vendor_host("us-east5-aiplatform.googleapis.com.evil.com") is False
    assert proxy.is_vendor_host("a.b-aiplatform.googleapis.com") is False
    assert proxy.is_vendor_host("-aiplatform.googleapis.com") is False


def test_stats_counts_vertex_and_foundry_requests(tmp_path):
    from contexto.store import SessionStore

    store = SessionStore(tmp_path, "vf1")
    store.log({"event": "launch", "cli": "claude", "config": {"bound": 150_000}, "ts": "2026-11-22T00:00:00.000000Z"})
    store.log({"event": "proxy_start", "pages": 0, "ts": "2026-11-22T00:00:01.000000Z"})
    store.log({
        "event": "request", "method": "POST",
        "path": "/projects/p/locations/us-east5/publishers/anthropic/models/claude-sonnet-4-6:streamRawPredict",
        "status": 200, "original_request_bytes": 1000, "request_bytes": 1000,
        "usage": {"input_tokens": 50, "output_tokens": 10},
        "transform_events": [{"event": "request", "main": True, "cli_session": True, "request_class": "main"}],
        "ts": "2026-11-22T00:00:02.000000Z",
    })
    store.log({
        "event": "request", "method": "POST",
        "path": "/projects/p/locations/us-east5/publishers/anthropic/models/count-tokens:rawPredict",
        "status": 200, "ts": "2026-11-22T00:00:03.000000Z",
    })
    store.log({
        "event": "request", "method": "POST", "path": "/anthropic/v1/messages",
        "status": 200, "original_request_bytes": 1000, "request_bytes": 1000,
        "usage": {"input_tokens": 60, "output_tokens": 10},
        "transform_events": [{"event": "request", "main": True, "cli_session": True, "request_class": "main"}],
        "ts": "2026-11-22T00:00:04.000000Z",
    })
    store.log({"event": "proxy_stop", "pages": 0, "ts": "2026-11-22T00:00:05.000000Z"})
    store.log({"event": "exit", "code": 0, "ts": "2026-11-22T00:00:06.000000Z"})

    out = stats.session_stats(tmp_path, "vf1")
    assert out["requests"]["model"] == 2
    assert out["requests"]["count_tokens"] == 1


# ---------------------------------------------------------------------------
# 5. Non-invasive Claude OAuth detection
# ---------------------------------------------------------------------------


def test_claude_oauth_detection_is_non_invasive_and_never_reads_credentials_file(tmp_path, monkeypatch):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()

    # Nothing configured -> None.
    assert upstream.claude_oauth_detected(env={}, cwd=cwd, home=home) is None

    # 1. CLAUDE_CODE_OAUTH_TOKEN in env -> presence detected, value never returned.
    secret = "sk-ant-oat01-super-secret-value"
    found = upstream.claude_oauth_detected(env={"CLAUDE_CODE_OAUTH_TOKEN": secret}, cwd=cwd, home=home)
    assert found == "env:CLAUDE_CODE_OAUTH_TOKEN" and secret not in found

    # 2. oauthAccount metadata in .claude.json -> detected, account values never returned.
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "secret-uuid"}}), encoding="utf-8")
    found = upstream.claude_oauth_detected(env={}, cwd=cwd, home=home)
    assert found == "config:oauthAccount" and "secret-uuid" not in found

    # Doctor reports PASS with transparent Authorization: Bearer guidance.
    ctx = doctor.Context({}, cwd=cwd, home=home)
    chk = doctor.check_claude_key(ctx)
    assert chk.status == doctor.PASS
    assert "OAuth" in chk.summary and "Authorization: Bearer" in chk.summary
    assert "secret-uuid" not in chk.summary and chk.details.get("oauth") == "config:oauthAccount"

    # 3. .credentials.json -> stat-checked ONLY, never opened!
    (home / ".claude.json").unlink()
    creds = home / ".claude" / ".credentials.json"
    creds.parent.mkdir()
    creds.write_text('{"claudeAiOauth":{"accessToken":"MUST_NOT_BE_READ"}}', encoding="utf-8")
    real_open = open

    def guard_open(file, *args, **kwargs):
        assert Path(file) != creds, ".credentials.json must never be opened!"
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guard_open)
    found = upstream.claude_oauth_detected(env={}, cwd=cwd, home=home)
    assert found == "file:.credentials.json"


# ---------------------------------------------------------------------------
# 6. Real Claude Code 2.1.328 end-to-end checks (merged --settings + Vertex/Foundry)
# ---------------------------------------------------------------------------


@needs_claude
def test_real_claude_merged_user_settings_and_vertex_foundry_interception(tmp_path):
    seen: list[tuple[str, str, dict[str, str]]] = []

    class Stub(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            _ = raw
            seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
            resp = json.dumps({
                "id": "msg_real", "type": "message", "role": "assistant",
                "model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "pong"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 50, "output_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        do_HEAD = do_GET = do_POST

        def log_message(self, *args: Any) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stub_url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        base_env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("ANTHROPIC_", "CLAUDE", "CONTEXTO_", "CLOUD_ML_", "VERTEX_"))}
        base_env.update({
            "HOME": str(tmp_path / "home"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"),
            "CONTEXTO_HOME": str(tmp_path / "store"),
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        (tmp_path / "home").mkdir()
        (tmp_path / "cfg").mkdir()

        # Case A: User --settings inline JSON merged + literal "-- --settings" prompt
        seen.clear()
        env_a = {**base_env, "ANTHROPIC_API_KEY": "sk-ant-api03-fake-key"}
        proc = subprocess.run(
            [sys.executable, "-m", "contexto", "run", "--upstream", stub_url, "--session", "real-s1",
             "claude", "--settings", '{"env":{"MY_CUSTOM_FLAG":"ok"}}',
             "-p", "--disable-slash-commands", "--setting-sources", "user",
             "--model", "claude-sonnet-4-6", "--", "--settings"],
            cwd=tmp_path, env=env_a, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert any(path.startswith("/v1/messages") for _, path, _ in seen), seen

        # Case B: Vertex AI intercepted via ANTHROPIC_VERTEX_BASE_URL
        seen.clear()
        env_v = {
            **base_env,
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH": "1",
            "ANTHROPIC_VERTEX_BASE_URL": stub_url,
            "ANTHROPIC_VERTEX_PROJECT_ID": "proj-test",
            "CLOUD_ML_REGION": "us-east5",
        }
        proc_v = subprocess.run(
            [sys.executable, "-m", "contexto", "run", "--session", "real-vertex",
             "claude", "-p", "--disable-slash-commands", "--setting-sources", "user",
             "--model", "claude-sonnet-4-6", "say hi"],
            cwd=tmp_path, env=env_v, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        assert proc_v.returncode == 0, proc_v.stderr + proc_v.stdout
        assert any(":streamRawPredict" in path or ":rawPredict" in path for _, path, _ in seen), seen
        log_v = [json.loads(line) for line in
                 (tmp_path / "store" / "sessions" / "real-vertex" / "log.jsonl").read_text().splitlines()]
        assert any(e.get("event") == "request" and e.get("api") == "messages" for e in log_v)

        # Case C: Microsoft Foundry intercepted via ANTHROPIC_FOUNDRY_BASE_URL
        seen.clear()
        env_f = {
            **base_env,
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "ANTHROPIC_FOUNDRY_BASE_URL": stub_url,
            "ANTHROPIC_FOUNDRY_API_KEY": "fake-foundry-key",
        }
        proc_f = subprocess.run(
            [sys.executable, "-m", "contexto", "run", "--session", "real-foundry",
             "claude", "-p", "--disable-slash-commands", "--setting-sources", "user",
             "--model", "claude-sonnet-4-6", "say hi"],
            cwd=tmp_path, env=env_f, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        assert proc_f.returncode == 0, proc_f.stderr + proc_f.stdout
        assert any(path.startswith("/v1/messages") for _, path, _ in seen), seen
        log_f = [json.loads(line) for line in
                 (tmp_path / "store" / "sessions" / "real-foundry" / "log.jsonl").read_text().splitlines()]
        assert any(e.get("event") == "request" and e.get("api") == "messages" for e in log_f)

        # Case D: Microsoft Foundry with ANTHROPIC_FOUNDRY_RESOURCE set (Contexto clears it in --settings
        # so the Foundry SDK does not fail with "baseURL and resource are mutually exclusive")
        seen.clear()
        env_fr = {
            **base_env,
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "ANTHROPIC_FOUNDRY_RESOURCE": "my-test-foundry-res",
            "ANTHROPIC_FOUNDRY_API_KEY": "fake-foundry-key",
        }
        proc_fr = subprocess.run(
            [sys.executable, "-m", "contexto", "run", "--upstream", stub_url, "--session", "real-foundry-res",
             "claude", "-p", "--disable-slash-commands", "--setting-sources", "user",
             "--model", "claude-sonnet-4-6", "say hi"],
            cwd=tmp_path, env=env_fr, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        assert proc_fr.returncode == 0, proc_fr.stderr + proc_fr.stdout
        assert any(path.startswith("/v1/messages") for _, path, _ in seen), seen
    finally:
        srv.shutdown()
        srv.server_close()


# Round 1 review security & edge-case regressions ------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "attacker.com#",
    "169.254.169.254/latest/meta-data/#",
    "evil.com:8080/x?",
    "user@evil.com",
    "a.b",
    "../escape",
    "-leading",
    "trailing-",
    "a" * 64,
    "https://evil.example",
    "us east5",
    "us-east5\n",
    "us-east5\u00ad",
])
def test_provider_region_and_resource_reject_url_injection(tmp_path, bad):
    with pytest.raises(ValueError) as exc_v:
        upstream.resolve_provider_upstream(
            "CLAUDE_CODE_USE_VERTEX",
            env={"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": bad},
            cwd=tmp_path, home=tmp_path, managed=tmp_path / "none.json",
        )
    assert bad not in str(exc_v.value)
    assert "CLOUD_ML_REGION" in str(exc_v.value)

    with pytest.raises(ValueError) as exc_f:
        upstream.resolve_provider_upstream(
            "CLAUDE_CODE_USE_FOUNDRY",
            env={"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": bad},
            cwd=tmp_path, home=tmp_path, managed=tmp_path / "none.json",
        )
    assert bad not in str(exc_f.value)
    assert "ANTHROPIC_FOUNDRY_RESOURCE" in str(exc_f.value)


def test_settings_rejects_huge_integers_nonfinite_and_echoes_no_raw_input(tmp_path):
    huge_json = '{"x": ' + "9" * 5000 + "}"
    with pytest.raises(launcher.LaunchError) as exc_inline:
        launcher.extract_user_settings(["--settings", huge_json])
    assert exc_inline.value.code == launcher.EXIT_USAGE
    assert "9999" not in str(exc_inline.value)

    huge_file = tmp_path / "huge.json"
    huge_file.write_text(huge_json, encoding="utf-8")
    with pytest.raises(launcher.LaunchError) as exc_file:
        launcher.extract_user_settings(["--settings", str(huge_file)])
    assert exc_file.value.code == launcher.EXIT_USAGE

    with pytest.raises(launcher.LaunchError) as exc_secret:
        launcher.extract_user_settings(["--settings", '{"secret_token_xyz": NaN}'])
    assert "secret_token_xyz" not in str(exc_secret.value)

    with pytest.raises(ValueError):
        launcher.write_private_json(tmp_path, "nan.json", {"bad": float("inf")})


def test_multiple_settings_merge_permissions_deny_and_ask_and_validate(tmp_path):
    remaining, merged = launcher.extract_user_settings([
        "--settings", json.dumps({"permissions": {"allow": ["Read"], "deny": ["Bash(rm:*)"], "ask": ["Write"]}}),
        "--settings", json.dumps({"permissions": {"allow": ["Grep", "Read"], "deny": ["WebFetch", "Bash(rm:*)"],
                                                  "ask": ["Edit", "Write"]}}),
        "-p", "hi",
    ])
    assert remaining == ["-p", "hi"]
    assert merged["permissions"]["allow"] == ["Read", "Grep"]
    assert merged["permissions"]["deny"] == ["Bash(rm:*)", "WebFetch"]
    assert merged["permissions"]["ask"] == ["Write", "Edit"]

    for bad_field in ("deny", "ask"):
        with pytest.raises(launcher.LaunchError):
            launcher.extract_user_settings(["--settings", json.dumps({"permissions": {bad_field: "not-a-list"}})])


def test_conflicting_intercepted_provider_precedence_and_trust_fail_closed(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    # Env enables Vertex, user settings explicitly disables it -> conflict, fail closed
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_USE_VERTEX": "0"}}))
    found = upstream.cloud_provider(
        env={"CLAUDE_CODE_USE_VERTEX": "1"}, cwd=tmp_path, home=home,
        managed=tmp_path / "none.json", allow_intercepted=True,
    )
    assert found is not None and found.conflicts
    assert upstream.active_cloud_provider(
        env={"CLAUDE_CODE_USE_VERTEX": "1"}, cwd=tmp_path, home=home, managed=tmp_path / "none.json",
    ) == (None, ())
    with pytest.raises(launcher.LaunchError) as exc:
        launcher.check_cloud_provider(tmp_path, {"CLAUDE_CODE_USE_VERTEX": "1"},
                                      home=home, managed=tmp_path / "none.json")
    assert exc.value.code == launcher.EXIT_USAGE and "consistent" in str(exc.value)

    # Project settings enables Vertex while CLAUDE_CODE_IGNORE_PERSISTED_TRUST=1 -> fail closed
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_USE_VERTEX": "1"}}))
    found_untrusted = upstream.cloud_provider(
        env={"CLAUDE_CODE_IGNORE_PERSISTED_TRUST": "1"}, cwd=proj, home=tmp_path / "empty_home",
        managed=tmp_path / "none.json", allow_intercepted=True,
    )
    assert found_untrusted is not None and found_untrusted.conflicts
    assert "CLAUDE_CODE_IGNORE_PERSISTED_TRUST" in upstream.cloud_provider_note(found_untrusted)


def test_resolve_provider_upstream_respects_managed_isolation(tmp_path):
    managed = tmp_path / "managed.json"
    managed.write_text(json.dumps({"env": {"CLOUD_ML_REGION": "europe-west4"}}))
    up = upstream.resolve_provider_upstream(
        "CLAUDE_CODE_USE_VERTEX", env={"CLAUDE_CODE_USE_VERTEX": "1"},
        cwd=tmp_path, home=tmp_path, managed=managed,
    )
    assert up.url == "https://europe-west4-aiplatform.googleapis.com/v1"


def test_toolcheck_deferred_listing_without_contexto_still_alerts_even_when_listed():
    class Heartbeat:
        def mcp_listed_at(self) -> float | None:
            return 55.0

    now = [0.0]
    tc = toolcheck.ToolCheck(Heartbeat(), clock=lambda: now[0], wall=lambda: 50.0)
    # A recognised deferred-tool listing that omits mcp__contexto__* (e.g. permissions.deny) MUST alert.
    denied_req = {
        "system": [{"type": "text", "text": (
            "The following deferred tools are available via ToolSearch:\nRead\nWebFetch\n"
        )}],
        "tools": [{"name": "ToolSearch"}, {"name": "Bash"}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert tc.observe(denied_req, "anthropic", main=True) == []
    now[0] = toolcheck.GRACE_S + 1
    events = tc.observe(denied_req, "anthropic", main=True)
    assert any(e.get("event") == "mcp_tools_missing" and e.get("server_listed") is True for e in events)

    # Rephrased 4.x removal <system-reminder> is also parsed and triggers missing alert.
    now[0] = 0.0
    tc2 = toolcheck.ToolCheck(Heartbeat(), clock=lambda: now[0], wall=lambda: 50.0)
    removed_req = {
        "system": [],
        "tools": [{"name": "ToolSearch"}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>\nDeferred tools available:\n"
                                     "mcp__contexto__contexto_recall\n</system-reminder>"},
            {"type": "text", "text": "<system-reminder>\nDeferred tools removed:\n"
                                     "mcp__contexto__contexto_recall\n</system-reminder>"},
            {"type": "text", "text": "hello"},
        ]}],
    }
    now[0] = toolcheck.GRACE_S + 1
    events2 = tc2.observe(removed_req, "anthropic", main=True)
    assert any(e.get("event") == "mcp_tools_missing" for e in events2)


def test_vertex_path_traversal_and_newlines_rejected_and_double_prefix_avoided():
    bad_paths = [
        "/v1/projects/../locations/us-east5/publishers/anthropic/models/claude-sonnet-4-6:rawPredict",
        "/projects/p1/locations/../publishers/anthropic/models/claude-sonnet-4-6:streamRawPredict",
        "/projects/p1/locations/us-east5/publishers/anthropic/models/..:rawPredict",
        "/projects/p1/locations/us-east5/publishers/anthropic/models/claude-sonnet-4-6:rawPredict\n",
        "/projects/p1%2fother/locations/us-east5/publishers/anthropic/models/claude-sonnet-4-6:rawPredict",
    ]
    for p in bad_paths:
        assert proxy.classify_path(p) == (None, None), p
        assert stats._is_messages_path(p) is False, p
        assert stats._is_count_tokens_path(p) is False, p

    assert proxy.classify_path(
        "/v1/projects/p1/locations/us-east5/publishers/anthropic/models/claude-sonnet-4@20250514:streamRawPredict"
    ) == ("messages", proxy.MESSAGES_PATH)
    assert proxy.is_vendor_host("myres.cognitiveservices.azure.com") is True
    assert proxy.is_vendor_host("us-east5-aiplatform.googleapis.com\n") is False

    # Proxy double-prefix guard: /v1/projects/... to upstream .../v1 does not produce /v1/v1/projects/...
    seen: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append(self.path)
            out = json.dumps({"id": "m", "type": "message", "role": "assistant", "content": [],
                              "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    store = MemoryStore(key=b"k" * 32)
    px_v = proxy.ContextoProxy(f"http://127.0.0.1:{srv.server_address[1]}/v1", store, proxy.identity_transform)
    px_f = proxy.ContextoProxy(f"http://127.0.0.1:{srv.server_address[1]}/anthropic", store, proxy.identity_transform)
    px_v.start()
    px_f.start()
    try:
        v_path = "/v1/projects/p/locations/us-east5/publishers/anthropic/models/claude-sonnet-4-6:rawPredict"
        req1 = urllib.request.Request(f"{px_v.url}{v_path}", data=b'{"messages":[]}',
                                      headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req1, timeout=5) as r:
            assert r.status == 200
        req2 = urllib.request.Request(f"{px_f.url}/anthropic/v1/messages", data=b'{"messages":[]}',
                                      headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req2, timeout=5) as r:
            assert r.status == 200
    finally:
        px_v.stop()
        px_f.stop()
        srv.shutdown()
        srv.server_close()
    assert seen == [v_path, "/anthropic/v1/messages"]


def test_oauth_guidance_line_and_doctor_compatible_copy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "u1"}}))
    custom_up = upstream.Upstream("https://gw.example.com", "env")
    vendor_up = upstream.Upstream("https://api.anthropic.com", "default")
    loop_up = upstream.Upstream("http://127.0.0.1:8317", "env")
    vertex_up = upstream.Upstream("https://us-east5-aiplatform.googleapis.com/v1", "vertex:env",
                                  base_url_key=upstream.VERTEX_BASE_URL_KEY, provider="CLAUDE_CODE_USE_VERTEX")

    note = launcher._oauth_guidance_line(custom_up, tmp_path, None, env={}, home=home)
    assert note is not None and "Authorization: Bearer" in note
    assert launcher._oauth_guidance_line(vendor_up, tmp_path, None, env={}, home=home) is None
    assert launcher._oauth_guidance_line(loop_up, tmp_path, None, env={}, home=home) is None
    assert launcher._oauth_guidance_line(vertex_up, tmp_path, None, env={}, home=home) is None
    assert launcher._oauth_guidance_line(custom_up, tmp_path, None,
                                         env={"ANTHROPIC_API_KEY": "sk-test"}, home=home) is None

    # Doctor copy distinguishes explicitly tested vs compatible patch version
    from contexto.commands import doctor
    monkeypatch.setattr(doctor.shutil, "which", lambda _c, path=None: "/bin/claude")
    monkeypatch.setattr(doctor, "read_version", lambda _p, _e: ("2.1.328", None))
    chk_tested = doctor.check_cli("claude", {})
    monkeypatch.setattr(doctor, "read_version", lambda _p, _e: ("2.1.350", None))
    chk_compat = doctor.check_cli("claude", {})
    assert "(a tested version)" in chk_tested.summary
    assert "(compatible; tested:" in chk_compat.summary and chk_compat.status == doctor.PASS

    # Semver hyphenated pre-release and +build metadata parse cleanly
    assert cliversions.is_compatible("claude", "2.1.329-rc-1+build.42") is True
    assert cliversions.is_compatible("claude", "2.1.302-rc-1") is False
