"""Responses API dialect tests (M2 A5). Item shapes come from codex-cli 0.154.0 with gpt-6-astra as
measured with zero-spend probes (spikes/m2/codex/); all content is synthetic."""

import argparse
import copy
import io
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from contexto.canonical import canonical_bytes
from contexto.codex import (
    CHATGPT_MODE,
    CHATGPT_UPSTREAM,
    KEY_HELPER_ENV,
    RESPONSES,
    CodexLaunchError,
    ResponsesDialect,
    add_arguments,
    claude_api_key_helper,
    plan_codex,
    run_codex,
)
from contexto.rewrite import MESSAGES, Config, RewriteEngine, rewrite
from contexto.store.memory import MemoryStore
from contexto.store.render import render_page
from contexto.tokens import TokenEstimator

FILL = "synthetic filler text " * 90
CONFIG = Config(bound=6_000, headroom_floor=500, pin_budget=200, index_budget=200)


def dev(text):
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def lead():
    """The measured first-request lead, before the user's first prompt."""
    return [
        {"type": "additional_tools", "id": "at_synthetic", "role": "developer",
         "tools": [{"type": "namespace", "name": "functions", "tools": []}]},
        dev("synthetic base instructions"),
        {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": "<skills_instructions>synthetic</skills_instructions>"},
            {"type": "input_text", "text": "<permissions instructions>synthetic</permissions instructions>"}]},
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>synthetic</environment_context>"}]},
    ]


