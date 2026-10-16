"""Responses API dialect tests (M2 A5). Item shapes come from codex-cli 0.154.0 with gpt-6-astra as
measured with zero-spend probes (spikes/m2/codex/); all content is synthetic."""

import copy

from contexto.canonical import canonical_bytes
from contexto.codex import RESPONSES, ResponsesDialect
from contexto.rewrite import MESSAGES, Config, RewriteEngine, rewrite
from contexto.store.memory import MemoryStore
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
        items += [prompt(f"prompt {t} {FILL}"), reasoning(t), call(t), output(t, f"out {t} {FILL}"), answer(f"answer {t}")]
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

import json
import os
import stat
import tomllib

import pytest

from contexto.codex import (CHATGPT_MODE, CHATGPT_UPSTREAM, KEY_HELPER_ENV, CodexLaunchError,
                            claude_api_key_helper, plan_codex)

PROXY = "http://127.0.0.1:43210"


def claude_home(tmp_path, helper="/synthetic/bin/key-helper --profile synthetic"):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
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
        assert config["mcp_servers"]["contexto"]["args"] == ["mcp", "--session", "S1"]
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
        assert tomllib.loads((launch.codex_home / "config.toml").read_text())["model_providers"]["contexto"]["auth"]["command"] == "/from/env"
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
    assert any("O4" in note for note in launch.notes)
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

import argparse
import io

from contexto.codex import add_arguments, run_codex


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

    def check_bound(self, bound, allow):
        if self.bound_error:
            raise FakeLaunchError(self.bound_error, 2)

    def options_from_args(self, args):
        return {"bound": args.bound}

    def engine_config(self, options):
        return {"bound": options["bound"]}

    def open_session(self, home, session_id, workdir):
        self.calls["session"] = session_id
        return "store"

    def start_proxy(self, store, upstream_url, upstream_source, config, observe_thinking):
        self.calls["upstream"] = upstream_url
        return self.proxy, "rewrite"

    def run_child(self, argv, env):
        home = env.get("CODEX_HOME")
        self.calls.update(argv=argv, home_existed=bool(home) and os.path.isdir(home), home=home)
        return self.child_code


def parse(*argv):
    parser = argparse.ArgumentParser()
    for name, kw in (("--bound", {"type": int, "default": 150_000}), ("--allow-large-bound", {"action": "store_true"}),
                     ("--window-fraction", {"type": float, "default": None}), ("--headroom-floor", {"type": int, "default": None}),
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
    assert fake.calls["argv"] == ["codex", "exec", "hi"] and fake.calls["home_existed"]
    assert not os.path.exists(fake.calls["home"]) and fake.proxy.stopped
    assert "session S9" in err.getvalue()


def test_run_codex_chatgpt_mode_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    args = parse("--codex-auth", "chatgpt", "--upstream", "https://u.invalid", "codex", "exec", "hi")
    fake = FakeLauncher()
    err = io.StringIO()
    assert run_codex(args, launcher=fake, stderr=err) == 0
    assert fake.calls["upstream"] == "https://chatgpt.com/backend-api/codex"
    assert "requires_openai_auth=true" in " ".join(fake.calls["argv"])
    assert fake.calls["home"] == os.environ.get("CODEX_HOME")  # the user's own CODEX_HOME, untouched
    assert "O4" in err.getvalue()
    refused = FakeLauncher(bound_error="bound too large")
    assert run_codex(parse("codex"), launcher=refused, stderr=io.StringIO()) == 2
