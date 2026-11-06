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

from contexto.canonical import MAX_JSON_DEPTH, canonical_bytes
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


@pytest.fixture(autouse=True)
def isolated_from_the_checkout(tmp_path, monkeypatch):
    """No test here may read the developer's Claude settings or upstream: in a linked worktree the
    upstream resolution reads the main checkout's .claude/settings.local.json, which CI does not have
    (reviewer A, PR #236)."""
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "claude-config").mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    for name in ("ANTHROPIC_BASE_URL", "CONTEXTO_CODEX_UPSTREAM", "CONTEXTO_CODEX_KEY_HELPER", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)


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
        # Run through a shell, as Claude Code runs apiKeyHelper (D54).
        assert provider["auth"] == {"command": "/bin/sh",
                                    "args": ["-c", "/synthetic/bin/key-helper --profile synthetic"]}
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
                ["model_providers"]["contexto"]["auth"]["args"] == ["-c", "/from/env"])
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
    assert any("provider's terms" in note and "cliproxyapi.md" in note for note in launch.notes)
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


class FakeStore:
    def __init__(self):
        self.events = []

    def log(self, event):
        self.events.append(event)


class FakeLauncher:
    LaunchError = FakeLaunchError
    EXIT_NOT_FOUND = 127
    EXIT_PROXY = 1

    def __init__(self, bound_error=None, child_code=0):
        self.bound_error, self.child_code = bound_error, child_code
        self.proxy = FakeProxy()
        self.calls = {}

    def check_options(self, options):
        if self.bound_error:
            raise FakeLaunchError(self.bound_error, 2)

    def options_from_args(self, args):
        from contexto.launcher import options_from_args
        return options_from_args(args)

    def engine_config(self, options):
        return {"bound": options.bound}

    def start_warning(self, bound, headroom_floor, overhead):
        from contexto.launcher import start_warning
        return start_warning(bound, headroom_floor, overhead)

    def open_session(self, home, session_id, workdir):
        self.calls["session"] = session_id
        self.store = FakeStore()
        return self.store

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