def prompt(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def reasoning(i):
    return {"type": "reasoning", "id": f"rs_{i}", "summary": [], "encrypted_content": f"opaque-synthetic-{i}"}


def answer(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def call(i):
    return {"type": "function_call", "call_id": f"call_{i}", "name": "exec", "arguments": "{}"}


def output(i, text="synthetic output"):
    return {"type": "function_call_output", "call_id": f"call_{i}", "output": text}


def history(turns):
    items = lead()
    for t in range(turns):
        items += [prompt(f"prompt {t} {FILL}"), reasoning(t), call(t), output(t, f"out {t} {FILL}"),
                  answer(f"answer {t}")]
    return items


def request(items, **extra):
    return {"model": "gpt-6-astra", "stream": True, "store": False, "input": items,
            "prompt_cache_key": "synthetic-session", "include": ["reasoning.encrypted_content"], **extra}


def run(items, state=None, store=None, config=CONFIG):
    store = store if store is not None else MemoryStore(key=b"k" * 32)
    up, st, ev = rewrite(request(items), state, store, config, TokenEstimator(), RESPONSES)
    return up, st, ev, store


def names(events):
    return [e["event"] for e in events]


def test_turn_starts_lead_and_system_like_items():
    d = ResponsesDialect()
    items = lead() + [prompt("p0"), reasoning(0), answer("a0")]
    assert d.lead_count(items) == 4
    assert [d.is_turn_start(i) for i in items] == [False, False, False, False, True, False, False]
    assert all(d.is_system_like(i) for i in items[:4]) and not d.is_system_like(items[4])
    assert d.is_assistant(reasoning(1)) and d.is_assistant(call(1)) and d.is_assistant(answer("x"))
    assert not d.is_assistant(output(1))
    assert d.classify_system(items, 1) == "session_start"
    assert d.classify_system(items + [dev("later context")], 7) == "carried_text"


def test_first_request_passes_through_unchanged():
    items = lead() + [prompt("first prompt")]
    req = request(items)
    before = copy.deepcopy(req)
    up, st, ev = rewrite(req, None, MemoryStore(key=b"k" * 32), Config(), TokenEstimator(), RESPONSES)
    assert req == before and up["input"] == items and st.k == 0
    for field in ("model", "store", "stream", "prompt_cache_key", "include"):
        assert up[field] == req[field]


def test_rotation_keeps_lead_first_merges_preamble_and_drops_old_reasoning():
    items = history(8) + [prompt("new prompt")]
    up, st, ev, store = run(items)
    assert "rotation" in names(ev) and st.k > 4
    out = up["input"]
    assert out[:4] == lead()  # the lead stays first, unchanged
    first = out[4]
    assert first["role"] == "user" and first["content"][0]["type"] == "input_text"
    assert first["content"][1:] == items[st.k]["content"]  # preamble part, then the prompt
    assert not any(i.get("type") == "reasoning" for i in out)  # existed at rotation: dropped
    calls = {i["call_id"] for i in out if i.get("type") == "function_call"}
    outputs = {i["call_id"] for i in out if i.get("type") == "function_call_output"}
    assert calls == outputs  # turns are paged whole, so pairs stay together
    pages = "".join(canonical_bytes(p).decode() for p in store.pages.values())
    assert "environment_context" not in pages and "opaque-synthetic-0" in pages  # pages keep reasoning


def test_reasoning_after_rotation_passes_untouched_and_prefix_is_stable():
    store = MemoryStore(key=b"k" * 32)
    items = history(8) + [prompt("new prompt")]
    up1, st, _, _ = run(items, store=store)
    later = items + [reasoning(99), call(99), output(99)]
    up2, _, ev2, _ = run(later, state=st, store=store)
    assert "rotation" not in names(ev2)
    assert up2["input"][-3] is later[-3]  # added after the rotation: kept
    n = len(up1["input"])
    assert [canonical_bytes(i) for i in up2["input"][:n]] == [canonical_bytes(i) for i in up1["input"]]


def test_store_failure_enters_degraded_mode_with_the_responses_dialect():
    """Rule 14 on the Responses dialect (A2d after the #43 seam): the input list is extended append-only."""
    class FailingStore(MemoryStore):
        def page_out(self, turns, first_turn):
            raise OSError("synthetic")

    items = history(8) + [prompt("new prompt")]
    up, st, ev, _ = run(items, store=FailingStore(key=b"k" * 32))
    assert names(ev)[:2] == ["store_error", "degraded_enter"] and "rotation" not in names(ev)
    assert st.degraded and up["input"] == items and ev[-1]["degraded"] is True
    loop = items + [reasoning(99), call(99), output(99)]
    up2, _, ev2, _ = run(loop, state=st, store=FailingStore(key=b"k" * 32))
    assert up2["input"] == loop and ev2[-1]["degraded"] is True and "store_error" not in names(ev2)


def test_room_condition_rotates_the_responses_dialect_too():
    """A12 (contract PR #115) on the Responses dialect: the room alone fires a rotation, logged with
    reason min_turn_room, and the lead items stay first."""
    items = history(6) + [prompt("new prompt")]
    quiet = Config(bound=10_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=0)
    _, _, ev, _ = run(items, config=quiet)
    assert "rotation" not in names(ev)  # the size trigger alone would not rotate here
    roomy = Config(bound=10_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=2_000)
    up, st, ev, _ = run(items, config=roomy)
    assert [e["reason"] for e in ev if e["event"] == "rotation"] == ["min_turn_room"] and st.k > 4
    assert up["input"][:4] == lead()
    assert roomy.bound - ev[-1]["sent_estimate"] - roomy.headroom_floor >= roomy.min_turn_room


def test_no_rotation_inside_a_tool_loop():
    items = history(10) + [prompt("go"), call(50), output(50, FILL * 3)]
    up, st, ev, store = run(items)
    assert "rotation" not in names(ev) and not store.pages and up["input"] == items
    assert ev[-1].get("over_bound") is True


def test_usage_reads_input_tokens_as_the_total():
    assert RESPONSES.usage_tokens({"input_tokens": 1_000, "input_tokens_details": {"cached_tokens": 900},
                                   "output_tokens": 5}) == 1_000


def test_engine_dispatches_responses_paths_with_separate_state():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    items = lead() + [prompt("codex prompt")]
    for path in ("/responses", "/v1/responses"):
        up, headers, ev = engine(request(items), {"authorization": "x"}, path)
        assert up["input"] == items and headers == {"authorization": "x"} and ev
    anthropic = {"model": "m", "messages": [{"role": "user", "content": "claude prompt"}]}
    engine(anthropic, {}, MESSAGES)
    assert sorted(k.split(":")[0] for k in engine.states) == ["anthropic", "responses"]
    engine.observe_usage("/responses", {"input_tokens": 777, "input_tokens_details": {"cached_tokens": 700}},
                         request(items))
    responses_state = next(v for k, v in engine.states.items() if k.startswith("responses:"))
    assert responses_state.last_input_tokens == 777
    assert engine(request(items), {}, "/models") == (request(items), {}, [])


# Launch planning (D22) ------------------------------------------------------------------------------

PROXY = "http://127.0.0.1:43210"


def claude_home(tmp_path, helper="/synthetic/bin/key-helper --profile synthetic"):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"apiKeyHelper": helper}))
    return home


