"""``contexto run claude`` (M2 lane A packet A3) with a fake ``claude`` executable. No live model."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import launcher
from contexto.launcher import (
    ASSUMED_OVERHEAD_DEFAULT,
    ASSUMED_OVERHEAD_OPENRIG,
    BOUND_GUARD,
    EXIT_USAGE,
    LaunchError,
    LaunchOptions,
    NoticeLog,
    assumed_overhead,
    check_bound,
    check_user_args,
    claude_argv,
    contexto_tool_rules,
    engine_config,
    exit_code,
    mcp_payload,
    open_session,
    settings_payload,
    start_warning,
    write_private_json,
)
from contexto.mcp import TOOLS as MCP_TOOLS
from contexto.paths import contexto_home

FAKE_CLAUDE = r'''
import json, os, signal, sys, time, urllib.request

args = sys.argv[1:]
if args == ["--version"]:  # contexto run's version check (D38)
    if os.environ.get("FAKE_VERSION_HANG"):
        open(os.environ["FAKE_VERSION_HANG"], "w").close()  # the marker: --version is running
        time.sleep(30)
    print(os.environ.get("FAKE_VERSION", "2.1.302 (Claude Code)"))
    sys.exit(0)
record = {"argv": args, "pid": os.getpid(), "env_base_url": os.environ.get("ANTHROPIC_BASE_URL")}
for flag, key in (("--settings=", "settings"), ("--mcp-config=", "mcp")):
    path = next(a for a in args if a.startswith(flag))[len(flag):]  # the launcher's one-token form
    record[key + "_path"] = path
    record[key] = json.load(open(path))
    record[key + "_mode"] = os.stat(path).st_mode & 0o777
    record[key + "_dir_mode"] = os.stat(os.path.dirname(path)).st_mode & 0o777
if os.environ.get("FAKE_REQUEST"):
    base = record["settings"]["env"]["ANTHROPIC_BASE_URL"]
    payload = {"model": "claude-haiku-5-5", "max_tokens": 16,
               "messages": [{"role": "user", "content": [{"type": "text", "text": "synthetic"}]}]}
    if os.environ.get("FAKE_TOOLS"):  # the main conversation's tool list, as Claude Code sends it
        payload["tools"] = [{"name": n, "input_schema": {"type": "object"}}
                            for n in os.environ["FAKE_TOOLS"].split(",")]
    if os.environ.get("FAKE_DENIED"):  # Claude Code 2.1.302 refused this tool: no permission rule allowed it
        name = os.environ["FAKE_DENIED"]
        payload["messages"] += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True,
             "content": f"Claude requested permissions to use {name}, but you haven't granted it yet."}]}]
    body = json.dumps(payload).encode()
    request = urllib.request.Request(base + "/v1/messages", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "x-api-key": "fake-key"})
    with urllib.request.urlopen(request, timeout=10) as response:
        record["response_status"] = response.status
        response.read()  # to the end, as Claude Code does: the proxy logs the entry before the last bytes
# Write atomically: the tests wait for this file to appear and then read it at once.
with open(os.environ["FAKE_RECORD"] + ".tmp", "w") as f:
    json.dump(record, f)
os.replace(os.environ["FAKE_RECORD"] + ".tmp", os.environ["FAKE_RECORD"])
if os.environ.get("FAKE_SLEEP"):
    try:
        time.sleep(float(os.environ["FAKE_SLEEP"]))
    except KeyboardInterrupt:
        sys.exit(130)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[tuple[str, str, str | None]] = []
    body_delay = 0.0

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Upstream.seen.append((self.command, self.path, self.headers.get("x-api-key")))
        body = json.dumps({"type": "message", "content": [],
                           "usage": {"input_tokens": 5, "output_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if Upstream.body_delay:
            # Headers first, the body later: the proxy has relayed the status before it can log the
            # request, which is the window a client that stops at the status falls into.
            self.wfile.flush()
            time.sleep(Upstream.body_delay)
        self.wfile.write(body)


@pytest.fixture
def upstream():
    Upstream.seen = []
    Upstream.body_delay = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    fake.chmod(0o755)
    home = tmp_path / "userhome"
    home.mkdir()
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE", "OPENRIG_", "RIGGED_"))}
    clean.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", HOME=str(home),
                 CONTEXTO_HOME=str(tmp_path / "contexto"), FAKE_RECORD=str(tmp_path / "record.json"))
    return clean


def launch(env, *args, cwd, **popen):
    return subprocess.Popen([sys.executable, "-m", "contexto", "run", *args], env=env, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **popen)


def session_log(env, session) -> list[dict]:
    path = Path(env["CONTEXTO_HOME"]) / "sessions" / session / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)


def test_run_wires_settings_mcp_and_args_and_cleans_up(env, upstream, tmp_path):
    env["FAKE_REQUEST"] = "1"
    proc = launch(env, "--upstream", upstream, "--session", "s1", "claude", "-p", "hello", "--model", "m",
                  cwd=tmp_path)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    record = json.loads(Path(env["FAKE_RECORD"]).read_text())
    argv = record["argv"]
    assert argv[0] == "--settings=" + record["settings_path"] and argv[1] == "--mcp-config=" + record["mcp_path"]
    assert argv[2:] == ["-p", "hello", "--model", "m"]
    base = record["settings"]["env"]["ANTHROPIC_BASE_URL"]
    assert record["settings"] == {"env": {"ANTHROPIC_BASE_URL": base}, "permissions": {"allow": contexto_tool_rules()}}
    assert base.startswith("http://127.0.0.1:")
    server = record["mcp"]["mcpServers"]["contexto"]
    assert server["command"] == sys.executable and server["args"] == ["-P", "-m", "contexto", "mcp", "--session", "s1"]
    assert server["env"] == {"CONTEXTO_HOME": env["CONTEXTO_HOME"]}
    assert record["settings_mode"] == record["mcp_mode"] == 0o600 and record["settings_dir_mode"] == 0o700
    assert record["env_base_url"] is None  # the base URL travels only in the --settings file
    assert not Path(record["settings_path"]).exists() and not Path(record["mcp_path"]).parent.exists()
    assert record["response_status"] == 200 and Upstream.seen == [("POST", "/v1/messages", "fake-key")]
    assert "contexto: session s1" in err
    meta = json.loads((Path(env["CONTEXTO_HOME"]) / "sessions" / "s1" / "meta.json").read_text())
    assert meta["workdir"] == str(tmp_path.resolve())  # the directory claude ran in
    events = session_log(env, "s1")
    kinds = [e["event"] for e in events]
    assert kinds[:2] == ["proxy_start", "launch"] and kinds[-2:] == ["proxy_stop", "exit"]
    launch_event = next(e for e in events if e["event"] == "launch")
    assert launch_event["upstream_source"] == "flag" and launch_event["config"] == {"bound": 150_000}
    assert launch_event["user_arg_count"] == 4 and launch_event["engine"] == "rewrite"  # A2 is on main
    assert launch_event["affinity_header"] is True  # a local upstream is not a vendor host (D36)
    assert not any(e["event"] == "engine_unavailable" for e in events)
    request = next(e for e in events if e["event"] == "request")
    assert request["path"] == "/v1/messages" and request["status"] == 200
    assert request["transform_events"] and all("epoch" in e for e in request["transform_events"])
    log_text = json.dumps(events)
    assert "fake-key" not in log_text and "hello" not in log_text and upstream not in log_text


def test_request_entry_is_logged_before_the_cli_sees_the_response_end(env, upstream, tmp_path):
    """The flake in test_run_wires_settings_mcp_and_args_and_cleans_up (StopIteration, CI on PR #99): the
    fake claude read only the status, so it could exit, and the launcher stop the proxy, while the proxy
    was still relaying the body and had not logged the request. The proxy logs before the last bytes,
    so a client that reads to the end, as Claude Code does, always finds the entry. A slow upstream body
    makes that window wide: without the fake's read this fails every time."""
    env["FAKE_REQUEST"] = "1"
    Upstream.body_delay = 0.5
    proc = launch(env, "--upstream", upstream, "--session", "s9", "claude", "-p", "hello", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    kinds = [e["event"] for e in session_log(env, "s9")]
    assert "request" in kinds and kinds.index("request") < kinds.index("proxy_stop")


def test_untested_claude_version_gets_one_warning_line_and_the_launch_goes_on(env, upstream, tmp_path):
    # D38: claude --version at start; an untested version is one stderr line, logged, never a refusal.
    env["FAKE_VERSION"] = "2.1.400 (Claude Code)"
    env.pop("CONTEXTO_NO_VERSION_CHECK", None)
    proc = launch(env, "--upstream", upstream, "--session", "v1", "claude", "-p", "hi", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    warnings = [line for line in err.splitlines() if "2.1.400" in line]
    assert len(warnings) == 1 and warnings[0].startswith("contexto: warning: Claude Code 2.1.400 ")
    assert "tested: 2.1.302" in warnings[0]
    assert err.index("contexto: session v1") < err.index(warnings[0])
    assert Path(env["FAKE_RECORD"]).exists()  # claude ran
    check = [e for e in session_log(env, "v1") if e["event"] == "cli_version_check"]
    assert check == [dict(check[0], cli="claude", version="2.1.400", tested=False)]


@pytest.mark.parametrize("how", ["flag", "env"])
def test_version_check_is_silenced_by_the_flag_or_the_env(env, upstream, tmp_path, how):
    env["FAKE_VERSION"] = "2.1.400 (Claude Code)"
    flags = ["--no-version-check"] if how == "flag" else []
    if how == "env":
        env["CONTEXTO_NO_VERSION_CHECK"] = "1"
    proc = launch(env, "--upstream", upstream, "--session", "v2", *flags, "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert "2.1.400" not in err and "warning" not in err
    check = [e for e in session_log(env, "v2") if e["event"] == "cli_version_check"]
    assert check == [{**check[0], "cli": "claude", "skipped": True}]


def test_ctrl_c_during_the_version_check_exits_130_without_a_traceback(env, upstream, tmp_path):
    # D38: claude --version hangs; Ctrl+C reaches the launcher (the check runs --version in its own
    # process group, so the hanging command does not get it). The launcher stops the proxy, logs the
    # exit, and never starts claude.
    marker = tmp_path / "version-running"
    env["FAKE_VERSION_HANG"] = str(marker)
    env.pop("CONTEXTO_NO_VERSION_CHECK", None)
    proc = launch(env, "--upstream", upstream, "--session", "v4", "claude", cwd=tmp_path)
    assert proc.stderr is not None
    line = proc.stderr.readline()
    assert "contexto: session v4" in line, line
    wait_for(marker)  # inside the version check: its 3 s window has just started
    proc.send_signal(signal.SIGINT)
    rest = proc.communicate(timeout=30)[1]
    assert proc.returncode == 130, rest
    assert "Traceback" not in rest and "KeyboardInterrupt" not in rest and "contexto: interrupted" in rest
    assert not Path(env["FAKE_RECORD"]).exists()  # claude never ran
    events = session_log(env, "v4")
    assert [e for e in events if e["event"] == "cli_version_check"][0]["error"] == "interrupted"
    assert events[-1]["event"] == "exit" and events[-1]["code"] == 130
    assert "proxy_stop" in [e["event"] for e in events]


def test_tested_claude_version_prints_nothing(env, upstream, tmp_path):
    env.pop("CONTEXTO_NO_VERSION_CHECK", None)
    proc = launch(env, "--upstream", upstream, "--session", "v3", "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert "warning" not in err
    check = [e for e in session_log(env, "v3") if e["event"] == "cli_version_check"]
    assert len(check) == 1 and check[0]["version"] == "2.1.302" and check[0]["tested"] is True


def test_exit_code_is_propagated(env, upstream, tmp_path):
    env["FAKE_EXIT"] = "7"
    proc = launch(env, "--upstream", upstream, "--session", "s2", "claude", cwd=tmp_path)
    proc.communicate(timeout=30)
    assert proc.returncode == 7
    assert session_log(env, "s2")[-1] == dict(session_log(env, "s2")[-1], event="exit", code=7)


def test_upstream_is_resolved_from_settings_before_the_proxy_starts(env, upstream, tmp_path):
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": upstream}}))
    env["FAKE_REQUEST"] = "1"
    proc = launch(env, "--session", "s3", "claude", cwd=project)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert Upstream.seen == [("POST", "/v1/messages", "fake-key")]
    launch_event = next(e for e in session_log(env, "s3") if e["event"] == "launch")
    assert launch_event["upstream_source"] == "settings:local"


@pytest.mark.parametrize("config_dir", ["~nosuchuser-contexto-test/cfg", "~/cfg", " "])
def test_an_odd_claude_config_dir_does_not_stop_the_launch(env, upstream, tmp_path, config_dir):
    """reviewer-A, round 2: an unknown ~user in CLAUDE_CONFIG_DIR raised out of `contexto run`."""
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env["FAKE_REQUEST"] = "1"
    env["ANTHROPIC_BASE_URL"] = upstream  # no settings file sets one, so the environment is used
    proc = launch(env, "--session", "s3c", "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0 and "Traceback" not in err, err
    launch_event = next(e for e in session_log(env, "s3c") if e["event"] == "launch")
    assert launch_event["upstream_source"] == "env"


def test_sigterm_is_forwarded_to_claude_and_everything_is_cleaned_up(env, upstream, tmp_path):
    env["FAKE_SLEEP"] = "30"
    proc = launch(env, "--upstream", upstream, "--session", "s4", "claude", cwd=tmp_path)
    wait_for(Path(env["FAKE_RECORD"]))
    record = json.loads(Path(env["FAKE_RECORD"]).read_text())
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    assert proc.returncode == 128 + signal.SIGTERM
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)
    assert not Path(record["settings_path"]).parent.exists()
    assert session_log(env, "s4")[-1]["event"] == "exit"


def test_ctrl_c_reaches_claude_and_does_not_kill_the_launcher(env, upstream, tmp_path):
    # The fake sleeps far past the deadline below, so only the interrupt can end it in time.
    env["FAKE_SLEEP"] = "300"
    proc = launch(env, "--upstream", upstream, "--session", "s5", "claude", cwd=tmp_path, start_new_session=True)
    wait_for(Path(env["FAKE_RECORD"]), timeout=60)
    os.killpg(proc.pid, signal.SIGINT)  # what a terminal does on Ctrl+C: the whole foreground group
    try:
        proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
    assert proc.returncode == 130  # claude's own exit code after handling the interrupt
    events = session_log(env, "s5")
    assert [e["event"] for e in events][-2:] == ["proxy_stop", "exit"] and events[-1]["code"] == 130


@pytest.mark.parametrize("args, code, message", [
    (["--bound", str(BOUND_GUARD + 1)], EXIT_USAGE, "--allow-large-bound"),
    (["--bound", "0"], EXIT_USAGE, "positive"),
    (["--window-fraction", "1.5"], EXIT_USAGE, "--window-fraction"),
    (["--bound", "100000", "--headroom-floor", "100000"], EXIT_USAGE, "--headroom-floor"),
])
def test_usage_errors_exit_2_without_running_claude(env, upstream, tmp_path, args, code, message):
    proc = launch(env, "--upstream", upstream, *args, "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == code and message in err
    assert not Path(env["FAKE_RECORD"]).exists()


def test_large_bound_is_allowed_with_the_flag(env, upstream, tmp_path):
    proc = launch(env, "--upstream", upstream, "--bound", "300000", "--allow-large-bound", "--session", "s6",
                  "claude", cwd=tmp_path)
    proc.communicate(timeout=30)
    assert proc.returncode == 0
    launch_event = next(e for e in session_log(env, "s6") if e["event"] == "launch")
    assert launch_event["config"] == {"bound": 300_000}


def test_missing_claude_exits_127(env, upstream, tmp_path):
    env["PATH"] = "/usr/bin:/bin"
    proc = launch(env, "--upstream", upstream, "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 127 and "not found" in err


# Unit level


def test_check_bound():
    check_bound(BOUND_GUARD, False)
    check_bound(BOUND_GUARD + 1, True)
    with pytest.raises(LaunchError):
        check_bound(BOUND_GUARD + 1, False)


def test_engine_config_passes_only_set_values():
    assert engine_config(LaunchOptions()) == {"bound": 150_000}
    assert engine_config(LaunchOptions(bound=100_000, window_fraction=0.4, headroom_floor=10_000)) == {
        "bound": 100_000, "window_fraction": 0.4, "headroom_floor": 10_000}


def test_payloads_and_private_files(tmp_path):
    assert settings_payload("http://127.0.0.1:1") == {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1"},
                                                      "permissions": {"allow": contexto_tool_rules()}}
    assert mcp_payload("s", tmp_path)["mcpServers"]["contexto"]["args"][-2:] == ["--session", "s"]
    path = write_private_json(tmp_path, "x.json", {"a": 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and json.loads(path.read_text()) == {"a": 1}
    with pytest.raises(FileExistsError):
        write_private_json(tmp_path, "x.json", {})


def test_settings_allow_exactly_contexto_s_own_tools():
    # D44: Claude Code 2.1.302 refuses an MCP tool in -p mode unless a rule allows it. The rules name each
    # tool the server serves, under the server name the --mcp-config gives it, and nothing else: no
    # server-wide or wildcard rule, no deny or ask list, no mode change.
    rules = contexto_tool_rules()
    assert rules == ["mcp__contexto__contexto_recall", "mcp__contexto__contexto_pin", "mcp__contexto__contexto_close",
                     "mcp__contexto__contexto_state", "mcp__contexto__contexto_search"]
    assert rules == [f"mcp__contexto__{tool['name']}" for tool in MCP_TOOLS]
    assert list(mcp_payload("s", Path("/h"))["mcpServers"]) == ["contexto"]
    assert all("*" not in rule and "(" not in rule for rule in rules)
    payload = settings_payload("http://127.0.0.1:1")
    assert set(payload) == {"env", "permissions"} and set(payload["permissions"]) == {"allow"}
    assert set(payload["env"]) == {"ANTHROPIC_BASE_URL"}


def test_exit_code_maps_signals():
    assert exit_code(0) == 0 and exit_code(3) == 3 and exit_code(-signal.SIGTERM) == 128 + signal.SIGTERM


def test_start_warning_depends_on_bound_and_assumed_overhead(env, upstream, tmp_path):
    assert start_warning(150_000, None, ASSUMED_OVERHEAD_DEFAULT) is None
    assert start_warning(150_000, None, ASSUMED_OVERHEAD_OPENRIG) is None
    warning = start_warning(100_000, None, ASSUMED_OVERHEAD_DEFAULT)
    assert "below 25,000" in warning and "--bound 130,000" in warning
    assert assumed_overhead({}) == ASSUMED_OVERHEAD_DEFAULT
    assert assumed_overhead({"OPENRIG_SESSION_NAME": "x"}) == ASSUMED_OVERHEAD_OPENRIG
    quiet = launch(env, "--upstream", upstream, "--session", "w1", "claude", cwd=tmp_path)
    _, err = quiet.communicate(timeout=30)
    assert err.count("contexto:") == 1  # only the session line
    noisy = launch(env, "--upstream", upstream, "--bound", "100000", "--session", "w2", "claude", cwd=tmp_path)
    _, err = noisy.communicate(timeout=30)
    assert "window budget about 20,000" in err
    assert any(e["event"] == "window_budget_warning" for e in session_log(env, "w2"))


class FakeStore:
    def __init__(self, home, session_id):
        self.events, self.workdir = [], None

    def set_workdir(self, path):
        self.workdir = path

    def log(self, event):
        self.events.append(event)


def test_open_session_records_the_workdir(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "SessionStore", FakeStore)
    assert open_session(tmp_path, "s", tmp_path / "work").workdir == tmp_path / "work"


def test_notice_log_shows_engine_sizing_problems_once(tmp_path):
    import io

    store, err = FakeStore(tmp_path, "s"), io.StringIO()
    notices = NoticeLog(store, err)
    ok = {"event": "request", "transform_events": [{"event": "rotation", "fixed_overhead": 50_000,
                                                    "window_budget": 70_000}]}
    small = {"event": "request", "transform_events": [{"event": "rotation_noop", "fixed_overhead": 90_000,
                                                       "window_budget": 20_000}]}
    too_small = {"event": "request", "transform_events": [{"event": "bound_too_small", "fixed_overhead": 140_000,
                                                           "window_budget": -15_000, "bound": 150_000}]}
    for event in (ok, small, small, too_small, too_small):
        notices.log(event)
    lines = err.getvalue().splitlines()
    assert len(store.events) == 5 and len(lines) == 2
    assert "window budget 20,000" in lines[0] and "bound 150,000 is too small" in lines[1]
    # Since contract PR #50 the engine still rotates, to a window of only the new turn (rule 4).
    assert "keeps only the new turn" in lines[1] and "may exceed the bound" in lines[1]
    assert "unrotated" not in err.getvalue()


@pytest.mark.parametrize("event", [{"event": "bound_too_small"},
                                   {"event": "bound_too_small", "fixed_overhead": 140_000, "window_budget": -15_000}])
def test_bound_too_small_notice_without_full_sizing(tmp_path, event):
    import io

    store, err = FakeStore(tmp_path, "s"), io.StringIO()
    NoticeLog(store, err).log({"event": "request", "transform_events": [event]})
    line = err.getvalue().strip()
    assert "keeps only the new turn" in line and "may exceed the bound" in line and "unrotated" not in line


@pytest.mark.parametrize("user_args", [["--settings", "mine.json"], ["-p", "hi", "--settings={}"],
                                       ["--append-system-prompt", "--", "--settings", "mine.json", "-p", "x"]])
def test_user_settings_are_refused_before_anything_starts(env, upstream, tmp_path, user_args):
    proc = launch(env, "--upstream", upstream, "--session", "r1", "claude", *user_args, cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_USAGE and "--settings in the claude arguments is not supported" in err
    assert not Path(env["FAKE_RECORD"]).exists()  # claude never ran
    assert not (Path(env["CONTEXTO_HOME"]) / "sessions" / "r1").exists()  # no session, proxy, or temp files


def test_user_mcp_config_passes_through(env, upstream, tmp_path):
    mine = tmp_path / "my-mcp.json"
    mine.write_text(json.dumps({"mcpServers": {}}))
    proc = launch(env, "--upstream", upstream, "--session", "r2", "claude", "--mcp-config", str(mine), "-p", "x",
                  cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    argv = json.loads(Path(env["FAKE_RECORD"]).read_text())["argv"]
    assert argv[1].startswith("--mcp-config=") and argv[2:] == ["--mcp-config", str(mine), "-p", "x"]


@pytest.mark.parametrize("user_args", [["hello there"], ["hello", "-p"], [], ["--model", "m", "hello"]])
def test_user_arguments_follow_the_launcher_flags_unchanged(env, upstream, tmp_path, user_args):
    # A prompt right after `claude` must stay a prompt: in the two-token form `--mcp-config <file>`,
    # Claude Code's variadic --mcp-config took it as a second config file ("MCP config file not found").
    proc = launch(env, "--upstream", upstream, "--session", "r3", "claude", *user_args, cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    record = json.loads(Path(env["FAKE_RECORD"]).read_text())
    assert record["argv"] == ["--settings=" + record["settings_path"], "--mcp-config=" + record["mcp_path"],
                              *user_args]


def test_claude_argv_joins_each_flag_with_its_file():
    argv = claude_argv("/bin/claude", Path("/t/settings.json"), Path("/t/mcp.json"), ["a prompt", "-p"])
    assert argv == ["/bin/claude", "--settings=/t/settings.json", "--mcp-config=/t/mcp.json", "a prompt", "-p"]
    # No launcher flag is followed by a separate value token that a variadic option could extend.
    assert not any(arg in ("--settings", "--mcp-config") for arg in argv)


def test_check_user_args():
    check_user_args(["-p", "x", "--mcp-config", "a.json", "b.json"])
    check_user_args(["a prompt", "--mcp-config=a.json"])
    check_user_args(["-p", "--", "a prompt naming --settings"])  # only an argument that is the flag is refused
    for args in (["--settings", "f"], ["--settings=f"], ["-p", "x", "--settings", "f"], ["a prompt", "--settings={}"],
                 ["--settings="], ["--settings"],
                 # A -- can be another option's value; Claude Code 2.1.302 and 2.1.310 still read the
                 # --settings after it, so the check does not stop at --.
                 ["--append-system-prompt", "--", "--settings", "x.json"], ["-p", "--", "--settings=x.json"],
                 ["-p", "--", "--settings"]):
        with pytest.raises(LaunchError):
            check_user_args(args)


# Shared helpers for the codex path (contexto.codex, lane A packet A5) and the run dispatch


def test_options_from_args_reads_the_run_namespace():
    import argparse

    ns = argparse.Namespace(cli="codex", cli_args=["exec", "x"], bound=120_000, allow_large_bound=False,
                            window_fraction=0.4, headroom_floor=None, session="s", upstream=None,
                            observe_thinking=True)
    options = launcher.options_from_args(ns)
    assert (options.claude_args, options.bound, options.session_id, options.observe_thinking,
            options.window_fraction) == (["exec", "x"], 120_000, "s", True, 0.4)


def test_start_proxy_starts_on_loopback_and_rejects_a_bad_upstream(tmp_path):
    from contexto.store.memory import MemoryStore

    store = MemoryStore()
    proxy, engine = launcher.start_proxy(store, "http://127.0.0.1:9", "codex", {"bound": 150_000}, False)
    try:
        assert proxy.url.startswith("http://127.0.0.1:") and engine == "rewrite"
        assert store.events[0]["event"] == "proxy_start" and store.events[0]["upstream_source"] == "codex"
    finally:
        proxy.stop()
    with pytest.raises(LaunchError) as exc:
        launcher.start_proxy(MemoryStore(), "ftp://nope.invalid", "codex", {"bound": 150_000}, False)
    assert exc.value.code == EXIT_USAGE


def test_run_child_returns_exit_codes_and_127_when_missing(tmp_path):
    import io

    assert launcher.run_child([sys.executable, "-c", "raise SystemExit(5)"]) == 5
    assert launcher.run_child([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"]) == (
        128 + signal.SIGTERM)
    err = io.StringIO()
    assert launcher.run_child([str(tmp_path / "missing-binary")], stderr=err) == 127
    assert "cannot start" in err.getvalue()
    env = dict(os.environ, CONTEXTO_CHILD_PROBE="7")
    probe = "import os; raise SystemExit(int(os.environ['CONTEXTO_CHILD_PROBE']))"
    assert launcher.run_child([sys.executable, "-c", probe], env) == 7


def test_run_codex_without_the_codex_module_exits_2(monkeypatch, capsys):
    from contexto import cli

    monkeypatch.setitem(sys.modules, "contexto.codex", None)  # behaves as not installed
    assert cli.main(["run", "codex", "exec", "hi"]) == 2
    assert "codex support is not installed" in capsys.readouterr().err


def test_run_codex_dispatches_to_contexto_codex(monkeypatch):
    import types

    from contexto import cli

    seen = {}
    fake = types.ModuleType("contexto.codex")

    def add_arguments(parser):
        parser.add_argument("--codex-auth", default="apikey")

    def run_codex(args):
        seen.update(cli=args.cli, cli_args=args.cli_args, auth=args.codex_auth, bound=args.bound)
        return 9

    fake.add_arguments, fake.run_codex = add_arguments, run_codex
    monkeypatch.setitem(sys.modules, "contexto.codex", fake)
    assert cli.main(["run", "--bound", "120000", "--codex-auth", "chatgpt", "codex", "exec", "hi"]) == 9
    assert seen == {"cli": "codex", "cli_args": ["exec", "hi"], "auth": "chatgpt", "bound": 120_000}


def test_relative_contexto_home_gives_the_store_and_claude_mcp_server_the_same_absolute_path(monkeypatch, tmp_path):
    """Packet A18: the proxy's store and Claude's MCP server see one absolute home for a relative
    CONTEXTO_HOME (Codex's MCP server is covered in test_codex.py)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONTEXTO_HOME", "rel-home")
    home = contexto_home()
    store = open_session(home, "s-rel", tmp_path)
    assert store.dir == tmp_path / "rel-home" / "sessions" / "s-rel"
    env = mcp_payload("s-rel", home)["mcpServers"]["contexto"]["env"]
    assert env == {"CONTEXTO_HOME": str(tmp_path / "rel-home")}


def test_claude_without_contexto_tools_gets_one_warning_at_exit(env, upstream, tmp_path):
    """The failure neither CLI reports: the MCP server did not start, so Claude Code's main conversation
    has its built-in tools and no contexto tool. A run shorter than the check's grace is judged at exit."""
    env.update(FAKE_REQUEST="1", FAKE_TOOLS="Bash,Read,Edit")
    proc = launch(env, "--upstream", upstream, "--session", "t1", "claude", "-p", "hello", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    warnings = [line for line in err.splitlines() if "warning" in line]
    assert len(warnings) == 1 and "Claude Code is running without Contexto's tools" in warnings[0]
    assert "MCP server did not start" in warnings[0] and "README" in warnings[0]
    events = session_log(env, "t1")
    [missing] = [e for e in events if e["event"] == "mcp_tools_missing"]
    assert missing == {"ts": missing["ts"], "event": "mcp_tools_missing", "dialect": "anthropic", "at": "exit",
                       "server_listed": False, "requests": 1, "elapsed_s": missing["elapsed_s"],
                       "repeated": False}
    kinds = [e["event"] for e in events]
    assert kinds.index("proxy_stop") < kinds.index("mcp_tools_missing") < kinds.index("exit")


def test_claude_with_contexto_tools_gets_no_warning(env, upstream, tmp_path):
    env.update(FAKE_REQUEST="1", FAKE_TOOLS="Bash,Read,mcp__contexto__contexto_search,mcp__contexto__contexto_recall")
    proc = launch(env, "--upstream", upstream, "--session", "t2", "claude", "-p", "hello", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert "warning" not in err and "Contexto's tools" not in err
    assert not [e for e in session_log(env, "t2") if str(e.get("event")).startswith("mcp_tools")]


def test_claude_refusing_contexto_tools_gets_one_warning(env, upstream, tmp_path):
    """D44: the tools are offered but Claude Code refuses them for lack of a permission rule (what 2.1.302
    did in -p mode before the launcher's --settings allowed them; a user's 'ask' rule still does it)."""
    env.update(FAKE_REQUEST="1", FAKE_TOOLS="Bash,mcp__contexto__contexto_search,mcp__contexto__contexto_recall",
               FAKE_DENIED="mcp__contexto__contexto_recall")
    proc = launch(env, "--upstream", upstream, "--session", "t3", "claude", "-p", "hello", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    warnings = [line for line in err.splitlines() if "warning" in line]
    assert len(warnings) == 1 and "Claude Code refused Contexto's contexto_recall" in warnings[0], err
    [denied] = [e for e in session_log(env, "t3") for e in [e, *e.get("transform_events", [])]
                if e.get("event") == "mcp_tools_denied"]
    assert denied["tools"] == ["contexto_recall"] and denied["main"] is True