@pytest.fixture
def codex_bin(tmp_path, monkeypatch):
    """A stub ``codex`` first on PATH (CI has no Codex): prints a version, and its exit code otherwise."""
    bindir = tmp_path / "codex-bin"
    bindir.mkdir()
    stub = bindir / "codex"
    stub.write_text('#!/bin/sh\n'
                    'case "$1" in\n'
                    '--version) [ -n "$STUB_CODEX_NO_VERSION" ] && exit 1\n'
                    '  echo "codex-cli ${STUB_CODEX_VERSION:-0.166.0}" ;;\n'
                    '--help) printf "%s\\n" "${STUB_CODEX_HELP:-Usage: codex [OPTIONS] [PROMPT]}" ;;\n'
                    'esac\n'
                    'exit 0\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return str(stub)


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


def test_run_codex_api_key_mode_runs_and_cleans_up(tmp_path, monkeypatch, codex_bin):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    args = parse("--session", "S9", "--upstream", "https://owner-proxy.invalid",
                 "--codex-key-helper", "/synthetic/helper", "codex", "exec", "hi")
    fake = FakeLauncher(child_code=7)
    err = io.StringIO()
    assert run_codex(args, launcher=fake, stderr=err) == 7
    assert fake.calls["upstream"] == "https://owner-proxy.invalid/v1" and fake.calls["session"] == "S9"
    assert fake.calls["upstream_source"] == "claude-flag+/v1"  # D35: where the URL came from, never the URL
    assert "upstream claude-flag+/v1" in err.getvalue() and "owner-proxy" not in err.getvalue()
    assert fake.calls["argv"] == [codex_bin, "exec", "hi"] and fake.calls["home_existed"]
    home = fake.calls["home"]
    assert home.endswith(os.path.join("sessions", "S9", "codex-home"))  # under the Contexto session dir
    assert os.path.isdir(home) and not os.path.exists(os.path.join(home, "config.toml"))  # rollouts kept
    assert fake.proxy.stopped and fake.calls["reported_after_stop"]
    assert "session S9" in err.getvalue()


def test_run_codex_survives_an_odd_claude_config_dir(tmp_path, monkeypatch, codex_bin):
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


def test_run_codex_chatgpt_mode_and_errors(tmp_path, monkeypatch, codex_bin):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    args = parse("--codex-auth", "chatgpt", "--upstream", "https://u.invalid", "codex", "exec", "hi")
    fake = FakeLauncher()
    err = io.StringIO()
    assert run_codex(args, launcher=fake, stderr=err) == 0
    assert fake.calls["upstream"] == "https://chatgpt.com/backend-api/codex"
    assert fake.calls["upstream_source"] == "chatgpt-default"
    assert "requires_openai_auth=true" in " ".join(fake.calls["argv"])
    assert fake.calls["home"] == os.environ.get("CODEX_HOME")  # the user's own CODEX_HOME, untouched
    assert "provider's terms" in err.getvalue() and "CLIProxyAPI" in err.getvalue()
    refused = FakeLauncher(bound_error="bound too large")
    assert run_codex(parse("codex"), launcher=refused, stderr=io.StringIO()) == 2


def test_run_codex_with_the_real_launcher_helpers(tmp_path, monkeypatch, codex_bin):
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
    assert seen["argv"][0] == codex_bin and seen["home"].endswith(os.path.join("S7", "codex-home"))
    assert seen["stderr"] is err and "session S7" in err.getvalue()
    bad = parse("--window-fraction", "1.5", "codex")
    err = io.StringIO()
    assert run_codex(bad, stderr=err) == 2 and "--window-fraction" in err.getvalue()



def test_run_codex_checks_the_codex_binary_unless_silenced(tmp_path, monkeypatch, codex_bin):
    # D38: after the session line, before Codex starts; --no-version-check turns it off.
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    for enabled in (True, False):
        args = parse("--upstream", "http://127.0.0.1:9", "--codex-key-helper", "/synthetic/helper", "codex",
                     "exec", "hi")
        if not enabled:
            args.no_version_check = True
        fake = FakeLauncher()
        assert run_codex(args, launcher=fake, stderr=io.StringIO()) == 0
        assert fake.calls["version_check"] == ("codex", codex_bin, enabled)
    fake = FakeLauncher()
    fake.interrupted = True  # Ctrl+C during the check: codex never runs, the proxy still stops
    assert run_codex(parse("--upstream", "http://127.0.0.1:9", "--codex-key-helper", "/synthetic/helper", "codex",
                           "exec", "hi"), launcher=fake, stderr=io.StringIO()) == 130
    assert "argv" not in fake.calls and fake.proxy.stopped and fake.calls["reported_after_stop"]


def test_tag_leading_user_prompts_stay_turn_starts():
    from contexto.codex import CONTEXT_SECTIONS_CODEX_VERSION
    assert CONTEXT_SECTIONS_CODEX_VERSION == "0.166.0"  # measured on 0.154.0, rechecked on 0.163.0 (D38), 0.166.0 (D56)
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


@pytest.mark.parametrize("depth", [MAX_JSON_DEPTH + 1, 70_000, 1_000_000])  # over the limit; json.loads
def test_call_arguments_nested_too_deep_for_json_render_as_their_string(depth):  # may parse 70,000, not 1,000,000
    deep = "[" * depth + "]" * depth
    item = {"type": "function_call", "call_id": "call_0", "name": "exec", "arguments": deep}
    assert RESPONSES.retained_blocks(item) == [("assistant", {"type": "tool_use", "name": "exec", "input": deep})]
    shallow = dict(item, arguments="[" * MAX_JSON_DEPTH + "]" * MAX_JSON_DEPTH)
    assert RESPONSES.retained_blocks(shallow)[0][1]["input"] != shallow["arguments"]  # at the limit: parsed
    items = history(8) + [prompt("new prompt")]
    items[items.index(call(0))] = item
    up, st, ev, store = run(items, config=Config(bound=6_000, headroom_floor=500, pin_budget=200, index_budget=2_000))
    assert "rotation" in names(ev) and st.k > 4
    page = next(p for p in store.pages.values() if item in p["messages"])
    assert render_page(page).count("[") >= depth  # the arguments, shown as the text they are


# D54: the key helper through a shell, no Anthropic default for Codex, --no-daemon, launch checks ----


def auth_of(launch):
    return tomllib.loads((launch.codex_home / "config.toml").read_text())["model_providers"]["contexto"]["auth"]


@pytest.mark.parametrize("helper", ['echo $SYNTH_KEY | tr -d x', 'cat ~/key.txt', 'cat "$HOME/key.txt" && true',
                                    "printf '%s' \"k\\\\e'y\" >/dev/null; echo key"])
def test_key_helper_runs_through_a_shell_like_claude_codes_api_key_helper(tmp_path, helper):
    """QA live-codex F1: the helper was split into words and run without a shell, so ``$VAR``, ``~`` and
    pipes reached the helper literally (Codex sent ``Bearer $SYNTH_KEY | tr -d x``)."""
    import subprocess

    (tmp_path / "key.txt").write_text("key\n")
    launch = plan_codex([], PROXY, "S", env={}, cwd=tmp_path, home=claude_home(tmp_path), key_helper=helper,
                        upstream="https://u.invalid/v1")
    try:
        auth = auth_of(launch)
        assert auth == {"command": "/bin/sh", "args": ["-c", helper]}  # the string survives TOML exactly
        out = subprocess.run([auth["command"], *auth["args"]], capture_output=True, text=True, timeout=10,
                             env={"HOME": str(tmp_path), "SYNTH_KEY": "kxeyx", "PATH": os.defpath})
        assert out.stdout.strip() == "key"  # what Claude Code's shell would print for the same helper
    finally:
        launch.cleanup()


def test_key_helper_strings_round_trip_through_the_config(tmp_path):
    helper = 'op read "op://v/it em/\\x" \t--x=\'q\'\n# 日本 \\u0041 é $(id) `id` [table] = "'
    launch = plan_codex([], PROXY, "S", env={}, cwd=tmp_path, home=tmp_path, key_helper=helper,
                        upstream="https://u.invalid/v1")
    try:
        assert auth_of(launch)["args"] == ["-c", helper]
    finally:
        launch.cleanup()
    with pytest.raises(CodexLaunchError, match="empty"):
        plan_codex([], PROXY, "S", env={}, cwd=tmp_path, home=tmp_path, key_helper="  \t",
                   upstream="https://u.invalid/v1")
    # TOML forbids a raw DEL, which JSON leaves raw (reviewer A, L1), and has no lone surrogates.
    launch = plan_codex([], PROXY, "S", env={}, cwd=tmp_path, home=tmp_path, key_helper="echo a\x7fb\x00c",
                        upstream="https://u.invalid/v1")
    try:
        assert "\x7f" not in (launch.codex_home / "config.toml").read_text()
        assert auth_of(launch)["args"] == ["-c", "echo a\x7fb\x00c"]
    finally:
        launch.cleanup()
    with pytest.raises(CodexLaunchError, match="surrogate"):
        plan_codex([], PROXY, "S", env={}, cwd=tmp_path, home=tmp_path, key_helper="echo \ud800",
                   upstream="https://u.invalid/v1")


@pytest.mark.parametrize("url", ["https://api.anthropic.com", "https://API.Anthropic.COM/", "https://anthropic.com.",
                                 "https://eu.api.anthropic.com:443/"])
def test_codex_does_not_derive_its_upstream_from_anthropics_api(url):
    """QA docs-audit 1: with no upstream set, an OpenAI key went to https://api.anthropic.com/v1."""
    from contexto.codex import codex_upstream_with_source

    with pytest.raises(CodexLaunchError, match=r"--codex-upstream https://api\.openai\.com/v1") as info:
        codex_upstream_with_source("apikey", None, {}, url, "default")
    assert "CLIProxyAPI" in str(info.value) and "(default)" in str(info.value)
    # The user's own choice and ChatGPT mode are not derived from Claude's upstream.
    assert codex_upstream_with_source("apikey", "https://api.openai.com/v1", {}, url, "default") == (
        "https://api.openai.com/v1", "codex-flag")
    assert codex_upstream_with_source("apikey", None, {"CONTEXTO_CODEX_UPSTREAM": "https://x.invalid/v1"}, url,
                                      "default")[1] == "codex-env"
    assert codex_upstream_with_source("chatgpt", None, {}, url, "default")[1] == "chatgpt-default"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8317", "https://cpa.example.invalid/",
                                 "https://anthropic.com.evil.invalid", "https://notanthropic.com"])