def test_api_key_mode_writes_a_private_temporary_codex_home(tmp_path):
    home = claude_home(tmp_path)
    launch = plan_codex(["exec", "hi"], PROXY, "S1", env={}, cwd=tmp_path, home=home,
                        claude_upstream="https://owner-proxy.invalid")
    try:
        root = launch.codex_home
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        config_path = root / "config.toml"
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        config = tomllib.loads(config_path.read_text())
        provider = config["model_providers"]["contexto"]
        assert config["model_provider"] == "contexto"
        assert provider["base_url"] == PROXY and provider["wire_api"] == "responses"
        assert provider["supports_websockets"] is False
        assert provider["auth"] == {"command": "/synthetic/bin/key-helper", "args": ["--profile", "synthetic"]}
        assert config["features"]["enable_request_compression"] is False
        assert config["mcp_servers"]["contexto"]["command"] == sys.executable
        assert config["mcp_servers"]["contexto"]["args"] == ["-P", "-m", "contexto", "mcp", "--session", "S1"]
        # A6 measured exec mode refusing MCP calls without this (approval policy "never").
        assert config["mcp_servers"]["contexto"]["default_tools_approval_mode"] == "approve"
        assert launch.env["CODEX_HOME"] == str(root) and launch.argv == ["codex", "exec", "hi"]
        assert launch.upstream == "https://owner-proxy.invalid/v1"
    finally:
        launch.cleanup()
    assert not root.exists()


def test_key_helper_resolution_order(tmp_path):
    home = claude_home(tmp_path, helper="/from/claude/settings")
    assert claude_api_key_helper(tmp_path, home) == "/from/claude/settings"
    env = {KEY_HELPER_ENV: "/from/env"}
    launch = plan_codex([], PROXY, "S", env=env, cwd=tmp_path, home=home, upstream="https://u.invalid/v1")
    try:
        assert (tomllib.loads((launch.codex_home / "config.toml").read_text())
                ["model_providers"]["contexto"]["auth"]["command"] == "/from/env")
    finally:
        launch.cleanup()
    explicit = plan_codex([], PROXY, "S", env=env, cwd=tmp_path, home=home, upstream="https://u.invalid/v1",
                          key_helper="/from/flag")
    try:
        assert "/from/flag" in (explicit.codex_home / "config.toml").read_text()
    finally:
        explicit.cleanup()


def test_api_key_mode_without_a_helper_or_upstream_fails_cleanly(tmp_path):
    empty = tmp_path / "empty-home"
    empty.mkdir()
    with pytest.raises(CodexLaunchError, match="key helper"):
        plan_codex([], PROXY, "S", env={}, cwd=tmp_path, home=empty, upstream="https://u.invalid/v1")
    with pytest.raises(CodexLaunchError, match="upstream"):
        plan_codex([], PROXY, "S", env={}, cwd=tmp_path, home=claude_home(tmp_path))


def test_chatgpt_mode_uses_overrides_only(tmp_path):
    launch = plan_codex(["exec", "hi"], PROXY, "S2", auth_mode=CHATGPT_MODE, env={"PATH": "/bin"},
                        cwd=tmp_path, home=tmp_path)
    assert launch.codex_home is None and "CODEX_HOME" not in launch.env
    argv = " ".join(launch.argv)
    assert "requires_openai_auth=true" in argv and "supports_websockets=false" in argv
    assert "model_provider=contexto" in argv and "features.enable_request_compression=false" in argv
    assert launch.argv[-2:] == ["exec", "hi"] and launch.upstream == CHATGPT_UPSTREAM
    assert any("D35" in note and "provider's terms" in note for note in launch.notes)
    overrides = dict(launch.argv[i + 1].split("=", 1) for i, a in enumerate(launch.argv) if a == "-c")
    approval = overrides["mcp_servers.contexto.default_tools_approval_mode"]
    assert tomllib.loads(f"v = {approval}")["v"] == "approve"  # a valid TOML value, as codex parses -c
    launch.cleanup()  # nothing to remove


def test_config_never_contains_a_key(tmp_path):
    os.environ.pop("OPENAI_API_KEY", None)
    launch = plan_codex([], PROXY, "S", env={"OPENAI_API_KEY": "sk-synthetic-not-a-key"}, cwd=tmp_path,
                        home=claude_home(tmp_path), upstream="https://u.invalid/v1")
    try:
        text = (launch.codex_home / "config.toml").read_text()
        assert "sk-synthetic" not in text and "api_key" not in text.lower()
    finally:
        launch.cleanup()


# run_codex with a fake launcher (builder-1's contexto.launcher API) ---------------------------------


class FakeLaunchError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class FakeProxy:
    url = "http://127.0.0.1:50505"

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeLauncher:
    LaunchError = FakeLaunchError

    def __init__(self, bound_error=None, child_code=0):
        self.bound_error, self.child_code = bound_error, child_code
        self.proxy = FakeProxy()
        self.calls = {}

    def check_options(self, options):
        if self.bound_error:
            raise FakeLaunchError(self.bound_error, 2)

    def options_from_args(self, args):
        return {"bound": args.bound}

    def engine_config(self, options):
        return {"bound": options["bound"]}

    def open_session(self, home, session_id, workdir):
        self.calls["session"] = session_id
        return "store"

    def start_proxy(self, store, upstream_url, upstream_source, config, observe_thinking, *, stderr=None):
        self.calls["upstream"] = upstream_url
        self.calls["upstream_source"] = upstream_source
        return self.proxy, "rewrite"

    def report_missing_tools(self, proxy):
        self.calls["reported_after_stop"] = proxy.stopped

    EXIT_INTERRUPTED = 130
    interrupted = False

    def check_cli_version(self, cli, binary, store, *, enabled=True, stderr=None):
        self.calls["version_check"] = (cli, binary, enabled)
        return not self.interrupted

    def run_child(self, argv, env, *, stderr=None):
        home = env.get("CODEX_HOME")
        self.calls.update(argv=argv, home_existed=bool(home) and os.path.isdir(home), home=home)
        return self.child_code


def parse(*argv):
    parser = argparse.ArgumentParser()
    for name, kw in (("--bound", {"type": int, "default": 150_000}), ("--allow-large-bound", {"action": "store_true"}),
                     ("--window-fraction", {"type": float, "default": None}),
                     ("--headroom-floor", {"type": int, "default": None}),
                     ("--session", {"default": None}), ("--upstream", {"default": None}),
                     ("--observe-thinking", {"action": "store_true"})):
        parser.add_argument(name, **kw)
    add_arguments(parser)
    parser.add_argument("cli")
    parser.add_argument("cli_args", nargs=argparse.REMAINDER)
    return parser.parse_args(list(argv))