def test_codex_still_derives_a_cliproxyapi_upstream(url):
    from contexto.codex import codex_upstream_with_source

    assert codex_upstream_with_source("apikey", None, {}, url, "env") == (url.rstrip("/") + "/v1", "claude-env+/v1")


@pytest.mark.parametrize("codex_args,version,expected", [
    ([], "0.166.0", True), (["fix the bug"], "0.166.0", True), (["-m", "gpt-6-astra", "hi"], "0.166.0", True),
    (["resume", "--last"], "0.166.0", True), (["fork"], "0.167.0-alpha.3", True), ([], "0.156.0", True),
    ([], "0.155.0", False), ([], None, False), ([], "unparsed", False),
    (["exec", "hi"], "0.166.0", False), (["e", "hi"], "0.166.0", False), (["mcp", "list"], "0.166.0", False),
    (["--no-daemon"], "0.166.0", False), (["--remote", "ws://127.0.0.1:1"], "0.166.0", False),
    (["--remote=unix://"], "0.166.0", False),
    # The subcommand is found after leading options (reviewer A, N1): codex refuses --no-daemon with agents.
    (["-m", "x", "agents"], "0.166.0", False), (["-c", "k=1", "--search", "exec", "hi"], "0.166.0", False),
    (["--image=a.png", "exec"], "0.166.0", False), (["-ia.png", "exec"], "0.166.0", False),
    (["-m", "x", "resume", "--last"], "0.166.0", True), (["--", "exec"], "0.166.0", True),
    (["-i", "a.png", "exec"], "0.166.0", True),  # -i takes "exec" as a second image and opens the TUI
    (["-i", "a.png", "--search", "exec"], "0.166.0", False)])