def test_run_codex_api_key_mode_runs_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    args = parse("--session", "S9", "--upstream", "https://owner-proxy.invalid",
                 "--codex-key-helper", "/synthetic/helper", "codex", "exec", "hi")
    fake = FakeLauncher(child_code=7)
    err = io.StringIO()
    assert run_codex(args, launcher=fake, stderr=err) == 7
    assert fake.calls["upstream"] == "https://owner-proxy.invalid/v1" and fake.calls["session"] == "S9"
    assert fake.calls["upstream_source"] == "claude-flag+/v1"  # D35: where the URL came from, never the URL
    assert "upstream claude-flag+/v1" in err.getvalue() and "owner-proxy" not in err.getvalue()
    assert fake.calls["argv"] == ["codex", "exec", "hi"] and fake.calls["home_existed"]
    home = fake.calls["home"]
    assert home.endswith(os.path.join("sessions", "S9", "codex-home"))  # under the Contexto session dir
    assert os.path.isdir(home) and not os.path.exists(os.path.join(home, "config.toml"))  # rollouts kept
    assert fake.proxy.stopped and fake.calls["reported_after_stop"]
    assert "session S9" in err.getvalue()


def test_run_codex_survives_an_odd_claude_config_dir(tmp_path, monkeypatch):
    """reviewer-A, round 2: run_codex resolves Claude's upstream even with --codex-upstream, so an
    unknown ~user in CLAUDE_CONFIG_DIR must not raise out of it."""
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~nosuchuser-contexto-test/cfg")
    monkeypatch.chdir(tmp_path)
    args = parse("--codex-upstream", "https://cpa.invalid/v1", "--codex-key-helper", "/synthetic/helper",
                 "codex", "exec", "hi")
    fake = FakeLauncher()
    err = io.StringIO()
    assert run_codex(args, launcher=fake, stderr=err) == 0
    assert fake.calls["upstream"] == "https://cpa.invalid/v1" and fake.calls["upstream_source"] == "codex-flag"


def test_run_codex_chatgpt_mode_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    args = parse("--codex-auth", "chatgpt", "--upstream", "https://u.invalid", "codex", "exec", "hi")
    fake = FakeLauncher()
    err = io.StringIO()
    assert run_codex(args, launcher=fake, stderr=err) == 0
    assert fake.calls["upstream"] == "https://chatgpt.com/backend-api/codex"
    assert fake.calls["upstream_source"] == "chatgpt-default"
    assert "requires_openai_auth=true" in " ".join(fake.calls["argv"])
    assert fake.calls["home"] == os.environ.get("CODEX_HOME")  # the user's own CODEX_HOME, untouched
    assert "D35" in err.getvalue() and "CLIProxyAPI" in err.getvalue()
    refused = FakeLauncher(bound_error="bound too large")
    assert run_codex(parse("codex"), launcher=refused, stderr=io.StringIO()) == 2


def test_run_codex_with_the_real_launcher_helpers(tmp_path, monkeypatch):
    """PR #57's launcher: real option checks, session, and proxy on 127.0.0.1; only the child is faked."""
    from contexto import launcher

    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    seen = {}

    def fake_child(argv, env=None, *, stderr=None):
        seen.update(argv=argv, home=env.get("CODEX_HOME"), stderr=stderr)
        return 5

    monkeypatch.setattr(launcher, "run_child", fake_child)
    err = io.StringIO()
    args = parse("--session", "S7", "--upstream", "https://owner-proxy.invalid",
                 "--codex-key-helper", "/synthetic/helper", "codex", "exec", "hi")
    assert run_codex(args, stderr=err) == 5
    assert seen["argv"][0] == "codex" and seen["home"].endswith(os.path.join("S7", "codex-home"))
    assert seen["stderr"] is err and "session S7" in err.getvalue()
    bad = parse("--window-fraction", "1.5", "codex")
    err = io.StringIO()
    assert run_codex(bad, stderr=err) == 2 and "--window-fraction" in err.getvalue()