def test_no_daemon_only_for_interactive_launches_that_take_it(codex_args, version, expected):
    from contexto.codex import takes_no_daemon

    assert takes_no_daemon(codex_args, version) is expected


def test_an_unreadable_version_falls_back_to_codex_help(codex_bin):
    from contexto.codex import help_lists_no_daemon, takes_no_daemon

    assert takes_no_daemon([], None, help_lists_flag=True) and not takes_no_daemon([], None)
    assert not takes_no_daemon(["exec", "hi"], None, help_lists_flag=True)
    assert not help_lists_no_daemon(codex_bin)
    os.environ["STUB_CODEX_HELP"] = "Options:\n      --no-daemon\n          Run without the shared background server"
    try:
        assert help_lists_no_daemon(codex_bin)
    finally:
        del os.environ["STUB_CODEX_HELP"]
    assert not help_lists_no_daemon("/nonexistent/codex")


def test_plans_put_no_daemon_right_after_codex(tmp_path):
    launch = plan_codex(["resume", "--last"], PROXY, "S", env={}, cwd=tmp_path, home=claude_home(tmp_path),
                        upstream="https://u.invalid/v1", no_daemon=True)
    try:
        assert launch.argv == ["codex", "--no-daemon", "resume", "--last"]
    finally:
        launch.cleanup()
    chatgpt = plan_codex(["hi"], PROXY, "S", auth_mode=CHATGPT_MODE, env={}, cwd=tmp_path, home=tmp_path,
                         no_daemon=True)
    assert chatgpt.argv[:3] == ["codex", "--no-daemon", "-c"] and chatgpt.argv[-1] == "hi"
    exec_plan = plan_codex(["exec", "hi"], PROXY, "S", env={}, cwd=tmp_path, home=claude_home(tmp_path),
                           upstream="https://u.invalid/v1")
    try:
        assert exec_plan.argv == ["codex", "exec", "hi"]
    finally:
        exec_plan.cleanup()