def test_run_codex_checks_the_codex_binary_unless_silenced(tmp_path, monkeypatch):
    # D38: after the session line, before Codex starts; --no-version-check turns it off.
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    for enabled in (True, False):
        args = parse("--codex-key-helper", "/synthetic/helper", "codex", "exec", "hi")
        if not enabled:
            args.no_version_check = True
        fake = FakeLauncher()
        assert run_codex(args, launcher=fake, stderr=io.StringIO()) == 0
        assert fake.calls["version_check"] == ("codex", "codex", enabled)
    fake = FakeLauncher()
    fake.interrupted = True  # Ctrl+C during the check: codex never runs, the proxy still stops
    assert run_codex(parse("--codex-key-helper", "/synthetic/helper", "codex", "exec", "hi"), launcher=fake,
                     stderr=io.StringIO()) == 130
    assert "argv" not in fake.calls and fake.proxy.stopped and fake.calls["reported_after_stop"]


def test_tag_leading_user_prompts_stay_turn_starts():
    from contexto.codex import CONTEXT_SECTIONS_CODEX_VERSION
    assert CONTEXT_SECTIONS_CODEX_VERSION == "0.163.0"  # measured on 0.154.0, rechecked on 0.163.0 (D38)
    d = ResponsesDialect()
    for text in ("<div>fix this layout</div>", "<template>synthetic</template>", "<T>generic question",
                 "<environment_context_extra>not measured", "# AGENTS.md is a file name I mention"):
        item = prompt(text)
        assert d.is_turn_start(item) and not d.is_system_like(item), text
    for text in ("<environment_context>synthetic</environment_context>",
                 "<permissions instructions>synthetic</permissions instructions>",
                 "<skills_instructions>x</skills_instructions>", "<collaboration_mode>x</collaboration_mode>",
                 "<multi_agent_role>x</multi_agent_role>", "<multi_agent_mode>x</multi_agent_mode>",
                 "# AGENTS.md instructions for /synthetic/dir"):
        item = prompt(text)
        assert d.is_system_like(item) and not d.is_turn_start(item), text


def test_session_dir_codex_home_keeps_rollouts_and_removes_config(tmp_path):
    home_dir = tmp_path / "ctx" / "sessions" / "S3" / "codex-home"
    launch = plan_codex([], PROXY, "S3", env={}, cwd=tmp_path, home=claude_home(tmp_path),
                        upstream="https://u.invalid/v1", codex_home_dir=home_dir)
    assert launch.codex_home == home_dir and stat.S_IMODE(home_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((home_dir / "config.toml").stat().st_mode) == 0o600
    (home_dir / "sessions").mkdir()
    (home_dir / "sessions" / "rollout-synthetic.jsonl").write_text("{}")
    launch.cleanup()
    assert not (home_dir / "config.toml").exists() and (home_dir / "sessions" / "rollout-synthetic.jsonl").exists()
    again = plan_codex([], PROXY, "S3", env={}, cwd=tmp_path, home=claude_home(tmp_path, helper="/h2"),
                       upstream="https://u.invalid/v1", codex_home_dir=home_dir)  # a resumed session reuses it
    try:
        assert "/h2" in (home_dir / "config.toml").read_text()
    finally:
        again.cleanup()


def test_retained_snapshot_on_the_responses_dialect():
    """A14 (contract PR #125) for Codex: message text, calls as tool_use, call outputs as tool_result,
    rendered as pages render them; the lead, context sections, and reasoning are left out. The engine
    publishes it through the /responses path, once per request, before the room."""
    from contexto.rewrite import retained_entries

    items = lead() + [prompt("prompt 0"), reasoning(0),
                      {"type": "function_call", "call_id": "c0", "name": "exec", "arguments": '{"cmd": "ls", "a": 1}'},
                      output(0, "chain link 3 at node 12"), answer("answer 0")]
    entries = retained_entries(items, 0, RESPONSES)
    assert [(e["turn"], e["message_index"], e["role"], e["text"]) for e in entries] == [
        (1, 4, "user", "prompt 0"),
        (1, 6, "assistant", 'tool_use exec {"a": 1, "cmd": "ls"}'),
        (1, 7, "tool_result", "tool_result:\nchain link 3 at node 12"),
        (1, 8, "assistant", "answer 0")]
    assert "opaque-synthetic" not in json.dumps(entries) and "environment_context" not in json.dumps(entries)

    class Recording(MemoryStore):
        def set_retained(self, epoch, k, turns):
            self.snapshots = [*getattr(self, "snapshots", []), (epoch, k, turns)]

    store = Recording(key=b"k" * 32)
    engine = RewriteEngine(store, config=Config(bound=1_000_000))
    engine(request(items + [prompt("prompt 1")]), {}, "/v1/responses")
    assert len(store.snapshots) == 1 and store.snapshots[0][2][-1]["text"] == "prompt 1"


def test_mcp_server_gets_contexto_home_in_api_key_mode(tmp_path, monkeypatch):
    """Packet A18: Codex starts MCP servers with a restricted environment, so the contexto server is
    given CONTEXTO_HOME explicitly; without it the server opened ~/.contexto, a different store."""
    store_home = tmp_path / 'store home with "quotes" and spaces'
    launch = plan_codex([], PROXY, "S1", env={}, cwd=tmp_path, home=claude_home(tmp_path),
                        upstream="https://u.invalid/v1", contexto_home=store_home)
    try:
        config = tomllib.loads((launch.codex_home / "config.toml").read_text())
        assert config["mcp_servers"]["contexto"]["env"] == {"CONTEXTO_HOME": str(store_home)}
    finally:
        launch.cleanup()

    def mcp_home(env):
        launch = plan_codex([], PROXY, "S1", env=env, cwd=tmp_path, home=claude_home(tmp_path),
                            upstream="https://u.invalid/v1")
        try:
            return tomllib.loads((launch.codex_home / "config.toml").read_text())["mcp_servers"]["contexto"]["env"]
        finally:
            launch.cleanup()

    # Always passed, the default home too, and read from the env mapping plan_codex is given.
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "process-env-ignored"))
    assert mcp_home({}) == {"CONTEXTO_HOME": str(Path.home() / ".contexto")}
    assert mcp_home({"CONTEXTO_HOME": str(tmp_path / "from-env")}) == {"CONTEXTO_HOME": str(tmp_path / "from-env")}
    # A relative home is made absolute, as the proxy's store opens it (Codex starts the server elsewhere).
    monkeypatch.chdir(tmp_path)
    assert mcp_home({"CONTEXTO_HOME": "rel-home"}) == {"CONTEXTO_HOME": str(tmp_path / "rel-home")}


def test_mcp_server_gets_contexto_home_in_chatgpt_mode(tmp_path):
    store_home = tmp_path / "store home"
    launch = plan_codex(["exec", "hi"], PROXY, "S2", auth_mode=CHATGPT_MODE, env={"PATH": "/bin"},
                        cwd=tmp_path, home=tmp_path, contexto_home=store_home)
    overrides = dict(launch.argv[i + 1].split("=", 1) for i, a in enumerate(launch.argv) if a == "-c")
    value = overrides["mcp_servers.contexto.env.CONTEXTO_HOME"]
    assert tomllib.loads(f"v = {value}")["v"] == str(store_home)  # a valid TOML string, as codex parses -c