def test_cleanup_drops_codexs_release_copy_but_keeps_rollouts_and_the_plugin_clone(tmp_path):
    """QA verify-codex-orphans: each kept home held packages/ (417 MB) after exit. The plugin clone in
    .tmp/ stays, so a resumed session does not download it again (reviewer A, PR #236)."""
    home_dir = tmp_path / "ctx" / "sessions" / "S4" / "codex-home"
    launch = plan_codex([], PROXY, "S4", env={}, cwd=tmp_path, home=claude_home(tmp_path),
                        upstream="https://u.invalid/v1", codex_home_dir=home_dir)
    for name in ("packages/app-server-daemon/releases/0.166.0/bin", ".tmp/plugins/.git", "sessions/2026/11/05"):
        (home_dir / name).mkdir(parents=True)
        (home_dir / name / "f").write_text("x")
    (home_dir / "state_5.sqlite").write_text("x")
    launch.cleanup()
    assert sorted(p.name for p in home_dir.iterdir()) == [".tmp", "sessions", "state_5.sqlite"]
    assert (home_dir / ".tmp" / "plugins" / ".git" / "f").exists()
    assert (home_dir / "sessions" / "2026" / "11" / "05" / "f").exists()


def session_names(ctx):
    sessions = ctx / "sessions"
    return sorted(p.name for p in sessions.iterdir()) if sessions.exists() else []


def test_run_codex_without_codex_on_path_says_so_before_making_a_session(tmp_path, monkeypatch):
    """QA install 2 and 3: with no codex, run codex blamed a missing key helper and left a session."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.delenv(KEY_HELPER_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    err = io.StringIO()
    assert run_codex(parse("--upstream", "http://127.0.0.1:9", "codex"), stderr=err) == 127
    assert err.getvalue() == "contexto: 'codex' not found on PATH\n"
    assert session_names(tmp_path / "ctx") == []


def test_run_codex_failures_leave_no_empty_session(tmp_path, monkeypatch, codex_bin):
    from contexto import launcher

    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.delenv(KEY_HELPER_ENV, raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("CONTEXTO_CODEX_UPSTREAM", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launcher, "run_child", lambda *a, **k: pytest.fail("codex must not start"))
    err = io.StringIO()  # no upstream anywhere: Claude Code's default, Anthropic's API
    assert run_codex(parse("--codex-key-helper", "echo k", "codex"), stderr=err) == 2
    assert "--codex-upstream https://api.openai.com/v1" in err.getvalue()
    err = io.StringIO()  # no key helper: found only after the session and proxy exist
    assert run_codex(parse("--upstream", "http://127.0.0.1:9", "codex", "exec", "hi"), stderr=err) == 2
    assert "no key helper" in err.getvalue()
    assert session_names(tmp_path / "ctx") == []
    (tmp_path / "ctx" / "sessions" / "kept").mkdir(parents=True)  # a session the user resumes is never removed
    assert run_codex(parse("--session", "kept", "--upstream", "http://127.0.0.1:9", "codex"), stderr=err) == 2
    assert session_names(tmp_path / "ctx") == ["kept"]


def log_events(ctx, session_id):
    path = ctx / "sessions" / session_id / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_run_codex_logs_launch_and_exit_and_warns_early_like_claude(tmp_path, monkeypatch, codex_bin):
    """QA launcher-ux nits: no launch or exit event, no bound in the session line, and the start-time
    window warning only for Claude."""
    from contexto import launcher

    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    seen = {}

    def fake_child(argv, env=None, *, stderr=None):
        seen.update(argv=argv, err=stderr.getvalue())
        return 5

    monkeypatch.setattr(launcher, "run_child", fake_child)
    err = io.StringIO()
    args = parse("--session", "W1", "--bound", "40000", "--upstream", "http://127.0.0.1:9",
                 "--codex-key-helper", "echo k", "codex")
    args.no_version_check = True
    assert run_codex(args, stderr=err) == 5
    assert ("contexto: session W1 (bound 40,000, upstream claude-flag+/v1, codex auth apikey, engine rewrite)"
            in seen["err"])
    assert "window budget about" in seen["err"] and "Consider --bound" in seen["err"]  # before codex starts
    assert seen["argv"] == [codex_bin, "--no-daemon"]  # interactive, on the stub's 0.166.0
    events = log_events(tmp_path / "ctx", "W1")
    launch = next(e for e in events if e["event"] == "launch")
    assert launch["cli"] == "codex" and launch["no_daemon"] is True and launch["config"] == {"bound": 40000}
    assert launch["upstream_source"] == "claude-flag+/v1" and launch["codex_auth"] == "apikey"
    assert any(e["event"] == "window_budget_warning" for e in events)
    assert events[-1]["event"] == "exit" and events[-1]["code"] == 5
    monkeypatch.setenv("STUB_CODEX_VERSION", "0.155.0")  # before --no-daemon existed: argv as typed
    args = parse("--session", "W2", "--upstream", "http://127.0.0.1:9", "--codex-key-helper", "echo k", "codex")
    args.no_version_check = True
    err = io.StringIO()
    assert run_codex(args, stderr=err) == 5
    assert seen["argv"] == [codex_bin] and "window budget" not in err.getvalue()
    assert "background app-server" not in err.getvalue()  # a known version too old for the flag: no warning
    assert next(e for e in log_events(tmp_path / "ctx", "W2") if e["event"] == "launch")["no_daemon"] is False


def test_run_codex_without_a_readable_version_asks_codex_help_then_warns(tmp_path, monkeypatch, codex_bin):
    """Reviewer A, L2: an unreadable ``codex --version`` silently left the daemon running."""
    from contexto import launcher

    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    monkeypatch.setenv("STUB_CODEX_NO_VERSION", "1")
    seen = {}
    monkeypatch.setattr(launcher, "run_child", lambda argv, env=None, *, stderr=None: seen.update(argv=argv) or 0)

    def launch(session):
        args = parse("--session", session, "--upstream", "http://127.0.0.1:9", "--codex-key-helper", "echo k",
                     "codex")
        args.no_version_check = True
        err = io.StringIO()
        assert run_codex(args, stderr=err) == 0
        event = next(e for e in log_events(tmp_path / "ctx", session) if e["event"] == "launch")
        return err.getvalue(), event

    err, event = launch("H1")  # help does not list the flag: not passed, one warning
    assert seen["argv"] == [codex_bin] and err.count("may leave a background app-server") == 1
    assert event["no_daemon"] is False and event["codex_version_error"] == "exit_1"
    monkeypatch.setenv("STUB_CODEX_HELP", "Options:\n      --no-daemon\n          Run without the shared server")
    err, event = launch("H2")  # help lists it: passed, no warning
    assert seen["argv"] == [codex_bin, "--no-daemon"] and "background app-server" not in err
    assert event["no_daemon"] is True and event["codex_version_error"] == "exit_1"


@pytest.mark.parametrize("codex_args,refused", [
    (["agents"], True), (["-m", "x", "agents"], True), (["agents", "--remote", "ws://127.0.0.1:1"], False),
    (["--remote=unix://", "agents"], False), (["agents is a word in my prompt"], False), (["--", "agents"], False),
    (["exec", "agents"], False), ([], False)])
def test_codex_agents_is_refused_because_it_needs_the_shared_server(codex_args, refused):
    """Reviewer B, PR #236: ``codex agents`` (0.166.0) starts the shared server pair in the session's
    CODEX_HOME, leaves it running, and refuses --no-daemon."""
    from contexto.codex import needs_shared_server

    assert needs_shared_server(codex_args) == ("agents" if refused else None)


def test_run_codex_agents_exits_2_before_making_a_session(tmp_path, monkeypatch, codex_bin):
    from contexto import launcher

    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "ctx"))
    monkeypatch.setattr(launcher, "run_child", lambda *a, **k: pytest.fail("codex must not start"))
    err = io.StringIO()
    args = parse("--upstream", "http://127.0.0.1:9", "--codex-key-helper", "echo k", "codex", "-m", "x", "agents")
    assert run_codex(args, stderr=err) == 2
    assert "'codex agents' needs Codex's shared background server" in err.getvalue()
    assert session_names(tmp_path / "ctx") == []