def test_mcp_server_command_does_not_depend_on_path(tmp_path):
    """Codex resolves an MCP server's command on PATH and, when it cannot start it, runs the turn
    without the server's tools and without an error. A bare ``contexto`` broke recall for any run
    whose PATH lacked the executable (``.venv/bin/contexto run codex`` without the venv activated:
    the stub dry run saw no contexto tools in code mode's ALL_TOOLS). The server is started the way
    the Claude path starts it (launcher.mcp_payload): this interpreter, ``-P -m contexto``."""
    expected = ["-P", "-m", "contexto", "mcp", "--session", "S3"]
    launch = plan_codex([], PROXY, "S3", env={"PATH": "/nonexistent"}, cwd=tmp_path, home=claude_home(tmp_path),
                        upstream="https://u.invalid/v1")
    try:
        server = tomllib.loads((launch.codex_home / "config.toml").read_text())["mcp_servers"]["contexto"]
        assert server["command"] == sys.executable and Path(server["command"]).is_absolute()
        assert server["args"] == expected
    finally:
        launch.cleanup()
    chatgpt = plan_codex(["exec", "hi"], PROXY, "S3", auth_mode=CHATGPT_MODE, env={"PATH": "/nonexistent"},
                         cwd=tmp_path, home=tmp_path)
    overrides = dict(chatgpt.argv[i + 1].split("=", 1) for i, a in enumerate(chatgpt.argv) if a == "-c")
    assert tomllib.loads(f"v = {overrides['mcp_servers.contexto.command']}")["v"] == sys.executable
    assert tomllib.loads(f"v = {overrides['mcp_servers.contexto.args']}")["v"] == expected


def test_chatgpt_mode_mcp_home_is_absolute_for_a_relative_contexto_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    launch = plan_codex(["exec", "hi"], PROXY, "S2", auth_mode=CHATGPT_MODE, env={"CONTEXTO_HOME": "rel-home"},
                        cwd=tmp_path, home=tmp_path)
    overrides = dict(launch.argv[i + 1].split("=", 1) for i, a in enumerate(launch.argv) if a == "-c")
    assert (tomllib.loads(f"v = {overrides['mcp_servers.contexto.env.CONTEXTO_HOME']}")["v"]
            == str(tmp_path / "rel-home"))


def test_mcp_server_ignores_a_contexto_package_in_the_working_directory(tmp_path):
    """Claude Code and Codex start MCP servers in the user's project. ``python -m contexto`` would
    put that directory first on sys.path, so a project with its own ``contexto/__main__.py`` would
    run in place of the installed server, outside the CLI's sandbox and approvals. ``-P`` keeps the
    working directory off sys.path (Python 3.11+)."""
    import subprocess

    from contexto.launcher import mcp_payload

    project = tmp_path / "project"
    (project / "contexto").mkdir(parents=True)
    (project / "contexto" / "__init__.py").write_text("")
    (project / "contexto" / "__main__.py").write_text("print('SHADOWED')\n")
    claude = mcp_payload("S4", tmp_path / "home")["mcpServers"]["contexto"]
    launch = plan_codex([], PROXY, "S4", env={"PATH": "/nonexistent"}, cwd=tmp_path, home=claude_home(tmp_path),
                        upstream="https://u.invalid/v1")
    try:
        codex = tomllib.loads((launch.codex_home / "config.toml").read_text())["mcp_servers"]["contexto"]
    finally:
        launch.cleanup()
    for server in (claude, codex):
        args = server["args"]
        prefix = args[:args.index("mcp")]  # the interpreter flags and module, without the subcommand
        out = subprocess.run([server["command"], *prefix, "--version"], cwd=project, capture_output=True,
                             text=True, timeout=30, env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        assert out.returncode == 0, out.stderr
        assert out.stdout.startswith("contexto ") and "SHADOWED" not in out.stdout


def test_call_arguments_nested_too_deep_for_json_render_as_their_string():
    deep = "[" * 1_000_000 + "]" * 1_000_000  # beyond what json.loads parses
    item = {"type": "function_call", "call_id": "call_0", "name": "exec", "arguments": deep}
    assert RESPONSES.retained_blocks(item) == [("assistant", {"type": "tool_use", "name": "exec", "input": deep})]
    items = history(8) + [prompt("new prompt")]
    items[items.index(call(0))] = item
    up, st, ev, store = run(items, config=Config(bound=6_000, headroom_floor=500, pin_budget=200, index_budget=2_000))
    assert "rotation" in names(ev) and st.k > 4
    page = next(p for p in store.pages.values() if item in p["messages"])
    assert render_page(page).count("[") >= 1_000_000  # the arguments, shown as the text they are
